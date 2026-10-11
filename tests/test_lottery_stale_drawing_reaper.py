"""僵尸 drawing 回收测试。

背景：开奖认领(open→drawing)已提交、开奖完成前进程崩溃（SIGKILL/OOM/部署重启）
时，in-process 的 try/except 回滚够不着，轮次永久卡死在 drawing：
调度只扫 open、管理后台认领要求 open、取消要求 open，三条路全堵死。

修复：run_due_draws 开头调 reap_stale_drawing_rounds，把超过宽限期
（_STALE_DRAWING_GRACE=15min）仍为 drawing 的轮次打回 open；drawing_at IS NULL
视为僵尸（升级前遗留、无时间戳的行）。
"""
import hashlib
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")

from backend.database import Base  # noqa: F401 保持与其它测试相同的导入习惯
from backend import models
from backend import lottery
from backend.api import welfare_admin


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.LotteryRound.__table__.create(engine, checkfirst=True)
    models.LotteryRoundPrize.__table__.create(engine, checkfirst=True)
    models.LotteryRoundEntry.__table__.create(engine, checkfirst=True)
    models.LotteryRoundWinner.__table__.create(engine, checkfirst=True)
    models.LotteryBlacklist.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
    models.WelfareGrantLog.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username, tg_id):
    user = models.WebUser(username=username, password_hash="", telegram_id=tg_id)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = value
    else:
        db.add(models.SystemConfig(key=key, value=value))
    db.commit()


def _make_round(db, title="僵尸回收测试", chat_id=-100121, n_users=2, draw_at=None):
    rnd = lottery.create_round(
        db,
        title=title,
        chat_id=chat_id,
        prizes=[{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
        draw_at=draw_at,
    )
    rnd.seed = f"fixed-seed-stale-{chat_id}"
    rnd.seed_hash = hashlib.sha256(rnd.seed.encode()).hexdigest()
    db.commit()
    for i in range(n_users):
        user = _make_user(db, f"stale_u{i}_{rnd.id}", 810000 + i * 10 + rnd.id)
        res = lottery.join_round(db, rnd, user, user.telegram_id)
        assert res["ok"], res
    return rnd


def _crash_after_claim(db, rnd, drawing_at):
    """模拟崩溃：认领已提交，进程在开奖完成前死亡。"""
    db.execute(
        text("UPDATE lottery_rounds SET status='drawing', drawing_at=:da WHERE id=:id"),
        {"id": rnd.id, "da": drawing_at},
    )
    db.commit()


def test_reap_resets_stale_drawing_to_open(db):
    rnd = _make_round(db)
    _crash_after_claim(db, rnd, datetime.now() - timedelta(minutes=20))
    summary = {}
    reaped = lottery.reap_stale_drawing_rounds(db, summary)
    assert reaped == [rnd.id]
    assert summary["reaped_stale_drawing"] == [rnd.id]
    db.refresh(rnd)
    assert rnd.status == "open"
    assert rnd.drawing_at is None


def test_reap_ignores_fresh_drawing(db):
    # 别的 worker 刚刚认领（活进程正在开奖），不许偷
    rnd = _make_round(db)
    _crash_after_claim(db, rnd, datetime.now())
    reaped = lottery.reap_stale_drawing_rounds(db)
    assert reaped == []
    db.refresh(rnd)
    assert rnd.status == "drawing"


def test_reap_treats_null_drawing_at_as_stale(db):
    # 升级前遗留的僵尸行没有 drawing_at，必须能回收，否则老卡死永远修不好
    rnd = _make_round(db)
    db.execute(
        text("UPDATE lottery_rounds SET status='drawing', drawing_at=NULL WHERE id=:id"),
        {"id": rnd.id},
    )
    db.commit()
    reaped = lottery.reap_stale_drawing_rounds(db)
    assert reaped == [rnd.id]
    db.refresh(rnd)
    assert rnd.status == "open"


def test_run_due_draws_recovers_crashed_auto_round(db, monkeypatch):
    # 端到端：崩溃的自动轮次在下一个 tick 被回收→重新认领→开奖→发奖
    _set_config(db, "lottery_notify_winners", "0")
    monkeypatch.setattr(
        lottery, "fetch_drand_beacon", lambda: {"round": 1, "randomness": "cd" * 32}
    )
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))
    _crash_after_claim(db, rnd, datetime.now() - timedelta(minutes=20))

    result = lottery.run_due_draws(db)

    assert rnd.id in result["reaped_stale_drawing"]
    assert rnd.id in result["drawn"]
    assert result["errors"] == []
    db.refresh(rnd)
    assert rnd.status == "done"
    winners = (
        db.query(models.LotteryRoundWinner)
        .filter(models.LotteryRoundWinner.round_id == rnd.id)
        .all()
    )
    assert len(winners) == 1
    assert all(w.distributed for w in winners)
    assert (
        db.query(models.PointsLog)
        .filter(models.PointsLog.type == "lottery_win")
        .count()
        == 1
    )


def test_run_due_draws_recovers_crashed_manual_round(db, monkeypatch):
    # 手动轮次（draw_at=None）：回收后打回 open，管理后台可重试开奖
    monkeypatch.setattr(welfare_admin, "_audit", lambda *a, **k: None)
    _set_config(db, "lottery_notify_winners", "0")
    monkeypatch.setattr(
        lottery, "fetch_drand_beacon", lambda: {"round": 2, "randomness": "ef" * 32}
    )
    admin = _make_user(db, "admin_stale_retry", 999031)
    rnd = _make_round(db, draw_at=None)
    _crash_after_claim(db, rnd, datetime.now() - timedelta(minutes=20))

    result = lottery.run_due_draws(db)
    assert rnd.id in result["reaped_stale_drawing"]
    assert result["drawn"] == []  # 手动轮次不自动开奖
    db.refresh(rnd)
    assert rnd.status == "open"

    # 管理后台重试：走真实 endpoint，开奖成功
    res = welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert res["success"] is True
    db.refresh(rnd)
    assert rnd.status == "done"


def test_reaper_runs_even_when_auto_draw_disabled(db):
    # 解卡死不依赖自动开奖总开关
    _set_config(db, "lottery_auto_draw_enabled", "0")
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))
    _crash_after_claim(db, rnd, datetime.now() - timedelta(minutes=20))
    result = lottery.run_due_draws(db)
    assert rnd.id in result["reaped_stale_drawing"]
    assert result["drawn"] == []
    db.refresh(rnd)
    assert rnd.status == "open"


def test_manual_claim_writes_drawing_at(db, monkeypatch):
    # 管理后台认领 SQL 必须带上 drawing_at，否则回收器看不到时间戳
    monkeypatch.setattr(welfare_admin, "_audit", lambda *a, **k: None)
    _set_config(db, "lottery_notify_winners", "0")
    monkeypatch.setattr(
        lottery, "fetch_drand_beacon", lambda: {"round": 3, "randomness": "12" * 32}
    )
    admin = _make_user(db, "admin_drawing_at", 999032)
    rnd = _make_round(db)
    seen = {}
    real_draw = lottery.draw_round

    def spy_draw(db_, round_id):
        # draw_round 执行前读：此时 drawing_at 只能是认领 SQL 写进去的
        seen["drawing_at"] = db_.execute(
            text("SELECT drawing_at FROM lottery_rounds WHERE id=:id"),
            {"id": round_id},
        ).scalar()
        return real_draw(db_, round_id)

    monkeypatch.setattr(lottery, "draw_round", spy_draw)
    res = welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert res["success"] is True
    assert seen["drawing_at"] is not None

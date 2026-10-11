"""管理后台手动开奖失败恢复测试。

背景：自动开奖 run_due_draws 认领后失败会把状态打回 open（已有测试覆盖），
但管理后台手动开奖 draw_lottery_round 认领后没有任何 try/except——开奖中途抛异常
轮次永久卡死在 drawing（调度只扫 open、管理后台拒绝非 open 重试）。

本文件覆盖修复后的行为：
- draw 阶段抛异常 → 状态打回 open，管理后台可重试；
- distribute 阶段抛异常（draw 已提交 done）→ 状态保持 done，走 done+pending
  补发路径，不重开奖、不重复发奖。
"""
import hashlib
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
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


def _make_round(db, title="手动开奖失败测试", chat_id=-100115, n_users=2):
    rnd = lottery.create_round(
        db,
        title=title,
        chat_id=chat_id,
        prizes=[{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
        draw_at=None,
    )
    # 固定 seed，保证开奖确定性
    rnd.seed = "fixed-seed-manual-draw-fail"
    rnd.seed_hash = hashlib.sha256(rnd.seed.encode()).hexdigest()
    db.commit()
    for i in range(n_users):
        user = _make_user(db, f"mdraw_u{i}_{rnd.id}", 800000 + i * 10 + rnd.id)
        res = lottery.join_round(db, rnd, user, user.telegram_id)
        assert res["ok"], res
    return rnd


def _boom_draw(*args, **kwargs):
    raise RuntimeError("draw crashed")


def _boom_distribute(*args, **kwargs):
    raise RuntimeError("distribute crashed")


def test_manual_draw_failure_resets_to_open(db, monkeypatch):
    # draw 阶段抛异常：状态打回 open，管理后台可重试
    monkeypatch.setattr(welfare_admin, "_audit", lambda *a, **k: None)
    _set_config(db, "lottery_notify_winners", "0")
    admin = _make_user(db, "admin_mdraw_fail", 999011)
    rnd = _make_round(db)

    real_draw = lottery.draw_round
    monkeypatch.setattr(lottery, "draw_round", _boom_draw)
    with pytest.raises(HTTPException) as ei:
        welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert ei.value.status_code == 500
    db.refresh(rnd)
    assert rnd.status == "open"

    # 恢复真实实现后重试：路径打通，开奖成功
    monkeypatch.setattr(lottery, "draw_round", real_draw)
    res = welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert res["success"] is True
    db.refresh(rnd)
    assert rnd.status == "done"


def test_manual_draw_distribute_failure_keeps_done(db, monkeypatch):
    # distribute 阶段抛异常：draw 已提交 done，不许打回 open，
    # 走 done+pending 补发路径，不重开奖、不重复发奖
    monkeypatch.setattr(welfare_admin, "_audit", lambda *a, **k: None)
    _set_config(db, "lottery_notify_winners", "0")
    admin = _make_user(db, "admin_mdraw_dist", 999012)
    rnd = _make_round(db)
    user_ids = [
        row.user_id
        for row in db.query(models.LotteryRoundEntry.user_id)
        .filter(models.LotteryRoundEntry.round_id == rnd.id)
        .all()
    ]

    real_distribute = lottery.distribute_round
    monkeypatch.setattr(lottery, "distribute_round", _boom_distribute)
    with pytest.raises(HTTPException) as ei:
        welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert ei.value.status_code == 500
    db.refresh(rnd)
    # WHERE status='drawing' 匹配不到行：状态保持 done，不会触发重开奖
    assert rnd.status == "done"
    assert (
        db.query(models.PointsLog).filter(models.PointsLog.user_id.in_(user_ids)).count() == 0
    )

    # 恢复真实发奖后重试：走 done+pending 补发，只发一份
    monkeypatch.setattr(lottery, "distribute_round", real_distribute)
    res = welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert res.get("redistributed") is True
    assert res["distribute"]["distributed"] == 1
    db.refresh(rnd)
    assert rnd.status == "done"
    logs = db.query(models.PointsLog).filter(models.PointsLog.user_id.in_(user_ids)).all()
    assert len(logs) == 1

    # 再点一次：已全部发放，400 拒绝，不会重复发奖
    with pytest.raises(HTTPException) as ei2:
        welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert ei2.value.status_code == 400
    assert db.query(models.PointsLog).filter(models.PointsLog.user_id.in_(user_ids)).count() == 1


def test_manual_draw_failure_detail_matches_actual_state_draw(db, monkeypatch):
    """500 文案必须与实际状态一致：draw 阶段失败→打回 open，文案承诺可重试。"""
    monkeypatch.setattr(welfare_admin, "_audit", lambda *a, **k: None)
    _set_config(db, "lottery_notify_winners", "0")
    admin = _make_user(db, "admin_mdraw_detail1", 999013)
    rnd = _make_round(db, title="文案-draw失败")

    monkeypatch.setattr(lottery, "draw_round", _boom_draw)
    with pytest.raises(HTTPException) as ei:
        welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert ei.value.status_code == 500
    assert "已恢复为可重试状态" in ei.value.detail
    db.refresh(rnd)
    assert rnd.status == "open"


def test_manual_draw_failure_detail_matches_actual_state_distribute(db, monkeypatch):
    """500 文案必须与实际状态一致：distribute 失败（draw 已提交 done）→
    不打回 open，文案必须说明走补发路径，而不是谎称"已恢复为可重试状态"。"""
    monkeypatch.setattr(welfare_admin, "_audit", lambda *a, **k: None)
    _set_config(db, "lottery_notify_winners", "0")
    admin = _make_user(db, "admin_mdraw_detail2", 999014)
    rnd = _make_round(db, title="文案-distribute失败")

    monkeypatch.setattr(lottery, "distribute_round", _boom_distribute)
    with pytest.raises(HTTPException) as ei:
        welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert ei.value.status_code == 500
    assert "已恢复为可重试状态" not in ei.value.detail
    assert "补发" in ei.value.detail
    db.refresh(rnd)
    assert rnd.status == "done"


def test_manual_draw_reset_failure_detail_warns_manual_check(db, monkeypatch):
    """500 文案必须与实际状态一致：状态回滚本身失败→轮次卡在 drawing，
    文案必须提示人工检查，而不是谎称已恢复。"""
    monkeypatch.setattr(welfare_admin, "_audit", lambda *a, **k: None)
    _set_config(db, "lottery_notify_winners", "0")
    admin = _make_user(db, "admin_mdraw_detail3", 999015)
    rnd = _make_round(db, title="文案-回滚失败")

    real_execute = db.execute

    def boom_execute(stmt, *a, **k):
        if "SET status='open'" in str(stmt):
            raise RuntimeError("db down")
        return real_execute(stmt, *a, **k)

    monkeypatch.setattr(db, "execute", boom_execute)
    monkeypatch.setattr(lottery, "draw_round", _boom_draw)
    with pytest.raises(HTTPException) as ei:
        welfare_admin.draw_lottery_round(rnd.id, current_admin=admin, db=db)
    assert ei.value.status_code == 500
    assert "人工检查" in ei.value.detail
    assert "已恢复为可重试状态" not in ei.value.detail
    db.refresh(rnd)
    assert rnd.status == "drawing"


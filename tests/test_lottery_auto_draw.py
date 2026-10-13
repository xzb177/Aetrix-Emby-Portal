"""群抽奖自动开奖调度（backend.lottery 新增函数）隔离测试。"""
import hashlib
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base  # noqa: F401 保持与其它测试相同的导入习惯
from backend import models
from backend import lottery


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.LotteryRound.__table__.create(engine, checkfirst=True)
    models.LotteryRoundPrize.__table__.create(engine, checkfirst=True)
    models.LotteryRoundEntry.__table__.create(engine, checkfirst=True)
    models.LotteryRoundWinner.__table__.create(engine, checkfirst=True)
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


def _make_round(db, title="自动开奖测试", chat_id=-100111, draw_at=None, n_users=2):
    rnd = lottery.create_round(
        db,
        title=title,
        chat_id=chat_id,
        prizes=[{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
        draw_at=draw_at,
    )
    # 固定 seed，保证开奖确定性
    rnd.seed = "fixed-seed-auto-draw-test"
    rnd.seed_hash = hashlib.sha256(rnd.seed.encode()).hexdigest()
    db.commit()
    for i in range(n_users):
        user = _make_user(db, f"autodraw_u{i}_{rnd.id}", 700000 + i * 10 + rnd.id)
        res = lottery.join_round(db, rnd, user, user.telegram_id)
        assert res["ok"], res
    # 参与后 create/join 已 commit；draw_at 在 create 时传入
    return rnd


def test_config_defaults(db):
    assert lottery.auto_draw_enabled(db) is True
    assert lottery.notify_winners_enabled(db) is True
    assert lottery.draw_interval_sec(db) == 60


def test_config_disabled(db):
    _set_config(db, "lottery_auto_draw_enabled", "0")
    assert lottery.auto_draw_enabled(db) is False
    _set_config(db, "lottery_notify_winners", "false")
    assert lottery.notify_winners_enabled(db) is False


def test_config_invalid_falls_back(db):
    _set_config(db, "lottery_draw_interval_sec", "abc")
    assert lottery.draw_interval_sec(db) == 60
    # 非法开关值不在白名单 -> 视为关闭
    _set_config(db, "lottery_auto_draw_enabled", "乱填")
    assert lottery.auto_draw_enabled(db) is False


def test_interval_min_clamp(db):
    _set_config(db, "lottery_draw_interval_sec", "10")
    assert lottery.draw_interval_sec(db) == 30
    _set_config(db, "lottery_draw_interval_sec", "120")
    assert lottery.draw_interval_sec(db) == 120


def test_run_due_draws_draws_expired(db, monkeypatch):
    # 通知走 fake sender，避免真实网络
    import backend.tg_bot.sender as sender_mod
    calls = []

    def fake_send(db_, chat_id, text, reply_markup=None):
        calls.append((chat_id, text))
        return True, None

    monkeypatch.setattr(sender_mod, "send_message", fake_send)
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))
    result = lottery.run_due_draws(db)
    assert result["checked"] == 1
    assert rnd.id in result["drawn"]
    assert result["errors"] == []
    db.refresh(rnd)
    assert rnd.status == "done"
    winners = db.query(models.LotteryRoundWinner).filter(models.LotteryRoundWinner.round_id == rnd.id).all()
    assert len(winners) == 1
    assert all(w.distributed for w in winners)
    # 群公告 + 1 条私聊
    assert len(calls) == 2
    assert calls[0][0] == rnd.chat_id


def test_run_due_draws_skips_future(db):
    rnd = _make_round(db, draw_at=datetime.now() + timedelta(hours=1))
    result = lottery.run_due_draws(db)
    assert result["checked"] == 0
    assert result["drawn"] == []
    db.refresh(rnd)
    assert rnd.status == "open"


def test_run_due_draws_skips_no_draw_at(db):
    rnd = _make_round(db, draw_at=None)
    result = lottery.run_due_draws(db)
    assert result["checked"] == 0
    assert result["drawn"] == []
    db.refresh(rnd)
    assert rnd.status == "open"


def test_run_due_draws_atomic_claim(db):
    # 模拟别的 worker 已认领（status=drawing），本轮不重复开奖
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))
    rnd.status = "drawing"
    db.commit()
    result = lottery.run_due_draws(db)
    # drawing 状态不在扫描范围（只扫 open），checked=0
    assert result["checked"] == 0
    assert result["drawn"] == []


def test_run_due_draws_disabled_switch(db):
    _set_config(db, "lottery_auto_draw_enabled", "0")
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))
    result = lottery.run_due_draws(db)
    assert result["drawn"] == []
    db.refresh(rnd)
    assert rnd.status == "open"


def test_notify_disabled(db, monkeypatch):
    import backend.tg_bot.sender as sender_mod
    called = []

    def fake_send(db_, chat_id, text, reply_markup=None):
        called.append(chat_id)
        return True, None

    monkeypatch.setattr(sender_mod, "send_message", fake_send)
    _set_config(db, "lottery_notify_winners", "0")
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))
    winners = lottery.draw_round(db, rnd.id)
    result = lottery.notify_draw_results(db, rnd.id, winners)
    assert result["sent"] is False
    assert called == []


def test_notify_failure_tolerant(db, monkeypatch):
    import backend.tg_bot.sender as sender_mod

    def boom(db_, chat_id, text, reply_markup=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(sender_mod, "send_message", boom)
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))
    winners = lottery.draw_round(db, rnd.id)
    # 不抛异常
    result = lottery.notify_draw_results(db, rnd.id, winners)
    assert result["sent"] is True
    assert result["group"] is False
    assert result["dm_failed"] == 1


def test_notify_masks_telegram_id(db, monkeypatch):
    import backend.tg_bot.sender as sender_mod
    texts = []

    def fake_send(db_, chat_id, text, reply_markup=None):
        texts.append(text)
        return True, None

    monkeypatch.setattr(sender_mod, "send_message", fake_send)
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5), n_users=1)
    winners = lottery.draw_round(db, rnd.id)
    lottery.notify_draw_results(db, rnd.id, winners)
    group_text = texts[0]
    # 打码：后四位可见，前缀星号；完整 tg id 不应出现在公告里
    entry = db.query(models.LotteryRoundEntry).filter(models.LotteryRoundEntry.round_id == rnd.id).first()
    assert "****" in group_text
    assert str(entry.telegram_id) not in group_text


def test_scheduler_single_start(db, monkeypatch):
    # 避免 daemon 线程真跑业务
    monkeypatch.setattr(lottery, "run_due_draws", lambda db_: {"checked": 0, "drawn": [], "errors": []})
    lottery._LOTTERY_DRAW_SCHEDULER_STARTED = False
    try:
        assert lottery.start_lottery_auto_draw_scheduler() is True
        assert lottery.start_lottery_auto_draw_scheduler() is False
    finally:
        lottery._LOTTERY_DRAW_SCHEDULER_STARTED = False


def test_draw_round_unchanged_for_manual_path(db):
    # 回归：手动开奖路径行为不变（open -> done）
    rnd = _make_round(db, draw_at=None)
    winners = lottery.draw_round(db, rnd.id)
    assert len(winners) == 1
    db.refresh(rnd)
    assert rnd.status == "done"


def test_run_due_draws_failure_resets_to_open(db, monkeypatch):
    # 开奖中抛异常：状态打回 open，下一轮可重试
    rnd = _make_round(db, draw_at=datetime.now() - timedelta(minutes=5))

    def boom(db_, round_id):
        raise RuntimeError("draw crashed")

    monkeypatch.setattr(lottery, "draw_round", boom)
    result = lottery.run_due_draws(db)
    assert len(result["errors"]) == 1
    assert result["drawn"] == []
    db.refresh(rnd)
    assert rnd.status == "open"

"""口令抽奖测试：抽奖类型 button/password、口令匹配、重复口令幂等、三重门复用。"""
import sys
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base  # noqa: F401
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
    models.LotteryBlacklist.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username, tg_id, created_at=None):
    user = models.WebUser(username=username, password_hash="", telegram_id=tg_id)
    if created_at is not None:
        user.created_at = created_at
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _prizes():
    return [{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}]


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = value
    else:
        db.add(models.SystemConfig(key=key, value=value))
    db.commit()


# ---------- create_round ----------

def test_create_button_round_default(db):
    r = lottery.create_round(db, title="按钮抽奖", chat_id=1, prizes=_prizes())
    assert r.lottery_type == "button"
    assert r.password_keyword is None


def test_create_password_round(db):
    r = lottery.create_round(
        db, title="口令抽奖", chat_id=1, prizes=_prizes(),
        lottery_type="password", password_keyword="  我要抽奖  ",
    )
    assert r.lottery_type == "password"
    assert r.password_keyword == "我要抽奖"  # 去空格


def test_create_password_round_requires_keyword(db):
    with pytest.raises(ValueError, match="口令"):
        lottery.create_round(
            db, title="口令抽奖", chat_id=1, prizes=_prizes(),
            lottery_type="password", password_keyword="   ",
        )


def test_create_invalid_lottery_type(db):
    with pytest.raises(ValueError):
        lottery.create_round(
            db, title="x", chat_id=1, prizes=_prizes(), lottery_type="weird",
        )


def test_create_button_ignores_keyword(db):
    r = lottery.create_round(
        db, title="按钮", chat_id=1, prizes=_prizes(),
        lottery_type="button", password_keyword="xxx",
    )
    assert r.password_keyword is None


# ---------- get_password_round ----------

def test_get_password_round_match(db):
    lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    found = lottery.get_password_round(db, 100, "  抽我  ")
    assert found is not None
    assert found.lottery_type == "password"


def test_get_password_round_no_match(db):
    lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    assert lottery.get_password_round(db, 100, "抽他") is None
    # 按钮类型的轮次不被口令匹配
    lottery.create_round(db, title="按钮", chat_id=100, prizes=_prizes())
    assert lottery.get_password_round(db, 100, "抽我") is not None  # 仍是口令那轮


def test_get_password_round_empty_keyword(db):
    assert lottery.get_password_round(db, 100, "   ") is None
    assert lottery.get_password_round(db, 100, "") is None


def test_get_password_round_closed_not_matched(db):
    r = lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    r.status = "done"
    db.commit()
    assert lottery.get_password_round(db, 100, "抽我") is None


# ---------- 口令参加走三重门 ----------

def test_password_join_goes_through_gates(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    user = _make_user(db, "u1", 111)
    # 黑名单用户参加应被拒绝
    db.add(models.LotteryBlacklist(user_id=user.id, reason="test"))
    db.commit()
    result = lottery.join_round(db, r, user, 111)
    assert result["ok"] is False
    assert result["reason"] == "blacklisted"


def test_password_join_duplicate_is_idempotent(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    user = _make_user(db, "u1", 111)
    first = lottery.join_round(db, r, user, 111)
    assert first["ok"] is True
    # 重复发送口令：第二次返回 already（调用方静默处理，不刷屏）
    second = lottery.join_round(db, r, user, 111)
    assert second["ok"] is False
    assert second["reason"] == "already"
    # 数据库只有一条记录
    count = db.query(models.LotteryRoundEntry).filter(
        models.LotteryRoundEntry.round_id == r.id,
        models.LotteryRoundEntry.user_id == user.id,
    ).count()
    assert count == 1


# ---------- TG bot 口令处理器 ----------

def test_handle_lottery_password_success(db):
    from backend.tg_bot import callbacks

    _set_config(db, "lottery_enabled", "1")
    lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    user = _make_user(db, "u1", 111)

    # 2026-10-31 根治：成功反馈走 send_message_ex（带人数＋自毁），不再是
    # 无编号的 send_message（旧反馈发送失败被吞、用户亲报群里没反馈）。
    with patch.object(callbacks, "resolve", return_value=user), \
         patch("backend.tg_bot.sender.get_chat_member", return_value=(True, "member")), \
         patch("backend.tg_bot.sender.send_message_ex",
               return_value=(True, None, 555)) as mock_send, \
         patch("backend.tg_bot.ephemeral.schedule_delete", return_value=True):
        consumed = callbacks.handle_lottery_password(
            db, 100, {"id": 111, "first_name": "张三"}, "抽我"
        )
    assert consumed is True
    mock_send.assert_called_once()
    # 数据库有参加记录
    entry = db.query(models.LotteryRoundEntry).filter(
        models.LotteryRoundEntry.telegram_id == 111
    ).first()
    assert entry is not None


def test_handle_lottery_password_no_match(db):
    from backend.tg_bot import callbacks

    _set_config(db, "lottery_enabled", "1")
    consumed = callbacks.handle_lottery_password(
        db, 100, {"id": 111, "first_name": "张三"}, "随便聊聊"
    )
    assert consumed is False


def test_handle_lottery_password_duplicate_feedback(db):
    """重复口令：只算一次参加，但必须回一条「已参加」自毁反馈。

    2026-10-31 行为变更（用户亲定）：旧版重复口令静默吞掉，用户亲报
    「发了口令群里没反馈」；新版发带人数的自毁反馈（不常驻刷屏）。
    """
    from backend.tg_bot import callbacks

    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    user = _make_user(db, "u1", 111)
    lottery.join_round(db, r, user, 111)  # 先参加一次

    with patch.object(callbacks, "resolve", return_value=user), \
         patch("backend.tg_bot.sender.get_chat_member", return_value=(True, "member")), \
         patch("backend.tg_bot.sender.send_message_ex",
               return_value=(True, None, 556)) as mock_send, \
         patch("backend.tg_bot.ephemeral.schedule_delete", return_value=True):
        consumed = callbacks.handle_lottery_password(
            db, 100, {"id": 111, "first_name": "张三"}, "抽我"
        )
    assert consumed is True
    mock_send.assert_called_once()
    assert "已参加" in mock_send.call_args[0][2]
    # 仍然只算一次参加
    assert db.query(models.LotteryRoundEntry).filter(
        models.LotteryRoundEntry.round_id == r.id).count() == 1


def test_handle_lottery_password_not_member_silent(db):
    from backend.tg_bot import callbacks

    _set_config(db, "lottery_enabled", "1")
    lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )
    user = _make_user(db, "u1", 111)

    with patch.object(callbacks, "resolve", return_value=user), \
         patch("backend.tg_bot.sender.get_chat_member", return_value=(False, "left")), \
         patch("backend.tg_bot.sender.send_message") as mock_send:
        consumed = callbacks.handle_lottery_password(
            db, 100, {"id": 111, "first_name": "张三"}, "抽我"
        )
    assert consumed is True
    mock_send.assert_not_called()  # 非群成员静默
    # 没有参加记录
    assert db.query(models.LotteryRoundEntry).count() == 0


def test_handle_lottery_password_unbound_silent(db):
    from backend.tg_bot import callbacks

    _set_config(db, "lottery_enabled", "1")
    lottery.create_round(
        db, title="口令", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )

    with patch.object(callbacks, "resolve", return_value=None), \
         patch("backend.tg_bot.sender.send_message") as mock_send:
        consumed = callbacks.handle_lottery_password(
            db, 100, {"id": 999, "first_name": "路人"}, "抽我"
        )
    assert consumed is True
    mock_send.assert_not_called()  # 未绑定静默

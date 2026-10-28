"""B1 测试：TG Bot 基础设施（identity/router/bind/poller offset）。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend import models
from backend.emby_server import models as em
from backend.tg_bot import identity, handlers, router


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # 只建需要的表
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.TgBindCode.__table__.create(engine, checkfirst=True)
    # SystemConfig 表（store.get_value 需要）
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    # 绑定成功消息需要查订阅与套餐
    models.UserSubscription.__table__.create(engine, checkfirst=True)
    models.SubscriptionPlan.__table__.create(engine, checkfirst=True)
    # /start 已绑定 dashboard 需要查成就与馆灵
    models.PanelAchievementDef.__table__.create(engine, checkfirst=True)
    models.PanelAchievementUnlock.__table__.create(engine, checkfirst=True)
    em.PlaybackSession.__table__.create(engine, checkfirst=True)
    em.MediaItem.__table__.create(engine, checkfirst=True)
    # /start 详细版 dashboard 新增：查看凭证、连签、网盘绑定
    models.EmbyViewUnlock.__table__.create(engine, checkfirst=True)
    models.CheckinRecord.__table__.create(engine, checkfirst=True)
    em.UserStorageBinding.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username="u1", tg_id=None):
    u = models.WebUser(username=username, telegram_id=tg_id)
    # 必填字段兜底
    for col in ("password_hash",):
        if hasattr(u, col) and getattr(u, col) is None:
            try:
                setattr(u, col, "")
            except Exception:
                pass
    db.add(u)
    db.commit()
    return u


def test_identity_resolve_found(db):
    u = _make_user(db, tg_id=123456789)
    found = identity.resolve(db, 123456789)
    assert found is not None
    assert found.id == u.id


def test_identity_resolve_not_found(db):
    _make_user(db, tg_id=111)
    assert identity.resolve(db, 999) is None


def test_handle_start_unbound(db):
    tg_user = {"id": 555, "first_name": "Tom"}
    result = handlers.handle_start(db, tg_user, 555, "")
    text = result[0] if isinstance(result, tuple) else result
    assert "绑定" in text and "左下角" in text


def test_handle_start_bound(db):
    _make_user(db, tg_id=555)
    tg_user = {"id": 555, "first_name": "Tom"}
    result = handlers.handle_start(db, tg_user, 555, "")
    text = result[0] if isinstance(result, tuple) else result
    assert "欢迎回到" in text and "账号：" in text


def test_handle_help(db):
    text = handlers.handle_help(db, {}, 1, "")
    assert "左下角" in text and "/bind" in text


def test_handle_bind_no_args_returns_guide(db):
    text = handlers.handle_bind(db, {"id": 999010}, 999010, "")
    assert "如何绑定账号" in text
    assert "/bind 绑定码" in text
    assert db.query(models.TgBindCode).count() == 0


def test_handle_bind_with_valid_code(db):
    u = _make_user(db, username="webuser2")
    code = models.TgBindCode(
        code="234567",
        user_id=u.id,
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db.add(code)
    db.commit()

    text = handlers.handle_bind(db, {"id": 999011}, 999011, "234567")

    assert "绑定成功" in text
    assert "👤 用户名：webuser2" in text
    assert "无有效订阅" in text
    db.refresh(u)
    assert u.telegram_id == 999011


def test_handle_bind_with_invalid_code(db):
    text = handlers.handle_bind(db, {"id": 999012}, 999012, "000000")
    assert text == "绑定码无效或已过期，请在网页端重新获取绑定码"


def test_handle_bind_success_with_subscription(db):
    u = _make_user(db, username="subuser")
    plan = models.SubscriptionPlan(name="月卡", price=30, duration_days=30)
    db.add(plan)
    db.commit()
    db.refresh(plan)
    sub = models.UserSubscription(
        user_id=u.id,
        plan_id=plan.id,
        status="active",
        start_date=datetime.now(),
        end_date=datetime.now() + timedelta(days=30),
    )
    db.add(sub)
    db.commit()
    code = models.TgBindCode(
        code="345678",
        user_id=u.id,
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db.add(code)
    db.commit()

    text = handlers.handle_bind(db, {"id": 999013}, 999013, "345678")

    assert "📦 订阅服务：月卡" in text
    assert "有有效订阅" in text


def test_handle_help_appends_bind_guide(db):
    text = handlers.handle_help(db, {}, 1, "")
    assert "如何绑定账号" in text
    assert "左下角" in text


def test_handle_bind_already_bound(db):
    _make_user(db, tg_id=888)
    tg_user = {"id": 888, "first_name": "Ann"}
    text = handlers.handle_bind(db, tg_user, 888, "")
    assert "已绑定" in text


def test_verify_bind_code_success(db):
    """网页发起的码，用户给 bot 发码 → 绑定成功。"""
    u = _make_user(db, username="webuser")
    code = models.TgBindCode(
        user_id=u.id,
        telegram_id=None,
        code="123456",
        expires_at=datetime.now() + timedelta(minutes=10),
    )
    db.add(code)
    db.commit()

    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 999001, "first_name": "Web"},
            "chat": {"id": 999001, "type": "private"},
            "text": "123456",
        },
    }
    # mock sender.send_message 避免真实 HTTP
    from backend.tg_bot import sender
    sent = []
    orig = sender.send_message
    sender.send_message = lambda db_, chat_id, text, rm=None: sent.append((chat_id, text)) or (True, None)
    try:
        router.dispatch(db, update)
    finally:
        sender.send_message = orig

    db.refresh(u)
    assert u.telegram_id == 999001
    assert any("绑定成功" in t for _, t in sent)


def test_verify_bind_code_expired(db):
    u = _make_user(db, username="expuser")
    code = models.TgBindCode(
        user_id=u.id,
        code="654321",
        expires_at=datetime.now() - timedelta(minutes=1),
    )
    db.add(code)
    db.commit()

    update = {
        "update_id": 2,
        "message": {
            "message_id": 2,
            "from": {"id": 999002, "first_name": "Exp"},
            "chat": {"id": 999002, "type": "private"},
            "text": "654321",
        },
    }
    from backend.tg_bot import sender
    orig = sender.send_message
    sender.send_message = lambda *a, **k: (True, None)
    try:
        router.dispatch(db, update)
    finally:
        sender.send_message = orig

    db.refresh(u)
    assert u.telegram_id is None  # 过期码不绑定


def test_router_unknown_command(db):
    update = {
        "update_id": 3,
        "message": {
            "message_id": 3,
            "from": {"id": 111, "first_name": "X"},
            "chat": {"id": 111, "type": "private"},
            "text": "/foobar",
        },
    }
    from backend.tg_bot import sender
    sent = []
    orig = sender.send_message
    sender.send_message = lambda db_, chat_id, text, reply_markup=None, **kw: sent.append(text) or (True, None)
    try:
        router.dispatch(db, update)
    finally:
        sender.send_message = orig
    assert any("未知命令" in t for t in sent)


def test_router_ignores_plain_text(db):
    """普通文本不回复。"""
    update = {
        "update_id": 4,
        "message": {
            "message_id": 4,
            "from": {"id": 222, "first_name": "Y"},
            "chat": {"id": 222, "type": "private"},
            "text": "你好",
        },
    }
    from backend.tg_bot import sender
    sent = []
    orig = sender.send_message
    sender.send_message = lambda *a, **k: sent.append(1) or (True, None)
    try:
        router.dispatch(db, update)
    finally:
        sender.send_message = orig
    assert sent == []

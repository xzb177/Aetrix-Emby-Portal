"""B1-P5 一键免密登录隔离测试：内存 SQLite + 关闭 Redis"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    from backend.database import Base
except ImportError:  # 兼容 Base 定义在 models 中的项目结构
    from backend.models import Base  # type: ignore

from backend.integrations import store
from backend.models import WebUser
from backend.tg_bot import handlers, login_token


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 清理模块级内存兜底数据，避免用例间互相影响
    login_token._mem_tokens.clear()
    login_token._mem_rate.clear()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_user(db, telegram_id: int) -> WebUser:
    user = WebUser(username=f"u_{telegram_id}", telegram_id=telegram_id, is_active=True,
                   password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_create_and_consume(db):
    token = login_token.create_login_token(1001, 1001)
    assert token is not None
    payload = login_token.consume_login_token(token)
    assert payload is not None
    assert payload["user_id"] == 1001
    assert payload["telegram_id"] == 1001
    # 一次性：二次消费返回 None
    assert login_token.consume_login_token(token) is None


def test_rate_limit_per_hour(db):
    for _ in range(10):
        assert login_token.create_login_token(1002, 1002) is not None
    # 同一用户 1 小时内第 11 次触发限流
    assert login_token.create_login_token(1002, 1002) is None


def test_build_login_url_without_base_url(db):
    user = _make_user(db, 1003)
    assert store.get_value(db, "site_base_url", "") == ""
    assert login_token.build_login_url(db, user) is None


def test_handle_start_bound_returns_tuple(db):
    store.write_values(db, {"site_base_url": "https://example.com"})
    _make_user(db, 1004)
    reply = handlers.handle_start(db, {"id": 1004, "first_name": "Tester"}, 1004, "")
    assert isinstance(reply, tuple)
    text, markup = reply
    assert "账号：" in text
    assert isinstance(markup, dict)
    assert "inline_keyboard" in markup
    assert markup["inline_keyboard"][0][0]["url"].startswith("https://example.com/tg-login?token=")


def test_handle_start_unbound_returns_str(db):
    reply = handlers.handle_start(db, {"id": 1005, "first_name": "Tester"}, 1005, "")
    text = reply[0] if isinstance(reply, tuple) else reply
    assert "绑定" in text

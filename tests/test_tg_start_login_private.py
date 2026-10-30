"""TG Bot / 注册门禁安全回归测试（be/core 审查）。"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.integrations import store
from backend.tg_bot import handlers, login_token, poller


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


def _make_user(db, username="u1", tg_id=None):
    u = models.WebUser(username=username, telegram_id=tg_id, password_hash="x")
    db.add(u)
    db.commit()
    return u


# ---------- /start 一键登录按钮不得发到群里 ----------

def test_start_in_group_has_no_login_button(db, monkeypatch):
    _make_user(db, tg_id=555)
    db.add(models.SystemConfig(key="site_base_url", value="https://example.com"))
    db.commit()
    store.invalidate()
    issued = []
    monkeypatch.setattr(login_token, "create_login_token",
                        lambda uid, tid: issued.append(uid) or "TOKEN")
    # 群聊：chat_id 为负数，与用户 id 不同
    result = handlers.handle_start(db, {"id": 555, "first_name": "T"}, -100123, "")
    # 群 /start 现返回 (信息卡正文, 群菜单) 元组（PR-1 定版），旧断言
    # “只能回纯字符串”已过时；本意不变：群里不下一键登录按钮。
    # 按姊妹测试 test_start_bound_group_has_dashboard_no_login_link
    # 的口径改判：菜单里不得有任何 url 按钮、正文不得含 token。
    assert isinstance(result, tuple)
    text, markup = result
    assert "token=" not in text
    assert not [b for r in markup["inline_keyboard"] for b in r
                if b.get("url")], "群菜单里不能有一键免密登录按钮"
    assert issued == [], "群聊里不应签发登录 token"


def test_start_in_private_keeps_login_button(db, monkeypatch):
    _make_user(db, tg_id=556)
    db.add(models.SystemConfig(key="site_base_url", value="https://example.com"))
    db.commit()
    store.invalidate()
    monkeypatch.setattr(login_token, "create_login_token", lambda uid, tid: "TOKEN")
    result = handlers.handle_start(db, {"id": 556, "first_name": "T"}, 556, "")
    assert isinstance(result, tuple)
    assert "token=TOKEN" in result[1]["inline_keyboard"][0][0]["url"]

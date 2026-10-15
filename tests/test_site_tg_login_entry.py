# -*- coding: utf-8 -*-
"""登录页 TG 登录入口公开端点测试（GET /api/site/tg-login-entry）。

隔离：最小 FastAPI 应用只挂 site_router，get_db 覆写为内存 SQLite 会话，
get_bot_username 用 monkeypatch 替身，不碰真实配置与 Telegram API。

覆盖：
1. 已配置机器人 → 200 返回其公开用户名
2. 未配置机器人（空串）→ 200 返回空串（前端据此不渲染入口）
3. 读取机器人用户名抛异常 → 仍 200 返回空串（公开端点绝不 500 拖垮登录页）
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api import site as site_module
from backend.database import get_db
from backend import models


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    app = FastAPI()
    app.include_router(site_module.site_router)

    def _override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_returns_bot_username_when_configured(client, monkeypatch):
    monkeypatch.setattr(site_module, "get_bot_username", lambda db: "aetrix_bot")
    resp = client.get("/api/site/tg-login-entry")
    assert resp.status_code == 200
    assert resp.json() == {"bot_username": "aetrix_bot"}


def test_returns_empty_when_bot_not_configured(client, monkeypatch):
    monkeypatch.setattr(site_module, "get_bot_username", lambda db: "")
    resp = client.get("/api/site/tg-login-entry")
    assert resp.status_code == 200
    assert resp.json() == {"bot_username": ""}


def test_never_500_when_lookup_raises(client, monkeypatch):
    def _boom(db):
        raise RuntimeError("config store down")

    monkeypatch.setattr(site_module, "get_bot_username", _boom)
    resp = client.get("/api/site/tg-login-entry")
    assert resp.status_code == 200
    assert resp.json() == {"bot_username": ""}

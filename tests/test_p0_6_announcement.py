"""P0-6 回归测试：未鉴权客户端不得通过传参读出已停用公告全文。

背景：GET /api/user/announcements 的 active_only 参数可被未鉴权客户端
传 false 绕过，读出已停用/撤回公告的完整 content。

修复：公开接口恒只返回 is_active=True 的公告；停用公告走管理端接口。

全部离线：内存 SQLite + 进程内路由，不发起真实网络请求。
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def user_client(db):
    from backend.api import user as user_api
    from backend.database import get_db

    app = FastAPI()
    app.include_router(user_api.user_router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def _announcement(db, title, content, is_active=True, is_pinned=False):
    a = models.Announcement(title=title, content=content, is_active=is_active,
                            is_pinned=is_pinned, type="info",
                            created_at=datetime.now())
    db.add(a)
    db.commit()
    return a


def test_public_announcements_only_active(db, user_client):
    _announcement(db, "正常公告", "这是正常内容", is_active=True)
    _announcement(db, "已撤回公告", "这是撤回的秘密内容", is_active=False)

    r = user_client.get("/api/user/announcements")
    assert r.status_code == 200, r.text
    data = r.json()
    titles = [a["title"] for a in data]
    assert "正常公告" in titles
    assert "已撤回公告" not in titles
    # 内容也不能泄露
    all_content = " ".join(a["content"] for a in data)
    assert "撤回的秘密内容" not in all_content


def test_active_only_false_cannot_bypass(db, user_client):
    """P0-6 核心：传 active_only=false 也必须拿不到停用公告。"""
    _announcement(db, "正常公告", "正常内容", is_active=True)
    _announcement(db, "已停用公告", "停用的秘密内容", is_active=False)

    r = user_client.get("/api/user/announcements", params={"active_only": "false"})
    assert r.status_code == 200, r.text
    data = r.json()
    titles = [a["title"] for a in data]
    assert "已停用公告" not in titles
    all_content = " ".join(a["content"] for a in data)
    assert "停用的秘密内容" not in all_content


def test_no_announcements_returns_empty(db, user_client):
    r = user_client.get("/api/user/announcements")
    assert r.status_code == 200, r.text
    assert r.json() == []

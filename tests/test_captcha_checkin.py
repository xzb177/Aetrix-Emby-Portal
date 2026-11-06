"""网站签到人机验证（2026-11-06 用户亲令）：登录已有保护，签到也要过同一道闸。

口径：
- 只保护网站 POST /api/user/economy/checkin；TG 机器人签到不走这个端点、不受影响；
- 未配置提供方/密钥时直接放行（不锁门），与登录/注册同一套 captcha.guard；
- 开启保护后无令牌/坏令牌一律 400 且不落签到记录，好令牌才放行；
- widget_info 必须下发 checkin 动作开关，前端挂件才知道要渲染。
"""
import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import economy
from backend.api.user import get_current_user
from backend.database import get_db
from backend.integrations import captcha
from backend.tg_bind import require_tg_bound


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _seed(db, extra=None):
    cfg = {
        "checkin_enabled": "true",
        "checkin_base_min": "3",
        "checkin_base_max": "3",
        "checkin_crit_pct": "0",
        "captcha_provider": "turnstile",
        "captcha_site_key": "site-key",
        "captcha_secret_key": "secret-key",
    }
    if extra:
        cfg.update(extra)
    for k, v in cfg.items():
        db.add(models.SystemConfig(key=k, value=str(v)))
    db.commit()


def _user(db, name="capcheckin"):
    u = models.WebUser(username=name, password_hash="x", is_active=True,
                       is_welfare=True, points=0, vitality=14)
    db.add(u)
    db.commit()
    return u


def _client(db, user):
    app = FastAPI()
    app.include_router(economy.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_tg_bound] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _quiet_notify(monkeypatch):
    async def _fake(**kwargs):
        return None

    monkeypatch.setattr(economy, "notify_admin_event_quiet", _fake)


def _stub_verify(monkeypatch):
    def fake_verify(db_, cfg, token, ip=""):
        if token == "good-token":
            return True, ""
        if not (token or "").strip():
            return False, "请完成人机验证"
        return False, "人机验证失败（invalid-input-response）"

    monkeypatch.setattr(captcha, "_verify", fake_verify)


def test_widget_info_includes_checkin_action(db):
    _seed(db)
    info = captcha.widget_info(db)
    assert info["enabled"] is True
    assert info["actions"].get("checkin") is True
    # 既有动作口径不许被签到改动带歪
    assert info["actions"].get("login") is True
    assert info["actions"].get("register") is True
    assert info["actions"].get("admin_login") is False


def test_checkin_verify_request_requires_token(db):
    _seed(db)
    ok, msg = captcha.verify_request(db, "checkin", None)
    assert ok is False and "人机验证" in msg


def test_checkin_endpoint_blocks_without_token_and_allows_with_token(db, monkeypatch):
    _seed(db)
    _stub_verify(monkeypatch)
    u = _user(db)
    client = _client(db, u)

    r = client.post("/api/user/economy/checkin", json={})
    assert r.status_code == 400, r.text
    assert "人机验证" in r.json().get("detail", "")
    assert db.query(models.CheckinRecord).filter_by(user_id=u.id).count() == 0
    # 失败要留痕（与登录同一 reason）
    assert db.query(models.LoginLog).filter_by(reason="captcha_failed").count() >= 1

    r = client.post("/api/user/economy/checkin", json={"captcha_token": "bad-token"})
    assert r.status_code == 400, r.text
    assert db.query(models.CheckinRecord).filter_by(user_id=u.id).count() == 0

    r = client.post("/api/user/economy/checkin", json={"captcha_token": "good-token"})
    assert r.status_code == 200, r.text
    assert r.json().get("success") is True
    assert db.query(models.CheckinRecord).filter_by(user_id=u.id).count() == 1


def test_checkin_endpoint_open_when_captcha_not_configured(db):
    # 只开签到、不配人机验证：照常签到（没配置 = 直接放行，不锁门）
    _seed(db, {"captcha_provider": "none", "captcha_site_key": "",
               "captcha_secret_key": ""})
    u = _user(db, name="nocaptcha")
    client = _client(db, u)
    r = client.post("/api/user/economy/checkin")
    assert r.status_code == 200, r.text
    assert db.query(models.CheckinRecord).filter_by(user_id=u.id).count() == 1


class _FakeRequest:
    """最小 Request 替身：与 scripts/smoke_test_concurrency.py 同口径。"""

    headers: dict = {}
    query_params: dict = {}

    class client:
        host = "10.0.0.9"

    class url:
        path = "/api/user/economy/checkin"


def test_checkin_positional_call_compat(db):
    """回归：do_checkin 的位置参数顺序不许被新增的可选 body 参数顶掉。

    并发冒烟脚本（scripts/smoke_test_concurrency.py）按
    ``do_checkin(request, user, db)`` 直调端点；PR #1874 曾把 payload
    插在第 2 位，导致 current_user 拿到 Session、全体 500
    （'Session' object has no attribute 'id'）。payload 必须排在
    Depends 参数之后，位置调用才不会错位。
    """
    import asyncio

    _seed(db, {"captcha_provider": "none", "captcha_site_key": "",
               "captcha_secret_key": ""})
    u = _user(db, name="positional")
    res = asyncio.run(economy.do_checkin(_FakeRequest(), u, db))
    assert res.get("success") is True, res
    assert db.query(models.CheckinRecord).filter_by(user_id=u.id).count() == 1

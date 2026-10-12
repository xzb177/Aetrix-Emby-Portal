# -*- coding: utf-8 -*-
"""API Key / Service Token 机制测试。

隔离：内存 SQLite + 最小 FastAPI 应用，不碰开发库/生产库，不 import backend.main。
（旧版曾用 SessionLocal + 顶层 init_db 写真实库，已按 test_access_guard.py 的隔离惯例重写。）

覆盖：
1. 创建返回明文一次；库里只存 SHA256(明文 + pepper)，明文永不落库
2. pepper 确实参与哈希（换 pepper → 换哈希）
3. scope 层级：默认 admin > write > read；名单/层级走环境变量可配
4. 过期 key 被拒绝；吊销立即生效
5. 错误 key 被拒绝（格式错误 / 不存在）
6. last_used_at 在验证成功时更新
7. key_id 碰撞时循环重试
8. 空 scope / 未知 scope 创建抛 ValueError
9. require_api_key：无头 → 401；scope 不足 → 403；未知 scope 名直接炸（防配错）
10. 管理 API（mock 管理员鉴权）：创建 / 列表不含明文 / 编辑 / 吊销后 verify 立刻 401
11. 写操作真的留下审计（create/revoke 落 AdminLog，_audit 后必须 commit）
12. 超管边界：运营角色写 /api/admin/api-keys 被拦（SUPER_ONLY_PREFIXES）
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import api_key as ak
from backend import models
from backend.admin_roles import ROLE_OPERATOR, ROLE_SUPER, blocked_reason


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False：TestClient 在另一个线程里跑 app，
    # 内存库必须跨线程共享同一个连接，否则报 "objects created in a thread"。
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


@pytest.fixture()
def keypair(db):
    """一个 read scope 的 key（用完即删）。"""
    rec, plaintext = ak.create_api_key(db, name="测试key", scopes=["read"])
    yield rec, plaintext
    db.query(models.ApiKey).filter(models.ApiKey.id == rec.id).delete()
    db.commit()


def _scrub(db, rec):
    db.query(models.ApiKey).filter(models.ApiKey.id == rec.id).delete()
    db.commit()


# ==================== 创建与哈希 ====================

def test_create_returns_plaintext_once_and_stores_hash_only(db):
    rec, plaintext = ak.create_api_key(db, name="一次明文", scopes=["read", "write"])
    try:
        assert plaintext.startswith("ak_")
        assert len(plaintext) > 20
        # 库里只有哈希：明文与 key_hash 互不包含
        assert rec.key_hash != plaintext
        assert plaintext not in (rec.key_hash or "")
        assert len(rec.key_hash) == 64  # SHA256 hex
        # key_id 是明文的公开前缀，可安全展示
        assert plaintext.startswith(rec.key_id)
    finally:
        _scrub(db, rec)


def test_plaintext_never_hits_db(db, keypair):
    """grep 式断言：整表扫一遍，明文没进任何字段。"""
    rec, plaintext = keypair
    row = db.query(models.ApiKey).filter(models.ApiKey.id == rec.id).one()
    for col in ("key_id", "key_hash", "name"):
        assert plaintext not in str(getattr(row, col) or "")


def test_pepper_participates_in_hash(monkeypatch):
    monkeypatch.setenv("API_KEY_PEPPER", "pepper-a")
    h1 = ak._hash_key("ak_test_pepper_check")
    monkeypatch.setenv("API_KEY_PEPPER", "pepper-b")
    h2 = ak._hash_key("ak_test_pepper_check")
    assert len(h1) == 64
    assert h1 != h2  # 换 pepper → 哈希变了，pepper 确实参与


def test_key_id_collision_retries(db, monkeypatch):
    """key_id 极小概率碰撞：循环重试直到拿到空闲的。"""
    clash, _ = ak.create_api_key(db, name="占位", scopes=["read"])
    seq = iter([
        (clash.key_id, "ak_" + "x" * 45),   # 第一次：撞
        ("ak_fresh0001", "ak_" + "y" * 45),  # 第二次：空闲
    ])
    monkeypatch.setattr(ak, "generate_plaintext", lambda: next(seq))
    try:
        rec, _ = ak.create_api_key(db, name="重试", scopes=["read"])
        assert rec.key_id == "ak_fresh0001"
        _scrub(db, rec)
    finally:
        _scrub(db, clash)


def test_create_empty_or_unknown_scopes_rejected(db):
    with pytest.raises(ValueError):
        ak.create_api_key(db, name="空scope", scopes=[])
    with pytest.raises(ValueError):
        ak.create_api_key(db, name="坏scope", scopes=["nope"])


# ==================== scope 层级 ====================

def test_scope_hierarchy_default(keypair):
    rec, _ = keypair  # scopes=["read"]
    assert ak.has_scope(rec, "read")
    assert not ak.has_scope(rec, "write")
    assert not ak.has_scope(rec, "admin")


def test_scope_admin_includes_all(db):
    rec, _ = ak.create_api_key(db, name="admin", scopes=["admin"])
    try:
        assert ak.has_scope(rec, "read")
        assert ak.has_scope(rec, "write")
        assert ak.has_scope(rec, "admin")
        assert not ak.has_scope(rec, "superuser")  # 名单外 fail closed
    finally:
        _scrub(db, rec)


def test_scopes_configurable_via_env(db, monkeypatch):
    """API_KEY_SCOPES 改名单与层级：顺序即层级，靠后包含靠前。"""
    monkeypatch.setenv("API_KEY_SCOPES", "basic,pro")
    assert ak.allowed_scopes() == ("basic", "pro")
    rec, _ = ak.create_api_key(db, name="env-scope", scopes=["pro"])
    try:
        assert ak.has_scope(rec, "basic")
        assert ak.has_scope(rec, "pro")
        assert not ak.has_scope(rec, "read")  # 名单里没了就失效
    finally:
        _scrub(db, rec)
    # 恢复默认后 read 又可用
    monkeypatch.delenv("API_KEY_SCOPES")
    assert ak.allowed_scopes() == ("read", "write", "admin")


def test_max_expire_days_configurable(monkeypatch):
    assert ak.max_expire_days() == 3650
    monkeypatch.setenv("API_KEY_MAX_EXPIRE_DAYS", "30")
    assert ak.max_expire_days() == 30
    monkeypatch.setenv("API_KEY_MAX_EXPIRE_DAYS", "not-a-number")
    assert ak.max_expire_days() == 3650  # 非法值回退默认


# ==================== 验证：过期 / 吊销 / 错误 ====================

def test_verify_roundtrip(keypair, db):
    rec, plaintext = keypair
    got = ak.verify_api_key(db, plaintext)
    assert got is not None
    assert got.id == rec.id


def test_verify_rejects_garbage(db):
    assert ak.verify_api_key(db, "") is None
    assert ak.verify_api_key(db, "not-a-key") is None
    assert ak.verify_api_key(db, "ak_" + "x" * 60) is None


def test_expired_key_rejected(db):
    rec, plaintext = ak.create_api_key(db, name="过期", scopes=["read"], expires_days=1)
    try:
        rec.expires_at = datetime.now() - timedelta(seconds=1)
        db.commit()
        assert ak.verify_api_key(db, plaintext) is None
    finally:
        _scrub(db, rec)


def test_revoked_key_rejected_immediately(keypair, db):
    """吊销立即生效：无缓存窗口，verify 当场失败。"""
    rec, plaintext = keypair
    assert ak.revoke_api_key(db, rec.key_id) is True
    assert ak.verify_api_key(db, plaintext) is None
    assert ak.revoke_api_key(db, "ak_nonexistent") is False


def test_touch_last_used(keypair, db):
    rec, _ = keypair
    assert rec.last_used_at is None
    ak.touch_last_used(db, rec)
    db.refresh(rec)
    assert rec.last_used_at is not None


# ==================== require_api_key 依赖 ====================

def test_require_api_key_no_header_401(db):
    dep = ak.require_api_key("read")

    class FakeRequest:
        headers = {}

    with pytest.raises(HTTPException) as ei:
        dep(FakeRequest(), db)
    assert ei.value.status_code == 401


def test_require_api_key_insufficient_scope_403(db, keypair):
    _, plaintext = keypair  # read only
    dep = ak.require_api_key("admin")

    class FakeRequest:
        headers = {"X-API-Key": plaintext}

    with pytest.raises(HTTPException) as ei:
        dep(FakeRequest(), db)
    assert ei.value.status_code == 403


def test_require_api_key_ok_and_touches_last_used(db, keypair):
    rec, plaintext = keypair
    dep = ak.require_api_key("read")

    class FakeRequest:
        headers = {"X-API-Key": plaintext}

    got = dep(FakeRequest(), db)
    assert got.id == rec.id
    db.refresh(rec)
    assert rec.last_used_at is not None


def test_require_api_key_unknown_scope_name_fails_fast():
    """配错 scope 名在定义路由时就炸，而不是等请求进来才 403。"""
    with pytest.raises(ValueError):
        ak.require_api_key("superuser")


# ==================== 管理 API（HTTP 级，mock 管理员鉴权） ====================

def _make_app(db):
    """最小应用：只挂 admin_apikey 路由；鉴权与 get_db 全部 override。"""
    from backend.api import admin_apikey as mod
    from backend.api.admin_core import get_current_admin
    from backend.database import get_db

    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(id=999)
    app.dependency_overrides[get_db] = lambda: db
    return app


@pytest.fixture()
def admin_client(db):
    app = _make_app(db)
    with TestClient(app) as client:
        yield client


def test_admin_create_list_revoke_flow(admin_client, db):
    # 创建：明文只返回这一次
    r = admin_client.post("/api/admin/api-keys", json={
        "name": "第三方对接", "scopes": ["read"], "expires_days": 30,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"].startswith("ak_")
    plaintext, key_id = body["key"], body["key_id"]

    # 列表：不含明文
    r2 = admin_client.get("/api/admin/api-keys")
    assert r2.status_code == 200
    items = r2.json()["items"]
    assert any(i["key_id"] == key_id for i in items)
    assert all("key" not in i for i in items)

    # 连通性测试端点：X-API-Key 鉴权
    r3 = admin_client.get("/api/admin/api-keys/verify", headers={"X-API-Key": plaintext})
    assert r3.status_code == 200, r3.text
    assert r3.json()["key_id"] == key_id

    # 吊销 → verify 立刻 401
    r4 = admin_client.delete(f"/api/admin/api-keys/{key_id}")
    assert r4.status_code == 200
    r5 = admin_client.get("/api/admin/api-keys/verify", headers={"X-API-Key": plaintext})
    assert r5.status_code == 401

    _scrub(db, SimpleNamespace(id=body["id"]))


def test_admin_create_bad_scope_400(admin_client):
    r = admin_client.post("/api/admin/api-keys", json={"name": "坏", "scopes": ["root"]})
    assert r.status_code == 400


def test_admin_create_over_max_expire_400(admin_client, monkeypatch):
    monkeypatch.setenv("API_KEY_MAX_EXPIRE_DAYS", "30")
    r = admin_client.post("/api/admin/api-keys",
                          json={"name": "超长", "scopes": ["read"], "expires_days": 31})
    assert r.status_code == 400
    r2 = admin_client.post("/api/admin/api-keys",
                           json={"name": "刚好", "scopes": ["read"], "expires_days": 30})
    assert r2.status_code == 200, r2.text
    admin_client.delete(f"/api/admin/api-keys/{r2.json()['key_id']}")


def test_admin_update_api_key(admin_client, db):
    r = admin_client.post("/api/admin/api-keys", json={"name": "改名测试", "scopes": ["read"]})
    key_id = r.json()["key_id"]
    rid = r.json()["id"]
    r2 = admin_client.put(f"/api/admin/api-keys/{key_id}",
                          json={"name": "新名字", "scopes": ["read", "write"]})
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "新名字"
    assert sorted(r2.json()["scopes"]) == ["read", "write"]
    _scrub(db, SimpleNamespace(id=rid))


def test_admin_revoke_nonexistent_404(admin_client):
    r = admin_client.delete("/api/admin/api-keys/ak_nonexistent")
    assert r.status_code == 404


def test_verify_endpoint_no_key_401(admin_client):
    assert admin_client.get("/api/admin/api-keys/verify").status_code == 401


def test_write_ops_leave_audit_log(admin_client, db):
    """写操作必须留下审计（_audit 后 commit，否则 AdminLog 丢了）。"""
    r = admin_client.post("/api/admin/api-keys", json={"name": "审计检查", "scopes": ["read"]})
    key_id = r.json()["key_id"]
    admin_client.delete(f"/api/admin/api-keys/{key_id}")
    actions = [row.action for row in
               db.query(models.AdminLog).filter(models.AdminLog.target_type == "api_key").all()]
    assert "create_api_key" in actions
    assert "revoke_api_key" in actions


# ==================== 超管边界 ====================

def _req(path, method="POST"):
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


def test_operator_cannot_write_api_keys():
    """运营写 /api/admin/api-keys* 一律被拦（防自发 admin Key 提权）。"""
    admin = SimpleNamespace(admin_role=ROLE_OPERATOR)
    for method, path in [("POST", "/api/admin/api-keys"),
                         ("PUT", "/api/admin/api-keys/ak_123"),
                         ("DELETE", "/api/admin/api-keys/ak_123")]:
        assert blocked_reason(_req(path, method), admin), f"{method} {path} 应仅限超管"


def test_super_can_write_and_reads_stay_open():
    sup = SimpleNamespace(admin_role=ROLE_SUPER)
    assert blocked_reason(_req("/api/admin/api-keys", "POST"), sup) is None
    op = SimpleNamespace(admin_role=ROLE_OPERATOR)
    assert blocked_reason(_req("/api/admin/api-keys", "GET"), op) is None

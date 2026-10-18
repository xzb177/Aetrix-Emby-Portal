"""严重 / 高危安全修复的回归测试（S1–S3、H1–H3、H5；H4 见 test_domain_guard.py）

全部离线：内存 SQLite + 进程内路由，不发起真实网络请求。
"""
from __future__ import annotations

import ast
import os
import pathlib
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import admin_roles, models
from backend.emby_server import models as em
from backend.security import create_access_token

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEST_SECRET = "unit-test-secret-key-0123456789abcdefghij"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    """节点密钥从 SECRET_KEY 派生：测试里显式给一个（与 backend.security 无关）"""
    if not (os.environ.get("SECRET_KEY") or "").strip():
        monkeypatch.setenv("SECRET_KEY", TEST_SECRET)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, username, *, is_staff=False, role=None, active=True):
    u = models.WebUser(username=username, password_hash="x", is_staff=is_staff,
                       admin_role=role, is_active=active)
    db.add(u)
    db.commit()
    return u


def _bearer(user) -> dict:
    return {"Authorization": "Bearer " + create_access_token(user.id, {"username": user.username})}


def _request(path="/", method="GET", headers=None, query=b"", client=("1.2.3.4", 1)):
    scope = {
        "type": "http", "method": method, "path": path, "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client, "path_params": {},
    }
    return Request(scope)


# ===========================================================================
# S1 / S2：管理员角色与「旧用户接口」
# ===========================================================================

@pytest.fixture()
def admin_client(db):
    from backend.api import admin as admin_api  # noqa: F401 — 注册 /users 路由
    from backend.api.admin_core import admin_router
    from backend.database import get_db

    app = FastAPI()
    app.include_router(admin_router)

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c


def test_s1_empty_role_is_viewer():
    assert admin_roles.normalize_role(None) == admin_roles.ROLE_VIEWER
    assert admin_roles.normalize_role("") == admin_roles.ROLE_VIEWER
    assert admin_roles.normalize_role("ROOT") == admin_roles.ROLE_VIEWER
    assert admin_roles.normalize_role("Super") == admin_roles.ROLE_SUPER


def test_s1_operator_cannot_grant_admin(db, admin_client):
    op = _user(db, "op", is_staff=True, role="operator")
    pawn = _user(db, "pawn")
    r = admin_client.put(f"/api/admin/users/{pawn.id}", json={"is_staff": True}, headers=_bearer(op))
    assert r.status_code == 403
    db.refresh(pawn)
    assert not pawn.is_staff and pawn.admin_role is None


def test_s1_super_grant_via_users_endpoint_is_least_privilege(db, admin_client):
    boss = _user(db, "boss", is_staff=True, role="super")
    pawn = _user(db, "pawn")
    r = admin_client.put(f"/api/admin/users/{pawn.id}", json={"is_staff": True}, headers=_bearer(boss))
    assert r.status_code == 200, r.text
    db.refresh(pawn)
    assert pawn.is_staff and pawn.admin_role == admin_roles.ROLE_VIEWER
    assert not admin_roles.is_super(pawn)


def test_s1_legacy_migration_promotes_only_first_admin(db):
    owner = _user(db, "owner", is_staff=True)          # 安装向导建的站长（最早）
    other = _user(db, "other", is_staff=True)          # 另一个空角色老管理员
    explicit = _user(db, "ops", is_staff=True, role="operator")
    result = admin_roles.ensure_legacy_admin_roles(db)
    db.refresh(owner), db.refresh(other), db.refresh(explicit)
    assert owner.admin_role == "super"
    assert other.admin_role is None and admin_roles.role_of(other) == "viewer"
    assert explicit.admin_role == "operator"
    assert result["migrated"] == [owner.id] and result["left_viewer"] == [other.id]
    # 只跑一次：之后新出现的空角色管理员不会被自动提权
    late = _user(db, "late", is_staff=True)
    admin_roles.ensure_legacy_admin_roles(db)
    db.refresh(late)
    assert late.admin_role is None


def test_s1_self_heal_when_no_super(db):
    from backend import models as m

    db.add(m.SystemConfig(key=admin_roles.LEGACY_MIGRATION_KEY, value="1"))
    db.commit()
    a = _user(db, "a", is_staff=True, role="viewer")
    _user(db, "b", is_staff=True, role="operator")
    result = admin_roles.ensure_legacy_admin_roles(db)
    db.refresh(a)
    assert result["healed"] == a.id and a.admin_role == "super"
    # 已有超管时绝不触发
    assert admin_roles.ensure_legacy_admin_roles(db)["healed"] is None


def test_s1_cli_and_setup_admin_is_explicit_super(db, monkeypatch):
    from backend import admin_accounts

    monkeypatch.setattr("backend.emby_server.auth.ensure_emby_credentials",
                        lambda *a, **k: None)
    user, created, _ = admin_accounts.upsert_admin(db, "siteowner", "Str0ng-Passw0rd!x")
    assert created and user.admin_role == "super"


def test_s2_operator_cannot_reset_super_password(db, admin_client):
    op = _user(db, "op", is_staff=True, role="operator")
    boss = _user(db, "boss", is_staff=True, role="super")
    before = boss.password_hash
    r = admin_client.post(f"/api/admin/users/{boss.id}/reset-password",
                          json={"new_password": "hijacked1"}, headers=_bearer(op))
    assert r.status_code == 403
    db.refresh(boss)
    assert boss.password_hash == before


def test_s2_operator_cannot_disable_or_demote_admins(db, admin_client):
    op = _user(db, "op", is_staff=True, role="operator")
    boss = _user(db, "boss", is_staff=True, role="super")
    other_op = _user(db, "op2", is_staff=True, role="operator")
    for target in (boss, other_op):
        for body in ({"is_active": False}, {"is_staff": False}):
            r = admin_client.put(f"/api/admin/users/{target.id}", json=body, headers=_bearer(op))
            assert r.status_code == 403, (target.username, body, r.text)
    db.refresh(boss)
    assert boss.is_active and boss.is_staff


def test_s2_operator_can_still_manage_ordinary_users(db, admin_client, monkeypatch):
    monkeypatch.setattr("backend.emby_server.auth.ensure_emby_credentials",
                        lambda *a, **k: None)
    op = _user(db, "op", is_staff=True, role="operator")
    alice = _user(db, "alice")
    r = admin_client.put(f"/api/admin/users/{alice.id}", json={"is_active": False}, headers=_bearer(op))
    assert r.status_code == 200, r.text
    r = admin_client.post(f"/api/admin/users/{alice.id}/reset-password",
                          json={"new_password": "newpass123"}, headers=_bearer(op))
    assert r.status_code == 200, r.text


def test_s2_super_can_manage_other_admins(db, admin_client):
    boss = _user(db, "boss", is_staff=True, role="super")
    other = _user(db, "boss2", is_staff=True, role="super")
    op = _user(db, "op", is_staff=True, role="operator")
    r = admin_client.put(f"/api/admin/users/{op.id}", json={"is_active": False}, headers=_bearer(boss))
    assert r.status_code == 200, r.text
    r = admin_client.put(f"/api/admin/users/{other.id}", json={"is_staff": False}, headers=_bearer(boss))
    assert r.status_code == 200, r.text
    db.refresh(other)
    assert not other.is_staff and other.admin_role is None


# ===========================================================================
# S3：节点密钥 / 签名，不再发送 SECRET_KEY，不跟随重定向
# ===========================================================================

def test_s3_node_secret_is_not_secret_key(monkeypatch):
    from backend import node_auth

    monkeypatch.delenv("NODE_SHARED_SECRET", raising=False)
    sk = os.environ.get("SECRET_KEY") or ""
    derived = node_auth.node_shared_secret()
    assert derived and derived != sk
    monkeypatch.setenv("NODE_SHARED_SECRET", "explicit-node-secret")
    assert node_auth.node_shared_secret() == "explicit-node-secret"


def test_s3_signed_headers_verify_and_bind_path(monkeypatch):
    from backend import node_auth

    monkeypatch.setenv("NODE_SHARED_SECRET", "n" * 40)
    h = node_auth.signed_headers("GET", "https://ea.example.com/api/admin/mounts/health")
    assert node_auth.PANEL_KEY_HEADER not in h
    assert "n" * 40 not in "".join(h.values())
    assert node_auth.verify_headers(h, "GET", "/api/admin/mounts/health")
    # 重放同一个 nonce → 拒绝
    assert not node_auth.verify_headers(h, "GET", "/api/admin/mounts/health")
    # 换路径（拿去打 bundle）→ 拒绝
    h2 = node_auth.signed_headers("GET", "https://ea.example.com/api/admin/mounts/health")
    assert not node_auth.verify_headers(h2, "GET", "/api/admin/stream-nodes/bundle")
    # 过期 → 拒绝
    h3 = node_auth.signed_headers("GET", "https://x/api/admin/nodes/me")
    later = time.time() + 3600
    monkeypatch.setattr(node_auth.time, "time", lambda: later)
    assert not node_auth.verify_headers(h3, "GET", "/api/admin/nodes/me")


def test_s3_raw_secret_key_no_longer_accepted(monkeypatch):
    from backend import node_auth
    from backend.emby_server import mount_health

    monkeypatch.delenv("NODE_SHARED_SECRET", raising=False)
    sk = os.environ.get("SECRET_KEY") or ""
    assert sk
    req = _request("/api/admin/stream-nodes/bundle", headers={"X-Panel-Key": sk})
    assert not mount_health.panel_key_ok(req)
    # 运维脚本用的静态节点密钥仍可用
    req = _request("/api/admin/stream-nodes/bundle",
                   headers={"X-Panel-Key": node_auth.node_shared_secret()})
    assert mount_health.panel_key_ok(req)


class _FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data or {"service": "ea", "mounts": []}

    def json(self):
        return self._data


def _capture_httpx(monkeypatch, resp):
    import httpx

    seen = {}

    class FakeClient:
        def __init__(self, *a, **kw):
            seen["kwargs"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, **kw):
            seen["url"], seen["headers"] = url, headers or {}
            return resp

        async def post(self, url, headers=None, **kw):
            seen["url"], seen["headers"] = url, headers or {}
            return resp

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return seen


@pytest.mark.parametrize("which", ["mount_health", "node_identity", "push_scan"])
def test_s3_probes_never_send_secret_and_never_follow_redirects(monkeypatch, which):
    import asyncio

    from backend.emby_server import mount_health, nodes

    monkeypatch.delenv("NODE_SHARED_SECRET", raising=False)
    sk = os.environ.get("SECRET_KEY") or ""
    seen = _capture_httpx(monkeypatch, _FakeResp())
    if which == "mount_health":
        out = asyncio.run(mount_health.fetch_ea_health("https://evil.example.com"))
    elif which == "node_identity":
        out = asyncio.run(nodes.fetch_node_identity("https://evil.example.com"))
    else:
        out = asyncio.run(nodes.push_scan("https://evil.example.com", 1))
    assert out["ok"], out
    assert seen["kwargs"].get("follow_redirects") is False
    sent = " ".join(str(v) for v in seen["headers"].values())
    assert sk not in sent
    from backend import node_auth
    assert node_auth.node_shared_secret() not in sent
    assert "X-Panel-Sign" in seen["headers"]


def test_s3_redirect_is_reported_not_followed(monkeypatch):
    import asyncio

    from backend.emby_server import mount_health

    _capture_httpx(monkeypatch, _FakeResp(status=302))
    out = asyncio.run(mount_health.fetch_ea_health("https://ea.example.com"))
    assert not out["ok"] and "重定向" in out["error"]


# ===========================================================================
# H1：按 id 取条目统一过可见范围
# ===========================================================================

def test_h1_require_visible_item_blocks_hidden_library(db, monkeypatch):
    from backend.emby_server import api

    lib_ok = em.Library(guid="lib-ok", name="可见", is_enabled=True)
    lib_hidden = em.Library(guid="lib-hidden", name="内部", is_enabled=True)
    db.add_all([lib_ok, lib_hidden])
    db.flush()
    db.add_all([
        em.MediaItem(guid="visible", library_id=lib_ok.id, item_type="movie", name="a"),
        em.MediaItem(guid="secret", library_id=lib_hidden.id, item_type="movie", name="b"),
    ])
    db.commit()
    user = _user(db, "alice")
    monkeypatch.setattr(api, "_library_scope", lambda _db, _u: {lib_ok.id})
    assert api._require_visible_item(db, user, "visible").guid == "visible"
    with pytest.raises(HTTPException) as exc:
        api._require_visible_item(db, user, "secret")
    assert exc.value.status_code == 403
    monkeypatch.setattr(api, "_library_scope", lambda _db, _u: None)
    assert api._require_visible_item(db, user, "secret").guid == "secret"


def test_h1_guardrail_no_unscoped_item_lookup_in_protocol_routes():
    """协议路由里不许直接 _require_item(...)（只允许 api.py 里 helper 自己调用）"""
    offenders = []
    for name in ("api.py", "compat_routes.py", "media_routes.py", "mount_routes.py",
                 "stream_routes.py", "session_routes.py", "search_api.py", "image_routes.py"):
        path = ROOT / "backend" / "emby_server" / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if fn.name in ("_require_visible_item", "_require_item"):
                continue
            for call in ast.walk(fn):
                if isinstance(call, ast.Call):
                    target = call.func
                    fname = getattr(target, "id", None) or getattr(target, "attr", None)
                    if fname == "_require_item":
                        offenders.append(f"{name}:{call.lineno}")
                    # run_db(_require_item, ...)
                    for arg in call.args:
                        if isinstance(arg, ast.Name) and arg.id == "_require_item":
                            offenders.append(f"{name}:{call.lineno}")
    assert not offenders, "协议端点必须用 _require_visible_item：" + ", ".join(offenders)


def _two_libs(db):
    lib_ok = em.Library(guid="lib-ok", name="可见", is_enabled=True)
    lib_hidden = em.Library(guid="lib-hidden", name="内部", is_enabled=True)
    db.add_all([lib_ok, lib_hidden])
    db.flush()
    db.add_all([
        em.MediaItem(guid="visible", library_id=lib_ok.id, item_type="series", name="a", tmdb_id="1"),
        em.MediaItem(guid="secret", library_id=lib_hidden.id, item_type="series", name="b", tmdb_id="2"),
    ])
    db.commit()
    return lib_ok, lib_hidden


def test_h1_strmassistant_item_routes_enforce_visibility(db, monkeypatch):
    """main 新增的按条目 id 路由（多版本 / 片头标记 / 剧集组 / 缩略图 / 缺失集）同样要校验可见库"""
    from backend.emby_server import api, media_routes

    lib_ok, _ = _two_libs(db)
    user = _user(db, "alice")
    monkeypatch.setattr(api, "_library_scope", lambda _db, _u: {lib_ok.id})
    req = _request("/Items/secret/AlternateVersions")
    calls = [
        lambda: media_routes.item_alternate_versions("secret", req, user, db),
        lambda: media_routes.item_intro_markers("secret", user, db),
        lambda: media_routes.item_episode_groups("secret", user, db),
        lambda: media_routes.item_thumbnails("secret", req, user, db),
        lambda: media_routes.item_thumbnail("secret", 0, req, user, db),
        lambda: api.get_missing_episodes("secret", user, db),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403
    # 可见库里的条目照常
    assert media_routes.item_intro_markers("visible", user, db)["Markers"] == []


def test_h1_shared_writes_need_writable_admin(db, monkeypatch):
    """片头标记 / 剧集组选择是全站共享数据：普通用户与只读管理员都不能改"""
    from backend.emby_server import api, media_routes

    lib_ok, _ = _two_libs(db)
    monkeypatch.setattr(api, "_library_scope", lambda _db, _u: {lib_ok.id})
    alice = _user(db, "alice")
    viewer = _user(db, "viewer", is_staff=True, role=admin_roles.ROLE_VIEWER)
    for u in (alice, viewer):
        for method, call in (
            ("POST", lambda r: media_routes.item_intro_marker_set("visible", r, "intro", 0, 1000, u, db)),
            ("DELETE", lambda r: media_routes.item_intro_marker_delete("visible", "intro", r, u, db)),
            ("PUT", lambda r: media_routes.item_episode_group_select("visible", r, "g1", u, db)),
        ):
            with pytest.raises(HTTPException) as exc:
                call(_request("/Items/visible/IntroMarkers", method=method))
            assert exc.value.status_code == 403


def test_h1_guardrail_no_raw_guid_lookup_in_item_routes():
    """路由函数里不许直接按 guid 查 MediaItem 绕过可见性（图片端点除外：<img> 不带凭据）"""
    allow = {("media_routes.py", "item_image"), ("portal.py", "admin_delete_item")}
    offenders = []
    for name in ("api.py", "compat_routes.py", "media_routes.py", "mount_routes.py",
                 "stream_routes.py", "session_routes.py", "search_api.py", "image_routes.py"):
        path = ROOT / "backend" / "emby_server" / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if not any("router" in ast.unparse(d) for d in fn.decorator_list):
                continue
            if (name, fn.name) in allow:
                continue
            for cmp in ast.walk(fn):
                if (isinstance(cmp, ast.Compare) and "MediaItem.guid" in ast.unparse(cmp.left)
                        and any(isinstance(c, ast.Name) and c.id == "item_id" for c in cmp.comparators)):
                    offenders.append(f"{name}:{fn.name}:{cmp.lineno}")
    assert not offenders, "按条目 id 的路由必须用 _require_visible_item：" + ", ".join(offenders)


# ===========================================================================
# H2：JWT 不进 URL；网页端改用短期播放签名
# ===========================================================================

def test_h2_api_key_for_never_echoes_jwt(db):
    from backend.emby_server import api

    jwt = create_access_token(1, {"username": "x"})
    assert api._api_key_for(db, _request(headers={"Authorization": f"Bearer {jwt}"})) == ""
    assert api._api_key_for(db, _request(query=f"api_key={jwt}".encode())) == ""
    emby_tok = "a" * 40
    assert api._api_key_for(db, _request(headers={"X-Emby-Token": emby_tok})) == emby_tok
    assert api._api_key_for(db, _request(query=f"api_key={emby_tok}".encode())) == emby_tok


def test_h2_url_auth_qs_uses_play_sign_for_portal(db):
    from backend.emby_server import api, play_sign

    user = _user(db, "alice")
    jwt = create_access_token(user.id, {"username": "alice"})
    qs = api._url_auth_qs(db, _request(headers={"Authorization": f"Bearer {jwt}"}), user, "guid1")
    assert jwt not in qs and "api_key" not in qs
    parts = dict(p.split("=", 1) for p in qs.split("&"))
    assert play_sign.verify_play_sign(int(parts["uid"]), "guid1", int(parts["exp"]), parts["sign"])
    qs2 = api._url_auth_qs(db, _request(headers={"X-Emby-Token": "b" * 40}), user, "guid1")
    assert qs2 == "api_key=" + "b" * 40


def test_h2_echo_auth_qs_rejects_jwt():
    """_echo_auth_qs 绝不回显 JWT（2026-10-17 转码删除：_rewrite_playlist
    已不存在，原 HLS 播放列表签名回显半段随之移除，只保留 JWT 拒绝半段）。"""
    from backend.emby_server import api

    jwt = create_access_token(1, {"username": "x"})
    req = _request(query=f"session=s&api_key={jwt}".encode())
    assert api._echo_auth_qs(req) == ""


def test_h2_emby_user_rejects_jwt_in_query(db):
    from backend.emby_server import auth

    user = _user(db, "alice")
    jwt = create_access_token(user.id, {"username": "alice"})
    with pytest.raises(HTTPException) as exc:
        auth.get_emby_user(_request(query=f"api_key={jwt}".encode()), db, None)
    assert exc.value.status_code == 401
    # 请求头里的 JWT 仍然可用（网页端 fetch 走 Authorization 头）
    ok = auth.get_emby_user(_request(headers={"Authorization": f"Bearer {jwt}"}), db,
                            HTTPAuthorizationCredentials(scheme="Bearer", credentials=jwt))
    assert ok.id == user.id
    with pytest.raises(HTTPException):
        auth.get_admin_or_emby_user(_request(query=f"api_key={jwt}".encode()), db)
    assert auth.resolve_request_user(db, _request(query=f"api_key={jwt}".encode())) is None


def test_h2_frontends_do_not_put_tokens_in_image_urls():
    emby_ts = (ROOT / "user_frontend" / "src" / "api" / "emby.ts").read_text(encoding="utf-8")
    assert "api_key=${encodeURIComponent(token)}" not in emby_ts
    player = (ROOT / "web_player" / "index.html").read_text(encoding="utf-8")
    assert "'&api_key=' + encodeURIComponent(store.k.token)" not in player


# ===========================================================================
# H3：/api/admin/emby/* 只认 Authorization 头里的管理员 JWT
# ===========================================================================

def test_h3_require_staff_rejects_emby_token_and_query_jwt(db):
    from backend.emby_server import auth as emby_auth
    from backend.emby_server import portal

    boss = _user(db, "boss", is_staff=True, role="super")
    raw = "c" * 40
    db.add(em.EmbyApiToken(token=emby_auth.hash_emby_token(raw), user_id=boss.id,
                           device_id="infuse", is_revoked=False))
    db.commit()
    path = "/api/admin/emby/libraries"
    # Emby 客户端 token（请求头 / 查询串）→ 401
    for req in (_request(path, headers={"X-Emby-Token": raw}),
                _request(path, query=f"api_key={raw}".encode())):
        with pytest.raises(HTTPException) as exc:
            portal._bearer_admin(req, None, db)
        assert exc.value.status_code == 401
    # Bearer 里塞 Emby token → 401（不是 JWT）
    with pytest.raises(HTTPException) as exc:
        portal._bearer_admin(_request(path), HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw), db)
    assert exc.value.status_code == 401
    # URL 里的管理员 JWT → 401
    jwt = create_access_token(boss.id, {"username": "boss", "staff": True})
    with pytest.raises(HTTPException) as exc:
        portal._bearer_admin(_request(path, query=f"api_key={jwt}".encode()), None, db)
    assert exc.value.status_code == 401
    # 正路：Authorization: Bearer <admin JWT>
    got = portal._bearer_admin(_request(path), HTTPAuthorizationCredentials(scheme="Bearer", credentials=jwt), db)
    assert got.id == boss.id
    # 非管理员 JWT → 403
    alice = _user(db, "alice")
    with pytest.raises(HTTPException) as exc:
        portal._bearer_admin(_request(path), HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=create_access_token(alice.id)), db)
    assert exc.value.status_code == 403


# ===========================================================================
# H5：多次可用码每人限一次
# ===========================================================================

@pytest.fixture()
def economy_client(db, monkeypatch):
    from backend.api import economy
    from backend.api.user import get_current_user
    from backend.database import get_db

    async def _noop(**kw):
        return None

    monkeypatch.setattr(economy, "notify_admin_event", _noop)
    monkeypatch.setattr(economy, "check_rate_limit", lambda *a, **k: (True, 0))
    app = FastAPI()
    app.include_router(economy.router)
    state = {"user": None}

    def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    with TestClient(app) as c:
        yield c, state


def test_h5_exchange_code_once_per_user(db, economy_client):
    client, state = economy_client
    db.add(models.ExchangeCode(code="PROMO100", type="points", points_value=10,
                               max_uses=100, is_active=True))
    db.add(models.ExchangeCode(code="UNLIMITED", type="points", points_value=5,
                               is_active=True))
    db.commit()
    db.query(models.ExchangeCode).filter_by(code="UNLIMITED").update({"max_uses": None})
    db.commit()
    alice, bob = _user(db, "alice"), _user(db, "bob")
    for code in ("PROMO100", "UNLIMITED"):
        state["user"] = alice
        assert client.post("/api/user/economy/exchange/redeem", json={"code": code}).status_code == 200
        r = client.post("/api/user/economy/exchange/redeem", json={"code": code})
        assert r.status_code == 400 and "兑换过" in r.json()["detail"]
        state["user"] = bob
        assert client.post("/api/user/economy/exchange/redeem", json={"code": code}).status_code == 200
    db.refresh(alice)
    assert alice.points == 15
    row = db.query(models.ExchangeCode).filter_by(code="PROMO100").one()
    assert row.use_count == 2


def test_h5_legacy_used_by_is_respected(db, economy_client):
    client, state = economy_client
    alice = _user(db, "alice")
    db.add(models.ExchangeCode(code="OLDCODE1", type="points", points_value=10,
                               max_uses=10, use_count=1, used_by=str(alice.id), is_active=True))
    db.commit()
    state["user"] = alice
    assert client.post("/api/user/economy/exchange/redeem", json={"code": "OLDCODE1"}).status_code == 400


def test_h5_db_unique_constraint_blocks_concurrent_duplicate(db):
    """并发兜底：即使绕过预检查，同一 (码, 用户) 第二条核销记录也写不进去"""
    from backend import codes

    alice = _user(db, "alice")
    assert codes.record_redemption(db, codes.REDEMPTION_KIND_EXCHANGE, 7, alice.id)
    db.commit()
    assert not codes.record_redemption(db, codes.REDEMPTION_KIND_EXCHANGE, 7, alice.id)
    assert db.query(models.CodeRedemption).count() == 1


def test_h5_membership_code_once_per_user(db, monkeypatch):
    from backend import codes

    monkeypatch.setattr(codes, "grant_membership_days",
                        lambda *a, **k: type("S", (), {"end_date": __import__("datetime").datetime.now()})())
    monkeypatch.setattr(codes, "code_realm_id", lambda *a, **k: None)
    monkeypatch.setattr(codes, "realm_label", lambda *a, **k: "主服")
    db.add(models.RegistrationCode(code="REN-TEAM-50", code_type=codes.CODE_TYPE_RENEW,
                                   days=30, max_uses=50, is_active=True))
    db.commit()
    alice, bob = _user(db, "alice"), _user(db, "bob")
    assert codes.redeem_code(db, alice, "REN-TEAM-50")["success"]
    db.commit()
    second = codes.redeem_code(db, alice, "REN-TEAM-50")
    assert not second["success"] and "已经使用过" in second["message"]
    db.rollback()
    assert codes.redeem_code(db, bob, "REN-TEAM-50")["success"]
    db.commit()
    code = db.query(models.RegistrationCode).filter_by(code="REN-TEAM-50").one()
    assert code.use_count == 2
    # claim_code 本身也拒绝同一人第二次（注册 / 其他调用方共用）
    assert not codes.claim_code(db, code, alice.id)

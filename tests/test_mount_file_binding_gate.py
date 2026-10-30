"""挂载版 /Items/{id}/File 的绑定门禁（360° 审计 A-007）。

旧版 item_file（stream_routes）把 File 定性为「新播放」并过绑定门禁；
挂载替换版 mounted_item_file 曾静默丢了这道门禁，未绑定用户可经此口
拿到共享原件 302 直链。本文件锁定三条行为＋一条路由表断言：

- 未绑定用户调 mounted File → 403 绑定引导文案，拿不到 302；
- 已绑定用户 → 正常放行（302 直链）；
- 带有效 play_sign 的已下发链接消费 → 不追溯，放行；
- 签名无效时仍按门禁判定 → 403；
- 经 install_mount_routes 替换后，路由表上 served 的 File 端点就是
  mounted_item_file，且该端点本身带门禁（防下次替换再丢）。
"""

import types

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mount_routes
from backend.emby_server import playback_gate as gate


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, name):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id, status="active", provider="drive"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status=status,
    )
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label="盘",
        credential_id=cred.id, status=status,
    )
    db.add(binding)
    db.commit()
    return binding


def _plain_request():
    return types.SimpleNamespace(query_params={}, path_params={"item_id": "guid-1"})


def _signed_request(valid=True):
    req = types.SimpleNamespace(
        query_params={"sign": "s", "exp": "9999999999", "uid": "7"},
        path_params={"item_id": "guid-1"},
    )
    req._valid = valid
    return req


@pytest.fixture()
def _mounted(monkeypatch):
    """把 mounted_item_file 的非门禁依赖全部钉住，只留绑定门禁待测。"""
    item = types.SimpleNamespace(drive_file_id="fid-123")
    monkeypatch.setattr(mount_routes, "_require_visible_item", lambda db, user, item_id: item)
    monkeypatch.setattr(mount_routes, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(mount_routes, "_play_target", lambda db, item: types.SimpleNamespace(kind="url", value=""))
    from backend.emby_server import drive_auth
    monkeypatch.setattr(drive_auth, "drive_usercontent_download_url", lambda fid: "https://example.invalid/dl/" + fid)


def test_unbound_user_blocked_on_mounted_file(db, _mounted):
    user = _user(db, "u1")
    with pytest.raises(HTTPException) as excinfo:
        mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == gate.GATE_MESSAGE


def test_bound_user_passes_gate_to_302(db, _mounted):
    user = _user(db, "u1")
    _binding(db, user.id, "active")
    resp = mount_routes.mounted_item_file("guid-1", _plain_request(), user, db)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://example.invalid/dl/")


def test_issued_play_sign_exempt_for_unbound_user(db, _mounted, monkeypatch):
    user = _user(db, "u1")
    from backend.emby_server import play_sign
    monkeypatch.setattr(play_sign, "hotlink_enabled", lambda db: True)
    monkeypatch.setattr(play_sign, "verify_play_sign", lambda uid, item_id, exp, sign: True)
    resp = mount_routes.mounted_item_file("guid-1", _signed_request(), user, db)
    assert resp.status_code == 302


def test_invalid_play_sign_still_blocked(db, _mounted, monkeypatch):
    user = _user(db, "u1")
    from backend.emby_server import play_sign
    monkeypatch.setattr(play_sign, "hotlink_enabled", lambda db: True)
    monkeypatch.setattr(play_sign, "verify_play_sign", lambda uid, item_id, exp, sign: False)
    with pytest.raises(HTTPException) as excinfo:
        mount_routes.mounted_item_file("guid-1", _signed_request(), user, db)
    assert excinfo.value.status_code == 403


def _old_file_route(path):
    def item_file():
        pass
    return types.SimpleNamespace(path=path, endpoint=item_file)


class _FakeRouter:
    def __init__(self, routes):
        self.routes = routes

    def get(self, path):
        def deco(fn):
            self.routes.append(types.SimpleNamespace(path=path, endpoint=fn))
            return fn
        return deco


def test_install_mount_routes_serves_gated_endpoint(db, _mounted):
    router = _FakeRouter(routes=[
        _old_file_route("/emby/Items/{item_id}/File"),
        _old_file_route("/Items/{item_id}/File"),
        types.SimpleNamespace(path="/other", endpoint=lambda: None),
    ])
    replaced = mount_routes.install_mount_routes(router)
    assert replaced == 2
    file_routes = [r for r in router.routes if r.path in mount_routes.SUPERSEDED_FILE_PATHS]
    assert len(file_routes) == 2
    assert all(r.endpoint is mount_routes.mounted_item_file for r in file_routes)
    # 路由表上 served 的端点本身必须带门禁：未绑定用户调它应被拦
    user = _user(db, "u1")
    with pytest.raises(HTTPException) as excinfo:
        file_routes[0].endpoint("guid-1", _plain_request(), user, db)
    assert excinfo.value.status_code == 403

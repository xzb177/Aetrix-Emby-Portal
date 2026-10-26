"""播放门禁（2026-10-26 用户亲定硬规矩）。

用例三类：
- 未绑定拒绝：无绑定 / 仅 invalid / 仅 expired / 别人的绑定，一律 403；
- 已绑定放行：active 的 drive 或 pan115 任一都算；
- 浏览不受影响：门禁判定只读绑定行，不碰条目/浏览状态（同一用户在
  有路径条目与无路径条目下判定结果一致；浏览路由本身不调本模块，
  见 PR diff：调用点只在 playback_info / video_stream / upstream_stream /
  item_file 四处）。
- 已下发链接不追溯：带有效 play_sign 的请求放行，签名无效/防盗链关时
  仍按门禁判定。
"""

import types

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
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


def _binding(db, user_id, status, provider="drive", label="盘"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status=status,
    )
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=cred.id, status=status,
    )
    db.add(binding)
    db.commit()
    return binding


def _assert_blocked(db, user_id):
    user = db.query(base_models.WebUser).filter(
        base_models.WebUser.id == user_id).first()
    with pytest.raises(HTTPException) as excinfo:
        gate.ensure_binding_playback_allowed(db, user)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == gate.GATE_MESSAGE


def test_gate_rejects_user_without_bindings(db):
    user = _user(db, "u1")
    _assert_blocked(db, user.id)


def test_gate_rejects_invalid_only(db):
    user = _user(db, "u1")
    _binding(db, user.id, "invalid")
    _assert_blocked(db, user.id)


def test_gate_rejects_expired_only(db):
    user = _user(db, "u1")
    _binding(db, user.id, "expired")
    _assert_blocked(db, user.id)


def test_gate_allows_active_drive(db):
    user = _user(db, "u1")
    _binding(db, user.id, "active", provider="drive")
    gate.ensure_binding_playback_allowed(db, user)


def test_gate_allows_active_pan115(db):
    user = _user(db, "u1")
    _binding(db, user.id, "active", provider="pan115")
    gate.ensure_binding_playback_allowed(db, user)


def test_gate_allows_active_among_invalid(db):
    user = _user(db, "u1")
    _binding(db, user.id, "invalid", label="旧盘")
    _binding(db, user.id, "active", label="新盘")
    gate.ensure_binding_playback_allowed(db, user)


def test_gate_ignores_other_users_bindings(db):
    owner = _user(db, "owner")
    stranger = _user(db, "stranger")
    _binding(db, owner.id, "active")
    _assert_blocked(db, stranger.id)


def _signed_request():
    return types.SimpleNamespace(
        query_params={"sign": "s", "exp": "9999999999", "uid": "7"},
        path_params={"item_id": "guid-1"},
    )


def test_gate_exempts_issued_play_sign(db, monkeypatch):
    user = _user(db, "u1")
    monkeypatch.setattr(
        "backend.emby_server.play_sign.hotlink_enabled", lambda db: True)
    monkeypatch.setattr(
        "backend.emby_server.play_sign.verify_play_sign",
        lambda uid, item_id, exp, sign: True)
    gate.ensure_binding_playback_allowed(db, user, _signed_request())


def test_gate_rejects_invalid_play_sign(db, monkeypatch):
    user = _user(db, "u1")
    monkeypatch.setattr(
        "backend.emby_server.play_sign.hotlink_enabled", lambda db: True)
    monkeypatch.setattr(
        "backend.emby_server.play_sign.verify_play_sign",
        lambda uid, item_id, exp, sign: False)
    with pytest.raises(HTTPException) as excinfo:
        gate.ensure_binding_playback_allowed(db, user, _signed_request())
    assert excinfo.value.status_code == 403


def test_gate_rejects_when_hotlink_disabled(db, monkeypatch):
    user = _user(db, "u1")
    monkeypatch.setattr(
        "backend.emby_server.play_sign.hotlink_enabled", lambda db: False)
    monkeypatch.setattr(
        "backend.emby_server.play_sign.verify_play_sign",
        lambda uid, item_id, exp, sign: True)
    with pytest.raises(HTTPException) as excinfo:
        gate.ensure_binding_playback_allowed(db, user, _signed_request())
    assert excinfo.value.status_code == 403


def test_gate_ignores_item_state(db):
    """浏览不受影响：门禁只读绑定行，条目状态不改变判定结果。"""
    lib = em.Library(guid="lib1", name="lib1")
    db.add(lib)
    db.commit()
    with_path = em.MediaItem(
        guid="g1", library_id=lib.id, item_type="movie", name="有路径",
        file_path="mount://1/f.mkv")
    without_path = em.MediaItem(
        guid="g2", library_id=lib.id, item_type="movie", name="无路径",
        file_path="")
    db.add_all([with_path, without_path])
    db.commit()
    bound = _user(db, "bound")
    _binding(db, bound.id, "active")
    unbound = _user(db, "unbound")
    # 同一门禁函数在两种条目状态下结论不变：只看绑定，不看条目。
    gate.ensure_binding_playback_allowed(db, bound)
    gate.ensure_binding_playback_allowed(db, bound)
    _assert_blocked(db, unbound.id)
    _assert_blocked(db, unbound.id)

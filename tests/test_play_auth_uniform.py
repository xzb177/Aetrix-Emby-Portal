"""r62：播放投递端点鉴权统一为 get_play_user。

``/Items/{id}/Download``、``/Items/{id}/File``（mount_routes 的挂载感知实现与
stream_routes 的本机实现）、``/Videos/{id}/hls1`` 此前只认 ``get_emby_user``：
H2 之后门户网页端凭 uid/exp/sign 播放签名请求这些端点会被 401，而其它播放
端点（stream/字幕/HLS/PlaybackInfo）都走 ``get_play_user``（签名优先，回退
Emby token/JWT）。本轮把四个端点统一为同一份鉴权依赖。
"""
import inspect
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.security as sec

sec.SECRET_KEY = "test-secret-key-for-play-sign-0000"

from backend import models
from backend.emby_server import api as api_mod
from backend.emby_server import auth, media_routes, mount_routes, play_sign, stream_routes

# 全部媒体投递端点：必须走签名优先的 get_play_user（横切只许一套实现）
_MEDIA_ENDPOINTS = [
    (api_mod, "video_stream_ext"),
    (api_mod, "video_hls"),
    (stream_routes, "video_stream_container"),
    (stream_routes, "item_file"),
    (stream_routes, "video_subtitle"),
    (stream_routes, "video_subtitle_offset"),
    (stream_routes, "video_hls_upper"),
    (media_routes, "download_item"),
    (media_routes, "video_hls1"),
    (mount_routes, "mounted_item_file"),
]


def test_all_media_endpoints_use_get_play_user():
    """静态断言：媒体投递端点一律声明 Depends(get_play_user)，不留 get_emby_user。"""
    bad = []
    for mod, name in _MEDIA_ENDPOINTS:
        src = inspect.getsource(getattr(mod, name))
        if "Depends(get_play_user)" not in src or "Depends(get_emby_user)" in src:
            bad.append(f"{mod.__name__}.{name}")
    assert not bad, f"仍用 get_emby_user 的投递端点: {bad}"


@pytest.fixture()
def signed_env(monkeypatch):
    """sqlite 最小环境：用户 + 媒体库 + 带本机文件的 movie 条目；返回 (db, user, item, tmp_path)。"""
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    from backend.emby_server import models as em

    em.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    lib = em.Library(guid="lib-r62", name="电影", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.commit()

    tmp = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False)
    tmp.write(b"x" * 4096)
    tmp.close()

    user = models.WebUser(username="r62dl", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    item = em.MediaItem(guid="g-r62-dl", name="R62", item_type="movie",
                        library_id=lib.id, file_path=tmp.name)
    db.add(item)
    db.commit()
    db.refresh(user)

    # 可见范围：测试里不测库权限，直接放行
    monkeypatch.setattr(api_mod, "_library_scope", lambda db_, user_: None)
    # 付费墙/下载开关：不是本轮范围，跳过
    monkeypatch.setattr(media_routes, "ensure_playback_allowed", lambda *a, **k: None)
    monkeypatch.setattr(media_routes, "ensure_download_allowed", lambda *a, **k: None)
    yield db, user, item, tmp.name
    os.unlink(tmp.name)


def _signed_request(item_guid, uid, exp, sign):
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": f"/emby/Items/{item_guid}/Download",
        "query_string": f"uid={uid}&exp={exp}&sign={sign}".encode(),
        "headers": [], "client": ("127.0.0.1", 5000), "server": ("t", 80),
        "path_params": {"item_id": item_guid},
    }
    return Request(scope)


def test_signed_download_without_any_other_credential(signed_env):
    """只带 uid/exp/sign（无 api_key、无请求头凭据）的下载请求必须放行。"""
    db, user, item, _ = signed_env
    exp, sign = play_sign.issue_play_sign(user.id, item.guid)
    req = _signed_request(item.guid, user.id, exp, sign)

    resolved = auth.get_play_user(req, db, None)
    assert resolved.id == user.id

    resp = media_routes.download_item(item.guid, req, resolved, db)
    assert isinstance(resp, FileResponse)
    assert resp.status_code == 200


def test_tampered_sign_rejected(signed_env):
    """签名被篡改 → 403（与其它播放端点同口径）。"""
    db, user, item, _ = signed_env
    exp, sign = play_sign.issue_play_sign(user.id, item.guid)
    bad = ("0" if sign[0] != "0" else "1") + sign[1:]
    req = _signed_request(item.guid, user.id, exp, bad)
    with pytest.raises(HTTPException) as ei:
        auth.get_play_user(req, db, None)
    assert ei.value.status_code == 403


def test_no_credential_still_401(signed_env):
    """无签名、无 token、无 JWT 的请求仍然 401（没有放宽鉴权）。"""
    db, user, item, _ = signed_env
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": f"/emby/Items/{item.guid}/Download",
        "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 5000), "server": ("t", 80),
        "path_params": {"item_id": item.guid},
    }
    with pytest.raises(HTTPException) as ei:
        auth.get_play_user(Request(scope), db, None)
    assert ei.value.status_code == 401

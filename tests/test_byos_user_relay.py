"""BYOS 用户令牌中继（Drive 腿，闭环 #13）。

锁三件事（115 腿已通，不在本文件）：
- streaming.serve_drive_relay_async(access_token=...)：给了用户令牌就
  直接用、只试一次，上游 403 原样抛，绝不咨询共享 SA 池、绝不换
  凭据重试；
- api.video_stream 的 byos 分支：byos:// 条目 + 中继开关开时走绑定
  令牌中继；绑定缺失/非 active/非 drive → 409、他人绑定 → 403、
  令牌失败 → 503，任何一步都不静默回落 SA 中继；
- 非 byos 条目：分支跳过，现有行为逐字不变。

桩 harness 仿照 tests/test_drive_relay.py 的 video_stream 用例。
"""
import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import playback_strategy as _strategy
from backend.emby_server import streaming
from backend.emby_server.drive_oauth import DriveOAuthError


def _run(coro):
    return asyncio.run(coro)


class _FakeQuery:
    def __init__(self, row=None):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def one_or_none(self):
        return None

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, row=None):
        self._row = row

    def query(self, *args, **kwargs):
        return _FakeQuery(self._row)


def _fake_request():
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _setup_video_stream(monkeypatch, item, user, binding_row,
                        relay_behavior=None, token_behavior=None):
    """video_stream 桩：与 test_drive_relay 的接线用例同口径。

    返回 (relay_calls, token_calls, sa_calls, notes)。
    relay_behavior 默认返回 200 Response；token_behavior 默认返回
    "USERTOK"；SA 池（drive_changes.get_token_for_drive）默认一调就
    断言失败——byos 路上它绝不许被咨询。
    """
    from backend.emby_server import api
    from backend.emby_server import drive_changes as _dc

    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, u, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, u: None)
    monkeypatch.setattr(api, "_enforce_path_access",
                        lambda db, u, it: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db, req, u, it, item_id: None)
    monkeypatch.setattr(api, "_observe_traffic", lambda resp: resp)
    monkeypatch.setattr(api.play_sign, "check_referer",
                        lambda request, db: None)

    relay_calls = []

    async def fake_relay(request, drive_id, file_id, media_type="video/mp4",
                         pt=None, access_token=None):
        relay_calls.append({"drive_id": drive_id, "file_id": file_id,
                            "media_type": media_type,
                            "access_token": access_token})
        if relay_behavior is not None:
            return relay_behavior(relay_calls[-1])
        return Response(content=b"x", media_type="video/mp4")

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)

    token_calls = []

    def fake_token(db, binding, http_post=None):
        token_calls.append(binding)
        if token_behavior is not None:
            return token_behavior(binding)
        return "USERTOK"

    from backend.emby_server import drive_oauth as _do
    monkeypatch.setattr(_do, "get_binding_drive_access_token", fake_token)

    sa_calls = []

    def fake_sa(drive_id, force_refresh=False):
        sa_calls.append(drive_id)
        raise AssertionError("byos 路上不许咨询共享 SA 池")

    monkeypatch.setattr(_dc, "get_token_for_drive", fake_sa)

    notes = []

    def fake_note(item_, **kwargs):
        notes.append(kwargs)

    monkeypatch.setattr(api, "_note_play_start_safe", fake_note)

    def fake_get(db, key, default="", **kwargs):
        if key == _strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == _strategy.KEY_DRIVE_RELAY:
            return "true"
        return default

    monkeypatch.setattr(_strategy.store, "get_value", fake_get)
    return relay_calls, token_calls, sa_calls, notes


def _byos_item():
    return SimpleNamespace(id="item1", drive_file_id="FID7",
                           container="mkv", file_path="byos://7/movie.mkv")


def _binding(**over):
    row = {"id": 7, "user_id": 11, "provider": "drive",
           "status": "active"}
    row.update(over)
    return SimpleNamespace(**row)


def _user():
    return SimpleNamespace(id=11)


def test_byos_drive_uses_binding_token_not_sa(monkeypatch):
    from backend.emby_server import api

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), _binding())
    resp = _run(api.video_stream(
        "item1", _fake_request(), _user(), _FakeDB(_binding())))
    assert resp.status_code == 200
    assert relay == [{"drive_id": "byos:7", "file_id": "FID7",
                      "media_type": "video/mkv", "access_token": "USERTOK"}]
    assert len(token) == 1 and token[0].id == 7
    assert sa == []
    assert notes and notes[0]["method"] == "relay"
    assert notes[0]["variant"] == "drive_relay"
    assert notes[0]["result"] == "ok"


def test_byos_no_variant_ignores_drive_direct_first(monkeypatch):
    """#1353 回归：Drive 路由首选改成直链后，无变体请求的 BYOS 条目
    仍须走 BYOS 用户令牌中继，不许因 drive_direct 是首选就跳过本分支
    落回标准流程（标准流程的 _play_target 会把 byos:// 当成本地文件、
    绕过绑定检查；CI 原 6 个 BYOS 用例即因此红）。

    这里把 drive_route_choice 显式钉成直链首选，BYOS 中继仍须跑。
    """
    from backend.emby_server import api
    from backend.emby_server import play_variants as _pv
    from backend.emby_server import variant_switch as _vs

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), _binding())
    monkeypatch.setattr(
        _vs, "drive_route_choice",
        lambda db: ((_pv.DRIVE_DIRECT, None),
                    [(_pv.DRIVE_DIRECT, None), (_pv.DRIVE_RELAY, None)], []))
    resp = _run(api.video_stream(
        "item1", _fake_request(), _user(), _FakeDB(_binding())))
    assert resp.status_code == 200
    assert relay == [{"drive_id": "byos:7", "file_id": "FID7",
                      "media_type": "video/mkv", "access_token": "USERTOK"}]
    assert sa == []


def test_byos_other_users_binding_403_and_no_relay(monkeypatch):
    from backend.emby_server import api

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), _binding(user_id=99))
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(),
            _FakeDB(_binding(user_id=99))))
    assert ei.value.status_code == 403
    assert relay == [] and token == [] and sa == []


def test_byos_missing_binding_403_gate_no_sa_fallback(monkeypatch):
    """无绑定用户：播放门禁 403 先于 byos 分支的 409（2026-10-26 硬规矩）。

    门禁上线前本用例断言 409（byos 分支“绑定缺失”）；门禁上线后，
    名下完全无绑定的用户在门禁处即被 403 拦下，不再走到 byos 分支。
    “绝不静默回落 SA”这一核心性质不变（sa == []）；绑定不可用
    （expired / 非 drive）时仍走 byos 分支的 409，见相邻用例。
    """
    from backend.emby_server import api

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), None)
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(), _FakeDB(None)))
    assert ei.value.status_code == 403
    assert "绑定" in ei.value.detail
    assert relay == [] and token == [] and sa == []


def test_byos_non_active_binding_409_no_sa_fallback(monkeypatch):
    from backend.emby_server import api

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), _binding(status="expired"))
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(),
            _FakeDB(_binding(status="expired"))))
    assert ei.value.status_code == 409
    assert relay == [] and token == [] and sa == []


def test_byos_non_drive_provider_409(monkeypatch):
    from backend.emby_server import api

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), _binding(provider="pan115"))
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(),
            _FakeDB(_binding(provider="pan115"))))
    assert ei.value.status_code == 409
    assert relay == [] and token == [] and sa == []


def test_byos_expired_token_503_rebind_message_no_relay(monkeypatch):
    from backend.emby_server import api

    def boom(binding):
        raise DriveOAuthError("expired", "登录已过期，请重新绑定。")

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), _binding(),
        token_behavior=boom)
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(), _FakeDB(_binding())))
    assert ei.value.status_code == 503
    assert "重新绑定" in ei.value.detail
    assert relay == [] and sa == []


def test_byos_transient_token_503_retryable_no_relay(monkeypatch):
    from backend.emby_server import api

    def boom(binding):
        raise DriveOAuthError("transient", "刷新令牌时上游限流，可重试。")

    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, _byos_item(), _user(), _binding(),
        token_behavior=boom)
    with pytest.raises(HTTPException) as ei:
        _run(api.video_stream(
            "item1", _fake_request(), _user(), _FakeDB(_binding())))
    assert ei.value.status_code == 503
    assert "重试" in ei.value.detail
    assert relay == [] and sa == []


def test_non_byos_item_skips_branch(monkeypatch):
    """非 byos 条目：byos 分支不进中继，落回标准流程（此处桩到 serve_file）。"""
    from backend.emby_server import api

    item = SimpleNamespace(id="item2", drive_file_id="FID9",
                           container="mkv", file_path="mount://3/x.mkv")
    relay, token, sa, notes = _setup_video_stream(
        monkeypatch, item, _user(), None)
    monkeypatch.setattr(api, "_play_target",
                        lambda db, it, variant=None: SimpleNamespace(
                            kind="file", value="/tmp/nope.mkv"))
    monkeypatch.setattr(
        api, "serve_file",
        lambda path, request, media_type, cache_control=None: Response(
            content=b"f", media_type="video/mp4"))
    # 直接播放开关关：标准中继分支也不进，流程直达 serve_file。
    from backend.emby_server import playback_strategy as _st

    def fake_get(db, key, default="", **kwargs):
        if key == _st.KEY_DRIVE_DIRECT_PLAY:
            return "false"
        if key == _st.KEY_DRIVE_RELAY:
            return "true"
        return default

    monkeypatch.setattr(_st.store, "get_value", fake_get)
    # 门禁上线后：分支跳过逻辑须用已绑定用户测（无绑定用户在门禁处即 403）。
    resp = _run(api.video_stream(
        "item2", _fake_request(), _user(), _FakeDB(_binding())))
    assert resp.status_code == 200
    assert relay == [] and token == [] and sa == []


# ---------- streaming 层：access_token 单次直试 ----------

def _patch_mount(monkeypatch, behavior):
    calls = []

    async def fake_mount(url, request, headers=None, media_type="video/mp4",
                         pt=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return behavior(len(calls), headers)

    monkeypatch.setattr(streaming, "serve_mount_relay_async", fake_mount)
    return calls


def _patch_sa_token(monkeypatch):
    calls = []

    import backend.emby_server.drive_changes as _dc

    def fake_token(drive_id, force_refresh=False):
        calls.append(drive_id)
        raise AssertionError("给了 access_token 就不许咨询 SA 池")

    monkeypatch.setattr(_dc, "get_token_for_drive", fake_token)
    return calls


def test_relay_with_access_token_uses_bearer_once(monkeypatch):
    calls = _patch_mount(monkeypatch, lambda n, h: "RESP")
    sa = _patch_sa_token(monkeypatch)
    out = _run(streaming.serve_drive_relay_async(
        SimpleNamespace(headers={}), "byos:7", "FID7",
        access_token="USERTOK"))
    assert out == "RESP"
    assert len(calls) == 1
    assert calls[0]["headers"]["Authorization"] == "Bearer USERTOK"
    assert "FID7" in calls[0]["url"]
    assert sa == []


def test_relay_with_access_token_403_not_retried(monkeypatch):
    def behavior(n, headers):
        raise HTTPException(status_code=403, detail="quota")

    calls = _patch_mount(monkeypatch, behavior)
    sa = _patch_sa_token(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        _run(streaming.serve_drive_relay_async(
            SimpleNamespace(headers={}), "byos:7", "FID7",
            access_token="USERTOK"))
    assert ei.value.status_code == 403
    assert len(calls) == 1
    assert sa == []

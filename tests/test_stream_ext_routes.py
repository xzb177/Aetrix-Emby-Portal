"""EA 带扩展名的直接流路由（/stream.mkv /stream.mp4）版本化测试。

背景：iOS 客户端（Lenna/SenPlayer）请求播放时 URL 带扩展名，
如 /emby/videos/{id}/stream.mkv。若无此路由，请求会被
/{transcode_path:path}（HLS catch-all）误接收，导致 ffmpeg 误启动。
"""
import asyncio
import inspect

import pytest

from backend.emby_server import api


EXPECTED_ROUTES = [
    "/emby/videos/{item_id}/stream.mkv",
    "/emby/videos/{item_id}/stream.mp4",
    "/emby/videos/{item_id}/stream.strm",
    "/emby/Videos/{item_id}/stream.mkv",
    "/emby/Videos/{item_id}/stream.mp4",
    "/emby/Videos/{item_id}/stream.strm",
]

CATCH_ALL_ROUTES = [
    "/emby/videos/{item_id}/{transcode_path:path}",
    "/videos/{item_id}/{transcode_path:path}",
]


def _route_paths():
    return [getattr(r, "path", "") for r in api.emby_router.routes]


def test_all_four_extension_routes_registered():
    paths = _route_paths()
    for expected in EXPECTED_ROUTES:
        assert expected in paths, f"路由缺失: {expected}"


def test_extension_routes_defined_before_catch_all():
    """FastAPI 按定义顺序匹配：扩展名路由必须在 /{transcode_path:path} 之前。"""
    paths = _route_paths()
    for ext in EXPECTED_ROUTES:
        ext_idx = paths.index(ext)
        for catch in CATCH_ALL_ROUTES:
            if catch in paths:
                assert ext_idx < paths.index(catch), (
                    f"{ext} 必须在 {catch} 之前定义，否则会被 HLS catch-all 抢走"
                )


def test_video_stream_ext_signature_matches_video_stream():
    """video_stream_ext 参数签名必须与 video_stream 一致（透传不丢参）。"""
    ext_params = list(inspect.signature(api.video_stream_ext).parameters)
    stream_params = list(inspect.signature(api.video_stream).parameters)
    assert ext_params == stream_params, (
        f"签名不一致: video_stream_ext={ext_params}, video_stream={stream_params}"
    )


def test_video_stream_ext_delegates_to_video_stream(monkeypatch):
    """video_stream_ext 必须把全部参数原样透传给 video_stream。"""
    calls = []

    async def fake_video_stream(item_id, request, user, db):
        calls.append((item_id, request, user, db))
        return "STREAM_RESULT"

    monkeypatch.setattr(api, "video_stream", fake_video_stream)

    sentinel_request, sentinel_user, sentinel_db = object(), object(), object()
    result = asyncio.run(
        api.video_stream_ext("item123", sentinel_request, sentinel_user, sentinel_db)
    )

    assert result == "STREAM_RESULT"
    assert calls == [("item123", sentinel_request, sentinel_user, sentinel_db)]


def test_video_hls_stream_dot_prefix_delegates_to_video_stream(monkeypatch):
    """video_hls 的 catch-all 收到 stream.*（如 stream.strm）时，必须转交
    直连处理，绝不能起转码。

    回归：SenPlayer 按 Emby 约定拼 /emby/videos/{id}/stream.strm 取流，
    曾掉进 video_hls 被误判为新建转码请求，ffmpeg 起来返 m3u8，
    客户端等视频字节流等不到，一直转圈。
    """
    from unittest.mock import MagicMock

    stream_calls = []
    transcode_calls = []

    async def fake_video_stream(item_id, request, user, db):
        stream_calls.append((item_id, request, user, db))
        return "STREAM_RESULT"

    async def fake_prepare(*args, **kwargs):
        transcode_calls.append(args)
        return "M3U8"

    monkeypatch.setattr(api, "video_stream", fake_video_stream)
    monkeypatch.setattr(api, "_prepare_new_transcode", fake_prepare)

    sentinel_request, sentinel_user = object(), object()
    sentinel_db = MagicMock()
    for transcode_path in ("stream.strm", "stream.foo", "stream.mkv"):
        result = asyncio.run(
            api.video_hls("item123", transcode_path, sentinel_request,
                          sentinel_user, sentinel_db)
        )
        assert result == "STREAM_RESULT", f"{transcode_path} 未转交直连"

    assert len(stream_calls) == 3
    assert transcode_calls == [], "stream.* 绝不能触发 _prepare_new_transcode"


def test_video_hls_non_stream_path_still_transcodes(monkeypatch):
    """守卫不能误伤正常转码路径：master.m3u8 这类仍走转码流程。"""
    from unittest.mock import MagicMock
    from starlette.requests import Request

    async def fake_prepare(*args, **kwargs):
        return "M3U8"

    monkeypatch.setattr(api, "_prepare_new_transcode", fake_prepare)
    # video_hls 在函数体内 from backend.emby_server.async_db import run_db，
    # 要 patch 源模块才够得着
    import backend.emby_server.async_db as async_db

    async def fake_run_db(fn, *args, **kwargs):
        return MagicMock()
    monkeypatch.setattr(async_db, "run_db", fake_run_db)

    scope = {
        "type": "http", "method": "GET",
        "path": "/emby/videos/item123/master.m3u8",
        "query_string": b"", "headers": [],
    }
    request = Request(scope)
    sentinel_db = MagicMock()
    result = asyncio.run(
        api.video_hls("item123", "master.m3u8", request,
                      MagicMock(), sentinel_db)
    )
    assert result is not None


def test_is_segment_path_stream_strm():
    """stream.strm 直接流应与 stream.mkv 同等对待，可进 CDN 分片缓存。"""
    from backend.emby_server import cdn
    assert cdn.is_segment_path("/emby/videos/x/stream.strm") is True
    assert cdn.is_segment_path("/emby/Videos/abc123/stream.strm") is True

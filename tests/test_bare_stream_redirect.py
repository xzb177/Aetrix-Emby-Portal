"""无后缀 /stream 兼容（2026-10-27 #1358 补丁）。

背景：REX 2 小时内对小写裸地址 /emby/videos/{guid}/stream 发起 96 次
请求、全部 404——全仓此前只有大写裸路由与小写带后缀路由，小写裸地址
没有任何路由，请求落到小写通配后按非 stream.* 判 404；REX 反复重试、
最后才回落 alt=media 出画面，这就是它首次 38 秒、第二次快的原因。
修法（通用，不按播放器名分流）：裸地址按条目容器 307 重定向到已有的
带后缀路由，原查询串原样保留；未知容器保持 404 但记日志。

本文件只测路由层：路由注册顺序 + handler 的重定向目标与 404 语义，
不碰真实 DB（_require_visible_item 用桩替换）与真实谷歌。
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.emby_server import api

GUID = "c871c37df8a46cfb6f85a5e85b6d625d"
QUERY = (
    "DeviceId=AFA9A2BA-9F66-4670-B479-9BE1E9E91FC0"
    "&MediaSourceId=c871c37df8a46cfb6f85a5e85b6d625d"
    "&Static=true&PlaySessionId=ae55d3349f7de376"
    "&api_key=tok123&X-Emby-Token=tok123"
)


def _run(handler_coro):
    return asyncio.run(handler_coro)


def _bare(monkeypatch, container, query=QUERY):
    monkeypatch.setattr(
        api, "_require_visible_item",
        lambda db, user, item_id: SimpleNamespace(
            container=container, file_path=""))
    req = SimpleNamespace(url=SimpleNamespace(query=query))
    return _run(api.video_stream_bare(GUID, req, SimpleNamespace(), None))


def test_bare_stream_routes_registered_before_catch_all():
    paths = [getattr(r, "path", "") for r in api.emby_router.routes]
    for route in ("/emby/videos/{item_id}/stream",
                  "/videos/{item_id}/stream"):
        assert route in paths, f"路由缺失: {route}"
    catch_all = "/emby/videos/{item_id}/{transcode_path:path}"
    if catch_all in paths:
        for route in ("/emby/videos/{item_id}/stream",):
            assert paths.index(route) < paths.index(catch_all), (
                "裸 stream 路由必须在小写通配之前，否则仍会被通配抢走判 404")


def test_bare_stream_mkv_redirect_keeps_all_params(monkeypatch):
    resp = _bare(monkeypatch, "mkv")
    assert resp.status_code == 307
    assert resp.headers["location"] == (
        f"/emby/videos/{GUID}/stream.mkv?{QUERY}")


def test_bare_stream_mp4_redirect_keeps_all_params(monkeypatch):
    resp = _bare(monkeypatch, "mp4")
    assert resp.status_code == 307
    assert resp.headers["location"] == (
        f"/emby/videos/{GUID}/stream.mp4?{QUERY}")


def test_bare_stream_no_query_redirects_without_question_mark(monkeypatch):
    resp = _bare(monkeypatch, "mkv", query="")
    assert resp.headers["location"] == f"/emby/videos/{GUID}/stream.mkv"


def test_bare_stream_unknown_container_keeps_404_and_logs(monkeypatch, caplog):
    for container in ("iso", "xyz", None, ""):
        with caplog.at_level("WARNING", logger="backend.emby_server.api"):
            with pytest.raises(HTTPException) as exc:
                _bare(monkeypatch, container)
        assert exc.value.status_code == 404
        assert any("bare-stream" in r.message and GUID in r.message
                   for r in caplog.records), (
            f"未知容器 {container!r} 的 404 必须记日志，不能静默")
        caplog.clear()

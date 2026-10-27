"""无后缀 /stream 兼容（2026-10-27 #1358 补丁，第二轮改为一跳）。

背景：REX 2 小时内对小写裸地址 /emby/videos/{guid}/stream 发起 96 次
请求、全部 404——全仓此前只有大写裸路由与小写带后缀路由，小写裸地址
没有任何路由，请求落到小写通配后按非 stream.* 判 404；REX 反复重试、
最后才回落 alt=media 出画面，这就是它首次 38 秒、第二次快的原因。
#1358 先以 307 重定向到带后缀路由消掉 404；第二轮再把两跳并成一跳：
裸地址校验容器后直接委派给 video_stream（与带后缀路由同一 handler），
省一次往返，门禁/限流/查询串全在 video_stream 里，不存在跳过一说。
未知容器保持 404 但记日志。

本文件只测路由层：路由注册顺序 + handler 的委派语义与 404 语义，
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
    calls = []

    async def _fake_stream(item_id, request, user, db):
        calls.append((item_id, request, user, db))
        return SimpleNamespace(status_code=302, headers={"location": "direct"})

    monkeypatch.setattr(api, "video_stream", _fake_stream)
    req = SimpleNamespace(url=SimpleNamespace(query=query))
    return _run(api.video_stream_bare(GUID, req, SimpleNamespace(), None)), calls, req


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


def test_bare_stream_mkv_delegates_in_one_hop(monkeypatch):
    resp, calls, req = _bare(monkeypatch, "mkv")
    # 一跳：不是 307 重定向，而是 video_stream 的直出结果；
    # 原请求对象原样传过去（查询串不重建、不丢参数）。
    assert resp.status_code == 302
    assert resp.headers["location"] == "direct"
    assert len(calls) == 1
    assert calls[0][0] == GUID and calls[0][1] is req


def test_bare_stream_mp4_delegates_in_one_hop(monkeypatch):
    resp, calls, _req = _bare(monkeypatch, "mp4")
    assert resp.status_code == 302
    assert len(calls) == 1


def test_bare_stream_no_query_still_delegates(monkeypatch):
    resp, calls, _req = _bare(monkeypatch, "mkv", query="")
    assert resp.status_code == 302
    assert len(calls) == 1


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

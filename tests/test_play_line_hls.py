"""转码 / HLS 路径（video_hls）回归测试（2026-10 简化版）

单路径：转码请求统一记录，不再按线路区分。（2026-10-09 起 local_cache
已按用户要求从播放链路移除：不再有"本地缓存自动层"。）

（本文件最初还怀疑「转码把源站凭据丢了」，实测 ``start_transcode`` 确实带着
``input_headers`` 走到 ffmpeg 的 ``-headers``，那个猜测是错的；
对应的用例保留下来当回归护栏。）

全部用 monkeypatch 桩，不碰网络、不碰生产库。
"""
import asyncio
from types import SimpleNamespace

import pytest


class _StopAtTranscode(Exception):
    """在 start_transcode 处停下：再往下就是拼播放列表，与路径判定无关"""


def _request(query: str = ""):
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "GET", "path": "/videos/item/master.m3u8",
        "raw_path": b"/videos/item/master.m3u8", "query_string": query.encode(),
        "headers": [], "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 50000), "root_path": "",
    }
    return Request(scope)


@pytest.fixture()
def harness(monkeypatch):
    """把 video_hls 起转码之前的所有外部依赖都桩掉"""
    from backend.emby_server import api
    from backend.emby_server.mounts import PlayTarget

    state = SimpleNamespace(
        recorded=[], transcode_args=None,
        target=PlayTarget("url", "https://origin.example/movie.mkv",
                          {"Authorization": "Basic c2VjcmV0"}),
    )

    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db, user, item_id: SimpleNamespace(container="mp4", guid="g1"))
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(api.playback_policy, "ensure_transcode_allowed",
                        lambda db, user, **kw: None)
    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(api, "_api_key_for", lambda db, request: "k")
    monkeypatch.setattr(api, "_play_target", lambda db, item: state.target)
    monkeypatch.setattr(
        api.line_stats, "record_request",
        lambda **kw: state.recorded.append(kw),
    )

    def fake_start_transcode(url, start, bitrate, height, **kw):
        state.transcode_args = dict(source=url, start=start, bitrate=bitrate,
                                    height=height, **kw)
        raise _StopAtTranscode()

    monkeypatch.setattr(api, "start_transcode", fake_start_transcode)
    return api, state


def _run(api, user=SimpleNamespace(id=7)):
    with pytest.raises(_StopAtTranscode):
        asyncio.run(api.video_hls("item", "master.m3u8", _request(), user, object()))


def test_transcode_records_request(harness):
    """转码请求要被记录（单路径，不再区分线路）"""
    api, state = harness
    _run(api)
    assert len(state.recorded) == 1


def test_transcode_input_carries_source_credentials(harness):
    """回归护栏：ffmpeg 是**外部进程**，没有本服务的会话，
    源站凭据只能靠 ``input_headers`` → ffmpeg ``-headers`` 这一处透传。"""
    api, state = harness
    _run(api)
    assert state.transcode_args is not None
    assert state.transcode_args["input_headers"] == state.target.headers


def test_transcode_input_headers_empty_when_source_has_none(harness):
    from backend.emby_server.mounts import PlayTarget

    api, state = harness
    state.target = PlayTarget("url", "https://origin.example/public.mkv", {})
    _run(api)
    assert state.transcode_args["input_headers"] in ({}, None)

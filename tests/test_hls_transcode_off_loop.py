"""S3：HLS 新建转码路径的同步 DB / 115 取直链 / Popen 不在事件循环上执行。

桩掉的每个阻塞依赖都记录「调用时当前线程上有没有正在跑的事件循环」；
另外让 _play_target 真睡 0.4s（模拟 115 取直链慢），同时跑一个心跳协程，
断言事件循环期间仍在调度（旧实现会整个卡住）。
"""
import asyncio
import time
from types import SimpleNamespace

from tests.test_play_line_hls import _request


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_new_transcode_preparation_runs_in_threadpool(monkeypatch):
    from backend.emby_server import api
    from backend.emby_server.mounts import PlayTarget

    seen = {}

    def rec(name, ret=None, sleep=0.0):
        def _fn(*a, **k):
            seen[name] = _on_loop()
            if sleep:
                time.sleep(sleep)
            return ret
        return _fn

    item = SimpleNamespace(container="mkv", guid="g1", height=1080, file_fingerprint=None)
    monkeypatch.setattr(api, "_require_visible_item", lambda db, user, item_id: item)
    monkeypatch.setattr(api.play_sign, "check_referer", rec("check_referer"))
    monkeypatch.setattr(api, "ensure_playback_allowed", rec("ensure_playback_allowed"))
    monkeypatch.setattr(api.playback_policy, "ensure_transcode_allowed", rec("ensure_transcode_allowed"))
    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(api, "_play_target",
                        rec("_play_target", PlayTarget("url", "https://115.example/v.mkv", {}), sleep=0.4))
    monkeypatch.setattr(api.line_stats, "record_request", lambda *a, **k: None)
    from backend.emby_server import transcode as tc
    monkeypatch.setattr(tc, "find_cache", lambda *a, **k: None)
    monkeypatch.setattr(tc, "ensure_slot_or_503", rec("ensure_slot_or_503"))
    monkeypatch.setattr(api, "start_transcode", rec("start_transcode", "sess123"))
    monkeypatch.setattr(api, "_url_auth_qs", rec("_url_auth_qs", "sig=abc"))

    ticks = []

    async def heartbeat():
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.35:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def go():
        hb = asyncio.create_task(heartbeat())
        resp = await api.video_hls("g1", "master.m3u8", _request(), SimpleNamespace(id=7), object())
        await hb
        return resp

    resp = asyncio.run(go())
    assert resp.status_code == 200
    assert b"session=sess123" in resp.body and b"sig=abc" in resp.body
    for name in ("check_referer", "ensure_playback_allowed",
                 "ensure_transcode_allowed", "_play_target",
                 "ensure_slot_or_503", "start_transcode", "_url_auth_qs"):
        assert seen.get(name) is False, f"{name} 在事件循环上执行"
    # 115 慢的 0.4s 里事件循环仍在调度心跳（卡住时只会有 1 次）
    assert len(ticks) >= 5


def test_existing_session_playlist_rewrite_off_loop(monkeypatch, tmp_path):
    from backend.emby_server import api

    (tmp_path / "master.m3u8").write_text("#EXTM3U\n#EXTINF:4,\nseg00000.ts\n")
    item = SimpleNamespace(container="mkv", guid="g1")
    monkeypatch.setattr(api, "_require_visible_item", lambda db, user, item_id: item)
    monkeypatch.setattr(api, "get_transcode", lambda sid: {"dir": str(tmp_path)})
    seen = {}
    real = api._rewrite_playlist

    def spy(*a, **k):
        seen["on_loop"] = _on_loop()
        return real(*a, **{**k, "db": None})

    monkeypatch.setattr(api, "_rewrite_playlist", spy)
    resp = asyncio.run(api.video_hls("g1", "main.m3u8", _request("session=s1"),
                                     SimpleNamespace(id=7), object()))
    assert resp.status_code == 200
    assert seen["on_loop"] is False

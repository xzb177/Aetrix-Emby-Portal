# -*- coding: utf-8 -*-
"""点播入口预热：在途合并、单条 Ids、日志。不打谷歌、不按播放器名分支。"""
import logging
import time

import pytest

from backend.emby_server import first_play as fp


@pytest.fixture(autouse=True)
def _clear_inflight():
    fp._inflight.clear()
    yield
    fp._inflight.clear()


class _FakeThread:
    instances = []

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target = target
        self.args = args
        self.daemon = daemon
        _FakeThread.instances.append(self)

    def start(self):
        return None


@pytest.fixture()
def fake_thread(monkeypatch):
    _FakeThread.instances = []
    monkeypatch.setattr(fp.threading, "Thread", _FakeThread)
    return _FakeThread


def test_direct_item_guid_only_exactly_one():
    assert fp.iter_query_ids(["a,b", " c "]) == ["a", "b", "c"]
    assert fp.direct_item_guid(["only"]) == "only"
    assert fp.direct_item_guid(["a", "b"]) is None
    assert fp.direct_item_guid([]) is None
    assert fp.direct_item_guid([""]) is None
    assert fp.direct_item_guid(fp.iter_query_ids(["g1,g2"])) is None


def test_spawn_logs_trigger_and_dedups_inflight(fake_thread, caplog):
    caplog.set_level(logging.INFO, logger="backend.emby_server.play_timing")
    assert fp.spawn_first_play_warm(9, 7, source="detail", self_target=True) is True
    assert fp.spawn_first_play_warm(9, 7, source="detail", self_target=True) is True
    assert len(fake_thread.instances) == 1
    text = caplog.text
    assert "warm trigger" in text
    assert "source=detail" in text
    assert "item_id=9" in text
    assert "user_id=7" in text
    assert "warm already inflight" in text
    # 在途标记还在，零超时的等待不会当成已经预热完。
    ev = fake_thread.instances[0].args[3]
    assert fp.wait_inflight(7, 9, 0) is False
    assert not ev.is_set()
    fp.finish_inflight(7, 9, ev)
    assert ev.is_set()
    assert fp._inflight.get(fp.inflight_key(7, 9)) is None


def test_items_id_logs_parent_list_does_not(fake_thread, caplog):
    caplog.set_level(logging.INFO, logger="backend.emby_server.play_timing")
    assert fp.spawn_first_play_warm_by_guid("movie-guid", 3, source="items_id") is True
    assert "source=items_id" in caplog.text
    assert "guid=movie-guid" in caplog.text
    caplog.clear()
    assert fp.spawn_first_play_warm_by_guid("lib-guid", 3, source="parent_list") is True
    assert "warm trigger" not in caplog.text
    assert fp.spawn_first_play_warm_by_guid("", 3, source="items_id") is False


def test_missing_item_releases_preclaim(monkeypatch):
    class _Q:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

    class _S:
        def query(self, *args, **kwargs):
            return _Q()

        def close(self):
            return None

    monkeypatch.setattr("backend.database.SessionLocal", lambda: _S())
    ev, owner = fp.claim_inflight(3, 4)
    assert owner is True
    fp._warm_thread_body(4, 3, "detail", ev)
    assert ev.is_set()
    assert fp.wait_inflight(3, 4, 0.01) is False


def test_consume_inflight_warm_reuses_cached_token():
    ev, owner = fp.claim_inflight(7, 9)
    assert owner is True
    store = {}

    def cache_set(key, value, ttl):
        store[key] = value

    def cache_get(key):
        return store.get(key)

    fp.store_warm(cache_set, "guid-1", 7, {
        "url": "https://example.invalid/a", "token": "tok",
        "fid": "fid-1", "rotations": 0,
    })

    def _finish():
        time.sleep(0.05)
        fp.finish_inflight(7, 9, ev)

    import threading
    threading.Thread(target=_finish, daemon=True).start()
    got = fp.consume_inflight_warm(cache_get, 7, 9, "guid-1", "fid-1", 1.0)
    assert got is not None
    assert got["token"] == "tok"
    assert got["url"] == "https://example.invalid/a"
    # 文件对不上：等完了也不拿别的 fid 的令牌。
    ev2, owner2 = fp.claim_inflight(7, 9)
    assert owner2 is True

    def _finish_mismatch():
        time.sleep(0.05)
        fp.finish_inflight(7, 9, ev2)

    threading.Thread(target=_finish_mismatch, daemon=True).start()
    assert fp.consume_inflight_warm(
        cache_get, 7, 9, "guid-1", "other-fid", 1.0) is None


def test_no_inflight_consume_returns_immediately():
    t0 = time.monotonic()
    assert fp.consume_inflight_warm(lambda k: None, 1, 2, "g", "f", 5.0) is None
    assert time.monotonic() - t0 < 0.5


def test_no_player_name_on_entry_api():
    import inspect

    sigs = [
        inspect.signature(fp.spawn_first_play_warm),
        inspect.signature(fp.spawn_first_play_warm_by_guid),
        inspect.signature(fp.consume_inflight_warm),
        inspect.signature(fp.direct_item_guid),
        inspect.signature(fp.claim_inflight),
    ]
    joined = " ".join(str(s) for s in sigs).lower()
    for name in ("rex", "senplayer", "vidhub", "client_name", "device_name",
                 "user-agent", "user_agent"):
        assert name not in joined

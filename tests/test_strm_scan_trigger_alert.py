"""Tests for strm 扫库触发器失败告警（2026-10-13）。

覆盖：
- _record_scan_trigger_failures：记录格式、只保留最近 20 条、写入失败不抛
- _trigger_strm_scan：逐库 push 失败时记 warning 日志且写失败记录；
  成功时不写记录
- system_alerts._check_strm_scan_trigger_failed：空记录/过期记录不告警，
  24 小时内记录告警（沿用 strm_source_failed 通道模式）

不碰网络/DB：store 与外部模块全部 monkeypatch 隔离。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg
from backend import system_alerts as sa


class FakeStore:
    """backend.integrations.store 的内存替身。"""

    def __init__(self):
        self.data = {}

    def get_value(self, db, key, default=""):
        return self.data.get(key, default)

    def write_values(self, db, values, descriptions=None):
        self.data.update(values)


@pytest.fixture()
def fake_store(monkeypatch):
    store = FakeStore()
    import backend.integrations as integ
    monkeypatch.setattr(integ, "store", store)
    return store


class TestRecordScanTriggerFailures:
    def test_record_format(self, fake_store):
        sg._record_scan_trigger_failures(object(), [(7, "boom")])
        raw = fake_store.data[sg.CONFIG_SCAN_TRIGGER_FAILURES]
        records = json.loads(raw)
        assert len(records) == 1
        assert records[0]["lib"] == "7"
        assert records[0]["error"] == "boom"
        assert "at" in records[0]

    def test_cap_20(self, fake_store):
        for i in range(25):
            sg._record_scan_trigger_failures(object(), [(i, "e%d" % i)])
        records = json.loads(fake_store.data[sg.CONFIG_SCAN_TRIGGER_FAILURES])
        assert len(records) == 20
        # 只保留最近 20 条
        assert records[0]["lib"] == "5"
        assert records[-1]["lib"] == "24"

    def test_store_failure_no_raise(self, monkeypatch):
        import backend.integrations as integ

        class BadStore:
            def get_value(self, db, key, default=""):
                raise RuntimeError("db down")

            def write_values(self, db, values, descriptions=None):
                raise RuntimeError("db down")

        monkeypatch.setattr(integ, "store", BadStore())
        # 写入失败不影响主流程：不抛异常
        sg._record_scan_trigger_failures(object(), [(1, "x")])


class FakeLib:
    is_enabled = True  # 类属性：供 em.Library.is_enabled == True 过滤表达式用

    def __init__(self, lib_id, paths, name="L", is_virtual=False):
        self.id = lib_id
        self.paths = paths
        self.name = name
        self.is_virtual = is_virtual


class FakeQuery:
    def __init__(self, libs):
        self._libs = libs

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._libs


class FakeDB:
    def __init__(self, libs):
        self._libs = libs

    def query(self, *a, **k):
        return FakeQuery(self._libs)


class TestTriggerScanRecords:
    def _patch(self, monkeypatch, libs, push_impl):
        import backend.emby_server.scan_queue_redis as sqr
        import backend.emby_server.models as em
        monkeypatch.setattr(sqr, "push_scan_request", push_impl)
        monkeypatch.setattr(em, "Library", FakeLib, raising=False)
        monkeypatch.setattr(sg, "_container_strm_root", lambda db: "/strm")

    def test_push_failure_recorded(self, monkeypatch, fake_store):
        def _boom(lib_id, trigger=None):
            raise RuntimeError("redis down")

        self._patch(monkeypatch, [FakeLib(3, "/strm/剧集")], _boom)
        sg._trigger_strm_scan(FakeDB([FakeLib(3, "/strm/剧集")]))
        records = json.loads(fake_store.data[sg.CONFIG_SCAN_TRIGGER_FAILURES])
        assert len(records) == 1
        assert records[0]["lib"] == "3"
        assert "redis down" in records[0]["error"]

    def test_push_success_no_record(self, monkeypatch, fake_store):
        def _ok(lib_id, trigger=None):
            return {"created": True}

        self._patch(monkeypatch, [FakeLib(3, "/strm/剧集")], _ok)
        sg._trigger_strm_scan(FakeDB([FakeLib(3, "/strm/剧集")]))
        assert sg.CONFIG_SCAN_TRIGGER_FAILURES not in fake_store.data

    def test_non_strm_lib_skipped(self, monkeypatch, fake_store):
        calls = []

        def _ok(lib_id, trigger=None):
            calls.append(lib_id)
            return {"created": True}

        self._patch(monkeypatch, [FakeLib(9, "/media/other")], _ok)
        sg._trigger_strm_scan(FakeDB([FakeLib(9, "/media/other")]))
        assert calls == []
        assert sg.CONFIG_SCAN_TRIGGER_FAILURES not in fake_store.data


class TestChecker:
    def _seed(self, fake_store, records):
        fake_store.data["strm_gen_scan_trigger_failures"] = json.dumps(records)

    def test_empty_no_alert(self, fake_store):
        assert sa._check_strm_scan_trigger_failed({}, None, db=object()) is None

    def test_old_records_no_alert(self, fake_store):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self._seed(fake_store, [{"lib": "3", "error": "x", "at": old}])
        assert sa._check_strm_scan_trigger_failed({}, None, db=object()) is None

    def test_recent_records_alert(self, fake_store):
        now = datetime.now(timezone.utc).isoformat()
        self._seed(fake_store, [
            {"lib": "3", "error": "redis down", "at": now},
            {"lib": "5", "error": "timeout", "at": now},
        ])
        title, body, ctx = sa._check_strm_scan_trigger_failed({}, None, db=object())
        assert "2" in title
        assert "redis down" in body or "timeout" in body
        assert ctx["failed_count"] == 2

    def test_event_registered(self):
        assert sa.EVT_STRM_SCAN_TRIGGER_FAILED in sa.ALL_EVENTS
        assert sa._CHECKERS[sa.EVT_STRM_SCAN_TRIGGER_FAILED] is sa._check_strm_scan_trigger_failed

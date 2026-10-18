"""singleflight + TTL 300 + 观测计数器测试。

覆盖：
- TTL 默认 300（常量 + 配置读法 + config_payload）
- 计数器四项在 FakeRedis 下累加正确（hit/miss/put/coalesced）
- 并发未命中只算一次（N 线程同 key → 1 个领头计算，其余 coalesced）
- 领头没写（抛错/不回填）→ 等待者 fail-open 自算，slot 不泄漏
- 等待超时回退（短 wait_seconds）
- api 层：领头计算抛错时 in-flight 标记被 finally 清理（无泄漏）

注意：并发测试用文件 sqlite（check_same_thread=False）+ 每线程独立
session；in-memory sqlite 的连接是线程隔离的，不能用。
"""
import os
import threading
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api
from backend.emby_server import items_dedup_cache as idc
from backend.emby_server import items_list_cache as ilc
from backend.emby_server import models as em
from backend.integrations import store


class FakeRedis:
    """最小 Redis 替身：本模块用到的命令子集（含 TTL 与 incr）。"""

    def __init__(self):
        self.strings = {}
        self.zsets = {}
        self.counters = {}

    def get(self, key):
        if key in self.counters:
            return str(self.counters[key])
        v = self.strings.get(key)
        if v is None:
            return None
        val, exp = v
        if exp is not None and exp <= time.time():
            del self.strings[key]
            return None
        return val

    def set(self, key, val, ex=None):
        exp = time.time() + ex if ex else None
        self.strings[key] = (val, exp)
        return True

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                n += 1
        return n

    def zadd(self, name, mapping):
        z = self.zsets.setdefault(name, {})
        z.update(mapping)
        return len(mapping)

    def zcard(self, name):
        return len(self.zsets.get(name, {}))

    def zrange(self, name, start, end):
        items = sorted(self.zsets.get(name, {}).items(), key=lambda kv: kv[1])
        keys = [k for k, _ in items]
        if end < 0:
            end = len(keys) + end
        return keys[start:end + 1]

    def zrem(self, name, *members):
        z = self.zsets.get(name, {})
        n = 0
        for m in members:
            if m in z:
                del z[m]
                n += 1
        return n


def _g():
    return uuid.uuid4().hex


@pytest.fixture()
def fake():
    fb = FakeRedis()
    idc._set_backend_override(fb)
    ilc._in_flight.clear()
    yield fb
    idc._set_backend_override(None)
    ilc._in_flight.clear()
    idc._reset_bump_state()


@pytest.fixture(autouse=True)
def _clean_store_cache():
    store.invalidate(ilc.CFG_ENABLED, ilc.CFG_TTL_SECONDS, ilc.CFG_MAX_IDS,
                     idc.CFG_ENABLED, idc.CFG_TTL_SECONDS, idc.CFG_MAX_ENTRIES)
    yield
    store.invalidate(ilc.CFG_ENABLED, ilc.CFG_TTL_SECONDS, ilc.CFG_MAX_IDS,
                     idc.CFG_ENABLED, idc.CFG_TTL_SECONDS, idc.CFG_MAX_ENTRIES)


def _file_db():
    path = f"/tmp/sf_test_{_g()}.db"
    eng = create_engine(f"sqlite:///{path}",
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng, path


def _session(eng):
    return sessionmaker(bind=eng)()


def _req(params):
    return Request({"type": "http", "method": "GET",
                    "query_string": urlencode(params).encode(), "headers": []})


def _seed_minimal(eng):
    """1 个电影库 + 3 部电影 + 1 个用户（api 层测试用）。"""
    db = _session(eng)
    lib = em.Library(guid=_g(), name="电影", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    t0 = datetime(2026, 1, 1)
    for i, name in enumerate(["A", "B", "C"]):
        db.add(em.MediaItem(guid=_g(), library_id=lib.id, item_type="movie",
                            name=name, sort_name=name.lower(),
                            file_path=f"/m/{_g()}.mkv", date_added=t0,
                            tmdb_id=str(900 + i), production_year=2000 + i))
    user = models.WebUser(username=f"sf_{_g()[:8]}", password_hash="x",
                          is_active=True)
    db.add(user)
    db.commit()
    uid = user.id
    db.close()
    return SimpleNamespace(id=uid, is_staff=False)


# ---------------- TTL ----------------

def test_ttl_default_300(fake):
    eng, path = _file_db()
    try:
        db = _session(eng)
        try:
            assert ilc.TTL_DEFAULT == 300
            assert ilc.cache_ttl_seconds(db) == 300
            p = ilc.config_payload(db)
            assert p["ttl_seconds"] == 300
            assert p["defaults"]["ttl_seconds"] == 300
        finally:
            db.close()
    finally:
        eng.dispose()
        os.unlink(path)


def test_ttl_still_clamped(fake):
    eng, path = _file_db()
    try:
        db = _session(eng)
        try:
            # 显式配置仍钳制在 [10, 3600]，改默认值不影响钳制逻辑
            store.write_values(db, {ilc.CFG_TTL_SECONDS: "5"},
                               {ilc.CFG_TTL_SECONDS: "test"})
            db.commit()
            store.invalidate(ilc.CFG_TTL_SECONDS)
            assert ilc.cache_ttl_seconds(db) == 10
            store.write_values(db, {ilc.CFG_TTL_SECONDS: "99999"},
                               {ilc.CFG_TTL_SECONDS: "test"})
            db.commit()
            store.invalidate(ilc.CFG_TTL_SECONDS)
            assert ilc.cache_ttl_seconds(db) == 3600
            db.query(models.SystemConfig).filter(
                models.SystemConfig.key == ilc.CFG_TTL_SECONDS).delete(
                synchronize_session=False)
            db.commit()
            store.invalidate(ilc.CFG_TTL_SECONDS)
            assert ilc.cache_ttl_seconds(db) == 300
        finally:
            db.close()
    finally:
        eng.dispose()
        os.unlink(path)


# ---------------- 计数器 ----------------

def test_stats_counters(fake):
    eng, path = _file_db()
    try:
        db = _session(eng)
        try:
            assert ilc.get_stats() == {"hit": 0, "miss": 0, "put": 0,
                                      "coalesced": 0}
            # miss（coalesced_get 领头）→ put → hit
            fr = ilc.coalesced_get(db, {"a": "1"}, [1])
            assert fr.mode == "leader"
            with ilc.leader_slot(fr):
                ilc.put_cached_page(db, {"a": "1"}, [1], [7, 8], 2, False)
            assert ilc.get_cached_page(db, {"a": "1"}, [1]).ids == [7, 8]
            st = ilc.get_stats()
            assert st["miss"] == 1, st
            assert st["put"] == 1, st
            assert st["hit"] == 1, st
            assert st["coalesced"] == 0, st
            # 再读一次 → hit+1
            ilc.get_cached_page(db, {"a": "1"}, [1])
            assert ilc.get_stats()["hit"] == 2
            assert ilc._in_flight == {}
        finally:
            db.close()
    finally:
        eng.dispose()
        os.unlink(path)


# ---------------- singleflight 并发 ----------------

def test_coalesced_single_compute(fake):
    """6 线程同 key 并发未命中：只有 1 个领头计算，其余 5 个复用。"""
    eng, path = _file_db()
    try:
        barrier = threading.Barrier(6)
        modes = {}
        lock = threading.Lock()

        def worker(i):
            db = _session(eng)
            try:
                barrier.wait(timeout=10)
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                if fr.mode == "leader":
                    with ilc.leader_slot(fr):
                        time.sleep(0.3)  # 模拟全量计算
                        ilc.put_cached_page(db, {"a": "1"}, [1], [7, 8], 2,
                                            False)
                    fr2 = ilc.coalesced_get(db, {"a": "1"}, [1])
                    mode = ("leader", fr2.mode)
                else:
                    mode = (fr.mode, None)
                    assert fr.page.ids == [7, 8]
                with lock:
                    modes[i] = mode
            finally:
                db.close()

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert all(not t.is_alive() for t in ts), "线程卡死"
        leaders = [i for i, m in modes.items() if m[0] == "leader"]
        coalesced = [i for i, m in modes.items() if m[0] == "coalesced"]
        assert len(leaders) == 1, modes
        assert len(coalesced) == 5, modes
        st = ilc.get_stats()
        assert st["miss"] == 1, st
        assert st["coalesced"] == 5, st
        assert st["put"] == 1, st
        assert ilc._in_flight == {}, "slot 泄漏"
    finally:
        eng.dispose()
        os.unlink(path)


def test_leader_no_write_waiter_fail_open(fake):
    """领头没写缓存（抛错/不回填的模拟）→ 等待者被唤醒后自行计算。"""
    eng, path = _file_db()
    try:
        db_main = _session(eng)
        ready = threading.Event()
        out = {}

        def leader():
            db = _session(eng)
            try:
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                assert fr.mode == "leader"
                ready.set()
                with ilc.leader_slot(fr):
                    pass  # 模拟：计算抛错 / 没写缓存，直接退出
            finally:
                db.close()

        def waiter():
            db = _session(eng)
            try:
                ready.wait(timeout=10)
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                out["mode"] = fr.mode
                out["slot"] = fr.slot
                if fr.mode == "leader":
                    with ilc.leader_slot(fr):
                        ilc.put_cached_page(db, {"a": "1"}, [1], [9], 1, False)
            finally:
                db.close()

        t1 = threading.Thread(target=leader)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        assert not t1.is_alive() and not t2.is_alive()
        # 等待者被唤醒、回读 miss → 自己成为领头（fail-open）
        assert out["mode"] == "leader", out
        st = ilc.get_stats()
        assert st["miss"] == 2, st  # 两个领头各算一次
        assert st["put"] == 1, st
        assert ilc._in_flight == {}, "slot 泄漏"
        db_main.close()
    finally:
        eng.dispose()
        os.unlink(path)


def test_wait_timeout_fallback(fake):
    """领头算得太久 → 等待者超时后自行计算，不无限等。"""
    eng, path = _file_db()
    try:
        ready = threading.Event()
        out = {}

        def slow_leader():
            db = _session(eng)
            try:
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                assert fr.mode == "leader"
                ready.set()
                with ilc.leader_slot(fr):
                    time.sleep(1.0)  # 比等待者的 0.2s 长得多
                    ilc.put_cached_page(db, {"a": "1"}, [1], [5], 1, False)
            finally:
                db.close()

        def waiter():
            db = _session(eng)
            try:
                ready.wait(timeout=10)
                t0 = time.monotonic()
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=0.2)
                out["mode"] = fr.mode
                out["waited"] = time.monotonic() - t0
                with ilc.leader_slot(fr):
                    pass
            finally:
                db.close()

        t1 = threading.Thread(target=slow_leader)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        assert not t1.is_alive() and not t2.is_alive()
        assert out["mode"] == "leader", out
        assert out["waited"] < 1.0, out  # 没等领头算完
        assert ilc._in_flight == {}, "slot 泄漏"
    finally:
        eng.dispose()
        os.unlink(path)


def test_double_check_after_lock(fake):
    """拿锁期间被别的请求写好 → 不再重复计算，直接按 hit 返回。"""
    eng, path = _file_db()
    try:
        db = _session(eng)
        try:
            ilc.put_cached_page(db, {"a": "1"}, [1], [3], 1, False)
            # 此时再 coalesced_get：顶层就是 hit
            fr = ilc.coalesced_get(db, {"a": "1"}, [1])
            assert fr.mode == "hit" and fr.page.ids == [3]
            assert ilc._in_flight == {}
        finally:
            db.close()
    finally:
        eng.dispose()
        os.unlink(path)


# ---------------- api 层接线 ----------------

def test_api_leader_exception_cleans_slot(fake, monkeypatch):
    """领头计算抛错 → with 退出时 finally 清理 slot，不泄漏。"""
    eng, path = _file_db()
    try:
        user = _seed_minimal(eng)
        db = _session(eng)
        try:
            def boom(*a, **kw):
                raise RuntimeError("dedup boom")

            monkeypatch.setattr(idc, "dedup_dropped_ids_cached", boom)
            with pytest.raises(RuntimeError):
                api._query_items(
                    _req({"Recursive": "true", "IncludeItemTypes": "Movie",
                          "Limit": "20"}),
                    user, db, "http://t")
            assert ilc._in_flight == {}, "领头抛错后 slot 泄漏"
        finally:
            db.close()
    finally:
        eng.dispose()
        os.unlink(path)


def test_api_slow_log_threshold(fake, seeded_api, caplog, monkeypatch):
    """慢日志：正常请求不打；cache_get 被人为拖慢 0.6s 时打一行且带段名。"""
    import logging
    s = seeded_api
    db = _session(s.eng)
    real = ilc.coalesced_get
    try:
        params = {"Recursive": "true", "IncludeItemTypes": "Movie", "Limit": "20"}
        # 预热一次（冷请求走全量计算，耗时不稳定，结果忽略）
        api._query_items(_req(params), s.user, db, "http://t")
        # 正常热请求：走缓存命中，远快于 500ms，不打慢日志
        with caplog.at_level(logging.WARNING, logger="backend.emby_server.api"):
            api._query_items(_req(params), s.user, db, "http://t")
        assert not [r for r in caplog.records
                    if "items 列表慢请求" in r.getMessage()]
        caplog.clear()

        # cache_get 人为拖慢 0.6s → 触发慢日志（第二次请求走缓存命中分支）
        def slow(db_, qp, libs, versions=None, wait_seconds=None):
            time.sleep(0.6)
            return real(db_, qp, libs, versions=versions,
                        wait_seconds=wait_seconds)

        monkeypatch.setattr(ilc, "coalesced_get", slow)
        with caplog.at_level(logging.WARNING, logger="backend.emby_server.api"):
            api._query_items(_req(params), s.user, db, "http://t")
        hits = [r for r in caplog.records
                if "items 列表慢请求" in r.getMessage()]
        assert len(hits) == 1, [r.getMessage() for r in caplog.records]
        assert "cache_get=" in hits[0].getMessage()
        assert "flight=" in hits[0].getMessage()
    finally:
        db.close()


@pytest.fixture()
def seeded_api():
    eng, path = _file_db()
    user = _seed_minimal(eng)
    yield SimpleNamespace(eng=eng, user=user, path=path)
    eng.dispose()
    if os.path.exists(path):
        os.unlink(path)

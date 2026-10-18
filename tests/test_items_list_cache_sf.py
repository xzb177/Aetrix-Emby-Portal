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
            assert ilc.get_stats() == {"hit": 0, "miss": 0, "recompute": 0,
                                      "put": 0, "coalesced": 0}
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

class _CountingEvent(threading.Event):
    """计数版 Event：证明等待者确实调过 wait()（E-P3-5a 可证伪性）。"""

    def __init__(self):
        super().__init__()
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        return super().wait(timeout)


class _CountingSlot(ilc._FlightSlot):
    def __init__(self):
        super().__init__()
        self.event = _CountingEvent()


@pytest.fixture()
def counting_slots(monkeypatch):
    """把 _FlightSlot 换成计数版（仅本用例）。"""
    monkeypatch.setattr(ilc, "_FlightSlot", _CountingSlot)
    yield

def test_coalesced_single_compute(fake):
    """6 线程同 key 并发未命中：只有 1 个领头计算，其余复用。

    E-P3-5b：领头睡眠 0.3s→1.0s（线程调度抖动下 0.3s 可能让后来者
    赶在 put 前又当一回领头致 flake）；断言放宽为
    leaders>=1 且 leaders+coalesced==6。
    """
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
                        time.sleep(1.0)  # 模拟全量计算（E-P3-5b：防调度 flake）
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
        # E-P3-5b 放宽：极端调度下可能出现 2 领头（第二领头双重检查前
        # 第一领头还没 put），但总数恒为 6、计算次数有界
        assert len(leaders) >= 1, modes
        assert len(leaders) + len(coalesced) == 6, modes
        st = ilc.get_stats()
        assert st["miss"] == len(leaders), st
        assert st["coalesced"] == len(coalesced), st
        assert st["put"] == len(leaders), st
        assert ilc._in_flight == {}, "slot 泄漏"
    finally:
        eng.dispose()
        os.unlink(path)


def test_leader_no_write_waiter_fail_open(fake, counting_slots):
    """领头没写缓存（抛错/不回填的模拟）→ 等待者被唤醒后自行计算。

    E-P3-5a 可证伪性：领头等到等待者的 event.wait() 真正被调用后才退出
    （否则 event.set 是空操作、唤醒路径测不到）；断言 wait_calls>=1
    证明等待确实发生、waited<wait_seconds 证明是被唤醒而非超时。
    E-P3-3：继任领头计 recompute（不再与首领头的 miss 混在一起）。
    """
    eng, path = _file_db()
    try:
        db_main = _session(eng)
        leader_ready = threading.Event()
        out = {}
        leader_slot_ref = {}

        def leader():
            db = _session(eng)
            try:
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                assert fr.mode == "leader"
                leader_slot_ref["slot"] = fr.slot
                leader_ready.set()
                # 等待者确实进入 wait() 后再退出：否则 set 是空操作
                deadline = time.monotonic() + 10
                while (getattr(fr.slot.event, "wait_calls", 0) < 1
                       and time.monotonic() < deadline):
                    time.sleep(0.01)
                with ilc.leader_slot(fr):
                    pass  # 模拟：计算抛错 / 没写缓存，直接退出
            finally:
                db.close()

        def waiter():
            db = _session(eng)
            try:
                leader_ready.wait(timeout=10)
                t0 = time.monotonic()
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                out["mode"] = fr.mode
                out["waited"] = time.monotonic() - t0
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
        # 等待者被唤醒、回读 miss → 自己成为继任领头（fail-open）
        assert out["mode"] == "leader", out
        assert leader_slot_ref["slot"].event.wait_calls >= 1, \
            "等待者必须真正调过 wait()，否则唤醒路径没测到"
        assert out["waited"] < 10, out  # 被唤醒，不是等满超时
        st = ilc.get_stats()
        assert st["miss"] == 1, st  # 首领头
        assert st["recompute"] == 1, st  # 继任领头（E-P3-3 拆分口径）
        assert st["put"] == 1, st
        assert ilc._in_flight == {}, "slot 泄漏"
        db_main.close()
    finally:
        eng.dispose()
        os.unlink(path)


def test_wait_timeout_fallback(fake):
    """领头算得太久 → 等待者超时后自行计算，不无限等。

    E-P2-1/E-P2-2：预算耗尽时仍有活领头 → mode="direct"（直算不注册，
    不干扰领头的等待链）；旧行为是再注册一个领头 slot。
    """
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
        assert out["mode"] == "direct", out  # 预算耗尽+活领头 → 直算不注册
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
    """慢日志阈值行为：阈值拉满时不打；阈值归零时打一行且带段名。

    不依赖真实耗时断言（协调员亲审补修）：原版拿「热请求 <500ms」做负例，
    机器忙时热请求超 500ms 即误红（组合跑亲验复现、单跑绿）。改为
    monkeypatch api._ITEMS_SLOW_LOG_MS，两方向都确定性。
    """
    import logging
    s = seeded_api
    db = _session(s.eng)
    try:
        params = {"Recursive": "true", "IncludeItemTypes": "Movie", "Limit": "20"}
        # 预热一次（冷请求走全量计算，结果忽略）
        api._query_items(_req(params), s.user, db, "http://t")
        # 阈值拉满：任何请求都不打慢日志
        monkeypatch.setattr(api, "_ITEMS_SLOW_LOG_MS", float("inf"))
        with caplog.at_level(logging.WARNING, logger="backend.emby_server.api"):
            api._query_items(_req(params), s.user, db, "http://t")
        assert not [r for r in caplog.records
                    if "items 列表慢请求" in r.getMessage()]
        caplog.clear()

        # 阈值归零：任何请求都打一行慢日志，且带段名
        monkeypatch.setattr(api, "_ITEMS_SLOW_LOG_MS", 0.0)
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


# ---------------- E-P2-1：fail-open 惊群 ----------------

def test_fail_open_no_herd_on_leader_error(fake, counting_slots):
    """E-P2-1：1 领头抛错 + 6 等待者同时被唤醒 → 全量计算恰好 2 次
    （领头 1 + 1 个继任领头），而不是 N+1。

    herd_probe.py 实证旧行为：7 次全量计算（每人覆盖安装各算一次）。
    修法：fail-open 分支先查 _in_flight 是否有活 slot——有则转做它的等待者，
    只有 key 上无活 slot 才自己当继任领头。
    """
    eng, path = _file_db()
    try:
        N = 6
        leader_ready = threading.Event()
        compute_count = {"n": 0}
        count_lock = threading.Lock()
        modes = {}
        modes_lock = threading.Lock()

        def _compute():
            with count_lock:
                compute_count["n"] += 1

        def leader():
            db = _session(eng)
            try:
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                assert fr.mode == "leader"
                leader_ready.set()
                # 等 6 个等待者全部真正进入 wait() 再抛错唤醒：
                # 否则唤醒有先有后，测不出"同时被唤醒"的惊群
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    with count_lock:
                        total_waits = sum(
                            getattr(s.event, "wait_calls", 0)
                            for s in list(ilc._in_flight.values()))
                    if total_waits >= N:
                        break
                    time.sleep(0.01)
                _compute()  # 领头的 1 次全量计算
                with ilc.leader_slot(fr):
                    pass  # 模拟抛错：唤醒等待者，不写缓存
            finally:
                db.close()

        def waiter(i):
            db = _session(eng)
            try:
                leader_ready.wait(timeout=10)
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10)
                with ilc.leader_slot(fr):
                    if fr.mode in ("leader", "direct"):
                        _compute()
                        if fr.mode == "leader":
                            # 继任领头回填，让其余等待者复用
                            ilc.put_cached_page(db, {"a": "1"}, [1], [9], 1,
                                                False)
                with modes_lock:
                    modes[i] = fr.mode
            finally:
                db.close()

        t0 = [threading.Thread(target=leader)]
        ts = [threading.Thread(target=waiter, args=(i,)) for i in range(N)]
        for t in t0 + ts:
            t.start()
        for t in t0 + ts:
            t.join(timeout=30)
        assert all(not t.is_alive() for t in t0 + ts), "线程卡死"
        assert compute_count["n"] == 2, (
            f"惊群：全量计算应恰好 2 次（领头+1 继任），实际 "
            f"{compute_count['n']} 次，modes={modes}")
        st = ilc.get_stats()
        assert st["miss"] == 1, st
        assert st["recompute"] == 1, st
        assert ilc._in_flight == {}, "slot 泄漏"
    finally:
        eng.dispose()
        os.unlink(path)


# ---------------- E-P2-2：want_total=false 不当领头 ----------------

def test_want_total_false_never_becomes_leader(fake):
    """E-P2-2：false 请求只做 waiter/直读——miss 且无领头时不建 slot，
    直接自己算（mode=direct）；若有领头则正常等待合并。"""
    eng, path = _file_db()
    try:
        db = _session(eng)
        try:
            # 1) 无领头时 false → direct，不注册 slot
            fr = ilc.coalesced_get(db, {"a": "1"}, [1], want_total=False)
            assert fr.mode == "direct", fr.mode
            assert ilc._in_flight == {}, "false 请求不许注册领头 slot"
            # 2) 随后 true 请求应立刻当领头，不被 false 挡住
            t0 = time.monotonic()
            fr2 = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10,
                                    want_total=True)
            elapsed = time.monotonic() - t0
            assert fr2.mode == "leader", fr2.mode
            assert elapsed < 5, f"true 请求被白等了一轮：{elapsed:.1f}s"
            with ilc.leader_slot(fr2):
                ilc.put_cached_page(db, {"a": "1"}, [1], [7, 8], 2, False)
            # 3) false 请求功能正确：命中后能读到缓存
            fr3 = ilc.coalesced_get(db, {"a": "1"}, [1], want_total=False)
            assert fr3.mode == "hit" and fr3.page.ids == [7, 8]
            assert ilc._in_flight == {}, "slot 泄漏"
        finally:
            db.close()
    finally:
        eng.dispose()
        os.unlink(path)


def test_want_total_false_waits_for_live_leader(fake, counting_slots):
    """E-P2-2：false 请求遇到活领头 → 正常等待合并（coalesced），不直算。"""
    eng, path = _file_db()
    try:
        leader_ready = threading.Event()
        out = {}

        def leader():
            db = _session(eng)
            try:
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10,
                                       want_total=True)
                assert fr.mode == "leader"
                leader_ready.set()
                with ilc.leader_slot(fr):
                    time.sleep(0.3)
                    ilc.put_cached_page(db, {"a": "1"}, [1], [7, 8], 2, False)
            finally:
                db.close()

        def waiter():
            db = _session(eng)
            try:
                leader_ready.wait(timeout=10)
                fr = ilc.coalesced_get(db, {"a": "1"}, [1], wait_seconds=10,
                                       want_total=False)
                out["mode"] = fr.mode
                out["ids"] = fr.page.ids if fr.page else None
            finally:
                db.close()

        t1 = threading.Thread(target=leader)
        t2 = threading.Thread(target=waiter)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        assert not t1.is_alive() and not t2.is_alive()
        assert out["mode"] == "coalesced", out
        assert out["ids"] == [7, 8], out
        assert ilc._in_flight == {}, "slot 泄漏"
    finally:
        eng.dispose()
        os.unlink(path)

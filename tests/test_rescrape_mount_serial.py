"""rescrape 挂载串行化回归测试。

## 背景（2026-10-17 生产 run 4811 / 4814 的另一半根因）
scan_queue 靠 ``_MOUNT_OWNER`` 做挂载串行化：一个库扫描期间锁住它要读的
远程挂载，别的库排队——防的是「不同的库同时打同一个远程挂载」。

但 rescrape（``backend/api/admin_scrape._run_library_rescrape``）不走
enqueue：它在 API 进程里直接起线程跑 ``scan_library_sync``，过去**不登记
占用**。于是日韩剧（库 45）与综艺（库 48）两条 rescrape 在同一进程里
并发跑了十几分钟——同用 mount 3，队列的串行化对它们完全不存在。
（两条 run 的会话崩溃已由 expire_on_commit=False 修掉，见
tests/test_rescrape_session_lifecycle.py；这里修的是并发本身。）

## 修复
scan_queue 提供公开的 ``mount_serial_guard(library_id, mount_ids)`` 上下文
管理器，与排队任务共用同一张 ``_MOUNT_OWNER`` 表和同一把条件锁：
rescrape 扫描前阻塞等挂载空闲、登记占用，扫完释放并唤醒等待者。
"""
import os
import tempfile
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.database import configure_session_local
from backend.emby_server import models as em
from backend.emby_server import scan_queue


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    base_models.Base.metadata.create_all(engine)
    return engine, path


def _add_lib(setup, name, mount_id):
    lib = em.Library(guid=f"guid-{name}", name=name,
                     collection_type="movies",
                     paths=f"mount://{mount_id}/media",
                     scrape_policy="missing_only", mount_ids="")
    setup.add(lib)
    setup.commit()
    lib_id = lib.id
    setup.close()
    return lib_id


def _clear_owners():
    with scan_queue._LOCK:
        scan_queue._MOUNT_OWNER.clear()


# ==================== guard 本身 ====================

def test_guard_registers_and_releases():
    _clear_owners()
    with mock.patch.object(scan_queue, "SCAN_QUEUE_ENABLED", True), \
            mock.patch.object(scan_queue, "SCAN_MOUNT_SERIAL", True):
        with scan_queue.mount_serial_guard(11, (3,)):
            with scan_queue._LOCK:
                assert scan_queue._MOUNT_OWNER == {3: 11}
        with scan_queue._LOCK:
            assert scan_queue._MOUNT_OWNER == {}


def test_guard_waits_until_other_library_releases():
    _clear_owners()
    with mock.patch.object(scan_queue, "SCAN_QUEUE_ENABLED", True), \
            mock.patch.object(scan_queue, "SCAN_MOUNT_SERIAL", True):
        with scan_queue._COND:
            scan_queue._MOUNT_OWNER[3] = 999  # 别的库正占着 mount 3
        entered = threading.Event()

        def _enter():
            with scan_queue.mount_serial_guard(11, (3,)):
                entered.set()

        t = threading.Thread(target=_enter)
        t.start()
        assert not entered.wait(timeout=0.5), "挂载被别人占着时必须等，不许并发进入"
        # 按 _release_locked 的方式释放 + 唤醒
        with scan_queue._COND:
            scan_queue._MOUNT_OWNER.pop(3, None)
            scan_queue._COND.notify_all()
        assert entered.wait(timeout=5), "占用释放后等待者应立刻接上"
        t.join(timeout=5)
        with scan_queue._LOCK:
            assert scan_queue._MOUNT_OWNER == {}


def test_guard_no_mounts_is_noop():
    _clear_owners()
    with mock.patch.object(scan_queue, "SCAN_QUEUE_ENABLED", True), \
            mock.patch.object(scan_queue, "SCAN_MOUNT_SERIAL", True):
        with scan_queue.mount_serial_guard(11, ()):
            pass
        with scan_queue._LOCK:
            assert scan_queue._MOUNT_OWNER == {}


# ==================== rescrape 接线 ====================

def test_concurrent_rescrapes_same_mount_serialize():
    """两个同挂载库的 rescrape 不许同时进入扫描；不同挂载的照常并行。

    修复前：rescrape 不登记占用，B 在 A 扫描期间立刻进入 → 红；
    修复后：B 等 A 释放后才进入，C（不同挂载）与 A 并行 → 绿。
    """
    from backend.api import admin_scrape
    from backend.emby_server import scanner as scan_lib

    engine, path = _make_db()
    try:
        setup = sessionmaker(bind=engine)()
        lib_a = _add_lib(setup, "A", 3)
        lib_b = _add_lib(setup, "B", 3)
        lib_c = _add_lib(setup, "C", 4)
        configure_session_local(sessionmaker(bind=engine))
        _clear_owners()

        entered = {lib_a: threading.Event(), lib_b: threading.Event(),
                   lib_c: threading.Event()}
        release = threading.Event()

        def fake_scan(db, library, snapshot, trigger="manual"):
            entered[library.id].set()
            release.wait(timeout=15)
            return {"added": 0}

        with mock.patch.object(scan_queue, "SCAN_QUEUE_ENABLED", True), \
                mock.patch.object(scan_queue, "SCAN_MOUNT_SERIAL", True), \
                mock.patch.object(scan_lib, "scan_library_sync",
                                  side_effect=fake_scan):
            ta = threading.Thread(target=admin_scrape._run_library_rescrape,
                                  args=(lib_a, "missing_only"))
            ta.start()
            assert entered[lib_a].wait(timeout=10), "A 应先进入扫描"
            with scan_queue._LOCK:
                assert scan_queue._MOUNT_OWNER.get(3) == lib_a, (
                    "rescrape 扫描期间必须把它的挂载登记在 _MOUNT_OWNER 里，"
                    "否则队列的挂载串行化对它不存在（2026-10-17 run 4811/4814）")

            tb = threading.Thread(target=admin_scrape._run_library_rescrape,
                                  args=(lib_b, "missing_only"))
            tb.start()
            assert not entered[lib_b].wait(timeout=1.5), (
                "同挂载的 B 必须等 A 扫完，不许并发进入扫描")

            tc = threading.Thread(target=admin_scrape._run_library_rescrape,
                                  args=(lib_c, "missing_only"))
            tc.start()
            assert entered[lib_c].wait(timeout=10), "不同挂载的 C 应能与 A 并行"

            release.set()
            assert entered[lib_b].wait(timeout=10), "A 释放挂载后 B 应接上"
            for t in (ta, tb, tc):
                t.join(timeout=10)
        with scan_queue._LOCK:
            assert scan_queue._MOUNT_OWNER == {}, "全部结束后占用必须清干净"
    finally:
        configure_session_local()
        engine.dispose()
        os.unlink(path)

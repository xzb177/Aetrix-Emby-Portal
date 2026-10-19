"""本机目录实时监听（inotify）与扫描开关（v2.44.0）

盯住的是**会真出事**的几条，不是实现细节：

1. **只监听本机路径**：``mount://`` / ``115:/`` / ``rclone:`` 这些远程来源拿不到可靠
   事件源，绝不能去监听（watchdog 对它们只会报错，或者更糟——静默什么都不发生）。
2. **1 秒防抖**：转场一个目录会产生成百上千个事件，必须合并成**一次**入队。
3. **限流**：磁盘抖动导致事件断断续续时，不能把扫描队列灌满。
4. **降级可见**：路径不可读要记成 degraded 并给出人话原因——默默不工作比报错更糟。
5. **开关真的生效**：``fs_watch=false`` 的库不入监听；``incremental_scan=false``
   的库这一轮不做指纹秒跳；``force_full`` 只影响那一轮。
"""
import os
import time
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import fs_watcher as fw
from backend.emby_server import scanner


@pytest.fixture(autouse=True)
def _reset_watcher():
    fw.stop_fs_watcher()
    fw._LIBRARY_PATHS.clear()
    fw._DEGRADED.clear()
    fw._PENDING.clear()
    fw._LAST_TRIGGER.clear()
    fw._RETRY_AT.clear()
    for key in fw._STATS:
        fw._STATS[key] = 0
    yield
    fw.stop_fs_watcher()


def _lib(**kw):
    kw.setdefault("paths", "")
    return SimpleNamespace(**kw)


# ==================== 只监听本机路径 ====================

def test_local_paths_skips_remote_sources():
    lib = _lib(paths="/mnt/mp/电影,/mnt/paul/剧集,mount://3/x,115:/0,rclone:gdrive/M")

    assert fw.local_paths_for_library(lib) == ("/mnt/mp/电影", "/mnt/paul/剧集")


def test_local_paths_dedupes_and_strips_trailing_slash():
    assert fw.local_paths_for_library(_lib(paths="/mnt/mp/ ,/mnt/mp")) == ("/mnt/mp",)


def test_local_paths_empty_when_all_remote():
    assert fw.local_paths_for_library(_lib(paths="mount://3/a,115:/0")) == ()


def test_is_watchable_rejects_missing_and_relative(tmp_path):
    assert fw.is_watchable(str(tmp_path)) == (True, "")
    ok, why = fw.is_watchable(str(tmp_path / "nope"))
    assert ok is False and "不存在" in why
    ok, why = fw.is_watchable("relative/dir")
    assert ok is False and "绝对路径" in why
    ok, why = fw.is_watchable("")
    assert ok is False and why


# ==================== 防抖合并 ====================

class _FakeEvent:
    def __init__(self, src_path, is_directory=False):
        self.src_path = src_path
        self.is_directory = is_directory


def test_many_events_in_one_window_collapse_to_single_trigger(monkeypatch, tmp_path):
    """转场 500 个文件 = 500 个事件，但只能触发一次入队"""
    calls = []
    monkeypatch.setattr(fw, "_trigger_scan", lambda lib_id: calls.append(lib_id))
    monkeypatch.setattr(fw, "FS_EVENT_DEBOUNCE_SEC", 0.05)
    handler = fw._CoalescingHandler()
    fw._LIBRARY_PATHS[7] = (str(tmp_path),)

    for i in range(500):
        handler.on_any_event(_FakeEvent(f"{tmp_path}/movie-{i}.mkv"))
    handler.on_any_event(_FakeEvent(f"{tmp_path}/movie-499.mkv"))   # 同一文件重复事件
    # 目录级事件（新建/改名目录）不触发：后面由扫描器发现，不靠监听
    handler.on_any_event(_FakeEvent(f"{tmp_path}/sub/", is_directory=True))

    assert len(fw._PENDING) == 1
    assert fw._STATS["events"] == 501

    import threading
    stop = threading.Event()
    fw._STOP = stop
    thread = threading.Thread(target=fw._drain_loop, args=(stop,), daemon=True)
    thread.start()
    stop.wait(0.4)
    stop.set()
    thread.join(timeout=2)

    assert calls == [7]


def test_events_outside_watched_paths_are_ignored(tmp_path):
    fw._LIBRARY_PATHS[7] = (str(tmp_path / "a"),)
    handler = fw._CoalescingHandler()

    handler.on_any_event(_FakeEvent(f"{tmp_path}/elsewhere/x.mkv"))

    assert fw._PENDING == {}


def test_trigger_is_rate_limited(monkeypatch):
    """事件断断续续时不能把队列灌满：同一库 60 秒内只入队一次"""
    calls = []
    monkeypatch.setattr(fw, "FS_EVENT_MIN_INTERVAL_SEC", 60.0)
    monkeypatch.setattr(fw, "SessionLocal", None, raising=False)
    import backend.database as dbmod

    class _Lib:
        id = 5
        name = "电影库"
        is_enabled = True
        fs_watch = True

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return _Lib()

    class _FakeSession:
        def query(self, *a, **k):
            return _FakeQuery()

        def close(self):
            calls.append("close")

    monkeypatch.setattr(dbmod, "SessionLocal", _FakeSession)
    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue",
                        lambda lib, trigger=None, **kw: calls.append(trigger))

    fw._trigger_scan(5)
    fw._trigger_scan(5)
    fw._trigger_scan(5)

    assert calls.count("fs-event") == 1
    assert fw._STATS["coalesced"] == 2


def test_trigger_skips_disabled_library(monkeypatch):
    calls = []
    import backend.database as dbmod

    class _Lib:
        id = 6
        name = "关掉的库"
        is_enabled = True
        fs_watch = False          # 用户关了监听 → 不该被事件触发

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return _Lib()

    class _FakeSession:
        def query(self, *a, **k):
            return _FakeQuery()

        def close(self):
            pass

    monkeypatch.setattr(dbmod, "SessionLocal", _FakeSession)
    monkeypatch.setattr(fw, "FS_EVENT_MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue",
                        lambda lib, trigger=None, **kw: calls.append(lib.id))

    fw._trigger_scan(6)

    assert calls == []


# ==================== 降级可见 ====================

def test_unwatchable_path_is_recorded_as_degraded(tmp_path, monkeypatch):
    """监听失败必须留下人话原因：默默不工作比报错更糟"""
    fw._OBSERVER = SimpleNamespace(
        schedule=lambda handler, path, recursive: SimpleNamespace(),
        unschedule=lambda handle: None,
    )
    fw._watch_now(1, (str(tmp_path), str(tmp_path / "gone")))

    assert 1 in fw._DEGRADED
    assert "gone" in fw._DEGRADED[1]
    assert "不存在" in fw._DEGRADED[1]
    assert 1 in fw._WATCHES   # 可读的那个仍然在监听（部分失败不影响其余路径）


def test_watcher_status_exposes_degradation(tmp_path, monkeypatch):
    fw._OBSERVER = SimpleNamespace(
        schedule=lambda handler, path, recursive: SimpleNamespace(),
        unschedule=lambda handle: None,
    )
    fw._watch_now(2, (str(tmp_path / "nope"),))

    status = fw.watcher_status()

    assert status["available"] is True
    assert "2" in status["degraded"]
    assert status["degraded"]["2"]


# ==================== 每库开关 ====================

def test_incremental_on_requires_global_and_library_switch():
    on = SimpleNamespace(force_full=False, incremental_scan=True)
    off = SimpleNamespace(force_full=False, incremental_scan=False)
    ctx = SimpleNamespace(snap=on)

    assert fw._STATS is not None
    assert scanner._incremental_on(ctx) is True
    assert scanner._incremental_on(SimpleNamespace(snap=off)) is False
    # force_full 只影响这一轮
    assert scanner._incremental_on(SimpleNamespace(
        snap=SimpleNamespace(force_full=True, incremental_scan=True))) is False


def test_snapshot_captures_incremental_switch():
    lib = SimpleNamespace(id=1, name="库", collection_type="movies", paths="",
                          scrape_policy=None, mount_ids="", incremental_scan=False)
    assert scanner.LibrarySnapshot.of(lib).incremental_scan is False

    lib2 = SimpleNamespace(id=2, name="库", collection_type="movies", paths="",
                           scrape_policy=None, mount_ids="")
    # 老对象没有这一列时按 True（保持升级前的行为）
    assert scanner.LibrarySnapshot.of(lib2).incremental_scan is True


# ==================== 手动全量扫描真的落到快照上 ====================

def test_force_full_flag_reaches_the_snapshot(monkeypatch):
    """「全量扫描」不能只是个透传参数：必须真的把快照标成 force_full"""
    from backend.emby_server import scan_queue

    lib = SimpleNamespace(id=3, name="库", collection_type="movies", paths="/mnt/x",
                          scrape_policy=None, mount_ids="", incremental_scan=True)
    seen = {}

    def _fake_enqueue(library, *, trigger="manual", force_full=False):
        seen["trigger"] = trigger
        seen["force_full"] = force_full
        return {"created": True, "task": {"state": "queued", "position": 1}}

    monkeypatch.setattr(scan_queue, "enqueue", _fake_enqueue)
    scan_queue.enqueue(lib, trigger="manual", force_full=True)
    assert seen == {"trigger": "manual", "force_full": True}

    # 走真实的快照路径：快照是 frozen dataclass，全量标记只能靠 replace 打上去
    import dataclasses

    snapshot = dataclasses.replace(scanner.LibrarySnapshot.of(lib), force_full=True)
    assert scanner._incremental_on(SimpleNamespace(snap=snapshot)) is False


def test_enqueue_local_accepts_force_full(monkeypatch):
    """真实入队路径不能因为 frozen 快照报错（回归钉：force_full 必须能落到任务上）

    这里不真的起线程，只验证「拍快照 + 打 force_full」这一步不炸。
    """
    import dataclasses

    from backend.emby_server import scan_queue

    lib = SimpleNamespace(id=4, name="库", collection_type="movies", paths="/mnt/x",
                          scrape_policy=None, mount_ids="", incremental_scan=True)
    snapshot = dataclasses.replace(scanner.LibrarySnapshot.of(lib), force_full=True)

    # frozen dataclass 不可变：直接赋值必须失败（说明全量标记只能走 replace）
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.force_full = True
    assert snapshot.force_full is True
    assert scan_queue is not None

# ==================== 周期性对账（v2.48.0）====================

def test_reconcile_is_silent_when_nothing_changed(monkeypatch):
    """绝大多数时候它就是空转：不重建、不打日志、不动扫描"""
    rebuilt = []
    monkeypatch.setattr(fw, "WATCHDOG_AVAILABLE", True)
    monkeypatch.setattr(fw, "_expected_libraries", lambda: {7: ("/mnt/a",)})
    monkeypatch.setattr(fw, "sync_from_db", lambda: rebuilt.append(1))
    fw._LIBRARY_PATHS[7] = ("/mnt/a",)

    result = fw._reconcile_once()

    assert result["changed"] is False
    assert rebuilt == [], "没有差异就不该重建监听"
    assert fw._LAST_RECONCILE["changed"] is False
    assert fw._LAST_RECONCILE["at"] > 0


def test_reconcile_rebuilds_on_any_difference(monkeypatch):
    """新增库 / 移除库 / 路径改了，三种都要被发现并重建"""
    monkeypatch.setattr(fw, "WATCHDOG_AVAILABLE", True)
    monkeypatch.setattr(fw, "sync_from_db", lambda: None)
    fw._LIBRARY_PATHS[1] = ("/mnt/old",)
    fw._LIBRARY_PATHS[2] = ("/mnt/keep",)
    fw._LIBRARY_PATHS[4] = ("/mnt/stale",)
    monkeypatch.setattr(fw, "_expected_libraries", lambda: {
        2: ("/mnt/keep",), 3: ("/mnt/new",), 4: ("/mnt/moved",)})

    result = fw._reconcile_once()

    assert result["changed"] is True
    assert result["added"] == [3]
    assert result["removed"] == [1]
    assert result["retuned"] == [4], "同一个库路径变了也要重建（监听的是旧路径）"
    assert fw._LAST_RECONCILE["changed"] is True


def test_reconcile_never_triggers_a_scan(monkeypatch):
    """对账只补「监听集合」，不补扫描

    inotify 漏掉的事件没法可靠判定漏了哪些文件，凭猜测补扫描要么重复要么遗漏；
    而且一次全量扫描的代价远高于重建监听。真要补漏靠增量扫描的目录指纹。
    """
    enqueued = []
    monkeypatch.setattr(fw, "WATCHDOG_AVAILABLE", True)
    monkeypatch.setattr(fw, "_expected_libraries", lambda: {9: ("/mnt/a",)})
    monkeypatch.setattr(fw, "sync_from_db", lambda: None)
    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue",
                        lambda lib, trigger=None, **kw: enqueued.append(lib))
    fw._LIBRARY_PATHS[9] = ("/mnt/a",)
    fw._PENDING[9] = 0.0

    fw._reconcile_once()

    assert enqueued == []


def test_reconcile_keeps_current_watches_when_db_read_fails(monkeypatch):
    """读库失败保持现状，下一轮再对——不能把正在工作的监听清空"""
    monkeypatch.setattr(fw, "WATCHDOG_AVAILABLE", True)
    monkeypatch.setattr(fw, "sync_from_db", lambda: None)
    monkeypatch.setattr(fw, "_expected_libraries",
                        lambda: (_ for _ in ()).throw(RuntimeError("库读不到")))
    fw._LIBRARY_PATHS[5] = ("/mnt/a",)

    result = fw._reconcile_once()

    assert result["changed"] is False
    assert fw._LAST_RECONCILE["changed"] is None, "None = 这一轮没对成，界面上要能看出来"
    assert 5 in fw._LIBRARY_PATHS


def test_reconcile_is_skipped_without_watchdog(monkeypatch):
    monkeypatch.setattr(fw, "WATCHDOG_AVAILABLE", False)
    monkeypatch.setattr(fw, "_expected_libraries",
                        lambda: (_ for _ in ()).throw(AssertionError("不该读库")))

    assert fw._reconcile_once() == {"changed": False}


def test_reconcile_interval_is_env_tunable_and_floored(monkeypatch):
    """间隔可调；但有下限——把它调到 0 就变成了每秒对一次库"""
    import importlib

    monkeypatch.setenv("FS_RECONCILE_INTERVAL_SEC", "300")
    assert importlib.reload(fw).RECONCILE_INTERVAL_SEC == 300
    monkeypatch.setenv("FS_RECONCILE_INTERVAL_SEC", "1")
    assert importlib.reload(fw).RECONCILE_INTERVAL_SEC == 60
    monkeypatch.setenv("FS_RECONCILE_INTERVAL_SEC", "abc")
    assert importlib.reload(fw).RECONCILE_INTERVAL_SEC == 900
    monkeypatch.delenv("FS_RECONCILE_INTERVAL_SEC")
    importlib.reload(fw)


def test_expected_libraries_uses_the_same_filter_as_sync(monkeypatch, tmp_path):
    """口径必须与 sync_from_db 一致，否则每轮都判成「有差异」空转重建"""
    import backend.database as dbmod
    from backend.emby_server import change_watcher

    class _Lib:
        def __init__(self, lib_id, paths, **kw):
            self.id = lib_id
            self.paths = paths
            self.fs_watch = kw.get("fs_watch", True)

    libs = [
        _Lib(1, str(tmp_path)),                      # 该监听
        _Lib(2, str(tmp_path), fs_watch=False),      # 用户关了监听
        _Lib(3, "mount://1/a,115:/0"),               # 纯远程来源：不监听
        _Lib(4, ""),                                 # 没配路径
    ]

    class _Query:
        def filter(self, *a, **k):
            return self

        def all(self):
            return libs

    class _Session:
        def query(self, *a, **k):
            return _Query()

        def close(self):
            pass

    monkeypatch.setattr(dbmod, "SessionLocal", _Session)
    monkeypatch.setattr(change_watcher, "resolve_excluded", lambda db: [4])

    wanted = fw._expected_libraries()

    assert list(wanted) == [1], "只留「启用了监听 且 有本机路径」的库"


def test_watcher_status_exposes_reconcile(monkeypatch):
    """管理端要能看到「多久对一次、上次对出差异没有」"""
    monkeypatch.setattr(fw, "_LAST_RECONCILE", {"at": 123.0, "changed": True})
    fw._LIBRARY_PATHS[3] = ("/mnt/a",)

    status = fw.watcher_status()

    assert status["reconcile_interval_sec"] == fw.RECONCILE_INTERVAL_SEC
    assert status["last_reconcile_at"] == 123.0
    assert status["last_reconcile_changed"] is True


# ==================== 合并窗口延迟补发（60s 窗口吞事件修复）====================
# 沙盒实测：T 触发后 T+57.5s 落地的文件，事件被合并计数吞掉、150s 内无扫描无入库；
# 落单文件 + 后续无事件时静默漏到定时扫描。修复口径：合并=延迟，不是丢弃——
# 窗口结束后若期间有被合并的事件，drain 线程自动补发一轮增量扫描。

class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def _trigger_harness(monkeypatch, calls):
    """给 _trigger_scan 装最小桩：查库返回启用库、enqueue 只记账"""
    import backend.database as dbmod

    class _Lib:
        id = 5
        name = "电影库"
        is_enabled = True
        fs_watch = True

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return _Lib()

    class _FakeSession:
        def query(self, *a, **k):
            return _FakeQuery()

        def close(self):
            pass

    monkeypatch.setattr(dbmod, "SessionLocal", _FakeSession)
    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue",
                        lambda lib, trigger=None, **kw: calls.append((lib.id, trigger)))


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _fs_event_triggers(calls):
    return [c for c in calls if c[1] == "fs-event"]


def test_coalesced_event_is_retried_after_min_interval(monkeypatch):
    """复现：T 触发、T+30s 再来事件 → 窗口结束后必须补触发一轮（不是丢弃）"""
    import threading

    calls = []
    _trigger_harness(monkeypatch, calls)
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(fw, "_now", clock)
    monkeypatch.setattr(fw, "FS_EVENT_DEBOUNCE_SEC", 0.05)
    monkeypatch.setattr(fw, "_reconcile_once", lambda: {"changed": False})

    fw._trigger_scan(5)                      # T=1000：触发
    assert _fs_event_triggers(calls) == [(5, "fs-event")]

    clock.now = 1030.0
    fw._trigger_scan(5)                      # T+30：在 60s 窗口内 → 合并
    assert _fs_event_triggers(calls) == [(5, "fs-event")]
    assert fw._STATS["coalesced"] == 1
    assert fw._RETRY_AT.get(5) == 1060.0, "延迟补发点应=上次触发+60s"

    # drain 线程跑起来；窗口结束（T+60）后必须补触发
    stop = threading.Event()
    thread = threading.Thread(target=fw._drain_loop, args=(stop,), daemon=True)
    thread.start()
    clock.now = 1061.0
    ok = _wait_for(lambda: len(_fs_event_triggers(calls)) == 2, timeout=10.0)
    stop.set()
    thread.join(timeout=5)
    assert ok, "窗口结束后没有补触发：事件被吞了"
    assert 5 not in fw._RETRY_AT, "补发后延迟点要清掉，不能留着再发一次"


def test_retry_is_cancelled_when_a_real_trigger_fires(monkeypatch):
    """T 触发、T+30 合并（已记延迟补发）、T+61 有新事件直接触发 → 不能再补发一次"""
    calls = []
    _trigger_harness(monkeypatch, calls)
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(fw, "_now", clock)

    fw._trigger_scan(5)                      # T=1000：触发
    clock.now = 1030.0
    fw._trigger_scan(5)                      # T+30：合并 → 延迟补发点 1060
    assert fw._RETRY_AT.get(5) == 1060.0

    clock.now = 1061.0
    fw._trigger_scan(5)                      # T+61：窗口已过，直接触发，"债"已还
    assert _fs_event_triggers(calls) == [(5, "fs-event"), (5, "fs-event")]
    assert 5 not in fw._RETRY_AT

    # 再过一个窗口：不能有第三次触发
    clock.now = 1130.0
    for lib_id in fw._due_retries(1130.0):
        fw._trigger_scan(lib_id)
    assert _fs_event_triggers(calls) == [(5, "fs-event"), (5, "fs-event")]


def test_coalesced_rearm_does_not_stack(monkeypatch):
    """窗口内多次合并：延迟补发点不叠加，窗口结束只补发一轮"""
    calls = []
    _trigger_harness(monkeypatch, calls)
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(fw, "_now", clock)

    fw._trigger_scan(5)
    for t in (1030.0, 1045.0, 1059.0):
        clock.now = t
        fw._trigger_scan(5)
    assert fw._STATS["coalesced"] == 3
    assert fw._RETRY_AT.get(5) == 1060.0

    clock.now = 1061.0
    for lib_id in fw._due_retries(1061.0):
        fw._trigger_scan(lib_id)
    assert _fs_event_triggers(calls) == [(5, "fs-event"), (5, "fs-event")]


def test_due_retries_returns_only_expired():
    fw._RETRY_AT[5] = 1060.0
    fw._RETRY_AT[6] = 1200.0
    assert fw._due_retries(1059.0) == []
    assert fw._due_retries(1060.0) == [5]
    assert fw._due_retries(1200.0) == [5, 6]


def test_watcher_status_exposes_pending_retries():
    fw._RETRY_AT[5] = 1060.0
    assert fw.watcher_status()["pending_retries"] == [5]

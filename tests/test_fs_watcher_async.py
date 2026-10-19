"""fs_watcher 异步初始同步 + 大树降级（v2.53.0）

盯住的是**会真出事**的几条：

1. **启动不等同步**：start_fs_watcher 必须立即返回——之前它是同步调
   sync_from_db，/strm 8.5 万文件的 recursive 监听把 worker 主线程卡住几十分钟，
   后面的 Redis 扫描队列消费线程永远起不来，扫描一直「等待调度」。
2. **后台同步最终会跑完**：起了线程就不能丢，监听最终要建起来。
3. **大树直接降级**：子目录超过预算的路径不做 recursive 监听（建 watch 本身
   就是灾难），记成 degraded 走定时扫描，界面看得见。
4. **停机竞态不抛异常**：后台同步跑一半时 stop_fs_watcher 被调，不能 AttributeError。
"""
import os
import time
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import fs_watcher as fw


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
    # 等后台同步线程收尾，避免它把状态写进下一个测试
    t = fw._SYNC_THREAD
    if t is not None:
        t.join(timeout=10)
    fw.stop_fs_watcher()


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ==================== 1. 启动不等同步 ====================

def test_start_returns_immediately_while_sync_is_slow(monkeypatch):
    """sync 再慢，start_fs_watcher 也必须秒回——worker 主线程不能被它卡住"""
    started = time.monotonic()
    release = []

    def slow_sync():
        release.append(True)
        time.sleep(5)  # 模拟 /strm 大树下的漫长建监听
        return {"watched": 0, "degraded": 0, "dirs": 0, "available": True}

    monkeypatch.setattr(fw, "sync_from_db", slow_sync)
    assert fw.start_fs_watcher() is True
    assert time.monotonic() - started < 2.0, "start_fs_watcher 被同步卡住了"
    assert release, "后台同步线程没有被启动"


def test_initial_sync_completes_in_background(monkeypatch):
    """后台线程最终会把同步跑完（起了就不能丢）"""
    calls = []

    def fast_sync():
        calls.append(True)
        return {"watched": 0, "degraded": 0, "dirs": 0, "available": True}

    monkeypatch.setattr(fw, "sync_from_db", fast_sync)
    assert fw.start_fs_watcher() is True
    assert _wait_for(lambda: bool(calls), timeout=10.0), "后台同步没有执行"


def test_sync_running_visible_in_status(monkeypatch):
    """watcher_status 暴露 sync_running：同步中 watched 为空是正常的，界面可提示"""
    gate = []

    def slow_sync():
        gate.append(True)
        time.sleep(3)
        return {"watched": 0, "degraded": 0, "dirs": 0, "available": True}

    monkeypatch.setattr(fw, "sync_from_db", slow_sync)
    assert fw.start_fs_watcher() is True
    assert _wait_for(lambda: fw.watcher_status()["sync_running"], timeout=5.0)
    assert _wait_for(lambda: not fw.watcher_status()["sync_running"], timeout=10.0)


# ==================== 2. 大树降级 ====================

class _FakeObserver:
    def __init__(self):
        self.scheduled = []

    def schedule(self, handler, path, recursive=False):
        self.scheduled.append((path, recursive))
        return object()

    def unschedule(self, handle):
        pass


def _make_tree(root, depth=1, breadth=4):
    """造 depth 层、每层 breadth 个子目录的树"""
    import pathlib
    root = pathlib.Path(root)
    level = [root]
    for _ in range(depth):
        nxt = []
        for parent in level:
            for i in range(breadth):
                d = parent / f"d{i}"
                d.mkdir(parents=True, exist_ok=True)
                nxt.append(d)
        level = nxt
    return root


def test_huge_tree_degrades_to_polling(tmp_path, monkeypatch):
    """子目录超预算 → 不建 watch，记 degraded（人话原因），走定时扫描"""
    _make_tree(tmp_path, depth=2, breadth=3)  # 3 + 9 = 12 个子目录
    monkeypatch.setattr(fw, "FS_WATCH_MAX_SUBDIRS", 5)
    fake = _FakeObserver()
    monkeypatch.setattr(fw, "_OBSERVER", fake)

    fw._watch_now(7, (str(tmp_path),))

    assert fake.scheduled == [], "超预算的树不应该建任何 watch"
    assert 7 not in fw._WATCHES
    reason = fw._DEGRADED.get(7, "")
    assert "子目录过多" in reason and "定时扫描" in reason


def test_small_tree_still_watched(tmp_path, monkeypatch):
    """预算内的树照常监听，不误伤"""
    _make_tree(tmp_path, depth=1, breadth=2)  # 2 个子目录
    monkeypatch.setattr(fw, "FS_WATCH_MAX_SUBDIRS", 2000)
    fake = _FakeObserver()
    monkeypatch.setattr(fw, "_OBSERVER", fake)

    fw._watch_now(7, (str(tmp_path),))

    assert len(fake.scheduled) == 1
    assert 7 in fw._WATCHES
    assert 7 not in fw._DEGRADED


def test_count_subdirs_capped_early_exits(tmp_path):
    """计数到 cap 就停：8.5 万文件级别的树也不用全走完"""
    _make_tree(tmp_path, depth=3, breadth=10)  # 10+100+1000 = 1110 个子目录
    started = time.monotonic()
    n = fw._count_subdirs_capped(str(tmp_path), 50)
    elapsed = time.monotonic() - started
    assert n == 50
    assert elapsed < 5.0, f"cap 没生效，全树走了 {elapsed:.1f}s"
    # cap 足够大时能数全
    assert fw._count_subdirs_capped(str(tmp_path), 5000) == 1110


def test_count_subdirs_ignores_symlink_loops(tmp_path):
    """symlink 循环不能把计数器卡死：symlink 根本不跟进去（也不计数）"""
    a = tmp_path / "a"
    a.mkdir()
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    started = time.monotonic()
    n = fw._count_subdirs_capped(str(tmp_path), 100)
    assert time.monotonic() - started < 5.0, "symlink 循环卡死了计数器"
    assert n == 1  # 只有 a；loop 是 symlink，不跟进也不计数


# ==================== 3. 停机竞态 ====================

def test_watch_now_handles_stopped_observer(monkeypatch):
    """后台同步跑一半时 stop 被调：observer 没了就记降级，不抛 AttributeError"""
    monkeypatch.setattr(fw, "_OBSERVER", None)
    fw._watch_now(9, ("/tmp",))  # 不抛就是赢
    assert "停止" in fw._DEGRADED.get(9, "")


def test_concurrent_syncs_do_not_overlap(monkeypatch):
    """初始同步和对账同时触发也不能重叠跑（_WATCHES 会被两个线程同时改）"""
    active = []
    max_active = []

    def slow_inner():
        active.append(1)
        max_active.append(len(active))
        time.sleep(0.3)
        active.pop()
        return {"watched": 0, "degraded": 0, "dirs": 0, "available": True}

    monkeypatch.setattr(fw, "_sync_from_db", slow_inner)
    threads = [
        __import__("threading").Thread(target=fw.sync_from_db) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert max(max_active) == 1, f"同步重叠跑了: {max_active}"


def test_start_is_idempotent_while_running(monkeypatch):
    """重复调用 start_fs_watcher 不会起第二个 observer/同步线程"""
    calls = []

    def fast_sync():
        calls.append(1)
        return {"watched": 0, "degraded": 0, "dirs": 0, "available": True}

    monkeypatch.setattr(fw, "sync_from_db", fast_sync)
    assert fw.start_fs_watcher() is True
    assert fw.start_fs_watcher() is False  # 已经在跑
    assert _wait_for(lambda: bool(calls), timeout=10.0)
    time.sleep(0.3)
    assert len(calls) == 1, f"同步被触发了 {len(calls)} 次"

"""播放预热（backend/emby_server/playback_prewarm.py）单元测试。

覆盖：并发上限（超限直接跳过、不排队）、同文件去重窗口、超时放弃、
中途取消、指标快照、配置解析（默认/DB 值/非法钳制）。

用隔离的内存 SQLite，不碰生产库；工作线程的文件 IO 全部走注入的
假文件对象，不碰真实磁盘。
"""
import threading
import time

import pytest


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models
    from backend.integrations import store

    # 热读缓存（store）是进程级全局的：不清掉会把上一个用例的库态带进来
    store.invalidate()
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        store.invalidate()


def _cfg(**overrides):
    cfg = {
        "enabled": True,
        "bytes": 1024,
        "max_concurrent": 4,
        "dedup_seconds": 300,
        "timeout_seconds": 30.0,
        "chunk_bytes": 256,
    }
    cfg.update(overrides)
    return cfg


class _FakeFile:
    """可配速/可配大小的假文件（with 上下文 + read(n)）。"""

    def __init__(self, size=10 ** 6, read_delay=0.0):
        self._remaining = size
        self._read_delay = read_delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        if self._read_delay:
            time.sleep(self._read_delay)
        if self._remaining <= 0:
            return b""
        n = min(n, self._remaining)
        self._remaining -= n
        return b"\0" * n


class _GateFile(_FakeFile):
    """第一块 read 阻塞住（占住并发槽），测试放行后才继续。"""

    def __init__(self):
        super().__init__(size=10 ** 6)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._reads = 0

    def read(self, n):
        self._reads += 1
        if self._reads == 1:
            self.entered.set()
            assert self.release.wait(10), "测试 10 秒内没放行，卡住了"
        return super().read(n)


class _FailFile:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        raise OSError("disk gone")



def _wait_idle(pw, timeout=15.0):
    """轮询等工作线程自然结束（不用 shutdown：它会先取消在途任务）。"""
    deadline = time.monotonic() + timeout
    while pw.metrics_snapshot()["active"] > 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    pw.shutdown()  # 收尾（线程已结束时是空操作）
    assert pw.metrics_snapshot()["active"] == 0, "工作线程 15 秒内没结束"

def _new_pw():
    from backend.emby_server.playback_prewarm import PlaybackPrewarmer

    return PlaybackPrewarmer()


# ---------- 并发上限 ----------

def test_concurrent_limit_skips_without_queueing():
    pw = _new_pw()
    gate = _GateFile()
    pw._open = lambda path, mode: gate
    cfg = _cfg(max_concurrent=1, dedup_seconds=0)

    assert pw.submit("/a.mp4", cfg) == "started"
    assert gate.entered.wait(10), "工作线程 10 秒内没开始读"
    # 并发已满：直接跳过，不排队
    assert pw.submit("/b.mp4", cfg) == "busy"
    assert pw.submit("/c.mp4", cfg) == "busy"

    snap = pw.metrics_snapshot()
    assert snap["counters"]["triggered"] == 3
    assert snap["counters"]["started"] == 1
    assert snap["counters"]["skipped_busy"] == 2
    assert snap["active"] == 1

    gate.release.set()
    _wait_idle(pw)
    snap = pw.metrics_snapshot()
    assert snap["counters"]["success"] == 1
    assert snap["active"] == 0


def test_concurrent_limit_allows_up_to_limit():
    pw = _new_pw()
    gates = [_GateFile(), _GateFile()]
    it = iter(gates)
    pw._open = lambda path, mode: next(it)
    cfg = _cfg(max_concurrent=2, dedup_seconds=0)
    assert pw.submit("/a.mp4", cfg) == "started"
    assert pw.submit("/b.mp4", cfg) == "started"
    assert all(g.entered.wait(10) for g in gates), "两个工作线程都应占住槽位"
    assert pw.submit("/c.mp4", cfg) == "busy"
    for g in gates:
        g.release.set()
    _wait_idle(pw)
    assert pw.metrics_snapshot()["counters"]["success"] == 2


# ---------- 去重 ----------

def test_dedup_window_skips_repeat():
    pw = _new_pw()
    pw._open = lambda path, mode: _FakeFile(size=64)
    cfg = _cfg(dedup_seconds=300)

    assert pw.submit("/a.mp4", cfg) == "started"
    assert pw.submit("/a.mp4", cfg) == "dedup"     # 窗口内同一文件
    assert pw.submit("/b.mp4", cfg) == "started"    # 不同文件不受影响
    _wait_idle(pw)

    snap = pw.metrics_snapshot()
    assert snap["counters"]["started"] == 2
    assert snap["counters"]["skipped_dedup"] == 1
    assert snap["counters"]["success"] == 2


def test_dedup_zero_disables():
    pw = _new_pw()
    pw._open = lambda path, mode: _FakeFile(size=64)
    cfg = _cfg(dedup_seconds=0)
    assert pw.submit("/a.mp4", cfg) == "started"
    assert pw.submit("/a.mp4", cfg) == "started"
    _wait_idle(pw)
    snap = pw.metrics_snapshot()
    assert snap["counters"]["skipped_dedup"] == 0
    assert snap["counters"]["success"] == 2


# ---------- 超时 ----------

def test_timeout_abandons_slow_read():
    pw = _new_pw()
    # 每块 4096 字节读 0.3 秒，共 10 块 ≈ 3 秒；超时 1 秒 → 必超时
    pw._open = lambda path, mode: _FakeFile(size=10 ** 6, read_delay=0.3)
    cfg = _cfg(bytes=40960, chunk_bytes=4096, timeout_seconds=1.0, dedup_seconds=0)

    assert pw.submit("/slow.mp4", cfg) == "started"
    # 注意：不能调 shutdown()——它会先取消在途任务，口径就变成 cancelled 了；
    # 这里轮询等工作线程自己因超时退出
    deadline = time.monotonic() + 15
    while pw.metrics_snapshot()["active"] > 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    pw.shutdown()  # 收尾（此时线程已结束，是空操作）

    snap = pw.metrics_snapshot()
    assert snap["counters"]["timeout"] == 1
    assert snap["counters"]["success"] == 0
    assert snap["counters"]["failed"] == 0
    # 超时任务也计入耗时统计
    assert snap["avg_elapsed_ms"] >= 900


def test_fast_read_does_not_timeout():
    pw = _new_pw()
    pw._open = lambda path, mode: _FakeFile(size=64)
    cfg = _cfg(timeout_seconds=30.0)
    assert pw.submit("/ok.mp4", cfg) == "started"
    _wait_idle(pw)
    snap = pw.metrics_snapshot()
    assert snap["counters"]["success"] == 1
    assert snap["counters"]["timeout"] == 0


# ---------- 取消 ----------

def test_cancel_stops_inflight():
    pw = _new_pw()
    gate = _GateFile()
    pw._open = lambda path, mode: gate
    # 两块 4096：第一块读时阻塞住，取消发生在第二块边界
    cfg = _cfg(bytes=8192, chunk_bytes=4096, dedup_seconds=0)

    assert pw.submit("/a.mp4", cfg) == "started"
    assert gate.entered.wait(10)
    assert pw.cancel("/a.mp4") is True
    assert pw.cancel("/nope.mp4") is False
    gate.release.set()
    pw.shutdown()

    snap = pw.metrics_snapshot()
    assert snap["counters"]["cancelled"] == 1
    assert snap["counters"]["success"] == 0


# ---------- 失败与开关 ----------

def test_failed_read_counts():
    pw = _new_pw()
    pw._open = lambda path, mode: _FailFile()
    cfg = _cfg(dedup_seconds=0)
    assert pw.submit("/gone.mp4", cfg) == "started"
    _wait_idle(pw)
    snap = pw.metrics_snapshot()
    assert snap["counters"]["failed"] == 1
    assert snap["counters"]["success"] == 0


def test_disabled_counts_and_skips():
    pw = _new_pw()
    pw._open = lambda path, mode: _FakeFile(size=64)
    assert pw.submit("/a.mp4", _cfg(enabled=False)) == "disabled"
    snap = pw.metrics_snapshot()
    assert snap["counters"]["skipped_disabled"] == 1
    assert snap["counters"]["started"] == 0


def test_zero_bytes_skips_without_thread():
    pw = _new_pw()
    pw._open = lambda path, mode: _FakeFile(size=64)
    assert pw.submit("/a.mp4", _cfg(bytes=0)) == "disabled"
    snap = pw.metrics_snapshot()
    assert snap["counters"]["started"] == 0
    assert snap["counters"]["skipped_disabled"] == 1


def test_small_file_reads_to_eof_as_success():
    # 文件比目标字节数小：读到 EOF 即成功，不算失败
    pw = _new_pw()
    pw._open = lambda path, mode: _FakeFile(size=10)
    assert pw.submit("/tiny.mp4", _cfg(bytes=1024, dedup_seconds=0)) == "started"
    _wait_idle(pw)
    snap = pw.metrics_snapshot()
    assert snap["counters"]["success"] == 1
    assert snap["total_bytes_read"] == 10


# ---------- 指标 ----------

def test_metrics_snapshot_shape():
    pw = _new_pw()
    pw._open = lambda path, mode: _FakeFile(size=100)
    cfg = _cfg(bytes=100, chunk_bytes=32, dedup_seconds=0)
    pw.submit("/m.mp4", cfg)
    _wait_idle(pw)
    snap = pw.metrics_snapshot()
    assert set(snap["counters"]) == {
        "triggered", "started", "skipped_disabled", "skipped_dedup",
        "skipped_busy", "skipped_quota", "success", "timeout", "failed",
        "cancelled",
    }
    assert snap["total_bytes_read"] == 100
    assert snap["avg_elapsed_ms"] >= 0
    assert snap["max_elapsed_ms"] >= snap["avg_elapsed_ms"]
    assert snap["active"] == 0


def test_maybe_prewarm_never_raises(db):
    from backend import models
    from backend.emby_server import playback_prewarm
    from backend.integrations import store

    # 预热默认关（2026-10-16 口径）：本测试验开启后的容错，先显式打开开关
    db.add(models.SystemConfig(key="playback_prewarm_enabled", value="true"))
    db.commit()
    store.invalidate()

    pw = _new_pw()
    pw._open = lambda path, mode: _FailFile()
    assert pw.maybe_prewarm(db, "/x.mp4") == "started"
    _wait_idle(pw)
    assert pw.metrics_snapshot()["counters"]["failed"] == 1
    # 空路径不抛
    assert pw.maybe_prewarm(db, "") == "disabled"
    assert pw.maybe_prewarm(db, None) == "disabled"


# ---------- 配置解析 ----------

def test_read_config_defaults(db):
    from backend.emby_server import playback_prewarm

    cfg = playback_prewarm.read_config(db)
    assert cfg == {
        "enabled": False,
        "bytes": 10485760,
        "max_concurrent": 4,
        "dedup_seconds": 300,
        "timeout_seconds": 30.0,
        "chunk_bytes": 262144,
    }


def test_read_config_honors_db(db):
    from backend import models
    from backend.emby_server import playback_prewarm
    from backend.integrations import store

    db.add(models.SystemConfig(key="playback_prewarm_enabled", value="false"))
    db.add(models.SystemConfig(key="playback_prewarm_bytes", value="5242880"))
    db.add(models.SystemConfig(key="playback_prewarm_max_concurrent", value="8"))
    db.commit()
    store.invalidate()

    cfg = playback_prewarm.read_config(db)
    assert cfg["enabled"] is False
    assert cfg["bytes"] == 5242880
    assert cfg["max_concurrent"] == 8
    # 没设的键走默认
    assert cfg["dedup_seconds"] == 300


def test_read_config_clamps_garbage(db):
    from backend import models
    from backend.emby_server import playback_prewarm
    from backend.integrations import store

    db.add(models.SystemConfig(key="playback_prewarm_max_concurrent", value="9999"))
    db.add(models.SystemConfig(key="playback_prewarm_bytes", value="not-a-number"))
    db.add(models.SystemConfig(key="playback_prewarm_timeout_seconds", value="-5"))
    db.add(models.SystemConfig(key="playback_prewarm_dedup_seconds", value=""))
    db.commit()
    store.invalidate()

    cfg = playback_prewarm.read_config(db)
    assert cfg["max_concurrent"] == 64      # 钳到上限
    assert cfg["bytes"] == 10485760        # 非法回默认
    assert cfg["timeout_seconds"] == 1.0   # 钳到下限
    assert cfg["dedup_seconds"] == 300     # 空串回默认


def test_singleton_accessors():
    from backend.emby_server import playback_prewarm

    assert playback_prewarm.get_prewarmer() is playback_prewarm.get_prewarmer()
    snap = playback_prewarm.metrics_snapshot()
    assert "counters" in snap and "avg_elapsed_ms" in snap


def test_maybe_prewarm_skips_quota_blocked(db):
    """配额负缓存期内的文件：预热直接跳过（返回 quota），不起线程读文件头。"""
    from backend.emby_server import playback_prewarm, streaming

    streaming.note_drive_quota_exceeded("fid-quota-test-pw")
    pw = _new_pw()
    assert pw.maybe_prewarm(db, "/x.mp4", "fid-quota-test-pw") == "quota"
    snap = pw.metrics_snapshot()["counters"]
    assert snap["skipped_quota"] == 1
    assert snap["started"] == 0

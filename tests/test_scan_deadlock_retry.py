"""扫描批次撞 PG 死锁时的重试行为

背景（2026-10-14 生产事故）：enrich worker 的批量认领
``UPDATE emby_items ... WHERE id IN (...) AND enrich_status='pending'``
与扫描的逐行 ``UPDATE emby_items ... WHERE id = ...`` 并发时，行锁获取
顺序相反 → PG 报 DeadlockDetected → 整库扫描失败
（run 1981 华语电影 03:03、run 2077 欧美剧 07:11）。

修复：``emit_batch`` 把整批包进死锁重试（抖动退避，最多 3 次）。
这里测的是：

1. ``is_deadlock`` 能识别 PG 死锁（修前：函数不存在；修后：True）；
2. 批次写循环里撞死锁 → 整批回滚重试，而不是把异常抛出去杀死整库扫描
   （修前红：死锁异常直接穿透生成器；修后绿：重试后成功）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.db_retry import deadlock_backoff, is_deadlock
from backend.emby_server import scanner
from backend.emby_server import models as emby_models


# psycopg2.errors.DeadlockDetected 在测试环境不一定装了：
# is_deadlock 用类名 + 消息双重判定，这里造一个同名类即可。
FakeDeadlockDetected = type("DeadlockDetected", (Exception,), {})


def _deadlock_exc():
    return OperationalError(
        "UPDATE emby_items SET size=%(size)s WHERE emby_items.id = %(id)s",
        {},
        FakeDeadlockDetected("deadlock detected"),
    )


# ==================== 第 1 层：is_deadlock 判定 ====================

def test_is_deadlock_detects_psycopg2_deadlock():
    assert is_deadlock(_deadlock_exc()) is True


def test_is_deadlock_detects_message():
    exc = OperationalError("x", {}, Exception("deadlock detected"))
    assert is_deadlock(exc) is True


def test_is_deadlock_rejects_sqlite_lock():
    exc = OperationalError("x", {}, Exception("database is locked"))
    assert is_deadlock(exc) is False


def test_is_deadlock_rejects_generic_error():
    assert is_deadlock(ValueError("boom")) is False


def test_deadlock_backoff_sleeps_briefly(monkeypatch):
    slept = []
    monkeypatch.setattr("backend.db_retry.time.sleep", slept.append)
    deadlock_backoff(1)
    assert len(slept) == 1 and 0 < slept[0] <= 0.5


# ==================== 第 2 层：emit_batch 死锁重试 ====================

@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    emby_models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(emby_models.Library(id=99, guid="test-lib-99", name="测试库"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _fake_pending(scan_file):
    return SimpleNamespace(scan_file=scan_file, skipped=False, fast_skipped=False)


def test_emit_batch_retries_whole_batch_on_deadlock(db, monkeypatch):
    """写回调在 flush 时撞死锁 → 整批回滚重试，最终成功（修前红修后绿）"""
    prepare_calls = []

    def fake_prepare(db_, batch, ctx, pool):
        prepare_calls.append(list(batch))
        return [_fake_pending(sf) for sf in batch]

    monkeypatch.setattr(scanner, "_prepare_and_prefetch", fake_prepare)
    monkeypatch.setattr(scanner, "_claim_batch_guids", lambda *a: None)
    monkeypatch.setattr(scanner, "_store_dir_states", lambda *a: None)
    monkeypatch.setattr(scanner, "SCAN_BATCH", 2)
    monkeypatch.setattr("backend.emby_server.scanner.deadlock_backoff", lambda attempt: None)

    ctx = scanner._ScanContext(snap=SimpleNamespace(), lib_id=99, stats={})
    files = [SimpleNamespace(stored_path=f"/v/f{i}.mkv", name=f"f{i}.mkv")
             for i in range(2)]

    seen = []
    raised_once = {"done": False}

    def _process_one(scan_file, _pending):
        # 模拟生产时序：写回调处理第一个条目时 flush 撞死锁
        if not raised_once["done"]:
            raised_once["done"] = True
            raise _deadlock_exc()
        seen.append(scan_file)

    # 回调驱动：死锁在 _process_one 里抛出，被 emit_batch 兜住重试
    scanner._iter_prepared(ctx, files, None, db, _process_one,
                           "test", SimpleNamespace(total=0), {}, [])

    # 死锁被吞掉并重试：准备阶段跑了两次，第二遍两个条目都正常产出
    assert len(prepare_calls) == 2
    assert len(seen) == 2


def test_emit_batch_reraises_non_deadlock(db, monkeypatch):
    """非死锁的 OperationalError 不重试，直接抛出去"""
    def fake_prepare(db_, batch, ctx, pool):
        return [_fake_pending(sf) for sf in batch]

    monkeypatch.setattr(scanner, "_prepare_and_prefetch", fake_prepare)
    monkeypatch.setattr(scanner, "_claim_batch_guids", lambda *a: None)
    monkeypatch.setattr(scanner, "_store_dir_states", lambda *a: None)
    monkeypatch.setattr(scanner, "SCAN_BATCH", 2)

    ctx = scanner._ScanContext(snap=SimpleNamespace(), lib_id=99, stats={})
    files = [SimpleNamespace(stored_path="/v/f1.mkv", name="f1.mkv")]

    def _process_one(_scan_file, _pending):
        raise OperationalError("x", {}, Exception("database is locked"))

    with pytest.raises(OperationalError, match="database is locked"):
        scanner._iter_prepared(ctx, files, None, db, _process_one,
                               "test", SimpleNamespace(total=0), {}, [])


def test_emit_batch_gives_up_after_three_deadlocks(db, monkeypatch):
    """连续 3 次死锁后不再重试，抛出去留证据（不无限重试）"""
    def fake_prepare(db_, batch, ctx, pool):
        return [_fake_pending(sf) for sf in batch]

    monkeypatch.setattr(scanner, "_prepare_and_prefetch", fake_prepare)
    monkeypatch.setattr(scanner, "_claim_batch_guids", lambda *a: None)
    monkeypatch.setattr(scanner, "_store_dir_states", lambda *a: None)
    monkeypatch.setattr(scanner, "SCAN_BATCH", 1)
    monkeypatch.setattr("backend.emby_server.scanner.deadlock_backoff", lambda attempt: None)

    ctx = scanner._ScanContext(snap=SimpleNamespace(), lib_id=99, stats={})
    files = [SimpleNamespace(stored_path="/v/f1.mkv", name="f1.mkv")]

    def _process_one(_scan_file, _pending):
        # 每次都撞死锁
        raise _deadlock_exc()

    with pytest.raises(OperationalError):
        scanner._iter_prepared(ctx, files, None, db, _process_one,
                               "test", SimpleNamespace(total=0), {}, [])

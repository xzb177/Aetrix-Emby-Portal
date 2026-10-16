"""被进程重启打断的扫描：记「已中断」+ 启动时自动续扫。

## 背景

部署/重启打断正在跑的扫描时，启动维护（reset_stale_scan_flags）原来把那一轮
收尾成 failed——健康快照按 failed 计数，于是每次部署都冒出一块假的「扫描失败」
告警牌（24 小时内出现过 4 次），还得管理员手动重新触发扫描才清得掉。

## 改后口径

- 收尾状态是 interrupted（已中断），不是 failed：不进健康快照的失败计数；
- run_startup_maintenance 在复位之后调用 resume_interrupted_scans，
  把 interrupted 的库逐个增量重新入队（trigger=resume），自己接着跑完。
"""
import importlib
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402

from backend.emby_server import models as em  # noqa: E402


def _mt():
    return importlib.import_module("backend.emby_server.maintenance")


@pytest.fixture()
def db():
    from backend import models as base_models
    engine = _ce("sqlite:///:memory:")
    original = _dbmod.SessionLocal
    _dbmod.configure_session_local(_sm(bind=engine))
    base_models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    session = _dbmod.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        _dbmod.configure_session_local(original)
        engine.dispose()


def _lib(db, guid_ch, status, is_scanning=False):
    lib = em.Library(guid=guid_ch * 32, name=f"库{guid_ch}", collection_type="movies",
                     paths="")
    db.add(lib)
    db.flush()
    lib.scan_status = status
    lib.is_scanning = is_scanning
    db.commit()
    return lib


@pytest.fixture()
def queue_spy(monkeypatch):
    """记录 scan_queue.enqueue 调用；is_busy 默认 False，可按库改。"""
    calls = []
    busy = set()

    def fake_enqueue(library, *, trigger="manual", force_full=False):
        calls.append({"library_id": library.id, "trigger": trigger,
                      "force_full": force_full})
        return {"created": True, "task": {}}

    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue", fake_enqueue)
    monkeypatch.setattr("backend.emby_server.scan_queue.is_busy",
                        lambda library_id: library_id in busy)
    return calls, busy


def test_resume_enqueues_interrupted_library(db, queue_spy):
    calls, _busy = queue_spy
    lib = _lib(db, "A", "interrupted")
    n = _mt().resume_interrupted_scans(db)
    assert n == 1
    assert calls == [{"library_id": lib.id, "trigger": "resume", "force_full": False}]


def test_resume_skips_failed_and_success_libraries(db, queue_spy):
    """只有 interrupted 才续扫：真失败的库不许被自动重试掩盖掉"""
    calls, _busy = queue_spy
    _lib(db, "B", "failed")
    _lib(db, "C", "success")
    _lib(db, "D", "partial")
    assert _mt().resume_interrupted_scans(db) == 0
    assert calls == []


def test_resume_skips_busy_library(db, queue_spy):
    calls, busy = queue_spy
    lib = _lib(db, "E", "interrupted")
    busy.add(lib.id)
    assert _mt().resume_interrupted_scans(db) == 0
    assert calls == []


def test_resume_skips_still_scanning_library(db, queue_spy):
    calls, _busy = queue_spy
    _lib(db, "F", "interrupted", is_scanning=True)
    assert _mt().resume_interrupted_scans(db) == 0
    assert calls == []


def test_resume_one_failure_does_not_block_others(db, monkeypatch):
    good = _lib(db, "G", "interrupted")
    bad = _lib(db, "H", "interrupted")
    seen = []

    def flaky_enqueue(library, *, trigger="manual", force_full=False):
        if library.id == bad.id:
            raise RuntimeError("队列挂了")
        seen.append(library.id)
        return {"created": True, "task": {}}

    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue", flaky_enqueue)
    monkeypatch.setattr("backend.emby_server.scan_queue.is_busy", lambda i: False)
    n = _mt().resume_interrupted_scans(db)  # 不许抛
    assert n == 1
    assert seen == [good.id]


def test_reset_then_resume_chain(db, monkeypatch, queue_spy):
    """全链：卡死的 running 库 → 复位成 interrupted → 被自动续扫入队"""
    calls, _busy = queue_spy
    monkeypatch.setattr("backend.emby_server.scanner.is_scan_active", lambda i: False)
    lib = _lib(db, "I", "running", is_scanning=True)
    mt = _mt()
    assert mt.reset_stale_scan_flags(db) == 1
    db.commit()
    assert lib.scan_status == "interrupted"
    assert mt.resume_interrupted_scans(db) == 1
    assert calls[0]["library_id"] == lib.id
    assert calls[0]["trigger"] == "resume"

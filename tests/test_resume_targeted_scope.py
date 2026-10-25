"""定向扫描被打断后，续扫必须还是定向，不许放大成整库遍历。

## 背景（2026-10-22 生产实证）

Drive Changes 的定向扫描只扫几个变化目录（几分钟的活）。它被部署重启
打断后，旧的自动续扫不看原范围、一律整库增量入队——而整库「增量」也要
把整个库的目录清单隔着网盘重走一遍：国产剧 5 万多条的一轮增量被反复
打断、反复整库续，15 次都没走完，还堵死全站扫描队列一整晚。

## 改后口径

- begin_scan 在定向扫描开跑时把范围（limit_prefixes）记进流水 stats 的
  resume_scope 键（流水收尾不动 stats，被打断后范围还在；对外流水结构
  是白名单提取，这个键不会漏到管理端）；
- resume_interrupted_scans 续扫前先读最近一条 interrupted 流水的范围：
  有定向范围 → enqueue_targeted 定向续；没有（整库扫描 / 老流水 /
  stats 损坏）→ 回退整库增量续，既有口径一字不变；
- 定向续跑起来后 begin_scan 会再把范围记进新流水——链条多长都不放大。
"""
import importlib
import os
import tempfile
import types

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402

from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import scanner  # noqa: E402

PREFIXES = ["/mnt/mp/剧集/国产剧/危险关系 (2026)", "/mnt/mp/剧集/国产剧/新剧"]


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


def _lib(db, guid_ch, status="interrupted"):
    lib = em.Library(guid=guid_ch * 32, name=f"库{guid_ch}", collection_type="tvshows",
                     paths="")
    db.add(lib)
    db.flush()
    lib.scan_status = status
    lib.is_scanning = False
    db.commit()
    return lib


def _run(db, lib, status, stats=None, trigger="drive-changes"):
    run = em.ScanRun(library_id=lib.id, status=status, trigger=trigger, stats=stats)
    db.add(run)
    db.commit()
    return run


def _targeted_snap(prefixes=PREFIXES):
    return types.SimpleNamespace(targeted=True, limit_prefixes=tuple(prefixes))


@pytest.fixture()
def queue_spy(monkeypatch):
    """同时记录 enqueue 与 enqueue_targeted；is_busy 默认 False。"""
    calls = {"whole": [], "targeted": []}

    def fake_enqueue(library, *, trigger="manual", force_full=False):
        calls["whole"].append({"library_id": library.id, "trigger": trigger,
                               "force_full": force_full})
        return {"created": True, "task": {}}

    def fake_enqueue_targeted(library, prefixes, trigger="drive-changes"):
        calls["targeted"].append({"library_id": library.id,
                                  "prefixes": list(prefixes), "trigger": trigger})
        return {"created": True, "task": {}}

    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue", fake_enqueue)
    monkeypatch.setattr("backend.emby_server.scan_queue.enqueue_targeted",
                        fake_enqueue_targeted)
    monkeypatch.setattr("backend.emby_server.scan_queue.is_busy",
                        lambda library_id: False)
    return calls


# ── begin_scan 记范围 ──────────────────────────────────────────────

def test_begin_scan_records_targeted_scope(db):
    lib = _lib(db, "A", status="running")
    run_id = scanner.begin_scan(db, lib, "drive-changes", snapshot=_targeted_snap())
    run = db.query(em.ScanRun).filter(em.ScanRun.id == run_id).first()
    stats = scanner.decode_scan_stats(run.stats)
    assert stats["resume_scope"] == {"targeted": True, "limit_prefixes": PREFIXES}


def test_begin_scan_records_nothing_for_whole_scan(db):
    lib = _lib(db, "B", status="running")
    snap = types.SimpleNamespace(targeted=False, limit_prefixes=())
    run_id = scanner.begin_scan(db, lib, "manual", snapshot=snap)
    run = db.query(em.ScanRun).filter(em.ScanRun.id == run_id).first()
    assert "resume_scope" not in scanner.decode_scan_stats(run.stats)


def test_begin_scan_without_snapshot_keeps_old_behavior(db):
    lib = _lib(db, "C", status="running")
    run_id = scanner.begin_scan(db, lib, "manual")
    run = db.query(em.ScanRun).filter(em.ScanRun.id == run_id).first()
    assert run is not None and run.status == "running"
    assert "resume_scope" not in scanner.decode_scan_stats(run.stats)


# ── 范围读取 ───────────────────────────────────────────────────────

def test_interrupted_resume_prefixes_reads_latest_interrupted(db):
    lib = _lib(db, "D")
    scope_old = scanner.encode_scan_stats(
        {"resume_scope": {"targeted": True, "limit_prefixes": ["/old"]}})
    scope_new = scanner.encode_scan_stats(
        {"resume_scope": {"targeted": True, "limit_prefixes": PREFIXES}})
    _run(db, lib, "interrupted", stats=scope_old)
    _run(db, lib, "interrupted", stats=scope_new)
    # 之后还有一条 success（不带范围）也不能把定向范围顶掉
    _run(db, lib, "success", stats=None, trigger="manual")
    assert scanner.interrupted_resume_prefixes(db, lib.id) == PREFIXES


def test_interrupted_resume_prefixes_none_without_scope(db):
    lib = _lib(db, "E")
    _run(db, lib, "interrupted", stats=None)
    assert scanner.interrupted_resume_prefixes(db, lib.id) is None


def test_interrupted_resume_prefixes_none_on_garbage_stats(db):
    lib = _lib(db, "F")
    _run(db, lib, "interrupted", stats="{not json")
    assert scanner.interrupted_resume_prefixes(db, lib.id) is None


# ── 续扫分流 ───────────────────────────────────────────────────────

def test_resume_enqueues_targeted_when_scope_recorded(db, queue_spy):
    lib = _lib(db, "G")
    stats = scanner.encode_scan_stats(
        {"resume_scope": {"targeted": True, "limit_prefixes": PREFIXES}})
    _run(db, lib, "interrupted", stats=stats)
    n = _mt().resume_interrupted_scans(db)
    assert n == 1
    assert queue_spy["whole"] == []
    assert queue_spy["targeted"] == [
        {"library_id": lib.id, "prefixes": PREFIXES, "trigger": "resume"}]


def test_resume_falls_back_to_whole_without_scope(db, queue_spy):
    lib = _lib(db, "H")
    _run(db, lib, "interrupted", stats=None, trigger="manual")
    n = _mt().resume_interrupted_scans(db)
    assert n == 1
    assert queue_spy["targeted"] == []
    assert queue_spy["whole"] == [
        {"library_id": lib.id, "trigger": "resume", "force_full": False}]


def test_resume_chain_stays_targeted(db, queue_spy):
    """定向扫被打断→定向续→再被打断→还定向续：新流水继续带着范围。"""
    lib = _lib(db, "I")
    stats = scanner.encode_scan_stats(
        {"resume_scope": {"targeted": True, "limit_prefixes": PREFIXES}})
    _run(db, lib, "interrupted", stats=stats)
    assert _mt().resume_interrupted_scans(db) == 1
    # 续跑开跑（begin_scan 记范围）→ 又被打断 → 收尾成 interrupted
    run_id = scanner.begin_scan(db, lib, "resume", snapshot=_targeted_snap())
    run = db.query(em.ScanRun).filter(em.ScanRun.id == run_id).first()
    run.status = scanner.SCAN_STATUS_INTERRUPTED
    lib.scan_status = "interrupted"
    lib.is_scanning = False
    db.commit()
    queue_spy["targeted"].clear()
    assert _mt().resume_interrupted_scans(db) == 1
    assert queue_spy["whole"] == []
    assert queue_spy["targeted"][0]["prefixes"] == PREFIXES

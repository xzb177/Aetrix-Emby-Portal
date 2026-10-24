"""扫描跨进程防重 + 检查点专属落点（FMBY 学习批一 #7）。

## 背景

进程内互斥（scanner._acquire_scan）只挡得住本进程：worker 与 EA 是两个
进程，两边同时对同一个库触发扫描时各扫各的（重复遍历网盘、互相顶状态）。
另外定向扫描的续扫范围（resume_scope）此前只借住流水的 stats——那是
「统计」的地盘，收尾会被整轮统计覆写，范围靠白名单夹带续命。

## 改后口径

- 库层部分唯一索引 ``uq_scan_runs_one_running``：同库同时只许一条
  running 流水；begin_scan 插入撞上它 → 抛 ScanInProgress（与进程内
  防线同一个异常，队列层按重复任务拒绝处理）；别的写库失败仍是
  「吞掉只记 warning、本轮照常扫」的老口径，不许误升级；
- 存量库迁移先收尾同库重复的僵尸 running（只留最新一条、其余记
  interrupted），再建索引——顺序反了 CREATE INDEX 直接失败连累启动；
- resume_scope 同时落检查点专属列 state_json：begin_scan 写、收尾永不
  触碰；interrupted_resume_prefixes 先读 state_json、老行回退 stats。
"""
import os
import tempfile
import types
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy import inspect as _inspect  # noqa: E402
from sqlalchemy import text as _text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402

from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import scanner  # noqa: E402

PREFIXES = ["/mnt/mp/剧集/国产剧/危险关系 (2026)", "/mnt/mp/剧集/国产剧/新剧"]


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


@pytest.fixture()
def file_engine(tmp_path):
    """文件型引擎：多 session / 迁移测试共用同一个库文件（模拟存量库）。"""
    engine = _ce(f"sqlite:///{tmp_path}/scan_guard.db")
    original = _dbmod.engine
    _dbmod.engine = engine
    try:
        yield engine
    finally:
        _dbmod.engine = original
        engine.dispose()


def _lib(db, guid_ch):
    lib = em.Library(guid=guid_ch * 32, name=f"库{guid_ch}",
                     collection_type="movies", paths="")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _targeted_snap(prefixes=PREFIXES):
    return types.SimpleNamespace(targeted=True, limit_prefixes=tuple(prefixes))


# ── begin_scan 跨进程防重（同一 session 先钉住基本语义） ─────────────

def test_second_begin_same_library_raises_scan_in_progress(db):
    lib = _lib(db, "A")
    run_id = scanner.begin_scan(db, lib, "manual")
    assert run_id is not None
    with pytest.raises(scanner.ScanInProgress):
        scanner.begin_scan(db, lib, "manual")


def test_other_library_can_begin_while_first_is_running(db):
    lib_a = _lib(db, "B")
    lib_b = _lib(db, "C")
    assert scanner.begin_scan(db, lib_a, "manual") is not None
    assert scanner.begin_scan(db, lib_b, "manual") is not None


def test_begin_allowed_again_after_finish(db):
    lib = _lib(db, "D")
    run_id = scanner.begin_scan(db, lib, "manual")
    scanner._finish_scan_run(db, run_id, scanner.SCAN_STATUS_SUCCESS,
                             {"duration_ms": 3}, None)
    run_id2 = scanner.begin_scan(db, lib, "manual")
    assert run_id2 is not None and run_id2 != run_id


# ── 跨进程语义：两个独立 session（= 两个进程的替身）只许一个成功 ──────

def test_two_sessions_only_one_begin_wins(file_engine):
    from backend import models as base_models
    base_models.Base.metadata.create_all(bind=file_engine)
    em.Base.metadata.create_all(bind=file_engine)
    mk = _sm(bind=file_engine)
    sa, sb = mk(), mk()
    try:
        lib_a = _lib(sa, "E")
        lib_b = sb.query(em.Library).filter(em.Library.id == lib_a.id).first()
        assert scanner.begin_scan(sa, lib_a, "manual") is not None
        with pytest.raises(scanner.ScanInProgress):
            scanner.begin_scan(sb, lib_b, "manual")
    finally:
        sa.close()
        sb.close()


# ── 冲突判别：只认这条索引，别类 IntegrityError 不许误升级 ───────────

def _fake_integrity(orig_msg):
    # 语句文本故意中性：判别只许看驱动原始报错，不许被语句里的表名带偏
    return IntegrityError("INSERT INTO t ...", {}, Exception(orig_msg))


def test_conflict_discrimination_by_index_name():
    # PG：约束名即索引名
    assert scanner._is_single_running_violation(_fake_integrity(
        'duplicate key value violates unique constraint '
        '"uq_scan_runs_one_running"'))
    # SQLite（实测 3.45）：只报「表.列」不报索引名；本表唯一约束只有这条
    assert scanner._is_single_running_violation(_fake_integrity(
        "UNIQUE constraint failed: emby_scan_runs.library_id"))
    # 别的表的唯一冲突 / 本表的非唯一错误：不许误升级成 ScanInProgress
    assert not scanner._is_single_running_violation(_fake_integrity(
        "UNIQUE constraint failed: emby_items.guid"))
    assert not scanner._is_single_running_violation(_fake_integrity(
        "NOT NULL constraint failed: emby_scan_runs.library_id"))


# ── 存量迁移：先收尾僵尸 running、再建索引（顺序与幂等都钉死） ──────

_LEGACY_SCAN_RUNS_DDL = """
CREATE TABLE emby_scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL,
    trigger_kind VARCHAR(20),
    started_at DATETIME,
    finished_at DATETIME,
    duration_ms INTEGER,
    stats TEXT,
    state_json TEXT,
    error VARCHAR(500)
)
"""


def _legacy_run(session, library_id, status, minutes_ago, error=None):
    run = em.ScanRun(library_id=library_id, status=status, trigger="manual",
                     started_at=datetime.now() - timedelta(minutes=minutes_ago),
                     error=error)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def test_migration_closes_zombies_then_builds_index(file_engine):
    with file_engine.begin() as conn:
        conn.execute(_text(_LEGACY_SCAN_RUNS_DDL))
    mk = _sm(bind=file_engine)
    s = mk()
    try:
        # 库 1：三条僵尸 running（中间一条带原有错误，收尾不许覆盖它）
        _legacy_run(s, 1, "running", minutes_ago=30)
        _legacy_run(s, 1, "running", minutes_ago=20, error="原有错误")
        newest = _legacy_run(s, 1, "running", minutes_ago=10)
        # 库 2：一条正常 running，不许动
        solo = _legacy_run(s, 2, "running", minutes_ago=5)
        # 库 3：已收尾的历史行，不许动
        done = _legacy_run(s, 3, "success", minutes_ago=60)

        _dbmod._migrate_scan_run_single_running({"emby_scan_runs"})

        rows = {r.id: r for r in s.query(em.ScanRun).all()}
        assert rows[newest.id].status == "running"          # 最新一条留下
        closed = [r for r in rows.values()
                  if r.library_id == 1 and r.id != newest.id]
        assert len(closed) == 2
        for r in closed:
            assert r.status == "interrupted"                # 记中断不记失败
            assert r.finished_at is not None
        by_id = {r.id: r for r in closed}
        assert "原有错误" in [r.error for r in by_id.values()]
        assert rows[solo.id].status == "running"
        assert rows[done.id].status == "success"

        names = {ix["name"] for ix in _inspect(file_engine).get_indexes("emby_scan_runs")}
        assert "uq_scan_runs_one_running" in names

        # 索引已生效：再插一条同库 running 必须被库层顶回来
        with pytest.raises(IntegrityError):
            _legacy_run(s, 1, "running", minutes_ago=0)
        s.rollback()

        # 幂等：再跑一次迁移，无事发生
        _dbmod._migrate_scan_run_single_running({"emby_scan_runs"})
        s.expire_all()
        running = s.query(em.ScanRun).filter(
            em.ScanRun.status == "running").count()
        assert running == 2                                  # 库 1、库 2 各一条
    finally:
        s.close()


def test_migration_noop_without_table(file_engine):
    _dbmod._migrate_scan_run_single_running(set())           # 不许抛


# ── 检查点专属落点 state_json ────────────────────────────────────────

def test_begin_scan_writes_state_json_checkpoint(db):
    lib = _lib(db, "F")
    run_id = scanner.begin_scan(db, lib, "drive-changes",
                                snapshot=_targeted_snap())
    run = db.query(em.ScanRun).filter(em.ScanRun.id == run_id).first()
    scope = scanner.decode_scan_stats(run.state_json).get("resume_scope")
    assert scope == {"targeted": True, "limit_prefixes": PREFIXES}
    # 老落点 stats 同步还在（两代读口径并行期）
    assert scanner.decode_scan_stats(run.stats).get("resume_scope") == scope


def test_resume_prefixes_survive_stats_wipe_via_state_json(db):
    """被打断后哪怕 stats 被清掉，state_json 里的范围也必须读得出来。"""
    lib = _lib(db, "G")
    run_id = scanner.begin_scan(db, lib, "drive-changes",
                                snapshot=_targeted_snap())
    run = db.query(em.ScanRun).filter(em.ScanRun.id == run_id).first()
    run.status = scanner.SCAN_STATUS_INTERRUPTED
    run.stats = None
    db.commit()
    assert scanner.interrupted_resume_prefixes(db, lib.id) == PREFIXES


def test_resume_prefixes_state_json_wins_over_stats(db):
    lib = _lib(db, "H")
    run = em.ScanRun(
        library_id=lib.id, status="interrupted", trigger="manual",
        stats=scanner.encode_scan_stats(
            {"resume_scope": {"targeted": True, "limit_prefixes": ["/old"]}}),
        state_json=scanner.encode_scan_stats(
            {"resume_scope": {"targeted": True, "limit_prefixes": PREFIXES}}),
    )
    db.add(run)
    db.commit()
    assert scanner.interrupted_resume_prefixes(db, lib.id) == PREFIXES


def test_resume_prefixes_fall_back_to_stats_for_legacy_row(db):
    """老流水没有 state_json（NULL）：回退 stats，两代兼容。"""
    lib = _lib(db, "I")
    run = em.ScanRun(
        library_id=lib.id, status="interrupted", trigger="manual",
        stats=scanner.encode_scan_stats(
            {"resume_scope": {"targeted": True, "limit_prefixes": PREFIXES}}),
        state_json=None,
    )
    db.add(run)
    db.commit()
    assert scanner.interrupted_resume_prefixes(db, lib.id) == PREFIXES


def test_resume_prefixes_fall_back_when_state_json_garbage(db):
    lib = _lib(db, "J")
    run = em.ScanRun(
        library_id=lib.id, status="interrupted", trigger="manual",
        stats=scanner.encode_scan_stats(
            {"resume_scope": {"targeted": True, "limit_prefixes": PREFIXES}}),
        state_json="{not json",
    )
    db.add(run)
    db.commit()
    assert scanner.interrupted_resume_prefixes(db, lib.id) == PREFIXES

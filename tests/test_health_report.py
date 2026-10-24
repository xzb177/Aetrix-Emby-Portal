"""健康检查必须反映真实故障，不能永远 healthy。

生产事故：刮削链整条挂掉（3117 个 series 搜不到、worker 刷几千条探测失败），
而「服务健康」页一片绿——``/api/health`` 的 ``status`` 是**硬编码常量**
``"healthy"``。出问题时没人能从页面看到，只能去翻日志。

口径见 backend/health_report.py。三档：ok / warn / down。
"""
import os
import tempfile
from types import SimpleNamespace

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

from backend import health_report  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


@pytest.fixture()
def db():
    """自建库 + 用完还原，不依赖测试执行顺序"""
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


def _items(db, n, item_type="movie", probe="done", enrich="done", lib=None):
    lib = lib or em.Library(guid="L" * 32, name="库", collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    for i in range(n):
        db.add(em.MediaItem(guid=f"{i:032d}", library_id=lib.id, item_type=item_type,
                            name=f"条目{i}", probe_status=probe, enrich_status=enrich))
    db.flush()


def test_healthy_when_everything_fine(db, monkeypatch):
    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "library_scan_summary", lambda db: {"failed": 0})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    res = health_report.collect(db)
    assert res["level"] == "ok", res
    assert res["status"] == "healthy"
    assert res["issues"] == [], res


def test_warns_on_high_probe_failure_rate(db, monkeypatch):
    """探测大面积失败（今天真实发生过的：ffprobe 刷屏）必须 warn"""
    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 10 if s == "done" else 40)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "library_scan_summary", lambda db: {"failed": 0})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    res = health_report.collect(db)
    assert res["level"] == "warn", res
    assert any(i["key"] == "probe" for i in res["issues"]), res


def test_down_on_enqueue_backlog(db, monkeypatch):
    """补全队列堆积到处理不过来 = 核心不可用"""
    counts = {"done": 100, "failed": 0, "pending": 900, "enriching": 100}
    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: counts.get(s, 0))
    monkeypatch.setattr(health_report, "library_scan_summary", lambda db: {"failed": 0})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    res = health_report.collect(db)
    assert res["level"] == "down", res
    assert res["status"] == "unhealthy"
    assert any(i["key"] == "enrich_backlog" for i in res["issues"]), res


def test_warns_on_library_scan_failure(db, monkeypatch):
    """某个库扫描失败要在健康里露出来（今天「动漫」库扫描超时就是这么发现的）"""
    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "library_scan_summary", lambda db: {
        "failed": 1, "last_failed": [{"name": "动漫", "error": "timed out"}]})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    res = health_report.collect(db)
    assert res["level"] == "warn", res
    assert any("动漫" in i["message"] for i in res["issues"]), res


def test_small_sample_does_not_warn(db, monkeypatch):
    """刚开机样本少时不该误报"""
    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 3 if s == "failed" else 1)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: 1 if s == "done" else 0)
    monkeypatch.setattr(health_report, "library_scan_summary", lambda db: {"failed": 0})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    res = health_report.collect(db)
    assert res["level"] == "ok", res


def test_collect_never_raises_on_broken_db():
    """健康检查本身不能抛 —— 它是最该一直可用的一环"""
    class _Broken:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def execute(self, *a, **k):
            raise RuntimeError("db down")

    res = health_report.collect(_Broken())
    assert res["level"] in ("warn", "down"), res
    assert res["issues"], res



def _big_backlog(monkeypatch):
    """1000 条积压的通用 monkeypatch"""
    counts = {"done": 100, "failed": 0, "pending": 900, "enriching": 100}
    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: counts.get(s, 0))
    monkeypatch.setattr(health_report, "library_scan_summary", lambda db: {"failed": 0})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})


def test_no_false_down_when_counter_empty(db, monkeypatch):
    """计数器为空（刚启动/分离部署 API 进程）时只报 warn，不误报 down。

    回归：旧实现 backlog>=500 且计数器为 0 时直接报 down，而分离部署下
    API 进程的计数器恒为 0，导致恒误报。现在 total==0 只报 warn（诚实）。
    """
    from backend.emby_server import scan_progress as progress
    _big_backlog(monkeypatch)
    progress.reset()
    try:
        res = health_report.collect(db)
    finally:
        progress.reset()
    # 积压本身是 down，但「停滞」不能是 down
    assert not any(i["key"] == "enrich_stalled" for i in res["issues"]), res
    assert any(i["key"] == "enrich_no_progress" and i["level"] == "warn"
               for i in res["issues"]), res


def test_stalled_down_when_had_activity_but_stalled(db, monkeypatch):
    """有过动作（total>0）但窗口内无成功 = 真卡住，报 down enrich_stalled"""
    import time
    from backend.emby_server import scan_progress as progress
    _big_backlog(monkeypatch)
    progress.reset()
    try:
        # 注入 10 分钟前的完成记录：total>0 但 done_per_min==0
        old = time.monotonic() - 700
        with progress._LOCK:
            progress._COMPLETIONS.append((old, "done"))
            progress._COMPLETED_TOTAL["done"] += 1
        res = health_report.collect(db)
    finally:
        progress.reset()
    assert res["level"] == "down", res
    assert any(i["key"] == "enrich_stalled" for i in res["issues"]), res


def test_enrich_count_excludes_merged_copies(db):
    """合并副本不计入补全统计：副本按设计不参与补全（母条目补完即可），
    算进来会让积压虚高几万条、健康灯长红（2026-10-16 生产实测：
    真实 pending 2 条、副本 pending 72167 条）。"""
    lib = em.Library(guid="M" * 32, name="库", collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    master = em.MediaItem(guid=f"{1:032d}", library_id=lib.id, item_type="movie",
                          name="母条目", enrich_status="done")
    db.add(master)
    db.flush()
    db.add(em.MediaItem(guid=f"{2:032d}", library_id=lib.id, item_type="movie",
                        name="副本", enrich_status="pending",
                        merged_into_id=master.id))
    db.add(em.MediaItem(guid=f"{3:032d}", library_id=lib.id, item_type="movie",
                        name="真实待补", enrich_status="pending"))
    db.flush()
    assert health_report._enrich_count(db, "pending") == 1
    assert health_report._enrich_count(db, "done") == 1


def _lib_with_scan_status(db, name, status, error=None, guid_ch="Q"):
    """scan_status 指定的库（直接写真实行，不走猴子补丁）

    必须 commit：health_report.collect() 里 probe_worker.status_snapshot
    做完纯 SELECT 后会 db.rollback()（清 idle 事务，生产习惯），
    只 flush 不 commit 的行会被撤销。
    """
    lib = em.Library(guid=guid_ch * 32, name=name, collection_type="movies", paths="")
    lib.scan_status = status
    lib.is_scanning = False
    if error:
        lib.scan_error = error
    db.add(lib)
    db.commit()
    return lib


def test_scan_summary_counts_interrupted_not_failed(db):
    """「已中断」不是失败：记 interrupted 计数、不进 failed、不进 last_failed
    （GLM-5.3 复核 #938 指出 health_report 对 interrupted 零直接断言）"""
    _lib_with_scan_status(db, "中断库", "interrupted")
    s = health_report.library_scan_summary(db)
    assert s["interrupted"] == 1, s
    assert s["failed"] == 0, s
    assert s["last_failed"] == [], s


def test_scan_summary_failed_still_recorded(db):
    """对照：真失败的库仍正常记 failed 并进 last_failed"""
    _lib_with_scan_status(db, "失败库", "failed", error="boom", guid_ch="W")
    s = health_report.library_scan_summary(db)
    assert s["failed"] == 1, s
    assert s["interrupted"] == 0, s
    assert any(x["name"] == "失败库" for x in s["last_failed"]), s


@pytest.fixture()
def clean_cache():
    """collect_cached 进程内缓存的测试隔离：用完还原"""
    health_report._reset_health_cache()
    try:
        yield
    finally:
        health_report._reset_health_cache()


def test_collect_cached_hits_cache_within_ttl(db, monkeypatch, clean_cache):
    """TTL 窗口内第二次调用直接命中缓存，不再重跑聚合"""
    monkeypatch.setenv("HEALTH_REPORT_TTL_SECONDS", "600")
    r1 = health_report.collect_cached(db)
    r2 = health_report.collect_cached(db)
    assert r1 is r2


def test_collect_cached_disabled_when_ttl_zero(db, monkeypatch, clean_cache):
    """TTL<=0 时关闭缓存：每次调用都重新聚合"""
    monkeypatch.setenv("HEALTH_REPORT_TTL_SECONDS", "0")
    r1 = health_report.collect_cached(db)
    r2 = health_report.collect_cached(db)
    assert r1 is not r2


def test_collect_cached_refreshes_after_ttl(db, monkeypatch, clean_cache):
    """TTL 过期后重新聚合"""
    monkeypatch.setenv("HEALTH_REPORT_TTL_SECONDS", "600")
    r1 = health_report.collect_cached(db)
    health_report._HEALTH_CACHE["at"] -= 601
    r2 = health_report.collect_cached(db)
    assert r2 is not r1


def test_collect_cached_serves_stale_on_refresh_error(db, monkeypatch, clean_cache):
    """刷新失败时返回旧快照，健康检查不 500"""
    monkeypatch.setenv("HEALTH_REPORT_TTL_SECONDS", "600")
    primed = health_report.collect_cached(db)
    health_report._HEALTH_CACHE["at"] -= 601

    def _boom(db):
        raise RuntimeError("db gone")

    monkeypatch.setattr(health_report, "collect", _boom)
    assert health_report.collect_cached(db) is primed


def test_collect_cached_raises_without_stale_on_error(db, monkeypatch, clean_cache):
    """无旧快照且聚合失败时，异常照常抛出（由调用方退化处理）"""
    monkeypatch.setenv("HEALTH_REPORT_TTL_SECONDS", "600")

    def _boom(db):
        raise RuntimeError("db gone")

    monkeypatch.setattr(health_report, "collect", _boom)
    with pytest.raises(RuntimeError):
        health_report.collect_cached(db)


def test_collect_no_scan_issue_for_interrupted(db, monkeypatch):
    """已中断的库在健康检查里不出告警；真失败的库出 warn"""
    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: 20 if s == "done" else 0)
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    _lib_with_scan_status(db, "中断库", "interrupted")
    res = health_report.collect(db)
    assert not any(i["key"] == "scan" for i in res["issues"]), res

    _lib_with_scan_status(db, "失败库", "failed", error="boom", guid_ch="W")
    res = health_report.collect(db)
    assert any(i["key"] == "scan" and i["level"] == "warn" for i in res["issues"]), res
    assert any("失败库" in i["message"] for i in res["issues"] if i["key"] == "scan"), res

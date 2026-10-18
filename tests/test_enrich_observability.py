"""补全可观测性（v2.42.9）：阶段计数器 / 完成速率 / 插桩确实计上了。

为什么单独一个测试文件：这批评分数据是后面几批优化的**验收依据** ——
「单条平均耗时」「done/分钟」「远程列举与 NFO 读取次数」都要靠它，
计数口径错了就等于白优化（而且会在几周后才发现）。

- 计数器是进程内状态，每个测试前 ``scan_progress.reset()``；
- 不碰真实网络：TMDB 用假 session、NFO 用假 provider。
"""
import importlib.util
import os
import pathlib
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest  # noqa: E402

from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import enrich_worker, scanner  # noqa: E402
from backend.emby_server import scan_progress as progress  # noqa: E402
from backend.emby_server import tmdb as tmdb_lib  # noqa: E402

init_db()

_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_counters():
    progress.reset()
    yield
    progress.reset()


# ---------- 计数器本体 ----------

def test_note_stage_counts_times_and_averages_only_timed_calls():
    """次数与耗时分开记：只计次数的那几次（缓存命中）不该把均值拉低"""
    progress.note_stage("remote_list", 100.0)
    progress.note_stage("remote_list", 300.0)
    progress.note_stage("remote_list")          # 只计次数（如缓存命中）

    row = progress.stage_stats()["remote_list"]
    assert row["count"] == 3
    assert row["ms"] == 400
    assert row["avg_ms"] == 200                  # 400ms / 2 次计过时的
    assert row["label"] == "远程目录列举"
    assert row["last_at"]                         # 有最近一次时间戳


def test_stage_timer_records_failures_too():
    """失败也占用了时间：抛异常的那一段照样要计一次"""
    with pytest.raises(RuntimeError):
        with progress.stage_timer("image_dl"):
            raise RuntimeError("下载炸了")

    row = progress.stage_stats()["image_dl"]
    assert row["count"] == 1
    assert row["ms"] >= 0


def test_throughput_per_minute_and_idle():
    """完成速率：窗口内计数 / 窗口分钟数；未知 kind 与 skip 不进口径"""
    for _ in range(6):
        progress.note_completed("done")
    progress.note_completed("retry")
    progress.note_completed("failed")
    progress.note_completed("skip")              # 条目已消失：既非成功也非失败

    tp = progress.throughput(window_sec=120)     # 2 分钟窗口
    assert tp["window_sec"] == 120
    assert tp["done_per_min"] == 3.0             # 6 / 2
    assert tp["retry_per_min"] == 0.5
    assert tp["failed_per_min"] == 0.5
    assert tp["samples"] == 8                    # 含 skip（窗口内样本）
    assert tp["done_total"] == 6
    assert tp["last_done_at"]
    assert tp["idle_sec"] is not None and tp["idle_sec"] >= 0


def test_idle_sec_none_before_any_success():
    """一次都没成功过时不该报虚假的空闲秒数（否则会被读成「卡住了」）"""
    progress.note_completed("retry")
    tp = progress.throughput()
    assert tp["last_done_at"] is None
    assert tp["idle_sec"] is None
    assert tp["done_per_min"] == 0.0


def test_reset_clears_stages_and_rates():
    progress.note_stage("tmdb_req", 10.0)
    progress.note_completed("done")
    progress.reset()
    assert progress.stage_stats() == {}
    tp = progress.throughput()
    assert tp["done_total"] == 0 and tp["samples"] == 0


# ---------- 插桩：NFO 读取与 TMDB 请求 ----------

class _FakeProvider:
    """只实现 read_text：NFO 读取路径够用"""

    def __init__(self):
        self.calls = 0

    def read_text(self, rel):
        self.calls += 1
        return "<movie><title>测试电影</title><tmdbid>603</tmdbid></movie>"


def test_nfo_read_counted_and_cache_hit_counted_separately():
    """真读一次计 nfo_read；同一条目再取命中 ctx 缓存 → 只计 nfo_hit"""
    snap = scanner.LibrarySnapshot(
        library_id=1, name="", collection_type="movies", paths=(), scrape_policy="smart")
    ctx = scanner._ScanContext(snap=snap, lib_id=1, stats={})
    provider = _FakeProvider()
    scan_file = SimpleNamespace(mount_id=1, provider=provider)

    data = scanner._read_nfo_cached(ctx, scan_file, mount_rel="/电影/x.nfo")
    assert data and data.get("tmdb_id") == "603"
    assert provider.calls == 1

    stats = progress.stage_stats()
    assert stats["nfo_read"]["count"] == 1
    assert "nfo_hit" not in stats                 # 第一次是未命中，不该有命中计数

    scanner._read_nfo_cached(ctx, scan_file, mount_rel="/电影/x.nfo")
    stats = progress.stage_stats()
    assert provider.calls == 1                    # 没有第二次真实读取
    assert stats["nfo_read"]["count"] == 1
    assert stats["nfo_hit"]["count"] == 1         # 缓存复用率 = hit / (hit + read)


def test_tmdb_each_http_request_counted(monkeypatch):
    """计的是**请求**不是条目：一次 _get = 一次计数（与限流器按条目计数区分开）"""
    monkeypatch.setattr(tmdb_lib.TmdbClient, "_ensure_session", lambda self: None)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"results": []}

    class _Sess:
        def __init__(self):
            self.urls = []

        def get(self, url, params=None):
            self.urls.append(url)
            return _Resp()

    client = tmdb_lib.TmdbClient.__new__(tmdb_lib.TmdbClient)
    client.api_keys = ["k"]
    client.api_key = "k"
    client.session = _Sess()
    # v2.42.9：_get 先过请求级令牌桶，所以裸客户端要按 __init__ 的口径补上这两个字段
    client._limiter = tmdb_lib._RequestLimiter(rate=1_000_000.0, min_rate=1.0)
    client._stats = {"short_circuit": 0, "retry": 0, "net_fail": 0}

    assert client._get("/search/tv", {"query": "x"}) == {"results": []}
    assert client._get("/search/tv", {"query": "y"}) == {"results": []}
    assert len(client.session.urls) == 2
    assert progress.stage_stats()["tmdb_req"]["count"] == 2


# ---------- 进度接口 ----------

def test_enrich_progress_exposes_stages_and_throughput():
    """管理端进度接口：状态计数之外，必须能读到阶段分解与速率"""
    progress.note_stage("remote_list", 250.0)
    progress.note_completed("done")
    payload = enrich_worker.get_progress()
    assert payload["stages"]["remote_list"]["count"] == 1
    assert payload["stages"]["remote_list"]["avg_ms"] == 250
    assert payload["throughput"]["done_total"] == 1
    assert "mount_io" in payload
    # 老字段一个都不能少（管理端与脚本都在用）
    assert {"pending", "enriching", "done", "failed", "retrying"} <= set(payload["enrich"])
    assert payload["workers"] == enrich_worker.ENRICH_WORKERS


def test_worker_result_mapping_done_retry_failed():
    """三种结果都必须被计上：done（刮干净）/ retry（留待下次）/ failed（超限）"""
    progress.note_completed("done")
    progress.note_completed("retry")
    progress.note_completed("failed")
    tp = progress.throughput()
    assert (tp["done_total"], tp["retry_total"], tp["failed_total"]) == (1, 1, 1)


# ---------- 基线报表脚本（SQL 在真 schema 上跑一遍）----------

def _load_report_module():
    """加载 scripts/enrich_backlog_report.py（不是包，按路径载入）"""
    spec = importlib.util.spec_from_file_location(
        "enrich_backlog_report", _ROOT / "scripts" / "enrich_backlog_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backlog_report_sections_run_on_real_schema(capsys):
    """报表脚本的四段 SQL 在真实建表后的库上能跑通（SQL 写错只会在生产暴露）"""
    mod = _load_report_module()

    db = SessionLocal()
    try:
        mod.section_mix(db)
        mod.section_hopeless(db)
        mod.section_status(db, 10)
        mod.section_libraries(db)
    finally:
        db.close()

    out = capsys.readouterr().out
    assert "积压构成" in out
    assert "无望队列" in out
    assert "状态分布" in out
    assert "各媒体库" in out


# ---------- 健康检查：把「堆积」与「卡住」拆开 ----------

def _health_env(monkeypatch, counts):
    """按 tests/test_health_report.py 的手法把 collect() 与环境隔离"""
    from backend import health_report

    monkeypatch.setattr(health_report, "_count", lambda db, t, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "_enrich_count", lambda db, s: counts.get(s, 0))
    monkeypatch.setattr(health_report, "library_scan_summary", lambda db: {"failed": 0})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    return health_report


def test_health_down_when_backlog_stalled(monkeypatch):
    """堆积 + 近窗口只有重试没有成功 = 卡住（与「跑得慢」不是一回事）"""
    health_report = _health_env(monkeypatch, {"done": 100, "failed": 10, "pending": 900,
                                             "enriching": 100})
    progress.note_completed("retry")        # 有动作，但没成功
    db = SessionLocal()
    try:
        res = health_report.collect(db)
    finally:
        db.close()
    keys = {i["key"] for i in res["issues"]}
    assert "enrich_stalled" in keys, res["issues"]
    assert "enrich_backlog" in keys             # 老的堆积告警照旧
    assert res["metrics"]["enrich_done_per_min"] == 0.0


def test_health_warns_not_down_on_fresh_process(monkeypatch):
    """刚重启（本进程还没完成过任何条目）只能 warn：不能把「刚起来」当故障"""
    health_report = _health_env(monkeypatch, {"done": 100, "failed": 0, "pending": 900,
                                             "enriching": 100})
    db = SessionLocal()
    try:
        res = health_report.collect(db)
    finally:
        db.close()
    keys = {i["key"] for i in res["issues"]}
    assert "enrich_no_progress" in keys, res["issues"]
    assert "enrich_stalled" not in keys


def test_health_quiet_when_backlog_flowing(monkeypatch):
    """堆积很大但确实在往下消时，不该报「卡住」"""
    health_report = _health_env(monkeypatch, {"done": 100, "failed": 0, "pending": 900,
                                             "enriching": 100})
    for _ in range(3):
        progress.note_completed("done")
    db = SessionLocal()
    try:
        res = health_report.collect(db)
    finally:
        db.close()
    keys = {i["key"] for i in res["issues"]}
    assert "enrich_stalled" not in keys
    assert "enrich_no_progress" not in keys


def test_report_pct_handles_empty_database():
    """空库不能除零：报表最常见的场景恰恰是「刚清完」或「新部署」"""
    mod = _load_report_module()
    assert "—" in mod._pct(0, 0)
    assert mod._pct(1, 4).strip() == "25.0%"

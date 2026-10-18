# -*- coding: utf-8 -*-
"""错误码大白话映射测试。

覆盖：
1. 词典完整性：100 条、id 唯一、分类合法、正则可编译、文案齐全
2. translate()：真实错误原文（probe/enrich/scan/Drive/DB/网络）命中正确条目
3. translate()：None/空/无命中/怪异输入永不抛异常
4. health_report 集成：issue 与扫描失败记录都挂上 hint
5. probe_worker 集成：top_errors 每条都挂上 hint
"""
import os
import re
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

from backend import error_lexicon as lex  # noqa: E402
from backend import health_report  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


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


# ---------------------------------------------------------------- 1. 完整性
def test_lexicon_has_100_unique_entries():
    assert lex.LEXICON_COUNT == 100
    assert len(lex.LEXICON) == 100
    ids = [e["id"] for e in lex.LEXICON]
    assert len(set(ids)) == 100, "id 有重复"


def test_lexicon_categories_valid():
    for e in lex.LEXICON:
        assert e["category"] in lex.CATEGORIES, e["id"]
        assert e["title"] and e["what"], e["id"]
        assert isinstance(e["steps"], list) and len(e["steps"]) >= 1, e["id"]
        # 正则必须可编译（模块加载时已编译，这里复查 pattern 本体合法）
        re.compile(e["pattern"], re.IGNORECASE)


def test_lexicon_covers_all_six_categories():
    cats = {e["category"] for e in lex.LEXICON}
    assert cats == set(lex.CATEGORIES), cats


# ---------------------------------------------------------------- 2. 真实错误命中
@pytest.mark.parametrize("text,expected", [
    # probe：生产真实出现过的 probe_last_error
    ("[quota] 远端配额耗尽（约24h后重试）", "probe_quota"),
    ("[ratelimit] 远端限流（HTTP 429），约24h后重试", "probe_ratelimit"),
    ("[server] 远端服务异常（HTTP 502），约24h后重试", "probe_server"),
    ("取不到完整时长（已达3次上限）", "probe_no_duration"),
    ("探测未返回有效媒体信息（已达3次上限）", "probe_no_media_info"),
    ("无法解析探测地址", "probe_resolve_fail"),
    ("解析探测地址：远端配额耗尽", "probe_resolve_quota"),
    ("ffprobe 异常: Invalid data found", "probe_ffprobe_error"),
    ("远端返回 HTTP 404，地址无效", "probe_404"),
    ("远端认证失败（HTTP 401）", "probe_auth"),
    # enrich
    ("补全 TMDB 失败 /path/to/file: 429", "enrich_tmdb_fail"),
    ("补全重试超限转 failed id=123", "enrich_retry_exhausted"),
    ("豆瓣演职员表失败 xxx", "enrich_douban_cast"),
    # scan
    ("进程重启，本轮扫描未完成", "scan_restart"),
    # drive / SA
    ("quotaExceeded: download quota exceeded", "drive_quota_exceeded"),
    ("invalid_grant: Token has been expired", "drive_401"),
    ("userRateLimitExceeded", "drive_ratelimit"),
    # system_db
    ("database is locked", "db_locked"),
    ("no space left on device", "db_disk_full"),
    ("Redis 不可用：Connection refused", "redis_down"),
    # network
    ("Temporary failure in name resolution", "net_dns"),
    ("502 Bad Gateway", "net_502"),
    ("no_healthy_account", "net_503"),
    # 健康消息形态（中文）
    ("媒体探测失败率 35%（120/340）", "probe_ratio_high"),
    ("补全队列堆积 5027 条，已经处理不过来", "enrich_backlog"),
    ("磁盘余量偏低（12% 剩余）", "disk_low"),
    ("后台 worker 线程已死亡：enrich", "worker_dead"),
    ("2 个媒体库最近一次扫描失败：国产剧", "scan_failed_generic"),
    ("近 1 小时 enrich done→pending 翻转 5723 条", "enrich_flip"),
    ("120 条待探测，但探测 worker 没有在运行", "probe_worker_stopped"),
])
def test_translate_hits_expected_entry(text, expected):
    hint = lex.translate(text)
    assert hint is not None, text
    assert hint["id"] == expected, f"{text!r} -> {hint['id']}"
    assert hint["title"] and hint["what"] and hint["steps"]


# ---------------------------------------------------------------- 3. 边界
@pytest.mark.parametrize("text", [None, "", "   ", "xyz nothing matches here 12345"])
def test_translate_returns_none_gracefully(text):
    assert lex.translate(text) is None


def test_translate_never_raises_on_weird_input():
    assert lex.translate(12345) is None  # 纯数字无命中
    assert lex.translate("HTTP 429") is not None  # 数字+关键字命中
    # 超长文本不卡死
    hint = lex.translate("x" * 100000 + "quotaExceeded")
    assert hint["id"] == "drive_quota_exceeded"


# ---------------------------------------------------------------- 4. health_report 集成
def test_health_issues_carry_hint(db, monkeypatch):
    monkeypatch.setattr(health_report, "_count",
                        lambda db, t, s: 10 if s == "done" else 40)
    monkeypatch.setattr(health_report, "_enrich_count",
                        lambda db, s: 20 if s == "done" else 0)
    monkeypatch.setattr(health_report, "library_scan_summary",
                        lambda db: {"failed": 0})
    monkeypatch.setattr("backend.emby_server.maintenance.resource_report",
                        lambda: {"disk_free_percent": 80.0})
    res = health_report.collect(db)
    assert res["level"] == "warn"
    for issue in res["issues"]:
        assert "hint" in issue, issue
    probe_issue = next(i for i in res["issues"] if i["key"] == "probe")
    assert probe_issue["hint"] is not None
    assert probe_issue["hint"]["id"] == "probe_ratio_high"


def test_scan_last_failed_carries_hint(db):
    lib = em.Library(guid="L" * 32, name="国产剧", collection_type="tv",
                     paths="", scan_status="failed",
                     scan_error="进程重启，本轮扫描未完成")
    db.add(lib)
    db.flush()
    out = health_report.library_scan_summary(db)
    assert out["failed"] == 1
    row = out["last_failed"][0]
    assert row["hint"] is not None
    assert row["hint"]["id"] == "scan_restart"
    assert row["hint"]["title"]


# ---------------------------------------------------------------- 5. probe_worker 集成
def test_probe_top_errors_carry_hint(db, monkeypatch):
    from backend.emby_server import probe_worker

    lib = em.Library(guid="M" * 32, name="库", collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    db.add(em.MediaItem(guid="1" * 32, library_id=lib.id, item_type="movie",
                        name="片1", probe_status="failed",
                        probe_last_error="[quota] 远端配额耗尽（约24h后重试）"))
    db.add(em.MediaItem(guid="2" * 32, library_id=lib.id, item_type="movie",
                        name="片2", probe_status="failed",
                        probe_last_error="取不到完整时长（已达3次上限）"))
    db.flush()

    snap = probe_worker.status_snapshot(db)
    assert len(snap["top_errors"]) == 2
    by_err = {t["error"]: t for t in snap["top_errors"]}
    assert by_err["[quota] 远端配额耗尽（约24h后重试）"]["hint"]["id"] == "probe_quota"
    assert by_err["取不到完整时长（已达3次上限）"]["hint"]["id"] == "probe_no_duration"


def test_probe_snapshot_when_dispatcher_alive(db, monkeypatch):
    """dispatcher 线程存活时 pending 变量必须已定义（缩进回归）。"""
    from backend.emby_server import probe_worker

    class _FakeThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(probe_worker, "_dispatcher_thread", _FakeThread())
    monkeypatch.setattr(probe_worker, "runtime_status",
                        lambda: {"rate_per_min": 60})
    snap = probe_worker.status_snapshot(db)
    assert snap["runtime"]["source"] == "local"
    assert "pending_ready" in snap
    assert isinstance(snap["top_errors"], list)

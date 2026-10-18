# -*- coding: utf-8 -*-
"""一键体检（只读诊断）测试。

覆盖：
1. 全健康 → 9 条规则全 info
2. 探测队列积压 → warn；有活没人干 → critical
3. 补全停滞 → warn；夜间模式 → info（不误报）
4. 磁盘低/极低 → warn/critical
5. 扫描失败 → warn；SA 为 0 → critical；API 错误率高 → warn
6. 数据库 down → critical
7. 探针无数据 → info（如实说"暂无数据"，不误报）
8. 阈值可配：环境变量覆盖默认值
9. 单个规则异常 → 该规则降级为"暂不可用"，不影响其他
10. run_diagnosis 返回结构：ran_at/summary/findings/note
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend import diagnosis
from backend.database import SessionLocal, init_db

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        try:
            s.rollback()
        except Exception:  # noqa: BLE001
            pass
        s.close()


def _probe(key, status="ok", message="", metrics=None):
    return {"key": key, "status": status, "message": message,
            "metrics": metrics or {}}


def healthy_subs():
    return {
        "database": _probe("database", "ok", "ok"),
        "disk": _probe("disk", "ok", "ok", {"disk_free_percent": 63.0}),
        "probe": _probe("probe", "ok", "ok",
                        {"pending_ready": 12, "retrying": 0, "running": True,
                         "paused": False, "rate_per_min": 88.0,
                         "eta_hours": 0.1, "open_breakers": []}),
        "enrich": _probe("enrich", "ok", "ok",
                         {"backlog": 50, "idle_sec": 60,
                          "done_per_min": 5.0}),
        "scan": _probe("scan", "ok", "ok",
                       {"failed": 0, "scanning": 0, "last_failed": []}),
        "sa_quota": _probe("sa_quota", "ok", "ok", {"sa_count": 100}),
        "api": _probe("api", "ok", "ok",
                      {"global_error_ratio": 0.001, "global_p99_ms": 120}),
        "workers": _probe("workers", "ok", "ok", {"total": 8, "dead": []}),
    }


@pytest.fixture()
def mock_subs(monkeypatch):
    """把探针整体换成可控数据；默认全健康。"""
    holder = {"subs": healthy_subs()}

    def fake_probe_subsystems(db):
        return holder["subs"]

    import backend.emby_server.observability as obs
    monkeypatch.setattr(obs, "probe_subsystems", fake_probe_subsystems)
    # enrich 夜间判断默认"不在夜间模式"（白天场景）
    monkeypatch.setattr(diagnosis, "_enrich_night_paused", lambda db: False)
    return holder


def _by_id(report, rule_id):
    for f in report["findings"]:
        if f["rule_id"] == rule_id:
            return f
    raise AssertionError(f"规则 {rule_id} 不在报告里")


def test_all_healthy(db, mock_subs):
    rep = diagnosis.run_diagnosis(db)
    assert rep["summary"]["total"] == 9
    assert rep["summary"]["warn"] == 0
    assert rep["summary"]["critical"] == 0
    assert rep["summary"]["info"] == 9
    assert "ran_at" in rep and "note" in rep and "只读" in rep["note"]
    for f in rep["findings"]:
        assert f["level"] in ("info", "warn", "critical")
        assert f["level_cn"] in ("正常", "需要注意", "严重")
        assert f["title"] and f["description"]


def test_probe_backlog_warn(db, mock_subs):
    mock_subs["subs"]["probe"]["metrics"]["pending_ready"] = 8000
    rep = diagnosis.run_diagnosis(db)
    f = _by_id(rep, "probe_queue")
    assert f["level"] == "warn"
    assert "8000" in f["title"]
    assert f["steps"]


def test_probe_worker_stalled_critical(db, mock_subs):
    mock_subs["subs"]["probe"]["metrics"]["pending_ready"] = 300
    mock_subs["subs"]["probe"]["metrics"]["running"] = False
    rep = diagnosis.run_diagnosis(db)
    f = _by_id(rep, "probe_queue")
    assert f["level"] == "critical"
    assert "没在跑" in f["title"]


def test_probe_paused_not_critical(db, mock_subs):
    # 暂停是主动行为，不算"没人干"（宁可漏报不要误报）
    mock_subs["subs"]["probe"]["metrics"]["pending_ready"] = 300
    mock_subs["subs"]["probe"]["metrics"]["running"] = False
    mock_subs["subs"]["probe"]["metrics"]["paused"] = True
    rep = diagnosis.run_diagnosis(db)
    f = _by_id(rep, "probe_queue")
    assert f["level"] != "critical"


def test_enrich_stalled_warn(db, mock_subs):
    mock_subs["subs"]["enrich"]["metrics"]["backlog"] = 5000
    mock_subs["subs"]["enrich"]["metrics"]["idle_sec"] = 10800
    rep = diagnosis.run_diagnosis(db)
    f = _by_id(rep, "enrich_stall")
    assert f["level"] == "warn"
    assert "停滞" in f["title"]


def test_enrich_night_mode_no_false_alarm(db, mock_subs, monkeypatch):
    # 夜间模式的白天：积压再多也不判停滞
    monkeypatch.setattr(diagnosis, "_enrich_night_paused", lambda db: True)
    mock_subs["subs"]["enrich"]["metrics"]["backlog"] = 5000
    mock_subs["subs"]["enrich"]["metrics"]["idle_sec"] = 20000
    rep = diagnosis.run_diagnosis(db)
    f = _by_id(rep, "enrich_stall")
    assert f["level"] == "info"
    assert "夜间" in f["title"]


def test_disk_warn_and_critical(db, mock_subs):
    mock_subs["subs"]["disk"]["metrics"]["disk_free_percent"] = 12.0
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "disk_space")["level"] == "warn"
    mock_subs["subs"]["disk"]["metrics"]["disk_free_percent"] = 5.0
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "disk_space")["level"] == "critical"


def test_scan_failed_warn(db, mock_subs):
    mock_subs["subs"]["scan"]["metrics"]["failed"] = 2
    mock_subs["subs"]["scan"]["metrics"]["last_failed"] = [{"name": "欧美剧"}]
    rep = diagnosis.run_diagnosis(db)
    f = _by_id(rep, "scan_failed")
    assert f["level"] == "warn"
    assert "欧美剧" in f["title"]


def test_sa_zero_critical(db, mock_subs):
    mock_subs["subs"]["sa_quota"]["metrics"]["sa_count"] = 0
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "sa_health")["level"] == "critical"


def test_api_error_warn(db, mock_subs):
    mock_subs["subs"]["api"]["metrics"]["global_error_ratio"] = 0.12
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "api_errors")["level"] == "warn"


def test_db_down_critical(db, mock_subs):
    mock_subs["subs"]["database"] = _probe("database", "down", "连接被拒")
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "db_health")["level"] == "critical"


def test_workers_dead_warn(db, mock_subs):
    mock_subs["subs"]["workers"]["metrics"]["dead"] = ["probe"]
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "workers_alive")["level"] == "warn"


def test_breakers_warn(db, mock_subs):
    mock_subs["subs"]["probe"]["metrics"]["open_breakers"] = ["m1"]
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "probe_breakers")["level"] == "warn"


def test_missing_data_no_false_alarm(db, mock_subs):
    # 探针没拿到数据 → 如实说，不误报 warn/critical
    mock_subs["subs"]["disk"]["metrics"] = {}
    mock_subs["subs"]["api"]["metrics"] = {}
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "disk_space")["level"] == "warn"  # 读不到是采集问题，值得看一眼
    assert _by_id(rep, "api_errors")["level"] == "info"
    assert "暂无数据" in _by_id(rep, "api_errors")["title"]


def test_threshold_configurable(db, mock_subs, monkeypatch):
    # 环境变量改阈值：默认 5000 不报，改成 100 就报
    mock_subs["subs"]["probe"]["metrics"]["pending_ready"] = 500
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "probe_queue")["level"] == "info"
    monkeypatch.setenv("DIAG_PROBE_QUEUE_WARN", "100")
    rep = diagnosis.run_diagnosis(db)
    assert _by_id(rep, "probe_queue")["level"] == "warn"


def test_rule_exception_degrades_gracefully(db, mock_subs, monkeypatch):
    def boom(subs):
        raise RuntimeError("boom")
    # 直接替换注册表中的函数
    orig = diagnosis._RULES
    patched = [(rid, name, boom if rid == "disk_space" else fn)
               for rid, name, fn in orig]
    monkeypatch.setattr(diagnosis, "_RULES", patched)
    rep = diagnosis.run_diagnosis(db)
    f = _by_id(rep, "disk_space")
    assert f["level"] == "info"
    assert "暂不可用" in f["title"]
    # 其他规则不受影响
    assert _by_id(rep, "db_health")["level"] == "info"


def test_probes_total_failure_still_reports(db, monkeypatch):
    import backend.emby_server.observability as obs
    def boom(db):
        raise RuntimeError("probes down")
    monkeypatch.setattr(obs, "probe_subsystems", boom)
    monkeypatch.setattr(diagnosis, "_enrich_night_paused", lambda db: False)
    rep = diagnosis.run_diagnosis(db)
    assert rep["summary"]["total"] == 9
    # 探针整体失败：各规则按"暂无数据"处理，不抛异常
    assert _by_id(rep, "db_health")["level"] == "info"


def test_rules_list_matches_registry():
    ids = [r["rule_id"] for r in diagnosis.RULES]
    assert ids == [rid for rid, _, _ in diagnosis._RULES]
    assert len(ids) == len(set(ids))  # 无重复

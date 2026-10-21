"""GET /api/admin/tasks/overview 任务总览聚合接口。

- 5 张卡片一次返回：scan / probe / enrich / thumbnail / chase_new
  （注 2026-10-17：strm_gen 删除，strm 卡片同步下线）
- 结构断言：每张卡都有 key/title/status/status_text/summary/metrics
- 种子数据驱动状态：扫描运行中、probe 有失败、追新开着
- 降级：单个任务采集抛异常时，该卡 status=error，其余卡片不受影响

全部在隔离临时 SQLite 里跑，不连生产库、不碰网络。
"""
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend import models as base_models
from backend.api import admin_tasks
from backend.emby_server import models as em
from backend.integrations import store
from backend.database import SessionLocal, init_db

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        # 每个测试前清空（隔离，避免测试间污染）
        s.query(em.ScanRun).delete()
        s.query(em.MediaItem).delete()
        s.query(em.ChaseRun).delete()
        s.query(em.ChaseSourceState).delete()
        s.query(em.Library).delete()
        s.query(base_models.SystemConfig).delete()
        s.commit()
        store.invalidate()
        yield s
    finally:
        s.close()


@pytest.fixture()
def seed(db):
    """种子数据：2 个库 + 扫描流水 + 若干条目（probe/enrich 各状态）。"""
    lib1 = em.Library(guid="lib1", name="电影库", scan_status="running")
    lib2 = em.Library(guid="lib2", name="剧集库", scan_status="success")
    db.add_all([lib1, lib2])
    db.flush()
    now = datetime.now()
    db.add(em.ScanRun(library_id=lib1.id, status="running",
                      trigger="manual", started_at=now - timedelta(minutes=5)))
    db.add(em.ScanRun(library_id=lib2.id, status="success",
                      trigger="auto", started_at=now - timedelta(hours=2),
                      finished_at=now - timedelta(hours=1),
                      duration_ms=3600000))
    items = [
        # probe: 3 待探测 / 1 失败 / 2 完成
        em.MediaItem(guid="m1", library_id=lib1.id, item_type="movie",
                     name="片1", probe_status="pending", enrich_status="pending"),
        em.MediaItem(guid="m2", library_id=lib1.id, item_type="movie",
                     name="片2", probe_status="pending", enrich_status="pending"),
        em.MediaItem(guid="m3", library_id=lib1.id, item_type="movie",
                     name="片3", probe_status="pending", enrich_status="enriching"),
        em.MediaItem(guid="m4", library_id=lib1.id, item_type="movie",
                     name="片4", probe_status="failed", probe_last_error="timeout",
                     enrich_status="done"),
        em.MediaItem(guid="m5", library_id=lib1.id, item_type="movie",
                     name="片5", probe_status="done", enrich_status="done"),
        em.MediaItem(guid="m6", library_id=lib2.id, item_type="episode",
                     name="剧1E1", probe_status="done", enrich_status="failed"),
    ]
    db.add_all(items)
    # 追新：开着
    store.write_values(db, {
        "chase_new_enabled": "1",
        "chase_new_interval": "30",
        "chase_new_last_check": (now - timedelta(minutes=20)).isoformat(),
        "chase_new_last_found": "7",
    }, {})
    db.commit()
    store.invalidate()
    return {"lib1": lib1, "lib2": lib2}


def _call(db):
    return admin_tasks.tasks_overview(db=db, admin=SimpleNamespace())


def _by_key(res):
    return {t["key"]: t for t in res["tasks"]}


def test_overview_returns_five_cards(db, seed):
    res = _call(db)
    assert res["success"] is True
    assert "fetched_at" in res
    cards = _by_key(res)
    assert set(cards) == {"scan", "probe", "enrich", "thumbnail", "chase_new"}
    for key, card in cards.items():
        for field in ("key", "title", "status", "status_text", "summary", "metrics"):
            assert field in card, f"{key} 缺字段 {field}"
        assert card["status"] in ("running", "ok", "warning", "error",
                                  "idle", "disabled"), key
        assert isinstance(card["metrics"], list)


def test_scan_card_reflects_running_library(db, seed):
    card = _by_key(_call(db))["scan"]
    # 电影库 scan_status=running → 运行中
    assert card["status"] == "running"
    assert len(card["libraries"]) == 2
    lib1 = next(l for l in card["libraries"] if l["name"] == "电影库")
    assert lib1["scan_status"] == "running"
    assert lib1["last_status"] == "running"
    lib2 = next(l for l in card["libraries"] if l["name"] == "剧集库")
    assert lib2["last_status"] == "success"
    # 2026-10-21 A 类摘面板：定时扫描开关已摘除，卡片不再带 switch
    assert card["switch"] is None


def test_probe_card_counts_and_fail_rate(db, seed):
    card = _by_key(_call(db))["probe"]
    metrics = {m["label"]: m["value"] for m in card["metrics"]}
    assert metrics["待探测"] == "3 条"
    assert "1 条" in metrics["已失败"]
    # 6 条里 1 条失败 = 16.7%，不到 20% 阈值 → 仍是运行中（有排队）
    assert card["status"] == "running"


def test_enrich_card_backlog(db, seed):
    card = _by_key(_call(db))["enrich"]
    metrics = {m["label"]: m["value"] for m in card["metrics"]}
    assert metrics["待刮削"] == "2 条"
    assert metrics["刮削中"] == "1 条"
    assert metrics["已完成"] == "2 条"
    assert metrics["失败"] == "1 条"
    assert card["status"] == "running"


def test_chase_new_card(db, seed):
    card = _by_key(_call(db))["chase_new"]
    # 2026-10-21 A 类摘面板：追新开关已摘除，卡片不再带 switch
    assert card["switch"] is None
    metrics = {m["label"]: m["value"] for m in card["metrics"]}
    assert metrics["上次发现"] == "7 个新文件"


def test_single_task_failure_degrades_only_that_card(db, seed, monkeypatch):
    """单个任务采集抛异常：该卡 error，其余卡片照常。"""
    def _boom(_db):
        raise RuntimeError("probe 炸了")

    monkeypatch.setattr(admin_tasks, "_task_probe", _boom)
    cards = _by_key(_call(db))
    assert cards["probe"]["status"] == "error"
    assert "probe 炸了" in cards["probe"]["summary"]
    assert cards["scan"]["status"] == "running"
    assert cards["enrich"]["status"] == "running"


@pytest.fixture(autouse=True)
def _clear_task_snapshots():
    """快照缓存是进程级的：每个测试前后清掉，避免测试间污染。"""
    admin_tasks._clear_task_snapshots()
    yield
    admin_tasks._clear_task_snapshots()


def test_cards_keep_builders_order(db, seed):
    """并行采集后，卡片顺序仍与 _BUILDERS 定义一致。"""
    res = _call(db)
    assert [t["key"] for t in res["tasks"]] == [k for k, _, _ in admin_tasks._BUILDERS]


def test_probe_snapshot_cache_serves_stale_within_ttl(db, seed):
    """probe 快照 20s 内走缓存：DB 变了，卡片数字不变；清缓存后重算。"""
    m1 = {m["label"]: m["value"] for m in _by_key(_call(db))["probe"]["metrics"]}
    assert m1["待探测"] == "3 条"
    db.add(em.MediaItem(guid="mx", library_id=seed["lib1"].id, item_type="movie",
                        name="新增", probe_status="pending", enrich_status="done"))
    db.commit()
    m2 = {m["label"]: m["value"] for m in _by_key(_call(db))["probe"]["metrics"]}
    assert m2["待探测"] == "3 条"  # 缓存命中，仍是旧数
    admin_tasks._clear_task_snapshots()
    m3 = {m["label"]: m["value"] for m in _by_key(_call(db))["probe"]["metrics"]}
    assert m3["待探测"] == "4 条"  # 缓存清掉，重算拿到新数


def test_enrich_snapshot_cache_serves_stale_within_ttl(db, seed):
    e1 = {m["label"]: m["value"] for m in _by_key(_call(db))["enrich"]["metrics"]}
    assert e1["待刮削"] == "2 条"
    db.query(em.MediaItem).filter(em.MediaItem.guid == "m1").update(
        {"enrich_status": "done"})
    db.commit()
    e2 = {m["label"]: m["value"] for m in _by_key(_call(db))["enrich"]["metrics"]}
    assert e2["待刮削"] == "2 条"  # 缓存命中
    admin_tasks._clear_task_snapshots()
    e3 = {m["label"]: m["value"] for m in _by_key(_call(db))["enrich"]["metrics"]}
    assert e3["待刮削"] == "1 条"


def test_light_snapshot_keeps_shape_but_skips_error_scan(db, seed):
    """light 快照：键不变、计数一致；跳过 probe_last_error 全表聚合。"""
    from backend.emby_server import probe_worker

    full = probe_worker.status_snapshot(db)
    light = probe_worker.status_snapshot(db, light=True)
    assert set(light) == set(full)
    assert light["counts"] == full["counts"]
    assert light["retrying"] == 0
    assert light["stale_probing"] == 0
    assert light["top_errors"] == []
    # 全量模式照旧：种子里 m4（failed + timeout）应出现在 top_errors
    assert any(e["error"] == "timeout" for e in full["top_errors"])


def test_cached_snapshot_does_not_cache_loader_errors(db, seed, monkeypatch):
    """loader 抛异常不进缓存：下一次调用会重新执行 loader。"""
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("loader 炸了")

    with pytest.raises(RuntimeError):
        admin_tasks._cached_snapshot("k-boom", _boom)
    with pytest.raises(RuntimeError):
        admin_tasks._cached_snapshot("k-boom", _boom)
    assert calls["n"] == 2

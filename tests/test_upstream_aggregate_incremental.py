"""上游聚合增量拉取（定时轮）：水位线、启动新鲜度门、全量兜底、对账只认全量。

钉死的行为（用户 2026-11-01 亲点「按增量方案改，闲时请求压到最低」）：
- 定时轮增量：逐库按 DateLastContentAdded 倒序拉，整页不高于
  水位线即停（该页照常处理）；上游无更新时每库只发 1 页；
- 水位线只在该库本轮成功后推进，失败不推进；无水位线按全量
  跑并建立水位线；
- 调度启动首轮新鲜度门：距上轮成功不足门限整轮跳过不入队
  （零上游请求，只查本地 DB）；
- 全量兜底：每源每天至多一轮全量由 kind 决策把守（jobs 层）；
  手动聚合恒全量；
- 生命周期对账只认全量轮：增量轮绝不调 reconcile；
- 任务 result / 逐库 summary 带 kind、实际请求页数、水位线。

HTTP 全部注入替身，不发真实请求；DB 用内存 SQLite。
本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
import uuid
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_jobs as jobs
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_aggregate_scheduler as uas
from backend.emby_server import upstream_import as uimp
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as ups


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_jobs(db_factory):
    from backend.integrations import store as config_store
    config_store._ttl_cache.clear()
    jobs.reset_for_tests()
    jobs._session_factory = db_factory
    yield
    jobs.reset_for_tests()
    config_store._ttl_cache.clear()


def _source(db, *, name="src-1"):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=True,
        credential_scope="source", health_status="unknown", note="")
    db.add(row)
    db.flush()
    return row


def _local(db, *, item_id, name, tmdb):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type="movie", name=name, tmdb_id=tmdb,
        merged_into_id=None)
    db.add(row)
    db.flush()
    return row


def _raw(item_id, ts, tmdb=None):
    raw = {"Id": item_id, "Name": f"片-{item_id}", "Type": "Movie",
           "ProductionYear": 2024,
           "ProviderIds": ({"Tmdb": tmdb} if tmdb else {}),
           "MediaSources": []}
    if ts is not None:
        raw["DateLastContentAdded"] = ts
    return raw


# Emby 风格时间戳（7 位小数 + Z）：解析器须能吃下。
T_OLD = "2026-10-30T09:00:00.0000000Z"
T_WM = "2026-10-30T10:00:00.0000000Z"
T_MID = "2026-10-30T11:00:00.0000000Z"
T_NEW = "2026-10-30T12:00:00.0000000Z"
WM_ISO = "2026-10-30T10:00:00+00:00"  # T_WM 的归一化存量形态
NEW_ISO = "2026-10-30T12:00:00+00:00"


class _FakeUpstream:
    """按 ParentId 分库、认 SortBy 的替身上游 + 请求计数。

    libraries: {lib_id: {"name": str, "items": [raw, ...]}}
    带 SortBy=DateLastContentAdded 时按该字段倒序（与真 Emby 同
    语义）；不带 SortBy 时按存量顺序（旧全量行为）。
    """

    def __init__(self, libraries, fail_libraries=()):
        self.libraries = libraries
        self.fail_libraries = set(fail_libraries)
        self.item_urls = []  # (lib_id, url)

    def get(self, url, headers, timeout):
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [
                {"Id": lib_id, "Name": spec["name"],
                 "CollectionType": "movies"}
                for lib_id, spec in self.libraries.items()]}
        query = parse_qs(urlsplit(url).query)
        lib_id = query["ParentId"][0]
        if lib_id in self.fail_libraries:
            return 401, {}
        self.item_urls.append((lib_id, url))
        items = list(self.libraries[lib_id]["items"])
        sort_by = (query.get("SortBy") or [""])[0]
        if "DateLastContentAdded" in sort_by:
            items.sort(key=lambda r: r.get("DateLastContentAdded") or "",
                       reverse=True)
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        return 200, {"Items": items[start:start + limit],
                     "TotalRecordCount": len(items)}

    def count(self, lib_id=None):
        if lib_id is None:
            return len(self.item_urls)
        return sum(1 for lib, _ in self.item_urls if lib == lib_id)


def _seed_watermark(db, source_id, lib_id, watermark_at=WM_ISO):
    row = em.UpstreamAggregateWatermark(
        source_id=source_id, upstream_library_id=lib_id,
        watermark_at=watermark_at)
    db.add(row)
    db.flush()
    return row


def _watermark(db, source_id, lib_id):
    return (db.query(em.UpstreamAggregateWatermark)
            .filter(em.UpstreamAggregateWatermark.source_id == source_id,
                    em.UpstreamAggregateWatermark.upstream_library_id
                    == lib_id)
            .first())


# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------

def test_parse_upstream_ts_variants():
    parsed = uar._parse_upstream_ts("2026-10-30T10:00:00.1234567Z")
    assert parsed is not None
    assert parsed.isoformat() == "2026-10-30T10:00:00.123456+00:00"
    assert uar._parse_upstream_ts(T_WM).isoformat() == WM_ISO
    assert uar._parse_upstream_ts("2026-10-30T12:00:00+02:00").isoformat() \
        == "2026-10-30T12:00:00+02:00"
    naive = uar._parse_upstream_ts("2026-10-30T10:00:00")
    assert naive is not None and naive.tzinfo is not None
    assert uar._parse_upstream_ts("not-a-date") is None
    assert uar._parse_upstream_ts("") is None
    assert uar._parse_upstream_ts(None) is None


# ---------------------------------------------------------------------------
# 增量拉取：请求数、水位线推进/不推进
# ---------------------------------------------------------------------------

def test_incremental_idle_round_one_page_per_library(db):
    """上游无更新：每库只发 1 页，水位线不动，result 带 kind。"""
    source = _source(db)
    _seed_watermark(db, source.id, "lib-a")
    _seed_watermark(db, source.id, "lib-b")
    db.commit()
    fake = _FakeUpstream({
        "lib-a": {"name": "电影A", "items": [
            _raw("a1", T_WM), _raw("a2", T_OLD), _raw("a3", T_OLD)]},
        "lib-b": {"name": "电影B", "items": [
            _raw("b1", T_OLD), _raw("b2", T_OLD)]},
    })
    result = uar.run_manual_aggregation(
        db, source, http_get=fake.get, run_kind="incremental",
        commit_each_library=True)
    assert result["kind"] == "incremental"
    assert fake.count("lib-a") == 1
    assert fake.count("lib-b") == 1
    assert fake.count() == 2
    by_lib = {s["library_id"]: s for s in result["libraries"]}
    assert by_lib["lib-a"]["kind"] == "incremental"
    assert by_lib["lib-a"]["pages"] == 1
    assert by_lib["lib-a"]["watermark_before"] == WM_ISO
    assert by_lib["lib-a"]["watermark_after"] == WM_ISO
    assert by_lib["lib-b"]["pages"] == 1
    # 水位线原地不动（本轮见到的最大值不高于旧水位）。
    assert _watermark(db, source.id, "lib-a").watermark_at == WM_ISO
    assert _watermark(db, source.id, "lib-b").watermark_at == WM_ISO
    # 该页照常处理：5 条都进了聚合口径。
    assert result["fetched"] == 5


def test_incremental_stops_at_watermark_and_advances(db, monkeypatch):
    """有新增：只拉到水位线页为止，新记录照常配对，水位推进到最大值。"""
    source = _source(db)
    _local(db, item_id=10, name="新片", tmdb="900")
    _seed_watermark(db, source.id, "lib-a")
    db.commit()
    fake = _FakeUpstream({
        "lib-a": {"name": "电影A", "items": [
            _raw("old2", T_OLD), _raw("new1", T_MID),
            _raw("edge", T_WM), _raw("new0", T_NEW, tmdb="900")]},
    })
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(
        db, source, http_get=fake.get, page_size=2,
        run_kind="incremental", commit_each_library=True)
    # 倒序页： [new0, new1] 页最大 > 水位 → 继续；
    # [edge, old2] 页最大 == 水位 → 处理后停。共 2 页，不是全量 2 页后
    # 再探空页。
    assert fake.count("lib-a") == 2
    assert result["fetched"] == 4
    summary = result["libraries"][0]
    assert summary["pages"] == 2
    assert summary["watermark_before"] == WM_ISO
    assert summary["watermark_after"] == NEW_ISO
    assert _watermark(db, source.id, "lib-a").watermark_at == NEW_ISO
    # 增量请求必须带倒序排序参数；新记录走了既有配对管线。
    assert all("SortBy=DateLastContentAdded" in url
               and "SortOrder=Descending" in url
               for _, url in fake.item_urls)
    assert result["paired"] >= 1
    assert db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.upstream_item_id == "new0").count() == 1


def test_incremental_failed_library_watermark_not_advanced(db):
    """单库失败：该库水位线不推进；成功库照常推进、失败只记该库。"""
    source = _source(db)
    _seed_watermark(db, source.id, "lib-good")
    _seed_watermark(db, source.id, "lib-bad")
    db.commit()
    fake = _FakeUpstream({
        "lib-good": {"name": "好库", "items": [_raw("g1", T_NEW)]},
        "lib-bad": {"name": "坏库", "items": [_raw("x1", T_NEW)]},
    }, fail_libraries={"lib-bad"})
    result = uar.run_manual_aggregation(
        db, source, http_get=fake.get, run_kind="incremental",
        commit_each_library=True)
    assert [f["library_id"] for f in result["failed_libraries"]] \
        == ["lib-bad"]
    assert _watermark(db, source.id, "lib-good").watermark_at == NEW_ISO
    assert _watermark(db, source.id, "lib-bad").watermark_at == WM_ISO


def test_incremental_library_without_watermark_falls_back_full(db):
    """增量轮里没水位线的库按全量拉（不带排序早停），并建立水位线。"""
    source = _source(db)
    _seed_watermark(db, source.id, "lib-old")
    db.commit()
    fake = _FakeUpstream({
        "lib-old": {"name": "老库", "items": [_raw("o1", T_OLD)]},
        "lib-new": {"name": "新库", "items": [
            _raw("n1", T_MID), _raw("n2", T_OLD)]},
    })
    result = uar.run_manual_aggregation(
        db, source, http_get=fake.get, run_kind="incremental",
        commit_each_library=True)
    by_lib = {s["library_id"]: s for s in result["libraries"]}
    assert by_lib["lib-new"]["kind"] == "full"
    assert by_lib["lib-new"]["watermark_before"] is None
    assert by_lib["lib-new"]["watermark_after"] \
        == "2026-10-30T11:00:00+00:00"
    assert _watermark(db, source.id, "lib-new") is not None
    new_lib_urls = [u for lib, u in fake.item_urls if lib == "lib-new"]
    assert all("SortBy" not in u for u in new_lib_urls)


# ---------------------------------------------------------------------------
# 对账只认全量轮
# ---------------------------------------------------------------------------

def test_reconcile_only_on_full_rounds(db, monkeypatch):
    source = _source(db)
    _seed_watermark(db, source.id, "lib-a")
    db.commit()
    calls = {"n": 0}

    def _spy(db_, source_, seen):
        calls["n"] += 1
        return {"tracked": 0}

    monkeypatch.setattr(uimp, "reconcile_hosted_lifecycle", _spy)
    fake = _FakeUpstream({
        "lib-a": {"name": "电影A", "items": [_raw("a1", T_OLD)]},
    })
    result_inc = uar.run_manual_aggregation(
        db, source, http_get=fake.get, run_kind="incremental",
        commit_each_library=True)
    assert calls["n"] == 0
    assert result_inc["lifecycle"] is None
    result_full = uar.run_manual_aggregation(
        db, source, http_get=fake.get, run_kind="full",
        commit_each_library=True)
    assert calls["n"] == 1
    assert result_full["lifecycle"] == {"tracked": 0}


def test_full_round_establishes_watermark_and_records_kind(db):
    """存量首次（无水位线）：全量跑、建水位线、请求不带排序参数。"""
    source = _source(db)
    fake = _FakeUpstream({
        "lib-a": {"name": "电影A", "items": [
            _raw("a1", T_OLD), _raw("a2", T_NEW), _raw("a3", T_MID)]},
    })
    result = uar.run_manual_aggregation(
        db, source, http_get=fake.get, commit_each_library=True)
    assert result["kind"] == "full"
    summary = result["libraries"][0]
    assert summary["kind"] == "full"
    assert summary["pages"] == 1
    assert summary["watermark_before"] is None
    assert summary["watermark_after"] == NEW_ISO
    row = _watermark(db, source.id, "lib-a")
    assert row is not None and row.watermark_at == NEW_ISO
    assert all("SortBy" not in url for _, url in fake.item_urls)


# ---------------------------------------------------------------------------
# jobs 层：kind 决策与执行传参
# ---------------------------------------------------------------------------

def _seed_task(db, source_id, *, kind, finished_at, trigger="scheduled"):
    row = em.UpstreamAggregateTask(
        task_id=(f"t-{kind}-s{source_id}-"
                 f"{int(finished_at.timestamp())}-{uuid.uuid4().hex[:6]}"),
        source_id=source_id, trigger=trigger, status="succeeded",
        page_size=500,
        result_json=json.dumps({"kind": kind}) if kind else None,
        created_at=finished_at, started_at=finished_at,
        finished_at=finished_at, heartbeat_at=finished_at)
    db.add(row)
    db.flush()
    return row


def test_scheduled_kind_decision_matrix(db, monkeypatch):
    source = _source(db)
    now = datetime.now()
    # 无水位线 → 全量（存量首次）。
    assert jobs.decide_scheduled_run_kind(db, source.id) == "full"
    _seed_watermark(db, source.id, "lib-a")
    db.commit()
    # 有水位线但无任何成功记录 → 全量。
    assert jobs.decide_scheduled_run_kind(db, source.id) == "full"
    # 上次全量在 1 小时前 → 增量。
    _seed_task(db, source.id, kind="full",
               finished_at=now - timedelta(hours=1))
    db.commit()
    assert jobs.decide_scheduled_run_kind(db, source.id) == "incremental"
    # 增量成功不刷新全量时钟：全量已在 25 小时前 → 回全量兜底。
    _seed_task(db, source.id, kind="incremental",
               finished_at=now - timedelta(minutes=10))
    db.query(em.UpstreamAggregateTask).filter(
        em.UpstreamAggregateTask.result_json
        == json.dumps({"kind": "full"})).delete()
    _seed_task(db, source.id, kind="full",
               finished_at=now - timedelta(hours=25))
    db.commit()
    assert jobs.decide_scheduled_run_kind(db, source.id) == "full"
    # 回退开关关 → 恒全量。
    _seed_task(db, source.id, kind="full",
               finished_at=now - timedelta(minutes=30))
    db.commit()
    assert jobs.decide_scheduled_run_kind(db, source.id) == "incremental"
    monkeypatch.setenv("UPSTREAM_AGGREGATE_INCREMENTAL_ENABLED", "0")
    assert jobs.decide_scheduled_run_kind(db, source.id) == "full"


def _summary(source, **overrides):
    result = {
        "source_id": source.id, "fetched": 1, "aggregates": 1,
        "pairs": [], "paired": 0, "written": 0,
        "mapping_enabled": False, "unpaired_reasons": {},
        "libraries": [], "libraries_discovered": 0,
        "failed_libraries": [],
    }
    result.update(overrides)
    return result


def test_execute_task_passes_kind_and_result_records_it(db, monkeypatch):
    """定时任务执行时按决策传 run_kind；手动任务恒 full。"""
    seen = {}

    def fake_fn(db_session, src, page_size=100, **kwargs):
        seen[src.id] = kwargs.get("run_kind")
        return _summary(src, kind=kwargs.get("run_kind"))

    monkeypatch.setattr(jobs, "_aggregate_fn", fake_fn)
    now = datetime.now()
    # 定时源：水位线 + 1 小时前的全量成功 → 增量。
    sched = _source(db, name="sched")
    _seed_watermark(db, sched.id, "lib-a")
    _seed_task(db, sched.id, kind="full",
               finished_at=now - timedelta(hours=1))
    db.commit()
    view = jobs.enqueue_scheduled(sched.id)
    jobs.execute_task(view["task_id"])
    assert seen[sched.id] == "incremental"
    row = (db.query(em.UpstreamAggregateTask)
           .filter(em.UpstreamAggregateTask.task_id == view["task_id"])
           .one())
    assert row.status == "succeeded"
    assert json.loads(row.result_json)["kind"] == "incremental"
    # 手动源：哪怕水位线与全量时钟都满足增量条件，仍恒全量。
    manual = _source(db, name="manual")
    _seed_watermark(db, manual.id, "lib-a")
    _seed_task(db, manual.id, kind="full",
               finished_at=now - timedelta(hours=1))
    db.commit()
    view2 = jobs.enqueue(manual.id, 100)
    jobs.execute_task(view2["task_id"])
    assert seen[manual.id] == "full"


# ---------------------------------------------------------------------------
# 调度启动新鲜度门
# ---------------------------------------------------------------------------

class _FakeSource:
    def __init__(self, source_id, *, enabled=True, source_type="emby"):
        self.id = source_id
        self.enabled = enabled
        self.source_type = source_type


def _enqueue_recorder(calls):
    def _enqueue(source):
        calls.append(source.id)
        return {"task_id": f"t-{source.id}", "status": "queued",
                "created": True,
                "view": {"task_id": f"t-{source.id}", "status": "queued",
                         "trigger": "scheduled"}}
    return _enqueue


def test_startup_gate_skips_enqueue_when_fresh(monkeypatch):
    monkeypatch.setenv("UPSTREAM_AGGREGATE_STARTUP_FRESH_SEC", "10800")
    calls = []
    results = uas.run_startup_aggregation(
        enqueue_fn=_enqueue_recorder(calls),
        list_sources_fn=lambda: [_FakeSource(1)],
        last_success_fn=lambda src: datetime.now() - timedelta(seconds=60))
    assert calls == []
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "startup_fresh"


def test_startup_gate_enqueues_when_stale_or_never(monkeypatch):
    monkeypatch.setenv("UPSTREAM_AGGREGATE_STARTUP_FRESH_SEC", "10800")
    calls = []
    results = uas.run_startup_aggregation(
        enqueue_fn=_enqueue_recorder(calls),
        list_sources_fn=lambda: [_FakeSource(1)],
        last_success_fn=lambda src: datetime.now() - timedelta(hours=4))
    assert calls == [1]
    assert results[0]["status"] == "queued"
    calls2 = []
    results2 = uas.run_startup_aggregation(
        enqueue_fn=_enqueue_recorder(calls2),
        list_sources_fn=lambda: [_FakeSource(1)],
        last_success_fn=lambda src: None)
    assert calls2 == [1]
    assert results2[0]["status"] == "queued"


def test_startup_gate_disabled_by_zero(monkeypatch):
    monkeypatch.setenv("UPSTREAM_AGGREGATE_STARTUP_FRESH_SEC", "0")
    calls = []
    results = uas.run_startup_aggregation(
        enqueue_fn=_enqueue_recorder(calls),
        list_sources_fn=lambda: [_FakeSource(1)],
        last_success_fn=lambda src: datetime.now())
    assert calls == [1]
    assert results[0]["status"] == "queued"

"""上游聚合任务持久化 + 逐库提交 + 可恢复（优化 O-01）。

钉死的行为：
- 任务状态落库：入队即有任务行；模块状态清空（模拟进程重启）后
  任务仍可查（queued 原样保留，不再 404「不存在或已过期」）；
- 逐库提交：一库跑完即 commit 该库映射；后续库撞映射冲突只回滚
  该库、记入 failed_libraries（code=mapping_conflict），前面库的
  映射已落库、任务整体仍 succeeded；
- 进度随库落库：中途整轮异常失败时，已完成库的进度仍在任务行里
  （libraries_done / libraries 明细 / 计数可查），不是全量蒸发；
- 心跳过期（默认 120 秒）的 running 任务在下一次入队时被标
  interrupted，不再冒充在跑；
- 续跑：interrupted / failed 任务可 resume，跳过已完成库（不重复
  拉取），新任务带 resume_of 与 prior 库的摘要续报；
- 终态 result 只存摘要：不带全量 pairs（明细走映射表分页查），
  paired 计数保留。

HTTP 全部注入替身；DB 用内存 SQLite。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_jobs as jobs
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_item_mappings as uim
from backend.integrations import store as config_store


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
def _clean(db_factory):
    config_store._ttl_cache.clear()
    jobs.reset_for_tests()
    jobs._session_factory = db_factory
    yield
    jobs.reset_for_tests()
    config_store._ttl_cache.clear()


def _source(db, *, enabled=True, name="src-1"):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=enabled,
        credential_scope="source", health_status="unknown")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _local(db, *, item_id, tmdb):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type="movie", name=f"片{item_id}", tmdb_id=tmdb,
        merged_into_id=None)
    db.add(row)
    db.flush()
    return row


def _hold_submit(monkeypatch):
    # 历史替身：O-01/O-02 时代防 enqueue 起执行线程；O-03 起
    # jobs 已无 _submit（执行归 worker），此处仅作无操作保留。
    return []


def _task_row(db, task_id):
    return (db.query(em.UpstreamAggregateTask)
            .filter(em.UpstreamAggregateTask.task_id == task_id).one())


class _TwoLibItems:
    """两库替身：lib-1 一部 tmdb=278，lib-2 一部 tmdb=999。"""

    def __init__(self):
        self.urls = []

    def get(self, url, headers, timeout):
        self.urls.append(url)
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [{"Id": "lib-1", "Name": "电影"},
                                   {"Id": "lib-2", "Name": "剧集"}]}
        query = parse_qs(urlsplit(url).query)
        parent = query["ParentId"][0]
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        items = {
            "lib-1": [{"Id": "u-1", "Name": "甲", "Type": "Movie",
                       "ProductionYear": 1994,
                       "ProviderIds": {"Tmdb": "278"}}],
            "lib-2": [{"Id": "u-2", "Name": "乙", "Type": "Movie",
                       "ProductionYear": 2020,
                       "ProviderIds": {"Tmdb": "999"}}],
        }[parent]
        return 200, {"Items": items[start:start + limit],
                     "TotalRecordCount": len(items)}

    def parents_fetched(self):
        parents = []
        for url in self.urls:
            if "/Items?" in url:
                parents.append(parse_qs(urlsplit(url).query)["ParentId"][0])
        return parents


def test_task_row_persists_and_survives_module_reset(
        db, db_factory, monkeypatch):
    source = _source(db)
    _hold_submit(monkeypatch)
    view = jobs.enqueue(source.id, 100)
    assert view["status"] == "queued"
    row = _task_row(db, view["task_id"])
    assert row.status == "queued"
    assert row.page_size == 100
    assert row.trigger == "manual"
    assert row.source_id == source.id

    # 模拟进程重启：模块内存与注入全清，任务行还在、仍可查。
    jobs.reset_for_tests()
    jobs._session_factory = db_factory
    snap = jobs.get_task(source.id, view["task_id"])
    assert snap is not None
    assert snap["status"] == "queued"
    assert snap["task_id"] == view["task_id"]


def test_execute_result_is_summary_without_pairs(db, monkeypatch):
    source = _source(db)
    _hold_submit(monkeypatch)

    def fake(db_session, src, page_size=100, **kwargs):
        return {
            "source_id": src.id, "fetched": 4, "aggregates": 3,
            "pairs": [{"upstream_item_id": "u1", "local_item_id": 1}],
            "paired": 2, "written": 1, "mapping_enabled": True,
            "unpaired_reasons": {}, "libraries": [],
            "libraries_discovered": 0, "failed_libraries": [],
        }

    monkeypatch.setattr(jobs, "_aggregate_fn", fake)
    view = jobs.enqueue(source.id, 100)
    jobs.execute_task(view["task_id"])
    snap = jobs.get_task(source.id, view["task_id"])
    assert snap["status"] == "succeeded"
    assert "pairs" not in snap["result"]
    assert snap["result"]["paired"] == 2
    assert snap["result"]["written"] == 1
    row = _task_row(db, view["task_id"])
    assert "u1" not in (row.result_json or "")


def test_per_library_commit_isolates_mapping_conflict(
        db, monkeypatch):
    source = _source(db)
    _local(db, item_id=10, tmdb="278")
    _local(db, item_id=11, tmdb="999")
    db.commit()
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    real_record = uim.record_mapping

    def flaky_record(db_session, **kwargs):
        if kwargs["upstream_item_id"] == "u-2":
            raise IntegrityError("INSERT", {}, Exception("dup"))
        return real_record(db_session, **kwargs)

    monkeypatch.setattr(uim, "record_mapping", flaky_record)
    _hold_submit(monkeypatch)
    fake = _TwoLibItems()
    monkeypatch.setattr(uar, "_default_http_get", fake.get)
    view = jobs.enqueue(source.id, 100)
    jobs.execute_task(view["task_id"])

    snap = jobs.get_task(source.id, view["task_id"])
    assert snap["status"] == "succeeded"
    assert snap["result"]["written"] == 1
    assert snap["result"]["failed_libraries"][0]["library_id"] == "lib-2"
    assert snap["result"]["failed_libraries"][0]["code"] == "mapping_conflict"
    # lib-1 的映射已独立落库（换新会话查，证明 commit 真发生）。
    check = sessionmaker(bind=db.get_bind())()
    try:
        rows = check.query(em.UpstreamItemMapping).all()
        assert [(r.local_item_id, r.upstream_item_id) for r in rows] == [
            (10, "u-1")]
    finally:
        check.close()
    row = _task_row(db, view["task_id"])
    assert row.libraries_done == 1
    assert row.written == 1


def test_legacy_run_still_propagates_integrity_error(db, monkeypatch):
    """不带 commit_each_library 的旧调用口径：冲突仍穿透（兼容守卫）。"""
    source = _source(db)
    _local(db, item_id=10, tmdb="278")
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")

    def boom(db_session, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("dup"))

    monkeypatch.setattr(uim, "record_mapping", boom)
    fake = _TwoLibItems()
    with pytest.raises(IntegrityError):
        uar.run_manual_aggregation(db, source, http_get=fake.get)


def test_progress_survives_midrun_failure(db, monkeypatch):
    source = _source(db)
    _hold_submit(monkeypatch)

    def fake(db_session, src, page_size=100, **kwargs):
        on_event = kwargs.get("on_event")
        assert on_event is not None, "执行器应注入进度回调"
        on_event({"type": "libraries_discovered",
                  "libraries": [{"id": "lib-1", "name": "电影"},
                                {"id": "lib-2", "name": "剧集"}]})
        on_event({"type": "library_done", "summary": {
            "library_id": "lib-1", "library_name": "电影", "fetched": 5,
            "aggregates": 5, "paired": 4, "written": 3, "status": "ok"}})
        raise RuntimeError("mid-run boom")

    monkeypatch.setattr(jobs, "_aggregate_fn", fake)
    view = jobs.enqueue(source.id, 100)
    jobs.execute_task(view["task_id"])
    snap = jobs.get_task(source.id, view["task_id"])
    assert snap["status"] == "failed"
    row = _task_row(db, view["task_id"])
    assert row.libraries_total == 2
    assert row.libraries_done == 1
    assert row.fetched == 5
    assert row.written == 3
    libs = json.loads(row.libraries_json)
    assert libs[0]["library_id"] == "lib-1"
    assert libs[0]["status"] == "ok"


def test_stale_running_task_marked_interrupted(db, monkeypatch):
    source = _source(db)
    stale = em.UpstreamAggregateTask(
        task_id="stale-task", source_id=source.id, trigger="manual",
        status="running", page_size=100,
        created_at=datetime.now() - timedelta(hours=1),
        started_at=datetime.now() - timedelta(hours=1),
        heartbeat_at=datetime.now() - timedelta(hours=1))
    db.add(stale)
    db.commit()
    _hold_submit(monkeypatch)
    jobs.enqueue(source.id, 100)  # 入队时的过期清扫应标出它
    db.refresh(stale)
    assert stale.status == "interrupted"
    snap = jobs.get_task(source.id, "stale-task")
    assert snap["status"] == "interrupted"


def test_resume_skips_completed_libraries(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=10, tmdb="278")
    _local(db, item_id=11, tmdb="999")
    db.commit()
    old = em.UpstreamAggregateTask(
        task_id="old-task", source_id=source.id, trigger="manual",
        status="interrupted", page_size=100,
        libraries_json=json.dumps([
            {"library_id": "lib-1", "library_name": "电影", "fetched": 1,
             "aggregates": 1, "paired": 1, "written": 1, "status": "ok"},
            {"library_id": "lib-2", "library_name": "剧集", "fetched": 0,
             "aggregates": 0, "paired": 0, "written": 0, "status": "failed",
             "error_code": "upstream_unavailable", "error": "上游不可用"},
        ]),
        created_at=datetime.now() - timedelta(hours=1),
        heartbeat_at=datetime.now() - timedelta(hours=1))
    db.add(old)
    db.commit()
    _hold_submit(monkeypatch)
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    fake = _TwoLibItems()
    monkeypatch.setattr(uar, "_default_http_get", fake.get)

    view = jobs.resume_task(source.id, "old-task")
    assert view is not None
    assert view["task_id"] != "old-task"
    row = _task_row(db, view["task_id"])
    assert row.resume_of == "old-task"
    assert json.loads(row.skip_libraries_json) == ["lib-1"]

    jobs.execute_task(view["task_id"])
    snap = jobs.get_task(source.id, view["task_id"])
    assert snap["status"] == "succeeded"
    # lib-1 没被重新拉取；结果里它的摘要来自上一轮、带续跑标记。
    assert "lib-1" not in fake.parents_fetched()
    assert "lib-2" in fake.parents_fetched()
    libs = {lib["library_id"]: lib for lib in snap["result"]["libraries"]}
    assert libs["lib-1"].get("skipped_resume") is True
    assert libs["lib-1"]["written"] == 1
    assert libs["lib-2"]["status"] == "ok"
    assert snap["result"]["fetched"] == 2  # 上轮 lib-1 的 1 + 本轮 lib-2 的 1


def test_resume_rejects_unknown_or_active_task(db, monkeypatch):
    source = _source(db)
    _hold_submit(monkeypatch)
    assert jobs.resume_task(source.id, "no-such") is None
    view = jobs.enqueue(source.id, 100)
    # 还在 queued（未终态、未中断）不许 resume；且同源已有进行中
    # 任务时 resume 返回进行中的那条（与 enqueue 同口径）。
    again = jobs.resume_task(source.id, view["task_id"])
    assert again["task_id"] == view["task_id"]

"""上游聚合进度可观测（优化 O-02，依赖 O-01 的任务表与事件）。

钉死的行为：
- run 每窗发 ``window`` 进度事件：带库、窗序号、累计
  fetched/paired 与未配对原因累计；管理端不用再靠 pg_stat_activity
  猜进度；
- 每窗（可配 ``upstream_aggregate_progress_log_every``，默认每窗）
  打一行进度 INFO 日志：source、库、窗序号、累计计数；
- jobs 的进度回调处理 window 事件：当前库、累计计数、未配对
  原因、心跳逐窗写回任务行——长库跑一半也能从任务行看到进度，
  且心跳新鲜时过期清扫不误标 interrupted；
- 任务视图带 ``unpaired_reasons``（进行中也可读）。

HTTP 全部注入替身；DB 用内存 SQLite。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
import logging
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


def _source(db):
    row = em.UpstreamSource(
        name="src-1", source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=True,
        credential_scope="source", health_status="unknown")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class _Items:
    """单库三部电影（tmdb 各异，本地不建条目 → 全部未配对）。"""

    def get(self, url, headers, timeout):
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [{"Id": "lib-1", "Name": "电影库"}]}
        query = parse_qs(urlsplit(url).query)
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        items = [{"Id": f"u-{i}", "Name": f"片{i}", "Type": "Movie",
                  "ProductionYear": 2000 + i,
                  "ProviderIds": {"Tmdb": str(100 + i)}}
                 for i in range(3)]
        return 200, {"Items": items[start:start + limit],
                     "TotalRecordCount": len(items)}


def test_run_emits_window_events_with_cumulative_counts(db, monkeypatch):
    source = _source(db)
    monkeypatch.setattr(uar, "WINDOW_RECORDS", 2)
    events = []
    result = uar.run_manual_aggregation(
        db, source, http_get=_Items().get, on_event=events.append)
    assert result["fetched"] == 3
    windows = [e for e in events if e["type"] == "window"]
    assert len(windows) == 2
    assert windows[0]["library_id"] == "lib-1"
    assert windows[0]["window_index"] == 1
    assert windows[0]["fetched"] == 2
    assert windows[1]["window_index"] == 2
    assert windows[1]["fetched"] == 3
    assert windows[1]["unpaired_reasons"] == {"identity_no_local_match": 3}
    types = [e["type"] for e in events]
    assert types.index("window") < types.index("library_done")


def test_run_logs_progress_per_window(db, monkeypatch, caplog):
    source = _source(db)
    monkeypatch.setattr(uar, "WINDOW_RECORDS", 2)
    with caplog.at_level(logging.INFO,
                         logger="backend.emby_server.upstream_aggregate_run"):
        uar.run_manual_aggregation(db, source, http_get=_Items().get)
    progress = [r for r in caplog.records
                if "上游聚合进度" in r.getMessage()]
    assert len(progress) == 2
    assert "lib-1" in progress[-1].getMessage()
    assert "fetched=3" in progress[-1].getMessage()


def test_window_event_updates_task_row_and_heartbeat(
        db, db_factory, monkeypatch):
    source = _source(db)
    # enqueue 现只登记（O-03），无需再压执行线程。
    view = jobs.enqueue(source.id, 100)
    # 把任务行伪造成「跑了很久、心跳过期」：window 事件一到，
    # 心跳应刷新、进度应落行，清扫不再误标 interrupted。
    claim = db_factory()
    row = (claim.query(em.UpstreamAggregateTask)
           .filter(em.UpstreamAggregateTask.task_id == view["task_id"])
           .one())
    row.status = "running"
    row.started_at = datetime.now() - timedelta(hours=1)
    row.heartbeat_at = datetime.now() - timedelta(hours=1)
    claim.commit()
    claim.close()

    # 直接调进度回调验证落行与心跳（execute 的认领路径已由
    # O-01 的测试覆盖，这里只钉 window 事件的落行口径）。
    handler = jobs._make_progress_handler(view["task_id"])
    handler({"type": "window", "library_id": "lib-9",
             "library_name": "大库", "window_index": 3,
             "fetched": 12000, "paired": 9000,
             "unpaired_reasons": {"identity_no_local_match": 3000}})
    check = db_factory()
    try:
        row = (check.query(em.UpstreamAggregateTask)
               .filter(em.UpstreamAggregateTask.task_id == view["task_id"])
               .one())
        assert row.current_library_id == "lib-9"
        assert row.current_library_name == "大库"
        assert row.fetched == 12000
        assert row.paired == 9000
        assert json.loads(row.unpaired_reasons_json) == {
            "identity_no_local_match": 3000}
        assert row.heartbeat_at > datetime.now() - timedelta(minutes=1)
        assert jobs.mark_stale_interrupted(check) == 0
    finally:
        check.close()
    snap = jobs.get_task(source.id, view["task_id"])
    assert snap["unpaired_reasons"] == {"identity_no_local_match": 3000}
    assert snap["current_library_id"] == "lib-9"

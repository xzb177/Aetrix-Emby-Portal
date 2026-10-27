"""手动聚合从同步长请求改成后台任务 + 查询。

钉死的行为：
- POST /aggregate 在聚合真正跑起来之前就返回 task_id + queued，
  不把 run_manual_aggregation 堵在请求线程里（axios 30 秒超时会把
  这种长请求说成「网络暂时连不上」）；
- GET 按任务 id 查到完成，result 带原先那份摘要
  （libraries_discovered / fetched / paired / written / failed_libraries）；
- 源未启用、苹果 CMS、凭据失效、上游不可用、额度、映射冲突都还在，
  状态码和中文 detail 不改写、不吞掉；
- 苹果 CMS 仍不走这条聚合口。
"""

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import admin_upstream_sources as admin_ups
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_jobs as jobs
from backend.emby_server import upstream_aggregate_run as uar


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture()
def staff():
    return SimpleNamespace(id=7, is_staff=True, username="admin")


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs.reset_for_tests()
    yield
    jobs.reset_for_tests()


def _source(db, *, enabled=True, source_type="emby", name="src-1"):
    row = em.UpstreamSource(
        name=name, source_type=source_type,
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=enabled,
        credential_scope="source", health_status="unknown")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _summary(source, **overrides):
    result = {
        "source_id": source.id,
        "fetched": 4,
        "aggregates": 3,
        "pairs": [{"upstream_item_id": "u1"}],
        "paired": 2,
        "written": 1,
        "mapping_enabled": False,
        "unpaired_reasons": {},
        "libraries": [{
            "library_id": "lib-1", "library_name": "电影",
            "fetched": 4, "aggregates": 3, "paired": 2, "written": 1,
            "status": "ok",
        }],
        "libraries_discovered": 2,
        "failed_libraries": [{
            "library_id": "lib-2", "library_name": "剧集",
            "code": "upstream_unavailable", "message": "上游服务当前不可用，请稍后再试",
        }],
    }
    result.update(overrides)
    return result


def _hold_submit(monkeypatch):
    started = []
    monkeypatch.setattr(jobs, "_submit", lambda task_id: started.append(task_id))
    return started


def test_post_returns_queued_before_aggregation_runs(
        db, db_factory, staff, monkeypatch):
    """请求线程不等聚合跑完。卡住的聚合期间状态是 running、没有摘要。"""
    source = _source(db, enabled=True)
    entered = threading.Event()
    release = threading.Event()

    def slow(db_session, src, page_size=100):
        entered.set()
        assert release.wait(5)
        return _summary(src)

    monkeypatch.setattr(jobs, "_session_factory", db_factory)
    monkeypatch.setattr(jobs, "_aggregate_fn", slow)
    started = _hold_submit(monkeypatch)

    t0 = time.monotonic()
    resp = admin_ups.aggregate_upstream_source(
        source.id, None, staff=staff, db=db)
    assert time.monotonic() - t0 < 1
    assert resp["status"] == "queued"
    assert resp["task_id"]
    assert resp["source_id"] == source.id
    assert resp["result"] is None
    assert resp["error"] is None
    assert started == [resp["task_id"]]
    assert not entered.is_set()

    db.close()
    thread = threading.Thread(
        target=jobs.execute_task, args=(resp["task_id"],), daemon=True)
    thread.start()
    assert entered.wait(2)
    mid = admin_ups.get_upstream_aggregate_task(
        source.id, resp["task_id"], staff=staff)
    assert mid["status"] == "running"
    assert mid["result"] is None
    assert mid["error"] is None

    release.set()
    thread.join(3)
    assert not thread.is_alive()
    snap = admin_ups.get_upstream_aggregate_task(
        source.id, resp["task_id"], staff=staff)
    assert snap["status"] == "succeeded"
    assert snap["error"] is None
    result = snap["result"]
    assert result["libraries_discovered"] == 2
    assert result["fetched"] == 4
    assert result["paired"] == 2
    assert result["written"] == 1
    assert result["aggregates"] == 3
    assert result["mapping_enabled"] is False
    assert result["failed_libraries"][0]["library_id"] == "lib-2"
    assert result["libraries"][0]["status"] == "ok"
    assert result["pairs"][0]["upstream_item_id"] == "u1"


def test_success_commits_worker_session_once(db, db_factory, staff, monkeypatch):
    source = _source(db, enabled=True)
    commits = {"n": 0}

    def factory():
        session = db_factory()
        real_commit = session.commit

        def wrapped(*args, **kwargs):
            commits["n"] += 1
            return real_commit(*args, **kwargs)

        session.commit = wrapped
        return session

    seen = {}

    def record(db_session, src, page_size=100):
        seen["page_size"] = page_size
        return _summary(src)

    monkeypatch.setattr(jobs, "_session_factory", factory)
    monkeypatch.setattr(jobs, "_aggregate_fn", record)
    _hold_submit(monkeypatch)
    resp = admin_ups.aggregate_upstream_source(
        source.id, admin_ups.UpstreamAggregateRequest(page_size=50),
        staff=staff, db=db)
    db.close()
    jobs.execute_task(resp["task_id"])
    jobs.execute_task(resp["task_id"])
    assert seen["page_size"] == 50
    assert commits["n"] == 1


def test_missing_disabled_and_applecms_still_reject_immediately(
        db, staff, monkeypatch):
    started = _hold_submit(monkeypatch)

    with pytest.raises(HTTPException) as missing:
        admin_ups.aggregate_upstream_source(999999, None, staff=staff, db=db)
    assert missing.value.status_code == 404
    assert missing.value.detail == "上游源不存在"

    off = _source(db, name="off", enabled=False)
    with pytest.raises(HTTPException) as disabled:
        admin_ups.aggregate_upstream_source(off.id, None, staff=staff, db=db)
    assert disabled.value.status_code == 400
    assert disabled.value.detail == "上游源未启用，先启用再聚合"

    cms = _source(db, name="cms", source_type="applecms", enabled=True)
    with pytest.raises(HTTPException) as apple:
        admin_ups.aggregate_upstream_source(cms.id, None, staff=staff, db=db)
    assert apple.value.status_code == 400
    assert apple.value.detail == "苹果 CMS 源走自己的同步，不走 Emby 目录聚合"
    assert started == []


def test_worker_preserves_real_run_errors(db, db_factory, monkeypatch):
    """入队后源被停用 / 苹果 CMS：后台仍响亮失败，不静默跑、不改文案。"""
    monkeypatch.setattr(jobs, "_submit", lambda task_id: None)
    monkeypatch.setattr(jobs, "_session_factory", db_factory)

    off = _source(db, name="off", enabled=False)
    off_id = off.id
    queued = jobs.enqueue(off_id, 100)
    db.close()
    jobs.execute_task(queued["task_id"])
    snap = jobs.get_task(off_id, queued["task_id"])
    assert snap["status"] == "failed"
    assert snap["result"] is None
    assert snap["error"] == {
        "status": 400,
        "code": "source_disabled",
        "detail": "上游源未启用，先启用再聚合",
    }

    # 上一条已终态，同 id 不会复用；苹果 CMS 走类型拒绝而不是未启用。
    cms = em.UpstreamSource(
        name="cms", source_type="applecms",
        base_url="https://cms.example.com", auth_method="none",
        enabled=True, credential_scope="source", health_status="unknown")
    session = db_factory()
    session.add(cms)
    session.commit()
    cms_id = cms.id
    session.close()
    queued_cms = jobs.enqueue(cms_id, 100)
    assert queued_cms["task_id"] != queued["task_id"]
    jobs.execute_task(queued_cms["task_id"])
    cms_snap = jobs.get_task(cms_id, queued_cms["task_id"])
    assert cms_snap["error"]["status"] == 400
    assert cms_snap["error"]["code"] == "source_type_not_supported"
    assert cms_snap["error"]["detail"] == "苹果 CMS 源走自己的同步，不走 Emby 目录聚合"


@pytest.mark.parametrize("status,code,detail", [
    (409, "upstream_credential_invalid", "上游凭据失效，请管理员重录该源凭据"),
    (502, "upstream_unavailable", "上游服务当前不可用，请稍后再试"),
    (429, "upstream_quota_exhausted", "上游额度已耗尽，请稍后再试或换个源"),
    (502, "upstream_frontend_blocked",
     "上游前门（nginx）拦截，未到 Emby：请对方放行本方出口 IP "
     "15.204.95.101，重录凭据无效"),
])
def test_upstream_errors_stay_on_the_task(
        db, db_factory, staff, monkeypatch, status, code, detail):
    source = _source(db, enabled=True, name=f"src-{code}")

    def boom(db_session, src, page_size=100):
        raise uar.UpstreamAggregateRunError(status, code, detail)

    monkeypatch.setattr(jobs, "_session_factory", db_factory)
    monkeypatch.setattr(jobs, "_aggregate_fn", boom)
    _hold_submit(monkeypatch)
    resp = admin_ups.aggregate_upstream_source(
        source.id, None, staff=staff, db=db)
    db.close()
    jobs.execute_task(resp["task_id"])
    snap = admin_ups.get_upstream_aggregate_task(
        source.id, resp["task_id"], staff=staff)
    assert snap["status"] == "failed"
    assert snap["result"] is None
    assert snap["error"] == {"status": status, "code": code, "detail": detail}


def test_mapping_conflict_and_unexpected_error_are_not_swallowed(
        db, db_factory, staff, monkeypatch):
    source = _source(db, enabled=True)
    monkeypatch.setattr(jobs, "_session_factory", db_factory)
    _hold_submit(monkeypatch)

    def conflict(db_session, src, page_size=100):
        raise IntegrityError("INSERT", {}, Exception("dup"))

    monkeypatch.setattr(jobs, "_aggregate_fn", conflict)
    resp = admin_ups.aggregate_upstream_source(
        source.id, None, staff=staff, db=db)
    db.close()
    jobs.execute_task(resp["task_id"])
    snap = jobs.get_task(source.id, resp["task_id"])
    assert snap["error"] == {
        "status": 409,
        "code": "mapping_conflict",
        "detail": "映射写入冲突，请重试",
    }

    def explode(db_session, src, page_size=100):
        raise RuntimeError("token=s3cret-pw")

    monkeypatch.setattr(jobs, "_aggregate_fn", explode)
    again = jobs.enqueue(source.id, 100)
    assert again["task_id"] != resp["task_id"]
    jobs.execute_task(again["task_id"])
    failed = jobs.get_task(source.id, again["task_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["status"] == 500
    assert failed["error"]["code"] == "aggregate_failed"
    assert failed["error"]["detail"] == "聚合失败，请稍后重试"
    assert "s3cret" not in str(failed)


def test_get_unknown_task_is_404_and_inflight_is_reused(db_factory, staff, monkeypatch):
    monkeypatch.setattr(jobs, "_submit", lambda task_id: None)
    with pytest.raises(HTTPException) as missing:
        admin_ups.get_upstream_aggregate_task(1, "no-such-task", staff=staff)
    assert missing.value.status_code == 404
    assert missing.value.detail == "聚合任务不存在或已过期"

    first = jobs.enqueue(1, 100)
    again = jobs.enqueue(1, 100)
    assert again["task_id"] == first["task_id"]
    assert again["status"] == "queued"
    with pytest.raises(HTTPException) as other_source:
        admin_ups.get_upstream_aggregate_task(
            2, first["task_id"], staff=staff)
    assert other_source.value.status_code == 404

    monkeypatch.setattr(jobs, "_session_factory", db_factory)
    jobs.execute_task(first["task_id"])
    done = jobs.get_task(1, first["task_id"])
    assert done["status"] == "failed"
    assert done["error"]["status"] == 404
    assert done["error"]["detail"] == "上游源不存在"
    nxt = jobs.enqueue(1, 100)
    assert nxt["task_id"] != first["task_id"]
    assert nxt["status"] == "queued"


def test_scheduler_still_calls_run_manual_aggregation_directly():
    """调度不改走任务表。"""
    import inspect

    from backend.emby_server import upstream_aggregate_scheduler as sched

    src = inspect.getsource(sched.run_scheduled_aggregation)
    assert "run_manual_aggregation" in src
    assert "enqueue" not in src
    assert "upstream_aggregate_jobs" not in src

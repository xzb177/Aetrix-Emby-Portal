"""PA-024/025/026 回归：求片审核终态守卫、推送状态守卫、商店登记并发双建。

只读/隔离：内存 SQLite（PA-024/025）与临时文件 SQLite（PA-026 双线程，
各线程独立 session）；不碰生产库、不调真实外部服务（推送用假替身）。
"""

import asyncio
import os
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import media_seek, models  # noqa: E402
from backend.api import admin as admin_api  # noqa: E402
from backend.api.store_requests import post_request  # noqa: E402
from backend.emby_server.store_requests_contract import RequestIn  # noqa: E402
from backend.integrations import store  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


@pytest.fixture()
def file_db(tmp_path):
    """文件库：推送端点把同步查询下放线程池，内存库连接跨不了线程。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'seek.db'}",
        connect_args={"check_same_thread": False},
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()
    engine.dispose()


def _user(db, username):
    user = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _request(db, user, *, status="pending", tmdb_id=None):
    row = models.MovieRequest(
        user_id=user.id, movie_name="星际穿越", type="movie",
        status=status, tmdb_id=tmdb_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ==================== PA-024：审核入口终态守卫 ====================

def test_review_completed_cannot_flip_back_to_approved(db):
    admin = _user(db, "admin")
    row = _request(db, _user(db, "u1"), status="completed")
    with pytest.raises(HTTPException) as exc:
        admin_api._review_media_seek_sync(db, admin.id, row.id, "approved", "")
    assert exc.value.status_code == 400
    db.refresh(row)
    assert row.status == "completed"


def test_review_withdrawn_cannot_flip_back(db):
    admin = _user(db, "admin")
    row = _request(db, _user(db, "u1"), status="withdrawn")
    with pytest.raises(HTTPException) as exc:
        admin_api._review_media_seek_sync(db, admin.id, row.id, "rejected", "理由")
    assert exc.value.status_code == 400
    db.refresh(row)
    assert row.status == "withdrawn"


def test_review_active_states_still_flow(db):
    admin = _user(db, "admin")
    row = _request(db, _user(db, "u1"), status="pending")
    admin_api._review_media_seek_sync(db, admin.id, row.id, "approved", "")
    db.refresh(row)
    assert row.status == "approved"
    admin_api._review_media_seek_sync(db, admin.id, row.id, "rejected", "画质不达标")
    db.refresh(row)
    assert row.status == "rejected"


# ==================== PA-025：推送入口状态守卫 ====================

class _FakePusher:
    def __init__(self):
        self.calls = []

    async def __call__(self, db, request, target, link=""):
        self.calls.append((request.id, target))
        return {"ok": True, "target": target, "server": "mp", "message": ""}


@pytest.mark.parametrize("status", ["withdrawn", "rejected", "completed"])
def test_push_terminal_state_refused_without_external_call(file_db, monkeypatch, status):
    db = file_db
    admin = _user(db, "admin")
    row = _request(db, _user(db, "u1"), status=status)
    fake = _FakePusher()
    monkeypatch.setattr(admin_api.servers, "push_media_seek", fake)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_api.push_media_seek(
            row.id, admin_api.MediaSeekPushRequest(target="moviepilot"),
            admin, db,
        ))
    assert exc.value.status_code in (400, 409)
    assert fake.calls == [], "终态求片推送必须在任何外部调用之前拦住"
    db.refresh(row)
    assert row.status == status and row.push_status is None


def test_push_pending_still_flows(file_db, monkeypatch):
    db = file_db
    admin = _user(db, "admin")
    row = _request(db, _user(db, "u1"), status="pending")
    fake = _FakePusher()
    monkeypatch.setattr(admin_api.servers, "push_media_seek", fake)
    result = asyncio.run(admin_api.push_media_seek(
        row.id, admin_api.MediaSeekPushRequest(target="moviepilot"),
        admin, db,
    ))
    assert result["success"] is True
    assert fake.calls == [(row.id, "moviepilot")]
    db.refresh(row)
    assert row.status == "approved" and row.push_status == "ok"


# ==================== PA-026：商店登记并发双建 ====================

def test_concurrent_same_tmdb_registers_single_row(tmp_path):
    from datetime import datetime, timedelta

    from backend import subscriptions

    db_path = tmp_path / "r1-concurrent.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    models.Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    previous = subscriptions._realm_resolver
    subscriptions.set_process_realm_resolver(None)
    try:
        setup = maker()
        user_ids = []
        for name in ("first", "second"):
            user = models.WebUser(username=name, password_hash="x", is_active=True)
            setup.add(user)
            setup.flush()
            plan = models.SubscriptionPlan(
                name="月卡", price=1, duration_days=30, is_active=True)
            setup.add(plan)
            setup.flush()
            now = datetime.now()
            setup.add(models.UserSubscription(
                user_id=user.id, plan_id=plan.id, realm_id=None,
                start_date=now, end_date=now + timedelta(days=30),
                status="active",
            ))
            user_ids.append(user.id)
        setup.commit()
        setup.close()

        barrier = threading.Barrier(2)
        errors = []

        def _register(uid):
            session = maker()
            try:
                barrier.wait(timeout=10)
                user = session.get(models.WebUser, uid)
                post_request(RequestIn(tmdb_id="999", title="片"), user, session)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=_register, args=(uid,))
                   for uid in user_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []

        check = maker()
        rows = (check.query(models.MovieRequest)
                .filter(models.MovieRequest.tmdb_id == "999",
                        models.MovieRequest.status.in_(
                            media_seek.ACTIVE_STATUSES))
                .all())
        assert len(rows) == 1, "同一 tmdb 并发登记只能剩 1 条处理中行"
        votes = (check.query(models.MovieRequestVote)
                 .filter(models.MovieRequestVote.request_id == rows[0].id)
                 .count())
        assert votes == 2, "两人的票应落在同一行上"
        check.close()
    finally:
        subscriptions.set_process_realm_resolver(previous)
        engine.dispose()

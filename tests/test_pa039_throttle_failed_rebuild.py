"""PA-039：找源确认节流分支遇终态失败单须重建、不谎报 already_queued。

不连生产库。复用 test_source_discovery.py 同款夹具形态（内存 SQLite +
TestClient + 占位绑定行），只覆盖确认分支的三态：

- 窗内唯一任务已 failed → 再确认不回 already_queued、可新建 pending 单；
- 窗内有在途 pending 单、另有一行更新的 failed 单 → 仍回 already_queued，
  且指着在途单（不指着死单）；
- 窗内唯一任务已 done → 仍回 already_queued（done 不重建，不扩范围）。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.source_discovery import router
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(engine):
    application = FastAPI()
    application.include_router(router)
    sessions = sessionmaker(bind=engine)

    def _override_get_db():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = _override_get_db
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


def _user(db, username):
    user = models.WebUser(
        username=username, password_hash="x", is_active=True, is_staff=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _bind(db, user, provider):
    cred = em.MountCredential(scope="user", owner_user_id=user.id,
                              sealed_credential="x", key_id="k")
    db.add(cred)
    db.commit()
    db.add(em.UserStorageBinding(
        user_id=user.id, provider=provider, account_label="盘",
        credential_id=cred.id, status="active",
    ))
    db.commit()


def _drive_item(db, tmdb_id):
    mount = em.StorageMount(name=f"pa039-mount-{tmdb_id}", mount_type="drive",
                            path="drive:shared-drive-id")
    db.add(mount)
    db.commit()
    library = em.Library(guid=f"lib-pa039-{tmdb_id}", name="找源库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-pa039-{tmdb_id}",
        library_id=library.id,
        item_type="movie",
        name="已在库",
        tmdb_id=tmdb_id,
        file_path=f"mount://{mount.id}/x.mp4",
        video_resolution="1080p",
        container="mkv",
        size=123456789,
        subtitle_languages="zh",
    )
    db.add(item)
    db.commit()
    return item


def _source(db, name):
    src = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://example.invalid",
        auth_method="api_key", enabled=True,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def _mapping(db, item, source, upstream_id):
    row = em.UpstreamItemMapping(
        local_item_id=item.id, source_id=source.id,
        upstream_item_id=upstream_id, confidence=0.9, evidence="tmdb",
    )
    db.add(row)
    db.commit()
    return row


def _as(client, user):
    client.app.dependency_overrides[get_current_user] = lambda: user
    return client


def _jobs(engine):
    session = sessionmaker(bind=engine)()
    try:
        return session.query(em.TransferJob).order_by(em.TransferJob.id).all()
    finally:
        session.close()


def _setup(client, db, monkeypatch, username, tmdb_id):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, username)
    _bind(db, user, "drive")
    item = _drive_item(db, tmdb_id)
    src = _source(db, "云海服")
    _mapping(db, item, src, f"up-{tmdb_id}")
    payload = {"local_item_id": item.id, "source_id": src.id,
               "upstream_item_id": f"up-{tmdb_id}"}
    return user, item, _as(client, user), payload


def _set_status(db, job_id, status):
    job = db.query(em.TransferJob).filter(em.TransferJob.id == job_id).first()
    job.status = status
    if status == "failed":
        job.error_kind = "dead"
    db.commit()


def test_confirm_after_failed_rebuilds(client, db, engine, monkeypatch):
    user, item, c, payload = _setup(client, db, monkeypatch, "u-pa039-a", "201")
    first = c.post("/api/user/source-discovery/confirm", json=payload)
    assert first.json()["state"] == "queued"
    first_id = first.json()["job_id"]
    _set_status(db, first_id, "failed")
    second = c.post("/api/user/source-discovery/confirm", json=payload)
    body = second.json()
    assert body["state"] == "queued"
    assert body["job_id"] != first_id
    jobs = _jobs(engine)
    assert len(jobs) == 2
    assert jobs[1].status == "pending"


def test_confirm_with_live_pending_plus_newer_failed_still_queued(
        client, db, engine, monkeypatch):
    user, item, c, payload = _setup(client, db, monkeypatch, "u-pa039-b", "202")
    first = c.post("/api/user/source-discovery/confirm", json=payload)
    assert first.json()["state"] == "queued"
    first_id = first.json()["job_id"]
    db.add(em.TransferJob(
        item_id=item.id, trigger="source_discovery", provider="drive",
        owner_scope="user", owner_ref=str(user.id),
        dest_drive_id=f"userdrive:{user.id}", status="failed",
        error_kind="dead",
    ))
    db.commit()
    second = c.post("/api/user/source-discovery/confirm", json=payload)
    body = second.json()
    assert body["state"] == "already_queued"
    assert body["job_id"] == first_id
    assert len(_jobs(engine)) == 2


def test_confirm_after_done_still_queued(client, db, engine, monkeypatch):
    user, item, c, payload = _setup(client, db, monkeypatch, "u-pa039-c", "203")
    first = c.post("/api/user/source-discovery/confirm", json=payload)
    assert first.json()["state"] == "queued"
    first_id = first.json()["job_id"]
    _set_status(db, first_id, "done")
    second = c.post("/api/user/source-discovery/confirm", json=payload)
    body = second.json()
    assert body["state"] == "already_queued"
    assert body["job_id"] == first_id
    assert len(_jobs(engine)) == 1

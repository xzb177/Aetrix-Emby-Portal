"""PA-039（查询层口径）：节流窗与最新单查询只认有效状态。

接在已合入的确认分支分流（test_pa039_throttle_failed_rebuild.py）
之后，根因仍在查询层：_recent_user_jobs 不滤状态、
_latest_job 不限状态。本文件钉住查询层口径：

- 窗内唯一任务已 failed → 节流判 clear（不挡重建）、不耗豁免；
- 最新单查询（有效状态）遇 failed 单 → 不指死单；
- 真正在途 pending / 已成 done 单 → 节流口径不变（不许把节流修没）；
- 确认端到端：failed 单后重确认回 queued（不回 already_queued 指死单），
  在途 pending + 更新 failed 共存时仍回 already_queued 指着在途单。

不连生产库。查询层用内存 SQLite 单表；确认层复用
test_pa039_throttle_failed_rebuild.py 同款夹具形态。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

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
from backend.emby_server import transfer_variant as tv

VALID = ("pending", "running", "done")


def _tiny_db():
    engine = create_engine("sqlite://")
    em.TransferJob.__table__.create(engine)
    return sessionmaker(bind=engine)()


def _add(db, item_id, status, hours_ago=1, dest="userdrive:7"):
    db.add(em.TransferJob(
        item_id=item_id, trigger="playback", provider="drive",
        owner_scope="user", owner_ref="7", dest_drive_id=dest,
        status=status, created_at=datetime.now() - timedelta(hours=hours_ago)))
    db.commit()


def test_failed_only_throttle_is_clear():
    db = _tiny_db()
    _add(db, 42, "failed")
    state, from_scheme = tv._user_throttle_state(db, 42, 7, "userdrive:7")
    assert state == "clear"
    assert from_scheme == ""
    # 不抛节流异常：重建一路畅通
    use_waiver, _ = tv._raise_if_user_throttled(db, 42, 7, "userdrive:7")
    assert use_waiver is False


def test_failed_only_latest_valid_is_none_but_default_unchanged():
    db = _tiny_db()
    _add(db, 42, "failed")
    assert tv._latest_job(db, 42, "user", "7", statuses=VALID) is None
    # 默认不限状态：首播门禁/next302/商店仍能看到 failed 行做各自的分流
    assert tv._latest_job(db, 42, "user", "7").status == "failed"


def test_live_plus_failed_latest_valid_points_at_live():
    db = _tiny_db()
    _add(db, 42, "pending", hours_ago=2)
    _add(db, 42, "failed", hours_ago=1)
    latest = tv._latest_job(db, 42, "user", "7", statuses=VALID)
    assert latest is not None and latest.status == "pending"


def test_pending_and_done_still_throttle():
    db = _tiny_db()
    _add(db, 42, "pending")
    state, _ = tv._user_throttle_state(db, 42, 7, "userdrive:7")
    assert state == "blocked"
    with pytest.raises(tv.TransferError) as excinfo:
        tv._raise_if_user_throttled(db, 42, 7, "userdrive:7")
    assert excinfo.value.kind == "throttled"

    db = _tiny_db()
    _add(db, 42, "done")
    state, _ = tv._user_throttle_state(db, 42, 7, "userdrive:7")
    assert state == "blocked"


def test_failed_does_not_burn_rebind_waiver():
    # 只有死单（另一方案）时不走豁免：直接 clear，不耗豁免额度。
    db = _tiny_db()
    _add(db, 42, "failed", dest="pan115:9")
    state, _ = tv._user_throttle_state(db, 42, 7, "userdrive:7")
    assert state == "clear"


# ---------------- 确认端到端（复用已验证夹具形态） ----------------


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
    mount = em.StorageMount(name=f"pa039f-mount-{tmdb_id}", mount_type="drive",
                            path="drive:shared-drive-id")
    db.add(mount)
    db.commit()
    library = em.Library(guid=f"lib-pa039f-{tmdb_id}", name="找源库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-pa039f-{tmdb_id}",
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


def _setup(client, db, monkeypatch, username, tmdb_id):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, username)
    _bind(db, user, "drive")
    item = _drive_item(db, tmdb_id)
    src = _source(db, "云海服")
    _mapping(db, item, src, f"up-{tmdb_id}")
    client.app.dependency_overrides[get_current_user] = lambda: user
    payload = {"local_item_id": item.id, "source_id": src.id,
               "upstream_item_id": f"up-{tmdb_id}"}
    return user, item, client, payload


def _set_status(db, job_id, status):
    job = db.query(em.TransferJob).filter(em.TransferJob.id == job_id).first()
    job.status = status
    if status == "failed":
        job.error_kind = "dead"
    db.commit()


def test_confirm_failed_only_returns_queued_never_already_queued(
        client, db, engine, monkeypatch):
    _user, _item, c, payload = _setup(
        client, db, monkeypatch, "u-pa039f-a", "301")
    first = c.post("/api/user/source-discovery/confirm", json=payload)
    assert first.json()["state"] == "queued"
    first_id = first.json()["job_id"]
    _set_status(db, first_id, "failed")
    # 死单后重确认：必须新建一单，绝不能 already_queued 指着死单
    body = c.post("/api/user/source-discovery/confirm", json=payload).json()
    assert body["state"] == "queued"
    assert body["job_id"] != first_id


def test_confirm_live_pending_plus_failed_still_queued_live(
        client, db, engine, monkeypatch):
    _user, _item, c, payload = _setup(
        client, db, monkeypatch, "u-pa039f-b", "302")
    first = c.post("/api/user/source-discovery/confirm", json=payload)
    assert first.json()["state"] == "queued"
    first_id = first.json()["job_id"]
    db.add(em.TransferJob(
        item_id=_item.id, trigger="playback", provider="drive",
        owner_scope="user", owner_ref=str(_user.id),
        dest_drive_id="userdrive:7", status="failed",
        error_kind="dead", created_at=datetime.now()))
    db.commit()
    body = c.post("/api/user/source-discovery/confirm", json=payload).json()
    assert body["state"] == "already_queued"
    assert body["job_id"] == first_id

"""想看商店：总闸、会员闸、R0 映射、真实入队。

不连生产库。总闸关时四个口都是 404（路由级依赖先于鉴权，未登录也是 404）。
总闸开时非会员想看 403；会员对未入库 tmdb 得到 missing_source，且不写转存任务。
会员对已入库 tmdb 点想看：provider 可判定且有对应生效绑定、转存总闸开时真建
用户盘转存任务（trigger=store_want）；建不了时按原因诚实回 failed，不假装入队。
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

from backend import models, subscriptions
from backend.api.store import router
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server.store_contract import (
    MISSING_SOURCE_MESSAGE,
    NO_BINDING_MESSAGE,
    NO_SOURCE_FID_MESSAGE,
    STORE_DISABLED_DETAIL,
    STORE_MEMBERSHIP_REQUIRED_DETAIL,
    TRANSFER_MASTER_OFF_MESSAGE,
    UNKNOWN_PROVIDER_MESSAGE,
    WANT_ALREADY_QUEUED_MESSAGE,
    WANT_QUEUED_MESSAGE,
)


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
    # 全量 pytest 里别的用例可能装过「本进程的服」。想看会员闸不按服过滤，
    # 这里清掉解析器，结束再装回去，避免订阅行被服条件滤掉。
    previous = subscriptions._realm_resolver
    subscriptions.set_process_realm_resolver(None)
    try:
        yield session
    finally:
        subscriptions.set_process_realm_resolver(previous)
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


def _subscribe(db, user):
    plan = models.SubscriptionPlan(name="月卡", price=1, duration_days=30, is_active=True)
    db.add(plan)
    db.commit()
    now = datetime.now()
    sub = models.UserSubscription(
        user_id=user.id,
        plan_id=plan.id,
        realm_id=None,
        start_date=now,
        end_date=now + timedelta(days=30),
        status="active",
    )
    db.add(sub)
    db.commit()
    return sub


def _bind(db, user, provider):
    """生效中的用户盘绑定行（凭据行只占位，不含真实凭据）。"""
    cred = em.MountCredential(scope="user", owner_user_id=user.id,
                              sealed_credential="x", key_id="k")
    db.add(cred)
    db.commit()
    db.add(em.UserStorageBinding(
        user_id=user.id, provider=provider, account_label="盘",
        credential_id=cred.id, status="active",
    ))
    db.commit()


def _drive_item(db, tmdb_id, file_path=None):
    """Drive 挂载库内条目：mount.path 带 drive: 前缀才取得到源盘标识。"""
    mount = em.StorageMount(name=f"store-mount-{tmdb_id}", mount_type="drive",
                            path="drive:shared-drive-id")
    db.add(mount)
    db.commit()
    library = em.Library(guid=f"lib-store-{tmdb_id}", name="商店库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-store-{tmdb_id}",
        library_id=library.id,
        item_type="movie",
        name="已在库",
        tmdb_id=tmdb_id,
        file_path=file_path or f"mount://{mount.id}/x.mp4",
    )
    db.add(item)
    db.commit()
    return item


def _pan115_item(db, tmdb_id):
    library = em.Library(guid=f"lib-store115-{tmdb_id}", name="商店115库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-store115-{tmdb_id}",
        library_id=library.id,
        item_type="movie",
        name="已在库115",
        tmdb_id=tmdb_id,
        file_path="115:/x.mp4",
        drive_file_id="fid1",
    )
    db.add(item)
    db.commit()
    return item


def _as(client, user):
    client.app.dependency_overrides[get_current_user] = lambda: user
    return client


def _jobs(engine) -> int:
    session = sessionmaker(bind=engine)()
    try:
        return session.query(em.TransferJob).count()
    finally:
        session.close()


def _job_rows(engine):
    session = sessionmaker(bind=engine)()
    try:
        return session.query(em.TransferJob).order_by(em.TransferJob.id).all()
    finally:
        session.close()


def test_flag_off_all_store_routes_404(client, monkeypatch):
    """总闸关：目录、详情、想看、清单都是 404，未登录也不会先 401。"""
    monkeypatch.setenv("STORE_ENABLED", "0")
    catalog = client.get("/api/user/store/catalog")
    item = client.get("/api/user/store/items/550")
    want = client.post("/api/user/store/want", json={"tmdb_id": "550"})
    wants = client.get("/api/user/store/wants")
    for response in (catalog, item, want, wants):
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == STORE_DISABLED_DETAIL


def test_flag_on_guest_browses_empty_catalog(client, monkeypatch):
    """总闸开：访客只读目录和详情；写口没有登录凭证则 401。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    catalog = client.get("/api/user/store/catalog")
    assert catalog.status_code == 200, catalog.text
    assert catalog.json() == {"items": [], "total": 0}
    item = client.get("/api/user/store/items/550")
    assert item.status_code == 200, item.text
    body = item.json()
    assert body["tmdb_id"] == "550"
    assert body["in_library"] is False
    assert body["library_item_id"] is None
    assert client.post("/api/user/store/want", json={"tmdb_id": "550"}).status_code == 401
    assert client.get("/api/user/store/wants").status_code == 401


def test_flag_on_non_member_want_403(client, db, engine, monkeypatch):
    """总闸开、已登录但没有有效订阅：想看和清单都是 403，不入队。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "guest-memberless")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "550"})
    assert want.status_code == 403, want.text
    assert want.json()["detail"] == STORE_MEMBERSHIP_REQUIRED_DETAIL
    wants = client.get("/api/user/store/wants")
    assert wants.status_code == 403, wants.text
    assert wants.json()["detail"] == STORE_MEMBERSHIP_REQUIRED_DETAIL
    assert _jobs(engine) == 0


def test_flag_on_member_unmapped_want_missing_source(client, db, engine, monkeypatch):
    """会员想看一个库里没有的 tmdb：missing_source，转存任务表仍为空。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    user = _user(db, "member")
    _subscribe(db, user)
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "424242"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "missing_source"
    assert body["tmdb_id"] == "424242"
    assert body["in_library"] is False
    assert body["library_item_id"] is None
    assert body["message"] == MISSING_SOURCE_MESSAGE
    assert _jobs(engine) == 0
    wants = client.get("/api/user/store/wants")
    assert wants.status_code == 200, wants.text
    assert wants.json() == {"items": []}


def test_flag_on_member_mapped_want_enqueues(client, db, engine, monkeypatch):
    """库内已有条目 + Drive 绑定 + 转存总闸开：真建任务，重复点不重复建。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "member-mapped")
    _subscribe(db, user)
    _drive_item(db, "603")
    _bind(db, user, "drive")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "603"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "wanted"
    assert body["in_library"] is True
    assert body["library_item_id"] is not None
    assert body["job_id"] is not None
    assert body["message"] == WANT_QUEUED_MESSAGE
    assert _jobs(engine) == 1
    job = _job_rows(engine)[0]
    assert job.trigger == "store_want"
    assert job.owner_scope == "user"
    assert job.owner_ref == str(user.id)
    assert job.status == "pending"
    detail = client.get("/api/user/store/items/603")
    assert detail.status_code == 200, detail.text
    assert detail.json()["in_library"] is True
    wants = client.get("/api/user/store/wants")
    assert wants.status_code == 200, wants.text
    assert len(wants.json()["items"]) == 1
    assert wants.json()["items"][0]["state"] == "wanted"
    assert wants.json()["items"][0]["tmdb_id"] == "603"
    again = client.post("/api/user/store/want", json={"tmdb_id": "603"})
    assert again.status_code == 200, again.text
    assert again.json()["state"] == "wanted"
    assert again.json()["message"] == WANT_ALREADY_QUEUED_MESSAGE
    assert again.json()["job_id"] == job.id
    assert _jobs(engine) == 1


def test_flag_on_member_mapped_want_no_binding_failed(client, db, engine, monkeypatch):
    """库内有条目但用户无对应绑定：不建注定失败的单，诚实回 failed。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "member-nobind")
    _subscribe(db, user)
    _drive_item(db, "604")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "604"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "failed"
    assert body["in_library"] is True
    assert body["job_id"] is None
    assert body["message"] == NO_BINDING_MESSAGE
    assert _jobs(engine) == 0


def test_flag_on_member_mapped_want_unknown_provider_failed(client, db, engine, monkeypatch):
    """条目路径不在门禁认识的形状里：判定不出 provider，不建任务。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "member-local")
    _subscribe(db, user)
    _drive_item(db, "605", file_path="/media/local/x.mp4")
    _bind(db, user, "drive")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "605"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "failed"
    assert body["message"] == UNKNOWN_PROVIDER_MESSAGE
    assert _jobs(engine) == 0


def test_flag_on_member_mapped_want_master_off_failed(client, db, engine, monkeypatch):
    """转存总闸未开：绑定齐了也不入队，明说总闸未开。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "0")
    user = _user(db, "member-masteroff")
    _subscribe(db, user)
    _drive_item(db, "606")
    _bind(db, user, "drive")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "606"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "failed"
    assert body["message"] == TRANSFER_MASTER_OFF_MESSAGE
    assert _jobs(engine) == 0


def test_flag_on_member_pan115_want_enqueues(client, db, engine, monkeypatch):
    """115 条目 + 115 绑定：走 pan115 分支真建任务。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "member-115")
    _subscribe(db, user)
    _pan115_item(db, "607")
    _bind(db, user, "pan115")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "607"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "wanted"
    assert body["job_id"] is not None
    job = _job_rows(engine)[0]
    assert job.provider == "pan115"
    assert job.dest_drive_id == f"pan115:user-{user.id}"


def test_flag_on_member_pan115_want_no_fid_failed(client, db, engine, monkeypatch):
    """115 条目无源 fid：无从 copy，不建任务。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "member-115-nofid")
    _subscribe(db, user)
    item = _pan115_item(db, "608")
    item.drive_file_id = ""
    db.commit()
    _bind(db, user, "pan115")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "608"})
    assert want.status_code == 200, want.text
    body = want.json()
    assert body["state"] == "failed"
    assert body["message"] == NO_SOURCE_FID_MESSAGE
    assert _jobs(engine) == 0


def test_wants_list_maps_job_status(client, db, engine, monkeypatch):
    """清单按任务行 status 映射：done→ready、failed→failed。"""
    monkeypatch.setenv("STORE_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "member-status")
    _subscribe(db, user)
    _drive_item(db, "609")
    _bind(db, user, "drive")
    _as(client, user)
    want = client.post("/api/user/store/want", json={"tmdb_id": "609"})
    assert want.json()["state"] == "wanted"
    session = sessionmaker(bind=engine)()
    try:
        job = session.query(em.TransferJob).first()
        job.status = "done"
        session.commit()
    finally:
        session.close()
    wants = client.get("/api/user/store/wants")
    assert wants.json()["items"][0]["state"] == "ready"
    session = sessionmaker(bind=engine)()
    try:
        job = session.query(em.TransferJob).first()
        job.status = "failed"
        session.commit()
    finally:
        session.close()
    wants = client.get("/api/user/store/wants")
    assert wants.json()["items"][0]["state"] == "failed"

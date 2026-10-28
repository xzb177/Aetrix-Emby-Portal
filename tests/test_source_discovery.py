"""找源通用能力：总闸、分源开关、候选、确认入队。

不连生产库。总闸关时两个口都是 404（路由级依赖先于鉴权，未登录也是 404）。
总闸开时：搜索只读不建任务；确认复用既有转存链路，建不了按原因诚实回
failed；自动模式默认关，确认必须指定候选三元组。
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
from backend.emby_server.source_discovery.contract import (
    DISABLED_DETAIL,
    NO_BINDING_MESSAGE,
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


def _drive_item(db, tmdb_id, name="已在库"):
    """Drive 挂载库内条目：mount.path 带 drive: 前缀才取得到源盘标识。"""
    mount = em.StorageMount(name=f"disc-mount-{tmdb_id}", mount_type="drive",
                            path="drive:shared-drive-id")
    db.add(mount)
    db.commit()
    library = em.Library(guid=f"lib-disc-{tmdb_id}", name="找源库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-disc-{tmdb_id}",
        library_id=library.id,
        item_type="movie",
        name=name,
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


def _source(db, name, enabled=True):
    src = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://example.invalid",
        auth_method="api_key", enabled=enabled,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def _mapping(db, item, source, upstream_id, confidence=0.9, evidence="tmdb"):
    row = em.UpstreamItemMapping(
        local_item_id=item.id, source_id=source.id,
        upstream_item_id=upstream_id, confidence=confidence,
        evidence=evidence,
    )
    db.add(row)
    db.commit()
    return row


def _as(client, user):
    client.app.dependency_overrides[get_current_user] = lambda: user
    return client


def _jobs(engine) -> int:
    session = sessionmaker(bind=engine)()
    try:
        return session.query(em.TransferJob).count()
    finally:
        session.close()


def test_gate_off_search_and_confirm_404(client, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "0")
    assert client.get("/api/user/source-discovery/search",
                      params={"tmdb_id": "1"}).status_code == 404
    assert client.post("/api/user/source-discovery/confirm",
                       json={"tmdb_id": "1"}).status_code == 404


def test_search_and_confirm_require_auth(client, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    assert client.get("/api/user/source-discovery/search",
                      params={"tmdb_id": "1"}).status_code == 401
    assert client.post("/api/user/source-discovery/confirm",
                       json={"tmdb_id": "1"}).status_code == 401


def test_gate_on_empty_query_400(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-empty")
    resp = _as(client, user).get("/api/user/source-discovery/search")
    assert resp.status_code == 400


def test_tmdb_match_returns_candidate(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-tmdb")
    item = _drive_item(db, "100")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-1", confidence=0.9)
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "100"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    cand = body["candidates"][0]
    assert cand["adapter"] == "upstream_mapping"
    assert cand["local_item_id"] == item.id
    assert cand["source_name"] == "云海服"
    assert cand["upstream_item_id"] == "up-1"
    assert cand["confidence"] == 0.9
    assert cand["resolution"] == "1080p"
    assert cand["container"] == "mkv"
    assert cand["size_bytes"] == 123456789
    assert cand["subtitle_languages"] == "zh"
    assert cand["playable_now"] is False
    assert cand["acquisition"] == "transfer_after_confirm"
    assert "base_url" not in cand


def test_title_normalized_match_and_miss(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-title")
    item = _drive_item(db, "101", name="The Matrix")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-2")
    c = _as(client, user)
    hit = c.get("/api/user/source-discovery/search",
                params={"title": "  the MATRIX  "})
    assert hit.status_code == 200
    assert hit.json()["total"] == 1
    miss = c.get("/api/user/source-discovery/search",
                 params={"title": "不存在的片名"})
    assert miss.status_code == 200
    assert miss.json()["total"] == 0


def test_imdb_match(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-imdb")
    item = _drive_item(db, "102")
    item.imdb_id = "tt0133093"
    db.commit()
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-3")
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"imdb_id": "tt0133093"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_disabled_source_gives_no_candidates(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-dis")
    item = _drive_item(db, "103")
    src = _source(db, "云海服", enabled=False)
    _mapping(db, item, src, "up-4")
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "103"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_dead_mapping_skipped(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-dead")
    item = _drive_item(db, "104")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-5")
    db.delete(src)
    db.commit()
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "104"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_candidates_sorted_by_confidence(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-sort")
    item = _drive_item(db, "105")
    low = _source(db, "源低")
    high = _source(db, "源高")
    _mapping(db, item, low, "up-low", confidence=0.5)
    _mapping(db, item, high, "up-high", confidence=0.95)
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "105"})
    names = [c["source_name"] for c in resp.json()["candidates"]]
    assert names == ["源高", "源低"]


def test_search_creates_no_jobs(client, db, engine, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-nojob")
    item = _drive_item(db, "106")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-6")
    _as(client, user).get("/api/user/source-discovery/search",
                          params={"tmdb_id": "106"})
    assert _jobs(engine) == 0


def test_per_source_switch_off_gives_no_candidates(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("SOURCE_DISCOVERY_UPSTREAM_MAPPING_ENABLED", "0")
    user = _user(db, "u-switch")
    item = _drive_item(db, "107")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-7")
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "107"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_adapters_list_without_mapping_gives_no_candidates(
        client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("SOURCE_DISCOVERY_ADAPTERS", "nonexistent_adapter")
    user = _user(db, "u-list")
    item = _drive_item(db, "108")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-8")
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "108"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0
    assert resp.json()["adapters"] == []


def test_confirm_queues_transfer_job(client, db, engine, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "u-confirm")
    _bind(db, user, "drive")
    item = _drive_item(db, "109")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-9")
    resp = _as(client, user).post(
        "/api/user/source-discovery/confirm",
        json={"local_item_id": item.id, "source_id": src.id,
              "upstream_item_id": "up-9"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "queued"
    assert body["job_id"] is not None
    assert _jobs(engine) == 1
    session = sessionmaker(bind=engine)()
    try:
        job = session.query(em.TransferJob).first()
        assert job.trigger == "source_discovery"
        assert job.owner_scope == "user"
        assert job.owner_ref == str(user.id)
        assert job.dest_drive_id == f"userdrive:{user.id}"
    finally:
        session.close()


def test_confirm_twice_second_is_already_queued(client, db, engine, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "u-twice")
    _bind(db, user, "drive")
    item = _drive_item(db, "110")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-10")
    c = _as(client, user)
    payload = {"local_item_id": item.id, "source_id": src.id,
               "upstream_item_id": "up-10"}
    assert c.post("/api/user/source-discovery/confirm",
                  json=payload).json()["state"] == "queued"
    second = c.post("/api/user/source-discovery/confirm", json=payload)
    assert second.json()["state"] == "already_queued"
    assert _jobs(engine) == 1


def test_confirm_without_binding_failed(client, db, engine, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    user = _user(db, "u-nobind")
    item = _drive_item(db, "111")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-11")
    resp = _as(client, user).post(
        "/api/user/source-discovery/confirm",
        json={"local_item_id": item.id, "source_id": src.id,
              "upstream_item_id": "up-11"})
    assert resp.json()["state"] == "failed"
    assert resp.json()["message"] == NO_BINDING_MESSAGE
    assert _jobs(engine) == 0


def test_confirm_unknown_triple_404(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-unknown")
    resp = _as(client, user).post(
        "/api/user/source-discovery/confirm",
        json={"local_item_id": 999999, "source_id": 999999,
              "upstream_item_id": "nope"})
    assert resp.status_code == 404


def test_confirm_auto_off_requires_triple(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("SOURCE_DISCOVERY_AUTO_TRANSFER_ENABLED", "0")
    user = _user(db, "u-triple")
    item = _drive_item(db, "112")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-12")
    resp = _as(client, user).post(
        "/api/user/source-discovery/confirm", json={"tmdb_id": "112"})
    assert resp.status_code == 400


def test_confirm_auto_on_picks_top_candidate(client, db, engine, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    monkeypatch.setenv("SOURCE_DISCOVERY_AUTO_TRANSFER_ENABLED", "1")
    user = _user(db, "u-auto")
    _bind(db, user, "drive")
    item = _drive_item(db, "113")
    low = _source(db, "源低")
    high = _source(db, "源高")
    _mapping(db, item, low, "up-low", confidence=0.5)
    _mapping(db, item, high, "up-high", confidence=0.95)
    resp = _as(client, user).post(
        "/api/user/source-discovery/confirm", json={"tmdb_id": "113"})
    body = resp.json()
    assert body["state"] == "queued"
    assert body["source_id"] == high.id
    assert body["upstream_item_id"] == "up-high"
    assert _jobs(engine) == 1


def test_confirm_master_off_failed(client, db, engine, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "0")
    user = _user(db, "u-masteroff")
    _bind(db, user, "drive")
    item = _drive_item(db, "114")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-14")
    resp = _as(client, user).post(
        "/api/user/source-discovery/confirm",
        json={"local_item_id": item.id, "source_id": src.id,
              "upstream_item_id": "up-14"})
    assert resp.json()["state"] == "failed"
    assert _jobs(engine) == 0


def test_confirm_partial_triple_auto_on_400(client, db, engine, monkeypatch):
    """自动模式开时，部分三元组 + tmdb 也不许滑进自动挑选：400，不建任务。

    回归：之前只有“全三元组”才走三元组支，残缺三元组会被丢弃、按 tmdb
    另挑最高候选建任务——确认的根本不是调用方指的那一个（例如
    local_item_id=999999 照常 200 建任务）。
    """
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    monkeypatch.setenv("SOURCE_DISCOVERY_AUTO_TRANSFER_ENABLED", "1")
    user = _user(db, "u-partial")
    _bind(db, user, "drive")
    item = _drive_item(db, "115")
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-15")
    c = _as(client, user)
    resp = c.post("/api/user/source-discovery/confirm",
                  json={"local_item_id": 999999, "tmdb_id": "115"})
    assert resp.status_code == 400
    resp = c.post("/api/user/source-discovery/confirm",
                  json={"local_item_id": item.id, "source_id": src.id,
                        "tmdb_id": "115"})
    assert resp.status_code == 400
    assert _jobs(engine) == 0

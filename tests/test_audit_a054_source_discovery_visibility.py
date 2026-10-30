"""审计 A-054：找源链须认 is_hidden 与 merged_into_id。

搜索（_resolve_items）、确认复核（resolve_candidate）、聚合配对
（pair_aggregates 的 _alive_local_query）三处，均须只认「未隐藏、
非合并副本」的活行，与本站浏览口径（is_hidden=false AND
deleted_at IS NULL）对齐。隐藏条目与合并副本不得出现在搜索结果
里、不得经确认建单、不得被聚合配对写映射。
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
from backend.emby_server.source_discovery import orchestrator
from backend.emby_server import upstream_aggregate_run as run_mod
from backend.emby_server import upstream_aggregation as agg


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


def _item(db, tmdb_id, name="已在库", **overrides):
    mount = em.StorageMount(name=f"a054-mount-{tmdb_id}-{name}",
                            mount_type="drive", path="drive:shared-drive-id")
    db.add(mount)
    db.commit()
    library = em.Library(guid=f"lib-a054-{tmdb_id}-{name}", name="找源库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-a054-{tmdb_id}-{name}",
        library_id=library.id,
        item_type="movie",
        name=name,
        tmdb_id=tmdb_id,
        file_path=f"mount://{mount.id}/x.mp4",
    )
    for key, value in overrides.items():
        setattr(item, key, value)
    db.add(item)
    db.commit()
    db.refresh(item)
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


def _mapping(db, item, source, upstream_id):
    row = em.UpstreamItemMapping(
        local_item_id=item.id, source_id=source.id,
        upstream_item_id=upstream_id, confidence=0.9,
        evidence="tmdb",
    )
    db.add(row)
    db.commit()
    return row


def _as(client, user):
    client.app.dependency_overrides[get_current_user] = lambda: user
    return client


def test_hidden_item_not_in_search(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-a054-hidden")
    item = _item(db, "200", is_hidden=True)
    src = _source(db, "云海服")
    _mapping(db, item, src, "up-h")
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "200"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_merged_item_not_in_search(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-a054-merged")
    master = _item(db, "201", name="正本")
    merged = _item(db, "201", name="副本", merged_into_id=master.id)
    src = _source(db, "云海服")
    _mapping(db, merged, src, "up-m")
    resp = _as(client, user).get(
        "/api/user/source-discovery/search", params={"tmdb_id": "201"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0


def test_confirm_hidden_and_merged_triple_404(client, db, monkeypatch):
    monkeypatch.setenv("SOURCE_DISCOVERY_ENABLED", "1")
    user = _user(db, "u-a054-confirm")
    hidden = _item(db, "202", is_hidden=True)
    master = _item(db, "203", name="正本")
    merged = _item(db, "203", name="副本", merged_into_id=master.id)
    src = _source(db, "云海服")
    _mapping(db, hidden, src, "up-h2")
    _mapping(db, merged, src, "up-m2")
    c = _as(client, user)
    for item, upstream_id in ((hidden, "up-h2"), (merged, "up-m2")):
        resp = c.post(
            "/api/user/source-discovery/confirm",
            json={"local_item_id": item.id, "source_id": src.id,
                  "upstream_item_id": upstream_id})
        assert resp.status_code == 404


def test_resolve_candidate_rejects_hidden_and_merged(db):
    master = _item(db, "204", name="正本")
    hidden = _item(db, "205", is_hidden=True)
    merged = _item(db, "206", name="副本", merged_into_id=master.id)
    normal = _item(db, "207")
    src = _source(db, "云海服")
    for item, upstream_id in ((hidden, "up-h3"), (merged, "up-m3"),
                              (normal, "up-n3")):
        _mapping(db, item, src, upstream_id)
    assert orchestrator.resolve_candidate(
        db, hidden.id, src.id, "up-h3") is None
    assert orchestrator.resolve_candidate(
        db, merged.id, src.id, "up-m3") is None
    assert orchestrator.resolve_candidate(
        db, normal.id, src.id, "up-n3") is not None


def test_pair_aggregates_skips_hidden(db):
    hidden = _item(db, "208", is_hidden=True)
    normal = _item(db, "209")
    pairs, _unpaired = run_mod.pair_aggregates(db, [
        agg.UpstreamAggregateItem(
            aggregate_key="k-h", name="隐藏片", item_type="Movie",
            tmdb_id="208", lines=[agg.UpstreamLine(
                line_index=0, line_key="l-h", source_id="1",
                upstream_item_id="up-h")]),
        agg.UpstreamAggregateItem(
            aggregate_key="k-n", name="正常片", item_type="Movie",
            tmdb_id="209", lines=[agg.UpstreamLine(
                line_index=0, line_key="l-n", source_id="1",
                upstream_item_id="up-n")]),
    ])
    local_ids = {pair["local_item_id"] for pair in pairs}
    assert hidden.id not in local_ids
    assert normal.id in local_ids

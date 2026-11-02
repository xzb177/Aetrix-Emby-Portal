"""苹果 CMS 媒体库类型·地基单回归。

钉死的纪律：
① 库行新列 kind / upstream_source_ids / cms_type_map：老库默认
   kind='local'，行为与升级前逐字一致；
② 扫描隔离（本案第一要点）：kind='applecms' 的库绝不进扫描队列、
   绝不走 scan_library_sync——空 paths 的 CMS 库若被普通扫描当成
   「文件全消失」，清理阶段会把托管行整库误删；
③ 管理端建库/改库：kind='applecms' 时不要求本地路径/挂载，
   来源必须是已存在的苹果 CMS 型上游源；库类型建库后不可更改。
"""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import portal, scan_queue, scanner
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    staff = base_models.WebUser(username="boss", password_hash="x", is_staff=True)
    db.add(staff)
    db.commit()
    app = FastAPI()
    app.include_router(portal.admin_emby_router)
    app.dependency_overrides[portal.require_staff] = lambda: staff

    def _get_db():
        yield db

    app.dependency_overrides[portal.get_db] = _get_db
    store.invalidate()
    with TestClient(app) as c:
        yield c, db
    store.invalidate()


def _cms_source(db, name="非凡资源", enabled=True):
    row = em.UpstreamSource(
        name=name, source_type="applecms",
        base_url="https://cms.example.invalid", auth_method="api_key",
        enabled=enabled)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _emby_source(db, name="别人的 Emby"):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://emby.example.invalid", auth_method="api_key",
        enabled=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ==================== 模型默认值 ====================

def test_library_kind_defaults_to_local(db):
    lib = em.Library(guid="g-local", name="普通库")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    assert lib.kind == "local"
    assert lib.upstream_source_ids == ""
    assert lib.cms_type_map is None


# ==================== 扫描隔离 ====================

def _ns_library(kind, lib_id=77):
    return SimpleNamespace(
        id=lib_id, name="CMS 库", collection_type="movies", paths="",
        scrape_policy=None, mount_ids="", incremental_scan=True,
        kind=kind)


def test_scan_queue_enqueue_skips_applecms():
    scan_queue.reset_for_tests()
    result = scan_queue.enqueue(_ns_library("applecms"), trigger="manual")
    assert result["created"] is False
    assert result["skipped"] == "applecms_library"
    assert result["task"]["state"] == "skipped"
    assert scan_queue.is_busy(77) is False


def test_scan_queue_enqueue_local_skips_applecms():
    scan_queue.reset_for_tests()
    result = scan_queue.enqueue_local(_ns_library("applecms"), trigger="manual")
    assert result["created"] is False
    assert result["skipped"] == "applecms_library"
    assert scan_queue.is_busy(77) is False


def test_scan_queue_enqueue_targeted_skips_applecms():
    scan_queue.reset_for_tests()
    result = scan_queue.enqueue_targeted(
        _ns_library("applecms"), ["/whatever"], trigger="drive-changes")
    assert result["created"] is False
    assert result["skipped"] == "applecms_library"
    assert scan_queue.is_busy(77) is False


def test_scan_library_sync_skips_applecms_and_keeps_items(db):
    lib = em.Library(guid="g-cms", name="CMS 库", collection_type="movies",
                     paths="", kind="applecms")
    db.add(lib)
    db.flush()
    item = em.MediaItem(
        guid="hosted-1", library_id=lib.id, item_type="movie",
        name="托管电影", upstream_source_id=7, probe_status="done")
    db.add(item)
    db.commit()
    stats = scanner.scan_library_sync(db, lib)
    assert stats.get("skipped") == "applecms_library"
    db.expire_all()
    row = db.query(em.MediaItem).filter(em.MediaItem.guid == "hosted-1").first()
    assert row is not None
    assert row.deleted_at is None


# ==================== 管理端建库 / 改库 ====================

def test_create_applecms_library(client):
    c, db = client
    src = _cms_source(db)
    res = c.post("/api/admin/emby/libraries", json={
        "name": "在线电影", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [src.id],
        "cms_type_map": {"电影": "in"},
    })
    assert res.status_code == 200, res.text
    lib = db.query(em.Library).filter(em.Library.id == res.json()["id"]).first()
    assert lib.kind == "applecms"
    assert lib.paths == ""
    assert lib.upstream_source_ids == str(src.id)
    assert lib.cms_type_map == '{"电影": "in"}'
    # 列表回显：kind 与解析后的来源/映射要下发给管理端表单
    row = c.get("/api/admin/emby/libraries").json()["libraries"][0]
    assert row["kind"] == "applecms"
    assert row["upstream_source_ids"] == [src.id]
    assert row["cms_type_map"] == {"电影": "in"}


def test_create_applecms_library_requires_sources(client):
    c, _db = client
    res = c.post("/api/admin/emby/libraries", json={
        "name": "在线电影", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [],
    })
    assert res.status_code == 400


def test_create_applecms_library_rejects_non_cms_source(client):
    c, db = client
    src = _emby_source(db)
    res = c.post("/api/admin/emby/libraries", json={
        "name": "在线电影", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [src.id],
    })
    assert res.status_code == 400


def test_create_applecms_library_rejects_missing_source(client):
    c, _db = client
    res = c.post("/api/admin/emby/libraries", json={
        "name": "在线电影", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [9999],
    })
    assert res.status_code == 400


def test_create_local_library_still_needs_paths(client, tmp_path):
    c, _db = client
    res = c.post("/api/admin/emby/libraries", json={
        "name": "本地库", "collection_type": "movies", "paths": [],
    })
    assert res.status_code == 400
    res = c.post("/api/admin/emby/libraries", json={
        "name": "本地库", "collection_type": "movies",
        "paths": [str(tmp_path)],
    })
    assert res.status_code == 200, res.text


def test_update_applecms_library_sources_and_kind_lock(client):
    c, db = client
    src1 = _cms_source(db, "非凡资源")
    src2 = _cms_source(db, "暴风资源")
    res = c.post("/api/admin/emby/libraries", json={
        "name": "在线电影", "collection_type": "movies",
        "kind": "applecms", "upstream_source_ids": [src1.id],
    })
    lib_id = res.json()["id"]
    res = c.put(f"/api/admin/emby/libraries/{lib_id}", json={
        "upstream_source_ids": [src1.id, src2.id],
        "cms_type_map": {"电影": "in", "电视剧": "out"},
    })
    assert res.status_code == 200, res.text
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    db.refresh(lib)
    assert lib.upstream_source_ids == f"{src1.id},{src2.id}"
    assert lib.cms_type_map == '{"电影": "in", "电视剧": "out"}'
    # 库类型建库后不可更改
    res = c.put(f"/api/admin/emby/libraries/{lib_id}", json={"kind": "local"})
    assert res.status_code == 400

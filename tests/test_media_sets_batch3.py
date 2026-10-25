"""合集域 L3 批三：path 来源 + 人物绑定 + preset 模板机制 +
douban_doulist 接口位 + D1 开关。

模板只存规则、不存条目清单；豆列只留接口位，刷新恒返回
not_implemented、不假装导入。
"""

import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend import models as base_models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import media_sets as ms

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.EmbyMediaSetMember, em.EmbyMediaSetRule,
                      em.EmbyMediaSet, em.EmbyMediaSetTemplate,
                      em.EmbyPerson, em.MediaItem, em.Library,
                      base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="setadmin3", is_staff=True,
                            is_active=True, password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from backend.database import get_db
    from backend.main import app

    def _override_get_db():
        _s = SessionLocal()
        try:
            yield _s
        finally:
            _s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def authed(client, admin):
    from backend.api.admin_core import get_current_admin
    from backend.api.user import get_current_user
    from backend.main import app

    app.dependency_overrides[get_current_admin] = lambda: admin
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_admin]
        del app.dependency_overrides[get_current_user]


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(ms, "COLLECTIONS_ENABLED", 1)
    monkeypatch.setattr(ms, "COLLECTION_SHOW_UNBOUND", 0)
    yield


def _lib(db, name):
    lib = em.Library(guid=f"lib-{name}", name=name, is_enabled=True)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib_id, name, path, year=None, tmdb=None):
    item = em.MediaItem(guid=f"item-{name}", library_id=lib_id,
                        item_type="movie", name=name, production_year=year,
                        tmdb_id=tmdb, file_path=path)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _member_names(authed, sid):
    r = authed.get(f"/api/user/collections/{sid}/items?limit=100")
    assert r.status_code == 200, r.text
    return [i["name"] for i in r.json()["items"]]


def test_path_source_matches_prefix(authed, db, gate_on):
    lib = _lib(db, "甲库")
    _item(db, lib.id, "甲一", "/mnt/测试系列甲/甲一.mkv", tmdb="1001")
    _item(db, lib.id, "甲二", "/mnt/测试系列甲/甲二.mkv", tmdb="1002")
    _item(db, lib.id, "甲三", "/mnt/别处/甲三.mkv", tmdb="1003")
    r = authed.post("/api/admin/media-sets",
                    json={"name": "路径集", "source_type": "path",
                          "path_prefix": "/mnt/测试系列甲/",
                          "min_effective_members": 2})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r = authed.post(f"/api/admin/media-sets/{sid}/refresh")
    assert r.json()["added"] == 2, r.text
    assert sorted(_member_names(authed, sid)) == ["甲一", "甲二"]
    # path 合集缺前缀直接 400
    assert authed.post("/api/admin/media-sets",
                       json={"name": "坏路径集",
                             "source_type": "path"}).status_code == 400


def test_bound_person_unions_into_candidates(authed, db, gate_on):
    lib = _lib(db, "甲库")
    a = _item(db, lib.id, "甲一", "/data/甲一.mkv", tmdb="1001")
    b = _item(db, lib.id, "甲二", "/data/甲二.mkv", tmdb="1002")
    _item(db, lib.id, "甲三", "/data/甲三.mkv", tmdb="1003")
    db.add(em.EmbyPerson(item_id=a.id, name="测试演员甲",
                         person_tmdb_id="p9001"))
    db.add(em.EmbyPerson(item_id=b.id, name="测试演员甲",
                         person_tmdb_id="p9001"))
    db.commit()
    person_row = db.query(em.EmbyPerson).filter(
        em.EmbyPerson.item_id == a.id).first()
    r = authed.post("/api/admin/media-sets",
                    json={"name": "人物集", "source_type": "rule",
                          "bound_person_id": person_row.id,
                          "min_effective_members": 2})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    # 无规则时仅靠绑定人物并入候选（同人两条目）
    r = authed.post(f"/api/admin/media-sets/{sid}/refresh")
    assert r.json()["added"] == 2, r.text
    assert sorted(_member_names(authed, sid)) == ["甲一", "甲二"]


def test_preset_template_mechanism_only(authed, db, gate_on):
    lib = _lib(db, "甲库")
    _item(db, lib.id, "甲一", "/data/甲一.mkv", year=1994, tmdb="1001")
    _item(db, lib.id, "甲二", "/data/甲二.mkv", year=2001, tmdb="1002")
    # 模板夹带条目清单直接 400：模板只存规则
    assert authed.post("/api/admin/media-set-templates",
                       json={"name": "坏模板",
                             "rules": [{"rule_kind": "year",
                                        "value_int": 1994,
                                        "item_ids": [1, 2]}]}).status_code == 400
    r = authed.post("/api/admin/media-set-templates",
                    json={"name": "年份模板",
                          "rules": [{"rule_kind": "year",
                                     "value_int": 1994}]})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    r = authed.post(f"/api/admin/media-set-templates/{tid}/instantiate",
                    json={"name": "模板实例集", "min_effective_members": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_type"] == "preset"
    assert body["refresh"]["added"] == 1
    assert _member_names(authed, body["id"]) == ["甲一"]


def test_douban_doulist_is_stub_not_fake_success(authed, db, gate_on):
    lib = _lib(db, "甲库")
    _item(db, lib.id, "甲一", "/data/甲一.mkv", tmdb="1001")
    r = authed.post("/api/admin/media-sets",
                    json={"name": "豆列集", "source_type": "douban_doulist"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    # 顶一次号再刷新：completed 必须追不上 requested，状态明确未实现
    row = db.query(em.EmbyMediaSet).filter(
        em.EmbyMediaSet.id == sid).first()
    ms.bump_requested(row)
    db.commit()
    r = authed.post(f"/api/admin/media-sets/{sid}/refresh")
    assert r.json()["status"] == "not_implemented", r.text
    assert r.json()["completed_revision"] == 0
    assert r.json()["requested_revision"] == 1
    detail = authed.get(f"/api/admin/media-sets/{sid}").json()
    assert detail["refresh_status"] == "not_implemented"
    assert detail["members"] == []


def test_d1_switch_gates_unbound_at_read_time(authed, db, gate_on,
                                             monkeypatch):
    lib = _lib(db, "甲库")
    _item(db, lib.id, "甲一", "/data/甲一.mkv", tmdb="1001")
    _item(db, lib.id, "甲二", "/data/甲二.mkv", tmdb=None)  # 未绑定
    r = authed.post("/api/admin/media-sets",
                    json={"name": "混合集", "source_type": "manual",
                          "min_effective_members": 1})
    sid = r.json()["id"]
    for item_id in (1, 2):
        pass
    items = db.query(em.MediaItem).order_by(em.MediaItem.id).all()
    for item in items:
        authed.post(f"/api/admin/media-sets/{sid}/members",
                    json={"item_id": item.id})
    # 默认 0：未绑定成员不露，有效数只算已绑定的 1 个
    r = authed.get(f"/api/user/collections/{sid}/items")
    assert [i["name"] for i in r.json()["items"]] == ["甲一"]
    assert r.json()["effective_count"] == 1
    # 开关开：全露，不返工、不重算成员表
    monkeypatch.setattr(ms, "COLLECTION_SHOW_UNBOUND", 1)
    r = authed.get(f"/api/user/collections/{sid}/items")
    assert sorted(i["name"] for i in r.json()["items"]) == ["甲一", "甲二"]
    assert r.json()["effective_count"] == 2
    members = db.query(em.EmbyMediaSetMember).filter(
        em.EmbyMediaSetMember.set_id == sid).count()
    assert members == 2  # 成员表行数未变：判定只在读时

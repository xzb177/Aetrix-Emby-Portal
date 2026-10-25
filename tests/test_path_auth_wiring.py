"""路径授权接线（#1240 落库后的播放入口门禁 + 管理端导入触发点）。

只测接线本身，不测判定口径（判定已在 test_path_auth_policies.py 钉死）：
- 播放入口 helper：无行放行、命中授权放行、不命中 403 + 人话原因、
  user=None 只算库层、非挂载路径放行。
- 管理端导入端点：旧配置行落表、幂等（二次导入只 skipped）。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import playback_admin as pa_admin
from backend.emby_server import models as em
from backend.emby_server import api as emby_api
from backend.emby_server import stream_limits as sl
from backend.integrations import store


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
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


def _item(library_id=1, file_path="mount://3/电影/a.mkv"):
    return SimpleNamespace(library_id=library_id, file_path=file_path)


def _user(user_id=7):
    return SimpleNamespace(id=user_id)


def _seed_library_grant(db, library_id=1, mount_id=3, prefix="/电影"):
    db.add(em.LibraryAccessGrant(library_id=library_id, mount_id=mount_id,
                                 path_prefix=prefix))
    db.commit()


def _seed_config(db, rows):
    db.add(base_models.SystemConfig(key=sl.POLICIES_KEY,
                                    value=json.dumps(rows)))
    db.commit()


def _fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), headers={})


# ---------- 播放入口门禁 ----------

def test_enforce_path_access_allows_without_grants(db):
    assert emby_api._enforce_path_access(db, _user(), _item()) is None


def test_enforce_path_access_allows_when_under_prefix(db):
    _seed_library_grant(db)
    assert emby_api._enforce_path_access(db, _user(), _item()) is None


def test_enforce_path_access_denies_403_with_reason(db):
    _seed_library_grant(db)
    with pytest.raises(HTTPException) as excinfo:
        emby_api._enforce_path_access(
            db, _user(), _item(file_path="mount://3/综艺/b.mkv"))
    assert excinfo.value.status_code == 403
    assert "库" in excinfo.value.detail


def test_enforce_path_access_user_none_only_library_counts(db):
    _seed_library_grant(db)
    with pytest.raises(HTTPException) as excinfo:
        emby_api._enforce_path_access(
            db, None, _item(file_path="mount://3/综艺/b.mkv"))
    assert excinfo.value.status_code == 403


def test_enforce_path_access_non_mount_allows_even_with_grants(db):
    _seed_library_grant(db)
    assert emby_api._enforce_path_access(
        db, _user(), _item(file_path="/local/a.mkv")) is None


# ---------- 管理端导入触发点 ----------

def test_admin_import_endpoint_imports_then_skips(db):
    _seed_config(db, [{"mount_id": 3, "prefix": "/电影", "max_streams": 2},
                      {"mount_id": 3, "prefix": "坏行"}])
    first = pa_admin.import_stream_path_policies(
        request=_fake_request(), current_admin=1, db=db)
    assert first == {"success": True, "imported": 1, "skipped": 0}
    row = db.query(em.SourcePathPolicy).filter(
        em.SourcePathPolicy.mount_id == 3,
        em.SourcePathPolicy.path_prefix == "/电影").first()
    assert row is not None and row.max_concurrent_streams == 2
    second = pa_admin.import_stream_path_policies(
        request=_fake_request(), current_admin=1, db=db)
    assert second == {"success": True, "imported": 0, "skipped": 1}


def test_admin_import_endpoint_empty_config_noop(db):
    got = pa_admin.import_stream_path_policies(
        request=_fake_request(), current_admin=1, db=db)
    assert got == {"success": True, "imported": 0, "skipped": 0}

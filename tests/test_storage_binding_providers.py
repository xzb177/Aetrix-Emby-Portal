"""用户端“我的网盘”挂载驱动名单（GET /storage-bindings/providers 的查询本体）。

只测 list_binding_providers：挂载类型 → provider 映射、is_enabled 过滤、
不返回任何敏感字段（返回值只有 provider 名串）。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import user_bindings as ub


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
    yield session
    session.close()


def _mount(db, name, mount_type, is_enabled=True):
    row = em.StorageMount(name=name, mount_type=mount_type, path="",
                          is_enabled=is_enabled)
    db.add(row)
    db.commit()
    return row


def test_no_mounts_returns_empty(db):
    assert ub.list_binding_providers(db) == []


def test_drive_and_115_mapped_and_others_skipped(db):
    _mount(db, "m-drive", "drive")
    _mount(db, "m-115", "115")
    _mount(db, "m-local", "local")
    _mount(db, "m-rclone", "rclone")
    assert ub.list_binding_providers(db) == ["drive", "pan115"]


def test_alias_mount_types_mapped(db):
    _mount(db, "m-gdrive", "gdrive")
    _mount(db, "m-pan115", "pan115")
    assert ub.list_binding_providers(db) == ["drive", "pan115"]


def test_disabled_mounts_not_counted(db):
    _mount(db, "m-drive-off", "drive", is_enabled=False)
    _mount(db, "m-115-on", "115", is_enabled=True)
    assert ub.list_binding_providers(db) == ["pan115"]


def test_result_contains_only_provider_names(db):
    _mount(db, "m-drive", "drive", )
    result = ub.list_binding_providers(db)
    assert result == ["drive"]
    assert all(isinstance(p, str) for p in result)

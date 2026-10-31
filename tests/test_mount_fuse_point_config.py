"""A-095 收尾：挂载 config 的 fuse_mount_point 往返回归。

后端契约（portal_mount_routes）：MountCreate/MountUpdate 的 config 整包
透传（仅 MOUNT_TYPES 里标 secret 的键留空＝不改；fuse_mount_point 不是
secret），GET /mounts 的 _serialize_mount 回脱敏后的 config（该键非
secret，明文回读）。管理端「FUSE 挂载点」字段靠这条契约保存 / 回读 /
清空，drive_changes._fuse_remote_map 靠它从挂载行派生映射（空＝不声明）。
本测试锁住契约，防日后有人给 config 加白名单时把这个键静默吞掉。
"""

import json
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import portal_mount_routes as pmr


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


def _listed(db, mount_id):
    out = pmr.list_mounts(staff=SimpleNamespace(), db=db, realm_id=0)
    for m in out["mounts"]:
        if m["id"] == mount_id:
            return m
    raise AssertionError(f"挂载 {mount_id} 不在列表里")


def _create(db, config=None, name="fuse-point-a"):
    req = pmr.MountCreate(name=name, mount_type="rclone",
                          path="rclone:gdrive:/Movies", config=config or {})
    out = pmr.create_mount(req, staff=SimpleNamespace(), db=db)
    return out["mount"]


def test_create_with_fuse_mount_point_roundtrips(db):
    mount = _create(db, {"fuse_mount_point": "/mnt/gdrive-direct"})
    assert mount["config"].get("fuse_mount_point") == "/mnt/gdrive-direct"
    listed = _listed(db, mount["id"])
    assert listed["config"].get("fuse_mount_point") == "/mnt/gdrive-direct"
    row = db.query(em.StorageMount).filter(em.StorageMount.id == mount["id"]).first()
    assert json.loads(row.config)["fuse_mount_point"] == "/mnt/gdrive-direct"


def test_create_without_fuse_mount_point_stays_undeclared(db):
    mount = _create(db, name="fuse-point-b")
    assert not (mount["config"].get("fuse_mount_point") or "")
    assert not (_listed(db, mount["id"])["config"].get("fuse_mount_point") or "")


def test_update_sets_and_clears_fuse_mount_point(db):
    mount = _create(db, {"fuse_mount_point": "/mnt/old", "mode": "cli"},
                    name="fuse-point-c")
    mid = mount["id"]
    out = pmr.update_mount(mid, pmr.MountUpdate(config={"fuse_mount_point": "/mnt/new"}),
                           staff=SimpleNamespace(), db=db)
    assert out["mount"]["config"]["fuse_mount_point"] == "/mnt/new"
    # 合并语义：没提到的键保留
    assert out["mount"]["config"].get("mode") == "cli"
    # 传空串 = 清除声明（非 secret 键空值覆盖），其余键不受影响
    out = pmr.update_mount(mid, pmr.MountUpdate(config={"fuse_mount_point": ""}),
                           staff=SimpleNamespace(), db=db)
    assert not (out["mount"]["config"].get("fuse_mount_point") or "")
    assert out["mount"]["config"].get("mode") == "cli"
    assert not (_listed(db, mid)["config"].get("fuse_mount_point") or "")


def test_update_without_config_keeps_fuse_mount_point(db):
    """只改名称/路径（config=None）时不得把已有声明抹掉。"""
    mount = _create(db, {"fuse_mount_point": "/mnt/keep"}, name="fuse-point-d")
    out = pmr.update_mount(mount["id"], pmr.MountUpdate(name="renamed"),
                           staff=SimpleNamespace(), db=db)
    assert out["mount"]["config"].get("fuse_mount_point") == "/mnt/keep"

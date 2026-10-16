"""媒体库「本机目录可读性」下发字段回归测试

根因（2026-10-16）：`list_libraries` 曾把 `_local_read_state` 返回的**整个 state
字典**塞进 `local_dirs_readable` 字段，而前端契约是 boolean|null
（admin_frontend/src/types/index.ts + Dashboard.vue 用 === true/=== false 比较），
字典永远不等于 true → 管理后台仪表盘存储健康卡恒显示「0 个正常/共 N 条来源」。

走真实 GET /libraries（portal.admin_emby_router 的 list_libraries），断言：
- 该字段只能是 bool 或 None，绝不能是 dict
- 存在的本机目录 → True；不存在的本机路径 → False；纯 mount:// → None
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
from backend.emby_server import models as em
from backend.emby_server import portal
from backend.integrations import store


@pytest.fixture()
def db():
    # TestClient 在线程池里跑路由，而线程里的 SQLite 连接不能跨线程用，
    # 所以要 check_same_thread=False + StaticPool（内存库只此一个连接）
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


@pytest.fixture()
def client(db):
    staff = models.WebUser(username="boss", password_hash="x", is_staff=True)
    db.add(staff)
    db.commit()

    app = FastAPI()
    app.include_router(portal.admin_emby_router)
    app.dependency_overrides[portal.require_staff] = lambda: staff

    def _get_db():
        yield db

    app.dependency_overrides[portal.get_db] = _get_db
    with TestClient(app) as c:
        yield c, db


def _library(db, paths: str, name: str = "测试库") -> em.Library:
    lib = em.Library(guid="g-read", name=name, collection_type="movies", paths=paths)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _mount(db, mount_type: str, name: str) -> em.StorageMount:
    row = em.StorageMount(name=name, mount_type=mount_type, path="", config="{}",
                          is_enabled=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _field_of(client, name):
    libs = client.get("/api/admin/emby/libraries").json()["libraries"]
    assert len(libs) == 1
    return libs[0]["local_dirs_readable"]


def test_existing_local_dir_is_true(client, tmp_path):
    c, db = client
    _library(db, str(tmp_path))
    value = _field_of(c, "测试库")
    assert value is True


def test_missing_local_path_is_false(client, tmp_path):
    c, db = client
    _library(db, str(tmp_path / "根本不存在的目录"))
    value = _field_of(c, "测试库")
    assert value is False


def test_mount_only_paths_is_none(client):
    c, db = client
    mount = _mount(db, "rclone", "谷歌云盘")
    _library(db, f"mount://{mount.id}/Movies")
    value = _field_of(c, "测试库")
    assert value is None


def test_mixed_good_and_missing_is_false(client, tmp_path):
    c, db = client
    _library(db, f"{tmp_path},{tmp_path}/不存在的子目录")
    value = _field_of(c, "测试库")
    assert value is False


def test_field_is_never_a_dict(client, tmp_path):
    """字段类型契约：只能是 bool 或 None（曾被整个 state 字典穿透）"""
    c, db = client
    _library(db, str(tmp_path))
    value = _field_of(c, "测试库")
    assert isinstance(value, bool) or value is None
    assert not isinstance(value, dict)

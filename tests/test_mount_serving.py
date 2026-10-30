# -*- coding: utf-8 -*-
"""挂载直读出流判定（2026-10-30 立项）。

钉死（设计 reports/mount-serving-design-20261030.md）：
- 命中白名单＋配了本地根＋文件在挂载点 → 返回本地路径（走挂载）；
- 未命中（开关关 / 不在白名单 / 没配根 / 非 mount 路径 / 挂载
  停用）→ 返回 None，调用方走现行出法，行为逐字不变；
- engages 后文件不在挂载点 → MountServingUnavailable 响亮失败，
  **不许返回 None 装作未命中**（静默换源就是把用户偷偷送回
  已证实不可用的公开链）；
- 路径穿越（../）→ 同样响亮失败，不许拼出根外路径；
- 本地出流复用 streaming.serve_file：Range 请求 206 +
  Content-Range + Accept-Ranges: bytes，字节与源文件一致；
- 配置写入校验：白名单坏项 / 根映射格式坏 / 重复 id → ValueError；
  写后热读即新值（回滚＝清空名单一步到位）。
全部离线（sqlite 内存库 + tmp_path 假挂载点）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models  # noqa: F401 — 注册表
from backend.database import Base
from backend.emby_server import models as em  # noqa: F401 — 注册表
from backend.emby_server import mount_serving as ms
from backend.emby_server import streaming
from backend.integrations import store

REL = "MoviePilot/电影/动画电影/大唐妖探 (2026)/大唐妖探 (2026) - 2160p WEB-DL.mp4"
FILE_PATH = f"mount://3/{REL}"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(em.StorageMount(
        id=3, name="gdrive-direct", mount_type="drive",
        path="drive:0AERUVu4kfWEfUk9PVA", is_enabled=True))
    session.commit()
    store.invalidate()
    try:
        yield session
    finally:
        store.invalidate()
        session.close()


def _set_config(db, *, enabled=None, ids=None, roots=None):
    rows = {}
    if enabled is not None:
        rows[ms.KEY_MOUNT_SERVING_ENABLED] = "true" if enabled else "false"
    if ids is not None:
        rows[ms.KEY_MOUNT_SERVING_MOUNT_IDS] = ids
    if roots is not None:
        rows[ms.KEY_MOUNT_SERVING_LOCAL_ROOTS] = roots
    for key, value in rows.items():
        db.add(models.SystemConfig(key=key, value=value))
    db.commit()
    store.invalidate()


def _item(path=FILE_PATH):
    return SimpleNamespace(file_path=path)


def _make_file(root, rel=REL, data=b"\x00\x00\x00\x18ftypisom" + b"x" * 4096):
    full = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return full


def test_whitelist_hit_returns_local_path(db, tmp_path):
    _make_file(tmp_path)
    _set_config(db, ids="3", roots=f"3={tmp_path}")
    assert ms.resolve_local_file(db, _item()) == os.path.join(str(tmp_path), REL)


def test_switch_off_returns_none(db, tmp_path):
    _make_file(tmp_path)
    _set_config(db, enabled=False, ids="3", roots=f"3={tmp_path}")
    assert ms.resolve_local_file(db, _item()) is None


def test_not_in_whitelist_returns_none(db, tmp_path):
    _make_file(tmp_path)
    _set_config(db, ids="2", roots=f"3={tmp_path}")
    assert ms.resolve_local_file(db, _item()) is None


def test_whitelist_without_root_returns_none(db, tmp_path):
    _make_file(tmp_path)
    _set_config(db, ids="3", roots="")
    assert ms.resolve_local_file(db, _item()) is None


def test_non_mount_path_returns_none(db, tmp_path):
    _set_config(db, ids="3", roots=f"3={tmp_path}")
    assert ms.resolve_local_file(db, _item("/media/local/x.mp4")) is None
    assert ms.resolve_local_file(db, _item("byos://drive/9/x.mp4")) is None


def test_missing_file_fails_loudly_not_silent(db, tmp_path):
    # 文件不在挂载点：必须抛错，绝不返回 None 让调用方静默换源
    _set_config(db, ids="3", roots=f"3={tmp_path}")
    with pytest.raises(ms.MountServingUnavailable):
        ms.resolve_local_file(db, _item())


def test_path_traversal_fails_loudly(db, tmp_path):
    _set_config(db, ids="3", roots=f"3={tmp_path}")
    with pytest.raises(ms.MountServingUnavailable):
        ms.resolve_local_file(db, _item("mount://3/../../etc/passwd"))


def test_disabled_mount_returns_none(db, tmp_path):
    _make_file(tmp_path)
    mount = db.query(em.StorageMount).filter_by(id=3).one()
    mount.is_enabled = False
    db.commit()
    _set_config(db, ids="3", roots=f"3={tmp_path}")
    assert ms.resolve_local_file(db, _item()) is None


def test_default_config_is_noop(db, tmp_path):
    # 什么都不配（白名单默认空）：即使文件就在挂载点也不介入
    _make_file(tmp_path)
    assert ms.resolve_local_file(db, _item()) is None
    payload = ms.config_payload(db)
    assert payload["enabled"] is True
    assert payload["mount_ids"] == []
    assert payload["local_roots"] == ""


def test_parse_helpers_tolerate_garbage_on_read():
    assert ms.parse_mount_ids("3, abc ,5,0,-1") == [3, 5]
    assert ms.parse_local_roots("3=/mnt/a,bad,2=relative") == {3: "/mnt/a"}
    with pytest.raises(ValueError):
        ms.parse_mount_ids("3,abc", strict=True)
    with pytest.raises(ValueError):
        ms.parse_local_roots("3=/mnt/a,3=/mnt/b", strict=True)
    with pytest.raises(ValueError):
        ms.parse_local_roots("3=relative/path", strict=True)


def test_write_config_roundtrip_and_rollback(db, tmp_path):
    result = ms.write_config(
        db, enabled=True, mount_ids=[3], local_roots=f"3={tmp_path}")
    db.commit()
    assert result["mount_ids"] == [3]
    assert result["local_roots"] == f"3={tmp_path}"
    _make_file(tmp_path)
    assert ms.resolve_local_file(db, _item()) is not None
    # 回滚一步到位：清空名单后立即不介入
    ms.write_config(db, enabled=True, mount_ids=[], local_roots="")
    db.commit()
    store.invalidate()
    assert ms.resolve_local_file(db, _item()) is None


def test_write_config_rejects_bad_values(db):
    with pytest.raises(ValueError):
        ms.write_config(db, enabled=True, mount_ids=[0], local_roots="")
    with pytest.raises(ValueError):
        ms.write_config(db, enabled=True, mount_ids=[3],
                        local_roots="3=relative/path")
    with pytest.raises(ValueError):
        ms.write_config(db, enabled=True, mount_ids=["x"], local_roots="")


def _range_request(start, end):
    scope = {
        "type": "http", "method": "GET", "path": "/emby/videos/x/stream.mp4",
        "headers": [(b"range", f"bytes={start}-{end}".encode())],
    }
    return Request(scope)


async def _collect(resp):
    return b"".join([chunk async for chunk in resp.body_iterator])


def test_serve_file_range_headers_and_bytes(tmp_path):
    data = bytes(range(256)) * 64
    full = _make_file(tmp_path, "a/b.mp4", data)
    resp = streaming.serve_file(full, _range_request(100, 199), "video/mp4")
    assert resp.status_code == 206
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-range"] == f"bytes 100-199/{len(data)}"
    assert asyncio.run(_collect(resp)) == data[100:200]

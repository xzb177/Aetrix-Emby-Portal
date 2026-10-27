# -*- coding: utf-8 -*-
"""115 能力收尾（2026-10-27）：只做无需真实 115 账号就能验证的事。

全部离线：客户端一律用桩替身，不碰真实 115、不碰生产库。
覆盖：
- stale 清理里 115 行跳过且进 skipped_rows（不删、不调客户端）；
- LRU 腾地方里 115 行跳过（此前会把 115 fid 交给 Drive 客户端，
  Drive 查不到即误标 already_gone——本单修掉）；
- 115 简单上传入口 5 GiB 上限守卫（超限在 OSS 初始化前即拒）。
本单不含任何 115 删除 API 调用：删除口径证据等级 GRADE 0，
仓库里无端点记录、无真实账号验证，任何删除代码都须另行确认。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_cleanup as tc
from backend.emby_server import transfer115


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


class _NotFound(Exception):
    pass


class FakeDriveClient:
    def __init__(self, existing=None):
        self.existing = set(existing or [])
        self.deleted = []
        self.get_calls = []

    def get_file(self, drive_marker, fid):
        self.get_calls.append((drive_marker, fid))
        if fid not in self.existing:
            raise _NotFound("404")
        return {"id": fid, "trashed": False, "size": 1000}

    def delete_file(self, drive_marker, fid):
        self.deleted.append((drive_marker, fid))
        self.existing.discard(fid)


def _variant(db, item_id, fid="copyfid", status="ready", drive="userdrive:1",
             size=1000, last_played=None, created=None, ref="1"):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid, variant_drive_id=drive,
                             owner_scope="user", owner_ref=ref,
                             status=status, size_bytes=size,
                             last_played_at=last_played,
                             created_at=created or datetime(2026, 10, 27))
    db.add(row)
    db.commit()
    return row


def _counting_factory(client, calls):
    def factory(db_arg, user_id):
        calls.append(user_id)
        return client, f"myDrive:{user_id}"
    return factory


def test_stale_pan115_skipped_and_listed_not_deleted(db):
    row = _variant(db, 13, fid="panfid", status="stale",
                   drive="pan115:user-1")
    client = FakeDriveClient(existing={"panfid"})
    calls = []
    stats = tc.cleanup_stale_variants(db, _counting_factory(client, calls))
    assert stats["skipped_unsupported"] == 1
    assert calls == [] and client.deleted == [] and client.get_calls == []
    assert stats["skipped_rows"] == [{
        "variant_id": row.id, "owner_ref": "1", "item_id": 13,
        "drive_id": "pan115:user-1", "file_id": "panfid",
        "reason": "pan115_delete_unverified"}]
    db.refresh(row)
    assert row.status == "stale"


def test_stale_skipped_rows_cover_unsafe_and_no_client(db):
    # 不合规 fid 的判定在删文件函数里，须先有客户端才走到那一步；
    # 无客户端行走另一条（同一批里用不同 owner_ref 区分两者）。
    unsafe = _variant(db, 14, fid="../../etc/passwd", status="stale")
    noclient = _variant(db, 15, fid="nofid", status="stale", ref="2")

    def _factory(db_arg, uid):
        if uid == 1:
            return FakeDriveClient(existing=set()), "myDrive:1"
        return None, None

    stats = tc.cleanup_stale_variants(db, _factory)
    assert stats["skipped_unsafe"] == 1 and stats["skipped_no_client"] == 1
    reasons = {r["variant_id"]: r["reason"] for r in stats["skipped_rows"]}
    assert reasons[unsafe.id] == "unsafe_fid"
    assert reasons[noclient.id] == "no_client"
    db.refresh(unsafe)
    db.refresh(noclient)
    assert unsafe.status == "stale" and noclient.status == "stale"


def test_evict_skips_pan115_and_evicts_drive_only(db):
    # 115 行最久没播（排第一）：修之前它会被 Drive 客户端误标已删。
    pan = _variant(db, 21, fid="panfid", drive="pan115:user-1",
                   last_played=datetime(2026, 10, 1),
                   created=datetime(2026, 10, 1))
    drv = _variant(db, 22, fid="drivefid", drive="userdrive:1", size=500,
                   last_played=datetime(2026, 10, 2),
                   created=datetime(2026, 10, 2))
    client = FakeDriveClient(existing={"drivefid"})
    result = tc.evict_lru_variants(db, _counting_factory(client, []), 1,
                                   need_bytes=400)
    assert result["evicted"] == [drv.id] and result["bytes_freed"] == 500
    assert result["skipped_unsupported"] == 1
    assert [r["variant_id"] for r in result["skipped_rows"]] == [pan.id]
    assert result["skipped_rows"][0]["reason"] == "pan115_delete_unverified"
    assert client.deleted == [("myDrive:1", "drivefid")]
    assert [fid for _, fid in client.get_calls] == ["drivefid"]
    db.refresh(pan)
    db.refresh(drv)
    assert pan.status == "ready" and drv.status == "deleted"


def test_evict_only_pan115_rows_evicts_nothing(db):
    pan = _variant(db, 23, fid="panonly", drive="pan115:user-1", size=900)
    client = FakeDriveClient(existing=set())
    result = tc.evict_lru_variants(db, _counting_factory(client, []), 1,
                                   need_bytes=100)
    assert result["evicted"] == [] and result["bytes_freed"] == 0
    assert result["reason"] == "insufficient_candidates"
    assert result["skipped_unsupported"] == 1
    assert client.deleted == [] and client.get_calls == []
    db.refresh(pan)
    assert pan.status == "ready"


class _SizedReader:
    """seek/tell 报指定大小的假文件体（不分配真实字节）。"""

    def __init__(self, size):
        self._size = size
        self._pos = 0

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence == 0:
            pos = offset
        elif whence == 1:
            pos = self._pos + offset
        elif whence == 2:
            pos = self._size + offset
        else:
            raise ValueError("whence")
        self._pos = max(0, min(self._size, pos))
        return self._pos

    def read(self, n=-1):
        return b""


def test_simple_upload_oversize_refused_before_init(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("超限时不应调用 OSS 初始化")

    monkeypatch.setattr(transfer115, "_http_json", _boom)
    client = transfer115.Pan115Client("cookie-test")
    with pytest.raises(transfer115.Pan115Error, match="5 GiB"):
        client.simple_upload(_SizedReader(5 * 1024 * 1024 * 1024 + 1),
                             "big.mkv", "900")


def test_simple_upload_small_reader_passes_guard(monkeypatch):
    import httpx as _httpx
    import io

    def _fake_call(method, url, params=None, data=None, cookie=None, ua=""):
        return {"host": "https://bucket.example", "object": "obj-key",
                "policy": "pol", "accessid": "acc", "callback": "cb",
                "signature": "sig"}

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"file_id": "fid9", "pick_code": "pc9"}}

    class _OSS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, data=None, files=None, headers=None):
            return _Resp()

    monkeypatch.setattr(transfer115, "_http_json", _fake_call)
    monkeypatch.setattr(_httpx, "Client", lambda *a, **k: _OSS())
    out = transfer115.Pan115Client("cookie-test").simple_upload(
        io.BytesIO(b"hello"), "a.mkv", "900")
    assert out["fid"] == "fid9" and out["pickcode"] == "pc9"

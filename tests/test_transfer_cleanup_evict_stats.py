# -*- coding: utf-8 -*-
"""evict_lru_variants 返回统计与 stale 清理同口径（第三轮第 4 路）。

回归：此前 evict 对循环内两类行无任何统计落点——不合规 fid 行
（_delete_one 返回 skipped_unsafe）与删前核查/删除失败行，返回
里 skipped_unsupported=0、skipped_rows=[]，与“无候选”无法区分，
而 stale 清理对同一类行有 skipped_unsafe/failed 计数 + skipped_rows。
本文件钉住 evict 同口径：skipped_unsafe 计入统计并进 skipped_rows，
failed 计入统计、行不动；其余行为（腾够即停、pan115 跳过）不变。
全部离线：客户端用桩替身，不碰真实 Drive、不碰生产库。
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


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class _FailingClient:
    """get_file 一律抛通用异常（模拟删前核查失败，非 404）。"""
    def get_file(self, marker, fid):
        raise RuntimeError("500 upstream")

    def delete_file(self, marker, fid):
        raise AssertionError("must not reach delete")


def _variant(db, fid, size=1000):
    row = em.TransferVariant(item_id=1, source_file_id="origfid",
                             variant_file_id=fid, variant_drive_id="userdrive:1",
                             owner_scope="user", owner_ref="1",
                             status="ready", size_bytes=size,
                             created_at=datetime(2026, 10, 27))
    db.add(row)
    db.commit()
    return row


def _factory(client):
    return lambda db, uid: (client, "myDrive:1")


def test_evict_counts_unsafe_fid_like_stale(db):
    row = _variant(db, "bad/fid")
    result = tc.evict_lru_variants(db, _factory(_FailingClient()), 1,
                                   need_bytes=5000)
    assert result["skipped_unsafe"] == 1
    assert result["skipped_rows"] == [{
        "variant_id": row.id, "owner_ref": "1", "item_id": 1,
        "drive_id": "userdrive:1", "file_id": "bad/fid",
        "reason": "unsafe_fid"}]
    assert result["evicted"] == []
    assert db.query(em.TransferVariant).get(row.id).status == "ready"


def test_evict_counts_failed_delete(db):
    row = _variant(db, "goodfid123")
    result = tc.evict_lru_variants(db, _factory(_FailingClient()), 1,
                                   need_bytes=5000)
    assert result["failed"] == 1
    assert result["evicted"] == []
    assert result["skipped_unsafe"] == 0
    assert db.query(em.TransferVariant).get(row.id).status == "ready"


def test_evict_early_returns_carry_new_keys(db):
    result = tc.evict_lru_variants(db, _factory(_FailingClient()), 1)
    assert result == {"evicted": [], "bytes_freed": 0, "reason": "no_need",
                      "skipped_unsupported": 0, "skipped_rows": [],
                      "skipped_unsafe": 0, "failed": 0}

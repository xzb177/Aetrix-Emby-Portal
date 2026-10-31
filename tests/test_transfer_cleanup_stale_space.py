# -*- coding: utf-8 -*-
"""A-090 回归：转存前量空间须先清 stale 死副本再腾 ready。

审计 R28-04 §A-090：cleanup_stale_variants 全仓无生产调用，且
ensure_space_for_transfer 的腾地方候选只取 ready 行——stale 死副本
把盘占满时也腾不掉，致 ensure_space 误判 insufficient、转存失败。
修法（审计建议①）：ensure_space 在腾 ready 之前先对本用户跑一轮
stale 清理，stale 字节先计入估算；stale 够了就不再动 ready 行。

全部离线：Drive 客户端一律用桩替身，不碰真实 Drive、不碰生产库。
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

NOW = datetime(2026, 10, 27, 12, 0, 0)


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
    """桩 Drive 客户端：about/get/delete 只记调用，不碰网络。"""

    def __init__(self, existing=None, free=10 ** 9):
        self.existing = set(existing or [])
        self.deleted = []
        self.free = free

    def about_storage(self, drive_marker):
        return {"limit": 10 ** 12, "usage": 10 ** 12 - self.free,
                "free": self.free}

    def get_file(self, drive_marker, fid):
        if fid not in self.existing:
            raise _NotFound("404")
        return {"id": fid, "trashed": False, "size": 1000}

    def delete_file(self, drive_marker, fid):
        self.deleted.append((drive_marker, fid))
        self.existing.discard(fid)


def _factory(client):
    def factory(db, user_id):
        return client, f"myDrive:{user_id}"
    return factory


def _variant(db, item_id, fid="copyfid", ref="435", status="ready",
             drive="userdrive:435", size=3000, created=None):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid, variant_drive_id=drive,
                             owner_scope="user", owner_ref=ref,
                             status=status, size_bytes=size,
                             created_at=created or datetime(2026, 10, 27))
    db.add(row)
    db.commit()
    return row


def test_stale_deleted_before_ready_when_space_short(db):
    """空间不足时先删 stale、再腾 ready；stale 字节计入估算。"""
    stale = _variant(db, 201, fid="stalefid", status="stale", size=3000)
    ready = _variant(db, 202, fid="readyfid", status="ready", size=3000)
    client = FakeDriveClient(existing={"stalefid", "readyfid"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    # need=4000：stale 3000 先清，剩余 need=1000 再腾 ready 3000，
    # 估算 1000+3000+3000=7000 >= 5000，可以转。
    assert result["reason"] == "evicted_enough" and result["proceed"] is True
    assert result["stale_deleted"] == 1 and result["stale_freed"] == 3000
    assert result["evicted"] == [ready.id]
    assert client.deleted == [("myDrive:435", "stalefid"),
                              ("myDrive:435", "readyfid")]
    db.refresh(stale)
    db.refresh(ready)
    assert stale.status == "deleted" and ready.status == "deleted"


def test_stale_alone_covers_need_ready_untouched(db):
    """stale 字节够用时 ready 行一条不动。"""
    stale = _variant(db, 211, fid="stalefid", status="stale", size=4500)
    ready = _variant(db, 212, fid="readyfid", status="ready", size=3000)
    client = FakeDriveClient(existing={"stalefid", "readyfid"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    # need=4000：stale 4500 已够，ready 不动。
    assert result["reason"] == "evicted_enough" and result["evicted"] == []
    assert result["stale_deleted"] == 1 and result["stale_freed"] == 4500
    assert client.deleted == [("myDrive:435", "stalefid")]
    db.refresh(ready)
    assert ready.status == "ready"


def test_stale_pan115_row_skipped_ready_still_evicts(db):
    """stale 的 115 行跳过（删除口径未实测），ready 正常腾。"""
    stale115 = _variant(db, 221, fid="panfid", status="stale",
                        drive="pan115:user-435", size=4000)
    ready = _variant(db, 222, fid="readyfid", status="ready", size=3000)
    client = FakeDriveClient(existing={"panfid", "readyfid"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    # stale 115 行不动；ready 3000 被腾后估算 4000 < 5000，仍不足。
    assert result["reason"] == "insufficient" and result["proceed"] is False
    assert result["stale_deleted"] == 0 and result["stale_freed"] == 0
    assert result["evicted"] == [ready.id]
    assert client.deleted == [("myDrive:435", "readyfid")]
    db.refresh(stale115)
    assert stale115.status == "stale"


def test_stale_other_user_untouched(db):
    """stale 清理只对本用户：他人 stale 行不动（哪怕工厂不分用户）。"""
    mine = _variant(db, 231, fid="minefid", status="stale", size=3000)
    other = _variant(db, 232, fid="otherfid", ref="999", status="stale",
                     drive="userdrive:999", size=3000)
    client = FakeDriveClient(existing={"minefid", "otherfid"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    assert result["stale_deleted"] == 1
    assert client.deleted == [("myDrive:435", "minefid")]
    db.refresh(other)
    assert other.status == "stale"


def test_cleanup_stale_variants_user_id_filter(db):
    """cleanup_stale_variants 的 user_id 过滤：只清指定用户。"""
    mine = _variant(db, 241, fid="minefid", status="stale", size=1000)
    other = _variant(db, 242, fid="otherfid", ref="999", status="stale",
                     drive="userdrive:999", size=1000)
    client = FakeDriveClient(existing={"minefid", "otherfid"})
    stats = tc.cleanup_stale_variants(db, _factory(client), user_id=435)
    assert stats["deleted"] == 1 and client.deleted == [("myDrive:435",
                                                         "minefid")]
    db.refresh(other)
    assert other.status == "stale"

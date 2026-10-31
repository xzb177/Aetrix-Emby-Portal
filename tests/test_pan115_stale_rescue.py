# -*- coding: utf-8 -*-
"""115 stale 变体救回（#1575 后续）：巡检重验 stale 的 115 行。

旧逻辑只查 ready 行，stale 行永不重验——#1575 修了取链通道后，
78 条被 50028 误标的 stale 行仍躺在 stale 里。本单修法：巡检每轮
同时重验 stale 的 115 行，验活即复活为 ready；核不到/认证错/
通道限且目录核不到则保持 stale、不重复记 strike。Drive stale 行
不在本单范围。
全部离线：115 API 一律用桩替身。
"""
import os
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.integrations import store
from backend.emby_server import stream_limits as sl


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


_counter = [0]


def _item(db):
    _counter[0] += 1
    row = em.MediaItem(guid=f"rescue-{_counter[0]}", library_id=1,
                       item_type="movie", name="测试片",
                       drive_file_id="srcfid",
                       file_path="mount://9/电影/a.mkv", size=1000)
    db.add(row)
    db.commit()
    return row


def _binding(db, user_id, status="active"):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider="pan115",
                                account_label="test115",
                                credential_id=cred.id, status=status)
    db.add(row)
    db.commit()
    return row


def _stale_115_row(db, item_id, pickcode="PC-NEW", fid="newfid"):
    row = em.TransferVariant(item_id=item_id, source_file_id="srcfid",
                             variant_file_id=fid,
                             variant_drive_id="pan115:user-7",
                             owner_scope="user", owner_ref="7",
                             status="stale", stale_count=2,
                             pickcode=pickcode)
    db.add(row)
    db.commit()
    return row


class _FakePan115:
    """目录树 + download 行为桩；d1 为转存目录。"""

    def __init__(self, download_behavior="ok", dir_files=None):
        self.dirs = {"0": [{"cid": "d1", "name": "aetrix-transfer",
                            "is_dir": True}],
                     "d1": list(dir_files or [])}
        self.download_behavior = download_behavior
        self.download_calls = []

    def list_dir(self, cid="0", limit=200):
        return [dict(e) for e in self.dirs.get(cid or "0", [])]

    def download_url(self, pickcode):
        self.download_calls.append(pickcode)
        if self.download_behavior == "gone":
            raise transfer115.Pan115Error("文件不存在")
        if self.download_behavior == "limit":
            raise transfer115.Pan115ChannelLimitError("文件大小超出限制")
        if self.download_behavior == "auth":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        return f"https://example.com/dl/{pickcode}"


def _patch(monkeypatch, fake):
    monkeypatch.setattr(tv, "_make_pan115_client", lambda auth: fake)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: "FAKEAUTH")


def test_stale_115_revived_on_download_ok(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    row = _stale_115_row(db, item.id)
    fake = _FakePan115("ok")
    _patch(monkeypatch, fake)
    stats = tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert stats["revived"] == 1
    assert row.status == "ready" and row.stale_count == 0
    assert fake.download_calls == ["PC-NEW"]


def test_stale_115_stays_stale_when_gone(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    row = _stale_115_row(db, item.id)
    fake = _FakePan115("gone")
    _patch(monkeypatch, fake)
    stats = tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2
    assert stats["stale"] == 0  # 不重复计入本轮新 stale


def test_stale_115_revived_via_dir_listing_on_channel_limit(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    row = _stale_115_row(db, item.id)
    fake = _FakePan115("limit", dir_files=[
        {"fid": "newfid", "cid": "c-new", "name": "a.mkv",
         "is_dir": False, "size": 1000, "pickcode": "PC-NEW"}])
    _patch(monkeypatch, fake)
    stats = tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert stats["revived"] == 1
    assert row.status == "ready" and row.stale_count == 0


def test_stale_115_stays_stale_on_channel_limit_without_listing(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    row = _stale_115_row(db, item.id)
    fake = _FakePan115("limit")
    _patch(monkeypatch, fake)
    stats = tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2
    assert stats["revived"] == 0


def test_stale_115_no_revive_without_active_binding(db, monkeypatch):
    item = _item(db)
    _binding(db, 7, status="expired")
    row = _stale_115_row(db, item.id)
    fake = _FakePan115("ok")
    _patch(monkeypatch, fake)
    stats = tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2
    assert stats["revived"] == 0
    assert fake.download_calls == []


class _ExplodingDrive:
    def get_file(self, *args, **kwargs):
        raise AssertionError("stale Drive 行不应被巡检验活")


def test_stale_drive_row_not_revived(db):
    item = _item(db)
    row = em.TransferVariant(item_id=item.id, source_file_id="srcfid",
                             variant_file_id="drivefid",
                             variant_drive_id="userdrive:7",
                             owner_scope="user", owner_ref="7",
                             status="stale", stale_count=2,
                             pickcode="")
    db.add(row)
    db.commit()
    stats = tv.patrol_once(db, lambda: _ExplodingDrive())
    db.refresh(row)
    assert row.status == "stale" and row.stale_count == 2
    assert stats["revived"] == 0


def test_stale_115_orphaned_when_item_deleted(db, monkeypatch):
    item = _item(db)
    _binding(db, 7)
    row = _stale_115_row(db, item.id)
    item.deleted_at = datetime.now()
    db.commit()
    fake = _FakePan115("ok")
    _patch(monkeypatch, fake)
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "orphan"

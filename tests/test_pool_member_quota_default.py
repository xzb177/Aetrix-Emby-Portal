# -*- coding: utf-8 -*-
"""贡献池选盘默认配额口径回归。

缺陷：pool_gate._member_quota 的默认分支曾调用
transfer_variant._default_drive_quota——该函数只在
quota_dashboard 里有定义（f4e773d 额度驾驶舱），从未在
transfer_variant 里实现过。每次选盘读成员盘余量都抛
AttributeError、被吞成「容量未知」，选盘从没真正看过成员盘
还剩多少空间。

本文件钉住：不注入 quota_fn 时，_member_quota 必须走既有默认
探针（quota_dashboard._default_drive_quota →
UserDriveApiClient.about_storage）读到三数；探针失败时仍按
未知回 None、不抛。
"""
from __future__ import annotations

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
from backend.emby_server import pool_gate
from backend.emby_server import transfer_variant as tv

GIB = 1024 ** 3


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_quota_cache():
    pool_gate.clear_pool_quota_cache()
    yield
    pool_gate.clear_pool_quota_cache()


def _member(db, user_id=777):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider="drive", account_label="贡献盘",
        credential_id=cred.id, status="active")
    db.add(binding)
    db.flush()
    row = em.PoolMember(
        binding_id=binding.id, user_id=user_id, provider="drive",
        status="active", joined_at=datetime.now())
    db.add(row)
    db.commit()
    return row


def test_member_quota_default_path_reads_about_storage(db, monkeypatch):
    """默认口径（不注入 quota_fn）必须真读到成员盘余量三数。"""
    member = _member(db)
    seen = {}

    def fake_about_storage(self, drive_id):
        seen["drive_id"] = drive_id
        return {"limit": 100 * GIB, "usage": 40 * GIB, "free": 60 * GIB}

    # patch 类属性（不 patch 实例，避免残留污染后续用例）
    monkeypatch.setattr(
        tv.UserDriveApiClient, "about_storage", fake_about_storage)

    quota = pool_gate._member_quota(db, member)

    assert quota == {"limit": 100 * GIB, "usage": 40 * GIB,
                     "free": 60 * GIB}
    assert seen["drive_id"] == f"myDrive:{member.user_id}"


def test_member_quota_default_path_failure_stays_unknown(db, monkeypatch):
    """默认探针失败时按容量未知回 None、不抛（选盘不判死成员）。"""
    member = _member(db)

    def boom(self, drive_id):
        raise RuntimeError("about 抖动")

    monkeypatch.setattr(tv.UserDriveApiClient, "about_storage", boom)

    assert pool_gate._member_quota(db, member) is None

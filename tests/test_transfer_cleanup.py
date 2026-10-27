# -*- coding: utf-8 -*-
"""转存副本清理（2026-10-27 用户亲定规则）。

全部离线：Drive 客户端一律用桩替身，不碰真实 Drive、不碰生产库。
覆盖：stale 删、已删幂等、115 跳过、不合规 fid 跳过、无客户端跳过、
ready LRU 顺序、无需求不删、按需腾够即停、删后可再转（行不再是
ready）、播放回写（只写本人 ready 行、1 小时节流）、解绑不删。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_cleanup as tc
from backend.emby_server import user_bindings


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
    """桩 Drive 客户端：get_file/delete_file 只记调用，不碰网络。"""

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


def _variant(db, item_id, fid="copyfid", scope="user", ref="1",
             status="ready", drive="userdrive:1", size=1000,
             last_played=None, created=None):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid, variant_drive_id=drive,
                             owner_scope=scope, owner_ref=ref,
                             status=status, size_bytes=size,
                             last_played_at=last_played,
                             created_at=created or datetime(2026, 10, 27))
    db.add(row)
    db.commit()
    return row


def _factory(client):
    def factory(db, user_id):
        return client, f"myDrive:{user_id}"
    return factory


def test_stale_drive_row_deleted_and_logged(db, caplog):
    row = _variant(db, 11, fid="stalefid", status="stale", size=1234)
    client = FakeDriveClient(existing={"stalefid"})
    with caplog.at_level("INFO", logger="backend.emby_server.transfer_cleanup"):
        stats = tc.cleanup_stale_variants(db, _factory(client))
    assert stats["deleted"] == 1 and stats["bytes_freed"] == 1234
    assert client.deleted == [("myDrive:1", "stalefid")]
    db.refresh(row)
    assert row.status == "deleted"
    # 删前记账：用户、条目、文件 id、大小都在日志里。
    assert any("stalefid" in r.message and "11" in r.message
               for r in caplog.records)


def test_stale_already_gone_marked_deleted_without_delete_call(db):
    row = _variant(db, 12, fid="gonefid", status="stale")
    client = FakeDriveClient(existing=set())
    stats = tc.cleanup_stale_variants(db, _factory(client))
    assert stats["already_gone"] == 1 and client.deleted == []
    db.refresh(row)
    assert row.status == "deleted"


def test_stale_pan115_row_skipped_and_untouched(db):
    row = _variant(db, 13, fid="panfid", status="stale",
                   drive="pan115:user-1")
    client = FakeDriveClient(existing={"panfid"})
    stats = tc.cleanup_stale_variants(db, _factory(client))
    assert stats["skipped_unsupported"] == 1 and client.deleted == []
    db.refresh(row)
    assert row.status == "stale"


def test_stale_unsafe_fid_skipped_and_untouched(db):
    row = _variant(db, 14, fid="../../etc/passwd", status="stale")
    client = FakeDriveClient(existing={"../../etc/passwd"})
    stats = tc.cleanup_stale_variants(db, _factory(client))
    assert stats["skipped_unsafe"] == 1 and client.deleted == []
    db.refresh(row)
    assert row.status == "stale"


def test_stale_no_client_skipped_and_untouched(db):
    row = _variant(db, 15, fid="nofid", status="stale")
    stats = tc.cleanup_stale_variants(db, lambda db, uid: (None, None))
    assert stats["skipped_no_client"] == 1
    db.refresh(row)
    assert row.status == "stale"


def test_stale_cleanup_never_touches_ready_rows(db):
    ready = _variant(db, 16, fid="readyfid", status="ready")
    _variant(db, 17, fid="stalefid", status="stale")
    client = FakeDriveClient(existing={"readyfid", "stalefid"})
    tc.cleanup_stale_variants(db, _factory(client))
    assert client.deleted == [("myDrive:1", "stalefid")]
    db.refresh(ready)
    assert ready.status == "ready"


def test_lru_order_oldest_first_with_session_progress_proxy(db):
    # A：上次播是 10 天前（10 月 17 日）；B：变体 last_played 空，
    # 但会话代理是昨天（10 月 26 日）；C：变体 last_played 空、
    # 创建时间是 10 月 18 日。顺序应为 A、C、B（代理只在
    # last_played 空时生效，且昨天的代理新于 C 的创建时间）。
    now = datetime(2026, 10, 27, 12, 0, 0)
    a = _variant(db, 21, fid="fid-a", last_played=now - timedelta(days=10),
                 created=datetime(2026, 10, 20))
    b = _variant(db, 22, fid="fid-b", last_played=None,
                 created=datetime(2026, 10, 26))
    c = _variant(db, 23, fid="fid-c", last_played=None,
                 created=datetime(2026, 10, 18))
    proxy = {(1, 22): now - timedelta(days=1)}
    order = tc.select_lru_candidates(db, 1, session_progress=proxy)
    assert [r.id for r in order] == [a.id, c.id, b.id]


def test_evict_without_need_deletes_nothing(db):
    _variant(db, 31, fid="fid31")
    client = FakeDriveClient(existing={"fid31"})
    result = tc.evict_lru_variants(db, _factory(client), 1)
    assert result["reason"] == "no_need" and result["evicted"] == []
    assert client.deleted == []


def test_evict_with_need_stops_when_freed_enough_and_retransfer_possible(db):
    old = _variant(db, 41, fid="fid-old", size=500,
                   last_played=datetime(2026, 10, 1),
                   created=datetime(2026, 10, 1))
    new = _variant(db, 42, fid="fid-new", size=900,
                   last_played=datetime(2026, 10, 26),
                   created=datetime(2026, 10, 26))
    client = FakeDriveClient(existing={"fid-old", "fid-new"})
    result = tc.evict_lru_variants(db, _factory(client), 1,
                                   need_bytes=400)
    assert result["evicted"] == [old.id] and result["bytes_freed"] == 500
    db.refresh(new)
    assert new.status == "ready"
    # 删后该条目再无 ready 行：门禁会按无副本自动重新转存（回归口径）。
    ready = (db.query(em.TransferVariant)
             .filter(em.TransferVariant.item_id == 41,
                     em.TransferVariant.status == "ready").all())
    assert ready == []


def test_mark_variant_played_only_own_ready_row_and_throttled(db):
    now = datetime(2026, 10, 27, 12, 0, 0)
    mine = _variant(db, 51, fid="fid-mine", last_played=None)
    other = _variant(db, 51, fid="fid-other", ref="2", last_played=None)
    stale = _variant(db, 51, fid="fid-stale", status="stale",
                     last_played=None)
    assert tc.mark_variant_played(db, 1, 51, now=now) is True
    db.refresh(mine)
    db.refresh(other)
    db.refresh(stale)
    assert mine.last_played_at == now
    assert other.last_played_at is None
    assert stale.last_played_at is None
    # 1 小时内重复回写不产生写事务。
    assert tc.mark_variant_played(
        db, 1, 51, now=now + timedelta(minutes=30)) is False
    # 1 小时后可再写。
    assert tc.mark_variant_played(
        db, 1, 51, now=now + timedelta(hours=2)) is True


def test_record_hook_calls_mark_variant_played(monkeypatch):
    from backend.emby_server import compat_routes
    calls = []

    def fake_mark(db, user_id, item_id):
        calls.append((user_id, item_id))
        return True

    monkeypatch.setattr(tc, "mark_variant_played", fake_mark)
    # compat_routes 是惰性导入 transfer_cleanup，patch 模块属性即生效。
    compat_routes._mark_variant_played_best_effort(
        None, SimpleNamespace(id=7), SimpleNamespace(id=99))
    assert calls == [(7, 99)]


def test_unbind_does_not_delete_transfer_copies(db):
    # 绑定行 + 凭据行 + 变体行齐全时解绑：变体行原样不动，
    # 且 delete_binding 不调用任何清理函数（锁死第 5 条；只断言
    # 调用名，不断言 docstring 里是否出现“清理”二字）。
    import inspect
    src = inspect.getsource(user_bindings.delete_binding)
    assert "cleanup_stale_variants" not in src
    assert "evict_lru_variants" not in src
    cred = em.MountCredential(scope="user", owner_user_id=9,
                              sealed_credential="x", key_id="k")
    db.add(cred)
    db.commit()
    user = base_models.WebUser(username="u9", password_hash="x")
    db.add(user)
    db.commit()
    binding = em.UserStorageBinding(user_id=user.id, provider="drive",
                                    account_label="盘", credential_id=cred.id,
                                    status="active")
    db.add(binding)
    db.commit()
    row = _variant(db, 61, fid="keepfid", ref=str(user.id))
    user_bindings.delete_binding(db, user.id, binding.id)
    assert db.query(em.UserStorageBinding).filter(
        em.UserStorageBinding.id == binding.id).first() is None
    db.refresh(row)
    assert row.status == "ready" and row.variant_file_id == "keepfid"

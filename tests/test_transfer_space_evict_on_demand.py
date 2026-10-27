# -*- coding: utf-8 -*-
"""转存前量空间、不足先腾旧副本（2026-10-27 用户亲定）。

全部离线：Drive 客户端一律用桩替身，不碰真实 Drive、不碰生产库。
覆盖：空间够不腾、空间不够按 LRU 顺序腾、腾够即停、腾完仍
不足明确失败（三数）、正在播的不腾（活跃会话 + 近期播放 +
本条排除）、个人文件不动、115 行不腾、about 拿不到不腾照旧
copy、文件大小未知不腾、配额 403 与凭据 403 区分、copy 中途
配额腾一次重试一次（二次仍配额则明确失败，不再腾）、任务级
空间不足明确失败且无静默共享回落。
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
from backend.emby_server import transfer_variant as tv

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
    """桩 Drive 客户端：about/get/delete/copy 只记调用，不碰网络。"""

    def __init__(self, existing=None, free=10 ** 9, about_error=False,
                 copy_behaviour=None):
        self.existing = set(existing or [])
        self.deleted = []
        self.free = free
        self.about_error = about_error
        self.copy_behaviour = list(copy_behaviour or [])
        self.copy_calls = 0
        self.about_calls = 0

    def about_storage(self, drive_marker):
        self.about_calls += 1
        if self.about_error:
            raise Exception("about boom")
        return {"limit": 10 ** 12, "usage": 10 ** 12 - self.free,
                "free": self.free}

    def get_file(self, drive_marker, fid):
        if fid not in self.existing:
            raise _NotFound("404")
        return {"id": fid, "trashed": False, "size": 1000}

    def delete_file(self, drive_marker, fid):
        self.deleted.append((drive_marker, fid))
        self.existing.discard(fid)

    def copy_file(self, drive_id, source_fid, folder_id, name):
        self.copy_calls += 1
        if self.copy_behaviour:
            action = self.copy_behaviour.pop(0)
            if action == "quota":
                raise tv._CopyQuotaExceeded("quota")
            if action == "forbidden":
                raise tv._CopyForbidden("forbidden")
        return {"id": "newfid", "size": 5000}

    def ensure_folder_cached(self, drive_id, dir_name):
        return "folder1", False

    def ensure_folder(self, drive_id, dir_name):
        return "folder1"

    def list_file_ids_by_name(self, drive_id, folder_id, name):
        return ["newfid"]

    def add_anyone_reader(self, drive_id, fid):
        return None


def _factory(client):
    def factory(db, user_id):
        return client, f"myDrive:{user_id}"
    return factory


def _variant(db, item_id, fid="copyfid", drive="userdrive:435", size=3000,
             last_played=None, created=None):
    row = em.TransferVariant(item_id=item_id, source_file_id="origfid",
                             variant_file_id=fid, variant_drive_id=drive,
                             owner_scope="user", owner_ref="435",
                             status="ready", size_bytes=size,
                             last_played_at=last_played,
                             created_at=created or datetime(2026, 10, 27))
    db.add(row)
    db.commit()
    return row


def _session(db, item_id, last_update, ended=None, paused=False):
    row = em.PlaybackSession(
        session_key=f"sk-{item_id}-{paused}-{last_update}",
        user_id=435, item_id=item_id, last_update_at=last_update,
        ended_at=ended, is_paused=paused)
    db.add(row)
    db.commit()
    return row


def test_space_sufficient_no_evict(db):
    row = _variant(db, 101, fid="a", size=3000)
    client = FakeDriveClient(existing={"a"}, free=9000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    assert result["reason"] == "sufficient" and result["proceed"] is True
    assert result["evicted"] == [] and client.deleted == []
    db.refresh(row)
    assert row.status == "ready"


def test_space_short_evicts_lru_order_and_stops_when_enough(db):
    old = _variant(db, 101, fid="a", size=4000,
                   created=datetime(2026, 10, 20))
    new = _variant(db, 102, fid="b", size=4000,
                   created=datetime(2026, 10, 21))
    newest = _variant(db, 103, fid="c", size=4000,
                      created=datetime(2026, 10, 22))
    client = FakeDriveClient(existing={"a", "b", "c"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=8000, now=NOW)
    # need = 7000：腾 a(4000) + b(4000) = 8000 即停，c 不动。
    assert result["reason"] == "evicted_enough"
    assert result["evicted"] == [old.id, new.id]
    assert client.deleted == [("myDrive:435", "a"), ("myDrive:435", "b")]
    for row, status in ((old, "deleted"), (new, "deleted"),
                        (newest, "ready")):
        db.refresh(row)
        assert row.status == status


def test_space_insufficient_after_all_candidates_reports_three_numbers(db):
    a = _variant(db, 101, fid="a", size=1000,
                 created=datetime(2026, 10, 20))
    b = _variant(db, 102, fid="b", size=1000,
                 created=datetime(2026, 10, 21))
    client = FakeDriveClient(existing={"a", "b"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    assert result["reason"] == "insufficient"
    assert result["proceed"] is False
    assert result["file_size"] == 5000 and result["free_bytes"] == 1000
    # 估算剩余 = 1000 + 2000 = 3000，还差 2000。
    assert result["shortfall"] == 2000
    assert result["evicted"] == [a.id, b.id]


def test_active_session_variant_not_evicted_and_ended_session_is_not(db):
    ended = _variant(db, 100, fid="e", size=3000,
                     created=datetime(2026, 10, 19))
    active = _variant(db, 101, fid="a", size=3000,
                      created=datetime(2026, 10, 20))
    other = _variant(db, 102, fid="b", size=3000,
                     created=datetime(2026, 10, 21))
    _session(db, 100, NOW - timedelta(seconds=30),
             ended=NOW - timedelta(seconds=10))
    _session(db, 101, NOW - timedelta(seconds=30))
    client = FakeDriveClient(existing={"e", "a", "b"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    # need = 4000：已结束会话的 e 可腾（3000）仍不足，再腾 b 才够；
    # 活跃会话的 a 全程不动。
    assert result["evicted"] == [ended.id, other.id]
    db.refresh(active)
    assert active.status == "ready"


def test_recently_played_variant_not_evicted(db):
    recent = _variant(db, 101, fid="a", size=3000,
                      created=datetime(2026, 10, 20),
                      last_played=NOW - timedelta(seconds=60))
    other = _variant(db, 102, fid="b", size=3000,
                     created=datetime(2026, 10, 21))
    client = FakeDriveClient(existing={"a", "b"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    assert result["evicted"] == [other.id]
    db.refresh(recent)
    assert recent.status == "ready"


def test_current_item_variant_not_evicted(db):
    current = _variant(db, 100, fid="a", size=3000,
                       created=datetime(2026, 10, 20))
    other = _variant(db, 200, fid="b", size=3000,
                     created=datetime(2026, 10, 21))
    client = FakeDriveClient(existing={"a", "b"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW,
        exclude_item_ids=[100])
    assert result["evicted"] == [other.id]
    db.refresh(current)
    assert current.status == "ready"


def test_pan115_ready_row_never_evicted(db):
    pan = _variant(db, 300, fid="panfid", drive="pan115:user-435",
                   size=5000, created=datetime(2026, 10, 20))
    drive_row = _variant(db, 400, fid="d", size=3000,
                         created=datetime(2026, 10, 21))
    client = FakeDriveClient(existing={"panfid", "d"}, free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    assert result["evicted"] == [drive_row.id]
    assert client.deleted == [("myDrive:435", "d")]
    db.refresh(pan)
    assert pan.status == "ready"


def test_pan115_only_insufficient_and_personal_file_untouched(db):
    pan = _variant(db, 300, fid="panfid", drive="pan115:user-435",
                   size=5000, created=datetime(2026, 10, 20))
    client = FakeDriveClient(existing={"panfid", "personalfile"},
                             free=1000)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    assert result["reason"] == "insufficient"
    assert result["evicted"] == [] and client.deleted == []
    db.refresh(pan)
    assert pan.status == "ready"


def test_quota_unknown_no_evict_and_proceeds(db):
    row = _variant(db, 101, fid="a", size=3000)
    client = FakeDriveClient(existing={"a"}, free=0, about_error=True)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=5000, now=NOW)
    assert result["reason"] == "quota_unknown"
    assert result["proceed"] is True and client.deleted == []
    db.refresh(row)
    assert row.status == "ready"


def test_unknown_size_no_evict_and_proceeds(db):
    row = _variant(db, 101, fid="a", size=3000)
    client = FakeDriveClient(existing={"a"}, free=0)
    result = tc.ensure_space_for_transfer(
        db, _factory(client), 435, file_size=None, now=NOW)
    assert result["reason"] == "unknown_size"
    assert result["proceed"] is True and client.deleted == []
    db.refresh(row)
    assert row.status == "ready"


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def test_copy_quota_vs_forbidden_classification(monkeypatch):
    cases = [
        ({"error": {"errors": [{"reason": "storageQuotaExceeded"}]}},
         tv._CopyQuotaExceeded),
        ({"error": {"message": "Storage quota exceeded for this account"}},
         tv._CopyQuotaExceeded),
        ({"error": {"errors": [{"reason": "forbidden"}]}},
         tv._CopyForbidden),
        ({"error": {"errors": [{"reason": "downloadQuotaExceeded"}]}},
         tv._CopyForbidden),
        ({"error": {"message": "Download quota exceeded"}},
         tv._CopyForbidden),
    ]
    for body, exc_type in cases:
        resp = _Resp(403, body)

        def fake_request(self, method, url, drive_id, **kwargs):
            return resp
        monkeypatch.setattr(tv.DriveApiClient, "_request", fake_request)
        client = tv.DriveApiClient()
        with pytest.raises(exc_type):
            client.copy_file("myDrive:1", "src", "folder", "name")
    # 404 仍是 _CopyForbidden，不是配额错。
    resp = _Resp(404, {})

    def fake_request_404(self, method, url, drive_id, **kwargs):
        return resp
    monkeypatch.setattr(tv.DriveApiClient, "_request", fake_request_404)
    with pytest.raises(tv._CopyForbidden):
        tv.DriveApiClient().copy_file("myDrive:1", "src", "folder", "name")
    # 配额错不是 _CopyForbidden 的子类：旧的“死文件”分支接不住它。
    assert not issubclass(tv._CopyQuotaExceeded, tv._CopyForbidden)


def test_about_storage_parsing(monkeypatch):
    def fake_request(self, method, url, drive_id, **kwargs):
        assert "about" in url and kwargs["params"] == {"fields": "storageQuota"}
        return _Resp(200, {"storageQuota": {"limit": "15000",
                                            "usage": "10000"}})
    monkeypatch.setattr(tv.DriveApiClient, "_request", fake_request)
    assert tv.DriveApiClient().about_storage("myDrive:1") == {
        "limit": 15000, "usage": 10000, "free": 5000}

    def fake_request_unlimited(self, method, url, drive_id, **kwargs):
        return _Resp(200, {"storageQuota": {"usage": "10000"}})
    monkeypatch.setattr(tv.DriveApiClient, "_request",
                        fake_request_unlimited)
    assert tv.DriveApiClient().about_storage("myDrive:1")["free"] is None


def test_midcopy_quota_evict_once_then_retry_succeeds(db):
    old = _variant(db, 101, fid="a", size=5000,
                   created=datetime(2026, 10, 20))
    client = FakeDriveClient(existing={"a"}, free=1000,
                             copy_behaviour=["quota"])
    state = {"retried": False}
    result = tv._attempt_user_copy(
        db, client, "myDrive:435", "orig", "folder", "name",
        _factory(client), 435, 999, 5000, state)
    assert result == {"id": "newfid", "size": 5000}
    assert client.copy_calls == 2 and state["retried"] is True
    assert client.deleted == [("myDrive:435", "a")]
    db.refresh(old)
    assert old.status == "deleted"


def test_midcopy_quota_twice_raises_without_second_evict(db):
    old = _variant(db, 101, fid="a", size=5000,
                   created=datetime(2026, 10, 20))
    newer = _variant(db, 102, fid="b", size=5000,
                     created=datetime(2026, 10, 21))
    client = FakeDriveClient(existing={"a", "b"}, free=1000,
                             copy_behaviour=["quota", "quota"])
    state = {"retried": False}
    with pytest.raises(tv._SpaceInsufficient) as excinfo:
        tv._attempt_user_copy(
            db, client, "myDrive:435", "orig", "folder", "name",
            _factory(client), 435, 999, 5000, state)
    assert client.copy_calls == 2 and state["retried"] is True
    # 只腾了第一轮的 a；二次失败没有再腾 b。
    assert client.deleted == [("myDrive:435", "a")]
    assert excinfo.value.file_size == 5000
    assert excinfo.value.free_bytes == 1000
    assert excinfo.value.shortfall == 4000
    db.refresh(newer)
    assert newer.status == "ready"


def test_job_insufficient_space_fails_without_fallback(db, monkeypatch):
    class FakeUserClient(FakeDriveClient):
        def __init__(self, db_, binding):
            super().__init__(existing=set(), free=1000)

    monkeypatch.setattr(tv, "UserDriveApiClient", FakeUserClient)
    monkeypatch.setattr(
        tv, "_active_binding",
        lambda db_, uid, provider: SimpleNamespace(id=1))
    job = SimpleNamespace(id=7, item_id=999, owner_scope="user",
                          owner_ref="435", status="pending",
                          error_kind=None, error_detail=None,
                          attempts=1, next_retry_at=None)
    item = SimpleNamespace(id=999, drive_file_id="orig",
                           size=5000, file_path="/x/movie.mkv",
                           name="movie")
    result = tv._run_drive_user_job(db, job, item, None)
    assert result["outcome"] == "failed"
    assert job.status == "failed"
    assert job.error_kind == "insufficient_space"
    assert "5000" in job.error_detail and "1000" in job.error_detail
    assert "4000" in job.error_detail
    # 没有静默回落：没有 ready 行被登记，也没有 Path B 的 done。
    assert db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "ready").count() == 0

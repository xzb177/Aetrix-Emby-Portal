# -*- coding: utf-8 -*-
"""全自动追剧（opt-in，2026-10-27 首批第二单）。

全部离线：不碰真实 Drive、不碰生产库、不对外发消息；转存任务
只建行（pending），工人执行（真 copy）不在本单测试范围（已有
转存工人测试覆盖）。

覆盖：
- 订阅幂等：已订阅再订不重复建、取消后重订原地恢复、非 series
  条目拒绝、不存在的剧拒绝；
- 扫集只转订阅之后到货的集：旧集不转、date_added 为空的集跳过
  并计数、已 ready 的集不重复建、24h 节流内不重复建；
- 无绑定不建单（reason=no_binding，不污染节流窗）、115 源跳过
  （unsupported_provider_115）、未知 provider 跳过；
- 开关关时扫集零结果、接口 404；开关开时 HTTP 订阅/状态口径
  与状态可见性（ready / pending / not_queued + queue_reason）；
- 状态接口只读：调状态不建任务。
"""
import os
import tempfile
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest
from fastapi.testclient import TestClient

from backend import models as base_models
from backend.api.user import get_current_user
from backend.database import SessionLocal, get_db, init_db
from backend.emby_server import models as em
from backend.emby_server import series_follow as sf
from backend.main import app

init_db()

T0 = datetime(2026, 10, 27, 10, 0, 0)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.SeriesFollow, em.TransferJob,
                      em.TransferVariant, em.MediaItem,
                      em.UserStorageBinding, em.StorageMount,
                      base_models.WebUser):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def user(db):
    u = base_models.WebUser(username="followuser", is_staff=False,
                            is_active=True, password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client():
    app.dependency_overrides[get_db] = lambda: SessionLocal()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def authed(client, user):
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield client
    finally:
        del app.dependency_overrides[get_current_user]


def _mount(db):
    m = em.StorageMount(name="drive-mount", mount_type="drive",
                        path="drive:shared123")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _series(db):
    row = em.MediaItem(guid="g-series", library_id=1, item_type="series",
                       name="测试剧")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _episode(db, series, mount, guid, added, fid="fid-1"):
    row = em.MediaItem(guid=guid, library_id=1, item_type="episode",
                       name="第 1 集", series_id=series.id,
                       season_number=1, episode_number=1,
                       file_path=f"mount://{mount.id}/ep.mkv",
                       drive_file_id=fid, date_added=added)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _follow(db, user_id, series_id, created_at=T0):
    row = em.SeriesFollow(user_id=user_id, series_item_id=series_id,
                          status="active", created_at=created_at)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _binding(db, user_id):
    row = em.UserStorageBinding(user_id=user_id, provider="drive",
                                account_label="mydrive",
                                credential_id=999, status="active")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _enable(monkeypatch):
    monkeypatch.setenv("SERIES_FOLLOW_ENABLED", "1")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


def test_subscribe_idempotent_and_rejects_non_series(db, user):
    series = _series(db)
    out = sf.subscribe(db, user.id, series.id)
    assert out["subscribed"] is True and out["reason"] == "subscribed"
    out2 = sf.subscribe(db, user.id, series.id)
    assert out2["reason"] == "already_active"
    assert out2["follow_id"] == out["follow_id"]
    assert db.query(em.SeriesFollow).count() == 1
    movie = em.MediaItem(guid="g-movie", library_id=1, item_type="movie",
                         name="电影")
    db.add(movie)
    db.commit()
    assert sf.subscribe(db, user.id, movie.id)["reason"] == "not_a_series"
    assert sf.subscribe(db, user.id, 999999)["reason"] == "series_not_found"
    out3 = sf.unsubscribe(db, user.id, series.id)
    assert out3["reason"] == "cancelled"
    out4 = sf.subscribe(db, user.id, series.id)
    assert out4["reason"] == "resubscribed"
    assert db.query(em.SeriesFollow).count() == 1


def test_sweep_only_new_episodes_and_ready_and_throttle(db, user,
                                                        monkeypatch):
    _enable(monkeypatch)
    mount = _mount(db)
    series = _series(db)
    _follow(db, user.id, series.id)
    _binding(db, user.id)
    old = _episode(db, series, mount, "g-old",
                   datetime(2026, 10, 27, 9, 0, 0))
    new = _episode(db, series, mount, "g-new",
                   datetime(2026, 10, 27, 12, 0, 0))
    # date_added 为空的 legacy 行：ORM 列默认值会把显式 None 填回
    # 当前时间，故用原始 SQL 置 NULL 才能真实复现空值行。
    unknown = _episode(db, series, mount, "g-unknown",
                       datetime(2026, 10, 27, 12, 0, 0))
    from sqlalchemy import text as _text
    db.execute(_text("UPDATE emby_items SET date_added=NULL WHERE id=:i"),
               {"i": unknown.id})
    db.commit()
    db.refresh(unknown)
    assert unknown.date_added is None
    ready_ep = _episode(db, series, mount, "g-ready",
                        datetime(2026, 10, 27, 12, 0, 0), fid="fid-ready")
    db.add(em.TransferVariant(
        item_id=ready_ep.id, source_file_id="fid-ready",
        variant_file_id="copy-ready", variant_drive_id=f"userdrive:{user.id}",
        owner_scope="user", owner_ref=str(user.id), status="ready"))
    db.commit()
    out = sf.process_series_follows(db)
    assert out["jobs_created"] == 1
    assert out["skipped"].get("unknown_date") == 1
    jobs = db.query(em.TransferJob).all()
    assert len(jobs) == 1
    assert jobs[0].item_id == new.id
    assert jobs[0].trigger == "series_follow"
    assert jobs[0].provider == "drive"
    assert jobs[0].dest_drive_id == f"userdrive:{user.id}"
    assert jobs[0].status == "pending"
    # 第二轮：新集已有任务（24h 节流）+ ready 集仍不建
    out2 = sf.process_series_follows(db)
    assert out2["jobs_created"] == 0
    assert out2["skipped"].get("throttled") == 1
    assert db.query(em.TransferJob).count() == 1


def test_sweep_no_binding_and_115_and_unknown(db, user, monkeypatch):
    _enable(monkeypatch)
    mount = _mount(db)
    series = _series(db)
    _follow(db, user.id, series.id)
    # 无绑定：不建单
    new = _episode(db, series, mount, "g-nobind",
                   datetime(2026, 10, 27, 12, 0, 0))
    out = sf.process_series_follows(db)
    assert out["skipped"].get("no_binding") == 1
    assert db.query(em.TransferJob).count() == 0
    # 绑上后下一轮即建单（无节流污染）
    _binding(db, user.id)
    out2 = sf.process_series_follows(db)
    assert out2["jobs_created"] == 1
    # 115 源与未知 provider 各跳过
    ep115 = em.MediaItem(guid="g-115", library_id=1, item_type="episode",
                         name="115 集", series_id=series.id,
                         season_number=1, episode_number=2,
                         file_path="115:/ep.mkv", drive_file_id="fid115",
                         date_added=datetime(2026, 10, 27, 13, 0, 0))
    ep_local = em.MediaItem(guid="g-local", library_id=1,
                            item_type="episode", name="本地集",
                            series_id=series.id, season_number=1,
                            episode_number=3,
                            file_path="/media/ep.mkv",
                            drive_file_id="fidlocal",
                            date_added=datetime(2026, 10, 27, 13, 0, 0))
    db.add_all([ep115, ep_local])
    db.commit()
    out3 = sf.process_series_follows(db)
    assert out3["skipped"].get("unsupported_provider_115") == 1
    assert out3["skipped"].get("unknown_provider") == 1


def test_switch_off_sweep_is_noop_and_status_readonly(db, user,
                                                      monkeypatch):
    monkeypatch.setenv("SERIES_FOLLOW_ENABLED", "0")
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")
    mount = _mount(db)
    series = _series(db)
    _follow(db, user.id, series.id)
    _binding(db, user.id)
    _episode(db, series, mount, "g-off",
             datetime(2026, 10, 27, 12, 0, 0))
    assert sf.process_series_follows(db)["jobs_created"] == 0
    assert db.query(em.TransferJob).count() == 0
    status = sf.follow_status(db, user.id)
    assert status["follows"][0]["episodes"][0]["state"] == "not_queued"
    assert db.query(em.TransferJob).count() == 0  # 状态口只读


def test_http_subscribe_status_and_switch_off(authed, db, user,
                                              monkeypatch):
    _enable(monkeypatch)
    mount = _mount(db)
    series = _series(db)
    r = authed.post("/api/user/series-follows",
                    json={"series_item_id": series.id})
    assert r.status_code == 200, r.text
    r = authed.post("/api/user/series-follows",
                    json={"series_item_id": series.id})
    assert r.json()["reason"] == "already_active"
    r = authed.get("/api/user/series-follows")
    assert r.status_code == 200
    assert r.json()["follows"][0]["series_name"] == "测试剧"
    # 开关关时三口一律 404
    monkeypatch.setenv("SERIES_FOLLOW_ENABLED", "0")
    assert authed.get("/api/user/series-follows").status_code == 404
    assert authed.post("/api/user/series-follows",
                       json={"series_item_id": series.id}).status_code == 404
    assert authed.delete(
        f"/api/user/series-follows/{series.id}").status_code == 404


def test_http_status_shows_job_state(authed, db, user, monkeypatch):
    _enable(monkeypatch)
    mount = _mount(db)
    series = _series(db)
    _follow(db, user.id, series.id)
    _binding(db, user.id)
    ep = _episode(db, series, mount, "g-http",
                  datetime(2026, 10, 27, 12, 0, 0))
    sf.process_series_follows(db)
    body = authed.get("/api/user/series-follows").json()
    ep_row = body["follows"][0]["episodes"][0]
    assert ep_row["episode_id"] == ep.id
    assert ep_row["state"] == "pending"
    assert ep_row["job_id"] is not None
    # 取消订阅后状态口仍列该剧（status=cancelled），不删行
    authed.delete(f"/api/user/series-follows/{series.id}")
    body2 = authed.get("/api/user/series-follows").json()
    assert body2["follows"][0]["status"] == "cancelled"

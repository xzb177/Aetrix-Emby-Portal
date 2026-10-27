# -*- coding: utf-8 -*-
"""配额方案 1：共享 Drive 限速 + 错峰。

总闸默认关。开闸后：未超限放行、超限 429 且错误码稳定、
已知 Quota/429 且没有用户副本时 409 要求先转存、总闸关保持旧行为。
不覆盖多账号轮换或秒传池。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from starlette.responses import Response

from backend import models
from backend.emby_server import api, play_events, play_sign, playback_strategy
from backend.emby_server import models as em
from backend.emby_server import shared_quota_pace as pace
from backend.emby_server import transfer_variant as tv
from backend.integrations import store

FID = "file-shared-1"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(pace, "_redis_client", lambda: None)
    pace.reset_for_tests()
    play_events.reset_for_tests()
    yield
    pace.reset_for_tests()
    play_events.reset_for_tests()


@pytest.fixture()
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", factory)
    return factory


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _user(uid=1):
    return SimpleNamespace(id=uid, is_staff=False)


def _enable(monkeypatch, *, global_cap="8", cooldown="0", per_file="0",
            lease="120"):
    monkeypatch.setenv(pace.ENABLED_ENV, "1")
    monkeypatch.setenv(pace.GLOBAL_CAP_ENV, str(global_cap))
    monkeypatch.setenv(pace.USER_COOLDOWN_ENV, str(cooldown))
    monkeypatch.setenv(pace.PER_FILE_ENV, str(per_file))
    monkeypatch.setenv(pace.LEASE_ENV, str(lease))


def _code(exc: HTTPException) -> str:
    return (exc.headers or {}).get("X-Error-Code", "")


# ---------------------------------------------------------------------------
# 决策层
# ---------------------------------------------------------------------------

def test_flag_off_does_not_limit_or_count(monkeypatch):
    monkeypatch.setenv(pace.ENABLED_ENV, "0")
    monkeypatch.setenv(pace.PER_FILE_ENV, "1")
    for uid in (1, 2, 3):
        assert pace.admit_shared_start(None, _user(uid), "same-file") is None
    assert pace.counters() == {"rate_limit_hit": 0, "quota_forced_transfer": 0}
    assert pace.transfer_required_http(None, personal_copy=False) is None
    # 关闸期间的请求没有占租约：开闸后单文件上限仍从零计。
    _enable(monkeypatch, per_file="1", global_cap="8")
    assert pace.admit_shared_start(None, _user(1), "same-file") is None
    denied = pace.admit_shared_start(None, _user(2), "same-file")
    assert denied is not None
    assert denied.status_code == 429
    assert _code(denied) == pace.CODE_PER_FILE


def test_under_limit_admits_and_refresh_is_free(monkeypatch):
    _enable(monkeypatch, global_cap="2", cooldown="30", per_file="2")
    assert pace.admit_shared_start(None, _user(1), "file-a") is None
    # 同一用户同一文件的后续 Range 只刷新，不占新名额、不吃冷却。
    assert pace.admit_shared_start(None, _user(1), "file-a") is None
    assert pace.admit_shared_start(None, _user(2), "file-b") is None
    assert pace.counters()["rate_limit_hit"] == 0


def test_over_global_cap_stable_code(monkeypatch):
    _enable(monkeypatch, global_cap="1", cooldown="0", per_file="0")
    assert pace.admit_shared_start(None, _user(1), "file-a") is None
    denied = pace.admit_shared_start(None, _user(2), "file-b")
    assert denied.status_code == 429
    assert _code(denied) == pace.CODE_GLOBAL
    assert denied.detail.startswith(pace.CODE_GLOBAL + ":")
    assert denied.headers["Retry-After"]
    assert pace.counters()["rate_limit_hit"] == 1


def test_user_cooldown_blocks_new_file_not_refresh(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr(pace, "_now", lambda: now["t"])
    _enable(monkeypatch, global_cap="10", cooldown="10", per_file="0", lease="120")
    assert pace.admit_shared_start(None, _user(1), "file-a") is None
    now["t"] = 1_005.0
    assert pace.admit_shared_start(None, _user(1), "file-a") is None
    denied = pace.admit_shared_start(None, _user(1), "file-b")
    assert denied.status_code == 429
    assert _code(denied) == pace.CODE_USER_COOLDOWN
    assert denied.detail.startswith(pace.CODE_USER_COOLDOWN + ":")
    # 别的用户不受这一人的冷却影响。
    assert pace.admit_shared_start(None, _user(2), "file-b") is None
    now["t"] = 1_011.0
    assert pace.admit_shared_start(None, _user(1), "file-b") is None


def test_per_file_cap_optional_zero_means_off(monkeypatch):
    _enable(monkeypatch, global_cap="10", cooldown="0", per_file="0")
    assert pace.admit_shared_start(None, _user(1), "file-a") is None
    assert pace.admit_shared_start(None, _user(2), "file-a") is None
    _enable(monkeypatch, global_cap="10", cooldown="0", per_file="1")
    pace.reset_for_tests()
    assert pace.admit_shared_start(None, _user(1), "file-a") is None
    denied = pace.admit_shared_start(None, _user(2), "file-a")
    assert _code(denied) == pace.CODE_PER_FILE
    assert pace.admit_shared_start(None, _user(2), "file-b") is None


def test_quota_forces_transfer_only_when_enabled_and_no_copy(monkeypatch):
    monkeypatch.delenv(pace.ENABLED_ENV, raising=False)
    assert pace.transfer_required_http(None, personal_copy=False) is None
    _enable(monkeypatch)
    skipped = pace.transfer_required_http(None, personal_copy=True)
    assert skipped is None
    forced = pace.transfer_required_http(None, personal_copy=False)
    assert forced.status_code == 409
    assert _code(forced) == pace.CODE_TRANSFER_REQUIRED
    assert forced.detail.startswith(pace.CODE_TRANSFER_REQUIRED + ":")
    assert "转存" in forced.detail
    assert pace.counters()["quota_forced_transfer"] == 1


def test_known_quota_http_is_429_only():
    assert pace.is_known_drive_quota_http(
        HTTPException(status_code=429, detail="源站返回 429"))
    assert pace.is_known_drive_quota_http(HTTPException(
        status_code=503, detail="媒体来源暂不可用：files.get 返回 429"))
    assert not pace.is_known_drive_quota_http(
        HTTPException(status_code=403, detail="forbidden"))
    assert not pace.is_known_drive_quota_http(
        HTTPException(status_code=503, detail="Drive 凭据不可用"))
    assert not pace.is_known_drive_quota_http(
        HTTPException(status_code=503, detail="files.get 返回 1429"))
    assert not pace.is_known_drive_quota_http(
        HTTPException(status_code=502, detail="源站返回 502"))


def test_personal_copy_and_byos_are_not_shared_starts():
    item = SimpleNamespace(drive_file_id=FID, file_path="mount://3/a.mkv")
    user_copy = SimpleNamespace(owner_scope="user")
    source_copy = SimpleNamespace(owner_scope="source")
    assert pace.is_personal_copy(user_copy) is True
    assert pace.is_personal_copy(source_copy) is False
    assert pace.is_personal_copy(None) is False
    assert pace.is_shared_drive_start(item, None, None) is True
    assert pace.is_shared_drive_start(item, None, source_copy) is True
    assert pace.is_shared_drive_start(item, None, user_copy) is False
    assert pace.is_shared_drive_start(
        item, "byos_drive_relay", None) is False
    byos = SimpleNamespace(drive_file_id=FID, file_path="byos://9/a.mkv")
    assert pace.is_shared_drive_start(byos, None, None) is False
    local = SimpleNamespace(drive_file_id="", file_path="/media/a.mkv")
    assert pace.is_shared_drive_start(local, None, None) is False


def test_system_config_and_env_override(db, monkeypatch):
    for name in (pace.ENABLED_ENV, pace.GLOBAL_CAP_ENV, pace.USER_COOLDOWN_ENV,
                 pace.PER_FILE_ENV, pace.LEASE_ENV):
        monkeypatch.delenv(name, raising=False)
    db.add(models.SystemConfig(key=pace.KEY_ENABLED, value="1"))
    db.add(models.SystemConfig(key=pace.KEY_GLOBAL, value="4"))
    db.commit()
    store.invalidate()
    cfg = pace.load_config(db)
    assert cfg.enabled is True
    assert cfg.global_cap == 4
    assert cfg.user_cooldown_sec == 3
    assert cfg.per_file_cap == 2
    monkeypatch.setenv(pace.ENABLED_ENV, "0")
    monkeypatch.setenv(pace.GLOBAL_CAP_ENV, "1")
    overridden = pace.load_config(db)
    assert overridden.enabled is False
    monkeypatch.setenv(pace.ENABLED_ENV, "1")
    enabled_override = pace.load_config(db)
    assert enabled_override.enabled is True
    assert enabled_override.global_cap == 1


def test_bad_number_falls_back(monkeypatch):
    _enable(monkeypatch, global_cap="nope")
    cfg = pace.load_config(None)
    assert cfg.enabled is True
    assert cfg.global_cap == 8


# ---------------------------------------------------------------------------
# video_stream：总闸关走旧路，开闸才拦
# ---------------------------------------------------------------------------

def _request(query=b""):
    scope = {
        "type": "http", "method": "GET", "path": "/emby/Videos/x/stream",
        "raw_path": b"/emby/Videos/x/stream", "query_string": query,
        "headers": [], "scheme": "http", "server": ("testserver", 80),
        "client": ("testclient", 50000), "root_path": "",
    }
    return Request(scope)


def _drive_item(guid="item-1"):
    item = em.MediaItem()
    item.guid = guid
    item.name = "测试片"
    item.file_path = "mount://3/电影/测试片.mkv"
    item.drive_file_id = FID
    item.item_type = "movie"
    item.container = "mkv"
    item.size = 1_000
    return item


def _stub_switches(monkeypatch):
    monkeypatch.setattr(api, "_drive_mount_id_for_item",
                        lambda db_, it: "drive-x")
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return "true"
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return "true"
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)


def _stub_gates(monkeypatch, item):
    monkeypatch.setattr(api, "_require_visible_item",
                        lambda db_, u_, iid: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db_, u_: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db_, u_, r=None: None)
    monkeypatch.setattr(play_sign, "check_referer", lambda request, db_: None)
    monkeypatch.setattr(api, "_enforce_stream_limit",
                        lambda db_, req, u_, it, iid: None)
    monkeypatch.setattr(api, "drive_exists_rapid_retry",
                        lambda request, user_, fid: False)
    monkeypatch.setattr(
        "backend.emby_server.drive_auth.drive_usercontent_download_url",
        lambda fid: f"https://usercontent.test/{fid}")


def _set_direct_unavailable(db):
    db.add(em.VariantAvailability(
        variant_key="drive_direct", availability_status="unavailable",
        consecutive_failures=3))
    db.commit()


def _call(monkeypatch, db, item, user, *, relay, ensure, query=b"", calls=None):
    _stub_switches(monkeypatch)
    _stub_gates(monkeypatch, item)
    if calls is None:
        calls = {"relay": 0, "ensure": 0}

    async def fake_relay(request, drive_id, fid, **kwargs):
        calls["relay"] += 1
        if isinstance(relay, BaseException):
            raise relay
        return relay

    async def fake_ensure(db_, it, fid, **kwargs):
        calls["ensure"] += 1
        if isinstance(ensure, BaseException):
            raise ensure
        return ensure

    monkeypatch.setattr(api, "serve_drive_relay_async", fake_relay)
    monkeypatch.setattr(api, "_ensure_drive_file_playable", fake_ensure)
    resp = asyncio.run(api.video_stream(
        item.guid, _request(query), user=user, db=db))
    return resp, calls


def test_stream_under_limit_still_302(db, monkeypatch):
    _enable(monkeypatch, global_cap="4", cooldown="0", per_file="0")
    resp, calls = _call(
        monkeypatch, db, _drive_item(), _user(1),
        relay=Response(status_code=206), ensure=True)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"https://usercontent.test/{FID}"
    assert calls["ensure"] == 1
    assert pace.counters()["rate_limit_hit"] == 0


def test_stream_over_limit_rejected_before_shared_fetch(db, monkeypatch):
    _enable(monkeypatch, global_cap="1", cooldown="0", per_file="0")
    first, calls1 = _call(
        monkeypatch, db, _drive_item("a"), _user(1),
        relay=Response(status_code=206), ensure=True)
    assert first.status_code == 302
    assert calls1["ensure"] == 1
    calls2 = {"relay": 0, "ensure": 0}
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, db, _drive_item("b"), _user(2),
              relay=Response(status_code=206), ensure=True, calls=calls2)
    assert excinfo.value.status_code == 429
    assert _code(excinfo.value) == pace.CODE_GLOBAL
    assert calls2 == {"relay": 0, "ensure": 0}
    assert pace.counters()["rate_limit_hit"] == 1


def test_stream_quota_page_forces_transfer_and_skips_302(db, monkeypatch):
    _enable(monkeypatch, global_cap="8", cooldown="0", per_file="0")
    _set_direct_unavailable(db)
    calls = {"relay": 0, "ensure": 0}
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, db, _drive_item(), _user(1),
              relay=tv.QuotaPageError("quota page"), ensure=True, calls=calls)
    assert excinfo.value.status_code == 409
    assert _code(excinfo.value) == pace.CODE_TRANSFER_REQUIRED
    assert "转存" in excinfo.value.detail
    assert calls == {"relay": 1, "ensure": 0}
    assert pace.counters()["quota_forced_transfer"] == 1


def test_stream_quota_page_flag_off_still_falls_through_to_302(db, monkeypatch):
    monkeypatch.setenv(pace.ENABLED_ENV, "0")
    _set_direct_unavailable(db)
    resp, calls = _call(
        monkeypatch, db, _drive_item(), _user(1),
        relay=tv.QuotaPageError("quota page"), ensure=True)
    assert resp.status_code == 302
    assert calls["ensure"] == 1
    assert calls["relay"] == 1
    assert pace.counters()["quota_forced_transfer"] == 0


def test_stream_relay_429_forces_transfer_when_enabled(db, monkeypatch):
    _enable(monkeypatch, global_cap="8", cooldown="0", per_file="0")
    _set_direct_unavailable(db)
    calls = {"relay": 0, "ensure": 0}
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, db, _drive_item(), _user(1),
              relay=HTTPException(status_code=429, detail="源站返回 429"),
              ensure=True, calls=calls)
    assert excinfo.value.status_code == 409
    assert _code(excinfo.value) == pace.CODE_TRANSFER_REQUIRED
    assert calls == {"relay": 1, "ensure": 0}


def test_stream_relay_429_flag_off_is_unchanged(db, monkeypatch):
    monkeypatch.setenv(pace.ENABLED_ENV, "0")
    _set_direct_unavailable(db)
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, db, _drive_item(), _user(1),
              relay=HTTPException(status_code=429, detail="源站返回 429"),
              ensure=True)
    assert excinfo.value.status_code == 429
    assert excinfo.value.detail == "源站返回 429"
    assert pace.counters()["quota_forced_transfer"] == 0


def test_stream_files_get_429_forces_transfer(db, monkeypatch):
    _enable(monkeypatch, global_cap="8", cooldown="0", per_file="0")
    with pytest.raises(HTTPException) as excinfo:
        _call(
            monkeypatch, db, _drive_item(), _user(1),
            relay=Response(status_code=206),
            ensure=HTTPException(
                status_code=503,
                detail="媒体来源暂不可用：files.get 返回 429"))
    assert excinfo.value.status_code == 409
    assert _code(excinfo.value) == pace.CODE_TRANSFER_REQUIRED
    assert "变体切换" not in excinfo.value.detail


def test_stream_files_get_429_flag_off_keeps_old_503(db, monkeypatch):
    monkeypatch.setenv(pace.ENABLED_ENV, "0")
    with pytest.raises(HTTPException) as excinfo:
        _call(
            monkeypatch, db, _drive_item(), _user(1),
            relay=Response(status_code=206),
            ensure=HTTPException(
                status_code=503,
                detail="媒体来源暂不可用：files.get 返回 429"))
    assert excinfo.value.status_code == 503
    assert "变体切换后仍失败" in excinfo.value.detail
    assert pace.counters()["quota_forced_transfer"] == 0


def test_stream_personal_copy_quota_keeps_old_fallback(db, monkeypatch):
    _enable(monkeypatch, global_cap="8", cooldown="0", per_file="0")
    _set_direct_unavailable(db)

    def fake_resolve(db_, item_, user_):
        return "copy-file", SimpleNamespace(
            owner_scope="user", variant_drive_id="drive-x",
            variant_file_id="copy-file", id=7)

    monkeypatch.setattr(tv, "resolve_play_file_id", fake_resolve)
    resp, calls = _call(
        monkeypatch, db, _drive_item(), _user(1),
        relay=tv.QuotaPageError("quota page"), ensure=True)
    assert resp.status_code == 302
    assert calls["ensure"] == 1
    assert pace.counters()["quota_forced_transfer"] == 0


def test_stream_relay_403_is_not_treated_as_quota(db, monkeypatch):
    _enable(monkeypatch, global_cap="8", cooldown="0", per_file="0")
    _set_direct_unavailable(db)
    db.add(em.VariantAvailability(
        variant_key="drive_relay", availability_status="available",
        consecutive_failures=0))
    db.commit()
    with pytest.raises(HTTPException) as excinfo:
        _call(monkeypatch, db, _drive_item(), _user(1),
              relay=HTTPException(status_code=403, detail="forbidden"),
              ensure=True)
    assert excinfo.value.status_code == 409
    assert "变体切换失败" in excinfo.value.detail
    assert pace.CODE_TRANSFER_REQUIRED not in str(excinfo.value.detail)
    assert pace.counters()["quota_forced_transfer"] == 0

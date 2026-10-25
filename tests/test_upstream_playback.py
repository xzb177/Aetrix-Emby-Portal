# -*- coding: utf-8 -*-
"""上游播放接入（播放阶段 7，#25-4）。

钉死三条（开关开时）：
- 先探后 302：探到可播（200/206）才返回地址，探到问题一律结构化
  错误（code + 状态码），不把死链、401、429 原样丢给播放器；
- 超限结构化：上游 429 → 429 + upstream_quota_exhausted（Retry-After
  有才带）；403 只报拒绝不谎称额度；401 报凭据失效 409；
- 开关关时全拒绝（404 upstream_playback_disabled），本地播放无影响。

全部离线：DB 用内存 SQLite；上游 HTTP 全靠注入的 http_probe /
http_post 桩（与 25-3 的 http_get 注入同口径）；凭据密封走
25-1 的 seal/unseal（同进程内 SECRET_KEY 默认随机值即可）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models
from backend.emby_server import api as api_mod
from backend.emby_server import models as em
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

ENV = "UPSTREAM_PLAYBACK_ENABLED"


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _source(db, **kw):
    kw.setdefault("name", "upstream-a")
    kw.setdefault("source_type", "emby")
    kw.setdefault("base_url", "https://up.example.com")
    kw.setdefault("auth_method", "api_key")
    row = us.create_source(db, **kw)
    db.commit()
    return row


def _enable(db, row):
    us.enable_source(db, row)
    db.commit()
    return row


def _probe_ok(captured):
    def probe(url, headers, timeout):
        captured.append(url)
        return 206, None
    return probe


def _probe_status(status, retry_after=None):
    def probe(url, headers, timeout):
        return status, retry_after
    return probe


def test_switch_off_by_default(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert up.playback_enabled() is False


def test_switch_on_via_env(monkeypatch):
    monkeypatch.setenv(ENV, "1")
    assert up.playback_enabled() is True
    monkeypatch.setenv(ENV, "off")
    assert up.playback_enabled() is False


def test_build_url_api_key_and_media_source():
    url = up.build_upstream_stream_url(
        "https://up.example.com/", "item 42",
        api_key="KEY123", media_source_id="ms-9")
    assert url.startswith("https://up.example.com/Videos/item%2042/stream?")
    assert "static=true" in url
    assert "mediaSourceId=ms-9" in url
    assert "api_key=KEY123" in url


def test_build_url_anonymous_has_no_api_key():
    url = up.build_upstream_stream_url("https://up.example.com", "abc")
    assert "api_key" not in url
    assert url == "https://up.example.com/Videos/abc/stream?static=true"


def test_build_url_empty_item_rejected():
    with pytest.raises(ValueError):
        up.build_upstream_stream_url("https://up.example.com", "  ")


def test_resolve_switch_off_returns_disabled(db, monkeypatch):
    monkeypatch.setenv(ENV, "0")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(db, 1, "x")
    assert exc.value.status == 404
    assert exc.value.code == "upstream_playback_disabled"


def test_resolve_source_not_found(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(db, 9999, "x")
    assert exc.value.status == 404
    assert exc.value.code == "upstream_source_not_found"


def test_resolve_disabled_source_rejected(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _source(db)  # 新建恒为关闭
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(db, row.id, "x")
    assert exc.value.status == 409
    assert exc.value.code == "upstream_source_disabled"


def test_resolve_anonymous_probe_ok(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db))
    captured = []
    url = up.resolve_upstream_stream(
        db, row.id, "item-1", http_probe=_probe_ok(captured))
    assert url == "https://up.example.com/Videos/item-1/stream?static=true"
    assert captured and "api_key" not in captured[0]


def test_resolve_api_key_probe_sees_key(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db, secret="K-SECRET"))
    captured = []
    url = up.resolve_upstream_stream(
        db, row.id, "item-1", http_probe=_probe_ok(captured))
    assert "api_key=K-SECRET" in url
    assert "api_key=K-SECRET" in captured[0]


def test_resolve_quota_exhausted_with_retry_after(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db))
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1",
            http_probe=_probe_status(429, retry_after=120))
    assert exc.value.status == 429
    assert exc.value.code == "upstream_quota_exhausted"
    assert exc.value.retry_after == 120
    assert exc.value.to_body()["retry_after"] == 120


def test_resolve_quota_without_retry_after_omits_field(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db))
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_status(429))
    assert exc.value.retry_after is None
    assert "retry_after" not in exc.value.to_body()


def test_resolve_forbidden_not_claimed_as_quota(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db))
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_status(403))
    assert exc.value.status == 403
    assert exc.value.code == "upstream_forbidden"


def test_resolve_probe_401_is_credential_invalid(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db))
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_status(401))
    assert exc.value.status == 409
    assert exc.value.code == "upstream_credential_invalid"


def test_resolve_probe_404_and_500_and_none(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db))
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_status(404))
    assert (exc.value.status, exc.value.code) == (404, "upstream_item_not_found")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_status(500))
    assert (exc.value.status, exc.value.code) == (502, "upstream_unavailable")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_status(None))
    assert (exc.value.status, exc.value.code) == (502, "upstream_unavailable")


def test_password_auth_exchanges_token(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(
        db, auth_method="password", username="alice", secret="pw123"))
    posted = {}

    def fake_post(url, payload, headers, timeout):
        posted["url"] = url
        posted["payload"] = payload
        assert payload["Username"] == "alice"
        assert payload["Pw"] == "pw123"
        return 200, {"AccessToken": "TOK-9"}

    captured = []
    url = up.resolve_upstream_stream(
        db, row.id, "item-1",
        http_probe=_probe_ok(captured), http_post=fake_post)
    assert posted["url"] == "https://up.example.com/Users/AuthenticateByName"
    assert "api_key=TOK-9" in url


def test_password_auth_401_is_credential_invalid(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(
        db, auth_method="password", username="alice", secret="pw123"))

    def fake_post(url, payload, headers, timeout):
        return 401, {}

    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1",
            http_probe=_probe_ok([]), http_post=fake_post)
    assert (exc.value.status, exc.value.code) == (
        409, "upstream_credential_invalid")


def test_password_auth_unreachable_is_unavailable(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(
        db, auth_method="password", username="alice", secret="pw123"))

    def fake_post(url, payload, headers, timeout):
        return None, None

    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1",
            http_probe=_probe_ok([]), http_post=fake_post)
    assert (exc.value.status, exc.value.code) == (502, "upstream_unavailable")


def test_broken_seal_is_credential_invalid(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db, secret="K-SECRET"))
    # 换掉派生密钥：旧密封值解不开 → SealedSecretError → 凭据失效 409
    monkeypatch.setattr("backend.security.SECRET_KEY", "rotated-key")
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_ok([]))
    assert (exc.value.status, exc.value.code) == (
        409, "upstream_credential_invalid")


def test_media_source_id_passthrough(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db))
    url = up.resolve_upstream_stream(
        db, row.id, "item-1", media_source_id="ms-7",
        http_probe=_probe_ok([]))
    assert "mediaSourceId=ms-7" in url


def _route_request(query=b""):
    scope = {"type": "http", "method": "GET",
             "path": "/emby/Videos/upstream/1/item-1/stream",
             "raw_path": b"/emby/Videos/upstream/1/item-1/stream",
             "query_string": query,
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _stub_route_auth(monkeypatch):
    monkeypatch.setattr(api_mod, "ensure_playback_allowed",
                        lambda db, user: None)

    async def fake_run_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("backend.emby_server.async_db.run_db", fake_run_db)


def test_route_switch_off_returns_structured_404(db, monkeypatch):
    monkeypatch.setenv(ENV, "0")
    _stub_route_auth(monkeypatch)
    resp = asyncio.run(api_mod.upstream_stream(
        1, "x", _route_request(), SimpleNamespace(id=7), db))
    assert resp.status_code == 404
    assert json.loads(resp.body.decode())["code"] == "upstream_playback_disabled"


def test_route_probe_ok_redirects_302(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _stub_route_auth(monkeypatch)
    row = _enable(db, _source(db))
    monkeypatch.setattr(
        up, "_default_http_range_probe",
        lambda url, headers, timeout: (206, None))
    resp = asyncio.run(api_mod.upstream_stream(
        row.id, "item-1", _route_request(), SimpleNamespace(id=7), db))
    assert resp.status_code == 302
    assert ("https://up.example.com/Videos/item-1/stream"
            in resp.headers["location"])


def test_route_quota_returns_structured_429(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    _stub_route_auth(monkeypatch)
    row = _enable(db, _source(db))
    monkeypatch.setattr(
        up, "_default_http_range_probe",
        lambda url, headers, timeout: (429, 60))
    resp = asyncio.run(api_mod.upstream_stream(
        row.id, "item-1", _route_request(), SimpleNamespace(id=7), db))
    assert resp.status_code == 429
    body = json.loads(resp.body.decode())
    assert body["code"] == "upstream_quota_exhausted"
    assert body["retry_after"] == 60
    assert body["source_id"] == row.id


def test_error_body_never_contains_url_or_secret(db, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    row = _enable(db, _source(db, secret="K-SECRET"))
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, row.id, "item-1", http_probe=_probe_status(429))
    body = exc.value.to_body()
    assert body == {
        "code": "upstream_quota_exhausted",
        "message": exc.value.message,
        "source_id": row.id,
    }
    assert "K-SECRET" not in str(body)
    assert "up.example.com" not in str(body)

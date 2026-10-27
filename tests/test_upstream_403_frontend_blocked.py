"""上游聚合 403 报错分流：nginx 前门拦截 ≠ 凭据失效。

亲验背景：对方 nginx 在 Emby 之前回 HTML 403（被传输层吞成
body=None），旧代码把任何 401/403 一律报成 409 凭据失效，管理员
重录凭据白忙一场。本文件只测报错映射：HTTP 全部注入替身，
不发真实请求；DB 用内存 SQLite。
本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_sources as ups


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


def _source(db, *, auth_method="none", secret=None, name="src-403"):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method=auth_method,
        username="u1" if auth_method == "password" else None,
        sealed_secret=ups.seal_secret(secret) if secret else None,
        enabled=True, credential_scope="source", health_status="unknown")
    db.add(row)
    db.flush()
    return row


def _fake_get(status, body):
    def get(url, headers, timeout):
        return status, body
    return get


def _assert_frontend_blocked(exc_info):
    assert exc_info.value.status == 502
    assert exc_info.value.code == "upstream_frontend_blocked"
    assert "上游前门（nginx）拦截" in exc_info.value.message
    assert "15.204.95.101" in exc_info.value.message
    assert "重录凭据无效" in exc_info.value.message


def _assert_credential(exc_info):
    assert exc_info.value.status == 409
    assert exc_info.value.code == "upstream_credential_invalid"


# --- 库清单口 fetch_upstream_libraries ---

def test_libraries_nginx_html_403_maps_to_frontend_blocked(db):
    source = _source(db)
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_libraries(source, http_get=_fake_get(403, None))
    _assert_frontend_blocked(exc_info)


def test_libraries_emby_json_403_maps_to_credential(db):
    source = _source(db)
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_libraries(
            source, http_get=_fake_get(403, {"error": "Unauthorized"}))
    _assert_credential(exc_info)


def test_libraries_401_maps_to_credential(db):
    source = _source(db)
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_libraries(source, http_get=_fake_get(401, {}))
    _assert_credential(exc_info)


def test_libraries_429_maps_to_quota(db):
    source = _source(db)
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_libraries(source, http_get=_fake_get(429, {}))
    assert exc_info.value.status == 429
    assert exc_info.value.code == "upstream_quota_exhausted"


def test_libraries_disconnect_maps_to_unavailable(db):
    source = _source(db)
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_libraries(
            source, http_get=_fake_get(None, None))
    assert exc_info.value.status == 502
    assert exc_info.value.code == "upstream_unavailable"


# --- 取页回调 _page_fetcher ---

def test_page_fetcher_nginx_html_403_maps_to_frontend_blocked(db):
    source = _source(db)
    fetch_page = uar._page_fetcher(source, None, _fake_get(403, None))
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        fetch_page(0, 100)
    _assert_frontend_blocked(exc_info)


def test_page_fetcher_emby_json_403_maps_to_credential(db):
    source = _source(db)
    fetch_page = uar._page_fetcher(
        source, None, _fake_get(403, {"error": "Unauthorized"}))
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        fetch_page(0, 100)
    _assert_credential(exc_info)


def test_page_fetcher_401_maps_to_credential(db):
    source = _source(db)
    fetch_page = uar._page_fetcher(source, None, _fake_get(401, {}))
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        fetch_page(0, 100)
    _assert_credential(exc_info)


def test_page_fetcher_429_maps_to_quota(db):
    source = _source(db)
    fetch_page = uar._page_fetcher(source, None, _fake_get(429, {}))
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        fetch_page(0, 100)
    assert exc_info.value.status == 429
    assert exc_info.value.code == "upstream_quota_exhausted"


# --- 密码认证 POST _api_key_for_source ---

def _fake_post(status, body):
    def post(url, payload, headers, timeout):
        assert url.endswith("/Users/AuthenticateByName")
        return status, body
    return post


def test_password_auth_nginx_html_403_maps_to_frontend_blocked(db):
    source = _source(db, auth_method="password", secret="pw-1")
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar._api_key_for_source(source, http_post=_fake_post(403, None))
    _assert_frontend_blocked(exc_info)


def test_password_auth_emby_json_403_maps_to_credential(db):
    source = _source(db, auth_method="password", secret="pw-1")
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar._api_key_for_source(
            source, http_post=_fake_post(403, {"error": "Unauthorized"}))
    _assert_credential(exc_info)


def test_password_auth_401_maps_to_credential(db):
    source = _source(db, auth_method="password", secret="pw-1")
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar._api_key_for_source(source, http_post=_fake_post(401, {}))
    _assert_credential(exc_info)

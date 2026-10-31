"""上游聚合拉取瘦身 + 分页韧性（优化 O-05）。

钉死的行为：
- 聚合分页请求不再拉 MediaSources（映射链零消费，纯浪费）：
  Fields 只留 ProviderIds；
- 默认单页大小 500（原 100）：常量与管理端缺省同口径，且缺省值
  可经 SystemConfig ``upstream_aggregate_default_page_size`` 调
  （钳 10–500，非法回落 500）；
- 单页瞬时失败（断连 / 超时 / 5xx）有限重试（默认 2 次、退避 2 秒，
  次数与退避走 SystemConfig 可调）；401 / 403 / 429 是确定性错误，
  不重试、错误映射逐字不变；重试耗尽仍映射 upstream_unavailable。

HTTP 全部注入替身，不发真实请求；DB 用内存 SQLite。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import admin_upstream_sources as admin_ups
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.integrations import store as config_store


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


@pytest.fixture(autouse=True)
def _clear_config_cache():
    config_store._ttl_cache.clear()
    yield
    config_store._ttl_cache.clear()


@pytest.fixture(autouse=True)
def _category_route_library(db):
    """备注-分类对位（2026-10-31）后 run 先按源备注找对位库：
    本文件的本地行都在 library_id=1，统一备一个同名库「聚合库」，
    源默认备注「测试-聚合库」对位到它，旧断言口径不变。"""
    db.add(em.Library(id=1, guid="lib-guid-1", name="聚合库"))
    db.flush()


def _source(db):
    row = em.UpstreamSource(
        name="src-1", source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=True,
        credential_scope="source", health_status="unknown",
        note="测试-聚合库")
    db.add(row)
    db.flush()
    return row


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.flush()
    config_store._ttl_cache.clear()


def _movie(item_id, tmdb="278"):
    return {"Id": item_id, "Name": "片", "Type": "Movie",
            "ProductionYear": 1994,
            "ProviderIds": ({"Tmdb": tmdb} if tmdb else {})}


class _Items:
    """单库替身：MediaFolders 恒一库；Items 按 StartIndex 切一页。"""

    def __init__(self, items):
        self.items = items
        self.urls = []

    def get(self, url, headers, timeout):
        self.urls.append(url)
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [{"Id": "lib-1", "Name": "库"}]}
        query = parse_qs(urlsplit(url).query)
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        return 200, {"Items": self.items[start:start + limit],
                     "TotalRecordCount": len(self.items)}

    def item_urls(self):
        return [u for u in self.urls if "/Items?" in u]


def test_page_requests_skip_media_sources(db):
    source = _source(db)
    fake = _Items([_movie("a")])
    records = uar.fetch_upstream_records(source, http_get=fake.get,
                                         page_size=100)
    assert [r.upstream_item_id for r in records] == ["a"]
    assert fake.item_urls(), "应至少发一页 Items 请求"
    for url in fake.item_urls():
        assert "MediaSources" not in url
        assert "Fields=ProviderIds" in url


def test_default_page_size_is_500_and_configurable(db):
    assert uar.DEFAULT_PAGE_SIZE == 500
    assert uar.configured_default_page_size(db) == 500
    _set_config(db, "upstream_aggregate_default_page_size", "250")
    assert uar.configured_default_page_size(db) == 250
    _set_config(db, "upstream_aggregate_default_page_size", "9999")
    # 超上限钳到 500，不让脏配置把单页放大到接口上限之外。
    assert uar.configured_default_page_size(db) == 500
    _set_config(db, "upstream_aggregate_default_page_size", "abc")
    assert uar.configured_default_page_size(db) == 500


def test_run_without_page_size_uses_500_limit(db):
    source = _source(db)
    fake = _Items([_movie("a")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["fetched"] == 1
    limits = {parse_qs(urlsplit(u).query)["Limit"][0]
              for u in fake.item_urls()}
    assert limits == {"500"}


def test_admin_enqueue_uses_configured_default_page_size(
        db, monkeypatch):
    source = _source(db)
    db.commit()
    captured = {}

    def fake_enqueue(source_id, page_size):
        captured["page_size"] = page_size
        return {"task_id": "t", "source_id": source_id,
                "status": "queued", "result": None, "error": None}

    monkeypatch.setattr(admin_ups.uaj, "enqueue", fake_enqueue)
    from types import SimpleNamespace
    staff = SimpleNamespace(id=1, is_staff=True)
    admin_ups.aggregate_upstream_source(source.id, None, staff=staff, db=db)
    assert captured["page_size"] == 500
    _set_config(db, "upstream_aggregate_default_page_size", "120")
    admin_ups.aggregate_upstream_source(source.id, None, staff=staff, db=db)
    assert captured["page_size"] == 120
    # 显式传 page_size 时以请求为准，不被配置盖掉。
    admin_ups.aggregate_upstream_source(
        source.id, admin_ups.UpstreamAggregateRequest(page_size=50),
        staff=staff, db=db)
    assert captured["page_size"] == 50


def test_page_retry_recovers_from_transient_failures(db, monkeypatch):
    source = _source(db)
    sleeps = []
    monkeypatch.setattr(uar, "_sleep", lambda sec: sleeps.append(sec))
    calls = {"n": 0}

    def flaky(url, headers, timeout):
        calls["n"] += 1
        if calls["n"] <= 2:
            return None, None  # 断连 / 超时形态
        return 200, {"Items": [_movie("a")], "TotalRecordCount": 1}

    records = uar.fetch_upstream_records(source, http_get=flaky,
                                         page_size=100)
    assert [r.upstream_item_id for r in records] == ["a"]
    assert calls["n"] == 3
    assert sleeps == [2, 2]


def test_page_retry_not_used_for_deterministic_errors(db, monkeypatch):
    source = _source(db)
    monkeypatch.setattr(uar, "_sleep", lambda sec: None)
    calls = {"n": 0}

    def unauthorized(url, headers, timeout):
        calls["n"] += 1
        return 401, {}

    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_records(source, http_get=unauthorized)
    assert exc_info.value.code == "upstream_credential_invalid"
    assert calls["n"] == 1


def test_page_retry_exhaustion_maps_to_unavailable(db, monkeypatch):
    source = _source(db)
    monkeypatch.setattr(uar, "_sleep", lambda sec: None)
    calls = {"n": 0}

    def down(url, headers, timeout):
        calls["n"] += 1
        return 503, None

    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_records(source, http_get=down)
    assert exc_info.value.code == "upstream_unavailable"
    assert calls["n"] == 3  # 首发 + 默认重试 2 次


def test_run_retry_count_configurable_to_zero(db, monkeypatch):
    source = _source(db)
    _set_config(db, "upstream_aggregate_page_retries", "0")
    monkeypatch.setattr(uar, "_sleep", lambda sec: None)
    calls = {"items": 0}

    def flaky(url, headers, timeout):
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [{"Id": "lib-1", "Name": "库"}]}
        calls["items"] += 1
        return None, None

    result = uar.run_manual_aggregation(db, source, http_get=flaky)
    assert calls["items"] == 1  # 配 0 次重试：首发失败即判该库失败
    assert result["failed_libraries"]
    assert result["failed_libraries"][0]["code"] == "upstream_unavailable"

"""25-2 接线：手动聚合触发（拉目录 → 聚合 → 配对 → 写映射）。

只测本模块 + 管理端映射逻辑的纯部分：HTTP 全部注入替身，
不发真实请求；DB 用内存 SQLite；开关用 monkeypatch。
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
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_item_mappings as uim
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


def _source(db, *, auth_method="none", secret=None, enabled=True,
            source_type="emby", name="src-1"):
    row = em.UpstreamSource(
        name=name, source_type=source_type,
        base_url="https://up.example.com/emby", auth_method=auth_method,
        username="u1" if auth_method == "password" else None,
        sealed_secret=ups.seal_secret(secret) if secret else None,
        enabled=enabled, credential_scope="source", health_status="unknown")
    db.add(row)
    db.flush()
    return row


def _local(db, *, item_id, name, item_type, tmdb=None, imdb=None,
           scan_title=None, scan_year=None, year=None):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type=item_type, name=name, tmdb_id=tmdb, imdb_id=imdb,
        scan_title=scan_title, scan_year=scan_year, production_year=year,
        merged_into_id=None)
    db.add(row)
    db.flush()
    return row


def _movie(item_id, name="肖申克的救赎", tmdb="278", year=1994):
    raw = {"Id": item_id, "Name": name, "Type": "Movie",
           "ProductionYear": year,
           "ProviderIds": ({"Tmdb": tmdb} if tmdb else {}),
           "MediaSources": [{"Id": f"ms-{item_id}", "Name": "1080p"}]}
    return raw


class _FakeItems:
    """按 StartIndex 切页的替身上游；record_urls 供断言鉴权形态。"""

    def __init__(self, pages):
        self.pages = pages  # list of (items, total)
        self.urls = []

    def get(self, url, headers, timeout):
        from urllib.parse import parse_qs, urlsplit
        self.urls.append(url)
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            # 按库分批后 run 先取库清单：旧替身只模拟单库。
            return 200, {"Items": [{"Id": "lib-1", "Name": "库"}]}
        query = parse_qs(urlsplit(url).query)
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        flat = [raw for items, _ in self.pages for raw in items]
        total = self.pages[0][1] if self.pages else 0
        return 200, {"Items": flat[start:start + limit],
                     "TotalRecordCount": total}


def test_fetch_pages_and_parses(db):
    source = _source(db)
    fake = _FakeItems([([_movie("a"), _movie("b", tmdb=None)], 2)])
    records = uar.fetch_upstream_records(source, http_get=fake.get,
                                         page_size=100)
    assert [r.upstream_item_id for r in records] == ["a", "b"]
    assert records[0].tmdb_id == "278"
    assert records[1].tmdb_id is None
    assert "Recursive=true" in fake.urls[0]
    assert "api_key" not in fake.urls[0]  # 匿名不带 key


def test_fetch_api_key_source_sends_key(db):
    source = _source(db, auth_method="api_key", secret="key-123")
    fake = _FakeItems([([_movie("a")], 1)])
    uar.fetch_upstream_records(source, http_get=fake.get)
    assert "api_key=key-123" in fake.urls[0]


def test_fetch_password_source_exchanges_token(db):
    source = _source(db, auth_method="password", secret="pw-1")

    def fake_post(url, payload, headers, timeout):
        assert url.endswith("/Users/AuthenticateByName")
        assert payload == {"Username": "u1", "Pw": "pw-1"}
        return 200, {"AccessToken": "tok-9"}

    fake = _FakeItems([([_movie("a")], 1)])
    uar.fetch_upstream_records(source, http_get=fake.get,
                               http_post=fake_post)
    assert "api_key=tok-9" in fake.urls[0]


def test_fetch_unauthorized_maps_to_credential_error(db):
    source = _source(db)

    def bad_get(url, headers, timeout):
        return 401, {}

    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_records(source, http_get=bad_get)
    assert exc_info.value.status == 409
    assert exc_info.value.code == "upstream_credential_invalid"


def test_run_pairs_tmdb_imdb_title_year_and_respects_switch(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=10, name="肖申克的救赎", item_type="movie",
           tmdb="278")
    _local(db, item_id=11, name="星际穿越", item_type="movie",
           imdb="tt0816692")
    _local(db, item_id=12, name="流浪地球", item_type="movie",
           scan_title="流浪地球", scan_year=2019, year=2019)
    pages = _FakeItems([([
        _movie("u-tmdb", tmdb="278"),
        {"Id": "u-imdb", "Name": "星际穿越", "Type": "Movie",
         "ProductionYear": 2014, "ProviderIds": {"Imdb": "tt0816692"},
         "MediaSources": []},
        _movie("u-title", name="流浪地球", tmdb=None, year=2019),
    ], 3)])
    # 开关关：配对返回、但不写库。
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "0")
    result = uar.run_manual_aggregation(db, source, http_get=pages.get)
    assert result["fetched"] == 3
    assert result["aggregates"] == 3
    assert result["paired"] == 3
    assert result["written"] == 0
    assert result["mapping_enabled"] is False
    assert all(p["written"] is False for p in result["pairs"])
    by_key = {p["upstream_item_id"]: p for p in result["pairs"]}
    assert (by_key["u-tmdb"]["local_item_id"],
            by_key["u-tmdb"]["confidence"],
            by_key["u-tmdb"]["evidence"]) == (10, 1.0, "tmdb")
    assert (by_key["u-imdb"]["local_item_id"],
            by_key["u-imdb"]["confidence"],
            by_key["u-imdb"]["evidence"]) == (11, 0.95, "imdb")
    assert (by_key["u-title"]["local_item_id"],
            by_key["u-title"]["confidence"],
            by_key["u-title"]["evidence"]) == (12, 0.7, "title+year")
    assert db.query(em.UpstreamItemMapping).count() == 0
    # 开关开：写库；重跑幂等，不建新行。
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=pages.get)
    assert result["written"] == 3
    db.commit()
    assert db.query(em.UpstreamItemMapping).count() == 3
    uar.run_manual_aggregation(db, source, http_get=pages.get)
    db.commit()
    assert db.query(em.UpstreamItemMapping).count() == 3


def test_run_rejects_disabled_and_applecms(db):
    off = _source(db, name="off", enabled=False)
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.run_manual_aggregation(db, off, http_get=lambda *a: (200, {}))
    assert exc_info.value.status == 400
    cms = _source(db, name="cms", source_type="applecms", auth_method="none")
    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.run_manual_aggregation(db, cms, http_get=lambda *a: (200, {}))
    assert exc_info.value.status == 400


def test_run_identity_aggregate_never_downgrades_to_title(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=20, name="流浪地球", item_type="movie",
           scan_title="流浪地球", scan_year=2019, year=2019)
    pages = _FakeItems([([
        # 有 tmdb 但本地无此 tmdb：标题年份虽能对上，也不降级配对。
        _movie("u-x", name="流浪地球", tmdb="999999", year=2019),
    ], 1)])
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=pages.get)
    assert result["paired"] == 0
    assert result["unpaired_reasons"] == {"identity_no_local_match": 1}
    assert db.query(em.UpstreamItemMapping).count() == 0


def test_run_episode_without_identity_stays_unpaired(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=30, name="三体", item_type="series", tmdb="111")
    pages = _FakeItems([([
        {"Id": "u-ep", "Name": "第 1 集", "Type": "Episode",
         "SeriesName": "三体", "ParentIndexNumber": 1, "IndexNumber": 1,
         "ProviderIds": {}, "MediaSources": []},
    ], 1)])
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=pages.get)
    assert result["paired"] == 0
    assert result["unpaired_reasons"] == {"episode_no_identity": 1}


def test_run_movie_tmdb_never_matches_episode_row(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=40, name="第 1 集", item_type="episode", tmdb="278")
    pages = _FakeItems([([_movie("u-m", tmdb="278")], 1)])
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=pages.get)
    assert result["paired"] == 0
    assert result["unpaired_reasons"] == {"identity_no_local_match": 1}


def test_aggregate_key_probe_matches_aggregation_module():
    # 本地标题键与聚合模块同口径：探针法（照 applecms_sync 口径）。
    probe = agg.UpstreamCatalogRecord(
        source_id="", upstream_item_id="", name="流浪地球",
        item_type="Movie", production_year=2019)
    assert agg.aggregate_key_for(probe) == "title:流浪地球:2019"

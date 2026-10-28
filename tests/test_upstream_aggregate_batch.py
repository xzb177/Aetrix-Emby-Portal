"""上游按库分批聚合：库清单 → 逐库 ParentId → 逐库写映射。

只测本模块：HTTP 全部注入替身，不发真实请求；DB 用内存 SQLite；
开关用 monkeypatch。本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from urllib.parse import parse_qs, urlsplit

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_item_mappings as uim


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


def _source(db, **kwargs):
    row = em.UpstreamSource(
        name=kwargs.get("name", "src-1"), source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        enabled=kwargs.get("enabled", True), credential_scope="source",
        health_status="unknown")
    db.add(row)
    db.flush()
    return row


def _local(db, *, item_id, name, tmdb=None):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type="movie", name=name, tmdb_id=tmdb,
        scan_title=name, scan_year=1994, production_year=1994,
        merged_into_id=None)
    db.add(row)
    db.flush()
    return row


def _movie(item_id, name="肖申克的救赎", tmdb="278"):
    return {"Id": item_id, "Name": name, "Type": "Movie",
            "ProductionYear": 1994,
            "ProviderIds": ({"Tmdb": tmdb} if tmdb else {}),
            "MediaSources": [{"Id": f"ms-{item_id}", "Name": "1080p"}]}


class _FakeUpstream:
    """MediaFolders 返回两库；Items 按 ParentId 分库返回，失败库可注入。"""

    def __init__(self, libraries, by_parent, fail_parent=None):
        self.libraries = libraries
        self.by_parent = by_parent
        self.fail_parent = fail_parent
        self.urls = []

    def get(self, url, headers, timeout):
        self.urls.append(url)
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [
                {"Id": lib_id, "Name": name}
                for lib_id, name in self.libraries]}
        query = parse_qs(urlsplit(url).query)
        parent = query.get("ParentId", [None])[0]
        if parent == self.fail_parent:
            return 502, {}
        items = self.by_parent.get(parent, [])
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        return 200, {"Items": items[start:start + limit],
                     "TotalRecordCount": len(items)}


def test_run_batches_per_parent_and_writes_per_library(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=10, name="肖申克的救赎", tmdb="278")
    _local(db, item_id=11, name="星际穿越", tmdb="999")
    fake = _FakeUpstream(
        [("lib-a", "电影库"), ("lib-b", "剧集库")],
        {"lib-a": [_movie("u-a", tmdb="278")],
         "lib-b": [_movie("u-b", name="星际穿越", tmdb="999")]})
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["libraries_discovered"] == 2
    assert result["failed_libraries"] == []
    assert result["fetched"] == 2
    assert result["written"] == 2
    assert [lib["library_id"] for lib in result["libraries"]] == ["lib-a", "lib-b"]
    assert all(lib["status"] == "ok" for lib in result["libraries"])
    assert {p["upstream_item_id"]: p["library_id"] for p in result["pairs"]} == {
        "u-a": "lib-a", "u-b": "lib-b"}
    assert any("ParentId=lib-a" in url for url in fake.urls)
    assert any("ParentId=lib-b" in url for url in fake.urls)
    db.commit()
    assert db.query(em.UpstreamItemMapping).count() == 2
    # 重跑幂等：成功库不建新行（断点续跑语义）。
    uar.run_manual_aggregation(db, source, http_get=fake.get)
    db.commit()
    assert db.query(em.UpstreamItemMapping).count() == 2


def test_failed_library_does_not_destroy_earlier_mappings(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=10, name="肖申克的救赎", tmdb="278")
    fake = _FakeUpstream(
        [("lib-a", "电影库"), ("lib-b", "坏库")],
        {"lib-a": [_movie("u-a", tmdb="278")], "lib-b": [_movie("u-b")]},
        fail_parent="lib-b")
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["written"] == 1
    assert len(result["failed_libraries"]) == 1
    assert result["failed_libraries"][0]["library_id"] == "lib-b"
    assert result["libraries"][1]["status"] == "failed"
    # 本函数内已 flush，调用方提交前行已可见；提交后仍在。
    db.commit()
    assert db.query(em.UpstreamItemMapping).count() == 1


def test_large_library_processes_in_windows_without_cap(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=10, name="肖申克的救赎", tmdb="278")
    fake = _FakeUpstream(
        [("lib-a", "大库"), ("lib-b", "小库")],
        {"lib-a": [_movie(f"u-a{i}", name=f"不存在的片{i}", tmdb=None)
                   for i in range(5)],
         "lib-b": [_movie("u-b", tmdb="278")]})
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    # 旧单库上限 2 条仍在（遗留整源路径），但逐库分窗路径不再理它：
    # 大库 5 条分 3 窗照常跑完、不失败；小库映射照写；整源不 502。
    monkeypatch.setattr(uar, "MAX_RECORDS_PER_RUN", 2)
    monkeypatch.setattr(uar, "WINDOW_RECORDS", 2)
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["failed_libraries"] == []
    assert result["written"] == 1
    lib_a = [lib for lib in result["libraries"]
             if lib["library_id"] == "lib-a"][0]
    assert lib_a["status"] == "ok"
    assert lib_a["fetched"] == 5
    db.commit()
    assert db.query(em.UpstreamItemMapping).count() == 1


def test_window_split_pairs_do_not_duplicate_or_drop(db, monkeypatch):
    source = _source(db)
    _local(db, item_id=10, name="肖申克的救赎", tmdb="278")
    fake = _FakeUpstream(
        [("lib-a", "大库")],
        {"lib-a": [_movie(f"u-a{i}", tmdb="278") for i in range(3)]})
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")
    # 同一 tmdb 的 3 条分两窗（2 + 1）：每窗各自聚合配对，3 条全落、
    # 不重复、不丢失——窗切分不改变配对结果。
    monkeypatch.setattr(uar, "WINDOW_RECORDS", 2)
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["failed_libraries"] == []
    assert result["fetched"] == 3
    assert result["written"] == 3
    assert sorted(p["upstream_item_id"] for p in result["pairs"]) == [
        "u-a0", "u-a1", "u-a2"]
    db.commit()
    assert db.query(em.UpstreamItemMapping).count() == 3


def test_iter_record_windows_chunks():
    assert list(uar.iter_record_windows(iter([1, 2, 3, 4, 5, 6, 7]), 3)) == [
        [1, 2, 3], [4, 5, 6], [7]]
    assert list(uar.iter_record_windows(iter([1, 2, 3]), 3)) == [[1, 2, 3]]
    assert list(uar.iter_record_windows(iter([]), 3)) == []
    with pytest.raises(ValueError):
        list(uar.iter_record_windows(iter([1]), 0))


def test_empty_library_list_returns_empty_result(db):
    source = _source(db)
    fake = _FakeUpstream([], {})
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["libraries_discovered"] == 0
    assert result["fetched"] == 0
    assert result["written"] == 0
    assert result["failed_libraries"] == []


def test_fetch_libraries_parses_and_maps_errors(db):
    source = _source(db)
    fake = _FakeUpstream([("lib-a", "电影库")], {})
    assert uar.fetch_upstream_libraries(source, http_get=fake.get) == [
        {"id": "lib-a", "name": "电影库"}]

    def bad_get(url, headers, timeout):
        return 401, {}

    with pytest.raises(uar.UpstreamAggregateRunError) as exc_info:
        uar.fetch_upstream_libraries(source, http_get=bad_get)
    assert exc_info.value.code == "upstream_credential_invalid"

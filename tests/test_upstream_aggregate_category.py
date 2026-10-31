"""上游聚合按「源备注-分类」对位（2026-10-31 用户亲定口径）。

钉死的行为：
- 源备注形如「备注-分类」（如「乐观-国产剧」），聚合配对只在
  分类对位的本地库内进行，绝不跨库；
- 分类→本地库对应可经 SystemConfig
  ``upstream_aggregate_category_library_map``（JSON 对象）改配，
  没配到的分类默认同名对位；分类名单/库名不写死；
- 备注为空（解析不出分类）→ 整源跳过并在结果里报
  ``category_remark_missing``，且根本不向上游发请求；
- 分类对不上任何本地库 → 整源跳过并报
  ``category_library_unmatched``，同样不发请求；
- 跳过不是失败：结果带 skipped/skip_reason，计数全 0。

HTTP 全部注入替身；DB 用内存 SQLite；配置走 integrations.store。
本地 Python 3.12 口径，定向跑；全量以 GitHub CI（3.11）为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_schema
from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as ups
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


def _set_config(db, key, value):
    config_store.write_values(db, {key: value})
    db.commit()
    config_store._ttl_cache.clear()


def _library(db, *, library_id, name, is_virtual=False):
    row = em.Library(id=library_id, guid=f"lib-guid-{library_id}",
                     name=name, is_virtual=is_virtual)
    db.add(row)
    db.flush()
    return row


def _source(db, *, note=None, name="src-1", enabled=True):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        sealed_secret=None, enabled=enabled, credential_scope="source",
        health_status="unknown", note=note)
    db.add(row)
    db.flush()
    return row


def _local(db, *, item_id, library_id, name, item_type="movie",
           tmdb=None, imdb=None, year=None):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=library_id,
        item_type=item_type, name=name, tmdb_id=tmdb, imdb_id=imdb,
        production_year=year, merged_into_id=None)
    db.add(row)
    db.flush()
    return row


def _movie_raw(item_id, tmdb, name="片", year=2001):
    return {"Id": item_id, "Name": name, "Type": "Movie",
            "ProductionYear": year,
            "ProviderIds": ({"Tmdb": tmdb} if tmdb else {}),
            "MediaSources": [{"Id": f"ms-{item_id}", "Name": "1080p"}]}


class _FakeUpstream:
    """单上游库替身：库清单固定一库，Items 按页切。urls 供断言
    「跳过时根本没发请求」。"""

    def __init__(self, items):
        self.items = items
        self.urls = []

    def get(self, url, headers, timeout):
        from urllib.parse import parse_qs, urlsplit
        self.urls.append(url)
        if url.split("?")[0].endswith("/Library/MediaFolders"):
            return 200, {"Items": [{"Id": "up-lib-1", "Name": "上游库"}]}
        query = parse_qs(urlsplit(url).query)
        start = int(query["StartIndex"][0])
        limit = int(query["Limit"][0])
        return 200, {"Items": self.items[start:start + limit],
                     "TotalRecordCount": len(self.items)}


# ---------------------------------------------------------------------------
# 备注解析
# ---------------------------------------------------------------------------

def test_parse_remark_category():
    assert uar.parse_remark_category("乐观-国产剧") == "国产剧"
    assert uar.parse_remark_category("国产剧") == "国产剧"
    assert uar.parse_remark_category("  乐观 - 国产剧 ") == "国产剧"
    assert uar.parse_remark_category("甲-乙-丙") == "丙"
    assert uar.parse_remark_category("") is None
    assert uar.parse_remark_category("   ") is None
    assert uar.parse_remark_category(None) is None
    assert uar.parse_remark_category("乐观-") is None


def test_resolve_target_library_variants(db):
    _library(db, library_id=10, name="国产剧")
    hit = uar.resolve_target_library(db, _source(db, note="乐观-国产剧"))
    assert hit["skip"] is None
    assert hit["category"] == "国产剧"
    assert hit["library"].id == 10

    plain = uar.resolve_target_library(
        db, _source(db, note="国产剧", name="src-2"))
    assert plain["skip"] is None
    assert plain["library"].id == 10

    missing = uar.resolve_target_library(
        db, _source(db, note=None, name="src-3"))
    assert missing["library"] is None
    assert missing["skip"]["code"] == "category_remark_missing"

    unmatched = uar.resolve_target_library(
        db, _source(db, note="乐观-纪录片", name="src-4"))
    assert unmatched["library"] is None
    assert unmatched["skip"]["code"] == "category_library_unmatched"
    assert unmatched["skip"]["category"] == "纪录片"


def test_resolve_ignores_virtual_library(db):
    # 虚拟库（按平台生成）不是对位目标：同名也只认真实库。
    _library(db, library_id=20, name="国产剧", is_virtual=True)
    res = uar.resolve_target_library(db, _source(db, note="乐观-国产剧"))
    assert res["library"] is None
    assert res["skip"]["code"] == "category_library_unmatched"


# ---------------------------------------------------------------------------
# 聚合主路径：对位命中 / 跳过 / 不跨库
# ---------------------------------------------------------------------------

def test_aggregate_pairs_within_remark_library(db, monkeypatch):
    monkeypatch.setattr(uim, "mapping_enabled", lambda: True)
    _library(db, library_id=10, name="国产剧")
    _library(db, library_id=11, name="英美剧")
    _local(db, item_id=101, library_id=10, name="片甲", tmdb="278")
    source = _source(db, note="乐观-国产剧")
    fake = _FakeUpstream([_movie_raw("u1", "278")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["skipped"] is False
    assert result["category"] == "国产剧"
    assert result["target_library_id"] == 10
    assert result["target_library_name"] == "国产剧"
    assert [p["local_item_id"] for p in result["pairs"]] == [101]
    assert result["written"] == 1
    rows = db.query(em.UpstreamItemMapping).all()
    assert [(r.local_item_id, r.upstream_item_id) for r in rows] == [(101, "u1")]


def test_aggregate_skips_when_remark_empty(db):
    _library(db, library_id=10, name="国产剧")
    _local(db, item_id=101, library_id=10, name="片甲", tmdb="278")
    source = _source(db, note=None)
    fake = _FakeUpstream([_movie_raw("u1", "278")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["skipped"] is True
    assert result["skip_reason"]["code"] == "category_remark_missing"
    assert result["fetched"] == 0
    assert result["paired"] == 0
    assert result["written"] == 0
    assert result["pairs"] == []
    # 跳过发生在拉取之前：一个请求都不许发（不浪费上游额度）。
    assert fake.urls == []


def test_aggregate_skips_when_category_unmatched(db):
    _library(db, library_id=10, name="国产剧")
    _local(db, item_id=101, library_id=10, name="片甲", tmdb="278")
    source = _source(db, note="乐观-纪录片")
    fake = _FakeUpstream([_movie_raw("u1", "278")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["skipped"] is True
    assert result["skip_reason"]["code"] == "category_library_unmatched"
    assert result["skip_reason"]["category"] == "纪录片"
    assert result["fetched"] == 0
    assert fake.urls == []


def test_pairing_never_crosses_library(db):
    _library(db, library_id=10, name="国产剧")
    _library(db, library_id=11, name="英美剧")
    # 同一 tmdb 身份在两个库各有一行；另一身份只在错的库里有。
    _local(db, item_id=101, library_id=10, name="片甲", tmdb="278")
    _local(db, item_id=102, library_id=11, name="片甲", tmdb="278")
    _local(db, item_id=103, library_id=11, name="片乙", tmdb="999")
    source = _source(db, note="乐观-国产剧")
    fake = _FakeUpstream([_movie_raw("u1", "278"), _movie_raw("u2", "999")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    paired_locals = {p["local_item_id"] for p in result["pairs"]}
    assert paired_locals == {101}
    # 只在错库里有的身份：宁可不配，也不许跨库配。
    assert result["unpaired_reasons"].get("identity_no_local_match", 0) >= 1


# ---------------------------------------------------------------------------
# 分类→库映射配置（SystemConfig）
# ---------------------------------------------------------------------------

def test_category_library_map_override(db):
    _set_config(db, uar.KEY_CATEGORY_LIBRARY_MAP,
                json.dumps({"国产剧": "华语剧场"}, ensure_ascii=False))
    _library(db, library_id=12, name="华语剧场")
    _local(db, item_id=101, library_id=12, name="片甲", tmdb="278")
    source = _source(db, note="乐观-国产剧")
    fake = _FakeUpstream([_movie_raw("u1", "278")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert result["skipped"] is False
    assert result["target_library_id"] == 12
    assert [p["local_item_id"] for p in result["pairs"]] == [101]


def test_category_library_map_malformed_falls_back(db):
    # 存量坏值（人工直改库等）不许把聚合打停：解析不出来当没配，
    # 回落同名对位。
    _set_config(db, uar.KEY_CATEGORY_LIBRARY_MAP, "not-a-json")
    _library(db, library_id=10, name="国产剧")
    res = uar.resolve_target_library(db, _source(db, note="乐观-国产剧"))
    assert res["skip"] is None
    assert res["library"].id == 10


def test_category_map_admin_endpoint_roundtrip(db):
    """管理端读写口：走 config_service 统一保存通道，非法形态 400、
    空串删行回「未配置」；读口回 canonical 文本 + 解析后的映射。"""
    from fastapi import HTTPException

    from backend.api import admin_upstream_sources as aus

    got = aus.get_upstream_category_library_map(staff=None, db=db)
    assert got == {"value": "", "map": {}}

    out = aus.put_upstream_category_library_map(
        aus.UpstreamCategoryLibraryMapIn(value='{"国产剧": "华语剧场"}'),
        staff=None, db=db)
    assert out["map"] == {"国产剧": "华语剧场"}
    got = aus.get_upstream_category_library_map(staff=None, db=db)
    assert got["map"] == {"国产剧": "华语剧场"}
    assert json.loads(got["value"]) == {"国产剧": "华语剧场"}

    with pytest.raises(HTTPException) as exc_info:
        aus.put_upstream_category_library_map(
            aus.UpstreamCategoryLibraryMapIn(value='{"国产剧": 3}'),
            staff=None, db=db)
    assert exc_info.value.status_code == 400
    # 失败不落库：仍是上一份好值。
    got = aus.get_upstream_category_library_map(staff=None, db=db)
    assert got["map"] == {"国产剧": "华语剧场"}

    out = aus.put_upstream_category_library_map(
        aus.UpstreamCategoryLibraryMapIn(value=""), staff=None, db=db)
    assert out["map"] == {}


def test_config_schema_validates_category_map():
    spec = config_schema.UPSTREAM_AGGREGATE_CONFIG_SCHEMA[
        uar.KEY_CATEGORY_LIBRARY_MAP]
    ok = config_schema.normalize(
        uar.KEY_CATEGORY_LIBRARY_MAP, spec, '{"国产剧": "华语剧场"}')
    assert json.loads(ok) == {"国产剧": "华语剧场"}
    ok2 = config_schema.normalize(
        uar.KEY_CATEGORY_LIBRARY_MAP, spec, {"国产剧": "华语剧场"})
    assert json.loads(ok2) == {"国产剧": "华语剧场"}
    for bad in ("not json", '["国产剧"]', '{"国产剧": 3}',
                '{"": "华语剧场"}', '{"国产剧": ""}', '{"国产剧": "  "}'):
        with pytest.raises(config_schema.ConfigValueError):
            config_schema.normalize(uar.KEY_CATEGORY_LIBRARY_MAP, spec, bad)

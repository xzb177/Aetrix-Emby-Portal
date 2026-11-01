"""上游聚合与源备注的关系（2026-11-01 用户亲令删除分类选项）。

历史：#1639/#1643 曾给聚合加「备注-分类」机制——源备注末段写
分类名就把配对收窄到对位库、写「全部」或不写才全量、分类对不
上任何本地库时整源跳过（``category_library_unmatched``）。用户
2026-11-01 08:41 亲令推翻：分类选项多余、管理端详情页不再出现、
其他全部删除。本文件钉死删除后的口径：

- ``note`` 是纯备注：聚合不解析它、不因它收窄、不因它跳过；
- 任意 note（空 /「全部」/ 恰是某库名 / 对不上任何库的乱写）
  都整源全量真跑，配对按条目自身逐库对位；
- 聚合结果不再带分类字段（category / all_libraries /
  target_library_* / skipped / skip_reason 一律不存在）；
- 分类机制零件已删：``parse_remark_category`` /
  ``resolve_target_library`` / ``category_library_map`` /
  改配表 SystemConfig 键与管理端读写口都不许再回来
  （hasattr / 注册表断言防回归）。

HTTP 全部注入替身；DB 用内存 SQLite；配置走 integrations.store。
本地 Python 3.12 口径，定向跑；全量以 GitHub CI（3.11）为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_schema
from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_item_mappings as uim
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
    「任何 note 下都真的向上游发了请求」（旧跳过口径下为空）。"""

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


def _two_library_setup(db):
    _library(db, library_id=10, name="国产剧")
    _library(db, library_id=11, name="英美剧")
    _local(db, item_id=101, library_id=10, name="片甲", tmdb="278")
    _local(db, item_id=102, library_id=11, name="片乙", tmdb="999")


# ---------------------------------------------------------------------------
# 任意 note 都全量：不收窄、不跳过、真发请求
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("note", [
    None, "", "全部", "乐观-全部", "乐观-国产剧", "国产剧",
    "乐观-纪录片", "乱写一气", "乐观-",
])
def test_aggregate_runs_full_for_any_note(db, monkeypatch, note):
    monkeypatch.setattr(uim, "mapping_enabled", lambda: True)
    _two_library_setup(db)
    source = _source(db, note=note)
    fake = _FakeUpstream([_movie_raw("u1", "278"), _movie_raw("u2", "999")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    # 两个库的条目都配上：note 写什么都不许收窄到单一库。
    assert {p["local_item_id"] for p in result["pairs"]} == {101, 102}
    assert result["written"] == 2
    assert result["paired"] == 2
    # 是真跑不是跳过：上游请求照发（旧 unmatched 跳过时 urls 为空）。
    assert fake.urls != []
    rows = db.query(em.UpstreamItemMapping).all()
    assert {(r.local_item_id, r.upstream_item_id) for r in rows} == {
        (101, "u1"), (102, "u2")}


def test_aggregate_note_matching_library_does_not_narrow(db, monkeypatch):
    # note 末段恰是真实库名（旧口径会收窄到该库）：新口径下另一个
    # 库里的身份照样配得上——备注对聚合零影响。
    monkeypatch.setattr(uim, "mapping_enabled", lambda: True)
    _two_library_setup(db)
    source = _source(db, note="乐观-国产剧")
    fake = _FakeUpstream([_movie_raw("u1", "278"), _movie_raw("u2", "999")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    assert {p["local_item_id"] for p in result["pairs"]} == {101, 102}
    assert result["unpaired_reasons"].get("identity_no_local_match", 0) == 0


# ---------------------------------------------------------------------------
# 结果形状：分类字段不许再出现
# ---------------------------------------------------------------------------

def test_aggregate_result_has_no_category_fields(db, monkeypatch):
    monkeypatch.setattr(uim, "mapping_enabled", lambda: True)
    _two_library_setup(db)
    source = _source(db, note="乐观-国产剧")
    fake = _FakeUpstream([_movie_raw("u1", "278")])
    result = uar.run_manual_aggregation(db, source, http_get=fake.get)
    for key in ("category", "all_libraries", "target_library_id",
                "target_library_name", "skipped", "skip_reason"):
        assert key not in result, key


# ---------------------------------------------------------------------------
# 分类机制零件已删（防回归：谁把解析/改配/跳过加回来这里先红）
# ---------------------------------------------------------------------------

def test_category_machinery_removed():
    assert not hasattr(uar, "parse_remark_category")
    assert not hasattr(uar, "resolve_target_library")
    assert not hasattr(uar, "category_library_map")
    assert not hasattr(uar, "KEY_CATEGORY_LIBRARY_MAP")
    assert "upstream_aggregate_category_library_map" not in (
        config_schema.UPSTREAM_AGGREGATE_CONFIG_SCHEMA)

    from backend.api import admin_upstream_sources as aus

    assert not hasattr(aus, "get_upstream_category_library_map")
    assert not hasattr(aus, "put_upstream_category_library_map")

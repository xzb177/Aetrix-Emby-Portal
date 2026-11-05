# -*- coding: utf-8 -*-
"""N4-3 尾巴：主 /Items 浏览的 SQL 展示去重（_dedup_dropped_ids）纳入门控。

#1860 落地的每库开关只门控了三处展示分组（Latest/Resume 的
deduplicate_items、AlternateVersions、_version_siblings）；主浏览走的是
独立的第二套 SQL 去重（api._dedup_dropped_ids，PG ROW_NUMBER 下推），
本文件钉死它与回退全量实现（_dedup_primary_ids）的门控口径：

- merge_same=False 的库：未物理合并的同库同 TMDB 行不进 dropped，
  主浏览逐条出现，TotalRecordCount 同步变多（列表与总数都要验）；
- 已物理合并行（merged_into_id 非空）命运逐字不变：关库也照样不露面
  （物理层既有呈现，归 N4-4，本单不碰）；
- 分区/分组键本就含 library_id、从不跨库：混合查询里开着开关的库
  不受关库影响；enable_merge 在主浏览无作用；
- 没有任何库关开关（缺省/NULL）时行为与改前逐字一致；
- 开关翻转经 portal update_library 时 bump 本库去重数据版本号，
  让以版本号为键的去重/清单两层缓存即刻失效（翻转立即生效）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api
from backend.emby_server import items_dedup_cache as idc
from backend.emby_server import models as em
from backend.emby_server import portal

_T0 = datetime(2026, 1, 1)


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/n43browse.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib_on = em.Library(guid=uuid.uuid4().hex, name="开库",
                        collection_type="movies", is_enabled=True)
    lib_off = em.Library(guid=uuid.uuid4().hex, name="关库",
                         collection_type="movies", is_enabled=True,
                         merge_same=False)
    lib_unset = em.Library(guid=uuid.uuid4().hex, name="缺省库",
                           collection_type="movies", is_enabled=True)
    lib_flip = em.Library(guid=uuid.uuid4().hex, name="翻转库",
                          collection_type="tvshows", is_enabled=True)
    db.add_all([lib_on, lib_off, lib_unset, lib_flip])
    db.flush()
    # 缺省库模拟老行：merge_same 为 NULL（解析按开处理）
    lib_unset.merge_same = None
    db.flush()

    user = models.WebUser(username="n43browse", password_hash="x", is_active=True)
    db.add(user)
    db.flush()
    seq = [0]

    def mk(lib, item_type, name, **kw):
        seq[0] += 1
        kw.setdefault("sort_name", name.lower())
        kw.setdefault("date_added", _T0 + timedelta(hours=seq[0]))
        it = em.MediaItem(guid=uuid.uuid4().hex, library_id=lib.id,
                          item_type=item_type, name=name,
                          file_path=f"/m/{seq[0]}.mkv", **kw)
        db.add(it)
        db.flush()
        return it

    m = {"lib_on": lib_on, "lib_off": lib_off,
         "lib_unset": lib_unset, "lib_flip": lib_flip}
    # 开库：同 TMDB 两行（有海报者为选主），应并成 1 条
    m["ON1"] = mk(lib_on, "movie", "OnDup", tmdb_id="800")
    m["ON2"] = mk(lib_on, "movie", "OnDup HD", tmdb_id="800", poster_path="/p.jpg")
    # 关库：同 TMDB 两行、未物理合并，应逐条出 2 条
    m["OFF1"] = mk(lib_off, "movie", "OffDup", tmdb_id="700")
    m["OFF2"] = mk(lib_off, "movie", "OffDup HD", tmdb_id="700", poster_path="/p.jpg")
    # 关库里的物理合并组：副本带海报（旧口径下副本反而是选主、主行被
    # dropped）；门控后主行（未合并）恢复出现、副本（已合并）照样不露面
    m["OFFP"] = mk(lib_off, "movie", "OffMerged", tmdb_id="701")
    m["OFFC"] = mk(lib_off, "movie", "OffMerged Copy", tmdb_id="701",
                   poster_path="/p.jpg", merged_into_id=m["OFFP"].id)
    # 缺省库（NULL）：同 TMDB 两行，应与改前一致并成 1 条
    m["UN1"] = mk(lib_unset, "movie", "UnsetDup", tmdb_id="600")
    m["UN2"] = mk(lib_unset, "movie", "UnsetDup HD", tmdb_id="600",
                  poster_path="/p.jpg")
    # 翻转库：一对电影 + 一对同集文件（同剧同季同集）
    m["FL1"] = mk(lib_flip, "movie", "FlipDup", tmdb_id="950")
    m["FL2"] = mk(lib_flip, "movie", "FlipDup HD", tmdb_id="950",
                  poster_path="/p.jpg")
    m["FS"] = mk(lib_flip, "series", "Flip Show", tmdb_id="951")
    m["FE1"] = mk(lib_flip, "episode", "Flip E1 a", series_id=m["FS"].id,
                  season_number=1, episode_number=1)
    m["FE2"] = mk(lib_flip, "episode", "Flip E1 b", series_id=m["FS"].id,
                  season_number=1, episode_number=1)

    db.commit()
    data = SimpleNamespace(
        db=db, m=m, user=SimpleNamespace(id=user.id, is_staff=False))
    yield data
    db.close()
    engine.dispose()


def _req(params):
    return Request({"type": "http", "method": "GET",
                    "query_string": urlencode(params).encode(), "headers": []})


_REAL_DROPPED = api._dedup_dropped_ids


def _run(scene, params, legacy, monkeypatch):
    """同一请求分别走 SQL 下推（fast）与回退全量（legacy）各跑一遍。"""
    def spy(query, db):
        return None if legacy else _REAL_DROPPED(query, db)

    monkeypatch.setattr(api, "_dedup_dropped_ids", spy)
    return api._query_items(_req(params), scene.user, scene.db, "http://t")


def _names(res):
    return [i["Name"] for i in res["Items"]]


# ---------------------------------------------------------------------------
# ① 关库：同库同 TMDB 未合并两行 → 2 条且总数 = 2（开时 1 条）
# ---------------------------------------------------------------------------

def test_off_library_shows_both_rows_and_total(scene, monkeypatch):
    params = {"ParentId": scene.m["lib_off"].guid, "Recursive": "true",
              "IncludeItemTypes": "Movie", "Limit": 100}
    for legacy in (False, True):
        res = _run(scene, params, legacy, monkeypatch)
        assert sorted(_names(res)) == ["OffDup", "OffDup HD", "OffMerged"]
        assert res["TotalRecordCount"] == 3
    # 翻成开：同库并组恢复，OffDup 组只剩选主、OffMerged 组只剩副本选主
    lib_off = scene.m["lib_off"]
    lib_off.merge_same = True
    scene.db.flush()
    for legacy in (False, True):
        res = _run(scene, params, legacy, monkeypatch)
        assert res["TotalRecordCount"] == 2
        assert "OffDup HD" in _names(res) and "OffDup" not in _names(res)


# ---------------------------------------------------------------------------
# ② 混合查询：关库逐条、开库照旧并组，互不影响
# ---------------------------------------------------------------------------

def test_mixed_query_other_libraries_unaffected(scene, monkeypatch):
    params = {"Recursive": "true", "IncludeItemTypes": "Movie", "Limit": 100}
    for legacy in (False, True):
        res = _run(scene, params, legacy, monkeypatch)
        names = _names(res)
        # 开库并 1、关库出 2（+物理合并主行 1）、缺省库并 1、翻转库（开）并 1
        assert res["TotalRecordCount"] == 6
        assert "OnDup HD" in names and "OnDup" not in names
        assert "OffDup" in names and "OffDup HD" in names
        assert "UnsetDup HD" in names and "UnsetDup" not in names
        assert "FlipDup HD" in names and "FlipDup" not in names


# ---------------------------------------------------------------------------
# ③ 开关翻转：电影与单集都跟着 1 ↔ 2
# ---------------------------------------------------------------------------

def test_flip_switch_movies_and_episodes(scene, monkeypatch):
    db, m = scene.db, scene.m
    movie_params = {"ParentId": m["lib_flip"].guid, "Recursive": "true",
                    "IncludeItemTypes": "Movie", "Limit": 100}
    ep_params = {"Recursive": "true", "IncludeItemTypes": "Episode",
                 "Limit": 100}

    def flip_ep_count(res):
        return sum(1 for n in _names(res) if n.startswith("Flip E1"))

    for legacy in (False, True):
        res = _run(scene, movie_params, legacy, monkeypatch)
        assert res["TotalRecordCount"] == 1 and _names(res) == ["FlipDup HD"]
        assert flip_ep_count(_run(scene, ep_params, legacy, monkeypatch)) == 1
    # 关 → 电影 2 条/总数 2、单集 2 条
    m["lib_flip"].merge_same = False
    db.flush()
    for legacy in (False, True):
        res = _run(scene, movie_params, legacy, monkeypatch)
        assert res["TotalRecordCount"] == 2
        assert sorted(_names(res)) == ["FlipDup", "FlipDup HD"]
        assert flip_ep_count(_run(scene, ep_params, legacy, monkeypatch)) == 2
    # 再开回去 → 恢复并组
    m["lib_flip"].merge_same = True
    db.flush()
    for legacy in (False, True):
        res = _run(scene, movie_params, legacy, monkeypatch)
        assert res["TotalRecordCount"] == 1
        assert flip_ep_count(_run(scene, ep_params, legacy, monkeypatch)) == 1


# ---------------------------------------------------------------------------
# ④ 物理合并行：关库也照样不露面（N4-4 范围，本单不改其命运）
# ---------------------------------------------------------------------------

def test_merged_copy_stays_hidden_when_library_off(scene, monkeypatch):
    params = {"ParentId": scene.m["lib_off"].guid, "Recursive": "true",
              "IncludeItemTypes": "Movie", "Limit": 100}
    for legacy in (False, True):
        res = _run(scene, params, legacy, monkeypatch)
        assert "OffMerged" in _names(res)          # 未合并主行恢复出现
        assert "OffMerged Copy" not in _names(res)  # 已合并副本照样不露面


# ---------------------------------------------------------------------------
# ⑤ 缺省（NULL / 不设开关）行为与改前一致
# ---------------------------------------------------------------------------

def test_default_library_behaves_as_before(scene, monkeypatch):
    params = {"ParentId": scene.m["lib_unset"].guid, "Recursive": "true",
              "IncludeItemTypes": "Movie", "Limit": 100}
    for legacy in (False, True):
        res = _run(scene, params, legacy, monkeypatch)
        assert res["TotalRecordCount"] == 1
        assert _names(res) == ["UnsetDup HD"]


# ---------------------------------------------------------------------------
# ⑥ portal 翻开关 → bump 本库去重数据版本号（缓存即刻失效）
# ---------------------------------------------------------------------------

def test_update_library_switch_flip_bumps_dedup_version(scene):
    db, m = scene.db, scene.m
    lib = m["lib_on"]
    staff = SimpleNamespace(id=1, is_staff=True)
    assert idc.read_data_versions(db, [lib.id])[lib.id] == "0"
    # 翻 merge_same → bump
    portal.update_library(lib.id, portal.LibraryUpdate(merge_same=False),
                          staff, db)
    v1 = idc.read_data_versions(db, [lib.id])[lib.id]
    assert v1 != "0"
    assert lib.merge_same is False
    # 同值再存（没翻）→ 不 bump，版本号不动
    portal.update_library(lib.id, portal.LibraryUpdate(merge_same=False),
                          staff, db)
    assert idc.read_data_versions(db, [lib.id])[lib.id] == v1
    # 只翻 enable_merge → 同样 bump
    portal.update_library(lib.id, portal.LibraryUpdate(enable_merge=True),
                          staff, db)
    assert idc.read_data_versions(db, [lib.id])[lib.id] != v1

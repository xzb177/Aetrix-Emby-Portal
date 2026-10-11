# -*- coding: utf-8 -*-
"""单集多版本在各列表/计数/搜索出口的过滤口径回归测试。

新功能的不变式：被合并的版本（merged_into_id 非空）不独立出现在任何
"单集可见"出口里，展示的恒为 merge 主记录（默认最高画质）。

覆盖：
- F3: 通用 /Items（ParentId=series + IncludeItemTypes=Episode，第三方客户端
  剧集详情主路径）只返回主记录；且与 dedup 选主对齐（dedup 按海报/tmdb/id
  选主，merge 按分辨率选主，不一致时必须以 merge 主记录为准）
- F4: _prefetch_list_data 的 series 未播数不计入被合并版本
- F5: _child_count 的 season 分支不计入被合并版本
- F6: /Search/Hints 不返回被合并的单集版本
"""
import os
import tempfile
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as core  # noqa: F401
from backend.database import Base
from backend.emby_server import api, models as em
from backend.emby_server import search_api


class _QP(dict):
    """仿 Starlette QueryParams：get + getlist。"""

    def getlist(self, key):
        v = self.get(key)
        return [v] if v else []


@pytest.fixture()
def db():
    path = os.path.join(tempfile.mkdtemp(), "evapi.db")
    engine = create_engine(f"sqlite:///{path}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


@pytest.fixture()
def scene(db):
    """一剧一种子结构 + S01E01 两个版本（v1=720p 有海报先入库，v2=2160p 后入库）。

    merge 选主 → v2（分辨率优先）；dedup 选主 → v1（有海报）。
    两处选主故意不一致，测的是"列表展示必须跟 merge 主记录对齐"。
    """
    lib = em.Library(guid="L" * 32, name="剧库", collection_type="tvshows", paths="")
    db.add(lib)
    db.flush()
    series = em.MediaItem(
        guid="S" * 32, library_id=lib.id, item_type="series", name="测试剧",
        sort_name="测试剧", tmdb_id="tmdb-1", is_hidden=False,
        date_added=datetime.now())
    db.add(series)
    db.flush()
    season = em.MediaItem(
        guid="SE" * 32, library_id=lib.id, item_type="season", name="第 1 季",
        sort_name="第 1 季", parent_id=series.id, series_id=series.id,
        season_number=1, is_hidden=False, date_added=datetime.now())
    db.add(season)
    db.flush()
    v1 = em.MediaItem(
        guid="V1" * 16, library_id=lib.id, item_type="episode", name="第 1 集",
        sort_name="第 1 集", parent_id=season.id, series_id=series.id,
        season_number=1, episode_number=1, height=720, size=100,
        poster_path="/p/720.jpg", file_path="/x/S01E01 - 720p.strm",
        is_hidden=False, date_added=datetime.now())
    v2 = em.MediaItem(
        guid="V2" * 16, library_id=lib.id, item_type="episode", name="第 1 集",
        sort_name="第 1 集", parent_id=season.id, series_id=series.id,
        season_number=1, episode_number=1, height=2160, size=200,
        file_path="/x/S01E01 - 2160p.strm",
        is_hidden=False, date_added=datetime.now())
    db.add_all([v1, v2])
    db.flush()
    # 模拟合并完成态：v1 指向 v2（主记录）
    v1.merged_into_id = v2.id
    user = core.WebUser(username="u_evapi", password_hash="x")
    db.add(user)
    db.flush()
    db.commit()
    return SimpleNamespace(db=db, user=user, series=series, season=season,
                           v1=v1, v2=v2)


def _call_items(db, user, **params):
    req = SimpleNamespace(query_params=_QP(params))
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(api, "_library_scope", lambda db, user: None)
        mp.setattr(api, "_prefetch_list_data", lambda *a, **k: None)
        mp.setattr(api, "_item_dto",
                   lambda e, base, uid, db: {"Id": e.guid, "Name": e.name,
                                             "Type": e.item_type})
        return api._query_items(req, user, db, "http://test")


def test_query_items_episodes_show_merge_primary_only(scene):
    """F3：通用 /Items 单集列表只展示 merge 主记录（v2=2160p），
    即使 dedup 按自身规则会选中 v1。"""
    res = _call_items(scene.db, scene.user,
                      ParentId="S" * 32, IncludeItemTypes="Episode",
                      Recursive="true")
    guids = [it["Id"] for it in res["Items"]]
    assert guids == ["V2" * 16], f"应只展示主记录 v2，实际 {guids}"


def test_query_items_explicit_ids_still_resolves_merged(scene):
    """F3 边界：按 guid 直接取条目时不过滤（版本切换/播放走直接取条目）。
    注：不带 Recursive 时 _query_items 默认只返回 movie/series（既有行为），
    这里显式传 Recursive 测的是 merged 过滤本身。"""
    res = _call_items(scene.db, scene.user, Ids="V1" * 16, Recursive="true")
    guids = [it["Id"] for it in res["Items"]]
    assert guids == ["V1" * 16], f"直接取条目不应被过滤，实际 {guids}"


def test_prefetch_unplayed_excludes_merged(scene):
    """F4：series 未播数不把被合并的版本算进去。"""
    db, user, series, v2 = scene.db, scene.user, scene.series, scene.v2
    # 两版本都未看：旧口径会数出 2，新口径只数主记录 = 1
    api._prefetch_list_data(db, user.id, [series])
    prefetch = db.info.get("_aetrix_prefetch") or {}
    assert prefetch["unplayed"].get(series.id) == 1, (
        f"未播数应为 1（只计主记录），实际 {prefetch['unplayed']}")
    # 主记录看过之后，未播数归零（被合并版本不影响）
    db.add(em.UserMediaData(user_id=user.id, item_id=v2.id, played=True))
    db.commit()
    api._prefetch_list_data(db, user.id, [series])
    prefetch = db.info.get("_aetrix_prefetch") or {}
    assert prefetch["unplayed"].get(series.id, 0) == 0


def test_child_count_season_excludes_merged(scene):
    """F5：season 的 ChildCount 不计入被合并版本。"""
    assert api._child_count(scene.season, scene.db) == 1, (
        "季下应只计主记录 1 集")
    assert api._child_count(scene.series, scene.db) == 1, (
        "剧下应只计主记录 1 集")


def test_search_hints_excludes_merged_episodes(scene):
    """F6：搜索建议不返回被合并的单集版本。"""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(search_api.library_scope, "effective_ids_safe",
                   lambda db, user: None)
        req = SimpleNamespace(query_params=_QP({"SearchTerm": "第 1 集",
                                                "IncludeItemTypes": "Episode",
                                                "Limit": "20"}),
                              base_url="http://test/")
        res = search_api.search_hints(req, scene.user, scene.db)
    guids = [h["Id"] for h in res["SearchHints"]]
    assert guids == ["V2" * 16], f"搜索建议应只含主记录 v2，实际 {guids}"

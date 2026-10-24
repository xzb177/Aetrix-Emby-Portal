"""身份准入货架·一期：_query_items 拆面行为钉。

案 A 口径：门开时浏览（货架面）只露已绑定条目；SearchTerm 是搜索面
不挂门（搜索宽）；Ids 直查是直达面不挂门（凭 guid 直达放行，老收藏
与在播不断）；集跟随所属剧的绑定。门关时与基线逐字一致。

另钉「上架事件」：未绑定条目经既有写路径落库 tmdb_id 并 bump 该库
数据版本号（admin_scrape bind-tmdb 的真实动作）后，同一浏览请求立刻
能看见它——列表清单缓存键含各库数据版本，bump 即失效。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api, identity_gate, items_dedup_cache
from backend.emby_server import models as em


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/items.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="mixed", is_enabled=True)
    db.add(lib)
    db.flush()

    def mk(item_type, name, **kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
            name=name, sort_name=name,
            file_path=f"/m/{uuid.uuid4().hex}.mkv", **kw)
        db.add(it)
        db.flush()
        return it

    m = {"lib": lib}
    m["A"] = mk("movie", "已绑定电影甲", tmdb_id="100", production_year=2001)
    m["B"] = mk("movie", "未绑定电影乙", production_year=2002)
    m["F"] = mk("movie", "只有IMDb电影戊", imdb_id="tt7654321",
                production_year=2005)
    m["C"] = mk("series", "已绑定剧丙", tmdb_id="200", production_year=2003)
    m["D"] = mk("series", "未绑定剧丁", production_year=2004)
    season_c = mk("season", "丙 Season 01", series_id=m["C"].id,
                  season_number=1)
    season_d = mk("season", "丁 Season 01", series_id=m["D"].id,
                  season_number=1)
    m["E1"] = mk("episode", "丙 S01E01", series_id=m["C"].id,
                 parent_id=season_c.id, season_number=1, episode_number=1)
    m["E2"] = mk("episode", "丁 S01E01", series_id=m["D"].id,
                 parent_id=season_d.id, season_number=1, episode_number=1)
    user = models.WebUser(username="gate", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    data = SimpleNamespace(db=db, m=m,
                           user=SimpleNamespace(id=user.id, is_staff=False))
    yield data
    db.close()


def _req(params):
    scope = {"type": "http", "method": "GET",
             "query_string": urlencode(params).encode(), "headers": []}
    return Request(scope)


def _browse(scene, **extra):
    params = {"ParentId": scene.m["lib"].guid, "Recursive": "true"}
    params.update(extra)
    res = api._query_items(_req(params), scene.user, scene.db, "http://t")
    return {i["Name"] for i in res["Items"]}, res


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 1)
    assert identity_gate.gate_enabled() is True


def test_gate_off_browse_matches_baseline(scene):
    names, res = _browse(scene)
    assert names == {"已绑定电影甲", "未绑定电影乙", "只有IMDb电影戊",
                     "已绑定剧丙", "未绑定剧丁"}
    assert res["TotalRecordCount"] == 5


def test_gate_on_browse_only_bound(scene, gate_on):
    names, res = _browse(scene)
    assert names == {"已绑定电影甲", "只有IMDb电影戊", "已绑定剧丙"}
    # 分页自洽：总数与实返一致（第三方客户端靠 TotalRecordCount 分页）
    assert res["TotalRecordCount"] == 3
    names1, res1 = _browse(scene, Limit="1")
    assert res1["TotalRecordCount"] == 3
    assert len(res1["Items"]) == 1


def test_gate_on_search_stays_wide(scene, gate_on):
    # 搜索面不收紧：未绑定条目照样能被点名搜到（FMBY「浏览严、搜索宽」）
    res = api._query_items(
        _req({"SearchTerm": "未绑定电影乙", "Recursive": "true"}),
        scene.user, scene.db, "http://t")
    assert {i["Name"] for i in res["Items"]} == {"未绑定电影乙"}


def test_gate_on_ids_direct_stays_open(scene, gate_on):
    # 直达面放行：凭 guid 直查未绑定条目照常返回（老收藏/在播不断）
    res = api._query_items(
        _req({"Ids": scene.m["B"].guid}), scene.user, scene.db, "http://t")
    assert {i["Name"] for i in res["Items"]} == {"未绑定电影乙"}
    assert res["TotalRecordCount"] == 1


def test_gate_on_provider_id_lookup_not_blocked(scene, gate_on):
    # 按编号点名查（AnyProviderIdEquals）不被门误伤
    res = api._query_items(
        _req({"AnyProviderIdEquals": "Tmdb.100", "Recursive": "true"}),
        scene.user, scene.db, "http://t")
    assert "已绑定电影甲" in {i["Name"] for i in res["Items"]}


def test_gate_on_episodes_follow_series(scene, gate_on):
    # 集跟随剧：已绑定剧的集能列，未绑定剧的集不能列
    res = api._query_items(
        _req({"ParentId": scene.m["C"].guid, "IncludeItemTypes": "Episode",
              "Recursive": "true"}),
        scene.user, scene.db, "http://t")
    assert {i["Name"] for i in res["Items"]} == {"丙 S01E01"}
    res = api._query_items(
        _req({"ParentId": scene.m["D"].guid, "IncludeItemTypes": "Episode",
              "Recursive": "true"}),
        scene.user, scene.db, "http://t")
    assert res["Items"] == []


def test_gate_on_bind_then_visible(scene, gate_on):
    # 上架事件：绑定落库（tmdb 写入 + bump 数据版本，即 bind-tmdb 写路径
    # 的真实动作）后同一浏览立刻能见，不需要任何「发布」动作
    names, _ = _browse(scene)
    assert "未绑定电影乙" not in names
    item = scene.db.query(em.MediaItem).filter(
        em.MediaItem.id == scene.m["B"].id).one()
    item.tmdb_id = "300"
    items_dedup_cache.bump_library_item_versions(
        scene.db, [scene.m["lib"].id], force=True)
    scene.db.commit()
    names, res = _browse(scene)
    assert "未绑定电影乙" in names
    assert res["TotalRecordCount"] == 4

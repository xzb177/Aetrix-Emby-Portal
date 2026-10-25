"""身份准入货架·二期：个人面与剧集面货架过滤钉。

案 A 口径：二期只管「系统主动枚举」的个人面——EA 继续观看 / 追剧下一集 /
收藏 / 已看、门户继续观看 / 门户收藏 / 追新日历；展示过滤、数据不删
（进度与收藏标记原样保留，重绑后自动回来）。搜索面与直达面（详情 /
季集 / 播放）不在二期动，案 B 从未获专项拍板、本文件不测 404。

开关纪律：二期只在总开关与二期开关同时开启时生效——关总开关必须
连二期一起回旧口径（回滚完整性），只开二期不开总开关时行为与旧口径
逐字一致；两开关全关时亦然。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api, identity_gate, portal
from backend.emby_server import models as em


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/p2.db")
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
    m["G"] = mk("movie", "未绑定电影庚", production_year=2006)
    m["C"] = mk("series", "已绑定剧丙", tmdb_id="200", production_year=2003)
    m["D"] = mk("series", "未绑定剧丁", production_year=2004)
    season_c = mk("season", "丙 Season 01", series_id=m["C"].id, season_number=1)
    season_d = mk("season", "丁 Season 01", series_id=m["D"].id, season_number=1)
    m["E1"] = mk("episode", "丙 S01E01", series_id=m["C"].id,
                 parent_id=season_c.id, season_number=1, episode_number=1)
    m["E2"] = mk("episode", "丙 S01E02", series_id=m["C"].id,
                 parent_id=season_c.id, season_number=1, episode_number=2)
    m["E3"] = mk("episode", "丁 S01E01", series_id=m["D"].id,
                 parent_id=season_d.id, season_number=1, episode_number=1)
    m["E4"] = mk("episode", "丁 S01E02", series_id=m["D"].id,
                 parent_id=season_d.id, season_number=1, episode_number=2)
    user = models.WebUser(username="p2", password_hash="x", is_active=True)
    db.add(user)
    db.flush()

    def umd(item, **kw):
        row = em.UserMediaData(user_id=user.id, item_id=item.id, **kw)
        db.add(row)
        db.flush()
        return row

    # 一行多标记：同一 (user, item) 只能有一行 UMD（uq_umdata_user_item），
    # 故进度/收藏能合的合，不能合的（played=True 与 played=False 互斥）
    # 用不同条目分担。
    # 进度（续看用）：甲（电影）、丙 E1（已绑定剧的集）、丁 E3（未绑定剧的集）、
    # 乙（未绑定电影）；甲、乙同时挂收藏标记。
    umd(m["A"], playback_position_ticks=1000, played=False, is_favorite=True)
    umd(m["B"], playback_position_ticks=1000, played=False, is_favorite=True)
    umd(m["E1"], playback_position_ticks=1000, played=False)
    umd(m["E3"], playback_position_ticks=1000, played=False)
    # 收藏（丙、丁只有收藏行，无进度）：甲、乙、丙、丁
    umd(m["C"], is_favorite=True)
    umd(m["D"], is_favorite=True)
    # 已看（戊已绑定、庚未绑定；与进度行分开，避免 played 互斥）
    umd(m["F"], played=True)
    umd(m["G"], played=True)
    db.commit()
    data = SimpleNamespace(db=db, m=m,
                           user=SimpleNamespace(id=user.id, is_staff=False),
                           real_user=user)
    yield data
    db.close()


def _req(params=None):
    scope = {"type": "http", "method": "GET",
             "query_string": urlencode(params or {}).encode(), "headers": []}
    return Request(scope)


def _gate2(monkeypatch, gate, phase2):
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", gate)
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_PHASE2_ENABLED", phase2)


def _names(items):
    return {i["Name"] for i in items}


def test_phase2_off_shows_everything(scene, monkeypatch):
    """两开关全关：与旧口径逐字一致，未绑定全在。"""
    _gate2(monkeypatch, 0, 0)
    res = api.get_resume(_req(), scene.user, scene.db)
    assert _names(res["Items"]) == {"已绑定电影甲", "未绑定电影乙",
                                    "已绑定剧丙", "未绑定剧丁"}
    fav = api.get_favorites(_req(), scene.user, scene.db)
    assert _names(fav["Items"]) == {"已绑定电影甲", "未绑定电影乙",
                                   "已绑定剧丙", "未绑定剧丁"}
    assert fav["TotalRecordCount"] == 4


def test_phase2_on_only_without_main_gate_changes_nothing(scene, monkeypatch):
    """只开二期、不开总开关：回滚完整性——行为与旧口径一致。"""
    _gate2(monkeypatch, 0, 1)
    assert identity_gate.phase2_enabled() is False
    res = api.get_resume(_req(), scene.user, scene.db)
    assert _names(res["Items"]) == {"已绑定电影甲", "未绑定电影乙",
                                    "已绑定剧丙", "未绑定剧丁"}


def test_ea_resume_filters_unbound_but_keeps_progress(scene, monkeypatch):
    """EA 续看：未绑定电影与未绑定剧的集不出；进度行不删。"""
    _gate2(monkeypatch, 1, 1)
    res = api.get_resume(_req(), scene.user, scene.db)
    assert _names(res["Items"]) == {"已绑定电影甲", "已绑定剧丙"}
    # 进度行原样保留（展示过滤不删数据）
    assert scene.db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == scene.m["B"].id,
        em.UserMediaData.playback_position_ticks > 0).count() == 1
    assert scene.db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == scene.m["E3"].id,
        em.UserMediaData.playback_position_ticks > 0).count() == 1


def test_ea_favorites_and_played_filter_but_keep_marks(scene, monkeypatch):
    """EA 收藏/已看：未绑定不出；标记保留；计数与实返一致。"""
    _gate2(monkeypatch, 1, 1)
    fav = api.get_favorites(_req(), scene.user, scene.db)
    assert _names(fav["Items"]) == {"已绑定电影甲", "已绑定剧丙"}
    assert fav["TotalRecordCount"] == 2
    played = api.get_played_items(_req(), scene.user, scene.db)
    assert _names(played["Items"]) == {"只有IMDb电影戊"}
    assert played["TotalRecordCount"] == 1
    # 标记不删：乙的收藏行、庚的已看行还在
    assert scene.db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == scene.m["B"].id,
        em.UserMediaData.is_favorite == True).count() == 1  # noqa: E712
    assert scene.db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == scene.m["G"].id,
        em.UserMediaData.played == True).count() == 1  # noqa: E712


def test_nextup_filters_unbound_series(scene, monkeypatch):
    """NextUp：未绑定剧不推下一集；SeriesId 点名未绑定剧返回空。"""
    _gate2(monkeypatch, 1, 1)
    res = api.get_next_up(_req(), scene.user, scene.db)
    assert _names(res["Items"]) == {"丙 S01E02"}
    assert res["TotalRecordCount"] == 1
    # SeriesId 点名未绑定剧 → 空（推送面）；点名已绑定剧 → 正常
    res_d = api.get_next_up(_req({"SeriesId": scene.m["D"].guid}),
                            scene.user, scene.db)
    assert res_d["Items"] == [] and res_d["TotalRecordCount"] == 0
    res_c = api.get_next_up(_req({"SeriesId": scene.m["C"].guid}),
                            scene.user, scene.db)
    assert _names(res_c["Items"]) == {"丙 S01E02"}


def test_portal_resume_and_favorites(scene, monkeypatch):
    """门户续看/收藏：与 EA 同口径（电影按自身、单集按所属剧）。"""
    _gate2(monkeypatch, 1, 1)
    res = portal.get_resume_list(request_user=scene.user, db=scene.db)
    assert {i["name"] for i in res["items"]} == {"已绑定电影甲", "已绑定剧丙"}
    fav = portal.get_favorite_list(request_user=scene.user, db=scene.db)
    assert {i["name"] for i in fav["items"]} == {"已绑定电影甲", "已绑定剧丙"}


def test_portal_calendar_counts_and_items_share_gate(scene, monkeypatch):
    """门户日历：角标计数与条目详情同源（都从 base_query 出）。"""
    _gate2(monkeypatch, 1, 1)
    # 时区无关：日历把入参当上海日、把 date_added 当 UTC-naive，
    # 入库时刻落在查询日的上海帧 [当天 00:00, 次日 00:00) 换算后
    # 即 [前一日 16:00 UTC, 当日 16:00 UTC) 内才命中。用当天 12:00
    # 做入库时刻——无论跑道时区是 UTC 还是上海都落在帧内；
    # 原写法用 datetime.now() 原样入库，在 UTC 跑道 16:00–24:00
    # （上海次日 00:00–08:00）时条目落在查询日之外，days 为空。
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    for it in scene.m.values():
        if isinstance(it, em.MediaItem):
            it.date_added = now
    scene.db.commit()
    req = SimpleNamespace(
        query_params={"start": now.strftime("%Y-%m-%d"),
                      "end": now.strftime("%Y-%m-%d")},
        base_url="http://portal.test/")
    res = portal.get_chase_calendar(request=req, request_user=scene.user,
                                    db=scene.db)
    day = res["days"][0]
    # 已绑定：甲、戊、丙、丙 E1、丙 E2；未绑定（乙、庚、丁、丁 E1/E2）不在
    assert day["count"] == 5
    assert {i["Name"] for i in day["items"]} == {"已绑定电影甲", "已绑定剧丙",
                                                "只有IMDb电影戊", "丙 S01E01",
                                                "丙 S01E02"}
    assert day["count"] == len(day["items"])

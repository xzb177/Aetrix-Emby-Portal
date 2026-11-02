"""PA-037：软删全局钩子看不见 JOIN 查询，全仓联结查询漏滤已下架行。

钩子（soft_delete._touches_items）只看语句 FROM 的表名；联结查询的 FROM
是 Join 对象（无 .name），钩子不触发，已下架行同时漏进列表与计数。
本文件逐一钉住 R15 跟进扫出的联结查询点：1 可见＋1 已下架的夹具下，
各读口返回里必须没有已下架条目；另补一条静态门禁：已修函数的源码里
必须出现 deleted_at 条件，防止有人把显式过滤改丢。

隔离 SQLite 内存库，不连生产库；软删钩子装在 Session 类上，
import soft_delete 即生效（默认开）。已下架实例提交后在 ORM 里不可见，
故夹具只返回主键 id（同 test_pa035_count.py）。
"""

from __future__ import annotations

import inspect
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import soft_delete  # noqa: F401 — 装全局软删钩子

TICKS = 10_000_000


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db, name="pa037user", staff=True):
    u = base_models.WebUser(username=name, password_hash="x", is_active=True,
                            is_staff=staff)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _lib(db, guid="pa037lib"):
    lib = db.query(em.Library).filter(em.Library.guid == guid).first()
    if lib is None:
        lib = em.Library(guid=guid, name=guid, is_enabled=True)
        db.add(lib)
        db.commit()
        db.refresh(lib)
    return lib


def _item(db, guid, deleted=False, **kw):
    kw.setdefault("name", guid)
    kw.setdefault("item_type", "movie")
    kw.setdefault("library_id", _lib(db).id)
    kw.setdefault("duration_ticks", 3600 * TICKS)
    it = em.MediaItem(guid=guid, **kw)
    db.add(it)
    db.commit()
    db.refresh(it)
    item_id = it.id
    if deleted:
        it.deleted_at = datetime.now()
        db.commit()
        # 软删提交后该实例在 ORM 里即不可见，之后再碰它会报 ObjectDeletedError：
        # 从会话里逐出，后续查询（联结盲区）加载的是全新实例。
        db.expunge(it)
    return item_id


def _sess(db, user_id, item_id, key, **kw):
    kw.setdefault("start_time", datetime.now())
    kw.setdefault("last_update_at", datetime.now())
    kw.setdefault("position_ticks", 120 * TICKS)
    s = em.PlaybackSession(session_key=key, user_id=user_id, item_id=item_id, **kw)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _umd(db, user_id, item_id, **kw):
    kw.setdefault("playback_position_ticks", 120 * TICKS)
    kw.setdefault("played", False)
    kw.setdefault("hide_from_resume", False)
    row = em.UserMediaData(user_id=user_id, item_id=item_id, **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def _req(query_string: bytes = b""):
    return Request(scope={
        "type": "http", "method": "GET", "path": "/emby/Users/1/Items",
        "query_string": query_string, "headers": [(b"host", b"test")],
    })


def _vis_del(db):
    """1 可见＋1 已下架的电影，及配套会话/进度行。返回 (user, vis_id, del_id)。"""
    user = _user(db)
    vis = _item(db, "pa037-vis")
    dele = _item(db, "pa037-del", deleted=True)
    _sess(db, user.id, vis, "pa037-s-vis")
    _sess(db, user.id, dele, "pa037-s-del")
    _umd(db, user.id, vis, is_favorite=True, play_count=3)
    _umd(db, user.id, dele, is_favorite=True, play_count=9)
    return user, vis, dele


# ---------------- home_summary：统计与未结束会话 ----------------

def test_home_stats_and_sessions_exclude_deleted(db):
    from backend.api import home_summary
    user, _vis, _dele = _vis_del(db)
    stats = home_summary._stats(db, user)
    assert "pa037-del" not in _dump(stats["recent"])
    assert "pa037-vis" in _dump(stats["recent"])
    sessions = home_summary._sessions(db, user)
    assert "pa037-del" not in _dump(sessions["sessions"])
    assert "pa037-vis" in _dump(sessions["sessions"])


# ---------------- panel：昨日信与口味/成就 ----------------

def test_panel_letter_tastes_evaluate_exclude_deleted(db):
    from backend.api import panel
    from backend import panel_achievements
    user, _vis, _dele = _vis_del(db)
    # 昨日信：会话落进昨日窗口——用生产同款时钟（北京时间帧）算窗口内时间
    from backend.api.panel import _now_local_naive
    yesterday_start = (_now_local_naive() - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    yesterday_noon = yesterday_start + timedelta(hours=12)
    for s in db.query(em.PlaybackSession).filter(
            em.PlaybackSession.user_id == user.id).all():
        s.start_time = yesterday_noon
    db.commit()
    letter = panel.panel_letter(db=db, user=user)
    assert "pa037-del" not in _dump(letter)
    # 口味：已下架条目的 genre 不计入
    db.query(em.MediaItem).filter(em.MediaItem.id == _dele).update(
        {"genres": '["动画"]'}, synchronize_session=False)
    db.query(em.MediaItem).filter(em.MediaItem.id == _vis).update(
        {"genres": '["动画"]'}, synchronize_session=False)
    db.commit()
    tastes = panel_achievements.user_tastes(db, user.id)
    assert tastes["taste_anime"]["count"] == 1
    # 成就：只有已下架条目的深夜会话不许解锁 first_night
    db.add(base_models.PanelAchievementDef(
        key="first_night", name="深夜", desc="深夜", enabled=True))
    db.commit()
    night = yesterday_noon.replace(hour=23, minute=30)  # 北京时间 23:30，深夜档
    for s in db.query(em.PlaybackSession).filter(
            em.PlaybackSession.user_id == user.id).all():
        s.start_time = night
    db.commit()
    # 只留已下架条目的会话有效：可见条目会话的 position 清零（<=60 秒不算）
    db.query(em.PlaybackSession).filter(
        em.PlaybackSession.item_id == _vis).update(
        {"position_ticks": 0}, synchronize_session=False)
    db.commit()
    assert panel_achievements.evaluate(user.id, db) == []


# ---------------- portal：续看/收藏/统计/会话/历史/管理会话 ----------------

def test_portal_surfaces_exclude_deleted(db):
    from backend.emby_server import portal
    user, _vis, _dele = _vis_del(db)
    resume = portal.get_resume_list(request_user=user, db=db)
    assert "pa037-del" not in _dump(resume)
    assert "pa037-vis" in _dump(resume)
    favs = portal.get_favorite_list(request_user=user, db=db)
    assert "pa037-del" not in _dump(favs)
    assert "pa037-vis" in _dump(favs)
    stats = portal.get_watch_stats(request_user=user, db=db)
    assert "pa037-del" not in _dump(stats["recent"])
    my_sess = portal.get_my_sessions(request_user=user, db=db)
    assert "pa037-del" not in _dump(my_sess["sessions"])
    hist = portal.get_watch_history(request_user=user, db=db)
    assert "pa037-del" not in _dump(hist)
    admin_sess = portal.admin_sessions(staff=user, db=db)
    assert "pa037-del" not in _dump(admin_sess["sessions"])


# ---------------- Emby 兼容层：Resume / Items 收藏筛选 / 未看角标 ----------------

def test_emby_resume_and_query_items_exclude_deleted(db):
    from backend.emby_server import api as emby_api
    user, _vis, _dele = _vis_del(db)
    resume = emby_api.get_resume(request=_req(), user=user, db=db)
    assert "pa037-del" not in _dump(resume)
    assert "pa037-vis" in _dump(resume)
    items = emby_api._query_items(request=_req(b"IsFavorite=true"),
                                  user=user, db=db, base="http://test")
    assert "pa037-del" not in _dump(items["Items"])
    assert "pa037-vis" in _dump(items["Items"])
    assert items["TotalRecordCount"] == 1


def test_emby_unplayed_counts_exclude_deleted_episodes(db):
    from backend.emby_server import api as emby_api
    user = _user(db)
    series = _item(db, "pa037-series", item_type="series")
    _ep_vis = _item(db, "pa037-ep-vis", item_type="episode", series_id=series)
    _ep_del = _item(db, "pa037-ep-del", item_type="episode", series_id=series,
                    deleted=True)
    items = [db.query(em.MediaItem).filter(em.MediaItem.id == series).first()]
    emby_api._prefetch_list_data(db, user.id, items)
    dto = emby_api._item_dto(items[0], "http://test", user.id, db)
    assert dto["UserData"]["UnplayedItemCount"] == 1


def test_emby_popular_ranking_query_fixed():
    """热门排行查询本身已带 deleted_at（静态门禁覆盖 get_suggestions）。

    注意：经核查，该查询的占位效应经 get_suggestions 输出是中性的——
    第二步 pop_items 走 base_q（钩子生效，已下架行被滤掉），且 Items
    循环在 limit 处截断、按 id 去重，popular 恒不改变输出。因此这里不
    硬造一个输出级断言（会把“输出不变”误判成“修复无效”）；修法的意义
    是让排行查询本身的语义正确、防后来人复用 pop_rows 时漏网。
    本测试只作占位说明，真正钉住修法的是 test_fixed_functions_carry_deleted_at。
    """
    import inspect
    from backend.emby_server import api as emby_api
    src = inspect.getsource(emby_api.get_suggestions)
    assert "deleted_at" in src


def test_media_library_recent_excludes_deleted(db):
    from backend.api import media_library
    user, _vis, _dele = _vis_del(db)
    rows = media_library._recently_updated_items(db)
    assert "pa037-del" not in _dump(rows)
    assert "pa037-vis" in _dump(rows)


def test_sort_projection_orphan_row_falls_back_and_excludes_deleted(db):
    from backend.emby_server import item_sort_projection as isp
    user = _user(db)
    lib = _lib(db)
    vis_a = _item(db, "pa037-proj-a")
    vis_b = _item(db, "pa037-proj-b")
    dele = _item(db, "pa037-proj-del", deleted=True)
    # 孤行场景：投影表里有可见 A、B 与已下架 D。PA-042 之前本用例假定
    # _live_items_query 的 Query.count() 有子查询盲区、活数按 3 算，
    # 故 3 行投影能对上计数、走到分页查询；PA-042 把计数改成
    # with_entities(func.count()).scalar() 后钩子正常过滤，活数是 2、
    # 与 3 行投影对不上——覆盖校验正确判定为陈旧覆盖、返回 None 回落
    # 现算（回落路径同样过滤已下架）。本用例改按新口径断言：内层
    # _projection_page 返回 None（回落信号），公开口 load_page 走回落
    # 后仍只返回可见条目，已下架绝不冒出来。
    db.add(em.MediaItemSortProjection(library_id=lib.id, item_id=vis_a,
                                      title_key="a"))
    db.add(em.MediaItemSortProjection(library_id=lib.id, item_id=vis_b,
                                      title_key="b"))
    db.add(em.MediaItemSortProjection(library_id=lib.id, item_id=dele,
                                      title_key="d"))
    db.commit()
    assert isp._projection_page(db, lib.id, "title", None, None, 10) is None
    items, _cursor = isp.load_page(db, lib.id, "title", None, None, 10)
    names = [it.name for it in items]
    assert "pa037-proj-del" not in names
    assert "pa037-proj-a" in names
    assert "pa037-proj-b" in names


def test_sort_projection_join_filter_excludes_deleted_when_counts_match(db):
    from backend.emby_server import item_sort_projection as isp
    user = _user(db)
    lib = _lib(db)
    vis_a = _item(db, "pa037-proj2-a")
    vis_b = _item(db, "pa037-proj2-b")
    dele = _item(db, "pa037-proj2-del", deleted=True)
    # 联结过滤本身的行为级钉子：投影行故意摆成 A＋已下架 D（B 活着但
    # 缺投影行），活数与投影数恰好都是 2、覆盖校验放行，真正走到分页
    # 查询；此时只有联结里的显式 deleted_at 条件能把 D 挡在页外——
    # 若有人把该条件改丢，本用例会把 D 带出来而失败。
    now = datetime.now()
    db.add(em.MediaItemSortProjection(library_id=lib.id, item_id=vis_a,
                                      title_key="a", computed_at=now))
    db.add(em.MediaItemSortProjection(library_id=lib.id, item_id=dele,
                                      title_key="d", computed_at=now))
    db.commit()
    page = isp._projection_page(db, lib.id, "title", None, None, 10)
    assert page is not None
    names = [it.name for _proj, it in page[0]]
    assert names == ["pa037-proj2-a"]


def test_transfer_preheat_signal_ignores_deleted(db):
    from backend.emby_server import transfer_variant as tv
    user = _user(db)
    series = _item(db, "pa037-ph-series", item_type="series")
    anchor = SimpleNamespace(series_id=series)
    now = datetime.now()
    for i in range(2):
        ep = _item(db, f"pa037-ph-ep{i}", item_type="episode", series_id=series)
        _umd(db, user.id, ep, last_played_at=now - timedelta(days=1))
    assert tv._series_preheat_strong_signal(db, user.id, anchor) is True
    # 可见集全下架后，只剩已下架集的播放记录：信号必须消失
    db.query(em.MediaItem).filter(
        em.MediaItem.series_id == series,
        em.MediaItem.item_type == "episode").update(
        {"deleted_at": now}, synchronize_session=False)
    db.commit()
    assert tv._series_preheat_strong_signal(db, user.id, anchor) is False


def test_tg_pet_and_rank_exclude_deleted(db):
    from backend.tg_bot import features as tg_features
    from backend.tg_bot import queries as tg_queries
    user, _vis, _dele = _vis_del(db)
    hours, _cur, _nxt = tg_features._pet_summary(db, user.id)
    assert hours == 120 / 3600  # 仅可见条目 120 秒；旧实现连已下架一起算 240 秒
    rank = tg_queries.watch_duration_rank(db, "day", limit=10)
    assert rank and rank[0]["seconds"] == 120


def test_admin_hot_items_exclude_deleted(db):
    from backend.api import admin as admin_api
    user, _vis, _dele = _vis_del(db)
    stats = admin_api.get_playback_stats(current_admin=user, db=db)
    assert "pa037-del" not in _dump(stats)
    assert "pa037-vis" in _dump(stats)


# ---------------- 静态门禁：已修函数源码必须带 deleted_at 条件 ----------------

FIXED_FUNCTIONS = [
    ("backend.api.home_summary", "_stats"),
    ("backend.api.home_summary", "_sessions"),
    ("backend.api.media_library", "_recently_updated_items"),
    ("backend.api.panel", "panel_letter"),
    ("backend.api.panel", "panel_collector"),
    ("backend.api.admin", "get_playback_stats"),
    ("backend.emby_server.api", "_prefetch_list_data"),
    ("backend.emby_server.api", "_item_dto"),
    ("backend.emby_server.api", "_query_items"),
    ("backend.emby_server.api", "get_suggestions"),
    ("backend.emby_server.api", "get_resume"),
    ("backend.emby_server.item_sort_projection", "_projection_page"),
    ("backend.emby_server.portal", "get_resume_list"),
    ("backend.emby_server.portal", "get_favorite_list"),
    ("backend.emby_server.portal", "get_watch_stats"),
    ("backend.emby_server.portal", "get_my_sessions"),
    ("backend.emby_server.portal", "get_watch_history"),
    ("backend.emby_server.portal", "admin_sessions"),
    ("backend.emby_server.transfer_variant", "_series_preheat_strong_signal"),
    ("backend.panel_achievements", "user_tastes"),
    ("backend.panel_achievements", "evaluate"),
    ("backend.tg_bot.features", "_pet_summary"),
    ("backend.tg_bot.queries", "watch_duration_rank"),
]


def test_fixed_functions_carry_deleted_at():
    import importlib
    missing = []
    for mod_name, func_name in FIXED_FUNCTIONS:
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(getattr(mod, func_name))
        if "deleted_at" not in src:
            missing.append(f"{mod_name}.{func_name}")
    assert not missing, f"联结查询点丢了 deleted_at 显式过滤：{missing}"

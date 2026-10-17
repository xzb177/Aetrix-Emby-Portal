"""追新日历（GET /api/user/emby/calendar）单元测试

用户要问的问题只有一个——「今天/这周/这月新上了什么」。日历页的全部价值都压在
后端这个端点上，所以这里把它当成契约来钉住，每条都对应一个「做错了用户就会
看到假日历」的事故：

1. **按 ``date_added`` 分组，不是 ``date_modified``**。后者带 ``onupdate``，
   后台补全一次元数据就刷新一次，用它会把老片顶到今天——这是整个功能的命门。
2. **分天边界是左闭右开**：``end`` 当天 00:00 的条目不算进区间，
   23:59:59 的算。否则「按月翻页」会看到当月最后一天的东西漏到下个月。
3. **可见范围与媒体库列表同口径**：不可见的库里上了新片，日历里也不能出现
   （否则等于用日历把内部库剧透了）。
4. **筛选不能静默失效**：指定了一个解析不出的库要返回空，而不是退回「全部库」。
5. **单日截断**：一天上千条时 ``count`` 仍是真实总数，``items`` 才截断——
   拿 ``len(items)`` 当总数会让「+12」变成「+3」。

全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""
import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import library_scope, models
from backend.emby_server import models as em
from backend.emby_server import portal
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 配置热缓存是进程级的（key 不带库名）：进出都清一次，避免污染别的内存库测试
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _user(db, username="alice", **kw):
    row = models.WebUser(username=username, password_hash="x", **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _library(db, name, **kw):
    row = em.Library(guid=f"guid-{name}", name=name, **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib, name, item_type="movie", added=None, is_hidden=False, **kw):
    row = em.MediaItem(
        guid=f"g-{name}-{item_type}",
        library_id=lib.id,
        item_type=item_type,
        name=name,
        is_hidden=is_hidden,
        date_added=added,
        **kw,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _call(db, user, **params):
    """直接调 handler：只关心口径，不必为整个 App 起 TestClient"""
    request = SimpleNamespace(query_params=params, base_url="http://portal.test/")
    return portal.get_chase_calendar(request=request, request_user=user, db=db)


def _dates(payload):
    return [d["date"] for d in payload["days"]]


# ==================== 1. 分组口径 ====================


def test_groups_items_by_date_added(db):
    # r35：分组按 Asia/Shanghai 自然日（date_added 是 UTC-naive 入库）。
    # 甲 09:30 UTC=17:30 CST→10-01；乙 22:15 UTC=次日 06:15 CST→10-02；
    # 丙 07:00 UTC=15:00 CST→10-03。
    lib = _library(db, "电影")
    user = _user(db)
    _item(db, lib, "甲", added=datetime(2026, 10, 1, 9, 30))
    _item(db, lib, "乙", added=datetime(2026, 10, 1, 22, 15))
    _item(db, lib, "丙", added=datetime(2026, 10, 3, 7, 0))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    assert _dates(payload) == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert payload["days"][0]["count"] == 1
    assert payload["days"][1]["count"] == 1
    assert payload["total"] == 3
    assert [i["Name"] for i in payload["days"][1]["items"]] == ["乙"]
    assert [i["Name"] for i in payload["days"][2]["items"]] == ["丙"]


def test_uses_date_added_not_date_modified(db):
    """老片今天被补全（date_modified=今天，date_added=三个月前）不该出现在今天"""
    lib = _library(db, "电影")
    user = _user(db)
    old = _item(db, lib, "老片", added=datetime(2026, 7, 1, 12, 0))
    db.commit()
    old.date_modified = datetime(2026, 10, 5, 8, 0)
    db.commit()

    assert _dates(_call(db, user, start="2026-10-01", end="2026-10-31")) == []
    assert _dates(_call(db, user, start="2026-07-01", end="2026-07-31")) == ["2026-07-01"]


def test_hidden_items_are_excluded(db):
    lib = _library(db, "电影")
    user = _user(db)
    _item(db, lib, "可见", added=datetime(2026, 10, 2, 10, 0))
    _item(db, lib, "已隐藏", added=datetime(2026, 10, 2, 11, 0), is_hidden=True)

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    assert payload["total"] == 1
    assert [i["Name"] for i in payload["days"][0]["items"]] == ["可见"]


# ==================== 2. 区间边界 ====================


def test_range_end_is_exclusive(db):
    """end 当天 00:00:00（上海）的条目属于「下个月」，不能出现在本月最后一格"""
    # r35：边界按上海自然日。月末 15:59:59 UTC=23:59:59 CST→10-31；
    # 下月初 16:00 UTC=11-01 00:00 CST→11-01，不进 10 月区间。
    lib = _library(db, "电影")
    user = _user(db)
    _item(db, lib, "月末", added=datetime(2026, 10, 31, 15, 59, 59))
    _item(db, lib, "下月初", added=datetime(2026, 10, 31, 16, 0, 0))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    assert _dates(payload) == ["2026-10-31"]
    assert [i["Name"] for i in payload["days"][0]["items"]] == ["月末"]
    assert payload["end"] == "2026-10-31", "回给前端的 end 是闭区间口径（含当天）"


def test_single_day_range(db):
    lib = _library(db, "电影")
    user = _user(db)
    _item(db, lib, "当天", added=datetime(2026, 10, 9, 13, 0))
    _item(db, lib, "隔天", added=datetime(2026, 10, 10, 13, 0))

    payload = _call(db, user, start="2026-10-09", end="2026-10-09")

    assert _dates(payload) == ["2026-10-09"]
    assert payload["start"] == payload["end"] == "2026-10-09"


def test_end_before_start_degrades_to_one_day(db):
    """end < start 时按单天处理，而不是返回空日历或抛 500"""
    lib = _library(db, "电影")
    user = _user(db)
    _item(db, lib, "当天", added=datetime(2026, 10, 9, 13, 0))

    payload = _call(db, user, start="2026-10-09", end="2026-10-01")

    assert payload["start"] == payload["end"] == "2026-10-09"
    assert _dates(payload) == ["2026-10-09"]


def test_range_is_capped(db):
    """超长区间被截断到上限，不会因为有人传 1970 年而把整库拉出来"""
    lib = _library(db, "电影")
    user = _user(db)
    _item(db, lib, "被截在外面", added=datetime(1990, 1, 1, 10, 0))

    payload = _call(db, user, start="1999-01-01", end="2026-10-01")

    assert payload["total"] == 0
    assert payload["end"] == "1999-04-03"
    assert (datetime.fromisoformat(payload["end"]) - datetime.fromisoformat(payload["start"])).days \
        == portal.CALENDAR_MAX_DAYS - 1


def test_defaults_to_current_month(db):
    lib = _library(db, "电影")
    user = _user(db)
    today = datetime.now()
    _item(db, lib, "本月", added=today.replace(day=1, hour=10, minute=0, second=0, microsecond=0))
    _item(db, lib, "半年前", added=today.replace(month=1, day=1, hour=10, minute=0, second=0, microsecond=0))

    payload = _call(db, user)

    assert payload["start"] == today.replace(day=1).date().isoformat()
    assert payload["total"] == 1


def test_malformed_dates_fall_back_to_defaults(db):
    lib = _library(db, "电影")
    user = _user(db)
    today = datetime.now()
    _item(db, lib, "本月", added=today.replace(day=1, hour=10, minute=0, second=0, microsecond=0))

    for bad in ("not-a-date", "", "2026-13-45"):
        payload = _call(db, user, start=bad, end=bad)
        assert payload["start"] == today.replace(day=1).date().isoformat()
        assert payload["total"] == 1


# ==================== 3. 筛选：类型 ====================


def test_item_type_filter(db):
    lib = _library(db, "综合")
    user = _user(db)
    _item(db, lib, "电影甲", item_type="movie", added=datetime(2026, 10, 2, 10, 0))
    _item(db, lib, "剧集乙", item_type="series", added=datetime(2026, 10, 2, 11, 0))
    _item(db, lib, "单集丙", item_type="episode", added=datetime(2026, 10, 2, 12, 0))

    only_movie = _call(db, user, start="2026-10-01", end="2026-10-31", item_type="movie")
    assert [i["Name"] for i in only_movie["days"][0]["items"]] == ["电影甲"]

    both = _call(db, user, start="2026-10-01", end="2026-10-31", item_type="series,episode")
    assert {i["Name"] for i in both["days"][0]["items"]} == {"剧集乙", "单集丙"}


def test_season_is_never_listed(db):
    """季是剧集的中间层，出现在「今天新上了什么」里只是噪音"""
    lib = _library(db, "剧集")
    user = _user(db)
    _item(db, lib, "第一季", item_type="season", added=datetime(2026, 10, 2, 10, 0))
    _item(db, lib, "第一集", item_type="episode", added=datetime(2026, 10, 2, 10, 5))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    assert [i["Name"] for i in payload["days"][0]["items"]] == ["第一集"]


def test_unknown_item_type_falls_back_to_all(db):
    lib = _library(db, "电影")
    user = _user(db)
    _item(db, lib, "电影甲", item_type="movie", added=datetime(2026, 10, 2, 10, 0))
    _item(db, lib, "剧集乙", item_type="series", added=datetime(2026, 10, 2, 11, 0))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31", item_type="musicvideo")

    assert payload["total"] == 2


def test_type_counts_ignore_the_type_filter(db):
    """``types`` 是「这个区间里各类各有多少」，不受 item_type 筛选影响。

    前端拿它给筛选按钮写角标：跟着筛选走的话，用户点了「只看剧集」之后
    电影按钮就永远显示 0，看不出这个月到底有没有电影。
    """
    lib = _library(db, "综合")
    user = _user(db)
    _item(db, lib, "电影甲", item_type="movie", added=datetime(2026, 10, 2, 10, 0))
    _item(db, lib, "剧集乙", item_type="series", added=datetime(2026, 10, 2, 11, 0))
    _item(db, lib, "单集丙", item_type="episode", added=datetime(2026, 10, 2, 12, 0))

    only_series = _call(db, user, start="2026-10-01", end="2026-10-31", item_type="series")

    assert only_series["total"] == 1
    assert only_series["types"] == {"movie": 1, "series": 1, "episode": 1}


def test_season_is_not_counted_either(db):
    """季既不出现在 days 里，也不该出现在 types 的角标里"""
    lib = _library(db, "剧集")
    user = _user(db)
    _item(db, lib, "第一季", item_type="season", added=datetime(2026, 10, 2, 10, 0))
    _item(db, lib, "第一集", item_type="episode", added=datetime(2026, 10, 2, 10, 5))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    assert payload["types"] == {"episode": 1}


# ==================== 4. 筛选：媒体库 ====================


def test_library_filter_accepts_id_and_guid(db):
    movies = _library(db, "电影")
    shows = _library(db, "剧集")
    user = _user(db)
    _item(db, movies, "电影甲", added=datetime(2026, 10, 2, 10, 0))
    _item(db, shows, "剧集乙", item_type="series", added=datetime(2026, 10, 2, 11, 0))

    by_id = _call(db, user, start="2026-10-01", end="2026-10-31", library_id=str(movies.id))
    by_guid = _call(db, user, start="2026-10-01", end="2026-10-31", library_id=movies.guid)

    for payload in (by_id, by_guid):
        assert [i["Name"] for i in payload["days"][0]["items"]] == ["电影甲"]


def test_unknown_library_filter_yields_empty_not_everything(db):
    """筛选值解析不出来时返回空 —— 静默退回「全部库」等于把内部库的新片端出来"""
    movies = _library(db, "电影")
    user = _user(db)
    _item(db, movies, "电影甲", added=datetime(2026, 10, 2, 10, 0))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31", library_id="no-such-guid")

    assert payload["total"] == 0
    assert payload["days"] == []


def test_item_carries_library_name(db):
    movies = _library(db, "电影")
    user = _user(db)
    _item(db, movies, "电影甲", added=datetime(2026, 10, 2, 10, 0))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    assert payload["days"][0]["items"][0]["LibraryName"] == "电影"


# ==================== 5. 可见范围 ====================


def test_library_scope_is_applied(db):
    """不可见的库里上了新片，日历里也不能出现"""
    open_lib = _library(db, "电影")
    secret_lib = _library(db, "内部")
    user = _user(db)
    _item(db, open_lib, "公开片", added=datetime(2026, 10, 2, 10, 0))
    _item(db, secret_lib, "内部片", added=datetime(2026, 10, 2, 11, 0))
    library_scope.write_default(db, True, [open_lib.id])
    db.commit()

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    assert [i["Name"] for i in payload["days"][0]["items"]] == ["公开片"]


def test_staff_sees_every_library(db):
    open_lib = _library(db, "电影")
    secret_lib = _library(db, "内部")
    boss = _user(db, "boss", is_staff=True)
    _item(db, secret_lib, "内部片", added=datetime(2026, 10, 2, 11, 0))
    library_scope.write_default(db, True, [open_lib.id])
    db.commit()

    payload = _call(db, boss, start="2026-10-01", end="2026-10-31")

    assert [i["Name"] for i in payload["days"][0]["items"]] == ["内部片"]


# ==================== 6. 单日截断 ====================


def test_day_overflow_truncates_items_but_keeps_true_count(db):
    """一次导了 30 条的当天：count 必须是 30，items 只回上限条数。

    拿 len(items) 当总数会让日历上的「+N」变成「+3」，用户以为当天只有 3 部。
    """
    lib = _library(db, "电影")
    user = _user(db)
    total = portal.CALENDAR_MAX_ITEMS_PER_DAY + 6
    for i in range(total):
        _item(db, lib, f"片{i:02d}", added=datetime(2026, 10, 4, 8, i))

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")
    day = payload["days"][0]

    assert day["count"] == total
    assert len(day["items"]) == portal.CALENDAR_MAX_ITEMS_PER_DAY
    assert payload["total"] == total


def test_truncated_day_keeps_newest_items(db):
    """超限时留下的是当天**最新入库**的那批（用户最关心刚到的）"""
    lib = _library(db, "电影")
    user = _user(db)
    total = portal.CALENDAR_MAX_ITEMS_PER_DAY + 5
    for i in range(total):
        _item(db, lib, f"片{i:02d}", added=datetime(2026, 10, 4, 8, 0) + __import__("datetime").timedelta(minutes=i))

    names = {i["Name"] for i in _call(db, user, start="2026-10-01", end="2026-10-31")["days"][0]["items"]}

    assert f"片{total - 1:02d}" in names
    assert "片00" not in names


# ==================== 7. DTO 形状 ====================


def test_items_use_emby_dto_keys(db):
    """前端按 Emby 协议字段渲染（Id / Name / Type / ImageTags），
    直接复用 /emby 的 DTO 工厂，不另造一套下划线结构"""
    lib = _library(db, "电影")
    user = _user(db)
    row = _item(db, lib, "电影甲", added=datetime(2026, 10, 2, 10, 0))
    row.production_year = 2026
    row.community_rating = 8.1
    db.commit()

    item = _call(db, user, start="2026-10-01", end="2026-10-31")["days"][0]["items"][0]

    assert item["Id"] == row.guid, "Id 必须是 guid —— 详情页深链 /media/:id 靠它"
    assert item["Name"] == "电影甲"
    assert item["Type"] == "Movie"
    assert item["ProductionYear"] == 2026
    assert item["CommunityRating"] == 8.1


def test_episode_carries_series_context(db):
    """单集在日历里必须带剧名，否则一格「第 3 集」用户根本认不出是哪部"""
    lib = _library(db, "剧集")
    user = _user(db)
    show = _item(db, lib, "某剧", item_type="series", added=datetime(2026, 1, 1, 10, 0))
    _item(db, lib, "某剧 第 3 集", item_type="episode",
          added=datetime(2026, 10, 2, 10, 0), series_id=show.id, season_number=1, episode_number=3)

    item = _call(db, user, start="2026-10-01", end="2026-10-31")["days"][0]["items"][0]

    assert item["SeriesName"] == "某剧"
    assert item["IndexNumber"] == 3


def test_empty_result_is_well_formed(db):
    _library(db, "电影")
    user = _user(db)

    payload = _call(db, user, start="2026-10-01", end="2026-10-31")

    # 2026-10-13 起响应多带一个 teaser 字段（探险预告 · 保密新大陆揭晓位；
    # 无配置时为保密默认态）。它是公开契约的一部分，不是多余字段。
    assert payload == {
        "start": "2026-10-01", "end": "2026-10-31", "total": 0,
        "days": [], "types": {},
        "teaser": {
            "status": "secret",
            "title": "? ? ?",
            "subtitle": "神秘新大陆 · 到日子自动揭晓",
            "countdown_days": None,
        },
    }
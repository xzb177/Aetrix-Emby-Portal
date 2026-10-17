"""r35 后端完美迭代：展示统计口径 · 上海自然日收口。

F1: /api/panel/server 生长记录 7 天 —— date_added 是 UTC-naive 入库，
    此前按 func.date 的 UTC 日分组、标签按上海自然日画 → 每天 00:00–08:00（CST）
    入库的条目被算到前一天。改为原始时间戳 +8h 按上海自然日归桶。
F2: /api/panel/today 今日观影人数 —— 此前拿上海-naive 的 00:00 去比 UTC-naive
    的 start_time → 00:00–08:00 漏计、08:00 后多计入昨天的人。改走
    _today_start_utc_naive()。
F3: 每日一封/馆灵累计小时/口味印章/成就 —— 读的是零写入的死表 emby_sessions
    （生产 0 行），4 个功能静默空数据。数据源切换涉及口径定义，需用户拍板，
    本轮只报备不修（测试不断言死行为）。
F4: 管理后台 /stats/playback「今日播放次数/今日观看用户数」—— 此前用服务器
    本地=UTC 日，"今日"实为 UTC 日。改上海自然日语义。
F5: 观影碑日榜/周榜 —— 旧注释"假定 naive 时间本身就是上海本地时间"不成立
    （start_time 是 UTC-naive 入库），00:00–08:00 的播放被排除在榜外。
    比较改用 UTC 帧，对外 window_start 仍报上海口径。
F6: 追新日历 —— 当月界/分组按上海自然日（既有 test_chase_calendar.py 已按
    新口径更新，这里只补 00:00–08:00 边界用例）。

全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from types import SimpleNamespace

from backend import models
from backend.api import admin as admin_api
from backend.api import panel as panel_api
from backend.api.panel import _today_start_utc_naive
from backend.emby_server import models as em
from backend.emby_server import portal


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _user(db, username="r35u", **kw):
    u = models.WebUser(username=username, password_hash="x", is_active=True, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _library(db, name="r35lib"):
    lib = em.Library(guid=f"guid-{name}", name=name)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib, name, item_type="movie", added=None):
    row = em.MediaItem(
        guid=f"g-r35-{name}-{item_type}", library_id=lib.id,
        item_type=item_type, name=name, date_added=added,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _session(db, user, key, start, item_id=None):
    row = em.PlaybackSession(
        user_id=user.id, item_id=item_id, session_key=key,
        start_time=start, last_update_at=start,
    )
    db.add(row)
    db.commit()
    return row


def _shanghai_today_0200_utc():
    """上海今天 02:00 换算成 UTC-naive —— 旧口径下"今天"的统计会漏掉它。"""
    return _today_start_utc_naive() + timedelta(hours=2)


# ==================== F1：生长记录 7 天 ====================

def test_growth_buckets_by_shanghai_day(db):
    lib = _library(db)
    user = _user(db, is_staff=True)
    # 上海今天 02:00 入库（UTC 昨天 18:00）：旧口径算到"昨天"，新口径算"今天"
    _item(db, lib, "凌晨新片", added=_shanghai_today_0200_utc())
    # 上海昨天 20:00 入库（UTC 昨天 12:00）：两边都算"昨天"，守住不漂移
    _item(db, lib, "昨晚旧片",
          added=_today_start_utc_naive() - timedelta(hours=4))

    payload = panel_api.panel_server(db=db, user=user)["data"]
    growth = {g["date"]: g["count"] for g in payload["growth"]}

    from backend.api.panel import _now_local_naive
    today_key = _now_local_naive().strftime("%m-%d")
    yesterday_key = (_now_local_naive() - timedelta(days=1)).strftime("%m-%d")
    assert growth[today_key] == 1, "上海今天 02:00 入库应计入今天"
    assert growth[yesterday_key] == 1, "上海昨天 20:00 入库应计入昨天"


# ==================== F2：今日观影人数 ====================

def test_today_viewers_counts_shanghai_day_boundary(db):
    user = _user(db, username="r35v1")
    _user(db, username="r35v2")
    # 上海今天 02:00 开播：旧口径（上海-naive 00:00 去比 UTC 列）漏计
    _session(db, user, "r35-s1", _shanghai_today_0200_utc())
    # 上海昨天 23:00 开播（UTC 昨天 15:00）：新口径不计入今天
    other = db.query(models.WebUser).filter_by(username="r35v2").one()
    _session(db, other, "r35-s2", _today_start_utc_naive() - timedelta(hours=1))

    data = panel_api.panel_today(db=db, user=user)["data"]
    assert data["today_viewers"] == 1


# ==================== F4：管理后台播放统计 ====================

def test_admin_playback_stats_today_uses_shanghai_day(db):
    admin = _user(db, username="r35admin", is_staff=True)
    _session(db, admin, "r35-a1", _shanghai_today_0200_utc())
    _session(db, admin, "r35-a2", _today_start_utc_naive() - timedelta(hours=1))

    stats = admin_api.get_playback_stats(current_admin=admin, db=db)
    assert stats["today"]["plays"] == 1, "上海今天 02:00 的播放应计入今日"
    assert stats["today"]["users"] == 1


# ==================== F5：观影碑榜单 ====================

def test_leaderboard_day_counts_shanghai_boundary_and_reports_shanghai_window(db):
    lib = _library(db, "r35rank")
    user = _user(db, username="r35ranker")
    item = _item(db, lib, "榜单片", added=datetime(2026, 1, 1, 0, 0))
    # 上海今天 02:00 开播：旧口径下日榜漏掉这一播
    _session(db, user, "r35-r1", _shanghai_today_0200_utc(), item_id=item.id)

    payload = portal.get_leaderboard(
        board="hot", period="day", request_user=user, db=db)
    assert payload["items"], "上海今天 02:00 的开播应计入日榜"
    assert payload["items"][0]["count"] == 1
    # 对外仍报上海口径的窗口起点（前端照此画榜单日期），不能变成 UTC 值
    assert payload["window_start"].endswith("T00:00:00")
    from backend.api.panel import _now_local_naive
    assert payload["window_start"][:10] == _now_local_naive().strftime("%Y-%m-%d")


# ==================== F6：追新日历边界 ====================

def test_calendar_puts_early_morning_adds_on_shanghai_day(db):
    lib = _library(db, "r35cal")
    user = _user(db, username="r35cal_u")
    # UTC 昨天 18:00 入库 = 上海今天 02:00：旧口径落在"昨天"格子
    _item(db, lib, "凌晨上架", added=_shanghai_today_0200_utc())

    from backend.api.panel import _now_local_naive
    today_sh = _now_local_naive().strftime("%Y-%m-%d")
    request = SimpleNamespace(
        query_params={"start": today_sh, "end": today_sh},
        base_url="http://portal.test/",
    )
    payload = portal.get_chase_calendar(
        request=request, request_user=user, db=db)
    days = {d["date"]: d["count"] for d in payload["days"]}
    assert days.get(today_sh) == 1, "上海今天 02:00 入库应出现在今天格子"

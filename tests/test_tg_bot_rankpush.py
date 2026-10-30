"""观影榜海报日推（embyboss 借鉴清单第 5 项）测试。

覆盖：
- 聚合口径：数据源 emby_playback_sessions + panel_achievements.playback_seconds
  （账号面板「已观影 X 小时」同源同口径）；窗口复用 portal.leaderboard_window
  （Asia/Shanghai 自然日 / ISO 周），会话按 start_time（UTC 入库帧）落窗，
  跨日会话不进当日榜；show_on_leaderboard=False 不上榜；0 秒不上榜；
  同秒按 user_id 升序稳定排序；Top 10 截断。
- 空榜不推（send_photo 不调用）；未配置交流群不推不发分。
- 海报真出图：文件存在、PIL 可开、宽 900、高度随行数。
- 总开关关：手动 push 返回 disabled、调度 tick 不推。
- 发分开关关（默认）：零发分；开：按 bot_rankpush_reward_table 发分，
  台账 type=rankpush 且带 ref_id；重复推送不重复发（ref_id 幂等）。
- /rankpush：非管理员 / 未绑定拒绝且不推送；管理员可推送。
- 调度：20:30 前不推、到点推一次且同日不重复；周日 21:00 推周榜。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server.portal import leaderboard_window
from backend.integrations import store
from backend.tg_bot import handlers, queries, rankpush, sender

TICKS = 10_000_000
SH = ZoneInfo("Asia/Shanghai")
GROUP_ID = -1001234567890

_item_seq = [0]
_sess_seq = [0]


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for tbl in (
        models.WebUser.__table__,
        models.SystemConfig.__table__,
        models.PointsLog.__table__,
        em.MediaItem.__table__,
        em.PlaybackSession.__table__,
    ):
        tbl.create(engine, checkfirst=True)
    store.invalidate()
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _make_item(db, duration_secs=7200):
    _item_seq[0] += 1
    it = em.MediaItem(
        guid=f"{_item_seq[0]:032x}",
        library_id=1,
        item_type="Movie",
        name=f"片{_item_seq[0]}",
        duration_ticks=duration_secs * TICKS,
    )
    db.add(it)
    db.commit()
    return it


def _make_user(db, username, *, tg_id=None, staff=False, show=True, points=0):
    u = models.WebUser(
        username=username,
        password_hash="",
        points=points,
        telegram_id=tg_id,
        is_staff=staff,
        is_active=True,
        show_on_leaderboard=show,
    )
    db.add(u)
    db.commit()
    return u


def _watch(db, user, item, start_utc, watched_secs):
    _sess_seq[0] += 1
    s = em.PlaybackSession(
        session_key=f"rk-{_sess_seq[0]}",
        user_id=user.id,
        item_id=item.id,
        start_time=start_utc,
        position_ticks=watched_secs * TICKS,
        last_update_at=start_utc + timedelta(seconds=max(watched_secs, 1)),
        ended_at=start_utc + timedelta(seconds=max(watched_secs, 1)),
    )
    db.add(s)
    db.commit()
    return s


def _set_cfg(db, key, value):
    row = db.query(models.SystemConfig).filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()
    store.invalidate()


def _set_group(db):
    _set_cfg(db, "bot_group_ids", str(GROUP_ID))


def _today_start_utc():
    return leaderboard_window("day")[1]


def _today_key():
    return leaderboard_window("day")[0].date().isoformat()


def _stub_send_ok(monkeypatch):
    calls = []

    def fake(db_, chat_id, text=None, **kw):
        calls.append({"chat_id": chat_id, "text": text, "kw": kw})
        return True, None

    monkeypatch.setattr(sender, "send_photo", fake)
    return calls


# ---------------- 聚合口径 ----------------

def test_daily_rank_aggregation_caliber(db):
    item = _make_item(db)
    start = _today_start_utc()
    a = _make_user(db, "alice")
    b = _make_user(db, "bob")
    c = _make_user(db, "carol")
    hidden = _make_user(db, "hidden", show=False)
    zero = _make_user(db, "zero")
    _watch(db, a, item, start + timedelta(hours=1), 3600)
    _watch(db, a, item, start + timedelta(hours=3), 1800)
    _watch(db, b, item, start + timedelta(hours=2), 3600)
    # 昨天 23:00（上海）开播、时长跨进今天：按 start_time 落窗，不进今日榜
    _watch(db, c, item, start - timedelta(hours=1), 7200)
    _watch(db, hidden, item, start + timedelta(hours=1), 7200)
    _watch(db, zero, item, start + timedelta(hours=1), 0)
    rows = queries.watch_duration_rank(db, "day")
    assert [(r["username"], r["seconds"]) for r in rows] == [
        ("alice", 5400),
        ("bob", 3600),
    ]
    assert [r["user_id"] for r in rows] == [a.id, b.id]


def test_rank_tie_break_stable_and_top10(db):
    item = _make_item(db)
    start = _today_start_utc()
    users = [_make_user(db, f"u{i:02d}") for i in range(11)]
    for u in users:
        _watch(db, u, item, start + timedelta(hours=1), 3600)
    rows = queries.watch_duration_rank(db, "day")
    assert len(rows) == 10
    assert [r["user_id"] for r in rows] == sorted(u.id for u in users)[:10]


def test_weekly_rank_window(db):
    item = _make_item(db)
    _, monday_utc = leaderboard_window("week")
    a = _make_user(db, "alice")
    b = _make_user(db, "bob")
    _watch(db, a, item, monday_utc + timedelta(hours=2), 3600)
    # 上周日（本周窗口开始前 1 小时）不计入本周
    _watch(db, b, item, monday_utc - timedelta(hours=1), 7200)
    rows = queries.watch_duration_rank(db, "week")
    assert [(r["username"], r["seconds"]) for r in rows] == [("alice", 3600)]


def test_position_capped_by_duration(db):
    """position 口径：单会话秒数不超过片长（与账号面板同一函数）。"""
    item = _make_item(db, duration_secs=600)
    start = _today_start_utc()
    a = _make_user(db, "alice")
    _watch(db, a, item, start + timedelta(hours=1), 3600)
    rows = queries.watch_duration_rank(db, "day")
    assert rows[0]["seconds"] == 600


# ---------------- 海报 ----------------

def test_poster_render_real_image(db, tmp_path):
    from PIL import Image

    from backend.tg_bot import rank_poster

    rows = [
        {"user_id": 1, "username": "alice", "seconds": 5400},
        {"user_id": 2, "username": "bob", "seconds": 3600},
        {"user_id": 3, "username": "carol", "seconds": 60},
    ]
    out = tmp_path / "poster.png"
    path = rank_poster.render_rank_poster(
        "day", rows, leaderboard_window("day")[0], out_path=out
    )
    assert path.exists() and path.stat().st_size > 5000
    with Image.open(path) as im:
        assert im.format == "PNG"
        assert im.size[0] == rank_poster.POSTER_WIDTH == 900
        assert im.size[1] == rank_poster.poster_height(3)
    # 行数变多海报变高
    out2 = tmp_path / "poster2.png"
    rank_poster.render_rank_poster(
        "week", rows * 3, leaderboard_window("week")[0], out_path=out2
    )
    with Image.open(out2) as im2:
        assert im2.size[1] == rank_poster.poster_height(9)


# ---------------- 推送与开关 ----------------

def test_empty_board_no_push(db, monkeypatch):
    _set_group(db)
    calls = _stub_send_ok(monkeypatch)
    res = rankpush.push_rank_board(db, "day")
    assert res["pushed"] is False and res["reason"] == "empty"
    assert calls == []


def test_no_group_no_push_no_reward(db, monkeypatch):
    _set_cfg(db, "bot_rankpush_reward_enabled", "true")
    item = _make_item(db)
    a = _make_user(db, "alice")
    _watch(db, a, item, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send_ok(monkeypatch)
    res = rankpush.push_rank_board(db, "day")
    assert res["pushed"] is False and res["reason"] == "no_group"
    assert calls == []
    assert db.query(models.PointsLog).count() == 0


def test_enabled_off_blocks_push_and_tick(db, monkeypatch):
    _set_group(db)
    _set_cfg(db, "bot_rankpush_enabled", "false")
    item = _make_item(db)
    a = _make_user(db, "alice")
    _watch(db, a, item, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send_ok(monkeypatch)
    res = rankpush.push_rank_board(db, "day")
    assert res["pushed"] is False and res["reason"] == "disabled"
    now_sh = datetime.now(SH).replace(hour=20, minute=35, second=0, microsecond=0)
    assert rankpush.run_scheduled_tick(db, now_sh=now_sh) == []
    assert calls == []


# ---------------- 发分通道 ----------------

def test_reward_off_by_default_no_grants(db, monkeypatch):
    _set_group(db)
    item = _make_item(db)
    a = _make_user(db, "alice")
    _watch(db, a, item, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send_ok(monkeypatch)
    res = rankpush.push_rank_board(db, "day")
    assert res["pushed"] is True and len(calls) == 1
    assert calls[0]["chat_id"] == GROUP_ID
    assert db.query(models.PointsLog).count() == 0
    assert db.query(models.WebUser).filter_by(id=a.id).first().points == 0


def test_reward_on_grants_by_table_and_idempotent(db, monkeypatch):
    _set_group(db)
    _set_cfg(db, "bot_rankpush_reward_enabled", "true")
    item = _make_item(db)
    start = _today_start_utc()
    a = _make_user(db, "alice")
    b = _make_user(db, "bob")
    c = _make_user(db, "carol")
    _watch(db, a, item, start + timedelta(hours=1), 3 * 3600)
    _watch(db, b, item, start + timedelta(hours=1), 2 * 3600)
    _watch(db, c, item, start + timedelta(hours=1), 3600)
    _stub_send_ok(monkeypatch)
    res = rankpush.push_rank_board(db, "day")
    assert res["pushed"] is True
    got = {
        u.username: u.points
        for u in db.query(models.WebUser).all()
    }
    assert got == {"alice": 10, "bob": 8, "carol": 6}
    logs = db.query(models.PointsLog).filter_by(type="rankpush").all()
    assert len(logs) == 3
    assert {lg.ref_id for lg in logs} == {
        f"rankpush:day:{_today_key()}:1",
        f"rankpush:day:{_today_key()}:2",
        f"rankpush:day:{_today_key()}:3",
    }
    assert {lg.amount for lg in logs} == {10, 8, 6}
    # 幂等：再推一次不重复发分
    res2 = rankpush.push_rank_board(db, "day")
    assert res2["pushed"] is True
    assert db.query(models.PointsLog).filter_by(type="rankpush").count() == 3
    got2 = {u.username: u.points for u in db.query(models.WebUser).all()}
    assert got2 == got


def test_reward_custom_table(db, monkeypatch):
    _set_group(db)
    _set_cfg(db, "bot_rankpush_reward_enabled", "true")
    _set_cfg(db, "bot_rankpush_reward_table", "100,50")
    item = _make_item(db)
    start = _today_start_utc()
    a = _make_user(db, "alice")
    b = _make_user(db, "bob")
    c = _make_user(db, "carol")
    _watch(db, a, item, start + timedelta(hours=1), 3 * 3600)
    _watch(db, b, item, start + timedelta(hours=1), 2 * 3600)
    _watch(db, c, item, start + timedelta(hours=1), 3600)
    _stub_send_ok(monkeypatch)
    rankpush.push_rank_board(db, "day")
    got = {u.username: u.points for u in db.query(models.WebUser).all()}
    # 表只有两档：第 3 名不在表内，不发
    assert got == {"alice": 100, "bob": 50, "carol": 0}


# ---------------- /rankpush 管理员命令 ----------------

def test_rankpush_handler_rejects_non_admin(db, monkeypatch):
    _set_group(db)
    item = _make_item(db)
    plain = _make_user(db, "plain", tg_id=555001)
    _watch(db, plain, item, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send_ok(monkeypatch)
    text = handlers.handle_rankpush(db, {"id": 555001}, 555001, "")
    assert "仅管理员" in text
    # 未绑定 TG 同样拒绝
    text2 = handlers.handle_rankpush(db, {"id": 555999}, 555999, "")
    assert "仅管理员" in text2
    assert calls == []


def test_rankpush_handler_admin_pushes(db, monkeypatch):
    _set_group(db)
    item = _make_item(db)
    admin = _make_user(db, "boss", tg_id=555002, staff=True)
    _watch(db, admin, item, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send_ok(monkeypatch)
    text = handlers.handle_rankpush(db, {"id": 555002}, 555002, "")
    assert "已推送" in text
    assert len(calls) == 1 and calls[0]["chat_id"] == GROUP_ID


# ---------------- 调度 ----------------

def test_tick_daily_fires_once_at_2030(db, monkeypatch):
    _set_group(db)
    item = _make_item(db)
    a = _make_user(db, "alice")
    _watch(db, a, item, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send_ok(monkeypatch)
    today = datetime.now(SH).date()
    before = datetime(today.year, today.month, today.day, 20, 29, tzinfo=SH)
    assert rankpush.run_scheduled_tick(db, now_sh=before) == []
    assert calls == []
    at = datetime(today.year, today.month, today.day, 20, 30, tzinfo=SH)
    actions = rankpush.run_scheduled_tick(db, now_sh=at)
    assert actions == ["day:pushed"]
    assert len(calls) == 1
    later = datetime(today.year, today.month, today.day, 20, 31, tzinfo=SH)
    assert rankpush.run_scheduled_tick(db, now_sh=later) == []
    assert len(calls) == 1


def test_tick_weekly_fires_sunday_2100(db, monkeypatch):
    _set_group(db)
    item = _make_item(db)
    a = _make_user(db, "alice")
    _, monday_utc = leaderboard_window("week")
    _watch(db, a, item, monday_utc + timedelta(hours=2), 3600)
    calls = _stub_send_ok(monkeypatch)
    today = datetime.now(SH).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    at = datetime(sunday.year, sunday.month, sunday.day, 21, 0, tzinfo=SH)
    actions = rankpush.run_scheduled_tick(db, now_sh=at)
    assert "week:pushed" in actions
    captions = [c["text"] or "" for c in calls]
    assert any("本周观影榜" in t for t in captions)
    # 同一周不再重复推周榜
    actions2 = rankpush.run_scheduled_tick(db, now_sh=at + timedelta(minutes=5))
    assert "week:pushed" not in actions2

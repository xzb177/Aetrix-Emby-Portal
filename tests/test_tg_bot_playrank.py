"""每日播放排行榜（文字三榜）测试。

规格（站主拿竞品机器人 21:30 榜单截图亲点补做，后按竞品「月饼
Emby」实际发布追加图文合一＋置顶）：
- 每天 21:30（Asia/Shanghai，可配 bot_playrank_time）由站内机器人把
  当日（00:00 至发送时刻）三榜发到正式群（bot_group_ids 第一个群）：
  海报图（电影/剧集各前 5）＋文字榜 caption 合一一条、发出后置顶；
  文字超 caption 上限时图、文两条；海报关/失败降级纯文字。
  文字榜两行式，正文顺序 🎬 电影 / 📺 剧集 / 👑 观看时长榜，
  各 Top 5（可配 bot_playrank_top_n），按总时长降序、同长按播放
  次数降序。三榜总开关默认关，各榜可单独关、海报可单独关。
  当日无播放不发。

数据口径（不新造采集，与站内既有榜单同源）：
- 表 emby_playback_sessions（窗口按 start_time 落入上海自然日，
  比较帧走 portal.leaderboard_window 的 UTC 换算）；
- 时长 = panel_achievements.playback_seconds（末次上报进度近似，
  min(position, duration)），与账号面板/观影榜日推同口径；
- 播放次数 = 窗口内会话行数（与站内热播榜 hot_leaderboard_items
  同口径）；集按 series_id 汇总到剧，聚合实体若已折叠跟随
  merged_into_id 到最终存活正主；被播条目已软删/已合并的不计；
- 用户榜复用 tg_bot.queries.watch_duration_rank（隐藏榜单者不上榜）。

覆盖：集汇总到剧、窗口边界（00:00 前不算）、时长排序＋次数
tie-break、折叠剧跟随正主、总开关关/默认关不发、空数据不发、
分榜开关、Top N 截断、调度到点只发一次/失败重试、/playrank
管理员门与只读角色门、命令三处接线。
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
from backend.tg_bot import ephemeral, handlers, playrank, poller, router, sender

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


def _make_item(db, item_type, name, duration_secs=3600, **kw):
    _item_seq[0] += 1
    it = em.MediaItem(
        guid=f"{_item_seq[0]:032x}",
        library_id=1,
        item_type=item_type,
        name=name,
        duration_ticks=duration_secs * TICKS,
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _make_user(db, username, *, tg_id=None, staff=False, role=None, show=True):
    u = models.WebUser(
        username=username,
        password_hash="",
        points=0,
        telegram_id=tg_id,
        is_staff=staff,
        # 生产 staff 必有角色；推榜是写操作，测试管理员默认 operator。
        admin_role=role if role is not None else ("operator" if staff else None),
        is_active=True,
        show_on_leaderboard=show,
    )
    db.add(u)
    db.commit()
    return u


def _watch(db, user, item, start_utc, watched_secs):
    _sess_seq[0] += 1
    s = em.PlaybackSession(
        session_key=f"pr-{_sess_seq[0]}",
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


def _enable(db):
    _set_cfg(db, "bot_group_ids", str(GROUP_ID))
    _set_cfg(db, "bot_playrank_enabled", "true")


def _today_start_utc():
    return leaderboard_window("day")[1]


def _stub_send(monkeypatch, ok=True):
    calls = []

    def fake(db_, chat_id, text, reply_markup=None, **kw):
        calls.append({"chat_id": chat_id, "text": text, "kw": kw})
        return ok, None if ok else "boom"

    def fake_ex(db_, chat_id, text, reply_markup=None, **kw):
        calls.append({"chat_id": chat_id, "text": text, "kw": kw})
        return ok, None if ok else "boom", 4242 if ok else None

    monkeypatch.setattr(sender, "send_message", fake)
    monkeypatch.setattr(sender, "send_message_ex", fake_ex)
    # 海报默认开：本文件的用例只验文字路径与调度，海报发送一律
    # 打桩失败、走纯文字降级；置顶打桩成功（置顶另在海报测试验）。
    monkeypatch.setattr(sender, "send_photo", lambda *a, **kw: (False, "stubbed"))
    monkeypatch.setattr(
        sender, "send_photo_ex", lambda *a, **kw: (False, "stubbed", None)
    )
    monkeypatch.setattr(sender, "pin_message", lambda *a, **kw: (True, None))
    return calls


def _at(hour, minute):
    today = datetime.now(SH).date()
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=SH)


# ---------------- 条目聚合口径 ----------------

def test_item_rank_aggregates_episodes_into_series(db):
    start = _today_start_utc()
    series = _make_item(db, "series", "剧甲", 0)
    ep1 = _make_item(db, "episode", "剧甲 E1", 3600, series_id=series.id)
    ep2 = _make_item(db, "episode", "剧甲 E2", 3600, series_id=series.id)
    movie = _make_item(db, "movie", "片甲", 7200)
    a = _make_user(db, "alice")
    b = _make_user(db, "bob")
    _watch(db, a, ep1, start + timedelta(hours=1), 1800)
    _watch(db, b, ep2, start + timedelta(hours=2), 1800)
    _watch(db, a, ep2, start + timedelta(hours=3), 600)
    _watch(db, a, movie, start + timedelta(hours=4), 3600)
    boards = playrank.item_play_rank(db, start, 5)
    assert [(r["name"], r["plays"], r["seconds"]) for r in boards["series"]] == [
        ("剧甲", 3, 4200)
    ]
    assert [(r["name"], r["plays"], r["seconds"]) for r in boards["movies"]] == [
        ("片甲", 1, 3600)
    ]


def test_item_rank_sorted_by_seconds_then_plays(db):
    start = _today_start_utc()
    u = _make_user(db, "alice")

    def series_with(name, plays, secs_each):
        s = _make_item(db, "series", name, 0)
        ep = _make_item(db, "episode", f"{name} E1", 7200, series_id=s.id)
        for i in range(plays):
            _watch(db, u, ep, start + timedelta(hours=1, minutes=i), secs_each)
        return s

    series_with("剧A", 1, 3600)   # 总时长 3600、1 次
    series_with("剧B", 3, 1200)   # 总时长 3600、3 次 → 同长按次数在前
    series_with("剧C", 1, 7200)   # 总时长最长 → 第一
    boards = playrank.item_play_rank(db, start, 5)
    assert [r["name"] for r in boards["series"]] == ["剧C", "剧B", "剧A"]


def test_item_rank_window_boundary(db):
    start = _today_start_utc()
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    # 00:00（上海）前 1 分钟开播：不算今天
    _watch(db, u, movie, start - timedelta(minutes=1), 7200)
    # 恰在窗口起点开播：算
    _watch(db, u, movie, start, 600)
    boards = playrank.item_play_rank(db, start, 5)
    assert [(r["name"], r["plays"], r["seconds"]) for r in boards["movies"]] == [
        ("片甲", 1, 600)
    ]


def test_item_rank_follows_merged_series_to_primary(db):
    start = _today_start_utc()
    primary = _make_item(db, "series", "正主", 0)
    folded = _make_item(db, "series", "译名", 0, merged_into_id=primary.id)
    ep = _make_item(db, "episode", "译名 E1", 3600, series_id=folded.id)
    u = _make_user(db, "alice")
    _watch(db, u, ep, start + timedelta(hours=1), 1200)
    boards = playrank.item_play_rank(db, start, 5)
    assert [(r["name"], r["plays"], r["seconds"]) for r in boards["series"]] == [
        ("正主", 1, 1200)
    ]


def test_item_rank_skips_deleted_and_merged_played_items(db):
    start = _today_start_utc()
    series = _make_item(db, "series", "剧甲", 0)
    live_ep = _make_item(db, "episode", "剧甲 E1", 3600, series_id=series.id)
    merged_ep = _make_item(
        db, "episode", "剧甲 E1 副本", 3600,
        series_id=series.id, merged_into_id=live_ep.id,
    )
    gone_movie = _make_item(db, "movie", "下架片", 7200)
    gone_id = gone_movie.id
    # 软删走 bulk update 打标记：软删行会被全局 ORM 钩子过滤，commit
    # 后再读实例属性会脱钩，所以先把 id 摘出来、会话按 id 直造。
    db.query(em.MediaItem).filter_by(id=gone_id).update(
        {"deleted_at": start + timedelta(hours=1)}
    )
    db.commit()
    u = _make_user(db, "alice")
    _watch(db, u, merged_ep, start + timedelta(hours=1), 3600)
    _sess_seq[0] += 1
    db.add(em.PlaybackSession(
        session_key=f"pr-{_sess_seq[0]}",
        user_id=u.id,
        item_id=gone_id,
        start_time=start + timedelta(hours=1),
        position_ticks=3600 * TICKS,
        last_update_at=start + timedelta(hours=2),
        ended_at=start + timedelta(hours=2),
    ))
    db.commit()
    boards = playrank.item_play_rank(db, start, 5)
    assert boards["series"] == []
    assert boards["movies"] == []


def test_item_rank_top_n_limit(db):
    start = _today_start_utc()
    u = _make_user(db, "alice")
    for i in range(3):
        m = _make_item(db, "movie", f"片{i}", 7200)
        _watch(db, u, m, start + timedelta(hours=1), (3 - i) * 600)
    boards = playrank.item_play_rank(db, start, 2)
    assert [r["name"] for r in boards["movies"]] == ["片0", "片1"]


# ---------------- 时长格式 ----------------

def test_fmt_duration():
    assert playrank.fmt_duration(0) == "0 分钟"
    assert playrank.fmt_duration(1500) == "25 分钟"
    assert playrank.fmt_duration(3600) == "1 小时 0 分钟"
    assert playrank.fmt_duration(5260) == "1 小时 27 分钟"


# ---------------- 推送与开关 ----------------

def test_push_disabled_by_default(db, monkeypatch):
    _set_cfg(db, "bot_group_ids", str(GROUP_ID))
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is False and res["reason"] == "disabled"
    assert calls == []
    # 调度同样不动
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 30)) == []
    assert calls == []


def test_push_empty_no_send(db, monkeypatch):
    _enable(db)
    calls = _stub_send(monkeypatch)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is False and res["reason"] == "empty"
    assert calls == []


def test_push_no_group(db, monkeypatch):
    _set_cfg(db, "bot_playrank_enabled", "true")
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is False and res["reason"] == "no_group"
    assert calls == []


def test_push_sends_formatted_text(db, monkeypatch):
    _enable(db)
    start = _today_start_utc()
    series = _make_item(db, "series", "剧甲", 0)
    ep1 = _make_item(db, "episode", "剧甲 E1", 3600, series_id=series.id)
    ep2 = _make_item(db, "episode", "剧甲 E2", 3600, series_id=series.id)
    movie = _make_item(db, "movie", "片甲", 7200)
    alice = _make_user(db, "alice")
    bob = _make_user(db, "bob")
    hidden = _make_user(db, "hidden", show=False)
    _watch(db, alice, ep1, start + timedelta(hours=1), 1800)
    _watch(db, bob, ep2, start + timedelta(hours=2), 1800)
    _watch(db, alice, ep2, start + timedelta(hours=3), 600)
    _watch(db, alice, movie, start + timedelta(hours=4), 3600)
    _watch(db, hidden, movie, start + timedelta(hours=5), 7200)
    calls = _stub_send(monkeypatch)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True and res["reason"] == "ok"
    assert len(calls) == 1
    assert calls[0]["chat_id"] == GROUP_ID
    # 纯文字口径（与 updatedigest 一致，不走 HTML 解析免转义事故）
    assert calls[0]["kw"].get("parse_mode") is None
    text = calls[0]["text"]
    assert text.startswith("📺 今日热播榜\n──────────")
    # 正文顺序：电影 → 剧集 → 用户榜（与海报上下呼应）
    assert text.index("🎬 电影") < text.index("📺 剧集") < text.index("👑 观看时长榜")
    # 用户榜：隐藏榜单者不出现；alice 总时长 1800+600+3600=6000 秒
    assert "1. alice\n  观看 1 小时 40 分钟" in text
    assert "hidden" not in text
    # 剧集榜：集汇总到剧
    assert "📺 剧集" in text
    assert "1. 剧甲\n  播放 3 次 · 时长 1 小时 10 分钟" in text
    # 电影榜：hidden 的会话计入条目榜（条目榜不按用户过滤）
    assert "🎬 电影" in text
    assert "1. 片甲\n  播放 2 次 · 时长 3 小时 0 分钟" in text


def test_push_sub_board_switches(db, monkeypatch):
    _enable(db)
    _set_cfg(db, "bot_playrank_series_enabled", "false")
    _set_cfg(db, "bot_playrank_users_enabled", "false")
    start = _today_start_utc()
    series = _make_item(db, "series", "剧甲", 0)
    ep = _make_item(db, "episode", "剧甲 E1", 3600, series_id=series.id)
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, ep, start + timedelta(hours=1), 1800)
    _watch(db, u, movie, start + timedelta(hours=2), 3600)
    calls = _stub_send(monkeypatch)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    text = calls[0]["text"]
    assert "📺 剧集" not in text
    assert "观看时长榜" not in text
    assert "🎬 电影" in text


def test_push_all_sub_boards_off_is_empty(db, monkeypatch):
    _enable(db)
    for key in (
        "bot_playrank_users_enabled",
        "bot_playrank_series_enabled",
        "bot_playrank_movies_enabled",
    ):
        _set_cfg(db, key, "false")
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is False and res["reason"] == "empty"
    assert calls == []


def test_push_top_n_from_config(db, monkeypatch):
    _enable(db)
    _set_cfg(db, "bot_playrank_top_n", "2")
    start = _today_start_utc()
    u = _make_user(db, "alice")
    for i in range(3):
        m = _make_item(db, "movie", f"片{i}", 7200)
        _watch(db, u, m, start + timedelta(hours=1), (3 - i) * 600)
    calls = _stub_send(monkeypatch)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    assert [r["name"] for r in res["movies"]] == ["片0", "片1"]
    assert "片2" not in calls[0]["text"]


# ---------------- 调度 ----------------

def test_tick_fires_once_at_default_time(db, monkeypatch):
    _enable(db)
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 29)) == []
    assert calls == []
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 30)) == ["day:pushed"]
    assert len(calls) == 1
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 31)) == []
    assert len(calls) == 1


def test_tick_custom_time(db, monkeypatch):
    _enable(db)
    _set_cfg(db, "bot_playrank_time", "08:05")
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    assert playrank.run_scheduled_tick(db, now_sh=_at(8, 4)) == []
    assert playrank.run_scheduled_tick(db, now_sh=_at(8, 5)) == ["day:pushed"]
    assert len(calls) == 1


def test_tick_invalid_time_falls_back_to_2130(db):
    assert playrank.send_clock(db) == (21, 30)
    _set_cfg(db, "bot_playrank_time", "25:99")
    assert playrank.send_clock(db) == (21, 30)
    _set_cfg(db, "bot_playrank_time", "9:05")
    assert playrank.send_clock(db) == (9, 5)


def test_tick_send_failure_retries_next_tick(db, monkeypatch):
    _enable(db)
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch, ok=False)
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 30)) == ["day:send_failed"]
    assert len(calls) == 1
    # 失败不写标记：下一拍重试
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 31)) == ["day:send_failed"]
    assert len(calls) == 2


def test_tick_empty_writes_marker(db, monkeypatch):
    _enable(db)
    calls = _stub_send(monkeypatch)
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 30)) == ["day:empty"]
    assert calls == []
    # 空榜也写标记：同一晚不再反复尝试
    assert playrank.run_scheduled_tick(db, now_sh=_at(21, 45)) == []


# ---------------- /playrank 管理员命令 ----------------

def test_handler_rejects_non_admin(db, monkeypatch):
    _enable(db)
    plain = _make_user(db, "plain", tg_id=555001)
    movie = _make_item(db, "movie", "片甲", 7200)
    _watch(db, plain, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    text = handlers.handle_playrank(db, {"id": 555001}, 555001, "")
    assert "仅管理员" in text
    text2 = handlers.handle_playrank(db, {"id": 555999}, 555999, "")
    assert "仅管理员" in text2
    assert calls == []


def test_handler_rejects_viewer(db, monkeypatch):
    _enable(db)
    _make_user(db, "audit", tg_id=555003, staff=True, role="viewer")
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    text = handlers.handle_playrank(db, {"id": 555003}, 555003, "")
    assert "只读" in text
    assert calls == []


def test_handler_admin_pushes(db, monkeypatch):
    _enable(db)
    _make_user(db, "boss", tg_id=555002, staff=True)
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    calls = _stub_send(monkeypatch)
    text = handlers.handle_playrank(db, {"id": 555002}, 555002, "")
    assert "已推送" in text
    assert len(calls) == 1 and calls[0]["chat_id"] == GROUP_ID


# ---------------- 命令接线 ----------------

@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None),
    )
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None, 9001),
    )
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    router._user_cmd_limits.clear()
    router._group_windows.clear()
    return out


def _msg_update(tg_id, chat_id, text, chat_type="private", **msg_kw):
    msg = {
        "message_id": 10,
        "text": text,
        "from": {"id": tg_id, "first_name": "T"},
        "chat": {"id": chat_id, "type": chat_type},
    }
    msg.update(msg_kw)
    return {"update_id": 1, "message": msg}


def test_playrank_registered_in_bot_commands():
    cmds = dict(poller.BOT_COMMANDS)
    assert cmds.get("playrank") == "[私聊] 每日播放排行榜推送（管理员）"


def test_playrank_dispatch_reaches_handler(db, sent, monkeypatch):
    _enable(db)
    _make_user(db, "boss", tg_id=555100, staff=True)
    calls = []

    def fake_handler(db_, tg_user, chat_id, args):
        calls.append((tg_user.get("id"), chat_id, args))
        return "PLAYRANK-OK"

    monkeypatch.setattr(handlers, "handle_playrank", fake_handler)
    router.dispatch(db, _msg_update(555100, 555100, "/playrank"))
    assert calls == [(555100, 555100, "")]
    assert any("PLAYRANK-OK" in t for t in sent)


def test_playrank_group_blocked_private_only(db, sent, monkeypatch):
    _enable(db)
    _make_user(db, "boss", tg_id=555100, staff=True)
    calls = []
    monkeypatch.setattr(
        handlers, "handle_playrank",
        lambda *a, **kw: calls.append(a) or "X",
    )
    router.dispatch(db, _msg_update(555100, GROUP_ID, "/playrank", "group"))
    assert calls == []
    assert any("请私聊" in t for t in sent)

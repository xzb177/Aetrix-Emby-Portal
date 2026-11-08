"""每日播放排行榜海报图测试（tg_bot.playrank_poster ＋图文推送）。

规格（站主照竞品「月饼 Emby」实际发布追加）：
- 播报时文字榜之外再配一张海报图：电影前 5＋剧集前 5（固定 5，
  与 bot_playrank_top_n 无关），深色底红色点缀，名次/标题与文字
  榜同一份聚合；条目没海报用带片名的占位块顶上，不缺格。
- 图文合一：文字榜全文作 sendPhoto 的 caption 一条发；超
  Telegram caption 上限（1024 字符）才退回图（无 caption）＋
  文字两条。海报开关 bot_playrank_poster_enabled 默认开；关了
  只发文字。海报生成/发送失败只降级纯文字，不堵播报。
- 发完置顶（pinChatMessage），置顶失败不影响播报。
- 仅用户榜有数据时不生成海报、只发文字。

覆盖：海报数据组装（取前 5、本地图优先、远程图走 localize、
缺图占位）、渲染成图（尺寸稳定、单区、两区皆空报错）、图文
合一与超限拆分、海报关/失败/异常降级、置顶与置顶失败、仅
用户榜不发图。
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
from backend.emby_server import image_store, models as em
from backend.emby_server.portal import leaderboard_window
from backend.integrations import store
from backend.tg_bot import playrank, playrank_poster, sender

TICKS = 10_000_000
SH = ZoneInfo("Asia/Shanghai")
GROUP_ID = -1001234567890

_item_seq = [1000]
_sess_seq = [1000]


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


def _make_user(db, username):
    u = models.WebUser(
        username=username,
        password_hash="",
        points=0,
        is_active=True,
        show_on_leaderboard=True,
    )
    db.add(u)
    db.commit()
    return u


def _watch(db, user, item, start_utc, watched_secs):
    _sess_seq[0] += 1
    s = em.PlaybackSession(
        session_key=f"prp-{_sess_seq[0]}",
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


def _poster_img(tmp_path, name="poster.jpg", color=(120, 40, 40)):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (300, 450), color).save(path, format="JPEG")
    return str(path)


@pytest.fixture()
def events(monkeypatch):
    """记录发送通道的调用顺序：photo / message / pin。"""
    log = []

    def photo_ex(db_, chat_id, caption, reply_markup=None, photo_path=None, **kw):
        log.append(
            {"kind": "photo", "chat_id": chat_id, "caption": caption, "kw": kw}
        )
        return True, None, 777

    def message_ex(db_, chat_id, text, reply_markup=None, **kw):
        log.append({"kind": "message", "chat_id": chat_id, "text": text, "kw": kw})
        return True, None, 888

    def pin(db_, chat_id, message_id):
        log.append({"kind": "pin", "chat_id": chat_id, "message_id": message_id})
        return True, None

    monkeypatch.setattr(sender, "send_photo_ex", photo_ex)
    monkeypatch.setattr(sender, "send_message_ex", message_ex)
    monkeypatch.setattr(sender, "pin_message", pin)
    return log


def _date_sh():
    return datetime.now(SH)


# ---------------- 海报数据组装 ----------------

def test_build_sections_top5_local_poster_and_placeholder(db, tmp_path):
    poster = _poster_img(tmp_path)
    rows = []
    for i in range(6):
        kw = {"poster_path": poster} if i == 0 else {}
        it = _make_item(db, "movie", f"片{i}", **kw)
        rows.append(
            {"item_id": it.id, "name": f"片{i}", "plays": 1, "seconds": 100}
        )
    sections = playrank_poster.build_sections(db, rows, [])
    assert len(sections["movies"]) == 5  # 固定前 5，第 6 条不进海报
    assert [c["name"] for c in sections["movies"]] == [f"片{i}" for i in range(5)]
    assert sections["movies"][0]["poster"] == poster  # 本地文件直接用
    assert sections["movies"][1]["poster"] is None  # 没海报 → 占位
    assert sections["series"] == []


def test_build_sections_remote_url_goes_through_localize(db, tmp_path, monkeypatch):
    cached = _poster_img(tmp_path, "cached.jpg")
    seen = []

    def fake_localize(url):
        seen.append(url)
        return cached

    monkeypatch.setattr(image_store, "localize", fake_localize)
    it = _make_item(
        db, "series", "远程剧", primary_image_url="https://img.example/p.jpg"
    )
    sections = playrank_poster.build_sections(
        db, [], [{"item_id": it.id, "name": "远程剧", "plays": 2, "seconds": 5}]
    )
    assert seen == ["https://img.example/p.jpg"]
    assert sections["series"][0]["poster"] == cached


def test_build_sections_missing_item_keeps_name_placeholder(db):
    sections = playrank_poster.build_sections(
        db, [{"item_id": 999999, "name": "查无此行", "plays": 1, "seconds": 1}], []
    )
    assert sections["movies"] == [{"name": "查无此行", "poster": None}]


# ---------------- 渲染 ----------------

def test_render_image_stable_size_and_bytes(tmp_path):
    movies = [
        {"name": f"电影{i}", "poster": None} for i in range(5)
    ]
    series = [{"name": f"剧集{i}", "poster": None} for i in range(5)]
    out = tmp_path / "rank.png"
    path = playrank_poster.render_playrank_poster(
        _date_sh(), movies, series, out_path=out
    )
    data = path.read_bytes()
    assert len(data) > 10_000
    from PIL import Image

    with Image.open(path) as im:
        assert im.size == (1080, playrank_poster.canvas_height(2))
        assert im.mode == "RGB"
    # 同输入再渲一张：尺寸稳定
    out2 = tmp_path / "rank2.png"
    playrank_poster.render_playrank_poster(_date_sh(), movies, series, out_path=out2)
    with Image.open(out2) as im2:
        assert im2.size == (1080, playrank_poster.canvas_height(2))


def test_render_single_section_with_real_poster(tmp_path):
    poster = _poster_img(tmp_path)
    movies = [{"name": "有图电影", "poster": poster}]
    out = tmp_path / "one.png"
    path = playrank_poster.render_playrank_poster(
        _date_sh(), movies, [], out_path=out
    )
    from PIL import Image

    with Image.open(path) as im:
        assert im.size == (1080, playrank_poster.canvas_height(1))


def test_render_no_sections_raises(tmp_path):
    with pytest.raises(ValueError):
        playrank_poster.render_playrank_poster(_date_sh(), [], [])


def test_send_rank_photo_empty_sections_no_send(db, events):
    ok, mid = playrank_poster.send_rank_photo(db, GROUP_ID, _date_sh(), [], [])
    assert ok is False and mid is None
    assert events == []


# ---------------- 文字格式 ----------------

def test_render_message_two_line_format_and_order():
    users = [{"username": "alice", "seconds": 6000}]
    series = [{"name": "剧甲", "plays": 3, "seconds": 4200}]
    movies = [{"name": "片甲", "plays": 2, "seconds": 10800}]
    text = playrank.render_message(_date_sh(), users, series, movies)
    lines = text.split("\n")
    assert lines[0] == "📺 今日热播榜"
    assert lines[1] == "──────────"
    # 正文顺序：电影 → 剧集 → 用户榜
    assert text.index("🎬 电影") < text.index("📺 剧集") < text.index("👑 观看时长榜")
    assert "1. 片甲\n  播放 2 次 · 时长 3 小时 0 分钟" in text
    assert "1. 剧甲\n  播放 3 次 · 时长 1 小时 10 分钟" in text
    # 用户榜第二行只有观看时长，没有播放次数（无此数据、不编）
    assert "1. alice\n  观看 1 小时 40 分钟" in text


# ---------------- 图文推送 ----------------

def test_push_combined_photo_with_caption_then_pin(db, events):
    _enable(db)
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    kinds = [e["kind"] for e in events]
    assert kinds == ["photo", "pin"]  # 图文一条发完就置顶，不再单发文字
    caption = events[0]["caption"]
    assert caption.startswith("📺 今日热播榜")
    assert "1. 片甲\n  播放 1 次 · 时长 1 小时 0 分钟" in caption
    assert events[0]["chat_id"] == GROUP_ID
    assert events[1] == {"kind": "pin", "chat_id": GROUP_ID, "message_id": 777}


def test_push_caption_overflow_splits_photo_then_text(db, events):
    _enable(db)
    _set_cfg(db, "bot_playrank_top_n", "20")
    start = _today_start_utc()
    u = _make_user(db, "alice")
    for i in range(20):
        m = _make_item(db, "movie", f"超长片名测试电影第{i:02d}号加长版")
        _watch(db, u, m, start + timedelta(hours=1), 600)
        s = _make_item(db, "series", f"超长剧名测试剧集第{i:02d}号加长版", 0)
        ep = _make_item(
            db, "episode", f"超长剧名 E{i}", 3600, series_id=s.id
        )
        _watch(db, u, ep, start + timedelta(hours=2), 600)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    kinds = [e["kind"] for e in events]
    assert kinds == ["photo", "message", "pin"]
    assert events[0]["caption"] == ""  # 超限：图单独发、无 caption
    assert len(events[1]["text"]) > 1024  # 文字全文紧随其后
    # 置顶承载文字的那条
    assert events[2]["message_id"] == 888


def test_push_poster_switch_off_text_only(db, events):
    _enable(db)
    _set_cfg(db, "bot_playrank_poster_enabled", "false")
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    kinds = [e["kind"] for e in events]
    assert kinds == ["message", "pin"]
    assert events[1]["message_id"] == 888


def test_push_photo_failure_falls_back_to_text(db, events, monkeypatch):
    _enable(db)
    monkeypatch.setattr(
        sender, "send_photo_ex", lambda *a, **kw: (False, "boom", None)
    )
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    kinds = [e["kind"] for e in events]
    assert kinds == ["message", "pin"]  # 图失败 → 纯文字一条并置顶


def test_push_photo_exception_falls_back_to_text(db, events, monkeypatch):
    _enable(db)

    def boom(*a, **kw):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(playrank_poster, "send_rank_photo", boom)
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    assert [e["kind"] for e in events] == ["message", "pin"]


def test_push_users_only_no_poster(db, events):
    _enable(db)
    _set_cfg(db, "bot_playrank_series_enabled", "false")
    _set_cfg(db, "bot_playrank_movies_enabled", "false")
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True
    # 只有用户榜有数据：不生成海报，只发文字
    assert [e["kind"] for e in events] == ["message", "pin"]
    assert "👑 观看时长榜" in events[0]["text"]


def test_push_pin_failure_keeps_push_ok(db, events, monkeypatch):
    _enable(db)

    def bad_pin(db_, chat_id, message_id):
        raise RuntimeError("no pin rights")

    monkeypatch.setattr(sender, "pin_message", bad_pin)
    movie = _make_item(db, "movie", "片甲", 7200)
    u = _make_user(db, "alice")
    _watch(db, u, movie, _today_start_utc() + timedelta(hours=1), 3600)
    res = playrank.push_daily_rank(db)
    assert res["pushed"] is True and res["reason"] == "ok"
    assert [e["kind"] for e in events] == ["photo"]

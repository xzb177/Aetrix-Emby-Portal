"""新片入库通知（富媒体版）：配置 / 新增查询 / HTML 消息 / 海报 / 按钮 / 失败隔离"""
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, "/opt/aetrix-portal")

from backend import models as base_models
from backend.emby_server import models as emby_models
from backend.emby_server import new_media_notify as nmn
from backend.integrations.store import invalidate  # 热缓存失效（测试直接写库需手动清）


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    emby_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_config(db, key, value):
    db.add(base_models.SystemConfig(key=key, value=value))
    db.commit()
    invalidate(key)  # 热缓存失效：测试直接写库，手动清缓存


def _add_staff(db, username="admin"):
    u = base_models.WebUser(username=username, password_hash="x",
                            is_staff=True, is_active=True)
    db.add(u)
    db.commit()
    return u


def _add_item(db, name, item_type, date_added, series_id=None, library_id=1,
              year=None, rating=None, poster=None, season_number=None):
    item = emby_models.MediaItem(
        guid=f"g-{name}-{item_type}-{date_added.timestamp()}",
        library_id=library_id,
        item_type=item_type,
        name=name,
        series_id=series_id,
        date_added=date_added,
        production_year=year,
        community_rating=rating,
        primary_image_url=poster,
        season_number=season_number,
    )
    db.add(item)
    db.commit()
    return item


def _add_library(db, lib_id=1, name="动漫库", collection_type="tvshows"):
    lib = emby_models.Library(id=lib_id, guid=f"lib-guid-{lib_id}", name=name,
                              collection_type=collection_type)
    db.add(lib)
    db.commit()
    return lib


# ---------- 配置 ----------

def test_config_defaults(db):
    enabled, channels = nmn._get_config(db)
    assert enabled is True
    assert channels == ["telegram"]


def test_config_disabled(db):
    _add_config(db, nmn.CONFIG_ENABLED, "0")
    enabled, _ = nmn._get_config(db)
    assert enabled is False


def test_config_custom_channels(db):
    _add_config(db, nmn.CONFIG_CHANNELS, "telegram, email")
    _, channels = nmn._get_config(db)
    assert channels == ["telegram", "email"]


def test_config_empty_channels_falls_back(db):
    _add_config(db, nmn.CONFIG_CHANNELS, " , ")
    _, channels = nmn._get_config(db)
    assert channels == ["telegram"]


# ----------

# ---------- 新增汇总 ----------

def test_summarize_only_this_round(db):
    start = datetime.now()
    old = start - timedelta(hours=2)
    _add_item(db, "老剧", "series", old)
    _add_item(db, "新剧A", "series", start + timedelta(seconds=1),
              year=2026, rating=9.1, poster="https://img.example.com/a.jpg")
    _add_item(db, "新电影B", "movie", start + timedelta(seconds=2), year=2025)
    s = _add_item(db, "老剧2", "series", old)
    _add_item(db, "S01E11", "episode", start + timedelta(seconds=3),
              series_id=s.id, season_number=1)
    _add_item(db, "第2季", "season", start + timedelta(seconds=4),
              series_id=s.id)  # 季不通知

    summary = nmn._summarize_new_items(db, 1, start)
    assert summary is not None
    assert summary["total"] == 2
    names = [it["name"] for it in summary["items"]]
    assert names == ["新剧A", "新电影B"]
    assert summary["items"][0]["year"] == 2026
    assert summary["items"][0]["rating"] == 9.1
    assert summary["items"][0]["poster"] == "https://img.example.com/a.jpg"
    assert len(summary["episode_groups"]) == 1
    g = summary["episode_groups"][0]
    assert g["name"] == "老剧2" and g["count"] == 1 and g["season"] == 1


def test_summarize_new_series_episode_count(db):
    start = datetime.now()
    s = _add_item(db, "新剧", "series", start + timedelta(seconds=1))
    for i in range(3):
        _add_item(db, f"E{i}", "episode", start + timedelta(seconds=2 + i),
                  series_id=s.id, season_number=1)
    summary = nmn._summarize_new_items(db, 1, start)
    assert summary["items"][0]["episode_count"] == 3
    # 新剧自己的集不算"老剧出新集"
    assert summary["episode_groups"] == []


def test_summarize_other_library_excluded(db):
    start = datetime.now()
    _add_item(db, "别库的剧", "series", start + timedelta(seconds=1), library_id=99)
    assert nmn._summarize_new_items(db, 1, start) is None


def test_summarize_nothing_new_returns_none(db):
    assert nmn._summarize_new_items(db, 1, datetime.now()) is None


# ---------- HTML 消息组装 ----------

def _summary(items=(), ep_groups=(), total=None):
    return {
        "items": list(items),
        "total": len(items) if total is None else total,
        "episode_groups": list(ep_groups),
    }


def _item(name, item_type="series", year=2026, rating=9.1, poster=None,
          episode_count=12, item_id=1):
    return {"id": item_id, "name": name, "item_type": item_type, "year": year,
            "rating": rating, "poster": poster, "episode_count": episode_count}


def test_build_message_rich_format():
    text = nmn._build_message("📚", "动漫库", _summary([
        _item("葬送的芙莉莲", year=2026, rating=9.1, episode_count=12),
        _item("新电影", item_type="movie", year=2025, rating=8.5,
              episode_count=0, item_id=2),
    ]))
    assert "📚 <b>新片入库 · 动漫库</b>" in text
    assert "<b>《葬送的芙莉莲》</b>" in text
    assert "📅 2026" in text and "📺 12 集" in text and "⭐ 9.1" in text
    assert "🎬 电影" in text
    assert "overview" not in text.lower()  # 只列标题，不剧透


def test_build_message_episode_groups():
    text = nmn._build_message("📚", "动漫库", _summary(ep_groups=[
        {"series_id": 9, "name": "老剧", "season": 2, "count": 3,
         "poster": None},
    ]))
    assert "《老剧》" in text and "第 2 季" in text and "新增 3 集" in text


def test_build_message_escapes_html():
    text = nmn._build_message("📚", "动漫库",
                              _summary([_item("<script>alert(1)</script>")]))
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_build_message_truncates_items():
    items = [_item(f"剧{i:02d}", item_id=i) for i in range(15)]
    text = nmn._build_message("📚", "动漫库",
                              _summary(items[:10], total=15))
    assert "共 15 部新片" in text


# ---------- 海报 / 按钮 / emoji ----------

def test_collect_photos_dedup_and_limit():
    summary = _summary(
        [_item(f"剧{i}", poster=f"https://img.example.com/{i % 3}.jpg",
               item_id=i) for i in range(12)],
        [{"series_id": 99, "name": "老剧", "season": 1, "count": 2,
          "poster": "https://img.example.com/0.jpg"}],
    )
    photos = nmn._collect_photos(summary)
    assert len(photos) <= 10
    assert len(set(photos)) == len(photos)  # 去重


def test_safe_url_rejects_non_http():
    assert nmn._safe_url("https://img.example.com/a.jpg")
    assert nmn._safe_url("/data/poster.jpg") is None
    assert nmn._safe_url("javascript:alert(1)") is None
    assert nmn._safe_url(None) is None


def test_library_emoji():
    assert nmn._library_emoji("动漫库", "tvshows") == "📚"
    assert nmn._library_emoji("电影库", "movies") == "🎬"
    assert nmn._library_emoji("国产剧", "tvshows") == "📺"
    assert nmn._library_emoji("未知库", "") == "📚"


def test_to_plain_text():
    plain = nmn._to_plain_text("📚 <b>新片入库</b>\n<i>…等</i>")
    assert plain == "📚 新片入库\n…等"


# ---------- TelegramChannel.send_rich ----------

def _tg_channel(monkeypatch, calls):
    from backend.notifications import TelegramChannel
    ch = TelegramChannel(bot_token="test-token")

    async def fake_api(method, payload):
        calls.append((method, payload))
        return True, None

    monkeypatch.setattr(ch, "_api", fake_api)
    # 历史记录不写库
    monkeypatch.setattr(ch, "_record_history", lambda *a: None)
    return ch


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_send_rich_text_only(monkeypatch):
    calls = []
    ch = _tg_channel(monkeypatch, calls)
    ok, err = _run(ch.send_rich(123, text="<b>hi</b>"))
    assert ok and err is None
    method, payload = calls[0]
    assert method == "sendMessage"
    assert payload["parse_mode"] == "HTML"
    assert "reply_markup" not in payload


def test_send_rich_single_photo(monkeypatch):
    calls = []
    ch = _tg_channel(monkeypatch, calls)
    ok, _ = _run(ch.send_rich(123, text="cap",
                              photos=["https://img.example.com/a.jpg"]))
    assert ok
    method, payload = calls[0]
    assert method == "sendPhoto"
    assert payload["photo"] == "https://img.example.com/a.jpg"


def test_send_rich_media_group(monkeypatch):
    calls = []
    ch = _tg_channel(monkeypatch, calls)
    ok, _ = _run(ch.send_rich(
        123, text="cap",
        photos=["https://img.example.com/a.jpg",
                "https://img.example.com/b.jpg"]))
    assert ok
    assert len(calls) == 1  # 只有 media group，没有按钮补发
    method, payload = calls[0]
    assert method == "sendMediaGroup"
    media = payload["media"]
    assert len(media) == 2
    assert media[0]["caption"] == "cap"  # caption 挂第一张
    assert "caption" not in media[1]


def test_send_rich_photo_failure_falls_back_to_text(monkeypatch):
    from backend.notifications import TelegramChannel
    ch = TelegramChannel(bot_token="test-token")
    calls = []

    async def flaky_api(method, payload):
        calls.append(method)
        if method in ("sendPhoto", "sendMediaGroup"):
            return False, "bad photo"
        return True, None

    monkeypatch.setattr(ch, "_api", flaky_api)
    monkeypatch.setattr(ch, "_record_history", lambda *a: None)
    ok, _ = _run(ch.send_rich(123, text="cap",
                              photos=["https://img.example.com/a.jpg"]))
    assert ok  # 降级成功，通知不丢
    assert "sendMessage" in calls


def test_send_rich_no_token():
    from backend.notifications import TelegramChannel
    ch = TelegramChannel(bot_token=None)
    ok, err = _run(ch.send_rich(123, text="hi"))
    assert not ok and err


# ---------- 入口

# ---------- 入口与失败隔离 ----------

def test_maybe_notify_no_added_no_thread(monkeypatch):
    started = []

    def fake_thread(*a, **k):
        started.append(True)
        return None

    monkeypatch.setattr(nmn.threading, "Thread", fake_thread)
    nmn.maybe_notify_new_media(1, "动漫库", datetime.now(), 0)
    assert started == []


def test_maybe_notify_spawns_daemon_thread(monkeypatch):
    created = {}

    class FakeThread:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def start(self):
            created["started"] = True

    monkeypatch.setattr(nmn.threading, "Thread", FakeThread)
    nmn.maybe_notify_new_media(1, "动漫库", datetime.now(), 5)
    assert created.get("started") is True
    assert created.get("daemon") is True


def test_notify_worker_swallows_exceptions(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tg 炸了")

    monkeypatch.setattr(nmn, "_do_notify", boom)
    # 不抛异常就是成功：通知失败绝不能影响扫描
    nmn._notify_worker(1, "动漫库", datetime.now())


def test_do_notify_disabled_sends_nothing(db, monkeypatch):
    _add_config(db, nmn.CONFIG_ENABLED, "0")
    _add_staff(db)
    monkeypatch.setattr(nmn, "SessionLocal", lambda: db)

    async def fake_send(*a, **k):
        raise AssertionError("禁用时不该发送")

    monkeypatch.setattr(nmn, "_send_to_staff", fake_send)
    nmn._do_notify(1, "动漫库", datetime.now())  # 不抛异常即通过


def test_do_notify_no_new_items_sends_nothing(db, monkeypatch):
    _add_staff(db)
    monkeypatch.setattr(nmn, "SessionLocal", lambda: db)

    async def fake_send(*a, **k):
        raise AssertionError("无新增时不该发送")

    monkeypatch.setattr(nmn, "_send_to_staff", fake_send)
    nmn._do_notify(1, "动漫库", datetime.now())


def test_do_notify_no_staff_sends_nothing(db, monkeypatch):
    start = datetime.now()
    _add_item(db, "新剧", "series", start + timedelta(seconds=1))
    monkeypatch.setattr(nmn, "SessionLocal", lambda: db)

    async def fake_send(*a, **k):
        raise AssertionError("无管理员时不该发送")

    monkeypatch.setattr(nmn, "_send_to_staff", fake_send)
    nmn._do_notify(1, "动漫库", start)


def test_do_notify_no_start_time_sends_nothing(db, monkeypatch):
    _add_staff(db)
    monkeypatch.setattr(nmn, "SessionLocal", lambda: db)

    async def fake_send(*a, **k):
        raise AssertionError("started_at 为空时不该发送")

    monkeypatch.setattr(nmn, "_send_to_staff", fake_send)
    nmn._do_notify(1, "动漫库", None)


def test_do_notify_happy_path(db, monkeypatch):
    start = datetime.now()
    _add_library(db, 1, "动漫库", "tvshows")
    _add_item(db, "新剧A", "series", start + timedelta(seconds=1),
              year=2026, rating=9.1,
              poster="https://img.example.com/a.jpg")
    staff = _add_staff(db)
    staff_id = staff.id  # _do_notify 会 close session，先取值
    monkeypatch.setattr(nmn, "SessionLocal", lambda: db)

    sent = {}

    async def fake_send(staff_ids, text, photos, channels):
        sent["staff_ids"] = staff_ids
        sent["text"] = text
        sent["photos"] = photos
        sent["channels"] = channels

    monkeypatch.setattr(nmn, "_send_to_staff", fake_send)
    nmn._do_notify(1, "动漫库", start)
    assert sent["staff_ids"] == [staff_id]
    assert "新片入库 · 动漫库" in sent["text"]
    assert "《新剧A》" in sent["text"]
    assert sent["photos"] == ["https://img.example.com/a.jpg"]
    assert sent["channels"] == ["telegram"]


def test_send_to_staff_uses_rich_for_telegram(db, monkeypatch):
    """_send_to_staff：telegram 走 send_rich，其他渠道走文本。"""
    import asyncio
    from backend import notifications as notif_mod

    staff = _add_staff(db)
    staff_id = staff.id
    # r30 N1：旧 telegram_users 表已废弃，绑定走 WebUser.telegram_id
    staff.telegram_id = 777001
    db.commit()

    rich_calls = []

    class FakeTgChannel:
        async def send_rich(self, chat_id, **kwargs):
            rich_calls.append((chat_id, kwargs))
            return True, None

    class FakeService:
        channels = {"telegram": FakeTgChannel()}

        async def send(self, **kwargs):
            raise AssertionError("telegram 不该走旧文本通道")

    monkeypatch.setattr(notif_mod, "get_notification_service",
                        lambda: FakeService())
    monkeypatch.setattr(nmn, "SessionLocal", lambda: db)

    asyncio.run(nmn._send_to_staff(
        [staff_id], "<b>hi</b>", ["https://img.example.com/a.jpg"],
        ["telegram"]))
    assert len(rich_calls) == 1
    chat_id, kwargs = rich_calls[0]
    assert chat_id == 777001
    assert kwargs["photos"] == ["https://img.example.com/a.jpg"]
    assert kwargs["title"] == "新片入库通知"


# ---------- scan_queue 接线 ----------
# 策略：让 maybe_notify_new_media 的真实 guard 跑（added<=0 直接返回），
# 只把 _notify_worker 和 Thread 打桩，避免真起线程/真查库。

def _run_task_with_mocks(monkeypatch, scan_result):
    """跑一次 _run_task（扫描逻辑全打桩），返回 (task, worker_calls)。"""
    import types
    from backend.emby_server import scan_queue
    from backend.emby_server import scanner as _scanner
    from backend.emby_server import new_media_notify as _nmn
    from backend.emby_server import scan_progress as _progress

    worker_calls = []
    monkeypatch.setattr(_nmn, "_notify_worker",
                        lambda *a: worker_calls.append(a))

    class SyncThread:
        def __init__(self, target=None, args=(), **kwargs):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(_nmn.threading, "Thread", SyncThread)

    if isinstance(scan_result, Exception):
        def _raise(*a, **k):
            raise scan_result
        monkeypatch.setattr(_scanner, "scan_library_sync", _raise)
        scan_status = "failed"
    else:
        monkeypatch.setattr(_scanner, "scan_library_sync",
                            lambda *a, **k: scan_result)
        scan_status = "success"

    class _FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return types.SimpleNamespace(id=7, name="动漫库",
                                         scan_status=scan_status)

    class _FakeSession:
        def query(self, *a, **k):
            return _FakeQuery()

        def close(self):
            pass

    monkeypatch.setattr(scan_queue, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(_progress, "begin_scan", lambda *a, **k: None)
    monkeypatch.setattr(_progress, "end_scan", lambda *a, **k: None)
    monkeypatch.setattr(_progress, "remote_stats",
                        lambda: {"lists": 0, "reused": 0})
    monkeypatch.setattr(scan_queue, "clear_progress", lambda *a, **k: None)

    task = scan_queue.ScanTask(library_id=7, name="动漫库",
                               trigger="manual", snapshot=object())
    task.started_at = datetime.now()
    scan_queue._run_task(task)
    return task, worker_calls


def test_run_task_triggers_notify_on_success_with_added(monkeypatch):
    task, worker_calls = _run_task_with_mocks(monkeypatch, {"added": 3})
    assert task.result == "success"
    assert len(worker_calls) == 1
    lib_id, lib_name, started_at = worker_calls[0]
    assert lib_id == 7 and lib_name == "动漫库" and started_at is not None


def test_run_task_no_notify_when_nothing_added(monkeypatch):
    task, worker_calls = _run_task_with_mocks(
        monkeypatch, {"added": 0, "updated": 5})
    assert task.result == "success"
    assert worker_calls == [], "无新增时不该打扰管理员"


def test_run_task_no_notify_on_failure(monkeypatch):
    task, worker_calls = _run_task_with_mocks(
        monkeypatch, RuntimeError("扫描炸了"))
    assert task.result == "failed"
    assert worker_calls == [], "扫描失败不该发入库通知"

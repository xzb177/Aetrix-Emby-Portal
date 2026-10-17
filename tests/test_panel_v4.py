# -*- coding: utf-8 -*-
"""v4 探险家日志面板 · 后端地基测试。

隔离：内存 SQLite + 最小 FastAPI 应用，不碰开发库/生产库，不 import backend.main。

覆盖：
1. 成就定义 seed：12 条，幂等（重复 seed 不翻倍）
2. 成就评估器：12 条规则逐条验证 + 幂等（重复评估不重复解锁）
3. 盲盒拆封：幂等（重复拆返回 already）
4. 机关拨杆：普通用户 403，管理员可写，未知 key 400
5. 特性开关：PANEL_V4_ENABLED=0 时接口 403
6. envelope 格式：{code, msg, data}
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend import panel_achievements as pa
from backend.api import panel as panel_api


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    # emby_server models 也要建表（EmbyItem/EmbySession 用）
    try:
        from backend.emby_server import models as em
        em.Base.metadata.create_all(engine)
    except Exception:
        pass
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def user(db):
    u = models.WebUser(username="panel_test", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def admin_user(db):
    u = models.WebUser(username="panel_admin", password_hash="x", is_active=True, is_staff=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_app(db_session, current_user):
    app = FastAPI()

    def _get_db():
        yield db_session

    def _get_user():
        return current_user

    app.include_router(panel_api.router)
    app.dependency_overrides[panel_api.get_db] = _get_db
    app.dependency_overrides[panel_api.get_current_user] = _get_user
    return app


# ==================== 1. 成就定义 seed ====================

def test_seed_achievement_defs_13_and_idempotent(db):
    added = pa.seed_achievement_defs(db)
    assert added == 13
    # 重复 seed 不翻倍
    added2 = pa.seed_achievement_defs(db)
    assert added2 == 0
    count = db.query(models.PanelAchievementDef).count()
    assert count == 13


def test_seed_does_not_override_disabled(db):
    pa.seed_achievement_defs(db)
    row = db.query(models.PanelAchievementDef).filter_by(key="streak7").first()
    row.enabled = False
    db.commit()
    pa.seed_achievement_defs(db)  # 重复 seed 不覆盖用户改过的开关
    row2 = db.query(models.PanelAchievementDef).filter_by(key="streak7").first()
    assert row2.enabled is False


# ==================== 2. 成就评估器 ====================

def _mk_session(db, user, name, start, duration):
    s = models.EmbySession(
        id=f"sess-{name}-{start.isoformat()}",
        user_id=user.id,
        item_name=name,
        item_type="movie",
        start_time=start,
        play_duration=duration,
    )
    db.add(s)
    db.commit()
    return s


def _bj_to_storage(bj_hour, bj_minute=0, days_ago=0):
    """北京时间墙钟 → 入库存储帧（UTC naive）：成就会话时间按此换算构造。"""
    bj = datetime.now() + timedelta(hours=8)
    t = bj.replace(hour=bj_hour, minute=bj_minute, second=0, microsecond=0)
    if days_ago:
        t -= timedelta(days=days_ago)
    return t - timedelta(hours=8)


def test_evaluate_first_night_and_early_bird(db, user):
    pa.seed_achievement_defs(db)
    # 深夜 23:30（北京时间）开始
    _mk_session(db, user, "深夜电影", _bj_to_storage(23, 30), 3600)
    # 清晨 6:30（北京时间）开始
    _mk_session(db, user, "清晨电影", _bj_to_storage(6, 30), 3600)
    new = pa.evaluate(user.id, db)
    assert "first_night" in new
    assert "early_bird" in new
    # 幂等：重复评估不重复解锁
    new2 = pa.evaluate(user.id, db)
    assert new2 == []
    assert db.query(models.PanelAchievementUnlock).filter_by(user_id=user.id).count() == 2


def test_evaluate_night_owl(db, user):
    pa.seed_achievement_defs(db)
    # 北京时间 14:00 与 18:00（同北京时间自然日）
    _mk_session(db, user, "马拉松1", _bj_to_storage(14, 0), 3 * 3600 + 100)
    _mk_session(db, user, "马拉松2", _bj_to_storage(18, 0), 3 * 3600 + 100)
    new = pa.evaluate(user.id, db)
    assert "night_owl" in new  # 单日超 6h


def test_evaluate_streak7(db, user):
    pa.seed_achievement_defs(db)
    db.add(models.CheckinRecord(
        user_id=user.id,
        checkin_date=datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
        streak=7,
    ))
    db.commit()
    new = pa.evaluate(user.id, db)
    assert "streak7" in new


def test_evaluate_sharer(db, user):
    pa.seed_achievement_defs(db)
    code = models.InvitationCode(code="TEST123", user_id=user.id)
    db.add(code)
    db.commit()
    db.add(models.InvitationRecord(inviter_id=user.id, invitee_id=99999, code_id=code.id))
    db.commit()
    new = pa.evaluate(user.id, db)
    assert "sharer" in new


def test_evaluate_disabled_rule_not_granted(db, user):
    pa.seed_achievement_defs(db)
    row = db.query(models.PanelAchievementDef).filter_by(key="first_night").first()
    row.enabled = False
    db.commit()
    _mk_session(db, user, "深夜电影", _bj_to_storage(23, 30), 3600)
    new = pa.evaluate(user.id, db)
    assert "first_night" not in new


# ==================== 3. 盲盒拆封幂等 ====================

def test_unseal_idempotent(db, user):
    pa.seed_achievement_defs(db)
    # 造一个 emby 条目
    try:
        from backend.emby_server import models as em
        item = em.MediaItem(
            guid="test-guid-1", library_id=1, item_type="movie",
            name="测试电影", date_added=datetime.now(),
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        item_id = item.id
    except Exception:
        pytest.skip("EmbyItem 模型不可用")

    app = _make_app(db, user)
    client = TestClient(app)
    r1 = client.post(f"/api/panel/parcels/{item_id}/unseal")
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["code"] == 0
    assert d1["data"]["unsealed"] is True
    assert d1["data"]["already"] is False

    r2 = client.post(f"/api/panel/parcels/{item_id}/unseal")
    assert r2.json()["data"]["already"] is True

    # 404
    r3 = client.post("/api/panel/parcels/99999999/unseal")
    assert r3.status_code == 404


# ==================== 5. 特性开关 ====================

def test_panel_disabled_returns_403(db, user, monkeypatch):
    monkeypatch.setenv("PANEL_V4_ENABLED", "0")
    app = _make_app(db, user)
    client = TestClient(app)
    r = client.get("/api/panel/today")
    assert r.status_code == 403
    monkeypatch.delenv("PANEL_V4_ENABLED", raising=False)


# ==================== 6. envelope 格式 ====================

def test_envelope_format(db, user):
    pa.seed_achievement_defs(db)
    app = _make_app(db, user)
    client = TestClient(app)
    for path in ["/api/panel/today", "/api/panel/parcels", "/api/panel/pulse",
                 "/api/panel/letter", "/api/panel/home",
                 "/api/panel/collector", "/api/panel/server"]:
        r = client.get(path)
        assert r.status_code == 200, path
        body = r.json()
        assert body["code"] == 0, path
        assert body["msg"] == "ok", path
        assert "data" in body, path


def test_home_returns_all_four_sections(db, user):
    """GET /api/panel/home 一次返回 today/parcels/pulse/letter 四个区块（perf 2026-10-13）。"""
    pa.seed_achievement_defs(db)
    app = _make_app(db, user)
    client = TestClient(app)
    r = client.get("/api/panel/home")
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    for key in ["today", "parcels", "pulse", "letter"]:
        assert key in data, key
    # 与逐个接口的数据一致（剔除易变时间字段后比较）
    def _strip_volatile(section, key):
        # pulse 的 timestamp / activities[].at 是微秒级时间戳，
        # 两次 HTTP 调用（相隔几十 ms）必然不同，只能比较结构与业务字段。
        import copy
        section = copy.deepcopy(section)
        if key == "pulse" and isinstance(section, dict):
            section.pop("timestamp", None)
            for a in section.get("activities") or []:
                if isinstance(a, dict):
                    a.pop("at", None)
        return section

    for path, key in [("/api/panel/today", "today"),
                      ("/api/panel/parcels", "parcels"),
                      ("/api/panel/pulse", "pulse"),
                      ("/api/panel/letter", "letter")]:
        r2 = client.get(path)
        assert r2.status_code == 200, path
        got = _strip_volatile(r2.json()["data"], key)
        want = _strip_volatile(data[key], key)
        assert got == want, f"{path} 数据不一致"


def test_push_activity_ring_buffer(db):
    for i in range(505):
        panel_api.push_activity(db, f"活动 {i}")
    count = db.query(models.PanelActivity).count()
    assert count == 500  # 每服分区环形保留最近 500 条


def test_push_activity_ring_buffer_per_realm(db):
    for i in range(505):
        panel_api.push_activity(db, f"服1活动 {i}", realm_id=1)
    for i in range(3):
        panel_api.push_activity(db, f"服2活动 {i}", realm_id=2)
    from backend.panel_feed import FEED_KEEP_PER_REALM
    c1 = db.query(models.PanelActivity).filter(
        models.PanelActivity.realm_id == 1).count()
    c2 = db.query(models.PanelActivity).filter(
        models.PanelActivity.realm_id == 2).count()
    assert c1 == FEED_KEEP_PER_REALM
    assert c2 == 3  # 另一服的分区不受影响


# ==================== 7. today/parcels 字段契约（r20 P1 修复锁定）===

def test_today_field_contract(db, user):
    """GET /api/panel/today 返回前端命名契约：字段集合精确锁定。

    后端已改向前段命名（date_label/parcel_count/streak_days/letter_readable），
    前端 usePanelApi.TodayData + DiscoveryView 按此消费。字段增减必须同步改前端。
    """
    app = _make_app(db, user)
    client = TestClient(app)
    r = client.get("/api/panel/today")
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data.keys()) == {
        "greeting", "date_label", "weekday", "parcel_count",
        "unsealed_count", "streak_days", "letter_readable", "nickname",
        # 心理学批（2026-10-16）：今日观影人数 / 连签里程碑临界 / 今日一句话
        "today_viewers", "streak_milestone", "daypart_line",
    }


def test_parcels_envelope_contract(db, user):
    """GET /api/panel/parcels 的 data 为 {parcels: [...]} 信封，非裸数组。

    前端 usePanelApi.parcels 类型为 { parcels: ParcelItem[] }，与此对齐。
    """
    app = _make_app(db, user)
    client = TestClient(app)
    r = client.get("/api/panel/parcels")
    assert r.status_code == 200
    data = r.json()["data"]
    assert set(data.keys()) == {"parcels"}
    assert isinstance(data["parcels"], list)
    for p in data["parcels"]:
        assert "unsealed" in p and "deep_link" in p


def test_today_unsealed_count_contract(db, user):
    """today 的 parcel_count/unsealed_count 随拆封联动（r20 命名锁定）。"""
    try:
        from backend.emby_server import models as em
    except Exception:
        pytest.skip("EmbyItem 模型不可用")
    item = em.MediaItem(
        guid="contract-today-1", library_id=1, item_type="movie",
        name="契约电影", date_added=datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None),
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    app = _make_app(db, user)
    client = TestClient(app)
    r = client.post(f"/api/panel/parcels/{item.id}/unseal")
    assert r.json()["data"]["already"] is False

    d = client.get("/api/panel/today").json()["data"]
    assert d["parcel_count"] >= 1
    assert d["unsealed_count"] == 1
    assert d["streak_days"] == 0
    assert d["letter_readable"] is True


# ---------- 问候语时区回归（2026-10-15 生产验收） ----------
#
# 根因：生产服务器本地时间为 UTC，_greeting() 用 naive datetime.now().hour，
# 北京时间 19 点（= UTC 11 点）会显示"上午好"。修复：按 Asia/Shanghai 取小时。
def _fixed_datetime_at(utc_dt):
    from datetime import timezone

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            aware = utc_dt.replace(tzinfo=timezone.utc)
            return aware.astimezone(tz) if tz is not None else aware.replace(tzinfo=None)

    return _FixedDT


def test_greeting_uses_shanghai_not_server_utc(monkeypatch):
    # UTC 11:00 = 上海 19:00 → 期望"晚上好"；旧代码 naive 取 11 点会给出"上午好"
    monkeypatch.setattr(panel_api, "datetime", _fixed_datetime_at(datetime(2026, 10, 15, 11, 0, 0)))
    assert panel_api._greeting() == "晚上好"


def test_greeting_morning_boundary_shanghai(monkeypatch):
    # UTC 00:30 = 上海 08:30 → 期望"早上好"；旧代码 naive 取 0 点会给出"夜深了"
    monkeypatch.setattr(panel_api, "datetime", _fixed_datetime_at(datetime(2026, 10, 15, 0, 30, 0)))
    assert panel_api._greeting() == "早上好"


# ==================== 8. 今日封存口径（2026-10-16） ====================
#
# 诊断结论（生产实证）：
# ① panel_parcels 只统计整部新入库的 movie/series，单集永不算——日常入库
#    绝大多数是已有剧的新单集，故发现页常年显示"今天还没有新封存"。
# ② 帧错位：date_added 由 scanner 以容器本地时间（UTC）naive 写入，
#    而 panel 用 Shanghai-naive 的 00:00 去比较——每天 00:00–08:00（CST）
#    入库的条目被算成"昨天"（如 2026-10-16 02:06 CST 入库的《明天》）。
# 修复口径：今日整部新入库 movie/series ＋ 今日有新 episode 入库的 series
# （同一部剧只出现一次），按今日最新动态时间倒序，上限 5；
# /today 的 parcel_count 与 /parcels 同口径。

def _mk_media(db, item_type, name, date_added, **kw):
    from backend.emby_server import models as em
    kw.setdefault("guid", f"guid-{name}-{item_type}")
    kw.setdefault("library_id", 1)
    item = em.MediaItem(item_type=item_type, name=name, date_added=date_added, **kw)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _parcel_ids(client):
    r = client.get("/api/panel/parcels")
    assert r.status_code == 200
    return [p["id"] for p in r.json()["data"]["parcels"]]


def test_parcels_includes_series_with_new_episodes_today(db, user):
    """老剧今日有新单集 → 该剧作为一件封存出现。"""
    from backend.emby_server import models as em  # noqa: F401
    start_utc = panel_api._today_start_utc_naive()
    series = _mk_media(db, "series", "老剧", start_utc - timedelta(days=30))
    _mk_media(db, "episode", "老剧 E99", start_utc + timedelta(hours=10),
              series_id=series.id, season_number=1, episode_number=99)
    app = _make_app(db, user)
    assert series.id in _parcel_ids(TestClient(app))


def test_parcels_dedupe_whole_and_episode_series(db, user):
    """同一部剧今日既是整部新入库、又有新单集 → 只出现一次。"""
    from backend.emby_server import models as em  # noqa: F401
    start_utc = panel_api._today_start_utc_naive()
    series = _mk_media(db, "series", "新剧", start_utc + timedelta(hours=9))
    _mk_media(db, "episode", "新剧 E01", start_utc + timedelta(hours=10),
              series_id=series.id, season_number=1, episode_number=1)
    app = _make_app(db, user)
    ids = _parcel_ids(TestClient(app))
    assert ids.count(series.id) == 1


def test_parcels_limit_five_ordered_by_latest_activity(db, user):
    """候选超 5 件 → 只取 5，按今日最新动态时间倒序。"""
    from backend.emby_server import models as em  # noqa: F401
    start_utc = panel_api._today_start_utc_naive()
    made = []
    for i in range(7):
        s = _mk_media(db, "series", f"剧{i}", start_utc - timedelta(days=30))
        _mk_media(db, "episode", f"剧{i} E1", start_utc + timedelta(hours=9 + i),
                  series_id=s.id, season_number=1, episode_number=1)
        made.append(s.id)
    app = _make_app(db, user)
    ids = _parcel_ids(TestClient(app))
    assert len(ids) == 5
    # 最新动态的是剧6（+15h），应排第一
    assert ids[0] == made[6]


def test_parcels_utc_frame_includes_early_morning_items(db, user):
    """帧回归：CST 凌晨 2 点入库（存为 UTC 前一天 18:00）必须算"今日"。

    旧代码用 Shanghai-naive 00:00 比较，会把它排除（2026-10-16 生产实证）。
    """
    from backend.emby_server import models as em  # noqa: F401
    start_utc = panel_api._today_start_utc_naive()
    movie = _mk_media(db, "movie", "凌晨电影", start_utc + timedelta(hours=2))
    app = _make_app(db, user)
    assert movie.id in _parcel_ids(TestClient(app))


def test_parcels_excludes_merged_copies(db, user):
    """合并副本不单独占一件封存（与 #920 列表口径一致）。"""
    from backend.emby_server import models as em  # noqa: F401
    start_utc = panel_api._today_start_utc_naive()
    canon = _mk_media(db, "movie", "正片", start_utc + timedelta(hours=9))
    dup = _mk_media(db, "movie", "正片副本", start_utc + timedelta(hours=10),
                    merged_into_id=canon.id)
    app = _make_app(db, user)
    ids = _parcel_ids(TestClient(app))
    assert canon.id in ids
    assert dup.id not in ids


def test_today_parcel_count_same_caliber_as_parcels(db, user):
    """/today 的 parcel_count 与 /parcels 同口径（含单集驱动的剧）。"""
    from backend.emby_server import models as em  # noqa: F401
    start_utc = panel_api._today_start_utc_naive()
    series = _mk_media(db, "series", "追更剧", start_utc - timedelta(days=30))
    _mk_media(db, "episode", "追更剧 E50", start_utc + timedelta(hours=10),
              series_id=series.id, season_number=1, episode_number=50)
    app = _make_app(db, user)
    client = TestClient(app)
    parcels = client.get("/api/panel/parcels").json()["data"]["parcels"]
    today = client.get("/api/panel/today").json()["data"]
    assert today["parcel_count"] == len(parcels) >= 1
    assert {p["id"] for p in parcels} == {series.id}


# ==================== 8. GET /api/panel/feed（发现页实时动态流）====================

def _push_feed(db, text, event_type="system", realm_id=None, library_name=None):
    from backend.panel_feed import note_feed
    assert note_feed(db, text, event_type=event_type, realm_id=realm_id,
                     library_name=library_name) is True


def test_feed_items_shape_and_desc_order(db, user):
    _push_feed(db, "扫描完成 · 新增 3 · 更新 1", "scan", realm_id=None,
               library_name="国产剧")
    _push_feed(db, "一位影迷开始看《测试剧》第 1 集", "play", realm_id=None,
               library_name="国产剧")
    app = _make_app(db, user)
    r = TestClient(app).get("/api/panel/feed")
    assert r.status_code == 200
    data = r.json()["data"]
    items = data["items"]
    assert len(items) == 2
    # 时间倒序：后写入的在前
    assert items[0]["text"] == "一位影迷开始看《测试剧》第 1 集"
    assert items[0]["type"] == "play"
    assert items[0]["library"] == "国产剧"
    assert items[1]["type"] == "scan"
    # 字段契约
    assert set(items[0].keys()) == {"at", "type", "text", "library"}
    assert items[0]["at"]  # iso 时间戳


def test_feed_realm_filter(db, user):
    _push_feed(db, "服1扫描", "scan", realm_id=1, library_name="国产剧")
    _push_feed(db, "服2扫描", "scan", realm_id=2, library_name="国产剧")
    _push_feed(db, "全局系统事件", "system", realm_id=None)
    app = _make_app(db, user)
    client = TestClient(app)
    got1 = {i["text"] for i in client.get("/api/panel/feed?realm_id=1").json()["data"]["items"]}
    got2 = {i["text"] for i in client.get("/api/panel/feed?realm_id=2").json()["data"]["items"]}
    # 本服事件 + 全局事件可见，别服事件不可见（不聚合）
    assert got1 == {"服1扫描", "全局系统事件"}
    assert got2 == {"服2扫描", "全局系统事件"}


def test_feed_default_realm_is_active_realm(db, user):
    from backend import realms
    rid = realms.active_realm_id(db)
    _push_feed(db, "当前服事件", "scan", realm_id=rid, library_name="国产剧")
    _push_feed(db, "别服事件", "scan", realm_id=(rid + 999), library_name="国产剧")
    _push_feed(db, "全局事件", "system", realm_id=None)
    app = _make_app(db, user)
    r = TestClient(app).get("/api/panel/feed")  # 不传 realm_id=跟随当前服
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["realm_id"] == rid
    texts = {i["text"] for i in data["items"]}
    assert texts == {"当前服事件", "全局事件"}


def test_feed_limit_default_and_clamp(db, user):
    for i in range(60):
        _push_feed(db, f"事件 {i}", "system")
    app = _make_app(db, user)
    client = TestClient(app)
    assert len(client.get("/api/panel/feed").json()["data"]["items"]) == 12
    assert len(client.get("/api/panel/feed?limit=5").json()["data"]["items"]) == 5
    assert len(client.get("/api/panel/feed?limit=50").json()["data"]["items"]) == 50
    # 上限 50：超限直接 422（契约写明上限，不静默夹断）
    assert client.get("/api/panel/feed?limit=200").status_code == 422


def test_feed_invalid_event_type_falls_back_to_system(db, user):
    from backend.panel_feed import read_feed
    _push_feed(db, "未知类型", "bogus", realm_id=None)
    items = read_feed(db, None, 5)
    assert items[0]["type"] == "system"


def test_play_feed_text_anonymized(db, user):
    """播放事件文案匿名：绝不出现用户昵称/ID。"""
    from backend.emby_server.compat_routes import _note_play_feed
    from backend.emby_server import models as em

    user.nickname = "张三"
    db.commit()
    series = _mk_media(db, "series", "测试剧", datetime.now() - timedelta(days=30))
    ep = _mk_media(db, "episode", "测试剧 E3", datetime.now(),
                  series_id=series.id, season_number=1, episode_number=3)
    movie = _mk_media(db, "movie", "测试电影", datetime.now())

    _note_play_feed(db, ep, started=True, finished=False)
    _note_play_feed(db, movie, started=False, finished=True)

    rows = db.query(models.PanelActivity).order_by(
        models.PanelActivity.id).all()
    assert len(rows) == 2
    assert all(r.event_type == "play" for r in rows)
    texts = [r.text for r in rows]
    assert "一位影迷开始看《测试剧》第 3 集" in texts
    assert "一位影迷看完《测试电影》" in texts
    for t in texts:
        assert "张三" not in t
        assert "panel_test" not in t
        assert str(user.id) not in t

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


def test_evaluate_first_night_and_early_bird(db, user):
    pa.seed_achievement_defs(db)
    # 深夜 23:30 开始
    _mk_session(db, user, "深夜电影", datetime.now().replace(hour=23, minute=30), 3600)
    # 清晨 6:30 开始
    _mk_session(db, user, "清晨电影", datetime.now().replace(hour=6, minute=30), 3600)
    new = pa.evaluate(user.id, db)
    assert "first_night" in new
    assert "early_bird" in new
    # 幂等：重复评估不重复解锁
    new2 = pa.evaluate(user.id, db)
    assert new2 == []
    assert db.query(models.PanelAchievementUnlock).filter_by(user_id=user.id).count() == 2


def test_evaluate_night_owl(db, user):
    pa.seed_achievement_defs(db)
    base = datetime.now().replace(hour=14, minute=0)
    _mk_session(db, user, "马拉松1", base, 3 * 3600 + 100)
    _mk_session(db, user, "马拉松2", base + timedelta(hours=4), 3 * 3600 + 100)
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
    _mk_session(db, user, "深夜电影", datetime.now().replace(hour=23, minute=30), 3600)
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


# ==================== 4. 机关拨杆权限 ====================

def test_toggle_admin_only(db, user, admin_user):
    pa.seed_achievement_defs(db)
    # 普通用户 -> 403
    app = _make_app(db, user)
    client = TestClient(app)
    r = client.post("/api/panel/server/toggles", json={"key": "panel_night_scrape", "on": False})
    assert r.status_code == 403

    # 管理员 -> 可写
    app2 = _make_app(db, admin_user)
    client2 = TestClient(app2)
    r2 = client2.post("/api/panel/server/toggles", json={"key": "panel_night_scrape", "on": False})
    assert r2.status_code == 200
    assert r2.json()["data"] == {"key": "panel_night_scrape", "on": False}

    # 未知 key -> 400
    r3 = client2.post("/api/panel/server/toggles", json={"key": "nope", "on": True})
    assert r3.status_code == 400


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
    for i in range(55):
        panel_api.push_activity(db, f"活动 {i}")
    count = db.query(models.PanelActivity).count()
    assert count == 50  # 环形保留 50 条


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

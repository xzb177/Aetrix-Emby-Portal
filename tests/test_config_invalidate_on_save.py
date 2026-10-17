"""配置简化第 4 批第 1 步：6 个管理端保存接口的缓存失效。

回归背景：这 6 个接口此前直接 upsert SystemConfig 但从不调
store.invalidate()，按 store 的「写完即失效」纪律，保存后同进程内
60 秒都可能读到旧值（看起来像「保存没生效」）。

每个用例都走三步：先把旧值灌进 store 热缓存 → 调保存接口 →
断言用 store 立刻读到新值（而不是陈旧的旧值）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    try:
        from backend.emby_server import models as em

        em.Base.metadata.create_all(engine)
    except Exception:
        pass
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def admin_user(db):
    user = models.WebUser(
        username="invadmin",
        password_hash="x",
        is_active=True,
        is_staff=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture(autouse=True)
def _clean_store_cache():
    """store 缓存是进程全局的：每个用例后全清，避免互相污染。"""
    yield
    store.invalidate()


def _prime(db, key, old_value):
    """把旧值灌进热缓存（模拟真实运行时的陈旧状态）。"""
    store.write_values(db, {key: old_value})
    db.commit()
    store.invalidate()  # write_values 自带失效；这里清掉再读旧值灌缓存
    assert store.get_value(db, key, "nope") == old_value


def _app_with(router, get_current_admin, get_db, db, admin_user, staff_dep=None):
    app = FastAPI()
    app.include_router(router)

    def _gd():
        yield db

    app.dependency_overrides[get_db] = _gd
    if staff_dep is not None:
        app.dependency_overrides[staff_dep] = lambda: admin_user
    else:
        app.dependency_overrides[get_current_admin] = lambda: admin_user
    return app


def test_alert_settings_invalidate(db, admin_user):
    from backend.api import admin_alerts
    from backend import system_alerts

    app = _app_with(
        admin_alerts.admin_alerts_router,
        admin_alerts.get_current_admin,
        admin_alerts.get_db,
        db,
        admin_user,
    )
    _prime(db, system_alerts.INTERVAL_KEY, "600")
    r = TestClient(app).put("/api/admin/alerts/settings", json={"check_interval_sec": 300})
    assert r.status_code == 200
    assert store.get_value(db, system_alerts.INTERVAL_KEY, "600") == "300"


def test_expiry_reminder_settings_invalidate(db, admin_user):
    from backend.api import reminders_admin

    app = _app_with(
        reminders_admin.admin_reminders_router,
        reminders_admin.get_current_admin,
        reminders_admin.get_db,
        db,
        admin_user,
    )
    _prime(db, "expiry_reminder_enabled", "")
    r = TestClient(app).put(
        "/api/admin/economy/expiry-reminders/settings",
        json={"settings": {"expiry_reminder_enabled": True}},
    )
    assert r.status_code == 200
    assert store.get_value(db, "expiry_reminder_enabled", "") == "true"


def test_coupon_settings_invalidate(db, admin_user):
    from backend.api import coupons_admin
    from backend import coupons

    app = _app_with(
        coupons_admin.admin_coupons_router,
        coupons_admin.get_current_admin,
        coupons_admin.get_db,
        db,
        admin_user,
    )
    _prime(db, coupons.CONFIG_ENABLED, "true")
    r = TestClient(app).put(
        "/api/admin/economy/coupons/settings",
        json={"enabled": False, "reserve_hours": 48},
    )
    assert r.status_code == 200
    assert store.get_value(db, coupons.CONFIG_ENABLED, "true") == "false"
    assert store.get_value(db, coupons.CONFIG_RESERVE_HOURS, "") == "48"


def test_welfare_config_invalidate(db, admin_user):
    from backend.api import welfare_admin

    app = _app_with(
        welfare_admin.admin_router,
        welfare_admin.get_current_admin,
        welfare_admin.get_db,
        db,
        admin_user,
    )
    _prime(db, "welfare_grace_days", "7")
    r = TestClient(app).put("/api/admin/welfare/config", json={"welfare_grace_days": "10"})
    assert r.status_code == 200
    assert store.get_value(db, "welfare_grace_days", "7") == "10"


def test_registration_settings_invalidate(db, admin_user):
    from backend.api import admin as admin_api

    app = _app_with(
        admin_api.admin_router,
        admin_api.get_current_admin,
        admin_api.get_db,
        db,
        admin_user,
    )
    _prime(db, "register_ratelimit_max", "3")
    r = TestClient(app).put(
        "/api/admin/settings/registration",
        json={"mode": "closed", "ratelimit_max": 5, "ratelimit_window": 120},
    )
    assert r.status_code == 200
    assert store.get_value(db, "register_ratelimit_max", "3") == "5"
    assert store.get_value(db, "registration_mode", "") == "closed"


def test_view_unlock_invalidate(db, admin_user):
    from backend.emby_server import portal

    app = FastAPI()
    app.include_router(portal.admin_emby_router)

    def _gd():
        yield db

    app.dependency_overrides[portal.get_db] = _gd
    app.dependency_overrides[portal.require_staff] = lambda: admin_user
    _prime(db, portal.VIEW_UNLOCK_POINTS_KEY, "50")
    r = TestClient(app).put("/api/admin/emby/view-unlock", json={"points": 80, "days": 30})
    assert r.status_code == 200
    assert store.get_value(db, portal.VIEW_UNLOCK_POINTS_KEY, "50") == "80"
    assert store.get_value(db, portal.VIEW_UNLOCK_DAYS_KEY, "365") == "30"

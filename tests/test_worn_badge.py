import os
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("PANEL_V4_ENABLED", "true")

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.models as models
from backend import panel_achievements
from backend.api import panel as panel_api


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    try:
        from backend.emby_server import models as emby_models
        emby_models.Base.metadata.create_all(engine)
    except Exception:
        pass
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture
def user(db):
    u = models.WebUser(username="badge_test", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_app(db_session, current_user):
    app = FastAPI()
    app.include_router(panel_api.router)
    app.dependency_overrides[panel_api.get_db] = lambda: db_session
    app.dependency_overrides[panel_api.get_current_user] = lambda: current_user
    return TestClient(app)


def test_collector_worn_badge_none_for_new_user(db, user):
    client = _make_app(db, user)
    resp = client.get("/api/panel/collector")
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"]["worn_badge"] is None


def test_worn_badge_post_requires_unlock(db, user):
    panel_achievements.seed_achievement_defs(db)
    client = _make_app(db, user)
    resp = client.post("/api/panel/worn-badge", json={"key": "yearly_explorer"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "你还没有解锁这枚徽章"


def test_wear_badge_success(db, user):
    panel_achievements.seed_achievement_defs(db)
    unlock = models.PanelAchievementUnlock(
        user_id=user.id, ach_key="yearly_explorer"
    )
    db.add(unlock)
    db.commit()

    client = _make_app(db, user)
    resp = client.post("/api/panel/worn-badge", json={"key": "yearly_explorer"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"]["worn_badge"] == {
        "key": "yearly_explorer",
        "name": "年卡探险家",
        "icon": "🎫",
    }

    resp2 = client.get("/api/panel/collector")
    assert resp2.status_code == 200
    assert resp2.json()["data"]["worn_badge"]["key"] == "yearly_explorer"

    db.refresh(user)
    assert user.worn_badge_key == "yearly_explorer"


def test_take_off_badge(db, user):
    panel_achievements.seed_achievement_defs(db)
    unlock = models.PanelAchievementUnlock(
        user_id=user.id, ach_key="yearly_explorer"
    )
    db.add(unlock)
    db.commit()

    client = _make_app(db, user)
    resp = client.post("/api/panel/worn-badge", json={"key": "yearly_explorer"})
    assert resp.status_code == 200

    resp2 = client.post("/api/panel/worn-badge", json={"key": None})
    assert resp2.status_code == 200
    assert resp2.json()["data"]["worn_badge"] is None

    resp3 = client.get("/api/panel/collector")
    assert resp3.status_code == 200
    assert resp3.json()["data"]["worn_badge"] is None

    db.refresh(user)
    assert user.worn_badge_key is None


def test_disabled_def_not_shown(db, user):
    panel_achievements.seed_achievement_defs(db)
    unlock = models.PanelAchievementUnlock(
        user_id=user.id, ach_key="yearly_explorer"
    )
    db.add(unlock)
    db.commit()

    client = _make_app(db, user)
    resp = client.post("/api/panel/worn-badge", json={"key": "yearly_explorer"})
    assert resp.status_code == 200

    def_obj = (
        db.query(models.PanelAchievementDef)
        .filter_by(key="yearly_explorer")
        .first()
    )
    assert def_obj is not None
    def_obj.enabled = False
    db.commit()

    resp2 = client.get("/api/panel/collector")
    assert resp2.status_code == 200
    assert resp2.json()["data"]["worn_badge"] is None


def test_user_isolation(db, user):
    panel_achievements.seed_achievement_defs(db)
    unlock = models.PanelAchievementUnlock(
        user_id=user.id, ach_key="yearly_explorer"
    )
    db.add(unlock)
    db.commit()

    client_a = _make_app(db, user)
    resp = client_a.post("/api/panel/worn-badge", json={"key": "yearly_explorer"})
    assert resp.status_code == 200

    user_b = models.WebUser(username="badge_test_b", password_hash="x", is_active=True)
    db.add(user_b)
    db.commit()
    db.refresh(user_b)

    client_b = _make_app(db, user_b)
    resp_b = client_b.get("/api/panel/collector")
    assert resp_b.status_code == 200
    assert resp_b.json()["data"]["worn_badge"] is None


def test_worn_badge_payload_direct(db, user):
    panel_achievements.seed_achievement_defs(db)
    unlock = models.PanelAchievementUnlock(
        user_id=user.id, ach_key="yearly_explorer"
    )
    db.add(unlock)
    db.commit()

    user.worn_badge_key = "no_such_badge"
    db.commit()
    payload = panel_achievements.worn_badge_payload(db, user.id)
    assert payload is None

    user.worn_badge_key = "yearly_explorer"
    db.commit()
    db.delete(unlock)
    db.commit()
    payload2 = panel_achievements.worn_badge_payload(db, user.id)
    assert payload2 is None


def test_post_nonexistent_key(db, user):
    panel_achievements.seed_achievement_defs(db)
    client = _make_app(db, user)
    resp = client.post("/api/panel/worn-badge", json={"key": "no_such_badge"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "这枚徽章不存在或已下架"

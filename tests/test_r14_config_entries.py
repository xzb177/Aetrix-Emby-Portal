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
        username="r14admin",
        password_hash="x",
        is_active=True,
        is_staff=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_economy_whitelist_accepts_max_pending_orders(db, admin_user):
    from backend.api.admin_economy import (
        economy_update_settings,
        economy_get_settings,
        EconomySettingsRequest,
    )

    res = economy_update_settings(
        EconomySettingsRequest(settings={"max_pending_orders_per_user": "10"}),
        admin_user,
        db,
    )
    assert res.get("success") is True

    cfg = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == "max_pending_orders_per_user")
        .first()
    )
    assert cfg is not None
    assert cfg.value == "10"

    got = economy_get_settings(admin_user, db)
    assert got["settings"]["max_pending_orders_per_user"] == "10"


def _make_portal_app(db, admin_user):
    from backend.emby_server import portal

    app = FastAPI()
    app.include_router(portal.admin_emby_router)

    def _gd():
        yield db

    app.dependency_overrides[portal.require_staff] = lambda: admin_user
    app.dependency_overrides[portal.get_db] = _gd
    return app


def test_portal_view_unlock_get_default(db, admin_user):
    app = _make_portal_app(db, admin_user)
    client = TestClient(app)
    r = client.get("/api/admin/emby/view-unlock")
    assert r.status_code == 200
    assert r.json() == {"points": 50, "days": 365}


def test_portal_view_unlock_put_roundtrip(db, admin_user):
    app = _make_portal_app(db, admin_user)
    client = TestClient(app)

    r = client.put("/api/admin/emby/view-unlock", json={"points": 80, "days": 30})
    assert r.status_code == 200

    r = client.get("/api/admin/emby/view-unlock")
    assert r.status_code == 200
    assert r.json() == {"points": 80, "days": 30}

    r = client.put("/api/admin/emby/view-unlock", json={"points": -1, "days": 30})
    assert r.status_code == 400


def _make_panel_app(db, admin_user):
    from backend.api import panel as panel_api

    app = FastAPI()
    app.include_router(panel_api.admin_router)

    def _gd():
        yield db

    app.dependency_overrides[panel_api.get_current_admin] = lambda: admin_user
    app.dependency_overrides[panel_api.get_db] = _gd
    return app


def test_panel_pet_stages_get_default(db, admin_user):
    app = _make_panel_app(db, admin_user)
    client = TestClient(app)
    r = client.get("/api/admin/panel/pet-stages")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["stages"], list)
    assert len(body["stages"]) == 5
    assert body["stages"][0]["name"] == "蛋"


def test_panel_pet_stages_put_roundtrip(db, admin_user):
    app = _make_panel_app(db, admin_user)
    client = TestClient(app)

    payload = {
        "stages": [
            {"name": "幼苗", "icon": "🌱", "hours": 0},
            {"name": "大树", "icon": "🌳", "hours": 100},
        ]
    }
    r = client.put("/api/admin/panel/pet-stages", json=payload)
    assert r.status_code == 200

    r = client.get("/api/admin/panel/pet-stages")
    assert r.status_code == 200
    assert r.json()["stages"][1]["name"] == "大树"


def test_panel_pet_stages_put_invalid(db, admin_user):
    app = _make_panel_app(db, admin_user)
    client = TestClient(app)

    r = client.put("/api/admin/panel/pet-stages", json={"stages": []})
    assert r.status_code == 400

    r = client.put(
        "/api/admin/panel/pet-stages",
        json={"stages": [{"name": "x", "icon": "y", "hours": -5}]},
    )
    assert r.status_code == 400

"""play_start_admin 只读口 hours 钳制（后端完美审查第二轮第 8 项）。

口径（parent 已定，不再回问）：
- 上限只加在本只读口：hours > 720 按 720 算，不报错、不拒绝；
- 下限钳制保持不变：仍由 play_events.aggregate_play_start_events
  内部负责（<1 按 1 算）；
- 聚合函数语义不动、鉴权不动：本文件只测只读口，不测聚合口径
  本身（那是 test_play_start_events.py 的活）。

证明方式：窗口外事件作区分探针——一行业内事件（刚写入）加一行
800 小时前的旧事件。720 窗口应排除旧事件；若上限没钳住、
窗口被放成 10000 小时，旧事件就会被计入，测试即红。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import play_start_admin


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(play_start_admin.router)

    def _admin_super():
        return SimpleNamespace(id=1, admin_role="super", is_staff=True)

    def _db():
        yield db

    app.dependency_overrides[get_current_admin] = _admin_super
    app.dependency_overrides[get_db] = _db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed(db):
    # 1 行窗口内事件 + 1 行 800 小时前的旧事件（720 窗口外、10000 窗口内）。
    db.add(em.PlayStartEvent(provider="drive", method="relay", result="ok",
                             resolve_ms=100))
    db.add(em.PlayStartEvent(provider="drive", method="relay", result="ok",
                             resolve_ms=200,
                             created_at=datetime.now() - timedelta(hours=800)))
    db.commit()


def _total(body):
    return sum(row["total"] for row in body["by_provider"])


def test_hours_above_720_clamped_to_720_not_error(client, db):
    _seed(db)
    r = client.get("/api/admin/playback/play-start-stats",
                   params={"hours": 10000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_hours"] == 720
    # 钳到 720：800 小时前的旧事件不计入；若没钳住，total 会是 2。
    assert _total(body) == 1


def test_hours_just_above_720_clamped(client, db):
    _seed(db)
    r = client.get("/api/admin/playback/play-start-stats",
                   params={"hours": 721})
    assert r.status_code == 200, r.text
    assert r.json()["window_hours"] == 720
    assert _total(r.json()) == 1


def test_hours_at_720_unchanged(client, db):
    _seed(db)
    r = client.get("/api/admin/playback/play-start-stats",
                   params={"hours": 720})
    assert r.status_code == 200, r.text
    assert r.json()["window_hours"] == 720
    assert _total(r.json()) == 1


def test_hours_normal_range_unchanged(client, db):
    _seed(db)
    r = client.get("/api/admin/playback/play-start-stats",
                   params={"hours": 24})
    assert r.status_code == 200, r.text
    assert r.json()["window_hours"] == 24
    assert _total(r.json()) == 1


def test_hours_lower_bound_unchanged(client, db):
    # 下限行为不变：0 与负数仍按 1 算、不报错（聚合函数内部钳制）。
    _seed(db)
    for hours in (0, -5):
        r = client.get("/api/admin/playback/play-start-stats",
                       params={"hours": hours})
        assert r.status_code == 200, r.text
        assert r.json()["window_hours"] == 1
        assert _total(r.json()) == 1

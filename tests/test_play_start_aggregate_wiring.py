"""FMBY #2 闭环接线：聚合读口必须有真实生产调用方。

2026-10-27 亲验现状：aggregate_play_start_events 只有定义与单元测试，
生产代码零调用方（play_events.py 模块说明自己写着“管理端页面挂靠
留后续”）。本文件钉死接线本身，不重复测聚合口径（那是
test_play_start_events.py 的活）：

- GET /api/admin/playback/play-start-stats 经 TestClient 真走路由，
  返回的正是聚合函数的 by_provider / by_error_kind；
- hours 参数透传给聚合函数（窗口外事件不计入）；
- 非管理员（无鉴权依赖覆盖）进不来：未覆盖 get_current_admin 时
  应 401，而不是静默放行；
- 路由挂在主 app 上：main.py 的路由表里有这条路（不只孤立 router
  对象存在）。
"""
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
    db.add_all([
        em.PlayStartEvent(provider="drive", method="relay", result="ok",
                          resolve_ms=100),
        em.PlayStartEvent(provider="drive", method="direct_302", result="ok",
                          resolve_ms=300),
        em.PlayStartEvent(provider="drive", method="relay", result="error",
                          error_kind="upstream_403", resolve_ms=50),
    ])
    db.commit()


def test_play_start_stats_route_returns_aggregate(client, db):
    _seed(db)
    r = client.get("/api/admin/playback/play-start-stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_hours"] == 24
    by_key = {(row["provider"], row["mount_id"]): row
              for row in body["by_provider"]}
    drive = by_key[("drive", None)]
    assert drive["total"] == 3
    assert drive["errors"] == 1
    assert drive["avg_resolve_ms"] == 150.0
    assert drive["max_resolve_ms"] == 300
    kinds = {row["error_kind"]: row["total"] for row in body["by_error_kind"]}
    assert kinds == {"upstream_403": 1}


def test_play_start_stats_hours_window_passed_through(client, db):
    _seed(db)
    r = client.get("/api/admin/playback/play-start-stats", params={"hours": 1})
    assert r.status_code == 200, r.text
    assert r.json()["window_hours"] == 1
    # 窗口内事件仍在（刚写入）；窗口口径本身由聚合函数负责，本处只钉
    # “参数真的到了聚合函数、不是路由写死的 24”。
    assert r.json()["by_provider"][0]["total"] == 3


def test_play_start_stats_requires_admin(db):
    # 不覆盖 get_current_admin：无 token 应被鉴权依赖挡下，不是 200。
    app = FastAPI()
    app.include_router(play_start_admin.router)

    def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/admin/playback/play-start-stats")
    assert r.status_code == 401, r.text
    app.dependency_overrides.clear()


def test_play_start_stats_mounted_on_main_app():
    # 只读 main.py 的路由挂载声明，不启动整个 app：声明在即接线在。
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    main_src = (repo_root / "backend/main.py").read_text(encoding="utf-8")
    assert "play_start_admin.router" in main_src
    assert "from backend.emby_server import play_start_admin" in main_src


def test_aggregate_has_production_caller_besides_tests():
    # 防回归：聚合函数除自身模块与测试外，必须至少有一个生产调用方。
    import pathlib
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    callers = []
    for path in (repo_root / "backend").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if "aggregate_play_start_events" in src and path.name != "play_events.py":
            callers.append(str(path))
    assert callers, "aggregate_play_start_events 仍无生产调用方"
    assert any("play_start_admin" in c for c in callers)

"""种子角色模板管理端调用口（闭环 #11）。

只测调用口本身，不测判定口径（判定已在 test_path_auth_policies.py 钉死）
与种子函数本身（幂等已在 test_path_auth_policies.py 钉死）：
- handler 直调：仿照 test_path_auth_wiring.py 的 import 端点 harness
 （桩 admin=1、桩 request、sqlite db），一次调用建齐三档种子、
  二次调用结果一致且不产生重复行。
- 路由已注册：POST /api/admin/playback/stream-path-policies/seed-templates
  在 playback_admin.router 上存在。

不改前端、不新增 env 键、不改鉴权口径（与 import 端点同款 Depends）。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import playback_admin as pa_admin
from backend.emby_server import models as em
from backend.emby_server import path_auth as pa


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _fake_request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"), headers={})


def test_admin_seed_templates_endpoint_creates_all_seeds(db):
    got = pa_admin.seed_stream_path_templates(
        request=_fake_request(), current_admin=1, db=db)
    assert got["success"] is True
    assert set(got["templates"]) == set(pa.SEED_TEMPLATES)
    assert set(got["templates"]) == {"User", "RestrictedUser", "Admin"}
    rows = db.query(em.RoleTemplate).all()
    assert {r.name for r in rows} == {"User", "RestrictedUser", "Admin"}
    assert all(r.is_seed for r in rows)
    # 返回的 id 与落库行的 id 一一对应
    assert got["templates"] == {r.name: r.id for r in rows}


def test_admin_seed_templates_endpoint_idempotent(db):
    first = pa_admin.seed_stream_path_templates(
        request=_fake_request(), current_admin=1, db=db)
    second = pa_admin.seed_stream_path_templates(
        request=_fake_request(), current_admin=1, db=db)
    assert first == second
    assert db.query(em.RoleTemplate).count() == 3


def test_admin_seed_templates_route_registered():
    routes = [(r.path, sorted(getattr(r, "methods", set()) or []))
              for r in pa_admin.router.routes]
    assert ("/api/admin/playback/stream-path-policies/seed-templates",
            ["POST"]) in routes

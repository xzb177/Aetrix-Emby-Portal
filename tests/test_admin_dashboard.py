"""GET /api/admin/dashboard/overview 仪表盘聚合接口。

- 8 个首屏 part 一次返回：overview / playback / economy / libraries /
  servers / realms / backend_services / trend。
- 口径断言：每个 part 与直接调用原 handler 的结果逐字段相等
  （失败时同样为 None——与前端原 soft() 兜底一致）。
- 降级：单个采集抛异常时，该 part 为 None，其余不受影响。
- 缓存：20s TTL 内二次调用命中缓存。

全部在隔离临时 SQLite 里跑，不连生产库、不碰网络。
"""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend import models as base_models
from backend.api import admin_dashboard
from backend.api import admin as admin_mod
from backend.database import SessionLocal, init_db

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(base_models.WebUser).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture()
def admin(db):
    u = base_models.WebUser(username="dashadmin", is_staff=True, is_active=True,
                            password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _direct_parts(admin, days=14):
    """单线程直接调用 8 个原 handler，失败记 None（与聚合的降级口径对齐）。

    调用清单从 admin_dashboard._JOBS 派生：_JOBS 增减 part 时这里自动跟进；
    8 个 part 名的 tripwire 断言见 test_collect_matches_direct_handlers。
    """
    from backend.api.admin import get_playback_stats, get_stats_overview
    from backend.api.admin_economy import economy_stats, get_stats_trend
    from backend.api.admin_services import get_services_status
    from backend.api.realms import realms_overview
    from backend.api.servers import servers_summary
    from backend.emby_server.portal import list_libraries

    db = SessionLocal()
    builders = {
        "get_stats_overview": lambda: get_stats_overview(admin, db),
        "get_playback_stats": lambda: get_playback_stats(admin, db),
        "economy_stats": lambda: economy_stats(admin, db),
        "list_libraries": lambda: list_libraries(admin, db),
        "servers_summary": lambda: servers_summary(admin, db),
        "realms_overview": lambda: realms_overview(admin, db),
        "get_services_status": lambda: get_services_status(admin, db),
        "get_stats_trend": lambda: get_stats_trend(days, admin, db),
    }
    try:
        out = {}
        for key, name in admin_dashboard._JOBS:
            fn = builders[name]  # 未知 builder 名直接 KeyError，不静默成 None
            try:
                out[key] = fn()
            except Exception:  # noqa: BLE001
                out[key] = None
        return out
    finally:
        db.close()


def _strip_ts(obj):
    """递归去掉 timestamp 墙钟字段（每次调用必然不同，无业务语义）。"""
    if isinstance(obj, dict):
        return {k: _strip_ts(v) for k, v in obj.items() if k != "timestamp"}
    if isinstance(obj, list):
        return [_strip_ts(v) for v in obj]
    return obj


def test_collect_matches_direct_handlers(admin):
    """聚合 part 与原 handler 结果逐字段相等（字段一字不变）。"""
    # 先清缓存，避免其它用例的残留
    admin_dashboard._dash_cache.clear()
    got = admin_dashboard._collect(admin.id, 14)
    want = _direct_parts(admin, 14)
    assert set(got.keys()) == {
        "overview", "playback", "economy", "libraries",
        "servers", "realms", "backend_services", "trend",
    }
    for key in want:
        if key == "backend_services":
            assert _strip_ts(got[key]) == _strip_ts(want[key]), \
                f"part {key} 与原接口响应不一致"
        else:
            assert got[key] == want[key], f"part {key} 与原接口响应不一致"


def test_single_part_failure_isolated(admin, monkeypatch):
    """单个采集抛异常 → 该 part 为 None，其余不受影响。"""
    admin_dashboard._dash_cache.clear()

    def _boom(admin, db):
        raise RuntimeError("boom")

    monkeypatch.setattr(admin_mod, "get_playback_stats", _boom)
    got = admin_dashboard._collect(admin.id, 14)
    assert got["playback"] is None
    assert got["overview"] is not None
    assert got["libraries"] is not None


def test_call_one_unknown_admin_returns_none():
    key, payload = admin_dashboard._call_one("overview", lambda a, d: {}, 999999)
    assert key == "overview"
    assert payload is None


def test_cached_collect_hits_within_ttl(admin):
    admin_dashboard._dash_cache.clear()
    first = admin_dashboard._cached_collect(admin.id, 14)
    second = admin_dashboard._cached_collect(admin.id, 14)
    assert first is second


def test_endpoint_shape(admin):
    """端点函数返回 success/parts 骨架；days 钳制 1–90。"""
    admin_dashboard._dash_cache.clear()
    resp = admin_dashboard.dashboard_overview(
        days=14, current_admin=SimpleNamespace(id=admin.id))
    assert resp["success"] is True
    assert "fetched_at" in resp
    assert set(resp["parts"].keys()) == {
        "overview", "playback", "economy", "libraries",
        "servers", "realms", "backend_services", "trend",
    }
    # days 越界被钳制，不抛错
    admin_dashboard._dash_cache.clear()
    resp2 = admin_dashboard.dashboard_overview(
        days=999, current_admin=SimpleNamespace(id=admin.id))
    assert resp2["success"] is True


@pytest.fixture()
def dash_client(admin):
    """真实 app 的 TestClient：鉴权保持真实依赖（不覆盖 get_current_admin），
    get_db 指向隔离库。"""
    from fastapi.testclient import TestClient

    from backend.database import get_db
    from backend.main import app

    app.dependency_overrides[get_db] = lambda: SessionLocal()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def test_dashboard_overview_requires_auth(dash_client):
    """无鉴权头 → 401/403（与 8 个原接口中最严格者一致）。"""
    r = dash_client.get("/api/admin/dashboard/overview")
    assert r.status_code in (401, 403)


def test_dashboard_days_clamped_to_90_over_http(admin, dash_client):
    """days=999 → trend 按 90 天计算（trend payload 自带 days 字段）。"""
    from backend.api.admin_core import get_current_admin
    from backend.main import app

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        admin_dashboard._dash_cache.clear()
        r = dash_client.get("/api/admin/dashboard/overview", params={"days": 999})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        trend = body["parts"]["trend"]
        assert trend is not None
        assert trend["days"] == 90
        assert len(trend["series"]) == 90
    finally:
        del app.dependency_overrides[get_current_admin]


def test_stats_overview_total_items_excludes_merged_copies(admin, db):
    """总条目数只数可见行：合并副本不得计入。

    回归 2026-10-21 系统清点：get_stats_overview 用 count_visible 无过滤，
    生产显示 208,804 vs 真实 176,555，多算 32,249 条 merged_into_id 非空
    的合并副本（与 portal.admin_overview 的 176,555 自相矛盾）。
    """
    import uuid
    from datetime import datetime

    from backend.api.admin import get_stats_overview
    from backend.emby_server import models as em

    db.query(em.MediaItem).delete()
    db.commit()

    def _mk(**kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=1, item_type="movie",
            name="清点", file_path="/tmp/x.mp4", enrich_status="done", **kw)
        db.add(it)
        db.commit()
        return it

    _mk()                                   # 可见行：计入
    _mk(merged_into_id=999)                 # 合并副本：不计
    _mk(deleted_at=datetime.now())          # 软删行：不计
    out = get_stats_overview(admin, db)
    assert out["emby"]["total_items"] == 1

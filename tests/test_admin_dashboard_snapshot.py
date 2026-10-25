"""仪表盘定时快照（FMBY 批一 #9）：trend / detail 两路重聚合改读快照。

口径钉死：
- 快照缺失 → 同步算一次（与原 handler 逐字段一致）并落盘，记录带
  computed_at 快照时间；按 (part, days) 分键；
- 新鲜期内（<= REFRESH）读路径只读快照，不再调用 builder；
- 陈旧未过 MAX_STALE：先用旧快照（不等待），刷新后换新值；
- 陈旧过 MAX_STALE：同步重算；重算失败且有旧快照 → 先用旧快照，
  没有旧快照 → 照旧降级为 None（_call_one 口径不变）；
- 其余 part 不受快照影响（playback 抛错仍为 None）。

全部在隔离临时 SQLite 里跑；快照落点走 CacheManager 内存回退
（REDIS_ENABLED=false），不连真 Redis。
"""
import os
import tempfile
import time

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
from backend.api import admin_economy as economy_mod
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
    u = base_models.WebUser(username="snapadmin", is_staff=True, is_active=True,
                            password_hash="test")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture(autouse=True)
def _clean_snapshots():
    admin_dashboard._reset_snapshots()
    admin_dashboard._dash_cache.clear()
    yield
    admin_dashboard._reset_snapshots()
    admin_dashboard._dash_cache.clear()


def _backdate(key: str, seconds: float):
    rec = admin_dashboard._snap_load(key)
    assert rec is not None
    admin_dashboard._snap_save(key, rec["payload"],
                               computed_at=time.time() - seconds)


def test_miss_computes_live_and_stores_with_timestamp(admin, db):
    got = admin_dashboard._collect(admin.id, 14)
    assert got["trend"] == economy_mod.get_stats_trend(14, admin, db)
    assert got["detail"] == admin_dashboard.get_detail_tables(admin, db)

    for key in ("trend:14", "detail:0"):
        rec = admin_dashboard._snap_load(key)
        assert rec is not None, f"快照 {key} 应已落盘"
        assert abs(time.time() - rec["computed_at"]) < 60
    assert set(admin_dashboard._snap_registry) == {"trend:14", "detail:0"}


def test_fresh_snapshot_served_without_calling_builders(admin, monkeypatch):
    first = admin_dashboard._collect(admin.id, 14)

    def _boom(*a, **k):
        raise RuntimeError("fresh snapshot must not call builders")

    monkeypatch.setattr(economy_mod, "get_stats_trend", _boom)
    monkeypatch.setattr(admin_dashboard, "get_detail_tables", _boom)
    second = admin_dashboard._collect(admin.id, 14)
    assert second["trend"] == first["trend"]
    assert second["detail"] == first["detail"]
    assert second["overview"] is not None  # 其余 part 照常现算


def test_missing_snapshot_builder_failure_degrades_to_none(admin, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(economy_mod, "get_stats_trend", _boom)
    got = admin_dashboard._collect(admin.id, 14)
    assert got["trend"] is None
    assert got["detail"] is not None


def test_stale_within_max_serves_old_until_refresh(admin, monkeypatch):
    first = admin_dashboard._collect(admin.id, 14)
    sentinel = {"days": 14, "series": [], "totals": {}, "sentinel": True}
    monkeypatch.setattr(economy_mod, "get_stats_trend",
                        lambda days, a, d: sentinel)
    # 陈旧：过了新鲜期（120s）、远未过最大陈旧（900s）
    _backdate("trend:14", admin_dashboard._SNAPSHOT_REFRESH_SEC + 30)

    got = admin_dashboard._collect(admin.id, 14)
    assert got["trend"] == first["trend"]  # 先用旧快照，不等待刷新

    admin_dashboard._refresh_snapshot_now("trend:14")
    got2 = admin_dashboard._collect(admin.id, 14)
    assert got2["trend"] == sentinel


def test_stale_beyond_max_recomputes_synchronously(admin, monkeypatch):
    admin_dashboard._collect(admin.id, 14)
    sentinel = {"days": 14, "series": [], "totals": {}, "sentinel": True}
    monkeypatch.setattr(economy_mod, "get_stats_trend",
                        lambda days, a, d: sentinel)
    _backdate("trend:14", admin_dashboard._SNAPSHOT_MAX_STALE_SEC + 60)

    got = admin_dashboard._collect(admin.id, 14)
    assert got["trend"] == sentinel


def test_recompute_failure_falls_back_to_old_snapshot(admin, monkeypatch):
    first = admin_dashboard._collect(admin.id, 14)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(economy_mod, "get_stats_trend", _boom)
    _backdate("trend:14", admin_dashboard._SNAPSHOT_MAX_STALE_SEC + 60)
    got = admin_dashboard._collect(admin.id, 14)
    assert got["trend"] == first["trend"]  # 同步重算失败 → 旧快照兜底，不空白


def test_snapshot_keyed_by_days(admin, monkeypatch):
    calls = {"n": 0}
    real = economy_mod.get_stats_trend

    def _counting(days, a, d):
        calls["n"] += 1
        return real(days, a, d)

    monkeypatch.setattr(economy_mod, "get_stats_trend", _counting)
    admin_dashboard._collect(admin.id, 14)
    admin_dashboard._collect(admin.id, 14)
    assert calls["n"] == 1  # 第二次命中 trend:14 快照
    got = admin_dashboard._collect(admin.id, 30)
    assert calls["n"] == 2  # days=30 是另一把键，现算一次
    assert got["trend"]["days"] == 30

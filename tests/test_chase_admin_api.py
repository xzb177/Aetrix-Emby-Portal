"""GET /scrape/chase-new 的扩展字段（P1-3 管理后台可见性）。

直接调 get_chase_new(staff=..., db=...)（绕过 FastAPI Depends），断言返回里
多了 drive_changes / recent_runs / alerts / total_found，且原有配置字段不变。

全部用内存 SQLite，不碰网络与生产库。
"""
import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import admin_scrape
from backend.emby_server import models as em
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _run(db, **kw):
    base = dict(started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
                source="poll", libs_checked=1, files_listed=10,
                new_found=3, scans_triggered=1, status="ok", error="")
    base.update(kw)
    db.add(em.ChaseRun(**base))
    db.commit()


def test_chase_new_api_extended_fields(db):
    """有运行历史与告警源时，各扩展字段齐全且口径正确。"""
    _run(db, source="poll", new_found=3)
    _run(db, source="drive-changes", new_found=5, status="partial")
    db.add(em.ChaseSourceState(source_key="mount://3", consec_failures=3,
                               last_error="timeout"))
    db.add(em.ChaseSourceState(source_key="mount://9", consec_failures=1,
                               last_error="flaky"))
    db.commit()

    res = admin_scrape.get_chase_new(staff=SimpleNamespace(), db=db)

    assert res["success"] is True
    # 原有字段还在
    for k in ("enabled", "interval", "excluded", "libraries",
              "last_check", "last_found"):
        assert k in res, k
    # drive_changes 状态（heartbeat_source：心跳来源 local/shared/none，
    # 面板跨进程修复新增，API/面板读取时忽略多余字段即可）
    dc = res["drive_changes"]
    assert set(dc) == {"running", "last_poll", "last_changes",
                       "last_libs_triggered", "heartbeat_source"}
    assert dc["heartbeat_source"] == "none"  # 测试里无心跳、无线程
    assert dc["running"] is False  # 测试里没有守护线程在跑
    # 最近轮次：倒序、10 条上限内
    runs = res["recent_runs"]
    assert len(runs) == 2
    assert runs[0]["source"] == "drive-changes"
    assert runs[0]["new_found"] == 5
    assert runs[0]["status"] == "partial"
    assert set(runs[0]) == {"id", "started_at", "finished_at", "source",
                            "libs_checked", "files_listed", "new_found",
                            "scans_triggered", "status", "error"}
    # 告警：只收 consec_failures>=3 的
    alerts = res["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["source_key"] == "mount://3"
    assert alerts[0]["consec_failures"] == 3
    # 累计
    assert res["total_found"] == 8


def test_chase_new_api_empty_history(db):
    """空库时扩展字段给空值，不能 500。"""
    res = admin_scrape.get_chase_new(staff=SimpleNamespace(), db=db)

    assert res["success"] is True
    assert res["recent_runs"] == []
    assert res["alerts"] == []
    assert res["total_found"] == 0
    assert res["drive_changes"]["running"] is False

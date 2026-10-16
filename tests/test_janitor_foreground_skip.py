"""维护周期：前台活跃时缩略图补生成让路（任务留到下个周期，不丢）。"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import database as _database  # noqa: E402
from backend import models as _core_models  # noqa: E402,F401 — 注册共享 Base 全部表
from backend.emby_server import foreground_activity as fa  # noqa: E402
from backend.emby_server import image_store  # noqa: E402
from backend.emby_server import maintenance as maint  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from emby_api.asgi_middleware import ForegroundActivityMiddleware  # noqa: E402


@pytest.fixture()
def mem_db(monkeypatch):
    engine = create_engine("sqlite://")
    em.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    # janitor_tick 在函数内 from backend.database import SessionLocal
    monkeypatch.setattr(_database, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()
        fa.reset_for_tests()


class TestMiddlewareNotes:
    def test_emby_path_noted(self):
        async def app(scope, receive, send):
            pass

        fa.reset_for_tests()
        mw = ForegroundActivityMiddleware(app)
        asyncio.run(mw({"type": "http", "path": "/emby/Items/123"}, None, None))
        assert fa.active(60) is True
        fa.reset_for_tests()

    def test_health_path_not_noted(self):
        async def app(scope, receive, send):
            pass

        fa.reset_for_tests()
        mw = ForegroundActivityMiddleware(app)
        asyncio.run(mw({"type": "http", "path": "/api/health"}, None, None))
        assert fa.active(60) is False

    def test_non_http_passthrough(self):
        called = []

        async def app(scope, receive, send):
            called.append(1)

        fa.reset_for_tests()
        mw = ForegroundActivityMiddleware(app)
        asyncio.run(mw({"type": "websocket", "path": "/emby/x"}, None, None))
        assert called == [1]
        assert fa.active(60) is False


class TestJanitorSkipsBackfill:
    def test_skipped_when_foreground_active(self, mem_db, monkeypatch, caplog):
        calls = []
        monkeypatch.setattr(
            image_store, "backfill_thumbnails",
            lambda db, **kw: (calls.append(1), {"generated": 0, "done": True, "last_id": 0})[1],
        )
        fa.note()
        with caplog.at_level("INFO", logger="backend.emby_server.maintenance"):
            result = maint.janitor_tick()
        assert result.get("thumbs_backfill_skipped_active") is True
        assert calls == [], "前台活跃时不得跑缩略图补生成"
        assert any("前台" in r.message and "跳过" in r.message for r in caplog.records), \
            "日志必须能看出因前台活跃跳过"

    def test_runs_when_idle(self, mem_db, monkeypatch):
        calls = []
        monkeypatch.setattr(
            image_store, "backfill_thumbnails",
            lambda db, **kw: (calls.append(1), {"generated": 0, "done": True, "last_id": 0})[1],
        )
        fa.reset_for_tests()
        result = maint.janitor_tick()
        assert result.get("thumbs_backfill_skipped_active") is not True
        assert len(calls) == 1, "空闲时补生成必须正常跑"

"""启动维护的扫描收尾三件套只许扫描属主进程执行（scan_owner 门槛）。

## 背景（2026-10-22 开服夜事故）

run_startup_maintenance 有三个调用方：backend/main.py（api 角色已整体跳过）、
backend/worker.py（扫描属主）、emby_api/main.py（EA 播放前台，**原先无门槛**）。
每次服务重启，EA 先跑一遍三件套把被打断的库续上，十几秒后 worker 启动再跑
一遍：它看不见 EA 刚续上的扫描（跨进程），把那一轮当残留收尾掐掉、再续一遍
——每次重启成对产生两条 trigger=resume 流水（当晚 14 条、全成对）。库 44 的
巨型扫描因此永远跑不完，并占死串行队列，21:22 之后全站零扫描成功。

## 口径

- scan_owner=False（EA）：三件套（reset_stale_scan_flags /
  close_stale_scan_runs / resume_interrupted_scans）一次都不许调；
  其余启动维护（会话收尾 / facets / 转码与缓存清理）照常。
- scan_owner=True（默认，worker / 单体 main）：行为不变，三件套照跑。
"""
import importlib
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402


from backend.database import init_db  # noqa: E402


import pytest  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _hygiene_module_engine():
    """测试卫生：本文件独占临时库在模块夹具内换绑、模块结束原样还原。

    导入期顶层换绑会让全局 engine/SessionLocal 在 collection 后停在本文件
    的临时库上，后面的文件/用例撞 no such table 或读错库（#1668 同族）；
    换绑只在本模块用例期间生效。
    """
    _hyg_saved_engine = _dbmod.engine
    _hyg_saved_factory = _dbmod.SessionLocal._factory
    _hyg_saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = "sqlite:///" + _tmp
    _dbmod.engine = _ce(os.environ["DATABASE_URL"])
    _dbmod.configure_session_local(_sm(bind=_dbmod.engine))
    init_db()
    _hyg_engine = _dbmod.engine
    try:
        yield
    finally:
        _dbmod.engine = _hyg_saved_engine
        _dbmod.configure_session_local(_hyg_saved_factory)
        if _hyg_saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _hyg_saved_url
        _hyg_engine.dispose()

def _mt():
    return importlib.import_module("backend.emby_server.maintenance")


class _Query:
    def filter(self, *a, **k):
        return self

    def all(self):
        return []


class _DummySession:
    def query(self, *a, **k):
        return _Query()

    def rollback(self):
        pass

    def close(self):
        pass


class _FacetsStub:
    def ensure_backfill(self, db):
        return 0

    def prune_orphans(self, db):
        return 0

    def ready(self, db):
        return True


@pytest.fixture()
def rig(monkeypatch):
    mt = _mt()
    calls = {"reset": 0, "close": 0, "resume": 0}

    def _count(name, ret):
        def _fn(*a, **k):
            calls[name] += 1
            return ret
        return _fn

    monkeypatch.setattr(mt, "reset_stale_scan_flags", _count("reset", 0))
    monkeypatch.setattr(mt, "close_stale_scan_runs", _count("close", 0))
    monkeypatch.setattr(mt, "resume_interrupted_scans", _count("resume", 0))
    monkeypatch.setattr(mt, "reap_stale_playback_sessions", lambda *a, **k: 0)
    monkeypatch.setattr(mt, "facets", _FacetsStub())
    monkeypatch.setattr(mt, "prune_subtitle_cache", lambda *a, **k: 0)
    monkeypatch.setattr(_dbmod, "SessionLocal", lambda: _DummySession())
    import backend.emby_server.tmdb_cache as _tc
    monkeypatch.setattr(_tc, "prune", lambda *a, **k: {})
    return mt, calls


def test_non_owner_skips_scan_trio(rig):
    mt, calls = rig
    result = mt.run_startup_maintenance(scan_owner=False)
    assert calls == {"reset": 0, "close": 0, "resume": 0}
    assert result["scan_flags_reset"] == 0
    assert result["scan_runs_closed"] == 0
    assert result["scan_resumed"] == 0
    # 非扫描部分照常完成（facets 就绪位来自桩）
    assert result["item_facets_ready"] is True


def test_owner_runs_scan_trio(rig):
    mt, calls = rig
    mt.run_startup_maintenance(scan_owner=True)
    assert calls == {"reset": 1, "close": 1, "resume": 1}


def test_default_is_owner(rig):
    mt, calls = rig
    mt.run_startup_maintenance()
    assert calls == {"reset": 1, "close": 1, "resume": 1}

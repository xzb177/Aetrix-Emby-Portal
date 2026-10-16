"""扫描卡死状态必须能自动复位。

## 生产事故

部署打断扫描后，两个库永远停在 `is_scanning=1 / scan_status='running'`，
后台刷新一直显示「扫描中」。

## 根因

`reset_stale_scan_flags` 判定「这轮扫描是什么时候开始的」用的是
``lib.updated_at``——但进度刷盘 ``scan_queue.flush_once`` 每几秒写一次
``scan_progress``，ORM 的 ``onupdate`` 会连带刷新 ``updated_at``。于是它永远是
「刚刚」，超时判定恒不成立，**卡死状态自我维持**，永远复位不了。

修法（v2，单节点）：worker 启动时调用复位，那时本进程不可能有正在跑的扫描，
is_scanning=True 的一律是上一个被 SIGTERM 杀掉的进程的残留，全部复位——
不再做「开始时间 vs 阈值」判断（多机保护不需要）。
``scan_started_at`` 保留为「本轮扫描何时开始」的权威字段（不再被进度刷盘刷新）。
"""
import importlib
import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402

from backend.emby_server import models as em  # noqa: E402


def _mt():
    return importlib.import_module("backend.emby_server.maintenance")


@pytest.fixture()
def db():
    from backend import models as base_models
    engine = _ce("sqlite:///:memory:")
    original = _dbmod.SessionLocal
    _dbmod.configure_session_local(_sm(bind=engine))
    base_models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    session = _dbmod.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        _dbmod.configure_session_local(original)
        engine.dispose()


def _stuck(db, started_hours_ago, updated_now=False):
    lib = em.Library(guid="L" * 32, name="库", collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    lib.is_scanning = True
    lib.scan_status = "running"
    lib.scan_started_at = datetime.now() - timedelta(hours=started_hours_ago)
    lib.updated_at = datetime.now() if updated_now else datetime.now() - timedelta(
        hours=started_hours_ago)
    db.commit()
    return lib


def test_resets_when_started_long_ago_even_if_updated_now(db, monkeypatch):
    """核心回归：updated_at 一直被刷新，也不能阻止复位"""
    mt = _mt()
    monkeypatch.setattr("backend.emby_server.scanner.is_scan_active", lambda i: False)
    _stuck(db, 10, updated_now=True)  # 扫描 10 小时前开始，但 updated_at 是刚刚
    n = mt.reset_stale_scan_flags(db, stale_hours=6)
    db.commit()
    assert n == 1, "updated_at 刷新的卡死库必须被复位"


def test_resets_recent_scan_too(db, monkeypatch):
    """v2：刚开始不久的也复位——单节点 + 启动时调用，本进程不可能在扫"""
    mt = _mt()
    monkeypatch.setattr("backend.emby_server.scanner.is_scan_active", lambda i: False)
    lib = _stuck(db, 1)
    assert mt.reset_stale_scan_flags(db, stale_hours=6) == 1
    db.commit()
    assert lib.is_scanning is False
    # 被重启打断记「已中断」，不是失败（失败牌误报的根源）
    assert lib.scan_status == "interrupted"


def test_skip_active_scan_in_this_process(db, monkeypatch):
    """本进程真的在扫的库：绝不碰"""
    mt = _mt()
    monkeypatch.setattr("backend.emby_server.scanner.is_scan_active", lambda i: True)
    _stuck(db, 20)
    assert mt.reset_stale_scan_flags(db, stale_hours=6) == 0


def test_legacy_row_without_started_at(db, monkeypatch):
    """老数据没有 scan_started_at：退回 updated_at，至少能复位"""
    mt = _mt()
    monkeypatch.setattr("backend.emby_server.scanner.is_scan_active", lambda i: False)
    lib = em.Library(guid="M" * 32, name="老库", collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    lib.is_scanning = True
    lib.scan_status = "running"
    lib.scan_started_at = None
    lib.updated_at = datetime.now() - timedelta(hours=20)
    db.commit()
    assert mt.reset_stale_scan_flags(db, stale_hours=6) == 1
    db.commit()
    assert lib.is_scanning is False
    assert lib.scan_status == "interrupted"

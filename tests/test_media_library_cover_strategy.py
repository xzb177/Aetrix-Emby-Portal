"""媒体库设置：图片＋顺序合并——库封面策略管理端读写。

隔离临时 SQLite，不连生产库。策略来源口径：env（文件配置，优先）
→ 管理端保存的 system_configs → 默认 borrow,render,svg；语义与
默认值与原 env-only 行为逐字一致（未知步骤丢弃、重复去重、跑空
回默认）。借代表开关仍是文件配置（只读上报，不由管理端改）。
"""

import json
import os
import tempfile

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
from fastapi import HTTPException

from backend.api.admin_artwork import (
    CoverStrategyIn,
    get_cover_strategy,
    set_cover_strategy,
)
from backend.database import SessionLocal, init_db
from backend.emby_server import library_covers
from backend.emby_server import models as em
from backend.models import SystemConfig

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (SystemConfig, em.MediaItem, em.Library):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        # L65：teardown 必须与 setup 清同一批表，否则测试残留会漏进下一个文件
        # （#1287 的 system_configs 行泄漏即由此而来）。
        try:
            for table in (SystemConfig, em.MediaItem, em.Library):
                s.query(table).delete()
            s.commit()
        except Exception:
            s.rollback()
        finally:
            s.close()


def _store(db, order):
    row = SystemConfig(
        key=library_covers.COVER_STRATEGY_KEY,
        value=json.dumps(order),
        description="test",
    )
    db.add(row)
    db.commit()


def test_effective_prefers_env_over_stored(db, monkeypatch):
    """env 已设置时优先于管理端保存（文件配置优先，保存暂不生效）。"""
    monkeypatch.setenv("LIBRARY_COVER_STRATEGY_ORDER", "svg,render")
    _store(db, ["render", "borrow"])
    assert library_covers.env_strategy_order() == ["svg", "render"]
    assert library_covers.stored_strategy_order(db) == ["render", "borrow"]
    assert library_covers.effective_strategy_order(db) == ["svg", "render"]


def test_effective_uses_stored_when_env_unset(db, monkeypatch):
    """env 未设置时用管理端保存的顺序。"""
    monkeypatch.delenv("LIBRARY_COVER_STRATEGY_ORDER", raising=False)
    _store(db, ["uploaded", "svg"])
    assert library_covers.env_strategy_order() is None
    assert library_covers.effective_strategy_order(db) == ["uploaded", "svg"]


def test_effective_falls_back_to_default(db, monkeypatch):
    """两边都未设置时回默认 borrow,render,svg（原默认值逐字不变）。"""
    monkeypatch.delenv("LIBRARY_COVER_STRATEGY_ORDER", raising=False)
    assert library_covers.effective_strategy_order(db) == ["borrow", "render", "svg"]
    assert library_covers.strategy_order() == ["borrow", "render", "svg"]


def test_stored_ignores_unknown_and_dedupes(db, monkeypatch):
    """保存值里的未知步骤丢弃、重复去重，与 env 解析同口径。"""
    monkeypatch.delenv("LIBRARY_COVER_STRATEGY_ORDER", raising=False)
    _store(db, ["borrow", "nope", "borrow", "svg"])
    assert library_covers.stored_strategy_order(db) == ["borrow", "svg"]


def test_stored_invalid_returns_none(db, monkeypatch):
    """非法保存值（非 JSON / 非数组 / 空数组）一律视为未存，回默认。"""
    monkeypatch.delenv("LIBRARY_COVER_STRATEGY_ORDER", raising=False)
    for bad in ("not-json", '{"a": 1}', "[]"):
        row = SystemConfig(key=library_covers.COVER_STRATEGY_KEY, value=bad)
        db.add(row)
        db.commit()
        assert library_covers.stored_strategy_order(db) is None
        assert library_covers.effective_strategy_order(db) == ["borrow", "render", "svg"]
        db.delete(row)
        db.commit()


def test_put_rejects_unknown_and_empty(db):
    """PUT 校验：未知步骤 400、空表 400（至少保留一步）。"""
    with pytest.raises(HTTPException) as exc:
        set_cover_strategy(CoverStrategyIn(order=["borrow", "nope"]), db)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        set_cover_strategy(CoverStrategyIn(order=[]), db)
    assert exc.value.status_code == 400


def test_put_dedupes_and_persists(db, monkeypatch):
    """PUT 去重后落库；GET 回显保存值与生效顺序、开关只读上报。"""
    monkeypatch.delenv("LIBRARY_COVER_STRATEGY_ORDER", raising=False)
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 0)
    out = set_cover_strategy(CoverStrategyIn(order=["svg", "render", "svg"]), db)
    assert out["order"] == ["svg", "render"]
    assert out["effective_order"] == ["svg", "render"]
    state = get_cover_strategy(db)
    assert state["stored_order"] == ["svg", "render"]
    assert state["env_order"] is None
    assert state["effective_order"] == ["svg", "render"]
    assert state["borrow_enabled"] is False
    assert state["known_steps"] == ["borrow", "render", "uploaded", "svg"]


def test_resolve_uses_stored_order(db, monkeypatch):
    """保存的顺序真实驱动出图：svg 在前则命中占位，render 在前则命中渲染。"""
    monkeypatch.delenv("LIBRARY_COVER_STRATEGY_ORDER", raising=False)
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 1)
    lib = em.Library(
        name="库", guid=f"lib-{os.urandom(4).hex()}",
        cover_path="library-covers/x.webp", cover_template="poster",
    )
    db.add(lib)
    db.commit()
    db.refresh(lib)
    _store(db, ["svg", "render"])
    assert library_covers.resolve_library_cover(db, lib, "poster")["source"] == "svg"
    db.query(SystemConfig).delete()
    db.commit()
    _store(db, ["render", "svg"])
    assert library_covers.resolve_library_cover(db, lib, "poster")["source"] == "render"

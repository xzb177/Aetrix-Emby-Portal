# -*- coding: utf-8 -*-
"""N9-1：直出模式开关地基（Nova 学习立项 N9 第一单，零行为变化）。

钉死口径（本文件即契约）：
- 每库开关 ``direct_play`` 存媒体库记录（Library.direct_play，Boolean），
  缺省关＝现行行为逐字不变；老库由 _auto_migrate 补列为 0（幂等）。
- 管理口（portal）create/update/回显三处带上 direct_play：create 缺省
  关、显式开落开；update 不传＝不修改，传了才翻转；回显与存值一致。
- 解析单点在 playback_strategy.library_direct_play_enabled：开/关/库行
  不存在/值为 NULL/查询异常 五态里只有「存值 True」回 True，其余一律
  False（fail-closed 回现行主线）。
- 本单零行为变化：解析单点不在任何播放路径被调用（api.py 源码守卫），
  取流口接线是 N9-2 的事。
- 拍板口径（2026-11-05 用户点选）：直出只对 STRM 里的普通外链生效、
  带凭据的外链维持中继、远程挂载维持中继——那是 N9-2 的门控语义，
  本单只立开关。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import playback_strategy, portal
from backend.emby_server import models as em


# ---------------------------------------------------------------------------
# 模型缺省
# ---------------------------------------------------------------------------

def test_library_direct_play_defaults_off(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/n91.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        lib = em.Library(guid=uuid.uuid4().hex, name="直出测试库",
                         collection_type="movies", is_enabled=True)
        db.add(lib)
        db.commit()
        db.refresh(lib)
        # 缺省关：不显式开启就是现行行为
        assert lib.direct_play is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# _auto_migrate 补列（老库升级口径，幂等）
# ---------------------------------------------------------------------------

def test_auto_migrate_adds_direct_play_column_idempotent():
    """老表缺 direct_play 列时补上且默认 0（关）；重复跑不报错、不重加。"""
    from backend import database as dbmod

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE emby_libraries (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO emby_libraries (id) VALUES (1)"))
    original_engine = dbmod.engine
    dbmod.engine = engine
    try:
        dbmod._auto_migrate()
        cols = {c["name"] for c in inspect(engine).get_columns("emby_libraries")}
        assert "direct_play" in cols, "老库升级后必须补上 direct_play 列"
        with engine.connect() as conn:
            val = conn.execute(
                text("SELECT direct_play FROM emby_libraries WHERE id = 1")
            ).scalar()
        assert val in (0, False), "老库补列后必须为关（与升级前行为一致）"
        # 幂等：再跑一次不报错、不重复加列
        dbmod._auto_migrate()
        cols2 = {c["name"] for c in inspect(engine).get_columns("emby_libraries")}
        assert cols2 == cols
    finally:
        dbmod.engine = original_engine
        engine.dispose()


# ---------------------------------------------------------------------------
# 解析单点五态（fail-closed）
# ---------------------------------------------------------------------------

@pytest.fixture()
def libs_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/n91r.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib_on = em.Library(guid=uuid.uuid4().hex, name="开库",
                        collection_type="movies", is_enabled=True,
                        direct_play=True)
    lib_off = em.Library(guid=uuid.uuid4().hex, name="关库",
                         collection_type="movies", is_enabled=True,
                         direct_play=False)
    lib_null = em.Library(guid=uuid.uuid4().hex, name="空库",
                          collection_type="movies", is_enabled=True)
    db.add_all([lib_on, lib_off, lib_null])
    db.flush()
    lib_null.direct_play = None  # 模拟老行 NULL
    db.commit()
    yield db, lib_on, lib_off, lib_null
    db.close()


def test_resolver_only_true_when_stored_true(libs_db):
    db, lib_on, lib_off, lib_null = libs_db
    assert playback_strategy.library_direct_play_enabled(db, lib_on.id) is True
    assert playback_strategy.library_direct_play_enabled(db, lib_off.id) is False
    # NULL（老行没值）按关处理
    assert playback_strategy.library_direct_play_enabled(db, lib_null.id) is False
    # 库行不存在 → 关
    assert playback_strategy.library_direct_play_enabled(db, 999999) is False
    # library_id 缺失 → 关
    assert playback_strategy.library_direct_play_enabled(db, None) is False


def test_resolver_fail_closed_on_error(libs_db):
    db, lib_on, _off, _null = libs_db

    class _Boom:
        def query(self, *args, **kwargs):
            raise RuntimeError("db down")

    # 查询异常 → 关（回现行主线），不许把异常抛进播放路径
    assert playback_strategy.library_direct_play_enabled(_Boom(), lib_on.id) is False
    assert playback_strategy.library_direct_play_enabled(None, lib_on.id) is False


# ---------------------------------------------------------------------------
# 管理口 create / update / 回显往返
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    from backend.integrations import store

    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    staff = base_models.WebUser(username="boss", password_hash="x",
                                is_staff=True)
    session.add(staff)
    session.commit()

    def _get_db():
        yield session

    app = FastAPI()
    app.include_router(portal.admin_emby_router)
    app.dependency_overrides[portal.require_staff] = lambda: staff
    app.dependency_overrides[portal.get_db] = _get_db
    store.invalidate()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    with TestClient(app) as c:
        yield c, session, str(media_dir)
    store.invalidate()
    session.close()


def _echo_row(client, lib_id):
    rows = client.get("/api/admin/emby/libraries").json()["libraries"]
    return next(r for r in rows if r["id"] == lib_id)


def test_api_create_update_echo_roundtrip(client):
    c, db, media_dir = client
    # create 显式开
    res = c.post("/api/admin/emby/libraries", json={
        "name": "直出库", "collection_type": "movies",
        "paths": [media_dir], "direct_play": True,
    })
    assert res.status_code == 200, res.text
    lib_id = res.json()["id"]
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    assert lib.direct_play is True
    assert _echo_row(c, lib_id)["direct_play"] is True

    # create 不传 → 缺省关
    res2 = c.post("/api/admin/emby/libraries", json={
        "name": "普通库", "collection_type": "movies",
        "paths": [media_dir],
    })
    assert res2.status_code == 200, res2.text
    lib2_id = res2.json()["id"]
    lib2 = db.query(em.Library).filter(em.Library.id == lib2_id).first()
    assert lib2.direct_play is False
    assert _echo_row(c, lib2_id)["direct_play"] is False

    # update 不传 direct_play → 不修改
    res3 = c.put(f"/api/admin/emby/libraries/{lib_id}", json={"name": "直出库改名"})
    assert res3.status_code == 200, res3.text
    db.expire_all()
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    assert lib.direct_play is True

    # update 传 False → 关；回显跟上
    res4 = c.put(f"/api/admin/emby/libraries/{lib_id}",
                 json={"direct_play": False})
    assert res4.status_code == 200, res4.text
    db.expire_all()
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    assert lib.direct_play is False
    assert _echo_row(c, lib_id)["direct_play"] is False

    # update 传 True → 再开
    res5 = c.put(f"/api/admin/emby/libraries/{lib_id}",
                 json={"direct_play": True})
    assert res5.status_code == 200, res5.text
    db.expire_all()
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    assert lib.direct_play is True
    assert _echo_row(c, lib_id)["direct_play"] is True


# ---------------------------------------------------------------------------
# 零行为变化守卫：本单不许接播放路径
# ---------------------------------------------------------------------------

def test_resolver_not_wired_into_playback_paths():
    """N9-1 只立开关：解析单点不得出现在取流/播放路径源码里（N9-2 才接）。"""
    import re

    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    for rel in ("emby_server/api.py", "emby_server/upstream_playback.py",
                "emby_server/streaming.py", "emby_server/mounts.py"):
        src = (backend_dir / rel).read_text(encoding="utf-8")
        assert "library_direct_play_enabled" not in src, rel
        # 独立词 direct_play 不许出现（drive_direct_play_enabled 是 Drive
        # 既有开关，不在此列——它前接 drive_，(?<!\w) 不会误伤）
        assert re.search(r"(?<!\w)direct_play\b", src) is None, rel

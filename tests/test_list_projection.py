"""媒体库列表投影读模型（FMBY 列表投影读模型，清单第 16 项试点）。

钉死的范围（多一件不做）：
- 试点只切 user_views（客户端媒体库列表页）：条目数/最早入库/封面 tag
  三值走投影表；排序与分页语义与现行逐字一致（Views 本身无分页参数，
  返回全部启用库；顺序 = 库查询顺序，测试钉死）。
- 刷新 = 周期重建（工人），试点期零写路径改动；刷新失败/缺行/过期/
  开关关/身份门开时整包回落现算，绝不静默出旧数。
- 不碰扫描、入库、绑定、播放变体、Items 浏览端点。

全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""

import logging
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import api as emby_api
from backend.emby_server import identity_gate
from backend.emby_server import list_projection as lp
from backend.emby_server import models as em
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 配置热缓存是进程级的：进出都清一次，避免污染别的内存库测试。
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


@pytest.fixture(autouse=True)
def _projection_on(monkeypatch):
    """默认开投影（模块常量 monkeypatch，不动 env）。"""
    monkeypatch.setattr(lp, "ENABLED", 1)
    yield


def _user(db, username="alice", **kw):
    row = models.WebUser(username=username, password_hash="x", **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _library(db, name, **kw):
    row = em.Library(guid=f"guid-{name}", name=name, **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib, name, item_type, date_added, **kw):
    row = em.MediaItem(
        guid=f"item-{lib.id}-{name}",
        library_id=lib.id,
        item_type=item_type,
        name=name,
        date_added=date_added,
        **kw,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _scene(db):
    """两启用库（一有 created_at、一无）+ 一停用库 + 一虚拟库。

    A 库：created_at 为空（走最早入库回退）、item_count 为空（走实时计数）。
    B 库：created_at 已设、item_count 已设（走缓存列分支）。
    每库 2 部 movie + 1 部 series + 1 集 episode（集不计入 ChildCount）。
    """
    user = _user(db)
    # 注意：Library.item_count 列默认 0，且 ORM 在值为 None 时仍触发默认值，
    # 构造参数传 None 拿不到 NULL；A 库要覆盖“实时计数”分支，必须用裸 SQL
    # 把列写回 NULL（对应现行口径里“扫描未完成/失败过”的库）。
    lib_a = _library(db, "A", is_enabled=True)
    from sqlalchemy import text as _text

    # created_at 同理有默认值：要覆盖“最早入库回退”分支，裸 SQL 一并写 NULL。
    db.execute(_text("UPDATE emby_libraries SET item_count=NULL, created_at=NULL "
                     "WHERE id=:id"), {"id": lib_a.id})
    db.commit()
    db.expire_all()
    lib_b = _library(db, "B", is_enabled=True, created_at=datetime(2021, 5, 4, 3, 2, 1))
    lib_b.item_count = 7
    db.commit()
    _library(db, "停用", is_enabled=False)
    vlib = _library(db, "虚拟", is_enabled=True, is_virtual=True, platform="")
    for lib in (lib_a, lib_b):
        _item(db, lib, "m1", "movie", datetime(2022, 1, 10))
        _item(db, lib, "m2", "movie", datetime(2022, 1, 5))
        _item(db, lib, "s1", "series", datetime(2022, 1, 20))
        _item(db, lib, "e1", "episode", datetime(2022, 1, 1))
    return user, lib_a, lib_b, vlib


def _views(db, user):
    return emby_api.user_views("me", user=user, db=db)


def _by_name(payload):
    return {entry["Name"]: entry for entry in payload["Items"]}


def test_projection_matches_live_full_payload(db, monkeypatch):
    """投影开与关，Views 整包逐字段一致（顺序、计数、日期、Etag、封面）。"""
    user, lib_a, lib_b, _vlib = _scene(db)
    lp.rebuild_projection(db)
    db.commit()

    projected = _views(db, user)
    by_name = _by_name(projected)
    assert set(by_name) == {"A", "B", "虚拟"}

    # A：实时计数分支——2 movie + 1 series = 3（episode 不计）；最早入库回退。
    assert by_name["A"]["ChildCount"] == 3
    assert by_name["A"]["DateCreated"] == "2022-01-05T00:00:00Z"
    # B：缓存列分支——item_count=7；created_at 已设则不用最早入库。
    assert by_name["B"]["ChildCount"] == 7
    assert by_name["B"]["DateCreated"] == "2021-05-04T03:02:01Z"
    # 虚拟库走现行聚合口径（platform 为空 → 0），不受投影影响。
    assert by_name["虚拟"]["ChildCount"] == 0

    # 关投影重算：整包必须逐字段一致（含 Etag 与顺序）。
    monkeypatch.setattr(lp, "ENABLED", 0)
    live = _views(db, user)
    assert [e["Name"] for e in live["Items"]] == [e["Name"] for e in projected["Items"]]
    live_by_name = _by_name(live)
    for name, entry in by_name.items():
        assert live_by_name[name] == entry, f"{name} 投影与现算不一致"


def test_projection_path_does_not_call_live_helpers(db, monkeypatch):
    """路径证明：投影开时，非虚拟库不调用现算 helper（打补丁令其抛错）。

    本用例不用虚拟库：虚拟库在投影模式下仍走现算 helper（设计如此）。
    """
    user = _user(db)
    lib_a = _library(db, "A", is_enabled=True)
    _library(db, "B", is_enabled=True)
    _item(db, lib_a, "m1", "movie", datetime(2022, 1, 10))
    lp.rebuild_projection(db)
    db.commit()

    def _boom(*args, **kwargs):
        raise AssertionError("现算 helper 不应在投影路径被调用")

    monkeypatch.setattr(emby_api, "_library_item_count", _boom)
    monkeypatch.setattr(emby_api, "_library_cover_tag", _boom)
    payload = _views(db, user)
    assert {e["Name"] for e in payload["Items"]} == {"A", "B"}


def test_missing_row_falls_back_to_live(db, caplog):
    """缺行 → 整包回落现算并记警告，数字与现算一致。"""
    user, lib_a, _b, _v = _scene(db)
    lp.rebuild_projection(db)
    db.commit()
    db.query(em.LibraryListProjection).filter(
        em.LibraryListProjection.library_id == lib_a.id).delete()
    db.commit()
    with caplog.at_level(logging.WARNING):
        payload = _views(db, user)
    assert _by_name(payload)["A"]["ChildCount"] == 3
    assert any("回落现算" in rec.message for rec in caplog.records)


def test_stale_row_falls_back_to_live(db, caplog):
    """过期行 → 整包回落现算并记警告（延迟可观测、不假装实时）。"""
    user, _a, _b, _v = _scene(db)
    lp.rebuild_projection(db)
    db.commit()
    stale_at = datetime.now() - timedelta(seconds=lp.MAX_STALE_SEC + 60)
    db.query(em.LibraryListProjection).update({"computed_at": stale_at})
    db.commit()
    with caplog.at_level(logging.WARNING):
        payload = _views(db, user)
    assert _by_name(payload)["A"]["ChildCount"] == 3
    assert any("回落现算" in rec.message for rec in caplog.records)


def test_switch_off_ignores_projection(db, monkeypatch):
    """开关关 → 投影行写错数也不用，现算为准。"""
    user, _a, _b, _v = _scene(db)
    lp.rebuild_projection(db)
    db.query(em.LibraryListProjection).update({"child_count": 9999})
    db.commit()
    monkeypatch.setattr(lp, "ENABLED", 0)
    assert _by_name(_views(db, user))["A"]["ChildCount"] == 3


def test_gate_on_ignores_projection(db, monkeypatch):
    """身份门开 → 投影行不用，现行身份过滤计数为准（无绑定条目 → 0）。"""
    user, _a, _b, _v = _scene(db)
    lp.rebuild_projection(db)
    db.query(em.LibraryListProjection).update({"child_count": 9999})
    db.commit()
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 1)
    assert _by_name(_views(db, user))["A"]["ChildCount"] == 0


def test_rebuild_upserts_and_cleans(db):
    """重建幂等：只给启用非虚拟库建行；停用/虚拟库的行被清理。"""
    _user(db)
    lib_a = _library(db, "A", is_enabled=True)
    lib_b = _library(db, "B", is_enabled=True)
    _library(db, "虚拟", is_enabled=True, is_virtual=True, platform="")
    stats = lp.rebuild_projection(db)
    db.commit()
    assert stats["libraries"] == 2
    assert {r.library_id for r in db.query(em.LibraryListProjection).all()} == {
        lib_a.id, lib_b.id}

    lib_b.is_enabled = False
    db.commit()
    stats = lp.rebuild_projection(db)
    db.commit()
    assert stats["deleted"] == 1
    assert {r.library_id for r in db.query(em.LibraryListProjection).all()} == {lib_a.id}


def test_refresh_now_stats_and_worker_gating(db, monkeypatch):
    """refresh_now 回统计；工人开关关不启动、开时幂等单线程。"""
    _user(db)
    _library(db, "A", is_enabled=True)
    stats = lp.refresh_now(lambda: db)
    assert stats is not None and stats["libraries"] == 1

    monkeypatch.setattr(lp, "ENABLED", 0)
    assert lp.start_projection_worker() is False

    monkeypatch.setattr(lp, "ENABLED", 1)
    import backend.database as _dbmod

    def _boom_factory():
        raise RuntimeError("测试内不许碰真实 SessionLocal")

    monkeypatch.setattr(_dbmod, "SessionLocal", _boom_factory)
    stop = threading.Event()
    assert lp.start_projection_worker(stop) is True
    first = lp._worker_thread
    assert lp.start_projection_worker(stop) is True
    assert lp._worker_thread is first
    stop.set()
    first.join(timeout=10)
    assert not first.is_alive()

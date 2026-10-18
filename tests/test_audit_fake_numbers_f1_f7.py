"""系统清点 Batch 1：假数字 F1–F7 回归测试。

每条测试都在库里同时放「可见行 + 合并副本行 + 软删行」，断言统计接口
只数可见行。可见口径：deleted_at IS NULL（软删钩子）AND merged_into_id IS NULL。

全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _guid(prefix):
    return f"audit-f1f7-{prefix}-{uuid.uuid4().hex[:8]}"


def _library(db, name, realm_id=None):
    lib = em.Library(guid=_guid(f"lib-{name}"), name=name, realm_id=realm_id)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib=None, **kw):
    is_deleted = kw.get("deleted_at") is not None
    row = em.MediaItem(
        guid=_guid(kw.get("name", "item")),
        library_id=lib.id if lib is not None else kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试条目"),
        **kw,
    )
    db.add(row)
    db.commit()
    # 软删行不能 refresh/碰属性：软删钩子会给 SELECT 加上
    # deleted_at IS NULL，行就找不回来了（钩子正常工作）
    if not is_deleted:
        db.refresh(row)
    return row


def _merged_copy(db, visible, **kw):
    """合并副本：merged_into_id 非空的行"""
    return _item(db, merged_into_id=visible.id, **kw)


def _soft_deleted(db, lib=None, **kw):
    """软删行：deleted_at 非空的行"""
    return _item(db, lib, deleted_at=datetime.now(), **kw)


def _staff(db, username="auditstaff"):
    u = models.WebUser(username=username, password_hash="x", is_active=True,
                       is_staff=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ==================== F1：片库生态只数可见 ====================

def test_panel_eco_counts_visible_only(db):
    from backend.api import panel as panel_api
    lib = _library(db, "生态库")
    user = _staff(db)
    vis = _item(db, lib, name="可见电影", item_type="movie",
                date_added=datetime.now())
    _item(db, lib, name="可见剧", item_type="series", date_added=datetime.now())
    _merged_copy(db, vis, name="副本电影", item_type="movie",
                 date_added=datetime.now())
    _soft_deleted(db, lib, name="已删电影", item_type="movie",
                  date_added=datetime.now())

    payload = panel_api.panel_server(db=db, user=user)["data"]
    eco = {e["library_id"]: e["count"] for e in payload["eco"]}
    assert eco[lib.id] == 2, f"应只数 1 电影+1 剧，实际 {eco[lib.id]}"


# ==================== F2：生长记录只数可见 ====================

def test_panel_growth_counts_visible_only(db):
    from backend.api import panel as panel_api
    lib = _library(db, "生长库")
    user = _staff(db)
    now = datetime.now()
    vis = _item(db, lib, name="可见新片", item_type="movie", date_added=now)
    _merged_copy(db, vis, name="副本新片", item_type="movie", date_added=now)
    _soft_deleted(db, lib, name="已删新片", item_type="movie", date_added=now)

    payload = panel_api.panel_server(db=db, user=user)["data"]
    total = sum(g["count"] for g in payload["growth"])
    assert total == 1, f"7 天生长应只数 1 条可见，实际 {total}"


# ==================== F3：realms.stats 只数可见 ====================

def test_realms_stats_items_visible_only(db):
    from backend import realms
    lib1 = _library(db, "服库1", realm_id=7)
    lib2 = _library(db, "服库2", realm_id=7)
    vis = _item(db, lib1, name="可见条目1")
    _item(db, lib1, name="可见条目2")
    _item(db, lib2, name="可见条目3")
    _merged_copy(db, vis, name="副本条目")
    _soft_deleted(db, lib1, name="已删条目")

    out = realms.stats(db, 7)
    assert out["items"] == 3, f"服条目数应只数 3 条可见，实际 {out['items']}"


# ==================== F4：field_missing_stats 只数可见 ====================

def test_field_missing_stats_visible_only(db):
    from backend.emby_server import enrich_worker as ew
    _item(db, name="可见已刮剧", item_type="series", enrich_status="done",
          metadata_source="tmdb", overview="有简介", production_year=2024)
    vis = _item(db, name="母行", item_type="series", enrich_status="done",
                metadata_source="tmdb", overview="有简介")
    _merged_copy(db, vis, name="副本剧", item_type="series",
                 enrich_status="done", metadata_source="tmdb", overview="")
    _soft_deleted(db, name="已删剧", item_type="series", enrich_status="done",
                  metadata_source="tmdb", overview="")

    stats = ew.field_missing_stats(db)
    assert stats["total"] == 2, f"分母应只数 2 条可见，实际 {stats['total']}"
    # 可见的两行简介都有 → 简介缺失 0；若副本/软删被算进来会是 2
    assert stats["missing"]["overview"] == 0


# ==================== F5：none_stock_stats 只数可见 ====================

def _none_row(db, name, **kw):
    kw.setdefault("enrich_status", "done")
    kw.setdefault("metadata_source", "none")
    kw.setdefault("tmdb_id", None)
    kw.setdefault("item_type", "series")
    kw.setdefault("file_fingerprint", "fp-" + name)
    return _item(db, name=name, **kw)


def test_none_stock_stats_visible_only(db):
    from backend.emby_server import enrich_worker as ew
    old = datetime.now() - timedelta(days=31)
    vis = _none_row(db, "可见none", enrich_next_retry_at=old)
    _merged_copy(db, vis, name="副本none",
                 enrich_status="done", metadata_source="none", tmdb_id=None,
                 item_type="series", file_fingerprint="fp-x",
                 enrich_next_retry_at=old)
    _soft_deleted(db, name="已删none", enrich_status="done",
                  metadata_source="none", tmdb_id=None, item_type="series",
                  file_fingerprint="fp-y", enrich_next_retry_at=old)

    stats = ew.none_stock_stats(db)
    assert stats["total"] == 1, f"none 存量应只数 1 条可见，实际 {stats['total']}"
    assert stats["by_type"] == {"series": 1}
    # 软删行符合回捞条件但旧 Query.count() 写法会数进去（钩子挡不住子查询）；
    # 合并副本按设计永不回捞。都应被排除
    assert stats["reschedulable_now"] == 1


# ==================== F6：probe status_snapshot 只数可见 ====================

def test_probe_status_snapshot_visible_only(db):
    from backend.emby_server import probe_worker as pw
    now = datetime.now()
    _item(db, name="可见待探", item_type="movie", probe_status="pending")
    _item(db, name="可见重试", item_type="movie", probe_status="pending",
          probe_next_retry_at=now + timedelta(hours=1))
    _item(db, name="可见失败", item_type="movie", probe_status="failed",
          probe_last_error="boom")
    _item(db, name="可见卡住", item_type="movie", probe_status="probing",
          probe_claimed_at=now - timedelta(seconds=pw.PROBE_CLAIM_TTL_SEC * 2))
    vis = _item(db, name="母行探", item_type="movie", probe_status="pending")
    _merged_copy(db, vis, name="副本待探", item_type="movie",
                 probe_status="pending")
    _merged_copy(db, vis, name="副本重试", item_type="movie",
                 probe_status="pending",
                 probe_next_retry_at=now + timedelta(hours=1))
    _merged_copy(db, vis, name="副本失败", item_type="movie",
                 probe_status="failed", probe_last_error="boom")
    _soft_deleted(db, name="已删待探", item_type="movie",
                  probe_status="pending")

    snap = pw.status_snapshot(db, light=False)
    # 可见 pending：待探 + 重试 + 母行探 = 3；副本/软删的 pending 不应计入
    assert snap["counts"].get("pending") == 3, snap["counts"]
    assert snap["counts"].get("failed") == 1, snap["counts"]
    assert snap["retrying"] == 1, "重试中只应数可见"
    assert snap["stale_probing"] == 1, "卡住只应数可见"
    boom = [e for e in snap["top_errors"] if e["error"] == "boom"]
    assert boom and boom[0]["count"] == 1, "错误排行只应数可见"


# ==================== F7：health _count 只数可见 ====================

def test_health_probe_count_visible_only(db):
    from backend import health_report as hr
    _item(db, name="可见失败集", item_type="episode", probe_status="failed")
    vis = _item(db, name="母行集", item_type="episode", probe_status="failed")
    _merged_copy(db, vis, name="副本失败集", item_type="episode",
                 probe_status="failed")
    _soft_deleted(db, name="已删失败集", item_type="episode",
                  probe_status="failed")
    _item(db, name="可见失败电影", item_type="movie", probe_status="failed")

    # 可见 episode/failed：可见失败集 + 母行集 = 2；副本/软删的不应计入
    assert hr._count(db, "episode", "failed") == 2
    assert hr._count(db, "movie", "failed") == 1

"""定向扫描不误报"没有可用来源"（v2.56.x 修复）

背景：定向扫描（Drive Changes 增量 / .strm 增量触发）只看部分目录，
跳过清理阶段是预期行为。但 finish_scan() 把 removal_skipped=True 一律判为
partial，前端显示"媒体库没有可用来源，本轮已跳过清理"——明明一切正常。

本文件钉住：
1. 定向扫描 + 无真实失败 → status=success，不报"没有可用来源"；
2. 定向扫描 + 有 failed_roots → 仍记 partial（真有问题）；
3. 定向扫描 + 有 removal_guard → 仍记 partial（真有问题）；
4. 非定向扫描的 removal_skipped → 行为不变（partial + 兜底文案）；
5. _scan_metrics 对外暴露 targeted_scan。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import scanner
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _library(db, **kw):
    name = kw.pop("name", "电影库")
    lib = em.Library(guid=f"g-{name}", name=name, collection_type="movies", **kw)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def test_targeted_scan_no_failure_is_success(db):
    """定向扫描无失败 → success，不误报"""
    lib = _library(db, name="定向正常")
    scanner.finish_scan(db, lib, {
        "added": 5, "updated": 2,
        "removal_skipped": True,
        "targeted_scan": True,
        # failed_roots 和 removal_guard 都为空 = 没有真实失败
    }, None)
    db.refresh(lib)
    assert lib.scan_status == scanner.SCAN_STATUS_SUCCESS
    assert not lib.scan_error
    assert "没有可用来源" not in (lib.scan_error or "")


def test_targeted_scan_with_failed_roots_still_partial(db):
    """定向扫描但有来源失败 → 仍记 partial"""
    lib = _library(db, name="定向有失败")
    scanner.finish_scan(db, lib, {
        "added": 0,
        "removal_skipped": True,
        "targeted_scan": True,
        "failed_roots": ["/strm/电影: 挂载不可读"],
    }, None)
    db.refresh(lib)
    assert lib.scan_status == scanner.SCAN_STATUS_PARTIAL
    assert lib.scan_error == "/strm/电影: 挂载不可读"


def test_targeted_scan_with_guard_still_partial(db):
    """定向扫描但有删除保护拦截 → 仍记 partial"""
    lib = _library(db, name="定向被拦截")
    guard_msg = "本轮一个文件都没看到，但库里有 42 条记录"
    scanner.finish_scan(db, lib, {
        "added": 0,
        "removal_skipped": True,
        "targeted_scan": True,
        "removal_guard": guard_msg,
    }, None)
    db.refresh(lib)
    assert lib.scan_status == scanner.SCAN_STATUS_PARTIAL
    assert lib.scan_error == guard_msg


def test_non_targeted_skip_unchanged(db):
    """非定向扫描的 removal_skipped → 行为不变（partial + 兜底）"""
    lib = _library(db, name="非定向跳过")
    scanner.finish_scan(db, lib, {
        "added": 0,
        "removal_skipped": True,
        # 没有 targeted_scan 标记
    }, None)
    db.refresh(lib)
    assert lib.scan_status == scanner.SCAN_STATUS_PARTIAL
    assert lib.scan_error == "媒体库没有可用来源，本轮已跳过清理"


def test_scan_metrics_exposes_targeted_scan():
    """_scan_metrics 对外暴露 targeted_scan"""
    m = scanner._scan_metrics({"targeted_scan": True, "removal_skipped": True})
    assert m["targeted_scan"] is True
    assert m["removal_skipped"] is True


def test_scan_metrics_targeted_defaults_false():
    """没标记时默认 False"""
    m = scanner._scan_metrics({})
    assert m["targeted_scan"] is False
    assert "targeted_scan" in m

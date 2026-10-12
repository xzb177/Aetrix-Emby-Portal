"""扫描删除保护拦截原因展示（v2.55.x 修复）

背景：删除保护正确拦截清理时，``finish_scan()`` 组装 error 只看
``failed_roots``、无视 ``stats["removal_guard"]``，用户看到的是误导性兜底文案
"媒体库没有可用来源，本轮已跳过清理"，且状态永远 partial、前端一直显示。

本文件钉住三件事：
1. ``removal_guard`` 存在时，error 用它而不是兜底文案；
2. ``failed_roots`` 非空时仍优先（具体来源失败原因更精确）；
3. 两者都空时才用兜底文案；
4. ``_scan_metrics()`` 对外暴露 ``removal_guard``，不再是"有写入、零读取"的死字段。
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


GUARD_MSG = ("本轮一个文件都没看到，但库里有 42 条文件类条目——"
             "这正是「挂载断了底层空目录还在」的事故形状，本轮禁止清理")


def test_removal_guard_used_instead_of_fallback(db):
    """删除保护拦截时：error 用 guard 真话，不用兜底文案"""
    lib = _library(db, name="guard优先")
    scanner.finish_scan(db, lib, {
        "added": 0, "updated": 0,
        "removal_skipped": True,
        "removal_guard": GUARD_MSG,
    }, None)
    db.refresh(lib)
    assert lib.scan_status == scanner.SCAN_STATUS_PARTIAL
    assert lib.scan_error == GUARD_MSG
    assert "没有可用来源" not in (lib.scan_error or "")


def test_failed_roots_still_take_priority(db):
    """failed_roots 非空时仍优先——具体来源失败原因比 guard 更精确"""
    lib = _library(db, name="roots优先")
    scanner.finish_scan(db, lib, {
        "added": 0,
        "removal_skipped": True,
        "removal_guard": GUARD_MSG,
        "failed_roots": ["/strm/电影: 挂载不可读"],
    }, None)
    db.refresh(lib)
    assert lib.scan_error == "/strm/电影: 挂载不可读"
    assert GUARD_MSG not in (lib.scan_error or "")


def test_fallback_only_when_nothing_else(db):
    """两者都空时才用兜底文案（行为不变）"""
    lib = _library(db, name="兜底")
    scanner.finish_scan(db, lib, {"added": 0, "removal_skipped": True}, None)
    db.refresh(lib)
    assert lib.scan_error == "媒体库没有可用来源，本轮已跳过清理"


def test_success_has_no_error(db):
    """正常扫完：error 为空，不受影响"""
    lib = _library(db, name="正常")
    scanner.finish_scan(db, lib, {"added": 3}, None)
    db.refresh(lib)
    assert lib.scan_status == scanner.SCAN_STATUS_SUCCESS
    assert not lib.scan_error


def test_scan_metrics_exposes_removal_guard():
    """_scan_metrics 对外暴露 removal_guard，不再是死字段"""
    m = scanner._scan_metrics({"removal_guard": GUARD_MSG, "removal_skipped": True})
    assert m["removal_guard"] == GUARD_MSG
    assert m["removal_skipped"] is True


def test_scan_metrics_guard_defaults_to_empty():
    """没 guard 时默认空字符串（前端/接口不用判 None）"""
    m = scanner._scan_metrics({})
    assert m["removal_guard"] == ""
    assert "removal_guard" in m


def test_scan_result_payload_carries_guard(db):
    """scan_result_payload（管理端/客户端共用口径）带出 guard"""
    lib = _library(db, name="payload")
    scanner.finish_scan(db, lib, {
        "added": 0, "removal_skipped": True, "removal_guard": GUARD_MSG,
    }, None)
    db.refresh(lib)
    payload = scanner.scan_result_payload(lib)
    assert payload is not None
    assert payload["removal_guard"] == GUARD_MSG
    assert payload["error"] == GUARD_MSG


def test_guard_message_truncated_at_500(db):
    """超长 guard 按 500 截断（与 failed_roots 同口径）"""
    lib = _library(db, name="截断")
    long_guard = "原" * 600
    scanner.finish_scan(db, lib, {
        "added": 0, "removal_skipped": True, "removal_guard": long_guard,
    }, None)
    db.refresh(lib)
    assert lib.scan_error == "原" * 500

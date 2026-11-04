# -*- coding: utf-8 -*-
"""CMS 库扫描状态口径回归（2026-11-04 截图点名误报）。

生产现象：无界 9 个库（58–66）长期 scan_status=partial，管理端
驾驶舱「扫描状态」卡显示「11 正常/异常 9」。根因有二：

1. ``applecms_library_sync._sync_source`` 把**已停用源**记进
   ``stats["errors"]``（``source_disabled:<id>``），``_write_progress``
   见 errors 非空就落 partial——库里只要挂着一个停用源（例如已定案
   停用的源 11），整库永远 partial、永远「异常」。
2. 状态落定只看 errors 有无：启用源全部真失败、整轮零产出时也落
   partial，而不是 failed。

定案口径（本文件钉住）：
- 已停用源不进 errors，另记 ``skipped_disabled``（含写回 scan_stats
  的 cms_sync 明细），不影响状态；
- 全部启用源完成＝success；≥1 启用源真失败但同步有产出＝partial；
  有真失败且整轮零产出＝failed；一个启用源都没有（全停用）＝failed
  （整轮等于没同步，与修复前该边界的落定一致）；
- 本地库扫描状态逻辑不在本文件范围（scanner.py 另一套，不受影响）。
"""

from __future__ import annotations

import json
import urllib.parse
import zlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import models as em


CLASS_TREE = [
    {"type_id": 1, "type_pid": 0, "type_name": "电影"},
    {"type_id": 2, "type_pid": 1, "type_name": "动作片"},
]


def _entry(vod_id, name, *, year="2021"):
    return {
        "vod_id": vod_id, "vod_name": name, "vod_year": year,
        "type_id": 2, "type_name": "动作片", "vod_serial": "0",
        "vod_time": "2024-05-01 10:00:00",
        "vod_pic": "http://img.example/x.jpg",
        "vod_content": "简介文本", "vod_class": "动作",
        "vod_douban_id": str(
            100000 + zlib.crc32(f"{name}|{year}".encode()) % 900000),
    }


OK_PAGE = {
    "code": 1, "page": 1, "pagecount": 1, "limit": 20, "total": 1,
    "class": CLASS_TREE,
    "list": [_entry(101, "沙丘")],
}


class FakeHttpGet:
    """s7 主机正常回一页；s9 主机抛异常（fetcher 归类 network）。"""

    def __call__(self, url, headers=None, timeout=None):
        host = urllib.parse.urlparse(url).netloc
        if host == "s9.example":
            raise RuntimeError("connection refused")
        assert host == "s7.example", host
        return 200, json.loads(json.dumps(OK_PAGE, ensure_ascii=False))


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db, sources, source_ids):
    """sources: [(id, host, enabled)]；source_ids: 库引用的源 id 串。"""
    for sid, host, enabled in sources:
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=enabled,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-status", name="CMS电影",
        collection_type="movies", is_enabled=True,
        kind="applecms", upstream_source_ids=source_ids,
        cms_type_map=json.dumps({"电影": "in"}, ensure_ascii=False))
    db.add(lib)
    db.commit()
    return lib


def test_disabled_source_is_skipped_not_error_and_status_success(db):
    """启用源全成功＋挂一个已停用源 → success，停用源只记 skipped。"""
    lib = _seed(db, [(7, "s7.example", True), (8, "s8.example", False)],
                "7,8")
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["errors"] == []
    assert stats["skipped_disabled"] == [8]
    assert stats["pages"] == 1
    db.refresh(lib)
    assert lib.scan_status == "success"
    saved = json.loads(lib.scan_stats)
    assert saved["cms_sync"]["errors"] == []
    assert saved["cms_sync"]["skipped_disabled"] == [8]


def test_enabled_source_failure_with_output_is_partial(db):
    """一个启用源正常产出、一个启用源真失败（network）→ partial。"""
    lib = _seed(db, [(7, "s7.example", True), (9, "s9.example", True)],
                "7,9")
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["errors"] == ["source_error:9:network"]
    assert stats["skipped_disabled"] == []
    assert stats["pages"] == 1
    db.refresh(lib)
    assert lib.scan_status == "partial"


def test_all_enabled_sources_fail_no_output_is_failed(db):
    """唯一的启用源真失败、整轮零产出 → failed（不是 partial）。"""
    lib = _seed(db, [(9, "s9.example", True)], "9")
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["errors"] == ["source_error:9:network"]
    assert stats["pages"] == 0
    db.refresh(lib)
    assert lib.scan_status == "failed"


def test_all_sources_disabled_is_failed(db):
    """库里只有已停用源：一个启用源都没跑成，整轮等于没同步 → failed，
    且停用源仍只记 skipped_disabled、不进 errors。"""
    lib = _seed(db, [(8, "s8.example", False)], "8")
    stats = als.sync_library(db, lib, http_get=FakeHttpGet())
    assert stats["errors"] == []
    assert stats["skipped_disabled"] == [8]
    db.refresh(lib)
    assert lib.scan_status == "failed"

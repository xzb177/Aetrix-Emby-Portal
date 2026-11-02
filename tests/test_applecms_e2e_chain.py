# -*- coding: utf-8 -*-
"""苹果 CMS 库端到端桩链（新媒体库类型一期·验收衔接单）。

把一期的四段接成一条链一次验穿（全部桩件、无真实网络）：
建库（kind=applecms）→ 同步工人灌托管行 → 身份（同步后无身份
不上架；模拟 enrich 补上 TMDB 身份后上架）→ mapped 取流口点播
（detail 现取 + 探活：403 软放行照常出地址、404 判死）。
生产真机点穿（播放器里真出画）仍欠，见一期汇报口径。
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import identity_gate
from backend.emby_server import models as em
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_playback as up


CLASS_TREE = [{"type_id": 1, "type_pid": 0, "type_name": "电影"},
              {"type_id": 2, "type_pid": 1, "type_name": "动作片"}]


def _entry(vod_id, name, year="2021"):
    return {"vod_id": vod_id, "vod_name": name, "vod_year": year,
            "type_id": 2, "type_name": "动作片", "vod_serial": "0",
            "vod_time": "2024-05-01 10:00:00"}


PAGES = {
    "s7.example": {"code": 1, "page": 1, "pagecount": 1, "total": 2,
                   "class": CLASS_TREE,
                   "list": [_entry(101, "沙丘"), _entry(102, "无名之辈", "2018")]},
    "s8.example": {"code": 1, "page": 1, "pagecount": 1, "total": 1,
                   "class": CLASS_TREE, "list": [_entry("55", "沙丘")]},
}

DETAIL = {"vod_id": 101, "vod_name": "沙丘",
          "vod_play_from": "线路A",
          "vod_play_url": "正片$https://cdn.example.com/dune.mp4"}


def _list_get(url, headers=None, timeout=None):
    host = urllib.parse.urlparse(url).netloc
    return 200, PAGES[host]


def _detail_get(url, headers=None, timeout=None):
    return 200, {"code": 1, "list": [DETAIL]}


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db):
    for sid, host in ((7, "s7.example"), (8, "s8.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(
        guid="lib-cms-movies", name="CMS电影", collection_type="movies",
        is_enabled=True, kind="applecms", upstream_source_ids="7,8")
    db.add(lib)
    db.commit()
    return lib


def test_full_chain_sync_identity_playback(db):
    lib = _seed(db)
    # 1) 同步：3 条目录 → 2 行（沙丘跨源去重）+ 3 映射
    stats = als.sync_library(db, lib, http_get=_list_get)
    assert stats["added"] == 2 and stats["mapped"] == 3
    dune = (db.query(em.MediaItem)
            .filter(em.MediaItem.name == "沙丘").one())
    anon = (db.query(em.MediaItem)
            .filter(em.MediaItem.name == "无名之辈").one())
    # 2) 身份：同步后两行都没身份 → 货架判据不上架（用户口径：
    #    不豁免，没身份就等 TMDB 补上）
    assert identity_gate.is_bound_row(dune) is False
    assert identity_gate.is_bound_row(anon) is False
    # 模拟 enrich 补身份的结果（enrich 对 upstream 行自动走 TMDB
    # 搜索补 tmdb_id，这一步在生产由 enrich 工人完成）
    dune.tmdb_id = "438631"
    db.commit()
    assert identity_gate.is_bound_row(dune) is True
    assert identity_gate.is_bound_row(anon) is False
    # 3) 点播：映射定位 (源， vod_id) → detail 现取 → 探活 403
    #    软放行照常出地址（播放单口径）
    mapping = uim.get_best_mapping(db, dune.id)
    # 同置信度时取最近更新的一条，两源任一都是有效取流口
    assert (mapping.source_id, mapping.upstream_item_id) in {
        (7, "101"), (8, "55")}
    url = up.resolve_upstream_stream(
        db, mapping.source_id, mapping.upstream_item_id,
        http_get=_detail_get, http_probe=lambda *a: (403, None))
    assert url == "https://cdn.example.com/dune.mp4"
    # 探活 200 同样出地址；404 判死结构化报错
    url2 = up.resolve_upstream_stream(
        db, mapping.source_id, mapping.upstream_item_id,
        http_get=_detail_get, http_probe=lambda *a: (200, None))
    assert url2 == url
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(
            db, mapping.source_id, mapping.upstream_item_id,
            http_get=_detail_get, http_probe=lambda *a: (404, None))
    assert exc.value.code == "upstream_item_not_found"

# -*- coding: utf-8 -*-
"""N4-3：多版本展示合并的每库开关（照 Nova mergeSame / enableMerge 两档）。

钉死口径（本文件即契约）：
- 库级两开关存媒体库记录：merge_same 缺省开（同库并组＝现行行为）、
  enable_merge 缺省关（跨库并组须显式 opt-in）；
- 开关只管展示层分组，三处生效：dedup.deduplicate_items 的分组、
  AlternateVersions 拼装（get_alternate_versions 汇合两条路）、
  详情 DTO Versions 的 _version_siblings 拼装；
- 跨库并组 fail-closed：涉及的库没全开 enable_merge 就不并；
- 开关翻转立即生效且 DB 里 merged_into_id 等物理合并数据零改动；
- 管理面（apply_display_switches=False）看物理真相，不受展示开关影响。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api, dedup, media_routes
from backend.emby_server import merge_versions_worker as mvw
from backend.emby_server import models as em


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/n43.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib_a = em.Library(guid=uuid.uuid4().hex, name="电影库A",
                       collection_type="movies", is_enabled=True)
    lib_b = em.Library(guid=uuid.uuid4().hex, name="电影库B",
                       collection_type="movies", is_enabled=True)
    db.add_all([lib_a, lib_b])
    db.flush()
    user = models.WebUser(username="n43", password_hash="x", is_active=True)
    db.add(user)
    db.flush()

    def mk(lib, name, file_path, **kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
            name=name, sort_name=name, file_path=file_path,
            is_hidden=False, **kw)
        db.add(it)
        db.flush()
        return it

    m = {"lib_a": lib_a, "lib_b": lib_b}
    # ---- dedup 场景：同库同 TMDB 两行、未被物理合并（展示层去重并组）
    m["D1"] = mk(lib_a, "去重片", "/liba/dup/Dup - 一.mkv", tmdb_id="400")
    m["D2"] = mk(lib_a, "去重片", "/liba/dup2/Dup - 二.mkv", tmdb_id="400")
    # ---- 物理合并组：主记录与同库副本在 A 库；X1 是存量跨库合并行
    # （merged_into_id 跨库指向 A 库主记录，N4-1 前的老数据形态）。
    m["P"] = mk(lib_a, "合并主", "/liba/Film/Film - 主.mkv", tmdb_id="500")
    m["A1"] = mk(lib_a, "合并同库", "/liba/Film/Film - 同库.mkv",
                 tmdb_id="500", merged_into_id=m["P"].id)
    m["X1"] = mk(lib_b, "合并跨库", "/libb/Film/Film - 跨库.mkv",
                 tmdb_id="500", merged_into_id=m["P"].id)
    # ---- 详情 Versions 场景：同目录同主文件名的兄弟版本（未合并）
    m["S1"] = mk(lib_a, "兄弟片", "/liba/Pic/Pic - 低码.mkv", bitrate=1_000_000)
    m["S2"] = mk(lib_a, "兄弟片", "/liba/Pic/Pic - 高码.mkv", bitrate=8_000_000)

    db.commit()
    data = SimpleNamespace(
        db=db, m=m, user_id=user.id,
        user=SimpleNamespace(id=user.id, is_staff=False))
    yield data
    db.close()


def _req(params=None):
    scope = {"type": "http", "method": "GET",
             "query_string": urlencode(params or {}).encode(), "headers": []}
    return Request(scope)


def _items_snapshot(db):
    return [(i.id, i.library_id, i.merged_into_id)
            for i in db.query(em.MediaItem).order_by(em.MediaItem.id).all()]


# ---------------------------------------------------------------------------
# 模型缺省与开关解析
# ---------------------------------------------------------------------------

def test_library_switch_defaults(scene):
    lib = scene.m["lib_a"]
    # merge_same 缺省开（保持现行同库行为）、enable_merge 缺省关（Nova 缺省）
    assert lib.merge_same is True
    assert lib.enable_merge is False


def test_resolve_library_switches(scene):
    db, m = scene.db, scene.m
    sw = dedup.resolve_library_switches(db, [m["lib_a"].id, m["lib_b"].id])
    assert sw[m["lib_a"].id] == (True, False)
    assert sw[m["lib_b"].id] == (True, False)
    # 查不到的库按缺省（同库开、跨库关），不许崩
    sw2 = dedup.resolve_library_switches(db, [999999])
    assert sw2[999999] == (True, False)
    # 翻转后解析立即跟上
    m["lib_b"].enable_merge = True
    db.flush()
    sw3 = dedup.resolve_library_switches(db, [m["lib_b"].id])
    assert sw3[m["lib_b"].id] == (True, True)


def test_version_group_allowed_fail_closed():
    sw = {1: (True, False), 2: (False, True), 3: (True, True)}
    # 同库：只看本库 merge_same
    assert dedup.version_group_allowed(sw, 1, 1) is True
    assert dedup.version_group_allowed(sw, 2, 2) is False
    # 跨库：两边 enable_merge 都开才并（1 号库没开 → 不并）
    assert dedup.version_group_allowed(sw, 1, 3) is False
    assert dedup.version_group_allowed(sw, 3, 1) is False
    assert dedup.version_group_allowed(sw, 2, 3) is True
    # 未知库按缺省：跨库关
    assert dedup.version_group_allowed(sw, 3, 99) is False


# ---------------------------------------------------------------------------
# ① dedup 展示分组：同库两行 1 ↔ 2
# ---------------------------------------------------------------------------

def test_dedup_same_library_switch(scene):
    db, m = scene.db, scene.m
    pair = [m["D1"], m["D2"]]
    sw = dedup.resolve_library_switches(db, [m["lib_a"].id])
    assert len(dedup.deduplicate_items(pair, library_switches=sw)) == 1
    # 关 merge_same → 立即恢复两条
    m["lib_a"].merge_same = False
    db.flush()
    sw_off = dedup.resolve_library_switches(db, [m["lib_a"].id])
    assert len(dedup.deduplicate_items(pair, library_switches=sw_off)) == 2
    # 不传开关（老调用方）行为与升级前逐字一致：仍并成一条
    assert len(dedup.deduplicate_items(pair)) == 1


# ---------------------------------------------------------------------------
# ②③ AlternateVersions：同库门控 + 跨库 fail-closed
# ---------------------------------------------------------------------------

def test_alternate_versions_same_library_gate(scene):
    db, m = scene.db, scene.m
    res = media_routes.item_alternate_versions(
        m["P"].guid, _req(), scene.user, db)
    ids = {i["Id"] for i in res["Items"]}
    # 同库副本在列；跨库行缺省不并
    assert ids == {m["P"].guid, m["A1"].guid}
    # 关同库开关 → 只剩主记录自己
    m["lib_a"].merge_same = False
    db.flush()
    res2 = media_routes.item_alternate_versions(
        m["P"].guid, _req(), scene.user, db)
    assert {i["Id"] for i in res2["Items"]} == {m["P"].guid}


def test_alternate_versions_cross_library_enable_merge(scene):
    db, m = scene.db, scene.m
    # 只开主记录所在库 → 仍不并（fail-closed）
    m["lib_a"].enable_merge = True
    db.flush()
    got = {v.id for v in mvw.get_alternate_versions(db, m["P"].id)}
    assert m["X1"].id not in got
    # 两边都开 → 并入
    m["lib_b"].enable_merge = True
    db.flush()
    got2 = {v.id for v in mvw.get_alternate_versions(db, m["P"].id)}
    assert got2 == {m["P"].id, m["A1"].id, m["X1"].id}
    # 管理面口径：不看展示开关，物理合并行全量可见
    m["lib_a"].merge_same = False
    m["lib_a"].enable_merge = False
    m["lib_b"].enable_merge = False
    db.flush()
    got3 = {v.id for v in mvw.get_alternate_versions(
        db, m["P"].id, apply_display_switches=False)}
    assert got3 == {m["P"].id, m["A1"].id, m["X1"].id}


# ---------------------------------------------------------------------------
# ③ 详情 Versions / _version_siblings：同库兄弟门控
# ---------------------------------------------------------------------------

def test_version_siblings_switch(scene):
    db, m = scene.db, scene.m
    sibs = api._version_siblings(m["S1"], db)
    assert {s.id for s in sibs} == {m["S1"].id, m["S2"].id}
    m["lib_a"].merge_same = False
    db.flush()
    assert api._version_siblings(m["S1"], db) == []


def test_detail_dto_versions_switch(scene):
    db, m = scene.db, scene.m
    dto = api._item_dto(m["S1"], "http://test", scene.user_id, db, full=True)
    assert len(dto["Versions"]) == 2
    m["lib_a"].merge_same = False
    db.flush()
    dto2 = api._item_dto(m["S1"], "http://test", scene.user_id, db, full=True)
    assert dto2["Versions"] == []


# ---------------------------------------------------------------------------
# ④ 开关翻转：DB 物理合并数据零改动
# ---------------------------------------------------------------------------

def test_switch_flip_leaves_items_untouched(scene):
    db, m = scene.db, scene.m
    before = _items_snapshot(db)
    m["lib_a"].merge_same = False
    m["lib_a"].enable_merge = True
    m["lib_b"].enable_merge = True
    db.flush()
    # 三处展示路径各跑一遍
    sw = dedup.resolve_library_switches(db, [m["lib_a"].id])
    dedup.deduplicate_items([m["D1"], m["D2"]], library_switches=sw)
    media_routes.item_alternate_versions(m["P"].guid, _req(), scene.user, db)
    api._item_dto(m["S1"], "http://test", scene.user_id, db, full=True)
    assert _items_snapshot(db) == before

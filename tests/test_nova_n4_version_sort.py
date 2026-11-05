# -*- coding: utf-8 -*-
"""N4-2：多版本展示默认排序照 Nova versionSort=bitrate（高码率在前）。

钉死口径（本文件即契约）：
- 客户端两处版本呈现——详情 DTO 的 Versions[] 与 AlternateVersions
  接口——同组版本默认按码率从高到低排，排第一条即默认版本；
- 码率口径与 MediaSource.Bitrate 同源（_effective_bitrate：探测值
  优先，缺失时 size×8÷时长推导，再缺为「无码率」）；
- 无码率垫底；同码率（含同为无码率）保持既有稳定次序（id 升序，
  即两处呈现此前显式 id 排序的次序）；
- IsPrimary 语义不变：仍标物理主记录，不随排序变。
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
from backend.emby_server import api, media_routes
from backend.emby_server import models as em


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/n42.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib = em.Library(guid=uuid.uuid4().hex, name="电影库",
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    user = models.WebUser(username="n42", password_hash="x", is_active=True)
    db.add(user)
    db.flush()

    def mk(name, file_path, **kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
            name=name, sort_name=name, file_path=file_path,
            is_hidden=False, **kw)
        db.add(it)
        db.flush()
        return it

    m = {"lib": lib}
    # ---- AlternateVersions 场景：物理合并组，故意让 id 次序与码率
    # 次序完全错位（主记录先入库、码率居中；最高码率后入库）。
    m["P"] = mk("合并主", "/liba/Film/Film - 主.mkv",
                tmdb_id="300", bitrate=4_000_000, height=1080)
    m["A"] = mk("合并高码", "/liba/Film/Film - 高码.mkv",
                tmdb_id="300", bitrate=9_000_000, height=2160,
                merged_into_id=m["P"].id)
    m["D"] = mk("合并同码", "/liba/Film/Film - 同码.mkv",
                tmdb_id="300", bitrate=9_000_000, height=2160,
                merged_into_id=m["P"].id)
    # 无探测码率但 size/时长可推导：1.8e9 B / 5400 s → 2_666_666 bps
    m["C"] = mk("合并推导", "/liba/Film/Film - 推导.mkv",
                tmdb_id="300", bitrate=0, size=1_800_000_000,
                duration_ticks=5400 * 10_000_000,
                merged_into_id=m["P"].id)
    # 彻底无码率（探测缺失、size/时长也缺）
    m["B"] = mk("合并无码", "/liba/Film/Film - 无码.mkv",
                tmdb_id="300", bitrate=0, size=0, duration_ticks=0,
                merged_into_id=m["P"].id)

    # ---- 详情 DTO Versions 场景：同目录同主名的兄弟版本（未合并）。
    m["S_LOW"] = mk("兄弟片", "/libb/Pic/Pic - 低码.mkv", bitrate=1_000_000)
    m["S_HIGH"] = mk("兄弟片", "/libb/Pic/Pic - 高码.mkv", bitrate=8_000_000)
    m["S_NONE"] = mk("兄弟片", "/libb/Pic/Pic - 无码.mkv", bitrate=0)

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


def _row(**kw):
    base = dict(bitrate=0, size=0, duration_ticks=0, id=0)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 排序口径单元（_sort_versions_by_bitrate）
# ---------------------------------------------------------------------------

def test_sort_helper_bitrate_desc_missing_last():
    low = _row(id=1, bitrate=1_000_000)
    none = _row(id=2)
    high = _row(id=3, bitrate=9_000_000)
    out = api._sort_versions_by_bitrate([low, none, high])
    assert [v.id for v in out] == [3, 1, 2]


def test_sort_helper_derived_bitrate_ranks_above_missing():
    derived = _row(id=1, size=1_800_000_000,
                   duration_ticks=5400 * 10_000_000)  # ≈2.67 Mbps
    probed = _row(id=2, bitrate=1_000_000)
    none = _row(id=3)
    out = api._sort_versions_by_bitrate([none, probed, derived])
    assert [v.id for v in out] == [1, 2, 3]


def test_sort_helper_ties_keep_input_order():
    a = _row(id=5, bitrate=5_000_000)
    b = _row(id=3, bitrate=5_000_000)
    c = _row(id=9)
    d = _row(id=7)
    # 输入已是 id 升序（两处调用点的既有基线次序）
    out = api._sort_versions_by_bitrate([b, a, d, c])
    assert [v.id for v in out] == [3, 5, 7, 9]


# ---------------------------------------------------------------------------
# AlternateVersions 接口：下发顺序＋第一条即最高码率
# ---------------------------------------------------------------------------

def test_alternate_versions_sorted_by_bitrate(scene):
    m = scene.m
    res = media_routes.item_alternate_versions(
        m["P"].guid, _req(), scene.user, scene.db)
    ids = [i["Id"] for i in res["Items"]]
    # 高码（同码两条按 id 稳定）→ 主记录 → 推导码率 → 无码率垫底
    assert ids == [m["A"].guid, m["D"].guid, m["P"].guid,
                   m["C"].guid, m["B"].guid]
    # IsPrimary 仍标物理主记录，不随排序变
    prim = [i for i in res["Items"] if i["IsPrimary"]]
    assert [i["Id"] for i in prim] == [m["P"].guid]


# ---------------------------------------------------------------------------
# 详情 DTO Versions[]：同口径排序
# ---------------------------------------------------------------------------

def test_detail_dto_versions_sorted_by_bitrate(scene):
    m = scene.m
    dto = api._item_dto(m["S_LOW"], "http://test", scene.user_id,
                        scene.db, full=True)
    names = [v["Name"] for v in dto["Versions"]]
    assert names == ["高码", "低码", "无码"]

"""身份准入货架·三期：代表与版本完善钉。

三期三项（开关 IDENTITY_SHELF_PHASE3_ENABLED，须总开关同时开启）：
1. 组内已绑定优先选主——有海报的未绑定份不得压过已绑定的成员当代表
   （设计稿 §3.2 的坑）；只改组内代表人选，不改分组结果。
2. 版本列表并入同库同身份未合并副本——主记录有 tmdb → 同 trim(tmdb)；
   只有 imdb → 同 trim(imdb)；主记录无身份 → 不扩；跨库不串。
3. 计数按代表组对齐——电影/剧集按分层身份组计数（同 tmdb 一组、
   同 imdb 一组），集与其它类型仍按行数；总数相应重组。

明确不做的：分组键三级扩展（tmdb→imdb→弱键）本批未动
``dedup_key`` / ``dedup_key_for``——分页去重层 ``_DEDUP_FIELDS`` 与
扫描防重层都要同步改键才一致，属高危热路径，另立项再做；本文件
用一则断言钉死该现状（开关开时分组键仍为旧行为），防止半吊子改动。
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
from backend.emby_server import api, dedup, identity_gate, media_routes
from backend.emby_server import models as em


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/p3.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="mixed", is_enabled=True)
    db.add(lib)
    db.flush()

    def mk(item_type, name, **kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
            name=name, sort_name=name,
            file_path=f"/m/{uuid.uuid4().hex}.mkv", **kw)
        db.add(it)
        db.flush()
        return it

    m = {"lib": lib}
    # 计数场景：两部同 tmdb（同组）、一部同名不同 tmdb（不同组）、
    # 两部同 imdb-only（一组）、一部 imdb-only 独苗
    m["T1"] = mk("movie", "同片甲份一", tmdb_id="100", production_year=2001)
    m["T2"] = mk("movie", "同片甲份二", tmdb_id="100", production_year=2001)
    m["T3"] = mk("movie", "同片甲份三", tmdb_id="101", production_year=2001)
    m["I1"] = mk("movie", "同 imdb 份一", imdb_id="tt111",
                 production_year=2002)
    m["I2"] = mk("movie", "异名同 imdb 份二", imdb_id="tt111",
                 production_year=2003)
    m["I3"] = mk("movie", "imdb 独苗", imdb_id="tt222", production_year=2004)
    m["U"] = mk("movie", "未绑定电影", production_year=2005)
    # 版本场景：主记录 tmdb 300 + 未合并同 tmdb 副本 + 已物理合并副本
    # + 跨库同 tmdb 行（不许串进来，另建库）
    m["V"] = mk("movie", "版本主", tmdb_id="300", production_year=2010)
    m["V2"] = mk("movie", "版本主未合并副本", tmdb_id="300",
                 production_year=2010)
    m["V3"] = mk("movie", "版本主已合并副本", tmdb_id="300",
                 production_year=2010, merged_into_id=None)
    db.flush()
    m["V3"].merged_into_id = m["V"].id
    lib2 = em.Library(guid=uuid.uuid4().hex, name="库二",
                      collection_type="mixed", is_enabled=True)
    db.add(lib2)
    db.flush()
    m["VX"] = mk("movie", "跨库同 tmdb", tmdb_id="300", production_year=2010)
    m["VX"].library_id = lib2.id
    # imdb-only 主记录 + 同 imdb 未合并副本
    m["W"] = mk("movie", "imdb 主", imdb_id="tt999", production_year=2011)
    m["W2"] = mk("movie", "imdb 未合并副本", imdb_id="tt999",
                 production_year=2011)
    # 无身份主记录 + 同名无身份行（不许扩）
    m["N"] = mk("movie", "无身份主", production_year=2012)
    m["N2"] = mk("movie", "无身份主", production_year=2012)
    user = models.WebUser(username="p3", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    data = SimpleNamespace(db=db, m=m,
                           user=SimpleNamespace(id=user.id, is_staff=False))
    yield data
    db.close()


def _req(params=None):
    scope = {"type": "http", "method": "GET",
             "query_string": urlencode(params or {}).encode(), "headers": []}
    return Request(scope)


def _gate3(monkeypatch, gate, phase3):
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", gate)
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_PHASE3_ENABLED", phase3)


def _row(item_type, **kw):
    row = SimpleNamespace(item_type=item_type, **kw)
    return row


def test_pick_primary_bound_first_when_on(scene, monkeypatch):
    """组内有已绑定成员时，选主只在已绑定成员里挑——有海报的未绑定
    份不得当代表；开关关时恢复旧评分（海报优先）。"""
    unbound_poster = _row("movie", tmdb_id=None, imdb_id=None,
                          poster_path="/p.jpg", primary_image_url=None,
                          overview=None, id=1)
    bound_plain = _row("movie", tmdb_id="100", imdb_id=None,
                       poster_path=None, primary_image_url=None,
                       overview=None, id=2)
    _gate3(monkeypatch, 1, 1)
    assert dedup.pick_primary([unbound_poster, bound_plain]) is bound_plain
    _gate3(monkeypatch, 1, 0)
    assert dedup.pick_primary([unbound_poster, bound_plain]) is unbound_poster


def test_pick_primary_all_unbound_unchanged(scene, monkeypatch):
    """全组无绑定时，选主仍按旧评分（不凭空变卦）。"""
    a = _row("movie", tmdb_id=None, imdb_id=None, poster_path="/p.jpg",
             primary_image_url=None, overview=None, id=1)
    b = _row("movie", tmdb_id=None, imdb_id=None, poster_path=None,
             primary_image_url=None, overview=None, id=2)
    _gate3(monkeypatch, 1, 1)
    assert dedup.pick_primary([b, a]) is a


def test_dedup_key_tiering_deferred(scene, monkeypatch):
    """分组键三级扩展本批未做：开关开时 dedup_key 仍为旧行为
    （imdb-only 行仍走名+年弱键），防止半吊子改动造成分页层与
    内存层去重口径撕裂；完整改键另立项。"""
    _gate3(monkeypatch, 1, 1)
    row = _row("movie", library_id=1, tmdb_id=None, imdb_id="tt111",
               name="异名同 imdb 份二", production_year=2003)
    key = dedup.dedup_key(row)
    assert key[2] == "name"  # 不是 "imdb"


def test_versions_expand_unmerged_same_identity(scene, monkeypatch):
    """版本列表：同库同 tmdb 未合并副本并入，已合并副本仍在，
    跨库同 tmdb 不串，主记录标记不变。"""
    _gate3(monkeypatch, 1, 1)
    res = media_routes.item_alternate_versions(
        scene.m["V"].guid, _req(), scene.user, scene.db)
    # _version_dto 的 Name 是版本标签（如 480p）不是片名，故按 Id（guid）断言
    ids = {i["Id"] for i in res["Items"]}
    assert ids == {scene.m["V"].guid, scene.m["V2"].guid, scene.m["V3"].guid}
    assert res["TotalRecordCount"] == 3
    prim = [i for i in res["Items"] if i["IsPrimary"]]
    assert len(prim) == 1 and prim[0]["Id"] == scene.m["V"].guid


def test_versions_expand_imdb_only_and_unbound_no_expand(scene, monkeypatch):
    """imdb-only 主记录按 imdb 并入；无身份主记录不扩。"""
    _gate3(monkeypatch, 1, 1)
    res = media_routes.item_alternate_versions(
        scene.m["W"].guid, _req(), scene.user, scene.db)
    assert {i["Id"] for i in res["Items"]} == {scene.m["W"].guid,
                                               scene.m["W2"].guid}
    res_n = media_routes.item_alternate_versions(
        scene.m["N"].guid, _req(), scene.user, scene.db)
    assert {i["Id"] for i in res_n["Items"]} == {scene.m["N"].guid}


def test_versions_off_keeps_merged_only(scene, monkeypatch):
    """三期开关关时，版本列表只含物理合并副本（旧行为逐字一致）。"""
    _gate3(monkeypatch, 1, 0)
    res = media_routes.item_alternate_versions(
        scene.m["V"].guid, _req(), scene.user, scene.db)
    assert {i["Id"] for i in res["Items"]} == {scene.m["V"].guid,
                                               scene.m["V3"].guid}


def test_counts_group_aligned_when_on(scene, monkeypatch):
    """计数三期对齐：同 tmdb 两行计 1 组、同 imdb 两行计 1 组、
    同片跨库计 2 组（组键含库，与列表去重键同口径）；
    关时恢复行数口径；集数与总数口径同步。"""
    _gate3(monkeypatch, 1, 1)
    res = api.items_counts(scene.user, scene.db)
    # 电影组（库1：tmdb100、tmdb101、imdb tt111、imdb tt222、tmdb300、
    # imdb tt999；库2：tmdb300）= 7 组（未绑定行被门滤掉，不计）
    assert res["MovieCount"] == 7
    assert res["EpisodeCount"] == 0
    assert res["ItemCount"] == 7
    _gate3(monkeypatch, 1, 0)
    res_off = api.items_counts(scene.user, scene.db)
    # 行数口径：已绑定电影行 = T1,T2,T3,I1,I2,I3,V,V2,V3,W,W2,VX = 12
    assert res_off["MovieCount"] == 12
    assert res_off["ItemCount"] == 12


def test_phase3_on_without_main_gate_changes_nothing(scene, monkeypatch):
    """只开三期、不开总开关：回滚完整性——计数与版本列表皆旧口径。"""
    _gate3(monkeypatch, 0, 1)
    assert identity_gate.phase3_enabled() is False
    res = api.items_counts(scene.user, scene.db)
    # 总开关关 = 门全关：已绑定 12 行 + 未绑定 U、N、N2 3 行 = 15 行
    assert res["MovieCount"] == 15
    res_v = media_routes.item_alternate_versions(
        scene.m["V"].guid, _req(), scene.user, scene.db)
    assert {i["Id"] for i in res_v["Items"]} == {scene.m["V"].guid,
                                                 scene.m["V3"].guid}

"""身份准入货架·一期：Latest / Suggestions / Counts / Views 计数同口径。

计数撕裂是用户最先看见的问题（库上写 120 部、点进去 40 部），所以一期
把四处一起钉：Latest 与 Suggestions 不露未绑定；Counts 只数已绑定行
（episode/season 跟随 series）；Views 的 ChildCount 在门开时不许用
扫描写回的全量缓存列，必须实时数已绑定行；虚拟库计数同口径。
门关时四处全部与基线一致。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api, facets, identity_gate
from backend.emby_server import models as em


@pytest.fixture()
def scene(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/counts.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    # item_count=99 是故意放的全量假缓存值：门关时 ChildCount 用它，
    # 门开时必须绕开它实时数（只数已绑定 = 2）
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="mixed", is_enabled=True,
                     item_count=99)
    db.add(lib)
    db.flush()
    now = datetime(2026, 10, 24, 12, 0, 0)

    def mk(item_type, name, days_ago=0, **kw):
        it = em.MediaItem(
            guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
            name=name, sort_name=name,
            file_path=f"/m/{uuid.uuid4().hex}.mkv",
            date_added=now - timedelta(days=days_ago), **kw)
        db.add(it)
        db.flush()
        return it

    m = {"lib": lib}
    # 未绑定电影入库最新（Latest 名额钉：门开时它不许占 Limit 名额）
    m["mu"] = mk("movie", "未绑定新片", days_ago=0)
    m["mb"] = mk("movie", "已绑定新片", days_ago=1, tmdb_id="1")
    m["sb"] = mk("series", "已绑定剧", days_ago=2, tmdb_id="2")
    m["su"] = mk("series", "未绑定剧", days_ago=3)
    m["season_b"] = mk("season", "已绑定剧季", series_id=m["sb"].id,
                       season_number=1)
    m["season_u"] = mk("season", "未绑定剧季", series_id=m["su"].id,
                       season_number=1)
    m["ep_b"] = mk("episode", "已绑定剧集", series_id=m["sb"].id,
                   season_number=1, episode_number=1)
    m["ep_u"] = mk("episode", "未绑定剧集", series_id=m["su"].id,
                   season_number=1, episode_number=1)
    user = models.WebUser(username="gate", password_hash="x", is_active=True)
    db.add(user)
    db.flush()
    # 未绑定新片播放量最高：Suggestions 的 popular 路若不挂门它必上榜
    db.add(em.UserMediaData(user_id=user.id, item_id=m["mu"].id,
                            played=True, play_count=100))
    db.add(em.UserMediaData(user_id=user.id, item_id=m["mb"].id,
                            played=True, play_count=1))
    db.commit()
    data = SimpleNamespace(db=db, m=m, user=user)
    yield data
    db.close()


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 1)


def _stub_api(mp):
    mp.setattr(api, "_base_url", lambda request: "http://test")
    mp.setattr(api, "_prefetch_list_data", lambda *a, **k: None)
    mp.setattr(api, "_library_scope", lambda db, user: None)
    mp.setattr(api, "_item_dto",
               lambda e, base, uid, db: {"Id": e.guid, "Name": e.name,
                                         "Type": e.item_type})


def _latest(scene, limit=16):
    req = SimpleNamespace(query_params={"Limit": str(limit)})
    with pytest.MonkeyPatch().context() as mp:
        _stub_api(mp)
        return api.get_latest(req, scene.user, scene.db)


def _suggestions(scene):
    req = SimpleNamespace(query_params={"Limit": "20"})
    with pytest.MonkeyPatch().context() as mp:
        _stub_api(mp)
        return api.get_suggestions("1", req, scene.user, scene.db)


def test_latest_gate_off_baseline(scene):
    names = [i["Name"] for i in _latest(scene)]
    assert names[0] == "未绑定新片"  # 按入库倒序，未绑定照样在（旧口径）


def test_latest_gate_on_hides_unbound(scene, gate_on):
    names = [i["Name"] for i in _latest(scene)]
    assert "未绑定新片" not in names and "未绑定剧" not in names
    assert "已绑定新片" in names and "已绑定剧" in names
    # 名额钉：未绑定最新也不许占 Limit——Limit=1 应拿到已绑定新片
    names1 = [i["Name"] for i in _latest(scene, limit=1)]
    assert names1 == ["已绑定新片"]


def test_suggestions_gate_on_hides_unbound(scene, gate_on):
    res = _suggestions(scene)
    names = {i["Name"] for i in res["Items"]}
    assert names <= {"已绑定新片", "已绑定剧"}, names
    assert "未绑定新片" not in names, "popular 路（播放量最高）也不能漏出"


def test_counts_gate_off_baseline(scene):
    res = api.items_counts(user=scene.user, db=scene.db)
    assert res["MovieCount"] == 2
    assert res["SeriesCount"] == 2
    assert res["EpisodeCount"] == 2
    assert res["ItemCount"] == 8


def test_counts_gate_on_bound_only(scene, gate_on):
    res = api.items_counts(user=scene.user, db=scene.db)
    assert res["MovieCount"] == 1
    assert res["SeriesCount"] == 1
    assert res["EpisodeCount"] == 1, "episode 跟随 series 判据计数"
    # ItemCount = 已绑定 movie/series 各 1 + 跟随已绑定剧的 season/episode 各 1
    assert res["ItemCount"] == 4


def test_views_child_count_gate_off_uses_cache_column(scene):
    res = api.user_views("me", user=scene.user, db=scene.db)
    entry = {i["Name"]: i for i in res["Items"]}["库"]
    assert entry["ChildCount"] == 99  # 门关：扫描写回的缓存列原样（旧口径）


def test_views_child_count_gate_on_counts_bound_live(scene, gate_on):
    res = api.user_views("me", user=scene.user, db=scene.db)
    entry = {i["Name"]: i for i in res["Items"]}["库"]
    assert entry["ChildCount"] == 2, \
        "门开时不许用全量缓存列，必须实时只数已绑定 movie/series"


def test_virtual_library_count_follows_gate(scene, gate_on):
    vlib = em.Library(guid=uuid.uuid4().hex, name="虚拟库",
                      collection_type="mixed", is_enabled=True,
                      is_virtual=True, platform="Netflix")
    scene.db.add(vlib)
    scene.db.flush()
    # 四个顶层条目都带 Netflix 平台标记（走 facets 未就绪时的文本口径）
    for key in ("mb", "mu", "sb", "su"):
        scene.m[key].platforms = "Netflix"
    scene.db.commit()
    assert facets.count_virtual_items(scene.db, vlib) == 2, \
        "虚拟库计数与真实库同口径：只数已绑定"

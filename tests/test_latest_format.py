"""Latest（最新入库）回归测试。

事实（多个独立信源证实）：
1. Emby/Jellyfin 官方 /Items/Latest 返回**裸 JSON 数组** [...]，不是
   {"Items": [...]}。PR #383 包成 dict 是错的，已纠正。
2. SenPlayer 按库调 Latest（ParentId=<库 GUID>）取各库海报行，
   ParentId 必须按库过滤——之前忽略 ParentId，10 次调用返回完全相同的全局数据。
"""
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as core  # noqa: F401
from backend.database import Base
from backend.emby_server import api, models as em


@pytest.fixture
def db():
    path = os.path.join(tempfile.mkdtemp(), "latest.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


def _mk(db, lib, item_type, name, date_added=None):
    it = em.MediaItem(
        guid=uuid.uuid4().hex[:32],
        library_id=lib.id,
        item_type=item_type,
        name=name,
        sort_name=name,
        is_hidden=False,
        date_added=date_added or datetime.now(),
    )
    db.add(it)
    db.flush()
    return it


@pytest.fixture
def scene(db):
    lib_a = em.Library(guid="A" * 32, name="电影库", collection_type="movies", paths="")
    lib_b = em.Library(guid="B" * 32, name="剧集库", collection_type="tvshows", paths="")
    db.add_all([lib_a, lib_b])
    db.flush()
    now = datetime.now()
    m_old = _mk(db, lib_a, "movie", "老电影", date_added=now - timedelta(days=30))
    m_new = _mk(db, lib_a, "movie", "新电影", date_added=now - timedelta(days=1))
    s_new = _mk(db, lib_b, "series", "新剧集", date_added=now - timedelta(hours=1))
    user = core.WebUser(username="u1", password_hash="x")
    db.add(user)
    db.flush()
    db.commit()
    return SimpleNamespace(db=db, user=user, lib_a=lib_a, lib_b=lib_b,
                           m_old=m_old, m_new=m_new, s_new=s_new)


def _call_latest(db, user, limit=16, parent_id=None):
    params = {"Limit": str(limit)}
    if parent_id:
        params["ParentId"] = parent_id
    req = SimpleNamespace(query_params=params)
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(api, "_base_url", lambda request: "http://test")
        mp.setattr(api, "_prefetch_list_data", lambda *a, **k: None)
        mp.setattr(api, "_library_scope", lambda db, user: None)
        mp.setattr(api, "_item_dto",
                   lambda e, base, uid, db: {"Id": e.guid, "Name": e.name,
                                             "ServerId": "test-server",
                                             "Type": e.item_type})
        return api.get_latest(req, user, db)


def test_latest_returns_bare_array(scene):
    """Emby/Jellyfin 官方行为：裸数组，不能是 dict。"""
    res = _call_latest(scene.db, scene.user)
    assert isinstance(res, list), "必须返回裸 list，不能是 dict"
    assert not isinstance(res, dict)


def test_latest_not_wrapped_dict(scene):
    """回归：PR #383 的 {"Items": ...} 包装是错的，必须消失。"""
    res = _call_latest(scene.db, scene.user)
    assert not (isinstance(res, dict) and "Items" in res), \
        "dict 包装格式是 bug（PR #383），见回归说明"


def test_latest_newest_first(scene):
    res = _call_latest(scene.db, scene.user)
    names = [i["Name"] for i in res]
    assert names[0] == "新剧集"  # 全局最新
    assert names[1] == "新电影"


def test_latest_limit(scene):
    res = _call_latest(scene.db, scene.user, limit=1)
    assert len(res) == 1


def test_latest_empty_db(db):
    user = core.WebUser(username="u2", password_hash="x")
    db.add(user)
    db.flush()
    res = _call_latest(db, user)
    assert res == []


def test_latest_parent_id_filters_by_library(scene):
    """ParentId=<库GUID> 必须只返回该库的条目。"""
    res = _call_latest(scene.db, scene.user, parent_id="A" * 32)
    names = [i["Name"] for i in res]
    assert "新电影" in names
    assert "老电影" in names
    assert "新剧集" not in names, "ParentId 过滤失效：混入了别的库"

    res_b = _call_latest(scene.db, scene.user, parent_id="B" * 32)
    names_b = [i["Name"] for i in res_b]
    assert names_b == ["新剧集"], f"剧集库应只返回自己的最新，实际: {names_b}"


def test_latest_parent_id_per_library_differs(scene):
    """10 个库调 Latest 不应返回完全相同的数据（Bug 2 的回归）。"""
    res_a = _call_latest(scene.db, scene.user, parent_id="A" * 32)
    res_b = _call_latest(scene.db, scene.user, parent_id="B" * 32)
    ids_a = {i["Id"] for i in res_a}
    ids_b = {i["Id"] for i in res_b}
    assert ids_a != ids_b, "不同库的 Latest 返回完全相同，ParentId 被忽略"


def test_latest_unknown_parent_id_no_filter(scene):
    """ParentId 查不到库时不加过滤（与之前行为一致，不 500）。"""
    res = _call_latest(scene.db, scene.user, parent_id="Z" * 32)
    names = [i["Name"] for i in res]
    assert "新剧集" in names and "新电影" in names


def test_latest_keeps_type_scope(scene):
    """Latest 仍只给 movie/series。"""
    db = scene.db
    ep = em.MediaItem(
        guid=uuid.uuid4().hex[:32], library_id=scene.lib_a.id,
        item_type="episode", name="单集", sort_name="单集",
        is_hidden=False, date_added=datetime.now(),
    )
    db.add(ep)
    db.flush()
    db.commit()
    res = _call_latest(db, scene.user)
    types = {i["Type"] for i in res}
    assert types <= {"movie", "series"}, f"混入了非法类型: {types}"


def test_latest_all_newest_are_merged_copies_returns_masters(scene):
    """复刻生产事故：库里最新 16 条全是合并副本时，Latest 不能回空数组。

    事故现场：外语电影/演唱会两库最新 16 条恰好全是 10-14 多版本合并产生的
    副本（副本入库比主记录新），get_latest 先查 16 条、再用
    deduplicate_items 把副本全扔掉且不回填 → 接口回 []，Reflix 整行空白。
    修复后：查询层直接排除副本（merged_into_id IS NULL），主记录顶上来。
    """
    db = scene.db
    now = datetime.now()
    masters = []
    for i in range(4):
        m = _mk(db, scene.lib_a, "movie", f"主{i}", date_added=now - timedelta(days=5))
        m.tmdb_id = f"tm{i}"
        db.flush()
        masters.append(m)
    # 16 个副本全部比主记录新，模拟"最新 16 条全是副本"
    for j in range(16):
        target = masters[j % 4]
        c = _mk(db, scene.lib_a, "movie", f"副本{j}",
                date_added=now - timedelta(hours=1) + timedelta(minutes=10 * j))
        c.tmdb_id = target.tmdb_id
        c.merged_into_id = target.id
        db.flush()
    db.commit()

    res = _call_latest(db, scene.user, parent_id=scene.lib_a.guid, limit=16)
    names = {i["Name"] for i in res}
    assert res != [], "事故重现：最新 16 条全是副本时 Latest 回了空数组"
    assert {"主0", "主1", "主2", "主3"} <= names, f"4 个主记录应全部返回，实际: {names}"
    assert not any("副本" in n for n in names), f"结果里混入了副本: {names}"


def test_latest_copies_do_not_crowd_out_masters(scene):
    """副本不能挤占 Latest 的 limit 名额：6 个最新是副本时仍应回满 8 条主记录。"""
    db = scene.db
    now = datetime.now()
    masters = []
    for i in range(10):
        m = _mk(db, scene.lib_b, "series", f"剧{i}",
                date_added=now - timedelta(days=10 - i))
        m.tmdb_id = f"tv{i}"
        db.flush()
        masters.append(m)
    for j in range(6):
        target = masters[j]
        c = _mk(db, scene.lib_b, "series", f"剧副本{j}",
                date_added=now - timedelta(hours=2) + timedelta(minutes=20 * j))
        c.tmdb_id = target.tmdb_id
        c.merged_into_id = target.id
        db.flush()
    db.commit()

    res = _call_latest(db, scene.user, parent_id=scene.lib_b.guid, limit=8)
    assert len(res) == 8, f"应回满 8 条主记录，实际 {len(res)} 条"
    assert all("副本" not in i["Name"] for i in res), "返回结果里混入了副本"

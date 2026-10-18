"""列表 ID 清单缓存（items_list_cache）测试。

覆盖：
- 配置默认值/钳制；禁用/TTL=0 走旧路径
- 缓存读写往返、损坏值按 miss 处理、条目上限淘汰
- 键规范化（参数顺序无关、api_key/StartIndex/Limit 排除在外、
  同一 allowed 不同用户共享键、不同 allowed 不同键）
- 可缓存性判定（用户状态筛选/排序/Ids 绕行）
- 版本 bump 失效（复用去重缓存版本行）
- 截断：页窗口超出已存前缀 → 绕行现状路径，结果仍一致
- 端到端：api._query_items 冷→热逐条一致（多页/多排序/单库/
  want_total=false 的 HasMore 语义）；用户状态查询不写缓存；
  数据变更+bump 后拿到新数据；关闭后与开时一致

注意：与去重缓存共用 Redis 后端（含测试注入点），不自起第二套。
"""
import os
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api
from backend.emby_server import items_dedup_cache as idc
from backend.emby_server import items_list_cache as ilc
from backend.emby_server import models as em
from backend.integrations import store


class FakeRedis:
    """最小 Redis 替身：实现本模块用到的命令子集（含 TTL）。"""

    def __init__(self):
        self.strings = {}
        self.zsets = {}

    def get(self, key):
        v = self.strings.get(key)
        if v is None:
            return None
        val, exp = v
        if exp is not None and exp <= time.time():
            del self.strings[key]
            return None
        return val

    def set(self, key, val, ex=None):
        exp = time.time() + ex if ex else None
        self.strings[key] = (val, exp)
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                n += 1
        return n

    def zadd(self, name, mapping):
        z = self.zsets.setdefault(name, {})
        z.update(mapping)
        return len(mapping)

    def zcard(self, name):
        return len(self.zsets.get(name, {}))

    def zrange(self, name, start, end):
        items = sorted(self.zsets.get(name, {}).items(), key=lambda kv: kv[1])
        keys = [k for k, _ in items]
        if end < 0:
            end = len(keys) + end
        return keys[start:end + 1]

    def zremrangebyrank(self, name, start, end):
        z = self.zsets.get(name, {})
        items = sorted(z.items(), key=lambda kv: kv[1])
        keys = [k for k, _ in items]
        if end < 0:
            end = len(keys) + end
        doomed = keys[start:end + 1]
        for k in doomed:
            z.pop(k, None)
        return len(doomed)

    def zrem(self, name, *members):
        z = self.zsets.get(name, {})
        n = 0
        for m in members:
            if m in z:
                del z[m]
                n += 1
        return n


def _g():
    return uuid.uuid4().hex


@pytest.fixture()
def fake_backend():
    fb = FakeRedis()
    idc._set_backend_override(fb)
    yield fb
    idc._set_backend_override(None)
    idc._reset_bump_state()


@pytest.fixture()
def clean_config():
    """每个用例后清掉两模块的配置行与 store 热缓存（防跨文件污染）。"""
    yield
    db = _test_db()
    try:
        db.query(models.SystemConfig).filter(
            models.SystemConfig.key.in_([
                ilc.CFG_ENABLED, ilc.CFG_TTL_SECONDS, ilc.CFG_MAX_IDS,
                idc.CFG_ENABLED, idc.CFG_TTL_SECONDS, idc.CFG_MAX_ENTRIES,
            ])).delete(synchronize_session=False)
        db.query(models.SystemConfig).filter(
            models.SystemConfig.key.like(idc._VERSION_KEY_PREFIX + "%")
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    store.invalidate(ilc.CFG_ENABLED, ilc.CFG_TTL_SECONDS, ilc.CFG_MAX_IDS,
                     idc.CFG_ENABLED, idc.CFG_TTL_SECONDS, idc.CFG_MAX_ENTRIES)
    idc._reset_bump_state()


_TEST_ENGINE = None


def _test_db():
    global _TEST_ENGINE
    if _TEST_ENGINE is None:
        _TEST_ENGINE = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(_TEST_ENGINE)
    return sessionmaker(bind=_TEST_ENGINE)()


def _set_cfg(db, **kv):
    store.write_values(db, kv, {k: "test" for k in kv})
    db.commit()
    for k in kv:
        store.invalidate(k)


@pytest.fixture(scope="module")
def seeded():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    t0 = datetime(2026, 1, 1)
    libm = em.Library(guid=_g(), name="电影", collection_type="movies", is_enabled=True)
    libt = em.Library(guid=_g(), name="剧集", collection_type="tvshows", is_enabled=True)
    db.add_all([libm, libt])
    db.flush()

    def mk(lib, item_type, name, **kw):
        kw.setdefault("date_added", t0)
        kw.setdefault("sort_name", (name or "").lower())
        it = em.MediaItem(guid=_g(), library_id=lib.id, item_type=item_type,
                          name=name, file_path=f"/m/{_g()}.mkv", **kw)
        db.add(it)
        db.flush()
        return it

    # 电影：两对同 tmdb（m2/m4 有海报是主，m1/m3 被去重掉）+ 若干独立
    m1 = mk(libm, "movie", "Alpha", tmdb_id="100", production_year=2001)
    m2 = mk(libm, "movie", "Alpha 1080p", tmdb_id="100", poster_path="/p.jpg",
            production_year=2001)
    m3 = mk(libm, "movie", "Beta", tmdb_id="200", production_year=2002)
    m4 = mk(libm, "movie", "Beta 4K", tmdb_id="200", poster_path="/p2.jpg",
            production_year=2002)
    m5 = mk(libm, "movie", "Gamma", tmdb_id="300", production_year=2005)
    m6 = mk(libm, "movie", "Delta", tmdb_id="400", production_year=2010)
    m7 = mk(libm, "movie", "Epsilon", tmdb_id="500", production_year=2015)
    s1 = mk(libt, "series", "Show", tmdb_id="600", production_year=2020)

    user = models.WebUser(username="listcache", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    data = SimpleNamespace(db=db, user=SimpleNamespace(id=user.id, is_staff=False),
                           libm=libm, libt=libt,
                           m1=m1, m2=m2, m3=m3, m4=m4, m5=m5, m6=m6, m7=m7, s1=s1)
    yield data
    db.close()
    engine.dispose()


def _req(params):
    return Request({"type": "http", "method": "GET",
                    "query_string": urlencode(params).encode(), "headers": []})


def _ids(res):
    return [i["Id"] for i in res["Items"]]


def _list_keys(fb):
    return [k for k in fb.strings if k.startswith("items:list:")]


# ---------------- 单元 ----------------

def test_config_defaults(clean_config):
    db = _test_db()
    try:
        assert ilc.cache_enabled(db) is True
        assert ilc.cache_ttl_seconds(db) == 60
        assert ilc.cache_max_ids(db) == 5000
        p = ilc.config_payload(db)
        assert p["enabled"] is True and p["ttl_seconds"] == 60 and p["max_ids"] == 5000
        assert p["defaults"]["max_ids"] == 5000
    finally:
        db.close()


def test_config_clamp(clean_config):
    db = _test_db()
    try:
        _set_cfg(db, **{ilc.CFG_TTL_SECONDS: "5"})  # 低于下限 → 钳到 10
        assert ilc.cache_ttl_seconds(db) == 10
        _set_cfg(db, **{ilc.CFG_MAX_IDS: "999999"})  # 高于上限 → 钳到 50000
        assert ilc.cache_max_ids(db) == 50000
        _set_cfg(db, **{ilc.CFG_MAX_IDS: "abc"})  # 非法 → 默认
        assert ilc.cache_max_ids(db) == 5000
    finally:
        db.close()


def test_put_get_roundtrip(fake_backend, clean_config):
    db = _test_db()
    try:
        assert ilc.get_cached_page(db, {"a": "1"}, [1, 2]) is None
        ilc.put_cached_page(db, {"a": "1"}, [1, 2], [10, 20, 30], 30, False)
        page = ilc.get_cached_page(db, {"a": "1"}, [1, 2])
        assert page == ilc.CachedPage(ids=[10, 20, 30], total=30, truncated=False)
        # 截断标记往返
        ilc.put_cached_page(db, {"b": "2"}, [1], [1, 2], 999, True)
        page = ilc.get_cached_page(db, {"b": "2"}, [1])
        assert page.total == 999 and page.truncated is True and page.ids == [1, 2]
        # 空清单也缓存（与 miss 的 None 可区分）
        ilc.put_cached_page(db, {"c": "3"}, [1], [], 0, False)
        page = ilc.get_cached_page(db, {"c": "3"}, [1])
        assert page is not None and page.ids == [] and page.total == 0
    finally:
        db.close()


def test_disabled_bypass(fake_backend, clean_config):
    # 注：cache_ttl_seconds 有下限钳制（"0"→10），TTL<=0 在读路径不可达；
    # 该分支是与去重缓存同构的防御性代码，这里只测开关关闭。
    db = _test_db()
    try:
        _set_cfg(db, **{ilc.CFG_ENABLED: "false"})
        ilc.put_cached_page(db, {"a": "1"}, [1], [1, 2], 2, False)
        assert _list_keys(fake_backend) == []
        assert ilc.get_cached_page(db, {"a": "1"}, [1]) is None
    finally:
        db.close()


def test_key_canonicalization(fake_backend, clean_config):
    db = _test_db()
    try:
        ilc.put_cached_page(db, {"b": "2", "a": "1"}, [1], [9], 1, False)
        # 参数顺序不同 → 同一键命中
        assert ilc.get_cached_page(db, {"a": "1", "b": "2"}, [1]).ids == [9]
        # api_key 不参与键
        assert ilc.get_cached_page(
            db, {"a": "1", "b": "2", "api_key": "zzz"}, [1]).ids == [9]
        # StartIndex/Limit 是切片维度，不参与键（同一份清单服务所有页）
        assert ilc.get_cached_page(
            db, {"a": "1", "b": "2", "StartIndex": "40", "Limit": "20"}, [1]).ids == [9]
        assert ilc.get_cached_page(
            db, {"a": "1", "b": "2", "startindex": "0", "limit": "100"}, [1]).ids == [9]
        # EnableTotalRecordCount 只影响返回形态，不参与键
        assert ilc.get_cached_page(
            db, {"a": "1", "b": "2", "EnableTotalRecordCount": "false"}, [1]).ids == [9]
        # 不同参数 → 不同键
        assert ilc.get_cached_page(db, {"a": "1"}, [1]) is None
        # 不同 allowed → 不同键
        assert ilc.get_cached_page(db, {"a": "1", "b": "2"}, [2]) is None
    finally:
        db.close()


def test_key_shared_across_users_same_scope(fake_backend, clean_config):
    """DTO 不进缓存：相同可见范围的不同用户共享同一份清单（键里无用户 id）。"""
    db = _test_db()
    try:
        ilc.put_cached_page(db, {"a": "1"}, [1, 2], [7], 1, False)
        # put/get 签名里根本没有 user_id：换库范围才换键
        assert ilc.get_cached_page(db, {"a": "1"}, [1, 2]).ids == [7]
        assert ilc.get_cached_page(db, {"a": "1"}, [2, 1]).ids == [7]  # 顺序无关
        assert ilc.get_cached_page(db, {"a": "1"}, [1]) is None
    finally:
        db.close()


def test_empty_scope_never_shares_with_unrestricted(fake_backend, clean_config):
    """fail-closed 空可见范围（allowed=∅）不得与不限制（None→全部库）共用键。

    回归：scope_lib_ids 曾用真值判断区分 None/集合，空集合（_library_scope
    的 fail-closed「全部不可见」）会被膨胀成全部启用库，与 allowed=None
    的键撞车——本该什么都看不到的用户会读到全量清单。
    """
    db = _test_db()
    try:
        lib1 = em.Library(guid=_g(), name="甲库", collection_type="movies", is_enabled=True)
        lib2 = em.Library(guid=_g(), name="乙库", collection_type="tvshows", is_enabled=True)
        db.add_all([lib1, lib2])
        db.commit()
        assert ilc.scope_lib_ids(db, set()) == []
        all_libs = ilc.scope_lib_ids(db, None)
        assert {lib1.id, lib2.id} <= set(all_libs)
        # 不限制作用域存了清单 → 空作用域读不到
        ilc.put_cached_page(db, {"a": "1"}, all_libs, [7, 8], 2, False)
        assert ilc.get_cached_page(db, {"a": "1"}, ilc.scope_lib_ids(db, set())) is None
        # 反向亦然：空作用域存的空清单不污染不限制作用域
        ilc.put_cached_page(db, {"a": "1"}, [], [], 0, False)
        assert ilc.get_cached_page(db, {"a": "1"}, all_libs).ids == [7, 8]
    finally:
        db.close()


def test_corrupt_value_is_miss(fake_backend, clean_config):
    db = _test_db()
    try:
        ilc.put_cached_page(db, {"a": "1"}, [1], [1], 1, False)
        key = next(k for k in _list_keys(fake_backend))
        fake_backend.strings[key] = ("not-a-list-cache-value", None)
        assert ilc.get_cached_page(db, {"a": "1"}, [1]) is None
    finally:
        db.close()


def test_is_cacheable_matrix():
    ok = dict(filters=set(), sort_by=["SortName"], has_ids=False)
    assert ilc.is_cacheable(**ok) is True
    # 用户状态筛选 → 绕行
    for f in ("isfavorite", "isplayed", "isunplayed", "isresumable"):
        assert ilc.is_cacheable(filters={f}, sort_by=["SortName"], has_ids=False) is False
    # 按用户数据排序 → 绕行
    for s in ("DatePlayed", "PlayCount", "dateplayed"):
        assert ilc.is_cacheable(filters=set(), sort_by=[s], has_ids=False) is False
    # Ids 定向取条目 → 绕行
    assert ilc.is_cacheable(filters=set(), sort_by=["SortName"], has_ids=True) is False
    # 普通筛选/排序 → 可缓存
    assert ilc.is_cacheable(filters={"genre"}, sort_by=["ProductionYear"],
                            has_ids=False) is True


def test_version_bump_invalidates(fake_backend, clean_config):
    """版本号与去重缓存共用同一套版本行：bump 后旧条目自然失效。"""
    db = _test_db()
    try:
        ilc.put_cached_page(db, {"a": "1"}, [1], [5], 1, False)
        assert ilc.get_cached_page(db, {"a": "1"}, [1]).ids == [5]
        assert idc.bump_library_item_versions(db, [1], force=True) == 1
        db.commit()
        assert ilc.get_cached_page(db, {"a": "1"}, [1]) is None
    finally:
        db.close()


def test_max_entries_evicts_oldest(fake_backend, clean_config):
    db = _test_db()
    try:
        for i in range(ilc._MAX_ENTRIES + 3):
            ilc.put_cached_page(db, {"n": str(i)}, [1], [i], 1, False)
        assert len(_list_keys(fake_backend)) <= ilc._MAX_ENTRIES
        # 最旧的被淘汰
        assert ilc.get_cached_page(db, {"n": "0"}, [1]) is None
        last = ilc._MAX_ENTRIES + 2
        assert ilc.get_cached_page(db, {"n": str(last)}, [1]).ids == [last]
    finally:
        db.close()


# ---------------- 端到端（走 api._query_items 真路径） ----------------

def _full_params(**kw):
    p = {"Recursive": "true", "IncludeItemTypes": "Movie,Series"}
    p.update(kw)
    return p


def test_e2e_cold_hot_identical_multipage(seeded, fake_backend, clean_config):
    s = seeded
    base = _full_params(Limit="3")
    r1 = api._query_items(_req(base), s.user, s.db, "http://t")
    # 去重语义：m1/m3（无海报）被去重掉
    assert s.m1.guid not in _ids(r1) and s.m3.guid not in _ids(r1)
    assert s.m2.guid in _ids(r1) and s.m4.guid in _ids(r1)
    assert _list_keys(fake_backend), "冷请求应回填清单缓存"
    total = r1["TotalRecordCount"]
    # 热：翻页（StartIndex/Limit 不进键，同一份清单切片）
    r2 = api._query_items(_req(_full_params(Limit="3", StartIndex="3")), s.user, s.db, "http://t")
    assert r2["TotalRecordCount"] == total
    # 与「关闭缓存」跑出来的逐页结果逐条一致
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    off = api._query_items(_req(_full_params(Limit="3", StartIndex="3")), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    assert _ids(r2) == _ids(off)
    assert r2["TotalRecordCount"] == off["TotalRecordCount"]
    # 第一页同样一致
    r1_hot = api._query_items(_req(base), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    r1_off = api._query_items(_req(base), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    assert _ids(r1_hot) == _ids(r1_off) == _ids(r1)


def test_e2e_different_sort_and_library(seeded, fake_backend, clean_config):
    s = seeded
    # 不同 SortBy → 各自缓存、各自正确
    by_name = api._query_items(_req(_full_params(SortBy="SortName")), s.user, s.db, "http://t")
    by_year = api._query_items(_req(_full_params(SortBy="ProductionYear", SortOrder="Descending")),
                               s.user, s.db, "http://t")
    assert _ids(by_name) != _ids(by_year)
    assert by_name["TotalRecordCount"] == by_year["TotalRecordCount"]
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    by_name_off = api._query_items(_req(_full_params(SortBy="SortName")), s.user, s.db, "http://t")
    by_year_off = api._query_items(
        _req(_full_params(SortBy="ProductionYear", SortOrder="Descending")),
        s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    assert _ids(by_name) == _ids(by_name_off)
    assert _ids(by_year) == _ids(by_year_off)
    # 单库 ParentId（媒体库 guid）→ 与关缓存一致
    lib_params = _full_params(ParentId=s.libm.guid)
    on = api._query_items(_req(lib_params), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    off = api._query_items(_req(lib_params), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    assert _ids(on) == _ids(off) and on["TotalRecordCount"] == off["TotalRecordCount"]
    assert all(i != s.s1.guid for i in _ids(on))  # 剧集库的剧不在电影库结果里


def test_e2e_want_total_false_hasmore(seeded, fake_backend, clean_config):
    s = seeded
    params = _full_params(Limit="3", EnableTotalRecordCount="false")
    hot = api._query_items(_req(params), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    cold = api._query_items(_req(params), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    # Emby 约束：TotalRecordCount 永不为负；HasMore 语义一致
    assert hot["TotalRecordCount"] >= 0 and cold["TotalRecordCount"] >= 0
    assert hot.get("HasMore") == cold.get("HasMore") == True
    assert _ids(hot) == _ids(cold)
    # 尾页：HasMore=False
    total = api._query_items(_req(_full_params()), s.user, s.db, "http://t")["TotalRecordCount"]
    last = api._query_items(
        _req(_full_params(Limit="3", StartIndex=str(total), EnableTotalRecordCount="false")),
        s.user, s.db, "http://t")
    assert last.get("HasMore") is False


def test_e2e_user_state_bypasses_cache(seeded, fake_backend, clean_config):
    s = seeded
    n_before = len(_list_keys(fake_backend))
    # 用户状态筛选 → 完全绕行（不读不写）
    r = api._query_items(_req(_full_params(Filters="IsPlayed")), s.user, s.db, "http://t")
    assert len(_list_keys(fake_backend)) == n_before
    # 按用户数据排序 → 绕行
    r2 = api._query_items(_req(_full_params(SortBy="DatePlayed")), s.user, s.db, "http://t")
    assert len(_list_keys(fake_backend)) == n_before
    assert isinstance(r["TotalRecordCount"], int) and isinstance(r2["TotalRecordCount"], int)
    # Ids 定向 → 绕行
    api._query_items(_req({"Ids": s.m5.guid}), s.user, s.db, "http://t")
    assert len(_list_keys(fake_backend)) == n_before


def test_e2e_bump_invalidates_list(seeded, fake_backend, clean_config):
    s = seeded
    r1 = api._query_items(_req(_full_params()), s.user, s.db, "http://t")
    n1 = r1["TotalRecordCount"]
    assert _list_keys(fake_backend)
    # 新增条目 + bump（模拟扫描收尾）→ 下一次请求拿到新数据
    new = em.MediaItem(guid=_g(), library_id=s.libm.id, item_type="movie", name="Zeta",
                       sort_name="zeta", file_path="/m/z.mkv", date_added=datetime(2026, 1, 2),
                       tmdb_id="700", production_year=2021)
    s.db.add(new)
    s.db.commit()
    idc.bump_library_item_versions(s.db, [s.libm.id], force=True)
    s.db.commit()
    r2 = api._query_items(_req(_full_params()), s.user, s.db, "http://t")
    assert r2["TotalRecordCount"] == n1 + 1
    assert new.guid in _ids(r2)


def test_e2e_truncated_prefix_falls_back(seeded, fake_backend, clean_config, monkeypatch):
    """max_ids 很小 → 前缀截断；页窗口超出前缀的请求绕行现状路径，结果仍正确。"""
    s = seeded
    monkeypatch.setattr(ilc, "cache_max_ids", lambda db=None: 4)
    on_p1 = api._query_items(_req(_full_params(Limit="3")), s.user, s.db, "http://t")
    on_p2 = api._query_items(_req(_full_params(Limit="3", StartIndex="3")), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    off_p1 = api._query_items(_req(_full_params(Limit="3")), s.user, s.db, "http://t")
    off_p2 = api._query_items(_req(_full_params(Limit="3", StartIndex="3")), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    assert _ids(on_p1) == _ids(off_p1)
    assert _ids(on_p2) == _ids(off_p2)
    assert on_p1["TotalRecordCount"] == off_p1["TotalRecordCount"]
    # 深页（start 超出前缀）→ 走了 DB 路径但结果正确
    deep = api._query_items(_req(_full_params(Limit="3", StartIndex="30")), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    deep_off = api._query_items(_req(_full_params(Limit="3", StartIndex="30")), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    assert _ids(deep) == _ids(deep_off)


def test_redis_unavailable_fail_open(seeded, clean_config):
    """不注入假后端（REDIS_ENABLED=false → get_redis() 为 None）：
    get 恒 miss、put 静默，请求走现状路径结果正确。"""
    s = seeded
    on = api._query_items(_req(_full_params()), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "false"})
    off = api._query_items(_req(_full_params()), s.user, s.db, "http://t")
    _set_cfg(s.db, **{ilc.CFG_ENABLED: "true"})
    assert _ids(on) == _ids(off)
    assert on["TotalRecordCount"] == off["TotalRecordCount"]


def test_e2e_serialize_bounded(fake_backend, clean_config):
    """条目大小有界：5000 个 id 的条目远小于 100KB。"""
    db = _test_db()
    try:
        ids = list(range(1, 5001))
        ilc.put_cached_page(db, {"a": "1"}, [1], ids, 180000, True)
        key = next(k for k in _list_keys(fake_backend))
        val, _ = fake_backend.strings[key]
        assert len(val) < 100 * 1024, f"entry too big: {len(val)}"
        page = ilc.get_cached_page(db, {"a": "1"}, [1])
        assert page.ids == ids and page.total == 180000 and page.truncated is True
    finally:
        db.close()


def test_scope_db_failure_returns_none():
    """DB 抖动 → scope_lib_ids 返回 None（作用域未知），绝不拿 [] 充数。"""
    class BoomDB:
        def query(self, *a, **kw):
            raise RuntimeError("db down")

    assert ilc.scope_lib_ids(BoomDB(), None) is None
    # 真实空集合不受影响：仍是 []（键独立、语义正确）
    assert ilc.scope_lib_ids(BoomDB(), set()) == []


def test_e2e_unknown_scope_fail_closed(seeded, fake_backend, clean_config, monkeypatch):
    """作用域未知 → 整层清单缓存 fail-closed：不读不写，结果与基线逐条一致。"""
    s = seeded
    params = _full_params(Limit="3")
    base = api._query_items(_req(params), s.user, s.db, "http://t")
    keys_before = set(_list_keys(fake_backend))

    monkeypatch.setattr(ilc, "scope_lib_ids", lambda db, allowed: None)
    res = api._query_items(_req(params), s.user, s.db, "http://t")

    assert set(_list_keys(fake_backend)) == keys_before, "作用域未知时不许写清单缓存"
    assert _ids(res) == _ids(base)
    assert res["TotalRecordCount"] == base["TotalRecordCount"]


def test_e2e_want_total_false_hit_branch(seeded, fake_backend, clean_config, monkeypatch):
    """want_total=false 命中清单缓存的分支：HasMore/总数/Ids 都对。"""
    s = seeded
    hits = []
    _real = ilc.get_cached_page

    def spy(db, query_params, lib_ids, versions=None):
        page = _real(db, query_params, lib_ids, versions=versions)
        hits.append(page is not None)
        return page

    monkeypatch.setattr(ilc, "get_cached_page", spy)

    # 先 want_total=true 写热（seeded 去重后 N 条，前面别的用例可能新增过条目）
    hot = api._query_items(_req(_full_params(Limit="3")), s.user, s.db, "http://t")
    total = hot["TotalRecordCount"]
    assert total >= 6
    assert hits, "至少写过一次清单缓存"
    hits.clear()

    # 同参数、EnableTotalRecordCount=false → 必须命中
    no_total = api._query_items(
        _req(_full_params(Limit="3", EnableTotalRecordCount="false")), s.user, s.db, "http://t")
    assert hits[-1] is True, "false 请求应命中清单缓存"
    assert no_total.get("HasMore") is True
    assert no_total["TotalRecordCount"] == 3  # Emby 约束：永不为负，本页数量
    assert _ids(no_total) == _ids(hot)

    # 尾页：StartIndex=total-3 → 命中且 HasMore=False
    tail = api._query_items(
        _req(_full_params(Limit="3", StartIndex=str(total - 3), EnableTotalRecordCount="false")),
        s.user, s.db, "http://t")
    assert hits[-1] is True
    assert tail.get("HasMore") is False
    assert len(tail["Items"]) == 3


def test_random_sort_does_not_500(seeded, fake_backend, clean_config):
    """SortBy=Random 有结果时不 500（r1307 之前 NameError）：顺序随机，只验形态。"""
    s = seeded
    res = api._query_items(
        _req(_full_params(SortBy="Random", Limit="5")), s.user, s.db, "http://t")
    assert len(res["Items"]) > 0
    assert res["TotalRecordCount"] > 0
    assert res["StartIndex"] == 0

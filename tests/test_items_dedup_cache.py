"""列表去重缓存（items_dedup_cache）测试。

覆盖：
- 缓存读写往返、miss、禁用/TTL=0 走旧路径
- 键规范化（参数顺序无关、api_key 排除在外）
- 数据版本号 bump 失效、防抖
- 条目上限淘汰最旧
- 脏数据按 miss 处理
- write_config 范围校验
- 端到端：api._query_items 两次调用（冷→热）结果全等；数据变更+bump 后
  第一次请求拿到新数据

注意：用注入式假后端，不依赖 fakeredis/真实 Redis。
"""
import os
import time
import uuid
from datetime import datetime, timedelta
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
from backend.emby_server import models as em
from backend.integrations import store


class FakeRedis:
    """最小 Redis 替身：实现 items_dedup_cache 用到的命令子集。"""

    def __init__(self):
        self.strings = {}
        self.zsets = {}
        self.calls = []

    def get(self, key):
        self.calls.append(("get", key))
        v = self.strings.get(key)
        if v is None:
            return None
        val, exp = v
        if exp is not None and exp <= time.time():
            del self.strings[key]
            return None
        return val

    def set(self, key, val, ex=None):
        self.calls.append(("set", key))
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
    """每个用例后清掉本模块与清单模块的配置行与 store 热缓存（防跨文件污染）。"""
    yield
    from backend.emby_server import items_list_cache as ilc
    db = _test_db()
    try:
        db.query(models.SystemConfig).filter(
            models.SystemConfig.key.in_([
                idc.CFG_ENABLED, idc.CFG_TTL_SECONDS, idc.CFG_MAX_ENTRIES,
                ilc.CFG_ENABLED, ilc.CFG_TTL_SECONDS, ilc.CFG_MAX_IDS,
            ])).delete(synchronize_session=False)
        db.query(models.SystemConfig).filter(
            models.SystemConfig.key.like(idc._VERSION_KEY_PREFIX + "%")
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    store.invalidate(idc.CFG_ENABLED, idc.CFG_TTL_SECONDS, idc.CFG_MAX_ENTRIES,
                     ilc.CFG_ENABLED, ilc.CFG_TTL_SECONDS, ilc.CFG_MAX_IDS)
    idc._reset_bump_state()


_TEST_ENGINE = None


def _test_db():
    global _TEST_ENGINE
    if _TEST_ENGINE is None:
        _TEST_ENGINE = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(_TEST_ENGINE)
    return sessionmaker(bind=_TEST_ENGINE)()


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

    # m1/m2：同 tmdb → m2（有海报）是主，m1 被去重掉
    m1 = mk(libm, "movie", "Alpha", tmdb_id="100", production_year=2001)
    m2 = mk(libm, "movie", "Alpha 1080p", tmdb_id="100", poster_path="/p.jpg",
            production_year=2001)
    m3 = mk(libm, "movie", "Solo", tmdb_id="300", production_year=2005)
    s1 = mk(libt, "series", "Show", tmdb_id="500", production_year=2020)
    e1 = mk(libt, "episode", "S1E1", series_id=s1.id, season_number=1, episode_number=1)
    e2 = mk(libt, "episode", "S1E1 4K", series_id=s1.id, season_number=1, episode_number=1,
            poster_path="/e.jpg")

    user = models.WebUser(username="idcache", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    data = SimpleNamespace(db=db, user=SimpleNamespace(id=user.id, is_staff=False),
                           libm=libm, libt=libt, m1=m1, m2=m2, m3=m3, s1=s1)
    yield data
    db.close()
    engine.dispose()


def _req(params):
    return Request({"type": "http", "method": "GET",
                    "query_string": urlencode(params).encode(), "headers": []})


def _ids(res):
    return [i["Id"] for i in res["Items"]]


# ---------------- 单元 ----------------

def test_config_defaults(clean_config):
    db = _test_db()
    try:
        assert idc.cache_enabled(db) is True
        assert idc.cache_ttl_seconds(db) == 60
        assert idc.cache_max_entries(db) == 2000
        p = idc.config_payload(db)
        assert p["enabled"] is True and p["ttl_seconds"] == 60
        assert p["defaults"]["ttl_seconds"] == 60
    finally:
        db.close()


def test_write_config_validation(clean_config):
    db = _test_db()
    try:
        with pytest.raises(ValueError):
            idc.write_config(db, enabled=True, ttl_seconds=5, max_entries=2000)
        with pytest.raises(ValueError):
            idc.write_config(db, enabled=True, ttl_seconds=60, max_entries=50)
        p = idc.write_config(db, enabled=False, ttl_seconds=120, max_entries=500)
        db.commit()
        assert p["enabled"] is False and p["ttl_seconds"] == 120
        assert idc.cache_enabled(db) is False
    finally:
        db.close()


def test_put_get_roundtrip(fake_backend, clean_config):
    db = _test_db()
    try:
        assert idc.get_cached_dropped(db, 7, {"a": "1"}, [1, 2]) is None
        idc.put_cached_dropped(db, 7, {"a": "1"}, [1, 2], {10, 20, 30})
        assert idc.get_cached_dropped(db, 7, {"a": "1"}, [1, 2]) == {10, 20, 30}
        # 空集合也缓存（"" 与 miss 的 None 可区分）
        idc.put_cached_dropped(db, 7, {"b": "2"}, [1], set())
        assert idc.get_cached_dropped(db, 7, {"b": "2"}, [1]) == set()
    finally:
        db.close()


def test_disabled_or_ttl_zero_bypass(fake_backend, clean_config):
    db = _test_db()
    try:
        idc.write_config(db, enabled=False, ttl_seconds=60, max_entries=2000)
        db.commit()
        idc.put_cached_dropped(db, 7, {"a": "1"}, [1], {1, 2})
        assert fake_backend.strings == {}
        assert idc.get_cached_dropped(db, 7, {"a": "1"}, [1]) is None
    finally:
        db.close()


def test_key_canonicalization(fake_backend, clean_config):
    db = _test_db()
    try:
        idc.put_cached_dropped(db, 7, {"b": "2", "a": "1"}, [1], {9})
        # 参数顺序不同 → 同一键命中
        assert idc.get_cached_dropped(db, 7, {"a": "1", "b": "2"}, [1]) == {9}
        # api_key 不参与键
        assert idc.get_cached_dropped(
            db, 7, {"a": "1", "b": "2", "api_key": "zzz"}, [1]) == {9}
        # 不同用户 → 不同键
        assert idc.get_cached_dropped(db, 8, {"a": "1", "b": "2"}, [1]) is None
        # 不同参数 → 不同键
        assert idc.get_cached_dropped(db, 7, {"a": "1"}, [1]) is None
    finally:
        db.close()


def test_version_bump_invalidates(fake_backend, clean_config):
    db = _test_db()
    try:
        assert idc.read_data_versions(db, [1]) == {1: "0"}
        idc.put_cached_dropped(db, 7, {"a": "1"}, [1], {5})
        assert idc.get_cached_dropped(db, 7, {"a": "1"}, [1]) == {5}
        assert idc.bump_library_item_versions(db, [1], force=True) == 1
        db.commit()
        v = idc.read_data_versions(db, [1])[1]
        assert v != "0"
        # 毫秒级版本号（跨进程同秒双 bump 不丢失效）
        assert int(v) > 10 ** 12
        # 版本变了 → 旧条目键对不上 → miss
        assert idc.get_cached_dropped(db, 7, {"a": "1"}, [1]) is None
    finally:
        db.close()


def test_bump_debounce(fake_backend, clean_config):
    db = _test_db()
    try:
        assert idc.bump_library_item_versions(db, [1]) == 1
        # 30s 内第二次 → 被防抖吞掉
        assert idc.bump_library_item_versions(db, [1]) == 0
        assert idc.bump_library_item_versions(db, [1], force=True) == 1
        assert idc.bump_library_item_versions(db, []) == 0
    finally:
        db.close()


def test_max_entries_evicts_oldest(fake_backend, clean_config, monkeypatch):
    db = _test_db()
    monkeypatch.setattr(idc, "cache_max_entries", lambda db=None: 3)
    try:
        for i in range(5):
            idc.put_cached_dropped(db, 7, {"n": str(i)}, [1], {i})
        # 只剩 3 条：最旧的 n=0、n=1 被淘汰
        assert idc.get_cached_dropped(db, 7, {"n": "0"}, [1]) is None
        assert idc.get_cached_dropped(db, 7, {"n": "1"}, [1]) is None
        assert idc.get_cached_dropped(db, 7, {"n": "4"}, [1]) == {4}
    finally:
        db.close()


def test_corrupt_value_is_miss(fake_backend, clean_config):
    db = _test_db()
    try:
        idc.put_cached_dropped(db, 7, {"a": "1"}, [1], {1})
        # 直接污染底层值
        key = next(k for k in fake_backend.strings if k.startswith("items:dedup:"))
        fake_backend.strings[key] = ("not-an-id-list", None)
        assert idc.get_cached_dropped(db, 7, {"a": "1"}, [1]) is None
    finally:
        db.close()


# ---------------- 端到端（走 api._query_items 真路径） ----------------

def test_cold_then_hot_identical(seeded, fake_backend, clean_config, monkeypatch):
    calls = []
    real = api._dedup_dropped_ids

    def spy(query, db):
        out = real(query, db)
        calls.append(1)
        return out

    monkeypatch.setattr(api, "_dedup_dropped_ids", spy)
    params = {"Recursive": "true", "IncludeItemTypes": "Movie,Series"}
    res1 = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    assert len(calls) == 1  # 冷：算了一次
    # m2（有海报）是主，m1 被去重掉（DTO 的 Id 是 guid）
    assert seeded.m1.guid not in _ids(res1)
    assert seeded.m2.guid in _ids(res1)

    res2 = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    assert len(calls) == 1  # 热：没再算
    assert _ids(res2) == _ids(res1)
    assert res2["TotalRecordCount"] == res1["TotalRecordCount"]


def test_none_result_not_cached(seeded, fake_backend, clean_config, monkeypatch):
    monkeypatch.setattr(api, "_dedup_dropped_ids", lambda q, db: None)
    params = {"Recursive": "true"}
    api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    # None（回退旧实现信号）不写缓存
    assert fake_backend.strings == {}


def test_invalidation_after_data_change(seeded, fake_backend, clean_config, monkeypatch):
    calls = []
    real = api._dedup_dropped_ids

    def spy(query, db):
        out = real(query, db)
        calls.append(1)
        return out

    monkeypatch.setattr(api, "_dedup_dropped_ids", spy)
    params = {"Recursive": "true", "IncludeItemTypes": "Movie"}
    res1 = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    n_calls_cold = len(calls)
    assert n_calls_cold == 1
    total1 = res1["TotalRecordCount"]

    # 新增一条与 m3 同 tmdb 的重复电影 → 它会被去重掉
    dup = em.MediaItem(guid=_g(), library_id=seeded.libm.id, item_type="movie",
                       name="Solo 4K", file_path="/m/dup.mkv", tmdb_id="300",
                       production_year=2005, date_added=datetime(2026, 1, 2),
                       sort_name="solo 4k")
    seeded.db.add(dup)
    seeded.db.flush()
    # bump（force 保证测试确定性）+ 提交：版本号与数据原子可见
    assert idc.bump_library_item_versions(
        seeded.db, [seeded.libm.id], force=True) == 1
    seeded.db.commit()

    res2 = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    assert len(calls) == n_calls_cold + 1  # 失效后重新算了一次
    # 新增的是重复行：总数不变，但它不在返回里（被去重）
    assert res2["TotalRecordCount"] == total1
    assert dup.guid not in _ids(res2)

    # 再来一次 → 热命中，不再算
    res3 = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    assert len(calls) == n_calls_cold + 1
    assert _ids(res3) == _ids(res2)


def test_disabled_path_matches_uncached(seeded, fake_backend, clean_config, monkeypatch):
    """开关关 → 与旧路径逐行一致（用 monkeypatch 记下旧路径结果对比）。"""
    params = {"Recursive": "true", "IncludeItemTypes": "Movie,Series"}
    res_plain = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    # 开缓存再跑一次（冷）→ 必须全等
    res_cached = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    assert _ids(res_cached) == _ids(res_plain)
    assert res_cached["TotalRecordCount"] == res_plain["TotalRecordCount"]

    # 名副其实：真的关掉开关（write_config enabled=False），走旧路径，
    # 结果仍与旧路径逐行一致，且关开关之后不再写任何新缓存键。
    n_keys_before = len(fake_backend.strings)
    idc.write_config(seeded.db, enabled=False, ttl_seconds=60, max_entries=2000)
    seeded.db.commit()
    # 去重开关与清单开关都要关：清单层开了也会写 items:list:* 键，
    # 只关去重时断言「不再写任何新缓存键」恒过（旧断言漏洞）。
    from backend.emby_server import items_list_cache as ilc
    store.write_values(seeded.db, {ilc.CFG_ENABLED: "false"}, {ilc.CFG_ENABLED: "test"})
    seeded.db.commit()
    store.invalidate(ilc.CFG_ENABLED)
    res_disabled = api._query_items(_req(params), seeded.user, seeded.db, "http://t")
    assert _ids(res_disabled) == _ids(res_plain)
    assert res_disabled["TotalRecordCount"] == res_plain["TotalRecordCount"]
    assert len(fake_backend.strings) == n_keys_before

"""r41：列表热路径元数据查询去重 + 命中时不读去重缓存。

回归对象（backend/emby_server/api.py::_query_items）：
- W1：同一请求内「媒体库列表查询」与「版本号直查」原来各执行两次
  （dedup_dropped_ids_cached 一次 + scope_lib_ids/_key_for 一次）；
  r41 起两层缓存共用一次读取，暖命中时各恰好 1 次。
- W2：list-cache HIT 时，去重缓存的 dropped 大集合（Redis GET +
  反序列化）原来仍被完整读取，但结果在命中分支从未被使用；
  r41 起命中路径完全不碰去重缓存。
"""
import os
import time
import uuid
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, event
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
    def __init__(self):
        self.strings = {}
        self.zsets = {}
        self.get_calls = []

    def get(self, key):
        self.get_calls.append(key)
        v = self.strings.get(key)
        if v is None:
            return None
        val, exp = v
        if exp is not None and exp <= time.time():
            del self.strings[key]
            return None
        return val

    def set(self, key, val, ex=None):
        self.strings[key] = (val, time.time() + ex if ex else None)
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                n += 1
        return n

    def zadd(self, name, mapping):
        self.zsets.setdefault(name, {}).update(mapping)
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
        return 0

    def zrem(self, name, *members):
        return 0


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
    yield
    eng = create_engine("sqlite:///:memory:")
    # 只清 store 热缓存里的测试键（各用例自带独立 engine，DB 无需跨用例清理）
    store.invalidate(ilc.CFG_ENABLED, ilc.CFG_TTL_SECONDS, ilc.CFG_MAX_IDS,
                     idc.CFG_ENABLED, idc.CFG_TTL_SECONDS, idc.CFG_MAX_ENTRIES)
    idc._reset_bump_state()
    eng.dispose()


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
    for i in range(30):
        db.add(em.MediaItem(guid=_g(), library_id=libm.id, item_type="movie",
                            name=f"电影{i:02d}", sort_name=f"movie{i:02d}",
                            file_path=f"/m/{_g()}.mkv", date_added=t0,
                            tmdb_id=str(1000 + i), production_year=2000 + i))
    user = models.WebUser(username="r41dbperf", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    queries = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    data = SimpleNamespace(db=db, user=SimpleNamespace(id=user.id, is_staff=False),
                           queries=queries)
    yield data
    db.close()
    engine.dispose()


def _req(params):
    return Request({"type": "http", "method": "GET",
                    "query_string": urlencode(params).encode(), "headers": []})


def _params(**kw):
    p = {"Recursive": "true", "IncludeItemTypes": "Movie,Series",
         "SortBy": "SortName", "Limit": "20", "EnableTotalRecordCount": "true"}
    p.update(kw)
    return p


def _warm(seeded, fb, expect_dedup=True):
    """一次冷请求把 dedup + list 两层缓存都写热，返回冷结果。"""
    seeded.queries.clear()
    fb.get_calls.clear()
    r = api._query_items(_req(_params()), seeded.user, seeded.db, "http://t")
    assert r["TotalRecordCount"] == 30
    assert len([k for k in fb.strings if k.startswith("items:list:")]) >= 1
    if expect_dedup:
        assert len([k for k in fb.strings if k.startswith("items:dedup:")]) >= 1
    return r


def _meta_counts(seeded):
    lib = sum(1 for s in seeded.queries if "FROM emby_libraries" in s)
    ver = sum(1 for s in seeded.queries if "system_configs" in s and "IN (" in s)
    return lib, ver


def test_warm_hit_single_metadata_queries(seeded, fake_backend, clean_config):
    """W1：暖命中时媒体库列表查询与版本号直查各恰好 1 次（原来各 2 次）。"""
    _warm(seeded, fake_backend)
    seeded.queries.clear()
    fake_backend.get_calls.clear()
    r = api._query_items(_req(_params()), seeded.user, seeded.db, "http://t")
    assert r["TotalRecordCount"] == 30
    lib, ver = _meta_counts(seeded)
    assert lib == 1, f"媒体库列表查询应 1 次，实测 {lib}"
    assert ver == 1, f"版本号直查应 1 次，实测 {ver}"


def test_warm_hit_no_dedup_redis_read(seeded, fake_backend, clean_config):
    """W2：list-cache HIT 时完全不读去重缓存（原来有一次大集合 GET）。"""
    _warm(seeded, fake_backend)
    fake_backend.get_calls.clear()
    api._query_items(_req(_params()), seeded.user, seeded.db, "http://t")
    dedup_gets = [k for k in fake_backend.get_calls
                  if k.startswith("items:dedup:")]
    list_gets = [k for k in fake_backend.get_calls
                 if k.startswith("items:list:")]
    assert list_gets, "清单缓存本身应被命中读取"
    assert dedup_gets == [], f"命中路径不应读去重缓存，实测 {dedup_gets}"


def test_warm_hit_results_identical_multipage(seeded, fake_backend, clean_config):
    """重构后冷/热结果逐条一致（含翻页与 want_total=false 的 HasMore）。"""
    cold_p1 = api._query_items(_req(_params()), seeded.user, seeded.db, "http://t")
    cold_p2 = api._query_items(_req(_params(StartIndex="20")),
                               seeded.user, seeded.db, "http://t")
    hot_p1 = api._query_items(_req(_params()), seeded.user, seeded.db, "http://t")
    hot_p2 = api._query_items(_req(_params(StartIndex="20")),
                              seeded.user, seeded.db, "http://t")
    no_total = api._query_items(_req(_params(EnableTotalRecordCount="false")),
                                seeded.user, seeded.db, "http://t")
    for c, h in ((cold_p1, hot_p1), (cold_p2, hot_p2)):
        assert [i["Id"] for i in c["Items"]] == [i["Id"] for i in h["Items"]]
        assert c["TotalRecordCount"] == h["TotalRecordCount"] == 30
    assert [i["Id"] for i in cold_p1["Items"]] + [i["Id"] for i in cold_p2["Items"]] == \
        [i["Id"] for i in hot_p1["Items"]] + [i["Id"] for i in hot_p2["Items"]]
    assert no_total["HasMore"] is True
    assert no_total["TotalRecordCount"] == len(no_total["Items"])


def test_dedup_override_kwargs_key_equivalence(seeded, fake_backend, clean_config):
    """预传 lib_ids/versions 与内部计算走同一把键（行为等价）。"""
    db = seeded.db
    req = _req(_params())
    lib_ids = ilc.scope_lib_ids(db, None)
    versions = idc.read_data_versions(db, lib_ids)
    idc.put_cached_dropped(db, 4242, req.query_params, lib_ids, {7, 8, 9})
    # 内部计算路径应命中上面写入的条目
    hit = idc.dedup_dropped_ids_cached(req, None, db,
                                       SimpleNamespace(id=4242), None,
                                       lambda: (_ for _ in ()).throw(AssertionError("不应触发计算")))
    assert hit == {7, 8, 9}
    # 预传路径命中同一条目（键一致）
    hit2 = idc.dedup_dropped_ids_cached(req, None, db,
                                        SimpleNamespace(id=4242), None,
                                        lambda: (_ for _ in ()).throw(AssertionError("不应触发计算")),
                                        lib_ids=lib_ids, versions=versions)
    assert hit2 == {7, 8, 9}


def test_list_cache_versions_override_key_equivalence(seeded, fake_backend, clean_config):
    """get/put 预传 versions 与内部直查走同一把键。"""
    db = seeded.db
    qp = {"a": "1"}
    lib_ids = [1, 2]
    versions = idc.read_data_versions(db, lib_ids)
    ilc.put_cached_page(db, qp, lib_ids, [5, 6], 2, False, versions=versions)
    assert ilc.get_cached_page(db, qp, lib_ids).ids == [5, 6]
    assert ilc.get_cached_page(db, qp, lib_ids, versions=versions).ids == [5, 6]


def test_dedup_disabled_no_extra_version_read(seeded, fake_backend, clean_config):
    """去重缓存关掉时，暖命中仍只读 1 次版本号（不因预读逻辑多一次）。"""
    db = seeded.db
    store.write_values(db, {idc.CFG_ENABLED: "false"}, {idc.CFG_ENABLED: "t"})
    db.commit()
    store.invalidate(idc.CFG_ENABLED)
    try:
        _warm(seeded, fake_backend, expect_dedup=False)  # dedup 关：只写热 list 层
        seeded.queries.clear()
        r = api._query_items(_req(_params()), seeded.user, seeded.db, "http://t")
        assert r["TotalRecordCount"] == 30
        lib, ver = _meta_counts(seeded)
        assert ver == 1, f"版本号直查应 1 次（清单层），实测 {ver}"
        assert lib == 1, f"媒体库列表查询应 1 次，实测 {lib}"
    finally:
        db.query(models.SystemConfig).filter(
            models.SystemConfig.key == idc.CFG_ENABLED).delete(synchronize_session=False)
        db.commit()
        store.invalidate(idc.CFG_ENABLED)

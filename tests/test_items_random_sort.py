"""SortBy=Random 分支回归测试（R63-1）。

旧实现每次请求 `query.with_entities(id).all()` 全量物化匹配主键
（十万级库 = O(全库)/请求），再在内存抽样。新实现用 row_number()
窗口按随机行号取 id，只物化本页需要的行。

覆盖：
- 随机页形态：条数/总数/StartIndex/去重/合法 id
- 超出总数：空页但总数正确
- 关键回归：任何一次 Query.all() 返回行数都不超过 Limit（无界物化不再发生）
- want_total=false：总数非负且等于本页条数
"""
import os
import uuid
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Query, sessionmaker
from starlette.requests import Request

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import api
from backend.emby_server import items_dedup_cache as idc
from backend.emby_server import models as em


class FakeRedis:
    def __init__(self):
        self.strings = {}

    def get(self, key):
        v = self.strings.get(key)
        return v[0] if v else None

    def set(self, key, val, ex=None):
        self.strings[key] = (val, None)
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.strings:
                del self.strings[k]
                n += 1
        return n

    def zadd(self, name, mapping):
        return 0

    def zcard(self, name):
        return 0

    def zrange(self, name, start, end):
        return []

    def zremrangebyrank(self, name, start, end):
        return 0

    def zrem(self, name, *members):
        return 0


@pytest.fixture()
def fake_backend():
    fb = FakeRedis()
    idc._set_backend_override(fb)
    yield fb
    idc._set_backend_override(None)
    idc._reset_bump_state()


@pytest.fixture(scope="module")
def seeded():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    lib = em.Library(guid=uuid.uuid4().hex, name="电影",
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    t0 = datetime(2026, 1, 1)
    items = []
    for i in range(60):
        it = em.MediaItem(guid=uuid.uuid4().hex, library_id=lib.id,
                          item_type="movie", name=f"Movie {i:03d}",
                          sort_name=f"movie {i:03d}",
                          file_path=f"/m/{uuid.uuid4().hex}.mkv",
                          date_added=t0)
        db.add(it)
        db.flush()
        items.append(it)
    user = models.WebUser(username="r63random", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    data = SimpleNamespace(db=db,
                           user=SimpleNamespace(id=user.id, is_staff=False),
                           item_ids={it.id for it in items},
                           item_guids={it.guid for it in items})
    yield data
    db.close()
    engine.dispose()


def _req(params):
    return Request({"type": "http", "method": "GET",
                    "query_string": urlencode(params).encode(), "headers": []})


def _random_params(**kw):
    p = {"Recursive": "true", "IncludeItemTypes": "Movie", "SortBy": "Random"}
    p.update(kw)
    return p


def test_random_sort_returns_page_subset(seeded, fake_backend):
    s = seeded
    res = api._query_items(_req(_random_params(Limit="5")), s.user, s.db, "http://t")
    assert len(res["Items"]) == 5
    assert res["TotalRecordCount"] == 60
    assert res["StartIndex"] == 0
    ids = [i["Id"] for i in res["Items"]]
    assert len(set(ids)) == 5, "页内 id 不应重复"
    assert set(ids) <= s.item_guids, "返回的 id 必须在种子集合内"


def test_random_sort_beyond_total_empty_page(seeded, fake_backend):
    s = seeded
    res = api._query_items(
        _req(_random_params(Limit="5", StartIndex="1000")), s.user, s.db, "http://t")
    assert res["Items"] == []
    assert res["TotalRecordCount"] == 60
    assert res["StartIndex"] == 1000


def test_random_sort_no_unbounded_id_materialization(seeded, fake_backend):
    """R63-1 核心回归：Limit=5 的随机请求，任何一次 .all() 不得返回超过
    Limit 行（旧实现一次拉回 60 个 id）。"""
    s = seeded
    orig_all = Query.all
    max_rows = [0]

    def spy_all(self):
        res = orig_all(self)
        if len(res) > max_rows[0]:
            max_rows[0] = len(res)
        return res

    Query.all = spy_all
    try:
        res = api._query_items(_req(_random_params(Limit="5")), s.user, s.db, "http://t")
    finally:
        Query.all = orig_all
    assert len(res["Items"]) == 5
    assert max_rows[0] <= 5, f"仍有单次 .all() 返回 {max_rows[0]} 行，超过 Limit=5"


def test_random_sort_want_total_false(seeded, fake_backend):
    s = seeded
    res = api._query_items(
        _req(_random_params(Limit="5", EnableTotalRecordCount="false")),
        s.user, s.db, "http://t")
    assert res["TotalRecordCount"] == len(res["Items"]) >= 0
    assert res["TotalRecordCount"] != -1, "Emby 约束：总数永不为负"

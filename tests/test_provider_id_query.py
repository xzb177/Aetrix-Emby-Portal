"""AnyProviderIdEquals / HasAnyProviderId 回归（v2.58.x）。

实证事故（2026-10-15 20:32）：Reflix 请求
  GET /Users/1/Items?AnyProviderIdEquals=Tmdb.519182&IncludeItemTypes=Movie&Limit=1&Recursive=true
参数被忽略，接口按 SortName 返回全库第一部电影《007：女王密使》（tmdb 668），
Reflix 把它当《神偷奶爸4》的 Aetrix 版本挂上架，用户点播即错片。
正牌神偷奶爸4 在库：tmdb 519182、imdb tt7510222。
"""
import os
import tempfile
import uuid
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as core  # noqa: F401
from backend.database import Base
from backend.emby_server import api, models as em


class _QP(dict):
    def getlist(self, key):
        v = self.get(key)
        return [v] if v else []


def _movie(db, lib, name, tmdb=None, imdb=None):
    it = em.MediaItem(
        guid=uuid.uuid4().hex[:32], library_id=lib.id, item_type="movie",
        name=name, sort_name=name, is_hidden=False,
        date_added=datetime.now(), file_path=f"/mnt/v/{name}.mkv",
        tmdb_id=tmdb, imdb_id=imdb,
    )
    db.add(it)
    return it


@pytest.fixture
def scene():
    path = os.path.join(tempfile.mkdtemp(), "provid.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        lib = em.Library(guid="P" * 32, name="电影库", collection_type="movies", paths="")
        db.add(lib)
        db.flush()
        _movie(db, lib, "神偷奶爸4", tmdb="519182", imdb="tt7510222")
        _movie(db, lib, "007：女王密使", tmdb="668")
        _movie(db, lib, "无名之辈", tmdb="999")
        _movie(db, lib, "无编号电影")
        series = em.MediaItem(
            guid=uuid.uuid4().hex[:32], library_id=lib.id, item_type="series",
            name="好团圆", sort_name="好团圆", is_hidden=False,
            date_added=datetime.now(), tmdb_id="245500",
        )
        db.add(series)
        db.flush()
        for n in (1, 2):
            db.add(em.MediaItem(
                guid=uuid.uuid4().hex[:32], library_id=lib.id, item_type="episode",
                name=f"好团圆 第 {n} 集 S01E0{n}", sort_name=f"好团圆{n}",
                is_hidden=False, date_added=datetime.now(),
                series_id=series.id, season_number=1, episode_number=n,
            ))
        user = core.WebUser(username="u_prov", password_hash="x")
        db.add(user)
        db.commit()
        yield SimpleNamespace(db=db, user=user)


def _names(scene, **params):
    req = SimpleNamespace(query_params=_QP(params))
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(api, "_library_scope", lambda db, user: None)
        mp.setattr(api, "_prefetch_list_data", lambda *a, **k: None)
        mp.setattr(api, "_item_dto", lambda e, base, uid, db: {"Name": e.name})
        res = api._query_items(req, scene.user, scene.db, "http://test")
    return sorted(i["Name"] for i in res["Items"]), res["TotalRecordCount"]


def test_tmdb_519182_returns_only_despicable_me4(scene):
    names, total = _names(scene, AnyProviderIdEquals="Tmdb.519182",
                          IncludeItemTypes="Movie", Recursive="true")
    assert names == ["神偷奶爸4"] and total == 1


def test_tmdb_668_returns_only_007(scene):
    names, total = _names(scene, AnyProviderIdEquals="Tmdb.668",
                          IncludeItemTypes="Movie", Recursive="true")
    assert names == ["007：女王密使"] and total == 1


def test_unknown_provider_id_returns_empty_not_unfiltered(scene):
    # 查无此编号必须返回空集，绝不许退化成无过滤列表
    names, total = _names(scene, AnyProviderIdEquals="Tmdb.000000",
                          IncludeItemTypes="Movie", Recursive="true")
    assert names == [] and total == 0


def test_episode_found_by_series_provider_id(scene):
    # 单集按所属剧编号可查到（与 DTO 层 _provider_ids 继承口径一致）
    names, total = _names(scene, AnyProviderIdEquals="Tmdb.245500",
                          IncludeItemTypes="Series,Episode", Recursive="true")
    assert "好团圆" in names
    assert "好团圆 第 1 集 S01E01" in names
    assert "好团圆 第 2 集 S01E02" in names
    assert total == 3


def test_imdb_lookup(scene):
    names, total = _names(scene, AnyProviderIdEquals="Imdb.tt7510222",
                          IncludeItemTypes="Movie", Recursive="true")
    assert names == ["神偷奶爸4"] and total == 1


def test_provider_prefix_case_insensitive(scene):
    names, total = _names(scene, AnyProviderIdEquals="tmdb.519182",
                          IncludeItemTypes="Movie", Recursive="true")
    assert names == ["神偷奶爸4"] and total == 1


def test_has_any_provider_id(scene):
    names, _ = _names(scene, HasAnyProviderId="true",
                      IncludeItemTypes="Movie", Recursive="true")
    assert "无编号电影" not in names
    assert "神偷奶爸4" in names and "007：女王密使" in names

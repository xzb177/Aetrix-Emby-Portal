"""Movie 缺类型（P0-3）回归。

根因：`apply_details()` 历史上完全忽略详情接口的 `genres` 字段。
凡走「tmdb_id → 详情」分支的电影（NFO 自带 tmdb_id、别名匹配、已有 id 补缺）
都永远拿不到类型——只有搜索命中走 `apply()` 的才有。

修法：
- `apply_details` 从详情 `genres=[{id, name}]` 补类型（只补缺项，不覆盖已有，
  与「NFO 管文字」B 方案一致）；
- id→中文映射收敛为模块级 `_GENRE_NAMES` 全项目唯一一份；
- 存量回填走 `scripts/backfill_movie_genres.py`（details 两级缓存 + 令牌桶，
  不增加日常调用）。
"""
from types import SimpleNamespace

import os
import pytest
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
from backend.database import init_db

from backend.emby_server import tmdb


@pytest.fixture(scope="module", autouse=True)
def _hygiene_module_engine():
    """测试卫生：本文件独占临时库在模块夹具内换绑、模块结束原样还原。

    导入期顶层换绑会让全局 engine/SessionLocal 在 collection 后停在本文件
    的临时库上，后面的文件/用例撞 no such table 或读错库（#1668 同族）；
    换绑只在本模块用例期间生效。
    """
    _hyg_saved_engine = _dbmod.engine
    _hyg_saved_factory = _dbmod.SessionLocal._factory
    _hyg_saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"
    _dbmod.engine = _ce(os.environ["DATABASE_URL"])
    _dbmod.configure_session_local(_sm(bind=_dbmod.engine))
    init_db()
    _hyg_engine = _dbmod.engine
    try:
        yield
    finally:
        _dbmod.engine = _hyg_saved_engine
        _dbmod.configure_session_local(_hyg_saved_factory)
        if _hyg_saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _hyg_saved_url
        _hyg_engine.dispose()

def _item(**kw):
    base = dict(tmdb_id="550", last_scraped_at=None, overview=None,
                community_rating=None, primary_image_url=None,
                poster_path=None, backdrop_path=None, backdrop_image_url=None,
                aliases="", genres="", name="x", original_title=None,
                metadata_source=None, imdb_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_genres_from_details_prefers_localized_name():
    got = tmdb._genres_from_details(
        {"genres": [{"id": 28, "name": "动作"}, {"id": 18, "name": "剧情"}]})
    assert got == ["动作", "剧情"], got


def test_genres_from_details_falls_back_to_id_mapping():
    # name 缺失/为空时按 id 翻译
    got = tmdb._genres_from_details(
        {"genres": [{"id": 28, "name": ""}, {"id": 999999}]})
    assert got == ["动作"], got


def test_genres_from_details_accepts_plain_ids_and_dedupes():
    got = tmdb._genres_from_details({"genres": [28, 28, "18", 35, 12, 99]})
    assert got == ["动作", "剧情", "喜剧", "冒险"], got  # 最多 4 个，去重


def test_genres_from_details_empty_payload():
    assert tmdb._genres_from_details({}) == []
    assert tmdb._genres_from_details({"genres": []}) == []
    assert tmdb._genres_from_details({"genres": None}) == []


def test_apply_details_fills_genres_when_missing():
    """主因回归：有 tmdb_id 走详情分支时，类型必须落库"""
    item = _item(genres="")
    tmdb.TmdbClient().apply_details(
        item, {"id": 550, "genres": [{"id": 28, "name": "动作"},
                                    {"id": 18, "name": "剧情"}]})
    assert item.genres == "动作,剧情", item.genres


def test_apply_details_does_not_overwrite_existing_genres():
    """只补缺项：NFO/搜索已给的类型不能被详情覆盖（B 方案：NFO 管文字）"""
    item = _item(genres="纪录片")
    tmdb.TmdbClient().apply_details(
        item, {"id": 550, "genres": [{"id": 28, "name": "动作"}]})
    assert item.genres == "纪录片", item.genres


def test_apply_details_without_genres_in_payload_keeps_empty():
    item = _item(genres="")
    tmdb.TmdbClient().apply_details(item, {"id": 550})
    assert item.genres == "", item.genres


def test_apply_search_hit_still_maps_genre_ids():
    """搜索路径 apply() 的 genre_ids 映射行为不变（重构只搬映射表）"""
    item = _item(genres="")
    tmdb.TmdbClient().apply(
        item,
        {"id": 550, "genre_ids": [28, 12, 16, 35, 99],
         "title": "x", "overview": "", "vote_average": 7.0},
        "movie",
    )
    assert item.genres == "动作,冒险,动画,喜剧", item.genres


def test_apply_search_hit_unknown_id_falls_back_to_number():
    """映射表里没有的 id 保持历史行为：写数字原文"""
    item = _item(genres="")
    tmdb.TmdbClient().apply(
        item, {"id": 1, "genre_ids": [123456], "title": "x"}, "movie")
    assert item.genres == "123456", item.genres

"""批2（刮削瘦身）：TMDB 文字只填空、不覆盖 + 详情门控收窄 + none 终态写戳。

全部纯逻辑（前两节不碰网络、不碰数据库；第三节用 sqlite 本地库）；
``image_base`` 打桩避免读配置库。
"""
import os
import tempfile
from datetime import datetime
from types import SimpleNamespace

import pytest

# 本文件自建 sqlite 引擎（见下），不写全局环境变量、不碰 backend.database
# 的全局 engine，避免与其它测试模块的导入期重绑互相污染。
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)

from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

from backend.database import Base
from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.tmdb import TmdbClient
from backend.emby_server import enrich_worker as ew
from backend.emby_server import models as em

# 本文件测试自建 sqlite 引擎，不依赖 backend.database 的全局 engine：
# 全量跑时其它测试模块会在导入期把 _dbmod.engine 重绑到自己的文件，
# 谁后导入谁赢——用自己的 sessionmaker 就不受导入顺序影响。
_engine = _ce(f"sqlite:///{_tmppath}")
_LocalSession = _sm(bind=_engine)
# 只建表，不走 init_db()：init_db 的 _acquire_migrate_lock 按 backend.database
# 模块首次导入时的 DATABASE_TYPE 选锁实现，与 sqlite 覆盖存在导入顺序竞态
# （全量跑时别的测试模块先导入就会 pg_advisory_lock 打到 sqlite 上）。
# 本文件测试只需要表结构，create_all 幂等且无此依赖。
Base.metadata.create_all(bind=_engine)

ENV_KEY = "ENRICH_TMDB_TEXT_FILL_ONLY"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(tmdb_mod, "image_base", lambda: "https://img.example")
    monkeypatch.delenv(ENV_KEY, raising=False)
    tmdb_mod._TEXT_FILL_CACHE["at"] = 0.0
    # 进程级 60s 调参缓存：别的测试模块（如 step5）写过 enrich_field_completeness
    # 会泄漏进来，把 none 终态项拐进 -10 补队列 → pending。测试前清掉。
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None
    # 进程单例 tmdb_client 的 api_keys：key 池测试模块会塞假 key，
    # configured 恒 True 会改变 _enrich_apply 的终态分支。测试前后快照/恢复。
    _keys_snapshot = list(tmdb_mod.tmdb_client.api_keys)
    _key_snapshot = tmdb_mod.tmdb_client.api_key
    tmdb_mod.tmdb_client.api_keys = []
    tmdb_mod.tmdb_client.api_key = ""
    yield
    tmdb_mod.tmdb_client.api_keys = _keys_snapshot
    tmdb_mod.tmdb_client.api_key = _key_snapshot
    tmdb_mod._TEXT_FILL_CACHE["at"] = 0.0
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None


def _item(**kw):
    base = dict(
        tmdb_id=None, last_scraped_at=None, metadata_source=None,
        overview=None, community_rating=None, imdb_id=None,
        name=None, aliases=None, genres=None, countries=None, languages=None,
        production_year=None, premiere_date=None,
        poster_path=None, primary_image_url=None,
        backdrop_path=None, backdrop_image_url=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


_CLIENT = None


def _client() -> TmdbClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = TmdbClient()
    return _CLIENT


# ---------------- 回滚开关 ----------------

def test_text_fill_only_default_on():
    assert tmdb_mod.tmdb_text_fill_only() is True


def test_text_fill_only_env_zero_falls_back(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "0")
    tmdb_mod._TEXT_FILL_CACHE["at"] = 0.0
    assert tmdb_mod.tmdb_text_fill_only() is False


# ---------------- apply()：搜索命中 ----------------

def test_apply_keeps_existing_name_and_rating():
    item = _item(name="人工整理的标题", community_rating=9.0,
                 genres="剧情", production_year=2018)
    hit = {"id": 123, "name": "TMDB 标题", "title": "TMDB 电影名",
           "vote_average": 7.5, "genre_ids": [28],
           "first_air_date": "2021-03-05"}
    _client().apply(item, hit, "series")
    assert item.name == "人工整理的标题"
    assert item.community_rating == 9.0
    assert item.genres == "剧情"
    assert item.production_year == 2018
    assert item.tmdb_id == "123"  # 身份字段照常落


def test_apply_fills_missing_text_fields():
    item = _item()
    hit = {"id": 123, "name": "TMDB 标题", "vote_average": 0.0,
           "genre_ids": [28], "first_air_date": "2021-03-05"}
    _client().apply(item, hit, "series")
    assert item.name == "TMDB 标题"
    # 0.0 是合法评分：空缺时照样填
    assert item.community_rating == 0.0
    assert item.genres  # genre_id 映射落了分类
    assert item.production_year == 2021


def test_apply_legacy_mode_still_overwrites(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "0")
    tmdb_mod._TEXT_FILL_CACHE["at"] = 0.0
    item = _item(name="旧标题", community_rating=9.0, genres="剧情")
    hit = {"id": 123, "name": "TMDB 标题", "vote_average": 7.5,
           "genre_ids": [28]}
    _client().apply(item, hit, "series")
    assert item.name == "TMDB 标题"
    assert item.community_rating == 7.5
    assert item.genres != "剧情"


# ---------------- apply_details()：详情 ----------------

def test_apply_details_keeps_existing_text():
    item = _item(overview="NFO 写的简介", community_rating=8.5,
                 aliases="已有别名", genres="剧情")
    data = {"overview": "TMDB 简介", "vote_average": 6.1,
            "alternative_titles": {"titles": [{"title": "新别名"}]},
            "genres": [{"id": 1, "name": "动作"}]}
    _client().apply_details(item, data)
    assert item.overview == "NFO 写的简介"
    assert item.community_rating == 8.5
    assert item.genres == "剧情"
    # 别名走增量合并：旧的不丢、新的追加
    assert "已有别名" in item.aliases
    assert "新别名" in item.aliases


def test_apply_details_fills_missing_text():
    item = _item()
    data = {"overview": "TMDB 简介", "vote_average": 7.2,
            "alternative_titles": {"titles": [{"title": "别名A"}]}}
    _client().apply_details(item, data)
    assert item.overview == "TMDB 简介"
    assert item.community_rating == 7.2
    assert "别名A" in item.aliases


def test_apply_details_legacy_mode_overwrites(monkeypatch):
    monkeypatch.setenv(ENV_KEY, "0")
    tmdb_mod._TEXT_FILL_CACHE["at"] = 0.0
    item = _item(overview="旧简介", community_rating=8.5, aliases="旧别名")
    data = {"overview": "TMDB 简介", "vote_average": 6.1,
            "alternative_titles": {"titles": [{"title": "新别名"}]}}
    _client().apply_details(item, data)
    assert item.overview == "TMDB 简介"
    assert item.community_rating == 6.1
    assert item.aliases == "新别名"  # 旧行为：整体替换


# ---------------- _tmdb_plan_for_item：详情门控 ----------------

def test_plan_nfo_id_with_image_needs_no_details(monkeypatch):
    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    item = _item(poster_path="/x.jpg", imdb_id=None, aliases=None)
    plan = ew._tmdb_plan_for_item(
        item, {"tmdb_id": "999"}, "series", needs_repair=False)
    assert plan["op"] == "none"  # 有图、缺 imdb/别名不再是拉详情的理由


def test_plan_nfo_id_without_image_still_fetches_details(monkeypatch):
    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    item = _item(poster_path=None, primary_image_url=None)
    plan = ew._tmdb_plan_for_item(
        item, {"tmdb_id": "999"}, "series", needs_repair=False)
    assert plan["op"] == "details"
    assert plan["tmdb_id"] == "999"


def test_plan_nfo_id_repair_fetches_details(monkeypatch):
    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    item = _item(poster_path="/x.jpg")
    plan = ew._tmdb_plan_for_item(
        item, {"tmdb_id": "999"}, "series", needs_repair=True)
    assert plan["op"] == "details"


# ---------------- none 终态写 last_scraped_at ----------------

def _db_item(**kw):
    import uuid
    db = _LocalSession()
    try:
        base = dict(guid=f"test-{uuid.uuid4().hex}", library_id=1,
                    name="测试剧", item_type="series", enrich_status="pending",
                    enrich_attempts=0)
        base.update(kw)
        item = em.MediaItem(**base)
        db.add(item)
        db.commit()
        db.refresh(item)
        return item.id
    finally:
        db.close()


def test_none_terminal_writes_last_scraped_at():
    """搜过无命中（但 TMDB 未配置→走 _incomplete=False 路径）终态写戳。

    fetched 为空：series 无 tmdb_id、无命中 → metadata_source='none' →
    终态 done。last_scraped_at 必须有值（与「从未跑过」区分）。
    """
    item_id = _db_item(production_year=2020)  # 标题+年份=核心字段齐→走终态
    db = _LocalSession()
    try:
        item = db.query(em.MediaItem).filter(
            em.MediaItem.id == item_id).first()
        assert item.last_scraped_at is None
        ew._enrich_apply(db, item, {})
        db.commit()
        db.refresh(item)
        assert item.enrich_status == "done"
        assert item.metadata_source == "none"
        assert isinstance(item.last_scraped_at, datetime)
    finally:
        db.close()


def test_normal_done_does_not_rewrite_existing_stamp():
    """已有 last_scraped_at 的 done 项：终态不再乱写（幂等）。"""
    stamp = datetime(2026, 1, 1, 12, 0, 0)
    item_id = _db_item(last_scraped_at=stamp, tmdb_id="123",
                       metadata_source="tmdb", enrich_status="done")
    db = _LocalSession()
    try:
        item = db.query(em.MediaItem).filter(
            em.MediaItem.id == item_id).first()
        ew._enrich_apply(db, item, {})
        db.commit()
        db.refresh(item)
        assert item.last_scraped_at == stamp
    finally:
        db.close()

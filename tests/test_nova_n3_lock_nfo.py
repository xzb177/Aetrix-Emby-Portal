"""Nova N3-2：锁定居首 + NFO 应用条件（NovaEmby 全面学习立项 N3）。

Nova 口径（novaemby-deep-raw §3b 字段优先级）：锁定 > NFO > 在线来源 >
文件名。Aetrix 此前两处与该优先级冲突：

- 缺口 1（锁定不居首）：``metadata_locked`` 只在 enrich 抢单/写库处被
  跳过，扫描期 ``apply_nfo`` 路径不查锁定——重扫时 NFO 仍能改锁定条目
  的文字，N3-1 新增的演员落库同样能给锁定条目新增/补全演员行。
- 缺口 2（NFO 应用条件）：enrich 的 nfo-only 分支是
  ``elif nfo_data and not item.overview``——无 TMDB 命中且条目已有
  （自动来源的）简介时，NFO 整体不应用，NFO 反而低于自动来源。

本文件钉住新口径：

- 锁定条目过 ``apply_nfo``（含 db 演员落库）后字段与演员行零变化；
- 未锁定、已有简介、无 TMDB 命中、NFO 在场时，enrich nfo-only 路径
  按 NFO 生效（文字以 NFO 为准）；
- 未锁定常规 NFO 应用（直调与 enrich 缺简介分支）回归不变。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import enrich_worker
from backend.emby_server import models as em
from backend.emby_server import nfo as nfo_lib

init_db()

MOVIE_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>NFO 权威标题</title>
  <originaltitle>NFO Original</originaltitle>
  <plot>NFO 权威简介</plot>
  <year>2019</year>
  <rating>8.7</rating>
  <genre>剧情</genre>
  <genre>悬疑</genre>
  <uniqueid type="tmdb" default="true">999003</uniqueid>
  <actor>
    <name>NFO Actor One</name>
    <role>Lead Role</role>
    <type>Actor</type>
  </actor>
  <actor>
    <name>NFO Actor Two</name>
    <role>Support Role</role>
  </actor>
</movie>
"""


def _clear(db, item_ids):
    db.rollback()
    if item_ids:
        db.query(em.EmbyPerson).filter(
            em.EmbyPerson.item_id.in_(item_ids)).delete(
                synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(item_ids)).delete(synchronize_session=False)
    db.commit()


@pytest.fixture()
def db():
    s = SessionLocal()
    made = []
    try:
        yield s, made
        _clear(s, made)
    finally:
        s.close()


def _make_item(s, made, **kw):
    base = dict(
        guid=uuid.uuid4().hex, library_id=1, item_type="movie",
        name="原始标题", file_path="/tmp/n3-lock.mp4",
        enrich_status="pending",
    )
    base.update(kw)
    it = em.MediaItem(**base)
    s.add(it)
    s.commit()
    made.append(it.id)
    return it


def _people(s, item_id):
    return s.query(em.EmbyPerson).filter(
        em.EmbyPerson.item_id == item_id).order_by(
            em.EmbyPerson.sort_order, em.EmbyPerson.id).all()


def _snapshot(item):
    return {
        "name": item.name,
        "sort_name": item.sort_name,
        "original_title": item.original_title,
        "overview": item.overview,
        "community_rating": item.community_rating,
        "genres": item.genres,
        "studios": item.studios,
        "official_rating": item.official_rating,
        "production_year": item.production_year,
        "tmdb_id": item.tmdb_id,
        "imdb_id": item.imdb_id,
        "aliases": item.aliases,
        "metadata_source": item.metadata_source,
        "last_scraped_at": item.last_scraped_at,
    }


# ==================== a) 锁定居首 ====================

def test_locked_item_apply_nfo_changes_nothing(db):
    """锁定条目 + NFO 改名/改简介/带演员：应用后字段与演员行零变化"""
    s, made = db
    item = _make_item(
        s, made, name="锁定原名", overview="锁定原简介",
        community_rating=7.1, genres="纪录", metadata_source="tmdb",
        metadata_locked=True)
    before = _snapshot(item)
    data = nfo_lib.parse_nfo(MOVIE_NFO)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    s.refresh(item)
    assert _snapshot(item) == before
    assert _people(s, item.id) == []


def test_locked_item_apply_nfo_does_not_fill_cast_role(db):
    """锁定条目已有同名演员行（role 空）：NFO 也不许补 role/头像"""
    s, made = db
    item = _make_item(s, made, metadata_locked=True)
    s.add(em.EmbyPerson(
        item_id=item.id, name="NFO Actor One", role="", image="",
        sort_order=0, person_tmdb_id=""))
    s.commit()
    data = nfo_lib.parse_nfo(MOVIE_NFO)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    rows = _people(s, item.id)
    assert len(rows) == 1
    assert rows[0].role == ""
    assert rows[0].image == ""


# ==================== b) enrich nfo-only 应用条件 ====================

def _fetched_nfo_only(data):
    return {
        "poster": None, "fanart": None, "external_subs": [],
        "nfo_data": data, "tmdb_hit": None, "tmdb_details": None,
        "tmdb_id": None, "ok": True,
    }


def test_enrich_nfo_only_applies_despite_existing_overview(db):
    """未锁定、已有简介、无 TMDB 命中、NFO 在场：文字按 NFO 生效"""
    s, made = db
    item = _make_item(
        s, made, name="自动来源标题", overview="自动来源的旧简介",
        metadata_locked=False)
    data = nfo_lib.parse_nfo(MOVIE_NFO)
    enrich_worker._enrich_apply(s, item, _fetched_nfo_only(data))
    s.commit()
    s.refresh(item)
    assert item.name == "NFO 权威标题"
    assert item.overview == "NFO 权威简介"
    assert item.community_rating == 8.7
    assert item.metadata_source == "nfo"
    # N3-1 的演员落库在 enrich 路径同样生效（未锁定）
    assert [r.name for r in _people(s, item.id)] == [
        "NFO Actor One", "NFO Actor Two"]


# ==================== c) 未锁定回归 ====================

def test_unlocked_apply_nfo_still_applies(db):
    """未锁定直调 apply_nfo：文字与演员照常落（回归不变）"""
    s, made = db
    item = _make_item(s, made, overview="旧简介", metadata_locked=False)
    data = nfo_lib.parse_nfo(MOVIE_NFO)
    nfo_lib.apply_nfo(item, data, "movie", db=s)
    s.commit()
    s.refresh(item)
    assert item.name == "NFO 权威标题"
    assert item.overview == "NFO 权威简介"
    assert item.community_rating == 8.7
    assert [r.name for r in _people(s, item.id)] == [
        "NFO Actor One", "NFO Actor Two"]


def test_enrich_nfo_only_applies_when_overview_empty(db):
    """未锁定、简介为空的旧分支场景：enrich nfo-only 照常生效（回归）"""
    s, made = db
    item = _make_item(s, made, name="无简介条目", overview="",
                      metadata_locked=False)
    data = nfo_lib.parse_nfo(MOVIE_NFO)
    enrich_worker._enrich_apply(s, item, _fetched_nfo_only(data))
    s.commit()
    s.refresh(item)
    assert item.name == "NFO 权威标题"
    assert item.overview == "NFO 权威简介"

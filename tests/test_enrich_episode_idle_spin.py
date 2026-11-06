"""单集空转最小修复：父级未终态不认领、打回不提权、托管单集走纯继承。

三条都有 env 开关，默认开。本文件钉默认行为，并用开关关掉对照旧口径。
"""
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_worker
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _lib(db):
    lib = em.Library(guid=uuid.uuid4().hex, name="剧集库",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    return lib


def _item(db, lib, **kw):
    name = kw.pop("name", "条目")
    kw.setdefault("enrich_status", "pending")
    kw.setdefault("item_type", "movie")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, name=name,
        sort_name=name, file_path=kw.pop("file_path", f"/m/{uuid.uuid4().hex}.mkv"),
        **kw)
    db.add(item)
    db.flush()
    return item


def _ids(rows):
    return {row.id for row in rows}


def test_parent_ready_clause_is_exists_on_sqlite_and_pg():
    """认领谓词在 SQLite 与 PG 上都是 EXISTS 反连接，不是两套手写 SQL。"""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    stmt = select(em.MediaItem.id).where(
        enrich_worker._episode_parent_ready_clause())
    for dialect in (sqlite_dialect.dialect(), postgresql.dialect()):
        sql = str(stmt.compile(
            dialect=dialect, compile_kwargs={"literal_binds": True}))
        folded = sql.upper()
        assert "EXISTS" in folded
        assert "'PENDING'" in folded
        assert "'ENRICHING'" in folded
        assert "EPISODE" in folded
        assert "DELETED_AT" in folded
        # 已合并父级不挡：谓词里必须出现 merged_into_id 排除条件。
        assert "MERGED_INTO_ID" in folded


def test_pending_parent_episode_not_claimed_until_terminal(db):
    """父级 pending/enriching 时单集不进 identify 也不进 fill；终态后可以。"""
    lib = _lib(db)
    series = _item(db, lib, item_type="series", name="未完成的剧",
                   file_path=None, enrich_status="pending")
    season = _item(db, lib, item_type="season", name="第 1 季",
                   file_path=None, series_id=series.id, parent_id=series.id,
                   season_number=1, enrich_status="done")
    identify_ep = _item(
        db, lib, item_type="episode", name="识别队列单集",
        series_id=series.id, parent_id=season.id, season_number=1,
        episode_number=1, enrich_priority=-10)
    fill_ep = _item(
        db, lib, item_type="episode", name="刮削队列单集",
        series_id=series.id, parent_id=season.id, season_number=1,
        episode_number=2, tmdb_id="1399", enrich_priority=-10)
    db.commit()

    identify = _ids(enrich_worker._claim_batch(db, 10, queue="identify"))
    assert series.id in identify
    assert identify_ep.id not in identify
    assert fill_ep.id not in identify
    assert _ids(enrich_worker._claim_batch(db, 10, queue="fill")) == set()
    db.refresh(identify_ep)
    db.refresh(fill_ep)
    assert identify_ep.enrich_status == "pending"
    assert fill_ep.enrich_status == "pending"
    assert identify_ep.enrich_priority == -10

    # 父级被认领后停在 enriching，单集仍然不能进。
    db.refresh(series)
    assert series.enrich_status == "enriching"
    assert identify_ep.id not in _ids(
        enrich_worker._claim_batch(db, 10, queue="identify"))

    db.refresh(series)
    series.enrich_status = "done"
    db.commit()
    identify = _ids(enrich_worker._claim_batch(db, 10, queue="identify"))
    fill = _ids(enrich_worker._claim_batch(db, 10, queue="fill"))
    assert identify_ep.id in identify
    assert fill_ep.id in fill


def test_missing_or_deleted_parent_does_not_block_episode(db):
    """父级行不存在，或已软删：按无父级放行，不永久挡住。"""
    lib = _lib(db)
    dangling = _item(
        db, lib, item_type="episode", name="父级不存在",
        series_id=999999, parent_id=888888, enrich_priority=-10)
    deleted_series = _item(
        db, lib, item_type="series", name="已删剧", file_path=None,
        enrich_status="pending", deleted_at=datetime.now())
    deleted_child = _item(
        db, lib, item_type="episode", name="父级已删",
        series_id=deleted_series.id, parent_id=deleted_series.id)
    failed_series = _item(
        db, lib, item_type="series", name="失败剧", file_path=None,
        enrich_status="failed")
    failed_child = _item(
        db, lib, item_type="episode", name="父级 failed",
        series_id=failed_series.id)
    # commit 之后软删钩子会把已删父级从会话里藏掉，id 必须先记下。
    deleted_series_id = deleted_series.id
    dangling_id, deleted_child_id, failed_child_id = (
        dangling.id, deleted_child.id, failed_child.id)
    db.commit()

    claimed = _ids(enrich_worker._claim_batch(db, 10))
    assert dangling_id in claimed
    assert deleted_child_id in claimed
    assert failed_child_id in claimed
    assert deleted_series_id not in claimed


def test_pending_season_blocks_even_if_series_done(db):
    """series 已终态但 season 仍 pending 时，单集继续等。季终态后可认领。"""
    lib = _lib(db)
    series = _item(db, lib, item_type="series", name="剧已完成",
                   file_path=None, enrich_status="done", tmdb_id="42")
    season = _item(db, lib, item_type="season", name="季未完成",
                   file_path=None, series_id=series.id, parent_id=series.id,
                   season_number=1, enrich_status="pending")
    ep = _item(db, lib, item_type="episode", name="等季的集",
               series_id=series.id, parent_id=season.id,
               season_number=1, episode_number=1)
    db.commit()

    claimed = enrich_worker._claim_batch(db, 10)
    assert [row.id for row in claimed] == [season.id]
    db.refresh(ep)
    assert ep.enrich_status == "pending"

    db.refresh(season)
    season.enrich_status = "done"
    db.commit()
    claimed = enrich_worker._claim_batch(db, 10)
    assert [row.id for row in claimed] == [ep.id]


def test_parent_ready_switch_off_claims_episode_with_pending_parent(db, monkeypatch):
    """开关关掉：回到父级 pending 也把单集一起抢走的旧口径。"""
    monkeypatch.setattr(enrich_worker, "ENRICH_CLAIM_EPISODE_PARENT_READY", 0)
    lib = _lib(db)
    series = _item(db, lib, item_type="series", name="未完成", file_path=None)
    ep = _item(db, lib, item_type="episode", name="跟着抢",
               series_id=series.id, parent_id=series.id, episode_number=1)
    db.commit()
    claimed = _ids(enrich_worker._claim_batch(db, 10))
    assert claimed == {series.id, ep.id}


def test_parent_wait_keeps_priority_and_does_not_mark_none(db):
    """等父级打回：优先级保持 -10，连打 21 次也不转 none。"""
    enrich_worker._parent_wait_counts.clear()
    lib = _lib(db)
    series = _item(db, lib, item_type="series", name="还在等",
                   file_path=None, enrich_status="pending")
    ep = _item(db, lib, item_type="episode", name="被打回",
               series_id=series.id, enrich_status="enriching",
               enrich_priority=-10, enrich_claimed_at=datetime.now(),
               enrich_claim_token="tok", metadata_source=None)
    db.commit()
    fetched = {
        "ok": True, "pure_inherit": False, "poster": None, "fanart": None,
        "external_subs": [], "nfo_data": None, "tmdb_hit": None,
        "tmdb_details": None,
    }
    for _ in range(21):
        ep.enrich_status = "enriching"
        enrich_worker._enrich_apply(db, ep, fetched)
        assert ep.enrich_status == "pending"
        assert ep.metadata_source is None
        assert ep.enrich_priority == -10
    assert ep.enrich_next_retry_at is not None
    delta = (ep.enrich_next_retry_at - datetime.now()).total_seconds()
    assert delta <= enrich_worker._PARENT_WAIT_MAX_SEC + 5


def test_parent_wait_switch_off_raises_priority_and_caps(monkeypatch):
    """开关关掉：打回把优先级抬到 0，第 21 次放行走 none。"""
    monkeypatch.setattr(enrich_worker, "ENRICH_PARENT_WAIT_NO_FALSE_NONE", 0)
    enrich_worker._parent_wait_counts.clear()
    item = SimpleNamespace(
        id=7, series_id=3, enrich_status="enriching",
        enrich_next_retry_at=None, enrich_claimed_at="x",
        enrich_claim_token="t", enrich_priority=-10)
    parent = SimpleNamespace(enrich_status="pending")

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return parent

    db = SimpleNamespace(query=lambda *a, **k: _Q())
    assert enrich_worker._parent_should_wait(db, item) is True
    assert item.enrich_priority == 0
    for _ in range(18):
        assert enrich_worker._parent_should_wait(db, item) is True
    assert enrich_worker._parent_should_wait(db, item) is True  # 第 20 次仍等
    assert enrich_worker._parent_should_wait(db, item) is False  # 第 21 次放行


def test_upstream_episode_without_file_inherits_parent(db, monkeypatch):
    """无 file_path 的上游托管单集走纯继承，落 inherit 而不是 none。"""
    monkeypatch.setattr(enrich_worker, "_fetch_episode_tmdb", lambda *a, **k: None)
    lib = _lib(db)
    series = _item(
        db, lib, item_type="series", name="父级已完成", file_path=None,
        enrich_status="done", tmdb_id="777", poster_path="/img/p.jpg")
    ep = _item(
        db, lib, item_type="episode", name="托管第 1 集", file_path=None,
        upstream_source_id=7, series_id=series.id, parent_id=series.id,
        season_number=1, episode_number=1, enrich_status="enriching",
        metadata_source=None)
    db.commit()
    info = enrich_worker._inherit_parent_info(db, ep)
    assert info is not None and info["tmdb_id"] == "777"
    snap = enrich_worker._snapshot_item(ep)
    assert snap.upstream_source_id == 7
    assert snap.file_path is None
    result, ctx = enrich_worker._enrich_fetch_pre(
        snap, holder={}, inherit_parent=info)
    assert result.get("pure_inherit") is True
    assert ctx is None
    enrich_worker._enrich_apply(db, ep, result)
    db.commit()
    assert ep.enrich_status == "done"
    assert ep.metadata_source == "inherit"


def test_upstream_episode_inherit_switch_off_stays_off_pure_inherit(db, monkeypatch):
    """开关关掉：无 file_path 的托管单集不进纯继承。"""
    monkeypatch.setattr(enrich_worker, "ENRICH_UPSTREAM_EPISODE_INHERIT", 0)
    monkeypatch.setattr(enrich_worker, "_fetch_episode_tmdb", lambda *a, **k: None)
    lib = _lib(db)
    series = _item(
        db, lib, item_type="series", name="父级已完成", file_path=None,
        enrich_status="done", tmdb_id="778", poster_path="/img/p.jpg")
    ep = _item(
        db, lib, item_type="episode", name="托管第 2 集", file_path=None,
        upstream_source_id=7, series_id=series.id,
        season_number=1, episode_number=2, enrich_status="enriching")
    db.commit()
    info = enrich_worker._inherit_parent_info(db, ep)
    snap = enrich_worker._snapshot_item(ep)
    result, ctx = enrich_worker._enrich_fetch_pre(
        snap, holder={}, inherit_parent=info)
    assert not result.get("pure_inherit")
    assert ctx is not None


def test_merged_parent_does_not_block_episode_claim(db):
    """已合并父级不再挡单集：series/season 已合并且仍 pending，单集可认领。

    回归：2026-11-07 首晚 0 认领——生产 1,016 个已合并 series 与 544 个
    已合并 season 仍挂 pending，挡住全部 34,186 条待补单集。已合并父级
    永远不会被认领做完，等它等于永远等，不应挡。
    """
    lib = _lib(db)
    survivor = _item(db, lib, item_type="series", name="存活剧",
                     file_path=None, enrich_status="done", tmdb_id="42")
    survivor_season = _item(
        db, lib, item_type="season", name="存活季", file_path=None,
        series_id=survivor.id, parent_id=survivor.id, season_number=1,
        enrich_status="done")
    merged_series = _item(
        db, lib, item_type="series", name="已合并剧", file_path=None,
        enrich_status="pending", merged_into_id=survivor.id)
    merged_season = _item(
        db, lib, item_type="season", name="已合并季", file_path=None,
        series_id=merged_series.id, parent_id=merged_series.id,
        season_number=1, enrich_status="pending",
        merged_into_id=survivor_season.id)
    ep = _item(db, lib, item_type="episode", name="被合并父级挡住的集",
               series_id=merged_series.id, parent_id=merged_season.id,
               season_number=1, episode_number=1, enrich_priority=-10)
    db.commit()

    claimed = _ids(enrich_worker._claim_batch(db, 10))
    assert ep.id in claimed
    assert merged_series.id not in claimed
    assert merged_season.id not in claimed


def test_merged_series_does_not_block_but_live_season_does(db):
    """边界：series 已合并放行，但活的 season 仍 pending 时单集继续等。"""
    lib = _lib(db)
    survivor = _item(db, lib, item_type="series", name="存活剧",
                     file_path=None, enrich_status="done", tmdb_id="42")
    merged_series = _item(
        db, lib, item_type="series", name="已合并剧", file_path=None,
        enrich_status="pending", merged_into_id=survivor.id)
    live_season = _item(
        db, lib, item_type="season", name="活的季", file_path=None,
        series_id=merged_series.id, parent_id=merged_series.id,
        season_number=1, enrich_status="pending")
    ep = _item(db, lib, item_type="episode", name="等活季的集",
               series_id=merged_series.id, parent_id=live_season.id,
               season_number=1, episode_number=1, enrich_priority=-10)
    db.commit()

    claimed = _ids(enrich_worker._claim_batch(db, 10))
    assert ep.id not in claimed
    assert live_season.id in claimed

    db.refresh(live_season)
    live_season.enrich_status = "done"
    db.commit()
    claimed = _ids(enrich_worker._claim_batch(db, 10))
    assert ep.id in claimed


def test_parent_should_wait_ignores_merged_parent(db):
    """处理时打回同口径：父级已合并时 _parent_should_wait 不等。

    只修认领谓词不够——认领后这里会把单集再打回 pending 空转。
    """
    enrich_worker._parent_wait_counts.clear()
    lib = _lib(db)
    survivor = _item(db, lib, item_type="series", name="存活剧",
                     file_path=None, enrich_status="done", tmdb_id="42")
    merged_series = _item(
        db, lib, item_type="series", name="已合并剧", file_path=None,
        enrich_status="pending", merged_into_id=survivor.id)
    ep = _item(db, lib, item_type="episode", name="父级已合并的集",
               series_id=merged_series.id, enrich_status="enriching",
               enrich_priority=-10, enrich_claimed_at=datetime.now(),
               enrich_claim_token="tok", metadata_source=None,
               tmdb_id="1399", poster_path="/img/e.jpg")
    db.commit()

    assert enrich_worker._parent_should_wait(db, ep) is False

    fetched = {
        "ok": True, "pure_inherit": False, "poster": None, "fanart": None,
        "external_subs": [], "nfo_data": None, "tmdb_hit": None,
        "tmdb_details": None,
    }
    enrich_worker._enrich_apply(db, ep, fetched)
    assert ep.enrich_status != "pending"
    assert ep.metadata_source == "none"

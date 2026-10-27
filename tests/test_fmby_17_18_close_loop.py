"""FMBY #17 / #18 闭环测试（2026-10-27 第 3 路）。

#17 钉死：
① 三开关默认开（两段式与两队列已是真实路径，不是实验开关）；
② 写库分发唯一入口 _apply_write_phase：两队列关 + 两段开时批量
   路径也不再绕回现行写库（闭环前批量路径只认两队列开关的缺口）；
③ 全关时仍回现行写库（旧行为可显式关回去，不是删掉）。

#18 钉死：
④ v1 词表冻存原文：v1 不剥 adweb 与声道多词条，v2 剥；两版哈希不同；
⑤ rule_pack_status 可查：旧规则产物数与各版记录分布；
⑥ 回滚：电影条目从文件名用 v1 重解析恢复被 v2 剥掉的词、回 pending，
   且不改显示名 name；剧条目跳过；不给目标包直接 ValueError。
"""

import re
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_queues as eq
from backend.emby_server import enrich_worker as ew
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em
from backend.emby_server import rule_pack as rp


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _collapse(text):
    return re.sub(r"\s+", " ", text).strip(" .-_") if text else text


def _make_item(db, scan_title=None, item_type="movie", file_path=None, **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="电影库",
                     collection_type="movies")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "done")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
        name="Film", sort_name="Film",
        file_path=file_path or f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2021, scan_title=scan_title, **kw)
    db.add(item)
    db.flush()
    return item


def _stage_record(db, item, version, rule_pack_hash):
    rec = em.EnrichStageRecord(item_id=item.id, stage="identified",
                               provider="tmdb", external_id="60300",
                               confidence="search",
                               rule_pack_version=version,
                               rule_pack_hash=rule_pack_hash)
    db.add(rec)
    db.flush()
    return rec


# ---- #17 ----

def test_two_stage_and_two_queue_defaults_on():
    assert ts.IDENTIFY_STAGE_ENABLED == 1
    assert ts.FILL_STAGE_ENABLED == 1
    assert eq.TWO_QUEUE_ENABLED == 1
    assert ts.any_enabled()
    assert eq.two_queue_enabled()


def test_write_phase_dispatch(monkeypatch):
    calls = {}

    def _legacy(db, item, fetched):
        calls["legacy"] = True

    def _two_stage(db, item, fetched, identify=None, fill=None):
        calls["two_stage"] = (identify, fill)

    monkeypatch.setattr(ew, "_enrich_apply", _legacy)
    monkeypatch.setattr(ew, "_enrich_apply_two_stage", _two_stage)
    # 两队列关 + 两段开：批量路径经此分发必须走两段，不是现行写库。
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 1)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 0)
    ew._apply_write_phase(None, object(), {}, queue=None)
    assert calls == {"two_stage": (None, None)}
    # 两队列模式：一次认领只跑一段。
    calls.clear()
    ew._apply_write_phase(None, object(), {}, queue="fill")
    assert calls == {"two_stage": (False, True)}
    # 全关：回现行写库。
    calls.clear()
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 0)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 0)
    ew._apply_write_phase(None, object(), {}, queue=None)
    assert calls == {"legacy": True}
    # 未知队列：不静默回落。
    with pytest.raises(ValueError):
        ew._apply_write_phase(None, object(), {}, queue="bogus")


# ---- #18 ----

def test_pack_versions_frozen():
    v1 = rp.pack_for_version("v1")
    v2 = rp.pack_for_version("v2")
    assert v1.version == "v1" and v2.version == "v2"
    assert v1.content_hash != v2.content_hash
    # v1 不剥 adweb 与声道多词条（v2 新增的那 7 条），v2 剥。
    assert _collapse(v1.strip("Film ADWeb 1080p")) == "Film ADWeb"
    assert _collapse(v2.strip("Film ADWeb 1080p")) == "Film"
    assert _collapse(v1.strip("Film DDP 2 0")) == "Film 2 0"
    assert _collapse(v2.strip("Film DDP 2 0")) == "Film"
    with pytest.raises(ValueError):
        rp.pack_for_version("v99")


def test_rule_pack_status_queryable(db):
    item = _make_item(db, scan_title="Film")
    _stage_record(db, item, "v2", "stale-hash")
    legacy = _make_item(db, scan_title="Other")
    _stage_record(db, legacy, None, None)
    status = rp.rule_pack_status(db)
    assert status["pack_version"] == rp.PACK_VERSION
    assert status["stale_count"] == 1
    assert status["records_by_version"]["v2"] == 1
    assert status["records_by_version"]["unknown"] == 1


def test_rollback_restores_movie_title_from_filename(db):
    item = _make_item(
        db, scan_title="Film", file_path="/m/Film ADWeb 1080p.mkv")
    _stage_record(db, item, "v2", "stale-hash")
    report = rp.replay_pack(
        db, dry_run=False, target_pack=rp.pack_for_version("v1"),
        rollback_from_path=True)
    assert report["requeued"] == 1
    db.flush()
    db.refresh(item)
    # scan_title 快照恢复为 v1 重解析结果，显示名 name 不动（不做回填）。
    assert item.scan_title == "Film ADWeb"
    assert item.name == "Film"
    assert item.enrich_status == "pending"


def test_rollback_skips_series_and_requires_target(db):
    series = _make_item(db, item_type="series", scan_title="Show",
                        file_path="/m/Show S01E01 1080p.mkv")
    _stage_record(db, series, "v2", "stale-hash")
    report = rp.replay_pack(
        db, dry_run=False, target_pack=rp.pack_for_version("v1"),
        rollback_from_path=True)
    assert report["skipped_non_movie"] == 1
    assert report["requeued"] == 0
    db.refresh(series)
    assert series.scan_title == "Show"
    with pytest.raises(ValueError):
        rp.replay_pack(db, dry_run=True, rollback_from_path=True)
    with pytest.raises(ValueError):
        rp.replay_pack(db, dry_run=True,
                       target_pack=rp.RulePack(version="v99"),
                       rollback_from_path=True)

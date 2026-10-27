"""B 批（扫描/刮削稳健性）：两队列身份“空”口径一致性回归。

Bug（复现见报告）：Python 侧 queue_of 用 strip 口径（纯空白算未绑定），
而 SQL 侧 identify_filter 只认 NULL 与 ""——纯空白身份（如 tmdb_id="  "）
会被刮削队列抢走、queue_of 却判为识别队列；同时 _enrich_apply_two_stage
用裸 bool() 判 bound，空白身份会跳过“未定身份回落现行终态”分支、走到
识别段尾的回 pending。两处合起来，空白身份条目在两段之间无限空转、
永不终态。本文件钉死：抢单口径与分段口径对空白身份一致，且空白身份
条目经识别段能在有限轮次内终态化。
"""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_queues as eq
from backend.emby_server import enrich_worker
from backend.emby_server import models as em


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


def _make_item(db, name="某剧", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="剧集库",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="series",
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2021, **kw)
    db.add(item)
    db.flush()
    return item


def _reload(db, item_id):
    db.expire_all()
    return db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()


def test_whitespace_identity_claimed_by_identify_not_fill(db):
    ws_tmdb = _make_item(db, name="空白tmdb", tmdb_id="  ")
    ws_imdb = _make_item(db, name="空白imdb", imdb_id="\t")
    bound = _make_item(db, name="已绑定", tmdb_id="60300")
    db.commit()

    assert eq.queue_of(SimpleNamespace(tmdb_id="  ", imdb_id=None)) == "identify"
    assert eq.queue_of(SimpleNamespace(tmdb_id=None, imdb_id="\t")) == "identify"

    identify_ids = {r.id for r in enrich_worker._claim_batch(db, 10, queue="identify")}
    assert identify_ids == {ws_tmdb.id, ws_imdb.id}
    fill_ids = {r.id for r in enrich_worker._claim_batch(db, 10, queue="fill")}
    assert fill_ids == {bound.id}


def test_whitespace_identity_terminalizes_via_identify_segment(db):
    # 标题+年份齐：与 test_enrich_two_queues 的未绑定终态用例同口径，
    # 只是身份列是纯空白而非 NULL——修前走识别段尾回 pending 空转。
    item = _make_item(db, tmdb_id="  ")
    db.commit()
    terminal = None
    for _ in range(8):
        current = _reload(db, item.id)
        if current.enrich_status in ("done", "failed"):
            terminal = current.enrich_status
            break
        enrich_worker._enrich_apply_two_stage(
            db, current, {}, identify=True, fill=False)
        db.commit()
    else:
        terminal = _reload(db, item.id).enrich_status
    assert terminal in ("done", "failed")

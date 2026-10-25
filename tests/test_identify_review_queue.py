"""低置信度识别人工审查队列（2026-10-24，FMBY 学习批二 #15 / 魂 1 身份与
证据先行、魂 6 状态集中维护）。

三纪律钉死：
① 枚举与约束同源：reason/status 允许值只在 models 的
   IDENTIFY_REVIEW_REASONS / IDENTIFY_REVIEW_STATUSES 定义，DB 的
   CheckConstraint SQL 由同一元组拼出——本文件断言约束 SQL 与元组逐字
   一致，且 DB 层真的拒非法值（FMBY 的 reason_code 漂移 bug 不许重演）。
② 写队列失败响亮报错：入队 DB 失败抛 ReviewEnqueueError + ERROR 日志，
   maybe_enqueue 不许吞；非法 reason 直接 ValueError。
③ 整链端到端：低置信条目 → 识别收尾入队 → 管理端列表/详情可见 →
   处置绑定（item.tmdb_id 落上、审查行 bound）/ 忽略 / 重跑（条目回
   pending 队列）→ 重复处置被拒。
"""
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import admin_identify_review as admin_review
from backend.emby_server import identify_review as irev
from backend.emby_server import models as em
from backend.emby_server.tmdb import TmdbClient


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


@pytest.fixture()
def staff():
    return SimpleNamespace(id=7, is_staff=True, username="admin")


def _make_item(db, name="某冷门剧", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="剧集库",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="series",
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2021, enrich_status="done", metadata_source="none",
        enrich_attempts=1, **kw)
    db.add(item)
    db.flush()
    return item


class _FakeTmdb:
    """识别当刻的 TMDB 替身：候选全是「被置信线拒掉」的低置信候选。"""

    def __init__(self, candidates):
        self._candidates = candidates

    def scored_candidates(self, name, year, kind):
        return list(self._candidates)


_CANDS = [
    {"tmdb_id": 111, "title": "某冷门剧 特别篇", "original_title": "",
     "year": "2021", "tier": None, "rank": None, "popularity": 3.2,
     "poster_path": "/a.jpg"},
    {"tmdb_id": 222, "title": "某冷门剧场版", "original_title": "",
     "year": "2020", "tier": None, "rank": None, "popularity": 1.1,
     "poster_path": None},
]


# ---------- 纪律①：枚举与约束同源 ----------

def test_constraint_sql_derived_from_constants():
    from sqlalchemy import CheckConstraint
    texts = {str(c.sqltext) for c in em.IdentifyReviewTask.__table__.constraints
             if isinstance(c, CheckConstraint)}
    assert em._in_sql("reason", em.IDENTIFY_REVIEW_REASONS) in texts
    assert em._in_sql("status", em.IDENTIFY_REVIEW_STATUSES) in texts


def test_db_rejects_values_outside_allowed_sets(db):
    task = em.IdentifyReviewTask(item_id=1, reason="SourceVerificationSkipped",
                                 status="pending")
    db.add(task)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    task2 = em.IdentifyReviewTask(item_id=1, reason="no_confident_match",
                                  status="whatever")
    db.add(task2)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_enqueue_rejects_unknown_reason(db):
    item = _make_item(db)
    with pytest.raises(ValueError):
        irev.enqueue_review(db, item, "not_a_reason", {})
    assert db.query(em.IdentifyReviewTask).count() == 0


# ---------- 纪律②：入队失败响亮报错 ----------

def test_enqueue_failure_raises_and_logs(db, monkeypatch, caplog):
    item = _make_item(db)

    def _boom(*a, **kw):
        raise SQLAlchemyError("disk gone")

    monkeypatch.setattr(db, "flush", _boom)
    with caplog.at_level(logging.ERROR, logger=irev.logger.name):
        with pytest.raises(irev.ReviewEnqueueError):
            irev.enqueue_review(db, item, "no_confident_match", {})
    assert any("入队失败" in r.message and r.levelno == logging.ERROR
               for r in caplog.records)


def test_maybe_enqueue_propagates_enqueue_failure(db, monkeypatch):
    item = _make_item(db)
    monkeypatch.setattr(irev, "IDENTIFY_REVIEW_QUEUE_ENABLED", 1)

    def _boom(*a, **kw):
        raise SQLAlchemyError("disk gone")

    monkeypatch.setattr(db, "flush", _boom)
    with pytest.raises(irev.ReviewEnqueueError):
        irev.maybe_enqueue_terminal_none(db, item, _FakeTmdb(_CANDS))


# ---------- 入队门槛与幂等 ----------

def test_maybe_enqueue_gating(db, monkeypatch):
    item = _make_item(db)
    # 开关关：不入队
    monkeypatch.setattr(irev, "IDENTIFY_REVIEW_QUEUE_ENABLED", 0)
    assert irev.maybe_enqueue_terminal_none(db, item, _FakeTmdb(_CANDS)) is False
    # 开关开但零候选（真没有，不是拿不准）：不入队
    monkeypatch.setattr(irev, "IDENTIFY_REVIEW_QUEUE_ENABLED", 1)
    assert irev.maybe_enqueue_terminal_none(db, item, _FakeTmdb([])) is False
    assert db.query(em.IdentifyReviewTask).count() == 0
    # 开关开 + 有低置信候选：入队，证据快照带分数
    assert irev.maybe_enqueue_terminal_none(db, item, _FakeTmdb(_CANDS)) is True
    db.commit()
    task = db.query(em.IdentifyReviewTask).one()
    assert task.status == "pending"
    assert task.reason == "no_confident_match"
    import json
    ev = json.loads(task.evidence_json)
    assert [c["tmdb_id"] for c in ev["candidates"]] == [111, 222]
    assert ev["candidates"][0]["tier"] is None


def test_enqueue_idempotent_pending(db):
    item = _make_item(db)
    irev.enqueue_review(db, item, "no_confident_match", {"candidates": []})
    db.commit()
    newer = {"candidates": [{"tmdb_id": 9}]}
    irev.enqueue_review(db, item, "no_confident_match", newer)
    db.commit()
    rows = db.query(em.IdentifyReviewTask).all()
    assert len(rows) == 1
    import json
    assert json.loads(rows[0].evidence_json) == newer


def test_enqueue_after_resolution_creates_new_pending(db):
    item = _make_item(db)
    t1 = irev.enqueue_review(db, item, "no_confident_match", {})
    irev.mark_resolved(db, t1, "ignored", user_id=7)
    db.commit()
    t2 = irev.enqueue_review(db, item, "no_confident_match", {})
    db.commit()
    assert t2.id != t1.id and t2.status == "pending"
    pendings = (db.query(em.IdentifyReviewTask)
                .filter(em.IdentifyReviewTask.status == "pending").count())
    assert pendings == 1


# ---------- TMDB 带分数候选 ----------

def test_scored_candidates_marks_rejected_with_none_tier(monkeypatch):
    client = TmdbClient()
    hits = [
        {"id": 111, "name": "某冷门剧", "original_name": "某冷门剧",
         "first_air_date": "2021-05-01", "popularity": 9.9,
         "poster_path": "/x.jpg"},
        {"id": 222, "name": "毫不相干的东西", "original_name": "Other",
         "first_air_date": "1999-01-01", "popularity": 0.5,
         "poster_path": None},
    ]
    monkeypatch.setattr(client, "_search_raw", lambda q, y, k: hits)
    out = client.scored_candidates("某冷门剧", 2021, "series")
    assert [c["tmdb_id"] for c in out] == [111, 222]
    assert out[0]["tier"] == 2          # 精确相等 = 高置信
    assert out[1]["tier"] is None       # 被置信线拒掉 = 低置信证据
    assert out[0]["year"] == "2021"


# ---------- 纪律③：端到端全链 ----------

def _bind_stub(item_id, req, staff, db):
    """bind_tmdb_id 的替身：只替「向 TMDB 校验 + 拉详情」这段网络行为，
    落库效果与真实现同口径（tmdb_id 落上 + 人工锁定 + 终态 done）。"""
    item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).one()
    item.tmdb_id = req.tmdb_id
    item.metadata_source = "tmdb"
    item.metadata_locked = True
    item.enrich_status = "done"
    db.commit()
    return {"success": True, "item": {"id": item.id, "tmdb_id": item.tmdb_id}}


def test_end_to_end_low_confidence_chain(db, monkeypatch, staff):
    item = _make_item(db)
    db.commit()
    monkeypatch.setattr(irev, "IDENTIFY_REVIEW_QUEUE_ENABLED", 1)
    monkeypatch.setattr(admin_review, "bind_tmdb_id", _bind_stub)

    # 1) 识别收尾：低置信 → 入队
    assert irev.maybe_enqueue_terminal_none(db, item, _FakeTmdb(_CANDS)) is True
    db.commit()

    # 2) 管理端列表可见
    listing = admin_review.list_identify_review_tasks(
        status="pending", page=1, page_size=20, staff=staff, db=db)
    assert listing["total"] == 1
    row = listing["items"][0]
    assert row["item_id"] == item.id and row["item_tmdb_id"] is None

    # 3) 详情带证据快照
    detail = admin_review.get_identify_review_task(row["id"], staff=staff, db=db)
    assert [c["tmdb_id"] for c in detail["evidence"]["candidates"]] == [111, 222]

    # 4) 处置：绑定到候选 111 → 条目落 id、审查行 bound 留痕
    out = admin_review.bind_identify_review_task(
        row["id"], admin_review.ReviewBindRequest(tmdb_id="111"),
        staff=staff, db=db)
    assert out["success"] is True
    assert out["task"]["status"] == "bound"
    assert out["task"]["resolved_tmdb_id"] == "111"
    assert out["task"]["resolved_by"] == staff.id
    db.refresh(item)
    assert item.tmdb_id == "111"

    # 5) 重复处置被拒（409）
    with pytest.raises(HTTPException) as ei:
        admin_review.ignore_identify_review_task(row["id"], staff=staff, db=db)
    assert ei.value.status_code == 409


def test_end_to_end_ignore_and_rerun(db, monkeypatch, staff):
    monkeypatch.setattr(irev, "IDENTIFY_REVIEW_QUEUE_ENABLED", 1)
    item_a = _make_item(db, name="剧甲")
    item_b = _make_item(db, name="剧乙")
    db.commit()
    irev.maybe_enqueue_terminal_none(db, item_a, _FakeTmdb(_CANDS))
    irev.maybe_enqueue_terminal_none(db, item_b, _FakeTmdb(_CANDS))
    db.commit()
    tasks = {t.item_id: t for t in db.query(em.IdentifyReviewTask).all()}

    # 忽略：审查行落 ignored，条目原样保持 none 终态
    out = admin_review.ignore_identify_review_task(
        tasks[item_a.id].id, staff=staff, db=db)
    assert out["task"]["status"] == "ignored"
    db.refresh(item_a)
    assert item_a.tmdb_id is None and item_a.metadata_source == "none"

    # 重跑：审查行落 requeued，条目回补全队列（pending + 高优先 + 清 attempts）
    out = admin_review.rerun_identify_review_task(
        tasks[item_b.id].id, staff=staff, db=db)
    assert out["task"]["status"] == "requeued"
    db.refresh(item_b)
    assert item_b.enrich_status == "pending"
    assert item_b.enrich_attempts == 0
    assert item_b.enrich_priority == irev.REQUEUE_PRIORITY
    assert item_b.enrich_next_retry_at is None

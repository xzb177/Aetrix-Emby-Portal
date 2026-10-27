"""识别 / 刮削两条队列 + AI 参谋政策（清单第 17 项，FMBY #17）。

钉死：
① 队列是虚拟的（抢单谓词），不是新状态值：未绑定进识别队列，
   已绑定进刮削队列；NULL 与空串都算未绑定；
② 两队列抢单互斥：识别抢单只捞未绑定，刮削抢单只捞已绑定；
③ AI 政策：权重上限 0.2、纯 AI 提名拒绝落库、强弱阈值 0.9/0.65，
   两开关默认关；
④ 识别段未定身份不再空转 pending：回落现行终态（计次，超限终态化）。
"""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import ai_policy
from backend.emby_server import enrich_queues as eq
from backend.emby_server import enrich_worker
from backend.emby_server import identify_two_stage as ts
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


# ---------- ① 队列归属 ----------

def test_queue_of_bound_and_unbound():
    assert eq.queue_of(SimpleNamespace(tmdb_id=None, imdb_id=None)) == "identify"
    assert eq.queue_of(SimpleNamespace(tmdb_id="", imdb_id="  ")) == "identify"
    assert eq.queue_of(SimpleNamespace(tmdb_id="60300", imdb_id=None)) == "fill"
    assert eq.queue_of(SimpleNamespace(tmdb_id=None, imdb_id="tt0903747")) == "fill"
    assert eq.queue_of(SimpleNamespace()) == "identify"  # 缺字段按未绑定


def test_queue_filter_unknown_raises():
    with pytest.raises(ValueError):
        eq.queue_filter("bogus")


def test_process_item_unknown_queue_raises():
    with pytest.raises(ValueError):
        enrich_worker._process_item(None, None, None, queue="bogus")


# ---------- ② 两队列抢单互斥 ----------

def test_claim_batches_split_by_binding(db):
    unbound_a = _make_item(db, name="未绑定A")
    unbound_b = _make_item(db, name="未绑定B")
    bound_a = _make_item(db, name="已绑定A", tmdb_id="60300")
    bound_b = _make_item(db, name="已绑定B", imdb_id="tt0903747")
    db.commit()

    identify_claimed = enrich_worker._claim_batch(db, 10, queue="identify")
    identify_ids = {row.id for row in identify_claimed}
    assert identify_ids == {unbound_a.id, unbound_b.id}

    fill_claimed = enrich_worker._claim_batch(db, 10, queue="fill")
    fill_ids = {row.id for row in fill_claimed}
    assert fill_ids == {bound_a.id, bound_b.id}


def test_claim_batch_no_queue_unchanged(db):
    # 不传 queue = 今天的单队列口径：未绑定与已绑定都捞得到。
    unbound = _make_item(db, name="未绑定")
    bound = _make_item(db, name="已绑定", tmdb_id="60300")
    db.commit()
    claimed = enrich_worker._claim_batch(db, 10)
    assert {row.id for row in claimed} == {unbound.id, bound.id}


# ---------- ③ AI 政策纯函数 ----------

def test_ai_weight_cap():
    assert ai_policy.cap_ai_weight(0.9) == 0.2
    assert ai_policy.cap_ai_weight(0.15) == 0.15
    assert ai_policy.cap_ai_weight(None) is None
    assert ai_policy.cap_ai_weight("not-a-number") is None


def test_ai_confidence_thresholds():
    assert ai_policy.classify_confidence(0.95) == "strong"
    assert ai_policy.classify_confidence(0.9) == "strong"
    assert ai_policy.classify_confidence(0.7) == "weak"
    assert ai_policy.classify_confidence(0.65) == "weak"
    assert ai_policy.classify_confidence(0.5) == "insufficient"
    assert ai_policy.classify_confidence(None) == "insufficient"


def test_ai_only_evidence_refused():
    verdict = ai_policy.evaluate_binding_evidence(
        {"ai_suggested": True, "sources": ["ai"]})
    assert verdict["allowed"] is False
    assert verdict["reason"] == "ai_only_refused"


def test_ai_mixed_evidence_allowed_and_capped():
    verdict = ai_policy.evaluate_binding_evidence(
        {"ai_suggested": True, "sources": ["ai", "tmdb"], "ai_weight": 0.9})
    assert verdict["allowed"] is True
    assert verdict["ai_weight"] == 0.2


def test_no_ai_markers_allowed():
    # 现行证据形状（无任何 AI 标记）：政策门零影响。
    verdict = ai_policy.evaluate_binding_evidence(
        {"tmdb_id_source": "fetched", "has_details": True, "has_nfo": False})
    assert verdict["allowed"] is True
    assert verdict["ai_weight"] is None


# ---------- ③ AI 政策门接线 ----------

def test_apply_identity_refuses_ai_only_binding(db, monkeypatch):
    monkeypatch.setattr(ai_policy, "AI_POLICY_ENABLED", 1)
    item = _make_item(db)
    fetched = {"tmdb_id": "60300", "ai_suggested": True, "ai_sources": ["ai"]}
    assert ts.apply_identity(db, item, fetched) is None
    assert item.tmdb_id is None
    db.flush()
    record = (db.query(em.EnrichStageRecord)
              .filter(em.EnrichStageRecord.item_id == item.id).first())
    assert record is None  # 拒绝落库：身份列与段记录都不写


def test_apply_identity_caps_ai_weight_when_mixed(db, monkeypatch):
    monkeypatch.setattr(ai_policy, "AI_POLICY_ENABLED", 1)
    item = _make_item(db)
    fetched = {"tmdb_id": "60300", "ai_suggested": True,
               "ai_sources": ["ai", "tmdb"], "ai_weight": 0.9}
    binding = ts.apply_identity(db, item, fetched)
    assert binding is not None
    assert binding["evidence"]["ai_weight"] == 0.2
    assert item.tmdb_id == "60300"


def test_apply_identity_policy_off_ignores_ai_markers(db):
    # 开关默认关：AI 标记不影响识别（与今天逐字一致）。
    item = _make_item(db)
    fetched = {"tmdb_id": "60300", "ai_suggested": True, "ai_sources": ["ai"]}
    binding = ts.apply_identity(db, item, fetched)
    assert binding is not None
    assert item.tmdb_id == "60300"


# ---------- ④ 识别段未定身份终态化 ----------

def test_identify_queue_unbound_terminalizes(db):
    # 标题+年份齐（has_core）→ 现行终态路径：首轮进补次要字段轮次，
    # 次轮终态化；绝不是旧行为的 pending + 300 秒延迟无限空转。
    item = _make_item(db)
    db.commit()
    terminal = None
    for _ in range(4):
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


def test_identify_queue_unbound_counts_attempts_to_terminal(db):
    # 只有标题（has_core 不足）→ 首轮计次（attempts=1，旧行为恒为 0），
    # 5 次内终态化（不无限空转；终态分支按设计把 attempts 清零）。
    item = _make_item(db, name="只有标题")
    item.production_year = None
    db.commit()
    enrich_worker._enrich_apply_two_stage(
        db, _reload(db, item.id), {}, identify=True, fill=False)
    db.commit()
    first = _reload(db, item.id)
    assert first.enrich_status == "pending"
    assert first.enrich_attempts == 1
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


# ---------- 开关默认（FMBY #17 闭环后两队列默认开，AI 政策门仍默认关） ----------

def test_switches_default_off():
    assert eq.two_queue_enabled() is True
    assert ai_policy.ai_policy_enabled() is False

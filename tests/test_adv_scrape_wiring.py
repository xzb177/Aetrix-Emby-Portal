"""高级刮削接线批（#1285 接线 + #1284 落地后复核）。

钉死：
① 参谋接线只提名不裁决：建议能不能进待批由门定，批了能不能落库
   由人工定；落库只走现行 identify 写库路径（apply_identity），本层
   不直写身份列。
② 门拒的建议批不动、落不了库；人工拍板后门拒不再适用（人定了、
   AI 只是提名），权重仍钳制到 0.2；未拍板的纯 AI 绑定走
   apply_identity 照样被门拒。
③ 建议一产出即持久化到 emby_ai_suggestions 行（含全量快照与 audit），
   拍板 / 落库结果追加进同一行 audit；不复用识别审查队列表。
④ 人物补缺只填 emby_people 现有三列、永不覆盖、不新建行；开关关时
   不问源；锚点从严；补资料段失败不拖垮。
⑤ 参谋直接导入政策门（#1284 已合署，防御性导入已退役）。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import ai_policy
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em
from backend.emby_server.metasources import ai_advisor as advisor
from backend.emby_server.metasources import ai_advisor_wiring as wiring
from backend.emby_server.metasources import people_scrape as people


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


def _make_item(db, **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="剧集库",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="series",
        name="某剧", sort_name="某剧",
        file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2021, **kw)
    db.add(item)
    db.flush()
    return item


def _make_person(db, item_id, name, role="", image="", person_tmdb_id=""):
    row = em.EmbyPerson(item_id=item_id, name=name, role=role, image=image,
                        sort_order=0, person_tmdb_id=person_tmdb_id)
    db.add(row)
    db.flush()
    return row


def _advisor_on(monkeypatch):
    monkeypatch.setattr(advisor, "AI_ADVISOR_ENABLED", 1)
    monkeypatch.setenv("AI_ADVISOR_PROVIDER", "test-provider")
    monkeypatch.setenv("AI_ADVISOR_MODEL", "test-model")


def _policy_on(monkeypatch):
    monkeypatch.setattr(ai_policy, "AI_POLICY_ENABLED", 1)


def _fake_provider(result):
    def _provider(item_key, existing_evidence):
        return dict(result)
    return _provider


# ---------- ⑤ 直接导入 ----------

def test_advisor_imports_real_gate_directly():
    assert advisor._gate is ai_policy


# ---------- ① 参谋接线：持久化与拍板 ----------

def test_request_persists_pending_with_non_ai_evidence(db, monkeypatch):
    _advisor_on(monkeypatch)
    item = _make_item(db)
    provider = _fake_provider({"tmdb_id": "60300", "imdb_id": "tt0903747",
                               "ai_weight": 0.9})
    row = wiring.request_and_persist_suggestion(
        db, item, {"sources": ["tmdb_search"]}, provider)
    db.commit()
    assert row is not None
    assert row.status == "pending"
    assert row.proposed_tmdb_id == "60300"
    assert row.proposed_imdb_id == "tt0903747"
    # 门钳制：0.9 → 0.2。
    assert row.ai_weight == 0.2
    assert any(e["event"] == "requested"
               for e in wiring.row_to_suggestion(row).audit)


def test_request_pure_ai_refused_recorded_and_unapprovable(db, monkeypatch):
    _advisor_on(monkeypatch)
    item = _make_item(db)
    provider = _fake_provider({"tmdb_id": "60300", "ai_weight": 0.5})
    row = wiring.request_and_persist_suggestion(db, item, {}, provider)
    db.commit()
    assert row.status == "refused_by_policy"
    suggestion = wiring.row_to_suggestion(row)
    assert advisor.build_pending_diff(suggestion) is None
    blocked = wiring.approve_suggestion(db, row.id, "admin")
    db.commit()
    assert blocked.status == "refused_by_policy"
    assert any(e["event"] == "approve_blocked"
               for e in wiring.row_to_suggestion(blocked).audit)


def test_approve_then_apply_writes_via_identify_path(db, monkeypatch):
    _advisor_on(monkeypatch)
    item = _make_item(db)
    provider = _fake_provider({"tmdb_id": "60300", "imdb_id": "tt0903747",
                               "ai_weight": 0.9})
    row = wiring.request_and_persist_suggestion(
        db, item, {"sources": ["tmdb_search"]}, provider)
    db.commit()
    approved = wiring.approve_suggestion(db, row.id, "admin")
    assert approved.status == "approved"
    binding, outcome = wiring.apply_approved_suggestion(db, row.id)
    db.commit()
    assert outcome == "applied"
    assert binding["provider"] == "tmdb"
    assert item.tmdb_id == "60300"
    assert item.imdb_id == "tt0903747"
    record = (db.query(em.EnrichStageRecord)
              .filter(em.EnrichStageRecord.item_id == item.id).first())
    assert record is not None and record.stage == "identified"
    done = wiring.get_suggestion_row(db, row.id)
    assert done.applied_at is not None
    assert any(e["event"] == "outcome:applied"
               for e in wiring.row_to_suggestion(done).audit)


def test_apply_without_approval_writes_nothing(db, monkeypatch):
    _advisor_on(monkeypatch)
    item = _make_item(db)
    provider = _fake_provider({"tmdb_id": "60300", "ai_weight": 0.5})
    row = wiring.request_and_persist_suggestion(
        db, item, {"sources": ["tmdb_search"]}, provider)
    db.commit()
    binding, outcome = wiring.apply_approved_suggestion(db, row.id)
    assert binding is None and outcome == "not_approved"
    assert not item.tmdb_id


def test_chain_closed_without_provider_writes_no_row(db, monkeypatch):
    _advisor_on(monkeypatch)
    monkeypatch.setattr(wiring, "build_provider", lambda db: None)
    item = _make_item(db)
    assert wiring.request_and_persist_suggestion(db, item, {}) is None
    assert db.query(em.AiAdvisorSuggestion).count() == 0


def test_parse_nomination_rejects_bad_ids():
    assert wiring._parse_nomination('{"tmdb_id": "abc"}') is None
    assert wiring._parse_nomination('{"imdb_id": "xyz"}') is None
    assert wiring._parse_nomination("no json here") is None
    parsed = wiring._parse_nomination(
        'prefix {"tmdb_id": "60300", "ai_weight": 0.5} suffix')
    assert parsed["tmdb_id"] == "60300" and parsed["ai_weight"] == 0.5


def test_apply_item_missing_records_failure(db, monkeypatch):
    _advisor_on(monkeypatch)
    item = _make_item(db)
    provider = _fake_provider({"tmdb_id": "60300", "ai_weight": 0.5})
    row = wiring.request_and_persist_suggestion(
        db, item, {"sources": ["tmdb_search"]}, provider)
    wiring.approve_suggestion(db, row.id, "admin")
    db.delete(item)
    db.commit()
    binding, outcome = wiring.apply_approved_suggestion(db, row.id)
    assert binding is None and outcome == "item_missing"


# ---------- ① 门行为：拍板例外 ----------

def test_gate_still_refuses_pure_ai_without_approval(db, monkeypatch):
    _policy_on(monkeypatch)
    item = _make_item(db)
    fetched = {"ai_suggested": True, "ai_sources": ["ai"], "ai_weight": 0.9}
    assert ts.apply_identity(db, item, fetched) is None
    assert not item.tmdb_id


def test_human_approved_pure_ai_lands_with_capped_weight(db, monkeypatch):
    _policy_on(monkeypatch)
    item = _make_item(db)
    fetched = {"tmdb_id": "60300", "ai_suggested": True,
               "ai_sources": ["ai"], "ai_weight": 0.9,
               "human_approved": True, "approved_by": "admin"}
    binding = ts.apply_identity(db, item, fetched)
    assert binding is not None
    assert item.tmdb_id == "60300"
    assert binding["evidence"]["ai_weight"] == 0.2
    assert binding["evidence"]["human_approved"] is True


# ---------- ② 人物补缺 ----------

def _bound_item_with_people(db):
    item = _make_item(db, tmdb_id="60300")
    row_a = _make_person(db, item.id, "演员甲", role="主角",
                         person_tmdb_id="")
    row_b = _make_person(db, item.id, "演员乙", role="配角",
                         image="http://img/x.jpg", person_tmdb_id="111")
    row_c = _make_person(db, item.id, "演员丙")
    db.commit()
    return item, row_a, row_b, row_c


def test_people_fill_fills_only_empty_never_overwrites(db, monkeypatch):
    monkeypatch.setattr(people, "PEOPLE_SCRAPE_ENABLED", 1)
    item, row_a, row_b, row_c = _bound_item_with_people(db)

    def _fetcher(record):
        if record["name"] == "演员甲":
            return {"name": "演员甲", "role": "新角色",
                    "image": "http://img/a.jpg", "person_tmdb_id": "222"}
        if record["name"] == "演员乙":
            return {"name": "演员乙", "role": "新角色",
                    "image": "http://img/b.jpg", "person_tmdb_id": "333"}
        return None

    filled = ts.apply_fill(db, item, {"tmdb_id": "60300"},
                           people_fetcher=_fetcher)
    db.commit()
    db.refresh(row_a)
    db.refresh(row_b)
    db.refresh(row_c)
    # 甲：空字段补上，已有角色不动。
    assert row_a.image == "http://img/a.jpg"
    assert row_a.person_tmdb_id == "222"
    assert row_a.role == "主角"
    # 乙：已有值一律不动。
    assert row_b.role == "配角" and row_b.image == "http://img/x.jpg"
    assert row_b.person_tmdb_id == "111"
    # 丙：抓不到，纹丝不动。
    assert not row_c.image and not row_c.person_tmdb_id
    assert isinstance(filled, list)


def test_people_anchor_mismatch_skipped(db, monkeypatch):
    monkeypatch.setattr(people, "PEOPLE_SCRAPE_ENABLED", 1)
    item = _make_item(db, tmdb_id="60300")
    row = _make_person(db, item.id, "演员甲", person_tmdb_id="999")
    db.commit()

    def _fetcher(record):
        return {"name": "演员甲", "image": "http://img/a.jpg",
                "person_tmdb_id": "111"}

    ts.apply_fill(db, item, {"tmdb_id": "60300"}, people_fetcher=_fetcher)
    db.commit()
    db.refresh(row)
    assert not row.image and row.person_tmdb_id == "999"


def test_people_switch_off_never_calls_fetcher(db, monkeypatch):
    item, row_a, _, _ = _bound_item_with_people(db)
    calls = []

    def _fetcher(record):
        calls.append(record["name"])
        return {"name": record["name"], "image": "http://img/a.jpg"}

    ts.apply_fill(db, item, {"tmdb_id": "60300"}, people_fetcher=_fetcher)
    assert calls == []
    db.refresh(row_a)
    assert not row_a.image


def test_people_default_fetcher_uses_credits(db, monkeypatch):
    monkeypatch.setattr(people, "PEOPLE_SCRAPE_ENABLED", 1)
    item = _make_item(db, tmdb_id="60300")
    row = _make_person(db, item.id, "演员甲")
    db.commit()
    fetched = {
        "tmdb_id": "60300",
        "tmdb_details": {
            "credits": {"cast": [
                {"name": "演员甲", "character": "主角",
                 "profile_path": "/a.jpg", "id": 222},
            ]},
        },
    }
    ts.apply_fill(db, item, fetched)
    db.commit()
    db.refresh(row)
    assert row.person_tmdb_id == "222"
    assert row.image.endswith("/a.jpg")
    assert row.role == "主角"


def test_people_fill_unbound_item_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(people, "PEOPLE_SCRAPE_ENABLED", 1)
    item = _make_item(db)
    row = _make_person(db, item.id, "演员甲")
    db.commit()
    calls = []

    def _fetcher(record):
        calls.append(record["name"])
        return {"name": "演员甲", "image": "http://img/a.jpg"}

    assert ts.apply_fill(db, item, {}, people_fetcher=_fetcher) == []
    assert calls == []
    db.refresh(row)
    assert not row.image

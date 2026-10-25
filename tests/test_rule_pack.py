"""规则包版本化（FMBY 清单 #18）。

钉死：
① 默认包的剥词行为与旧内联正则一致（代表性标题逐字断言）；
② 内容哈希只跟包自身内容走，生效哈希跟覆盖层走；
③ 三层覆盖语义：自定义追加、禁用摘词、保护词最高优先且保留原大小写；
④ 识别段记录钉版本 + 生效哈希（列与证据快照一致）；
⑤ 重放：只报不写是默认；写时只改标题 + 放回 pending；人工绑定
   （审查队列 bound）不动；剥空标题不动；NULL 哈希老记录不动。
"""

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em
from backend.emby_server import rule_pack as rp
from backend.emby_server import scanner as sc


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
    import re
    return re.sub(r"\s+", " ", text).strip(" .-_") if text else text


def _make_item(db, scan_title=None, **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="剧集库",
                     collection_type="tvshows")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "done")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="series",
        name="某剧", sort_name="某剧",
        file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2021, scan_title=scan_title, **kw)
    db.add(item)
    db.flush()
    return item


def _stage_record(db, item, rule_pack_hash):
    rec = em.EnrichStageRecord(item_id=item.id, stage="identified",
                               provider="tmdb", external_id="60300",
                               confidence="search",
                               rule_pack_version="v1",
                               rule_pack_hash=rule_pack_hash)
    db.add(rec)
    db.flush()
    return rec


# ---- ① 默认包行为与旧内联正则一致 ----

def test_default_pack_strips_legacy_terms():
    pack = rp.RulePack()
    assert _collapse(pack.strip("Movie 1080p BluRay x265")) == "Movie"
    assert _collapse(pack.strip("Alpha Target WEB DL")) == "Alpha Target"
    assert _collapse(pack.strip("Film HDR10 Atmos TrueHD")) == "Film"
    assert _collapse(pack.strip("Show S01E01 2160p")) == "Show S01E01"
    assert _collapse(pack.strip("比佛利拜金狗2")) == "比佛利拜金狗2"


def test_scanner_clean_name_unchanged_without_pack():
    assert sc.clean_name("Some Movie WEB-DL-3") == "Some Movie"
    assert sc.clean_name("比佛利拜金狗2") == "比佛利拜金狗2"
    assert sc.clean_name(" [2]") == ""


def test_parse_uses_explicit_pack():
    parsed = sc.parse_media_filename("/m/Movie HDR 2024 1080p.mkv", "movies")
    assert parsed["name"] == "Movie"
    pack = rp.RulePack(disabled_default_terms=("hdr",))
    parsed = sc.parse_media_filename("/m/Movie HDR 2024 1080p.mkv", "movies",
                                     pack=pack)
    assert parsed["name"] == "Movie HDR"


# ---- ② 哈希分层 ----

def test_hash_layers():
    a = rp.RulePack()
    b = rp.RulePack()
    assert a.content_hash == b.content_hash
    assert a.effective_hash == b.effective_hash
    changed = rp.RulePack(version="v2")
    assert changed.content_hash != a.content_hash
    overrode = rp.RulePack(custom_terms=("camrip",))
    assert overrode.content_hash == a.content_hash
    assert overrode.effective_hash != a.effective_hash


# ---- ③ 三层覆盖 ----

def test_custom_terms_append():
    pack = rp.RulePack(custom_terms=("camrip",))
    assert _collapse(pack.strip("Film Camrip 1080p")) == "Film"


def test_disabled_default_removes_term():
    pack = rp.RulePack(disabled_default_terms=("hdr",))
    assert _collapse(pack.strip("Movie HDR 1080p")) == "Movie HDR"


def test_protected_term_beats_default_and_keeps_case():
    pack = rp.RulePack(protected_terms=("web",))
    assert _collapse(pack.strip("WEB Movie 1080p")) == "WEB Movie"
    # 保护词只保整词：WebRip 不是 web 这个词，仍按 webrip 剥掉；
    # Webster 里的 web 前缀同样不受保护（与 \\b 同口径）。
    assert _collapse(pack.strip("Film WebRip")) == "Film"
    assert _collapse(pack.strip("Webster 1080p")) == "Webster"


def test_env_loading(monkeypatch):
    monkeypatch.setenv("RULE_PACK_CUSTOM_TERMS", '["camrip"]')
    monkeypatch.setenv("RULE_PACK_DISABLED_DEFAULT_TERMS", '["hdr"]')
    monkeypatch.setenv("RULE_PACK_PROTECTED_TERMS", '["web"]')
    pack = rp.default_pack()
    assert pack.custom_terms == ("camrip",)
    assert pack.disabled_default_terms == ("hdr",)
    assert pack.protected_terms == ("web",)


def test_env_invalid_json_ignored(monkeypatch):
    monkeypatch.setenv("RULE_PACK_CUSTOM_TERMS", "not-json{")
    assert rp.default_pack().custom_terms == ()


# ---- ④ 识别段打戳 ----

def test_identify_stamps_pack(db):
    item = _make_item(db)
    binding = ts.apply_identity(db, item, {"tmdb_id": "60300"})
    assert binding is not None
    rec = db.query(em.EnrichStageRecord).filter(
        em.EnrichStageRecord.item_id == item.id).first()
    assert rec is not None
    assert rec.rule_pack_version == "v1"
    assert rec.rule_pack_hash == rp.default_pack().effective_hash
    evidence = json.loads(rec.evidence_json)
    assert evidence["rule_pack_version"] == "v1"
    assert evidence["rule_pack_hash"] == rec.rule_pack_hash


# ---- ⑤ 重放 ----

def test_replay_dry_run_reports_without_writing(db, monkeypatch):
    monkeypatch.setenv("RULE_PACK_CUSTOM_TERMS", '["camrip"]')
    item = _make_item(db, scan_title="Film Camrip")
    _stage_record(db, item, "stale-hash")
    report = rp.replay_pack(db, dry_run=True)
    assert report["checked"] == 1
    assert report["changed"] == 1
    assert report["requeued"] == 0
    db.refresh(item)
    assert item.scan_title == "Film Camrip"
    assert item.enrich_status == "done"


def test_replay_writes_title_and_requeues(db, monkeypatch):
    monkeypatch.setenv("RULE_PACK_CUSTOM_TERMS", '["camrip"]')
    item = _make_item(db, scan_title="Film Camrip")
    _stage_record(db, item, "stale-hash")
    report = rp.replay_pack(db, dry_run=False)
    assert report["requeued"] == 1
    # 重放本身不 commit（与 apply_identity 同口径）：测试里先 flush 再验。
    db.flush()
    db.refresh(item)
    assert item.scan_title == "Film"
    assert item.enrich_status == "pending"
    assert item.enrich_claimed_at is None
    assert item.enrich_claim_token is None
    assert item.enrich_next_retry_at is None


def test_replay_skips_bound_items(db, monkeypatch):
    monkeypatch.setenv("RULE_PACK_CUSTOM_TERMS", '["camrip"]')
    item = _make_item(db, scan_title="Film Camrip")
    _stage_record(db, item, "stale-hash")
    task = em.IdentifyReviewTask(item_id=item.id, item_name="Film",
                                 item_type="series",
                                 reason="no_confident_match",
                                 status="bound")
    db.add(task)
    db.flush()
    report = rp.replay_pack(db, dry_run=False)
    assert report["skipped_protected"] == 1
    assert report["requeued"] == 0
    db.refresh(item)
    assert item.scan_title == "Film Camrip"


def test_replay_skips_empty_result_and_null_hash(db, monkeypatch):
    monkeypatch.setenv("RULE_PACK_CUSTOM_TERMS", '["camrip"]')
    blank = _make_item(db, scan_title="1080p")
    _stage_record(db, blank, "stale-hash")
    legacy = _make_item(db, scan_title="Film Camrip")
    _stage_record(db, legacy, None)
    report = rp.replay_pack(db, dry_run=False)
    assert report["checked"] == 1
    assert report["skipped_empty"] == 1
    assert report["requeued"] == 0
    db.refresh(blank)
    assert blank.scan_title == "1080p"

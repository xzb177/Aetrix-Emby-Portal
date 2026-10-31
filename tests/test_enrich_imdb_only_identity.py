"""imdb-only 盲区治本（2026-10-31 诊断定案）：身份升级落库。

钉死：
① extract_binding 新增最低优先来源 fetched["tmdb_hit"]["id"]：只有
   imdb 的条目 + 本轮搜索命中 → 抽出 tmdb 绑定，置信度记 search、
   证据来源记 search；条目自带 / fetched["tmdb_id"] / NFO 任一在场
   时命中不越位（优先序与旧口径逐字一致）。
② hit 无 id / 不是 dict 时按不存在处理：imdb-only 绑定与今天一致。
③ 补资料段（fill 分支）对无 tmdb_id 且本轮带 TMDB 候选的条目，先跑
   一次 apply_identity 把身份升级落库、再 apply_fill：tmdb_id 落库、
   状态 done、资料照填。段记录是单行 upsert、终态 stage=filled 属
   设计——identified 步以 apply_identity 调用留痕 + tmdb_id 落库
   （apply_fill 从不写身份列）双证。
④ 对照组：hit 已被门 pop（fetched 无 hit、无 tmdb_id）时行为与今天
   逐字一致——不调 apply_identity、不落 tmdb_id、不报错、照常 done。
⑤ 门不绕：年差 >1 年的 hit 即使进了 fill 分支，apply_identity 内
   的绑定复核门仍拒（返回 None），tmdb_id 不落库、补资料照常。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_worker
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em

_HIT = {
    "id": 1089598,
    "title": "敦煌英雄",
    "original_title": "敦煌英雄",
    "release_date": "2026-09-25",
}

_DETAILS = {
    "id": 1089598,
    "title": "敦煌英雄",
    "release_date": "2026-09-25",
    "overview": "详情简介",
    "vote_average": 8.1,
    "poster_path": "/tmdb_poster.jpg",
    "backdrop_path": "/tmdb_backdrop.jpg",
    "imdb_id": "tt28131387",
}


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


class _Item:
    """extract_binding 纯函数用的轻量条目替身（只读身份/类型/标题）。"""

    def __init__(self, **kw):
        self.tmdb_id = kw.get("tmdb_id")
        # 前提：imdb-only 条目——imdb 已有、tmdb 缺失。
        self.imdb_id = kw.get("imdb_id", "tt28131387")
        self.item_type = kw.get("item_type", "movie")
        self.name = kw.get("name", "敦煌英雄")
        self.production_year = kw.get("production_year", 2026)


def _make_item(db, **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="电影库",
                     collection_type="movies")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    kw.setdefault("imdb_id", "tt28131387")
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name="敦煌英雄", sort_name="敦煌英雄",
        file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=2026, **kw)
    db.add(item)
    db.flush()
    return item


def _stage_record(db, item_id):
    return (db.query(em.EnrichStageRecord)
            .filter(em.EnrichStageRecord.item_id == item_id).first())


def _fill_only(monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 0)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 1)


def _identity_spy(monkeypatch):
    calls = []
    real = ts.apply_identity

    def _spy(db_, item_, fetched_, tmdb_details_fetcher=None):
        result = real(db_, item_, fetched_,
                      tmdb_details_fetcher=tmdb_details_fetcher)
        calls.append(result)
        return result

    monkeypatch.setattr(ts, "apply_identity", _spy)
    return calls


# ---------- ① extract_binding：tmdb_hit 成最低优先 tmdb 来源 ----------

def test_extract_binding_reads_tmdb_hit_for_imdb_only_item():
    binding = ts.extract_binding(_Item(), {"tmdb_hit": dict(_HIT)})
    assert binding is not None
    assert binding["provider"] == "tmdb"
    assert binding["tmdb_id"] == "1089598"
    assert binding["external_id"] == "1089598"
    assert binding["imdb_id"] == "tt28131387"
    assert binding["confidence"] == "search"
    assert binding["evidence"]["tmdb_id_source"] == "search"


def test_extract_binding_hit_never_overrides_earlier_sources():
    # 条目自带 tmdb 在场：hit 不越位，口径与今天一致。
    binding = ts.extract_binding(
        _Item(tmdb_id="111"), {"tmdb_hit": dict(_HIT)})
    assert binding["tmdb_id"] == "111"
    assert binding["confidence"] == "explicit"
    assert binding["evidence"]["tmdb_id_source"] == "item"
    # fetched["tmdb_id"] 在场：hit 不越位。
    binding = ts.extract_binding(
        _Item(), {"tmdb_id": "60300", "tmdb_hit": dict(_HIT)})
    assert binding["tmdb_id"] == "60300"
    assert binding["evidence"]["tmdb_id_source"] == "fetched"
    # NFO 在场：hit 不越位。
    binding = ts.extract_binding(
        _Item(), {"nfo_data": {"tmdb_id": "60300"},
                  "tmdb_hit": dict(_HIT)})
    assert binding["tmdb_id"] == "60300"
    assert binding["evidence"]["tmdb_id_source"] == "nfo"


def test_extract_binding_hit_without_id_is_ignored():
    for bad_hit in (None, {}, {"title": "敦煌英雄"}, "1089598"):
        binding = ts.extract_binding(_Item(), {"tmdb_hit": bad_hit})
        assert binding is not None
        assert binding["provider"] == "imdb"
        assert binding["tmdb_id"] is None
        assert binding["external_id"] == "tt28131387"


# ---------- ③ fill 分支：身份升级先落库、再补资料 ----------

def test_fill_branch_upgrades_imdb_only_item(db, monkeypatch):
    _fill_only(monkeypatch)
    calls = _identity_spy(monkeypatch)
    item = _make_item(db, enrich_claim_token="tok")
    fetched = {"tmdb_hit": dict(_HIT), "tmdb_details": dict(_DETAILS)}
    enrich_worker._enrich_apply_two_stage(db, item, fetched)
    # 身份升级真实跑过且抽出了 tmdb 绑定。
    assert len(calls) == 1
    assert calls[0] is not None
    assert calls[0]["tmdb_id"] == "1089598"
    assert calls[0]["confidence"] == "search"
    # tmdb_id 落库（apply_fill 从不写身份列，落库只能来自识别段）。
    assert item.tmdb_id == "1089598"
    assert item.imdb_id == "tt28131387"
    # 补资料照常：字段填上、终态 done。
    assert item.poster_path == "/tmdb_poster.jpg"
    assert item.overview == "详情简介"
    assert item.enrich_status == "done"
    assert item.enrich_claim_token is None
    # 段记录终态是 filled（单行 upsert），但证据已按升级后的身份记。
    record = _stage_record(db, item.id)
    assert record is not None
    assert record.stage == "filled"
    assert record.provider == "tmdb"
    assert record.external_id == "1089598"


def test_fill_branch_rejected_hit_not_landed(db, monkeypatch):
    _fill_only(monkeypatch)
    _identity_spy(monkeypatch)
    item = _make_item(db, enrich_claim_token="tok")
    # 年差 >1 年的候选（hit 与 details 同口径带错年）：识别段复核门
    # 以 year_mismatch 拒绝，身份不落、补资料照常。生产中这种候选
    # 在 IO 阶段就已被门 pop，此处验的是写库段的防御纵深不绕门。
    bad_hit = dict(_HIT, release_date="1999-01-01")
    details = dict(_DETAILS, release_date="1999-01-01")
    fetched = {"tmdb_hit": bad_hit, "tmdb_details": details}
    enrich_worker._enrich_apply_two_stage(db, item, fetched)
    assert not item.tmdb_id
    assert item.enrich_status == "done"
    record = _stage_record(db, item.id)
    assert record.provider == "imdb"
    assert record.external_id == "tt28131387"


# ---------- ④ 对照组：无候选时与今天逐字一致 ----------

def test_fill_branch_without_candidate_behaves_as_before(db, monkeypatch):
    _fill_only(monkeypatch)
    calls = _identity_spy(monkeypatch)
    item = _make_item(db, enrich_claim_token="tok")
    fetched = {"tmdb_details": dict(_DETAILS)}
    enrich_worker._enrich_apply_two_stage(db, item, fetched)
    assert calls == []  # 无候选不触发身份升级
    assert not item.tmdb_id
    assert item.poster_path == "/tmdb_poster.jpg"
    assert item.enrich_status == "done"
    record = _stage_record(db, item.id)
    assert record is not None
    assert record.stage == "filled"
    assert record.provider == "imdb"
    assert record.external_id == "tt28131387"

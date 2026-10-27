"""后端完美审查第二轮第 3 项：extract_binding 空白身份根治回归。

Bug（B 批 #1374 实锤的域外同根点）：extract_binding 用裸 ``or`` 链取
已有身份值，纯空白字符串（如 ``"  "``、``"\\t"``）是 truthy，会被当成
已有绑定返回并 upsert 一段 identified 记录；空白还会压住后一顺位上
真实的身份值。本批在 extract_binding 层根治：各来源身份值先按 strip
口径归一再取优先序，None / 空串 / 纯空白一律不算身份。

本文件只测 extract_binding 与 apply_identity（本文件内的调用层），
不碰 enrich_worker / enrich_queues（第 2 项与 B 批的地盘），因此：
- 仅本批 alone 即可全过（基线 main 不含 B 批）；
- B 批合署后两层叠加仍过：根治后空白在 extract_binding 层即返回
  None，apply_identity 无段记录可写，B 批域内否决的 ``res is not None``
  前提不成立、不触发回滚——无 double-count、无语义冲突，B 批否决仅
  作为防御纵深保留（例如 donor 继承路径残留的空白，见报告残留项）。
真实绑定仍正常通过（优先序、explicit/search、去首尾空白归一均钉死）。
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em


@pytest.fixture()  # noqa: F821 — pytest 注入，下同
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


def _stage_record(db, item_id):
    return (db.query(em.EnrichStageRecord)
            .filter(em.EnrichStageRecord.item_id == item_id).first())


def _item(tmdb_id=None, imdb_id=None):
    return SimpleNamespace(tmdb_id=tmdb_id, imdb_id=imdb_id,
                           item_type="series", name="某剧")


# ---------- 空白身份在 extract_binding 层即不算绑定 ----------

def test_blank_item_tmdb_is_not_binding():
    assert ts.extract_binding(_item(tmdb_id="  "), {}) is None


def test_blank_item_imdb_is_not_binding():
    assert ts.extract_binding(_item(imdb_id="\t"), {}) is None


def test_blank_fetched_tmdb_is_not_binding():
    assert ts.extract_binding(_item(), {"tmdb_id": "  "}) is None


def test_blank_nfo_tmdb_is_not_binding():
    assert ts.extract_binding(
        _item(), {"nfo_data": {"tmdb_id": " "}}) is None


def test_blank_details_imdb_is_not_binding():
    assert ts.extract_binding(
        _item(), {"tmdb_details": {"imdb_id": " \n"}}) is None


def test_blank_fetched_imdb_is_not_binding():
    assert ts.extract_binding(_item(), {"imdb_id": "  "}) is None


def test_all_sources_blank_is_not_binding():
    assert ts.extract_binding(
        _item(tmdb_id=" ", imdb_id=" "),
        {"tmdb_id": " ", "imdb_id": "\t",
         "nfo_data": {"tmdb_id": " ", "imdb_id": " "},
         "tmdb_details": {"imdb_id": " "}}) is None


# ---------- 空白不压住真实身份；真实绑定优先序与归一不变 ----------

def test_blank_item_does_not_shadow_real_fetched():
    binding = ts.extract_binding(_item(tmdb_id="  "), {"tmdb_id": "60300"})
    assert binding is not None
    assert binding["provider"] == "tmdb"
    assert binding["external_id"] == "60300"
    assert binding["tmdb_id"] == "60300"
    # 空白条目不算“条目自带”，不记 explicit。
    assert binding["confidence"] == "search"
    assert binding["evidence"]["tmdb_id_source"] == "fetched"


def test_blank_item_with_real_nfo_is_explicit():
    binding = ts.extract_binding(
        _item(tmdb_id="  "), {"nfo_data": {"tmdb_id": "60300"}})
    assert binding["confidence"] == "explicit"
    assert binding["evidence"]["tmdb_id_source"] == "nfo"
    assert binding["external_id"] == "60300"


def test_padded_real_id_is_stripped():
    binding = ts.extract_binding(_item(), {"tmdb_id": " 60300 "})
    assert binding["external_id"] == "60300"
    assert binding["tmdb_id"] == "60300"


def test_real_item_id_still_wins_over_fetched():
    binding = ts.extract_binding(_item(tmdb_id="111"), {"tmdb_id": "60300"})
    assert binding["external_id"] == "111"
    assert binding["confidence"] == "explicit"
    assert binding["evidence"]["tmdb_id_source"] == "item"


def test_real_imdb_only_via_details_still_binds():
    binding = ts.extract_binding(
        _item(), {"tmdb_details": {"imdb_id": "tt0903747"}})
    assert binding["provider"] == "imdb"
    assert binding["external_id"] == "tt0903747"


def test_int_tmdb_id_still_binds():
    binding = ts.extract_binding(_item(tmdb_id=60300), {})
    assert binding["external_id"] == "60300"


# ---------- 调用层：空白不写段记录，真实绑定仍正常落库 ----------

def test_apply_identity_blank_writes_no_stage_record(db):
    item = _make_item(db, tmdb_id="  ")
    assert ts.apply_identity(db, item, {}) is None
    assert _stage_record(db, item.id) is None


def test_apply_identity_blank_item_with_real_fetched_binds(db):
    item = _make_item(db, tmdb_id="  ")
    binding = ts.apply_identity(db, item, {"tmdb_id": "60300"})
    db.flush()
    assert binding is not None
    assert binding["external_id"] == "60300"
    assert item.tmdb_id == "60300"
    record = _stage_record(db, item.id)
    assert record is not None
    assert record.stage == "identified"
    assert record.external_id == "60300"

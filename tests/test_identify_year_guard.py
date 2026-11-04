"""识别落账年份硬冲突门（2026-11-03「诅咒2025」错认亲定案）。

生产样本：条目「诅咒2025」（库 64，实为 2025 年台湾恐怖片，
非凡 vod 99240 的 vod_year=2025）被绑上 TMDB 10012 = Cursed
（2005 美国片，中文名正好同为《诅咒》），overview/海报全成了
2005 年那部。根因：条目 production_year 为空（CMS 入库 list
载荷缺 vod_year），年份只黏在片名尾；识别链的年份校验只认
production_year，整体落空。

钉死：
① 片名尾年份解析口径（裸黏尾/括号/未来年份不剥/整名即数字不剥）；
② 已知年份集合：production_year 与片名尾年份任一与候选冲突即拒；
③ apply_identity 落账：同形态候选（名尾 2025 + 候选 2005）拒绑、
   年份对上的候选照常绑、年份全未知时按既有口径不新降门槛。
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
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


def _make_item(db, name="诅咒2025", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="电影库",
                     collection_type="movies")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    kw.setdefault("production_year", None)
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        **kw)
    db.add(item)
    db.flush()
    return item


def _fetched_for(tmdb_id, release_date, imdb_id=None, title="候选片"):
    # title 默认占位名只供纯函数（binding_reject_reason_for_item）用；
    # 走 apply_identity 的用例须传与条目名有交集的标题——search 来源
    # 落账现另有标题硬门（2026-11-04 两段式错绑定案），占位标题与条目
    # 名零交集会被标题门先拒，年份门的行为就测不到了。
    details = {"id": tmdb_id, "title": title,
               "release_date": release_date, "overview": "简介"}
    if imdb_id:
        details["imdb_id"] = imdb_id
    return {
        "tmdb_hit": {"id": tmdb_id, "title": title,
                     "release_date": release_date},
        "tmdb_details": details,
    }


def _stage_record(db, item_id):
    return (db.query(em.EnrichStageRecord)
            .filter(em.EnrichStageRecord.item_id == item_id).first())


# ---------- ① 片名尾年份解析 ----------

@pytest.mark.parametrize("name,expected", [
    ("诅咒2025", 2025),          # 裸黏尾（生产样本）
    ("爆发夜2026", 2026),
    ("泪之女王 (2024)", 2024),   # 括号包裹
    ("某片（2023）", 2023),
    ("银翼杀手2049", None),      # 未来年份是片名本身，不剥
    ("2012", None),              # 整名就是年份数字，剥后为空
    ("abc12025", None),          # 尾数前一位仍是数字（更长数字串）
    ("寻秦记", None),            # 无年份
    ("第2012号", None),          # 非结尾四位形态不认（尾是「号」）
    ("", None),
])
def test_title_trailing_year(name, expected):
    assert ts._title_trailing_year(name) == expected


# ---------- ② 已知年份集合 ----------

class _FakeItem:
    def __init__(self, name, production_year):
        self.name = name
        self.production_year = production_year


def test_known_item_years_sources():
    assert ts.known_item_years(_FakeItem("诅咒2025", None)) == (2025,)
    assert ts.known_item_years(_FakeItem("诅咒2025", 2005)) == (2005, 2025)
    assert ts.known_item_years(_FakeItem("诅咒2025", 2025)) == (2025,)
    assert ts.known_item_years(_FakeItem("寻秦记", None)) == ()
    assert ts.known_item_year(_FakeItem("诅咒2025", None)) == 2025
    assert ts.known_item_year(_FakeItem("诅咒2025", 2005)) == 2005


def test_wrapper_rejects_on_any_known_year_conflict():
    details = {"id": 10012, "title": "Cursed", "release_date": "2005-02-25"}
    item = _FakeItem("诅咒2025", None)
    assert ts.binding_reject_reason_for_item(details, "movie", item) == \
        "year_mismatch"
    # production_year 与候选一致、但与片名尾年份冲突 → 照样拒（任一已知）
    item2 = _FakeItem("诅咒2025", 2005)
    assert ts.binding_reject_reason_for_item(details, "movie", item2) == \
        "year_mismatch"
    # 年份全未知 → 不判年份（既有口径），其余检查照常
    item3 = _FakeItem("寻秦记", None)
    assert ts.binding_reject_reason_for_item(details, "movie", item3) is None
    assert ts.binding_reject_reason_for_item(None, "movie", item3) == \
        "details_not_found"
    # 年份对上 → 放行
    ok = {"id": 777, "title": "诅咒", "release_date": "2025-01-10"}
    assert ts.binding_reject_reason_for_item(ok, "movie", item) is None


# ---------- ③ apply_identity 落账 ----------

def test_apply_identity_rejects_cursed_misbinding(db):
    """生产样本复刻：名尾 2025、production_year 空、候选 Cursed(2005)。

    候选标题用 Cursed 的真实中文名《诅咒》（与条目名有交集、过标题
    门），钉死拒绝必须来自年份门而非标题门。
    """
    item = _make_item(db, name="诅咒2025")
    fetched = _fetched_for(10012, "2005-02-25", imdb_id="tt0257516",
                           title="诅咒")
    assert ts.apply_identity(db, item, fetched) is None
    db.flush()
    assert not item.tmdb_id
    assert not item.imdb_id
    assert _stage_record(db, item.id) is None


def test_apply_identity_accepts_matching_year(db):
    item = _make_item(db, name="诅咒2025")
    fetched = _fetched_for(888001, "2025-06-01", title="诅咒")
    binding = ts.apply_identity(db, item, fetched)
    db.flush()
    assert binding is not None
    assert item.tmdb_id == "888001"


def test_apply_identity_bracketed_title_year_rejects(db):
    item = _make_item(db, name="某片 (2024)")
    fetched = _fetched_for(10012, "2005-02-25", title="某片")
    assert ts.apply_identity(db, item, fetched) is None
    assert not item.tmdb_id


def test_apply_identity_no_year_evidence_keeps_legacy_behavior(db):
    """年份全未知：不新降门槛，候选按既有口径照常落账。

    候选标题与条目名一致（过标题门），钉死的是年份维度不新设门槛。
    """
    item = _make_item(db, name="无年份的片")
    fetched = _fetched_for(10012, "2005-02-25", title="无年份的片")
    binding = ts.apply_identity(db, item, fetched)
    db.flush()
    assert binding is not None
    assert item.tmdb_id == "10012"

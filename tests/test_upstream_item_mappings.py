"""本地条目→上游条目持久映射层（#25 自动兜底的前置项）。

钉死的纪律：
① 写幂等：同一三元组重复写不建新行，只更新置信度 / 证据；
② 读偏好：多条时取置信度最高，相同时取最近更新；
③ 默认关：开关关时写路径不写任何东西，读路径不受开关影响；
④ 置信度恒在 [0, 1]：写时 clamp，DB CHECK 兜底。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_item_mappings as uim


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


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch):
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "1")


@pytest.fixture(autouse=True)
def _seed_live_rows(db):
    """O-10 读契约下 get_best_mapping 默认验源与本地活行：给本文件
    用到的条目 10/11 与源 3/5 预置活行（源 enabled），旧行为测试
    的断言口径不变。"""
    library = em.Library(guid="lib-mappings", name="映射库")
    db.add(library)
    db.flush()
    for item_id in (10, 11):
        db.add(em.MediaItem(
            id=item_id, guid=f"item-mappings-{item_id}",
            library_id=library.id, item_type="movie",
            name=f"片{item_id}", file_path=f"mount://1/{item_id}.mp4"))
    for source_id in (3, 5):
        db.add(em.UpstreamSource(
            id=source_id, name=f"src-{source_id}", source_type="emby",
            base_url="https://example.invalid", auth_method="api_key",
            enabled=True))
    db.commit()


def _count(db):
    return db.query(em.UpstreamItemMapping).count()


def test_record_and_read_best(db):
    row = uim.record_mapping(db, local_item_id=10, source_id=3,
                             upstream_item_id="abc123", confidence=0.9,
                             evidence="tmdb")
    assert row is not None
    assert row.local_item_id == 10
    assert row.source_id == 3
    assert row.upstream_item_id == "abc123"
    assert row.confidence == 0.9
    assert row.evidence == "tmdb"
    best = uim.get_best_mapping(db, 10)
    assert best is not None
    assert (best.source_id, best.upstream_item_id) == (3, "abc123")


def test_record_is_idempotent(db):
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="abc123", confidence=0.5,
                       evidence="title+year")
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="abc123", confidence=0.8,
                       evidence="tmdb")
    assert _count(db) == 1
    best = uim.get_best_mapping(db, 10)
    assert best.confidence == 0.8
    assert best.evidence == "tmdb"


def test_read_prefers_highest_confidence(db):
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="low", confidence=0.4)
    uim.record_mapping(db, local_item_id=10, source_id=5,
                       upstream_item_id="high", confidence=0.95)
    best = uim.get_best_mapping(db, 10)
    assert (best.source_id, best.upstream_item_id) == (5, "high")
    assert best.confidence == 0.95


def test_read_tie_prefers_latest_update(db):
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="first", confidence=0.7)
    uim.record_mapping(db, local_item_id=10, source_id=5,
                       upstream_item_id="second", confidence=0.7)
    assert uim.get_best_mapping(db, 10).upstream_item_id == "second"
    # 同一三元组重写（置信度不变）即最近更新，重新占优。
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="first", confidence=0.7)
    assert uim.get_best_mapping(db, 10).upstream_item_id == "first"


def test_switch_off_writes_nothing(monkeypatch, db):
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "0")
    assert uim.record_mapping(db, local_item_id=10, source_id=3,
                              upstream_item_id="abc123",
                              confidence=0.9) is None
    assert _count(db) == 0
    assert uim.get_best_mapping(db, 10) is None


def test_read_empty_returns_none(db):
    assert uim.get_best_mapping(db, 999) is None


def test_read_unaffected_by_switch(monkeypatch, db):
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="abc123", confidence=0.9)
    monkeypatch.setenv(uim.MAPPING_ENABLED_ENV, "0")
    best = uim.get_best_mapping(db, 10)
    assert best is not None
    assert best.upstream_item_id == "abc123"


def test_confidence_clamped(db):
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="over", confidence=1.5)
    uim.record_mapping(db, local_item_id=11, source_id=3,
                       upstream_item_id="under", confidence=-0.5)
    assert uim.get_best_mapping(db, 10).confidence == 1.0
    assert uim.get_best_mapping(db, 11).confidence == 0.0


def test_invalid_input_rejected(db):
    with pytest.raises(ValueError):
        uim.record_mapping(db, local_item_id=10, source_id=3,
                           upstream_item_id="   ", confidence=0.9)
    with pytest.raises(ValueError):
        uim.record_mapping(db, local_item_id=0, source_id=3,
                           upstream_item_id="abc123", confidence=0.9)
    with pytest.raises(ValueError):
        uim.record_mapping(db, local_item_id=10, source_id=-1,
                           upstream_item_id="abc123", confidence=0.9)
    assert _count(db) == 0


def test_episode_index_zero_accepted(db):
    # 集号口径：episode_index 载荷是上游集号（Emby IndexNumber），
    # 0 = 第 0 集 / 特别篇，合法（本地 episode_number 同口径）；
    # 此前按正整数拒 0，曾把一整轮聚合打死（2026-11-01 生产实证）。
    row = uim.record_mapping(db, local_item_id=10, source_id=3,
                             upstream_item_id="ep0", confidence=0.9,
                             evidence="tmdb", episode_index=0)
    assert row is not None
    assert row.episode_index == 0
    assert uim.get_best_mapping(db, 10).episode_index == 0


def test_episode_index_negative_rejected(db):
    with pytest.raises(ValueError):
        uim.record_mapping(db, local_item_id=10, source_id=3,
                           upstream_item_id="ep-neg", confidence=0.9,
                           episode_index=-1)
    assert _count(db) == 0


def test_other_local_item_isolated(db):
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="abc123", confidence=0.9)
    assert uim.get_best_mapping(db, 11) is None

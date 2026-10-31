"""映射读契约收紧（上游聚合链优化 O-10）。

钉死的纪律：
① get_best_mapping 默认过两道校验：源必须存在且 enabled、本地条目
   必须是活行（未软删、未合并、未隐藏）；任一不过 → 该映射不算数；
② 最佳映射所在源失效时，回落到下一条有效映射，而不是整体 None；
③ 本地条目整体不存在 / 已死 → None，绝不把死映射递给消费方；
④ validate=False 是显式逃生口（管理端追溯等场景自担），默认不走。
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


def _item(db, item_id=10, **overrides):
    library = db.query(em.Library).first()
    if library is None:
        library = em.Library(guid="lib-contract", name="契约库")
        db.add(library)
        db.flush()
    fields = dict(
        id=item_id, guid=f"item-contract-{item_id}",
        library_id=library.id, item_type="movie",
        name=f"片{item_id}", file_path=f"mount://1/{item_id}.mp4",
    )
    fields.update(overrides)
    item = em.MediaItem(**fields)
    db.add(item)
    db.commit()
    return item


def _source(db, source_id=3, *, enabled=True):
    src = em.UpstreamSource(
        id=source_id, name=f"src-{source_id}", source_type="emby",
        base_url="https://example.invalid", auth_method="api_key",
        enabled=enabled)
    db.add(src)
    db.commit()
    return src


def _map(db, local_id=10, source_id=3, upstream_id="up-1", confidence=0.9):
    row = uim.record_mapping(db, local_item_id=local_id, source_id=source_id,
                             upstream_item_id=upstream_id,
                             confidence=confidence, evidence="tmdb")
    db.commit()
    return row


def test_happy_path_live_item_enabled_source(db):
    _item(db)
    _source(db)
    _map(db)
    best = uim.get_best_mapping(db, 10)
    assert best is not None
    assert best.upstream_item_id == "up-1"


def test_source_row_missing_returns_none(db):
    _item(db)
    _map(db)  # 源行根本不存在
    assert uim.get_best_mapping(db, 10) is None


def test_source_disabled_returns_none(db):
    _item(db)
    _source(db, enabled=False)
    _map(db)
    assert uim.get_best_mapping(db, 10) is None


def test_falls_back_to_next_valid_mapping(db):
    _item(db)
    _source(db, 3, enabled=False)
    _source(db, 5, enabled=True)
    _map(db, source_id=3, upstream_id="dead-src", confidence=0.95)
    _map(db, source_id=5, upstream_id="live-src", confidence=0.8)
    best = uim.get_best_mapping(db, 10)
    assert best is not None
    assert best.upstream_item_id == "live-src"


def test_local_item_row_missing_returns_none(db):
    _source(db)
    _map(db)  # 本地条目行不存在
    assert uim.get_best_mapping(db, 10) is None


def test_local_item_soft_deleted_returns_none(db):
    from datetime import datetime
    _item(db, deleted_at=datetime.now())
    _source(db)
    _map(db)
    assert uim.get_best_mapping(db, 10) is None


def test_local_item_merged_returns_none(db):
    _item(db, merged_into_id=777)
    _source(db)
    _map(db)
    assert uim.get_best_mapping(db, 10) is None


def test_local_item_hidden_returns_none(db):
    _item(db, is_hidden=True)
    _source(db)
    _map(db)
    assert uim.get_best_mapping(db, 10) is None


def test_validate_opt_out_returns_raw_best(db):
    _item(db)
    _source(db, enabled=False)
    _map(db)
    best = uim.get_best_mapping(db, 10, validate=False)
    assert best is not None
    assert best.upstream_item_id == "up-1"

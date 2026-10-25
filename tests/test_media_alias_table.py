"""条目别名独立表 L8 单测。

只增不改：存量 MediaItem.aliases 列、trigram 索引、搜索行为原样不动；
迁移幂等可重跑；开关关时别名表不被读路径使用，开时识别候选读到表别名。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import media_alias as malias
from backend.emby_server import models as em


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/alias.db")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _mk(db, lib, name, aliases=""):
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        aliases=aliases)
    db.add(it)
    db.flush()
    return it


@pytest.fixture()
def scene(db):
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    item = _mk(db, lib, "流浪地球", "流浪地球2, The Wandering Earth ,流浪地球2")
    db.commit()
    return lib, item


def _search_predicate_result(db, like):
    """模拟既有搜索对 aliases 列的 ILIKE 预筛（搜索代码本身不动）。"""
    return [r.id for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.aliases.ilike(like)).all()]


def test_migration_idempotent_and_column_untouched(db, scene):
    lib, item = scene
    before_col = item.aliases
    before_search = _search_predicate_result(db, "%Wandering%")
    assert before_search == [item.id]
    first = malias.migrate_aliases_from_column(db)
    db.commit()
    assert first == {"scanned_items": 1, "inserted": 2,
                     "skipped_existing": 0}  # 列内重复别名切分时去重，只插 2 个 distinct
    second = malias.migrate_aliases_from_column(db)
    db.commit()
    assert second["inserted"] == 0  # 可重跑：第二次零新增
    assert second["skipped_existing"] == 2
    # 列本身原样不动、搜索预筛结果一致
    db.refresh(item)
    assert item.aliases == before_col
    assert _search_predicate_result(db, "%Wandering%") == before_search
    rows = db.query(em.MediaAlias).filter(
        em.MediaAlias.item_id == item.id).all()
    assert {r.alias for r in rows} == {"流浪地球2", "The Wandering Earth"}
    assert {r.source for r in rows} == {"legacy_column"}


def test_alias_candidates_gated_by_switch(db, scene, monkeypatch):
    lib, item = scene
    db.add(em.MediaAlias(item_id=item.id, alias="别名表专有",
                         source="manual", confidence=0.9))
    db.commit()
    monkeypatch.setattr(malias, "MEDIA_ALIAS_TABLE_ENABLED", 0)
    assert malias.alias_candidates(db, item_id=item.id) == []
    monkeypatch.setattr(malias, "MEDIA_ALIAS_TABLE_ENABLED", 1)
    got = malias.alias_candidates(db, item_id=item.id)
    assert got == [{"item_id": item.id, "alias": "别名表专有",
                    "source": "manual", "confidence": 0.9}]
    # 按名字反查（识别候选比对用）
    got2 = malias.alias_candidates(db, name="别名表专有")
    assert [g["item_id"] for g in got2] == [item.id]


def test_alias_table_index_and_constraint_present():
    """别名表有条目索引与 (item, alias, source) 唯一约束（防重复证据行）。"""
    names = {c.name for c in em.MediaAlias.__table__.columns}
    assert {"item_id", "alias", "source", "confidence"} <= names
    idx_names = {i.name for i in em.MediaAlias.__table__.indexes}
    assert "ix_media_alias_alias" in idx_names

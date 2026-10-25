"""苹果 CMS W1（类型与持久化）：不联网、不碰生产。

覆盖：枚举成员；none 鉴权创建校验（username 空、密封空都通过）；
record_mapping 的 episode_index 写入 / upsert 更新 / None 不覆盖；
分类绑定 helper 的 upsert / 列表 / 仅启用过滤；
CHECK 迁移在非 PG 下直接跳过不出错。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as ups

PUBLIC_URL = "https://upstream.example.com:8096/"


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


# ---------- 枚举成员 ----------

def test_enums_include_applecms_and_none():
    assert "applecms" in em.UPSTREAM_SOURCE_TYPES
    assert "applecms" in ups.SOURCE_TYPES
    assert "none" in em.UPSTREAM_AUTH_METHODS
    assert "none" in ups.AUTH_METHODS
    # 旧取值原样保留，不许借机删档。
    assert em.UPSTREAM_SOURCE_TYPES[:2] == ("emby", "jellyfin")
    assert em.UPSTREAM_AUTH_METHODS[:2] == ("password", "api_key")


def test_applecms_source_row_passes_sqlite_check(db):
    row = em.UpstreamSource(name="CMS 源", source_type="applecms",
                            base_url="https://cms.example.com",
                            auth_method="none",
                            credential_scope="source",
                            health_status="unknown")
    db.add(row)
    db.flush()
    assert row.id is not None


# ---------- none 鉴权 ----------

def test_none_auth_create_no_username_no_secret(db):
    row = ups.create_source(db, name="CMS 无凭据", source_type="applecms",
                            base_url=PUBLIC_URL, auth_method="none",
                            username=None, secret=None)
    assert row.auth_method == "none"
    assert row.username is None
    assert row.sealed_secret is None
    assert ups.has_secret(row) is False
    assert ups.load_credential(row) is None


def test_none_auth_create_empty_username_and_secret(db):
    row = ups.create_source(db, name="CMS 空凭据", source_type="applecms",
                            base_url=PUBLIC_URL, auth_method="none",
                            username="   ", secret="")
    # 既有 create_source 口径：空白用户名 strip 后为空串（非 None），
    # 只要为空即等价于“可空”——本单不改既有口径，只断言 falsy。
    assert not row.username
    assert row.sealed_secret is None


def test_password_and_api_key_rules_unchanged(db):
    # password 仍必须填用户名；api_key 仍不许填用户名。
    with pytest.raises(ups.UpstreamSourceError):
        ups.create_source(db, name="缺用户", source_type="emby",
                          base_url=PUBLIC_URL, auth_method="password",
                          username=None, secret="pw")
    with pytest.raises(ups.UpstreamSourceError):
        ups.create_source(db, name="多用户", source_type="emby",
                          base_url=PUBLIC_URL, auth_method="api_key",
                          username="someone", secret="key")


def test_none_auth_update_keeps_no_secret(db):
    row = ups.create_source(db, name="CMS 改", source_type="applecms",
                            base_url=PUBLIC_URL, auth_method="none")
    updated = ups.update_source(db, row, auth_method="none")
    assert updated.auth_method == "none"
    assert updated.sealed_secret is None


# ---------- episode_index ----------

def test_episode_index_written_on_insert(db):
    row = uim.record_mapping(db, local_item_id=10, source_id=3,
                             upstream_item_id="ep1", confidence=0.9,
                             episode_index=1)
    assert row.episode_index == 1
    movie = uim.record_mapping(db, local_item_id=11, source_id=3,
                               upstream_item_id="movie1", confidence=0.9)
    assert movie.episode_index is None


def test_episode_index_upsert_updates_only_when_not_none(db):
    uim.record_mapping(db, local_item_id=10, source_id=3,
                       upstream_item_id="ep1", confidence=0.5,
                       episode_index=2)
    # 传入 None：不覆盖已有值，只更新置信度。
    row = uim.record_mapping(db, local_item_id=10, source_id=3,
                             upstream_item_id="ep1", confidence=0.8,
                             episode_index=None)
    assert row.episode_index == 2
    assert row.confidence == 0.8
    # 传入新值：更新。
    row = uim.record_mapping(db, local_item_id=10, source_id=3,
                             upstream_item_id="ep1", confidence=0.8,
                             episode_index=3)
    assert row.episode_index == 3
    assert db.query(em.UpstreamItemMapping).count() == 1


def test_episode_index_confidence_clamp_unchanged(db):
    row = uim.record_mapping(db, local_item_id=10, source_id=3,
                             upstream_item_id="over", confidence=1.5,
                             episode_index=1)
    assert row.confidence == 1.0
    assert row.episode_index == 1


# ---------- 分类绑定 ----------

def test_category_binding_upsert_and_list(db):
    ups.set_category_binding(db, 3, "cat1", "电影", True)
    ups.set_category_binding(db, 3, "cat2", "电视剧", False)
    # 同一（源，分类）重写：不建新行，只更新名与开关。
    ups.set_category_binding(db, 3, "cat1", "电影新名", False)
    rows = ups.list_category_bindings(db, 3)
    assert len(rows) == 2
    assert rows[0].upstream_category_id == "cat1"
    assert rows[0].upstream_category_name == "电影新名"
    assert rows[0].sync_enabled is False
    # 别的源互不干扰。
    assert ups.list_category_bindings(db, 99) == []


def test_enabled_category_bindings_only(db):
    ups.set_category_binding(db, 3, "cat1", "电影", True)
    ups.set_category_binding(db, 3, "cat2", "电视剧", False)
    ups.set_category_binding(db, 3, "cat3", "综艺", True)
    enabled = ups.enabled_category_bindings(db, 3)
    assert [r.upstream_category_id for r in enabled] == ["cat1", "cat3"]


def test_category_binding_requires_category_id(db):
    with pytest.raises(ups.UpstreamSourceError):
        ups.set_category_binding(db, 3, "   ", "空", True)


def test_category_binding_unique_constraint(db):
    from sqlalchemy.exc import IntegrityError

    ups.set_category_binding(db, 3, "cat1", "电影", True)
    db.add(em.SourceCategoryBinding(source_id=3, upstream_category_id="cat1",
                                    upstream_category_name="重复",
                                    sync_enabled=True))
    with pytest.raises(IntegrityError):
        db.flush()


# ---------- CHECK 迁移：非 PG 跳过 ----------

def test_check_migration_skips_non_postgres(monkeypatch):
    # 本测试跑在 SQLite 语境下：函数须直接返回、不抛错、不碰 engine。
    # 用一个假 engine 对象验证“方言非 PG 即跳过”的分支本身。
    import backend.database as database_mod

    class _Dialect:
        name = "sqlite"

    class _FakeEngine:
        dialect = _Dialect()

    monkeypatch.setattr(database_mod, "engine", _FakeEngine())
    # existing_tables 传空集也应直接返回（先判方言）。
    assert database_mod._migrate_upstream_source_checks(set()) is None

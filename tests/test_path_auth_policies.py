"""三层路径授权 + 源路径限流落库（清单第 15 项，挂载-7 照抄）。

钉死的范围（多一件不做）：
- 三层授权判定：模板级 / 用户级 / 库级各自生效与叠加、无记录默认
  放行、DB 异常 fail-closed、非挂载路径放行。
- 限流策略来源切换：表优先、表空回落旧配置、导入幂等、执法一致
  （命中集合与租约键与现行配置口径逐字一致）。
- 判定与限流都不接播放请求链路（骨架先行，接线是后续项），故本
  文件只测函数本身，不测播放端到端。
"""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import path_auth as pa
from backend.emby_server import stream_limits as sl
from backend.integrations import store


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
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


def _item(library_id=1, file_path="mount://3/电影/a.mkv"):
    return SimpleNamespace(library_id=library_id, file_path=file_path)


def _seed_template(db, name="User"):
    tpl = em.RoleTemplate(name=name, description="", is_seed=True)
    db.add(tpl)
    db.commit()
    return tpl


def _assign(db, user_id, template_id):
    db.add(em.UserRoleTemplate(user_id=user_id,
                               role_template_id=template_id))
    db.commit()


# ---------- 前缀口径 ----------

def test_prefix_normalize_agrees_with_stream_limits():
    for raw, want in [("/电影/", "/电影"), ("电影", "/电影"), ("/", "/"),
                      ("", None), (None, None), ("  /A/B/  ", "/A/B")]:
        assert pa.normalize_prefix(raw) == want
        assert pa.normalize_prefix(raw) == sl._normalize_prefix(raw)


def test_under_prefix_sibling_trap():
    assert pa.under_prefix("/", "/任何/路径")
    assert pa.under_prefix("/电影", "/电影/a.mkv")
    assert pa.under_prefix("/电影", "/电影")
    assert not pa.under_prefix("/电影", "/电影院/a.mkv")
    assert not pa.under_prefix("/A", "/AB")


# ---------- 三层授权 ----------

def test_no_records_allow(db):
    assert pa.check_path_access(db, 7, _item()) is None


def test_non_mount_path_allow_even_with_grants(db):
    db.add(em.UserSourcePathGrant(user_id=7, mount_id=3,
                                  path_prefix="/电影"))
    db.commit()
    assert pa.check_path_access(
        db, 7, _item(file_path="/media/local/a.mkv")) is None


def test_template_grant_allow_and_deny(db):
    tpl = _seed_template(db)
    _assign(db, 7, tpl.id)
    db.add(em.RoleTemplateSourcePathGrant(
        role_template_id=tpl.id, mount_id=3, path_prefix="/电影"))
    db.commit()
    assert pa.check_path_access(db, 7, _item()) is None
    assert pa.check_path_access(
        db, 7, _item(file_path="mount://3/剧集/b.mkv")) is not None
    # 别人的模板授权不影响本用户
    assert pa.check_path_access(db, 8, _item()) is None


def test_user_grant_allow_and_deny(db):
    db.add(em.UserSourcePathGrant(user_id=7, mount_id=3,
                                  path_prefix="/电影/科幻"))
    db.commit()
    assert pa.check_path_access(
        db, 7, _item(file_path="mount://3/电影/科幻/c.mkv")) is None
    assert pa.check_path_access(db, 7, _item()) is not None


def test_library_grant_scoped_to_item_library(db):
    db.add(em.LibraryAccessGrant(library_id=1, mount_id=3,
                                 path_prefix="/电影"))
    db.commit()
    assert pa.check_path_access(db, 7, _item(library_id=1)) is None
    # 同一挂载同一路径、另一库的条目不受该库授权约束（该库无行 → 不约束）
    assert pa.check_path_access(db, 7, _item(library_id=2)) is None
    # 但该库自己的越界路径被拦
    assert pa.check_path_access(
        db, 7, _item(library_id=1,
                     file_path="mount://3/剧集/b.mkv")) is not None


def test_layers_stack_all_nonempty_must_pass(db):
    tpl = _seed_template(db)
    _assign(db, 7, tpl.id)
    db.add(em.RoleTemplateSourcePathGrant(
        role_template_id=tpl.id, mount_id=3, path_prefix="/电影"))
    db.add(em.UserSourcePathGrant(user_id=7, mount_id=3,
                                  path_prefix="/电影/科幻"))
    db.commit()
    # 模板层过、用户层不过 → 拒绝（两层都非空，必须都过）
    assert pa.check_path_access(db, 7, _item()) is not None
    assert pa.check_path_access(
        db, 7, _item(file_path="mount://3/电影/科幻/c.mkv")) is None


def test_empty_layer_does_not_constrain(db):
    tpl = _seed_template(db)
    _assign(db, 7, tpl.id)
    db.add(em.RoleTemplateSourcePathGrant(
        role_template_id=tpl.id, mount_id=3, path_prefix="/电影"))
    db.commit()
    # 用户层无行 → 不约束，只算模板层
    assert pa.check_path_access(db, 7, _item()) is None


def test_user_none_only_library_layer_counts(db):
    db.add(em.LibraryAccessGrant(library_id=1, mount_id=3,
                                 path_prefix="/电影"))
    db.commit()
    assert pa.check_path_access(db, None, _item()) is None
    assert pa.check_path_access(
        db, None, _item(file_path="mount://3/剧集/b.mkv")) is not None


def test_db_error_fail_closed():
    class BrokenDB:
        def query(self, *args):
            raise RuntimeError("db down")

    reason = pa.check_path_access(BrokenDB(), 7, _item())
    assert reason is not None


def test_seed_templates_idempotent(db):
    first = pa.ensure_seed_templates(db)
    second = pa.ensure_seed_templates(db)
    assert first == second
    assert set(first) == {"User", "RestrictedUser", "Admin"}
    assert db.query(em.RoleTemplate).count() == 3


def test_grant_unique_constraints(db):
    db.add(em.UserSourcePathGrant(user_id=7, mount_id=3,
                                  path_prefix="/电影"))
    db.commit()
    db.add(em.UserSourcePathGrant(user_id=7, mount_id=3,
                                  path_prefix="/电影"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- 限流策略来源 ----------

def _seed_config(db, rows):
    db.add(base_models.SystemConfig(key=sl.POLICIES_KEY,
                                    value=json.dumps(rows)))
    db.commit()


def test_table_first_wins_over_config(db):
    db.add(em.SourcePathPolicy(mount_id=3, path_prefix="/电影",
                               priority=0, max_concurrent_streams=5))
    db.commit()
    _seed_config(db, [{"mount_id": 3, "prefix": "/电影",
                       "max_streams": 2}])
    got = sl.load_policies(db)
    assert [(p.mount_id, p.prefix, p.max_streams) for p in got] == [
        (3, "/电影", 5)]


def test_config_fallback_when_table_empty(db):
    _seed_config(db, [{"mount_id": 3, "prefix": "/电影",
                       "max_streams": 2},
                      {"mount_id": 3, "prefix": "坏行"}])
    got = sl.load_policies(db)
    assert [(p.mount_id, p.prefix, p.max_streams) for p in got] == [
        (3, "/电影", 2)]


def test_import_config_policies_idempotent(db):
    _seed_config(db, [{"mount_id": 3, "prefix": "/电影",
                       "max_streams": 2},
                      {"mount_id": 4, "prefix": "/",
                       "max_streams": 1}])
    assert sl.import_config_policies(db) == {"imported": 2, "skipped": 0}
    assert sl.import_config_policies(db) == {"imported": 0, "skipped": 2}
    got = sl.load_policies(db)
    assert sorted((p.mount_id, p.prefix, p.max_streams)
                  for p in got) == [(3, "/电影", 2), (4, "/", 1)]


def test_enforcement_equivalence_table_vs_config(db):
    config_rows = [{"mount_id": 3, "prefix": "/电影", "max_streams": 2},
                   {"mount_id": 3, "prefix": "/", "max_streams": 9}]
    parsed = sl.parse_policies(config_rows)
    for p in parsed:
        db.add(em.SourcePathPolicy(mount_id=p.mount_id,
                                   path_prefix=p.prefix, priority=0,
                                   max_concurrent_streams=p.max_streams))
    db.commit()
    table_loaded = sl.load_policies(db)
    # 执法等价按集合比（表行按 priority/path 排序、配置行按插入序，
    # 顺序不同但命中集合相同；执法对全部命中策略生效，与顺序无关，
    # 仅多策略同拒时先报哪条前缀的名可能不同）。
    for rel in ["/电影/a.mkv", "/剧集/b.mkv"]:
        assert sorted(sl.match_policies(table_loaded, 3, rel),
                      key=lambda p: (p.prefix, p.max_streams)) == \
            sorted(sl.match_policies(parsed, 3, rel),
                   key=lambda p: (p.prefix, p.max_streams))
    # 租约键口径不变：同挂载同前缀的表行与配置行键相同（按前缀配对）
    by_prefix_table = {p.prefix: p for p in table_loaded}
    by_prefix_config = {p.prefix: p for p in parsed}
    for prefix in by_prefix_table:
        assert sl._scope_key(by_prefix_table[prefix]) == \
            sl._scope_key(by_prefix_config[prefix])


def test_table_policy_cache_and_clear(db):
    assert sl.load_policies(db) == []
    db.add(em.SourcePathPolicy(mount_id=3, path_prefix="/电影",
                               priority=0, max_concurrent_streams=2))
    db.commit()
    # 缓存期内仍见旧值；清缓存后见新值
    assert sl.load_policies(db) == []
    sl.clear_table_policy_cache()
    got = sl.load_policies(db)
    assert [(p.mount_id, p.prefix, p.max_streams) for p in got] == [
        (3, "/电影", 2)]


def test_policy_unique_pair(db):
    db.add(em.SourcePathPolicy(mount_id=3, path_prefix="/电影",
                               priority=0, max_concurrent_streams=2))
    db.commit()
    db.add(em.SourcePathPolicy(mount_id=3, path_prefix="/电影",
                               priority=5, max_concurrent_streams=3))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

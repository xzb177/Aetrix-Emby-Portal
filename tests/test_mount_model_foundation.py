"""挂载模型地基（FMBY 挂载学习，任务清单第 1 项）。

钉死的范围（多一件不做）：
- 新表 ``library_sources`` / ``mount_credentials`` + ``storage_mounts``
  五能力位列（只声明、不执法；无消费方读新表做分支）。
- 回填：旧 ``paths`` / ``mount_ids`` 只读不写；危险组合只记警告。
- 双读校验：新表读数与现行 ``library_sources()`` 输出逐项比对，
  差异只报不拦。
- 凭据密封复用 25-1；KEK 目录只从配置读、无代码默认值。
"""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mount_sources as ms


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


def _lib(db, guid, paths="", mount_ids=""):
    lib = em.Library(guid=guid, name=guid, paths=paths, mount_ids=mount_ids)
    db.add(lib)
    db.commit()
    return lib


def _mount(db, name, mount_type, path=""):
    mount = em.StorageMount(name=name, mount_type=mount_type, path=path)
    db.add(mount)
    db.commit()
    return mount


# ---------- 新表与约束 ----------

def test_library_sources_unique_constraint(db):
    lib = _lib(db, "lib-uq")
    db.add(em.LibrarySource(library_id=lib.id, mount_id=1, sub_path="/a"))
    db.commit()
    db.add(em.LibrarySource(library_id=lib.id, mount_id=1, sub_path="/a"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
    db.add(em.LibrarySource(library_id=lib.id, mount_id=1, sub_path="/b"))
    db.commit()
    rows = db.query(em.LibrarySource).filter(
        em.LibrarySource.library_id == lib.id).all()
    assert len(rows) == 2


def test_storage_mounts_capability_defaults_false(db):
    mount = _mount(db, "m-def", "local", "/media")
    assert mount.can_list is False
    assert mount.can_random_read is False
    assert mount.can_read_sidecar is False
    assert mount.can_generate_play_target is False
    assert mount.can_refresh_credentials is False


def test_scope_enum_extended_and_per_user_still_rejected(db):
    assert em.UPSTREAM_CREDENTIAL_SCOPES == ("source", "user")
    row = em.MountCredential(mount_id=1, scope="user", owner_user_id=9,
                             sealed_credential="x", key_id="fernet-v1")
    db.add(row)
    db.commit()
    bad = em.MountCredential(mount_id=1, scope="per_user",
                             sealed_credential="x", key_id="fernet-v1")
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.flush()


# ---------- 回填 ----------

def test_backfill_subpath_priority_warns_and_keeps_old_config(db, caplog):
    m1 = _mount(db, "m-drive", "drive", "drive:shared")
    m2 = _mount(db, "m-115", "115", "115:/")
    lib = _lib(db, "lib-danger", paths="mount://%d/剧集/儿童" % m1.id,
               mount_ids="%d,%d" % (m1.id, m2.id))
    with caplog.at_level(logging.WARNING, logger="backend.emby_server.mount_sources"):
        stats = ms.backfill_library_sources(db, lib)
    assert stats["rows_written"] == 2
    rows = {(r.mount_id, r.sub_path) for r in
            db.query(em.LibrarySource).filter(
                em.LibrarySource.library_id == lib.id).all()}
    # 子目录行胜出，整挂载根行被过滤（2026-09-26 演唱会事故教训）
    assert rows == {(m1.id, "/剧集/儿童"), (m2.id, "/")}
    assert "同挂载并存" in caplog.text
    # 旧配置只读不写
    db.refresh(lib)
    assert lib.paths == "mount://%d/剧集/儿童" % m1.id
    assert lib.mount_ids == "%d,%d" % (m1.id, m2.id)


def test_backfill_local_rows_and_idempotent(db, tmp_path):
    local_dir = str(tmp_path / "movies")
    lib = _lib(db, "lib-local", paths="%s,/media/dead" % local_dir)
    first = ms.backfill_library_sources(db, lib)
    assert first["rows_written"] == 2
    second = ms.backfill_library_sources(db, lib)
    assert second["rows_written"] == 0
    assert second["rows_skipped"] == 2
    rows = {(r.mount_id, r.sub_path) for r in
            db.query(em.LibrarySource).filter(
                em.LibrarySource.library_id == lib.id).all()}
    assert rows == {(None, local_dir), (None, "/media/dead")}


def test_capability_backfill_mapping(db):
    cases = {
        "local": (True, True, True, True, False),
        "115": (True, True, True, True, False),
        "drive": (True, True, True, True, True),
        "rclone": (True, True, True, True, False),
        "webdav-legacy": (False, False, False, False, False),
    }
    for mount_type, _ in cases.items():
        _mount(db, "m-%s" % mount_type, mount_type, "/x")
    ms.backfill_library_sources(db)
    for mount_type, expected in cases.items():
        mount = db.query(em.StorageMount).filter(
            em.StorageMount.name == "m-%s" % mount_type).one()
        got = (mount.can_list, mount.can_random_read, mount.can_read_sidecar,
               mount.can_generate_play_target, mount.can_refresh_credentials)
        assert got == expected, mount_type


# ---------- 双读校验 ----------

def test_compare_matches_legacy_output(db, tmp_path):
    sub = tmp_path / "儿童"
    sub.mkdir()
    mount = _mount(db, "m-local", "local", str(tmp_path))
    lib = _lib(db, "lib-cmp", paths="mount://%d/儿童" % mount.id,
               mount_ids="")
    ms.backfill_library_sources(db, lib)
    result = ms.compare_library_sources(db, lib)
    assert result["matched"] == {("mount", mount.id, "/儿童")}
    assert result["only_new"] == set()
    assert result["only_old"] == set()
    assert result["failed"] == []


def test_compare_flags_dangling_mount_as_only_new_and_failed(db):
    lib = _lib(db, "lib-dangle", paths="", mount_ids="99")
    ms.backfill_library_sources(db, lib)
    result = ms.compare_library_sources(db, lib)
    assert ("mount", 99, "/") in result["only_new"]
    assert any("99" in (entry.get("label", "") + entry.get("reason", ""))
               for entry in result["failed"])


# ---------- KEK 配置 ----------

def test_kek_dir_no_code_default(monkeypatch):
    monkeypatch.delenv("MOUNT_KEK_DIR", raising=False)
    assert ms.resolve_kek_dir() is None
    monkeypatch.setenv("MOUNT_KEK_DIR", "/data/secrets")
    assert ms.resolve_kek_dir() == "/data/secrets"

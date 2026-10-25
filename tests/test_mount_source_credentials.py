"""源级挂载凭据迁移 + KEK 文件信封（FMBY 挂载清单第 4 步）。

安全：全文件只用假密钥与假凭据（``fake-`` 前缀），不读生产、不碰
真实 KEK 目录；KEK 文件一律写进 pytest tmp_path，权限 0600。
断言只比 has/状态词/计数，不比任何真实秘密。
"""

import json
import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mount_source_credentials as msc
from backend.emby_server import mounts as mounts_mod
from backend.emby_server import upstream_sources as ups


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


@pytest.fixture()
def kek_dir(tmp_path, monkeypatch):
    """0600 的 KEK 目录：两把假 KEK（old/new），供轮换双读用。"""
    d = tmp_path / "secrets"
    d.mkdir()
    keys = {}
    for key_id in ("fake-kek-old", "fake-kek-new"):
        key = Fernet.generate_key()
        path = d / f"{key_id}.b64"
        path.write_bytes(key + b"\n")
        os.chmod(path, 0o600)
        keys[key_id] = key
    monkeypatch.setenv("MOUNT_KEK_DIR", str(d))
    msc.clear_kek_cache()
    yield d
    msc.clear_kek_cache()


def _mount(db, name, mount_type, path, config=None):
    mount = em.StorageMount(name=name, mount_type=mount_type, path=path,
                            config=json.dumps(config or {}))
    db.add(mount)
    db.commit()
    return mount


# ---------- KEK 文件信封 ----------

def test_kek_roundtrip_and_prefix(kek_dir):
    sealed = msc.seal_with_kek("fake-plaintext-1", "fake-kek-old")
    assert sealed.startswith("kek-v1:")
    assert "fake-plaintext-1" not in sealed
    assert msc.unseal_with_kek(sealed, "fake-kek-old") == "fake-plaintext-1"


def test_kek_rejects_bad_permissions(tmp_path, monkeypatch):
    d = tmp_path / "secrets"
    d.mkdir()
    path = d / "fake-kek-loose.b64"
    path.write_bytes(Fernet.generate_key())
    os.chmod(path, 0o644)
    monkeypatch.setenv("MOUNT_KEK_DIR", str(d))
    msc.clear_kek_cache()
    with pytest.raises(msc.KEKError):
        msc.load_kek("fake-kek-loose")
    msc.clear_kek_cache()


def test_kek_rejects_symlink_and_bad_content_and_missing_dir(tmp_path, monkeypatch):
    d = tmp_path / "secrets"
    d.mkdir()
    real = d / "real.b64"
    real.write_bytes(Fernet.generate_key())
    os.chmod(real, 0o600)
    os.symlink(real, d / "fake-kek-link.b64")
    bad = d / "fake-kek-bad.b64"
    bad.write_bytes(b"not-a-fernet-key")
    os.chmod(bad, 0o600)
    monkeypatch.setenv("MOUNT_KEK_DIR", str(d))
    msc.clear_kek_cache()
    for key_id in ("fake-kek-link", "fake-kek-bad", "fake-kek-missing"):
        with pytest.raises(msc.KEKError):
            msc.load_kek(key_id)
    with pytest.raises(msc.KEKError):
        msc.load_kek("../escape")
    monkeypatch.delenv("MOUNT_KEK_DIR", raising=False)
    msc.clear_kek_cache()
    with pytest.raises(msc.KEKError):
        msc.load_kek("fake-kek-old")
    msc.clear_kek_cache()


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("MOUNT_SOURCE_CREDENTIALS_ENABLED", raising=False)
    assert msc.source_credentials_enabled() is False
    monkeypatch.setenv("MOUNT_SOURCE_CREDENTIALS_ENABLED", "1")
    assert msc.source_credentials_enabled() is True


# ---------- 行级双读：旧行永可读，用户行不混入 ----------

def test_legacy_row_stays_readable_after_kek_adoption(db, kek_dir):
    m1 = _mount(db, "m-legacy", "115", "115:/")
    m2 = _mount(db, "m-kek", "115", "115:/")
    msc.upsert_source_credential(db, m1.id, "fake-legacy-cookie")
    msc.upsert_source_credential(db, m2.id, "fake-kek-cookie", key_id="fake-kek-old")
    db.commit()
    assert msc.load_source_credential(db, m1.id) == "fake-legacy-cookie"
    assert msc.load_source_credential(db, m2.id) == "fake-kek-cookie"
    row1 = msc.get_source_credential_row(db, m1.id)
    assert row1.key_id == "fernet-v1"
    row2 = msc.get_source_credential_row(db, m2.id)
    assert row2.key_id == "fake-kek-old"


def test_user_rows_never_read_as_source(db):
    binding_row = em.MountCredential(mount_id=None, scope="user",
                                     owner_user_id=7, sealed_credential="",
                                     key_id="fernet-v1", status="active")
    db.add(binding_row)
    db.commit()
    assert msc.get_source_credential_row(db, None) is None
    assert msc.load_source_credential(db, None) is None


def test_upsert_updates_in_place_without_duplicate(db):
    m = _mount(db, "m-upsert", "115", "115:/")
    msc.upsert_source_credential(db, m.id, "fake-first")
    msc.upsert_source_credential(db, m.id, "fake-second")
    db.commit()
    rows = db.query(em.MountCredential).filter(
        em.MountCredential.mount_id == m.id,
        em.MountCredential.scope == "source").all()
    assert len(rows) == 1
    assert msc.load_source_credential(db, m.id) == "fake-second"


def test_corrupt_row_raises_instead_of_silent_fallback(db):
    m = _mount(db, "m-corrupt", "115", "115:/")
    db.add(em.MountCredential(mount_id=m.id, scope="source",
                              sealed_credential="kek-v1:garbage",
                              key_id="fernet-v1", status="active"))
    db.commit()
    with pytest.raises(ups.SealedSecretError):
        msc.load_source_credential(db, m.id)


# ---------- 迁移：快照语义 + 已有行不覆盖 ----------

def test_migrate_115_explicit_cookie_and_skips(db):
    m_explicit = _mount(db, "m-115-exp", "115", "115:/",
                        {"cookie": "fake-config-cookie"})
    m_bare = _mount(db, "m-115-bare", "115", "115:/")
    m_local = _mount(db, "m-local", "local", "/media")
    stats = msc.migrate_source_credentials(db)
    assert stats["migrated"] == 1
    assert stats["skipped"] == 2
    assert msc.load_source_credential(db, m_explicit.id) == "fake-config-cookie"
    assert msc.load_source_credential(db, m_bare.id) is None
    # 第二轮：已有行不覆盖、不重加密（改配置再迁，表里仍是第一份）
    m_explicit.config = json.dumps({"cookie": "fake-changed-cookie"})
    db.commit()
    stats2 = msc.migrate_source_credentials(db)
    assert stats2["existing"] == 1
    assert stats2["migrated"] == 0
    assert msc.load_source_credential(db, m_explicit.id) == "fake-config-cookie"


def test_migrate_dry_run_writes_nothing(db):
    m = _mount(db, "m-115-dry", "115", "115:/", {"cookie": "fake-dry-cookie"})
    stats = msc.migrate_source_credentials(db, dry_run=True)
    assert stats["migrated"] == 1 and stats["dry_run"] is True
    assert msc.get_source_credential_row(db, m.id) is None


def test_migrate_drive_reference_payload_is_not_a_secret(db):
    m = _mount(db, "m-drive", "drive", "drive:shared")
    stats = msc.migrate_source_credentials(db)
    assert stats["migrated"] == 1
    payload = json.loads(msc.load_source_credential(db, m.id))
    assert payload["kind"] == "drive_sa_ref"
    assert "ref" in payload


def test_migrate_with_missing_kek_writes_nothing(db, monkeypatch):
    m = _mount(db, "m-115-kek", "115", "115:/", {"cookie": "fake-kek-cookie"})
    monkeypatch.delenv("MOUNT_KEK_DIR", raising=False)
    msc.clear_kek_cache()
    with pytest.raises(msc.KEKError):
        msc.migrate_source_credentials(db, key_id="fake-kek-old")
    assert msc.get_source_credential_row(db, m.id) is None


# ---------- 轮换：新 key_id + 重封 + 按挂载保留 ----------

def test_rotate_kek_with_keep_and_failed_row(db, kek_dir):
    m_old = _mount(db, "m-rot-old", "115", "115:/")
    m_legacy = _mount(db, "m-rot-legacy", "115", "115:/")
    m_keep = _mount(db, "m-rot-keep", "115", "115:/")
    m_bad = _mount(db, "m-rot-bad", "115", "115:/")
    msc.upsert_source_credential(db, m_old.id, "fake-old-cookie", key_id="fake-kek-old")
    msc.upsert_source_credential(db, m_legacy.id, "fake-legacy-cookie")
    msc.upsert_source_credential(db, m_keep.id, "fake-keep-cookie", key_id="fake-kek-old")
    db.add(em.MountCredential(mount_id=m_bad.id, scope="source",
                              sealed_credential="kek-v1:garbage",
                              key_id="fake-kek-old", status="active"))
    db.commit()
    stats = msc.rotate_source_credentials(
        db, new_key_id="fake-kek-new", keep_mount_ids=[m_keep.id])
    assert stats["rotated"] == 2
    assert stats["kept"] == 1
    assert stats["failed"] == 1
    assert msc.load_source_credential(db, m_old.id) == "fake-old-cookie"
    assert msc.load_source_credential(db, m_legacy.id) == "fake-legacy-cookie"
    assert msc.get_source_credential_row(db, m_old.id).key_id == "fake-kek-new"
    assert msc.get_source_credential_row(db, m_keep.id).key_id == "fake-kek-old"
    assert msc.load_source_credential(db, m_keep.id) == "fake-keep-cookie"
    bad = msc.get_source_credential_row(db, m_bad.id)
    assert bad.sealed_credential == "kek-v1:garbage"


def test_rotate_with_missing_new_kek_writes_nothing(db, monkeypatch):
    m = _mount(db, "m-rot-missing", "115", "115:/")
    msc.upsert_source_credential(db, m.id, "fake-cookie")
    db.commit()
    monkeypatch.delenv("MOUNT_KEK_DIR", raising=False)
    msc.clear_kek_cache()
    with pytest.raises(msc.KEKError):
        msc.rotate_source_credentials(db, new_key_id="fake-kek-new")
    assert msc.get_source_credential_row(db, m.id).key_id == "fernet-v1"


# ---------- 115 接线：旗标关走旧路，开走表、缺行回落、坏行响亮 ----------

def _pan115_mount(db, mount):
    return mounts_mod.Pan115Mount(mount, db=db)


def test_pan115_flag_off_ignores_table_row(db, monkeypatch):
    monkeypatch.delenv("MOUNT_SOURCE_CREDENTIALS_ENABLED", raising=False)
    monkeypatch.delenv("PAN115_COOKIE", raising=False)
    account = em.Pan115Account(name="fake-default", cookie="fake-default-cookie",
                               is_default=True, is_enabled=True)
    db.add(account)
    mount = _mount(db, "m-pan-off", "115", "115:/")
    msc.upsert_source_credential(db, mount.id, "fake-table-cookie")
    db.commit()
    assert _pan115_mount(db, mount)._cookie() == "fake-default-cookie"


def test_pan115_flag_on_prefers_table_and_falls_back_with_warning(
        db, monkeypatch, caplog):
    monkeypatch.setenv("MOUNT_SOURCE_CREDENTIALS_ENABLED", "1")
    monkeypatch.delenv("PAN115_COOKIE", raising=False)
    account = em.Pan115Account(name="fake-default2", cookie="fake-default-cookie",
                               is_default=True, is_enabled=True)
    db.add(account)
    mount = _mount(db, "m-pan-on", "115", "115:/")
    msc.upsert_source_credential(db, mount.id, "fake-table-cookie")
    db.commit()
    assert _pan115_mount(db, mount)._cookie() == "fake-table-cookie"
    # 删表行 → 回落旧路径并记警告（警告里只有挂载 id，没有凭据）
    db.delete(msc.get_source_credential_row(db, mount.id))
    db.commit()
    with caplog.at_level("WARNING"):
        assert _pan115_mount(db, mount)._cookie() == "fake-default-cookie"
    assert any("回落旧 115 凭据路径" in rec.message for rec in caplog.records)


def test_pan115_flag_on_corrupt_row_raises_auth_error(db, monkeypatch):
    monkeypatch.setenv("MOUNT_SOURCE_CREDENTIALS_ENABLED", "1")
    monkeypatch.delenv("PAN115_COOKIE", raising=False)
    mount = _mount(db, "m-pan-bad", "115", "115:/")
    db.add(em.MountCredential(mount_id=mount.id, scope="source",
                              sealed_credential="kek-v1:garbage",
                              key_id="fernet-v1", status="active"))
    db.commit()
    with pytest.raises(mounts_mod.MountAuthError):
        _pan115_mount(db, mount)._cookie()

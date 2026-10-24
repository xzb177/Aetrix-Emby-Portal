"""挂载来源切表 + 能力门控（FMBY 挂载学习，任务清单第 2 项）。

钉死的范围（多一件不做）：
- 来源读写切新表：扫描（read_library_sources）、追新、探针、监听、
  Drive Changes 读新表行；新表无行时回落旧列并记警告；管理端保存
  只写新表、旧列反向同步（双写期）。
- 能力门控：can_list=False 的源不进扫描、can_generate_play_target=False
  的源不进变体清单、can_refresh_credentials=False 的源不走刷新重试；
  四家 provider 声明与 #1230 回填派生口径逐字一致。
- 旧 resolve() 已删：换链一律走 resolve_play_target，全仓零调用方。
"""

import logging
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mount_sources as ms
from backend.emby_server import mounts as mnt
from backend.emby_server import play_variants as pv
from backend.emby_server.mount_drive import DriveMount
from backend.emby_server.mount_rclone import RcloneMount


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


def _mount(db, name, mount_type, path="", enabled=True):
    mount = em.StorageMount(name=name, mount_type=mount_type, path=path,
                            is_enabled=enabled)
    db.add(mount)
    db.commit()
    return mount


def _row(db, lib, mount_id, sub_path, enabled=True):
    row = em.LibrarySource(library_id=lib.id, mount_id=mount_id,
                           sub_path=sub_path, is_enabled=enabled)
    db.add(row)
    db.commit()
    return row


def _source_keys(sources):
    keys = set()
    for src in sources:
        if src.kind == "mount" and src.mount is not None:
            keys.add(("mount", src.mount.id, src.subpath))
        elif src.kind == "local":
            keys.add(("local", None, src.path))
    return keys


# ---------- 1. 来源读写切新表 ----------

def test_read_prefers_new_table_over_old_columns(db, tmp_path):
    """新表有行时读新表，不读旧列（切表本身）。"""
    real = tmp_path / "real"
    real.mkdir()
    mount = _mount(db, "m-local", "local", str(tmp_path))
    lib = _lib(db, "lib-cut", paths="/nonexistent-dir", mount_ids="")
    _row(db, lib, None, str(real))
    _row(db, lib, mount.id, "/")
    sources, failed = ms.read_library_sources(db, lib)
    assert failed == []
    assert _source_keys(sources) == {("local", None, str(real)),
                                     ("mount", mount.id, "/")}


def test_read_falls_back_to_old_columns_when_table_empty(db, tmp_path, caplog):
    """新表无行时回落旧列解析，并记警告（不是静默失败）。"""
    real = tmp_path / "real"
    real.mkdir()
    lib = _lib(db, "lib-fallback", paths=str(real), mount_ids="")
    with caplog.at_level(logging.WARNING, logger="backend.emby_server.mount_sources"):
        sources, failed = ms.read_library_sources(db, lib)
    assert failed == []
    assert _source_keys(sources) == {("local", None, str(real))}
    assert "回落旧" in caplog.text


def test_read_disabled_row_goes_to_failed_not_cleaned(db, tmp_path):
    """新表停用行进 failed（扫描据此跳过清理，不当成已删除）。"""
    real = tmp_path / "real"
    real.mkdir()
    gone = tmp_path / "gone"
    lib = _lib(db, "lib-disabled", paths="", mount_ids="")
    _row(db, lib, None, str(real), enabled=True)
    _row(db, lib, None, str(gone), enabled=False)
    sources, failed = ms.read_library_sources(db, lib)
    assert _source_keys(sources) == {("local", None, str(real))}
    assert len(failed) == 1
    assert failed[0]["label"] == str(gone)
    assert "停用" in failed[0]["reason"]


def test_cutover_matches_legacy_after_backfill(db, tmp_path):
    """切表后新表来源集合与旧输出逐行一致（复用 #1230 双读校验）。"""
    root = tmp_path / "root"
    sub = root / "剧集"
    sub.mkdir(parents=True)
    local_dir = tmp_path / "localdir"
    local_dir.mkdir()
    m_sub = _mount(db, "m-sub", "local", str(root))
    m_root = _mount(db, "m-root", "local", str(root))
    lib = _lib(db, "lib-compare",
               paths=f"{local_dir},mount://{m_sub.id}/剧集",
               mount_ids=str(m_root.id))
    ms.backfill_library_sources(db, lib)
    result = ms.compare_library_sources(db, lib)
    assert result["only_new"] == set()
    assert result["only_old"] == set()
    new_sources, new_failed = ms.read_library_sources(db, lib)
    old_sources, old_failed = mnt.library_sources(lib, db)
    assert _source_keys(new_sources) == _source_keys(old_sources)
    assert new_failed == old_failed


# ---------- 2. 能力门控 ----------

def test_capability_parity_four_providers(db, tmp_path):
    """四家 provider 声明与回填派生口径逐字一致；drive 全 True，
    其余 can_refresh_credentials=False（声明是保证不是尽力）。"""
    expected = {
        "local": {"can_list": True, "can_random_read": True, "can_read_sidecar": True,
                  "can_generate_play_target": True, "can_refresh_credentials": False},
        "115": {"can_list": True, "can_random_read": True, "can_read_sidecar": True,
                "can_generate_play_target": True, "can_refresh_credentials": False},
        "drive": {"can_list": True, "can_random_read": True, "can_read_sidecar": True,
                  "can_generate_play_target": True, "can_refresh_credentials": True},
        "rclone": {"can_list": True, "can_random_read": True, "can_read_sidecar": True,
                   "can_generate_play_target": True, "can_refresh_credentials": False},
    }
    assert ms.CAPABILITY_BY_MOUNT_TYPE == expected
    providers = [
        mnt.LocalMount(SimpleNamespace(path=str(tmp_path), config={})),
        mnt.Pan115Mount(SimpleNamespace(path="115:/", config={})),
        DriveMount(SimpleNamespace(path="drive:shared", config={})),
        RcloneMount(SimpleNamespace(path="rclone:MP:", config={"mode": "rc"})),
    ]
    for provider, mount_type in zip(providers, ("local", "115", "drive", "rclone")):
        assert provider.capabilities().as_dict() == expected[mount_type]


def test_scan_gate_skips_can_list_false(db, tmp_path, monkeypatch):
    """can_list=False 的源不进扫描，记 failed（门控不是静默跳过）。"""
    mount = _mount(db, "m-nolist", "local", str(tmp_path))
    lib = _lib(db, "lib-nolist", paths="", mount_ids="")
    _row(db, lib, mount.id, "/")
    monkeypatch.setattr(
        mnt.LocalMount, "capabilities",
        lambda self: mnt.ProviderCapabilities(
            can_list=False, can_random_read=True, can_read_sidecar=True,
            can_generate_play_target=True, can_refresh_credentials=False))
    sources, failed = ms.read_library_sources(db, lib)
    assert sources == []
    assert len(failed) == 1
    assert "can_list=False" in failed[0]["reason"]


def test_variant_gate_excludes_mount_relay():
    """can_generate_play_target=False 的源不进变体清单；None 保持旧行为。"""
    item = SimpleNamespace(drive_file_id="", file_path="mount://3/x.mkv")
    assert pv.list_variants(None, item, mount_can_generate_play_target=False) == []
    assert [v.name for v in pv.list_variants(None, item, mount_can_generate_play_target=True)] == ["mount_relay"]
    assert [v.name for v in pv.list_variants(None, item)] == ["mount_relay"]


class _Refreshable(mnt.MountProvider):
    def capabilities(self):
        return mnt.ProviderCapabilities(can_refresh_credentials=True)

    def refresh_credentials(self):
        return True


class _Static(mnt.MountProvider):
    def capabilities(self):
        return mnt.ProviderCapabilities(can_refresh_credentials=False)

    def refresh_credentials(self):
        raise AssertionError("静态凭据不许走到刷新")


def test_refresh_gate_blocks_static_and_passes_refreshable():
    """can_refresh_credentials=False 的源不走刷新重试（刷新口不被调到）。"""
    assert _Static(SimpleNamespace(config={})).refresh_credentials_if_supported() is False
    assert _Refreshable(SimpleNamespace(config={})).refresh_credentials_if_supported() is True


def test_refresh_gate_tolerates_broken_capabilities():
    """声明都读不到的提供者按无刷新能力处理，不炸调用方。"""

    class _Broken(mnt.MountProvider):
        def capabilities(self):
            raise RuntimeError("boom")

        def refresh_credentials(self):
            raise AssertionError("不许走到刷新")

    assert _Broken(SimpleNamespace(config={})).refresh_credentials_if_supported() is False


# ---------- 3. 旧 resolve() 已删 ----------

def test_resolve_deleted_and_chain_port_works(tmp_path):
    """MountProvider 已无 resolve；本机换链走 resolve_play_target。"""
    assert not hasattr(mnt.MountProvider, "resolve")
    assert hasattr(mnt.MountProvider, "resolve_play_target")
    real = tmp_path / "a.mkv"
    real.write_bytes(b"x")
    provider = mnt.LocalMount(SimpleNamespace(path=str(tmp_path), config={}))
    target = provider.resolve_play_target("/a.mkv")
    assert target.value == str(real)


# ---------- 4. 保存写新表 + 反向同步 ----------

def test_write_library_sources_and_reverse_sync(db, tmp_path):
    """保存只写新表；旧列反向同步后旧解析复原同一来源集合。"""
    root = tmp_path / "root"
    sub = root / "剧集"
    sub.mkdir(parents=True)
    m_sub = _mount(db, "m-w-sub", "local", str(root))
    m_root = _mount(db, "m-w-root", "local", str(root))
    lib = _lib(db, "lib-write", paths="", mount_ids="")
    out = ms.write_library_sources(db, lib, [str(tmp_path), f"mount://{m_sub.id}/剧集"],
                                   [m_root.id])
    assert out["rows"] == 3
    rows = {(r.mount_id, r.sub_path) for r in
            db.query(em.LibrarySource).filter(em.LibrarySource.library_id == lib.id).all()}
    assert rows == {(None, str(tmp_path)), (m_sub.id, "/剧集"), (m_root.id, "/")}
    assert lib.paths == f"{tmp_path},mount://{m_sub.id}/剧集"
    assert lib.mount_ids == str(m_root.id)
    # 旧解析在反向同步后的列上复原同一来源集合
    old_sources, _ = mnt.library_sources(lib, db)
    new_sources, _ = ms.read_library_sources(db, lib)
    assert _source_keys(old_sources) == _source_keys(new_sources)


def test_write_collapses_dangerous_combo_subpath_wins(db, tmp_path, caplog):
    """危险组合（子目录与整挂载并存）写新表时子目录胜出，旧列同步后不再并存。"""
    root = tmp_path / "root"
    (root / "剧集").mkdir(parents=True)
    m = _mount(db, "m-danger", "local", str(root))
    m_other = _mount(db, "m-other", "local", str(root))
    lib = _lib(db, "lib-danger-write", paths="", mount_ids="")
    with caplog.at_level(logging.WARNING, logger="backend.emby_server.mount_sources"):
        ms.write_library_sources(db, lib, [f"mount://{m.id}/剧集"], [m.id, m_other.id])
    rows = {(r.mount_id, r.sub_path) for r in
            db.query(em.LibrarySource).filter(em.LibrarySource.library_id == lib.id).all()}
    assert rows == {(m.id, "/剧集"), (m_other.id, "/")}
    assert lib.mount_ids == str(m_other.id)
    assert "同挂载并存" in caplog.text


def test_write_replaces_previous_rows(db):
    """重写覆盖旧行，不残留。"""
    lib = _lib(db, "lib-rewrite", paths="", mount_ids="")
    ms.write_library_sources(db, lib, ["/a"], [])
    ms.write_library_sources(db, lib, ["/b"], [])
    rows = {(r.mount_id, r.sub_path) for r in
            db.query(em.LibrarySource).filter(em.LibrarySource.library_id == lib.id).all()}
    assert rows == {(None, "/b")}
    assert lib.paths == "/b"


# ---------- 5. 追新 / 监听 / 探针读新表 ----------

def test_chase_mount_sources_include_root_rows(db):
    """追新按新表行算：mount_ids 根行也覆盖（旧实现只读 paths 条目）。"""
    from backend.emby_server import change_watcher as cw

    lib = _lib(db, "lib-chase", paths="", mount_ids="")
    _row(db, lib, 3, "/MoviePilot/电影")
    _row(db, lib, 4, "/")
    assert cw._library_mount_sources(lib, db) == [(3, "/MoviePilot/电影"), (4, "/")]


def test_chase_local_paths_from_rows(db, tmp_path):
    """追新本机路径按新表行算：本机行 + local 挂载根 + 子目录。"""
    from backend.emby_server import change_watcher as cw

    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    local_dir = tmp_path / "localdir"
    local_dir.mkdir()
    m_local = _mount(db, "m-chase-local", "local", str(root))
    lib = _lib(db, "lib-chase-local", paths="", mount_ids="")
    _row(db, lib, None, str(local_dir))
    _row(db, lib, m_local.id, "/sub")
    assert cw._library_local_paths(lib, db) == [str(local_dir), str(sub)]


def test_fs_watch_local_paths_from_rows(db, tmp_path):
    """监听按新表本机行算，挂载行不监听（与旧口径一致）。"""
    from backend.emby_server import fs_watcher as fw

    local_dir = tmp_path / "localdir"
    local_dir.mkdir()
    lib = _lib(db, "lib-watch", paths="", mount_ids="")
    _row(db, lib, None, str(local_dir))
    _row(db, lib, 9, "/MoviePilot")
    assert fw.local_paths_for_library(lib, db) == (str(local_dir),)


def test_mount_library_map_includes_path_derived_mounts(db):
    """探针映射按新表行算：paths 派生的挂载也算（旧实现只读 mount_ids）。"""
    from backend.emby_server import mount_health

    lib = _lib(db, "lib-health", paths="", mount_ids="")
    _row(db, lib, 5, "/剧集")
    _row(db, lib, 6, "/")
    usage = mount_health._mount_library_map(db)
    assert usage[5] == [lib.id]
    assert usage[6] == [lib.id]


def test_read_source_rows_fallback_without_db():
    """无 db 会话时回落旧配置派生（与旧解析同一口径）。"""
    lib = SimpleNamespace(paths="mount://3/国产剧,/data/x", mount_ids="4")
    rows = ms.read_source_rows(None, lib)
    assert rows == [{"mount_id": 3, "sub_path": "/国产剧", "is_enabled": True},
                    {"mount_id": 4, "sub_path": "/", "is_enabled": True},
                    {"mount_id": None, "sub_path": "/data/x", "is_enabled": True}]

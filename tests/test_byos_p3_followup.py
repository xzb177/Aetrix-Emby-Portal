"""BYOS P3 后续两项（同一 PR）：

① 绑定线定向清理：绑定源里文件没了时只清本绑定、本库的条目，
   带主线同款 fail-closed 门（清单不完整跳过、空清单拒绝批量删、
   缺失宽限多轮才下架），绝不触发整库清理；
② NFO / 外挂字幕绑定线接线：复用扫描体主线口（_find_nfos /
   _side_info 的绑定线分支），不另写语义。

网络与上游全部用桩替身，不碰真实 Drive / 115。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import byos_scan
from backend.emby_server import drive_oauth
from backend.emby_server import mounts as mount_lib
from backend.emby_server import scanner
from backend.emby_server import soft_delete as _soft_delete
from backend.emby_server import models as em
from backend.emby_server.upstream_sources import seal_secret


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


def _make_binding(db, user_id, provider, label, sealed, status="active"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=sealed, key_id="fernet-v1", status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=cred.id, status=status)
    db.add(binding)
    db.flush()
    return binding


def _make_library(db, name, collection_type="movies"):
    lib = em.Library(guid=f"lib-{name}", name=name,
                     collection_type=collection_type, paths="")
    db.add(lib)
    db.flush()
    return lib


def _mf(rel, name, file_id=""):
    return mount_lib.MountFile(rel=rel, name=name, size=123, file_id=file_id)


def _entry(name, rel):
    return mount_lib.MountEntry(name=name, rel=rel, is_dir=False)


class _StubProvider:
    """列举桩：walk_media 产出给定文件；list_dir / read_text 按表回放。"""

    def __init__(self, files, dir_entries=None, texts=None):
        self.files = list(files)
        self.dir_entries = dir_entries or {}
        self.texts = texts or {}
        self.local_root = None

    def walk_media(self, root="/"):
        yield from self.files

    def list_dir(self, rel="/"):
        return list(self.dir_entries.get(rel, []))

    def read_text(self, rel):
        if rel in self.texts:
            return self.texts[rel]
        raise mount_lib.MountError(f"桩里没有旁挂：{rel}")

    def resolve_final(self, rel):
        return mount_lib.PlayTarget(kind="url", value="https://example.invalid/x.mp4",
                                    headers={})


def _enable(monkeypatch):
    monkeypatch.setenv("BYOS_SCAN_ENABLED", "1")


def _item_by_path(db, path):
    db.expire_all()
    return db.query(em.MediaItem).filter(
        em.MediaItem.file_path == path).first()


def _visible_item(db, path):
    item = _item_by_path(db, path)
    assert item is not None and item.deleted_at is None
    return item


# ---------------------------------------------------------------- ② NFO / 字幕

def test_binding_movie_nfo_and_subtitle_wired(db, monkeypatch):
    _enable(monkeypatch)
    # 分层扫描默认把远程文件的 side/NFO 递延给 enrich（挂载线同款调度）；
    # 本用例测扫描期内联口，故关分层；递延路径另由 enrich 重建用例覆盖。
    monkeypatch.setattr(scanner, "SCAN_LAYERED", False)
    # 测的不是探测：桩直链 ffprobe 要等网络超时（约 90 秒），直接短路。
    monkeypatch.setattr(scanner, "probe_metadata", lambda *a, **k: None)
    lib = _make_library(db, "nfo")
    binding = _make_binding(db, 7, "drive", "u7",
                            seal_secret(drive_oauth.encode_drive_token(
                                "tok-7", "ref-7", 9999999999)))
    nfo = ("<movie><title>ZzqTest 2024</title>"
           "<plot>NFO-PLOT-UNIQUE-12345</plot></movie>")
    stub = _StubProvider(
        [_mf("/ZzqTest 2024.mkv", "ZzqTest 2024.mkv")],
        dir_entries={"/": [
            _entry("ZzqTest 2024.mkv", "/ZzqTest 2024.mkv"),
            _entry("ZzqTest 2024.nfo", "/ZzqTest 2024.nfo"),
            _entry("ZzqTest 2024.srt", "/ZzqTest 2024.srt"),
        ]},
        texts={"/ZzqTest 2024.nfo": nfo})
    out = byos_scan.scan_binding(db, 7, binding.id, lib.id, provider=stub)
    assert out["stats"].get("nfo", 0) >= 1
    item = _visible_item(db, f"byos://{binding.id}/ZzqTest 2024.mkv")
    assert item.overview == "NFO-PLOT-UNIQUE-12345"
    subs = db.query(em.MediaStream).filter(
        em.MediaStream.item_id == item.id,
        em.MediaStream.is_external.is_(True)).all()
    assert [s.external_path for s in subs] == [
        f"byos://{binding.id}/ZzqTest 2024.srt"]
    assert "mount://None" not in (subs[0].external_path or "")


def test_binding_series_tvshow_nfo_wired(db, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(scanner, "SCAN_LAYERED", False)
    monkeypatch.setattr(scanner, "probe_metadata", lambda *a, **k: None)
    lib = _make_library(db, "shows", collection_type="tvshows")
    binding = _make_binding(db, 7, "drive", "u7",
                            seal_secret(drive_oauth.encode_drive_token(
                                "tok-7", "ref-7", 9999999999)))
    tvshow = ("<tvshow><title>ZzqShow</title>"
              "<plot>TVSHOW-PLOT-UNIQUE-67890</plot></tvshow>")
    stub = _StubProvider(
        [_mf("/Show/ZzqShow S01E01.mkv", "ZzqShow S01E01.mkv")],
        dir_entries={"/Show": [
            _entry("ZzqShow S01E01.mkv", "/Show/ZzqShow S01E01.mkv"),
            _entry("tvshow.nfo", "/Show/tvshow.nfo"),
        ]},
        texts={"/Show/tvshow.nfo": tvshow})
    byos_scan.scan_binding(db, 7, binding.id, lib.id, provider=stub)
    ep = _visible_item(db, f"byos://{binding.id}/Show/ZzqShow S01E01.mkv")
    db.expire_all()
    series = db.query(em.MediaItem).filter(
        em.MediaItem.id == ep.series_id).first()
    assert series is not None and series.overview == "TVSHOW-PLOT-UNIQUE-67890"


def test_enrich_scanfile_from_item_byos_rebuilds_provider(db, monkeypatch):
    """递延路径：enrich 从 byos 条目重建出带提供者的 ScanFile（旧口径会
    把 byos:// 当成本地目录，side/NFO 永远空）。"""
    import backend.database as dbmod
    from backend.emby_server import enrich_worker as ew

    _enable(monkeypatch)
    lib = _make_library(db, "enrich-rebuild")
    binding = _make_binding(db, 7, "drive", "u7",
                            seal_secret(drive_oauth.encode_drive_token(
                                "tok-7", "ref-7", 9999999999)))
    byos_scan.scan_binding(
        db, 7, binding.id, lib.id,
        provider=_StubProvider([_mf("/FilmA.mkv", "FilmA.mkv")]))
    item = _visible_item(db, f"byos://{binding.id}/FilmA.mkv")

    class _Sess:
        def __init__(self, s):
            self._s = s

        def query(self, *a, **k):
            return self._s.query(*a, **k)

        def commit(self):
            return self._s.commit()

        def flush(self):
            return self._s.flush()

        def rollback(self):
            return self._s.rollback()

        def close(self):
            pass

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _Sess(db))
    sf = ew._scanfile_from_item(item)
    assert sf is not None
    assert sf.provider is not None
    assert sf.mount_id is None
    assert sf.local_dir is None
    assert sf.stored_path == f"byos://{binding.id}/FilmA.mkv"
    assert sf.dir_rel == "/"
    assert scanner._byos_binding_id_of(sf) == binding.id
    # 绑定删了也不炸：回落为无提供者的远程文件，TMDB 照常可走。
    db.delete(binding)
    db.commit()
    sf2 = ew._scanfile_from_item(item)
    assert sf2 is not None and sf2.provider is None and sf2.local_dir is None


def test_binding_path_prefix_separator():
    # 前缀必须带尾斜杠：绑定 1 的前缀绝不能命中绑定 12 的路径。
    assert not "byos://12/Film.mkv".startswith(byos_scan.binding_path(1, "/"))
    assert "byos://1/Film.mkv".startswith(byos_scan.binding_path(1, "/"))


# ---------------------------------------------------------------- ① 定向清理

def test_prune_incomplete_listing_skips_without_changes(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "prune-inc")
    binding = _make_binding(db, 7, "drive", "u7",
                            seal_secret(drive_oauth.encode_drive_token(
                                "tok-7", "ref-7", 9999999999)))
    byos_scan.scan_binding(
        db, 7, binding.id, lib.id,
        provider=_StubProvider([_mf("/FilmA.mkv", "FilmA.mkv")]))
    out = byos_scan.prune_binding_items(
        db, lib.id, binding.id, set(), listing_complete=False)
    assert out["skipped"] == "listing_incomplete"
    assert out["removed"] == 0
    _visible_item(db, f"byos://{binding.id}/FilmA.mkv")


def test_prune_empty_listing_refused(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "prune-empty")
    binding = _make_binding(db, 7, "drive", "u7",
                            seal_secret(drive_oauth.encode_drive_token(
                                "tok-7", "ref-7", 9999999999)))
    byos_scan.scan_binding(
        db, 7, binding.id, lib.id,
        provider=_StubProvider([_mf("/FilmA.mkv", "FilmA.mkv"),
                                _mf("/FilmB.mkv", "FilmB.mkv")]))
    out = byos_scan.scan_binding(
        db, 7, binding.id, lib.id, provider=_StubProvider([]))
    assert out["prune"]["skipped"] == "empty_listing_refused"
    assert out["prune"]["removed"] == 0
    _visible_item(db, f"byos://{binding.id}/FilmA.mkv")
    item_b = _visible_item(db, f"byos://{binding.id}/FilmB.mkv")
    assert (item_b.missing_count or 0) == 0


def test_prune_grace_then_removes_only_own_line(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "prune-grace")
    lib2 = _make_library(db, "prune-grace-2")
    b7 = _make_binding(db, 7, "drive", "u7",
                       seal_secret(drive_oauth.encode_drive_token(
                           "tok-7", "ref-7", 9999999999)))
    b8 = _make_binding(db, 8, "drive", "u8",
                       seal_secret(drive_oauth.encode_drive_token(
                           "tok-8", "ref-8", 9999999999)))
    full7 = _StubProvider([_mf("/FilmA.mkv", "FilmA.mkv"),
                           _mf("/FilmB.mkv", "FilmB.mkv")])
    out0 = byos_scan.scan_binding(db, 7, b7.id, lib.id, provider=full7)
    assert out0["prune"]["skipped"] is None
    assert out0["prune"]["checked"] == 2
    # 同一文件被另一绑定扫进同一库 + 另一绑定另一文件扫进另一库：两处都不许动。
    # （注：guid 由入库路径派生且全局唯一，同一绑定同一文件本就进不了
    # 第二库，故另一库对照必须用不同文件，否则前提不成立。）
    byos_scan.scan_binding(
        db, 8, b8.id, lib.id,
        provider=_StubProvider([_mf("/FilmB.mkv", "FilmB.mkv")]))
    byos_scan.scan_binding(
        db, 8, b8.id, lib2.id,
        provider=_StubProvider([_mf("/FilmC.mkv", "FilmC.mkv")]))
    only_a = _StubProvider([_mf("/FilmA.mkv", "FilmA.mkv")])
    grace = scanner.MISSING_GRACE_SCANS
    for round_no in range(1, grace):
        out = byos_scan.scan_binding(db, 7, b7.id, lib.id, provider=only_a)
        assert out["prune"]["skipped"] is None
        assert out["prune"]["removed"] == 0
        gone = _visible_item(db, f"byos://{b7.id}/FilmB.mkv")
        assert (gone.missing_count or 0) == round_no
        _visible_item(db, f"byos://{b7.id}/FilmA.mkv")
        other = _visible_item(db, f"byos://{b8.id}/FilmB.mkv")
        assert (other.missing_count or 0) == 0
        db.expire_all()
        lib2_row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib2.id,
            em.MediaItem.file_path == f"byos://{b8.id}/FilmC.mkv").first()
        assert lib2_row is not None and lib2_row.deleted_at is None
        assert (lib2_row.missing_count or 0) == 0
    final = byos_scan.scan_binding(db, 7, b7.id, lib.id, provider=only_a)
    assert final["prune"]["removed"] == 1
    with _soft_delete.include_deleted():
        db.expire_all()
        gone = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id,
            em.MediaItem.file_path == f"byos://{b7.id}/FilmB.mkv").first()
    assert gone is not None and gone.deleted_at is not None
    # 另一绑定同名条目与另一库另一绑定条目仍可见。
    _visible_item(db, f"byos://{b8.id}/FilmB.mkv")
    db.expire_all()
    lib2_row = db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib2.id,
        em.MediaItem.file_path == f"byos://{b8.id}/FilmC.mkv").first()
    assert lib2_row is not None and lib2_row.deleted_at is None


def test_prune_subtree_scan_skips(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "prune-sub")
    binding = _make_binding(db, 7, "drive", "u7",
                            seal_secret(drive_oauth.encode_drive_token(
                                "tok-7", "ref-7", 9999999999)))
    byos_scan.scan_binding(
        db, 7, binding.id, lib.id,
        provider=_StubProvider([_mf("/FilmA.mkv", "FilmA.mkv"),
                                _mf("/Show/Show S01E01.mkv",
                                    "Show S01E01.mkv")]))
    out = byos_scan.scan_binding(
        db, 7, binding.id, lib.id, root="/Show",
        provider=_StubProvider([_mf("/Show/Show S01E01.mkv",
                                    "Show S01E01.mkv")]))
    assert out["prune"]["skipped"] == "subtree_root"
    item = _visible_item(db, f"byos://{binding.id}/FilmA.mkv")
    assert (item.missing_count or 0) == 0


def test_prune_seen_file_resets_missing_count(db, monkeypatch):
    _enable(monkeypatch)
    lib = _make_library(db, "prune-reset")
    binding = _make_binding(db, 7, "drive", "u7",
                            seal_secret(drive_oauth.encode_drive_token(
                                "tok-7", "ref-7", 9999999999)))
    byos_scan.scan_binding(
        db, 7, binding.id, lib.id,
        provider=_StubProvider([_mf("/FilmA.mkv", "FilmA.mkv"),
                                _mf("/FilmB.mkv", "FilmB.mkv")]))
    # FilmB 缺一轮（grace 1），再回来一轮 → 清零，不下架。
    byos_scan.scan_binding(
        db, 7, binding.id, lib.id,
        provider=_StubProvider([_mf("/FilmA.mkv", "FilmA.mkv")]))
    assert _visible_item(
        db, f"byos://{binding.id}/FilmB.mkv").missing_count == 1
    out = byos_scan.scan_binding(
        db, 7, binding.id, lib.id,
        provider=_StubProvider([_mf("/FilmA.mkv", "FilmA.mkv"),
                                _mf("/FilmB.mkv", "FilmB.mkv")]))
    assert out["prune"]["resets"] == 1
    assert _visible_item(
        db, f"byos://{binding.id}/FilmB.mkv").missing_count == 0

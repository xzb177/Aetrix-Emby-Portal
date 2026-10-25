"""合集文件一文件顶两集（缺集普查 A2 的真正落地，2026-10-19 用户亲令插队）。

``S06E27-E28`` 这类文件名覆盖两集：#1092 修了解析（episode_end 已产出）
但下游零消费，入库只建一行（只有第一集）。本文件钉住：

1. 解析：区间形态产出 (episode, episode_end)；
2. 第二行 guid 与主行同族不冲突；
3. ``_maybe_make_multi_episode_pending``：第二集 pending 追加 + 幂等；
4. 改名识别：file_id 版按集号精确配对两行；指纹版用行自身集号参与指纹；
5. 清理安全：两行同文件，文件在则都不删、文件删则两行同下架；
6. 探测回填：同一文件只探测一次，结果共享给同文件其他行。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import mounts as _mounts  # noqa: F401 -- 必须先于 scanner 导入，避免循环导入
from backend.emby_server import scanner
from backend.emby_server import media_probe
from backend.integrations import store


@pytest.fixture(autouse=True)
def _grace_single_pass(monkeypatch):
    # 同 test_soft_delete：隔离缺失宽限（批一 #6）变量，只测清理主体
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)


OLD_PATH = "/media/tv/SpongeBob/SpongeBob S06E27-E28.mp4"
# 改名目标：只换容器后缀，不带版本标签（2160p/H265 等会改变版本签名，
# 指纹改名按设计不认——那是多版本不是改名）
NEW_PATH = "/media/tv/SpongeBob/SpongeBob S06E27-E28.mkv"
LIB_TYPE = "tvshows"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _guid():
    return uuid.uuid4().hex


def _make_lib(db):
    lib = em.Library(guid=_guid(), name="MultiEp Test", collection_type="tvshows",
                     paths="/media/tv", is_enabled=True)
    db.add(lib)
    db.flush()
    return lib


def _make_series(db, lib):
    s = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="series",
                     name="SpongeBob", date_added=datetime.now(),
                     probe_status="done")
    db.add(s)
    db.flush()
    return s


def _make_season(db, lib, series, no=6):
    s = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="season",
                     name="第 %d 季" % no, parent_id=series.id, series_id=series.id,
                     season_number=no, date_added=datetime.now(),
                     probe_status="done")
    db.add(s)
    db.flush()
    return s


def _make_episode(db, lib, series, season, ep_no, path=OLD_PATH, guid=None,
                  fid="fid-multi-1", size=1234567, deleted_at=None,
                  probe_status="done"):
    if guid is None:
        guid = (scanner.item_guid(path) if ep_no == 27
                else scanner._multi_episode_guid(path, ep_no))
    row = em.MediaItem(
        guid=guid,
        library_id=lib.id, item_type="episode",
        name="海绵宝宝 S06E%02d" % ep_no,
        series_id=series.id, parent_id=season.id,
        season_number=6, episode_number=ep_no,
        file_path=path, drive_file_id=fid, size=size,
        date_added=datetime.now(), probe_status=probe_status,
        deleted_at=deleted_at)
    db.add(row)
    db.flush()
    return row


def _cleanup(db, lib):
    ids = [r[0] for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.library_id == lib.id).all()]
    if ids:
        db.query(em.MediaStream).filter(
            em.MediaStream.item_id.in_(ids)).delete(synchronize_session=False)
    db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib.id).delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


# ---------- 1. 解析 ----------

def test_parse_range_gives_episode_end():
    p = scanner.parse_media_filename(OLD_PATH, LIB_TYPE)
    assert p["episode"] == 27
    assert p["episode_end"] == 28
    assert p["season"] == 6


def test_parse_range_concatenated():
    p = scanner.parse_media_filename(
        "/media/tv/Show/Show S01E27E28.mp4", LIB_TYPE)
    assert (p["episode"], p["episode_end"]) == (27, 28)


def test_parse_single_has_no_episode_end():
    p = scanner.parse_media_filename("/media/tv/Show/Show S01E05.mp4", LIB_TYPE)
    assert p["episode"] == 5
    assert p["episode_end"] is None


# ---------- 2. guid ----------

def test_multi_episode_guid_distinct_and_stable():
    g1 = scanner.item_guid(OLD_PATH)
    g2 = scanner._multi_episode_guid(OLD_PATH, 27)
    g3 = scanner._multi_episode_guid(OLD_PATH, 28)
    assert len({g1, g2, g3}) == 3
    assert all(len(g) == 32 for g in (g1, g2, g3))
    assert scanner._multi_episode_guid(OLD_PATH, 28) == g3  # 稳定可复算


# ---------- 3. pending 追加 ----------

def _fake_ctx():
    return SimpleNamespace(seen_guids=set(), stats={})


def _fake_pending(parsed, series_guid="sg", season_guid="seag"):
    return SimpleNamespace(
        scan_file=SimpleNamespace(stored_path=OLD_PATH),
        guid=scanner.item_guid(OLD_PATH),
        parsed=parsed, item_type="episode",
        series_guid=series_guid, series_name="SpongeBob",
        season_guid=season_guid, item=None)


def test_maybe_make_appends_second_pending():
    ctx = _fake_ctx()
    parsed = scanner.parse_media_filename(OLD_PATH, LIB_TYPE)
    p = _fake_pending(parsed)
    prepared, guids = [p], [p.guid]
    scanner._maybe_make_multi_episode_pending(
        ctx, prepared, guids, p.scan_file, p, parsed, "episode")
    assert len(prepared) == 2
    p2 = prepared[1]
    assert p2.multi_episode is True
    assert p2.master_pending is p
    assert p2.parsed["episode"] == 28
    assert p2.guid == scanner._multi_episode_guid(OLD_PATH, 28)
    assert p2.series_guid == "sg" and p2.season_guid == "seag"
    assert p2.guid in ctx.seen_guids and p2.guid in guids


def test_maybe_make_idempotent_on_rescan():
    ctx = _fake_ctx()
    parsed = scanner.parse_media_filename(OLD_PATH, LIB_TYPE)
    p = _fake_pending(parsed)
    prepared, guids = [p], [p.guid]
    scanner._maybe_make_multi_episode_pending(
        ctx, prepared, guids, p.scan_file, p, parsed, "episode")
    # 同一文件再次产出（遍历器重复）：不再追加
    scanner._maybe_make_multi_episode_pending(
        ctx, prepared, guids, p.scan_file, p, parsed, "episode")
    assert len(prepared) == 2
    assert ctx.stats.get("duplicate_files") == 1


def test_maybe_make_noop_without_episode_end():
    ctx = _fake_ctx()
    parsed = scanner.parse_media_filename("/media/tv/Show/Show S01E05.mp4", LIB_TYPE)
    p = _fake_pending(parsed)
    prepared, guids = [p], [p.guid]
    scanner._maybe_make_multi_episode_pending(
        ctx, prepared, guids, p.scan_file, p, parsed, "episode")
    assert len(prepared) == 1


# ---------- 4. 改名识别 ----------

def _fp_pending(path, series_name="SpongeBob"):
    parsed = scanner.parse_media_filename(path, LIB_TYPE)
    return SimpleNamespace(
        scan_file=SimpleNamespace(stored_path=path, size=1234567, file_id=""),
        guid=scanner.item_guid(path),
        parsed=parsed, item_type="episode",
        series_guid="sg", series_name=series_name,
        season_guid="seag", item=None, renamed=False)


def test_fingerprint_rename_matches_both_rows_by_episode(db):
    lib = _make_lib(db)
    series = _make_series(db, lib)
    season = _make_season(db, lib, series)
    r27 = _make_episode(db, lib, series, season, 27, path=OLD_PATH)
    r28 = _make_episode(db, lib, series, season, 28, path=OLD_PATH)
    db.commit()
    try:
        p1, p2 = _fp_pending(NEW_PATH), _fp_pending(NEW_PATH)
        p2.parsed = dict(p2.parsed); p2.parsed["episode"] = 28
        p2.guid = scanner._multi_episode_guid(NEW_PATH, 28)
        ctx = SimpleNamespace(seen_guids=set(), lib_id=lib.id, stats={})
        scanner._match_renames_by_fingerprint(db, [p1, p2], {}, ctx)
        # 两行各认各的，不许张冠李戴、不许丢
        assert p1.item is not None and p1.item.id == r27.id, "E27 行应被认出"
        assert p2.item is not None and p2.item.id == r28.id, "E28 行应被认出"
        assert p1.renamed and p2.renamed
    finally:
        _cleanup(db, lib)


def test_fingerprint_rename_single_episode_unchanged(db):
    lib = _make_lib(db)
    series = _make_series(db, lib)
    season = _make_season(db, lib, series)
    old = "/media/tv/Show/Show S01E05.mp4"
    new = "/media/tv/Show/Show S01E05.mkv"  # 不带版本标签，见 NEW_PATH 注释
    row = em.MediaItem(
        guid=scanner.item_guid(old), library_id=lib.id, item_type="episode",
        name="Show S01E05", series_id=series.id, parent_id=season.id,
        season_number=1, episode_number=5, file_path=old,
        drive_file_id="", size=1234567, date_added=datetime.now(),
        probe_status="done")
    db.add(row)
    db.flush()
    db.commit()
    try:
        p = _fp_pending(new, series_name="Show")
        ctx = SimpleNamespace(seen_guids=set(), lib_id=lib.id, stats={})
        scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
        assert p.item is not None and p.item.id == row.id
    finally:
        _cleanup(db, lib)


def _fid_pending(path, ep, fid="fid-multi-1"):
    parsed = scanner.parse_media_filename(path, LIB_TYPE)
    parsed = dict(parsed)
    parsed["episode"] = ep
    return SimpleNamespace(
        scan_file=SimpleNamespace(stored_path=path, size=1234567, file_id=fid),
        guid=(scanner.item_guid(path) if ep == 27
              else scanner._multi_episode_guid(path, ep)),
        parsed=parsed, item_type="episode",
        series_guid="sg", series_name="SpongeBob",
        season_guid="seag", item=None, renamed=False)


def test_file_id_rename_matches_each_row_by_episode(db):
    lib = _make_lib(db)
    series = _make_series(db, lib)
    season = _make_season(db, lib, series)
    r27 = _make_episode(db, lib, series, season, 27, path=OLD_PATH)
    r28 = _make_episode(db, lib, series, season, 28, path=OLD_PATH)
    db.commit()
    try:
        p27, p28 = _fid_pending(NEW_PATH, 27), _fid_pending(NEW_PATH, 28)
        ctx = SimpleNamespace(seen_guids=set(), lib_id=lib.id, stats={})
        scanner._match_renames_by_file_id(db, [p27, p28], {}, ctx)
        assert p27.item is not None and p27.item.id == r27.id
        assert p28.item is not None and p28.item.id == r28.id
        assert p27.renamed and p28.renamed
        # 两行都认领后 file_path 都应更新到新路径（写库循环做，这里只验配对）
    finally:
        _cleanup(db, lib)


# ---------- 5. 清理安全 ----------

def test_cleanup_keeps_both_rows_when_file_present(db, tmp_path):
    f = tmp_path / "SpongeBob S06E27-E28.mp4"
    f.write_bytes(b"x" * 64)
    path = str(f)
    lib = _make_lib(db)
    series = _make_series(db, lib)
    season = _make_season(db, lib, series)
    g1 = scanner.item_guid(path)
    g2 = scanner._multi_episode_guid(path, 28)
    _make_episode(db, lib, series, season, 27, path=path, guid=g1)
    _make_episode(db, lib, series, season, 28, path=path, guid=g2)
    db.commit()
    try:
        seen = {g1, g2}
        assert scanner._no_removals_possible(db, lib, seen) is True
        removed = scanner._remove_missing_items(db, lib, seen)
        assert removed == 0
        visible = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id,
            em.MediaItem.item_type == "episode",
            em.MediaItem.deleted_at.is_(None)).count()
        assert visible == 2
    finally:
        _cleanup(db, lib)


def test_cleanup_removes_both_rows_when_file_gone(db, tmp_path):
    f = tmp_path / "SpongeBob S06E27-E28.mp4"
    f.write_bytes(b"x" * 64)
    path = str(f)
    lib = _make_lib(db)
    series = _make_series(db, lib)
    season = _make_season(db, lib, series)
    g1 = scanner.item_guid(path)
    g2 = scanner._multi_episode_guid(path, 28)
    _make_episode(db, lib, series, season, 27, path=path, guid=g1)
    _make_episode(db, lib, series, season, 28, path=path, guid=g2)
    db.commit()
    f.unlink()  # 文件删了
    try:
        # 本轮还看到别的库文件（预算不拦），但没见到这两个 guid
        removed = scanner._remove_missing_items(db, lib, {"some-other-guid"})
        assert removed == 2
        gone = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id,
            em.MediaItem.item_type == "episode",
            em.MediaItem.deleted_at.is_(None)).count()
        assert gone == 0
    finally:
        _cleanup(db, lib)


# ---------- 6. 探测回填 ----------

def test_write_back_backfills_sibling_rows(db):
    lib = _make_lib(db)
    series = _make_series(db, lib)
    season = _make_season(db, lib, series)
    r27 = _make_episode(db, lib, series, season, 27, probe_status="pending")
    r28 = _make_episode(db, lib, series, season, 28, probe_status="pending")
    # 把 probe_status 改成 pending（_make_episode 默认 done）
    r27.probe_status = "pending"
    r28.probe_status = "pending"
    db.commit()
    try:
        probe = {"video_codec": "h264", "audio_codec": "aac", "width": 1920,
                 "height": 1080, "bitrate": 8000000, "size": 1234567,
                 "duration_ticks": 1320000000,
                 "streams": [{"stream_type": "video", "codec": "h264"}]}
        media_probe.write_back(db, r27, probe)
        db.refresh(r28)
        assert r28.probe_status == "done"
        assert r28.width == 1920 and r28.video_codec == "h264"
        # 轨道行两行各自独立
        n27 = db.query(em.MediaStream).filter(
            em.MediaStream.item_id == r27.id).count()
        n28 = db.query(em.MediaStream).filter(
            em.MediaStream.item_id == r28.id).count()
        assert n27 == 1 and n28 == 1
    finally:
        _cleanup(db, lib)


# ---------- 7. IO 复用 ----------

def test_reuse_master_io_futures():
    master = SimpleNamespace(probe=object(), side=object(), nfo=object())
    p2 = SimpleNamespace(master_pending=master, probe=None, side=None, nfo=None)
    scanner._reuse_master_io_futures(p2)
    assert p2.probe is master.probe and p2.side is master.side and p2.nfo is master.nfo

    # 主行本轮没提交对应 IO 时，第二行保留自己的（不丢活）
    master2 = SimpleNamespace(probe=None, side=object(), nfo=None)
    own_side = object()
    p3 = SimpleNamespace(master_pending=master2, probe=None, side=own_side, nfo=None)
    scanner._reuse_master_io_futures(p3)
    assert p3.probe is None and p3.nfo is None and p3.side is own_side

    # 非多集行：无 master_pending，不动
    p4 = SimpleNamespace(master_pending=None, probe=None, side=None, nfo=None)
    scanner._reuse_master_io_futures(p4)
    assert p4.probe is None and p4.side is None and p4.nfo is None


# ---------- 8. 入库准备：合集文件产出两个 pending ----------

def _local_scan_file(path, size=1234567):
    return SimpleNamespace(
        stored_path=path, size=size, file_id="", mtime=1700000000,
        mtime_ns=1700000000000000000,
        local_dir=os.path.dirname(path), dir_rel=None, mount_id=None,
        kind="local")


def test_prepare_and_prefetch_creates_two_pendings(db):
    lib = _make_lib(db)
    db.commit()
    snap = scanner.LibrarySnapshot(
        library_id=lib.id, name=lib.name, collection_type="tvshows",
        paths=("/media/tv",), scrape_policy="tmdb_first")
    ctx = scanner._ScanContext(snap=snap, lib_id=lib.id, stats={})
    try:
        prepared = scanner._prepare_and_prefetch(
            db, [_local_scan_file(OLD_PATH)], ctx, pool=None)
        assert len(prepared) == 2, "合集文件必须产出两个 pending"
        p1, p2 = prepared
        # 主行：区间起点
        assert p1.parsed["episode"] == 27 and p1.parsed["season"] == 6
        assert p1.guid == scanner.item_guid(OLD_PATH)
        # 第二行：区间终点，同 file_path、同季、guid 独立
        assert p2.parsed["episode"] == 28 and p2.parsed["season"] == 6
        assert p2.scan_file.stored_path == OLD_PATH
        assert p2.guid == scanner._multi_episode_guid(OLD_PATH, 28)
        assert p2.guid != p1.guid
        assert p2.multi_episode is True and p2.master_pending is p1
        # 两行剧/季身份一致（写库时落到同一季行下）
        assert p2.series_guid == p1.series_guid
        assert p2.season_guid == p1.season_guid
        # 两个 guid 都进了 seen_guids（清理快速路径保得住两行）
        assert p1.guid in ctx.seen_guids and p2.guid in ctx.seen_guids
    finally:
        _cleanup(db, lib)


# ---------- 7. r60：迁移场景不许双认领 ----------

def test_migration_old_single_row_not_double_claimed(db):
    """r60：迁移场景——旧单行（曾被误解析为 S1E28，guid 与主行一致）重扫时，
    主行按 guid 认领旧行，第二行不得再认领同一行。

    旧顺序（改名识别先跑）下：file_id 匹配把旧行给了第二行，
    _attach_known_items 又把同一旧行给了主行——两个 pending 共用一行，
    _process_one 里后写的覆盖先写的，第二行永远建不出来，且静默无报错。
    新顺序：guid 挂行先行，改名识别带 claimed 集合，不得抢走已认领的行。
    """
    lib = _make_lib(db)
    series = _make_series(db, lib)
    season = _make_season(db, lib, series)
    old = _make_episode(db, lib, series, season, 28, path=OLD_PATH,
                        guid=scanner.item_guid(OLD_PATH))
    old.season_number = 1  # 误解析时代的形态：S1E28
    old.name = "海绵宝宝 S01E28"
    db.commit()
    try:
        p_master = _fid_pending(OLD_PATH, 27)
        p_second = _fid_pending(OLD_PATH, 28)
        prepared = [p_master, p_second]
        known = scanner._load_items(db, [p_master.guid, p_second.guid])
        ctx = SimpleNamespace(seen_guids=set(), lib_id=lib.id, stats={})
        # 生产顺序：guid 挂行 → 改名识别（带 claimed）
        claimed = scanner._attach_known_items(prepared, known)
        scanner._match_renames_by_file_id(db, prepared, known, ctx, claimed)
        # 主行认领旧行（guid 命中是最强身份）
        assert p_master.item is not None and p_master.item.id == old.id
        # 第二行不许复用旧行：item 为 None，走新行占位流程
        assert p_second.item is None
    finally:
        _cleanup(db, lib)

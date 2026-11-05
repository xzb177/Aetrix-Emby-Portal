"""Nova N3-2 补遗：扫描期 NFO 应用不再被「已有简介」短路。

用户 2026-11-05 亲令「要留干净」：N3-2 已把 enrich 的 nfo-only 分支
短路去掉，扫描期同类尾巴一并清掉，不许留到以后。Nova 优先级口径：
锁定 > NFO > 在线来源 > 文件名——scrape_policy 管的是 TMDB 重刮节奏，
不该管本地 NFO 应用；未锁定条目以盘上 NFO 为准。

本文件钉住 scanner.py 三处同类条件（处置结论逐处对应）：

1. 顶层条目（movie/series）刮削段 ``elif nfo_data and not item.overview``
   ——整段跳过型短路，本单主改：policy 不要求重刮、条目已有简介时，
   NFO 仍按优先级应用（标题/简介/评分都以 NFO 为准）。
2. series 级 ``if series_nfo and (not series.tmdb_id or not
   series.overview or series_tmdb_applied)``——复合门：剧集已被 TMDB
   完全识别（有 tmdb_id＋有简介）且本轮 TMDB 没写过时，整段 series
   NFO 被跳过，新放/改过的 tvshow.nfo 永远不生效。同为整段跳过型，
   一并改（判据见 PR 说明）。
3. season 级 ``if _pending.season_nfo_data and not season_item.overview``
   ——season 的 apply 只写简介与评分两项，门却只看简介：有简介时
   连评分补缺都被挡。同为整段跳过型，一并改。

锁定条目同场景零变化由 N3-2 的 apply_nfo 入口守卫保证，本文件
钉住它在扫描分支上同样成立（不许本单改动把守卫绕过去）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import mounts as _mounts  # noqa: F401 -- 必须先于 scanner 导入，避免循环导入
from backend.emby_server import scanner

init_db()

MOVIE_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>NFO 权威标题</title>
  <plot>NFO 权威简介</plot>
  <rating>8.7</rating>
</movie>
"""

TVSHOW_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<tvshow>
  <title>NFO 剧名</title>
  <plot>NFO 剧简介</plot>
  <rating>9.1</rating>
</tvshow>
"""

SEASON_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<season>
  <plot>NFO 季简介</plot>
  <rating>9.0</rating>
</season>
"""


def _guid():
    return uuid.uuid4().hex


def _cleanup(db, lib):
    item_ids = [r[0] for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.library_id == lib.id).all()]
    if item_ids:
        db.query(em.MediaStream).filter(
            em.MediaStream.item_id.in_(item_ids)).delete(
                synchronize_session=False)
    db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib.id).delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


def _scanfile(path):
    return scanner.ScanFile(
        stored_path=str(path), name=path.name,
        local_dir=str(path.parent), size=64, container="mp4",
    )


def _run_scan(db, lib, files, monkeypatch):
    # 关分层扫描：分层 L1 口径下 NFO 不在扫描期取回（留给 enrich），
    # 本文件钉的是扫描期写库点位，故走非分层口径（与 N2 文件同法）。
    monkeypatch.setattr(scanner, "SCAN_LAYERED", False)
    monkeypatch.setattr(scanner, "PROBE_BACKGROUND", True)
    monkeypatch.setattr(
        scanner, "probe_metadata",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("background 模式不应探测")))

    def fake_sources(snap, library, db_, failed_roots, report=None):
        yield "test", iter([_scanfile(p) for p in files])

    monkeypatch.setattr(scanner, "iter_scan_sources", fake_sources)
    scanner.scan_library_sync(db, lib)


def _rows(db, lib, item_type):
    return db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib.id,
        em.MediaItem.item_type == item_type,
        em.MediaItem.deleted_at.is_(None)).all()


def _make_movie_lib(db, tmp_path, name):
    lib = em.Library(guid=_guid(), name=name, collection_type="movies",
                     paths=str(tmp_path), scrape_policy="missing_only")
    db.add(lib)
    db.commit()
    return lib


def _seed_identified_movie(db, tmp_path, monkeypatch, locked=False):
    """扫一遍建行，再把行改成「已被自动来源识别过」的稳态：
    有 tmdb_id（missing_only 下 should_scrape=False）、有简介。"""
    f = tmp_path / "Branch Movie (2020).mp4"
    f.write_bytes(b"\x00" * 64)
    lib = _make_movie_lib(db, tmp_path, "N3 补遗电影库")
    _run_scan(db, lib, [f], monkeypatch)
    rows = _rows(db, lib, "movie")
    assert len(rows) == 1
    item = rows[0]
    item.tmdb_id = "999001"
    item.name = "自动来源标题"
    item.overview = "自动来源简介"
    item.community_rating = 7.1
    item.metadata_locked = locked
    db.commit()
    return lib, f, item.id


def test_movie_nfo_applies_despite_existing_overview(tmp_path, monkeypatch):
    """主改：policy 不重刮＋已有简介，盘上 NFO 仍按优先级应用。"""
    db = SessionLocal()
    try:
        lib, f, item_id = _seed_identified_movie(db, tmp_path, monkeypatch)
        (tmp_path / "movie.nfo").write_text(MOVIE_NFO, encoding="utf-8")

        _run_scan(db, lib, [f], monkeypatch)

        db.expire_all()
        item = db.get(em.MediaItem, item_id)
        assert item.name == "NFO 权威标题"
        assert item.overview == "NFO 权威简介"
        assert item.community_rating == 8.7
    finally:
        _cleanup(db, lib)
        db.close()


def test_movie_nfo_locked_item_unchanged(tmp_path, monkeypatch):
    """锁定条目同场景零变化（N3-2 守卫在本分支同样成立）。"""
    db = SessionLocal()
    try:
        lib, f, item_id = _seed_identified_movie(
            db, tmp_path, monkeypatch, locked=True)
        (tmp_path / "movie.nfo").write_text(MOVIE_NFO, encoding="utf-8")

        _run_scan(db, lib, [f], monkeypatch)

        db.expire_all()
        item = db.get(em.MediaItem, item_id)
        # 只断言 NFO 能写的字段：条目名在重扫时由文件名解析刷新
        # （不经 apply_nfo，与 #1850 存疑的 series 目录名直写同类，
        # 不在本守卫与本单范围）；简介与评分只有 apply_nfo/TMDB
        # 能写，锁定后必须零变化。
        assert item.overview == "自动来源简介"
        assert item.community_rating == 7.1
        assert item.metadata_source != "nfo"
    finally:
        _cleanup(db, lib)
        db.close()


def _seed_identified_series(db, tmp_path, monkeypatch):
    """扫一遍建 series/season/episode 三层，series 改成已完全识别稳态。"""
    season_dir = tmp_path / "Branch Show (2021)" / "Season 01"
    season_dir.mkdir(parents=True)
    f = season_dir / "Branch Show S01E01.mp4"
    f.write_bytes(b"\x00" * 64)
    lib = em.Library(guid=_guid(), name="N3 补遗剧集库",
                     collection_type="tvshows", paths=str(tmp_path),
                     scrape_policy="missing_only")
    db.add(lib)
    db.commit()
    _run_scan(db, lib, [f], monkeypatch)
    series_rows = _rows(db, lib, "series")
    assert len(series_rows) == 1
    series = series_rows[0]
    series.tmdb_id = "999002"
    series.name = "自动来源剧名"
    series.overview = "自动来源剧简介"
    db.commit()
    season_rows = _rows(db, lib, "season")
    assert len(season_rows) == 1
    return lib, f, series.id, season_rows[0].id, season_dir


def test_series_nfo_applies_when_series_fully_identified(
        tmp_path, monkeypatch):
    """series 级复合门：已完全识别的剧，新放的 tvshow.nfo 必须生效。"""
    db = SessionLocal()
    try:
        lib, f, series_id, _season_id, season_dir = \
            _seed_identified_series(db, tmp_path, monkeypatch)
        (season_dir.parent / "tvshow.nfo").write_text(
            TVSHOW_NFO, encoding="utf-8")

        _run_scan(db, lib, [f], monkeypatch)

        db.expire_all()
        series = db.get(em.MediaItem, series_id)
        assert series.name == "NFO 剧名"
        assert series.overview == "NFO 剧简介"
        assert series.community_rating == 9.1
    finally:
        _cleanup(db, lib)
        db.close()


def test_season_nfo_applies_despite_existing_overview(
        tmp_path, monkeypatch):
    """season 级：已有季简介时，season.nfo 的简介与评分仍要应用。"""
    db = SessionLocal()
    try:
        lib, f, _series_id, season_id, season_dir = \
            _seed_identified_series(db, tmp_path, monkeypatch)
        season = db.get(em.MediaItem, season_id)
        season.overview = "旧季简介"
        season.community_rating = 5.0
        db.commit()
        (season_dir / "season.nfo").write_text(
            SEASON_NFO, encoding="utf-8")

        _run_scan(db, lib, [f], monkeypatch)

        db.expire_all()
        season = db.get(em.MediaItem, season_id)
        assert season.overview == "NFO 季简介"
        assert season.community_rating == 9.0
    finally:
        _cleanup(db, lib)
        db.close()

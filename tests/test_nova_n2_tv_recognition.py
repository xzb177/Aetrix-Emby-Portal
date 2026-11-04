"""Nova N2 剧集识别对照（NovaEmby 全面学习立项 N2-1）。

Nova 实证口径（novaemby-deep-raw §3a）：样本目录
``Deep Study Show (2021)/Season 01/Deep Study Show S01E01.mp4`` 在零 Key、
零 NFO 时扫入：Series 名=「Deep Study Show」、年份 2021，Season 名=
「第 1 季」，Episode name=「第 1 集」——**无元数据标题时集名只写
「第 N 集」**，不再拼「剧名 SxxExx」（剧名由 series 行承载，季集号有
独立字段）。

Aetrix 旧口径：无集标题时集名拼成「Deep Study Show S01E01」。
本文件钉住新口径，并同时钉住反向底线：**有真实集名（单集 NFO 标题）
时展示名拼接口径逐字不变**，不许被回退名改动误伤。
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


def _run_tv_scan(db, lib, files, monkeypatch):
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


def test_nova_sample_names_episodes_by_number_only(tmp_path, monkeypatch):
    """Nova 同款样本：无元数据时 Series/Season/Episode 三层与集名口径。"""
    season_dir = tmp_path / "Deep Study Show (2021)" / "Season 01"
    season_dir.mkdir(parents=True)
    f1 = season_dir / "Deep Study Show S01E01.mp4"
    f2 = season_dir / "Deep Study Show S01E02.mp4"
    f1.write_bytes(b"\x00" * 64)
    f2.write_bytes(b"\x00" * 64)

    db = SessionLocal()
    lib = em.Library(guid=_guid(), name="N2 Nova 样本库",
                     collection_type="tvshows", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        _run_tv_scan(db, lib, [f1, f2], monkeypatch)

        series_rows = _rows(db, lib, "series")
        assert len(series_rows) == 1
        series = series_rows[0]
        assert series.name == "Deep Study Show"
        assert series.production_year == 2021

        season_rows = _rows(db, lib, "season")
        assert len(season_rows) == 1
        season = season_rows[0]
        assert season.name == "第 1 季"
        assert season.season_number == 1
        assert season.parent_id == series.id

        eps = sorted(_rows(db, lib, "episode"),
                     key=lambda r: r.episode_number)
        assert [e.episode_number for e in eps] == [1, 2]
        # Nova 口径：无元数据标题时集名就是「第 N 集」
        assert [e.name for e in eps] == ["第 1 集", "第 2 集"]
        for e in eps:
            assert e.season_number == 1
            assert e.parent_id == season.id
            assert e.series_id == series.id
    finally:
        _cleanup(db, lib)
        db.close()


def test_nfo_episode_title_keeps_existing_display_name(tmp_path, monkeypatch):
    """有真实集名（单集 NFO 标题）时，展示名拼接口径不变（回退改动不许误伤）。

    关分层扫描：默认分层 L1 口径下 NFO 结果不在扫描期取回（留给 enrich
    后台补），本用例要钉的是写库点位上「有 NFO 标题」的分支，故走
    非分层口径让 NFO 在扫描期落库。
    """
    monkeypatch.setattr(scanner, "SCAN_LAYERED", False)
    season_dir = tmp_path / "Deep Study Show (2021)" / "Season 01"
    season_dir.mkdir(parents=True)
    f3 = season_dir / "Deep Study Show S01E03.mp4"
    f3.write_bytes(b"\x00" * 64)
    nfo = season_dir / "Deep Study Show S01E03.nfo"
    nfo.write_text(
        "<episodedetails><title>真实集名测试</title></episodedetails>",
        encoding="utf-8")

    db = SessionLocal()
    lib = em.Library(guid=_guid(), name="N2 NFO 标题库",
                     collection_type="tvshows", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        _run_tv_scan(db, lib, [f3], monkeypatch)

        eps = _rows(db, lib, "episode")
        assert len(eps) == 1
        # 旧口径逐字保留：真实标题 + SxxExx 后缀（_episode_display_name 契约）
        assert eps[0].name == "真实集名测试 S01E03"
    finally:
        _cleanup(db, lib)
        db.close()

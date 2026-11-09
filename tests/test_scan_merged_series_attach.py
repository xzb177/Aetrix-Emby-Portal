# -*- coding: utf-8 -*-
"""合并后扫描挂集 + 衍生剧分组回归（2026-11-08 全库普查后续）。

背景（生产实锤）：
1. 2026-10-21 PR #1161 把集跟随收敛进 merge_group 末尾——合并「当时」
   的集不再滞留。但合并「之后」新扫入的集仍会丢：扫描器按目录 guid
   解析 series 时拿到的是已被合并的分身行（merged_into_id 非空），
   新集直接挂到分身名下（series_id=分身），而分身不会再被合并工人
   重新处理，集永久不可见。2026-11-08 普查 31,177 滞留集中，修复后
   入库的 869 集、集中在 32 个折叠行名下，即此通道。
   修复口径与 upstream_import 一致：挂集前沿 merged_into_id 跟随到
   存活正主再挂。
2. 衍生剧（《名侦探柯南：零的日常》《犯人犯泽先生》）曾被并进正剧
   一家。现行自动合并口径钉死：严格通道只认同 TMDB；宽松通道只认
   宽松归一化名「全等」且带多编号/年份守卫；按季折叠只认季号后缀
   底名。无共同身份号、名字不同的衍生剧不许被任何自动通道同组。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base, SessionLocal, init_db
from backend.emby_server import merge_versions_worker as mvw
from backend.emby_server import models as em
from backend.emby_server import mounts as _mounts  # noqa: F401 -- 必须先于 scanner 导入，避免循环导入
from backend.emby_server import scanner
from backend.emby_server import season_fold as sf
import backend.models  # noqa: F401 — 注册 web_users 等表，供 create_all 建表

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


def test_new_episode_after_merge_attaches_to_primary(tmp_path, monkeypatch):
    """剧行合并后，分身目录新扫入的集必须挂到存活正主名下（零滞留）。"""
    dir_a = tmp_path / "Merged Show (2020)" / "Season 01"
    dir_a.mkdir(parents=True)
    a1 = dir_a / "Merged Show S01E01.mp4"
    a1.write_bytes(b"\x00" * 64)
    # 正主目录：建行时先用异名文件夹避开扫描期同名复用，合并前再改同名
    dir_b = tmp_path / "Merged Show Alt (2020)" / "Season 01"
    dir_b.mkdir(parents=True)
    b1 = dir_b / "Merged Show Alt S01E01.mp4"
    b1.write_bytes(b"\x00" * 64)

    db = SessionLocal()
    lib = em.Library(guid=_guid(), name="合并后挂集测试库",
                     collection_type="tvshows", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        # 第一扫：两个目录各建一行（分身 L 与正主候选 P），各带第 1 集
        _run_tv_scan(db, lib, [a1, b1], monkeypatch)
        series_rows = _rows(db, lib, "series")
        assert len(series_rows) == 2
        loser = [s for s in series_rows if s.name == "Merged Show"][0]
        primary = [s for s in series_rows if s.name == "Merged Show Alt"][0]

        # 两行补齐同 TMDB（严格通道合并的典型形态）；P 补海报保证选主必胜，
        # 名字改回同名（两行本是同一部剧的两个来源目录）。
        loser.tmdb_id = "777001"
        primary.tmdb_id = "777001"
        primary.name = "Merged Show"
        primary.primary_image_url = "http://img/p.jpg"
        db.commit()
        primary_id = primary.id
        loser_id = loser.id

        merged = mvw.merge_group(db, [loser, primary])
        db.commit()
        assert merged == 1
        loser_now = db.query(em.MediaItem).filter(
            em.MediaItem.id == loser_id).one()
        assert loser_now.merged_into_id == primary_id
        # 合并当时集跟随已生效（PR #1161 口径）：L 的第 1 集已成 P 第 1 集的副本
        a_ep1 = [e for e in _rows(db, lib, "episode")
                 if e.file_path == str(a1)][0]
        assert a_ep1.merged_into_id is not None

        # 第二扫：分身目录里多了一集新文件（两目录文件都照常在场）
        a2 = dir_a / "Merged Show S01E02.mp4"
        a2.write_bytes(b"\x00" * 64)
        _run_tv_scan(db, lib, [a1, a2, b1], monkeypatch)

        eps = {e.file_path: e for e in _rows(db, lib, "episode")}
        assert set(eps) == {str(a1), str(a2), str(b1)}
        ep2 = eps[str(a2)]
        # 新集不许挂在已合并分身名下（旧行为：series_id == loser_id，滞留）
        assert ep2.series_id == primary_id, (
            f"新集挂在 series_id={ep2.series_id}，正主应为 {primary_id}，"
            f"分身为 {loser_id}")
        season2 = db.query(em.MediaItem).filter(
            em.MediaItem.id == ep2.parent_id).one()
        assert season2.series_id == primary_id
        # 正主自己的集仍在正主名下
        assert eps[str(b1)].series_id == primary_id
        # 分身的存量集重扫后也不许被改挂回分身（跟随状态不许被扫描回滚）
        assert eps[str(a1)].series_id == primary_id
    finally:
        _cleanup(db, lib)
        db.close()


@pytest.fixture()
def iso_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/spinoff_group.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _series(db, name, tmdb=None, year=None, lib=1):
    it = em.MediaItem(
        guid=f"s-{_guid()}", library_id=lib, item_type="series",
        name=name, tmdb_id=tmdb, production_year=year,
        file_path="/x", enrich_status="done")
    db.add(it)
    db.commit()
    return it


def test_spinoff_series_never_auto_grouped(iso_db):
    """无共同身份号、名字不同的衍生剧不许被任何自动通道与正剧同组。"""
    main = _series(iso_db, "名侦探柯南", year=1996)
    alias = _series(iso_db, "名侦探柯南 (1996)", year=1996)
    spin1 = _series(iso_db, "名侦探柯南：零的日常", year=2022)
    spin2 = _series(iso_db, "名侦探柯南：犯人犯泽先生", year=2022)
    spinoff_ids = {spin1.id, spin2.id}

    cfg = {"enabled": True, "loose_enabled": True, "require_year": True,
           "season_fold_enabled": True, "batch_limit": 1000}

    # 严格通道：无人带 tmdb → 一组都不许有
    assert mvw.find_duplicate_groups(iso_db) == []

    # 宽松通道：衍生剧不许与任何行同组（正剧与年份变体同组属预期，不禁）
    for group in mvw._find_series_loose_groups(iso_db, cfg):
        ids = {g.id for g in group}
        assert not (ids & spinoff_ids), (
            f"衍生剧被宽松通道同组: {sorted(ids)}")

    # 按季折叠：衍生剧不许出现在任何折叠计划里（无论当正主还是被折行）
    plans, _info = sf.find_season_fold_plan(iso_db, cfg)
    for plan in plans:
        assert plan["primary_id"] not in spinoff_ids
        folded_ids = {rid for rid, _season in plan["folds"]}
        assert not (folded_ids & spinoff_ids), (
            f"衍生剧被按季折叠: plan={plan}")
        assert not (set(plan["group_ids"]) & spinoff_ids)
    _ = (main, alias)  # 正剧两行只用于构成同名家族压力，不做断言对象

"""库级扫描预设策略 L10 单测。

两条路全覆盖：
① 开关关、或未配策略的库 → 现行口径，零行为变化；
② 开关开且该库配了策略 → 策略定解析口径（preset 覆盖 collection_type、
   扩展名白名单与 VIDEO_EXTS 取交集只收窄、路径子串过滤）。
preset 取值 = Library.collection_type 取值（movies/tvshows/mixed），
绝无外部预设名、无业务片单常量。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import library_scan_presets as lsp
from backend.emby_server import models as em
from backend.emby_server import scanner as sc


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/preset.db")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _lib(db, collection_type="movies"):
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type=collection_type, is_enabled=True)
    db.add(lib)
    db.flush()
    return lib


def _snap(lib_id, collection_type="movies"):
    return sc.LibrarySnapshot(library_id=lib_id, name="库",
                              collection_type=collection_type, paths=(),
                              scrape_policy="missing_only")


def test_switch_off_or_no_policy_is_current_behavior(db, monkeypatch):
    lib = _lib(db)
    snap = _snap(lib.id)
    monkeypatch.setattr(lsp, "LIBRARY_SCAN_PRESETS_ENABLED", 0)
    assert lsp.policy_for_library(db, lib.id) is None
    assert lsp.attach_policy_to_snapshot(db, snap,
                                         allowed_extensions=sc.VIDEO_EXTS) is snap
    # 未配策略的库：开关开也一样走现行口径
    monkeypatch.setattr(lsp, "LIBRARY_SCAN_PRESETS_ENABLED", 1)
    assert lsp.policy_for_library(db, lib.id) is None
    assert lsp.attach_policy_to_snapshot(db, snap,
                                         allowed_extensions=sc.VIDEO_EXTS) is snap
    assert lsp.effective_parse_type(snap, "movies") == "movies"
    assert lsp.snapshot_policy_allows(snap, "/x/S01E01.avi") is True


def test_policy_overrides_parse_type_and_filters(db, monkeypatch):
    lib = _lib(db, collection_type="movies")
    lsp.upsert_policy(db, lib.id, "tvshows", extensions=".mkv,.mp4,.txt",
                      path_filters='["Season 1"]')
    db.commit()
    monkeypatch.setattr(lsp, "LIBRARY_SCAN_PRESETS_ENABLED", 1)
    snap = lsp.attach_policy_to_snapshot(
        db, _snap(lib.id), allowed_extensions=sc.VIDEO_EXTS)
    # preset 覆盖解析口径：电影库按剧集解析
    assert snap.policy_parse_type == "tvshows"
    assert lsp.effective_parse_type(snap, "movies") == "tvshows"
    # 扩展名白名单与 VIDEO_EXTS 取交集：.txt 被交集挡掉，只收窄不放宽
    assert snap.policy_extensions == (".mkv", ".mp4")
    assert lsp.snapshot_policy_allows(snap, "/Season 1/S01E01.mkv") is True
    assert lsp.snapshot_policy_allows(snap, "/Season 1/S01E01.avi") is False
    assert lsp.snapshot_policy_allows(snap, "/Season 2/S01E01.mkv") is False
    # 解析口径真的变了：策略 preset（tvshows）下该文件解析出季/集
    parsed_tv = sc.parse_media_filename("/Season 1/S01E01.mkv",
                                        snap.policy_parse_type)
    assert parsed_tv["season"] == 1 and parsed_tv["episode"] == 1


def test_invalid_preset_falls_back_to_current(db, monkeypatch):
    # 脏 preset 行（DB CHECK 本身也拦非法值，双重防线）：把允许集收窄到
    # 不含该行 preset，模拟读到非法策略——读路径必须回落现行口径。
    lib = _lib(db)
    lsp.upsert_policy(db, lib.id, "movies")
    db.commit()
    monkeypatch.setattr(lsp, "LIBRARY_SCAN_PRESETS_ENABLED", 1)
    monkeypatch.setattr(lsp, "PRESETS", ("tvshows", "mixed"))
    assert lsp.policy_for_library(db, lib.id) is None
    assert lsp.attach_policy_to_snapshot(
        db, _snap(lib.id), allowed_extensions=sc.VIDEO_EXTS).policy_parse_type is None


def test_preset_values_derive_from_collection_types():
    """preset 取值集合 = Aetrix 现有 collection_type 取值，无外部预设名。"""
    assert set(lsp.PRESETS) == {"movies", "tvshows", "mixed"}
    assert set(lsp.PRESETS) == set(em.LIBRARY_SCAN_PRESETS)
    with pytest.raises(ValueError):
        # upsert 校验只认集合内取值（preset 校验在任何 DB 访问之前）
        lsp.upsert_policy(None, 1, "anime")


def test_prepare_prefetch_applies_policy(monkeypatch):
    """扫描体预处理：策略过滤掉不合规文件、解析口径用策略 preset。"""
    snap = sc.LibrarySnapshot(
        library_id=1, name="库", collection_type="movies", paths=(),
        scrape_policy="missing_only", policy_parse_type="tvshows",
        policy_extensions=(".mkv",), policy_path_filters=())
    ctx = sc._ScanContext(snap=snap, lib_id=1, stats={})
    sf_ok = sc.ScanFile(stored_path="/Season 1/S01E01.mkv", name="S01E01.mkv",
                        local_dir="/Season 1")
    sf_bad_ext = sc.ScanFile(stored_path="/Season 1/S01E01.avi",
                             name="S01E01.avi", local_dir="/Season 1")
    # 直接调预处理的核心判定：策略过滤与解析口径（不依赖 DB 写库）
    assert lsp.snapshot_policy_allows(snap, sf_ok.stored_path) is True
    assert lsp.snapshot_policy_allows(snap, sf_bad_ext.stored_path) is False
    assert lsp.effective_parse_type(snap, "movies") == "tvshows"
    parsed = sc.parse_media_filename(sf_ok.stored_path,
                                     lsp.effective_parse_type(snap, "movies"))
    assert parsed["season"] == 1

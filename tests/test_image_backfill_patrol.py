# -*- coding: utf-8 -*-
"""图片后台补下载巡检（image_backlog_patrol）+ 刮削期预热开关测试。

背景（2026-10-21）：刮削期预热默认关（EMBY_IMAGE_PREWARM），刮削只落远程
地址；图靠 enrich janitor 的补下载巡检涓流落盘，新入库优先，用户打开时
基本是现成的。取图时按需自愈仍是最后兜底。

覆盖：
- prewarm_enabled / backfill_enabled 开关口径（默认关 / 默认开、env 覆盖）；
- tmdb.prewarm_images：预热关时零下载返回 0；开时照常下载；
- image_store.prewarm 本身不受开关影响（显式修复路径 refresh_images 靠它）；
- 巡检：缺图的条目被补上 poster_path/backdrop_path 并计数；
  已有本地图（poster_path 非空，含 sidecar）不碰；软删/合并副本跳过；
  新入库优先 + 单轮上限；下载失败不计数且冷却内不重复尝试；
  EMBY_IMAGE_BACKFILL=0 时整轮跳过、零下载。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import image_store
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import tmdb as tmdb_mod

init_db()

GOOD = "https://image.tmdb.org/t/p/w500/good.jpg"
GOOD_BG = "https://image.tmdb.org/t/p/w1280/goodbg.jpg"
BAD = "https://image.tmdb.org/t/p/w500/bad.jpg"


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _image_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBY_IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.delenv("EMBY_LOCALIZE_IMAGES", raising=False)
    monkeypatch.delenv("EMBY_IMAGE_PREWARM", raising=False)
    monkeypatch.delenv("EMBY_IMAGE_BACKFILL", raising=False)
    monkeypatch.setattr(image_store, "enqueue_thumb_warm", lambda *a, **k: None)
    enrich_worker._IMAGE_BACKFILL_TRIED.clear()
    yield
    enrich_worker._IMAGE_BACKFILL_TRIED.clear()


@pytest.fixture
def downloads(monkeypatch):
    """假下载：good 地址给字节，bad 地址返回 None（确定性失败）。"""
    calls = []

    def fake_download(url, attempts=1):
        calls.append(url)
        if "bad" in url:
            return None
        return b"fake-image-bytes"

    monkeypatch.setattr(image_store, "_download", fake_download)
    return calls


def _mk_item(db, **kw):
    import uuid as _uuid
    kw.setdefault("guid", _uuid.uuid4().hex)
    kw.setdefault("library_id", 1)
    it = em.MediaItem(**kw)
    db.add(it)
    db.commit()
    return it


class TestSwitches:
    def test_prewarm_default_off(self, monkeypatch):
        monkeypatch.delenv("EMBY_IMAGE_PREWARM", raising=False)
        assert image_store.prewarm_enabled() is False
        monkeypatch.setenv("EMBY_IMAGE_PREWARM", "1")
        assert image_store.prewarm_enabled() is True
        monkeypatch.setenv("EMBY_IMAGE_PREWARM", "0")
        assert image_store.prewarm_enabled() is False

    def test_backfill_default_on(self, monkeypatch):
        monkeypatch.delenv("EMBY_IMAGE_BACKFILL", raising=False)
        assert image_store.backfill_enabled() is True
        monkeypatch.setenv("EMBY_IMAGE_BACKFILL", "0")
        assert image_store.backfill_enabled() is False


class TestPrewarmImagesGate:
    def test_gate_off_no_download(self, downloads):
        n = tmdb_mod.prewarm_images({"poster_path": "/good.jpg"})
        assert n == 0
        assert downloads == []

    def test_gate_on_downloads(self, downloads, monkeypatch):
        monkeypatch.setenv("EMBY_IMAGE_PREWARM", "1")
        n = tmdb_mod.prewarm_images({"poster_path": "/good.jpg"})
        assert n == 1
        assert downloads == [GOOD]

    def test_store_prewarm_itself_not_gated(self, downloads):
        # image_store.prewarm 是显式下载入口（修复路径 refresh_images 直调），
        # 不受刮削预热开关影响
        assert image_store.prewarm([GOOD]) == 1
        assert downloads == [GOOD]


class TestBacklogPatrol:
    def test_fills_poster_and_backdrop(self, db, downloads):
        it = _mk_item(db, item_type="movie", name="有图无本地",
                      primary_image_url=GOOD, backdrop_image_url=GOOD_BG)
        n = enrich_worker.image_backlog_patrol(db)
        db.refresh(it)
        assert n == 1
        assert it.poster_path and os.path.isfile(it.poster_path)
        assert it.backdrop_path and os.path.isfile(it.backdrop_path)

    def test_skips_items_with_local_image(self, db, downloads):
        _mk_item(db, item_type="movie", name="已有本地图",
                 primary_image_url=GOOD, poster_path="/somewhere/poster.jpg")
        assert enrich_worker.image_backlog_patrol(db) == 0
        assert downloads == []

    def test_skips_invisible_rows(self, db, downloads):
        from datetime import datetime
        _mk_item(db, item_type="movie", name="软删",
                 primary_image_url=GOOD, deleted_at=datetime.now())
        keep = _mk_item(db, item_type="movie", name="主行",
                        primary_image_url=GOOD)
        _mk_item(db, item_type="movie", name="合并副本",
                 primary_image_url=GOOD, merged_into_id=keep.id)
        n = enrich_worker.image_backlog_patrol(db)
        assert n == 1
        assert downloads == [GOOD]

    def test_newest_first_and_limit(self, db, downloads, monkeypatch):
        monkeypatch.setattr(enrich_worker, "_IMAGE_BACKFILL_PER_ROUND", 2)
        items = [_mk_item(db, item_type="movie", name=f"片{i}",
                          primary_image_url=GOOD) for i in range(4)]
        n = enrich_worker.image_backlog_patrol(db)
        assert n == 2
        db.refresh(items[3])
        db.refresh(items[0])
        assert items[3].poster_path  # 最新两条先补
        assert not items[0].poster_path

    def test_failed_download_cooldown(self, db, downloads):
        _mk_item(db, item_type="movie", name="坏图", primary_image_url=BAD)
        assert enrich_worker.image_backlog_patrol(db) == 0
        first_calls = len(downloads)
        assert first_calls >= 1
        # 冷却内第二轮不再尝试同一条
        assert enrich_worker.image_backlog_patrol(db) == 0
        assert len(downloads) == first_calls

    def test_backfill_disabled(self, db, downloads, monkeypatch):
        monkeypatch.setenv("EMBY_IMAGE_BACKFILL", "0")
        _mk_item(db, item_type="movie", name="有图无本地",
                 primary_image_url=GOOD)
        assert enrich_worker.image_backlog_patrol(db) == 0
        assert downloads == []

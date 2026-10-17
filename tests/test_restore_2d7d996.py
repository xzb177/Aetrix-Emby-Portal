"""回归：2d7d996（安全修复 v2）误删的 StrmAssistant（神医助手）移植功能已恢复。

2d7d996 基于旧底稿重新应用，把 #403/#405/#406/#407/#408 的一批功能整段覆盖掉了：
IntroMarker 模型（表 emby_intro_markers，intro_marker.py 与路由还在 → 500）、
演员 person_tmdb_id、TMDB 备选语言链 / 原语言海报、代理 URL 工具、拼音 lru_cache、
worker 跨进程心跳、媒体信息 JSON 清理、缩略图 guid 净化 / 单视频超时等。
这里逐项锁住，防止再被整文件覆盖时悄悄丢失。

本文件自建内存库，不依赖测试执行顺序，也不改全局 SessionLocal。
"""
import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.emby_server import models as em  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


@pytest.fixture()
def mem_db():
    from backend import models as base_models

    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    try:
        lib = em.Library(guid="R" * 32, name="恢复测试库", collection_type="tvshows", paths="")
        db.add(lib)
        db.flush()
        item = em.MediaItem(library_id=lib.id, guid="e" * 32, item_type="episode",
                            name="第1集")
        db.add(item)
        db.commit()
        yield db, engine, item
    finally:
        db.close()
        engine.dispose()


# ---------- #3 片头片尾标记：模型 + 读写 ----------

def test_intro_marker_model_and_table(mem_db):
    _db, engine, _item = mem_db
    assert em.IntroMarker.__tablename__ == "emby_intro_markers"
    assert "IntroMarker" in em.__all__
    assert "emby_intro_markers" in inspect(engine).get_table_names()


def test_intro_marker_roundtrip(mem_db):
    from backend.emby_server import intro_marker as im

    db, _engine, item = mem_db
    im.set_marker(db, item.id, "intro", 1000, 90000)
    im.set_marker(db, item.id, "intro", 2000, 91000)  # 同类型幂等覆盖
    markers = im.get_markers(db, item.id)
    assert len(markers) == 1
    assert markers[0]["start_ms"] == 2000 and markers[0]["end_ms"] == 91000
    assert im.to_chapters(markers)[0]["MarkerType"] == "intro"
    assert im.delete_marker(db, item.id, "intro")
    assert im.get_markers(db, item.id) == []


# ---------- #9 演员增强：person_tmdb_id ----------

def test_person_tmdb_id_column_and_migration():
    assert "person_tmdb_id" in em.EmbyPerson.__table__.columns
    src = _read("backend/database.py")
    assert '("person_tmdb_id", "VARCHAR(32)", "NULL")' in src
    assert "idx_person_item_tmdb" in src
    assert "ON emby_people (item_id, person_tmdb_id)" in src
    assert "_ensure_admin_roles()" in src  # 2d7d996 的 S1 迁移保留


def test_cast_list_carries_person_id_and_apply_cast_persists(mem_db):
    from backend.emby_server import enrich_worker
    from backend.emby_server.tmdb import cast_list

    db, _engine, item = mem_db
    details = {"credits": {"cast": [
        {"id": 287, "name": "Brad Pitt", "character": "Tyler", "profile_path": "/a.jpg"},
    ]}}
    cast = cast_list(details)
    assert cast and cast[0]["person_id"] == "287"
    enrich_worker._apply_cast(db, item, details)
    db.commit()
    row = db.query(em.EmbyPerson).filter(em.EmbyPerson.item_id == item.id).one()
    assert row.person_tmdb_id == "287"


def test_refresh_person_worker_started_by_worker():
    assert "refresh_person_worker" in _read("backend/worker.py")


# ---------- #12 拼音缓存 ----------

def test_pinyin_initials_lru_cache_size():
    from backend.emby_server import pinyin_sort

    assert pinyin_sort.pinyin_initials.cache_info().maxsize == 65536


# ---------- #7 备选语言 / #10 原语言海报 ----------

def test_language_fallback_chain_starts_with_preferred(monkeypatch):
    from backend.emby_server import tmdb

    monkeypatch.setattr(tmdb, "preferred_language", lambda db=None: "ja-JP")
    chain = tmdb.language_fallback_chain()
    assert chain[0] == "ja-JP"
    assert len(chain) == len(set(chain))
    assert len(chain) <= 1 + tmdb.TMDB_FALLBACK_MAX_LANGS


def test_search_falls_back_to_next_language(monkeypatch):
    from backend.emby_server import tmdb

    monkeypatch.setattr(tmdb, "language_fallback_chain", lambda db=None: ["zh-CN", "en-US"])
    client = tmdb.TmdbClient()
    seen = []

    def fake_search_raw(query, year, kind, lang=None):
        seen.append(lang)
        if lang == "en-US":
            return [{"id": 42, "title": query, "name": query}]
        return []

    monkeypatch.setattr(client, "_search_raw", fake_search_raw)
    hit = client.search("Inception", 2010, "movie")
    assert hit and hit["id"] == 42
    assert "zh-CN" in seen and "en-US" in seen
    assert seen.index("zh-CN") < seen.index("en-US")  # 首选语言先走完


def test_poster_language_default_and_apply_images_prefers_lang_poster():
    from types import SimpleNamespace

    from backend.emby_server import tmdb

    assert tmdb.TMDB_POSTER_LANGUAGE_DEFAULT == "system"
    assert set(tmdb.TMDB_POSTER_LANGUAGE_OPTIONS) == {"system", "original", "zh-CN"}
    item = SimpleNamespace(poster_path=None, primary_image_url=None, backdrop_path=None,
                           backdrop_image_url=None, metadata_source=None)
    data = {"poster_path": "/default.jpg"}
    tmdb.tmdb_client.apply_images(item, data, images_lang={"posters": [{"file_path": "/orig.jpg"}]})
    assert "orig.jpg" in (item.primary_image_url or item.poster_path or "")


def test_invalidate_search_clears_fallback_languages():
    src = _read("backend/emby_server/tmdb_cache.py")
    assert "language_fallback_chain" in src


# ---------- #407 worker 跨进程心跳 ----------

class _FakeRedis:
    def __init__(self):
        self.kv = {}

    def setex(self, k, _ttl, v):
        self.kv[k] = v

    def get(self, k):
        return self.kv.get(k)

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return [k for k in list(self.kv) if k.startswith(prefix)]


def test_worker_registry_heartbeat_via_redis(monkeypatch):
    from backend.emby_server import worker_registry as wr

    fake = _FakeRedis()
    monkeypatch.setattr(wr, "_redis", lambda: fake)
    wr.heartbeat("restore_test_remote")
    assert "worker:heartbeat:restore_test_remote" in fake.kv
    snap = wr.snapshot()
    assert snap["restore_test_remote"]["alive"] is True
    assert snap["restore_test_remote"]["via"] == "redis"


def test_health_report_flags_dead_workers():
    src = _read("backend/health_report.py")
    assert 'not s["alive"]' in src


def test_workers_register_heartbeat():
    for rel in ("backend/emby_server/thumbnail_worker.py",
                "backend/emby_server/subtitle_scan_worker.py",
                "backend/emby_server/merge_versions_worker.py"):
        src = _read(rel)
        assert "_wr.register(" in src and "_wr.heartbeat(" in src, rel


# ---------- 扫描器：下架时清理媒体信息 JSON ----------

def test_purge_items_deletes_mediainfo_json(mem_db, monkeypatch):
    from backend.emby_server import mediainfo_persist, scanner

    db, _engine, item = mem_db
    deleted = []
    monkeypatch.setattr(mediainfo_persist, "delete_json", lambda it: deleted.append(it.guid))
    scanner._purge_items(db, [item.id])
    assert deleted == [item.guid]


# ---------- 缩略图 / 字幕 worker 打磨 ----------

def test_thumbnail_dir_sanitizes_guid():
    from backend.emby_server import thumbnail_worker as tw

    d = tw.thumbnail_dir("../../etc/passwd")
    assert ".." not in d
    assert hasattr(tw, "THUMBNAIL_PER_VIDEO_TIMEOUT")
    assert "THUMBNAIL_PER_VIDEO_TIMEOUT" in _read("backend/emby_server/thumbnail_worker.py") \
        .split("def extract_thumbnails", 1)[1]


def test_subtitle_detect_unknown_dir_is_none():
    from backend.emby_server import subtitle_scan_worker as sw

    assert sw._detect_external_subtitles("/definitely/not/here/x.mkv") is None


# ---------- #4 多版本管理后台接口 ----------

def test_admin_versions_routes_registered():
    from backend.api.admin import admin_router

    paths = {getattr(r, "path", "") for r in admin_router.routes}
    assert any(p.endswith("/media/versions/{item_id}") for p in paths)
    assert any(p.endswith("/media/versions/{item_id}/unmerge") for p in paths)
    assert any(p.endswith("/media/versions/{item_id}/unmerge-all") for p in paths)

"""PA-040：智能字幕双语轨取用口／用户维命名／缓存淘汰递归。

红绿口径（本机 Python 3.12 定向，CI 全量以 3.11 为准）：
- 同条目同语言，两用户生成不得互相覆盖；
- 生成后请求用户自己的取用口 → 200 且内容一致；
  他人取用 → 404（拿不到别人的文件）；未生成 → 404；
  开关关 → 403（先于查库）；不可见条目 → 403；不存在条目 → 404；
  播放门禁拒绝 → 403。
- 淘汰：smart/ 子目录内的过期文件同样被删、同样计入数量上限。
"""
from __future__ import annotations

import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.emby_server import smart_subtitles as ss
from backend.emby_server import models as em

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nHello\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\nWorld\n"
)


def _enable(monkeypatch):
    monkeypatch.setenv("SMART_SUBTITLES_ENABLED", "1")


def _disable(monkeypatch):
    monkeypatch.setenv("SMART_SUBTITLES_ENABLED", "0")


# ---- 用户维命名：同条目同语言，两用户互不覆盖 ----

def test_two_users_same_item_same_lang_do_not_overwrite(tmp_path, monkeypatch):
    cache = str(tmp_path)
    ss.write_bilingual_track(7, "AAA", "eng", "chi", cache_dir=cache,
                             user_id=101)
    ss.write_bilingual_track(7, "BBB", "eng", "chi", cache_dir=cache,
                             user_id=202)
    first = ss.bilingual_cache_path(7, "eng", "chi", cache_dir=cache,
                                    user_id=101)
    second = ss.bilingual_cache_path(7, "eng", "chi", cache_dir=cache,
                                     user_id=202)
    assert first.name != second.name
    assert first.read_text(encoding="utf-8") == "AAA"
    assert second.read_text(encoding="utf-8") == "BBB"


def test_user_scoped_filename_shape_and_legacy_default(tmp_path):
    scoped = ss.bilingual_cache_path(7, "eng", "chi", cache_dir=str(tmp_path),
                                     user_id=101)
    assert scoped.name == "item_7_101_eng-chi.vtt"
    legacy = ss.bilingual_cache_path(7, "eng", "chi", cache_dir=str(tmp_path))
    assert legacy.name == "item_7_eng-chi.vtt"


# ---- 路由取用口 ----

def _client(monkeypatch, user_id=1, scope_allowed=True, playback_allowed=True):
    from backend.api import smart_subtitles as router_mod
    from backend.api.user import get_current_user
    from backend.database import get_db
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    em.MediaItem.__table__.create(engine, checkfirst=True)
    from backend import models as _core_models
    _core_models.SystemConfig.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()

    class _Scope:
        @staticmethod
        def item_allowed(db, user, item):
            return scope_allowed

    monkeypatch.setattr(router_mod, "library_scope", _Scope)

    def _gate(db, user, realm_id=None):
        if not playback_allowed:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="付费墙")

    monkeypatch.setattr(router_mod, "ensure_playback_allowed", _gate)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"id": user_id, "is_staff": False})()
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app), session


def _seed_item(session, item_id=6):
    session.add(em.MediaItem(id=item_id, guid="h" * 32, library_id=1,
                             item_type="movie", name="Movie.2024.mkv"))
    session.commit()


def test_retrieval_returns_own_track(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(ss.subtitle_delivery, "SUB_CACHE_DIR", str(tmp_path))
    client, session = _client(monkeypatch, user_id=1)
    _seed_item(session)
    r = client.post("/api/user/smart-subtitles/6/bilingual",
                    json={"source_text": SRT, "source_lang": "eng",
                          "target_lang": "chi",
                          "translations": ["你好", "世界"]})
    assert r.status_code == 200, r.text
    r = client.get("/api/user/smart-subtitles/6/bilingual/eng-chi")
    assert r.status_code == 200, r.text
    assert "你好" in r.text and "世界" in r.text
    assert r.headers["content-type"].startswith("text/vtt")


def test_retrieval_other_user_gets_404(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(ss.subtitle_delivery, "SUB_CACHE_DIR", str(tmp_path))
    client, session = _client(monkeypatch, user_id=1)
    _seed_item(session)
    r = client.post("/api/user/smart-subtitles/6/bilingual",
                    json={"source_text": SRT, "source_lang": "eng",
                          "target_lang": "chi",
                          "translations": ["你好", "世界"]})
    assert r.status_code == 200, r.text
    client2, _ = _client(monkeypatch, user_id=2)
    r = client2.get("/api/user/smart-subtitles/6/bilingual/eng-chi")
    assert r.status_code == 404


def test_retrieval_missing_track_404(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(ss.subtitle_delivery, "SUB_CACHE_DIR", str(tmp_path))
    client, session = _client(monkeypatch, user_id=1)
    _seed_item(session)
    r = client.get("/api/user/smart-subtitles/6/bilingual/eng-chi")
    assert r.status_code == 404


def test_retrieval_disabled_403_before_db(monkeypatch, tmp_path):
    _disable(monkeypatch)
    monkeypatch.setattr(ss.subtitle_delivery, "SUB_CACHE_DIR", str(tmp_path))
    client, _ = _client(monkeypatch, user_id=1)
    r = client.get("/api/user/smart-subtitles/6/bilingual/eng-chi")
    assert r.status_code == 403


def test_retrieval_invisible_item_403(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(ss.subtitle_delivery, "SUB_CACHE_DIR", str(tmp_path))
    client, session = _client(monkeypatch, user_id=1, scope_allowed=False)
    _seed_item(session)
    r = client.get("/api/user/smart-subtitles/6/bilingual/eng-chi")
    assert r.status_code == 403


def test_retrieval_missing_item_404(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(ss.subtitle_delivery, "SUB_CACHE_DIR", str(tmp_path))
    client, _ = _client(monkeypatch, user_id=1)
    r = client.get("/api/user/smart-subtitles/999/bilingual/eng-chi")
    assert r.status_code == 404


def test_retrieval_playback_gate_denied_403(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setattr(ss.subtitle_delivery, "SUB_CACHE_DIR", str(tmp_path))
    client, session = _client(monkeypatch, user_id=1, playback_allowed=False)
    _seed_item(session)
    r = client.get("/api/user/smart-subtitles/6/bilingual/eng-chi")
    assert r.status_code == 403


# ---- 淘汰递归进 smart/ ----

def test_prune_reaches_smart_subdir(monkeypatch, tmp_path):
    from backend.emby_server import maintenance as maint
    from backend.emby_server import subtitles as subs
    monkeypatch.setattr(subs, "SUB_CACHE_DIR", str(tmp_path))
    old = time.time() - 10 * 86400
    top_old = tmp_path / "top_old.vtt"
    top_old.write_text("old", encoding="utf-8")
    os.utime(top_old, (old, old))
    smart_dir = tmp_path / "smart"
    smart_dir.mkdir()
    smart_old = smart_dir / "item_9_1_eng-chi.vtt"
    smart_old.write_text("old", encoding="utf-8")
    os.utime(smart_old, (old, old))
    fresh = tmp_path / "fresh.vtt"
    fresh.write_text("new", encoding="utf-8")
    removed = maint.prune_subtitle_cache()
    assert removed == 2
    assert not top_old.exists()
    assert not smart_old.exists()
    assert fresh.exists()


def test_prune_counts_smart_files_toward_limit(monkeypatch, tmp_path):
    from backend.emby_server import maintenance as maint
    from backend.emby_server import subtitles as subs
    monkeypatch.setattr(subs, "SUB_CACHE_DIR", str(tmp_path))
    smart_dir = tmp_path / "smart"
    smart_dir.mkdir()
    now = time.time()
    for i in range(3):
        p = smart_dir / f"item_{i}_1_eng-chi.vtt"
        p.write_text("x", encoding="utf-8")
        os.utime(p, (now + i, now + i))
    removed = maint.prune_subtitle_cache(max_files=2)
    assert removed == 1
    assert not (smart_dir / "item_0_1_eng-chi.vtt").exists()
    assert (smart_dir / "item_2_1_eng-chi.vtt").exists()

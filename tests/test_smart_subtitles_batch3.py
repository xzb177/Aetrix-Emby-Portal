"""第三批第一单：智能字幕。纯核心 + 路由门禁测试。

口径：本地 Python 3.12（CI 以 3.11 全量为准）；路由测试用依赖覆盖
隔离 DB/鉴权，只验门禁与接线，不冒充真实库内可见性判定
（真实 library_scope.item_allowed 由既有测试覆盖）。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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


def _clear_translate_env(monkeypatch):
    for k in ("SMART_SUBTITLE_TRANSLATE_PROVIDER",
              "SMART_SUBTITLE_TRANSLATE_ENDPOINT",
              "SMART_SUBTITLE_TRANSLATE_API_KEY"):
        monkeypatch.delenv(k, raising=False)


# ---- 季集对齐与语言匹配 ----

def test_episode_alignment_rejects_other_episode():
    out = ss.match_for_item(
        "Show.S01E02.mkv", 1, 2,
        ["Show.S01E02.chi.srt", "Show.S01E03.chi.srt", "Show.S01E02.eng.srt"])
    names = [o["name"] for o in out]
    assert "Show.S01E02.chi.srt" in names
    assert "Show.S01E02.eng.srt" in names
    assert "Show.S01E03.chi.srt" not in names


def test_movie_rejects_episode_marked_candidate():
    out = ss.match_for_item("Movie.2024.mkv", None, None,
                            ["Movie.2024.chi.srt", "Movie.2024.S01E01.chi.srt"])
    assert [o["name"] for o in out] == ["Movie.2024.chi.srt"]


def test_language_match_from_filename():
    out = ss.match_for_item("Movie.2024.mkv", None, None,
                            ["Movie.2024.chs.srt", "Movie.2024.eng.srt"])
    by_name = {o["name"]: o["language"] for o in out}
    assert by_name["Movie.2024.chs.srt"] == "chi"
    assert by_name["Movie.2024.eng.srt"] == "eng"


# ---- 翻译缺配置明确失败 ----

def test_translate_missing_config_raises_explicitly(monkeypatch):
    _clear_translate_env(monkeypatch)
    with pytest.raises(ss.TranslateNotConfigured, match="未配置"):
        ss.translate_texts(["Hello"], "eng", "chi")


def test_translate_partial_config_raises_explicitly(monkeypatch):
    _clear_translate_env(monkeypatch)
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_PROVIDER", "custom")
    with pytest.raises(ss.TranslateNotConfigured, match="端点"):
        ss.translate_texts(["Hello"], "eng", "chi")


def test_translate_injected_provider_no_fallback_on_mismatch(monkeypatch):
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_PROVIDER", "custom")
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_ENDPOINT", "https://example.invalid/t")
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_API_KEY", "dummy-test-key")
    with pytest.raises(ss.TranslateFailed):
        ss.translate_texts(["a", "b"], "eng", "chi",
                           http_post=lambda u, h, p: {"translations": ["only-one"]})


# ---- 双语轨结构 ----

def test_bilingual_vtt_pairs_line_by_line():
    vtt = ss.build_bilingual_vtt(SRT, ["你好", "世界"], "eng", "chi")
    assert vtt.startswith("WEBVTT")
    assert "Hello\n你好" in vtt
    assert "World\n世界" in vtt


def test_bilingual_cue_mismatch_refuses():
    with pytest.raises(ss.CueMismatch):
        ss.build_bilingual_vtt(SRT, ["只有一条"], "eng", "chi")


def test_parse_cues_ignores_blocks_without_timestamp():
    cues = ss.parse_cues("WEBVTT\n\nNOTE no timestamp here\n\n"
                         "00:00:01.000 --> 00:00:02.000\nHi\n")
    assert [c["text"] for c in cues] == ["Hi"]


# ---- 开关与缓存区写盘 ----

def test_disabled_generate_refuses(monkeypatch, tmp_path):
    _disable(monkeypatch)
    with pytest.raises(ss.SmartSubtitleDisabled):
        ss.generate_bilingual_track(7, SRT, "eng", "chi",
                                    translations=["你好", "世界"],
                                    cache_dir=str(tmp_path))


def test_generate_writes_only_into_cache_dir(monkeypatch, tmp_path):
    _enable(monkeypatch)
    result = ss.generate_bilingual_track(7, SRT, "eng", "chi",
                                         translations=["你好", "世界"],
                                         cache_dir=str(tmp_path))
    assert result["cue_count"] == 2
    assert "/" not in result["filename"]
    written = tmp_path / "smart" / result["filename"]
    assert written.exists()
    assert "Hello\n你好" in written.read_text(encoding="utf-8")
    # 盘内媒体路径绝不被写：缓存区外无任何新文件
    assert list(tmp_path.rglob("*.vtt")) == [written]


def test_cache_path_rejects_bad_lang(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        ss.bilingual_cache_path(1, "en;rm", "chi", cache_dir=str(tmp_path))


# ---- 路由门禁 ----

def _client(monkeypatch, scope_allowed=True):
    from backend.api import smart_subtitles as router_mod
    from backend.api.user import get_current_user
    from backend.database import get_db
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    em.MediaItem.__table__.create(engine, checkfirst=True)
    # 路由现经桥接读 system_configs（面板行优先、缺行回退 env），夹具须建该表
    from backend import models as _core_models
    _core_models.SystemConfig.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()

    class _Scope:
        @staticmethod
        def item_allowed(db, user, item):
            return scope_allowed

    monkeypatch.setattr(router_mod, "library_scope", _Scope)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: type(
        "U", (), {"id": 1, "is_staff": False})()
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app), session


def test_router_disabled_returns_403_before_db(monkeypatch):
    _disable(monkeypatch)
    client, _ = _client(monkeypatch)
    r = client.post("/api/user/smart-subtitles/1/match",
                    json={"candidates": ["Movie.2024.chi.srt"]})
    assert r.status_code == 403


def test_router_missing_item_returns_404(monkeypatch):
    _enable(monkeypatch)
    client, _ = _client(monkeypatch)
    r = client.post("/api/user/smart-subtitles/999/match",
                    json={"candidates": []})
    assert r.status_code == 404


def test_router_invisible_item_returns_403(monkeypatch):
    _enable(monkeypatch)
    client, session = _client(monkeypatch, scope_allowed=False)
    session.add(em.MediaItem(id=5, guid="g" * 32, library_id=1,
                             item_type="movie", name="Movie.2024.mkv"))
    session.commit()
    r = client.post("/api/user/smart-subtitles/5/match",
                    json={"candidates": []})
    assert r.status_code == 403


def test_router_bilingual_happy_path(monkeypatch):
    _enable(monkeypatch)
    client, session = _client(monkeypatch, scope_allowed=True)
    session.add(em.MediaItem(id=6, guid="h" * 32, library_id=1,
                             item_type="movie", name="Movie.2024.mkv"))
    session.commit()
    r = client.post("/api/user/smart-subtitles/6/bilingual",
                    json={"source_text": SRT, "source_lang": "eng",
                          "target_lang": "chi",
                          "translations": ["你好", "世界"]})
    assert r.status_code == 200, r.text
    assert r.json()["cue_count"] == 2
    assert r.json()["filename"].endswith(".vtt")


# ---- R4 修复：匹配基准名 + 语言码先验 ----

def test_router_match_prefers_file_basename_over_display_name(monkeypatch):
    # 展示名经改写后与文件名语言不同时，exact sidecar 也必须对得上；
    # 此前路由只传展示名，静默返回空列表。
    _enable(monkeypatch)
    client, session = _client(monkeypatch, scope_allowed=True)
    session.add(em.MediaItem(id=11, guid="k" * 32, library_id=1,
                             item_type="movie", name="流浪地球",
                             file_path="mount://3/Movies/The.Wandering.Earth.2019.mkv"))
    session.commit()
    r = client.post("/api/user/smart-subtitles/11/match",
                    json={"candidates": ["The.Wandering.Earth.2019.chi.srt"]})
    assert r.status_code == 200, r.text
    assert [o["name"] for o in r.json()["matched"]] == [
        "The.Wandering.Earth.2019.chi.srt"]


def test_router_match_falls_back_to_display_name_without_file_path(monkeypatch):
    _enable(monkeypatch)
    client, session = _client(monkeypatch, scope_allowed=True)
    session.add(em.MediaItem(id=12, guid="m" * 32, library_id=1,
                             item_type="movie", name="Movie.2024.mkv",
                             file_path=""))
    session.commit()
    r = client.post("/api/user/smart-subtitles/12/match",
                    json={"candidates": ["Movie.2024.chi.srt"]})
    assert r.status_code == 200, r.text
    assert [o["name"] for o in r.json()["matched"]] == ["Movie.2024.chi.srt"]


def test_generate_bad_lang_does_not_call_provider(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_PROVIDER", "custom")
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_ENDPOINT", "https://example.invalid/t")
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_API_KEY", "dummy-test-key")
    calls = []

    def _fake_post(url, headers, payload):
        calls.append(url)
        return {"translations": ["你好", "世界"]}

    with pytest.raises(ValueError, match="语言码"):
        ss.generate_bilingual_track(9, SRT, "ENG", "chi",
                                    http_post=_fake_post, cache_dir=str(tmp_path))
    assert calls == []


def test_generate_valid_lang_still_calls_provider(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_PROVIDER", "custom")
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_ENDPOINT", "https://example.invalid/t")
    monkeypatch.setenv("SMART_SUBTITLE_TRANSLATE_API_KEY", "dummy-test-key")
    calls = []

    def _fake_post(url, headers, payload):
        calls.append(url)
        return {"translations": ["你好", "世界"]}

    out = ss.generate_bilingual_track(9, SRT, "eng", "chi",
                                      http_post=_fake_post, cache_dir=str(tmp_path))
    assert out["cue_count"] == 2
    assert calls == ["https://example.invalid/t"]

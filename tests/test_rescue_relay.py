"""救援链产品化（2026-10-20）：原名接力 + Bangumi 真人剧类型 + IMDb 桥。

背景（人工救援复盘）：TMDB 按中文名搜不中的剧，卡在三处——
1. Bangumi 腿只收动画（type 2），真人剧集是 type 6，被结构性过滤；
2. 各源知道原名/IMDb 编号，却没人把它们再喂给 TMDB 拿 tmdb_id；
3. TVMaze 命中把 IMDb 编号丢了，桥搭不起来。
全部离线：网络一律替身。
"""
from __future__ import annotations

import io
import json

import pytest

from backend.emby_server import altmeta
from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import engine as ms_engine
from backend.emby_server.metasources import sources as ms_sources


# ---------------------------------------------------------------------------
# 1. altmeta.search_bangumi：真人剧（type 6）要收，且带回原名
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _bgm_payload(*items):
    return {"list": list(items)}


def _bgm_item(type_, name, name_cn, air="2023-01-06"):
    return {"id": 404804, "type": type_, "name": name, "name_cn": name_cn,
            "air_date": air, "images": {"large": "https://img.example/x.jpg"},
            "url": "https://bgm.tv/subject/404804"}


def test_bangumi_accepts_live_action_type6_and_returns_original(monkeypatch):
    payload = _bgm_payload(_bgm_item(6, "법쩐", "法钱"))
    monkeypatch.setattr(altmeta.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(payload))
    hit = altmeta.search_bangumi("法钱", 2023, "series", min_interval=0.0)
    assert hit is not None
    assert hit["title"] == "法钱"
    assert hit["original_title"] == "법쩐"


def test_bangumi_still_rejects_other_series_works(monkeypatch):
    # 同名异作（舞台剧 type 4 之类不在收录类型内）与名字不全等的都要拒
    payload = _bgm_payload(_bgm_item(6, "법쩐2", "法钱2"))
    monkeypatch.setattr(altmeta.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(payload))
    assert altmeta.search_bangumi("法钱", 2023, "series", min_interval=0.0) is None


# ---------------------------------------------------------------------------
# 2. sources 层：Bangumi 的 Hit 带原名；TVMaze 的 Hit 带 IMDb
# ---------------------------------------------------------------------------

def test_bangumi_hit_carries_original_title(monkeypatch):
    monkeypatch.setattr(altmeta, "search_bangumi",
                        lambda title, year, kind, min_interval=0.0: {
                            "id": "404804", "title": "法钱",
                            "original_title": "법쩐", "year": "2023",
                            "image": "https://img.example/x.jpg"})
    hit = ms_sources.search_bangumi("法钱", 2023, "series",
                                    pool=None, gate=_Gate())
    assert hit is not None
    assert hit.original_title == "법쩐"


class _Gate:
    def acquire(self):
        return None


def test_tvmaze_hit_carries_imdb(monkeypatch):
    show = {"id": 426189, "name": "Payback", "premiered": "2023-01-06",
            "summary": "<p>x</p>", "genres": [], "rating": {"average": 8},
            "image": {"original": "https://img.example/p.jpg"},
            "externals": {"imdb": "tt23860034"}}
    monkeypatch.setattr(ms_sources, "_http_json", lambda url: [{"show": show}])
    hit = ms_sources.search_tvmaze("Payback", 2023, "series",
                                   pool=None, gate=_Gate())
    assert hit is not None
    assert hit.external_ids.get("imdb") == "tt23860034"


# ---------------------------------------------------------------------------
# 3. find_tmdb_by_imdb：年份门
# ---------------------------------------------------------------------------

def test_find_by_imdb_year_gate(monkeypatch):
    from backend.emby_server import tmdb as tmdb_lib

    class _Client:
        configured = True

        @staticmethod
        def find_by_imdb(imdb_id):
            return {"tv_results": [
                {"id": 111, "name": "重拍版", "first_air_date": "1999-01-01"},
                {"id": 222, "name": "正版", "first_air_date": "2023-01-06"},
            ]}

    monkeypatch.setattr(tmdb_lib, "tmdb_client", _Client())
    monkeypatch.setattr(tmdb_lib, "image_base", lambda: "https://img.example", raising=False)
    hit = ms_sources.find_tmdb_by_imdb("tt23860034", 2023, "series",
                                       pool=None, gate=_Gate())
    assert hit is not None
    assert hit.external_ids.get("tmdb") == "222"


# ---------------------------------------------------------------------------
# 4. 引擎接力：原名桥 / IMDb 桥 / 不重复接力
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_registry():
    original = dict(ms_sources.SPEC_BY_ID)
    yield
    ms_sources.SPEC_BY_ID.clear()
    ms_sources.SPEC_BY_ID.update(original)


def _spec(source_id, label, search):
    spec = ms_sources.SourceSpec(source_id, label, False, "en", "测试源", search)
    ms_sources.SPEC_BY_ID[source_id] = spec
    return spec


def _snapshot(*specs):
    return ms_config.Snapshot(
        enabled=True, prefer_chinese=True,
        sources=[ms_config.SourceConfig(
            id=s.id, label=s.label, enabled=True, requires_key=False,
            lang=s.lang, keys=[], rate=0.0) for s in specs])


def _hit(**kwargs):
    base = dict(source="x", title="", original_title="", overview="", genres=[],
                rating=None, year=None, poster=None, lang="en",
                external_ids={}, kind_hint="")
    base.update(kwargs)
    return ms_sources.Hit(**base)


def test_relay_original_title_finds_tmdb():
    calls = []

    def bgm_search(title, year, kind, *, pool, gate):
        return _hit(source="bangumi", title="法钱", original_title="법쩐",
                    year=2023, lang="zh", external_ids={"bangumi": "404804"})

    def tmdb_search(title, year, kind, *, pool, gate):
        calls.append(title)
        if title == "법쩐":
            return _hit(source="tmdb", title="权贵复仇", year=2023,
                        poster="https://img.example/p.jpg",
                        external_ids={"tmdb": "209751"})
        return None

    snap = _snapshot(_spec("bangumi", "Bangumi", bgm_search),
                     _spec("tmdb", "TMDB", tmdb_search))
    result = ms_engine.collect(None, "法钱", 2023, "series", snapshot=snap)
    assert result.external_ids.get("tmdb") == "209751"
    assert calls == ["法钱", "법쩐"]
    assert any("原名接力" in (o.label or "") for o in result.outcomes)


def test_relay_not_used_when_tmdb_already_hit():
    calls = []

    def tmdb_search(title, year, kind, *, pool, gate):
        calls.append(title)
        return _hit(source="tmdb", title="权贵复仇", year=2023,
                    external_ids={"tmdb": "209751"})

    def bgm_search(title, year, kind, *, pool, gate):
        return _hit(source="bangumi", title="法钱", original_title="법쩐",
                    external_ids={"bangumi": "404804"})

    snap = _snapshot(_spec("tmdb", "TMDB", tmdb_search),
                     _spec("bangumi", "Bangumi", bgm_search))
    result = ms_engine.collect(None, "法钱", 2023, "series", snapshot=snap)
    assert result.external_ids.get("tmdb") == "209751"
    assert calls == ["法钱"]  # 只问了一次，没接力


def test_relay_imdb_bridge(monkeypatch):
    def tvmaze_search(title, year, kind, *, pool, gate):
        return _hit(source="tvmaze", title="Payback", year=2023,
                    external_ids={"tvmaze": "1", "imdb": "tt23860034"})

    def tmdb_search(title, year, kind, *, pool, gate):
        return None

    monkeypatch.setattr(
        ms_sources, "find_tmdb_by_imdb",
        lambda imdb, year, kind, *, pool, gate: _hit(
            source="tmdb", title="权贵复仇", year=2023,
            external_ids={"tmdb": "209751"}))
    snap = _snapshot(_spec("tvmaze", "TVMaze", tvmaze_search),
                     _spec("tmdb", "TMDB", tmdb_search))
    result = ms_engine.collect(None, "法钱", 2023, "series", snapshot=snap)
    assert result.external_ids.get("tmdb") == "209751"
    assert any("IMDb 接力" in (o.label or "") for o in result.outcomes)


def test_relay_no_original_no_imdb_stays_unbound():
    def bgm_search(title, year, kind, *, pool, gate):
        return _hit(source="bangumi", title="法钱", year=2023,
                    external_ids={"bangumi": "404804"})

    def tmdb_search(title, year, kind, *, pool, gate):
        return None

    snap = _snapshot(_spec("bangumi", "Bangumi", bgm_search),
                     _spec("tmdb", "TMDB", tmdb_search))
    result = ms_engine.collect(None, "法钱", 2023, "series", snapshot=snap)
    assert "tmdb" not in result.external_ids

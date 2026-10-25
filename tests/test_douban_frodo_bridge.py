"""豆瓣 Frodo 桥（批二 #14）：IMDb 编号对齐 + Frodo 文案 + 熔断隔离。

关键不变量（对应任务书的灵魂验收线）：
1. 桥以 IMDb 编号精确对齐——有编号走编号；没有可信编号**不桥**，
   绝不退化成拿片名模糊搜当主路；
2. 豆瓣腿失败只熔断自己（``douban_frodo`` 源名），TMDB 路径不受影响；
3. 搬回的文案走多源归并的既有口径，只填空、永不覆盖；
4. Frodo 超时 / 失败不挂死 enrich——采集照常返回其它源的结果。
"""
import json
import os
import urllib.error
from types import SimpleNamespace

# 直调 metasources 引擎（会连带 import tmdb 等模块）：先钉死 sqlite，
# 与其它 DB 相关测试文件同一 guard 口径。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server.metasources import breaker, config as ms_config
from backend.emby_server.metasources import douban_frodo, engine, sources

IMDB_ID = "tt0111161"
DOUBAN_ID = "1292052"

_V2_PAYLOAD = {
    "id": DOUBAN_ID,
    "title": "肖申克的救赎",
    "original_title": "The Shawshank Redemption",
    "year": "1994",
    "subtype": "movie",
    "rating": {"max": 10, "average": 9.7, "min": 0},
    "summary": "希望让人自由。",
    "images": {"large": "https://img.example/shawshank.jpg"},
    "genres": ["犯罪", "剧情"],
}

_FRODO_DETAIL = {
    "id": DOUBAN_ID,
    "title": "肖申克的救赎",
    "original_title": "The Shawshank Redemption",
    "year": "1994",
    "subtype": "movie",
    "intro": "20世纪40年代末，银行家安迪蒙冤入狱。",
    "rating": {"average": 9.7, "count": 2000000},
    "pic": {"large": "https://img.example/shawshank-frodo.jpg", "normal": ""},
    "genres": ["犯罪", "剧情"],
}

_FRODO_SEARCH = {
    "count": 1, "start": 0, "total": 1,
    "items": [{
        "target_id": DOUBAN_ID, "target_type": "movie",
        "target": {
            "id": DOUBAN_ID, "title": "肖申克的救赎", "year": "1994",
            "rating": {"average": 9.7},
            "pic": {"large": "https://img.example/shawshank-search.jpg"},
            "subtype": "movie",
        },
    }],
}


class _Resp:
    def __init__(self, payload):
        self._body = (payload if isinstance(payload, bytes)
                      else json.dumps(payload).encode())

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    breaker.reset_all()
    monkeypatch.setenv("PROVIDER_BREAKER_ENABLED", "1")
    monkeypatch.setenv("PROVIDER_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("PROVIDER_BREAKER_COOLDOWN_SEC", "60")
    monkeypatch.setenv("PROVIDER_BREAKER_FAST_SEC", "60")
    monkeypatch.setenv("DOUBAN_FRODO_MIN_INTERVAL", "0")
    # 缓存落到临时目录，别污染开发机上的真实 TMDB 缓存目录
    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(tmp_path / "cache"))
    yield
    breaker.reset_all()


def _route(monkeypatch, handler):
    """把 urllib.request.urlopen 换成按 URL 分发的假实现，返回调用记录。"""
    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        return handler(url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


def _http_error(code):
    def handler(url):
        raise urllib.error.HTTPError(url, code, "Forbidden", {}, None)
    return handler


# ---------------------------------------------------------------------------
# IMDb 桥（主路）
# ---------------------------------------------------------------------------

def test_imdb_bridge_exact_hit(monkeypatch):
    """有 imdb_id → v2 imdb 端点精确换到豆瓣条目，文案字段齐全。"""
    def handler(url):
        if "api.douban.com/v2/movie/imdb/" in url:
            assert IMDB_ID in url
            return _Resp(_V2_PAYLOAD)
        raise AssertionError(f"不该请求的 URL: {url}")

    _route(monkeypatch, handler)
    found = douban_frodo.find_by_imdb(IMDB_ID, 1994, "movie")
    assert found is not None
    assert found["id"] == DOUBAN_ID
    assert found["title"] == "肖申克的救赎"
    assert found["overview"] == "希望让人自由。"
    assert found["rating"] == 9.7
    assert found["year"] == 1994
    assert found["image"] == "https://img.example/shawshank.jpg"
    assert found["genres"] == ["犯罪", "剧情"]


def test_imdb_bridge_fills_thin_fields_from_frodo_detail(monkeypatch):
    """v2 文案不全（无简介）时用 Frodo 详情按 id 补齐，只补空缺。"""
    thin = dict(_V2_PAYLOAD, summary="", rating={"average": 0})
    thin.pop("genres")

    def handler(url):
        if "api.douban.com/v2/movie/imdb/" in url:
            return _Resp(thin)
        if "frodo.douban.com/api/v2/movie/" in url:
            return _Resp(_FRODO_DETAIL)
        raise AssertionError(f"不该请求的 URL: {url}")

    _route(monkeypatch, handler)
    found = douban_frodo.find_by_imdb(IMDB_ID, 1994, "movie")
    assert found["overview"] == "20世纪40年代末，银行家安迪蒙冤入狱。"
    assert found["rating"] == 9.7          # v2 的 0 分是空值，被详情补上
    assert found["title"] == "肖申克的救赎"  # v2 已给的不被覆盖


def test_imdb_bridge_miss_and_year_mismatch(monkeypatch):
    """查无此编号 → None；年份都已知且不一致 → 判没找到（不乱认）。"""
    def handler(url):
        if "imdb/tt0000000" in url:
            return _Resp({"msg": "movie_not_found", "code": 6000})
        if f"imdb/{IMDB_ID}" in url:
            return _Resp(_V2_PAYLOAD)
        raise AssertionError(f"不该请求的 URL: {url}")

    _route(monkeypatch, handler)
    assert douban_frodo.find_by_imdb("tt0000000", None, "movie") is None
    assert douban_frodo.find_by_imdb(IMDB_ID, 2001, "movie") is None


def test_imdb_bridge_result_cached(monkeypatch):
    """桥接成功响应落信封缓存：第二次查同一编号不再发请求。"""
    calls = _route(monkeypatch, lambda url: _Resp(_V2_PAYLOAD))
    first = douban_frodo.find_by_imdb(IMDB_ID, 1994, "movie")
    second = douban_frodo.find_by_imdb(IMDB_ID, 1994, "movie")
    assert first == second
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 熔断隔离：豆瓣腿坏掉只熔断自己
# ---------------------------------------------------------------------------

def test_frodo_403_trips_only_its_own_breaker(monkeypatch):
    """Frodo 吃 403 → douban_frodo 熔断打开；TMDB 路径（对照组）不受影响。"""
    calls = _route(monkeypatch, _http_error(403))
    assert douban_frodo.search_by_title("肖申克的救赎", 1994, "movie") is None

    status = breaker.status()
    assert status["douban_frodo"]["state"] == "open"
    assert "tmdb" not in status                 # TMDB 一笔账都没记
    assert breaker.allow("tmdb") is True        # TMDB 路径照常放行

    # 熔断打开后第二次调用直接被挡下，不再真的发请求（不逐条硬撞）
    assert douban_frodo.search_by_title("肖申克的救赎", 1994, "movie") is None
    assert len(calls) == 1


def test_frodo_timeout_returns_none_and_counts_failure(monkeypatch):
    """Frodo 超时 → 返回 None、不挂死；失败记账但一次不到阈值不开断。"""
    def handler(url):
        raise urllib.error.URLError("timed out")

    _route(monkeypatch, handler)
    assert douban_frodo.find_by_imdb(IMDB_ID, 1994, "movie") is None
    state = breaker.status()["douban_frodo"]
    assert state["failures"] == 1
    assert state["state"] == "closed"


# ---------------------------------------------------------------------------
# Frodo 片名搜（兜底）：置信门槛
# ---------------------------------------------------------------------------

def test_frodo_title_search_hit_with_detail(monkeypatch):
    """片名搜命中且过门槛 → 取详情补齐简介。"""
    def handler(url):
        if "/api/v2/search" in url:
            return _Resp(_FRODO_SEARCH)
        if "/api/v2/movie/" in url:
            return _Resp(_FRODO_DETAIL)
        raise AssertionError(f"不该请求的 URL: {url}")

    _route(monkeypatch, handler)
    found = douban_frodo.search_by_title("肖申克的救赎", 1994, "movie")
    assert found["id"] == DOUBAN_ID
    assert found["overview"] == "20世纪40年代末，银行家安迪蒙冤入狱。"
    assert found["rating"] == 9.7


def test_frodo_title_search_rejects_inconsistent(monkeypatch):
    """标题对不上 / 年份对不上 / 类型对不上的候选一律不认（宁可漏）。"""
    wrong = {
        "items": [
            {"target_id": "1", "target_type": "movie",
             "target": {"id": "1", "title": "肖申克救赎之路", "year": "1994"}},
            {"target_id": "2", "target_type": "movie",
             "target": {"id": "2", "title": "肖申克的救赎", "year": "2010"}},
            {"target_id": "3", "target_type": "tv",
             "target": {"id": "3", "title": "肖申克的救赎", "year": "1994"}},
        ],
    }
    _route(monkeypatch, lambda url: _Resp(wrong))
    assert douban_frodo.search_by_title("肖申克的救赎", 1994, "movie") is None


# ---------------------------------------------------------------------------
# 引擎接入：IMDb 桥 + 只填空 + 超时隔离
# ---------------------------------------------------------------------------

def _snapshot():
    return ms_config.Snapshot(
        enabled=True, prefer_chinese=True,
        sources=[
            ms_config.SourceConfig(id="tvmaze", label="TVmaze", enabled=True,
                                   requires_key=False, lang="en"),
            ms_config.SourceConfig(id="douban", label="豆瓣", enabled=True,
                                   requires_key=False, lang="zh"),
        ])


def _patch_spec(monkeypatch, source_id, fn):
    monkeypatch.setattr(sources.SPEC_BY_ID[source_id], "search", fn)


def _tvmaze_hit_with_imdb(title, year, kind, *, pool, gate):
    return sources.Hit(source="tvmaze", title="肖申克的救赎",
                       external_ids={"imdb": IMDB_ID})


def _no_hit(title, year, kind, *, pool, gate):
    return None


def test_engine_bridges_imdb_to_douban(monkeypatch):
    """其它源带回可信 IMDb 编号、豆瓣源本身没命中 → 桥补上豆瓣 id 与文案。"""
    _patch_spec(monkeypatch, "tvmaze", _tvmaze_hit_with_imdb)
    _patch_spec(monkeypatch, "douban", _no_hit)
    _route(monkeypatch, lambda url: _Resp(_V2_PAYLOAD))

    result = engine.collect(None, "肖申克的救赎", 1994, "movie",
                            snapshot=_snapshot())
    assert result.external_ids.get("douban") == DOUBAN_ID
    assert result.fields.get("overview") == "希望让人自由。"
    assert result.fields.get("rating") == 9.7
    assert any(o.label == "豆瓣（IMDb 桥）" and o.hit for o in result.outcomes)


def test_engine_uses_items_own_imdb_id(monkeypatch):
    """条目自身已有 imdb_id（调用方传入）时直接桥，不依赖其它源带回。"""
    _patch_spec(monkeypatch, "tvmaze", _no_hit)
    _patch_spec(monkeypatch, "douban", _no_hit)
    _route(monkeypatch, lambda url: _Resp(_V2_PAYLOAD))

    result = engine.collect(None, "肖申克的救赎", 1994, "movie",
                            snapshot=_snapshot(), imdb_id=IMDB_ID)
    assert result.external_ids.get("douban") == DOUBAN_ID


def test_engine_no_imdb_no_bridge(monkeypatch):
    """没有可信 IMDb 编号时不桥——绝不拿片名去豆瓣模糊搜当主路。"""
    _patch_spec(monkeypatch, "tvmaze", _no_hit)
    _patch_spec(monkeypatch, "douban", _no_hit)
    calls = _route(monkeypatch, lambda url: _Resp(_V2_PAYLOAD))

    result = engine.collect(None, "肖申克的救赎", 1994, "movie",
                            snapshot=_snapshot())
    assert "douban" not in result.external_ids
    assert not any("IMDb 桥" in o.label for o in result.outcomes)
    assert calls == []      # 一次豆瓣请求都没发


def test_engine_untrusted_imdb_not_bridged(monkeypatch):
    """标题对不上的源带回的 IMDb 编号不可信，不许被桥转正成错绑。"""
    def lying_hit(title, year, kind, *, pool, gate):
        return sources.Hit(source="tvmaze", title="完全无关的另一部片",
                           external_ids={"imdb": IMDB_ID})

    _patch_spec(monkeypatch, "tvmaze", lying_hit)
    _patch_spec(monkeypatch, "douban", _no_hit)
    calls = _route(monkeypatch, lambda url: _Resp(_V2_PAYLOAD))

    result = engine.collect(None, "肖申克的救赎", 1994, "movie",
                            snapshot=_snapshot())
    assert "douban" not in result.external_ids
    assert calls == []


def test_engine_frodo_timeout_does_not_break_collect(monkeypatch):
    """桥请求超时：静默放弃桥，其它源已采到的结果原样返回。"""
    _patch_spec(monkeypatch, "tvmaze", _tvmaze_hit_with_imdb)
    _patch_spec(monkeypatch, "douban", _no_hit)

    def handler(url):
        raise urllib.error.URLError("timed out")

    _route(monkeypatch, handler)
    result = engine.collect(None, "肖申克的救赎", 1994, "movie",
                            snapshot=_snapshot())
    assert result.fields.get("title") == "肖申克的救赎"   # TVmaze 的结果还在
    assert "douban" not in result.external_ids


def test_apply_to_item_fill_only_never_overwrites():
    """归并落库口径：已有简介不被豆瓣文案覆盖，空缺的评分 / 类型照常补上。"""
    item = SimpleNamespace(
        name="肖申克的救赎", overview="已有的权威简介", genres="",
        community_rating=None, production_year=1994,
        poster_path=None, primary_image_url=None,
        external_ids="", tmdb_id=None, imdb_id=IMDB_ID, metadata_source=None)
    result = engine.CollectResult(prefer_chinese=True)
    result.fields = {"overview": "豆瓣的简介", "rating": 9.7,
                     "genres": ["犯罪", "剧情"]}
    result.external_ids = {"douban": DOUBAN_ID, "imdb": IMDB_ID}

    changed = engine.apply_to_item(item, result)
    assert item.overview == "已有的权威简介"          # 不覆盖
    assert item.community_rating == 9.7               # 填空
    assert item.genres == "犯罪,剧情"                  # 填空
    assert json.loads(item.external_ids)["douban"] == DOUBAN_ID
    assert "overview" not in changed

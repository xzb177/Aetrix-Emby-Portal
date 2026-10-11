"""管理后台 · 元数据与刮削

- TMDB Key 来源优先级：环境变量 > SystemConfig；保存后 refresh_keys 立即生效
- 密钥轮询：401 掉线后换下一个，耗尽返回 False
- 界面/日志不泄露原文（masked_keys 只露后 4 位）
- 条目级重刮幂等：重复调用无字段变更
"""
import threading
from types import SimpleNamespace

import pytest

from backend.api import admin_scrape
from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.tmdb import TmdbClient, _split_keys


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    TmdbClient._cache.clear()
    # httpx 在构造 Client 时读取代理环境变量；CI/本地代理 URL 格式异常会导致
    # 与本测试无关的 InvalidURL 误报，这里先清掉
    for _var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(_var, raising=False)
    yield
    TmdbClient._cache.clear()


def _client_no_env(monkeypatch) -> TmdbClient:
    monkeypatch.delenv("TMDB_API_KEYS", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(tmdb_mod, "_read_config_value", lambda db: "")
    return TmdbClient()


# ---------- _split_keys ----------

def test_split_keys_multi_separators():
    assert _split_keys("a,b\nc d;e，f；g") == ["a", "b", "c", "d", "e", "f", "g"]


def test_split_keys_dedup_and_strip():
    assert _split_keys(" a , a ,\n b ") == ["a", "b"]


def test_split_keys_empty():
    assert _split_keys("") == []
    assert _split_keys(None) == []


# ---------- 来源优先级 ----------

def test_env_wins_over_db(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEYS", "env1,env2")
    monkeypatch.setattr(tmdb_mod, "_db_keys", lambda db=None: ["db1"])
    client = TmdbClient()
    assert client.key_source == "env"
    assert client.api_keys == ["env1", "env2"]
    assert client.configured


def test_db_fallback_when_env_empty(monkeypatch):
    monkeypatch.delenv("TMDB_API_KEYS", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(tmdb_mod, "_read_config_value", lambda db: "db1\ndb2")
    client = TmdbClient()
    assert client.key_source == "db"
    assert client.api_keys == ["db1", "db2"]
    assert client.configured


def test_none_when_nothing_configured(monkeypatch):
    client = _client_no_env(monkeypatch)
    assert client.key_source == "none"
    assert client.api_keys == []
    assert not client.configured


def test_refresh_keys_hot_reload(monkeypatch):
    """保存即生效：先无 key，DB 有 key 后 refresh_keys() 直接生效，无需重启"""
    client = _client_no_env(monkeypatch)
    assert not client.configured
    monkeypatch.setattr(tmdb_mod, "_read_config_value", lambda db: "newkey")
    info = client.refresh_keys()
    assert info == {"source": "db", "count": 1}
    assert client.configured
    assert client.api_key == "newkey"


def test_masked_keys_never_leak_raw(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "supersecretkey123")
    client = TmdbClient()
    masked = client.masked_keys()
    assert masked == ["****y123"]
    assert "supersecretkey123" not in "".join(masked)


# ---------- 密钥轮询 ----------

def test_rotate_cycles_keys():
    client = TmdbClient.__new__(TmdbClient)  # 不走 __init__（无网络/DB 副作用）
    client._keys_lock = threading.Lock()
    client._set_keys(["k1", "k2"], "env")
    assert client._rotate() is True
    assert client.api_key == "k2"
    assert client._rotate() is True
    assert client.api_key == "k1"  # 循环


def _bare_client(*keys) -> TmdbClient:
    """绕过 __init__ 的裸客户端：只给 _get 需要的那几个字段

    v2.42.9 起 _get 先过请求级令牌桶，所以 _limiter/_stats 要按 __init__ 的口径一并补上；
    这些用例走假会话，速率调快以免真的等。
    """
    client = TmdbClient.__new__(TmdbClient)
    client._keys_lock = threading.Lock()
    client._set_keys(list(keys), "env")
    client._limiter = tmdb_mod._RequestLimiter(rate=1_000_000.0, min_rate=1.0)
    client._stats = {"short_circuit": 0, "retry": 0, "net_fail": 0}
    return client


def test_get_skips_invalid_key(monkeypatch):
    """401 的 key 被跳过：_get 用下一个 key 重试并返回成功结果"""
    client = _bare_client("bad", "good")
    calls = []

    def fake_get(url, params):
        calls.append(params["api_key"])
        if params["api_key"] == "bad":
            return SimpleNamespace(status_code=401, json=lambda: {})
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    client.session = SimpleNamespace(get=fake_get)
    monkeypatch.setattr(client, "_ensure_session", lambda: None)
    assert client._get("/configuration", {}) == {"ok": True}
    assert calls == ["bad", "good"]


def test_get_all_keys_bad_returns_none(monkeypatch):
    client = _bare_client("only")
    client.session = SimpleNamespace(
        get=lambda url, params: SimpleNamespace(status_code=401, json=lambda: {}))
    monkeypatch.setattr(client, "_ensure_session", lambda: None)
    assert client._get("/configuration", {}) is None


# ---------- 条目级重刮幂等 ----------

NFO_DATA = {
    "title": "千与千寻",
    "originaltitle": "千と千尋の神隠し",
    "plot": "少女千寻误入神灵世界……",
    "year": 2001,
    "genres": ["动画"],
    "studios": ["吉卜力"],
    "tmdb_id": "129",
    "imdb_id": "tt0245429",
    "rating": 8.5,
    "mpaa": "PG",
}

DETAILS = {
    "poster_path": "/p.jpg",
    "backdrop_path": "/b.jpg",
    "external_ids": {"imdb_id": "tt0245429"},
    "alternative_titles": {"titles": [{"title": "Spirited Away"}]},
    "title": "千与千寻",
}


def _movie_item() -> SimpleNamespace:
    return SimpleNamespace(
        id=7, item_type="movie", name="旧名", original_title="", sort_name="",
        overview="", tagline="", production_year=None, community_rating=None,
        official_rating="", genres="", tags="", studios="", platforms="",
        tmdb_id="", imdb_id="", aliases="", file_path="/media/a.mkv",
        container="", poster_path="", backdrop_path="",
        primary_image_url="", backdrop_image_url="", last_scraped_at=None,
    )
# ---------- TMDB 候选搜索（Emby 式手动识别） ----------

_SERIES_HIT = {
    "id": 100, "name": "黑鸟", "first_air_date": "2022-07-08",
    "overview": "简介" * 100, "poster_path": "/abc.jpg", "media_type": "tv",
}
_MOVIE_HIT = {
    "id": 200, "title": "黑鸟行动", "release_date": "2021-05-01",
    "overview": "电影简介", "poster_path": None, "media_type": "movie",
}
_SERIES_HIT_2020 = {
    "id": 101, "name": "黑鸟", "first_air_date": "2020-01-01",
    "overview": "旧版", "poster_path": "/old.jpg", "media_type": "tv",
}


def _mock_search(monkeypatch, hits=None, exc=None):
    def _fake(name, kind, limit=10):
        if exc:
            raise exc
        assert limit == 10, "手动识别默认拉 10 个候选"
        return [dict(h) for h in (hits or [])]
    monkeypatch.setattr(admin_scrape.tmdb_client, "search_candidates", _fake)


def test_tmdb_search_candidate_shape(monkeypatch):
    """返回格式：tmdb_id / title / year（取前4位）/ overview（截200字）/ poster_path / media_type"""
    _mock_search(monkeypatch, [_SERIES_HIT])
    res = admin_scrape.search_tmdb_candidates(q="黑鸟")
    cands = res["candidates"]
    assert len(cands) == 1
    c = cands[0]
    assert c["tmdb_id"] == 100
    assert c["title"] == "黑鸟"
    assert c["year"] == "2022"
    assert c["overview"] == "简介" * 100
    assert len(c["overview"]) == 200
    assert c["poster_path"] == "/abc.jpg"
    assert c["media_type"] == "tv"


def test_tmdb_search_movie_kind(monkeypatch):
    """kind=movie 时标题取 title、年份取 release_date"""
    _mock_search(monkeypatch, [_MOVIE_HIT])
    res = admin_scrape.search_tmdb_candidates(q="黑鸟", kind="movie")
    c = res["candidates"][0]
    assert c["title"] == "黑鸟行动"
    assert c["year"] == "2021"
    assert c["poster_path"] is None
    assert c["media_type"] == "movie"


def test_tmdb_search_year_filter(monkeypatch):
    """给了年份只留年份一致的候选（重名翻拍就靠它区分）"""
    _mock_search(monkeypatch, [_SERIES_HIT, _SERIES_HIT_2020])
    res = admin_scrape.search_tmdb_candidates(q="黑鸟", year=2020)
    assert [c["tmdb_id"] for c in res["candidates"]] == [101]


def test_tmdb_search_failure_returns_empty(monkeypatch):
    """TMDB 抖动/抛异常：返回空列表，不 500"""
    _mock_search(monkeypatch, exc=RuntimeError("boom"))
    res = admin_scrape.search_tmdb_candidates(q="黑鸟")
    assert res == {"candidates": []}


def test_tmdb_search_empty_q_returns_empty(monkeypatch):
    """空关键字：返回空列表，不打 TMDB"""
    called = []
    monkeypatch.setattr(
        admin_scrape.tmdb_client, "search_candidates",
        lambda *a, **k: called.append(1) or [],
    )
    assert admin_scrape.search_tmdb_candidates(q="   ") == {"candidates": []}
    assert called == []

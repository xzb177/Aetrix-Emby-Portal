"""TMDB 请求级限流 / 429 退避 / Tier 2 短路（v2.42.9 刮削收口）

这三件事都是不碰网络、不碰数据库的纯逻辑，但它们决定了 4.2 万条补全积压能不能排空：

- **桶的口径从「条目/秒」改成「请求/秒」。** 旧桶在 enrich_worker 里按条目扣 token，
  而一个条目背后是 0~6 次 HTTP（中文标题 4~5 个候选搜索最贵），于是配置里的
  ``ENRICH_TMDB_PER_SEC=2`` 实际打出去 4~12 请求/秒；而且扫描那条路
  （``scanner._tmdb_work``）**完全没有限速**。现在只有一个桶，两条路共用。
- **429 真的读 ``Retry-After``。** 旧实现全文没有读过这个头，单 key 时直接放弃并
  把条目记失败；现在按该头退避（有上限），并把速率自适应减半，避免后续条目在同一个
  配额窗口里继续撞。
- **网络异常重试。** 旧实现一次抖动就放弃这个条目（要等下一轮补全才回来），
  httpx 的 transport retries 又只管连接建立失败，读超时全落在 ``_request`` 里重试。
- **Tier 2 短路。** ``search()`` 以前要把 4~5 个候选全打完才收手，即使第一个候选
  已经是「归一化后精确相等」。Tier 2 永远压过 Tier 1，后面的候选最多只能换来
  「更长的精确变体」这一个 rank 的差别，不值得再打 1~4 次 HTTP。

这些用例都在假会话上跑，不发真请求。
"""
from types import SimpleNamespace

import pytest

from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.tmdb import TmdbClient, TmdbTransientError


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """每个用例都从空缓存开始（``_cache`` 是类级 dict，整个进程共享）

    磁盘缓存（第 7 批）也一并关掉：本文件数的是**真实的 HTTP 次数**，
    磁盘命中（跨用例、甚至跨进程残留）会让计数断言失去确定性。
    磁盘缓存自身的正确性由 tests/test_tmdb_cache.py 守。
    """
    monkeypatch.setenv("EMBY_TMDB_CACHE", "0")
    TmdbClient._cache.clear()
    yield
    TmdbClient._cache.clear()


@pytest.fixture
def sleeps(monkeypatch):
    """拦掉真实 sleep 并记录秒数——这些用例要看的是「睡多久」，不是真睡

    注意 ``tmdb_mod.time`` 就是标准库 ``time``：``acquire()`` 内部的等待也会一起被打桩，
    所以用到本 fixture 的用例都把限速器调快（见 ``_client`` 的 ``rate``），
    否则空转轮询要等到真实时间过去才退出。
    """
    recorded: list = []
    monkeypatch.setattr(tmdb_mod.time, "sleep", lambda s: recorded.append(s))
    return recorded


class _Session:
    """假会话：记录每次请求的 (url, params)，结果由 answer 决定"""

    def __init__(self, answer):
        self.calls: list = []
        self._answer = answer

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        return self._answer(url, params)


def _resp(status=200, payload=None, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: ({} if payload is None else payload))


def _client(monkeypatch, keys=("k1",), answer=None, rate=1_000_000.0) -> TmdbClient:
    """不带 env key 的客户端 + 假会话（不重建 httpx client，也不碰真实密钥）"""
    monkeypatch.delenv("TMDB_API_KEYS", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(tmdb_mod, "_read_config_value", lambda db: "")
    client = TmdbClient()
    client._set_keys(list(keys), "env")
    # 默认把桶调快（几十个请求都不必等）；429 那几条显式传 rate=2.0 才看得清减半
    client._limiter = tmdb_mod._RequestLimiter(rate=rate, min_rate=min(rate, 1.0))
    if answer is not None:
        client.session = _Session(answer)
    monkeypatch.setattr(client, "_ensure_session", lambda: None)
    return client


# ==================== 令牌桶：口径是请求，不是条目 ====================

def test_every_http_request_consumes_a_token(monkeypatch):
    """一个条目打了几次 HTTP 就扣几个 token（旧口径是整条目只扣一个）"""
    # 语言 fallback 会放大请求数，测试"请求级"口径时固定为单语言
    monkeypatch.setattr(tmdb_mod, "MOVIEDB_FALLBACK_LANGUAGES", ("zh-CN",))
    client = _client(monkeypatch, answer=lambda url, params: _resp(payload={"results": []}))
    acquires: list = []
    original = client._limiter.acquire

    def counting():
        acquires.append(1)
        original()

    monkeypatch.setattr(client._limiter, "acquire", counting)

    name = "进击的巨人 (2013) 中字"
    candidates = tmdb_mod._search_candidates(name, 2013)
    assert len(candidates) >= 3, "候选塌缩了就测不出「请求级」这个口径"
    client.search(name, 2013, "series")

    # 年份回退：带年份搜出空结果会去年份再搜一轮，所以是 2 倍
    assert len(client.session.calls) == len(candidates) * 2
    assert len(acquires) == len(candidates) * 2   # 请求 = token，一一对应


def test_throttle_halves_the_rate_and_floors_at_min_rate():
    limiter = tmdb_mod._RequestLimiter(rate=4.0, min_rate=1.0)
    assert limiter.rate == 4.0
    for _ in range(4):
        limiter.note_throttled(None)
    assert limiter.rate == 1.0            # 4→2→1，下限处停住
    assert limiter.throttled_count == 4


def test_throttle_wait_is_capped_by_retry_after_cap():
    limiter = tmdb_mod._RequestLimiter(rate=4.0, min_rate=1.0)
    assert limiter.note_throttled(12.0) == 12.0
    # 后台 worker 睡太久会白白占着线程：超过上限就按上限退避
    assert limiter.note_throttled(9999.0) == tmdb_mod.TMDB_RETRY_AFTER_CAP_SEC
    # 没给（或给了 0）：按新速率等一个 token 的时间，别直接转圈
    wait = limiter.note_throttled(None)
    assert 0 < wait <= tmdb_mod.TMDB_RETRY_AFTER_CAP_SEC


def test_rate_recovers_after_a_streak_of_successes():
    limiter = tmdb_mod._RequestLimiter(rate=4.0, min_rate=1.0)
    limiter.note_throttled(1.0)
    assert limiter.rate == 2.0
    for _ in range(tmdb_mod.TMDB_RECOVER_AFTER):
        limiter.note_success()
    assert 2.0 < limiter.rate <= 4.0      # +25%，且不超配置上限


@pytest.mark.parametrize("raw,expected", [
    ("12", 12.0),
    ("", None),
    ("garbage", None),
])
def test_retry_after_seconds_parsing(raw, expected):
    assert tmdb_mod._retry_after_seconds(_resp(429, headers={"retry-after": raw})) == expected


def test_retry_after_seconds_accepts_http_date():
    far = tmdb_mod._retry_after_seconds(
        _resp(429, headers={"retry-after": "Wed, 01 Oct 2031 00:00:00 GMT"}))
    near = tmdb_mod._retry_after_seconds(
        _resp(429, headers={"retry-after": "Thu, 01 Jan 1970 00:00:00 GMT"}))
    assert far is not None and far > 0
    assert near == 0.0        # 已经过去的时间点 = 不用等


# ==================== Tier 2 短路 ====================

HIT = {"id": 1429, "name": "进击的巨人", "original_name": "進撃の巨人",
       "vote_average": 8.7}


def _search_client(monkeypatch, hit=HIT, rate=1_000_000.0) -> TmdbClient:
    return _client(monkeypatch, answer=lambda url, params: _resp(payload={"results": [hit]}),
                   rate=rate)


def test_exact_hit_stops_the_candidate_loop(monkeypatch):
    """第一个候选就是 Tier 2（归一化后精确相等）→ 后面 2 个候选不再打"""
    client = _search_client(monkeypatch)
    name = "进击的巨人 (2013) 中字"
    assert len(tmdb_mod._search_candidates(name)) >= 3

    assert client.search(name, 2013, "series") == HIT
    assert len(client.session.calls) == 1
    assert client.stats()["short_circuits"] == 1
    # 同时记进阶段计数：管理端「补全进度」面板能直接看到短路省下的候选
    assert tmdb_mod.progress.stage_stats()["tmdb_search_short"]["count"] >= 1


def test_no_exact_hit_still_tries_every_candidate(monkeypatch):
    """没有精确命中就不能收手（模糊命中也得比完，否则会漏掉更好的结果）"""
    monkeypatch.setattr(tmdb_mod, "MOVIEDB_FALLBACK_LANGUAGES", ("zh-CN",))
    client = _search_client(monkeypatch, hit={"id": 9, "name": "毫不相干的条目"})
    name = "进击的巨人 (2013) 中字"

    assert client.search(name, 2013, "series") is None
    assert len(client.session.calls) == len(tmdb_mod._search_candidates(name, 2013))
    assert client.stats()["short_circuits"] == 0


# ==================== 429：退避、换 key ====================

def test_single_key_429_reads_retry_after_and_backs_off(monkeypatch, sleeps):
    """单 key 429：按 Retry-After 退避 + 速率减半，然后抛瞬态（转重试队列）

    v2.53.0 前这里返回 ``None``——与「真搜不到」不可区分，条目被写成终态
    none（问题一）。退避与减半语义不变，只把结局换成可重试异常。
    """
    client = _client(monkeypatch,
                     answer=lambda url, params: _resp(429, headers={"retry-after": "12"}),
                     rate=2.0)
    assert client._limiter.rate == 2.0

    with pytest.raises(TmdbTransientError):
        client._get("/search/tv", {"query": "x"})
    assert client.stats()["throttled"] == 1
    assert sleeps == [12.0]                    # 真的按 Retry-After 退避（旧实现压根不读）
    assert client._limiter.rate == 1.0         # 同时把速率减半


def test_single_key_429_without_header_still_waits_a_token(monkeypatch, sleeps):
    client = _client(monkeypatch, answer=lambda url, params: _resp(429), rate=2.0)
    with pytest.raises(TmdbTransientError):   # 没有 Retry-After 也退避，然后转重试
        client._get("/search/tv", {"query": "x"})
    assert len(sleeps) == 1 and 0 < sleeps[0] <= tmdb_mod.TMDB_RETRY_AFTER_CAP_SEC


def test_multi_key_429_rotates_instead_of_sleeping_long(monkeypatch, sleeps):
    """还有别的 key 就换一把继续（配额按 key 算），不在这里睡满 Retry-After"""
    def answer(url, params):
        return _resp(429, headers={"retry-after": "12"}) if params["api_key"] == "k1" \
            else _resp(payload={"ok": True})

    client = _client(monkeypatch, keys=("k1", "k2"), answer=answer)
    assert client._get("/configuration", {}) == {"ok": True}
    assert [c[1]["api_key"] for c in client.session.calls] == ["k1", "k2"]
    assert client.stats()["throttled"] == 1
    assert sleeps[0] == 1.0                    # 只短暂停一下，不是 Retry-After 的 12 秒
    assert client.api_key == "k2"


def test_all_keys_dead_returns_none(monkeypatch, sleeps):
    client = _client(monkeypatch, keys=("k1", "k2"),
                     answer=lambda url, params: _resp(401), rate=2.0)
    assert client._get("/configuration", {}) is None
    assert len(client.session.calls) == 2      # 两把 key 各试一次，不多打
    assert sleeps == []                        # 401 不是配额问题，不用退避


# ==================== 网络异常：重试 ====================

def test_read_timeout_is_retried_then_succeeds(monkeypatch, sleeps):
    state = {"n": 0}

    def answer(url, params):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("read timeout")
        return _resp(payload={"ok": True})

    client = _client(monkeypatch, answer=answer)
    assert client._get("/configuration", {}) == {"ok": True}
    assert len(client.session.calls) == 2      # 失败一次 + 重试一次
    assert client.stats()["retries"] == 1
    assert client.stats()["net_fail"] == 0
    assert len(sleeps) == 1                    # 退避后重试


def test_network_error_gives_up_after_the_configured_retries(monkeypatch, sleeps):
    def answer(url, params):
        raise OSError("connection reset")

    client = _client(monkeypatch, answer=answer)
    # 重试次数照旧打满，但结局从「返回 None（会被写成终态 none）」改为抛瞬态
    with pytest.raises(TmdbTransientError):
        client._get("/configuration", {})
    assert client.stats()["retries"] == tmdb_mod.TMDB_NET_RETRIES
    assert client.stats()["net_fail"] == 1


# ==================== 收口：桶只有一份 ====================

def test_enrich_worker_no_longer_owns_a_tmdb_bucket():
    """旧桶留在 worker 里就会「双重限流」：一边按条目扣、一边按请求扣"""
    from backend.emby_server import enrich_worker as worker
    assert not hasattr(worker, "_tmdb_limiter")
    assert not hasattr(worker, "_RateLimiter")


def _session_proxy_urls(session) -> set:
    """读出一个 httpx.Client 实际生效的代理地址（与冒烟脚本同一口径）

    httpx 没有公开这个信息（代理在构造时就织进了 transport），只能读内部结构。
    """
    urls = set()
    for transport in (getattr(session, "_mounts", None) or {}).values():
        url = getattr(getattr(transport, "_pool", None), "_proxy_url", None)
        if not url:
            continue
        part = lambda v: v.decode() if isinstance(v, bytes) else str(v)  # noqa: E731
        port = getattr(url, "port", None)
        urls.add(f"{part(url.scheme)}://{part(url.host)}" + (f":{port}" if port else ""))
    return urls


def test_session_keeps_env_proxy_support(monkeypatch):
    """不要给 httpx.Client 传自定义 transport——那会让环境变量里的代理静默失效

    实测事故：为了加一层连接级重试传了 ``transport=httpx.HTTPTransport(retries=2)``，
    httpx 就不再按环境变量挂代理，「后台配的代理」对刮削完全不生效，而且**不报错**；
    只有真发请求的冒烟（scripts/smoke_test_capabilities.py）才发现。重试因此统一
    交给 _request()，会话按默认方式建。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("TMDB_API_KEYS", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(tmdb_mod, "_read_config_value", lambda db: "")
    client = TmdbClient()
    client._set_keys(["k"], "env")
    # 不接 _client()：那条路会把 _ensure_session 打桩，而这里要验的正是它
    client._ensure_session()
    assert client.session is not None
    try:
        proxies = _session_proxy_urls(client.session)
        assert proxies, "会话没有代理挂载点：要么没走默认 transport，要么 httpx 改了内部结构"
        assert "http://127.0.0.1:9" in proxies
    finally:
        client.session.close()


def test_admin_tmdb_keys_reports_the_effective_rate():
    """管理后台能看出「实际在打的速率」，配置值只是上限"""
    from backend.api import admin_scrape
    body = admin_scrape.get_tmdb_keys(staff=None, db=None)
    assert 0 < body["rate"] <= body["rate_ceiling"]
    # v2.57.0 起限流按 key 独立成桶：ceiling = key 数 × 单 key 上限
    # （无 key 时按 1 把算，与 _KeyedLimiter.ceiling 聚合口径一致）
    assert body["rate_ceiling"] == round(max(1, body["count"]) * tmdb_mod.TMDB_PER_SEC, 3)
    assert body["throttled"] >= 0

"""provider 熔断 + 引擎联动 + TMDB/豆瓣喂数（FMBY 学习批一 #8）。

FMBY 实测口径（学习实例第三阶段）：源熔断=连续失败 3 次触发、成功自动
恢复；豆瓣并发上限 1、403 从机房 IP 是常态——所以 403/429 这类「源明确
拒绝」不等凑满次数，一次就开断约 60 秒，冷却到期放一个探测请求试水。

熔断状态是进程全局的：本文件的 fixture 在每个用例前后都 reset，
不许把开着的断路器漏给同进程的其他测试文件（CI 单进程全量跑）。
"""
import urllib.error

import pytest

from backend.emby_server import altmeta, douban
from backend.emby_server.metasources import breaker, engine as ms_engine
from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import sources as ms_sources


@pytest.fixture(autouse=True)
def _breaker_isolation(monkeypatch):
    """熔断参数钉死 + 状态用例前后清零（进程全局状态不许跨用例/跨文件漏）"""
    monkeypatch.setenv("PROVIDER_BREAKER_ENABLED", "1")
    monkeypatch.setenv("PROVIDER_BREAKER_THRESHOLD", "3")
    monkeypatch.setenv("PROVIDER_BREAKER_COOLDOWN_SEC", "60")
    monkeypatch.setenv("PROVIDER_BREAKER_FAST_SEC", "60")
    breaker.reset_all()
    yield
    breaker.reset_all()


# ---------------------------------------------------------------------------
# 断路器本体：阈值 / 快速熔断 / 半开 / 恢复
# ---------------------------------------------------------------------------

def test_three_consecutive_failures_open_the_breaker():
    """连续失败 3 次开断（FMBY 实测阈值）；前两次仍放行"""
    assert breaker.allow("anilist")
    breaker.record_failure("anilist", error="连接超时")
    breaker.record_failure("anilist", error="连接超时")
    assert breaker.allow("anilist") is True        # 还差一次
    breaker.record_failure("anilist", error="连接超时")
    assert breaker.allow("anilist") is False       # 第三次 → 开断
    view = breaker.status()["anilist"]
    assert view["state"] == "open"
    assert view["failures"] == 3
    assert view["trips"] == 1
    assert view["open_remaining_sec"] > 0
    assert "连接超时" in view["last_error"]


def test_success_resets_the_consecutive_count():
    """成功清零连续计数：失败要「连续」才算数，中间成一次重新数"""
    breaker.record_failure("tvmaze", error="500")
    breaker.record_failure("tvmaze", error="500")
    breaker.record_success("tvmaze")
    breaker.record_failure("tvmaze", error="500")
    breaker.record_failure("tvmaze", error="500")
    assert breaker.allow("tvmaze") is True
    breaker.record_failure("tvmaze", error="500")
    assert breaker.allow("tvmaze") is False


@pytest.mark.parametrize("status", [403, 429])
def test_refusal_status_trips_immediately(status):
    """403/429 一次就开断（FMBY 口径：源明确拒绝，别再逐条撞去加深封禁）"""
    breaker.record_failure("omdb", status=status, error=f"HTTP {status}")
    assert breaker.allow("omdb") is False
    assert breaker.status()["omdb"]["trips"] == 1


def test_half_open_lets_one_probe_through_then_recovers(monkeypatch):
    """冷却到期 → 半开只放一个探测；探测成功 → 关断恢复正常放行"""
    monkeypatch.setenv("PROVIDER_BREAKER_FAST_SEC", "0")   # 冷却立即到期
    breaker.record_failure("tvdb", status=403, error="HTTP 403")
    assert breaker.allow("tvdb") is True                   # 半开：放探测
    assert breaker.allow("tvdb") is False                  # 第二个并发请求不放
    assert breaker.status()["tvdb"]["state"] == "half_open"
    breaker.record_success("tvdb")                         # 探测成功
    assert breaker.allow("tvdb") is True
    view = breaker.status()["tvdb"]
    assert view["state"] == "closed"
    assert view["failures"] == 0


def test_failed_probe_retrips_with_a_fresh_cooldown(monkeypatch):
    """半开探测又失败 → 立刻重新开断，冷却从这次失败重新算"""
    monkeypatch.setenv("PROVIDER_BREAKER_COOLDOWN_SEC", "0")
    for _ in range(3):
        breaker.record_failure("anilist", error="boom")
    assert breaker.allow("anilist") is True                # 半开探测
    monkeypatch.setenv("PROVIDER_BREAKER_COOLDOWN_SEC", "60")
    breaker.record_failure("anilist", error="还是不行")
    assert breaker.allow("anilist") is False
    assert breaker.status()["anilist"]["trips"] == 2


def test_released_probe_frees_the_half_open_slot(monkeypatch):
    """领了探测名额但请求没发出去（预检失败）→ 归还后还能再探测"""
    monkeypatch.setenv("PROVIDER_BREAKER_FAST_SEC", "0")
    breaker.record_failure("omdb", status=429, error="HTTP 429")
    assert breaker.allow("omdb") is True
    assert breaker.allow("omdb") is False
    breaker.release_probe("omdb")
    assert breaker.allow("omdb") is True


def test_stale_probe_slot_is_reclaimed(monkeypatch):
    """探测名额被占超时未归还（调用方异常没人记账）→ 下一个调用方回收重发。

    回归钉（协调员审查补，2026-10-24）：没有这道自愈时，任何一处
    「领了名额又抛了没人接的异常」都会让 probe_in_flight 永远为真、
    冷却到期后 allow 永远 False，这个源在进程重启前彻底堵死。
    """
    import time as _time

    monkeypatch.setenv("PROVIDER_BREAKER_FAST_SEC", "0")   # 冷却立即到期
    breaker.record_failure("omdb", status=403, error="HTTP 403")
    assert breaker.allow("omdb") is True                   # 半开：探测名额被领走
    assert breaker.allow("omdb") is False                  # 名额在飞，不放第二个
    state = breaker._states["omdb"]
    state.probe_since = _time.monotonic() - breaker._PROBE_STALE_SEC - 1
    assert breaker.allow("omdb") is True                   # 超时 → 回收重发
    breaker.record_success("omdb")                         # 回收的探测成功 → 关断
    assert breaker.status()["omdb"]["state"] == "closed"


def test_breaker_disabled_never_opens(monkeypatch):
    """总开关关掉：只记账不开断（紧急止血手段，不能反过来把源全关了）"""
    monkeypatch.setenv("PROVIDER_BREAKER_ENABLED", "0")
    for _ in range(5):
        breaker.record_failure("tmdb", status=403, error="HTTP 403")
    assert breaker.allow("tmdb") is True
    assert breaker.status()["tmdb"]["state"] == "closed"


def test_providers_are_independent():
    """一个源熔断不连累别的源"""
    breaker.record_failure("douban", status=403, error="HTTP 403")
    assert breaker.allow("douban") is False
    assert breaker.allow("tmdb") is True
    assert "tmdb" not in breaker.status()   # 没记过账的源不出现在状态里


# ---------------------------------------------------------------------------
# 引擎联动：开断跳过、试采集绕行、预检失败不计数
# ---------------------------------------------------------------------------

@pytest.fixture()
def _fake_registry():
    original = dict(ms_sources.SPEC_BY_ID)
    yield
    ms_sources.SPEC_BY_ID.clear()
    ms_sources.SPEC_BY_ID.update(original)


def _spec(source_id, search):
    spec = ms_sources.SourceSpec(source_id, source_id, False, "en", "测试源", search)
    ms_sources.SPEC_BY_ID[source_id] = spec
    return spec


def _snapshot(*specs):
    return ms_config.Snapshot(
        enabled=True, prefer_chinese=True,
        sources=[ms_config.SourceConfig(
            id=s.id, label=s.label, enabled=True, requires_key=s.requires_key,
            lang=s.lang, keys=[], rate=0.0) for s in specs])


def test_engine_skips_source_once_breaker_opens(_fake_registry):
    """源连续失败 3 轮后，自动采集直接跳过、不再发请求（outcome 写明原因）"""
    calls = {"n": 0}

    def _boom(title, year, kind, *, pool, gate):
        calls["n"] += 1
        raise ms_sources.SourceError("HTTP 500 炸了", status=500)

    spec = _spec("flaky", _boom)
    snap = _snapshot(spec)
    for _ in range(3):
        result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
        assert result.outcomes[0].error                      # 前 3 轮是真问真失败
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
    assert result.outcomes[0].skipped == "源熔断中，已跳过"
    assert calls["n"] == 3                                   # 第 4 轮没再发请求


def test_engine_manual_probe_bypasses_and_can_close_the_breaker(_fake_registry):
    """试采集（only）是人工探针：熔断中也放行；它成功了熔断随之解除"""
    state = {"fail": True}

    def _flaky(title, year, kind, *, pool, gate):
        if state["fail"]:
            raise ms_sources.SourceError("HTTP 403", status=403)
        return ms_sources.Hit(source="flaky", title="片名")

    spec = _spec("flaky", _flaky)
    snap = _snapshot(spec)
    ms_engine.collect(None, "片名", None, "movie", snapshot=snap)   # 403 一次开断
    assert breaker.allow("flaky") is False
    state["fail"] = False
    probed = ms_engine.collect(None, "片名", None, "movie", snapshot=snap, only="flaky")
    assert probed.primary == "flaky"                        # 探针真问到了
    assert breaker.allow("flaky") is True                    # 成功 → 熔断解除


def test_engine_preflight_failure_does_not_feed_the_breaker(_fake_registry):
    """没配密钥这类预检失败不是源的错：问多少轮都不许把源「熔断」掉"""
    def _search(title, year, kind, *, pool, gate):           # pragma: no cover
        raise AssertionError("预检就该拦下，不该走到 search")

    spec = ms_sources.SourceSpec("needkey", "needkey", True, "en", "测试源", _search)
    ms_sources.SPEC_BY_ID["needkey"] = spec
    snap = _snapshot(spec)
    for _ in range(5):
        ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
    assert breaker.allow("needkey") is True
    assert "needkey" not in breaker.status()


# ---------------------------------------------------------------------------
# TMDB 客户端喂数：按请求记账、熔断时零 HTTP
# ---------------------------------------------------------------------------

def test_tmdb_client_feeds_breaker_and_stops_calling(monkeypatch):
    """TMDB 连续 5xx 三次 → 熔断打开 → 后续 _get 直接抛瞬态、零 HTTP 请求"""
    from backend.emby_server import tmdb as tmdb_mod
    from backend.emby_server.tmdb import TmdbClient, TmdbTransientError

    monkeypatch.setenv("EMBY_TMDB_CACHE", "0")
    monkeypatch.setattr(tmdb_mod.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class _Resp:
        status_code = 503
        headers = {}

    class _Session:
        def get(self, url, params):
            calls["n"] += 1
            return _Resp()

    monkeypatch.delenv("TMDB_API_KEYS", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(tmdb_mod, "_read_config_value", lambda db: "")
    client = TmdbClient()
    client._set_keys(["k1"], "env")
    client.session = _Session()
    monkeypatch.setattr(client, "_ensure_session", lambda: None)

    for _ in range(3):
        with pytest.raises(TmdbTransientError):
            client._get("/search/tv", {"query": "x"})
    assert calls["n"] == 3
    with pytest.raises(TmdbTransientError):
        client._get("/search/tv", {"query": "x"})
    assert calls["n"] == 3                                   # 熔断后没再发请求
    assert breaker.status()["tmdb"]["state"] == "open"


# ---------------------------------------------------------------------------
# altmeta / douban 喂数：异常在 HTTP 层被吞，只能在那一层记账
# ---------------------------------------------------------------------------

def test_altmeta_douban_403_fast_trips_and_gates(monkeypatch):
    """豆瓣 suggest 吃 403 → 立即熔断；熔断中再搜直接返回 None、零请求"""
    calls = {"n": 0}

    def _forbidden(*a, **k):
        calls["n"] += 1
        raise urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)

    monkeypatch.setattr(altmeta.urllib.request, "urlopen", _forbidden)
    assert altmeta.search("某剧", min_interval=0) is None
    assert breaker.status()["douban"]["state"] == "open"
    assert altmeta.search("某剧", min_interval=0) is None
    assert calls["n"] == 1                                   # 第二次被熔断拦下


def test_douban_client_ban_mirrors_into_breaker():
    """豆瓣独立客户端触发封禁 → 统一注册表同步开断；成功 → 同步恢复"""
    saved = (douban._banned_until, douban._ban_strikes)
    try:
        douban._trip_ban("HTTP 403")
        view = breaker.status()["douban"]
        assert view["state"] == "open" and view["trips"] == 1
        douban._note_success()
        assert breaker.status()["douban"]["state"] == "closed"
    finally:
        douban._banned_until, douban._ban_strikes = saved

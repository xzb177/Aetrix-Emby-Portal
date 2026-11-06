# -*- coding: utf-8 -*-
"""external_failure_counts 熔断计数（PanSave 借鉴落地 B 线）回归。

锁定四件事：
1. 归一化键口径：query/fragment 整体丢弃（带签名的临时链接恒同
   键）、userinfo 丢弃、默认端口等价、host 小写、尾斜杠归一。
2. 计数行为：连续失败达阈值（默认 5）置 paused_until；成功清零；
   暂停到期即半开（再失败立即重新暂停）；scope 之间分账。
3. 非阻断：计数读写异常一律吞掉，绝不外泄到主流程。
4. 消费方接线：applecms_fetcher.fetch_page /
   upstream_health.probe_source / upstream_aggregate_run 三处在
   暂停期跳过、按口径喂数（凭据类/单资源类失败不计数）。

DB 口径：计数单测用注入 session（本地内存 sqlite，只建本表），
不碰全局 engine/SessionLocal；接线单测用内存假熔断替换模块函数。
"""

from __future__ import annotations

import time
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import external_failure_counts as efc
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_health as uh


# ---------------------------------------------------------------------------
# 归一化键口径
# ---------------------------------------------------------------------------

def test_normalize_drops_signed_query_variants():
    a = efc.normalize_target_key(
        "https://cms.example.com/api/provide?ac=detail&ids=1"
        "&token=AAA&sign=S1&expires=1700000000")
    b = efc.normalize_target_key(
        "https://cms.example.com/api/provide?ac=list&pg=9"
        "&token=BBB&sign=S2&expires=1900000000")
    assert a == b == "https://cms.example.com/api/provide"


def test_normalize_drops_fragment_and_userinfo():
    url = "https://" + "user" + ":" + "pw" + "@cms.example.com/x#frag"
    key = efc.normalize_target_key(url)
    assert key == "https://cms.example.com/x"
    assert "pw" not in key and "@" not in key


def test_normalize_default_port_equivalence():
    assert efc.normalize_target_key("https://h.example.com:443/a") == \
        efc.normalize_target_key("https://h.example.com/a")
    assert efc.normalize_target_key("http://h.example.com:80/a") == \
        efc.normalize_target_key("http://h.example.com/a")
    assert efc.normalize_target_key("https://h.example.com:8443/a") == \
        "https://h.example.com:8443/a"


def test_normalize_host_case_and_trailing_slash():
    assert efc.normalize_target_key("https://EXAMPLE.com/api/") == \
        "https://example.com/api"
    assert efc.normalize_target_key("https://example.com") == \
        efc.normalize_target_key("https://example.com/")
    assert efc.normalize_target_key("https://example.com/a") != \
        efc.normalize_target_key("https://example.com/b")


# ---------------------------------------------------------------------------
# 计数行为（注入 session，本地内存库）
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://")
    efc.ExternalFailureCount.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _fail(session, scope, key, n, now):
    for i in range(n):
        efc.record_failure(scope, key, now=now + i, session=session)


def test_pause_after_threshold_default_five(db_session):
    key = "https://dead.example.com"
    _fail(db_session, "cms_fetch", key, 4, now=1000.0)
    assert efc.is_paused("cms_fetch", key, now=1010.0,
                         session=db_session) is False
    _fail(db_session, "cms_fetch", key, 1, now=1004.0)
    assert efc.is_paused("cms_fetch", key, now=1010.0,
                         session=db_session) is True
    state = efc.pause_states_for_key(key, now=1010.0, session=db_session)
    assert state["paused"] is True
    assert state["scopes"]["cms_fetch"]["fail_count"] == 5
    assert state["scopes"]["cms_fetch"]["paused_until"] == \
        pytest.approx(1004.0 + efc.pause_seconds())


def test_pause_expires_then_half_open_refailure_repauses(db_session):
    key = "https://dead.example.com"
    _fail(db_session, "cms_fetch", key, 5, now=1000.0)
    until = efc.pause_states_for_key(
        key, session=db_session)["scopes"]["cms_fetch"]["paused_until"]
    assert efc.is_paused("cms_fetch", key, now=until - 1,
                         session=db_session) is True
    assert efc.is_paused("cms_fetch", key, now=until + 1,
                         session=db_session) is False
    # 半开试探再失败：计数已在阈值之上，立即重新暂停。
    efc.record_failure("cms_fetch", key, now=until + 2, session=db_session)
    assert efc.is_paused("cms_fetch", key, now=until + 3,
                         session=db_session) is True


def test_success_clears_count_and_pause(db_session):
    key = "https://flaky.example.com"
    _fail(db_session, "upstream_fetch", key, 5, now=1000.0)
    assert efc.is_paused("upstream_fetch", key, now=1001.0,
                         session=db_session) is True
    efc.record_success("upstream_fetch", key, session=db_session)
    assert efc.is_paused("upstream_fetch", key, now=1001.0,
                         session=db_session) is False
    state = efc.pause_states_for_key(key, session=db_session)
    assert state["scopes"]["upstream_fetch"]["fail_count"] == 0
    # 清零后重新计数：4 次失败仍不暂停。
    _fail(db_session, "upstream_fetch", key, 4, now=2000.0)
    assert efc.is_paused("upstream_fetch", key, now=2001.0,
                         session=db_session) is False


def test_scopes_are_independent(db_session):
    key = "https://dead.example.com"
    _fail(db_session, "cms_fetch", key, 5, now=1000.0)
    assert efc.is_paused("cms_fetch", key, now=1001.0,
                         session=db_session) is True
    assert efc.is_paused("upstream_probe", key, now=1001.0,
                         session=db_session) is False


def test_threshold_and_pause_seconds_configurable(db_session, monkeypatch):
    monkeypatch.setenv("EXTERNAL_FAILURE_PAUSE_THRESHOLD", "2")
    monkeypatch.setenv("EXTERNAL_FAILURE_PAUSE_SECONDS", "60")
    key = "https://dead.example.com"
    _fail(db_session, "cms_fetch", key, 2, now=1000.0)
    state = efc.pause_states_for_key(key, now=1001.0, session=db_session)
    assert state["paused"] is True
    # 最后一击在 now=1001（两次失败为 1000/1001），暂停 60 秒。
    assert state["paused_until"] == pytest.approx(1061.0)


class _BoomSession:
    """任何操作都抛错的坏 session：计数层必须吞掉、不许外泄。"""

    def query(self, *args, **kwargs):
        raise RuntimeError("db down")

    def commit(self):
        raise RuntimeError("db down")

    def rollback(self):
        raise RuntimeError("db down")


def test_public_api_accepts_now_kwarg(db_session):
    """公开函数的 now kwarg 口径必须与调用方一致（防签名漂移被
    调用方 try 吞掉、成功永远清不了零）。"""
    key = "https://api.example.com"
    efc.record_failure("cms_fetch", key, now=1000.0, session=db_session)
    assert efc.is_paused("cms_fetch", key, now=1000.0,
                         session=db_session) is False
    efc.record_success("cms_fetch", key, now=1001.0, session=db_session)
    state = efc.pause_states_for_key(key, now=1002.0, session=db_session)
    assert state["scopes"]["cms_fetch"]["fail_count"] == 0


def test_broken_session_never_raises():
    boom = _BoomSession()
    assert efc.is_paused("cms_fetch", "https://x.example.com",
                         session=boom) is False
    efc.record_failure("cms_fetch", "https://x.example.com", session=boom)
    efc.record_success("cms_fetch", "https://x.example.com", session=boom)
    state = efc.pause_states_for_key("https://x.example.com", session=boom)
    assert state["paused"] is False


# ---------------------------------------------------------------------------
# 消费方接线（内存假熔断替换模块函数，只验接线口径）
# ---------------------------------------------------------------------------

class _FakeBreaker:
    """内存假熔断：与真实实现同语义（计数/阈值/暂停集合）。"""

    def __init__(self, threshold=5):
        self.threshold = threshold
        self.counts = {}
        self.paused = set()
        self.calls = []

    def is_paused(self, scope, key, **kwargs):
        self.calls.append(("is_paused", scope, key))
        return (scope, key) in self.paused

    def record_failure(self, scope, key, **kwargs):
        self.calls.append(("record_failure", scope, key))
        n = self.counts.get((scope, key), 0) + 1
        self.counts[(scope, key)] = n
        if n >= self.threshold:
            self.paused.add((scope, key))

    def record_success(self, scope, key, **kwargs):
        self.calls.append(("record_success", scope, key))
        self.counts[(scope, key)] = 0
        self.paused.discard((scope, key))

    def pause_states_for_key(self, key, **kwargs):
        return {"paused": False, "paused_until": None, "scopes": {}}


@pytest.fixture()
def fake_breaker(monkeypatch):
    fake = _FakeBreaker()
    monkeypatch.setattr(efc, "is_paused", fake.is_paused)
    monkeypatch.setattr(efc, "record_failure", fake.record_failure)
    monkeypatch.setattr(efc, "record_success", fake.record_success)
    monkeypatch.setattr(efc, "pause_states_for_key",
                        fake.pause_states_for_key)
    return fake


def _scripted_get(script):
    calls = {"n": 0}

    def _get(url, headers, timeout):
        calls["n"] += 1
        return script[min(calls["n"] - 1, len(script) - 1)]

    return _get, calls


# --- applecms_fetcher.fetch_page -------------------------------------------

def test_cms_fetch_pauses_after_five_network_failures(fake_breaker):
    get, calls = _scripted_get([(None, None)])
    for _ in range(5):
        with pytest.raises(acf.AppleCMSError) as ei:
            acf.fetch_page("https://cms.example.com", http_get=get)
        assert ei.value.kind == "network"
    with pytest.raises(acf.AppleCMSError) as ei:
        acf.fetch_page("https://cms.example.com", http_get=get)
    assert ei.value.kind == "failure_paused"
    assert calls["n"] == 5  # 暂停期内不再发请求


def test_cms_fetch_success_resets_count(fake_breaker):
    get, calls = _scripted_get(
        [(None, None)] * 4 + [(200, {"code": 1, "list": []})]
        + [(None, None)] * 4)
    for _ in range(4):
        with pytest.raises(acf.AppleCMSError):
            acf.fetch_page("https://cms.example.com", http_get=get)
    payload = acf.fetch_page("https://cms.example.com", http_get=get)
    assert payload["code"] == 1
    for _ in range(4):
        with pytest.raises(acf.AppleCMSError) as ei:
            acf.fetch_page("https://cms.example.com", http_get=get)
        assert ei.value.kind == "network"  # 没被暂停，请求照发
    assert calls["n"] == 9


def test_cms_fetch_unauthorized_and_not_found_not_counted(fake_breaker):
    get401, calls401 = _scripted_get([(401, None)])
    for _ in range(7):
        with pytest.raises(acf.AppleCMSError) as ei:
            acf.fetch_page("https://cms.example.com", http_get=get401)
        assert ei.value.kind == "unauthorized"
    assert calls401["n"] == 7
    get404, calls404 = _scripted_get([(404, None)])
    for _ in range(7):
        with pytest.raises(acf.AppleCMSError) as ei:
            acf.fetch_page("https://cms.example.com", http_get=get404)
        assert ei.value.kind == "not_found"
    assert calls404["n"] == 7
    assert not any(c[0] == "record_failure" for c in fake_breaker.calls)


def test_cms_fetch_bad_code_not_counted(fake_breaker):
    """应用层结论（code != 1）不参与计数：连打多次不暂停、照常发请求。

    应用层失败由健康状态机自己的连续失败计数负责；本层只管
    传输层。此口径的回归意义：既有探活场景会对同一源反复打出
    bad_code，若它计入传输层计数，正常探活会被误暂停。
    """
    get, calls = _scripted_get([(200, {"code": 0, "list": []})])
    for _ in range(7):
        with pytest.raises(acf.AppleCMSError) as ei:
            acf.fetch_page("https://cms.example.com", http_get=get)
        assert ei.value.kind == "bad_code"
    assert calls["n"] == 7
    assert not any(c[0] == "record_failure" for c in fake_breaker.calls)


# --- upstream_health.probe_source ------------------------------------------

def _probe_source():
    return uh.UpstreamSource(key="src-1", kind="emby",
                             base_url="https://up.example.com",
                             enabled=True)


def test_probe_skips_when_failure_paused(monkeypatch, fake_breaker):
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "1")
    key = efc.normalize_target_key("https://up.example.com")
    fake_breaker.paused.add((efc.SCOPE_UPSTREAM_PROBE, key))
    get, calls = _scripted_get([(200, {"Id": "srv"})])
    result = uh.probe_source(_probe_source(), uh.InMemoryHealthStore(),
                             http_get=get)
    assert result["outcome"] == "skipped"
    assert result["skipped_reason"] == "failure_paused"
    assert calls["n"] == 0


def test_probe_feeds_failure_and_success(monkeypatch, fake_breaker):
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "1")
    store = uh.InMemoryHealthStore()

    def _dead(url, headers, timeout):
        return None, None

    key = efc.normalize_target_key("https://up.example.com")
    result = uh.probe_source(_probe_source(), store, http_get=_dead)
    assert result["outcome"] == "failure"
    assert ("record_failure", efc.SCOPE_UPSTREAM_PROBE,
            key) in fake_breaker.calls

    def _alive(url, headers, timeout):
        return 200, {"Id": "srv"}

    result = uh.probe_source(_probe_source(), store, http_get=_alive,
                             now=time.time() + 7200)
    assert result["outcome"] == "success"
    assert ("record_success", efc.SCOPE_UPSTREAM_PROBE,
            key) in fake_breaker.calls


def test_probe_feeds_real_counter(monkeypatch):
    """probe 喂数走真实计数模块（非假替身）：失败 +1、成功清零。

    守签名/口径漂移：真实 record_success 必须接受 probe 传入的
    now kwarg 并真的清零——假替身的 **kwargs 会把这类漂移藏住
    （本测试正是为一次实际发生的签名漂移补的回归）。
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    efc.ExternalFailureCount.__table__.create(engine)
    monkeypatch.setattr(efc, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setenv("UPSTREAM_HEALTH_PROBE_ENABLED", "1")
    store = uh.InMemoryHealthStore()
    key = efc.normalize_target_key("https://up.example.com")

    def _dead(url, headers, timeout):
        return None, None

    result = uh.probe_source(_probe_source(), store, http_get=_dead)
    assert result["outcome"] == "failure"
    state = efc.pause_states_for_key(key)
    assert state["scopes"][efc.SCOPE_UPSTREAM_PROBE]["fail_count"] == 1

    def _alive(url, headers, timeout):
        return 200, {"Id": "srv"}

    result = uh.probe_source(_probe_source(), store, http_get=_alive,
                             now=time.time() + 7200)
    assert result["outcome"] == "success"
    state = efc.pause_states_for_key(key)
    assert state["scopes"][efc.SCOPE_UPSTREAM_PROBE]["fail_count"] == 0
    engine.dispose()


# --- upstream_aggregate_run -------------------------------------------------

def _agg_source():
    return types.SimpleNamespace(
        id=7, base_url="https://up.example.com", user_agent=None,
        referer=None, extra_headers_json=None)


def test_aggregate_libraries_skips_when_paused(fake_breaker):
    key = efc.normalize_target_key("https://up.example.com")
    fake_breaker.paused.add((efc.SCOPE_UPSTREAM_FETCH, key))
    get, calls = _scripted_get([(200, {"Items": []})])
    with pytest.raises(uar.UpstreamAggregateRunError) as ei:
        uar.fetch_upstream_libraries(_agg_source(), http_get=get,
                                     api_key="k")
    assert ei.value.code == "upstream_unavailable"
    assert calls["n"] == 0


def test_aggregate_records_skips_when_paused(fake_breaker):
    key = efc.normalize_target_key("https://up.example.com")
    fake_breaker.paused.add((efc.SCOPE_UPSTREAM_FETCH, key))
    get, calls = _scripted_get([(200, {"Items": []})])
    with pytest.raises(uar.UpstreamAggregateRunError) as ei:
        uar.fetch_upstream_records(_agg_source(), http_get=get)
    assert ei.value.code == "upstream_unavailable"
    assert calls["n"] == 0


def test_aggregate_iter_records_skips_when_paused(fake_breaker):
    key = efc.normalize_target_key("https://up.example.com")
    fake_breaker.paused.add((efc.SCOPE_UPSTREAM_FETCH, key))
    get, calls = _scripted_get([(200, {"Items": []})])
    with pytest.raises(uar.UpstreamAggregateRunError) as ei:
        next(uar.iter_upstream_records_for_parent(
            _agg_source(), "lib1", http_get=get, api_key="k"))
    assert ei.value.code == "upstream_unavailable"
    assert calls["n"] == 0


def test_aggregate_libraries_counts_unavailable_not_credential(fake_breaker):
    def _dead(url, headers, timeout):
        return None, None

    with pytest.raises(uar.UpstreamAggregateRunError) as ei:
        uar.fetch_upstream_libraries(_agg_source(), http_get=_dead,
                                     api_key="k")
    assert ei.value.code == "upstream_unavailable"
    assert ("record_failure", efc.SCOPE_UPSTREAM_FETCH,
            efc.normalize_target_key("https://up.example.com")) in \
        fake_breaker.calls

    fake_breaker.calls.clear()

    def _unauth(url, headers, timeout):
        return 401, {"Message": "bad"}

    with pytest.raises(uar.UpstreamAggregateRunError) as ei:
        uar.fetch_upstream_libraries(_agg_source(), http_get=_unauth,
                                     api_key="k")
    assert ei.value.code == "upstream_credential_invalid"
    assert not any(c[0] == "record_failure" for c in fake_breaker.calls)


def test_aggregate_libraries_success_feeds_success(fake_breaker):
    def _ok(url, headers, timeout):
        return 200, {"Items": [{"Id": "lib1", "Name": "电影"}]}

    libs = uar.fetch_upstream_libraries(_agg_source(), http_get=_ok,
                                        api_key="k")
    assert libs == [{"id": "lib1", "name": "电影", "collection_type": ""}]
    assert ("record_success", efc.SCOPE_UPSTREAM_FETCH,
            efc.normalize_target_key("https://up.example.com")) in \
        fake_breaker.calls

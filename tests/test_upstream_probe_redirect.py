# -*- coding: utf-8 -*-
"""上游播放探活有界跟随重定向（生产实证缺陷回归）。

缺陷（2026-10-31 生产实测，源 4 乐观服 → 上游条目 1079867）：
``_default_http_range_probe`` 经 ``upstream_http.safe_urlopen`` 探活，
该 opener 按 SSRF 纪律刻意不跟随重定向，302 以 HTTPError 回到探活层
后原样返回；``_classify_probe_status`` 无 3xx 分支，302 直接落
502 upstream_unavailable。手工逐跳跟随（每跳先 verify_outbound_url）
实测 302（Emby stream → OpenList /d/ 直链）→ 302 → 206
video/x-matroska，内容完全可播——凡上游以重定向发流的源全线误判。

钉死口径：
- 探活在探活层内有界跟随（最多 3 跳），每一跳的新 URL 先过
  ``verify_outbound_url`` 才发请求；safe_urlopen 本身仍不跟随；
- 分类按最终跳状态走既有分类表（本文件只验探活返回值与 resolve
  的码映射，分类表本身不动）；
- resolve 成功返回的仍是最初拼的上游流地址，绝不是探活跟到的
  最终签名直链；
- 中间跳不读响应体（替身的响应体恒为空，读全也读不到东西）。

全部离线：stub 掉 ``upstream_http`` 模块的 ``safe_urlopen`` /
``verify_outbound_url`` 两个模块属性（patch 模块属性口径），
handler 按 URL 脚本化响应；非 2xx 以 HTTPError 形态抛出，
与无重定向处理器的真实 opener 行为一致。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import email.message
import io
import urllib.error

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import upstream_http
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as us

ENV = "UPSTREAM_PLAYBACK_ENABLED"
START = "https://up.example.com/Videos/item-1/stream?static=true&api_key=K"


# ---------------------------------------------------------------- 替身上游


class _FakeResp:
    """2xx 成功响应替身：只给 status / headers，不给可读的流体。"""

    def __init__(self, status, headers):
        self.status = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return b""


def _msg(headers):
    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return msg


class _Transport:
    """脚本化出站替身：handler(url) -> (status, headers dict)。

    verify 记录每个被咨询的 URL，blocked 片段命中的抛
    OutboundBlockedError（与真实 verify 的拒绝形态一致）；
    open 记录每个真正发出的请求 URL，非 2xx 抛 HTTPError。
    """

    def __init__(self, handler, blocked=()):
        self.handler = handler
        self.blocked = tuple(blocked)
        self.requested = []
        self.verified = []

    def verify(self, url):
        self.verified.append(url)
        if any(fragment in url for fragment in self.blocked):
            raise upstream_http.OutboundBlockedError("替身拦截")
        return None

    def open(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        status, headers = self.handler(url)
        if 200 <= status < 300:
            return _FakeResp(status, _msg(headers))
        raise urllib.error.HTTPError(
            url, status, "fake", _msg(headers), io.BytesIO(b""))


def _install(monkeypatch, handler, blocked=()):
    transport = _Transport(handler, blocked)
    monkeypatch.setattr(upstream_http, "safe_urlopen", transport.open)
    monkeypatch.setattr(
        upstream_http, "verify_outbound_url", transport.verify)
    return transport


# ---------------------------------------------------------------- DB 夹具


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _enabled_source(db, **kw):
    kw.setdefault("name", "upstream-a")
    kw.setdefault("source_type", "emby")
    kw.setdefault("base_url", "https://up.example.com")
    kw.setdefault("auth_method", "api_key")
    row = us.create_source(db, **kw)
    us.enable_source(db, row)
    db.commit()
    return row


# ---------------------------------------------------------------- 探活层


@pytest.mark.parametrize("first_status", [301, 302, 303, 307, 308])
def test_probe_follows_two_redirects_to_206(monkeypatch, first_status):
    """① 302→302→206：探活返回最终跳 206；相对 Location 走 urljoin；
    每个重定向目标都先验后发。"""
    hop2 = "https://up.example.com/d/file-1"
    hop3 = "https://cdn.example.com/media/file-1.mkv?sign=xyz"

    def handler(url):
        if url == START:
            return first_status, {"Location": "/d/file-1"}
        if url == hop2:
            return 302, {"Location": hop3}
        if url == hop3:
            return 206, {}
        raise AssertionError(f"未脚本化 URL: {url}")

    transport = _install(monkeypatch, handler)
    result = up._default_http_range_probe(START, {"Accept": "*/*"}, 5)
    assert result == (206, None)
    assert transport.requested == [START, hop2, hop3]
    assert hop2 in transport.verified
    assert hop3 in transport.verified


def test_probe_redirect_then_404_returns_final_status(monkeypatch):
    """②前半 302→404：探活返回最终跳 404（不是首跳 302）。"""
    gone = "https://cdn.example.com/gone"

    def handler(url):
        if url == START:
            return 302, {"Location": gone}
        if url == gone:
            return 404, {}
        raise AssertionError(f"未脚本化 URL: {url}")

    _install(monkeypatch, handler)
    result = up._default_http_range_probe(START, {"Accept": "*/*"}, 5)
    assert result == (404, None)


def test_probe_redirect_loop_beyond_limit_is_unavailable(monkeypatch):
    """③ 重定向跳不完（>3 跳）：按连不上口径 (None, None)；
    请求总数 = 初始 1 + 跟随 3，不许无限跟。"""
    def handler(url):
        return 302, {"Location": url + "x"}

    transport = _install(monkeypatch, handler)
    result = up._default_http_range_probe(START, {"Accept": "*/*"}, 5)
    assert result == (None, None)
    assert len(transport.requested) == 4


def test_probe_redirect_to_private_blocked_and_never_requested(monkeypatch):
    """④ Location 指向内网：verify 拒绝 → (None, None)，
    且第二跳请求根本不发出。"""
    internal = "http://10.0.0.5/internal"

    def handler(url):
        if url == START:
            return 302, {"Location": internal}
        raise AssertionError(f"内网地址绝不该被请求: {url}")

    transport = _install(monkeypatch, handler, blocked=("10.0.0.5",))
    result = up._default_http_range_probe(START, {"Accept": "*/*"}, 5)
    assert result == (None, None)
    assert transport.requested == [START]
    assert internal in transport.verified


def test_probe_direct_statuses_unchanged(monkeypatch):
    """⑤ 防回归：直达 206 / 404 行为与现状逐字一致。"""
    _install(monkeypatch, lambda url: (206, {}))
    assert up._default_http_range_probe(START, {"Accept": "*/*"}, 5) == (206, None)
    _install(monkeypatch, lambda url: (404, {}))
    assert up._default_http_range_probe(START, {"Accept": "*/*"}, 5) == (404, None)


def test_probe_final_hop_retry_after_wins(monkeypatch):
    """429 的 Retry-After 取最终跳的（中间跳给了也不用最终跳没给的）。"""
    quota = "https://cdn.example.com/quota"

    def handler(url):
        if url == START:
            return 302, {"Location": quota, "Retry-After": "7"}
        if url == quota:
            return 429, {"Retry-After": "45"}
        raise AssertionError(f"未脚本化 URL: {url}")

    _install(monkeypatch, handler)
    result = up._default_http_range_probe(START, {"Accept": "*/*"}, 5)
    assert result == (429, 45)


def test_probe_redirect_without_location_is_unavailable(monkeypatch):
    """3xx 无 Location：无从跟随，按连不上口径，不原样透传 3xx。"""
    _install(monkeypatch, lambda url: (302, {}))
    result = up._default_http_range_probe(START, {"Accept": "*/*"}, 5)
    assert result == (None, None)


# ---------------------------------------------------------------- resolve 层


def test_resolve_redirect_chain_returns_original_url(db, monkeypatch):
    """①后半 两跳重定向后可播：resolve 返回最初拼的流地址，
    绝不把探活跟到的最终签名直链下发给客户端。"""
    monkeypatch.setenv(ENV, "1")
    row = _enabled_source(db, secret="K-SECRET")
    original = up.build_upstream_stream_url(
        "https://up.example.com", "item-7", api_key="K-SECRET")
    openlist = "https://openlist.example.com/d/file-7"
    signed = "https://eos.example.com/signed/file-7.mkv?sig=1"

    def handler(url):
        if url == original:
            return 302, {"Location": openlist}
        if url == openlist:
            return 302, {"Location": signed}
        if url == signed:
            return 206, {}
        raise AssertionError(f"未脚本化 URL: {url}")

    _install(monkeypatch, handler)
    url = up.resolve_upstream_stream(db, row.id, "item-7")
    assert url == original
    assert "eos.example.com" not in url
    assert "openlist.example.com" not in url


def test_resolve_redirect_then_404_is_item_not_found(db, monkeypatch):
    """②后半 302→404：分类按最终跳走既有表 → upstream_item_not_found。"""
    monkeypatch.setenv(ENV, "1")
    row = _enabled_source(db, secret="K-SECRET")
    original = up.build_upstream_stream_url(
        "https://up.example.com", "item-7", api_key="K-SECRET")
    gone = "https://cdn.example.com/gone"

    def handler(url):
        if url == original:
            return 302, {"Location": gone}
        if url == gone:
            return 404, {}
        raise AssertionError(f"未脚本化 URL: {url}")

    _install(monkeypatch, handler)
    with pytest.raises(up.UpstreamPlaybackError) as exc:
        up.resolve_upstream_stream(db, row.id, "item-7")
    assert (exc.value.status, exc.value.code) == (404, "upstream_item_not_found")

"""域名守卫 + CF 真实 IP 中间件的回归测试。

两个都是纯 ASGI 中间件：直接调 ``__call__(scope, receive, send)``，
断言发出的 ASGI 消息，不经过 HTTP 客户端。
"""
import os

import pytest

from backend.domain_guard import (
    CloudflareIPMiddleware,
    DomainGuardMiddleware,
)


def _scope(path="/emby/System/Info", host="stream.example.com", client=("1.2.3.4", 5000)):
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "headers": [(b"host", host.encode("latin-1"))],
        "client": client,
    }


async def _run(mw_cls, scope):
    """跑一遍中间件，返回 (下游是否被调用, 发出的消息列表, 变更后的 scope)。"""
    called = []

    async def app(s, receive, send):
        called.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    messages = []

    async def send(msg):
        messages.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await mw_cls(app)(scope, receive, send)
    return bool(called), messages, scope


def _status(messages):
    for m in messages:
        if m["type"] == "http.response.start":
            return m["status"]
    return None


# ---------- DomainGuard ----------

def test_no_env_passthrough(monkeypatch):
    import asyncio
    monkeypatch.delenv("ENFORCE_DOMAIN", raising=False)
    called, msgs, _ = asyncio.run(_run(DomainGuardMiddleware, _scope(host="1.2.3.4")))
    assert called and _status(msgs) == 200


def test_wrong_host_403(monkeypatch):
    import asyncio
    monkeypatch.setenv("ENFORCE_DOMAIN", "stream.example.com")
    called, msgs, _ = asyncio.run(_run(DomainGuardMiddleware, _scope(host="5.6.7.8")))
    assert not called and _status(msgs) == 403


def test_correct_host_passthrough(monkeypatch):
    import asyncio
    monkeypatch.setenv("ENFORCE_DOMAIN", "stream.example.com")
    called, msgs, _ = asyncio.run(_run(DomainGuardMiddleware, _scope(host="stream.example.com")))
    assert called and _status(msgs) == 200


def test_host_with_port_stripped(monkeypatch):
    import asyncio
    monkeypatch.setenv("ENFORCE_DOMAIN", "stream.example.com")
    called, msgs, _ = asyncio.run(_run(DomainGuardMiddleware, _scope(host="stream.example.com:8001")))
    assert called and _status(msgs) == 200


def test_multi_domain(monkeypatch):
    import asyncio
    monkeypatch.setenv("ENFORCE_DOMAIN", "a.example.com, stream.example.com")
    called, msgs, _ = asyncio.run(_run(DomainGuardMiddleware, _scope(host="a.example.com")))
    assert called


def test_health_from_private_ip_allowed(monkeypatch):
    import asyncio
    monkeypatch.setenv("ENFORCE_DOMAIN", "stream.example.com")
    called, msgs, _ = asyncio.run(_run(DomainGuardMiddleware,
             _scope(path="/api/health", host="10.0.0.9", client=("10.0.0.9", 4000))))
    assert called and _status(msgs) == 200


def test_health_from_public_ip_still_blocked(monkeypatch):
    import asyncio
    monkeypatch.setenv("ENFORCE_DOMAIN", "stream.example.com")
    called, msgs, _ = asyncio.run(_run(DomainGuardMiddleware,
             _scope(path="/api/health", host="5.6.7.8", client=("5.6.7.8", 4000))))
    assert not called and _status(msgs) == 403


def test_non_http_passthrough(monkeypatch):
    import asyncio
    monkeypatch.setenv("ENFORCE_DOMAIN", "stream.example.com")
    scope = {"type": "lifespan"}
    called, _, _ = asyncio.run(_run(DomainGuardMiddleware, scope))
    assert called


# ---------- CloudflareIP ----------

def test_cf_ip_disabled_by_default(monkeypatch):
    import asyncio
    monkeypatch.delenv("TRUST_CF_IP", raising=False)
    scope = _scope()
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "1.2.3.4"  # 没动


CF_PEER = ("162.158.10.20", 443)  # Cloudflare 官方网段内的回源地址


def test_cf_connecting_ip_preferred(monkeypatch):
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    scope = _scope(client=CF_PEER)
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    scope["headers"].append((b"x-forwarded-for", b"8.8.8.8, 7.7.7.7"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "9.9.9.9"


def test_xff_fallback_uses_last_hop(monkeypatch):
    """H4：XFF 首段是客户端可伪造的，回退时取最近一跳代理追加的最后一段"""
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "1")
    scope = _scope(client=CF_PEER)
    scope["headers"].append((b"x-forwarded-for", b"8.8.8.8, 7.7.7.7"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "7.7.7.7"


def test_invalid_proxy_ip_ignored(monkeypatch):
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    scope = _scope(client=CF_PEER)
    scope["headers"].append((b"cf-connecting-ip", b"not-an-ip"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == CF_PEER[0]


# ---------- H4：只信任可信直连对端 ----------

def test_untrusted_peer_cannot_spoof_cf_header(monkeypatch):
    """直连源站的公网请求带 CF-Connecting-IP 不生效"""
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    scope = _scope(client=("5.6.7.8", 4000))
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    scope["headers"].append((b"x-forwarded-for", b"9.9.9.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "5.6.7.8"


def test_untrusted_peer_cannot_become_loopback(monkeypatch):
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    scope = _scope(client=("5.6.7.8", 4000))
    scope["headers"].append((b"cf-connecting-ip", b"127.0.0.1"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "5.6.7.8"


def test_trusted_peer_cannot_restore_loopback(monkeypatch):
    """即使经可信代理，还原成 127.0.0.1 也拒绝（防冒充本机）"""
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    scope = _scope(client=CF_PEER)
    scope["headers"].append((b"cf-connecting-ip", b"127.0.0.1"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == CF_PEER[0]


def test_loopback_cf_header_not_trusted(monkeypatch):
    """P2（审查第七批）：回环对端发来的 CF-Connecting-IP 不再采信。

    旧行为把回环当可信，攻击者经本机 nginx 伪造该头即可任意冒充客户端 IP
    （限流绕过、审计投毒）。现在只认 CF 官方网段，回环来的该头直接忽略。
    """
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    scope = _scope(client=("127.0.0.1", 4000))
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "127.0.0.1"


def test_trusted_proxy_cf_header_not_trusted_xff_still_works(monkeypatch):
    """P2（审查第七批）：自配可信代理发来的 CF-Connecting-IP 不再采信，
    回退走 X-Forwarded-For 末段（自配代理的正确传 IP 姿势）。"""
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    scope = _scope(client=("172.18.0.1", 4000))
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    scope["headers"].append((b"x-forwarded-for", b"8.8.8.8"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "8.8.8.8"


def test_cloudflare_ranges_override(monkeypatch):
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setenv("CLOUDFLARE_IP_RANGES", "10.9.0.0/16")
    scope = _scope(client=CF_PEER)  # 覆盖后官方网段不再可信
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == CF_PEER[0]
    scope = _scope(client=("10.9.1.1", 1))
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "9.9.9.9"


def test_star_forwarded_allow_does_not_trust_forged_client_ip(monkeypatch, caplog):
    """FORWARDED_ALLOW_IPS=* 不能让任意对端伪造客户端 IP。显式 IP/CIDR 与 CF 网段仍有效。"""
    import asyncio
    import logging

    from backend import domain_guard

    domain_guard._wildcard_warned = False
    monkeypatch.setenv("TRUST_CF_IP", "true")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    scope = _scope(client=("198.51.100.8", 4000))
    scope["headers"].append((b"cf-connecting-ip", b"192.0.2.9"))
    scope["headers"].append((b"x-forwarded-for", b"192.0.2.9"))
    with caplog.at_level(logging.WARNING):
        _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
        _, _, again = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "198.51.100.8"
    assert again["client"][0] == "198.51.100.8"
    warnings = [r for r in caplog.records if "FORWARDED_ALLOW_IPS" in r.getMessage()]
    assert len(warnings) == 1

    # 同一行里的显式 CIDR 仍然点名生效；* 不会把名单外面的对端也放进来。
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*,198.51.100.0/24")
    monkeypatch.setenv("TRUSTED_PROXIES", "203.0.113.0/24")
    scope = _scope(client=("198.51.100.8", 4000))
    scope["headers"].append((b"x-forwarded-for", b"192.0.2.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "192.0.2.9"
    scope = _scope(client=("203.0.113.10", 4000))
    scope["headers"].append((b"x-forwarded-for", b"192.0.2.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "192.0.2.9"
    scope = _scope(client=("192.0.2.8", 4000))
    scope["headers"].append((b"x-forwarded-for", b"192.0.2.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "192.0.2.8"

    # Cloudflare 网段不依赖 FORWARDED_ALLOW_IPS，* 既不扩大也不取消它。
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("CLOUDFLARE_IP_RANGES", "198.51.100.0/24")
    scope = _scope(client=("198.51.100.8", 4000))
    scope["headers"].append((b"cf-connecting-ip", b"192.0.2.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "192.0.2.9"


def test_forwarded_allow_cidr_trusts_xff(monkeypatch):
    """FORWARDED_ALLOW_IPS 的 CIDR（没有 *）仍然采信 XFF。"""
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "198.51.100.0/24")
    scope = _scope(client=("198.51.100.8", 4000))
    scope["headers"].append((b"x-forwarded-for", b"192.0.2.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "192.0.2.9"
    scope = _scope(client=("192.0.2.8", 4000))
    scope["headers"].append((b"x-forwarded-for", b"192.0.2.9"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "192.0.2.8"


def test_forwarded_allow_ips_peer_trusts_xff_not_cf_header(monkeypatch):
    """FORWARDED_ALLOW_IPS 与 TRUSTED_PROXIES 同一份可信名单：采信 XFF，不采信 CF 头。"""
    import asyncio
    monkeypatch.setenv("TRUST_CF_IP", "true")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "198.51.100.40")
    scope = _scope(client=("198.51.100.40", 4000))
    scope["headers"].append((b"cf-connecting-ip", b"9.9.9.9"))
    scope["headers"].append((b"x-forwarded-for", b"8.8.8.8"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "8.8.8.8"
    scope = _scope(client=("198.51.100.41", 4000))
    scope["headers"].append((b"x-forwarded-for", b"8.8.8.8"))
    _, _, out = asyncio.run(_run(CloudflareIPMiddleware, scope))
    assert out["client"][0] == "198.51.100.41"

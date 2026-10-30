"""上游出站 HTTP 公共层（审计 A-053 SSRF 纵深修补）。

三路上游拉取（聚合、探活、播放探活）原来都直接 ``urllib.request.urlopen``：
默认跟随重定向、发请求前不复验 DNS 解析结果。建源校验
``upstream_sources.normalize_base_url`` 只验字面主机，域名分支放行后，
域名经 DNS 重绑定指回内网、或上游回 302 到内网地址时，服务端都会
把请求发进内网。本模块是三路共用的唯一出站口：

- 发请求前解析目标主机并复验解析 IP：全部解析结果都必须是公网
  地址（拒绝回环/私网/链路本地/保留/未指定/多播，含 IPv4 映射
  的 IPv6 形式）；任一结果不合规、或 DNS 解析失败，一律拦截。
- 重定向一律不跟随：本 opener 刻意不装 ``HTTPRedirectHandler``，
  3xx 会以 ``HTTPError`` 回到调用方既有的失败口径（聚合/探活按
  上游不可用计，播放探活按 502 计），绝不把请求发往重定向目标。
  这是审计允许的两个选项之一（禁用跟随 / 跟随前复验），选禁用
  是因为 urllib 处理器优先级会让默认跟随先跑，复验不可靠。
- 拦截错误与日志只带主机名，绝不带完整 URL：播放探活 URL 的
  查询参数里可能含上游 api_key，完整 URL 进日志/异常就是凭据外泄。

残留缺口（见 PR 回传「未知」）：校验与建连之间仍有一次重解析
（urllib 建连时按主机名再解析一次），DNS 重绑定窗口未被彻底钉死；
彻底钉死须按解析 IP 直连 + Host/SNI 保持原主机名的定制连接类，
属后续加固，不在本单范围。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class OutboundBlockedError(RuntimeError):
    """出站目标被 SSRF 守卫拦截。消息只含主机名，绝不含完整 URL。"""


def is_public_ip(ip_text: str) -> bool:
    """IP 字面值是否公网地址：拒绝回环/私网/链路本地/保留/未指定/多播。

    IPv4 映射的 IPv6 形式（如 ::ffff:127.0.0.1）先还原成 IPv4 再判，
    否则映射形式会绕过回环检查。
    """
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (
        addr.is_loopback
        or addr.is_private
        or addr.is_reserved
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve_and_verify(host: str, port: int) -> None:
    """解析主机并复验全部结果：任一结果非公网、或解析失败即拦截。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundBlockedError(f"上游主机无法解析，已拦截: {host}") from exc
    ips = [str(info[4][0]) for info in infos if info[4]]
    if not ips:
        raise OutboundBlockedError(f"上游主机无解析结果，已拦截: {host}")
    for ip in ips:
        if not is_public_ip(ip):
            raise OutboundBlockedError(f"上游主机解析到非公网地址，已拦截: {host}")


def verify_outbound_url(url: str) -> None:
    """发请求前的出站校验：协议、userinfo、主机、端口、解析 IP 全过才放行。

    字面 IP 不走 DNS，直接判；域名走解析复验。失败一律
    OutboundBlockedError（调用方按既有口径计为上游不可用）。
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise OutboundBlockedError("上游地址无法解析，已拦截") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise OutboundBlockedError(f"上游地址协议非法，已拦截: {scheme!r}")
    if parts.username or parts.password:
        raise OutboundBlockedError("上游地址不得带 userinfo，已拦截")
    host = (parts.hostname or "").strip()
    if not host:
        raise OutboundBlockedError("上游地址缺少主机名，已拦截")
    try:
        port = parts.port
    except ValueError as exc:
        raise OutboundBlockedError("上游地址端口非法，已拦截") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    # 字面 IP 直接判，不走 DNS；其余走解析复验（含 0x7f.0.0.1 这类
    # 非十进制写法——ipaddress 不认它们，但 getaddrinfo 会解析到内网）。
    try:
        ipaddress.ip_address(host)
    except ValueError:
        _resolve_and_verify(host, port)
    else:
        if not is_public_ip(host):
            raise OutboundBlockedError(f"上游主机不是公网地址，已拦截: {host}")


def _build_opener() -> urllib.request.OpenerDirector:
    """出站 opener：刻意不装 HTTPRedirectHandler，重定向一律不跟随。

    其余与默认 opener 同款（代理、HTTP/HTTPS、错误处理），只少
    跟随重定向与 FTP/File 处理（出站层发请求前已限定 http/https，
    非 http(s) 到不了 opener）。
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.ProxyHandler())
    opener.add_handler(urllib.request.UnknownHandler())
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


_OPENER = _build_opener()


def safe_urlopen(request: urllib.request.Request, timeout: float):
    """SSRF 守卫版 urlopen：先校验（含解析复验），再发请求。

    调用方原有的 except 结构不变：HTTPError 走状态码分支，
    OutboundBlockedError 属 Exception，走超时/断连同款 (None, None)
    口径（各 _default_* 函数内已有该分支）。
    """
    verify_outbound_url(request.full_url)
    return _OPENER.open(request, timeout=timeout)

"""播放地址与媒体路径的安全校验

远程播放目标在交给 httpx / ffmpeg 之前必须过一遍这里。两层口径，别混：

- **配置来源的直链**（本机 `paths`、存储挂载、`.strm` 文件内容）：由运维在自己的机器 /
  自己的网盘上配置，指向内网是**正常用法**（局域网 NAS、自建 WebDAV / AList / MinIO、
  本机回环上的转发器都是这个项目的一等场景）。所以这里只拦真正无歧义的东西——
  协议必须是 http(s)、URL 里不许带内嵌凭据；**是否拦内网地址由
  ``EMBY_BLOCK_PRIVATE_MEDIA_URLS`` 决定（默认关，打开后配合 ``EMBY_MEDIA_URL_ALLOWLIST``
  放行自己的 NAS / 回环地址）**。
- **服务器自己追出去的重定向目标**（``reject_private_url``）：源站回一个 302 指向
  ``http://169.254.169.254/…`` 或内网运维接口，代理会带着你的凭据去取——这是真正要挡的 SSRF，
  所以**一律拦内网 / 回环，不受上面那个开关影响**。

``EMBY_MEDIA_URL_ALLOWLIST`` 两边都认：它是运维的**显式**决定（“这个主机我知道是谁”），
而不是一个绕过的口子。
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urlsplit

# 判定为「内部地址」的口径：内网 / 回环 / 链路本地（含云元数据 169.254.169.254）/
# 组播 / 保留段 / 未指定地址
_PRIVATE_FLAGS = ("is_private", "is_loopback", "is_link_local", "is_multicast",
                  "is_reserved", "is_unspecified")


def _allowed_hosts() -> set[str]:
    raw = os.getenv("EMBY_MEDIA_URL_ALLOWLIST", "")
    return {item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip()}


def block_private_urls() -> bool:
    """是否默认拒绝指向内网 / 回环的媒体地址（``EMBY_BLOCK_PRIVATE_MEDIA_URLS``，默认关）

    默认关不是「忘了开」：这个项目的媒体来源本来就大量在内网（局域网 NAS、自建 WebDAV /
    AList / MinIO、rclone 的本地 HTTP 端点）。默认拦会把这些部署的播放全部弄坏，
    而它们本来就得先有媒体库管理员权限才能配出来。
    """
    return os.getenv("EMBY_BLOCK_PRIVATE_MEDIA_URLS", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _private(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host.rstrip(".").lower() in {"localhost", "localhost.localdomain"}
    return any(getattr(addr, flag) for flag in _PRIVATE_FLAGS)


def _resolve_host_ips(host: str) -> list[str] | None:
    """解析主机名 → IP 字符串列表。

    解析失败返回 None（不是空列表）：名字解析不了不代表它指向内网，真正的请求
    会以「源站不可达」如实失败。曾经这里 fail-closed，结果 DNS 抖动 / 离线 /
    临时故障时公开 CDN 域名被当成内网地址拒掉（生产事故）——绝不重演。
    """
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return None
    return [record[4][0] for record in records]


def _resolves_private(host: str) -> bool:
    """域名解析后落到内网地址才算内部地址

    **解析失败一律当作「不是内部地址」**：名字解析不了并不代表它指向内网，而且真正的
    请求马上会以「源站不可达」失败——曾经的写法在这里 fail-closed，
    结果「DNS 里没有这个名字」被当成「内网地址」拒掉（离线 / 内网 DNS / 临时故障
    都会命中，连公开 CDN 域名都播不了）。
    """
    ips = _resolve_host_ips(host)
    if ips is None:
        return False
    return any(_private(ip) for ip in ips)


def _validate_url(raw_url: str, *, reject_private: bool) -> tuple[str, dict[str, list[str]]]:
    """统一的 URL 校验：协议 / 内嵌凭据始终检查，内网地址按需检查

    返回 ``(规范化 URL, dns_pinning)``。dns_pinning 形如 ``{host: [ip, ...]}``，
    是校验时刻的 DNS 快照——调用方把它喂给 pin 版 transport（``dns_pinning.py``），
    建连时直接连 pin 住的 IP，不再二次解析，从而堵住 DNS rebinding TOCTOU
    （校验时返回公网 IP、建连时返回内网 IP 的绕过）。

    DNS 解析失败时 pinning 为空 dict：保持 fail-open（见 ``_resolve_host_ips``
    的事故说明），不重演误拦。
    """
    from .mounts import MountError

    value = (raw_url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise MountError("播放地址必须是 http 或 https URL")
    if parsed.username or parsed.password:
        raise MountError("播放地址禁止携带内嵌凭据")
    pinning: dict[str, list[str]] = {}
    if reject_private:
        host = parsed.hostname.lower().rstrip(".")
        if host not in _allowed_hosts():
            if _private(host):
                raise MountError("播放地址指向受保护的内部地址")
            ips = _resolve_host_ips(host)
            if ips is not None:
                if any(_private(ip) for ip in ips):
                    raise MountError("播放地址指向受保护的内部地址")
                pinning[host] = ips
            # ips is None → 解析失败：fail-open，不 pin（事故教训）
    return value, pinning


def validate_remote_url(raw_url: str, *, allow_private: Optional[bool] = None) -> str:
    """校验媒体直链（配置来源用这个）

    ``allow_private`` 默认跟随 ``EMBY_BLOCK_PRIVATE_MEDIA_URLS``（默认允许内网地址）。
    """
    if allow_private is None:
        allow_private = not block_private_urls()
    url, _ = _validate_url(raw_url, reject_private=not allow_private)
    return url


def reject_private_url(raw_url: str) -> str:
    """校验**服务器自己要追过去取**的地址（重定向目标）：一律不许指向内网 / 回环"""
    url, _ = _validate_url(raw_url, reject_private=True)
    return url


def validate_url_pinned(raw_url: str, *, reject_private: bool) -> tuple[str, dict[str, list[str]]]:
    """校验 URL 并返回 DNS pinning（给 pin 版 transport 用，堵 DNS rebinding TOCTOU）。

    返回 ``(规范化 URL, {host: [ip, ...]})``。DNS 解析失败时 pinning 为空 dict
    （fail-open，见 ``_resolve_host_ips`` 的事故说明），调用方照常放行、
    建连走正常 DNS。
    """
    return _validate_url(raw_url, reject_private=reject_private)


def safe_local_path(root: str, relative: str) -> str:
    """Resolve a local media path without traversal or symlink escape."""
    import os
    root_real = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_real, relative))
    try:
        inside = os.path.commonpath([root_real, candidate]) == root_real
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("媒体路径超出挂载根目录")
    current = root_real
    for part in relative.replace(os.sep, "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("媒体路径包含非法目录遍历")
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise ValueError("媒体路径禁止经过符号链接")
    return candidate


def validate_local_file_path(path: str) -> str:
    """Validate an existing absolute local path and reject symlink traversal."""
    import os
    value = os.path.abspath(path)
    current = os.path.sep
    for part in value.split(os.path.sep):
        if not part:
            continue
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise ValueError("媒体路径禁止经过符号链接")
    return value


def safe_child_name(root: str, name: str) -> str:
    """Resolve a single generated-media filename below *root*."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("媒体文件名非法")
    return safe_local_path(root, name)

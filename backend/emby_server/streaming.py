"""自建 Emby 服务器：流媒体服务（直连流 / Range / HLS 转码）"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import formatdate
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from backend.emby_server import hwaccel as _hwaccel

# 开箱即用播放优化：单文件并发 Range 限流（只此一处实现）
from backend.emby_server.playback_tune import RANGE_LIMITER
from urllib.parse import parse_qsl, urljoin, urlsplit

from backend.emby_server.mounts import MOUNT_UA, MountError

if TYPE_CHECKING:
    # httpx 运行时是延迟导入（各函数内按需 import，保持模块导入轻量）；
    # 但模块注解引用了 httpx.AsyncClient / httpx.Timeout，这里给类型检查器一个声明，
    # 消除未定义名称（pyflakes F821）。运行时不执行，不影响延迟导入。
    import httpx

logger = logging.getLogger(__name__)



def _drive_alt_media_file_id(url: str) -> Optional[str]:
    """从 Drive alt=media URL 提取 file_id。

    https://www.googleapis.com/drive/v3/files/<fid>?alt=media → fid；
    非此格式（115/WebDAV/其它）返回 None。
    """
    try:
        parts = urlsplit(url or "")
        if parts.netloc != "www.googleapis.com":
            return None
        segs = parts.path.strip("/").split("/")
        if len(segs) == 4 and segs[0] == "drive" and segs[1] == "v3" \
                and segs[2] == "files" and segs[3]:
            from urllib.parse import unquote
            return unquote(segs[3])
    except Exception:
        pass
    return None


def _drive_403_error_reason(body) -> tuple:
    """解析 Google Drive API 403 错误体，返回 (reason, message)；解析失败返回 ("", "")，永不抛异常。"""
    try:
        import json  # 局部导入，兼容文件内既有风格

        if not body:
            return ("", "")
        data = json.loads(body)
        if not isinstance(data, dict):
            return ("", "")
        err = data.get("error")
        if not isinstance(err, dict):
            return ("", "")

        errors = err.get("errors")
        first = {}
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]

        reason = first.get("reason", "")
        if not isinstance(reason, str):
            reason = str(reason) if reason is not None else ""

        message = err.get("message")
        if not message:
            message = first.get("message", "")
        if not isinstance(message, str):
            message = str(message) if message is not None else ""
        message = message[:200]

        return (reason, message)
    except Exception:
        return ("", "")


TRANSCODE_DIR = os.getenv("EMBY_TRANSCODE_DIR", "/tmp/emby_transcode")
# 远程挂载（115 / WebDAV / AList）代理转发时的 UA。
# 同一环境变量只许一处默认值（横切纪律）：默认值在 mounts.MOUNT_UA 定义
# （115 优化的 PAN115_UA；115 直链对 UA 有偏好），这里直接复用，不另起一套。
REMOTE_UA = MOUNT_UA
FFMPEG = os.getenv("EMBY_FFMPEG_PATH", "ffmpeg")
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")

CHUNK = 1024 * 256

# 中转零拷贝优化：实际读盘/读源站的块大小。
# 256KB 小块意味着每秒上百次「线程池提交 + FUSE read 系统调用 + Python 层拷贝」；
# 1MB 大块把这些开销砍到 1/4，首字节不受影响（第一块照样立即发出）。
# 注意：CHUNK 仍用于「客户端没给 Range 结束位置时的默认区间长度」计算，别混用。
READ_CHUNK = 1024 * 1024

# rclone FUSE 挂载点前缀（/mnt/mp、/mnt/paul，可用 EMBY_FUSE_PREFIXES 覆盖）。
# 这些路径的读可能因 Drive 配额/网络抖动中途失败。
FUSE_PREFIXES = tuple(
    p for p in os.getenv("EMBY_FUSE_PREFIXES", "/mnt/mp/,/mnt/paul/").split(",") if p
)

# FUSE 读遇到 OSError 时的重试次数与退避基数（秒，线性退避）。
_FUSE_READ_RETRIES = 3
_FUSE_READ_RETRY_DELAY = 0.5


def is_fuse_path(path: str) -> bool:
    """是不是 rclone FUSE 挂载上的文件（读可能中途失败，不能预先声明 Content-Length）。"""
    return bool(path) and path.startswith(FUSE_PREFIXES)


# 远程代理最多自己追几次重定向（与 httpx 的默认上限一致）
MAX_REDIRECTS = 5
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_CREDENTIAL_HEADERS = {"authorization", "cookie", "proxy-authorization"}


def _redirect_headers(forward: dict, from_url: str, to_url: str) -> dict:
    """跨主机重定向时丢掉凭据头

    WebDAV 的 Basic / 115 的 Cookie 是发给**源站**的；源站回一个 302 指向第三方主机时
    httpx 会把同样的头带过去——等于把凭据交给重定向目标。同主机（含换端口以外的同站）
    保留，跨主机一律剥掉。

    Drive 例外：Drive 文件保持私有，服务端用 SA 的 Bearer token 拉流；Google 下载链
    常在 Drive 域名之间跳转（drive.google.com → drive.usercontent.google.com），
    目标仍在 Drive 域名白名单内时保留 ``Authorization``（其余凭据头照剥），
    否则 token 会在跳转时丢失、私有文件拉流失败。
    """
    same_host = urlsplit(from_url).hostname == urlsplit(to_url).hostname
    if same_host:
        return forward
    to_host = (urlsplit(to_url).hostname or "").lower()
    try:
        # 延迟导入：drive_auth 只依赖标准库 + drive_changes，无循环导入风险；
        # 但 streaming 是播放热路径，异常时退回原逻辑（全剥）。
        from backend.emby_server.drive_auth import DRIVE_HOSTS
        drive_hosts: frozenset = DRIVE_HOSTS
    except Exception:  # noqa: BLE001
        drive_hosts = frozenset()
    if to_host in drive_hosts:
        # 只保留 authorization（SA Bearer token），cookie / proxy-authorization 照剥
        return {k: v for k, v in forward.items()
                if k.lower() == "authorization" or k.lower() not in _CREDENTIAL_HEADERS}
    return {k: v for k, v in forward.items() if k.lower() not in _CREDENTIAL_HEADERS}


def _next_redirect(url: str, location: str) -> tuple[str, dict[str, list[str]]]:
    """解析重定向目标并做安全校验（内网 / 回环 / 非法协议一律拒绝）。

    返回 ``(目标 URL, dns_pinning)``：pinning 是校验时刻的 DNS 快照，调用方把它
    并入 pin 版 transport（见 ``dns_pinning.py``），建连时直接连 pin 住的 IP，
    不再二次解析——堵住 DNS rebinding TOCTOU（A-P2-2）。

    DNS 解析失败时 pinning 为空 dict：保持 fail-open（见 ``playback_security``
    的事故说明），不重演「DNS 抖动误拦公开 CDN」的生产事故。
    """
    from backend.emby_server.playback_security import validate_url_pinned

    return validate_url_pinned(urljoin(url, location or ""), reject_private=True)


def _range_header(start: int, end: int, total: int) -> dict:
    return {
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }


def _validators_header(path: str) -> dict:
    """给 206 分片响应补 ETag/Last-Modified，让 CDN 能落盘缓存"""
    try:
        etag, last_modified = _file_validators(os.stat(path))
        return {"ETag": etag, "Last-Modified": last_modified}
    except OSError:
        return {}


def serve_file(path: str, request: Request, media_type: str = "video/mp4",
               cache_control: Optional[str] = None) -> StreamingResponse:
    """带 Range 支持的文件流式响应

    ``cache_control``（CDN 预留，第 2/3 层）：分片请求传 ``public, max-age…``，
    让 CDN 边缘缓存热门分片；None 时不发缓存头（行为与升级前一致）。

    FUSE 说明：rclone 挂载（/mnt/mp、/mnt/paul）的读可能因 Drive 配额/网络抖动
    中途失败。若 Range 响应预先声明 Content-Length 而实际字节不足，uvicorn 会抛
    Response content shorter than Content-Length。因此 FUSE 路径的 Range 响应
    不声明 Content-Length（走 chunked），读失败时优雅截断，播放器会重试该分片。
    本地磁盘路径保持原有行为（声明 Content-Length）。
    """
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Media file not found")
    size = os.path.getsize(path)
    fuse = is_fuse_path(path)

    extra_headers = {"Accept-Ranges": "bytes", "X-Accel-Buffering": "no"}
    if cache_control:
        extra_headers["Cache-Control"] = cache_control

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers=dict(extra_headers))

    m = RANGE_RE.match(range_header)
    if not m:
        raise HTTPException(status_code=416, detail="Invalid Range")

    raw_start, raw_end = m.group(1), m.group(2)
    if not raw_start:
        # 后缀形式 ``bytes=-500``：表示「文件最后 500 字节」。旧实现当成 start=0/end=500，
        # 返回的是文件**开头**——播放器探测 MP4 尾部 moov 时拿到错字节，部分会黑屏/ \
        # 播放失败。这里按 RFC 7233 解析成末尾区间。
        suffix = int(raw_end or 0)
        if suffix <= 0:
            raise HTTPException(status_code=416, detail="Invalid Range")
        start, end = max(0, size - suffix), size - 1
    else:
        start = int(raw_start)
        end = int(raw_end) if raw_end else min(start + CHUNK * 200, size - 1)
    end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPException(status_code=416, detail="Requested range not satisfiable")

    # 单文件并发 Range 限流：同一文件最多 3 路并发读。
    # （移动端常开 5~7 个并发，7 路同时回源 Drive 会把带宽打满）。
    # 用非阻塞 try_acquire：事件循环里不能调 blocking 的 acquire（会卡住整个
    # 循环）。拿不到名额直接 503，播放器会重试，效果等同排队。
    # 租约 token（r13）：按 lease_id 精确归还，迟到释放会被忽略（ABA 漂移根治）。
    lease = RANGE_LIMITER.try_acquire(path)
    if lease is None:
        raise HTTPException(status_code=503, detail="Server busy, retry later")

    def _guarded_iter():
        try:
            yield from iter_file()
        finally:
            RANGE_LIMITER.release(path, lease)

    def iter_file():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            retries = 0
            while remaining > 0:
                try:
                    data = f.read(min(READ_CHUNK, remaining))
                except OSError as exc:
                    # FUSE 瞬时抖动：退避重试几次再放弃
                    retries += 1
                    if retries > _FUSE_READ_RETRIES:
                        logger.warning("FUSE 读失败放弃 %s [%d-%d]: %s", path, start, end, exc)
                        break
                    time.sleep(_FUSE_READ_RETRY_DELAY * retries)
                    continue
                if not data:
                    # rclone 放弃重试（如 Drive 403 配额耗尽）后读返回空。
                    # FUSE 下走 chunked（无 Content-Length），这里优雅截断：
                    # 播放器会把这次当成普通网络中断并重试该分片，而不会触发
                    # 服务端的 Response content shorter than Content-Length。
                    if fuse:
                        sent = (end - start + 1) - remaining
                        logger.warning(
                            "FUSE 读提前结束 %s [%d-%d]，已发送 %d/%d 字节",
                            path, start, end, sent, end - start + 1,
                        )
                    break
                retries = 0
                remaining -= len(data)
                yield data

    range_headers = _range_header(start, end, size)
    if fuse:
        # FUSE 路径的 Range 响应不声明 Content-Length，走 chunked，避免中途
        # 读失败时声明长度与实际字节对不上。Content-Range 保留（206 必需）。
        range_headers.pop("Content-Length", None)
    return StreamingResponse(
        _guarded_iter(),
        status_code=206,
        media_type=media_type,
        headers={**range_headers, **_validators_header(path), **extra_headers},
    )


def headers_arg(headers: Optional[dict]) -> list[str]:
    """把请求头转成 ffmpeg/ffprobe 的 ``-headers`` 参数（Cookie / 令牌不出服务器）"""
    if not headers:
        return []
    joined = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return ["-headers", joined]


# A-P2-2 DNS pinning 共享缓存（async 路径）：host -> [ip, ...]，TTL 300 秒。
# 只有经 ``_next_redirect`` 校验通过的重定向目标才会写入；每次建连时查。
# 总开关 EMBY_DNS_PINNING 在 transport 层实时检查，关掉即回退到正常 DNS。
from backend.emby_server.dns_pinning import PinningCache as _PinningCache
from backend.emby_server.dns_pinning import dns_pinning_enabled

_PINNING_CACHE = _PinningCache(ttl=300.0)

def _env_number(name: str, default: float, lo: float, hi: float, cast=float):
    """读数值型环境变量并夹到 [lo, hi]；非法值回落默认值（不让一个手误拖垮启动）"""
    raw = os.getenv(name, "")
    try:
        value = cast(raw) if str(raw).strip() else cast(default)
    except (TypeError, ValueError):
        logger.warning("环境变量 %s=%r 不是合法数值，使用默认值 %s", name, raw, default)
        value = cast(default)
    return max(cast(lo), min(cast(hi), value))


def _env_flag(name: str, default: bool) -> bool:
    """读布尔型环境变量；非法值记 warning 并回落默认值（fail-closed）"""
    raw = os.getenv(name, "")
    if not str(raw).strip():
        return default
    v = str(raw).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    logger.warning("环境变量 %s=%r 不是合法布尔值，使用默认值 %s", name, raw, default)
    return default


#: 同时保持的上游连接数（S2）。每个正在播放的远程流在整个播放期间占一个连接，
#: 旧默认 24 在第 25 路并发播放时就排队；默认提到 200，``RELAY_MAX_CONNECTIONS`` 可调。
async def _follow_redirects(
    client,
    start_url: str,
    forward: dict,
    silent: bool = False,
) -> tuple:
    """手动跟随 HTTP 重定向（resolve_drive_final_url 用）。

    每一跳都走 ``_next_redirect`` 内网校验 + ``_redirect_headers`` 跨主机剥凭据；
    中间跳的响应及时 aclose 归还连接池；终态响应**不关**，由调用方消费/关闭。

    返回 ``(terminal_resp, final_url, hops)``。

    ``silent``：False（播放口径）→ 非法重定向目标抛 HTTPException(403)、跳转超限
    抛 HTTPException(502)；True（预热口径）→ 这两种情况都返回 ``(None, current, hops)``，
    调用方静默失败。两种口径下失败时已打开的中间响应都已关闭，无泄漏。
    """
    current = start_url
    hops = 0
    resp = None
    for _ in range(MAX_REDIRECTS + 1):
        resp = await client.send(
            client.build_request("GET", current, headers=forward),
            stream=True,
        )
        location = resp.headers.get("location")
        if resp.status_code not in _REDIRECT_CODES or not location:
            break  # 非跳转码 / 3xx 却没给 Location：当作终态
        try:
            target, _hop_pinning = _next_redirect(current, location)
        except MountError as exc:
            await resp.aclose()
            resp = None
            logger.warning("拒绝代理到重定向目标 %s: %s", location, exc)
            if not silent:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            return (None, current, hops)
        except Exception as exc:
            # 畸形 Location（如 http://[::1）：原来落到外层 except → 502
            await resp.aclose()
            resp = None
            if not silent:
                logger.warning("重定向目标解析失败 %s: %s", location, exc)
                raise HTTPException(status_code=502, detail="源站不可达") from exc
            return (None, current, hops)
        await resp.aclose()
        resp = None
        # A-P2-2：把校验时刻的 DNS 快照存入共享 pin 缓存，下一跳建连直接走
        # pin 住的 IP，不再二次解析（堵 DNS rebinding TOCTOU）。
        # 开关关闭时不写：避免开关重新打开瞬间用上陈旧 pin。
        if dns_pinning_enabled():
            for _pin_host, _pin_ips in _hop_pinning.items():
                _PINNING_CACHE.store(_pin_host, _pin_ips)
        forward = _redirect_headers(forward, current, target)
        current = target
        hops += 1
    else:
        if resp is not None:
            await resp.aclose()
        if not silent:
            raise HTTPException(status_code=502, detail="源站重定向次数过多")
        return (None, current, hops)
    return (resp, current, hops)


def _drive_redirect_client() -> "httpx.AsyncClient":
    """resolve_drive_final_url 专用的独立小客户端。

    中继全删后不再用共享 relay 连接池：按次创建、用完关闭。
    保留 DNS pinning 校验（堵 DNS rebinding TOCTOU）与
    follow_redirects=False（跳转由本模块自己逐跳校验）。
    """
    import httpx

    from backend.emby_server.dns_pinning import PinnedAsyncHTTPTransport

    return httpx.AsyncClient(
        transport=PinnedAsyncHTTPTransport(
            _PINNING_CACHE.lookup,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ),
        timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=3.0),
        follow_redirects=False,
    )


async def resolve_drive_final_url(
    url: str,
    headers: dict | None = None,
) -> str | None:
    """解析 Drive 最终下载地址（SEAM-1：中继全删后的保守保留）。

    原始 URL → drive_api_media_url 转 API 地址 → 查 P0-2 跳转缓存 →
    未命中则手动跟随跳转 → 写缓存。

    返回最终 URL；任何失败返回 None（不抛异常，调用方静默）。
    用独立小客户端（按次建、用完关）；中间响应及时关闭。
    """
    try:
        # 1. uc 直链转 Drive API 地址
        try:
            from backend.emby_server.drive_auth import drive_api_media_url
            api_url = drive_api_media_url(url)
        except Exception:
            api_url = url
        if not api_url:
            return None

        # 2. 手动跟随跳转
        client = _drive_redirect_client()

        forward = {k: v for k, v in (headers or {}).items()}
        forward.pop("X-Pickcode", None)
        forward.setdefault("User-Agent", REMOTE_UA)

        try:
            resp, current, hops = await _follow_redirects(
                client, api_url, forward, silent=True)
            if resp is None:
                return None  # 非法重定向目标 / 跳转超限：静默失败
            try:
                terminal_status = resp.status_code
                if terminal_status in (403, 404, 410):
                    if terminal_status == 403 and _drive_alt_media_file_id(current):
                        try:
                            _pbody = await resp.aread()
                        except Exception:
                            _pbody = b""
                        _preason, _pmsg = _drive_403_error_reason(_pbody[:4096])
                        logger.warning("Drive 跳转解析 403 file_id=%s reason=%s msg=%.120s",
                                       _drive_alt_media_file_id(current),
                                       _preason or "-", _pmsg or "-")
                    return None
                return current
            finally:
                await resp.aclose()
        finally:
            await client.aclose()
    except Exception:
        # 调用方静默：任何异常都不抛
        return None


# ---------------------------------------------------------------------------
# Drive 302 目标：服务端跟链后的最终地址（2026-10-23 防盗链定版）
# ---------------------------------------------------------------------------
# 此前三处 302（video_stream / Download / File）直接把 usercontent 地址
# 下发给客户端：URL 里 drive_file_id 明文 + 拼法公开固定 + 文件是
# anyone reader —— 等于把可反复换字节的「永久入口」发给了客户端，
# play_sign 的 900 秒只守本站一跳（审计 playback-address-exposure-
# audit-20261023【高】）。改法（学随影）：服务端先跟链（只取响应头、
# 不下载字节）拿到短命的最终地址，只下发最终地址；解析失败/超时才
# 回落 legacy 地址保播放，并记 warning 留痕。
# 最终地址进程内缓存 300 秒：热门片重复点播不重复跟链，不给起播加延迟。

_FINAL_URL_CACHE: dict[str, tuple[str, float]] = {}
_FINAL_URL_CACHE_LOCK = threading.Lock()
_FINAL_URL_CACHE_TTL = 300.0
_FINAL_URL_CACHE_MAX = 1024
_FINAL_URL_RESOLVE_TIMEOUT = 8.0


def _final_url_cache_get(file_id: str) -> str | None:
    now = time.monotonic()
    with _FINAL_URL_CACHE_LOCK:
        hit = _FINAL_URL_CACHE.get(file_id)
        if hit and hit[1] > now:
            return hit[0]
        if hit:
            _FINAL_URL_CACHE.pop(file_id, None)
    return None


def _final_url_cache_put(file_id: str, url: str) -> None:
    with _FINAL_URL_CACHE_LOCK:
        if len(_FINAL_URL_CACHE) >= _FINAL_URL_CACHE_MAX:
            oldest = min(_FINAL_URL_CACHE, key=lambda k: _FINAL_URL_CACHE[k][1])
            _FINAL_URL_CACHE.pop(oldest, None)
        _FINAL_URL_CACHE[file_id] = (url, time.monotonic() + _FINAL_URL_CACHE_TTL)


async def drive_final_location(file_id: str) -> str:
    """给客户端的 Drive 302 目标（最终地址优先，legacy 回落）。永不抛异常。"""
    fid = (file_id or "").strip()
    if not fid:
        return ""
    cached = _final_url_cache_get(fid)
    if cached:
        return cached
    from backend.emby_server.drive_auth import drive_usercontent_download_url
    legacy = drive_usercontent_download_url(fid)
    try:
        final = await asyncio.wait_for(
            resolve_drive_final_url(legacy), timeout=_FINAL_URL_RESOLVE_TIMEOUT)
    except Exception:
        final = None
    if final:
        _final_url_cache_put(fid, final)
        return final
    logger.warning("Drive 最终地址解析失败，回落 legacy 302: file_id=%s", fid)
    return legacy


def drive_final_location_sync(file_id: str) -> str:
    """drive_final_location 的同步版（Download / File 等同步路由用）。

    同步路由跑在线程池里、无事件循环，直接 asyncio.run 一次；
    异常时回落 legacy 地址，永不抛。
    """
    try:
        return asyncio.run(drive_final_location(file_id))
    except Exception:
        try:
            from backend.emby_server.drive_auth import drive_usercontent_download_url
            return drive_usercontent_download_url((file_id or "").strip())
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# 远程挂载专用中继（2026-10-17：中继全删 P1 回归修复，用户拍板）
# ---------------------------------------------------------------------------
# 只服务远程挂载条目（115 / rclone 等 ``mount://`` 路径解析出的
# ``kind="url"`` 目标）：这类直链依赖挂载凭据（Cookie/令牌）与特定 UA，
# 下发给客户端即死，必须由服务端带头代理转发。调用方（api.video_stream）
# 保证：Drive 条目一律先走 302，**不许**经由这里。
#
# 与已删除的 ``serve_remote_async`` 的区别（刻意不带回来）：
# - 无共享中继连接池（``RELAY_*`` 已删）：每请求一个独立小客户端，用完即关；
#   远程挂载并发量小，这点开销可接受。
# - 无塌速重连 / Singleflight / 分块共享：用户要求全删，不复活。
# - 无 Drive 特有逻辑：uc→alt=media 转换、配额负缓存、403 换 SA 重试、
#   401 失效标记——这些是 Drive 链路的，远程挂载用不上。
# - 保留：凭据头转发（``X-Pickcode`` 是内部头，不转发给上游）、Range 透传、
#   手动跟随跳转（``_follow_redirects``：逐跳内网校验 + 跨主机剥凭据 +
#   DNS pinning）、115 直链过期清缓存（403/404/410 + X-Pickcode）、
#   响应头透传、流式转发。

_MOUNT_RELAY_TIMEOUT_CONNECT = 10.0
_MOUNT_RELAY_TIMEOUT_READ = 30.0


def _mount_relay_client() -> "httpx.AsyncClient":
    """远程挂载中继的独立小客户端：按次创建、用完关闭。

    口径照抄 ``_drive_redirect_client``（中继全删后不再有共享 relay 池）：
    DNS pinning 防 rebinding、跳转由 ``_follow_redirects`` 逐跳校验后手动跟。
    """
    import httpx

    from backend.emby_server.dns_pinning import PinnedAsyncHTTPTransport

    return httpx.AsyncClient(
        transport=PinnedAsyncHTTPTransport(
            _PINNING_CACHE.lookup,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        ),
        timeout=httpx.Timeout(
            connect=_MOUNT_RELAY_TIMEOUT_CONNECT,
            read=_MOUNT_RELAY_TIMEOUT_READ,
            write=30.0,
            pool=10.0,
        ),
        follow_redirects=False,
    )


async def serve_mount_relay_async(
    url: str,
    request: Request,
    headers: Optional[dict] = None,
    media_type: str = "video/mp4",
    pt=None,
):
    """远程挂载专用中继：服务端带挂载凭据取流、流式转发给播放器。

    - ``headers``：挂载解析出的鉴权头（UA/Referer/Cookie），只在本机使用、
      不下发给客户端；``X-Pickcode`` 是 115 缓存失效用的内部头，不转发。
    - Range 透传：客户端的 ``Range`` 原样发给上游，上游的 200/206 与
      ``Content-Range``/``Accept-Ranges`` 等头原样透回。
    - ``pt``（可选）：播放链路计时器，只加日志不改逻辑。
    """
    import httpx

    forward = {k: v for k, v in (headers or {}).items()}
    # X-Pickcode 是内部头（115 直链缓存失效用），不转发给上游
    forward.pop("X-Pickcode", None)
    forward.setdefault("User-Agent", REMOTE_UA)
    range_header = request.headers.get("range")
    if range_header:
        forward["Range"] = range_header

    client = _mount_relay_client()
    resp = None
    try:
        try:
            # 跳转自己逐跳校验后跟随（内网目标拒绝、跨主机剥凭据），终态
            # 响应不关，由下面的流消费/关闭；silent=False 口径：非法重定向
            # 目标抛 403、跳转超限抛 502。
            resp, _current, _hops = await _follow_redirects(
                client, url, forward, silent=False)
        except HTTPException:
            raise
        except httpx.PoolTimeout as exc:
            raise HTTPException(
                status_code=503, detail="远程挂载代理繁忙，请稍后重试",
                headers={"Retry-After": "2"}) from exc
        except Exception as exc:  # noqa: BLE001 — 源站不可达：给干净的 502
            logger.warning("远程挂载代理失败 %s: %s", url.split("?")[0], exc)
            raise HTTPException(status_code=502, detail="源站不可达") from exc
        if pt is not None:
            pt.mark("ttfb")
        # 115 直链过期检测：403/404/410 时清 pickcode 缓存，客户端重试时
        # 挂载解析会拿到新直链（沿用删除前的口径）。
        if resp.status_code in (403, 404, 410):
            _pickcode = (headers or {}).get("X-Pickcode")
            if _pickcode:
                try:
                    from backend.emby_server.mounts import _invalidate_115_url
                    _invalidate_115_url(_pickcode)
                except Exception as exc:
                    # r46补遗：清缓存失败不能静默——否则死直链继续被沿用，
                    # 后续播放反复 403/502 却无日志可查。
                    logger.warning("115 直链缓存失效失败 pickcode=%s: %s",
                                   _pickcode, exc)
        if resp.status_code >= 400:
            status = resp.status_code
            await resp.aclose()
            resp = None
            logger.warning("远程挂载源站返回 %s: %s", status, url.split("?")[0])
            raise HTTPException(
                status_code=502 if status >= 500 else status,
                detail=f"源站返回 {status}")

        passthrough = {}
        for name in ("content-range", "accept-ranges", "content-length",
                     "last-modified", "etag"):
            value = resp.headers.get(name)
            if value:
                passthrough[name.title()] = value
        # 与本地路径同口径：远程源支持 Range，上游没给也要补
        passthrough.setdefault("Accept-Ranges", "bytes")
        passthrough["X-Accel-Buffering"] = "no"
        # 边缘缓存调试头（通用能力）：EDGE_CACHE_MODE=query_ignore 时声明
        try:
            from backend.emby_server import edge_cache
            _edge_dbg = edge_cache.debug_header_value()
            if _edge_dbg:
                passthrough[edge_cache.DEBUG_HEADER] = _edge_dbg
        except Exception:
            pass
        content_type = resp.headers.get("content-type") or media_type

        async def iter_mount():
            try:
                async for chunk in resp.aiter_bytes(READ_CHUNK):
                    yield chunk
            except httpx.TransportError as exc:
                # 源站中途断开：结束这条流、归还连接；响应头已发出，
                # 播放器会对剩余区间重新发 Range。
                logger.warning("远程挂载流中断 %s: %s",
                               url.split("?")[0], type(exc).__name__)
            finally:
                # 只关响应和本请求的独立客户端——client 不是共享的，
                # 关掉不影响其他请求。
                try:
                    await resp.aclose()
                except Exception:
                    pass
                try:
                    await client.aclose()
                except Exception:
                    pass

        if pt is not None:
            pt.done()
        return StreamingResponse(
            iter_mount(), status_code=resp.status_code,
            media_type=content_type, headers=passthrough,
        )
    except HTTPException:
        if resp is not None:
            try:
                await resp.aclose()
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass
        raise


def _file_validators(stat: os.stat_result) -> tuple[str, str]:
    """按「mtime + 大小」生成 ETag 与 Last-Modified（内容一变校验器就变）"""
    return f'"{int(stat.st_mtime)}-{stat.st_size}"', formatdate(stat.st_mtime, usegmt=True)


def serve_image(path: Optional[str]) -> FileResponse:
    """图片响应：带 ETag / Last-Modified 与 24h 客户端强制缓存

    浏览媒体库时同一张海报会被反复请求（滚动、返回、切页），旧实现每次整文件重发。
    现在响应带 ``ETag`` / ``Last-Modified`` 与 ``Cache-Control: public, max-age=86400``，
    浏览器与客户端在有效期内直接命中本地缓存，不再为同一张图重复付出磁盘 I/O 与带宽。
    """
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Image not found")
    ext = os.path.splitext(path)[1].lower()
    media_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    try:
        stat = os.stat(path)
    except OSError:  # 判断与取属性之间文件被删 / 挂载掉线：干净 404，不要 500
        raise HTTPException(status_code=404, detail="Image not found") from None
    etag, last_modified = _file_validators(stat)
    headers = {
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "public, max-age=86400",
        "Accept-Ranges": "bytes",
    }
    return FileResponse(path, media_type=media_type, headers=headers)


def build_hls_command(
    file_path: str,
    out_dir: str,
    start_seconds: float = 0,
    video_bitrate: int = 4_000_000,
    audio_bitrate: int = 128_000,
    height: Optional[int] = None,
    copy_video: bool = False,
    input_headers: Optional[dict] = None,
) -> subprocess.Popen:
    """启动 ffmpeg HLS 转码进程

    ``file_path`` 可以是本机文件，也可以是远程直链（挂载来源）：ffmpeg 本身支持 http(s)
    输入，凭据通过 ``input_headers`` 传入。
    """
    os.makedirs(out_dir, exist_ok=True)
    playlist = os.path.join(out_dir, "master.m3u8")

    seek = ["-ss", str(start_seconds)] if start_seconds > 0 else []
    if copy_video:
        vcodec = ["-c:v", "copy"]
        hw_device_args = []  # 直通无需硬件设备
    else:
        # D-P1-3：设备参数（如 -vaapi_device）是输入前置选项，必须放在 -i 之前
        hw_device_args = _hwaccel.get_hw_device_args()
        vcodec = _hwaccel.get_encoder_args() + [
            "-b:v", str(video_bitrate),
            "-maxrate", str(int(video_bitrate * 1.2)), "-bufsize", str(video_bitrate * 2),
        ]
        # P1-5: vaapi/qsv 要求先软件 scale 再 hwupload（硬件帧不能再过软件 scale），
        # 由 get_production_filter 统一拼出完整生产滤镜链（与自检共用同一组装）。
        prod_filter = _hwaccel.get_production_filter(height=height)
        if prod_filter:
            vcodec += ["-vf", prod_filter]
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        *hw_device_args,
        *seek, *headers_arg(input_headers), "-i", file_path,
        *vcodec,
        "-c:a", "aac", "-b:a", str(audio_bitrate), "-ac", "2",
        "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
        "-hls_flags", "independent_segments+temp_file",
        "-hls_segment_filename", os.path.join(out_dir, "seg%05d.ts"),
        playlist,
    ]
    # ffmpeg 的报错不能丢进 DEVNULL：deploy-ea.md 让运维在服务日志里找转码失败原因，
    # 而日志里必须真的有原因。改写到会话目录里的 ffmpeg.log（随会话目录一起清理），
    # 异常退出时再把尾部提升到服务日志（见 _log_ffmpeg_tail）。
    log_path = os.path.join(out_dir, "ffmpeg.log")
    with open(log_path, "wb") as log_file:
        return spawn_ffmpeg(cmd, stdout=subprocess.DEVNULL, stderr=log_file)


# ---- ffmpeg 子进程生命周期（审查 S7）----
#
# 1. ``start_new_session=True``：ffmpeg 自成进程组，停止时整组发信号（被 nice/sh 包一层
#    或 ffmpeg 自己拉起的子进程也一并带走）；
# 2. Linux 上 ``PR_SET_PDEATHSIG(SIGTERM)``：本服务进程崩溃 / 被 OOM 杀掉时，ffmpeg
#    跟着收到 SIGTERM 退出，不再成为孤儿继续写盘、占 CPU。
#
# PDEATHSIG 的坑：它跟的是「fork 出子进程的那个**线程**」而不是进程——线程退出就会
#    触发。anyio 的 to_thread 工作线程闲置 10 秒就退出，如果在那里面 Popen，ffmpeg 会在
#    播放中途被莫名其妙杀掉。所以所有 ffmpeg 一律交给一个**常驻**的单线程 spawner 来 fork，
#    这个线程与进程同寿。

_PR_SET_PDEATHSIG = 1
_libc = None
_spawner = None
_spawner_lock = threading.Lock()


def _load_libc():
    """在父进程里预先加载 libc（子进程 fork 之后再 dlopen 不安全）"""
    global _libc
    if _libc is None and sys.platform.startswith("linux"):
        try:
            import ctypes
            import ctypes.util

            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        except OSError:
            _libc = False
    return _libc or None


def _child_preexec(parent_pid: int, libc):
    """子进程 exec 前执行：设 PDEATHSIG；父进程若已不在则立即退出（竞态兜底）"""
    def _fn():
        if libc is not None:
            libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != parent_pid:
            os._exit(1)
    return _fn


def _get_spawner():
    global _spawner
    if _spawner is None:
        with _spawner_lock:
            if _spawner is None:
                from concurrent.futures import ThreadPoolExecutor

                _spawner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ffmpeg-spawner")
    return _spawner


def _popen_detached(cmd, **kwargs) -> subprocess.Popen:
    if os.name == "posix":
        kwargs.setdefault("start_new_session", True)
        libc = _load_libc()
        if libc is not None:
            kwargs.setdefault("preexec_fn", _child_preexec(os.getpid(), libc))
    return subprocess.Popen(cmd, **kwargs)


def spawn_ffmpeg(cmd, **kwargs) -> subprocess.Popen:
    """启动 ffmpeg：独立进程组 + 随父进程退出（Linux），fork 在常驻 spawner 线程里做"""
    return _get_spawner().submit(_popen_detached, cmd, **kwargs).result()


def terminate_process_group(proc: subprocess.Popen, grace: float = 5.0) -> Optional[int]:
    """整组结束子进程并收尸：SIGTERM → 等 grace 秒 → SIGKILL → 再等 2 秒

    收不回（D 状态）就交给后台线程 wait，调用方不陪着卡死。返回退出码（未知时 None）。
    """
    if proc.poll() is not None:
        return getattr(proc, 'returncode', None)

    def _signal(sig):
        pid = getattr(proc, "pid", None)
        if os.name == "posix" and isinstance(pid, int) and pid > 0:
            try:
                # 只有 ffmpeg 自成一组（start_new_session）时才整组发；否则会误伤本进程组
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                    return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            (proc.kill if sig == signal.SIGKILL else proc.terminate)()
        except Exception:  # noqa: BLE001 - 进程已不在
            pass

    _signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    _signal(signal.SIGKILL)  # SIGTERM 不理就 SIGKILL，否则进程会一直挂着
    try:
        return proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        def _reap_later():
            try:
                proc.wait()
            except Exception:  # noqa: BLE001 - 收尸线程不抛
                pass

        threading.Thread(target=_reap_later, name="ffmpeg-reaper", daemon=True).start()
        logger.warning("ffmpeg 进程 %s 在 SIGKILL 后仍未退出，已交给后台收尸",
                       getattr(proc, "pid", "?"))
        return None


def start_transcode(
    file_path: str,
    start_seconds: float = 0,
    video_bitrate: int = 4_000_000,
    height: Optional[int] = None,
    user_id: Optional[int] = None,
    item_guid: Optional[str] = None,
    input_headers: Optional[dict] = None,
    tier: Optional[str] = None,
    cache_key: Optional[str] = None,
    fingerprint: Optional[str] = None,
) -> str:
    """启动转码，返回播放列表 URL 路径片段 /emby/videos/{session}/master.m3u8

    同一用户重复请求同一部影片时复用进行中的转码会话，避免客户端重试/多端拉起时
    反复 fork ffmpeg（旧实现每次请求 master.m3u8 都会新建一个进程）。
    """
    # 失效会话回收与并发上限保护放到后台线程：它们要 terminate 子进程、等它退出、
    # 删目录（最坏几秒），而本函数是在请求路径（事件循环）上被调用的，不该让本次播放
    # 为别人遗留的会话等待（见 _reap_in_background）。
    # P2 修复（审查）：此前每次调用都起一个新线程，突发播放时数百个短命线程
    # 同时跑同一份回收。现节流：距上次触发 <10 秒直接跳过（常驻 reaper 30s 一轮兜底）。
    global _last_bg_reap
    _now = time.monotonic()
    with _last_bg_reap_lock:
        if _now - _last_bg_reap >= 10.0:
            _last_bg_reap = _now
            threading.Thread(target=_reap_in_background, daemon=True).start()
    if user_id is not None and item_guid:
        existing = find_active_transcode(user_id, item_guid, tier)
        if existing:
            logger.info("复用进行中的 HLS 转码 %s", existing)
            return existing
    session_id = uuid.uuid4().hex[:16]
    out_dir = os.path.join(TRANSCODE_DIR, session_id)
    proc = build_hls_command(file_path, out_dir, start_seconds, video_bitrate, height,
                             input_headers=input_headers)
    _TRANSCODE_PROCS[session_id] = {
        "proc": proc,
        "dir": out_dir,
        "started": datetime.now(),
        "user_id": user_id,
        "item_guid": item_guid,      # 「结束播放」按它反查（见 find_transcodes）
        "file_path": file_path,
        # 按需转码 P1：档位 / 缓存键 / 源指纹 / 起始秒数（回收时判断能否落盘缓存）
        "tier": tier,
        "cache_key": cache_key,
        "fingerprint": fingerprint,
        "start_seconds": start_seconds,
        # S6：客户端最后一次拉播放列表 / 切片的时间（monotonic），闲置判定看它
        "last_access": time.monotonic(),
    }
    _ensure_reaper()
    logger.info("HLS 转码启动 %s -> %s", os.path.basename(file_path), session_id)
    return session_id


def find_transcodes(user_id: int, item_guid: Optional[str] = None) -> list:
    """按「用户 + 条目」找转码会话 id（``item_guid`` 省略时 = 该用户全部）

    转码会话 id 是 ``start_transcode`` 生成的随机 uuid，只出现在 HLS 播放列表的
    ``?session=`` 上；而客户端的 ``PlaySessionId``（播放会话键，随机 ``s…``）
    **不是同一个东西**。所以「结束这场播放」必须从播放会话反查出该条目的 guid 再按
    (user_id, item_guid) 找，不能拿播放会话键去 pop——旧实现就是这么写的，等于什么都没停到。
    """
    return [sid for sid, info in list(_TRANSCODE_PROCS.items())
            if info.get("user_id") == user_id
            and (item_guid is None or info.get("item_guid") == item_guid)]


def find_active_transcode(user_id: int, item_guid: str,
                        tier: Optional[str] = None) -> Optional[str]:
    """查找同一用户同一影片仍在运行的转码会话（tier 指定时只复用同档）"""
    # D-P1-2：回收线程会 pop _TRANSCODE_PROCS，直接遍历字典会抛
    # RuntimeError: dictionary changed size during iteration，故用 list() 快照
    for sid, info in list(_TRANSCODE_PROCS.items()):
        if info.get("user_id") != user_id or info.get("item_guid") != item_guid:
            continue
        if tier is not None and info.get("tier") != tier:
            continue
        proc = info.get("proc")
        if proc is not None and proc.poll() is None:
            return sid
    return None


def _reap_in_background() -> None:
    """后台回收失效/超龄会话并执行并发上限保护（由 start_transcode 异步触发）

    ``start_transcode`` 是在请求路径（事件循环）上被调用的，而回收要 terminate 子进程、
    等它退出、删目录（最坏几秒）；放到后台线程后，本次播放不再为别人遗留的会话排队。
    """
    try:
        reap_stale_transcodes()
        reap_idle_transcodes()
        enforce_transcode_capacity()
    except Exception as exc:  # noqa: BLE001 — 后台回收失败不能影响本次播放
        logger.warning("后台回收转码会话失败: %s", exc)


def reap_stale_transcodes(max_age_seconds: int = 6 * 3600) -> int:
    """回收已退出 / 超龄的转码会话（含清理磁盘目录）

    客户端异常断开时不会上报 Stopped，若不回收会长期残留 ffmpeg 进程与临时分片。

    同步实现，调用方都是**线程**（``_reap_in_background`` 后台线程、维护周期 ``janitor_tick``），
    不在事件循环上：这里会逐个 ``terminate`` + ``wait`` + 删目录，本身就是慢调用。
    """
    now = datetime.now()
    reaped = 0
    for sid, info in list(_TRANSCODE_PROCS.items()):
        proc = info.get("proc")
        started = info.get("started") or now
        exited = proc is not None and proc.poll() is not None
        expired = (now - started).total_seconds() > max_age_seconds
        if exited or expired:
            if exited and not info.get("cached"):
                # 按需转码 P1：整片转完的会话落盘进持久缓存（延迟导入防循环导入）
                try:
                    from backend.emby_server import transcode as _tc
                    _tc.maybe_promote_to_cache(sid, info)
                except Exception as exc:  # noqa: BLE001 — 提升失败不影响回收
                    logger.warning("转码缓存提升失败 %s: %s", sid, exc)
            stop_transcode(sid)
            reaped += 1
    if reaped:
        logger.info("回收 %d 个失效 HLS 转码会话", reaped)
    return reaped


async def wait_for_file(path: str, timeout: float = 10.0, interval: float = 0.2) -> bool:
    """等待 ffmpeg 产出目标文件（客户端请求切片往往早于转码进度）

    异步等待：旧实现用同步 ``time.sleep`` 轮询，等首片（最多 15s）或等切片（最多 12s）
    的请求会把整个事件循环占住，同一进程里其他人正在播放的请求全部排队等待。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True
        await asyncio.sleep(interval)
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _terminate_and_cleanup(session_id: str, info: dict) -> None:
    """停掉转码的**阻塞部分**：结束子进程 + 提升日志尾部 + 删会话目录

    两处都是慢调用：``proc.wait(timeout=5)`` 要等 ffmpeg 响应 SIGTERM（它可能正在写
    最后几个分片），``shutil.rmtree`` 要递归删掉整场播放的分片（上千个文件，挂载目录上更慢）。
    只在线程 / 同步路由 / 后台回收线程里直接调；事件循环上一律走 ``stop_transcode_async``。
    """
    proc = info.get("proc")
    if proc is not None:
        terminate_process_group(proc)  # S7：整组结束 + 收尸（不留僵尸 / 孤儿）
        _log_ffmpeg_tail(info.get("dir") or "", session_id, proc.poll())
    # 按需转码 P1：缓存会话（cached=True）跳过删目录，缓存留给下次复用
    if not info.get("cached"):
        shutil.rmtree(info.get("dir"), ignore_errors=True)


def stop_transcode(session_id: str) -> None:
    """同步停止一次转码（后台回收线程 / 同步 ``def`` 路由用）

    **不要在 ``async def`` 里直接调它**：它会阻塞到子进程退出与目录删完（最坏 5 秒起），
    一次调用就把整个进程的播放卡住。事件循环上用 ``stop_transcode_async``（同文件下方），
    ``scripts/smoke_test_transcode_stop.py`` 会静态拦住这个错误写法。
    """
    info = _TRANSCODE_PROCS.pop(session_id, None)
    if not info:
        return
    _terminate_and_cleanup(session_id, info)


async def stop_transcode_async(session_id: str) -> None:
    """异步停止一次转码——阻塞部分丢线程池，事件循环不等待

    v2.12/v2.13 修的就是这一类问题（阻塞调用不许出现在事件循环上），但 ``stop_transcode``
    一直是漏网的那一个：管理员点「结束播放」、客户端上报 Stop、维护周期回收会话，
    都会在 ``async def`` 路由里同步等上 5 秒 + 删一轮分片——全站一起顿住。

    登记摘除（``pop``）仍在调用线程里同步完成，所以并发的两次停止只有一个真正拿到会话，
    不会重复 terminate / 重复删目录。
    """
    info = _TRANSCODE_PROCS.pop(session_id, None)
    if not info:
        return
    await asyncio.to_thread(_terminate_and_cleanup, session_id, info)


def _log_ffmpeg_tail(directory: str, session_id: str, returncode) -> None:
    """ffmpeg 异常退出时把日志尾部提升到服务日志（排查转码失败时唯一的线索）"""
    if not directory or returncode in (0, None, -15):  # 正常结束 / 被本服务 SIGTERM
        return
    try:
        with open(os.path.join(directory, "ffmpeg.log"), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 2048))
            tail = handle.read().decode("utf-8", "replace").strip()
    except OSError:
        return
    if tail:
        logger.warning(
            "HLS 转码 %s 异常退出（code=%s），ffmpeg 日志尾部：\n%s", session_id, returncode, tail,
        )


def stop_all_transcodes() -> int:
    count = len(_TRANSCODE_PROCS)
    for sid in list(_TRANSCODE_PROCS.keys()):
        stop_transcode(sid)
    return count


def stop_user_transcodes(user_id: int) -> int:
    """停止某用户的全部转码会话（客户端请求 ActiveEncodings/Delete 时调用）"""
    stopped = 0
    for sid, info in list(_TRANSCODE_PROCS.items()):
        if info.get("user_id") == user_id:
            stop_transcode(sid)
            stopped += 1
    return stopped


def stop_transcodes_for(user_id: int, item_guid: Optional[str] = None) -> int:
    """同步停掉某用户某个条目的转码会话（同步 ``def`` 路由 / 线程里用）"""
    ids = find_transcodes(user_id, item_guid)
    for sid in ids:
        stop_transcode(sid)
    return len(ids)


async def stop_transcodes_for_async(user_id: int, item_guid: Optional[str] = None) -> int:
    """异步停掉某用户某个条目的转码会话（事件循环上用这个，见 stop_transcode_async）"""
    ids = find_transcodes(user_id, item_guid)
    if ids:
        await asyncio.gather(*(stop_transcode_async(sid) for sid in ids))
    return len(ids)


async def stop_all_transcodes_async() -> int:
    """异步停掉全部转码会话（管理后台「停止全部转码」用）

    并发执行：``async def`` 路由里逐个 ``await`` 会让总耗时变成「会话数 × 等待时间」
    （N 路转码同时被停就是 5×N 秒），gather 之后总耗时≈单个会话。
    """
    ids = list(_TRANSCODE_PROCS.keys())
    if ids:
        await asyncio.gather(*(stop_transcode_async(sid) for sid in ids))
    return len(ids)


async def stop_user_transcodes_async(user_id: int) -> int:
    """异步停掉某用户的全部转码会话（同上，并发执行）"""
    ids = [sid for sid, info in list(_TRANSCODE_PROCS.items())
           if info.get("user_id") == user_id]
    if ids:
        await asyncio.gather(*(stop_transcode_async(sid) for sid in ids))
    return len(ids)


def get_transcode(session_id: str):
    return _TRANSCODE_PROCS.get(session_id)


def transcode_alive(session_id: str) -> bool:
    """转码进程是否仍在运行（会话登记在册且进程还活着）"""
    info = _TRANSCODE_PROCS.get(session_id)
    if not info:
        return False
    proc = info.get("proc")
    return proc is not None and proc.poll() is None


def active_transcode_ids() -> list:
    """当前登记在册的转码会话 id（维护/健康检查用；不要在遍历中改字典）"""
    return list(_TRANSCODE_PROCS.keys())


def transcode_capacity() -> int:
    """允许同时运行的转码路数（默认按 CPU 核数：veryfast 单路按一核估算）"""
    raw = os.getenv("EMBY_MAX_TRANSCODES", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return max(1, os.cpu_count() or 2)


def transcode_idle_seconds() -> float:
    """客户端多久没来拉播放列表/切片，就认为这场转码在并发紧张时可以让位（默认 120s）"""
    try:
        value = float(os.getenv("EMBY_TRANSCODE_IDLE", "") or 120)
    except ValueError:
        value = 120.0
    return value if value > 0 else 120.0


def transcode_abandon_seconds() -> float:
    """客户端多久没来拉任何东西，就**无条件**回收这场转码（默认 600s）

    比 ``EMBY_TRANSCODE_IDLE`` 长：暂停看手机几分钟的用户不该被掐；真正离开的
    （关 App / 切走 / 断网）10 分钟后 ffmpeg 停掉，不再把整部片转到片尾。
    不小于 ``EMBY_TRANSCODE_IDLE``。
    """
    try:
        value = float(os.getenv("EMBY_TRANSCODE_ABANDON", "") or 600)
    except ValueError:
        value = 600.0
    if value <= 0:
        value = 600.0
    return max(value, transcode_idle_seconds())


def touch_transcode(session_id: str) -> None:
    """记一次客户端访问（video_hls 每次服务该会话的播放列表 / 切片时调用）"""
    info = _TRANSCODE_PROCS.get(session_id)
    if info is not None:
        info["last_access"] = time.monotonic()


def _transcode_idle(info: dict) -> float:
    """距客户端最后一次拉播放列表 / 切片过了多少秒（S6）

    旧口径看输出目录 mtime：ffmpeg（``-hls_list_size 0``）会一直写到片尾，目录永远
    「刚更新」，用户走了也永远不算闲置。现在只看客户端的访问时间。
    没有访问记录的老会话按启动时间算。
    """
    last = info.get("last_access")
    if last is None:
        started = info.get("started")
        if isinstance(started, datetime):
            return max(0.0, (datetime.now() - started).total_seconds())
        return float("inf")
    return max(0.0, time.monotonic() - float(last))


def reap_idle_transcodes(idle_limit: Optional[float] = None) -> int:
    """回收客户端闲置超过 ``idle_limit`` 秒的**正在运行**的转码（默认用放弃阈值）

    同步实现（内部 terminate + wait + 删目录），只在线程里调用。
    缓存复用会话（无进程）同样按闲置回收登记（不删缓存目录）。
    """
    limit = transcode_abandon_seconds() if idle_limit is None else float(idle_limit)
    reaped = 0
    for sid, info in list(_TRANSCODE_PROCS.items()):
        idle = _transcode_idle(info)
        if idle < limit:
            continue
        proc = info.get("proc")
        if proc is not None and proc.poll() is not None:
            continue  # 已退出的交给 reap_stale_transcodes（可能要提升进缓存）
        logger.info("回收闲置 HLS 转码会话 %s（客户端 %.0fs 未访问）", sid, idle)
        stop_transcode(sid)
        reaped += 1
    return reaped


_REAPER_INTERVAL = 30.0
_reaper_started = False
_reaper_lock = threading.Lock()
# P2 修复：start_transcode 触发的后台回收节流时间戳（进程内）
_last_bg_reap = 0.0
_last_bg_reap_lock = threading.Lock()


def transcode_reaper_tick() -> dict:
    """一轮转码回收：收尸已退出的（含提升缓存）、回收放弃的、按并发上限让位闲置的"""
    result = {"stale": 0, "abandoned": 0}
    result["stale"] = reap_stale_transcodes()
    result["abandoned"] = reap_idle_transcodes()
    enforce_transcode_capacity()
    return result


def _ensure_reaper() -> None:
    """进程内常驻的转码回收线程（首次起转码时懒启动；EM / EA 谁跑转码谁有）

    维护 janitor 每 10 分钟才一轮且不在每个进程里都启动；而用户离开后 ffmpeg 会一直
    转到片尾，所以这里单独 30 秒一轮。
    """
    global _reaper_started
    if _reaper_started:
        return
    with _reaper_lock:
        if _reaper_started:
            return
        _reaper_started = True

    def _loop():
        while True:
            time.sleep(_REAPER_INTERVAL)
            if not _TRANSCODE_PROCS:
                continue
            try:
                transcode_reaper_tick()
            except Exception as exc:  # noqa: BLE001 — 回收线程绝不能退出
                logger.warning("转码回收周期异常: %s", exc)

    threading.Thread(target=_loop, name="transcode-reaper", daemon=True).start()


def enforce_transcode_capacity() -> None:
    """并发上限保护：CPU 被多路转码打满时，先停掉已经没人看的会话

    同步实现（内部会等子进程退出 + 删目录）；调用方是后台回收线程，不在事件循环上。

    客户端切清晰度 / 拖进度后重新拉起、或者直接杀掉应用，都不会上报 Stopped，
    只留下一个不再增长的输出目录。这里**只回收闲置超时的会话**，不为了让新请求
    进场而掉头挚掉正在播放的会话：宁可短暂超限并告警，也不能让用户正看着的片子中断
    （上限可用 ``EMBY_MAX_TRANSCODES``、闲置判定用 ``EMBY_TRANSCODE_IDLE`` 调整）。
    """
    cap = transcode_capacity()
    running = {
        sid: info for sid, info in list(_TRANSCODE_PROCS.items())
        if info.get("proc") is not None and info["proc"].poll() is None
    }
    if len(running) < cap:
        return
    idle_limit = transcode_idle_seconds()
    for sid, info in sorted(running.items(), key=lambda kv: _transcode_idle(kv[1]), reverse=True):
        if len(running) <= cap - 1:
            break
        idle = _transcode_idle(info)
        if idle < idle_limit:
            break  # 已按闲置时间降序，后面只会更活跃
        logger.info("回收空闲 HLS 转码会话 %s（闲置 %.0fs，并发上限 %d）", sid, idle, cap)
        stop_transcode(sid)
        running.pop(sid, None)
    if len(running) >= cap:
        logger.warning(
            "HLS 转码并发已达上限 %d，本次仍继续启动（不中断正在播放的用户）；"
            "可用 EMBY_MAX_TRANSCODES 调整上限", cap,
        )


_TRANSCODE_PROCS: dict[str, dict] = {}


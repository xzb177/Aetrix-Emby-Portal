"""自建 Emby 服务器：流媒体服务（直连流 / Range / 中继直出）"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from email.utils import formatdate
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

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


# 远程挂载（115 / WebDAV / AList）代理转发时的 UA。
# 同一环境变量只许一处默认值（横切纪律）：默认值在 mounts.MOUNT_UA 定义
# （115 优化的 PAN115_UA；115 直链对 UA 有偏好），这里直接复用，不另起一套。
REMOTE_UA = MOUNT_UA
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


# A-P2-2 DNS pinning 共享缓存（async 路径）：host -> [ip, ...]，TTL 300 秒。
# 只有经 ``_next_redirect`` 校验通过的重定向目标才会写入；每次建连时查。
# 总开关 EMBY_DNS_PINNING 在 transport 层实时检查，关掉即回退到正常 DNS。
from backend.emby_server.dns_pinning import PinningCache as _PinningCache
from backend.emby_server.dns_pinning import dns_pinning_enabled

_PINNING_CACHE = _PinningCache(ttl=300.0)


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


def _mount_relay_stall_timeout() -> float:
    """客户端停读多久后放掉上游连接（秒）；``MOUNT_RELAY_STALL_TIMEOUT_SEC``。

    读超时只管「上游不给数据」，管不到「客户端不收数据」：播放器拖动/重开
    时弃用旧连接却不 close（Infuse、AVPlayer 系实测每会话残留 3-4 条），
    中继停在 yield 上等 send 排空，上游连接与共享池名额被占到客户端退出。
    缺省 60 秒（正常播放每块 1MB 的消费间隔远小于此；暂停超过阈值后恢复，
    播放器按 Range 重连续播）；0 关闭，非法值回落缺省并告警。
    """
    raw = os.getenv("MOUNT_RELAY_STALL_TIMEOUT_SEC", "")
    if not str(raw).strip():
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("环境变量 MOUNT_RELAY_STALL_TIMEOUT_SEC=%r 不是数字，使用默认值 60", raw)
        return 60.0
    return max(0.0, value)


async def _release_upstream_when_stalled(source, resp, url: str, timeout: float):
    """包一层流：消费方（客户端）停读超过 ``timeout`` 秒就关上游响应。

    只在停在 yield 上（等客户端收）时计时；上游慢由读超时负责。关上游后
    本流结束——客户端若之后恢复读取，会收到截断的响应并按 Range 重连。
    """
    if timeout <= 0:
        async for chunk in source:
            yield chunk
        return
    loop = asyncio.get_running_loop()
    waiting_since: Optional[float] = None
    stalled = False

    async def _watch():
        nonlocal stalled
        while True:
            await asyncio.sleep(min(timeout / 4, 5.0))
            if waiting_since is not None and loop.time() - waiting_since >= timeout:
                stalled = True
                logger.info("远程挂载客户端停读 %.0fs，释放上游连接 %s",
                            timeout, url.split("?")[0])
                try:
                    await resp.aclose()
                except Exception:
                    pass
                return

    watcher = asyncio.create_task(_watch())
    try:
        async for chunk in source:
            waiting_since = loop.time()
            yield chunk
            waiting_since = None
            if stalled:
                return
    finally:
        watcher.cancel()
        await source.aclose()


#: 中继客户端按 event loop 共享（2026-10-30 起播/拖动提速）：旧口径
#: 「按次创建、用完关闭」让每次拉流/拖动都重付一遍连接建立（VPS 到
#: 谷歌实测单次 TCP+TLS 约 0.11–0.16s；生产 stream 首字节 1.6–2.2s
#: 里中继段约 0.87s 有相当部分耗在新建连接上）。共享只动连接层：
#: DNS pinning 仍在 transport 层（连接建立时校验）、跳转仍由
#: _follow_redirects 逐跳校验、凭据头仍按请求传——出法与顺序零
#: 变化。注意 httpx 0.28 关 client 会连 transport 一起关，共享
#: client 绝不许在请求路径上 aclose，只关响应把连接归还池中。
_MOUNT_RELAY_CLIENTS: dict = {}


def _mount_relay_client() -> "httpx.AsyncClient":
    """远程挂载中继的共享客户端（按 event loop 一份）。

    池上限较旧按次口径放大（8→32）：旧口径每请求独享一份池，
    共享后全进程中继共用一份，太小会让并发拉流互相等池
    （PoolTimeout 的既有 503 口径不变）。
    """
    import asyncio

    import httpx

    from backend.emby_server.dns_pinning import PinnedAsyncHTTPTransport

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 无运行中 loop：退回按次创建，调用方自负关闭
        loop = None
    if loop is not None:
        for _dead in [l for l in _MOUNT_RELAY_CLIENTS if l.is_closed()]:
            _MOUNT_RELAY_CLIENTS.pop(_dead, None)
        client = _MOUNT_RELAY_CLIENTS.get(loop)
        if client is not None and not client.is_closed:
            return client
    client = httpx.AsyncClient(
        transport=PinnedAsyncHTTPTransport(
            _PINNING_CACHE.lookup,
            limits=httpx.Limits(max_connections=32,
                                max_keepalive_connections=16),
        ),
        timeout=httpx.Timeout(
            connect=_MOUNT_RELAY_TIMEOUT_CONNECT,
            read=_MOUNT_RELAY_TIMEOUT_READ,
            write=30.0,
            pool=10.0,
        ),
        follow_redirects=False,
    )
    if loop is not None:
        _MOUNT_RELAY_CLIENTS[loop] = client
    return client


async def serve_mount_relay_async(
    url: str,
    request: Request,
    headers: Optional[dict] = None,
    media_type: str = "video/mp4",
    pt=None,
    sniff_quota_page: bool = False,
):
    """远程挂载专用中继：服务端带挂载凭据取流、流式转发给播放器。

    - ``headers``：挂载解析出的鉴权头（UA/Referer/Cookie），只在本机使用、
      不下发给客户端；``X-Pickcode`` 是 115 缓存失效用的内部头，不转发。
    - Range 透传：客户端的 ``Range`` 原样发给上游，上游的 200/206 与
      ``Content-Range``/``Accept-Ranges`` 等头原样透回。
    - ``pt``（可选）：播放链路计时器，只加日志不改逻辑。
    - ``sniff_quota_page``（转存变体应急自动触发用）：只 Drive 中继传
      True；115 挂载中继不受影响。开启时，上游回 200 text/html 即预读
      首块（无论是否带 Range；A-089：带 Range 撞上 200 额度页时旧口径
      把 HTML 原样当视频流下发、播放器解码失败且服务端无归因）——命中
      Quota 标记则关响应关客户端、抛 transfer_variant.QuotaPageError
      （调用方记信号 + 入队，本次播放回落现行链）；未命中则首块原样
      补回流中，字节一分不少。带 Range 的 200 非 HTML（上游忽略 Range
      回全文件）同样经 prelude 原样续流；206 本就不满足 status==200，
      不受嗅探影响。
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
                # 批一 #1 连带：解析结果缓存里还留着同一条死链，只清换链
                # 缓存不够——不连带清，死链会被解析缓存续命最多一个 TTL。
                try:
                    from backend.emby_server.mounts import (
                        invalidate_resolve_cache_by_pickcode)
                    invalidate_resolve_cache_by_pickcode(_pickcode)
                except Exception as exc:
                    logger.warning("115 解析缓存连带失效失败 pickcode=%s: %s",
                                   _pickcode, exc)
        if resp.status_code >= 400:
            status = resp.status_code
            await resp.aclose()
            resp = None
            logger.warning("远程挂载源站返回 %s: %s", status, url.split("?")[0])
            raise HTTPException(
                status_code=502 if status >= 500 else status,
                detail=f"源站返回 {status}")

        # 转存变体 Quota 嗅探（仅 Drive 中继传 True；115 挂载中继不受影响）。
        # 上游回 200 时预读首块（带 Range 同样嗅探，A-089）：命中 Quota 标记
        # → 关响应关客户端、抛 QuotaPageError（video_stream 接住记信号 +
        # 入队，本次播放回落现行链）；未命中 → 首块记为 prelude，
        # iter_mount 先吐首块再续流，字节一分不少、透传头不动。
        # 嗅探本身绝不能拖垮播放：预读失败时当作未嗅探，照常流式转发。
        # 注意：QuotaPageError 在本块内关完响应才抛——外层 except 只认
        # HTTPException，QuotaPageError 会原样穿到调用方，不会漏关连接。
        _prelude = None
        if sniff_quota_page and resp.status_code == 200:
            from backend.emby_server import transfer_variant as _tv
            try:
                _aiter = resp.aiter_bytes(READ_CHUNK)
                try:
                    _first = await _aiter.__anext__()
                except StopAsyncIteration:
                    _first = b""
                if _tv.is_quota_page_bytes(
                        resp.status_code, resp.headers.get("content-type"),
                        _first):
                    await resp.aclose()
                    resp = None
                    raise _tv.QuotaPageError("上游响应为 Quota 额度页")
                _prelude = (_aiter, _first)
            except _tv.QuotaPageError:
                raise
            except Exception as exc:
                logger.warning("Quota 嗅探预读失败，照常转发：%s",
                               type(exc).__name__)
                _prelude = None

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
                if _prelude is not None:
                    # 嗅探已预读首块：先吐首块，再续跑同一迭代器——
                    # 非 Quota 的 HTML 原样续流，字节一个不少。
                    _aiter, _first = _prelude
                    if _first:
                        yield _first
                    async for chunk in _aiter:
                        yield chunk
                else:
                    async for chunk in resp.aiter_bytes(READ_CHUNK):
                        yield chunk
            except httpx.TransportError as exc:
                # 源站中途断开：结束这条流、归还连接；响应头已发出，
                # 播放器会对剩余区间重新发 Range。
                logger.warning("远程挂载流中断 %s: %s",
                               url.split("?")[0], type(exc).__name__)
            finally:
                # 只关响应：连接归还共享客户端的池（client 按 loop
                # 共享，关掉会连 transport 一起关、后续请求全废）。
                try:
                    await resp.aclose()
                except Exception:
                    pass

        if pt is not None:
            pt.done()
        return StreamingResponse(
            _release_upstream_when_stalled(
                iter_mount(), resp, url, _mount_relay_stall_timeout()),
            status_code=resp.status_code,
            media_type=content_type, headers=passthrough,
        )
    except HTTPException:
        if resp is not None:
            try:
                await resp.aclose()
            except Exception:
                pass
        raise


#: Drive 中继 SA failover 的触发状态码：令牌死（401）/ 权限与配额类
#: （403）/ 限流（429）——都是「换一个 SA 就可能好」的失败。404 是文件
#: 不存在、换号也没用，原样透传（#1192 定版）；其余状态码维持挂载
#: 中继既有口径（不触发换号）。
_RELAY_SA_FAILOVER_STATUSES = frozenset({401, 403, 429})

#: 中继 SA failover 总尝试次数缺省（调用方未传 max_attempts 时用；
#: 与 playback_strategy 的 SystemConfig 缺省同为 3，见该模块键注释）。
_RELAY_SA_FAILOVER_DEFAULT_ATTEMPTS = 3

#: failover 循环内排除已试 SA 的重抽上限（A-009）：轮换随机步长可能抽回
#: 同一个 SA；抽中已试 SA 时再抽一次，最多该次数，耗尽则接受最后一次
#: 抽到的令牌（退回旧语义，避免小池场景下直接 503）。
_RELAY_SA_EXCLUDE_REDRAWS = 3


def _note_relay_sa_cooldown(url: str, token: str, status: int) -> None:
    """把本次取流失败的 SA 凭据按值失效（冷却记账，只记账、绝不抛错）。

    复用核查（横切只许一套，不另起第二套冷却）：
    - drive_changes._api_get 的指数退避是请求级、无 per-SA 状态；
      _mark_dead_drive 是 per-盘 404 退避——对象都不是 SA，不适用。
    - 本仓唯一的「凭据失败→失效」实现是 drive_auth 的按值双层失效
      （401 毒化修复）：401 走其指定入口 note_drive_unauthorized，
      403/429 走 invalidate_drive_bearer_token，本函数只分发不新造。
    失效后，该 SA 在双层缓存里的坏令牌被清掉，下次轮到它时重新换取；
    本次重试则靠 get_token_for_drive 的轮换直接换下一个 SA。
    记账本身失败只记日志——冷却记账不能拖垮 failover。
    """
    try:
        from backend.emby_server import drive_auth

        if status == 401:
            drive_auth.note_drive_unauthorized(url, f"Bearer {token}")
        else:
            drive_auth.invalidate_drive_bearer_token(token)
    except Exception as exc:  # noqa: BLE001 - 记账失败只记日志
        logger.warning("Drive 中继 SA 冷却记账失败 status=%s: %s",
                       status, exc)


async def serve_drive_relay_async(
    request: Request,
    drive_id: str,
    file_id: str,
    media_type: str = "video/mp4",
    pt=None,
    access_token: Optional[str] = None,
    rotate_on_quota: bool = True,
    max_attempts: Optional[int] = None,
):
    """Drive 条目 SA 中继：服务端用盘凭据从 Drive API 取流、流式转发。

    播放器只跟本站说话：file_id 永不出服务器，也不经「任何人可读」的
    公开下载池（公开池是全网共用额度，爆池时整库播不了，2026-10-23
    实发）；alt=media + 盘凭据走 SA/OAuth 各自的额度，实测公开池已死的
    文件用 SA 凭据仍 206 出字节。

    - 取流 / Range 透传 / 流式转发复用 serve_mount_relay_async
      （横切只许一套）；这里只管 Drive 特有的凭据与地址：API 地址由
      drive_auth 的现成构造函数拼，凭据走 get_token_for_drive
      （全项目唯一入口：共享盘 SA 轮换 / 个人盘 OAuth 不回退）。
    - SA failover（2026-10-29 用户亲令「中转加上换号能力」）：上游
      取流回 401/403/429（令牌死 / 当前 SA 下载额度被打满 / 限流）→
      先按值失效该 SA 凭据（drive_auth 既有机制，见
      _note_relay_sa_cooldown），再轮换凭据重试；总尝试次数由
      ``max_attempts`` 界定（调用方传 SystemConfig 键
      drive_relay_sa_failover_max_attempts 的值，未传缺省 3，含首次），
      次数耗尽回最后一次的原始错误。每次换号打一条
      drive_relay_sa_failover 日志（sa= 字段与 drive_token_mint 同
      形态）。404 与其余状态码不换号、按挂载中继口径抛错（404
      透传、5xx 在下层已转 502）。
    - 请求撞上 Quota 额度页（上游回 200 HTML 额度页，无论是否带
      Range，A-089）→ 默认与 403 同口径：轮换凭据重试（与状态码
      failover 共用尝试预算）；预算耗尽才原样抛 QuotaPageError。
      额度页分支同样按值失效该 SA 凭据再重试（A-009）；已试 SA 在
      失效前记录（A-088：失效清缓存，事后反查记不上）；轮换抽中已试
      SA 时有界重抽（上限 3 次），
      耗尽则接受最后一次抽到的令牌。``rotate_on_quota=False``
      （绑定用户正在啃共享原件）时第一次额度页就抛，不换下一个 SA。
      调用方记信号；绑定用户不改播共享原件。BYOS 用户令牌腿不
      嗅探、不重试，原样不动。
    - failover 覆盖边界（按取流实情）：serve_mount_relay_async 在
      上游响应头阶段就把 ≥400 抛成 HTTPException、整段请求的 Quota
      页也在首块嗅探时抛出——都发生在首字节送达播放器之前（客户端
      Range 原样透传、同一 Range 段换号重取）。流已开始后的中途
      断流不在覆盖内（下层结束该流，播放器对剩余区间重新发 Range，
      那是新请求、新一轮 failover）。
    - 凭据获取失败（个人盘 OAuth 抛错、无 SA 可用）→ 503。
    - access_token 给了（BYOS 用户令牌中继）：直接用它、只试一次，
      上游 403 原样抛、绝不换凭据重试——换凭据就是跨用户复用，
      BYOS 铁律不许。drive_id 此时只作日志标识，不用于取凭据。
    """
    from backend.emby_server.drive_auth import (
        drive_api_media_url,
        drive_usercontent_download_url,
    )
    from backend.emby_server.drive_changes import get_token_for_drive
    from backend.emby_server.drive_changes import (
        sa_email_for_token as _dc_sa_email_for_token)
    from backend.emby_server import transfer_variant as _tv

    url = drive_api_media_url(drive_usercontent_download_url(file_id))
    if access_token is not None:
        # BYOS 用户令牌腿：单次直试，任何 HTTPException 原样抛。
        return await serve_mount_relay_async(
            url, request,
            headers={"Authorization": f"Bearer {access_token}"},
            media_type=media_type, pt=pt)
    total = (_RELAY_SA_FAILOVER_DEFAULT_ATTEMPTS if max_attempts is None
             else max(1, int(max_attempts)))
    tried_sa_emails: set[str] = set()

    def _remember_tried(tok):
        email = _dc_sa_email_for_token(tok)
        if email:
            tried_sa_emails.add(email)
        return email
    for attempt in range(total):
        last = attempt == total - 1
        try:
            token = await asyncio.to_thread(
                get_token_for_drive, drive_id, attempt > 0)
            # A-009：轮换随机步长可能抽回已试 SA——抽中已试则再抽
            # （有界），避免 3 次预算打在同一个 SA 上；邮箱反查不到
            # （个人盘 OAuth 腿等）时不排除、保持旧语义。
            for _ in range(_RELAY_SA_EXCLUDE_REDRAWS):
                email = _dc_sa_email_for_token(token)
                if not email or email not in tried_sa_emails:
                    break
                nxt = await asyncio.to_thread(
                    get_token_for_drive, drive_id, True)
                if not nxt:
                    break
                token = nxt
        except Exception as exc:  # noqa: BLE001 - 个人盘 OAuth 失败等
            logger.warning("Drive 中继取凭据失败 drive=%s: %s", drive_id, exc)
            raise HTTPException(
                status_code=503, detail="Drive 凭据不可用") from exc
        if not token:
            raise HTTPException(status_code=503, detail="Drive 凭据不可用")
        try:
            return await serve_mount_relay_async(
                url, request,
                headers={"Authorization": f"Bearer {token}"},
                media_type=media_type, pt=pt, sniff_quota_page=True)
        except _tv.QuotaPageError:
            if last or not rotate_on_quota:
                raise
            # A-009：额度页分支此前只记日志、不失效——同 SA 重抽会命中
            # 令牌缓存、必然复现同一张额度页；按值失效再重试（额度页
            # 本身没有状态码，429 走 invalidate 路径）。
            # A-088：先记录已试 SA 再冷却——冷却会同时清掉内外两层
            # 缓存条目，事后反查恒得 None、已试集记不上；日志 sa=
            # 直接用这次记下的邮箱，不再事后反查。
            sa_email = _remember_tried(token)
            _note_relay_sa_cooldown(url, token, 429)
            logger.warning(
                "drive_relay_sa_failover sa=%s failover_attempt=%d/%d "
                "status=quota_page file_id=%s",
                sa_email or "-", attempt + 1, total, file_id)
        except HTTPException as exc:
            if exc.status_code not in _RELAY_SA_FAILOVER_STATUSES or last:
                raise
            # A-088：先记录已试 SA 再冷却（同额度页分支；401 经
            # note_drive_unauthorized 同样走 invalidate，顺序要求相同）。
            sa_email = _remember_tried(token)
            _note_relay_sa_cooldown(url, token, exc.status_code)
            logger.warning(
                "drive_relay_sa_failover sa=%s failover_attempt=%d/%d "
                "status=%d file_id=%s",
                sa_email or "-", attempt + 1, total,
                exc.status_code, file_id)
    raise HTTPException(status_code=502, detail="Drive 中继失败")  # pragma: no cover


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

"""自建 Emby 服务器：流媒体服务（直连流 / Range / HLS 转码）"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from email.utils import formatdate
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from backend.emby_server import hwaccel as _hwaccel

# 开箱即用播放优化：单文件并发 Range 限流（只此一处实现）
from backend.emby_server.playback_tune import RANGE_LIMITER
from urllib.parse import parse_qsl, urljoin, urlsplit

from backend.emby_server.mounts import MountError

if TYPE_CHECKING:
    # httpx 运行时是延迟导入（各函数内按需 import，保持模块导入轻量）；
    # 但模块注解引用了 httpx.AsyncClient / httpx.Timeout，这里给类型检查器一个声明，
    # 消除未定义名称（pyflakes F821）。运行时不执行，不影响延迟导入。
    import httpx

logger = logging.getLogger(__name__)

# .strm 删除链路（块3）：播放时 Drive alt=media 返回 404 → 后台标记 source_invalid_at。
# fire-and-forget：模块级小线程池（2 封顶），热路径零阻塞；UPDATE 幂等
# （WHERE source_invalid_at IS NULL），标记风暴打不爆 DB 连接。
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

_gone_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gone-marker")


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


def note_drive_file_gone(file_id: str) -> None:
    """后台标记源已失效（fire-and-forget 入口）：最多 2 个线程排队，失败只记日志。"""
    try:
        _gone_pool.submit(_mark_source_gone_sync, file_id)
    except Exception as exc:
        logger.warning("streaming: 提交 gone 标记失败: %s", exc)


# 这些 403 reason 属于文件级永久限制，换 Service Account 重试也无用，命中时直接失败、不重试。
# downloadQuotaExceeded 是单文件下载配额：全网共用一份的文件级上限，换哪个 SA 都一样超，
# 与 rateLimitExceeded（SA 级限流、换 SA 有效，见 #915 实测）是两回事，必须分开对待。
_DRIVE_403_NON_RETRYABLE: frozenset[str] = frozenset({
    "cannotDownloadAbusiveFile",
    "fileNotDownloadable",
    "downloadQuotaExceeded",
})


# ---- Drive 单文件配额负缓存（跨请求护盾）----
#
# 文件配额超限后，播放器遇到 403 会自己反复重试（拖动、重连、换集回来再点），
# 每次重试都是一轮新的上游请求——等于拿我们的请求量去撞一面注定撞不开的墙，
# 还会把同一 SA 的其它请求拖下水。这里记一个进程内负缓存：某 file_id 一旦
# 确认 downloadQuotaExceeded，TTL 内同一文件的播放/下载请求本地直接回
# 403 + Retry-After，不再碰 Drive；TTL 到期放行一次真实请求探活（配额按日
# 重置，文件也可能被替换而提前恢复）。仅进程内、尽力而为，不追求跨进程一致。
_QUOTA_NEGATIVE_CACHE: dict[str, float] = {}
_QUOTA_NEGATIVE_CACHE_LOCK = threading.Lock()
_QUOTA_NEGATIVE_CACHE_MAX = 10000


def _quota_negative_ttl() -> float:
    return _env_number("DRIVE_QUOTA_NEGATIVE_CACHE_SEC", 900, 0, 86400)


def note_drive_quota_exceeded(file_id: str) -> None:
    """记录某文件已确认单文件配额超限（TTL 内同文件请求不再打 Drive）。"""
    if not file_id:
        return
    ttl = _quota_negative_ttl()
    if ttl <= 0:
        return
    now = time.monotonic()
    with _QUOTA_NEGATIVE_CACHE_LOCK:
        if len(_QUOTA_NEGATIVE_CACHE) >= _QUOTA_NEGATIVE_CACHE_MAX:
            for k in [k for k, v in _QUOTA_NEGATIVE_CACHE.items() if v <= now]:
                _QUOTA_NEGATIVE_CACHE.pop(k, None)
        _QUOTA_NEGATIVE_CACHE[file_id] = now + ttl


def drive_quota_blocked_retry_after(file_id: str) -> int:
    """file_id 在配额负缓存期内时返回剩余秒数（用作 Retry-After），否则返回 0。"""
    if not file_id:
        return 0
    now = time.monotonic()
    with _QUOTA_NEGATIVE_CACHE_LOCK:
        until = _QUOTA_NEGATIVE_CACHE.get(file_id)
        if until is None:
            return 0
        if until <= now:
            _QUOTA_NEGATIVE_CACHE.pop(file_id, None)
            return 0
        return max(1, int(until - now))


# ---- Drive 单文件配额救援（零空间：换入口 + 短冷却重试，2026-10-16）----
#
# 实测（2026-10-16 晚）：文件撞 downloadQuotaExceeded 后并非死一整天，
# 《消防员》21:03 实锤 403、21:15 同入口复测已 206 恢复——死亡窗口是
# 分钟级的短封。此前口径是撞一次就把 403 甩给播放器、播放当场断
# （拖进度条最常见）。救援在「首次确认配额 403、记入负缓存之前」插入
# 两步（每请求至多各一次，新增上游请求 ≤2，不放大请求量）：
#   1) 换入口：改走 drive.usercontent.google.com 再取一次——额度是否按
#      入口分开算未经证实，靠下面计数实测，通了就是当场复活；
#   2) 短冷却：等 DRIVE_QUOTA_RESCUE_WAIT_SEC 秒后用原入口重试一次，
#      等的是分钟级恢复窗口；仍死才记入负缓存、按旧口径回 403。
# 换 SA 对文件级配额无效（见 _DRIVE_403_NON_RETRYABLE 注释），救援不换 SA。
# 旋钮沿用本模块环境变量风格：DRIVE_QUOTA_RESCUE_ENTRY（默认开）、
# DRIVE_QUOTA_RESCUE_WAIT_SEC（默认 90，0=关等待重试）。
_QUOTA_RESCUE_COUNTERS: dict[str, int] = {
    "entry_attempt": 0, "entry_success": 0,
    "wait_attempt": 0, "wait_success": 0,
}


def quota_rescue_counters() -> dict[str, int]:
    """配额救援计数快照（进程内、尽力而为；备用入口是否独立靠它判读）。"""
    return dict(_QUOTA_RESCUE_COUNTERS)


def _quota_rescue_count(key: str) -> None:
    try:
        _QUOTA_RESCUE_COUNTERS[key] = _QUOTA_RESCUE_COUNTERS.get(key, 0) + 1
    except Exception:
        pass


def _drive_resp_is_media(resp, forward) -> bool:
    """判断上游响应是不是真媒体数据（备用入口救援的成功标准），永不抛异常。

    usercontent 对大文件可能先回「无法病毒扫描」确认页（200 + text/html，
    2026-10-16 实测 2040 字节），确认页一律不算成功；带 Range 的请求
    必须是 206 + Content-Range，防整文件/HTML 被当分片交付给播放器。
    """
    try:
        if resp is None or resp.status_code not in (200, 206):
            return False
        ctype = ""
        try:
            ctype = str(resp.headers.get("content-type") or "").lower()
        except Exception:
            ctype = ""
        if "text/html" in ctype:
            return False
        range_hdr = ""
        for k, v in (forward or {}).items():
            if str(k).lower() == "range":
                range_hdr = str(v or "")
                break
        if range_hdr:
            if resp.status_code != 206:
                return False
            try:
                if not resp.headers.get("content-range"):
                    return False
            except Exception:
                return False
        return True
    except Exception:
        return False


async def _quota_rescue_async(client, api_url: str, forward: dict, fid: str):
    """配额救援（异步）：换入口 → 短冷却重试原入口，各至多一次。

    成功返回 (resp, current)（真媒体响应，调用方照常交付）；
    全部失败返回 None（调用方记入负缓存、按旧口径回 403）。永不抛异常。
    """
    try:
        from backend.emby_server import drive_auth

        # 1) 换入口（立即，不等待）
        if _env_flag("DRIVE_QUOTA_RESCUE_ENTRY", True) and fid:
            uc_url = drive_auth.drive_usercontent_download_url(fid)
            if uc_url:
                _quota_rescue_count("entry_attempt")
                try:
                    resp, current, _hops = await _follow_redirects(
                        client, uc_url, forward, silent=False)
                except Exception:
                    resp, current = None, uc_url
                if _drive_resp_is_media(resp, forward):
                    _quota_rescue_count("entry_success")
                    logger.warning(
                        "配额救援：换入口成功 file_id=%s（备用入口可独立取流）", fid)
                    return resp, current
                if resp is not None:
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                logger.warning("配额救援：换入口未成功 file_id=%s，转短冷却重试", fid)

        # 2) 短冷却后重试原入口（同一 SA，配额是文件级、换 SA 无意义）
        wait_sec = _env_number("DRIVE_QUOTA_RESCUE_WAIT_SEC", 90, 0, 600)
        if wait_sec > 0:
            _quota_rescue_count("wait_attempt")
            await asyncio.sleep(wait_sec)
            try:
                resp, current, _hops = await _follow_redirects(
                    client, api_url, forward, silent=False)
            except Exception:
                resp, current = None, api_url
            if _drive_resp_is_media(resp, forward):
                _quota_rescue_count("wait_success")
                logger.warning(
                    "配额救援：短冷却 %.0fs 后原入口恢复 file_id=%s", wait_sec, fid)
                return resp, current
            if resp is not None:
                try:
                    await resp.aclose()
                except Exception:
                    pass
            logger.warning(
                "配额救援：短冷却 %.0fs 后原入口仍未恢复 file_id=%s", wait_sec, fid)
    except Exception:
        logger.debug("配额救援（异步）异常，按未救援处理", exc_info=True)
    return None


def _quota_rescue_sync(client, api_url: str, forward: dict, fid: str,
                       dns_pinning: dict):
    """配额救援（同步版，与 _quota_rescue_async 同口径）。

    成功返回 (resp, current, forward)，失败返回 None。永不抛异常。
    """
    try:
        from backend.emby_server import drive_auth

        if _env_flag("DRIVE_QUOTA_RESCUE_ENTRY", True) and fid:
            uc_url = drive_auth.drive_usercontent_download_url(fid)
            if uc_url:
                _quota_rescue_count("entry_attempt")
                try:
                    resp, current, out_forward = _open_upstream_sync(
                        client, uc_url, forward, dns_pinning, silent=False)
                except Exception:
                    resp, current, out_forward = None, uc_url, forward
                if _drive_resp_is_media(resp, forward):
                    _quota_rescue_count("entry_success")
                    logger.warning(
                        "配额救援：换入口成功 file_id=%s（备用入口可独立取流）", fid)
                    return resp, current, out_forward
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                logger.warning("配额救援：换入口未成功 file_id=%s，转短冷却重试", fid)

        wait_sec = _env_number("DRIVE_QUOTA_RESCUE_WAIT_SEC", 90, 0, 600)
        if wait_sec > 0:
            _quota_rescue_count("wait_attempt")
            time.sleep(wait_sec)
            try:
                resp, current, out_forward = _open_upstream_sync(
                    client, api_url, forward, dns_pinning, silent=False)
            except Exception:
                resp, current, out_forward = None, api_url, forward
            if _drive_resp_is_media(resp, forward):
                _quota_rescue_count("wait_success")
                logger.warning(
                    "配额救援：短冷却 %.0fs 后原入口恢复 file_id=%s", wait_sec, fid)
                return resp, current, out_forward
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            logger.warning(
                "配额救援：短冷却 %.0fs 后原入口仍未恢复 file_id=%s", wait_sec, fid)
    except Exception:
        logger.debug("配额救援（同步）异常，按未救援处理", exc_info=True)
    return None


def _drive_403_error_reason(body: bytes) -> tuple[str, str]:
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


def _mark_source_gone_sync(file_id: str) -> None:
    """线程池内执行：幂等 UPDATE emby_items.source_invalid_at。

    drive_file_id 已有索引，单条毫秒级；WHERE source_invalid_at IS NULL 保证幂等
    （100 个人点同一个死文件，只有第一条真正写行）。
    用 ORM 而不用 now()：SQLite/PG 通用（now() 是 PG 方言）。
    """
    try:
        from backend.emby_server import models as em
        from backend.database import SessionLocal
    except Exception:
        return
    sess = SessionLocal()
    try:
        sess.query(em.MediaItem).filter(
            em.MediaItem.drive_file_id == file_id,
            em.MediaItem.source_invalid_at.is_(None),
        ).update(
            {"source_invalid_at": datetime.now(timezone.utc).replace(tzinfo=None)},
            synchronize_session=False,
        )
        sess.commit()
    except Exception:
        sess.rollback()
        logger.warning("streaming: 标记 source_invalid_at 失败", exc_info=True)
    finally:
        sess.close()

TRANSCODE_DIR = os.getenv("EMBY_TRANSCODE_DIR", "/tmp/emby_transcode")
# 远程挂载（115 / WebDAV / AList）代理转发时的 UA
REMOTE_UA = os.getenv("MOUNT_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
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


def serve_remote(
    url: str,
    request: Request,
    headers: Optional[dict] = None,
    media_type: str = "video/mp4",
) -> StreamingResponse:
    """远程媒体代理：把客户端的请求（含 Range）转发到远端直链并流式回传

    为什么不直接 302 到第三方直链：

    - **凭据不下发**：115 Cookie、WebDAV Basic、AList 令牌全部留在服务器；
    - **地址稳定**：客户端只看到本服务器地址，换源 / 直链过期不需要客户端重新入库；
    - **可控**：代理层可以限速、统计与统一处理 CORS / Referer / UA 白名单。

    响应状态码与 ``Content-Range`` / ``Content-Length`` 原样透传，播放器的拖动与续播
    依赖它们，不能被吞掉。
    """
    import httpx

    forward = {k: v for k, v in (headers or {}).items()}
    # X-Pickcode 是内部头（用于 115 直链缓存失效），不下发给源站（与异步版同口径）
    forward.pop("X-Pickcode", None)
    forward.setdefault("User-Agent", REMOTE_UA)
    range_header = request.headers.get("range")
    if range_header:
        forward["Range"] = range_header

    # 重定向自己追（不用 httpx 的 follow_redirects）：每一跳都要校验目标是不是内网，
    # 并在跨主机时剥掉凭据头——否则一个公开直链就能把服务器（带着你的 Cookie / Basic）
    # 引到内网运维接口上。
    # S2：同一套超时（读超时 = 块间空闲上限），源站卡住时不再永久占着线程
    #
    # A-P2-2 DNS rebinding 防护：校验时解析一次、建连时 pin 住 IP（见 dns_pinning.py）。
    # pinning dict 与 transport 共享引用，逐跳 update 后实时生效，无需重建 client。
    from backend.emby_server.dns_pinning import PinnedHTTPTransport

    _dns_pinning: dict[str, list[str]] = {}
    client = httpx.Client(
        transport=PinnedHTTPTransport(_dns_pinning.get),
        timeout=relay_timeout(),
        follow_redirects=False,
    )
    # resp 预置为 None：兜底 except 分支需要关闭已打开的流式响应，
    # 若第一跳 send 就抛异常则 resp 尚未绑定；与异步版（#584）保持同口径。
    resp = None
    try:
        # _pre_convert_url：drive_auth_headers 建 SA 亲和用的 key（转换前的原始 URL）；
        # 403 换 SA 重试时要打破的正是这个 key 的亲和。
        _pre_convert_url = url
        # Drive 私有化修复（2026-10-10）：uc?export=download&id= 直链即使带 SA
        # Bearer token 也只返回登录页；转成 Drive API alt=media 端点才能用 token
        # 下载私有文件。www.googleapis.com 已在 DRIVE_HOSTS 白名单内，重定向/
        # 凭据保留逻辑不受影响。
        try:
            from backend.emby_server.drive_auth import drive_api_media_url
            url = drive_api_media_url(url)
        except Exception:
            pass
        current = None
        api_url = url
        # 建连走共用 helper（与重连建连同一套：手动跟随重定向、逐跳内网校验、
        # 跨主机剥凭据、DNS pinning）；silent=False 保持原错误语义。
        # forward_orig：重连建连的起点必须是"剥凭据之前"的原始头（与初始请求一致），
        # 不能用跳转后被剥过的 forward，否则 115 这类源站重连会丢 Cookie。
        forward_orig = dict(forward)
        # 单文件配额负缓存（与异步版同口径）：命中时本地直接回 403，不碰 Drive
        _quota_fid = _drive_alt_media_file_id(api_url)
        if _quota_fid:
            _retry_after = drive_quota_blocked_retry_after(_quota_fid)
            if _retry_after:
                logger.warning(
                    "Drive 单文件配额负缓存命中 file_id=%s，本地拒绝（约 %ds 后可重试）",
                    _quota_fid, _retry_after)
                client.close()
                raise HTTPException(
                    status_code=403,
                    detail="该文件今日下载次数已达网盘上限，请稍后再试",
                    headers={"Retry-After": str(_retry_after)})
        resp, current, forward = _open_upstream_sync(
            client, api_url, forward, _dns_pinning, silent=False)
        # Drive alt=media 403：记 Google 侧 reason，可重试类换 SA 重试（与异步版同口径）。
        # 传 forward_orig（剥凭据之前的原始头，含 Authorization），而不是跳转后被剥过的 forward。
        if resp is not None and resp.status_code == 403 and _drive_alt_media_file_id(current):
            resp, current, forward = _drive_403_retry_sync(
                client, api_url, forward_orig, resp, current,
                affinity_key=_pre_convert_url, dns_pinning=_dns_pinning)
        # 115 直链过期检测：403/404/410 时清 115 缓存，
        # 客户端重试将拿到新直链（2026-10-09）。与异步版 serve_remote_async 同口径。
        if resp.status_code in (403, 404, 410):
            _pickcode = (headers or {}).get("X-Pickcode")
            if _pickcode:
                try:
                    from backend.emby_server.mounts import _invalidate_115_url
                    _invalidate_115_url(_pickcode)
                    logger.info("115 direct URL expired, cache cleared")
                except Exception:
                    pass
        # Drive 源站 401 检测：SA Bearer token 已失效（密钥轮换/提前吊销），
        # 按 token 值失效内外两层缓存，下次播放重新换取；否则坏 token 在缓存里
        # 毒化 55 分钟，期间命中该 SA 的请求全部 401（2026-10-11）。
        # 与异步版 serve_remote_async 同口径
        if resp.status_code == 401:
            try:
                _auth_value = ""
                for _k, _v in (forward or {}).items():
                    if str(_k).lower() == "authorization":
                        _auth_value = _v or ""
                        break
                from backend.emby_server import drive_auth
                drive_auth.note_drive_unauthorized(current, _auth_value)
            except Exception:
                pass
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — 源站不可达：给出干净的 502，而非 500 堆栈
        if resp is not None:
            resp.close()
        client.close()
        logger.warning("远程媒体代理失败 %s: %s", url.split("?")[0], exc)
        raise HTTPException(status_code=502, detail="源站不可达")

    if resp.status_code >= 400:
        status = resp.status_code
        resp.close()
        client.close()
        logger.warning("远程媒体源站返回 %s: %s", status, url.split("?")[0])
        raise HTTPException(status_code=502 if status >= 500 else status, detail=f"源站返回 {status}")

    passthrough = {}
    for name in ("content-range", "accept-ranges", "content-length", "last-modified", "etag"):
        value = resp.headers.get(name)
        if value:
            passthrough[name.title()] = value
    # 与本地路径（serve_file）同口径：远程源本身支持 Range，即使上游 200
    # 没给这个头也要补上——否则播放器以为不可分段，对大文件只能顺序硬下，
    # 读不到片尾的索引（moov）就一直没画面、进度不动。
    passthrough.setdefault("Accept-Ranges", "bytes")
    # 边缘缓存调试头（通用能力，与异步版同口径）
    try:
        from backend.emby_server import edge_cache
        _edge_dbg = edge_cache.debug_header_value()
        if _edge_dbg:
            passthrough[edge_cache.DEBUG_HEADER] = _edge_dbg
    except Exception:
        pass
    content_type = resp.headers.get("content-type") or media_type

    # 慢连接自动重连状态：只对可续传上游启用；客户端只看到一条连续响应流，
    # 状态码/响应头保持初始建连时的值不变。
    _stall_state = _RelayStallState(
        _resume_offset(resp, range_header),
        log_ctx=url.split("?")[0],
        enabled=_RELAY_STALL_RECONNECT and _upstream_resumable(resp),
    )

    def iter_remote():
        cur_resp = resp
        it = None
        try:
            while True:
                if it is None:
                    it = cur_resp.iter_bytes(READ_CHUNK)
                # 只统计"等上游出数据"的时间：yield 处被慢客户端阻塞的时间不计入，
                # 客户端下行慢不会误触发重连。
                t0 = time.monotonic()
                try:
                    chunk = next(it)
                except StopIteration:
                    break
                wait = time.monotonic() - t0
                yield chunk
                _stall_state.on_chunk(len(chunk), wait)
                if _stall_state.want_reconnect():
                    ok, payload = _try_reconnect_upstream_sync(
                        client, api_url, forward_orig, _dns_pinning, _stall_state)
                    if ok:
                        new_resp, new_it = payload
                        old_resp, old_it = cur_resp, it
                        cur_resp, it = new_resp, new_it
                        # make-before-break：新连接已确认可用，才断旧连；
                        # 先关旧迭代器，再关旧响应。
                        try:
                            old_it.close()
                        except Exception:
                            pass
                        try:
                            old_resp.close()
                        except Exception:
                            pass
                        _stall_state.note_success()
                        # 首块=上一块（已给过客户端），确认用、此处不再下发
                        continue
                    reason = payload
                    logger.info("[relay-reconnect] 本次重连未生效，继续旧连接 %s：%s",
                                _stall_state.log_ctx, reason)
        except httpx.TransportError as exc:
            logger.warning("远程代理流中断 %s: %s", url.split("?")[0], type(exc).__name__)
        finally:
            try:
                cur_resp.close()
            except Exception:
                pass
            client.close()

    return StreamingResponse(
        iter_remote(), status_code=resp.status_code, media_type=content_type, headers=passthrough,
    )


# ---- 共享连接池（中转秒播：连接复用）----
#
# 原来每次代理请求都新建一个 ``httpx.AsyncClient``，用完就关：每个 Range 请求
# （用户拖进度条一次就一个）都要重新 TCP 握手 + TLS 握手到源站（Google Drive
# 是跨洲链路，两次握手加起来就是几百毫秒的白等）。改成进程内共享一个 client，
# 连接在多次请求之间复用。
#
# **只共享 client，不共享响应**：``resp`` 仍然是每次请求自己的流，用完在
# ``iter_remote`` 的 finally 里关掉。池里的连接会在响应体读完/关掉后归还。
_shared_client: Optional["httpx.AsyncClient"] = None
_client_lock = threading.Lock()

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
_RELAY_MAX_CONNECTIONS = _env_number("RELAY_MAX_CONNECTIONS", 200, 4, 5000, int)
#: 空闲 keep-alive 连接上限（不超过总上限）
_RELAY_MAX_KEEPALIVE = min(_RELAY_MAX_CONNECTIONS,
                           _env_number("RELAY_MAX_KEEPALIVE", 50, 2, 5000, int))
#: 连上源站的超时（秒）
_RELAY_CONNECT_TIMEOUT = _env_number("RELAY_CONNECT_TIMEOUT", 10.0, 1.0, 120.0)
#: 读超时（秒）= 两个数据块之间的最长空闲。旧值 None：源站 TCP 不断、不回数据时
#: 连接与请求永远挂着、只增不减直到池耗尽。正常播放的块间隔远小于它。
_RELAY_READ_TIMEOUT = _env_number("RELAY_READ_TIMEOUT", 30.0, 5.0, 600.0)
#: 等池里空出连接的上限（秒）。旧值继承 30s：池满时用户白等 30 秒再拿 502；
#: 现在很快给 503 + Retry-After，播放器会重试。
_RELAY_POOL_TIMEOUT = _env_number("RELAY_POOL_TIMEOUT", 3.0, 0.1, 60.0)
_RELAY_WRITE_TIMEOUT = 30.0


# ---- 慢连接自动重连（2026-10-13）----
#
# 背景：Google usercontent 单连接偶发被掐速到几十 KB/s（起播正常、ttfb 正常，
# 随后单条连接塌速；客户端重连即满速，服务端实测同文件 8.97MB/s）。
# 客户端（SenPlayer 等）不会自己重连，中继层兜底：监测上游出数据速度，
# 塌速时 make-before-break 建新连接续传，客户端只看到一条连续的响应流。
#
# 三条保护（缺一不可）：
# 1. make-before-break：新连接确认 206 + 首块数据后才切断旧连；新连失败则
#    继续用旧连——"慢"绝不能变成"断"。
# 2. 只看"上游等待时间"：生成器在 yield 处被慢客户端阻塞的时间不计入，
#    客户端下行慢不会误触发重连。
# 3. 全局保护：令牌桶（全服重连预算）+ 熔断（短窗口大量塌速→疑似账号级限速/
#    出口故障，冷却期内全服停重连）+ 重连时换 SA（打破亲和，分散单 SA 风险）。
# 全部可配，见 docs/relay-stall-reconnect.md；非法值 fail-closed 回默认。
#: 总开关（默认开）
_RELAY_STALL_RECONNECT = _env_flag("RELAY_STALL_RECONNECT", True)
#: 塌速阈值（KB/s）：窗口内"上游字节 / 上游等待秒"低于此值 → 塌速
_RELAY_STALL_MIN_KBPS = _env_number("RELAY_STALL_MIN_KBPS", 180.0, 10.0, 100000.0)
#: 判定窗口（秒，按"上游等待时间"累计）
_RELAY_STALL_WINDOW_SEC = _env_number("RELAY_STALL_WINDOW_SEC", 10.0, 3.0, 120.0)
#: 起播宽限：已转发字节数 / 墙钟秒数（任一达到即开始判定，首字节阶段不算）
_RELAY_STALL_GRACE_BYTES = _env_number("RELAY_STALL_GRACE_BYTES", 1048576, 65536, 1 << 30, int)
_RELAY_STALL_GRACE_SEC = _env_number("RELAY_STALL_GRACE_SEC", 15.0, 1.0, 300.0)
#: 每条流最多重连尝试次数（0=只监测不重连）
_RELAY_STALL_MAX_RECONNECTS = _env_number("RELAY_STALL_MAX_RECONNECTS", 3, 0, 10, int)
#: 同流连续重连退避：基数 * 2^(尝试次数-1)，上限封顶，再加 0~1s 随机抖动（反风控）
_RELAY_STALL_BACKOFF_BASE = _env_number("RELAY_STALL_BACKOFF_BASE_SEC", 2.0, 0.5, 60.0)
_RELAY_STALL_BACKOFF_CAP = _env_number("RELAY_STALL_BACKOFF_CAP_SEC", 30.0, 1.0, 300.0)
#: 全局重连预算：全服每分钟最多重连尝试次数（令牌桶）
_RELAY_STALL_BUDGET_PER_MIN = _env_number("RELAY_STALL_BUDGET_PER_MIN", 20, 1, 3600, int)
#: 全局熔断：窗口秒数内重连尝试达到阈值 → 冷却秒数内全服暂停重连
_RELAY_STALL_BREAKER_WINDOW_SEC = _env_number("RELAY_STALL_BREAKER_WINDOW_SEC", 60.0, 10.0, 3600.0)
_RELAY_STALL_BREAKER_THRESHOLD = _env_number("RELAY_STALL_BREAKER_THRESHOLD", 10, 2, 1000, int)
_RELAY_STALL_BREAKER_COOLDOWN_SEC = _env_number("RELAY_STALL_BREAKER_COOLDOWN_SEC", 300.0, 30.0, 3600.0)
#: 被全局预算/熔断拒绝后的短冷却（秒）：避免每 chunk 刷一次拒绝日志
_RELAY_STALL_DEFER_COOLDOWN = 5.0


class _StallDetector:
    """塌速判定器：纯逻辑、无 I/O、无锁，每条流独占一个实例。

    只统计"上游等待时间"——await 上游吐出下一个块的耗时。生成器在 ``yield``
    处被慢客户端阻塞的时间**不计入**（慢客户端只会让 yield park 住，不会让
    上游出数据变慢），所以客户端下行慢不会误触发重连。

    滑动窗口按上游等待时间的累计值推进：窗口内 上游字节 / 上游等待秒 < 阈值 → 塌速。
    每 chunk 一次 observe，摊还 O(1)，不给转发热路径加负担。
    """

    __slots__ = ("_window_sec", "_min_bps", "_grace_bytes", "_grace_sec",
                 "_samples", "_wait_sum", "_bytes_sum", "_total_bytes", "_start_wall")

    def __init__(self) -> None:
        # 读模块级常量（测试可 monkeypatch 模块常量改变行为）
        self._window_sec = float(_RELAY_STALL_WINDOW_SEC)
        self._min_bps = float(_RELAY_STALL_MIN_KBPS) * 1024.0
        self._grace_bytes = int(_RELAY_STALL_GRACE_BYTES)
        self._grace_sec = float(_RELAY_STALL_GRACE_SEC)
        self._samples: deque[tuple[float, int]] = deque()  # (上游等待秒, 字节)
        self._wait_sum = 0.0
        self._bytes_sum = 0
        self._total_bytes = 0
        self._start_wall: float | None = None

    def observe(self, nbytes: int, wait_sec: float) -> None:
        now = time.monotonic()
        if self._start_wall is None:
            self._start_wall = now
        if wait_sec < 0:
            wait_sec = 0.0
        self._total_bytes += nbytes
        self._samples.append((wait_sec, nbytes))
        self._wait_sum += wait_sec
        self._bytes_sum += nbytes
        # 窗口按等待时间累计滑动：只保留最近 _window_sec 秒的等待量（至少留一个样本）
        while len(self._samples) > 1 and \
                self._wait_sum - self._samples[0][0] >= self._window_sec:
            w, b = self._samples.popleft()
            self._wait_sum -= w
            self._bytes_sum -= b

    def stalled(self) -> bool:
        if self._start_wall is None:
            return False
        # 起播宽限：字节数和墙钟任一达标才开始判定（首字节/TTFB 阶段不算）
        if self._total_bytes < self._grace_bytes and \
                time.monotonic() - self._start_wall < self._grace_sec:
            return False
        if self._wait_sum <= 0:
            return False
        return (self._bytes_sum / self._wait_sum) < self._min_bps

    def snapshot_kbps(self) -> float:
        """当前窗口平均速度（KB/s），打日志用。"""
        if self._wait_sum <= 0:
            return 0.0
        return (self._bytes_sum / self._wait_sum) / 1024.0

    def reset(self) -> None:
        """重连成功后清窗口：新连接重新积累信号，旧连接的慢速不污染判定。
        宽限期只给一次（_total_bytes / _start_wall 保留）。"""
        self._samples.clear()
        self._wait_sum = 0.0
        self._bytes_sum = 0


class _ReconnectBudget:
    """全局重连令牌桶：全服每分钟重连尝试上限。

    锁只在触发重连的瞬间持有（临界区几次浮点运算）；热路径每 chunk 不碰，
    不会拖慢转发。
    """

    def __init__(self, per_min: float, _clock=time.monotonic) -> None:
        self._capacity = float(per_min)
        self._tokens = float(per_min)
        self._lock = threading.Lock()
        self._clock = _clock
        self._last = _clock()

    def try_acquire(self) -> bool:
        now = self._clock()
        with self._lock:
            self._tokens = min(self._capacity,
                               self._tokens + (now - self._last) * self._capacity / 60.0)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._tokens = self._capacity
            self._last = self._clock()


class _ReconnectBreaker:
    """全局熔断：短窗口内重连尝试过多 → 更像账号级限速/出口故障而非单连接运气差，
    打开冷却期，期间全服不再尝试重连，避免扎堆请求加重限速/触发 Google 风控。
    冷却结束后自动闭合（下一次尝试正常走预算）。

    ``is_open`` 无锁读 ``_open_until``：CPython 下 float 属性读写原子，
    并发下最多"晚一点看到闭合"，fail-safe（宁可多拦一次，不多放一次）。
    """

    def __init__(self, window_sec: float, threshold: int, cooldown_sec: float,
                 _clock=time.monotonic) -> None:
        self._window_sec = float(window_sec)
        self._threshold = int(threshold)
        self._cooldown_sec = float(cooldown_sec)
        self._clock = _clock
        self._attempts: deque[float] = deque()
        self._open_until = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def record(self) -> None:
        """每次实际发起重连尝试后调用；触发熔断时打 warning 日志。"""
        now = self._clock()
        tripped = False
        count = 0
        with self._lock:
            self._attempts.append(now)
            cutoff = now - self._window_sec
            while self._attempts and self._attempts[0] < cutoff:
                self._attempts.popleft()
            count = len(self._attempts)
            if count >= self._threshold and now >= self._open_until:
                self._open_until = now + self._cooldown_sec
                tripped = True
        if tripped:
            logger.warning(
                "[relay-reconnect] 全局熔断打开：%.0fs 内重连尝试 %d 次"
                "（疑似账号级限速/出口故障），冷却 %.0fs 内暂停全服重连",
                self._window_sec, count, self._cooldown_sec)

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._attempts.clear()
            self._open_until = 0.0


_RECONNECT_BUDGET = _ReconnectBudget(_RELAY_STALL_BUDGET_PER_MIN)
_RECONNECT_BREAKER = _ReconnectBreaker(
    _RELAY_STALL_BREAKER_WINDOW_SEC,
    _RELAY_STALL_BREAKER_THRESHOLD,
    _RELAY_STALL_BREAKER_COOLDOWN_SEC,
)


def _reset_reconnect_state() -> None:
    """测试钩子：重置全局预算与熔断（生产代码不调用）。"""
    _RECONNECT_BUDGET._reset_for_tests()
    _RECONNECT_BREAKER._reset_for_tests()


class _RelayStallState:
    """单条流的重连状态：检测器 + 已转发字节 + 尝试计数 + 退避（无锁，单流独占）。"""

    def __init__(self, range_start: int, log_ctx: str, enabled: bool) -> None:
        self.range_start = range_start
        self.log_ctx = log_ctx
        self.enabled = enabled
        self.sent = 0
        self.attempts = 0
        self.reconnects = 0
        self.next_allowed_at = 0.0
        self.detector = _StallDetector() if enabled else None

    def on_chunk(self, nbytes: int, wait_sec: float) -> None:
        self.sent += nbytes
        if self.detector is not None:
            self.detector.observe(nbytes, wait_sec)

    @property
    def resume_at(self) -> int:
        # overlap 回退一整块：httpx 的 ByteChunker 中途只吐整块，旧迭代器里至多
        # 残留 <1块 已读未吐出的数据；新连接从 sent-READ_CHUNK 开始，首块
        # （=上一块，已给过客户端）丢弃后无缝续传，无缺口无重复。
        return self.range_start + self.sent - READ_CHUNK

    def want_reconnect(self) -> bool:
        if not self.enabled or self.detector is None:
            return False
        if self.attempts >= _RELAY_STALL_MAX_RECONNECTS:
            return False
        if self.sent < READ_CHUNK:
            return False
        if time.monotonic() < self.next_allowed_at:
            return False
        return self.detector.stalled()

    def note_attempt(self) -> None:
        # 指数退避 + 0~1s 随机抖动：避免多条流同时塌速时重连请求扎堆（反风控）
        self.attempts += 1
        delay = min(_RELAY_STALL_BACKOFF_BASE * (2 ** (self.attempts - 1)),
                    _RELAY_STALL_BACKOFF_CAP)
        self.next_allowed_at = time.monotonic() + delay + random.uniform(0, 1.0)

    def note_deferred(self) -> None:
        # 被全局预算/熔断拒绝：短冷却避免每 chunk 刷一次拒绝日志，不计入 attempts
        self.next_allowed_at = (time.monotonic() + _RELAY_STALL_DEFER_COOLDOWN
                                + random.uniform(0, 1.0))

    def note_success(self) -> None:
        self.reconnects += 1
        if self.detector is not None:
            self.detector.reset()


def _upstream_resumable(resp) -> bool:
    """上游是否支持 Range 续传：只有可续传的才启用慢连接重连。"""
    accept = (resp.headers.get("accept-ranges") or "").lower()
    if "bytes" in accept:
        return True
    return bool(resp.headers.get("content-range"))


def _resume_offset(resp, range_header: str | None) -> int:
    """续传偏移起点：优先上游 Content-Range，其次客户端 Range，再次 0。"""
    cr = resp.headers.get("content-range") or ""
    m = re.match(r"bytes (\d+)-", cr)
    if m:
        return int(m.group(1))
    if range_header:
        m = RANGE_RE.match(range_header.strip())
        if m and m.group(1):
            return int(m.group(1))
    return 0


def _old_auth_token(forward: dict) -> str:
    for k, v in (forward or {}).items():
        if str(k).lower() == "authorization":
            return str(v or "")
    return ""


def _fresh_sa_token(old_authorization: str, api_url: str) -> str | None:
    """取一个与旧 token 不同的新 SA token（打破亲和，分散单 SA 被掐速的风险）。

    只用于 Drive 上游；失败返回 None（调用方继续用旧 token 建新连接，不阻塞重连）。
    token 按 SA 独立缓存、预铸常驻，取用是内存命中（毫秒级），不增加 Google 请求。
    """
    if not _drive_alt_media_file_id(api_url):
        return None
    old_token = old_authorization[len("Bearer "):].strip() \
        if old_authorization.startswith("Bearer ") else old_authorization.strip()
    try:
        from backend.emby_server import drive_auth
        for _ in range(3):
            token = drive_auth.get_drive_bearer_token(affinity_key=None)
            if token and token != old_token:
                return token
        return None
    except Exception:
        return None


async def _drive_403_retry_async(client, api_url: str, forward: dict, resp, current: str,
                                 affinity_key: str | None):
    """Drive alt=media 终态 403：记 Google 侧 reason；可重试类换 SA 重试（有界+抖动）。

    affinity_key：转换前的原始 URL（drive_auth_headers 建 SA 亲和时用的 key）；
    打破的正是这个 key 的亲和，drop api_url 是无用功。

    返回 (resp, current)。调用方原有 >=400 分支继续处理最终 resp。
    本函数永不抛异常：内部异常一律吞掉并返回最新的 (resp, current)。
    """
    # 用局部变量跟踪"最新"的 resp/current，保证异常兜底时不会返回最初的旧 resp
    _last_resp, _last_current = resp, current
    try:
        max_retry = _env_number("DRIVE_403_SA_RETRY_MAX", 2, 0, 5, cast=int)
        jitter_ms = _env_number("DRIVE_403_SA_RETRY_JITTER_MS", 800, 0, 10000)
        fid = _drive_alt_media_file_id(_last_current) or ""
        rescue_attempted = False

        for attempt in range(max_retry + 1):
            # 读取响应体，仅取前 4096 字节用于解析 reason
            try:
                body = await _last_resp.aread()
            except Exception:
                body = b""

            reason, message = _drive_403_error_reason(body[:4096])

            # 只记 file_id / reason / message，绝不记 token 或完整 headers
            logger.warning(
                "Drive 下载 403 file_id=%s reason=%s msg=%.120s attempt=%d",
                fid, reason or "-", message or "-", attempt)

            # 单文件配额超限：先救援（换入口/短冷却，本请求内至多一次），
            # 救援全失败才记入跨请求负缓存，后续同文件请求本地直接拒绝
            if reason == "downloadQuotaExceeded":
                if not rescue_attempted:
                    rescue_attempted = True
                    rescued = await _quota_rescue_async(
                        client, api_url, forward, fid)
                    if rescued is not None:
                        try:
                            await _last_resp.aclose()
                        except Exception:
                            pass
                        _last_resp, _last_current = rescued
                        break
                note_drive_quota_exceeded(fid)

            # 不可重试类，或已达重试上限：交给调用方处理
            if reason in _DRIVE_403_NON_RETRYABLE or attempt >= max_retry:
                break

            # 换 SA：先丢弃转换前原始 URL 的 SA 亲和，再取一个与旧 token 不同的新 token
            try:
                from backend.emby_server import drive_auth
                drive_auth.drop_sa_affinity(affinity_key)
            except Exception:
                pass

            new_token = _fresh_sa_token(_old_auth_token(forward), api_url)

            try:
                await _last_resp.aclose()
            except Exception:
                pass

            if not new_token:
                break

            # 反风控抖动
            await asyncio.sleep(random.uniform(0, jitter_ms / 1000.0))

            new_forward = dict(forward)
            new_forward["Authorization"] = "Bearer " + new_token

            try:
                resp, current, _hops = await _follow_redirects(
                    client, api_url, new_forward, silent=False)
            except Exception:
                break

            _last_resp, _last_current = resp, current

            # 仍是 403 且仍是 Drive alt=media 才继续重试
            if (resp is None or resp.status_code != 403
                    or not _drive_alt_media_file_id(current)):
                break
    except Exception:
        logger.debug("Drive 403 换 SA 重试过程异常，返回当前响应", exc_info=True)

    return _last_resp, _last_current


def _drive_403_retry_sync(client, api_url: str, forward: dict, resp, current: str,
                          affinity_key: str | None, dns_pinning: dict):
    """同步版：Drive alt=media 终态 403，记 reason、可重试类换 SA 重试（有界+抖动）。

    与 `_drive_403_retry_async` 逐行对应（供 serve_remote / 下载与拉文件路径用）。

    返回 (resp, current, forward)：forward 为最后一次实际建连用的头
    （未重试时原样返回传入的 forward）。永不抛异常。
    """
    _last_resp, _last_current, _last_forward = resp, current, forward
    try:
        max_retry = _env_number("DRIVE_403_SA_RETRY_MAX", 2, 0, 5, cast=int)
        jitter_ms = _env_number("DRIVE_403_SA_RETRY_JITTER_MS", 800, 0, 10000)
        fid = _drive_alt_media_file_id(_last_current) or ""
        rescue_attempted = False

        for attempt in range(max_retry + 1):
            try:
                body = _last_resp.read()
            except Exception:
                body = b""

            reason, message = _drive_403_error_reason(body[:4096])

            logger.warning(
                "Drive 下载 403 file_id=%s reason=%s msg=%.120s attempt=%d",
                fid, reason or "-", message or "-", attempt)

            # 单文件配额超限：先救援（与异步版同口径），全失败才记入负缓存
            if reason == "downloadQuotaExceeded":
                if not rescue_attempted:
                    rescue_attempted = True
                    rescued = _quota_rescue_sync(
                        client, api_url, _last_forward, fid, dns_pinning)
                    if rescued is not None:
                        try:
                            _last_resp.close()
                        except Exception:
                            pass
                        _last_resp, _last_current, _last_forward = rescued
                        break
                note_drive_quota_exceeded(fid)

            if reason in _DRIVE_403_NON_RETRYABLE or attempt >= max_retry:
                break

            try:
                from backend.emby_server import drive_auth
                drive_auth.drop_sa_affinity(affinity_key)
            except Exception:
                pass

            new_token = _fresh_sa_token(_old_auth_token(_last_forward), api_url)

            try:
                _last_resp.close()
            except Exception:
                pass

            if not new_token:
                break

            time.sleep(random.uniform(0, jitter_ms / 1000.0))

            new_forward = dict(_last_forward)
            new_forward["Authorization"] = "Bearer " + new_token

            try:
                resp, current, _h = _open_upstream_sync(
                    client, api_url, new_forward, dns_pinning, silent=False)
            except Exception:
                break

            _last_resp, _last_current, _last_forward = resp, current, new_forward

            if (resp is None or resp.status_code != 403
                    or not _drive_alt_media_file_id(current)):
                break
    except Exception:
        logger.debug("Drive 403 换 SA 重试过程异常，返回当前响应", exc_info=True)

    return _last_resp, _last_current, _last_forward


async def _follow_redirects(
    client,
    start_url: str,
    forward: dict,
    silent: bool = False,
) -> tuple:
    """手动跟随 HTTP 重定向（serve_remote_async 与 resolve_drive_final_url 共用，P3-5）。

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


def relay_timeout() -> "httpx.Timeout":
    """共享代理客户端（以及同步下载代理）用的超时组合"""
    import httpx

    return httpx.Timeout(
        connect=_RELAY_CONNECT_TIMEOUT,
        read=_RELAY_READ_TIMEOUT,
        write=_RELAY_WRITE_TIMEOUT,
        pool=_RELAY_POOL_TIMEOUT,
    )


def get_relay_client() -> "httpx.AsyncClient":
    """取（或懒建）共享的代理客户端。

    懒建 + 线程锁：首次调用可能并发，用锁避免建出两个池。
    """
    import httpx

    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                from backend.emby_server.dns_pinning import PinnedAsyncHTTPTransport

                limits = httpx.Limits(
                    max_connections=_RELAY_MAX_CONNECTIONS,
                    max_keepalive_connections=_RELAY_MAX_KEEPALIVE,
                )
                _shared_client = httpx.AsyncClient(
                    # A-P2-2：DNS pinning——校验时刻解析一次、建连走 pin 住的 IP，
                    # 堵住 DNS rebinding TOCTOU；总开关 EMBY_DNS_PINNING 实时生效。
                    # 注意：传了自定义 transport 后 client 的 limits 参数会被忽略，
                    # 所以 limits 直接给 transport。
                    transport=PinnedAsyncHTTPTransport(
                        _PINNING_CACHE.lookup, limits=limits),
                    timeout=relay_timeout(),
                    # 重定向仍由本模块自己追（要逐跳校验、跨主机剥凭据）
                    follow_redirects=False,
                )
    return _shared_client


async def close_relay_client() -> None:
    """关掉共享客户端（进程退出 / 测试收尾用）"""
    global _shared_client
    client = _shared_client
    _shared_client = None
    if client is not None:
        await client.aclose()


def _open_upstream_sync(client, api_url: str, forward: dict, dns_pinning: dict,
                        silent: bool = False):
    """同步建连：手动跟随重定向（逐跳内网校验 + 跨主机剥凭据），与原 serve_remote
    内联逻辑一致，只是抽出来给初始建连和重连建连共用。

    silent=False（初始建连）：非法重定向目标 → HTTPException(403)，
    跳转超限 → HTTPException(502)，建连异常向上传播（调用方转 502）。
    silent=True（重连建连）：上述情况一律返回 ``(None, current, forward)``，
    不抛异常——调用方继续用旧连接。

    成功返回 ``(resp, final_url, forward)``；resp 为终态流式响应，由调用方关闭。
    """
    current = api_url
    resp = None
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            resp = client.send(client.build_request("GET", current, headers=forward), stream=True)
        except Exception:
            if silent:
                return None, current, forward
            raise
        if resp.status_code not in _REDIRECT_CODES:
            return resp, current, forward
        location = resp.headers.get("location")
        if not location:
            return resp, current, forward  # 3xx 却没给 Location：当作终态
        try:
            target, _hop_pinning = _next_redirect(current, location)
        except MountError as exc:
            resp.close()
            logger.warning("拒绝代理到重定向目标 %s: %s", location, exc)
            if silent:
                return None, current, forward
            client.close()
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — 畸形 Location（如 http://[::1）：
            # _next_redirect 抛非 MountError 的 ValueError；关掉已打开的 resp
            # （否则慢泄漏共享池），silent 下返回 None，非 silent 下关 client 转 502
            # ——与原 serve_remote 内联逻辑的外层 except 口径一致。
            try:
                resp.close()
            except Exception:
                pass
            logger.warning("重定向目标解析失败 %s: %s", location, exc)
            if silent:
                return None, current, forward
            client.close()
            raise HTTPException(status_code=502, detail="源站不可达") from exc
        # A-P2-2：把校验时刻的 DNS 快照并入 pin 表，下一跳建连直接走 pin 住的 IP
        dns_pinning.update(_hop_pinning)
        resp.close()  # 确认要跟着走，这一跳的响应就不要再占着连接了
        forward = _redirect_headers(forward, current, target)
        current = target
    # 跳转超限：最后一个 resp 是 3xx 且还开着，先关掉（与原内联逻辑一致）
    try:
        if resp is not None:
            resp.close()
    except Exception:
        pass
    if silent:
        return None, current, forward
    client.close()
    raise HTTPException(status_code=502, detail="源站重定向次数过多")


def _check_new_upstream(resp, offset: int) -> str | None:
    """校验新连接可用性：必须是 206 且 Content-Range 起点 == 期望偏移。

    返回 None 表示可用；否则返回失败原因（调用方记日志后继续用旧连接）。
    """
    if resp.status_code != 206:
        return f"状态码 {resp.status_code}（期望 206）"
    cr = resp.headers.get("content-range") or ""
    m = re.match(r"bytes (\d+)-", cr)
    if not m:
        return "缺 Content-Range（无法确认拼接点）"
    if int(m.group(1)) != offset:
        return f"Content-Range 起点 {m.group(1)} 与期望偏移 {offset} 不符"
    return None


def _build_reconnect_forward(forward: dict, offset: int, old_auth: str,
                             api_url: str) -> tuple[dict, bool]:
    """构造重连请求头：Range 改为续传偏移；Drive 上游换一个 SA（打破亲和）。

    返回 ``(new_forward, sa_switched)``。
    """
    new_forward = dict(forward)
    new_forward["Range"] = f"bytes={offset}-"
    sa_switched = False
    new_token = _fresh_sa_token(old_auth, api_url)
    if new_token:
        new_forward["Authorization"] = f"Bearer {new_token}"
        sa_switched = True
    return new_forward, sa_switched


def _reconnect_guards(log_ctx: str, offset: int) -> str | None:
    """全局保护检查：熔断 / 预算。返回 None 表示放行，否则返回拒绝原因。

    只在触发重连的瞬间调用（热路径不碰）；拒绝后调用方给短冷却，避免刷日志。
    """
    if _RECONNECT_BREAKER.is_open():
        return "熔断冷却中"
    if not _RECONNECT_BUDGET.try_acquire():
        return "全局重连预算耗尽"
    return None


async def _try_reconnect_upstream(client, api_url: str, forward: dict,
                                  state: _RelayStallState):
    """make-before-break 重连（异步版）：先建新连接并确认 206 + 首块数据，
    再返回 ``(True, new_resp, new_aiter)``；任何失败返回 ``(False, reason)``，
    调用方继续用旧连接——"慢"绝不能变成"断"。

    overlap 说明：新连接从 ``state.resume_at``（= 已发送 - 一整块）开始，
    首块（=上一块，已给过客户端）在确认后丢弃，后续块无缝续传。
    旧连接在新连接确认可用之前一直开着、继续服务。
    """
    log_ctx = state.log_ctx
    offset = state.resume_at
    deny = _reconnect_guards(log_ctx, offset)
    if deny is not None:
        logger.info("[relay-reconnect] %s，跳过重连 %s 偏移=%d", deny, log_ctx, offset)
        state.note_deferred()
        return False, deny
    state.note_attempt()
    # 熔断计数：每次实际发起尝试都记（不只记成功的）——账号级限速时新连接会批量
    # 失败，只记成功就永远打不开熔断，起不到保护作用。
    _RECONNECT_BREAKER.record()
    new_forward, sa_switched = _build_reconnect_forward(
        forward, offset, _old_auth_token(forward), api_url)
    kbps = state.detector.snapshot_kbps() if state.detector else 0.0
    logger.info("[relay-reconnect] 塌速触发重连 %s 偏移=%d 已传=%dKB 塌速时=%.0fKB/s"
                "(阈值%.0f) 第%d次尝试 换SA=%s",
                log_ctx, offset, state.sent // 1024, kbps,
                _RELAY_STALL_MIN_KBPS, state.attempts, sa_switched)
    try:
        new_resp, _final_url, _hops = await _follow_redirects(
            client, api_url, new_forward, silent=True)
    except Exception as exc:  # noqa: BLE001 — 建连异常：旧连接继续服务
        return False, f"新连接建连异常 {type(exc).__name__}"
    if new_resp is None:
        return False, "新连接重定向校验失败"
    try:
        if new_resp.status_code == 401:
            # 新 token 也 401：按既有口径失效它，下次重连会再换一个 SA
            try:
                from backend.emby_server import drive_auth
                drive_auth.note_drive_unauthorized(api_url, new_forward.get("Authorization", ""))
            except Exception:
                pass
        bad = _check_new_upstream(new_resp, offset)
        if bad is not None:
            await new_resp.aclose()
            return False, f"新连接不可用：{bad}"
        # 读第一块确认真的有数据（旧连接此时还开着，make-before-break）；
        # 首块 = 上一块（已给过客户端），确认后丢弃。
        new_aiter = new_resp.aiter_bytes(READ_CHUNK).__aiter__()
        try:
            first = await asyncio.wait_for(new_aiter.__anext__(),
                                           timeout=_RELAY_READ_TIMEOUT)
        except (TimeoutError, StopAsyncIteration) as exc:
            await new_resp.aclose()
            return False, f"新连接首块读取失败 {type(exc).__name__}"
        if len(first) != READ_CHUNK:
            # 中途块必须是整块；短块说明上游行为异常，不可信 splicing
            await new_resp.aclose()
            return False, f"新连接首块长度 {len(first)} 非整块"
    except asyncio.CancelledError:
        # 客户端在确认阶段断开：关掉新连接再向上传播，避免共享池泄漏
        try:
            await new_resp.aclose()
        except Exception:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        try:
            await new_resp.aclose()
        except Exception:
            pass
        return False, f"新连接确认异常 {type(exc).__name__}"
    logger.info("[relay-reconnect] 重连成功 %s 新偏移=%d 第%d次",
                log_ctx, offset, state.reconnects + 1)
    return True, (new_resp, new_aiter)


def _try_reconnect_upstream_sync(client, api_url: str, forward: dict,
                                 dns_pinning: dict, state: _RelayStallState):
    """make-before-break 重连（同步版）：语义与异步版完全一致。"""
    log_ctx = state.log_ctx
    offset = state.resume_at
    deny = _reconnect_guards(log_ctx, offset)
    if deny is not None:
        logger.info("[relay-reconnect] %s，跳过重连 %s 偏移=%d", deny, log_ctx, offset)
        state.note_deferred()
        return False, deny
    state.note_attempt()
    # 熔断计数：每次实际发起尝试都记（不只记成功的）——账号级限速时新连接会批量
    # 失败，只记成功就永远打不开熔断，起不到保护作用。
    _RECONNECT_BREAKER.record()
    new_forward, sa_switched = _build_reconnect_forward(
        forward, offset, _old_auth_token(forward), api_url)
    kbps = state.detector.snapshot_kbps() if state.detector else 0.0
    logger.info("[relay-reconnect] 塌速触发重连 %s 偏移=%d 已传=%dKB 塌速时=%.0fKB/s"
                "(阈值%.0f) 第%d次尝试 换SA=%s",
                log_ctx, offset, state.sent // 1024, kbps,
                _RELAY_STALL_MIN_KBPS, state.attempts, sa_switched)
    try:
        new_resp, _final_url, _fwd = _open_upstream_sync(
            client, api_url, new_forward, dns_pinning, silent=True)
    except Exception as exc:  # noqa: BLE001 — 建连异常：旧连接继续服务
        return False, f"新连接建连异常 {type(exc).__name__}"
    if new_resp is None:
        return False, "新连接重定向校验失败"
    try:
        if new_resp.status_code == 401:
            try:
                from backend.emby_server import drive_auth
                drive_auth.note_drive_unauthorized(api_url, new_forward.get("Authorization", ""))
            except Exception:
                pass
        bad = _check_new_upstream(new_resp, offset)
        if bad is not None:
            new_resp.close()
            return False, f"新连接不可用：{bad}"
        new_iter = new_resp.iter_bytes(READ_CHUNK)
        try:
            first = next(new_iter)
        except StopIteration:
            new_resp.close()
            return False, "新连接首块为空"
        if len(first) != READ_CHUNK:
            new_resp.close()
            return False, f"新连接首块长度 {len(first)} 非整块"
    except Exception as exc:  # noqa: BLE001
        try:
            new_resp.close()
        except Exception:
            pass
        return False, f"新连接确认异常 {type(exc).__name__}"
    logger.info("[relay-reconnect] 重连成功 %s 新偏移=%d 第%d次",
                log_ctx, offset, state.reconnects + 1)
    return True, (new_resp, new_iter)


# ---- Singleflight：同文件同 Range 的并发请求合并 ----
#
# 播放器起播/拖动时会并发打出多个相同的 Range 探测请求（同一个文件、
# 同一段字节），旧实现每个请求都独立建连打源站：浪费连接与 SA 请求量，
# 配额紧张时还会放大撞墙（2026-10-16 北斗事件：36 秒内同文件被打 12 次）。
# 这里做请求合并：key = (URL, Range, 鉴权摘要)，同 key 的并发请求只真正
# 取流一次，其余共享同一份结果（状态码/响应头/正文字节）。只合并「小而
# 有界」的 Range（起止明确且长度 ≤ 上限）——整文件/开放 Range 的大流量
# 走原路径逐字节流式转发、不进内存。合并只发生在同进程同一瞬间；领队
# 失败时等待者共享同一异常（与它自己此刻去打源站的结果一致），正文超限
# 等情况一律回落各自独立取流，语义与未合并时一致。
#
# 上限走环境变量 DRIVE_SINGLEFLIGHT_MAX_RANGE_BYTES（默认 8MiB，0=关闭合并）。
_SF_INFLIGHT: dict = {}
_SF_COUNTERS: dict[str, int] = {"leader": 0, "joined": 0, "fallback": 0}


def _sf_max_range_bytes() -> int:
    return int(_env_number(
        "DRIVE_SINGLEFLIGHT_MAX_RANGE_BYTES",
        8 * 1024 * 1024, 0, 256 * 1024 * 1024))


def _sf_range_length(range_header) -> Optional[int]:
    """单段 bytes=start-end 的长度；开放/多段/非法一律 None（不合并）。"""
    if not range_header:
        return None
    m = re.fullmatch(r"bytes=(\d+)-(\d+)", str(range_header).strip())
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if end < start:
        return None
    return end - start + 1


def _auth_digest(headers: Optional[dict]) -> str:
    """鉴权头摘要（Authorization Bearer <redacted> 的 sha256 前 16 字符）。

    招1/招4 与 singleflight 共用：不同凭据绝不共享块/合并请求，防串流。
    """
    auth = ""
    for k, v in (headers or {}).items():
        if str(k).lower() == "authorization":
            auth = str(v or "")
            break
    return hashlib.sha256(
        auth.encode("utf-8", "ignore")).hexdigest()[:16] if auth else ""


def _sf_key(url: str, range_header: str, headers: Optional[dict]) -> tuple:
    """合并键：URL + Range + 鉴权头摘要（不同凭据绝不合并，防串流）。"""
    return (url, range_header, _auth_digest(headers))


async def _sf_collect(url, request, headers, media_type, cache_control,
                      drain_cap):
    """Singleflight 领队：走原取流实现并把正文收齐（只对小 Range 调用）。

    返回 (status, headers, media_type, body)；正文超过 drain_cap（多半是
    上游无视 Range 回了整文件）返回 None，调用方回落独立取流。
    异常（配额 403/源站 502 等）照常向上抛，由所有等待者共享。
    """
    resp = await _serve_remote_async_impl(
        url, request, headers, media_type, cache_control=cache_control,
        pt=None)
    body = bytearray()
    try:
        async for chunk in resp.body_iterator:
            body += chunk
            if len(body) > drain_cap:
                return None
    finally:
        aclose = getattr(resp.body_iterator, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
    hdrs = {k: v for k, v in resp.headers.items()
            if k.lower() != "content-length"}
    return (resp.status_code, hdrs, resp.media_type, bytes(body))


# ---------------------------------------------------------------------------
# 招1「分块共享」+ 招4「请求捏合整形」（2026-10-16 用户定版：几百人同时播
# **不同**资源时，把通往谷歌的上行请求压少、压平；零采购、纯中继层）
#
# 背景：#942 singleflight 只合并「字节级完全相同」的 Range——不同会话拖
# 不同进度、要不同区间时命中率≈0；且服务端原样透传播放器 Range，块大小
# 完全由播放器说了算，上行没有任何业务级并发上限/整形。
#
# 招1：有界单段 Range 先按固定块（默认 4MiB）对齐分解；同一块只向谷歌取
# 一次（领队），并发等待者共享字节，取过的块进进程内 LRU 内存缓存（默认
# 上限约 2GiB，块 0＝文件头优先保留）。文件头/尾块是每次起播、探测、seek
# 预览的公共部分，跨会话复用率最高，是本层的主要收益来源。
# 招4：领队取块要过两道闸——全局上行并发门（默认 64）与单 SA 并发门
# （默认 8，以鉴权摘要代 SA 身份：token 按 SA 缓存，摘要≈SA）；取块启动
# 加随机抖动（默认 0–50ms），把突发请求捏成平稳细流。闸超时不堵播放：
# 整请求回落原路径（连接池 200 仍是总上限）。
#
# 安全与语义红线：
# - 块键含鉴权摘要（与 singleflight 共用 _auth_digest），不同凭据绝不共享。
# - 配额负缓存命中的文件不进本层，仍由 impl 走本地 403 的既有口径。
# - 本层任何异常/不合资格一律回落原 serve 流程，绝不因新层让播放失败；
#   总开关 PLAYBACK_CHUNK_SHARE_ENABLED=0 时行为与升级前一致。
# - v1 只覆盖有界单段 Range；开放区间（bytes=X-）与多段仍走原路径
#   （原路径有 #942 合并与连接池上限兜底）。
_CS_COUNTERS: dict[str, int] = {
    "hit": 0, "miss": 0, "join": 0, "fetch": 0, "fetch_error": 0,
    "gate_timeout": 0, "fallback": 0, "bytes_from_cache": 0,
}
_CS_CACHE: dict = {}          # key -> (bytes, total)（插入序即 LRU 序）
_CS_CACHE_BYTES = 0
_CS_CACHE_LOCK = threading.Lock()
_CS_INFLIGHT: dict = {}       # key -> asyncio.Task
_CS_GATES: dict = {}          # loop id -> asyncio.Semaphore（全局上行门）
_CS_SA_GATES: dict = {}       # (loop id, auth digest) -> asyncio.Semaphore


def _cs_enabled() -> bool:
    return _env_flag("PLAYBACK_CHUNK_SHARE_ENABLED", True)


def _cs_block_size() -> int:
    return int(_env_number(
        "PLAYBACK_CHUNK_SIZE_BYTES", 4 * 1024 * 1024,
        256 * 1024, 32 * 1024 * 1024))


def _cs_max_range_bytes() -> int:
    return int(_env_number(
        "PLAYBACK_CHUNK_MAX_RANGE_BYTES", 32 * 1024 * 1024,
        0, 256 * 1024 * 1024))


def _cs_cache_cap_bytes() -> int:
    return int(_env_number(
        "PLAYBACK_CHUNK_CACHE_MB", 2048, 0, 16384)) * 1024 * 1024


def chunk_share_counters() -> dict:
    """招1/招4 计数快照（进程内；供健康/管理端读取）。"""
    with _CS_CACHE_LOCK:
        entries, nbytes = len(_CS_CACHE), _CS_CACHE_BYTES
    out = dict(_CS_COUNTERS)
    out["cache_entries"] = entries
    out["cache_bytes"] = nbytes
    out["inflight"] = len(_CS_INFLIGHT)
    return out


def _cs_cache_get(key):
    """命中返回 (body, total_or_None) 并把条目移到队尾（最近使用）。"""
    with _CS_CACHE_LOCK:
        entry = _CS_CACHE.pop(key, None)
        if entry is not None:
            _CS_CACHE[key] = entry
        return entry


def _cs_cache_put(key, body: bytes, total=None) -> None:
    global _CS_CACHE_BYTES
    cap = _cs_cache_cap_bytes()
    if cap <= 0 or len(body) > cap:
        return
    with _CS_CACHE_LOCK:
        old = _CS_CACHE.pop(key, None)
        if old is not None:
            _CS_CACHE_BYTES -= len(old[0])
        # 先赶最久未用的非 0 号块；只剩 0 号块时才动 0 号块（硬上限优先）
        while _CS_CACHE_BYTES + len(body) > cap and _CS_CACHE:
            victim = next((k for k in _CS_CACHE if k[1] != 0), None)
            if victim is None:
                victim = next(iter(_CS_CACHE))
            _CS_CACHE_BYTES -= len(_CS_CACHE.pop(victim)[0])
        _CS_CACHE[key] = (body, total)
        _CS_CACHE_BYTES += len(body)


def _cs_gate(sem_map: dict, map_key, limit: int) -> asyncio.Semaphore:
    sem = sem_map.get(map_key)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        sem_map[map_key] = sem
    return sem


class _RangeShim:
    """只带 Range 头的假请求：impl 对 request 的唯一读取是 headers.range。"""
    __slots__ = ("headers", "method")

    def __init__(self, range_header: str):
        self.headers = {"range": range_header}
        self.method = "GET"


async def _cs_fetch_block(url, headers, media_type, cache_control,
                          key, bstart: int, bend: int):
    """领队取一块：过闸（全局+单 SA）→ 抖动 → 复用 _sf_collect 收齐正文。

    返回 (body, total_or_None, first_headers)；失败抛异常/返回 None，
    由调用方整请求回落原路径。
    """
    digest = key[2]
    loop_id = id(asyncio.get_running_loop())
    timeout = float(_env_number(
        "PLAYBACK_UPSTREAM_GATE_TIMEOUT_SEC", 10.0, 0.5, 120.0))
    gmax = int(_env_number(
        "PLAYBACK_UPSTREAM_MAX_CONCURRENCY", 64, 0, 1000))
    smax = int(_env_number(
        "PLAYBACK_UPSTREAM_PER_SA_CONCURRENCY", 8, 0, 1000))
    acquired = []
    try:
        if gmax > 0:
            sem = _cs_gate(_CS_GATES, loop_id, gmax)
            await asyncio.wait_for(sem.acquire(), timeout)
            acquired.append(sem)
        if smax > 0 and digest:
            sem = _cs_gate(_CS_SA_GATES, (loop_id, digest), smax)
            await asyncio.wait_for(sem.acquire(), timeout)
            acquired.append(sem)
    except asyncio.TimeoutError:
        for sem in acquired:
            sem.release()
        _CS_COUNTERS["gate_timeout"] += 1
        return None
    try:
        jitter_ms = float(_env_number(
            "PLAYBACK_UPSTREAM_JITTER_MS", 50, 0, 1000))
        if jitter_ms > 0:
            await asyncio.sleep(random.uniform(0, jitter_ms) / 1000.0)
        _CS_COUNTERS["fetch"] += 1
        outcome = await _sf_collect(
            url, _RangeShim(f"bytes={bstart}-{bend}"), headers,
            media_type, cache_control, _cs_block_size() + 65536)
        if outcome is None:
            return None
        status, hdrs, _ctype, body = outcome
        if status == 200:
            # 上游无视 Range 回了整文件：仅当正文就是一块以内才可用，
            # 按整文件切片出本块，并顺手把其余对齐块也入缓存（免费的午餐）
            if bstart > 0 and len(body) <= bstart:
                return None
            total = len(body)
            piece = body[bstart:bend + 1]
            bs = _cs_block_size()
            pos = 0
            while pos < total:
                _cs_cache_put((url, pos // bs, digest),
                              body[pos:pos + bs], total)
                pos += bs
            return (piece, total, hdrs)
        if status != 206:
            return None
        total = None
        crange = ""
        for k, v in hdrs.items():
            if k.lower() == "content-range":
                crange = str(v)
                break
        m = re.search(r"/(\d+)\s*$", crange)
        if m:
            total = int(m.group(1))
        return (body, total, hdrs)
    finally:
        for sem in acquired:
            sem.release()


async def _cs_get_block(url, headers, media_type, cache_control,
                        key, bstart: int, bend: int):
    """取一块（永远取整块 [bstart, bend]，不按调用方的子区间截断取，

    否则缓存里会留下半块，后来者拿到短块误判文件结尾）。
    缓存命中→共享在飞→领队取（同 _SF_INFLIGHT 的骨架）。
    """
    cached = _cs_cache_get(key)
    if cached is not None:
        _CS_COUNTERS["hit"] += 1
        _CS_COUNTERS["bytes_from_cache"] += len(cached[0])
        return (cached[0], cached[1], None)
    task = _CS_INFLIGHT.get(key)
    if task is None:
        _CS_COUNTERS["miss"] += 1
        task = asyncio.ensure_future(_cs_fetch_block(
            url, headers, media_type, cache_control, key, bstart, bend))
        _CS_INFLIGHT[key] = task
        task.add_done_callback(lambda _t, k=key: _CS_INFLIGHT.pop(k, None))
    else:
        _CS_COUNTERS["join"] += 1
    try:
        outcome = await asyncio.shield(task)
    except Exception:
        _CS_COUNTERS["fetch_error"] += 1
        return None
    if outcome is None:
        return None
    body, total, hdrs = outcome
    if _cs_cache_get(key) is None:
        _cs_cache_put(key, body, total)
    return (body, total, hdrs)


async def _cs_try_serve(url, request, headers, media_type, cache_control,
                        pt):
    """招1/招4 入口：合资格返回组装好的 206 Response，否则 None（回落）。"""
    if not _cs_enabled():
        return None
    if getattr(request, "method", "GET") != "GET":
        return None
    if request.headers.get("if-range") or request.headers.get("if-match"):
        return None
    rlen = _sf_range_length(request.headers.get("range"))
    max_range = _cs_max_range_bytes()
    if rlen is None or max_range <= 0 or rlen > max_range:
        return None
    # 只做 Drive 文件：转 API 形态后能提出 file_id 才合资格
    try:
        from backend.emby_server.drive_auth import drive_api_media_url
        fid = _drive_alt_media_file_id(drive_api_media_url(url))
    except Exception:
        fid = None
    if not fid:
        return None
    # 配额负缓存命中：不进本层，留给 impl 的本地 403 既有口径
    if drive_quota_blocked_retry_after(fid):
        return None
    m = re.fullmatch(r"bytes=(\d+)-(\d+)",
                     str(request.headers.get("range")).strip())
    start, end = int(m.group(1)), int(m.group(2))
    bs = _cs_block_size()
    digest = _auth_digest(headers)
    first_idx, last_idx = start // bs, end // bs
    parts = []
    total = None
    resp_hdrs = None
    for idx in range(first_idx, last_idx + 1):
        bstart, bend_full = idx * bs, (idx + 1) * bs - 1
        got = await _cs_get_block(
            url, headers, media_type, cache_control,
            (url, idx, digest), bstart, bend_full)
        if got is None:
            _CS_COUNTERS["fallback"] += 1
            return None
        body, btotal, hdrs = got
        if btotal is not None:
            total = btotal
        if resp_hdrs is None and hdrs is not None:
            resp_hdrs = hdrs
        # 本块要覆盖到客户端区间在本块内的末端；短了只有一种合法情形：
        # 这就是文件的真尾巴（total 已知且块内实际字节正好接到 total）
        want = min(bend_full, end) - bstart + 1
        if len(body) < want and not (
                total is not None and bstart + len(body) >= total):
            _CS_COUNTERS["fallback"] += 1
            return None
        parts.append(body[:want])
    data = b"".join(parts)
    off = start - first_idx * bs
    data = data[off:off + rlen]
    if len(data) < rlen:
        end = start + len(data) - 1  # 与上游截断口径一致：实际给多少算多少
    hdrs = {k: v for k, v in (resp_hdrs or {}).items()
            if k.lower() not in ("content-length", "content-range")}
    hdrs["Content-Range"] = f"bytes {start}-{end}/{total if total else '*'}"
    hdrs["Content-Length"] = str(len(data))
    if pt is not None:
        pt.done()
    return Response(content=data, status_code=206, headers=hdrs,
                    media_type=media_type)


async def serve_remote_async(
    url: str,
    request: Request,
    headers: Optional[dict] = None,
    media_type: str = "video/mp4",
    cache_control: Optional[str] = None,
    pt=None,
) -> StreamingResponse:
    """远程媒体代理（异步版）：完整语义见 ``_serve_remote_async_impl``。

    本包装层只多一件事：同文件同 Range 的并发小请求做 Singleflight 合并
    （见上方注释）；不合资格的请求原样直达实现、行为零变化。
    """
    # 招1/招4 分块共享层：合资格（Drive 文件 + 有界单段 Range）先走块层，
    # 任何失败/不合资格返回 None，原样落回下面的 singleflight/原路径。
    try:
        _cs_resp = await _cs_try_serve(
            url, request, headers, media_type, cache_control, pt)
    except Exception:
        _cs_resp = None
    if _cs_resp is not None:
        return _cs_resp
    _key = None
    _drain_cap = 0
    try:
        _max = _sf_max_range_bytes()
        _rh = request.headers.get("range")
        _rlen = _sf_range_length(_rh) if _max > 0 else None
        if (_rlen is not None and _rlen <= _max
                and getattr(request, "method", "GET") == "GET"):
            _key = _sf_key(url, _rh, headers)
            _drain_cap = _max + 65536
    except Exception:
        _key = None
    if _key is None:
        return await _serve_remote_async_impl(
            url, request, headers, media_type,
            cache_control=cache_control, pt=pt)

    task = _SF_INFLIGHT.get(_key)
    if task is None:
        _SF_COUNTERS["leader"] += 1
        task = asyncio.ensure_future(_sf_collect(
            url, request, headers, media_type, cache_control, _drain_cap))
        _SF_INFLIGHT[_key] = task
        task.add_done_callback(lambda _t, k=_key: _SF_INFLIGHT.pop(k, None))
    else:
        _SF_COUNTERS["joined"] += 1
        if pt is not None:
            pt.mark("singleflight_join")
    try:
        outcome = await asyncio.shield(task)
    except HTTPException:
        if pt is not None:
            pt.done()
        raise
    except Exception:
        outcome = None
    if outcome is None:
        # 领队正文超限/异常：回落各自独立取流（语义与未合并一致）
        _SF_COUNTERS["fallback"] += 1
        return await _serve_remote_async_impl(
            url, request, headers, media_type,
            cache_control=cache_control, pt=pt)
    _status, _hdrs, _ctype, _body = outcome
    if pt is not None:
        pt.done()
    return Response(content=_body, status_code=_status,
                    headers=_hdrs, media_type=_ctype)


async def _serve_remote_async_impl(
    url: str,
    request: Request,
    headers: Optional[dict] = None,
    media_type: str = "video/mp4",
    cache_control: Optional[str] = None,
    pt=None,
) -> StreamingResponse:
    """远程媒体代理（异步版）：语义与 ``serve_remote`` 完全一致，但不在事件循环上等源站

    播放路径上的每次 Range 请求都要等「连上源站 + 源站回首字节」，同步客户端会把这等待
    变成整个进程的暂停（一个用户拖进度条，其他人全部卡住）。异步版的等待只挂起当前请求，
    Range / 状态码 / 透传头的口径与同步版逐项对齐，且凭据同样不下发。

    ``cache_control``（CDN 预留，第 2/3 层）：分片口径时随响应下发，让 CDN 边缘
    能缓存回源结果；None 时不发缓存头（行为与升级前一致）。凭据在查询串里的
    API 响应不走这里，302 分支由调用方自带 no-store。

    ``pt``（可选）：播放链路计时器（``backend.emby_server.play_timing``）。
    传入时记录 url_convert / ttfb 并在返回前 done()。
    只加日志，不改逻辑。
    """
    import httpx

    forward = {k: v for k, v in (headers or {}).items()}
    # X-Pickcode is internal (for 115 cache invalidation), do not forward to origin
    forward.pop("X-Pickcode", None)
    forward.setdefault("User-Agent", REMOTE_UA)
    range_header = request.headers.get("range")
    if range_header:
        forward["Range"] = range_header

    # 与同步版同一套口径：重定向自己追，逐跳校验目标、跨主机剥凭据（见 serve_remote）
    # 共享 client（连接复用）；**响应**仍然每次独立，用完在 iter_remote 里关。
    client = get_relay_client()
    # 兜底异常分支（PoolTimeout / 其它异常）里要关掉已经拿到的流式响应，
    # 否则这条连接永远不会归还给进程共享的连接池，反复命中会耗尽 RELAY_MAX_CONNECTIONS。
    resp = None
    try:
        # _pre_convert_url：drive_auth_headers 建 SA 亲和用的 key（转换前的原始 URL）；
        # 403 换 SA 重试时要打破的正是这个 key 的亲和。
        _pre_convert_url = url
        # Drive 私有化修复（2026-10-10）：同同步版，uc 直链转 API alt=media。
        try:
            from backend.emby_server.drive_auth import drive_api_media_url
            url = drive_api_media_url(url)
        except Exception:
            pass
        if pt is not None:
            pt.mark("url_convert")
        # api_url：重连建连的起点（drive_api_media_url 已转好，幂等，可复用）
        api_url = url
        # 单文件配额负缓存：该文件近期已确认配额超限时本地直接回 403，
        # 不再拿上游请求撞墙（播放器重试风暴的主要放大点）。
        _quota_fid = _drive_alt_media_file_id(api_url)
        if _quota_fid:
            _retry_after = drive_quota_blocked_retry_after(_quota_fid)
            if _retry_after:
                logger.warning(
                    "Drive 单文件配额负缓存命中 file_id=%s，本地拒绝（约 %ds 后可重试）",
                    _quota_fid, _retry_after)
                raise HTTPException(
                    status_code=403,
                    detail="该文件今日下载次数已达网盘上限，请稍后再试",
                    headers={"Retry-After": str(_retry_after)})
        # 跳转跟随走共用 helper（P3-5：与 resolve_drive_final_url 同逻辑，
        # 逐跳内网校验 + 跨主机剥凭据）；终态 resp 不关，由下面消费/关闭。
        # 注意：_follow_redirects 只 rebind 自己的 forward 形参，调用方的 forward
        # 保持"剥凭据之前"的原始头——重连建连直接复用它（与初始请求一致）。
        resp, current, _redirect_hops = await _follow_redirects(
            client, api_url, forward, silent=False)
        # Drive alt=media 403：记 Google 侧 reason，可重试类换 SA 重试（2026-10-15 实测：
        # 403 为 SA 级瞬时限流，换 SA 即恢复；同 URL 亲和 90-120s 会让客户端重试撞同一 SA）
        if resp is not None and resp.status_code == 403 and _drive_alt_media_file_id(current):
            resp, current = await _drive_403_retry_async(
                client, api_url, forward, resp, current, affinity_key=_pre_convert_url)
        # ttfb：_follow_redirects 返回时已收到源站响应头（首字节就绪）。
        if pt is not None:
            pt.mark("ttfb")
        # 115 直链过期检测：403/404 时清缓存，客户端重试将拿到新直链（2026-10-09）
        if resp.status_code in (403, 404, 410):
            _pickcode = (headers or {}).get("X-Pickcode")
            if _pickcode:
                try:
                    from backend.emby_server.mounts import _invalidate_115_url
                    _invalidate_115_url(_pickcode)
                    logger.info("115 direct URL expired, cache cleared")
                except Exception:
                    pass
        # Drive 源站 401 检测：SA Bearer token 已失效（密钥轮换/提前吊销），
        # 按 token 值失效内外两层缓存，下次播放重新换取；否则坏 token 在缓存里
        # 毒化 55 分钟，期间命中该 SA 的请求全部 401（2026-10-11）。
        # 与同步版 serve_remote 同口径
        if resp.status_code == 401:
            try:
                _auth_value = ""
                for _k, _v in (forward or {}).items():
                    if str(_k).lower() == "authorization":
                        _auth_value = _v or ""
                        break
                from backend.emby_server import drive_auth
                drive_auth.note_drive_unauthorized(current, _auth_value)
            except Exception:
                pass
    except HTTPException:
        raise
    except httpx.PoolTimeout as exc:
        # S2：上游连接池满（并发远程播放超过 RELAY_MAX_CONNECTIONS）：快速 503，
        # 让播放器稍后重试，而不是白等 30 秒再 502
        # 先把已打开的流式响应关掉归还共享池（aclose 幂等，多调无副作用，漏调才泄漏）
        if resp is not None:
            await resp.aclose()
        logger.warning("远程代理连接池已满（上限 %s），拒绝 %s", _RELAY_MAX_CONNECTIONS,
                       url.split("?")[0])
        raise HTTPException(status_code=503, detail="中转繁忙，请稍后重试",
                            headers={"Retry-After": "2"}) from exc
    except Exception as exc:  # noqa: BLE001 — 源站不可达：给出干净的 502，而非 500 堆栈
        # 同上：畸形 Location（如 http://[::1）会让 _next_redirect 抛非 MountError 的
        # ValueError，落到这里时 resp 可能已经打开，不关就会慢泄漏共享连接池
        if resp is not None:
            await resp.aclose()
        logger.warning("远程媒体代理失败 %s: %s", url.split("?")[0], exc)
        raise HTTPException(status_code=502, detail="源站不可达") from exc

    if resp.status_code >= 400:
        status = resp.status_code
        # 块3 播放兜底：只认 Drive alt=media 端点的 404
        # （= 文件不存在：删除/移出共享/进回收站不可下载，语义明确）。
        # 403 不认：可能是 SA 权限/限流/共享设置变更，可能是暂时的，标记会误伤。
        # fire-and-forget 标记 source_invalid_at（热路径零阻塞），对客户端抛 410。
        if status == 404:
            fid = _drive_alt_media_file_id(current)
            if fid:
                note_drive_file_gone(fid)
                await resp.aclose()
                logger.warning("远程媒体 Drive 文件已删除 file_id=%s，返回 410", fid)
                raise HTTPException(
                    status_code=410,
                    detail="该文件已在网盘中删除或移出共享，无法播放")
        await resp.aclose()
        logger.warning("远程媒体源站返回 %s: %s", status, url.split("?")[0])
        raise HTTPException(status_code=502 if status >= 500 else status, detail=f"源站返回 {status}")

    passthrough = {}
    for name in ("content-range", "accept-ranges", "content-length", "last-modified", "etag"):
        value = resp.headers.get(name)
        if value:
            passthrough[name.title()] = value
    # 与本地路径同口径：远程源支持 Range，上游没给也要补（理由见同步版）
    passthrough.setdefault("Accept-Ranges", "bytes")
    if cache_control and "Cache-Control" not in passthrough:
        passthrough["Cache-Control"] = cache_control
    passthrough["X-Accel-Buffering"] = "no"
    # 边缘缓存调试头（通用能力）：EDGE_CACHE_MODE=query_ignore 时声明，
    # 管理员看响应头就能确认服务端模式；off/header 模式不加（行为与今天一致）。
    try:
        from backend.emby_server import edge_cache
        _edge_dbg = edge_cache.debug_header_value()
        if _edge_dbg:
            passthrough[edge_cache.DEBUG_HEADER] = _edge_dbg
    except Exception:
        pass
    content_type = resp.headers.get("content-type") or media_type

    # 慢连接自动重连状态：只对可续传上游启用；客户端只看到一条连续响应流，
    # 状态码/响应头保持初始建连时的值不变。
    _stall_state = _RelayStallState(
        _resume_offset(resp, range_header),
        log_ctx=url.split("?")[0],
        enabled=_RELAY_STALL_RECONNECT and _upstream_resumable(resp),
    )

    async def iter_remote():
        cur_resp = resp
        ait = None
        try:
            while True:
                if ait is None:
                    ait = cur_resp.aiter_bytes(READ_CHUNK).__aiter__()
                # 只统计"等上游出数据"的时间：yield 处被慢客户端阻塞的时间不计入，
                # 客户端下行慢不会误触发重连。
                t0 = time.monotonic()
                try:
                    chunk = await ait.__anext__()
                except StopAsyncIteration:
                    break
                wait = time.monotonic() - t0
                yield chunk
                _stall_state.on_chunk(len(chunk), wait)
                if _stall_state.want_reconnect():
                    ok, payload = await _try_reconnect_upstream(
                        client, api_url, forward, _stall_state)
                    if ok:
                        new_resp, new_ait = payload
                        old_resp, old_ait = cur_resp, ait
                        cur_resp, ait = new_resp, new_ait
                        # make-before-break：新连接已确认可用，才断旧连；
                        # 先关旧迭代器（避免未关闭的 async generator 告警），再关旧响应。
                        try:
                            await old_ait.aclose()
                        except Exception:
                            pass
                        try:
                            await old_resp.aclose()
                        except Exception:
                            pass
                        _stall_state.note_success()
                        # 首块=上一块（已给过客户端），确认用、此处不再下发
                        continue
                    reason = payload
                    logger.info("[relay-reconnect] 本次重连未生效，继续旧连接 %s：%s",
                                _stall_state.log_ctx, reason)
        except httpx.TransportError as exc:
            # S2：源站卡住（块间空闲超过 RELAY_READ_TIMEOUT）或中途断开：结束这条流、
            # 归还连接。响应头已发出，只能截断；播放器会对剩余区间重新发 Range。
            logger.warning("远程代理流中断 %s: %s", url.split("?")[0], type(exc).__name__)
        finally:
            # 只关响应，不关 client —— client 是进程共享的，关了会让其它并发请求炸掉。
            # 连接会在响应关闭后自动归还池里，供下一次 Range 请求复用。
            try:
                await cur_resp.aclose()
            except Exception:
                pass

    # 播放链路耗时汇总（[play-timing]）：ttfb 已在上面 mark，这里输出整行。
    if pt is not None:
        pt.done()
    return StreamingResponse(
        iter_remote(), status_code=resp.status_code, media_type=content_type, headers=passthrough,
    )


async def resolve_drive_final_url(
    url: str,
    headers: dict | None = None,
) -> str | None:
    """解析 Drive 最终下载地址（预热用），走和 serve_remote_async 完全相同的路径。

    原始 URL → drive_api_media_url 转 API 地址 → 查 P0-2 跳转缓存 →
    未命中则手动跟随跳转 → 写缓存。

    返回最终 URL；任何失败返回 None（不抛异常，预热场景静默）。
    用 streaming 共享客户端（连接复用）；中间响应及时关闭归还连接池。
    """
    try:
        # 1. uc 直链转 Drive API 地址（和 serve_remote_async 同口径）
        try:
            from backend.emby_server.drive_auth import drive_api_media_url
            api_url = drive_api_media_url(url)
        except Exception:
            api_url = url
        if not api_url:
            return None

        # 2. 手动跟随跳转（走共用 helper，和 serve_remote_async 同逻辑）
        client = get_relay_client()
        if asyncio.iscoroutine(client):
            client = await client

        forward = {k: v for k, v in (headers or {}).items()}
        forward.pop("X-Pickcode", None)
        # 与 serve_remote_async 同口径：带默认 UA（P3-5 漂移修复）
        forward.setdefault("User-Agent", REMOTE_UA)

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
                    logger.warning("Drive 预热 403 file_id=%s reason=%s msg=%.120s",
                                   _drive_alt_media_file_id(current),
                                   _preason or "-", _pmsg or "-")
                return None
            return current
        finally:
            await resp.aclose()
    except Exception:
        # 预热静默：任何异常都不抛
        return None


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


"""自建 Emby 服务器：流媒体服务（本机文件直连流 / Range）

转码与远程挂载中继已删除（2026-10-17 用户拍板）：网盘条目一律 302 直链、
播放器自拉；本模块只剩本机文件的流式投递与图片投递。
"""
from __future__ import annotations

import logging
import os
import re
import time
from email.utils import formatdate
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

# 开箱即用播放优化：单文件并发 Range 限流（只此一处实现）
from backend.emby_server.playback_tune import RANGE_LIMITER

logger = logging.getLogger(__name__)

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


def _range_header(start: int, end: int, total: int) -> dict:
    return {
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }


def _validators_header(path: str) -> dict:
    """给 206 分片响应补 ETag/Last-Modified"""
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

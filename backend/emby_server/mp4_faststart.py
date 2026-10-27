# -*- coding: utf-8 -*-
"""新入库大 MP4 的 moov 前置（faststart）。

对准「直链已经通、首帧仍然晚」：moov 在文件尾时，播放器必须先向网盘
Range 到尾部才能出画。这里在扫描提交之后异步把大 MP4 重写成 moov 在头
（ffmpeg -c copy -movflags +faststart），不进写库事务，不挡入库。

范围：
- 默认关（``MP4_FASTSTART_ON_INGEST=0``）。打开后只处理扫描判定为新条目的
  mp4/m4v/mov，不回头扫全库。
- 存量要另开 ``MP4_FASTSTART_BACKFILL=1`` 再跑
  ``scripts/mp4_faststart_backfill.py --apply``。两个条件缺一不可，
  脚本默认只统计、不改文件。
- 小于阈值且 moov 不是 back 的跳过；moov 已在头的跳过；mkv 不处理。

失败：ffmpeg / 下载 / 上传任一失败都留下原文件，记日志，入库结果不动。
直链播放路径不在这里改，也不按播放器分支。
"""
from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

# ``*_ENV = "NAME"`` 才是 env.example 防漂移扫描认的读取证据
# （见 tests/test_perfection_r44_envdoc.py）。前缀写成 ENV_* 会被当成死键。
#: 新入库处理开关。默认关。
ON_INGEST_ENV = "MP4_FASTSTART_ON_INGEST"
#: 存量脚本开关。默认关。脚本还要再加 --apply 才会改文件。
BACKFILL_ENV = "MP4_FASTSTART_BACKFILL"
#: 大文件阈值（字节）。默认 2GiB。moov 已在尾时不受此阈值限制。
MIN_BYTES_ENV = "MP4_FASTSTART_MIN_BYTES"
#: ffmpeg 重写超时（秒）。大文件拷贝封装可能要很久。
TIMEOUT_SEC_ENV = "MP4_FASTSTART_TIMEOUT_SEC"
DEFAULT_MIN_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 7200

MP4_CONTAINERS = frozenset({"mp4", "m4v", "mov"})
_PEEK_BYTES = 256 * 1024
_UPLOAD_CHUNK = 8 * 1024 * 1024
_DISK_MARGIN = 64 * 1024 * 1024

_FALSE = {"0", "false", "no", "off", ""}

_queue: deque = deque()
_lock = threading.Lock()
_wake = threading.Event()
_worker_started = False


def _flag(name: str, default: str = "0") -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        raw = default
    return str(raw).strip().lower() not in _FALSE


def ingest_enabled() -> bool:
    """新入库 faststart 是否打开。默认关。"""
    return _flag(ON_INGEST_ENV, "0")


def backfill_enabled() -> bool:
    """存量脚本是否允许进入「可以改文件」的分支。默认关。"""
    return _flag(BACKFILL_ENV, "0")


def min_bytes() -> int:
    from backend.emby_server.env_util import env_int

    return env_int(MIN_BYTES_ENV, DEFAULT_MIN_BYTES, lo=1)


def timeout_sec() -> int:
    from backend.emby_server.env_util import env_int

    return env_int(TIMEOUT_SEC_ENV, DEFAULT_TIMEOUT_SEC, lo=30, hi=24 * 3600)


def ffmpeg_bin() -> str:
    return (os.getenv("EMBY_FFMPEG_PATH") or "ffmpeg").strip() or "ffmpeg"


def backfill_will_rewrite(*, apply: bool) -> tuple[bool, str]:
    """存量会不会真的改文件。关着或没 --apply 都不会。"""
    if not backfill_enabled():
        return False, "backfill_disabled"
    if not apply:
        return False, "dry_run"
    return True, "apply"


def container_name(container: str = "", path: str = "") -> str:
    name = (container or "").strip().lower().lstrip(".")
    if not name and path:
        bare = path.split("?", 1)[0].split("#", 1)[0]
        name = os.path.splitext(bare)[1].lstrip(".").lower()
    return name


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def eligible(*, container: str = "", size=0, moov_position: str | None = None,
             path: str = "") -> tuple[bool, str]:
    """要不要做 faststart。返回 (是否处理, 原因)。

    - 非 mp4/m4v/mov：跳过（mkv 无 moov）。
    - moov 已在头：跳过。
    - moov 在尾：处理（不受大小阈值限制，这是「或探测到 moov 在尾」）。
    - 没探测过但大小达到阈值：处理。
    - 其余小文件：跳过。
    """
    if container_name(container, path) not in MP4_CONTAINERS:
        return False, "not_mp4"
    moov = (moov_position or "").strip().lower()
    if moov == "front":
        return False, "already_front"
    if moov == "back":
        return True, "moov_back"
    if _as_int(size) >= min_bytes():
        return True, "large"
    return False, "below_threshold"


def note_new_ingest(*, item_id, file_path: str = "", size=0, container: str = "",
                    moov_position: str | None = None,
                    drive_file_id: str = "") -> bool:
    """扫描提交之后调用。开关关或不合格时立刻返回，不碰文件。

    合格则丢进内存队列，后台单线程处理。返回 True 表示已入队。
    """
    if not ingest_enabled():
        return False
    if not item_id:
        return False
    ok, reason = eligible(
        container=container, size=size, moov_position=moov_position, path=file_path)
    if not ok:
        if reason != "not_mp4":
            logger.info(
                "[mp4-faststart] skip item=%s reason=%s size=%s moov=%s",
                item_id, reason, _as_int(size), moov_position or "-")
        return False
    _enqueue({
        "item_id": item_id,
        "file_path": file_path or "",
        "size": _as_int(size),
        "container": container_name(container, file_path),
        "moov_position": (moov_position or "") or None,
        "drive_file_id": (drive_file_id or "").strip(),
        "reason": reason,
    })
    logger.warning(
        "[mp4-faststart] queued item=%s reason=%s size=%s container=%s",
        item_id, reason, _as_int(size), container_name(container, file_path))
    return True


def _enqueue(job: dict) -> None:
    global _worker_started
    with _lock:
        _queue.append(job)
        if not _worker_started:
            _worker_started = True
            threading.Thread(
                target=_worker, name="mp4-faststart", daemon=True).start()
    _wake.set()


def _worker() -> None:
    while True:
        _wake.wait(30)
        _wake.clear()
        while True:
            with _lock:
                if not _queue:
                    break
                job = _queue.popleft()
            try:
                process_job(job)
            except Exception:  # noqa: BLE001 — 一条失败不能停掉队列
                logger.warning(
                    "[mp4-faststart] kept original item=%s reason=worker_error",
                    job.get("item_id"), exc_info=True)


def _local_video_path(path: str, container: str) -> bool:
    """本机上的 mp4 本体。``.strm`` 纸条不能当视频重写。"""
    if not path or path.startswith("mount://") or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext == "strm":
        return False
    return ext in MP4_CONTAINERS or (container or "") in MP4_CONTAINERS and ext == ""


def process_job(job: dict, *, rewrite_local_fn=None, rewrite_drive_fn=None,
                mark_fn=None) -> dict:
    """处理一条。任何失败都返回 ok=False，不抛到扫描线程。"""
    item_id = job.get("item_id")
    path = job.get("file_path") or ""
    fid = (job.get("drive_file_id") or "").strip()
    container = job.get("container") or ""
    try:
        if _local_video_path(path, container):
            result = (rewrite_local_fn or rewrite_local)(path)
        elif fid:
            result = (rewrite_drive_fn or rewrite_drive_file)(
                fid, size_hint=_as_int(job.get("size")))
        else:
            result = {"ok": False, "reason": "unsupported_location"}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[mp4-faststart] kept original item=%s reason=%s",
            item_id, type(exc).__name__)
        return {"ok": False, "reason": type(exc).__name__}

    reason = result.get("reason")
    if result.get("ok") and reason in ("rewritten", "already_front"):
        try:
            (mark_fn or mark_moov_front)(item_id, result.get("size"))
        except Exception:  # noqa: BLE001 — 文件已是 faststart 时记库失败只打日志
            logger.warning(
                "[mp4-faststart] moov mark failed item=%s", item_id, exc_info=True)
        if reason == "rewritten":
            logger.warning(
                "[mp4-faststart] rewritten item=%s size=%s",
                item_id, result.get("size"))
        return result
    logger.warning(
        "[mp4-faststart] kept original item=%s reason=%s", item_id, reason)
    return result


def mark_moov_front(item_id, size=None) -> None:
    """把条目记成 moov 在头。记不上不影响已经改好的文件。"""
    if not item_id:
        return
    from backend.database import SessionLocal
    from backend.emby_server import models as em

    session = SessionLocal()
    try:
        item = session.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        if item is None:
            return
        item.moov_position = "front"
        if size:
            item.size = int(size)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def detect_moov_bytes(blob: bytes, file_size: int = 0) -> str:
    """从文件头的盒头判断 moov 在前还是 mdat 在前。读不到结论返回 unknown。"""
    file_size = file_size or len(blob)
    offset = 0
    limit = len(blob)
    while offset + 8 <= limit:
        size = struct.unpack(">I", blob[offset:offset + 4])[0]
        typ = blob[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > limit:
                return "unknown"
            size = struct.unpack(">Q", blob[offset + 8:offset + 16])[0]
            header = 16
        elif size == 0:
            size = file_size - offset if file_size > offset else 0
        if size < header:
            return "unknown"
        if typ == b"moov":
            return "front"
        if typ == b"mdat":
            return "back"
        if offset + header > limit:
            return "unknown"
        # 盒身超出本次读到的窗口：窗口里还没见到 moov/mdat。
        if offset + size > limit:
            return "unknown"
        offset += size
    return "unknown"


def detect_moov_position(path: str) -> str:
    """对本地文件做同样的判断。只 seek 盒头，不把 mdat 读进内存。"""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return "unknown"
    if file_size < 8:
        return "unknown"
    offset = 0
    try:
        with open(path, "rb") as fh:
            while offset + 8 <= file_size:
                fh.seek(offset)
                hdr = fh.read(16)
                if len(hdr) < 8:
                    return "unknown"
                size = struct.unpack(">I", hdr[:4])[0]
                typ = hdr[4:8]
                header = 8
                if size == 1:
                    if len(hdr) < 16:
                        return "unknown"
                    size = struct.unpack(">Q", hdr[8:16])[0]
                    header = 16
                elif size == 0:
                    size = file_size - offset
                if size < header:
                    return "unknown"
                if typ == b"moov":
                    return "front"
                if typ == b"mdat":
                    return "back"
                offset += size
    except OSError:
        return "unknown"
    return "unknown"


class FaststartError(Exception):
    pass


def remux_to(src: str, dst: str, *, run=None) -> None:
    """ffmpeg 流拷贝把 moov 挪到头。失败抛 FaststartError，不删 src。"""
    cmd = [
        ffmpeg_bin(), "-y", "-loglevel", "error",
        "-i", src, "-c", "copy", "-map", "0",
        "-movflags", "+faststart", dst,
    ]
    runner = run or subprocess.run
    try:
        proc = runner(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout_sec(), check=False)
    except subprocess.TimeoutExpired as exc:
        raise FaststartError("timeout") from exc
    except FaststartError:
        raise
    except Exception as exc:
        raise FaststartError(type(exc).__name__) from exc
    code = getattr(proc, "returncode", 1)
    if code != 0:
        err = getattr(proc, "stderr", b"") or b""
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        raise FaststartError((err or "ffmpeg failed")[:400])


def rewrite_local(path: str, *, run=None, detect=None) -> dict:
    """重写本机文件。临时文件 + replace；失败删临时文件，原文件一个字节不动。"""
    detect_fn = detect or detect_moov_position
    if not os.path.isfile(path):
        return {"ok": False, "reason": "not_a_file"}
    try:
        if detect_fn(path) == "front":
            return {"ok": True, "reason": "already_front", "skipped": True,
                    "size": os.path.getsize(path)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": type(exc).__name__}
    tmp = path + ".faststart-tmp"
    try:
        remux_to(path, tmp, run=run)
        if not os.path.isfile(tmp) or os.path.getsize(tmp) <= 0:
            raise FaststartError("empty output")
        if detect_fn(tmp) != "front":
            raise FaststartError("output moov not front")
        os.replace(tmp, path)
        return {"ok": True, "reason": "rewritten", "size": os.path.getsize(path)}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[mp4-faststart] local rewrite failed path=%s err=%s",
            path, type(exc).__name__)
        return {"ok": False, "reason": type(exc).__name__}
    finally:
        if os.path.exists(tmp):
            # replace 成功后 tmp 已不在；失败时清掉半成品。
            try:
                os.remove(tmp)
            except OSError:
                pass


def _enough_disk(directory: str, need: int) -> bool:
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return False
    return free >= need


def _work_dir() -> str:
    base = os.getenv("EMBY_CACHE_DIR") or tempfile.gettempdir()
    path = os.path.join(base, "mp4-faststart")
    os.makedirs(path, exist_ok=True)
    return tempfile.mkdtemp(prefix="job-", dir=path)


def rewrite_drive_file(file_id: str, *, size_hint: int = 0, token: str | None = None,
                       peek=None, download=None, upload=None,
                       rewrite_local_fn=None, detect=None) -> dict:
    """下载到临时目录、重写、再按同一 file id 上传覆盖。

    上传没成功就不动网盘上的原文件。moov 已经在头时不下载整文件。
    """
    if not file_id:
        return {"ok": False, "reason": "no_file_id"}
    if token is None:
        try:
            from backend.emby_server import drive_auth
            token = drive_auth.get_drive_bearer_token(f"faststart:{file_id}")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": type(exc).__name__}
    if not token:
        return {"ok": False, "reason": "no_token"}
    peek_fn = peek or peek_drive_moov
    try:
        peeked = peek_fn(file_id, token, _as_int(size_hint))
    except Exception:  # noqa: BLE001 — 窥探失败不否定后续整文件重写
        peeked = "unknown"
    if peeked == "front":
        return {"ok": True, "reason": "already_front", "skipped": True,
                "size": _as_int(size_hint) or None}
    work = _work_dir()
    src = os.path.join(work, "in.mp4")
    try:
        basis = _as_int(size_hint) or min_bytes()
        if not _enough_disk(work, basis * 2 + _DISK_MARGIN):
            return {"ok": False, "reason": "low_disk"}
        (download or download_drive_file)(file_id, token, src)
        if not os.path.isfile(src) or os.path.getsize(src) <= 0:
            return {"ok": False, "reason": "empty_download"}
        if rewrite_local_fn is not None:
            result = rewrite_local_fn(src)
        elif detect is not None:
            result = rewrite_local(src, detect=detect)
        else:
            result = rewrite_local(src)
        if not result.get("ok"):
            return result
        if result.get("reason") == "already_front" or result.get("skipped"):
            return result
        (upload or upload_drive_replace)(file_id, token, src)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[mp4-faststart] drive rewrite failed fid=%s err=%s",
            file_id, type(exc).__name__)
        return {"ok": False, "reason": type(exc).__name__}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def peek_drive_moov(file_id: str, token: str, file_size: int = 0) -> str:
    """只拉文件头，判断 moov 是否已经在前。失败返回 unknown。"""
    import httpx

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Range": f"bytes=0-{_PEEK_BYTES - 1}",
    }
    try:
        with httpx.stream(
            "GET", url,
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=headers, timeout=60, follow_redirects=True,
        ) as resp:
            if resp.status_code not in (200, 206):
                return "unknown"
            blob = b""
            for chunk in resp.iter_bytes():
                blob += chunk
                if len(blob) >= _PEEK_BYTES:
                    break
    except Exception:  # noqa: BLE001
        return "unknown"
    return detect_moov_bytes(blob[:_PEEK_BYTES], file_size or 0)


def download_drive_file(file_id: str, token: str, dest: str) -> None:
    import httpx

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.stream(
        "GET", url,
        params={"alt": "media", "supportsAllDrives": "true"},
        headers=headers, timeout=None, follow_redirects=True,
    ) as resp:
        if resp.status_code != 200:
            raise FaststartError(f"download {resp.status_code}")
        with open(dest, "wb") as out:
            for chunk in resp.iter_bytes(1024 * 1024):
                out.write(chunk)


def upload_drive_replace(file_id: str, token: str, src_path: str, *,
                         request=None) -> None:
    """可续传更新同一 file id。未完成的上传不会替换网盘上的旧内容。"""
    import httpx

    request_fn = request or httpx.request
    size = os.path.getsize(src_path)
    init = request_fn(
        "PATCH",
        f"https://www.googleapis.com/upload/drive/v3/files/{file_id}",
        params={"uploadType": "resumable", "supportsAllDrives": "true"},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
        timeout=120,
    )
    location = ""
    headers = getattr(init, "headers", {}) or {}
    getter = getattr(headers, "get", None)
    if getter:
        location = getter("location") or getter("Location") or ""
    if getattr(init, "status_code", 0) not in (200, 201) or not location:
        raise FaststartError(f"upload session {getattr(init, 'status_code', 0)}")
    with open(src_path, "rb") as fh:
        pos = 0
        while pos < size:
            fh.seek(pos)
            chunk = fh.read(_UPLOAD_CHUNK)
            if not chunk:
                raise FaststartError("short read")
            end = pos + len(chunk) - 1
            put = request_fn(
                "PUT", location,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Range": f"bytes {pos}-{end}/{size}",
                    "Content-Length": str(len(chunk)),
                },
                content=chunk,
                timeout=300,
            )
            code = getattr(put, "status_code", 0)
            if code in (200, 201):
                return
            if code == 308:
                put_headers = getattr(put, "headers", {}) or {}
                rng = ""
                pg = getattr(put_headers, "get", None)
                if pg:
                    rng = pg("range") or pg("Range") or ""
                if "-" in str(rng):
                    pos = int(str(rng).rsplit("-", 1)[-1]) + 1
                else:
                    pos = end + 1
                continue
            raise FaststartError(f"upload put {code}")
    raise FaststartError("upload incomplete")


def select_backfill_jobs(session, limit: int = 0) -> list[dict]:
    """存量候选。moov 已在头的不进。limit<=0 表示不额外截断（仍是显式查询）。"""
    from sqlalchemy import or_

    from backend.emby_server import models as em

    threshold = min_bytes()
    query = session.query(em.MediaItem).filter(
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.item_type.in_(("movie", "episode")),
        em.MediaItem.container.in_(tuple(sorted(MP4_CONTAINERS))),
        or_(
            em.MediaItem.moov_position == "back",
            em.MediaItem.size >= threshold,
        ),
        or_(
            em.MediaItem.moov_position.is_(None),
            em.MediaItem.moov_position != "front",
        ),
    ).order_by(em.MediaItem.id.asc())
    if limit and limit > 0:
        query = query.limit(limit)
    jobs = []
    for item in query.all():
        ok, reason = eligible(
            container=item.container, size=item.size,
            moov_position=item.moov_position, path=item.file_path or "")
        if not ok:
            continue
        jobs.append({
            "item_id": item.id,
            "file_path": item.file_path or "",
            "size": _as_int(item.size),
            "container": item.container or "",
            "moov_position": item.moov_position,
            "drive_file_id": (item.drive_file_id or "").strip(),
            "reason": reason,
        })
    return jobs


def run_backfill(session, *, apply: bool, limit: int = 0, process=None) -> dict:
    """存量入口。默认不改文件。"""
    allowed, why = backfill_will_rewrite(apply=apply)
    jobs = select_backfill_jobs(session, limit=limit)
    if not allowed:
        return {"ran": False, "reason": why, "candidates": len(jobs), "processed": 0,
                "rewritten": 0, "kept": 0}
    process_fn = process or process_job
    rewritten = 0
    kept = 0
    for job in jobs:
        # 队列线程与脚本不要叠在一起：脚本同步处理，失败继续下一条。
        result = process_fn(job)
        if result.get("ok") and result.get("reason") == "rewritten":
            rewritten += 1
        elif not result.get("ok"):
            kept += 1
        time.sleep(0)  # 让出；真正的节流是单线程顺序，不并行打网盘
    return {"ran": True, "reason": "apply", "candidates": len(jobs),
            "processed": len(jobs), "rewritten": rewritten, "kept": kept}

"""图床镜像（批一：库封面 + 合集封面，自有服务器静态托管）。

设计：~/workspace/reports/image-bed-mirror-design-20261025.md；P0–P4 已定：
P0 自有服务器静态托管；P1 首批只库封面 + 合集封面；P2 borrow 首批不镜像
（子开关默认关）；P3 首批同站出图（不前置 CDN）；P4 首批不签名。

核心口径（不许漂移）：
- 镜像不是顺序键的新一步：LIBRARY_COVER_STRATEGY_ORDER 不变；镜像只在
  解析选出源之后换地址，返回的 source 仍标原步。
- 开关关时读路径逐字不变（不查行、不入队）；失败永远回落原地址。
- 源指纹：本地文件取（大小，mtime_ns），远端 URL 取 URL 本身；同一指纹
  只一行。裸 URL 哈希不用——库封面 URL 按库 guid 稳定、内容换了 URL
  不变，裸 URL 哈希会在换封面后继续 serving 旧图；本地指纹变了旧行
  自然不再命中，新指纹重新入队。
- borrow 只镜 http(s) 远端 URL：borrow 的 Images 路由形态已是本地出图，
  镜像它无收益，且端点拿不到代表海报本地路径、指纹对不上，跳过。
- 合集封面：现行代码没有合集封面 URL（set_to_dict 不出图 URL、无拼贴
  出图路由），批一先接库封面；本模块的 apply_mirror 对任意
  {url, source} 结果通用，合集拼贴出图口一到、传 source="collage"
  即可复用（collage 与 render 同属自有图，不受 borrow 子开关管）。
- 出图路由公开（不鉴权）：封面图本就是公开可见内容（P4），且浏览器
  <img> 带不了门户 JWT；地址按 64 位十六进制源哈希寻址，不含用户数据。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import func

from backend.emby_server import image_store
from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

COVER_MIRROR_ENABLED = env_int("COVER_MIRROR_ENABLED", 0, 0, 1)
COVER_MIRROR_BORROW_ENABLED = env_int("COVER_MIRROR_BORROW_ENABLED", 0, 0, 1)
COVER_MIRROR_RETRY_MAX = env_int("COVER_MIRROR_RETRY_MAX", 3, 0, 10)
COVER_MIRROR_WORKER_CONCURRENCY = env_int("COVER_MIRROR_WORKER_CONCURRENCY", 1, 1, 4)

# 自有图源（render/uploaded/collage）：出图地址已是本站路由，镜像它得的是
# 公开、稳定、可挂 CDN 的地址；borrow 远端 URL 同理。svg/none 永不镜像。
_MIRRORABLE_LOCAL_SOURCES = ("render", "uploaded", "collage")
_IMAGES_ROUTE_RE = re.compile(r"^/emby/Items/([^/]+)/Images/Primary$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_MIRROR_DIR_NAME = "cover-mirrors"
_MAX_MIRROR_BYTES = 8 * 1024 * 1024  # 单张图上限，与 image_store 单图上限同值
_DOWNLOAD_TIMEOUT_SEC = 15
_WORKER_INTERVAL_SEC = 60
_WORKER_BATCH = 20
_MIRROR_DIR_SIZE_CAP = 1024 * 1024 * 1024  # 入队磁盘 guard：批一量级 <50MB，1GB 绰绰有余
_BACKOFF_BASE_SEC = 60
_BACKOFF_MAX_SEC = 3600

_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}
_EXT_TO_CONTENT_TYPE = {ext: ctype for ctype, ext in _CONTENT_TYPE_TO_EXT.items()}
_EXT_TO_CONTENT_TYPE[".jpeg"] = "image/jpeg"
_KNOWN_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")


def enabled() -> bool:
    """总闸（测试 patch 本模块常量）。"""
    return COVER_MIRROR_ENABLED > 0


def backend() -> str:
    """宿主：local（批一唯一实现）；其它值（115 等）批一不实现，入队拒绝。"""
    return (os.environ.get("COVER_MIRROR_BACKEND", "") or "").strip() or "local"


def borrow_enabled() -> bool:
    """borrow 远端源是否镜像（测试 patch 本模块常量）。"""
    return COVER_MIRROR_BORROW_ENABLED > 0


def retry_max() -> int:
    """失败重试上限（测试 patch 本模块常量）。"""
    return COVER_MIRROR_RETRY_MAX


def concurrency() -> int:
    """工人并发（测试 patch 本模块常量）；默认 1 即顺序处理。"""
    return COVER_MIRROR_WORKER_CONCURRENCY


def base_url() -> str:
    """出图基址：空 = 同站出图，填 CDN 域名即前置 CDN（现读 env）。"""
    return (os.environ.get("COVER_MIRROR_BASE_URL", "") or "").strip()


def mirror_dir() -> str:
    """镜像文件目录：图片缓存目录下的 cover-mirrors 子目录（不新增 env 键）。"""
    return os.path.join(image_store.image_dir(), _MIRROR_DIR_NAME)


def mirror_url(source_hash: str) -> str:
    """镜像地址：基址 + /cdn/covers/{hash}；基址空即同站相对地址。"""
    base = base_url().rstrip("/")
    return f"{base}/cdn/covers/{source_hash}"


def is_valid_hash(value: str) -> bool:
    """出图路由的 hash 形校验：64 位十六进制，否则 404。"""
    return bool(value) and _HASH_RE.match(value) is not None


def source_fingerprint(url: str, local_path: str | None) -> str:
    """源指纹：本地文件在 → 大小+mtime；否则 URL 本身。"""
    if local_path:
        try:
            st = os.stat(local_path)
            return f"local:{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            pass
    return f"url:{url}"


def source_hash(url: str, local_path: str | None = None) -> str:
    """源指纹的 SHA-256（即镜像行主键与出图地址的 hash）。"""
    return hashlib.sha256(source_fingerprint(url, local_path).encode("utf-8")).hexdigest()


def is_mirrorable(source: str | None, url: str | None, borrow_ok: bool = False) -> bool:
    """该解析结果能不能进镜像：svg/none 永不；borrow 只认 http(s) 远端
    且子开关开；render/uploaded/collage 有 URL 即可。"""
    if not url:
        return False
    if source in _MIRRORABLE_LOCAL_SOURCES:
        return True
    if source == "borrow" and borrow_ok:
        lowered = url.lower()
        return lowered.startswith("https://") or lowered.startswith("http://")
    return False


def local_path_for_library_cover(lib, result: dict) -> str | None:
    """render/uploaded 结果对应的本地文件（存在才回）；其余 None。

    口径与 media_routes.item_image 的库分支一致：cover_path 相对路径
    拼 image_dir，绝对路径直接用；文件不在 → None（出图本身也会 404，
    镜像不替它背锅）。
    """
    if (result or {}).get("source") not in ("render", "uploaded"):
        return None
    rel = (getattr(lib, "cover_path", None) or "").strip()
    if not rel:
        return None
    abs_path = rel if os.path.isabs(rel) else os.path.abspath(
        os.path.join(image_store.image_dir(), rel))
    return abs_path if os.path.isfile(abs_path) else None


_MIRROR_REL_RE = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]+$")


def mirror_file_path(row) -> str | None:
    """镜像行对应的本地文件绝对路径；mirror_rel 须为 {64 位哈希}.{扩展名}
    且解析结果不出镜像目录，否则回 None（防穿越与脏行错服）。"""
    rel = (getattr(row, "mirror_rel", None) or "").strip()
    if not rel or not _MIRROR_REL_RE.match(rel):
        return None
    base = os.path.abspath(mirror_dir())
    path = os.path.abspath(os.path.join(base, rel))
    if not path.startswith(base + os.sep):
        return None
    return path


def _mirror_dir_size() -> int:
    """镜像目录当前总字节（入队 guard 用；目录不在即 0）。"""
    total = 0
    try:
        for name in os.listdir(mirror_dir()):
            try:
                total += os.path.getsize(os.path.join(mirror_dir(), name))
            except OSError:
                continue
    except OSError:
        return 0
    return total


def classify_enqueue(db, url: str, source: str, local_path: str | None = None) -> str:
    """入队预判（不写库、不含总闸，dry-run 与 apply 共用）：
    enqueue / exists / not_mirrorable / backend / disk_cap。"""
    if backend() != "local":
        return "backend"
    if not is_mirrorable(source, url, borrow_enabled()):
        return "not_mirrorable"
    h = source_hash(url, local_path)
    if db.query(em.CoverMirror).filter(em.CoverMirror.source_hash == h).first() is not None:
        return "exists"
    if _mirror_dir_size() >= _MIRROR_DIR_SIZE_CAP:
        return "disk_cap"
    return "enqueue"


def enqueue_mirror(db, url: str, source: str, local_path: str | None = None,
                   dry_run: bool = False) -> bool:
    """入队：总闸关、宿主非 local、不可镜像、已有行、磁盘超 cap 任一成立
    即 False；dry_run 只预判不写库。成功建 pending 行才 True。"""
    if not enabled():
        return False
    if classify_enqueue(db, url, source, local_path) != "enqueue":
        return False
    if dry_run:
        return True
    row = em.CoverMirror(
        source_hash=source_hash(url, local_path),
        source_url=url,
        source=source,
        backend="local",
        status="pending",
        local_path_hint=local_path,
        attempts=0,
    )
    db.add(row)
    db.commit()
    return True


def apply_mirror(db, result: dict, local_path: str | None = None) -> dict:
    """读路径镜像层：解析结果 →（有成功镜像行且文件在 → 换镜像地址，
    source 不变；否则原样返回，并在无行时入队）。

    开关关时逐字返回，不查行、不入队。失败/pending/dead 行一律回原地址。
    """
    url = (result or {}).get("url")
    source = (result or {}).get("source")
    if not enabled() or not url:
        return result
    if not is_mirrorable(source, url, borrow_enabled()):
        return result
    h = source_hash(url, local_path)
    row = db.query(em.CoverMirror).filter(em.CoverMirror.source_hash == h).first()
    if row is not None:
        if (row.status == "success" and row.backend == "local"
                and mirror_file_path(row) and os.path.isfile(mirror_file_path(row))):
            return {"url": mirror_url(h), "source": source}
        return result
    enqueue_mirror(db, url, source, local_path=local_path)
    return result


def mirror_stats(db) -> dict:
    """管理端只读可见性：各状态行数 + 镜像目录总字节。"""
    counts = dict(
        db.query(em.CoverMirror.status, func.count(em.CoverMirror.source_hash))
        .group_by(em.CoverMirror.status).all())
    total = sum(counts.values())
    return {"total": total, "by_status": counts,
            "total_bytes": _mirror_dir_size()}


def retry_mirror(db, source_hash: str):
    """管理端手动重推：无行 → None；failed/dead → pending（计数清零）；
    进行中或已成功 → False（不动）。"""
    row = db.query(em.CoverMirror).filter(
        em.CoverMirror.source_hash == source_hash).first()
    if row is None:
        return None
    if row.status not in ("failed", "dead"):
        return False
    row.status = "pending"
    row.attempts = 0
    row.next_attempt_at = None
    row.last_error_kind = None
    db.commit()
    return True


class _Unresolvable(Exception):
    """源解析失败：kind 进 last_error_kind，不抛到调用方。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def _sniff_ext(data: bytes) -> str | None:
    """按文件头猜扩展名（本地后缀不可信 / 远端无类型时的回落）。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return None


def _local_file_for_guid(db, guid: str):
    """Images 路由形态的本地文件：先按库 guid 查 cover_path，再按条目
    guid 查 poster_path；文件不在 → None（调用方再判失败）。"""
    lib = db.query(em.Library).filter(em.Library.guid == guid).first()
    rel = getattr(lib, "cover_path", None) if lib is not None else None
    if rel:
        path = rel if os.path.isabs(rel) else os.path.abspath(
            os.path.join(image_store.image_dir(), rel))
        if os.path.isfile(path):
            return path
    item = db.query(em.MediaItem).filter(em.MediaItem.guid == guid).first()
    rel = getattr(item, "poster_path", None) if item is not None else None
    if rel and image_store.is_cached_path(rel) and os.path.isfile(rel):
        return rel
    return None


def _download_http(url: str):
    """远端下载（默认实现，可注入替身）：15 秒超时、跟随跳转、8MB 上限，
    超了直接失败不落盘。返回 (bytes, content_type)。"""
    import httpx

    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                raise _Unresolvable(f"http_{resp.status_code}")
            chunks = []
            total = 0
            for chunk in resp.iter_bytes(65536):
                total += len(chunk)
                if total > _MAX_MIRROR_BYTES:
                    raise _Unresolvable("too_large")
                chunks.append(chunk)
            data = b"".join(chunks)
    return data, (resp.headers.get("content-type", "") or "").split(";")[0].strip()


def _ext_for(data: bytes, content_type: str, local_path: str | None) -> str:
    """扩展名：本地文件取后缀（须在已知集内），远端取 content-type 映射，
    都没有则按文件头猜；猜不出 → 失败。"""
    if local_path:
        ext = os.path.splitext(local_path)[1].lower()
        if ext in _KNOWN_EXTS:
            return ext
    mapped = _CONTENT_TYPE_TO_EXT.get((content_type or "").lower())
    if mapped:
        return mapped
    sniffed = _sniff_ext(data)
    if sniffed:
        return sniffed
    raise _Unresolvable("unknown_type")


def resolve_source_bytes(db, row, http_get=None):
    """工人取源字节：返回 (bytes, content_type, ext)。

    顺序：hint 文件（指纹校验，不对 → source_changed，绝不将错就错）→
    Images 路由 guid 解析 → http(s) 远端下载；其它一律 unresolvable。
    """
    url = row.source_url or ""
    hint = (row.local_path_hint or "").strip() or None
    if hint:
        if not os.path.isfile(hint):
            raise _Unresolvable("local_missing")
        if source_hash(url, hint) != row.source_hash:
            raise _Unresolvable("source_changed")
        with open(hint, "rb") as handle:
            data = handle.read(_MAX_MIRROR_BYTES + 1)
        if len(data) > _MAX_MIRROR_BYTES:
            raise _Unresolvable("too_large")
        ext = _ext_for(data, "", hint)
        return data, _EXT_TO_CONTENT_TYPE.get(ext, ""), ext
    match = _IMAGES_ROUTE_RE.match(url.strip())
    if match:
        path = _local_file_for_guid(db, match.group(1))
        if path is None:
            raise _Unresolvable("local_missing")
        with open(path, "rb") as handle:
            data = handle.read(_MAX_MIRROR_BYTES + 1)
        if len(data) > _MAX_MIRROR_BYTES:
            raise _Unresolvable("too_large")
        ext = _ext_for(data, "", path)
        return data, _EXT_TO_CONTENT_TYPE.get(ext, ""), ext
    lowered = url.lower()
    if lowered.startswith("https://") or lowered.startswith("http://"):
        getter = http_get or _download_http
        data, content_type = getter(url)
        if len(data) > _MAX_MIRROR_BYTES:
            raise _Unresolvable("too_large")
        ext = _ext_for(data, content_type, None)
        return data, content_type or _EXT_TO_CONTENT_TYPE.get(ext, ""), ext
    raise _Unresolvable("unresolvable")


def _backoff_sec(attempts: int) -> int:
    """指数退避：60s 起，翻倍，上限 3600s。"""
    return min(_BACKOFF_MAX_SEC, _BACKOFF_BASE_SEC * (2 ** max(0, attempts - 1)))


def _mark_failed(db, row, kind: str) -> None:
    """失败记账：计数 +1；达上限进 dead 等手动重推，否则 failed + 下次时间。"""
    row.attempts = (row.attempts or 0) + 1
    row.last_error_kind = kind
    if row.attempts >= retry_max():
        row.status = "dead"
        row.next_attempt_at = None
    else:
        row.status = "failed"
        row.next_attempt_at = datetime.now() + timedelta(seconds=_backoff_sec(row.attempts))
    db.commit()


def process_row(db, row, http_get=None) -> str:
    """处理一行：取源字节 → 原子落盘 → success；任何失败只记账不抛。"""
    try:
        data, content_type, ext = resolve_source_bytes(db, row, http_get=http_get)
    except _Unresolvable as exc:
        _mark_failed(db, row, exc.kind)
        return "failed"
    except Exception:
        logger.warning("图床镜像取源异常：hash=%s", row.source_hash, exc_info=True)
        _mark_failed(db, row, "error")
        return "failed"
    rel = f"{row.source_hash}{ext}"
    target = os.path.join(mirror_dir(), rel)
    try:
        os.makedirs(mirror_dir(), exist_ok=True)
        temporary = f"{target}.{os.getpid()}.downloading"
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        logger.warning("图床镜像落盘异常：hash=%s", row.source_hash, exc_info=True)
        _mark_failed(db, row, "write_error")
        return "failed"
    row.status = "success"
    row.mirror_rel = rel
    row.content_type = content_type or ""
    row.attempts = 0
    row.next_attempt_at = None
    row.last_error_kind = None
    db.commit()
    return "success"


def claim_due_rows(db, limit: int):
    """认领到期行：pending 全认领；failed 只认领过了下次时间的。
    逐行 UPDATE + 行数 guard，抢单并发下不重复认领。"""
    now = datetime.now()
    candidates = (
        db.query(em.CoverMirror)
        .filter(
            (em.CoverMirror.status == "pending")
            | ((em.CoverMirror.status == "failed")
               & (em.CoverMirror.next_attempt_at.isnot(None))
               & (em.CoverMirror.next_attempt_at <= now))
        )
        .order_by(em.CoverMirror.created_at.asc())
        .limit(limit)
        .all()
    )
    claimed = []
    for row in candidates:
        updated = (
            db.query(em.CoverMirror)
            .filter(em.CoverMirror.source_hash == row.source_hash,
                    em.CoverMirror.status == row.status)
            .update({"status": "running"})
        )
        if updated == 1:
            claimed.append(row)
    if claimed:
        db.commit()
        for row in claimed:
            db.refresh(row)
    return claimed


def process_mirror_batch(db, http_get=None) -> dict:
    """工人一轮：认领到期行并处理；总闸关时一律不动。"""
    if not enabled():
        return {"claimed": 0, "success": 0, "failed": 0}
    rows = claim_due_rows(db, _WORKER_BATCH)
    if not rows:
        return {"claimed": 0, "success": 0, "failed": 0}
    workers = concurrency()
    if workers <= 1:
        outcomes = [process_row(db, row, http_get=http_get) for row in rows]
    else:
        def _one(source_hash: str) -> str:
            from backend.database import SessionLocal

            session = SessionLocal()
            try:
                target = session.query(em.CoverMirror).filter(
                    em.CoverMirror.source_hash == source_hash).first()
                if target is None:
                    return "skipped"
                return process_row(session, target, http_get=http_get)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(_one, [r.source_hash for r in rows]))
    return {"claimed": len(rows),
            "success": outcomes.count("success"),
            "failed": outcomes.count("failed")}


def start_mirror_daemon(stop_event=None):
    """启动图床镜像工人守护线程（必须显式调用；import 时绝不自动启动）。

    总闸关时线程空转等待，不发请求、不读写任何状态；开时每 60 秒一轮，
    单轮最多 20 行，并发数走 COVER_MIRROR_WORKER_CONCURRENCY（默认 1）。
    返回线程对象，调用方持有它以便观测。
    """
    stop = stop_event if stop_event is not None else threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                if enabled():
                    from backend.database import SessionLocal

                    session = SessionLocal()
                    try:
                        process_mirror_batch(session)
                    finally:
                        session.close()
            except Exception:
                logger.warning("图床镜像工人轮次异常，已跳过本轮", exc_info=True)
            stop.wait(_WORKER_INTERVAL_SEC)

    thread = threading.Thread(target=_loop, name="cover-mirror", daemon=True)
    thread.start()
    return thread


__all__ = [
    "enabled", "backend", "borrow_enabled", "retry_max", "concurrency",
    "base_url", "mirror_dir", "mirror_url", "is_valid_hash",
    "source_fingerprint", "source_hash",
    "is_mirrorable", "local_path_for_library_cover", "mirror_file_path",
    "classify_enqueue", "enqueue_mirror", "apply_mirror", "mirror_stats",
    "retry_mirror", "resolve_source_bytes", "process_row", "claim_due_rows",
    "process_mirror_batch", "start_mirror_daemon",
]

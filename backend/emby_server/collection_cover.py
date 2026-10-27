"""合集拼贴封面：渲染、磁盘缓存、读口 URL。

跟库封面同一条链，不另起图床：

- 拼图复用 ``library_cover_render.render_collage_bytes``（默认 poster
  样式，1920×1080 WebP）；选图换成合集的可见成员（与架上露出口径
  相同，走 ``visible_items_query``）。
- 缓存落在图片目录的 ``collection-covers/{id}.webp``，旁挂指纹文件。
  成员海报或合集名变了指纹就对不上，下次出图重拼并覆盖。
- 读口只出相对路径 ``/api/user/collections/{id}/cover``。有新鲜缓存
  文件时交给 ``cover_mirror.apply_mirror(..., source="collage")``，
  镜像开且已成功则换成 ``/cdn/covers/{hash}``；镜像关、或缓存还没
  生成，原路径逐字不动（不拿一条相对路径去入队——工人解析不了它）。
- ``artwork_mode=none`` 或没有任何可用海报：``cover_url`` 为 None
  （不是空串）。出图路由此时回库封面同一张 SVG 占位，不拼图。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import uuid

from sqlalchemy import or_

from backend.emby_server import cover_mirror, image_store
from backend.emby_server import library_cover_render as lcr
from backend.emby_server import library_covers
from backend.emby_server import models as em

logger = logging.getLogger(__name__)

_CACHE_DIR_NAME = "collection-covers"
_locks_guard = threading.Lock()
_set_locks: dict[int, threading.Lock] = {}


def portal_cover_path(set_id: int) -> str:
    """合集封面的门户相对路径（镜像关时读口原样返回这一条）。"""
    return f"/api/user/collections/{int(set_id)}/cover"


def _set_lock(set_id: int) -> threading.Lock:
    with _locks_guard:
        lock = _set_locks.get(set_id)
        if lock is None:
            lock = threading.Lock()
            _set_locks[set_id] = lock
        return lock


def _webp_path(set_id: int) -> str:
    return os.path.join(image_store.image_dir(), _CACHE_DIR_NAME, f"{int(set_id)}.webp")


def _sidecar_path(webp_path: str) -> str:
    return webp_path + ".fingerprint"


def _source_token(src: str) -> str:
    if src.startswith(("http://", "https://")):
        return f"url:{src}"
    try:
        st = os.stat(src)
        return f"file:{src}:{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return f"file:{src}:missing"


def _fingerprint(media_set, posters: list[str], backdrop: str | None) -> str:
    parts = [
        str(media_set.id),
        media_set.name or "",
        lcr.DEFAULT_TEMPLATE,
        *(_source_token(p) for p in posters),
        _source_token(backdrop or ""),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def _sidecar_matches(webp_path: str, fingerprint: str) -> bool:
    if not os.path.isfile(webp_path):
        return False
    try:
        with open(_sidecar_path(webp_path), encoding="utf-8") as handle:
            return handle.read().strip() == fingerprint
    except OSError:
        return False


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.rendering"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _write_sidecar(webp_path: str, fingerprint: str) -> None:
    side = _sidecar_path(webp_path)
    temporary = f"{side}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(fingerprint)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, side)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _item_poster(item) -> str | None:
    for candidate in (item.poster_path, item.primary_image_url):
        usable = lcr._usable_source(candidate)
        if usable:
            return usable
    return None


def _item_backdrop(item) -> str | None:
    for candidate in (item.backdrop_path, item.backdrop_image_url):
        usable = lcr._usable_source(candidate)
        if usable:
            return usable
    return None


def pick_sources(db, media_set) -> tuple[list[str], str | None] | None:
    """可见成员里的海报（最多模板张数）和一张横图。一张海报都没有 → None。"""
    from backend.emby_server.media_sets import visible_items_query

    need = lcr.TEMPLATE_POSTER_COUNT
    rows = (
        visible_items_query(db, media_set.id)
        .filter(or_(
            em.MediaItem.poster_path.isnot(None),
            em.MediaItem.primary_image_url.isnot(None),
            em.MediaItem.backdrop_path.isnot(None),
            em.MediaItem.backdrop_image_url.isnot(None),
        ))
        .order_by(em.MediaItem.id.desc())
        .limit(max(need * 8, 24))
        .all()
    )
    rows.sort(key=lambda item: (
        item.last_scraped_at is None,
        -(item.last_scraped_at.timestamp()) if item.last_scraped_at else 0,
        -item.id,
    ))
    posters: list[str] = []
    backdrop = None
    for item in rows:
        if backdrop is None:
            backdrop = _item_backdrop(item)
        if len(posters) < need:
            poster = _item_poster(item)
            if poster and poster not in posters:
                posters.append(poster)
        if backdrop is not None and len(posters) >= need:
            break
    if not posters:
        return None
    return posters, backdrop or posters[0]


def ensure_cached(db, media_set) -> str | None:
    """返回新鲜的拼贴文件绝对路径；none / 无海报 / 渲染失败 → None。

    已有同指纹文件则直接复用，不重拼。
    """
    if getattr(media_set, "artwork_mode", None) != "auto_collage":
        return None
    sources = pick_sources(db, media_set)
    if sources is None:
        return None
    posters, backdrop = sources
    fingerprint = _fingerprint(media_set, posters, backdrop)
    path = _webp_path(media_set.id)
    if _sidecar_matches(path, fingerprint):
        return path
    data = lcr.render_collage_bytes(
        posters,
        title_text=media_set.name or "",
        sub_text="",
        backdrop=backdrop,
        template=lcr.DEFAULT_TEMPLATE,
        label=f"合集「{media_set.name or media_set.id}」",
    )
    if not data:
        return None
    with _set_lock(int(media_set.id)):
        if _sidecar_matches(path, fingerprint):
            return path
        _atomic_write(path, data)
        _write_sidecar(path, fingerprint)
    return path


def cover_url_for(db, media_set) -> str | None:
    """读口 ``cover_url``：没有封面可展示时 None，否则门户路径或镜像路径。"""
    if getattr(media_set, "artwork_mode", None) != "auto_collage":
        return None
    url = portal_cover_path(media_set.id)
    if db is None:
        return url
    sources = pick_sources(db, media_set)
    if sources is None:
        return None
    if not cover_mirror.enabled():
        return url
    posters, backdrop = sources
    path = _webp_path(media_set.id)
    if not _sidecar_matches(path, _fingerprint(media_set, posters, backdrop)):
        # 缓存还没生成：先回原路径。不入队，避免工人拿到解析不了的相对 URL。
        return url
    mirrored = cover_mirror.apply_mirror(
        db, {"url": url, "source": "collage"}, local_path=path)
    out = (mirrored or {}).get("url")
    return out or None


def placeholder_response():
    """与库封面 SVG 占位同一份字节（无拼贴可出时的回落，不 500）。"""
    from fastapi.responses import Response

    return Response(
        content=library_covers.SVG_PLACEHOLDER_BYTES,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def serve_collection_cover(db, media_set):
    """出图：拼得出就发缓存 WebP，否则发库封面同一张 SVG 占位。"""
    path = ensure_cached(db, media_set)
    if not path:
        return placeholder_response()
    from backend.emby_server.streaming import serve_image

    return serve_image(path)

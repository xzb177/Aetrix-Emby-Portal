#!/usr/bin/env python3
"""一次性回填：库封面 + 合集拼贴封面镜像入队（图床镜像批一）。

背景：图床镜像是封面解析结果的持久化/加速层（见
~/workspace/reports/image-bed-mirror-design-20261025.md，P0–P4 已定）；
日常入队发生在封面接口解析命中时，存量封面需本脚本一次性入队。

口径（与读路径逐字一致）：
- 只枚举已启用库；五件套共用同一解析结果，只按 poster 解析一次。
- 合集只枚举 artwork_mode=auto_collage。先保证拼贴缓存文件在
  （与出图路由同一份 ensure_cached），再按 source="collage" 与该
  本地文件入队；拼不出图的计 not_mirrorable。artwork_mode=none 不扫。
- 入队预判走 cover_mirror.classify_enqueue（不写库）：enqueue / exists /
  not_mirrorable / backend / disk_cap；dry-run 只计数、不写库
  （合集侧仍可能落下拼贴缓存文件，那是出图缓存，不是镜像行）。
- --apply 才写库，且总闸 COVER_MIRROR_ENABLED=1 时才真写（闸关时
  apply 直接报错退出，让操作者先开闸——避免闸关着攒一堆 pending 行
  却没工人消费的 half 状态）。
- 本脚本只写入队行，不下载：下载由 worker 单并发 + 限速完成，脚本
  本身不限速。合集行带 local_path_hint，工人按本地文件取字节。

用法（容器里，aetrix-api 与 aetrix-worker 都能连 DB）：

    docker exec aetrix-api python scripts/backfill_cover_mirrors.py
    docker exec aetrix-api python scripts/backfill_cover_mirrors.py --apply --limit 100
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import collection_cover, cover_mirror, library_covers  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


def _empty_stats() -> dict:
    return {"libraries": 0, "collections": 0, "enqueue": 0, "exists": 0,
            "not_mirrorable": 0, "backend": 0, "disk_cap": 0}


def _tally(db, stats: dict, url: str | None, source: str | None,
           local_path: str | None, apply: bool) -> None:
    if not url:
        stats["not_mirrorable"] += 1
        return
    verdict = cover_mirror.classify_enqueue(db, url, source, local_path)
    stats[verdict] = stats.get(verdict, 0) + 1
    if verdict == "enqueue" and apply:
        cover_mirror.enqueue_mirror(db, url, source, local_path=local_path)


def scan(db, *, apply: bool = False, limit: int = 0) -> dict:
    """枚举库封面与 auto_collage 合集并预判入队。apply 才写镜像行。"""
    stats = _empty_stats()
    query = db.query(em.Library).filter(em.Library.is_enabled == True).order_by(  # noqa: E712
        em.Library.id.asc())
    if limit > 0:
        query = query.limit(limit)
    for lib in query.all():
        stats["libraries"] += 1
        result = library_covers.resolve_library_cover(db, lib, "poster")
        url = (result or {}).get("url")
        source = (result or {}).get("source")
        local_path = cover_mirror.local_path_for_library_cover(lib, result) if url else None
        _tally(db, stats, url, source, local_path, apply)

    sets = db.query(em.EmbyMediaSet).filter(
        em.EmbyMediaSet.artwork_mode == "auto_collage",
    ).order_by(em.EmbyMediaSet.id.asc())
    if limit > 0:
        sets = sets.limit(limit)
    for media_set in sets.all():
        stats["collections"] += 1
        local_path = collection_cover.ensure_cached(db, media_set)
        url = collection_cover.portal_cover_path(media_set.id) if local_path else None
        _tally(db, stats, url, "collage", local_path, apply)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="库封面与合集拼贴封面镜像入队回填（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真写库（默认只计数）")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="库、合集各自最多处理几个（0 = 不限）")
    args = parser.parse_args()

    if args.apply and not cover_mirror.enabled():
        print("拒绝：COVER_MIRROR_ENABLED 未开，先开闸再 --apply（dry-run 不受此限）。")
        return 2

    db = SessionLocal()
    try:
        stats = scan(db, apply=args.apply, limit=args.limit)
    finally:
        db.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] libraries={stats['libraries']} collections={stats['collections']} "
          f"enqueue={stats['enqueue']} exists={stats['exists']} "
          f"not_mirrorable={stats['not_mirrorable']} backend={stats['backend']} "
          f"disk_cap={stats['disk_cap']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

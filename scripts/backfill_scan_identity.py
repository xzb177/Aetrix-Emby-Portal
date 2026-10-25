#!/usr/bin/env python3
"""一次性回填：emby_items 的 scan 身份快照列（scan_title / scan_year）。

背景：清单第 10 项 #13 的 PR-1 给 emby_items 加了五列身份证据列
（scan_title / scan_year / content_hash / content_hash_algo / identity_json），
其中 scan 列由扫描器写库点落库（见 scanner.py `_process_one`）。
存量条目这两列为 NULL，PR-3 的认亲层读存量 scan 列前需要先回填。

**本脚本只回填 scan_title / scan_year**：content_hash 与 identity_json
离线算不出来（哈希来自列举时源端元数据），不由本脚本碰——它们随
PR-2 落地后的下一轮扫描自然收敛（列举带哈希的源写新值，拿不到的
源保持 NULL）；本脚本跑过之后存量行的 content_hash 原样不动。

**只算路径、不读任何外部服务**：回填逻辑与扫描器写库点共用同一入口
（`parse_media_filename` + episode 走 `_series_name_of`），结果逐字一致。
增量安全 + 幂等：只处理 scan_title 为 NULL 且 file_path 非空的文件行
（series/season 结构行没有文件身份，不碰）；填过的行重复跑直接被查询
条件排除；按 id 游标分批、每批 commit 一次，中断后重跑自动续上。

口径说明（与写库点一致）：
- movie/其他文件行：scan_title = parsed name，scan_year = parsed year。
- episode 行：scan_title = `_series_name_of` 的产物（剧名），季/集号读现行列；
  目录推导离线可算：本机路径取 dirname，mount:// 路径取 rel 的 dirname，
  不解析挂载、不发任何请求。
- 解析不出标题（空串）时留 NULL（= 身份未知），不写空串占位；
  这类行第二遍仍会被扫到但写不出值，属于“未填”而非“已填”，
  状态上与第一遍完全一致（幂等指 DB 状态不变，不是跳过计数为零）。
- .strm 行按 .strm 文件名解析（与扫描器对 .strm 的处理同口径的近似值），
  下一轮扫描写库点会按真实口径纠正。

用法（容器里，aetrix-api 与 aetrix-worker 都能连 DB）：

    docker exec aetrix-api python scripts/backfill_scan_identity.py --dry-run
    docker exec aetrix-api python scripts/backfill_scan_identity.py
    docker exec aetrix-api python scripts/backfill_scan_identity.py --limit 1000
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.mounts import parse_mount_path  # noqa: E402
from backend.emby_server.scanner import (  # noqa: E402
    ScanFile,
    _series_name_of,
    parse_media_filename,
)


def _scanfile_for(file_path: str) -> ScanFile:
    """为离线推导构造最小 ScanFile：只填 _series_name_of 用到的两个字段。"""
    parsed_mount = parse_mount_path(file_path)
    if parsed_mount is not None:
        _, rel = parsed_mount
        return ScanFile(
            stored_path=file_path,
            name=os.path.basename(file_path),
            local_dir=None,
            dir_rel=posixpath.dirname(rel) or "/",
        )
    return ScanFile(
        stored_path=file_path,
        name=os.path.basename(file_path),
        local_dir=os.path.dirname(file_path),
        dir_rel="/",
    )


def compute_scan_identity(item, library_type: str) -> tuple[str | None, int | None]:
    """算一条存量行的 (scan_title, scan_year)，与扫描器写库点同口径。

    返回 (None, None) 表示算不出（调用方留 NULL、不写空串）。
    """
    file_path = item.file_path or ""
    if not file_path:
        return None, None
    parsed = parse_media_filename(file_path, library_type)
    if item.item_type == "episode":
        # _series_name_of 可能回填 parsed["year"]（目录名尾巴上的年份），
        # 顺序与扫描器批次一致：先算剧名、再取年份。
        title = _series_name_of(_scanfile_for(file_path), parsed) or parsed["name"]
    else:
        title = parsed["name"]
    return (title or None, parsed["year"])


def backfill_scan_identity(session, *, batch_size: int = 500,
                           limit: int = 0, dry_run: bool = False) -> dict:
    """回填缺 scan 身份的条目，返回统计。

    幂等：只挑 scan_title 为 NULL 的文件行；已填的行第二遍被查询条件排除。
    dry_run 时只统计、不落库。
    """
    scanned = 0
    filled = 0
    unfilled = 0
    skipped_no_library = 0
    last_id = 0
    lib_types: dict[int, str] = {}
    while True:
        want = batch_size if limit <= 0 else min(batch_size, limit - filled)
        if want <= 0:
            break
        rows = (
            session.query(em.MediaItem)
            .filter(
                em.MediaItem.id > last_id,
                em.MediaItem.scan_title.is_(None),
                em.MediaItem.file_path.isnot(None),
                em.MediaItem.file_path != "",
                em.MediaItem.item_type.notin_(["series", "season"]),
            )
            .order_by(em.MediaItem.id)
            .limit(want)
            .all()
        )
        if not rows:
            break
        for item in rows:
            scanned += 1
            last_id = item.id
            if item.library_id not in lib_types:
                lib = session.get(em.Library, item.library_id) if item.library_id else None
                lib_types[item.library_id] = (lib.collection_type if lib else None)
            library_type = lib_types[item.library_id]
            if not library_type:
                skipped_no_library += 1
                continue
            title, year = compute_scan_identity(item, library_type)
            if title is None and year is None:
                unfilled += 1
                continue
            if item.scan_title != title or item.scan_year != year:
                item.scan_title = title
                item.scan_year = year
                filled += 1
        if dry_run:
            session.rollback()
        else:
            session.commit()
        print(f"  …已扫描 {scanned} 条、回填 {filled} 条、"
              f"未填 {unfilled} 条、无库跳过 {skipped_no_library} 条（last_id={last_id}）")
        if limit > 0 and filled >= limit:
            break
    return {"scanned": scanned, "filled": filled, "unfilled": unfilled,
            "skipped_no_library": skipped_no_library}


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 emby_items 的 scan_title/scan_year")
    parser.add_argument("--dry-run", action="store_true", help="只统计、不落库")
    parser.add_argument("--limit", type=int, default=0, help="最多回填条数（0=不限）")
    parser.add_argument("--batch-size", type=int, default=500, help="每批条数")
    args = parser.parse_args()
    session = SessionLocal()
    try:
        stats = backfill_scan_identity(
            session, batch_size=args.batch_size, limit=args.limit, dry_run=args.dry_run)
    finally:
        session.close()
    print(f"完成：{stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

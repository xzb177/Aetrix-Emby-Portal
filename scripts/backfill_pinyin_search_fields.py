#!/usr/bin/env python3
"""一次性回填：emby_items 的拼音搜索派生字段（pinyin_full / pinyin_initials）。

背景：2026-10-24（FMBY 学习批二 #16）给 emby_items 加了两列拼音搜索键
（由 name 生成的全拼 / 首字母紧凑串，见 pinyin_sort.make_pinyin_fields）。
新写入由 models 的落库钩子（fill_item_pinyin_fields）自动生成；存量条目
这两列为 NULL，拼音搜索对它们不命中，需要跑一遍本脚本补齐。

**只写 DB、不读任何外部服务**：生成逻辑与落库钩子共用同一入口，结果
逐字一致。增量安全 + 幂等：只处理两列任一为 NULL 且 name 非空的行，
已回填的行重复跑直接跳过；按 id 游标分批、每批 commit 一次，中断后
重跑自动续上，不会一次性全库重算压垮 worker。

用法（容器里，aetrix-api 与 aetrix-worker 都能连 DB）：

    docker exec aetrix-api python scripts/backfill_pinyin_search_fields.py --dry-run
    docker exec aetrix-api python scripts/backfill_pinyin_search_fields.py
    docker exec aetrix-api python scripts/backfill_pinyin_search_fields.py --limit 1000
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.pinyin_sort import make_pinyin_fields  # noqa: E402


def backfill_pinyin_fields(session, *, batch_size: int = 500,
                           limit: int = 0, dry_run: bool = False) -> dict:
    """回填缺拼音字段的条目，返回 {"scanned": n, "filled": n}。

    幂等：只挑 pinyin_full / pinyin_initials 任一为 NULL 的行；填过的行
    第二遍跑直接被查询条件排除。dry_run 时只统计、不落库。
    """
    scanned = 0
    filled = 0
    last_id = 0
    while True:
        want = batch_size if limit <= 0 else min(batch_size, limit - filled)
        if want <= 0:
            break
        rows = (
            session.query(em.MediaItem)
            .filter(
                em.MediaItem.id > last_id,
                em.MediaItem.name != "",
                or_(
                    em.MediaItem.pinyin_full.is_(None),
                    em.MediaItem.pinyin_initials.is_(None),
                ),
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
            full, initials = make_pinyin_fields(item.name or "")
            if item.pinyin_full != full or item.pinyin_initials != initials:
                item.pinyin_full = full
                item.pinyin_initials = initials
                filled += 1
        if dry_run:
            session.rollback()
        else:
            session.commit()
        print(f"  …已扫描 {scanned} 条、回填 {filled} 条（last_id={last_id}）")
        if limit > 0 and filled >= limit:
            break
    return {"scanned": scanned, "filled": filled}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计将回填多少条，不写库")
    ap.add_argument("--limit", type=int, default=0,
                    help="最多回填多少条（0 = 不限）")
    ap.add_argument("--batch", type=int, default=500,
                    help="每批处理条数（默认 500，逐批 commit）")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        stats = backfill_pinyin_fields(
            session, batch_size=args.batch, limit=args.limit,
            dry_run=args.dry_run)
    finally:
        session.close()
    mode = "（dry-run，未写库）" if args.dry_run else ""
    print(f"完成：扫描 {stats['scanned']} 条、回填 {stats['filled']} 条{mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""一次性重入队：enrich_status='done' 但无 overview/poster 的条目。

背景（2026-10-13）：豆瓣封禁期间，封禁被当成「没搜中」→ 条目以
metadata_source='none'/空 overview 落为 done。共约 6.9 万条。
封禁修复部署后，用本脚本把它们打回 pending，让 enrich 按正常节奏重补。

反惊群设计：next_retry_at 在 --spread-hours 内均匀随机散开，
避免几万条同时抢单瞬间再触发豆瓣封禁。

用法（容器里）：
    docker exec aetrix-worker python scripts/requeue_douban_missed.py --dry-run
    docker exec aetrix-worker python scripts/requeue_douban_missed.py --spread-hours 12
    docker exec aetrix-worker python scripts/requeue_douban_missed.py --limit 1000 --dry-run

幂等：只动 enrich_status='done' 且 overview 为空的条目；跑过一次后
这些条目变成 pending，再跑不会重复入队。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理条数（0=不限）")
    ap.add_argument("--spread-hours", type=float, default=12.0,
                    help="重试时间散开窗口（小时），默认 12")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = (db.query(em.MediaItem)
             .filter(em.MediaItem.enrich_status == "done")
             .filter((em.MediaItem.overview.is_(None)) |
                     (em.MediaItem.overview == "")))
        total = q.count()
        print(f"待重入队条目：{total}")
        if args.dry_run:
            print("dry-run：未写库")
            return 0
        if total == 0:
            return 0

        spread_sec = max(60.0, args.spread_hours * 3600.0)
        now = datetime.now()
        done = 0
        batch = 500
        while True:
            items = (q.limit(batch).all() if not args.limit
                     else q.limit(min(batch, args.limit - done)).all())
            if not items:
                break
            for it in items:
                it.enrich_status = "pending"
                it.enrich_attempts = 0
                it.enrich_claimed_at = None
                it.enrich_claim_token = None
                # 均匀随机散开，避免惊群
                it.enrich_next_retry_at = now + timedelta(
                    seconds=random.uniform(0, spread_sec))
            db.commit()
            done += len(items)
            print(f"  已入队 {done}/{total}")
            if args.limit and done >= args.limit:
                break
        print(f"完成：{done} 条已打回 pending，散开窗口 {args.spread_hours}h")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

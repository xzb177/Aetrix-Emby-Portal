#!/usr/bin/env python3
"""strm 条目 size 回填视频口径（一次性回填）——Rex 兼容修复

背景：扫描器入库 .strm 条目时，item.size 存的是本机 .strm 纸条文本的字节数
（纸条口径，<=64KB），而真实视频大小在 strm_gen_files 账本里（Drive 元数据）。
详情/PlaybackInfo 的 MediaSource.Size 直取 item.size，导致 Rex 等三方客户端
把文件大小显示为 0 KB。

代码已修（scanner._sync_video_size_from_ledger：增量入库时顺带对齐），这个脚本把
**存量**里「纸条口径 size 且账本有视频口径」的条目一次性补上。

口径（与 scanner._sync_video_size_from_ledger 逐字节一致）：
- 只碰 item_type in (movie, episode) 且 file_path 以 .strm 结尾的行；
  series/season 容器行 size=0 是设计，不碰。
- join 键：strm_gen_files.strm_path = substring(emby_items.file_path from 7)
 （去掉 "/strm/" 前缀）；非 /strm/ 路径用原路径，与诊断报告生产验证的口径一致。
- 只在账本 size > 64KB（视频口径）时写；账本无此文件、或账本 size 仍是纸条
  口径 → 保持现状，不乱填。纸条大小以外的东西绝不反写。

用法（容器里 / 工作区都行）：

    docker exec aetrix-api python scripts/backfill_strm_video_size.py          # 先 dry-run 看数
    docker exec aetrix-api python scripts/backfill_strm_video_size.py --apply  # 真正写库
    docker exec aetrix-api python scripts/backfill_strm_video_size.py --apply --limit 5000

幂等：只碰纸条口径的行，可反复跑。生产回填由主链路在合并后另行跑并报数。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import case, func, or_  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402

_STUB_MAX = 64 * 1024


def _stub_caliber():
    """纸条口径的过滤条件：NULL / 0 / <=64KB 都算缺视频口径"""
    return or_(em.MediaItem.size.is_(None), em.MediaItem.size <= _STUB_MAX)


def _rel_path():
    """去掉 "/strm/" 前缀的相对路径（与 strm_gen_files.strm_path 对齐；非 /strm/ 路径用原路径）"""
    return case(
        (em.MediaItem.file_path.startswith("/strm/"), func.substring(em.MediaItem.file_path, 7)),
        else_=em.MediaItem.file_path,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="回填 .strm 条目的视频口径 size")
    p.add_argument("--apply", action="store_true",
                   help="真正写库；不加则只 dry-run 统计")
    p.add_argument("--limit", type=int, default=0,
                   help="最多处理多少条（0=不限），dry-run 时同样生效")
    p.add_argument("--batch-size", type=int, default=2000,
                   help="每多少条提交一次（默认 2000）")
    return p.parse_args()


def _ledger_size_map(db, rels: list) -> dict:
    """一批 rel 路径 → 账本视频口径 size（> _STUB_MAX），无则不出现"""
    rows = (
        db.query(em.StrmGenFile.strm_path, func.max(em.StrmGenFile.size))
        .filter(em.StrmGenFile.strm_path.in_(rels))
        .filter(em.StrmGenFile.size > _STUB_MAX)
        .group_by(em.StrmGenFile.strm_path)
        .all()
    )
    return {rel: size for rel, size in rows}


def main() -> int:
    args = parse_args()

    # 总数先行：纸条口径 .strm 条目中，账本有视频口径的
    ledger_exists = (
        em.StrmGenFile.__table__.select()
        .where(em.StrmGenFile.strm_path == _rel_path())
        .where(em.StrmGenFile.size > _STUB_MAX)
        .exists()
    )
    db = SessionLocal()
    try:
        total = (
            db.query(func.count(em.MediaItem.id))
            .filter(em.MediaItem.item_type.in_(["movie", "episode"]))
            .filter(em.MediaItem.merged_into_id.is_(None))
            .filter(func.lower(em.MediaItem.file_path).like("%.strm"))
            .filter(_stub_caliber())
            .filter(ledger_exists)
            .scalar()
        ) or 0
    finally:
        db.close()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] 开始扫描纸条口径 .strm 条目（其中账本有视频口径约 {total} 条）")
    if not total:
        return 0

    fixed = 0
    skipped = 0
    done = 0
    last_id = 0
    db = SessionLocal()
    t0 = time.time()
    try:
        while True:
            batch = (
                db.query(em.MediaItem.id, _rel_path().label("rel"))
                .filter(em.MediaItem.item_type.in_(["movie", "episode"]))
                .filter(em.MediaItem.merged_into_id.is_(None))
                .filter(func.lower(em.MediaItem.file_path).like("%.strm"))
                .filter(_stub_caliber())
                .filter(em.MediaItem.id > last_id)
                .order_by(em.MediaItem.id)
                .limit(args.batch_size)
                .all()
            )
            if not batch:
                break
            size_map = _ledger_size_map(db, [rel for _, rel in batch])
            n_batch = 0
            for row_id, rel in batch:
                last_id = row_id
                done += 1
                video_size = size_map.get(rel)
                if video_size:
                    if args.apply:
                        db.query(em.MediaItem).filter(
                            em.MediaItem.id == row_id).update(
                            {"size": int(video_size)},
                            synchronize_session=False)
                        n_batch += 1
                    fixed += 1
                else:
                    skipped += 1
            if args.apply:
                db.commit()
            el = time.time() - t0
            print(f"  …已扫描 {done}  可回填 {fixed}  无账本 {skipped}  "
                  f"本批写入 {n_batch}  用时 {el:.0f}s")
            if args.limit and done >= args.limit:
                break
    finally:
        db.close()

    print(f"[{mode}] 完成：共扫描 {done} 条，可回填 {fixed} 条，"
          f"无账本跳过 {skipped} 条")
    if not args.apply:
        print("dry-run 未写库；确认数字后加 --apply 真正执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

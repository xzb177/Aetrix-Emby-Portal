#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量大 MP4 的 moov 前置。默认不改任何文件。

新入库走扫描钩子（MP4_FASTSTART_ON_INGEST，默认关）。本脚本只处理已经在库里的
mp4/m4v/mov，而且要同时满足：

1. 环境变量 ``MP4_FASTSTART_BACKFILL=1``（默认 0）
2. 命令行带 ``--apply``

缺任何一个：打印说明后退出，不下载、不调用 ffmpeg、不改网盘、不改库。

示例::

    python scripts/mp4_faststart_backfill.py
    MP4_FASTSTART_BACKFILL=1 python scripts/mp4_faststart_backfill.py
    MP4_FASTSTART_BACKFILL=1 python scripts/mp4_faststart_backfill.py --apply --limit 3

失败的文件保留原件并记日志，继续下一条。生产验证要等运维打开开关并部署之后，
对一部大 MP4 看起播，不在本脚本里做。
"""
from __future__ import annotations

import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.emby_server.mp4_faststart import backfill_will_rewrite, run_backfill


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="存量大 MP4 faststart。默认不改任何文件。")
    parser.add_argument(
        "--apply", action="store_true",
        help="真正重写。缺省只统计候选，不改文件。")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="最多处理多少条。0 表示不额外截断。")
    args = parser.parse_args(argv)
    allowed, why = backfill_will_rewrite(apply=bool(args.apply))
    if why == "backfill_disabled":
        print("MP4_FASTSTART_BACKFILL 默认关（0）。未改任何文件。")
        return 0
    from backend.database import SessionLocal

    session = SessionLocal()
    try:
        summary = run_backfill(
            session, apply=bool(args.apply), limit=max(0, int(args.limit or 0)))
    finally:
        session.close()
    print(
        "candidates={candidates} ran={ran} reason={reason} "
        "processed={processed} rewritten={rewritten} kept={kept}".format(**summary))
    if not allowed:
        print("未传 --apply，未改任何文件。确认后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

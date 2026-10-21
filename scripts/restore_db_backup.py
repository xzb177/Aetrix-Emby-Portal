#!/usr/bin/env python3
"""SQLite 备份恢复脚本（只适用于 SQLite 的 *.db.gz；不恢复 PostgreSQL）。

恢复步骤：
  1. 先停掉应用容器（否则恢复中的文件替换可能与正在写的进程冲突）：
       docker compose stop aetrix-api aetrix-worker aetrix-ea
  2. 跑本脚本，按提示选一个备份恢复：
       python3 scripts/restore_db_backup.py [--db /data/aetrix_unified.db] [--yes]
  3. 重启容器：
       docker compose up -d

安全机制：恢复前会自动把**当前**数据库再备份一份
（backups/ 下以 pre-restore- 为前缀），手滑选错了还能救回来。
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 恢复前必须能读到这些表，否则恢复完应用一启动就崩
_KEY_TABLES = ("web_users", "emby_items")


def find_backups(backup_dir: Path) -> list[Path]:
    """可选备份列表。**排除 pre-restore- 安全备份**——那是上次恢复留下的兜底，
    不是常规备份，混进列表会让人误选。"""
    files = sorted(backup_dir.glob("*.db.gz"), key=lambda p: p.name, reverse=True)
    return [f for f in files if not f.name.startswith("pre-restore-")]


def gunzip_to(src_gz: Path, dst: Path) -> None:
    with gzip.open(src_gz, "rb") as f_in, open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def verify_sqlite(path: Path) -> tuple[bool, str]:
    """校验解压出来的库是否真的可用。返回 (ok, 说明)。

    旧实现只 ``SELECT name FROM sqlite_master LIMIT 1``——文件头和 sqlite_master
    根页都在第 1 页，一个被截断/位翻转的库只要第 1 页完好就能通过，
    然后被 replace() 装成正在用的生产库。故必须跑 integrity_check 并核对关键表。
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return False, f"无法打开：{exc}"
    try:
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
        except Exception as exc:  # noqa: BLE001
            return False, f"完整性检查无法执行：{exc}"
        if not row or str(row[0]).lower() != "ok":
            return False, f"完整性检查未通过：{row[0] if row else '无结果'}"
        # 关键表必须存在且能读，否则恢复完应用一启动就报错
        present = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in _KEY_TABLES if t not in present]
        if missing:
            return False, f"缺少关键表：{', '.join(missing)}"
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _KEY_TABLES}
    except Exception as exc:  # noqa: BLE001
        return False, f"读取关键表失败：{exc}"
    finally:
        con.close()
    summary = "，".join(f"{t} {n} 行" for t, n in counts.items())
    return True, f"完整性检查通过（{summary}）"


def online_backup(db_path: Path, out_gz: Path) -> None:
    """在线备份当前库到 out_gz（WAL 安全的真正备份）。

    这里**不能**用裸文件复制：生产库开着 WAL，主文件里只有已 checkpoint 的页，
    最近的事务还在 ``-wal`` 里。裸 cp 出来的"安全备份"可能比正式备份旧一整天——
    而这恰恰是手滑之后的救命兜底。必须走 ``Connection.backup()``。
    """
    tmp_db = out_gz.parent / f".safety-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.db"
    try:
        src = sqlite3.connect(str(db_path), timeout=30)
        try:
            dst = sqlite3.connect(str(tmp_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with open(tmp_db, "rb") as f_in, gzip.open(out_gz, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
    finally:
        tmp_db.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="从定时备份恢复 SQLite 数据库")
    ap.add_argument("--db", default="/data/aetrix_unified.db",
                    help="数据库文件路径（默认 /data/aetrix_unified.db）")
    ap.add_argument("--yes", action="store_true",
                    help="跳过二次确认（已用 --index 指定备份时可用）")
    ap.add_argument("--index", type=int, default=None,
                    help="直接指定要恢复的备份序号（从 1 开始，见列表）")
    args = ap.parse_args()

    db_path = Path(args.db)
    backup_dir = db_path.parent / "backups"
    if not backup_dir.exists():
        print(f"备份目录不存在：{backup_dir}", file=sys.stderr)
        return 1
    backups = find_backups(backup_dir)
    if not backups:
        print(f"没有找到备份文件（{backup_dir}/*.db.gz）", file=sys.stderr)
        return 1

    print("可用的备份（新的在前）：")
    for i, b in enumerate(backups, 1):
        size_mb = b.stat().st_size / 1024 / 1024
        print(f"  [{i}] {b.name}  ({size_mb:.1f} MB)")

    idx = args.index
    if idx is None:
        try:
            idx = int(input("输入要恢复的序号：").strip())
        except (ValueError, EOFError):
            print("已取消", file=sys.stderr)
            return 1
    if not 1 <= idx <= len(backups):
        print(f"序号非法：{idx}", file=sys.stderr)
        return 1
    chosen = backups[idx - 1]

    if not args.yes:
        print(f"\n即将用 {chosen.name} 覆盖 {db_path}")
        print("⚠️  请确认应用容器已停止（docker compose stop api worker ea），否则可能损坏数据。")
        confirm = input("输入 YES 继续：").strip()
        if confirm != "YES":
            print("已取消", file=sys.stderr)
            return 1

    # 1. 先把当前库在线备份一份（防手滑）。
    #    必须是 sqlite3 backup()，不能裸 cp——WAL 模式下裸 cp 拿到的是旧数据。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety_name = f"pre-restore-{stamp}.db.gz"
    if db_path.exists():
        try:
            online_backup(db_path, backup_dir / safety_name)
            print(f"当前库已在线备份为：{safety_name}")
        except Exception as exc:  # noqa: BLE001
            # 兜底拿不到就明说，不能让用户以为"已有安全备份"其实没有
            print(f"⚠️  当前库安全备份失败：{exc}", file=sys.stderr)
            print("    没有这份兜底，手滑就回不来了。建议先解决后重试。", file=sys.stderr)
            if not args.yes:
                try:
                    if input("仍要继续恢复？输入 YES 继续：").strip() != "YES":
                        print("已取消", file=sys.stderr)
                        return 1
                except (EOFError, KeyboardInterrupt):
                    print("已取消", file=sys.stderr)
                    return 1
    else:
        print(f"注意：{db_path} 当前不存在，将直接恢复（无安全备份）")

    # 2. 解压选中备份到临时文件并校验
    tmp_path = backup_dir / f".restore-{stamp}.db"
    try:
        gunzip_to(chosen, tmp_path)
        ok, detail = verify_sqlite(tmp_path)
        if not ok:
            print(f"备份文件校验失败：{detail}", file=sys.stderr)
            print("已中止，未动当前库。", file=sys.stderr)
            return 1
        print(f"备份校验：{detail}")
        # 3. 原子替换。
        #    WAL/SHM 必须清：残留的 -wal 会被拿去重放到新库上（不匹配的库 → 损坏）。
        #    顺序：先清 WAL/SHM（此刻旧库已安全备份过），再把旧库改名保留，最后换入新库。
        for suffix in ("-wal", "-shm"):
            p = db_path.with_name(db_path.name + suffix)
            p.unlink(missing_ok=True)
        old_keep = db_path.with_name(db_path.name + f".pre-restore-{stamp}")
        db_path.replace(old_keep)
        try:
            tmp_path.replace(db_path)
        except Exception:
            old_keep.replace(db_path)  # 换名失败就把旧库放回去
            raise
        print(f"原库已保留为：{old_keep.name}")
    finally:
        tmp_path.unlink(missing_ok=True)

    print(f"恢复完成：{chosen.name} -> {db_path}")
    print("请重启应用容器：docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

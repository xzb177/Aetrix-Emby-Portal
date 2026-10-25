#!/usr/bin/env python3
"""一次性回填：emby_items 的身份锚（source_id / provider_file_id）。

背景：清单第 8 项（挂载模型第 3 步）给条目加了身份锚两列；新入库由
扫描写库点写锚（scanner.py：file_id 双写 provider_file_id、来源行 id
写 source_id），存量 mount:// 老行两列为 NULL，播放走现行 mount://
解析并计回落（database.py 迁移注释）。本脚本是断旧路的第一道前置：
把存量老行的锚补上；本脚本不断旧路、不删任何代码与列。

推导规则（与入库口径、锚定位 resolve_anchor 逐字对齐）：
- source_id：parse_mount_path(file_path) → (mount_id, rel)；在
  library_sources 里找 mount_id 相同、library_id 与条目相同、
  is_enabled 非 False、sub_path 按目录边界前缀匹配 rel 的行，取最长
  sub_path；无此行 → 不可映射，只上报、不猜。
  （目录边界：rel == sub_path，或 rel 以 sub_path 去尾斜杠 + "/"
  开头；sub_path "/" 匹配挂载下全部路径。）
- provider_file_id：仅当解析出的挂载类型属于 Drive/rclone 家族且
  drive_file_id 非空时取 drive_file_id（入库双写同一 _fid，不是猜）；
  115 型挂载不从 drive_file_id 推导（115 fid 只在 #13 PR-3 开关开时
  双写，列值本身分不清是否双写过）；本机/未知类型不推导；空值留空。
- 幂等只填空：source_id 仅当 NULL 时写，provider_file_id 仅当 NULL
  或空串时写；已有值永不覆盖。重跑自动跳过已填行。
- 适用全部条目类型：series/season 结构行的 mount:// 路径同样可推导
  source_id（fid 留空 → source_only），与 scan 身份回填排除结构行
  不同——锚的来源映射不依赖文件身份。

统计口径：scanned / skipped_complete / source_filled / fid_filled /
both_filled / source_only / fid_only / unmapped（按原因分）。

用法（容器里，aetrix-api 与 aetrix-worker 都能连 DB）：

    docker exec aetrix-api python scripts/backfill_item_anchor.py
    docker exec aetrix-api python scripts/backfill_item_anchor.py --sample 20
    docker exec aetrix-api python scripts/backfill_item_anchor.py --apply --limit 1000

默认 dry-run（只统计、不落库）；--apply 才写库，且只填空。
"""
from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.mounts import parse_mount_path  # noqa: E402

#: provider_file_id 可从 drive_file_id 推导的挂载类型（入库双写同一 _fid）。
FID_DERIVABLE_TYPES = ("drive", "rclone")


def _norm_subpath(sub_path: str) -> str:
    """sub_path 归一化：去尾斜杠，根 "/" 保持原样。"""
    sp = sub_path or "/"
    if len(sp) > 1:
        sp = sp.rstrip("/")
    return sp or "/"


def _prefix_match(rel: str, sub_path: str) -> bool:
    """目录边界前缀匹配：rel == sub_path，或 rel 在 sub_path 目录之下。"""
    sp = _norm_subpath(sub_path)
    if sp == "/":
        return True
    return rel == sp or rel.startswith(sp + "/")


def derive_anchor(file_path, library_id, drive_file_id, source_rows,
                  mount_types) -> dict:
    """为一条存量行推导身份锚（纯函数，不碰 DB、不发请求）。

    source_rows：library_sources 行 dict 列表，键为 id / library_id /
    mount_id / sub_path / is_enabled。mount_types：{mount_id: mount_type}。

    返回 dict：source_id / provider_file_id（推导不出为 None）、
    source_reason / fid_reason（见下）、matched_row（命中的源行 dict
    或 None）、mount_type。
    source_reason：ok / not_mount_path / no_source_row / source_disabled /
    library_mismatch；fid_reason：ok / fid_empty / fid_115_unresolved /
    fid_type_unknown。
    """
    parsed = parse_mount_path(file_path or "")
    if parsed is None:
        return {"source_id": None, "provider_file_id": None,
                "source_reason": "not_mount_path", "fid_reason": "fid_empty",
                "matched_row": None, "mount_type": None}
    mount_id, rel = parsed
    mount_type = mount_types.get(mount_id)
    # 同库 + 目录前缀的候选行（先不看 is_enabled，用于原因细分）。
    prefix_rows = [r for r in source_rows
                   if r.get("mount_id") == mount_id
                   and r.get("library_id") == library_id
                   and _prefix_match(rel, r.get("sub_path") or "/")]
    enabled_rows = [r for r in prefix_rows if r.get("is_enabled") is not False]
    if enabled_rows:
        matched = max(enabled_rows, key=lambda r: len(_norm_subpath(r.get("sub_path") or "/")))
        source_id = matched["id"]
        source_reason = "ok"
    elif prefix_rows:
        matched = None
        source_id = None
        source_reason = "source_disabled"
    else:
        # 同挂载下有别库的前缀行 → 配置重叠诊断，不取别库的行（定位会拒）。
        shadowed = any(r.get("mount_id") == mount_id
                       and _prefix_match(rel, r.get("sub_path") or "/")
                       for r in source_rows)
        matched = None
        source_id = None
        source_reason = "library_mismatch" if shadowed else "no_source_row"
    fid = (drive_file_id or "").strip() if drive_file_id else ""
    if not fid:
        fid_value, fid_reason = None, "fid_empty"
    elif mount_type in FID_DERIVABLE_TYPES:
        fid_value, fid_reason = fid, "ok"
    elif mount_type in ("115", "pan115"):
        fid_value, fid_reason = None, "fid_115_unresolved"
    else:
        fid_value, fid_reason = None, "fid_type_unknown"
    return {"source_id": source_id, "provider_file_id": fid_value,
            "source_reason": source_reason, "fid_reason": fid_reason,
            "matched_row": matched, "mount_type": mount_type}


def backfill_item_anchor(session, *, batch_size: int = 500,
                         limit: int = 0, dry_run: bool = True,
                         sample: int = 0) -> dict:
    """回填缺锚的 mount:// 条目，返回统计。

    幂等只填空：source_id 仅当 NULL 时写，provider_file_id 仅当 NULL
    或空串时写；已有值永不覆盖。dry_run 时只统计、不落库（每批 rollback）。
    sample > 0 时在 dry_run 下打印前 sample 条推导链。
    """
    source_rows = [{"id": r.id, "library_id": r.library_id,
                    "mount_id": r.mount_id, "sub_path": r.sub_path,
                    "is_enabled": r.is_enabled}
                   for r in session.query(em.LibrarySource).all()]
    mount_types = {m.id: (getattr(m, "mount_type", "") or "")
                   for m in session.query(em.StorageMount).all()}
    stats = {"scanned": 0, "skipped_complete": 0, "source_filled": 0,
             "fid_filled": 0, "both_filled": 0, "source_only": 0,
             "fid_only": 0, "unmapped": 0, "unmapped_by_reason": {}}
    samples: list[dict] = []
    last_id = 0
    written = 0
    while True:
        want = batch_size if limit <= 0 else min(batch_size, limit - written)
        if want <= 0:
            break
        rows = (
            session.query(em.MediaItem)
            .filter(
                em.MediaItem.id > last_id,
                em.MediaItem.file_path.isnot(None),
                em.MediaItem.file_path.like("mount://%"),
                em.MediaItem.deleted_at.is_(None),
                em.MediaItem.merged_into_id.is_(None),
            )
            .order_by(em.MediaItem.id)
            .limit(want)
            .all()
        )
        if not rows:
            break
        for item in rows:
            stats["scanned"] += 1
            last_id = item.id
            has_source = item.source_id is not None
            has_fid = bool((item.provider_file_id or "").strip())
            if has_source and has_fid:
                stats["skipped_complete"] += 1
                continue
            derived = derive_anchor(item.file_path, item.library_id,
                                    item.drive_file_id, source_rows,
                                    mount_types)
            new_source = derived["source_id"] if not has_source else None
            new_fid = derived["provider_file_id"] if not has_fid else None
            if sample > 0 and len(samples) < sample and dry_run:
                samples.append({
                    "id": item.id, "item_type": item.item_type,
                    "library_id": item.library_id,
                    "file_path": item.file_path,
                    "drive_file_id": item.drive_file_id,
                    "matched_row": derived["matched_row"],
                    "mount_type": derived["mount_type"],
                    "proposed_source_id": new_source,
                    "proposed_provider_file_id": new_fid,
                    "source_reason": derived["source_reason"],
                    "fid_reason": derived["fid_reason"],
                })
            if new_source is None and new_fid is None:
                stats["unmapped"] += 1
                reason = (derived["source_reason"]
                          if derived["source_id"] is None
                          else derived["fid_reason"])
                stats["unmapped_by_reason"][reason] = (
                    stats["unmapped_by_reason"].get(reason, 0) + 1)
                continue
            if new_source is not None and new_fid is not None:
                stats["both_filled"] += 1
            elif new_source is not None:
                stats["source_only"] += 1
            else:
                stats["fid_only"] += 1
            stats["source_filled"] += 1 if new_source is not None else 0
            stats["fid_filled"] += 1 if new_fid is not None else 0
            if not dry_run:
                if new_source is not None:
                    item.source_id = new_source
                if new_fid is not None:
                    item.provider_file_id = new_fid
                written += 1
        if dry_run:
            session.rollback()
        else:
            session.commit()
        print(f"  …已扫描 {stats['scanned']} 条、已填 {written} 条、"
              f"未映射 {stats['unmapped']} 条（last_id={last_id}）")
        if limit > 0 and written >= limit:
            break
    for s in samples:
        print(f"  样例 id={s['id']} type={s['item_type']} lib={s['library_id']} "
              f"path={s['file_path']} fid={s['drive_file_id']} "
              f"mount_type={s['mount_type']} matched_row={s['matched_row']} "
              f"→ source={s['proposed_source_id']} "
              f"provider_file_id={s['proposed_provider_file_id']} "
              f"({s['source_reason']}/{s['fid_reason']})")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 emby_items 的身份锚")
    parser.add_argument("--apply", action="store_true", help="写库（默认只试跑不写）")
    parser.add_argument("--limit", type=int, default=0, help="最多写库条数（0=不限）")
    parser.add_argument("--batch-size", type=int, default=500, help="每批条数")
    parser.add_argument("--sample", type=int, default=0, help="试跑下打印推导链样例条数")
    args = parser.parse_args()
    session = SessionLocal()
    try:
        stats = backfill_item_anchor(
            session, batch_size=args.batch_size, limit=args.limit,
            dry_run=not args.apply, sample=args.sample)
    finally:
        session.close()
    print(f"完成（dry_run={not args.apply}）：{stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

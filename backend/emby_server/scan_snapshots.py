"""按源维度的目录扫描快照（任务清单第 11 项：扫描目录指纹）。

FMBY ``directory_scan_snapshots`` 形态：``library_source_id``、
``directory_path``、``listing_fingerprint``、``entry_count``、
``last_scan_at``、``last_refresh_at``、``next_due_at``。

与旧 ``ScanDirState``（``emby_scan_dirs``，``library_id + dir_key``）的
分工（不许造两套增量互相打架）：
- 判定语义同一套：first / changed / due / not_due，周期复用
  ``SCAN_DIR_RECHECK_SEC``，总开关复用 ``SCAN_DIR_DUE_SCHEDULING``；
  本模块不定义第二套周期、不定义第二套开关（横切只许一套）。
- 快照是按源维度的权威记录：同一库多源时，各源目录互不干扰；
  旧表继续双写，只作为解析不到源行的目录的回落。
- fail-closed：开关关闭、源行解析不到、读表失败、指纹拿不到时，
  一律走旧路径，行为与今天逐字一致；快照层永远不剪枝枚举，
  ``seen_guids`` 永远完整——指纹对不上只影响处理深度，不碰清理
  （清理仍走 #1201 缺失宽限 + 清单守卫，见 scanner）。

源行解析的稳定性说明：我方管理端保存库时删掉源行重插（源行 id
会变），只凭源 id 查快照会在每次保存后 orphan；因此快照多带一列
``library_id``，按（库，路径）查到旧行时认领（把源 id 更新为当前
行），快照不因保存而丢。同一库同一路径在任一时刻只归一个源
（最长前缀匹配），两道唯一约束不冲突。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from backend.emby_server import models as em

logger = logging.getLogger(__name__)


def snapshots_enabled() -> bool:
    """按源快照层总开关（默认开）：关掉后扫描只走旧 ScanDirState 路径。

    运行时读环境变量，供测试中途开关（与 SCAN_DIR_DUE_SCHEDULING 同口径）。
    """
    return (os.getenv("SCAN_SOURCE_SNAPSHOT_ENABLED", "1") or "1").strip().lower() \
        not in {"0", "false", "no", "off"}


def decide_snapshot(stored: Optional[tuple], fingerprint: str,
                    entry_count: Optional[int], now: datetime) -> str:
    """快照判定（纯函数，无 IO）：first / changed / due / not_due。

    ``stored`` 是 (指纹, 项数, next_due_at) 或 None（无快照行）。
    判定顺序是 fail-closed 的：指纹对不上、项数对不上，统统算 changed
    （完整处理），绝不因为“看起来差不多”就跳过；只有指纹与项数都
    对上、且未到期，才算 not_due（整段跳过逐文件核对）。
    """
    if stored is None:
        return "first"
    stored_fp, stored_count, next_due_at = stored
    if stored_fp != fingerprint:
        return "changed"
    if entry_count is not None and stored_count is not None \
            and stored_count != entry_count:
        return "changed"
    if next_due_at is None or next_due_at <= now:
        return "due"
    return "not_due"


def _norm_sub_path(sub_path: str) -> str:
    """源行 sub_path 归一化：保证前导 /、根为 /、其余去尾部 /。"""
    if not sub_path:
        return "/"
    path = sub_path if sub_path.startswith("/") else "/" + sub_path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _path_prefix_match(sub_path: str, path: str) -> bool:
    """sub_path 是不是 path 的目录前缀（按段匹配，不许 /A 匹配 /AB）。"""
    sub = _norm_sub_path(sub_path)
    if sub == "/":
        return True
    return path == sub or path.startswith(sub + "/")


def resolve_source_id(source_rows: list, dir_key: str) -> Optional[int]:
    """目录 dir_key → 所属源行 id（最长前缀匹配，纯函数）。

    ``source_rows`` 是 [(id, mount_id, sub_path)]。挂载目录
    ``mount://<id>/<rel>`` 只匹配同 mount_id 的行；本机绝对路径只匹配
    mount_id 为空的行；无匹配回 None（调用方走旧路径）。
    """
    if dir_key.startswith("mount://"):
        rest = dir_key[len("mount://"):]
        mount_part, _, rel = rest.partition("/")
        try:
            mount_id = int(mount_part)
        except ValueError:
            return None
        rel_path = "/" + rel if rel else "/"
        best = None
        for row_id, row_mount_id, sub_path in source_rows:
            if row_mount_id != mount_id:
                continue
            sub = _norm_sub_path(sub_path)
            if _path_prefix_match(sub, rel_path):
                if best is None or len(sub) > len(best[1]):
                    best = (row_id, sub)
        return best[0] if best else None
    best = None
    for row_id, row_mount_id, sub_path in source_rows:
        if row_mount_id is not None:
            continue
        sub = _norm_sub_path(sub_path)
        if _path_prefix_match(sub, dir_key):
            if best is None or len(sub) > len(best[1]):
                best = (row_id, sub)
    return best[0] if best else None


def load_source_rows(db, library_id: int) -> Optional[list]:
    """库的源行（id, mount_id, sub_path），读失败回 None（调用方走旧路径）。"""
    if db is None:
        return None
    try:
        rows = db.query(em.LibrarySource.id, em.LibrarySource.mount_id,
                        em.LibrarySource.sub_path).filter(
            em.LibrarySource.library_id == library_id).all()
    except Exception as exc:  # noqa: BLE001 — 表缺失/旧夹具都回落旧路径
        logger.warning("库 id=%s 源行读取失败，快照层回落旧路径：%s", library_id, exc)
        return None
    return [(row.id, row.mount_id, row.sub_path) for row in rows]


def load_snapshots(db, library_id: int, directory_paths: list) -> dict:
    """一批目录的快照（分片查）：directory_path → (指纹, 项数, next_due_at)。

    只按（库，路径）查——源行重插后旧行仍能被认领，认领发生在写回时。
    """
    found: dict = {}
    unique = list(dict.fromkeys(p for p in directory_paths if p))
    if not unique:
        return found
    try:
        rows = db.query(em.DirectoryScanSnapshot).filter(
            em.DirectoryScanSnapshot.library_id == library_id,
            em.DirectoryScanSnapshot.directory_path.in_(unique)).all()
    except Exception as exc:  # noqa: BLE001 — 表缺失/旧夹具都回落旧路径
        logger.warning("库 id=%s 快照读取失败，回落旧路径：%s", library_id, exc)
        return found
    for row in rows:
        found[row.directory_path] = (row.listing_fingerprint, row.entry_count,
                                     row.next_due_at)
    return found


def store_snapshots(db, library_id: int, entries: dict, now: datetime) -> None:
    """把本批快照写回（与条目在同一个事务里提交）。

    ``entries``：directory_path → (source_id, fingerprint, entry_count,
    decision, due_at)。语义：
    - first / changed / due：完整核对过 → 指纹、项数、last_scan_at、
      last_refresh_at、next_due_at 全推进；
    - not_due：只见过清单、未做完整核对 → 只推进 last_scan_at，
      next_due_at 与 last_refresh_at 保持原值（不许把排期越推越远）。
    - 同库同路径的旧源行（管理端保存后源 id 变了）：认领——更新源 id，
      不另起一行（两道唯一约束都不许被顶破）。
    """
    if not entries:
        return
    paths = list(entries.keys())
    existing: dict = {}
    try:
        for row in db.query(em.DirectoryScanSnapshot).filter(
                em.DirectoryScanSnapshot.library_id == library_id,
                em.DirectoryScanSnapshot.directory_path.in_(paths)).all():
            existing[row.directory_path] = row
    except Exception as exc:  # noqa: BLE001 — 写回失败不拦扫描提交
        logger.warning("库 id=%s 快照写回查询失败，跳过本批快照写回：%s",
                       library_id, exc)
        return
    for path, (source_id, fingerprint, entry_count, decision, due_at) in entries.items():
        if fingerprint is None or source_id is None:
            continue
        row = existing.get(path)
        if row is None:
            row = em.DirectoryScanSnapshot(
                library_source_id=source_id, library_id=library_id,
                directory_path=path, listing_fingerprint=fingerprint,
                entry_count=entry_count or 0, last_scan_at=now,
                last_refresh_at=now if decision != "not_due" else None,
                next_due_at=due_at, updated_at=now)
            db.add(row)
            continue
        if row.library_source_id != source_id:
            row.library_source_id = source_id  # 认领：源行重插后的旧快照
        row.listing_fingerprint = fingerprint
        row.entry_count = entry_count or 0
        row.last_scan_at = now
        if decision != "not_due":
            row.last_refresh_at = now
            row.next_due_at = due_at
        row.updated_at = now

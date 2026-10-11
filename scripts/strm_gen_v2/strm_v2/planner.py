"""规划层：对比清单与状态，生成任务计划。

D-INCR-1: 变更判定只看 (file_id, md5, size) 三元组，mtime 完全不参与。
D-INCR-2: 移动/改名与内容变更分开处理。
"""

from __future__ import annotations

import logging
import os
from typing import Iterator, Optional

from .models import FileRecord, FileState, Plan, PlanItem, DirtyHit
from .rules_engine import RulesEngine
from .state_db import StateDB

log = logging.getLogger(__name__)


def strm_relpath(drive_path: str, strip_prefix: str) -> str:
    """Drive 相对路径 → .strm 相对路径（去前缀 + 换扩展名）。

    P1 修复：规范化路径，拒绝含 .. 的遍历。
    """
    rel = drive_path
    if strip_prefix and rel.startswith(strip_prefix):
        rel = rel[len(strip_prefix):]
    # 规范化并拦截遍历
    rel = os.path.normpath(rel)
    if rel.startswith("..") or os.path.isabs(rel):
        raise ValueError(f"非法路径（遍历）: {drive_path}")
    base, _ = os.path.splitext(rel)
    return base + ".strm"


class Planner:
    """diff 规划器。"""

    def __init__(self, state: StateDB, rules: Optional[RulesEngine] = None,
                 strip_prefix: str = "MoviePilot/"):
        self._state = state
        self._rules = rules
        self._strip = strip_prefix

    def classify(self, record: FileRecord,
                 old: Optional[FileState]) -> tuple[str, str]:
        """返回 (action, reason)。

        D-INCR-1: 只看 (file_id, md5, size)，mtime 不参与决策。
        md5 缺失时回退到 (size, mtime) 弱指纹并打标 reason。
        """
        if old is None:
            return "create", "新增文件"
        if old.path != record.path:
            # D-INCR-2: 纯移动/改名，内容没变
            return "move", f"移动/改名: {old.path} → {record.path}"
        # 内容变更判定
        if record.md5 and old.md5:
            if (record.md5, record.size) != (old.md5, old.size):
                return "update", "内容变更 (md5/size)"
        elif record.md5 != old.md5 or record.size != old.size:
            # 一方缺 md5：按 size 变严格判定
            if record.size != old.size:
                return "update", "内容变更 (size)"
            # md5 从无到有：回填，不重生成
            if old.md5 is None and record.md5:
                return "skip", "md5 回填（仅更新状态）"
        # mtime 变了但指纹不变 → 跳过（事故 #1 根因修复）
        return "skip", "指纹未变，跳过（mtime 抖动忽略）"

    def plan(self, records: Iterator[FileRecord],
             shard_filter: Optional[set[str]] = None,
             is_full: bool = False) -> Plan:
        """生成完整 Plan。

        is_full=True 时（全量模式），结束后做 ORPHAN 检测。
        """
        plan = Plan()
        seen_ids: set[str] = set()

        for record in records:
            # Changes API 删除事件：path 为空
            if not record.path:
                old = self._state.get(record.file_id)
                if old:
                    plan.items.append(PlanItem(
                        action="delete", record=None,
                        old_strm_path=old.strm_path,
                        reason="Drive 侧已删除",
                    ))
                continue

            if shard_filter and record.shard not in shard_filter:
                continue
            seen_ids.add(record.file_id)

            old = self._state.get(record.file_id)
            action, reason = self.classify(record, old)

            # 脏数据规则检查
            if self._rules:
                hits = self._rules.check(record)
                for hit in hits:
                    plan.dirty.append(hit)
                    log.info("脏数据 [%s]: %s", hit.rule_id, hit.path)

            new_strm = strm_relpath(record.path, self._strip)
            if action == "create":
                plan.items.append(PlanItem(
                    action="create", record=record,
                    new_strm_path=new_strm, reason=reason))
            elif action == "update":
                plan.items.append(PlanItem(
                    action="update", record=record,
                    old_strm_path=old.strm_path if old else None,
                    new_strm_path=new_strm, reason=reason))
            elif action == "move":
                plan.items.append(PlanItem(
                    action="move", record=record,
                    old_strm_path=old.strm_path if old else None,
                    new_strm_path=new_strm, reason=reason))
            # skip 不进 Plan

        # ORPHAN 检测：仅全量模式可靠；增量模式跳过（Changes 已处理删除事件）
        if is_full:
            for orphan_item in self.detect_orphans(seen_ids):
                plan.items.append(orphan_item)
                plan.dirty.append(DirtyHit(
                    rule_id="ORPHAN_STRM",
                    path=orphan_item.old_strm_path or "",
                    detail="本地有 .strm 但 Drive 已无对应文件",
                ))
        return plan

    def detect_orphans(self, seen_ids: set[str]) -> list[PlanItem]:
        """全量模式后调用：StateDB 中有、清单中没有的 → orphan 删除项。

        返回 delete 类型的 PlanItem 列表。
        """
        orphans = []
        for fid in self._state.all_file_ids():
            if fid not in seen_ids:
                old = self._state.get(fid)
                if old:
                    orphans.append(PlanItem(
                        action="delete", record=None,
                        old_strm_path=old.strm_path,
                        reason=f"ORPHAN_STRM: Drive 已无此文件（path={old.path})",
                    ))
        return orphans

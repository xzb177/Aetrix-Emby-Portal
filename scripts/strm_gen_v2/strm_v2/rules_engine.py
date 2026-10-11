"""规则引擎：脏数据结构判定。

D-DIRTY-1: 规则用声明式 YAML 表达，新增脏类型不需要改代码。
D-DIRTY-2: action 分级 report/normalize/ignore，默认全 report。
D-DIRTY-3: 归一化只改变 .strm 侧镜像路径，绝不写回 Drive。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import yaml

from .models import DirtyHit, FileRecord

log = logging.getLogger(__name__)


class Rule:
    """单条脏数据规则。"""

    def __init__(self, cfg: dict):
        self.id: str = cfg["id"]
        self.enabled: bool = cfg.get("enabled", True)
        self.description: str = cfg.get("description", "")
        self.match: dict = cfg.get("match", {})
        self.action: str = cfg.get("action", "report")
        self.normalize_target: str = cfg.get("normalize_target", "")

        # 预编译正则
        self._name_rx = None
        self._parent_rx = None
        if "name_pattern" in self.match:
            self._name_rx = re.compile(self.match["name_pattern"])
        if "parent_pattern" in self.match:
            self._parent_rx = re.compile(self.match["parent_pattern"])
        self._negate_parent = self.match.get("negate_parent", False)

    def matches(self, record: FileRecord, is_empty_dir: bool = False,
                is_dir: bool = False) -> Optional[str]:
        """命中返回 detail，否则 None。"""
        if not self.enabled:
            return None
        mtype = self.match.get("type", "file")
        if mtype == "orphan":
            return None  # orphan 由 planner 单独处理
        if mtype == "dir" and not is_dir:
            return None
        if mtype == "file" and is_dir:
            return None

        if self.match.get("empty") and not is_empty_dir:
            return None

        target = record.name if not is_dir else os.path.basename(record.path)
        if self._name_rx and not self._name_rx.search(target):
            return None

        if self._parent_rx:
            parent = os.path.dirname(record.path)
            hit = bool(self._parent_rx.search(parent))
            if self._negate_parent:
                hit = not hit
            if not hit:
                return None

        return f"规则 {self.id}: {self.description} | 路径={record.path}"


class RulesEngine:
    """YAML 规则引擎。"""

    def __init__(self, rules_path: str, mirror_mode: str = "strict"):
        self._rules: list[Rule] = []
        self._mode = mirror_mode
        if os.path.exists(rules_path):
            with open(rules_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for rcfg in data.get("rules", []):
                try:
                    self._rules.append(Rule(rcfg))
                except Exception as e:
                    log.warning("规则加载失败 %s: %s", rcfg.get("id"), e)
        log.info("加载脏数据规则 %d 条（mirror_mode=%s）",
                 len(self._rules), mirror_mode)

    def check(self, record: FileRecord) -> list[DirtyHit]:
        """检查单条记录，返回命中列表。"""
        hits = []
        for rule in self._rules:
            detail = rule.matches(record)
            if not detail:
                continue
            auto = False
            suggestion = ""
            # D-DIRTY-2: 只有 mirror_mode=normalize 且 rule action 含 normalize 才自动归一化
            if (self._mode == "normalize"
                    and "normalize" in rule.action
                    and rule.normalize_target):
                suggestion = self._render_target(rule, record)
                auto = True
                # D-DIRTY-3: 只影响 .strm 侧路径，不写回 Drive
            hits.append(DirtyHit(
                rule_id=rule.id,
                path=record.path,
                detail=detail,
                suggestion=suggestion,
                auto_normalized=auto,
            ))
        return hits

    def _render_target(self, rule: Rule, record: FileRecord) -> str:
        """渲染归一化目标路径（尽力而为）。"""
        try:
            # 提取 SxxEyy 中的季号
            m = re.search(r"(?i)s(\d{1,2})e\d{1,2}", record.name)
            season = m.group(1) if m else "1"
            parent = os.path.dirname(record.path)
            return rule.normalize_target.format(
                parent=parent, season_num=season.lstrip("0") or "1",
                name=record.name,
            )
        except Exception:
            return ""

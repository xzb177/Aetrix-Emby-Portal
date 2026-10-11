"""报告层：脏数据报告 / 校验报告 / 运行报告。

JSON + 人可读摘要，保留可配置天数。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

from .models import DirtyHit, Plan, VerifyResult

log = logging.getLogger(__name__)


class Reporter:
    """报告生成器。"""

    def __init__(self, report_dir: str, keep_days: int = 30):
        self._dir = report_dir
        self._keep = keep_days
        os.makedirs(report_dir, exist_ok=True)

    def _stamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    def _write(self, name: str, data: dict[str, Any]) -> str:
        path = os.path.join(self._dir, f"{name}_{self._stamp()}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def dirty_report(self, hits: list[DirtyHit]) -> str:
        """脏数据报告。"""
        by_rule: dict[str, list] = {}
        for h in hits:
            by_rule.setdefault(h.rule_id, []).append({
                "path": h.path, "detail": h.detail,
                "suggestion": h.suggestion,
                "auto_normalized": h.auto_normalized,
            })
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": len(hits),
            "by_rule": {k: {"count": len(v), "items": v}
                        for k, v in by_rule.items()},
        }
        path = self._write("dirty_report", data)
        # 人可读摘要
        lines = [f"脏数据报告: 共 {len(hits)} 条命中"]
        for rule_id, items in by_rule.items():
            lines.append(f"  [{rule_id}] {len(items)} 条")
            for it in items["items"][:5]:
                lines.append(f"    - {it['path']}")
            if len(items["items"]) > 5:
                lines.append(f"    ... 还有 {len(items['items'])-5} 条")
        log.info("\n".join(lines))
        return path

    def verify_report(self, results: list[VerifyResult]) -> str:
        """校验报告。"""
        failed = [r for r in results if not r.passed]
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": len(results),
            "passed": len(results) - len(failed),
            "failed": [
                {"strm_path": r.strm_path, "level": r.level,
                 "detail": r.detail, "checked_at": r.checked_at}
                for r in failed
            ],
        }
        path = self._write("verify_report", data)
        log.info("校验报告: %d 通过, %d 失败（已打标 verify_failed，未删除）",
                 data["passed"], len(failed))
        return path

    def run_report(self, stats: dict[str, Any]) -> str:
        """运行报告。"""
        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            **stats,
        }
        path = self._write("run_report", data)
        log.info("运行报告: %s", json.dumps(stats, ensure_ascii=False))
        return path

    def plan_summary(self, plan: Plan) -> str:
        """Plan 人可读摘要（dry-run 时用）。"""
        lines = ["Plan 摘要:"]
        for action in ("create", "update", "move", "delete", "skip"):
            items = plan.by_action(action)
            if items:
                lines.append(f"  {action}: {len(items)}")
                for it in items[:10]:
                    lines.append(
                        f"    - {it.new_strm_path or it.old_strm_path}"
                        f" ({it.reason})")
                if len(items) > 10:
                    lines.append(f"    ... 还有 {len(items)-10} 条")
        if plan.dirty:
            lines.append(f"  脏数据: {len(plan.dirty)} 条命中（见脏数据报告）")
        summary = "\n".join(lines)
        log.info(summary)
        return summary

    def cleanup_old(self) -> int:
        """清理过期报告。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._keep)
        removed = 0
        for fn in os.listdir(self._dir):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(self._dir, fn)
            try:
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(fp), tz=timezone.utc)
                if mtime < cutoff:
                    os.remove(fp)
                    removed += 1
            except OSError:
                pass
        if removed:
            log.info("清理过期报告 %d 个", removed)
        return removed

"""
管理后台 · 一键体检 API（阶段一：只读诊断；阶段二：自动修复）

- ``GET /api/admin/diagnosis/run``：跑一轮只读诊断，返回大白话报告
  （每条 finding 带 ``fixable`` 标记）。
- ``GET /api/admin/diagnosis/rules``：规则清单、当前阈值、可修复动作清单
  （``fixes``）、不可自动修的原因（``manual_only``）。
- ``POST /api/admin/diagnosis/fix``：执行单条修复，body
  ``{"rule_id": "...", "confirm": true}``。
  白名单制：只有 ``diagnosis_fix.FIXES`` 登记的规则能执行；未知规则 /
  不可修复 / 缺二次确认 → 400 结构化拒绝；修复函数内部异常 → 200
  ``{"ok": false, "error": "fix_failed"}``，不抛异常拖死主流程。
  成功 / 失败 / 被拒绝 **每次都写审计日志**（走 ``admin_core._audit``）。

鉴权口径与管理端完全一致：复用 ``get_current_admin`` + JWT。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import diagnosis, diagnosis_fix
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_diagnosis_router = APIRouter(prefix="/api/admin", tags=["管理后台·一键体检"])


@admin_diagnosis_router.get("/diagnosis/rules")
def list_diagnosis_rules(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """体检规则清单、当前生效阈值、可修复动作（阈值可配：DIAG_* 环境变量）。"""
    return {
        "rules": diagnosis.RULES,
        "thresholds": {rid: diagnosis.get_threshold(rid)
                       for rid in diagnosis.THRESHOLD_DEFS},
        "levels": list(diagnosis.LEVELS),
        "fixes": diagnosis_fix.list_fixes(),
        "fix_thresholds": {name: diagnosis_fix.get_fix_threshold(name)
                           for name in diagnosis_fix.FIX_THRESHOLD_DEFS},
        "manual_only": {rid: info.get("reason", "")
                        for rid, info in diagnosis_fix.MANUAL_ONLY.items()},
    }


@admin_diagnosis_router.get("/diagnosis/run")
def run_diagnosis_report(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """跑一轮只读体检，返回大白话诊断报告（finding 带 fixable 标记）。"""
    report = diagnosis.run_diagnosis(db)
    for f in report.get("findings", []):
        f["fixable"] = diagnosis_fix.is_fixable(f.get("rule_id", ""))
    return report


class DiagnosisFixRequest(BaseModel):
    rule_id: str = ""
    confirm: bool = False


def _reject(db, current_admin, rule_id: str, error: str, message: str,
            status: int = 400, extra: dict | None = None):
    """拒绝一次修复请求：先写审计，再抛 400 结构化错误。"""
    details = {"rule_id": rule_id, "ok": False, "error": error}
    if extra:
        details.update(extra)
    try:
        _audit(db, current_admin, "diagnosis.fix", "diagnosis_rule", None, details)
    except Exception:  # noqa: BLE001 — 审计失败不掩盖拒绝本身
        logger.warning("体检修复审计失败 rule=%s", rule_id, exc_info=True)
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — 审计落盘失败不掩盖拒绝本身
        logger.warning("体检修复审计落盘失败 rule=%s error=%s", rule_id, error,
                       exc_info=True)
        db.rollback()
    raise HTTPException(status_code=status, detail={"ok": False, "error": error,
                                                   "message": message,
                                                   **(extra or {})})


@admin_diagnosis_router.post("/diagnosis/fix")
def fix_diagnosis_rule(
    body: DiagnosisFixRequest,
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """执行单条诊断修复（白名单制 + 二次确认 + 审计）。

    - 未知 rule_id → 400 ``unknown_rule``
    - 规则不在修复白名单 → 400 ``not_fixable``（附手动步骤）
    - ``confirm`` 缺失/为 false → 400 ``confirm_required``
    - 修复函数内部异常 → 200 ``{"ok": false, "error": "fix_failed"}``
    """
    rule_id = (body.rule_id or "").strip()

    known_ids = {r["rule_id"] for r in diagnosis.RULES}
    if not rule_id or rule_id not in known_ids:
        _reject(db, current_admin, rule_id, "unknown_rule",
                f"未知规则：{rule_id or '(空)'}，本次体检没有这一项。")

    spec = diagnosis_fix.get_fix(rule_id)
    if spec is None:
        info = diagnosis_fix.MANUAL_ONLY.get(rule_id, {})
        _reject(db, current_admin, rule_id, "not_fixable",
                f"「{rule_id}」不支持一键自动修复，请按手动步骤处理。",
                extra={"reason": info.get("reason", ""),
                       "manual_steps": info.get("steps", [])})

    if not body.confirm:
        _reject(db, current_admin, rule_id, "confirm_required",
                "需要二次确认：请在确认弹窗中明确同意后再执行修复。")

    # 白名单内的修复动作：异常只记日志 + 结构化返回，不抛。
    try:
        result = spec.run(db)
        ok, error, message = True, "", ""
    except Exception as exc:  # noqa: BLE001 — 修复失败不拖死主流程
        logger.warning("体检修复执行失败 rule=%s", rule_id, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        result = {}
        ok, error, message = False, "fix_failed", f"修复执行失败：{exc}"

    payload = {"ok": ok, "rule_id": rule_id, "name": spec.name}
    if ok:
        payload["result"] = result
    else:
        payload["error"] = error
        payload["message"] = message

    try:
        _audit(db, current_admin, "diagnosis.fix", "diagnosis_rule", None,
               {"rule_id": rule_id, "ok": ok,
                "result": result if ok else None, "error": error or None})
    except Exception:  # noqa: BLE001 — 审计失败不掩盖修复结果
        logger.warning("体检修复审计失败 rule=%s", rule_id, exc_info=True)
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("体检修复审计落盘失败 rule=%s", rule_id, exc_info=True)
        db.rollback()
    return payload

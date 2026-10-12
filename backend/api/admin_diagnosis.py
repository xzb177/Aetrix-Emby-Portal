"""
管理后台 · 一键体检 API（阶段一：只读诊断）

- ``GET /api/admin/diagnosis/run``：跑一轮只读诊断，返回大白话报告。
- ``GET /api/admin/diagnosis/rules``：列出本次体检会检查哪些项及当前阈值。

只读保证：两个端点都是 GET，不写库、不改配置、不重启服务，
不需要审计日志（审计门禁只覆盖写端点）。

鉴权口径与管理端完全一致：复用 ``get_current_admin`` + JWT。
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend import diagnosis
from backend.api.admin_core import get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_diagnosis_router = APIRouter(prefix="/api/admin", tags=["管理后台·一键体检"])


@admin_diagnosis_router.get("/diagnosis/rules")
def list_diagnosis_rules(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """列出体检规则清单及当前生效阈值（阈值可配：DIAG_* 环境变量）。"""
    return {
        "rules": diagnosis.RULES,
        "thresholds": {rid: diagnosis.get_threshold(rid)
                       for rid, _ in diagnosis.THRESHOLD_DEFS},
        "levels": list(diagnosis.LEVELS),
    }


@admin_diagnosis_router.get("/diagnosis/run")
def run_diagnosis_report(
    current_admin=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """跑一轮只读体检，返回大白话诊断报告。"""
    return diagnosis.run_diagnosis(db)

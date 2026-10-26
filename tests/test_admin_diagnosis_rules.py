# -*- coding: utf-8 -*-
"""一键体检 · rules 口回归测试（管理后台清理批 0，M1）。

回归点：GET /api/admin/diagnosis/rules 曾因
``for rid, _ in diagnosis.THRESHOLD_DEFS`` 必 500——迭代字典产出的是键
字符串（"disk_warn_pct" 等），解包成两变量抛
``ValueError: too many values to unpack``。修复后改为 ``for rid in …``。

本测试直接调端点函数体：函数体内未使用 current_admin/db 入参
（鉴权由 FastAPI Depends 在路由层解决），故直接调用即可覆盖该 bug；
修前此调用必抛 ValueError。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import diagnosis
from backend.api import admin_diagnosis


def test_list_diagnosis_rules_thresholds_cover_all_defs():
    result = admin_diagnosis.list_diagnosis_rules()
    assert set(result["thresholds"]) == set(diagnosis.THRESHOLD_DEFS)
    for rid, value in result["thresholds"].items():
        assert isinstance(value, (int, float))
        assert value == diagnosis.get_threshold(rid)


def test_list_diagnosis_rules_shape():
    result = admin_diagnosis.list_diagnosis_rules()
    assert result["rules"] == diagnosis.RULES
    assert result["levels"] == list(diagnosis.LEVELS)
    assert isinstance(result["fixes"], dict)
    assert isinstance(result["fix_thresholds"], dict)
    assert isinstance(result["manual_only"], dict)

import pytest
from fastapi import HTTPException

from backend import diagnosis_fix


class _StubDB:
    def commit(self):
        return None

    def rollback(self):
        return None


def test_reject_still_400_when_audit_raises(monkeypatch):
    """F5 回归：_audit 抛错时 _reject 仍抛 400，不变成 500。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("audit boom")

    monkeypatch.setattr(admin_diagnosis, "_audit", _boom)
    with pytest.raises(HTTPException) as exc_info:
        admin_diagnosis._reject(_StubDB(), None, "disk_warn_pct",
                                "unknown_rule", "msg")
    assert exc_info.value.status_code == 400


def test_list_rules_tolerates_manual_only_without_reason(monkeypatch):
    """F2 回归：MANUAL_ONLY 某条缺 reason 时 rules 口不抛。"""
    patched = dict(diagnosis_fix.MANUAL_ONLY)
    patched["__no_reason_rule__"] = {"steps": ["手动处理"]}
    monkeypatch.setattr(diagnosis_fix, "MANUAL_ONLY", patched)
    result = admin_diagnosis.list_diagnosis_rules()
    assert result["manual_only"]["__no_reason_rule__"] == ""

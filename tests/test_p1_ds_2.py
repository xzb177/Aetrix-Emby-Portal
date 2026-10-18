"""P1 修复回归测试（DeepSeek 生成）：revoke_all_user_tokens。

（2026-10-17 转码删除：transcode_slot、hwaccel 参数、find_active_transcode
并发安全用例已随 transcode/hwaccel 模块删除。）"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest
from unittest import mock


def _import_or_skip(name: str):
    try:
        mod = __import__(name, fromlist=["*"])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{name} 导入失败，跳过: {exc!r}")
    return mod


def test_revoke_all_user_tokens_commit_and_rollback():
    """A-P1-1: revoke_all_user_tokens 正常时 commit，异常时 rollback 且不抛异常。"""
    security = _import_or_skip("backend.security")

    # 正常路径
    db = mock.MagicMock()
    security.revoke_all_user_tokens(db, 123)
    assert db.query.return_value.filter.return_value.update.called, "应调用 update"
    assert db.commit.called, "应调用 commit"
    assert not db.rollback.called, "正常路径不应回滚"

    # 异常路径
    db2 = mock.MagicMock()
    db2.query.return_value.filter.return_value.update.side_effect = RuntimeError("boom")
    security.revoke_all_user_tokens(db2, 123)  # 不应抛异常
    assert db2.rollback.called, "异常时应 rollback"


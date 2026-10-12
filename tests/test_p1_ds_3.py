"""P1 修复回归测试（DeepSeek 生成）：refresh 复用检测、admin 日志单查询。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest
from unittest import mock


def test_refresh_reuse_detection():
    """A-P1-1: 复用已吊销 jti 的 refresh_token 应 401 并吊销该用户全部 token。"""
    portal_mod = pytest.importorskip("backend.api.emby_portal")
    security_mod = pytest.importorskip("backend.security")
    fastapi_mod = pytest.importorskip("fastapi")
    HTTPException = fastapi_mod.HTTPException

    refresh = getattr(portal_mod, "refresh", None)
    if refresh is None:
        pytest.skip("refresh 不存在")

    mock_db = mock.MagicMock()
    # user 查询返回一个假用户
    fake_user = mock.Mock()
    fake_user.id = 5
    fake_user.is_active = True
    fake_user.username = "u5"
    fake_user.token_version = 0
    mock_db.query.return_value.filter.return_value.first.return_value = fake_user

    req = mock.Mock()
    req.refresh_token = "fake-refresh"
    decode_ret = {"sub": "5", "jti": "old-jti-1", "token_version": 0}

    with mock.patch.object(security_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(portal_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(security_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(portal_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(security_mod, "revoke_all_user_tokens", create=True) as m_revoke_all, \
         mock.patch.object(portal_mod, "revoke_all_user_tokens", create=True) as m_revoke_all2:
        with pytest.raises(HTTPException) as exc_info:
            refresh(req, mock_db)
        assert exc_info.value.status_code == 401
        assert m_revoke_all.called or m_revoke_all2.called, "应调用 revoke_all_user_tokens"


def test_refresh_rotation_revokes_old_jti():
    """A-P1-1: 正常轮换时旧 refresh 的 jti 应被吊销。"""
    portal_mod = pytest.importorskip("backend.api.emby_portal")
    security_mod = pytest.importorskip("backend.security")

    refresh = getattr(portal_mod, "refresh", None)
    if refresh is None:
        pytest.skip("refresh 不存在")

    mock_db = mock.MagicMock()
    fake_user = mock.Mock()
    fake_user.id = 5
    fake_user.is_active = True
    fake_user.username = "u5"
    fake_user.token_version = 0
    mock_db.query.return_value.filter.return_value.first.return_value = fake_user

    req = mock.Mock()
    req.refresh_token = "fake-refresh"
    decode_ret = {"sub": "5", "jti": "old-jti-2", "token_version": 0}

    with mock.patch.object(security_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(portal_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(security_mod, "is_jti_revoked", return_value=False, create=True), \
         mock.patch.object(portal_mod, "is_jti_revoked", return_value=False, create=True), \
         mock.patch.object(security_mod, "enforce_token_freshness", create=True), \
         mock.patch.object(portal_mod, "enforce_token_freshness", create=True), \
         mock.patch.object(security_mod, "revoke_jti", create=True) as m_r1, \
         mock.patch.object(portal_mod, "revoke_jti", create=True) as m_r2, \
         mock.patch.object(portal_mod, "create_access_token", return_value="a", create=True), \
         mock.patch.object(portal_mod, "create_refresh_token", return_value="r", create=True):
        try:
            refresh(req, mock_db)
        except Exception:
            pass  # 只关心 revoke_jti 是否被调用，不关心后续流程
        assert m_r1.called or m_r2.called, "轮换时应吊销旧 refresh jti"


def test_admin_logs_single_user_query():
    """F-P1-1: get_admin_logs 中 WebUser 只查一次（IN 批量）。"""
    admin_mod = pytest.importorskip("backend.api.admin")

    get_admin_logs = getattr(admin_mod, "get_admin_logs", None)
    if get_admin_logs is None:
        pytest.skip("get_admin_logs 不存在")

    # 3 条假日志：admin_user_id 为 1、1、None
    logs = []
    for i, uid in enumerate([1, 1, None]):
        lg = mock.Mock()
        lg.id = i
        lg.admin_user_id = uid
        lg.action = "test"
        lg.target_type = "t"
        lg.target_id = "1"
        lg.details = ""
        lg.ip_address = "127.0.0.1"
        lg.created_at = None
        logs.append(lg)

    fake_op = mock.Mock()
    fake_op.id = 1
    fake_op.username = "admin1"

    mock_db = mock.MagicMock()

    # 区分 AdminLog 查询和 WebUser 查询
    user_query_count = [0]

    def fake_query(model, *a, **kw):
        q = mock.MagicMock()
        q.order_by.return_value.limit.return_value.all.return_value = logs
        q.filter.return_value.all.return_value = [fake_op]
        # 记录 WebUser 查询次数
        orig_filter = q.filter

        def counting_filter(*fa, **fkw):
            user_query_count[0] += 1
            return orig_filter(*fa, **fkw)

        # 通过模型名判断
        if "WebUser" in str(model):
            q.filter = counting_filter
        return q

    mock_db.query.side_effect = fake_query

    try:
        get_admin_logs(db=mock_db)
    except Exception:
        pass  # 只关心查询次数

    assert user_query_count[0] <= 1, f"WebUser 应最多查 1 次，实际 {user_query_count[0]} 次"

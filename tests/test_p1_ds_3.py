"""P1 修复回归测试（DeepSeek 生成）：refresh 复用检测、admin 日志单查询。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest
from unittest import mock

portal_mod = pytest.importorskip("backend.api.emby_portal")
security_mod = pytest.importorskip("backend.security")


def test_refresh_reuse_detection():
    """A-P1-1: 复用已吊销 jti 的 refresh_token 应 401 并吊销该用户全部 token。

    显式把轮换容忍窗口判定为 False：这里模拟的是真正的复用攻击（非良性重试）。
    """
    from fastapi import HTTPException

    refresh = portal_mod.refresh

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
         mock.patch.object(security_mod, "_refresh_rotation_in_grace", return_value=False, create=True), \
         mock.patch.object(portal_mod, "_refresh_rotation_in_grace", return_value=False, create=True), \
         mock.patch.object(security_mod, "revoke_all_user_tokens", create=True) as m_revoke_all, \
         mock.patch.object(portal_mod, "revoke_all_user_tokens", create=True) as m_revoke_all2:
        with pytest.raises(HTTPException) as exc_info:
            refresh(req, mock_db)
        assert exc_info.value.status_code == 401
        assert m_revoke_all.called or m_revoke_all2.called, "应调用 revoke_all_user_tokens"


def test_refresh_rotation_grace_allows_retry():
    """P2-6: 轮换容忍窗口内的旧 refresh 重试视为良性重复：放行签发新对，
    不触发 revoke_all_user_tokens，也不走 enforce_token_freshness（旧 jti 已在吊销表）。"""
    from fastapi import HTTPException

    refresh = portal_mod.refresh

    mock_db = mock.MagicMock()
    fake_user = mock.Mock()
    fake_user.id = 5
    fake_user.is_active = True
    fake_user.username = "u5"
    fake_user.token_version = 0
    mock_db.query.return_value.filter.return_value.first.return_value = fake_user

    req = mock.Mock()
    req.refresh_token = "fake-refresh"
    decode_ret = {"sub": "5", "jti": "old-jti-3", "token_version": 0}

    with mock.patch.object(security_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(portal_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(security_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(portal_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(security_mod, "_refresh_rotation_in_grace", return_value=True, create=True), \
         mock.patch.object(portal_mod, "_refresh_rotation_in_grace", return_value=True, create=True), \
         mock.patch.object(security_mod, "revoke_all_user_tokens", create=True) as m_revoke_all, \
         mock.patch.object(portal_mod, "revoke_all_user_tokens", create=True) as m_revoke_all2, \
         mock.patch.object(security_mod, "enforce_token_freshness", create=True) as m_fresh, \
         mock.patch.object(portal_mod, "enforce_token_freshness", create=True) as m_fresh2, \
         mock.patch.object(security_mod, "revoke_jti", create=True), \
         mock.patch.object(portal_mod, "revoke_jti", create=True), \
         mock.patch.object(portal_mod, "create_access_token", return_value="a", create=True), \
         mock.patch.object(portal_mod, "create_refresh_token", return_value="r", create=True), \
         mock.patch.object(portal_mod, "_user_out", return_value={"id": 5}, create=True):
        # 窗口内重试必须放行，不能抛 401
        try:
            result = refresh(req, mock_db)
        except HTTPException as exc:
            pytest.fail(f"容忍窗口内不应 401，实际抛 {exc.status_code}")
        assert result["access_token"] == "a"
        assert result["refresh_token"] == "r"
        assert not (m_revoke_all.called or m_revoke_all2.called), "窗口内不得触发全量吊销"
        assert not (m_fresh.called or m_fresh2.called), "窗口内应跳过新鲜度检查"


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


def test_refresh_grace_rejects_stale_tv_after_password_change():
    """P1（安全）：容忍窗口内也必须校验 token_version。

    改密/登出 bump user.token_version 后，120s 容忍窗口内用旧 refresh 重试必须 401，
    否则旧凭证仍能换出新 access，违反"改密后旧 token 立即作废"。
    注意：此处不能走 enforce_token_freshness（它含 jti 吊销检查，会误杀良性重试），
    refresh() 内部应单独比对 tv。
    """
    from fastapi import HTTPException

    refresh = portal_mod.refresh

    mock_db = mock.MagicMock()
    # 用户已改密：token_version 被 bump 到 1
    fake_user = mock.Mock()
    fake_user.id = 5
    fake_user.is_active = True
    fake_user.username = "u5"
    fake_user.token_version = 1
    mock_db.query.return_value.filter.return_value.first.return_value = fake_user

    req = mock.Mock()
    req.refresh_token = "fake-refresh"
    # 旧 refresh：tv=0（改密前签发），jti 已在吊销表（轮换产生）
    decode_ret = {"sub": "5", "jti": "old-jti-9", "tv": 0}

    with mock.patch.object(security_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(portal_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(security_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(portal_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(security_mod, "_refresh_rotation_in_grace", return_value=True, create=True), \
         mock.patch.object(portal_mod, "_refresh_rotation_in_grace", return_value=True, create=True), \
         mock.patch.object(security_mod, "revoke_all_user_tokens", create=True) as m_revoke_all, \
         mock.patch.object(portal_mod, "revoke_all_user_tokens", create=True) as m_revoke_all2:
        with pytest.raises(HTTPException) as exc_info:
            refresh(req, mock_db)
        assert exc_info.value.status_code == 401
        assert "密码已变更" in exc_info.value.detail
        # tv 不一致走的是 401 短路，不是复用攻击的应急吊销
        assert not (m_revoke_all.called or m_revoke_all2.called), \
            "tv 不一致应直接 401，不触发 revoke_all_user_tokens"


def test_refresh_grace_allows_retry_with_matching_tv():
    """P1（安全）：容忍窗口内 tv 一致（未改密）的良性重试必须放行。

    这是与上一个测试配对的正向用例：修复不能误杀正常轮换重试。
    """
    from fastapi import HTTPException

    refresh = portal_mod.refresh

    mock_db = mock.MagicMock()
    fake_user = mock.Mock()
    fake_user.id = 5
    fake_user.is_active = True
    fake_user.username = "u5"
    fake_user.token_version = 2
    mock_db.query.return_value.filter.return_value.first.return_value = fake_user

    req = mock.Mock()
    req.refresh_token = "fake-refresh"
    decode_ret = {"sub": "5", "jti": "old-jti-10", "tv": 2}

    with mock.patch.object(security_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(portal_mod, "decode_token", return_value=decode_ret, create=True), \
         mock.patch.object(security_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(portal_mod, "is_jti_revoked", return_value=True, create=True), \
         mock.patch.object(security_mod, "_refresh_rotation_in_grace", return_value=True, create=True), \
         mock.patch.object(portal_mod, "_refresh_rotation_in_grace", return_value=True, create=True), \
         mock.patch.object(security_mod, "revoke_all_user_tokens", create=True) as m_revoke_all, \
         mock.patch.object(portal_mod, "revoke_all_user_tokens", create=True) as m_revoke_all2, \
         mock.patch.object(security_mod, "revoke_jti", create=True), \
         mock.patch.object(portal_mod, "revoke_jti", create=True), \
         mock.patch.object(portal_mod, "create_access_token", return_value="a", create=True), \
         mock.patch.object(portal_mod, "create_refresh_token", return_value="r", create=True), \
         mock.patch.object(portal_mod, "_user_out", return_value={"id": 5}, create=True):
        try:
            result = refresh(req, mock_db)
        except HTTPException as exc:
            pytest.fail(f"tv 一致的窗口内重试不应 401，实际抛 {exc.status_code}")
        assert result["access_token"] == "a"
        assert result["refresh_token"] == "r"
        assert not (m_revoke_all.called or m_revoke_all2.called), "窗口内不得触发全量吊销"


def test_admin_logs_single_user_query():
    """F-P1-1: get_admin_logs 中 WebUser 只查一次（IN 批量），且操作人名正确映射。

    P2 补强：旧版用 try/except: pass 吞掉异常——若被测函数在查询前抛错，
    计数保持 0 也能通过 `<= 1`，恒真。本版要求调用成功、恰好查 1 次、
    且返回的 admin_name 真的是批量映射的结果。
    """
    from backend.api.admin import get_admin_logs

    # 3 条假日志：admin_user_id 为 1、1、None（去重后只需查 id=1）
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

    user_query_count = [0]
    fake_query = mock.MagicMock()
    fake_query.order_by.return_value.limit.return_value.all.return_value = logs
    fake_query.filter.return_value.all.return_value = [fake_op]

    def _counting_filter(*args, **kwargs):
        user_query_count[0] += 1
        return fake_query.filter.return_value

    fake_query.filter.side_effect = _counting_filter
    mock_db = mock.MagicMock()
    mock_db.query.return_value = fake_query

    # 调用必须成功：抛异常不再被吞掉，恒真缺口堵上
    result = get_admin_logs(db=mock_db)

    assert user_query_count[0] == 1, \
        f"WebUser 应恰好查 1 次（批量 IN），实际 {user_query_count[0]} 次"
    assert len(result) == 3
    # admin_user_id=1 的两条映射到 admin1；None 的那条回退"未知"
    assert [r["admin_name"] for r in result] == ["admin1", "admin1", "未知"]

# -*- coding: utf-8 -*-
"""用户侧登录记录只读口（GET /api/user/auth/login-history）。

覆盖：
1. 只返回本人记录：他人行、user_id 为空的行都不许漏进来；
2. 分页：total 为本人总数，limit/offset 切片正确，倒序（新在前）；
3. 字段白名单：只出时间/IP/地区/设备/成败/事件类别，不带 detail 等内部字段；
4. limit 钳制：超大 limit 不许一次拉爆（上限 100）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

import pytest

from backend import models
from backend.api.emby_portal import get_login_history
from backend.database import SessionLocal, init_db

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _user(db):
    u = models.WebUser(
        username="lh_" + uuid.uuid4().hex[:12], password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    # 全量套件共用文件库：sqlite 会复用已删用户的 id，前序文件遗留的
    # LoginLog 行可能正好挂在这个 id 上（CI 实测 total 13 == 1）。
    # 建用户后先清掉挂在该 id 上的旧行，恢复「新用户无历史」前提；
    # 断言一字不动，过滤逻辑仍由被测口径验。
    db.query(models.LoginLog).filter(
        models.LoginLog.user_id == u.id).delete()
    db.commit()
    return u


def _log(db, user_id, *, success=True, reason="portal_login", minutes_ago=0,
         detail="内部备注不许外泄"):
    row = models.LoginLog(
        user_id=user_id,
        username="someone",
        ip="203.0.113.7",
        user_agent="UnitTestAgent/1.0",
        success=success,
        reason=reason,
        detail=detail,
        region="广东深圳",
        created_at=datetime.now() - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    db.commit()
    return row


def test_only_own_rows(db):
    me = _user(db)
    other = _user(db)
    _log(db, me.id, minutes_ago=1)
    _log(db, other.id, minutes_ago=0)
    _log(db, None, minutes_ago=0)  # 无主行（失败登录未命中用户）不许漏给任何人
    out = get_login_history(limit=20, offset=0, current_user=me, db=db)
    assert out["total"] == 1
    assert len(out["items"]) == 1
    assert out["items"][0]["ip"] == "203.0.113.7"


def test_field_whitelist_and_labels(db):
    me = _user(db)
    _log(db, me.id, success=False, reason="portal_login_failed")
    out = get_login_history(limit=20, offset=0, current_user=me, db=db)
    item = out["items"][0]
    assert set(item.keys()) == {
        "created_at", "ip", "region", "user_agent",
        "success", "reason", "reason_label",
    }
    assert "detail" not in item and "username" not in item
    assert item["success"] is False
    assert item["reason_label"] == "门户登录失败"
    assert item["region"] == "广东深圳"


def test_pagination_desc_order(db):
    me = _user(db)
    for i in range(5):
        _log(db, me.id, minutes_ago=10 - i)  # i 越大越新
    page1 = get_login_history(limit=2, offset=0, current_user=me, db=db)
    page2 = get_login_history(limit=2, offset=2, current_user=me, db=db)
    page3 = get_login_history(limit=2, offset=4, current_user=me, db=db)
    assert page1["total"] == 5
    times = [x["created_at"] for x in page1["items"] + page2["items"] + page3["items"]]
    assert len(times) == 5
    assert times == sorted(times, reverse=True)


def test_limit_clamped(db):
    me = _user(db)
    for _ in range(3):
        _log(db, me.id)
    out = get_login_history(limit=10 ** 9, offset=0, current_user=me, db=db)
    assert out["total"] == 3
    assert len(out["items"]) == 3

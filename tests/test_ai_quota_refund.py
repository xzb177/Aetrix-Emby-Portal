"""r48 遗留 ①：AI 上游调用失败时回滚本次预扣的配额。

覆盖三点：
1. 失败回滚（ai 层单元 + assistant 层集成）；
2. 成功不回滚（assistant 层为主）；
3. 重复回调/重复走失败分支不双回滚（幂等，ai 层 request_id 去重单元为主）。
"""
import os

# r55：与其它测试文件同口径——在 import backend 之前先钉住方言，否则本文件
# 先被 pytest 收集时，backend.database 会按默认 postgresql 初始化，
# 后续 test_parity_episode_renumber.py 模块级 init_db() 会走 pg_advisory_lock
# 在 sqlite 上收集失败（CI 全局设了 DATABASE_TYPE=sqlite 故不受影响）。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.integrations import ai


@pytest.fixture
def db():
    # 内存库：不依赖 /tmp（512M tmpfs，全量跑时会被其他测试的 sqlite 文件塞满）
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()


# ---------- ai 层：失败回滚 ----------

def test_refund_after_consume_restores_count(db):
    allowed, used, deducted = ai.consume(db, user_id=1, limit=20)
    assert (allowed, used, deducted) == (True, 1, True)
    assert ai.used_today(db, 1) == 1

    assert ai.refund_quota(db, 1, "req-1") is True
    assert ai.used_today(db, 1) == 0


def test_refund_never_goes_negative(db):
    """没扣过也退：计数不减出负数（幂等账本照记，空操作）。"""
    assert ai.used_today(db, 1) == 0
    assert ai.refund_quota(db, 1, "req-empty") is True
    assert ai.used_today(db, 1) == 0


def test_refund_same_request_id_is_idempotent(db):
    """同一 request_id 调两次：第二次是空操作，不双退。"""
    ai.consume(db, user_id=1, limit=20)
    assert ai.refund_quota(db, 1, "req-dup") is True
    assert ai.used_today(db, 1) == 0

    assert ai.refund_quota(db, 1, "req-dup") is False
    assert ai.used_today(db, 1) == 0


def test_refund_different_request_ids_each_refund_once(db):
    ai.consume(db, user_id=1, limit=20)
    ai.consume(db, user_id=1, limit=20)
    assert ai.used_today(db, 1) == 2

    assert ai.refund_quota(db, 1, "req-a") is True
    assert ai.used_today(db, 1) == 1
    assert ai.refund_quota(db, 1, "req-b") is True
    assert ai.used_today(db, 1) == 0
    # 再退一次 req-a：幂等，不动
    assert ai.refund_quota(db, 1, "req-a") is False
    assert ai.used_today(db, 1) == 0


def test_refund_without_request_id_skipped(db):
    ai.consume(db, user_id=1, limit=20)
    assert ai.refund_quota(db, 1, "") is False
    assert ai.used_today(db, 1) == 1


def test_safe_consume_fail_open_marks_not_deducted():
    """fail-open 穿透时 deducted=False：调用方不许退（没扣过）。"""
    from sqlalchemy.exc import SQLAlchemyError

    class _BadDB:
        def query(self, *a, **k):
            raise SQLAlchemyError("连接池耗尽")

    allowed, used, deducted = ai.safe_consume(_BadDB(), user_id=1)
    assert allowed is True
    assert deducted is False


# ---------- assistant 层：端到端 ----------

def _ask(db, monkeypatch, ask_result, safe_consume=None):
    from backend.api import assistant as assistant_api

    monkeypatch.setattr(ai, "available", lambda db: True)
    monkeypatch.setattr(ai, "daily_limit", lambda db: 20)
    if safe_consume is not None:
        monkeypatch.setattr(ai, "safe_consume", safe_consume)
    monkeypatch.setattr(ai, "ask", lambda db, q, h=None: ask_result)
    return assistant_api


def test_assistant_ask_failure_refunds_quota(db, monkeypatch):
    """上游失败 → 502，且预扣的 1 次配额被退回（+1 又 -1）。"""
    assistant_api = _ask(db, monkeypatch, {"ok": False, "message": "上游 429"})
    with pytest.raises(HTTPException) as exc_info:
        assistant_api.assistant_ask(
            assistant_api.AskRequest(question="你好"),
            SimpleNamespace(id=7), db)
    assert exc_info.value.status_code == 502
    assert ai.used_today(db, 7) == 0


def test_assistant_ask_success_does_not_refund(db, monkeypatch):
    """上游成功 → 配额保留（恰 +1），refund 未被调用。"""
    calls = []
    real_refund = ai.refund_quota

    def _counting_refund(db_, uid, rid):
        calls.append(rid)
        return real_refund(db_, uid, rid)

    assistant_api = _ask(db, monkeypatch,
                         {"ok": True, "answer": "你好呀", "model": "x"})
    monkeypatch.setattr(ai, "refund_quota", _counting_refund)
    payload = assistant_api.assistant_ask(
        assistant_api.AskRequest(question="你好"),
        SimpleNamespace(id=7), db)
    assert payload["answer"] == "你好呀"
    assert ai.used_today(db, 7) == 1
    assert calls == []


def test_assistant_ask_failure_failopen_does_not_refund(db, monkeypatch):
    """fail-open 穿透（没扣过）+ 上游失败 → 不许退（否则误减计数）。"""
    calls = []
    real_refund = ai.refund_quota

    def _counting_refund(db_, uid, rid):
        calls.append(rid)
        return real_refund(db_, uid, rid)

    assistant_api = _ask(
        db, monkeypatch, {"ok": False, "message": "boom"},
        safe_consume=lambda db_, uid, limit, request_id=None: (True, 0, False))
    monkeypatch.setattr(ai, "refund_quota", _counting_refund)
    with pytest.raises(HTTPException) as exc_info:
        assistant_api.assistant_ask(
            assistant_api.AskRequest(question="你好"),
            SimpleNamespace(id=7), db)
    assert exc_info.value.status_code == 502
    assert calls == []
    assert ai.used_today(db, 7) == 0


# ---------- 跨午夜漂移（r51 观察项 → 用户已批准修复） ----------

def _usage_row(db, user_id, day):
    return db.query(models.AiUsage).filter(
        models.AiUsage.user_id == user_id, models.AiUsage.day == day).first()


def test_refund_across_midnight_returns_to_deduct_day(db, monkeypatch):
    """23:59:59 扣的配额、次日凌晨退款：必须退回「扣费当天」的账，
    不能退到退款时刻的当天账上（否则当天账永久少退、净差 1 单位）。"""
    monkeypatch.setattr(ai, "_today", lambda: "2026-10-19")
    allowed, used, deducted = ai.consume(db, 1, 20, request_id="cross-day-1")
    assert (allowed, used, deducted) == (True, 1, True)
    assert ai.used_today(db, 1) == 1

    # 跨过午夜再退款
    monkeypatch.setattr(ai, "_today", lambda: "2026-10-20")
    assert ai.refund_quota(db, 1, "cross-day-1") is True

    # 扣费当天的计数必须被恢复
    day1 = _usage_row(db, 1, "2026-10-19")
    assert day1 is not None and day1.count == 0
    # 退款当天不许被误减
    day2 = _usage_row(db, 1, "2026-10-20")
    assert day2 is None or day2.count == 0


def test_refund_across_midnight_is_idempotent(db, monkeypatch):
    """跨日退款同样幂等：第二次是空操作，不双退。"""
    monkeypatch.setattr(ai, "_today", lambda: "2026-10-19")
    ai.consume(db, 1, 20, request_id="cross-day-2")

    monkeypatch.setattr(ai, "_today", lambda: "2026-10-20")
    assert ai.refund_quota(db, 1, "cross-day-2") is True
    assert ai.refund_quota(db, 1, "cross-day-2") is False

    day1 = _usage_row(db, 1, "2026-10-19")
    assert day1 is not None and day1.count == 0


def test_refund_legacy_row_not_refunded_twice(db):
    """老数据行（升级前已退过的 request_id，迁移后 refunded=TRUE=已退标记）：
    升级后重放不许再退一次。"""
    db.add(models.AiRefundLog(user_id=1, day="2026-10-19",
                              request_id="legacy-1", refunded=True))
    db.commit()
    assert ai.used_today(db, 1) == 0
    assert ai.refund_quota(db, 1, "legacy-1") is False
    assert ai.used_today(db, 1) == 0


def test_refund_rejects_mismatched_user_id(db):
    """r55 A1：claim 必须校验 user_id——归属行是 user1 的，user2 拿同一
    request_id 来退必须被拒绝，且两边账都不动；user1 自己的退款不受影响。"""
    ai.consume(db, 1, 20, request_id="cross-user-1")
    ai.consume(db, 2, 20, request_id="u2-seed")
    assert ai.used_today(db, 1) == 1
    assert ai.used_today(db, 2) == 1

    assert ai.refund_quota(db, 2, "cross-user-1") is False
    assert ai.used_today(db, 1) == 1  # user1 的归属行没被认领走
    assert ai.used_today(db, 2) == 1  # user2 没白捡一次配额

    assert ai.refund_quota(db, 1, "cross-user-1") is True
    assert ai.used_today(db, 1) == 0
    assert ai.used_today(db, 2) == 1

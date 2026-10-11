# -*- coding: utf-8 -*-
"""发送失败的到期提醒必须能补发：失败要撤掉占位，静默失败也算失败。"""

import asyncio
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, notifications, reminders


@pytest.fixture()
def db():
    """函数级内存 SQLite，建全表。"""
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db):
    """造一个 28 天前开卡、还有 2 天 1 小时到期的活跃订阅 → 落到默认档位的 "3d"。

    注意用 datetime.now()：collect_due 默认也按 datetime.now() 算剩余天数，
    用 utcnow 在非 UTC 时区会错档。
    """
    now = datetime.now()
    db.add(models.WebUser(id=1, username="u1", password_hash="x"))
    db.add(models.SubscriptionPlan(id=1, name="月卡", price=30, duration_days=30))
    db.add(models.UserSubscription(
        user_id=1,
        plan_id=1,
        status="active",
        start_date=now - timedelta(days=28),
        end_date=now + timedelta(days=2, hours=1),
    ))
    db.commit()


def test_raise_retracts_record_and_retry_succeeds(db, monkeypatch):
    """模式 A：发送抛异常 → 撤占位 → 下一轮能补发。"""
    _seed(db)

    async def _boom(**kwargs):
        raise RuntimeError("WebSocket 抖动")

    # run_expiry_reminders 内部是 from backend.notifications import notify_admin_event，
    # 每次调用都从模块属性读取，所以必须 patch backend.notifications 上的属性。
    monkeypatch.setattr(notifications, "notify_admin_event", _boom)
    result = asyncio.run(reminders.run_expiry_reminders(db))
    assert result["failed"] == 1
    assert result["reminded"] == 0
    assert db.query(models.SubscriptionReminder).count() == 0  # 占位已撤销，不是「已发送」

    async def _ok(**kwargs):
        return {"in_app": True}

    monkeypatch.setattr(notifications, "notify_admin_event", _ok)
    result = asyncio.run(reminders.run_expiry_reminders(db))
    assert result["reminded"] == 1
    rows = db.query(models.SubscriptionReminder).all()
    assert len(rows) == 1
    assert rows[0].kind == "3d"  # 重试补发成功


def test_all_false_counts_as_failure_and_retries(db, monkeypatch):
    """模式 B：全部渠道静默失败（返回 False 不抛）→ 不能记成成功，下一轮能补发。"""
    _seed(db)

    async def _silent_fail(**kwargs):
        return {"in_app": False}  # InAppChannel.send 内部吞异常，从不抛

    monkeypatch.setattr(notifications, "notify_admin_event", _silent_fail)
    result = asyncio.run(reminders.run_expiry_reminders(db))
    assert result["failed"] == 1
    assert result["reminded"] == 0
    assert db.query(models.SubscriptionReminder).count() == 0  # 消息没落库，就不能算已发送

    async def _ok(**kwargs):
        return {"in_app": True}

    monkeypatch.setattr(notifications, "notify_admin_event", _ok)
    result = asyncio.run(reminders.run_expiry_reminders(db))
    assert result["reminded"] == 1
    assert db.query(models.SubscriptionReminder).count() == 1

"""Aetrix 提醒扫描回归测试：键集分页 + 批量 sent_kinds。

本轮重构把 backend/reminders.py 的 collect_due 从
「全量 .all() + 每条订阅一次 _sent_kinds 查询（N+1）」
改成
「按 models.UserSubscription.id 升序键集分页（_SCAN_BATCH = 500）
 + 每页一次 _sent_kinds_batch 批量查询」。

本文件锁住两件事：
1. subscription_reminders 的 SELECT 次数随「页数」增长，而不是随「订阅数」增长；
2. collect_due 的对外语义（分档 / horizon 剪枝 / 已发去重 / info 字段）与重构前一致。
"""

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import pytest
import sqlalchemy
from sqlalchemy.pool import StaticPool

import backend.models as models
from backend.reminders import _SCAN_BATCH, collect_due


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def engine():
    """函数级内存 SQLite；StaticPool 保证所有会话共用同一条连接。"""
    eng = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        models.Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def db(engine):
    """函数级 session。"""
    session = sqlalchemy.orm.sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------
# 构造辅助
# --------------------------------------------------------------------------
def _build(model, **kwargs):
    """只保留模型真实存在的列，避免模型字段演进时测试直接崩。"""
    cols = {c.name for c in model.__table__.columns}
    return model(**{k: v for k, v in kwargs.items() if k in cols})


def _mk_sub(db, end_date, *, user_id=1, status="active", plan_id):
    """建一条订阅；realm_id 传 None（避免外键表），flush 后拿到 id。

    注意：user_subscriptions.plan_id / end_date 都是 NOT NULL，
    所以 plan 缺失用「指向不存在套餐的 plan_id」模拟（SQLite 默认不强制外键），
    end_date 缺失的老行在当前 schema 下造不出来，对应分支只保留代码里的兜底。
    """
    if end_date is not None:
        start_date = end_date - timedelta(days=30)
    else:
        start_date = datetime.now() - timedelta(days=30)
    sub = _build(
        models.UserSubscription,
        user_id=user_id,
        realm_id=None,
        plan_id=plan_id,
        status=status,
        start_date=start_date,
        end_date=end_date,
    )
    db.add(sub)
    db.flush()
    return sub


def _mk_plan(db, name):
    """建一条套餐。"""
    plan = _build(models.SubscriptionPlan, name=name, price=0, duration_days=30)
    db.add(plan)
    db.flush()
    return plan


def _info(entry):
    """兼容 {"kind": ..., "info": {...}} 与扁平 dict 两种条目结构。"""
    if isinstance(entry, dict) and isinstance(entry.get("info"), dict):
        return entry["info"]
    return entry


def _sid(entry):
    """取条目对应的 subscription_id。"""
    return _info(entry).get("subscription_id")


def _kind(entry):
    """取条目的档位 kind（扁平结构时为 None）。"""
    if isinstance(entry, dict):
        return entry.get("kind")
    return None


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------
def test_sent_kinds_batched(db, engine):
    """1200 条活跃订阅跨 3 页扫描：subscription_reminders 的查询次数应随页数增长。"""
    assert _SCAN_BATCH == 500

    now = datetime.now()
    plan = _mk_plan(db, "批量计划")
    subs = [
        _build(
            models.UserSubscription,
            user_id=1,
            realm_id=None,
            plan_id=plan.id,
            status="active",
            start_date=now - timedelta(days=30),
            end_date=now + timedelta(days=2),
        )
        for _ in range(1200)
    ]
    db.add_all(subs)
    db.flush()

    statements = []

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if sql.lstrip().startswith("select") and "subscription_reminders" in sql:
            statements.append(statement)

    sqlalchemy.event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        result = collect_due(db, now=now)
    finally:
        sqlalchemy.event.remove(engine, "before_cursor_execute", _before_cursor_execute)

    # 1200 条都在 "3d" 档，且都没发过
    assert len(result["reminders"]) == 1200
    # 3 页 → 至多几次批量查询；远小于 1200（旧的 N+1 实现）
    assert len(statements) <= 10
    assert len(statements) < 1200


def test_collect_due_semantics_unchanged(db):
    """混合 fixture 逐项断言 collect_due 的分档 / 剪枝 / 去重 / 字段语义。"""
    now = datetime.now()
    plan = _mk_plan(db, "专业版")

    # ① 剩余 2 天 → "3d" 档，plan_name 取套餐名
    s_3d = _mk_sub(db, now + timedelta(days=2), plan_id=plan.id)
    # ② 剩余 8 天 → 超出 horizon，被剪枝
    s_far = _mk_sub(db, now + timedelta(days=8), plan_id=plan.id)
    # ③ 昨天过期 → expired
    s_yesterday = _mk_sub(db, now - timedelta(days=1), plan_id=plan.id)
    # ④ 30 天前过期，但已经发过 "expired"
    s_old_sent = _mk_sub(db, now - timedelta(days=30), plan_id=plan.id)
    db.add(
        models.SubscriptionReminder(
            subscription_id=s_old_sent.id, user_id=1, kind="expired"
        )
    )
    # ⑤ 剩余 2 天，但已经发过 "3d"
    s_3d_sent = _mk_sub(db, now + timedelta(days=2), plan_id=plan.id)
    db.add(
        models.SubscriptionReminder(
            subscription_id=s_3d_sent.id, user_id=1, kind="3d"
        )
    )
    # ⑥ 已取消的订阅不参与
    s_canceled = _mk_sub(db, now + timedelta(days=2), plan_id=plan.id, status="canceled")
    # ⑦ plan_id 指向不存在的套餐 → plan_name 回退为 "会员"
    #     （plan_id 列 NOT NULL，用悬空外键模拟「套餐被删」）
    s_no_plan = _mk_sub(db, now + timedelta(days=2), plan_id=999999)

    db.flush()

    result = collect_due(db, now=now)
    reminders = result["reminders"]
    expired = result["expired"]

    assert result["thresholds"] == [7, 3, 1]

    # ① 剩余 2 天进 "3d" 档且 plan_name 取套餐名
    hit_3d = [e for e in reminders if _sid(e) == s_3d.id]
    assert len(hit_3d) == 1
    assert _kind(hit_3d[0]) == "3d"
    assert _info(hit_3d[0])["plan_name"] == "专业版"

    # ② 剩余 8 天的被 horizon 剪枝，两个桶都不出现
    assert not any(_sid(e) == s_far.id for e in reminders)
    assert not any(_sid(e) == s_far.id for e in expired)

    # ③ 昨天过期的进 expired 桶
    assert any(_sid(e) == s_yesterday.id for e in expired)

    # ④ 30 天前过期但已发过 "expired" 的既不进 expired 也不进 reminders
    assert not any(_sid(e) == s_old_sent.id for e in expired)
    assert not any(_sid(e) == s_old_sent.id for e in reminders)

    # ⑤ 剩余 2 天但已发过 "3d" 的不重复出现
    assert not any(_sid(e) == s_3d_sent.id for e in reminders)

    # ⑥ status="canceled" 的不参与
    assert not any(_sid(e) == s_canceled.id for e in reminders)
    assert not any(_sid(e) == s_canceled.id for e in expired)

    # ⑦ plan_id 悬空 → plan_name 回退为 "会员"
    hit_no_plan = [e for e in reminders if _sid(e) == s_no_plan.id]
    assert len(hit_no_plan) == 1
    assert _info(hit_no_plan[0])["plan_name"] == "会员"

    # reminders 的集合恰好是「未发过的 3d 档」两条
    assert {_sid(e) for e in reminders} == {s_3d.id, s_no_plan.id}

    # info 字段齐全
    for entry in reminders:
        info = _info(entry)
        for key in (
            "subscription_id",
            "user_id",
            "realm_id",
            "realm_name",
            "plan_name",
            "end_date",
        ):
            assert key in info, f"reminder info 缺少字段: {key}"


def test_collect_due_empty(db):
    """空库：结构完整、阈值默认 [7, 3, 1]。"""
    result = collect_due(db, now=datetime.now())
    assert result == {"reminders": [], "expired": [], "thresholds": [7, 3, 1]}

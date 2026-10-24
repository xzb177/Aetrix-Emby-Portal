"""订阅分档 PR-1：套餐权益表 + 权益判定单口（backend/entitlements.py）。

钉住的口径（设计 reports/subscription-tier-design-20261024.md §1.4/§2）：
- 回退 None：无订阅 / 套餐 entitlements 为 NULL 或 {} / 未声明该键 /
  值非法（坏数据不抛、不当权益）；
- 声明后读到值；「当前套餐」复用 subscriptions.active_subscription
  （过期/已取消不算、同用户多份取 end_date 最晚、顺延后 plan 跟随最近购买）；
- welfare 用户在 helper 层不被特殊化（helper 不看 is_welfare）；
- 两级缓存：命中读旧值、invalidate 后刷新、TTL 过期重查；缓存条目
  自带 end_date，订阅到期不被缓存拖延；
- 发放口失效挂点：economy._grant_subscription 发放后不手动 invalidate
  也能立刻读到新套餐权益；codes.grant_membership_days 与
  orders_admin._rollback_subscription 会清掉该用户的解析缓存。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import codes, entitlements, models
from backend.api import orders_admin
from backend.api.economy import _grant_subscription

KEY_SEEK = "media_seek_monthly_limit"
KEY_CONC = "concurrent_streams"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _clear_entitlement_caches():
    entitlements._PLAN_SNAPSHOT_CACHE.clear()
    entitlements._USER_PLAN_CACHE.clear()
    yield
    entitlements._PLAN_SNAPSHOT_CACHE.clear()
    entitlements._USER_PLAN_CACHE.clear()


def _make_user(db, username="u1", **kw):
    u = models.WebUser(username=username, password_hash="", points=0,
                       is_active=True, **kw)
    db.add(u)
    db.commit()
    return u


def _make_plan(db, name="远行执照", entitlements_value=None, **kw):
    p = models.SubscriptionPlan(
        name=name, price=100, duration_days=30, is_active=True,
        entitlements=entitlements_value, **kw)
    db.add(p)
    db.commit()
    return p


def _make_sub(db, user, plan, days=30, status="active"):
    now = datetime.now()
    s = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=None,
        start_date=now, end_date=now + timedelta(days=days), status=status)
    db.add(s)
    db.commit()
    return s


# ---------------- 回退 None：没声明就是没声明 ----------------

def test_no_subscription_returns_none(db):
    user = _make_user(db)
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) is None
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None
    assert entitlements.current_plan(db, user.id) is None


def test_plan_entitlements_null_returns_none(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value=None)
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) is None
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None


def test_plan_entitlements_empty_dict_returns_none(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None


def test_undeclared_key_returns_none(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: 25})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) == 25
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None


def test_declared_values_are_returned(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: 40, KEY_CONC: 4})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) == 40
    # user 也可直接传 user_id
    assert entitlements.get_user_entitlement(db, user.id, KEY_CONC) == 4


def test_unregistered_key_raises_keyerror(db):
    user = _make_user(db)
    with pytest.raises(KeyError):
        entitlements.get_user_entitlement(db, user, "download_enabled")


# ---------------- 当前套餐口径（复用 active_subscription） ----------------

def test_expired_subscription_not_current(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 4})
    _make_sub(db, user, plan, days=-1)  # end_date 已过
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None
    assert entitlements.current_plan(db, user.id) is None


def test_cancelled_subscription_not_current(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 4})
    _make_sub(db, user, plan, status="cancelled")
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None


def test_current_plan_prefers_latest_end_date(db):
    user = _make_user(db)
    plan_a = _make_plan(db, name="甲", entitlements_value={KEY_CONC: 2})
    plan_b = _make_plan(db, name="乙", entitlements_value={KEY_CONC: 6})
    _make_sub(db, user, plan_a, days=10)
    _make_sub(db, user, plan_b, days=40)
    assert entitlements.current_plan(db, user.id).id == plan_b.id
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 6


def test_plan_deleted_returns_none(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 4})
    _make_sub(db, user, plan)
    db.delete(plan)
    db.commit()
    assert entitlements.current_plan(db, user.id) is None
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None


def test_welfare_user_not_special_cased(db):
    # helper 不看 is_welfare：公益用户持有套餐时权益照常可读
    # （求片消费方按判定顺序根本不会来读，那是 PR-2 的事）
    user = _make_user(db, is_welfare=True)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: 30})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) == 30


# ---------------- 坏数据：回 None 不抛 ----------------

@pytest.mark.parametrize("bad", [
    "25",      # 字符串
    2.5,       # 浮点
    True,      # bool 不算整数
    0,         # 低于下限
    -3,        # 负数
    1001,      # 超过上限（media_seek 上限 1000）
    None,      # 键在但值为空
])
def test_bad_values_fall_back_to_none(db, bad):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_SEEK: bad})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) is None


@pytest.mark.parametrize("raw", ["junk", [1, 2], 42])
def test_non_dict_entitlements_fall_back(db, raw):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value=raw)
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None


def test_unknown_key_in_plan_is_ignored(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={"nope": 5, KEY_CONC: 3})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 3


def test_parse_entitlements_pure():
    assert entitlements.parse_entitlements(None) == {}
    assert entitlements.parse_entitlements("junk") == {}
    assert entitlements.parse_entitlements(
        {KEY_SEEK: 25, KEY_CONC: "x", "unknown": 1, "": 2}
    ) == {KEY_SEEK: 25}


def test_validate_entitlements():
    assert entitlements.validate_entitlements(None) == {}
    assert entitlements.validate_entitlements({KEY_CONC: 3}) == {KEY_CONC: 3}
    with pytest.raises(ValueError):
        entitlements.validate_entitlements("junk")
    with pytest.raises(ValueError):
        entitlements.validate_entitlements({"unknown_key": 1})
    with pytest.raises(ValueError):
        entitlements.validate_entitlements({KEY_CONC: "3"})
    with pytest.raises(ValueError):
        entitlements.validate_entitlements({KEY_CONC: True})
    with pytest.raises(ValueError):
        entitlements.validate_entitlements({KEY_CONC: 21})  # 上限 20


# ---------------- 缓存：命中 / 失效 / TTL / 到期精度 ----------------

def test_plan_snapshot_cache_until_invalidate(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 2})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 2
    plan.entitlements = {KEY_CONC: 5}  # 整对象赋值（JSON 列纪律）
    db.commit()
    # 快照缓存命中：还是旧值
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 2
    entitlements.invalidate()
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 5


def test_plan_snapshot_cache_ttl_expiry(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 2})
    _make_sub(db, user, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 2
    plan.entitlements = {KEY_CONC: 7}
    db.commit()
    ts, parsed = entitlements._PLAN_SNAPSHOT_CACHE[plan.id]
    entitlements._PLAN_SNAPSHOT_CACHE[plan.id] = (
        ts - 2 * entitlements.CACHE_TTL_SEC, parsed)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 7


def test_resolution_cache_negative_until_user_invalidate(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 4})
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None
    _make_sub(db, user, plan)  # 直插订阅行（绕开发放口）
    # 解析缓存里还是「无套餐」
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None
    entitlements.invalidate(user_id=user.id)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 4


def test_cached_resolution_respects_end_date(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 4})
    _make_sub(db, user, plan, days=1)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 4
    # 模拟「时间流逝到订阅到期之后」的状态：缓存条目里存的 end_date
    # 已成过去。不调 invalidate、也不动 DB——命中缓存时必须按条目自带
    # 的 end_date 判无套餐，不能等 60 秒 TTL 过期才发现订阅已到期。
    # （end_date 被改短只发生在发放/回滚路径上，那些路径都挂了失效。）
    key = (user.id, None)
    ts, plan_id, _end = entitlements._USER_PLAN_CACHE[key]
    entitlements._USER_PLAN_CACHE[key] = (
        ts, plan_id, datetime.now() - timedelta(seconds=1))
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None


# ---------------- 发放口失效挂点 ----------------

def test_grant_subscription_invalidates_cache(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 4})
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None
    _grant_subscription(db, user, plan, 30)
    db.commit()
    # 不手动 invalidate：发放口已挂失效，立刻读到
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 4


def test_grant_extend_follows_latest_plan(db):
    user = _make_user(db)
    plan_a = _make_plan(db, name="甲", entitlements_value=None)
    plan_b = _make_plan(db, name="乙", entitlements_value={KEY_CONC: 6})
    _grant_subscription(db, user, plan_a, 30)
    db.commit()
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None
    # 顺延到不同档位：plan_id 跟随最近一次购买
    _grant_subscription(db, user, plan_b, 30)
    db.commit()
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 6


def test_codes_grant_invalidates_resolution_cache(db):
    user = _make_user(db)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) is None
    assert (user.id, None) in entitlements._USER_PLAN_CACHE
    codes.grant_membership_days(db, user, 30)
    db.commit()
    # 卡码发放口已挂失效：该用户的解析缓存被清掉
    assert (user.id, None) not in entitlements._USER_PLAN_CACHE


def test_orders_rollback_invalidates_resolution_cache(db):
    user = _make_user(db)
    plan = _make_plan(db, entitlements_value={KEY_CONC: 4})
    sub = _make_sub(db, user, plan, days=30)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 4
    assert (user.id, None) in entitlements._USER_PLAN_CACHE
    order = SimpleNamespace(days_granted=10, subscription_id=sub.id,
                            user_id=user.id)
    orders_admin._rollback_subscription(db, order, plan, datetime.now())
    db.commit()
    assert (user.id, None) not in entitlements._USER_PLAN_CACHE

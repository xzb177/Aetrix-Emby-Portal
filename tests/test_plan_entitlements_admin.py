"""订阅分档 PR-4：套餐权益写入口（admin）+ 商店下发（economy）。

钉住的口径（设计 reports/subscription-tier-design-20261024.md §6）：
- 写路径校验只走 entitlements.validate_entitlements 单口：未知键 / 非整数
  （含 bool、字符串、浮点）/ 越界 → 400 且不落库（create 不建行、update
  一个字段都不动）；
- 合法权益整对象落库；全空（None / {}）存 NULL 不存空对象；
- 落库后同进程缓存即时失效：先读旧值暖缓存，再经接口改权益，同一进程
  立刻读到新值（不靠 60 秒 TTL）；
- admin list 回显与存量一致（编辑表单回填的依据）；create/update 审计
  details 带 entitlements；
- 商店 /payment/plans 下发 entitlements 经 parse 归一：未声明 → {}，
  已声明 → 原值，存量坏值被剔除且不抛。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import entitlements, models
from backend.api.admin import (
    PlanUpsertRequest,
    economy_create_plan,
    economy_list_plans,
    economy_update_plan,
)
from backend.api.economy import payment_plans

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


@pytest.fixture()
def admin(db):
    u = models.WebUser(username="admin", password_hash="", points=0,
                       is_active=True, is_staff=True)
    db.add(u)
    db.commit()
    return u


def _req(**kw):
    base = {"name": "远行执照", "price": 45, "duration_days": 90}
    base.update(kw)
    return PlanUpsertRequest(**base)


def _plan_count(db):
    return db.query(models.SubscriptionPlan).count()


def _only_plan(db):
    return db.query(models.SubscriptionPlan).one()


# ==================== create：非法权益 400 且不落库 ====================

@pytest.mark.parametrize("bad", [
    {"unknown_key": 5},                 # 未登记键
    {KEY_SEEK: 0},                       # 低于下限
    {KEY_SEEK: 1001},                    # 超过上限
    {KEY_CONC: 21},                      # 同播超过上限（与 share_guard 对齐的 20）
    {KEY_SEEK: "25"},                    # 字符串不是整数
    {KEY_SEEK: 25.0},                    # 浮点不是整数
    {KEY_SEEK: True},                    # bool 不算整数
    {KEY_SEEK: 25, "bogus": 1},          # 一半合法一半非法 → 整份拒绝
])
def test_create_plan_rejects_invalid_entitlements(db, admin, bad):
    with pytest.raises(HTTPException) as ei:
        economy_create_plan(_req(entitlements=bad), admin, db)
    assert ei.value.status_code == 400
    assert _plan_count(db) == 0


def test_request_model_rejects_non_dict_entitlements():
    # 整体不是对象（数组/字符串）在请求模型层就被拦（生产里 FastAPI 回
    # 422，进不了 handler），与 handler 内的 validate 400 是前后两道防线
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        _req(entitlements=["not-a-dict"])


def test_create_plan_error_detail_names_the_key(db, admin):
    with pytest.raises(HTTPException) as ei:
        economy_create_plan(_req(entitlements={KEY_CONC: 99}), admin, db)
    assert KEY_CONC in ei.value.detail


# ==================== create：合法落库 / 全空存 NULL ====================

def test_create_plan_stores_valid_entitlements(db, admin):
    out = economy_create_plan(
        _req(entitlements={KEY_SEEK: 25, KEY_CONC: 3}), admin, db)
    plan = _only_plan(db)
    assert out["id"] == plan.id
    assert plan.entitlements == {KEY_SEEK: 25, KEY_CONC: 3}


@pytest.mark.parametrize("empty", [None, {}])
def test_create_plan_empty_entitlements_stored_as_null(db, admin, empty):
    economy_create_plan(_req(entitlements=empty), admin, db)
    assert _only_plan(db).entitlements is None


def test_create_plan_audit_details_carry_entitlements(db, admin):
    economy_create_plan(_req(entitlements={KEY_SEEK: 25}), admin, db)
    log = db.query(models.AdminLog).filter(
        models.AdminLog.action == "economy_create_plan").one()
    assert log.details["entitlements"] == {KEY_SEEK: 25}


# ==================== list 回显 ====================

def test_list_plans_echoes_entitlements_as_stored(db, admin):
    economy_create_plan(_req(entitlements={KEY_SEEK: 25}), admin, db)
    economy_create_plan(_req(name="启程执照", entitlements=None), admin, db)
    by_name = {p["name"]: p for p in economy_list_plans(db=db, current_admin=admin)["plans"]}
    assert by_name["远行执照"]["entitlements"] == {KEY_SEEK: 25}
    assert by_name["启程执照"]["entitlements"] is None


# ==================== update：校验 / 落库 / 清空 ====================

def test_update_plan_rejects_invalid_and_leaves_plan_untouched(db, admin):
    economy_create_plan(_req(entitlements={KEY_SEEK: 25}), admin, db)
    plan = _only_plan(db)
    with pytest.raises(HTTPException) as ei:
        economy_update_plan(
            plan.id,
            _req(name="改名不应生效", entitlements={KEY_SEEK: -1}),
            admin, db)
    assert ei.value.status_code == 400
    db.expire_all()
    plan = _only_plan(db)
    assert plan.name == "远行执照"          # 连名字都没被改动
    assert plan.entitlements == {KEY_SEEK: 25}


def test_update_plan_stores_and_clears_entitlements(db, admin):
    economy_create_plan(_req(entitlements=None), admin, db)
    plan = _only_plan(db)
    economy_update_plan(plan.id, _req(entitlements={KEY_CONC: 4}), admin, db)
    db.expire_all()
    assert _only_plan(db).entitlements == {KEY_CONC: 4}
    # 全空 → 回到 NULL（撤回声明 = 全员回退全站默认）
    economy_update_plan(plan.id, _req(entitlements={}), admin, db)
    db.expire_all()
    assert _only_plan(db).entitlements is None
    log = db.query(models.AdminLog).filter(
        models.AdminLog.action == "economy_update_plan").order_by(
        models.AdminLog.id.desc()).first()
    assert log.details["entitlements"] is None


# ==================== 落库后同进程缓存即时失效 ====================

def _make_subscriber(db, plan, username="buyer"):
    u = models.WebUser(username=username, password_hash="", points=0,
                       is_active=True)
    db.add(u)
    db.flush()
    now = datetime.now()
    db.add(models.UserSubscription(
        user_id=u.id, plan_id=plan.id, realm_id=None,
        status="active", start_date=now, end_date=now + timedelta(days=30),
    ))
    db.commit()
    return u


def test_update_via_api_invalidates_cache_same_process(db, admin):
    economy_create_plan(_req(entitlements={KEY_SEEK: 25}), admin, db)
    plan = _only_plan(db)
    user = _make_subscriber(db, plan)
    # 先读一次把旧值读进两级缓存
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) == 25
    assert entitlements._PLAN_SNAPSHOT_CACHE  # 缓存确实已暖
    # 经管理接口改权益：同进程立刻读到新值，不等 TTL
    economy_update_plan(plan.id, _req(entitlements={KEY_SEEK: 40}), admin, db)
    assert entitlements.get_user_entitlement(db, user, KEY_SEEK) == 40


def test_create_via_api_invalidates_snapshot_cache(db, admin):
    # 先让快照缓存里留一份「无此套餐」的旧结果，再建套餐也读得到
    economy_create_plan(_req(entitlements={KEY_CONC: 5}), admin, db)
    plan = _only_plan(db)
    user = _make_subscriber(db, plan)
    assert entitlements.get_user_entitlement(db, user, KEY_CONC) == 5


# ==================== 商店下发（/payment/plans） ====================

def test_payment_plans_delivers_parsed_entitlements(db, admin):
    economy_create_plan(_req(entitlements={KEY_SEEK: 25, KEY_CONC: 3}), admin, db)
    economy_create_plan(_req(name="启程执照", entitlements=None), admin, db)
    out = payment_plans(db=db)
    by_name = {p["name"]: p for p in out["plans"]}
    assert by_name["远行执照"]["entitlements"] == {KEY_SEEK: 25, KEY_CONC: 3}
    # 未声明的套餐下发空对象（不是 None），前端据此不生成权益行
    assert by_name["启程执照"]["entitlements"] == {}


def test_payment_plans_filters_dirty_stored_values(db, admin):
    economy_create_plan(_req(entitlements={KEY_SEEK: 25}), admin, db)
    plan = _only_plan(db)
    # 模拟人工改库写坏：parse 归一剔除坏值，不抛、不把脏值下发给商店
    plan.entitlements = {KEY_SEEK: "脏值", "unknown_key": 1, KEY_CONC: 2}
    db.commit()
    out = payment_plans(db=db)
    assert out["plans"][0]["entitlements"] == {KEY_CONC: 2}

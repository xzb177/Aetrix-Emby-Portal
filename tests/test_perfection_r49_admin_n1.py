"""r49 后端完美迭代：管理后台 API 全链路 N+1 三项修复 + view_grant hardening 回归。

F1（welfare_admin.py）：求片审核列表 `/welfare/requests` 读 r.user.username，
    MediaRequest.user 是 lazy relationship，查询无 joinedload → 每页 ≤100 条额外 SQL。
    修法：查询加 .options(joinedload(MediaRequest.user))。
F2（welfare_admin.py）：抽奖黑名单列表 `/welfare/lottery/blacklist` 循环内逐行
    query WebUser → 每页 ≤100 条额外 SQL。修法：一次 IN 查完 user_id→username 映射。
F3（coupons_admin.py）：建券 `create_coupons` 循环内逐码查 DB 查重（count≤200）。
    修法：一次 IN 查完已存在码集，循环内只做集合命中判定，400 语义不变。
F4（subscriptions.py）：view_grant 付费分支此前用 resolve_realm_id(realm_id)，
    与免费分支的 _scoped_realm_id(db, realm_id) 口径不一致；现有调用方下等价，
    改为 target（零行为 hardening）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
import contextlib

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models, subscriptions
from backend.api import welfare_admin
from backend.api.coupons_admin import CouponCreateRequest, create_coupons
from backend.api.welfare_admin import list_lottery_blacklist, welfare_requests


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session._r49_engine = engine
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


class _Counter:
    def __init__(self):
        self.n = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.n += 1


def _count_statements(db):
    counter = _Counter()
    engine = db._r49_engine
    event.listen(engine, "before_cursor_execute", counter)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", counter)


_count_statements = contextlib.contextmanager(_count_statements)


def _user(db, username, is_staff=False):
    u = models.WebUser(username=username, password_hash="x", is_active=True,
                       is_staff=is_staff)
    db.add(u)
    db.commit()
    return u


def _realm(db, name="主服", slug="main", access_mode="paid"):
    r = models.ServerRealm(name=name, slug=slug, access_mode=access_mode,
                           is_active=True)
    db.add(r)
    db.commit()
    return r


# ---------------------------------------------------------------------------
# F1：求片审核列表 joinedload，1 次 count + 1 次带 JOIN 的查询，不再逐行回查
# ---------------------------------------------------------------------------

def test_welfare_requests_no_n_plus_one(db):
    admin = _user(db, "admin", is_staff=True)
    u1 = _user(db, "u1")
    u2 = _user(db, "u2")
    for i in range(5):
        db.add(models.MediaRequest(
            user_id=u1.id if i % 2 == 0 else u2.id,
            tmdb_id=str(1000 + i), media_type="movie", title=f"片{i}",
            status="pending"))
    db.commit()

    with _count_statements(db) as c:
        resp = welfare_requests(page=1, page_size=20, status=None,
                                current_admin=admin, db=db)
    # count(1) + 列表查询(1，含 LEFT JOIN 一次性带出 user)；此前是 1 + 1 + 5
    assert c.n == 2, f"期望 2 条 SQL，实际 {c.n}"
    names = {it["username"] for it in resp["items"]}
    assert names == {"u1", "u2"}
    assert resp["total"] == 5


# ---------------------------------------------------------------------------
# F2：黑名单列表一次 IN 查完用户名；用户名缺失（用户已删）时保持 None 口径
# ---------------------------------------------------------------------------

def test_lottery_blacklist_batch_usernames(db):
    admin = _user(db, "admin", is_staff=True)
    u1 = _user(db, "u1")
    u2 = _user(db, "u2")
    db.add(models.LotteryBlacklist(user_id=u1.id, reason="刷奖"))
    db.add(models.LotteryBlacklist(user_id=u2.id, reason=""))
    db.add(models.LotteryBlacklist(user_id=999999, reason="幽灵"))  # 用户已不存在
    db.commit()

    with _count_statements(db) as c:
        resp = list_lottery_blacklist(page=1, page_size=20,
                                      current_admin=admin, db=db)
    # count(1) + 列表(1) + IN 查用户名(1)；此前是 3 + 3
    assert c.n == 3, f"期望 3 条 SQL，实际 {c.n}"
    by_uid = {it["user_id"]: it["username"] for it in resp["items"]}
    assert by_uid[u1.id] == "u1"
    assert by_uid[u2.id] == "u2"
    assert by_uid[999999] is None  # 与原 getattr(u, "username", None) 口径一致
    assert resp["total"] == 3


# ---------------------------------------------------------------------------
# F3：建券查重批量化；显式重复码仍 400，语义不变；随机码建券正常
# ---------------------------------------------------------------------------

def test_create_coupons_explicit_duplicate_still_400(db):
    admin = _user(db, "admin", is_staff=True)
    db.add(models.CouponCode(code="FIXED1", kind="all", discount_type="percent",
                             value=90, is_active=True, created_by=admin.id))
    db.commit()

    with _count_statements(db) as c:
        try:
            create_coupons(CouponCreateRequest(code="FIXED1", count=1),
                           current_admin=admin, db=db)
        except Exception as exc:  # HTTPException
            assert getattr(exc, "status_code", None) == 400
            assert "FIXED1" in str(getattr(exc, "detail", ""))
        else:
            pytest.fail("重复显式码应抛 400")
    # 一次 IN 查即判出重复；此前至少 1 次逐码查
    assert c.n == 1, f"期望 1 条 SQL，实际 {c.n}"


def test_create_coupons_random_batch_ok(db):
    admin = _user(db, "admin", is_staff=True)
    resp = create_coupons(CouponCreateRequest(count=3), current_admin=admin, db=db)
    assert resp["success"] is True and resp["count"] == 3
    codes = [c["code"] for c in resp["coupons"]]
    assert len(set(codes)) == 3
    assert db.query(models.CouponCode).count() == 3


# ---------------------------------------------------------------------------
# F4：view_grant 付费分支改 target 后回归（调用方传入具体服 id，行为不变）
# ---------------------------------------------------------------------------

def test_view_grant_paid_realm_parity(db):
    realm = _realm(db, access_mode="paid")
    plan = models.SubscriptionPlan(name="月卡", price=10, duration_days=30,
                                   realm_id=realm.id, is_active=True)
    db.add(plan)
    db.commit()
    user = _user(db, "alice")
    db.add(models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id,
        start_date=datetime.now() - timedelta(days=1),
        end_date=datetime.now() + timedelta(days=30),
        status="active"))
    db.commit()

    granted, unlock = subscriptions.view_grant(db, user, realm.id)
    assert granted is True and unlock is None
    # 无订阅用户仍无权限
    bob = _user(db, "bob")
    assert subscriptions.view_grant(db, bob, realm.id)[0] is False

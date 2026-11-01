# -*- coding: utf-8 -*-
"""白名单码（code_type=3）永久生效回归测试（2026-11-01 用户实测没生效）。

既有机制：白名单码生成时 days=-1，核销 ``redeem_code`` 按 reward_type 分派。
断点（修复前）：一张白名单码只永久化 reward_type 对应的那一处——
- reward_type=subscription（默认）→ 只有订阅 +100 年，公益资格照旧到期、
  查看凭证根本不发；
- reward_type=welfare → 资格＋打包查看行永久，但订阅不发；
- reward_type=points/discount → 白名单语义完全丢失（只发能量/折扣）。

修复口径：白名单码核销 = 账号置为长期有效，三处一起落：
公益资格永久（grant_welfare days=0）＋查看行永久（expires_at=None）＋
该服订阅长期（grant_membership_days days=-1 → PERMANENT_DAYS）。
非白名单码行为逐字不变。本文件同时覆盖管理端「白名单账号」名单/回收。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

# 注意：本文件不许在导入时重绑全局 backend.database.engine。
# 套件同进程共享最后一次导入绑定的引擎（见 test_zz_fixture_hygiene.py）；
# 本文件此前在导入时把全局引擎绑到一个未 init_db 的空临时库，
# 导致之后所有依赖全局 SessionLocal 的测试在 setup 报 no such table
# （#1668 冒烟两轮同形态失败的根因）。本文件夹具自带内存库，不需要全局引擎。
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import pytest  # noqa: E402

from backend import codes, models, subscriptions  # noqa: E402
from backend.emby_server import portal  # noqa: E402


@pytest.fixture()
def db():
    # StaticPool + check_same_thread=False：TestClient 在另一线程跑 app，
    # 同一条连接要能跨线程用（与生产 PG 的连接池语义无关，仅测试夹具）。
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, username):
    u = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(u)
    db.flush()
    return u


def _code(db, code_str, **kw):
    fields = {"max_uses": 1, "use_count": 0, "is_active": True,
              "code_type": codes.CODE_TYPE_WHITELIST, "days": -1,
              "reward_type": "subscription"}
    fields.update(kw)
    c = models.RegistrationCode(code=code_str, **fields)
    db.add(c)
    db.flush()
    return c


def _assert_all_permanent(db, user):
    db.refresh(user)
    # ① 公益资格永久
    assert user.is_welfare is True
    assert user.welfare_expires_at is None
    assert portal.is_welfare_user(db, user) is True
    # ② 查看凭证永久（直接查查看行：view_grant 在付费服口径下以订阅
    # 授予、row 返回 None，裸测试库未给 realm 1 标公益服，故不断言 row）
    unlock = (db.query(models.EmbyViewUnlock)
              .filter(models.EmbyViewUnlock.user_id == user.id).first())
    assert unlock is not None and unlock.expires_at is None
    granted, _row = subscriptions.view_grant(db, user)
    assert granted is True
    # ③ 订阅长期有效（PERMANENT_DAYS=36500 的既有「永久近似」口径）
    assert subscriptions.has_active_subscription(db, user.id) is True
    sub = (db.query(models.UserSubscription)
           .filter(models.UserSubscription.user_id == user.id,
                   models.UserSubscription.status == "active")
           .order_by(models.UserSubscription.end_date.desc()).first())
    assert sub is not None
    assert sub.end_date > datetime.now() + timedelta(days=36000)


# ---------- 1. 三种 reward_type 的白名单码都必须三处永久 ----------

def test_whitelist_subscription_flavor_grants_all_three(db):
    u = _user(db, "wl_sub")
    _code(db, "VIP-SUB000000001")
    db.commit()
    result = codes.redeem_code(db, u, "VIP-SUB000000001")
    db.commit()
    assert result["success"] is True
    _assert_all_permanent(db, u)


def test_whitelist_welfare_flavor_grants_all_three(db):
    u = _user(db, "wl_welfare")
    _code(db, "VIP-WEL000000001", reward_type="welfare")
    db.commit()
    result = codes.redeem_code(db, u, "VIP-WEL000000001")
    db.commit()
    assert result["success"] is True
    _assert_all_permanent(db, u)


def test_whitelist_points_flavor_still_means_permanent(db):
    u = _user(db, "wl_points")
    _code(db, "VIP-PTS000000001", reward_type="points", points_value=50)
    db.commit()
    result = codes.redeem_code(db, u, "VIP-PTS000000001")
    db.commit()
    assert result["success"] is True
    _assert_all_permanent(db, u)


def test_whitelist_redeem_stacks_on_existing_finite_rights(db):
    """已有有限期资格/订阅/查看行时核销白名单码 → 全部升为永久，不报错。"""
    u = _user(db, "wl_stack")
    u.is_welfare = True
    u.welfare_expires_at = datetime.now() + timedelta(days=10)
    db.add(models.EmbyViewUnlock(
        user_id=u.id, realm_id=1, unlocked_at=datetime.now(),
        expires_at=datetime.now() + timedelta(days=5), points_spent=50))
    _code(db, "VIP-STK000000001", reward_type="welfare")
    db.commit()
    result = codes.redeem_code(db, u, "VIP-STK000000001")
    db.commit()
    assert result["success"] is True
    _assert_all_permanent(db, u)


# ---------- 2. 非白名单码行为不变 ----------

def test_renew_code_unchanged(db):
    u = _user(db, "plain_renew")
    _code(db, "REN-PLAIN000001", code_type=codes.CODE_TYPE_RENEW, days=30)
    db.commit()
    result = codes.redeem_code(db, u, "REN-PLAIN000001")
    db.commit()
    assert result["success"] is True
    db.refresh(u)
    assert not u.is_welfare
    granted, row = subscriptions.view_grant(db, u)
    # 查看口径里有效订阅本身即 granted，但不得生成永久查看行
    assert row is None
    sub = (db.query(models.UserSubscription)
           .filter(models.UserSubscription.user_id == u.id).first())
    assert sub is not None
    assert sub.end_date < datetime.now() + timedelta(days=31)


def test_welfare_code_unchanged_no_subscription(db):
    """普通资格码（code_type=注册码 + reward_type=welfare）仍只发资格+查看，
    不发订阅——白名单三处全包只属于 code_type=3。"""
    u = _user(db, "plain_welfare")
    _code(db, "REG-WEL000000001", code_type=codes.CODE_TYPE_REGISTER,
          days=30, reward_type="welfare")
    db.commit()
    result = codes.redeem_code(db, u, "REG-WEL000000001")
    db.commit()
    assert result["success"] is True
    db.refresh(u)
    assert u.is_welfare is True
    assert u.welfare_expires_at is not None  # 有限期，不是永久
    assert db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == u.id).count() == 0


# ---------- 3. 管理端名单 / 回收 ----------

@pytest.fixture()
def admin_client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.admin_core import get_current_admin
    from backend.api.admin_ops import admin_ops_router
    from backend.database import get_db

    admin = models.WebUser(username="boss", password_hash="x", is_active=True)
    db.add(admin)
    db.commit()

    app = FastAPI()
    app.include_router(admin_ops_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        yield TestClient(app, raise_server_exceptions=False), admin
    finally:
        app.dependency_overrides.clear()


def test_admin_whitelist_accounts_list_and_revoke(db, admin_client):
    client, _admin = admin_client
    u = _user(db, "wl_listed")
    _code(db, "VIP-LST000000001")
    db.commit()
    assert codes.redeem_code(db, u, "VIP-LST000000001")["success"] is True
    db.commit()

    r = client.get("/api/admin/registration-codes/whitelist-accounts")
    assert r.status_code == 200
    rows = r.json()["accounts"]
    mine = [x for x in rows if x["user_id"] == u.id]
    assert len(mine) == 1
    assert mine[0]["username"] == "wl_listed"
    assert mine[0]["welfare_permanent"] is True
    assert mine[0]["view_permanent"] is True
    assert mine[0]["subscription_longterm"] is True
    assert mine[0]["is_permanent_active"] is True

    r = client.post(
        f"/api/admin/registration-codes/whitelist-accounts/{u.id}/revoke")
    assert r.status_code == 200
    db.expire_all()
    db.refresh(u)
    assert portal.is_welfare_user(db, u) is False
    granted, _row = subscriptions.view_grant(db, u)
    assert granted is False
    assert subscriptions.has_active_subscription(db, u.id) is False

    r = client.get("/api/admin/registration-codes/whitelist-accounts")
    mine = [x for x in r.json()["accounts"] if x["user_id"] == u.id]
    assert len(mine) == 1  # 仍留痕，但已非永久生效
    assert mine[0]["is_permanent_active"] is False


def test_admin_whitelist_accounts_requires_auth(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.admin_ops import admin_ops_router
    from backend.database import get_db

    app = FastAPI()
    app.include_router(admin_ops_router)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/admin/registration-codes/whitelist-accounts")
    assert r.status_code in (401, 403)
    r = client.post("/api/admin/registration-codes/whitelist-accounts/1/revoke")
    assert r.status_code in (401, 403)

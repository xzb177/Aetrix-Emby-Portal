# -*- coding: utf-8 -*-
"""白名单「创世编号 + 专属徽章/称号」回归测试（独一无二体验包·身份组）。

口径：
- 编号按「成为白名单的先后」分配（核销痕迹时间，见 codes._whitelist_traces），
  No.1 起，一旦分配永不变更、永不复用（回收后再核销仍是原号，新人不得
  占走已分配的号）。
- 存量白名单用户按现有痕迹时间顺序补号：CodeRedemption.created_at 为准；
  只有 used_by 老痕迹（无核销记录行）的按码创建时间排。
- 展示只挂现行白名单判定（codes.is_whitelist_account，#1668/#1670 口径）：
  生效中才出徽章/称号/编号，普通用户与已回收用户一律不出。
- 分配并发安全靠 whitelist_serials 的 user_id / serial 双唯一约束兜底。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

# 本文件夹具自带内存库，不在导入时重绑全局引擎（见
# test_zz_fixture_hygiene.py 的卫生纪律）。
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import pytest  # noqa: E402

from backend import codes, models  # noqa: E402


@pytest.fixture()
def db():
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


def _redeem(db, user, code_str):
    result = codes.redeem_code(db, user, code_str)
    db.commit()
    assert result["success"] is True
    return result


def _make_permanent(db, user):
    """不走核销、直接把三处永久摆好（模拟存量白名单用户）。"""
    user.is_welfare = True
    user.welfare_expires_at = None
    db.add(models.EmbyViewUnlock(
        user_id=user.id, realm_id=1, unlocked_at=datetime.now(),
        expires_at=None, points_spent=0))
    plan = db.query(models.SubscriptionPlan).first()
    if plan is None:
        plan = models.SubscriptionPlan(name="t", price=0, duration_days=30)
        db.add(plan)
        db.flush()
    db.add(models.UserSubscription(
        user_id=user.id, plan_id=plan.id, status="active",
        start_date=datetime.now(),
        end_date=datetime.now() + timedelta(days=36500)))
    db.flush()


# ---------- 1. 核销顺序分配 ----------

def test_serials_follow_redeem_order(db):
    a = _user(db, "wl_first")
    b = _user(db, "wl_second")
    c = _user(db, "wl_third")
    _code(db, "VIP-ORD000000001")
    _code(db, "VIP-ORD000000002")
    _code(db, "VIP-ORD000000003")
    db.commit()
    _redeem(db, b, "VIP-ORD000000002")
    _redeem(db, a, "VIP-ORD000000001")
    _redeem(db, c, "VIP-ORD000000003")
    assert codes.whitelist_serial_for(db, b.id) == 1
    assert codes.whitelist_serial_for(db, a.id) == 2
    assert codes.whitelist_serial_for(db, c.id) == 3


def test_serial_never_changes_on_repeat_reads(db):
    u = _user(db, "wl_stable")
    _code(db, "VIP-STB000000001")
    db.commit()
    _redeem(db, u, "VIP-STB000000001")
    first = codes.whitelist_identity_payload(db, u)
    second = codes.whitelist_identity_payload(db, u)
    assert first is not None and first["serial"] == 1
    assert second == first
    rows = db.query(models.WhitelistSerial).all()
    assert len(rows) == 1


# ---------- 2. 存量补号（按痕迹时间） ----------

def test_backfill_orders_by_redemption_time(db):
    """存量用户（已有痕迹+永久、尚无编号）按核销时间先后补号，
    与用户 id 大小、补号调用时机无关。"""
    early = _user(db, "wl_early")
    late = _user(db, "wl_late")
    code_e = _code(db, "VIP-BFE000000001")
    code_l = _code(db, "VIP-BFL000000001")
    t0 = datetime.now() - timedelta(days=10)
    db.add(models.CodeRedemption(
        code_kind=codes.REDEMPTION_KIND_REG, code_id=code_l.id,
        user_id=late.id, created_at=t0 + timedelta(days=5)))
    db.add(models.CodeRedemption(
        code_kind=codes.REDEMPTION_KIND_REG, code_id=code_e.id,
        user_id=early.id, created_at=t0))
    _make_permanent(db, early)
    _make_permanent(db, late)
    db.commit()
    # 先问晚核销的人，也必须先给早核销的人补 1 号
    payload = codes.whitelist_identity_payload(db, late)
    assert payload is not None and payload["serial"] == 2
    assert codes.whitelist_serial_for(db, early.id) == 1


def test_backfill_used_by_only_trace_uses_code_time(db):
    """只有 used_by 老痕迹（无 CodeRedemption 行）时按码创建时间排，
    排在有明确核销时间且更晚的痕迹之前/之后都按时间序。"""
    legacy = _user(db, "wl_legacy")
    modern = _user(db, "wl_modern")
    old_code = _code(db, "VIP-LEG000000001")
    old_code.used_by = str(legacy.id)
    old_code.created_at = datetime.now() - timedelta(days=30)
    new_code = _code(db, "VIP-MOD000000001")
    db.add(models.CodeRedemption(
        code_kind=codes.REDEMPTION_KIND_REG, code_id=new_code.id,
        user_id=modern.id, created_at=datetime.now() - timedelta(days=1)))
    _make_permanent(db, legacy)
    _make_permanent(db, modern)
    db.commit()
    codes.ensure_whitelist_serials(db)
    assert codes.whitelist_serial_for(db, legacy.id) == 1
    assert codes.whitelist_serial_for(db, modern.id) == 2


# ---------- 3. 永不复用 / 回收后再核销仍是原号 ----------

def test_serial_not_reused_after_revoke_and_new_redeem(db):
    a = _user(db, "wl_revoked")
    b = _user(db, "wl_newcomer")
    _code(db, "VIP-RVK000000001")
    _code(db, "VIP-RVK000000002", max_uses=1)
    _code(db, "VIP-RVK000000003")
    db.commit()
    _redeem(db, a, "VIP-RVK000000001")
    assert codes.whitelist_serial_for(db, a.id) == 1
    codes.revoke_whitelist_account(db, a.id)
    db.commit()
    # 回收后徽章下线，但编号保留不放出
    assert codes.whitelist_identity_payload(db, a) is None
    _redeem(db, b, "VIP-RVK000000002")
    assert codes.whitelist_serial_for(db, b.id) == 2
    # A 重新核销 → 仍是 No.1
    _redeem(db, a, "VIP-RVK000000003")
    assert codes.whitelist_serial_for(db, a.id) == 1
    payload = codes.whitelist_identity_payload(db, a)
    assert payload is not None and payload["serial"] == 1


def test_serial_unique_constraints_backstop(db):
    """双唯一约束兜底：同 user 二行、同 serial 二人，库层直接拒绝。"""
    db.add(models.WhitelistSerial(user_id=101, serial=1))
    db.commit()
    db.add(models.WhitelistSerial(user_id=102, serial=1))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(models.WhitelistSerial(user_id=101, serial=2))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------- 4. 展示口径（payload + /me 出参） ----------

def test_payload_none_for_plain_user(db):
    u = _user(db, "plain_user")
    db.commit()
    assert codes.is_whitelist_account(db, u) is False
    assert codes.whitelist_identity_payload(db, u) is None
    assert codes.whitelist_serial_for(db, u.id) is None
    # 普通用户不该被补号流程误伤
    codes.ensure_whitelist_serials(db)
    assert db.query(models.WhitelistSerial).count() == 0


def test_payload_shape_for_whitelist_user(db):
    u = _user(db, "wl_shown")
    _code(db, "VIP-SHW000000001")
    db.commit()
    _redeem(db, u, "VIP-SHW000000001")
    payload = codes.whitelist_identity_payload(db, u)
    assert payload is not None
    assert payload["serial"] == 1
    assert payload["label"] == "No.1"
    assert payload["title"] == codes.WHITELIST_TITLE
    assert payload["title"]  # 固定称号文案非空


def test_user_out_carries_whitelist_identity(db):
    from backend.api.emby_portal import _user_out

    wl = _user(db, "wl_me")
    plain = _user(db, "plain_me")
    _code(db, "VIP-ME0000000001")
    db.commit()
    _redeem(db, wl, "VIP-ME0000000001")
    out_wl = _user_out(wl, db)
    assert out_wl.whitelist_identity is not None
    assert out_wl.whitelist_identity.serial == 1
    assert out_wl.whitelist_identity.label == "No.1"
    assert out_wl.whitelist_identity.title == codes.WHITELIST_TITLE
    out_plain = _user_out(plain, db)
    assert out_plain.whitelist_identity is None

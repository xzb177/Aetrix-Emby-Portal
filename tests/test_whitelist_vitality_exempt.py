# -*- coding: utf-8 -*-
"""白名单（永久）账号活力豁免回归测试（2026-11-01 用户亲定）。

口径：白名单账号判定只认 codes 的名单定义（核销痕迹＋三处永久现算，
即 codes.whitelist_accounts / is_whitelist_account 同一来源），活力侧
不另立第二套名单。豁免范围只有两处执行点：
- vitality.daily_deduct：白名单账号跳过每日扣减（计数 skipped_whitelist）；
- vitality.ensure_can_play：白名单账号哪怕活力 0 也恒放行。
非白名单用户行为逐字不变；回收白名单后豁免随三处永久失效一并消失。
造数一律走真实核销路径（codes.redeem_code），与 #1668 名单同批数据同结果。
"""
import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import database as _dbmod  # noqa: E402

_dbmod.engine = create_engine(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(sessionmaker(bind=_dbmod.engine))
# 换绑后必须立刻建全表：pytest 组合跑时 collection 先导入所有测试文件，
# 后导入文件的换绑会让全局 SessionLocal 指向新库；若新库没建表，先导入
# 文件（如 test_admin_task_cancel）的 fixture 用环境 SessionLocal 就会撞
# "no such table" 级联毒化（与 test_admin_task_cancel 导入期 init_db 同款）。
_dbmod.init_db()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from backend import codes, models, vitality  # noqa: E402
from backend.integrations import store as _store  # noqa: E402


@pytest.fixture()
def db():
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    # 配置有进程内 TTL 热缓存：别的测试改过的活力开关不能串进来
    _store.invalidate("vitality_enabled", "vitality_daily_cost", "vitality_max",
                      "vitality_limit_threshold",
                      vitality.CONFIG_LAST_DEDUCT_DATE)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, username, **kw):
    fields = {"password_hash": "x", "is_active": True}
    fields.update(kw)
    u = models.WebUser(username=username, **fields)
    db.add(u)
    db.flush()
    return u


def _whitelist_user(db, username, vitality_value=5):
    """走真实核销路径把账号变成白名单（永久）账号，并设定活力值。"""
    u = _user(db, username)
    c = models.RegistrationCode(
        code=f"VIP-{username.upper()[:12]:0<12}",
        max_uses=1, use_count=0, is_active=True,
        code_type=codes.CODE_TYPE_WHITELIST, days=-1,
        reward_type="subscription")
    db.add(c)
    db.commit()
    result = codes.redeem_code(db, u, c.code)
    assert result["success"] is True
    u.vitality = vitality_value
    db.commit()
    db.refresh(u)
    return u


# ---------- 1. 名单口径一致：#1668 名单与活力豁免判定同批数据同结果 ----------

def test_roster_and_exemption_agree(db):
    wl = _whitelist_user(db, "wl_roster")
    plain = _user(db, "plain_roster", is_welfare=True, vitality=5)
    db.commit()
    roster = {r["user_id"]: r for r in codes.whitelist_accounts(db)}
    assert wl.id in roster and roster[wl.id]["is_permanent_active"] is True
    assert codes.is_whitelist_account(db, wl) is True
    assert wl.id in codes.whitelist_account_ids(db)
    # 普通公益用户不在名单、也不豁免
    assert plain.id not in roster
    assert codes.is_whitelist_account(db, plain) is False
    assert plain.id not in codes.whitelist_account_ids(db)


# ---------- 2. 每日扣减：白名单跳过，普通公益用户照旧 ----------

def test_daily_deduct_skips_whitelist_only(db):
    wl = _whitelist_user(db, "wl_deduct", vitality_value=5)
    plain = _user(db, "plain_deduct", is_welfare=True, vitality=5)
    zero = _user(db, "zero_deduct", is_welfare=True, vitality=1)
    db.commit()
    res = vitality.daily_deduct(db)
    assert res["skipped_whitelist"] == 1
    assert res["deducted"] == 2  # plain 与 zero 照旧扣
    db.refresh(wl)
    db.refresh(plain)
    db.refresh(zero)
    assert wl.vitality == 5  # 分文未扣
    assert plain.vitality == 4
    assert zero.vitality == 0
    # 白名单用户不产生 daily_deduct 流水
    logs = (db.query(models.VitalityLog)
            .filter(models.VitalityLog.user_id == wl.id,
                    models.VitalityLog.reason == "daily_deduct").all())
    assert logs == []


# ---------- 3. 播放阈值：白名单活力 0 也放行，普通用户照旧拦 ----------

def test_ensure_can_play_whitelist_passes_at_zero(db):
    wl = _whitelist_user(db, "wl_play", vitality_value=0)
    # 不抛异常即放行
    vitality.ensure_can_play(db, wl)


def test_ensure_can_play_plain_still_blocked(db):
    for name, v in [("low_play", 2), ("zero_play", 0)]:
        u = _user(db, name, is_welfare=True, vitality=v)
        db.commit()
        with pytest.raises(HTTPException) as e:
            vitality.ensure_can_play(db, u)
        assert e.value.status_code == 403


# ---------- 4. 回收 / 永久不全 → 豁免消失 ----------

def test_revoked_whitelist_loses_exemption(db):
    wl = _whitelist_user(db, "wl_revoke", vitality_value=5)
    assert codes.is_whitelist_account(db, wl) is True
    vitality.ensure_can_play(db, wl)  # 活力够时本来就放行，先确认基线

    result = codes.revoke_whitelist_account(db, wl.id)
    assert result is not None
    db.commit()
    db.refresh(wl)
    assert codes.is_whitelist_account(db, wl) is False

    # 回收后照普通公益用户待遇：活力 0 被拦、日扣照旧
    wl.vitality = 0
    db.commit()
    with pytest.raises(HTTPException) as e:
        vitality.ensure_can_play(db, wl)
    assert e.value.status_code == 403
    wl.vitality = 5
    db.commit()
    res = vitality.daily_deduct(db)
    assert res["skipped_whitelist"] == 0
    db.refresh(wl)
    assert wl.vitality == 4


def test_partial_permanence_is_not_exempt(db):
    """三处永久缺一（如查看行被改成有限期）→ 不算生效中的白名单，不豁免。

    与 whitelist_accounts 的 is_permanent_active 现算口径一致。
    """
    wl = _whitelist_user(db, "wl_partial", vitality_value=5)
    unlock = (db.query(models.EmbyViewUnlock)
              .filter(models.EmbyViewUnlock.user_id == wl.id).first())
    assert unlock is not None and unlock.expires_at is None
    unlock.expires_at = datetime.now() + timedelta(days=30)
    db.commit()
    roster = {r["user_id"]: r for r in codes.whitelist_accounts(db)}
    assert roster[wl.id]["is_permanent_active"] is False
    assert codes.is_whitelist_account(db, wl) is False
    res = vitality.daily_deduct(db)
    assert res["skipped_whitelist"] == 0
    db.refresh(wl)
    assert wl.vitality == 4

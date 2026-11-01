# -*- coding: utf-8 -*-
"""公益资格自助兑换门槛：

1. 双钥匙（唯一兑换门槛）：自助兑换前置＝注册满 N 天（默认 3）＋近 7 天
   活跃 M 天（默认 2；签到/播放为硬信号，发言天数最多计 1 天）。阈值走
   SystemConfig。
2. 能量不分来源（2026-11-02 用户亲令删除来源限定）：2026-10-30 曾有
   「兑换只认自挣能量」门槛（welfare_redeem_self_earned_only），已随
   订阅侧的 subscription_buy_self_earned_only 一并删除——赠送/中奖/
   转入与自挣同权，兑换只看总余额＋双钥匙。来源分类
   （POINT_TYPE_SELF_EARNED）与自挣可用额公式作为台账分类保留（本文件
   1、2 节仍钉其口径），不再有任何消费口用它拦截；生产库里残留的旧键
   SystemConfig 行读侧已无人读、自动忽略（本文件有专测钉住）。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import ast
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.database import Base
import backend.emby_server.models as em_models  # noqa: F401  注册播放会话等表
from backend import points as points_mod
from backend.api import economy


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _make_user(db, username, age_days=10, points=0):
    u = models.WebUser(username=username, password_hash="x",
                       is_active=True, points=points)
    db.add(u)
    db.commit()
    db.refresh(u)
    if age_days is not None:
        u.created_at = datetime.now() - timedelta(days=age_days)
        db.commit()
        db.refresh(u)
    return u


def _log(db, user, amount, type_, days_ago=0, hour=5):
    """写一条积分流水并同步余额（保持 台账合计 == 余额 的一致性）。"""
    ts = economy._today_start_utc_naive() - timedelta(days=days_ago) \
        + timedelta(hours=hour)
    new_bal = int(user.points or 0) + amount
    db.add(models.PointsLog(user_id=user.id, amount=amount,
                            balance_after=new_bal, type=type_,
                            description="test", created_at=ts))
    user.points = new_bal
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _checkin_on(db, user, days_ago):
    ts = economy._today_start_utc_naive() - timedelta(days=days_ago)
    db.add(models.CheckinRecord(user_id=user.id, checkin_date=ts,
                                 points_awarded=0, streak=1))
    db.commit()


def _playback_on(db, user, days_ago):
    ts = economy._today_start_utc_naive() - timedelta(days=days_ago) \
        + timedelta(hours=3)
    db.add(em_models.PlaybackSession(
        session_key=f"gate-test-{uuid.uuid4().hex}", user_id=user.id,
        start_time=ts))
    db.commit()


def _active_user(db, username, age_days=10):
    """注册够老、近两天有签到的用户（双钥匙默认达标）。"""
    u = _make_user(db, username, age_days=age_days)
    _checkin_on(db, u, 0)
    _checkin_on(db, u, 1)
    return u


# ==================== 1. 来源分类（单一映射） ====================

def test_classification_spot_checks():
    earned = points_mod.POINT_TYPE_SELF_EARNED
    # 自挣：本人行为入账
    assert earned["checkin"] is True
    assert earned["chat"] is True  # 发言＋站内影评共用 award_chat_points
    # 非自挣：他人/系统/运气来源
    for t in ("transfer_in", "invitee", "rebate", "promotion", "redpacket",
              "code_redeem", "exchange", "recharge", "refund", "admin_grant",
              "lottery_win", "slots_win", "slots_jackpot", "slots_refund",
              "rankpush"):
        assert earned[t] is False, t
    # 未知来源 fail-closed：不算自挣
    assert points_mod.is_self_earned_point_type("some_future_type") is False
    assert points_mod.is_self_earned_point_type("checkin") is True


def test_classification_covers_all_backend_call_sites():
    """全仓每个写积分台账的调用点用到的 type 字面量都必须在分类表里，

    防止新增入账来源绕过分类（横切只许一套：分类只在 points.py 一处）。
    """
    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    # 函数名 -> type 参数位置
    targets = {"_add_points": 3, "_spend_points": 3, "_append_points_log": 4}
    found = {}
    for path in sorted(backend_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else \
                func.attr if isinstance(func, ast.Attribute) else None
            if name not in targets or len(node.args) <= targets[name]:
                continue
            arg = node.args[targets[name]]
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    found.setdefault(sub.value, str(path))
    assert found, "应至少扫描到若干 type 字面量"
    missing = {t: p for t, p in found.items()
               if t not in points_mod.POINT_TYPE_SELF_EARNED}
    assert not missing, f"未分类的积分来源 type: {missing}"
    # rankpush 的 type 走模块常量，不在字面量扫描面内，单独锁
    from backend.tg_bot import rankpush
    assert rankpush.REWARD_TYPE in points_mod.POINT_TYPE_SELF_EARNED


# ==================== 2. 自挣可用额公式 ====================

def test_self_earned_formula_mixed_and_spend_first(db):
    u = _make_user(db, "formula1")
    _log(db, u, 400, "chat", days_ago=3)
    _log(db, u, 200, "transfer_in", days_ago=2)
    _log(db, u, -250, "view_unlock", days_ago=1)
    # 余额 350；自挣 = min(350, 400 − 250) = 150（支出先扣自挣）
    assert int(u.points) == 350
    assert points_mod.self_earned_balance(db, u) == 150
    # 非自挣入账不抬自挣额
    _log(db, u, 100, "admin_grant", days_ago=0)
    assert points_mod.self_earned_balance(db, u) == 150
    # 退注类回款不是行为入账，不恢复自挣额（与红包退回同口径，见 PR 说明）
    _log(db, u, 50, "slots_refund", days_ago=0, hour=6)
    assert points_mod.self_earned_balance(db, u) == 150


def test_self_earned_formula_floor_and_balance_cap(db):
    u = _make_user(db, "formula2")
    _log(db, u, 800, "checkin", days_ago=5)
    _log(db, u, 200, "transfer_in", days_ago=4)
    _log(db, u, -700, "subscription_buy", days_ago=3)
    # 余额 300；自挣 = min(300, 800 − 700) = 100（存量折算同公式，无特殊分支）
    assert points_mod.self_earned_balance(db, u) == 100
    u2 = _make_user(db, "formula3")
    _log(db, u2, 100, "checkin", days_ago=2)
    _log(db, u2, -150, "admin_deduct", days_ago=1)
    _log(db, u2, 200, "transfer_in", days_ago=0)
    # 自挣净额为负 → 钳到 0，不为负
    assert points_mod.self_earned_balance(db, u2) == 0


# ==================== 3. 来源不限定（兑换口） ====================

def test_transfer_only_balance_can_redeem(db):
    """2026-11-02 起能量不分来源：只有赠送/转入来源的余额也能兑换 30 天。

    本例旧行为（来源限定开）应被拦，正是本单要删的门槛。
    """
    u = _active_user(db, "gate_transfer")
    _log(db, u, 400, "transfer_in", days_ago=0)
    out = points_mod.redeem_welfare(db, u, 30)
    assert out["cost"] == 300 and out["balance"] == 100
    db.refresh(u)
    assert int(u.points) == 100
    assert u.is_welfare and u.welfare_expires_at is not None


def test_redeem_ignores_legacy_self_earned_row(db):
    """生产库残留的旧键行（值 true）必须被忽略：读侧已删除，不许复活门槛。"""
    _set_config(db, "welfare_redeem_self_earned_only", "true")
    u = _active_user(db, "gate_legacy_row")
    _log(db, u, 400, "transfer_in", days_ago=0)
    out = points_mod.redeem_welfare(db, u, 30)
    assert out["balance"] == 100
    db.refresh(u)
    assert u.is_welfare


def test_self_earned_can_redeem(db):
    u = _active_user(db, "gate_earned")
    _log(db, u, 150, "chat", days_ago=2)
    _log(db, u, 150, "checkin", days_ago=1)
    out = points_mod.redeem_welfare(db, u, 30)
    assert out["cost"] == 300 and out["balance"] == 0
    db.refresh(u)
    assert u.is_welfare and u.welfare_expires_at is not None


def test_mixed_balance_can_redeem_30d(db):
    """混合来源按总余额判：自挣 150＋转入 200＝350 ≥ 300，30 天可兑。"""
    u = _active_user(db, "gate_mixed")
    _log(db, u, 150, "chat", days_ago=2)
    _log(db, u, 200, "transfer_in", days_ago=1)
    out = points_mod.redeem_welfare(db, u, 30)
    assert out["cost"] == 300 and out["balance"] == 50
    db.refresh(u)
    assert u.is_welfare and u.welfare_expires_at is not None


def test_insufficient_total_balance_message_kept(db):
    u = _active_user(db, "gate_poor")
    _log(db, u, 50, "chat", days_ago=0)
    with pytest.raises(ValueError) as ei:
        points_mod.redeem_welfare(db, u, 7)
    assert "能量不足" in str(ei.value)


# ==================== 4. 双钥匙 ====================

def test_account_age_gate(db):
    u = _make_user(db, "gate_young", age_days=1)
    _checkin_on(db, u, 0)
    _log(db, u, 400, "chat", days_ago=0)
    with pytest.raises(ValueError) as ei:
        points_mod.redeem_welfare(db, u, 7)
    msg = str(ei.value)
    assert "注册" in msg and "天" in msg
    db.refresh(u)
    assert int(u.points) == 400 and not u.is_welfare
    # 号龄达标后同一用户可兑
    u.created_at = datetime.now() - timedelta(days=5)
    db.commit()
    _checkin_on(db, u, 1)
    out = points_mod.redeem_welfare(db, u, 7)
    assert out["balance"] == 300


def test_activity_gate_and_chat_cap(db):
    u = _make_user(db, "gate_idle", age_days=10)
    # 只有发言天数：3 个不同天也最多计 1 天 → 不达标
    _log(db, u, 200, "chat", days_ago=0)
    _log(db, u, 100, "chat", days_ago=1)
    _log(db, u, 100, "chat", days_ago=2)
    with pytest.raises(ValueError) as ei:
        points_mod.redeem_welfare(db, u, 7)
    assert "活跃" in str(ei.value)
    # 补一天播放（硬信号）→ 硬 1 天 + 发言 1 天 = 2 天达标
    _playback_on(db, u, 1)
    out = points_mod.redeem_welfare(db, u, 7)
    assert out["balance"] == 300


def test_gates_can_be_configured_off(db):
    _set_config(db, "welfare_redeem_min_account_age_days", "0")
    _set_config(db, "welfare_redeem_min_active_days", "0")
    u = _make_user(db, "gate_off", age_days=0)
    _log(db, u, 300, "transfer_in", days_ago=0)
    out = points_mod.redeem_welfare(db, u, 30)
    assert out["balance"] == 0
    db.refresh(u)
    assert u.is_welfare


# ==================== 5. 豁免通道不受影响 ====================

def test_admin_grant_unaffected(db):
    from backend.emby_server import portal
    u = _make_user(db, "gate_admin", age_days=0, points=0)
    portal.grant_welfare(db, u, channel="admin", days=30, granted_by=1)
    db.refresh(u)
    assert u.is_welfare and u.welfare_expires_at is not None


def test_view_unlock_unaffected(db):
    from backend.emby_server import portal
    realm = models.ServerRealm(name="公益测试服", slug="gatefree",
                               access_mode="free")
    db.add(realm)
    db.commit()
    db.refresh(realm)
    u = _make_user(db, "gate_view", age_days=0)
    _log(db, u, 100, "transfer_in", days_ago=0)
    out = portal.unlock_emby_view(
        portal.UnlockViewRequest(realm_id=realm.id), u, db)
    assert out["success"] is True
    db.refresh(u)
    assert int(u.points) == 50, "查看行按总余额扣 50，不受来源限定影响"


# ==================== 6. 配置注册 ====================

def test_gate_config_keys_registered(db):
    from backend import config_schema
    from backend.api import welfare_admin
    keys = welfare_admin.WELFARE_CONFIG_KEYS
    # 来源限定键已于 2026-11-02 删除：默认表与 schema 均不得再出现
    assert "welfare_redeem_self_earned_only" not in keys
    assert keys["welfare_redeem_min_account_age_days"] == "3"
    assert keys["welfare_redeem_min_active_days"] == "2"
    schema = config_schema.WELFARE_CONFIG_SCHEMA
    assert "welfare_redeem_self_earned_only" not in schema
    assert schema["welfare_redeem_min_account_age_days"] == ("int", 0, 365)
    assert schema["welfare_redeem_min_active_days"] == ("int", 0, 7)
    with pytest.raises(config_schema.ConfigValueError):
        config_schema.normalize(
            "welfare_redeem_min_account_age_days",
            schema["welfare_redeem_min_account_age_days"], -1)
    with pytest.raises(config_schema.ConfigValueError):
        config_schema.normalize(
            "welfare_redeem_min_active_days",
            schema["welfare_redeem_min_active_days"], 8)
    # 保存读回
    from backend.config_service import save_config
    save_config(db, {"welfare_redeem_min_account_age_days": "5"}, schema="welfare")
    db.commit()
    got = welfare_admin.welfare_config_get(db=db, current_admin=_make_user(db, "cfgadmin"))
    assert got["welfare_redeem_min_account_age_days"] == "5"

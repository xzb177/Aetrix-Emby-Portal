"""经济系统冒烟测试：签到 → 兑换码 → 邀请返利 → 支付订单

直接调用函数级逻辑（不启动 HTTP 服务），验证核心链路。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 不写死库文件名：交给 backend.database 解析（新装 aetrix_unified.db，老部署沿用原库）
os.environ.setdefault("DATABASE_TYPE", "sqlite")

from datetime import datetime, timedelta
from decimal import Decimal

from backend.database import SessionLocal, init_db
from backend import models
from backend.security import hash_password
from backend.api.economy import (
    _add_points, _checkin_rules, _today_start, _yipay_sign, _verify_yipay_notify,
)
from backend.api.invitation import apply_invitation, apply_rebate, _generate_invite_code

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅" if cond else "  ❌") + f" {name}")


def main():
    init_db()
    db = SessionLocal()
    now = datetime.now()

    # ---- 准备测试用户（唯一后缀避免与现有数据冲突） ----
    suffix = now.strftime("%H%M%S%f")
    u1 = models.WebUser(username=f"smoke_inv_{suffix}", password_hash=hash_password("secret123"))
    u2 = models.WebUser(username=f"smoke_invitee_{suffix}", password_hash=hash_password("secret123"))
    db.add_all([u1, u2])
    db.commit()
    db.refresh(u1)
    db.refresh(u2)

    # ---- 1. 邀请码生成与双向奖励 ----
    print("\n[1] 邀请系统")
    code_str = _generate_invite_code(db)
    code = models.InvitationCode(code=code_str, user_id=u1.id)
    db.add(code)
    db.commit()
    result = apply_invitation(db, u2, code_str.lower())  # 测试大小写归一
    db.commit()
    check("邀请码应用成功", result.get("applied") is True)
    check("邀请者获得奖励积分", (u1.points or 0) > 0)
    check("被邀请者获得奖励积分", (u2.points or 0) > 0)
    record = db.query(models.InvitationRecord).filter(
        models.InvitationRecord.invitee_id == u2.id).first()
    check("邀请记录已建立", record is not None and record.inviter_id == u1.id)

    # ---- 2. 每日签到 ----
    print("\n[2] 每日签到")
    rules = _checkin_rules(db)
    check("签到规则读取", rules["enabled"] and rules["base_points"] > 0)
    today = _today_start()
    yesterday_record = None
    streak = 1
    bonus = min(rules["streak_bonus"] * (streak - 1), rules["streak_max_bonus"])
    reward = rules["base_points"] + bonus
    db.add(models.CheckinRecord(user_id=u2.id, checkin_date=today,
                                points_awarded=reward, streak=streak))
    bal = _add_points(db, u2, reward, "checkin", f"每日签到（连续 {streak} 天）")
    db.commit()
    check("签到积分入账", bal >= reward)
    dup = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == u2.id,
        models.CheckinRecord.checkin_date >= today).first()
    check("当日签到记录存在（防重复由 API 层保证）", dup is not None)

    # ---- 3. 兑换码（积分型 + 订阅型） ----
    print("\n[3] 兑换码")
    plan = models.SubscriptionPlan(name=f"smoke_plan_{suffix}", price=Decimal("19.90"),
                                   duration_days=30)
    db.add(plan)
    db.commit()
    db.refresh(plan)

    ex1 = models.ExchangeCode(code="SMOKEPT" + suffix[-4:], type="points", points_value=88)
    ex2 = models.ExchangeCode(code="SMOKESUB" + suffix[-4:], type="subscription",
                              plan_id=plan.id, duration_days=15)
    db.add_all([ex1, ex2])
    db.commit()

    # 模拟 API 层核销逻辑（与 economy.redeem_exchange_code 相同流程）
    c = db.query(models.ExchangeCode).filter(
        models.ExchangeCode.code == ex1.code).first()
    balance = _add_points(db, u2, c.points_value, "exchange", f"兑换码 {c.code}")
    c.use_count += 1
    db.commit()
    check("积分型兑换入账", balance >= 88)

    from backend.api.economy import _grant_subscription
    sub = _grant_subscription(db, u2, plan, ex2.duration_days)
    ex2.use_count += 1
    db.commit()
    db.refresh(sub)
    check("订阅型兑换发放", sub.end_date > now and sub.status == "active")
    # 顺延逻辑：同一订阅对象被延长 10 天
    sub_id_before = sub.id
    end_before = sub.end_date
    sub2 = _grant_subscription(db, u2, plan, 10)
    db.commit()
    db.refresh(sub)
    check("已有订阅则顺延",
          sub2.id == sub_id_before
          and (sub.end_date - end_before).days == 10)

    # ---- 4. 充值返利 ----
    print("\n[4] 邀请返利")
    before = u1.points or 0
    rebate = apply_rebate(db, u2, 100.0, f"smoke_order_{suffix}")
    db.commit()
    db.refresh(u1)
    check("返利已发放（100 元 × 10% = 10 积分）", rebate == 10 and (u1.points or 0) == before + 10)

    # ---- 5. 支付签名（易支付 MD5） ----
    print("\n[5] 支付网关签名")
    key = "testkey123"
    params = {
        "pid": "1001", "type": "alipay", "out_trade_no": "RB1234567890",
        "notify_url": "https://site.example/notify", "return_url": "https://site.example/return",
        "name": "积分充值", "money": "19.90",
    }
    sign = _yipay_sign(params, key)
    check("签名生成（32 位 MD5）", len(sign) == 32)
    check("回调验签通过", _verify_yipay_notify({**params, "sign": sign, "sign_type": "MD5"}, key))
    check("篡改金额验签失败", not _verify_yipay_notify(
        {**params, "money": "0.01", "sign": sign, "sign_type": "MD5"}, key))

    # ---- 6. 订单履约幂等 ----
    print("\n[6] 订单履约")
    from backend.api.economy import _fulfill_order, send_fulfill_notifications
    import asyncio

    pkg = models.RechargePackage(name=f"smoke_pkg_{suffix}", amount=500, price=Decimal("25.00"))
    db.add(pkg)
    db.commit()
    db.refresh(pkg)
    order = models.RechargeOrder(order_id=f"SMOKE{suffix}", user_id=u2.id,
                                 package_id=pkg.id, amount=500, price=Decimal("25.00"),
                                 payment_method="alipay", status="pending")
    db.add(order)
    db.commit()

    before_points = u2.points or 0
    loop = asyncio.get_event_loop()
    # _fulfill_order 是同步函数（async 路由用 run_in_threadpool 调它），这里直接调
    pending = _fulfill_order(db, recharge_order=order)
    db.commit()
    db.refresh(u2)
    check("充值履约发积分", order.status == "paid" and (u2.points or 0) == before_points + 500)
    check("履约返回待发通知（而不是在事务里就写站内信）",
          isinstance(pending, list) and len(pending) == 1
          and pending[0].get("event_type") == "economy.recharge_success")

    # 提交之后再发：此时另开会话写站内信不会撞 SQLite 写锁
    loop.run_until_complete(send_fulfill_notifications(pending))
    msg = db.query(models.StationMessage).filter(
        models.StationMessage.to_user_id == u2.id,
        models.StationMessage.title.like("%充值成功%"),
    ).first()
    check("充值成功站内信真的落库（此前会被写锁静默丢掉）", msg is not None)

    before_points = u2.points or 0
    pending2 = _fulfill_order(db, recharge_order=order)
    db.commit()
    db.refresh(u2)
    check("重复回调幂等（不重复发货）", (u2.points or 0) == before_points)
    check("重复回调不再重复通知", pending2 == [])

    # ---- 清理测试数据 ----
    uids = [u1.id, u2.id]
    db.query(models.StationMessage).filter(
        models.StationMessage.to_user_id.in_(uids)).delete(synchronize_session=False)
    db.query(models.PointsLog).filter(
        models.PointsLog.user_id.in_(uids)).delete(synchronize_session=False)
    db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id.in_(uids)).delete(synchronize_session=False)
    db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id.in_(uids)).delete(synchronize_session=False)
    for obj in [order, pkg, ex2, ex1, plan, record, code]:
        try:
            db.delete(obj)
        except Exception:
            pass
    db.query(models.WebUser).filter(models.WebUser.id.in_(uids)).delete(synchronize_session=False)
    db.commit()
    db.close()

    print(f"\n{'=' * 40}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("🎉 经济系统冒烟测试全部通过")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""优惠券冒烟测试（v2.10.0）

优惠券要解决的问题：站点只有「兑换码」（不花钱直接拿东西），没有「付费时打折」。
这一版把优惠券做成付费链路的一部分，最关键的几条口径：

1. **额度是预订制的**：下单就占额度（`reserved`），付款成功转 `consumed`，
   关单/退款转 `released` 并把额度还回去。只在付款成功时计数会让一串未支付订单
   同时绕过限额；而关单/退款不还额度又会让码被白白烧掉；
2. **超发防得住**：总额度用条件 UPDATE 抢占（`use_count < max_uses` 才 +1），
   并发下单也超不了上限；每人限用同样按「占用中 + 已消费」计算；
3. **预览与实付同口径**：下单前试算与真正下单共用 `coupons.quote`，
   不会出现「预览 8 折、实付全价」；
4. **金额不落浮点**：一律 Decimal、保留两位；订单快照 `list_price` /
   `discount_amount`，套餐改价后历史订单仍能解释；
5. **订单金额 = 实付**：对账、邀请返利比例、退款都以用户真付的钱为准；
6. **超时未支付自动收尾**：预订不能被一张永远不付款的订单白占着——
   到期后自动关单并还额度；已支付却还挂着的脏数据补记为已用；
7. **运营面**：建券（可批量随机码）、改券（总次数不能改到已占用之下）、
   停用、删券（有核销记录只能停用）、核销记录、总开关与预订时限、审计；
8. **鉴权**：普通用户碰不到管理端券接口；未登录碰不到试算与下单。

用法：python scripts/smoke_test_coupons.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "e2e-test-secret-key-not-for-production"

WORK = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(WORK, 'coupons.db')}"

from datetime import datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from backend import coupons, models, realms  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.security import hash_password  # noqa: E402

init_db()

from backend.main import app  # noqa: E402
from backend.integrations.store import invalidate  # noqa: E402

client = TestClient(app)

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ==================== 播种 ====================

with SessionLocal() as db:
    realm = realms.active_realm(db)
    # 第二个服：验证「定向到某个服的券不能跨服用」
    other = models.ServerRealm(name="冒烟二服", slug="smoke-two")
    db.add(other)
    db.commit()

    staff = models.WebUser(username="coupon_staff", password_hash=hash_password("staffpass123"),
                           is_staff=True, admin_role="super", is_active=True)
    buyer = models.WebUser(username="coupon_buyer", password_hash=hash_password("buyerpass123"),
                           is_active=True, points=0)
    buyer2 = models.WebUser(username="coupon_buyer2", password_hash=hash_password("buyer2pass123"),
                            is_active=True, points=0)
    db.add_all([staff, buyer, buyer2])
    db.commit()

    # 支付网关配置：下单接口在未配置时直接 503，这里给全，走真实下单路径
    for key, value in (
        ("payment_gateway_url", "https://pay.smoke.test"),
        ("payment_partner_id", "10001"),
        ("payment_partner_key", "smoke-partner-key"),
        ("site_url", "http://localhost:8000"),
    ):
        db.add(models.SystemConfig(key=key, value=value))

    pkg = models.RechargePackage(name="100 积分包", amount=100, price=100, bonus=0, is_active=True)
    plan = models.SubscriptionPlan(name="月卡", price=30, duration_days=30,
                                   realm_id=realm.id, is_active=True)
    db.add_all([pkg, plan])
    db.commit()
    for _k in ("payment_gateway_url", "payment_partner_id", "payment_partner_key", "site_url"):
        invalidate(_k)  # 热缓存失效：测试直接写库，手动清缓存

    realm_id, other_realm_id = realm.id, other.id
    pkg_id, plan_id = pkg.id, plan.id
    buyer_id, buyer2_id = buyer.id, buyer2.id


def coupon_row(code: str):
    with SessionLocal() as db:
        return db.query(models.CouponCode).filter(models.CouponCode.code == code).first()


def usage_of(order_id: str):
    with SessionLocal() as db:
        return db.query(models.CouponUsage).filter(
            models.CouponUsage.order_id == order_id).first()


def order_row(order_id: str):
    with SessionLocal() as db:
        row = db.query(models.RechargeOrder).filter(
            models.RechargeOrder.order_id == order_id).first()
        if row:
            return row
        return db.query(models.SubscriptionOrder).filter(
            models.SubscriptionOrder.order_id == order_id).first()


def points_of(user_id: int) -> int:
    with SessionLocal() as db:
        return int(db.query(models.WebUser.points).filter(
            models.WebUser.id == user_id).scalar() or 0)


r = client.post("/api/user/auth/login", json={"username": "coupon_staff", "password": "staffpass123"})
check("管理员登录成功", r.status_code == 200, f"status={r.status_code}")
ADMIN_H = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.post("/api/user/auth/login", json={"username": "coupon_buyer", "password": "buyerpass123"})
check("买家登录成功", r.status_code == 200, f"status={r.status_code}")
USER_H = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.post("/api/user/auth/login", json={"username": "coupon_buyer2", "password": "buyer2pass123"})
check("第二个买家登录成功", r.status_code == 200, f"status={r.status_code}")
USER2_H = {"Authorization": f"Bearer {r.json()['access_token']}"}

# ==================== 1. 建券（管理端） ====================

print("\n--- 建券：打折券 / 固定减免 / 定向服 / 批量随机码 ---")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "SAVE10", "kind": "all", "discount_type": "percent", "value": 90,
    "max_uses": 10, "per_user_limit": 1, "note": "九折券",
})
check("创建九折券", r.status_code == 200 and r.json()["count"] == 1,
      f"status={r.status_code} {r.text[:160]}")
SAVE10 = coupon_row("SAVE10")
check("券落库：折扣与限用正确",
      SAVE10 is not None and SAVE10.discount_type == "percent" and int(SAVE10.value) == 90
      and int(SAVE10.max_uses) == 10 and int(SAVE10.per_user_limit) == 1,
      f"{SAVE10.discount_type if SAVE10 else None}/{SAVE10.value if SAVE10 else None}")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "CUT5", "kind": "recharge", "discount_type": "fixed", "value": 5,
    "max_uses": 0, "per_user_limit": 0, "note": "立减 5 元",
})
check("创建固定减免券（不限次数）", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "CAP30", "kind": "recharge", "discount_type": "percent", "value": 50,
    "max_discount": 30, "max_uses": 1, "per_user_limit": 1, "note": "五折但最多省 30",
})
check("创建带封顶的百分比券", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "ONLYTWO", "kind": "subscription", "discount_type": "percent", "value": 80,
    "realm_id": other_realm_id, "max_uses": 5, "per_user_limit": 2, "note": "二服专用",
})
check("创建定向某个服的券", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "count": 3, "kind": "all", "discount_type": "percent", "value": 95,
    "valid_days": 7, "per_user_limit": 1, "note": "批量",
})
batch = r.json()["coupons"] if r.status_code == 200 else []
check("批量生成 3 张随机码", r.status_code == 200 and len(batch) == 3,
      f"status={r.status_code} {r.text[:120]}")
check("随机码够长且不重复",
      len({c["code"] for c in batch}) == 3 and all(len(c["code"]) == 10 for c in batch),
      str([c["code"] for c in batch]))
check("批量券带 7 天有效期", all(c["valid_until"] for c in batch), str(batch[:1]))

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "SAVE10", "kind": "all", "discount_type": "percent", "value": 90,
})
check("重复的优惠码被拒", r.status_code == 400, f"status={r.status_code}")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "count": 1, "kind": "all", "discount_type": "percent", "value": 0,
})
check("折扣值越界被拒（percent 必须 1~100）", r.status_code == 400, f"status={r.status_code}")

r = client.get("/api/admin/economy/coupons", headers=ADMIN_H)
body = r.json() if r.status_code == 200 else {}
check("券列表可读且带额度分布",
      r.status_code == 200 and body.get("total", 0) >= 5
      and all("stats" in c and "reserved" in c["stats"] for c in body["coupons"]),
      f"status={r.status_code} total={body.get('total')}")
check("列表带归属服名",
      any(c["code"] == "ONLYTWO" and c["realm_name"] == "冒烟二服" for c in body.get("coupons", [])),
      str([c["realm_name"] for c in body.get("coupons", []) if c["code"] == "ONLYTWO"]))

# ==================== 2. 试算（用户端） ====================

print("\n--- 试算：能不能用、省多少、实付多少 ---")

r = client.get("/api/user/economy/payment/coupon/config", headers=USER_H)
check("开关默认开启", r.status_code == 200 and r.json()["enabled"] is True, f"status={r.status_code}")

r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "save10", "kind": "recharge", "item_id": pkg_id})
q = r.json() if r.status_code == 200 else {}
check("九折券试算：原价 100 / 省 10 / 实付 90",
      r.status_code == 200 and q.get("list_price") == 100.0
      and q.get("discount_amount") == 10.0 and q.get("paid_amount") == 90.0,
      f"status={r.status_code} {r.text[:160]}")
check("小写输入也能识别（码统一大写）", q.get("code") == "SAVE10", str(q.get("code")))

r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "CUT5", "kind": "recharge", "item_id": pkg_id})
check("固定减免：省 5 元、实付 95",
      r.status_code == 200 and r.json()["discount_amount"] == 5.0
      and r.json()["paid_amount"] == 95.0, r.text[:160])

r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "CAP30", "kind": "recharge", "item_id": pkg_id})
check("封顶生效：五折本该省 50，封顶后只省 30",
      r.status_code == 200 and r.json()["discount_amount"] == 30.0
      and r.json()["paid_amount"] == 70.0, r.text[:160])

r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "NOPE", "kind": "recharge", "item_id": pkg_id})
check("不存在的码被拒", r.status_code == 400 and "不存在" in r.text, f"{r.status_code} {r.text[:80]}")

r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "ONLYTWO", "kind": "recharge", "item_id": pkg_id})
check("充值不能使用订阅专用券", r.status_code == 400 and "不适用" in r.text,
      f"{r.status_code} {r.text[:80]}")

r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "ONLYTWO", "kind": "subscription", "item_id": plan_id})
check("定向二服的券不能用于当前服的会员",
      r.status_code == 400 and "指定服" in r.text, f"{r.status_code} {r.text[:80]}")

# 门槛券：满 200 才能用，套餐价 100 应该被拒
r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "MIN200", "kind": "recharge", "discount_type": "fixed", "value": 20,
    "min_amount": 200, "per_user_limit": 1,
})
check("创建门槛券（满 200 减 20）", r.status_code == 200, f"status={r.status_code}")
r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "MIN200", "kind": "recharge", "item_id": pkg_id})
check("未达门槛被拒且说明金额", r.status_code == 400 and "满" in r.text,
      f"{r.status_code} {r.text[:80]}")

# 过期券
with SessionLocal() as db:
    db.add(models.CouponCode(code="EXPIRED1", kind="all", discount_type="percent",
                             value=50, max_uses=0, per_user_limit=1, is_active=True,
                             valid_until=datetime.now() - timedelta(days=1)))
    db.commit()
r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "EXPIRED1", "kind": "recharge", "item_id": pkg_id})
check("过期券被拒", r.status_code == 400 and "过期" in r.text, f"{r.status_code} {r.text[:80]}")

# 停用券
r = client.put(f"/api/admin/economy/coupons/{SAVE10.id}", headers=ADMIN_H, json={"is_active": False})
check("停用券成功", r.status_code == 200 and r.json()["coupon"]["is_active"] is False,
      f"status={r.status_code} {r.text[:120]}")
r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "SAVE10", "kind": "recharge", "item_id": pkg_id})
check("停用券被拒", r.status_code == 400 and "停用" in r.text, f"{r.status_code} {r.text[:80]}")
r = client.put(f"/api/admin/economy/coupons/{SAVE10.id}", headers=ADMIN_H, json={"is_active": True})
check("重新启用成功", r.status_code == 200 and r.json()["coupon"]["is_active"] is True)

# ==================== 3. 下单占额度 ====================

print("\n--- 下单：金额按实付、额度被预订 ---")

r = client.post("/api/user/economy/payment/order", headers=USER_H, json={
    "kind": "recharge", "item_id": pkg_id, "payment_method": "alipay", "coupon_code": "SAVE10",
})
body = r.json() if r.status_code == 200 else {}
check("用券下单成功", r.status_code == 200, f"status={r.status_code} {r.text[:160]}")
check("下单返回原价 / 优惠 / 实付",
      body.get("list_price") == 100.0 and body.get("discount_amount") == 10.0
      and body.get("amount") == 90.0, str(body)[:200])
ORDER_A = body.get("order_id", "")
check("支付链接金额是实付（易支付 money=90.00）",
      "money=90.00" in body.get("pay_url", ""), body.get("pay_url", "")[:120])

row = order_row(ORDER_A)
check("订单落库：price=实付 90、list_price=100、discount=10",
      row is not None and float(row.price) == 90.0 and float(row.list_price) == 100.0
      and float(row.discount_amount) == 10.0, str(row.price if row else None))
check("订单关联核销记录", row is not None and row.coupon_usage_id is not None)

usage = usage_of(ORDER_A)
check("核销记录为「预订」且带金额快照",
      usage is not None and usage.status == coupons.RESERVED
      and float(usage.paid_amount) == 90.0, f"{usage.status if usage else None}")
check("券的占用数 +1", int(coupon_row("SAVE10").use_count) == 1,
      str(coupon_row("SAVE10").use_count))

r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "SAVE10", "kind": "recharge", "item_id": pkg_id})
check("同一张券每人限用 1 次：预订中的额度也算",
      r.status_code == 400 and "限用" in r.text, f"{r.status_code} {r.text[:80]}")

r = client.get("/api/user/economy/payment/orders", headers=USER_H)
order_row_api = [o for o in r.json()["orders"] if o["order_id"] == ORDER_A]
check("用户端订单列表能看到优惠与码",
      bool(order_row_api) and order_row_api[0]["discount_amount"] == 10.0
      and order_row_api[0]["coupon_code"] == "SAVE10"
      and order_row_api[0]["list_price"] == 100.0,
      str(order_row_api[:1])[:240])

# 未付款就把额度还回去一次，验证「关单释放」的完整闭环放在第 5 节

# ==================== 4. 付款履约：预订 → 已消费 ====================

print("\n--- 付款：预订转已用，积分按套餐实发 ---")

r = client.post(f"/api/admin/economy/orders/{ORDER_A}/mark-paid", headers=ADMIN_H)
check("补单发货成功", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
check("到账积分按套餐发放（与优惠无关）", points_of(buyer_id) == 100, str(points_of(buyer_id)))
usage = usage_of(ORDER_A)
check("核销记录转为「已消费」", usage is not None and usage.status == coupons.CONSUMED,
      f"{usage.status if usage else None}")
check("已消费后占用数仍为 1（额度不还）", int(coupon_row("SAVE10").use_count) == 1,
      str(coupon_row("SAVE10").use_count))

# 真实回调路径也要能消费掉预订
r = client.post("/api/user/economy/payment/order", headers=USER2_H, json={
    "kind": "recharge", "item_id": pkg_id, "payment_method": "alipay", "coupon_code": "CUT5",
})
ORDER_B = r.json().get("order_id", "")
from backend.api.economy import _yipay_sign  # noqa: E402

params = {"out_trade_no": ORDER_B, "trade_status": "TRADE_SUCCESS",
          "money": "95.00", "pid": "10001"}
params["sign"] = _yipay_sign(params, "smoke-partner-key")
params["sign_type"] = "MD5"
r = client.get("/api/user/economy/payment/notify", params=params)
check("支付回调验签通过并确认", r.status_code == 200 and r.text == "success",
      f"{r.status_code} {r.text[:80]}")
usage = usage_of(ORDER_B)
check("回调履约后核销记录也是「已消费」",
      usage is not None and usage.status == coupons.CONSUMED, f"{usage.status if usage else None}")
check("回调履约：买家 2 到账 100 积分", points_of(buyer2_id) == 100, str(points_of(buyer2_id)))

# 重放回调不会重复消费 / 重复发货
r = client.get("/api/user/economy/payment/notify", params=params)
check("重放回调仍返回 success", r.status_code == 200 and r.text == "success")
check("重放回调不重复发积分", points_of(buyer2_id) == 100, str(points_of(buyer2_id)))

# ==================== 5. 关单 / 退款：额度还回去 ====================

print("\n--- 关单与退款：释放额度 ---")

# 充值专用券不能拿来买会员（反向也要成立）
r = client.post("/api/user/economy/payment/order", headers=USER_H, json={
    "kind": "subscription", "item_id": plan_id, "payment_method": "alipay", "coupon_code": "CUT5",
})
check("充值券不能用于买会员", r.status_code == 400 and "不适用" in r.text,
      f"{r.status_code} {r.text[:100]}")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "SUB5", "kind": "all", "discount_type": "fixed", "value": 5,
    "max_uses": 3, "per_user_limit": 1, "note": "会员立减 5 元",
})
check("创建可买会员的减免券", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

r = client.post("/api/user/economy/payment/order", headers=USER_H, json={
    "kind": "subscription", "item_id": plan_id, "payment_method": "alipay", "coupon_code": "SUB5",
})
ORDER_C = r.json().get("order_id", "")
check("用减免券买会员：月卡 30 → 实付 25",
      r.status_code == 200 and r.json().get("amount") == 25.0, r.text[:160])
check("订阅订单落库：amount=实付 25、list_price=30、discount=5",
      order_row(ORDER_C) is not None and float(order_row(ORDER_C).amount) == 25.0
      and float(order_row(ORDER_C).list_price) == 30.0
      and float(order_row(ORDER_C).discount_amount) == 5.0,
      str(order_row(ORDER_C).amount if order_row(ORDER_C) else None))
r = client.post(f"/api/admin/economy/orders/{ORDER_C}/close", headers=ADMIN_H)
check("关单成功", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
usage = usage_of(ORDER_C)
check("关单后核销记录转「已释放」",
      usage is not None and usage.status == coupons.RELEASED and usage.closed_at is not None,
      f"{usage.status if usage else None}")
check("关单后额度还回去（use_count 归零）", int(coupon_row("SUB5").use_count) == 0,
      str(coupon_row("SUB5").use_count))
r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "SUB5", "kind": "subscription", "item_id": plan_id})
check("释放后同一张券可以再用", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

# 退款释放
r = client.post("/api/user/economy/payment/order", headers=USER2_H, json={
    "kind": "recharge", "item_id": pkg_id, "payment_method": "alipay", "coupon_code": "CAP30",
})
ORDER_D = r.json().get("order_id", "")
check("用封顶券下单：实付 70", r.status_code == 200 and r.json().get("amount") == 70.0, r.text[:160])
client.post(f"/api/admin/economy/orders/{ORDER_D}/mark-paid", headers=ADMIN_H)
check("封顶券订单付款后额度为 1", int(coupon_row("CAP30").use_count) == 1,
      str(coupon_row("CAP30").use_count))
r = client.post(f"/api/admin/economy/orders/{ORDER_D}/refund", headers=ADMIN_H,
                json={"reason": "买了两次"})
check("退款成功且标记释放了券",
      r.status_code == 200 and r.json().get("coupon_released") is True,
      f"status={r.status_code} {r.text[:160]}")
check("退款后额度还回去", int(coupon_row("CAP30").use_count) == 0,
      str(coupon_row("CAP30").use_count))
usage = usage_of(ORDER_D)
check("退款后核销记录为「已释放」", usage is not None and usage.status == coupons.RELEASED,
      f"{usage.status if usage else None}")

# 幂等：再释放一次不会把额度减成负数
with SessionLocal() as db:
    row = db.query(models.CouponUsage).filter(
        models.CouponUsage.order_id == ORDER_D).first()
    coupons.release(db, row.id)
    db.commit()
check("重复释放不会多还额度（不会掉到负数）", int(coupon_row("CAP30").use_count) == 0,
      str(coupon_row("CAP30").use_count))

# 已消费的券再释放一次：同样不重复计数（消费中的额度应还回）
with SessionLocal() as db:
    row = db.query(models.CouponUsage).filter(
        models.CouponUsage.order_id == ORDER_A).first()
    coupons.release(db, row.id)
    db.commit()
check("已消费的券被释放时也正确归还",
      int(coupon_row("SAVE10").use_count) == 0, str(coupon_row("SAVE10").use_count))
# 还原：后续断言依赖这张券仍处于「已消费」状态
with SessionLocal() as db:
    db.query(models.CouponUsage).filter(models.CouponUsage.order_id == ORDER_A).update(
        {"status": coupons.CONSUMED, "closed_at": None})
    db.query(models.CouponCode).filter(models.CouponCode.code == "SAVE10").update(
        {"use_count": 1})
    db.commit()

# ==================== 6. 总次数与并发抢占 ====================

print("\n--- 总次数：抢占式额度 ---")

r = client.post("/api/admin/economy/coupons", headers=ADMIN_H, json={
    "code": "ONLYONE", "kind": "recharge", "discount_type": "percent", "value": 50,
    "max_uses": 1, "per_user_limit": 0, "note": "只有一张",
})
check("创建总数 1 的券", r.status_code == 200, f"status={r.status_code}")

r = client.post("/api/user/economy/payment/order", headers=USER2_H, json={
    "kind": "recharge", "item_id": pkg_id, "payment_method": "alipay", "coupon_code": "ONLYONE",
})
check("第一个人抢到（下了单）", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "ONLYONE", "kind": "recharge", "item_id": pkg_id})
check("额度被预订后第二个人不能再试算",
      r.status_code == 400 and "用完" in r.text, f"{r.status_code} {r.text[:80]}")
r = client.post("/api/user/economy/payment/order", headers=USER_H, json={
    "kind": "recharge", "item_id": pkg_id, "payment_method": "alipay", "coupon_code": "ONLYONE",
})
check("第二个人也不能下单", r.status_code == 400, f"{r.status_code} {r.text[:100]}")

with SessionLocal() as db:
    coupon = db.query(models.CouponCode).filter(models.CouponCode.code == "ONLYONE").first()
    with SessionLocal() as db2:
        user = db2.query(models.WebUser).filter(models.WebUser.id == buyer_id).first()
        try:
            coupons.reserve(db, coupon=coupon, user=user, order_id="FORCED",
                            kind="recharge", list_price=Decimal("100"),
                            discount=Decimal("50"), paid=Decimal("50"))
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = "用完" in str(getattr(exc, "detail", exc))
    check("条件 UPDATE 抢占：并发下也超不了总限", raised)

# ==================== 7. 超时未支付的预订：自动收尾 ====================

print("\n--- 超时预订：自动关单并还额度 ---")

with SessionLocal() as db:
    db.add(models.CouponCode(code="STALE1", kind="recharge", discount_type="fixed",
                             value=3, max_uses=5, per_user_limit=1, is_active=True))
    db.commit()
    coupon = db.query(models.CouponCode).filter(models.CouponCode.code == "STALE1").first()
    for order_id, age_hours, status in (("STALE-PENDING", 30, "pending"),
                                        ("STALE-PAID", 30, "paid"),
                                        ("STALE-FRESH", 1, "pending")):
        db.add(models.RechargeOrder(order_id=order_id, user_id=buyer_id, package_id=pkg_id,
                                    amount=100, price=97, payment_method="alipay",
                                    status=status))
        db.add(models.CouponUsage(coupon_id=coupon.id, user_id=buyer_id, order_id=order_id,
                                  kind="recharge", status=coupons.RESERVED,
                                  list_price=Decimal("100"), discount_amount=Decimal("3"),
                                  paid_amount=Decimal("97"),
                                  created_at=datetime.now() - timedelta(hours=age_hours)))
        db.query(models.CouponCode).filter(models.CouponCode.id == coupon.id).update(
            {"use_count": models.CouponCode.use_count + 1})
    db.commit()

    summary = coupons.sweep_stale_reservations(db, hours=24)
    db.commit()
    check("一轮清理：扫描到 2 条到期预订（1 小时前那条不动）",
          summary["scanned"] == 2, str(summary))
    check("超时未支付的订单被自动关单并释放",
          summary["closed_orders"] == 1 and summary["released"] == 1, str(summary))
    check("已支付却还挂着的预订补记为已用", summary["consumed"] == 1, str(summary))

    pending = db.query(models.RechargeOrder).filter(
        models.RechargeOrder.order_id == "STALE-PENDING").first()
    check("超时订单状态 closed 且带关单时间",
          pending.status == "closed" and pending.closed_at is not None,
          f"{pending.status} / {pending.closed_at}")
    stale_usage = db.query(models.CouponUsage).filter(
        models.CouponUsage.order_id == "STALE-PENDING").first()
    check("超时预订被释放", stale_usage.status == coupons.RELEASED, stale_usage.status)
    paid_usage = db.query(models.CouponUsage).filter(
        models.CouponUsage.order_id == "STALE-PAID").first()
    check("已支付订单的预订转为已消费", paid_usage.status == coupons.CONSUMED, paid_usage.status)
    fresh_usage = db.query(models.CouponUsage).filter(
        models.CouponUsage.order_id == "STALE-FRESH").first()
    check("未到期的预订不被动（用户可能还在付款页）",
          fresh_usage.status == coupons.RESERVED, fresh_usage.status)
    check("额度计数与状态一致（2 条仍占用）",
          int(coupon_row("STALE1").use_count) == 2, str(coupon_row("STALE1").use_count))
    check("清理返回明确的开关状态", summary["enabled"] is True, str(summary["enabled"]))

    disabled = coupons.sweep_stale_reservations(db, hours=0)
    check("时限为 0 = 不自动清理", disabled["scanned"] == 0 and disabled["enabled"] is False,
          str(disabled))

# ==================== 8. 运营面：改券 / 删券 / 开关 / 记录 ====================

print("\n--- 运营面 ---")

r = client.get("/api/admin/economy/coupons/usages", headers=ADMIN_H)
records = r.json().get("records", []) if r.status_code == 200 else []
check("核销记录可读且带用户名与订单",
      r.status_code == 200 and records and all(x.get("username") and x.get("order_id")
                                               for x in records[:5]),
      f"status={r.status_code} count={len(records)}")
check("核销记录带优惠金额快照",
      any(x["discount_amount"] > 0 for x in records), str(records[:1])[:200])

r = client.get(f"/api/admin/economy/coupons/{SAVE10.id}/usages", headers=ADMIN_H)
check("按券查核销记录可用", r.status_code == 200, f"status={r.status_code}")

# 改券：总次数不能改到已占用之下
r = client.put(f"/api/admin/economy/coupons/{SAVE10.id}", headers=ADMIN_H, json={"max_uses": 0})
check("总次数可以调大（0 = 不限）", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
r = client.put(f"/api/admin/economy/coupons/{SAVE10.id}", headers=ADMIN_H, json={"value": 70})
check("改折扣成功", r.status_code == 200 and r.json()["coupon"]["value"] == 70,
      r.text[:120])
check("改折扣不影响已下单的快照",
      float(order_row(ORDER_A).discount_amount) == 10.0,
      str(order_row(ORDER_A).discount_amount))
r = client.put(f"/api/admin/economy/coupons/999999", headers=ADMIN_H, json={"value": 70})
check("改不存在的券返回 404", r.status_code == 404, f"status={r.status_code}")

stale_coupon = coupon_row("STALE1")
r = client.put(f"/api/admin/economy/coupons/{stale_coupon.id}", headers=ADMIN_H,
               json={"max_uses": 1})
check("总次数不能改到已占用之下（否则立即超发）",
      r.status_code == 400 and "占用" in r.text, f"{r.status_code} {r.text[:140]}")

# 有核销记录的券不能删，只能停用
r = client.delete(f"/api/admin/economy/coupons/{SAVE10.id}", headers=ADMIN_H)
check("有核销记录的券不能删", r.status_code == 400 and "停用" in r.text,
      f"{r.status_code} {r.text[:120]}")
unused = coupon_row("MIN200")
r = client.delete(f"/api/admin/economy/coupons/{unused.id}", headers=ADMIN_H)
check("从没用过的券可以删", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
check("删除后库里查不到", coupon_row("MIN200") is None)

# 总开关（2026-10-21 A 类开关摘面板：改走 env COUPON_ENABLED，面板只改预订时限）
os.environ["COUPON_ENABLED"] = "false"
r = client.put("/api/admin/economy/coupons/settings", headers=ADMIN_H,
               json={"reserve_hours": 48})
check("env 关闭总开关 + 面板设置预订时限",
      r.status_code == 200 and r.json()["enabled"] is False
      and r.json()["reserve_hours"] == 48, f"{r.status_code} {r.text[:120]}")
r = client.get("/api/user/economy/payment/coupon/config", headers=USER_H)
check("用户端看到开关已关闭", r.json()["enabled"] is False, r.text[:80])
r = client.post("/api/user/economy/payment/coupon/quote", headers=USER_H,
                json={"code": "CUT5", "kind": "recharge", "item_id": pkg_id})
check("关闭后试算被拒且说明原因", r.status_code == 400 and "未开启" in r.text,
      f"{r.status_code} {r.text[:80]}")
r = client.post("/api/user/economy/payment/order", headers=USER_H, json={
    "kind": "recharge", "item_id": pkg_id, "payment_method": "alipay", "coupon_code": "CUT5",
})
check("关闭后带券下单也被拒", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
os.environ.pop("COUPON_ENABLED", None)  # 恢复缺省（开）
r = client.put("/api/admin/economy/coupons/settings", headers=ADMIN_H,
               json={"reserve_hours": 24})
check("去掉 env 后恢复开启", r.status_code == 200 and r.json()["enabled"] is True)
r = client.get("/api/admin/economy/coupons/settings", headers=ADMIN_H)
check("设置里能看到当前占用中的预订数",
      r.status_code == 200 and "active_usage" in r.json(), r.text[:120])

with SessionLocal() as db:
    actions = [log.action for log in db.query(models.AdminLog).filter(
        models.AdminLog.action.in_(["economy_create_coupons", "economy_update_coupon",
                                    "economy_delete_coupon",
                                    "economy_coupon_settings"])).all()]
check("建券 / 改券 / 删券 / 开关都写了审计",
      {"economy_create_coupons", "economy_update_coupon",
       "economy_delete_coupon", "economy_coupon_settings"} <= set(actions),
      str(sorted(set(actions))))

# ==================== 9. 统一核销入口（v2.10.1） ====================
# 钱包页此前是「卡码 · 兑换码」+「优惠码」两个入口上下并列，同一页两个
# 「输入码 → 应用」的面板。现在只有一个入口，靠预检区分来源——这里守的就是
# 「四种码都能被认出来」与「旧入口的过期指引不会再回来」。

print("\n--- 统一核销入口：一个输入框认四种码 ---")

with SessionLocal() as db:
    db.add(models.CouponCode(code="STOPPED10", kind="all", discount_type="percent",
                             value=90, is_active=False))
    db.add(models.ExchangeCode(code="EXCH100", type="points", points_value=100,
                               max_uses=1, is_active=True))
    db.add(models.RegistrationCode(code="REG-SMOKE1", code_type=1, days=30,
                                   max_uses=1, is_active=True))
    db.commit()


def preview_code(code: str) -> dict:
    r = client.post("/api/user/membership/redeem/preview", headers=USER_H, json={"code": code})
    assert r.status_code == 200, f"preview {code}: {r.status_code} {r.text[:120]}"
    return r.json()


uses_before = int(coupon_row("SAVE10").use_count)
pv = preview_code("SAVE10")
check("优惠券在统一入口被识别（kind=coupon）",
      pv["kind"] == "coupon" and pv["valid"] is True, str(pv))
check("预检只识别、不核销：券的占用数一点没动",
      int(coupon_row("SAVE10").use_count) == uses_before,
      f"{uses_before} → {coupon_row('SAVE10').use_count}")
check("小写输入的券也能识别",
      preview_code("save10")["kind"] == "coupon")

pv = preview_code("STOPPED10")
check("停用的券在预检里就说清原因",
      pv["kind"] == "coupon" and pv["valid"] is False and "停用" in pv["message"], str(pv))

pv = preview_code("EXCH100")
check("兑换码走同一个入口（kind=exchange）", pv["kind"] == "exchange", str(pv))
check("兑换码的指引不再指向已经收掉的「钱包 → 兑换码」",
      "钱包" not in pv["message"] and "使用" in pv["message"], pv["message"])

pv = preview_code("REG-SMOKE1")
check("卡码仍走同一个入口（kind=code + 类型与天数）",
      pv["kind"] == "code" and pv["valid"] is True
      and pv["type_name"] == "注册码" and bool(pv["days_text"]), str(pv))

check("不存在的码不会被误判成券",
      preview_code("NOPE-NOT-A-CODE")["kind"] == "unknown")

# ==================== 10. 鉴权 ====================

print("\n--- 鉴权 ---")

check("未登录不能列券",
      client.get("/api/admin/economy/coupons").status_code in (401, 403))
check("普通用户不能列券",
      client.get("/api/admin/economy/coupons", headers=USER_H).status_code in (401, 403))
check("普通用户不能建券",
      client.post("/api/admin/economy/coupons", headers=USER_H,
                  json={"count": 1, "kind": "all", "discount_type": "fixed", "value": 1}
                  ).status_code in (401, 403))
check("普通用户不能改开关",
      client.put("/api/admin/economy/coupons/settings", headers=USER_H,
                 json={"enabled": False}).status_code in (401, 403))
check("普通用户不能看核销记录",
      client.get("/api/admin/economy/coupons/usages", headers=USER_H).status_code in (401, 403))
check("未登录不能试算",
      client.post("/api/user/economy/payment/coupon/quote",
                  json={"code": "CUT5", "kind": "recharge", "item_id": pkg_id}
                  ).status_code in (401, 403))

# ==================== 收尾 ====================

print("\n" + "=" * 60)
if failures:
    print(f"❌ {len(failures)}/{checks} 项失败：")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ 优惠券冒烟测试全部通过（{checks} 项）")

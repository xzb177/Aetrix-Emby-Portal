"""经济链并发冒烟测试 + 限流 IP 取值

这些场景都是「读-判断-写」型逻辑，单线程跑永远是对的，只有真并发才暴露：

1. 单次兑换码被多个请求同时核销 → 只能成功一次（不能发两份奖励）
2. 同一用户同时签到 → 只能成功一次（不能发两份积分，也不能 500）
3. `_add_points` 并发累加 → 不能丢更新（最终余额 = 初始 + 各次之和）
4. 同一被邀请人被重复应用邀请码 → 只建立一次关系、只发一次双向奖励
5. `client_ip` 不再采信攻击者可控的 `X-Forwarded-For` 首值（限流桶不可伪造）

用临时 SQLite 文件库 + 真线程 + 真会话，不碰开发库。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "e2e-test-secret-key-not-for-production"
# 2026-10-21 A 类开关摘面板：签到缺省关（服主保持关），本冒烟验证签到链路，按 env 显式打开
os.environ["CHECKIN_ENABLED"] = "true"

DB = os.path.join(tempfile.mkdtemp(), "concurrency.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from fastapi import HTTPException  # noqa: E402

from backend import models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402

init_db()

from backend.api.economy import _add_points, do_checkin, redeem_exchange_code  # noqa: E402
from backend.api.economy import _today_start  # noqa: E402
from backend.api.invitation import apply_invitation  # noqa: E402
from backend.ratelimit import client_ip  # noqa: E402

failures: list[str] = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ==================== 请求替身（端点里只用 IP 与路径）====================

class _URL:
    path = "/api/user/economy/checkin"


class _Client:
    host = "10.0.0.9"


class FakeRequest:
    """最小 Request 替身：端点只读 headers / client / url"""

    def __init__(self, headers: dict | None = None, client_host: str | None = None):
        self.headers = headers or {}
        self.client = _Client()
        if client_host:
            # 允许覆盖直连 IP（用于测试可信代理场景）
            self.client = type("_ClientOverride", (), {"host": client_host})()
        self.url = _URL()
        self.query_params: dict = {}


def run_async_endpoint(fn, *args):
    """在独立线程里跑 async 端点，返回 (成功?, 结果或异常)"""
    try:
        return True, asyncio.run(fn(*args))
    except HTTPException as exc:
        return False, exc
    except Exception as exc:  # noqa: BLE001
        return False, exc


def run_parallel(target, count: int):
    """启动 count 个线程同时执行 target(i)，收集结果"""
    results: list = [None] * count
    barrier = threading.Barrier(count)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        results[i] = target(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


# ==================== 播种 ====================

with SessionLocal() as db:
    users = []
    for name in ("cc-alice", "cc-bob", "cc-inviter", "cc-invitee"):
        u = models.WebUser(username=name, password_hash="not-used", is_active=True, points=0)
        db.add(u)
        users.append(u)
    db.commit()
    for u in users:
        db.refresh(u)
    alice, bob, inviter, invitee = users
    alice_id, bob_id, inviter_id, invitee_id = alice.id, bob.id, inviter.id, invitee.id
    # 2026-10-09 签到合并后积分仅限公益服用户：bob 用于签到测试，设为公益服
    db.query(models.WebUser).filter(models.WebUser.id == bob_id).update({"is_welfare": True})
    db.commit()

    db.add(models.ExchangeCode(
        code="CC-SINGLE-USE", type="points", points_value=100,
        max_uses=1, use_count=0, is_active=True,
    ))
    inv = models.InvitationCode(code="CCINVITE", user_id=inviter_id, is_active=True, use_count=0)
    db.add(inv)
    db.commit()


def balance(uid: int) -> int:
    with SessionLocal() as db:
        return int(db.query(models.WebUser.points).filter(models.WebUser.id == uid).scalar() or 0)


# ==================== 1. 单次兑换码并发核销 ====================

def redeem_once(i: int):
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == alice_id).first()
        from backend.api.economy import RedeemRequest

        return run_async_endpoint(
            redeem_exchange_code, FakeRequest(), RedeemRequest(code="CC-SINGLE-USE"), user, db
        )
    finally:
        db.close()


results = run_parallel(redeem_once, 4)
oks = [r for r in results if r and r[0]]
fails = [r for r in results if r and not r[0]]

with SessionLocal() as db:
    code_row = db.query(models.ExchangeCode).filter(models.ExchangeCode.code == "CC-SINGLE-USE").first()
    use_count = code_row.use_count
    is_active = code_row.is_active

check("单次码并发核销：只有 1 个请求成功", len(oks) == 1, f"成功 {len(oks)} / 失败 {len(fails)}")
bad = [
    getattr(exc, "status_code", 500)
    for _, exc in fails
    if not (isinstance(exc, HTTPException) and exc.status_code in (400, 409))
]
check("单次码并发核销：失败方返回 400/409（可重试语义），不是 500", not bad, f"异常状态码={bad}")
check("单次码并发核销：只发了一份积分", balance(alice_id) == 100, f"余额={balance(alice_id)}")
check("单次码并发核销：use_count 恰好为 1", use_count == 1, f"use_count={use_count}")
check("单次码并发核销：用满后自动停用", is_active is False, f"is_active={is_active}")

# ==================== 2. 同一用户并发签到 ====================

def checkin_once(i: int):
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == bob_id).first()
        return run_async_endpoint(do_checkin, FakeRequest(), user, db)
    finally:
        db.close()


results = run_parallel(checkin_once, 4)
oks = [r for r in results if r and r[0]]
fails = [r for r in results if r and not r[0]]

with SessionLocal() as db:
    records = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == bob_id,
        models.CheckinRecord.checkin_date >= _today_start(),
    ).count()

check("并发签到：只有 1 个请求成功", len(oks) == 1, f"成功 {len(oks)} / 失败 {len(fails)}")
bad = [
    getattr(exc, "status_code", 500)
    for _, exc in fails
    if not (isinstance(exc, HTTPException) and exc.status_code in (400, 409))
]
check("并发签到：失败方返回 400/409，不是 500", not bad, f"异常状态码={bad}")
check("并发签到：当日只有 1 条记录", records == 1, f"记录数={records}")
check("并发签到：只发了一份奖励", balance(bob_id) == 5, f"余额={balance(bob_id)}")

# ==================== 2b. 非公益服用户签到不加分 ====================

def checkin_nonwelfare():
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == alice_id).first()
        return run_async_endpoint(do_checkin, FakeRequest(), user, db)
    finally:
        db.close()

ok_nw, res_nw = checkin_nonwelfare()
check("非公益服签到：请求成功", ok_nw, f"结果={res_nw}")
if ok_nw:
    check("非公益服签到：积分为 0", res_nw.get("points_awarded") == 0, f"奖励={res_nw.get('points_awarded')}")
    # alice 在第 1 节通过兑换码获得 100 分，签到不应增加
    check("非公益服签到：余额不变", balance(alice_id) == 100, f"余额={balance(alice_id)}")

# ==================== 3. _add_points 并发累加不丢更新 ====================

with SessionLocal() as db:
    db.query(models.WebUser).filter(models.WebUser.id == alice_id).update(
        {models.WebUser.points: 1000}, synchronize_session=False
    )
    db.commit()

def add_points_once(i: int):
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == alice_id).first()
        balance_after = _add_points(db, user, 7, "test", f"并发自增 {i}", f"cc:{i}")
        db.commit()
        return True, balance_after
    except HTTPException as exc:
        return False, exc
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, exc
    finally:
        db.close()


results = run_parallel(add_points_once, 6)
oks = [r for r in results if r and r[0]]
check("并发 _add_points：6 次全部成功", len(oks) == 6, f"成功 {len(oks)}")
final = balance(alice_id)
check("并发 _add_points：无丢更新（1000 + 6×7）", final == 1042, f"余额={final}")

with SessionLocal() as db:
    logs = db.query(models.PointsLog).filter(models.PointsLog.ref_id.like("cc:%")).count()
check("并发 _add_points：台账逐笔落库", logs == 6, f"台账条数={logs}")

# ==================== 4. 邀请关系幂等 ====================

before_inviter = balance(inviter_id)
before_invitee = balance(invitee_id)

applied = []
with SessionLocal() as db:
    for _ in range(2):
        user = db.query(models.WebUser).filter(models.WebUser.id == invitee_id).first()
        applied.append(apply_invitation(db, user, "CCINVITE"))
        db.commit()

check("邀请幂等：第一次应用成功", applied[0].get("applied") is True, f"{applied[0]}")
check("邀请幂等：第二次被拒（不重复发奖）", applied[1].get("applied") is False, f"{applied[1]}")

with SessionLocal() as db:
    rec = db.query(models.InvitationRecord).filter(
        models.InvitationRecord.invitee_id == invitee_id
    ).count()
check("邀请幂等：只有 1 条关系记录", rec == 1, f"记录数={rec}")
check(
    "邀请幂等：双向奖励各只发一次",
    balance(inviter_id) - before_inviter == 100 and balance(invitee_id) - before_invitee == 50,
    f"邀请者 +{balance(inviter_id) - before_inviter} / 被邀请者 +{balance(invitee_id) - before_invitee}",
)

# 并发重复应用
with SessionLocal() as db:
    u = models.WebUser(username="cc-invitee2", password_hash="not-used", is_active=True, points=0)
    db.add(u)
    db.commit()
    db.refresh(u)
    invitee2_id = u.id


def invite_once(i: int):
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == invitee2_id).first()
        res = apply_invitation(db, user, "CCINVITE")
        db.commit()
        return True, res
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, exc
    finally:
        db.close()


results = run_parallel(invite_once, 3)
applied_count = len([r for r in results if r and r[0] and r[1].get("applied") is True])
with SessionLocal() as db:
    rec2 = db.query(models.InvitationRecord).filter(
        models.InvitationRecord.invitee_id == invitee2_id
    ).count()
check("邀请并发：最多只应用一次", applied_count <= 1, f"applied={applied_count}")
check("邀请并发：关系记录不超过 1 条", rec2 <= 1, f"记录数={rec2}")
check("邀请并发：无未处理异常", all(r and r[0] for r in results),
      f"{[str(r[1])[:60] for r in results if r and not r[0]]}")

# ==================== 5. 限流 IP 取值不可伪造 ====================
# 注意：get_client_ip 只信任可信代理（TRUSTED_PROXIES，默认 127.0.0.0/8）的头。
# 测试头解析时，直连 IP 必须用 127.0.0.1（可信）；测试防伪造时，用非可信 IP。

ip_xff_multi = client_ip(FakeRequest({"x-forwarded-for": "1.2.3.4, 203.0.113.7"}, client_host="127.0.0.1"))
check("client_ip：XFF 取最后一段（忽略攻击者前缀）", ip_xff_multi == "203.0.113.7", f"ip={ip_xff_multi}")

ip_real_wins = client_ip(FakeRequest({
    "x-real-ip": "198.51.100.5",
    "x-forwarded-for": "1.1.1.1, 2.2.2.2",
}, client_host="127.0.0.1"))
check("client_ip：X-Real-IP 优先（Nginx 按 $remote_addr 硬写）",
      ip_real_wins == "198.51.100.5", f"ip={ip_real_wins}")

# 不可信来源伪造头 → 必须被忽略，用直连 IP
ip_spoofed = client_ip(FakeRequest({"x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8"}))
check("client_ip：不可信来源的伪造头被忽略", ip_spoofed == "10.0.0.9", f"ip={ip_spoofed}")

ip_direct = client_ip(FakeRequest())
check("client_ip：无代理头时使用直连地址", ip_direct == "10.0.0.9", f"ip={ip_direct}")

# 攻击者轮换 XFF 但真实 IP 固定 → 仍然只能用一个限流桶
from backend.ratelimit import check_rate_limit  # noqa: E402

key_ip = client_ip(FakeRequest({"x-real-ip": "203.0.113.99", "x-forwarded-for": "1.1.1.1"}))
statuses = []
for i in range(9):
    spoofed = FakeRequest({"x-real-ip": "203.0.113.99", "x-forwarded-for": f"9.9.9.{i}"})
    allowed, _ = check_rate_limit(f"cc-spoof:{client_ip(spoofed)}", 8, 60)
    statuses.append(allowed)
check("限流不可靠伪造 XFF 绕过：第 9 次被拒",
      statuses[:8] == [True] * 8 and statuses[8] is False, f"allowed={statuses}")

# ==================== 6. 优惠券单人限领不可被并发绕过 ====================
# 旧实现是「先查该人已领几张、再插入核销记录」：同一用户的两个请求同时走到这里，
# 都会读到「还没领满」，于是各领一张，绕过每人限领。现在占名额改成条件
# INSERT ... SELECT（该人有效核销数 < per_user_limit 才写入），并发下只可能成功一次。

from decimal import Decimal  # noqa: E402

from backend import coupons  # noqa: E402

with SessionLocal() as db:
    db.add(models.CouponCode(code="CC-PER-USER", kind="recharge", discount_type="fixed",
                             value=5, max_uses=50, use_count=0, per_user_limit=1, is_active=True))
    db.commit()
    coupon_id = db.query(models.CouponCode).filter(
        models.CouponCode.code == "CC-PER-USER").first().id


def reserve_once(i: int):
    db = SessionLocal()
    try:
        row = db.query(models.CouponCode).filter(models.CouponCode.id == coupon_id).first()
        user = db.query(models.WebUser).filter(models.WebUser.id == alice_id).first()
        usage = coupons.reserve(
            db, coupon=row, user=user, order_id=f"CC-ORDER-{i}", kind="recharge",
            list_price=Decimal("30.00"), discount=Decimal("5.00"), paid=Decimal("25.00"),
        )
        db.commit()
        return True, usage.id
    except HTTPException as exc:
        db.rollback()
        return False, exc
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, exc
    finally:
        db.close()


results = run_parallel(reserve_once, 5)
wins = [r for r in results if r and r[0]]
with SessionLocal() as db:
    usage_rows = db.query(models.CouponUsage).filter(
        models.CouponUsage.coupon_id == coupon_id).count()
    use_count = db.query(models.CouponCode.use_count).filter(
        models.CouponCode.id == coupon_id).scalar()

check("同一用户并发领同一张券：只成功一次", len(wins) == 1,
      f"成功 {len(wins)} 次；拒绝原因示例 {[str(r[1])[:40] for r in results if r and not r[0]][:2]}")
check("核销记录只留一行（没有多领）", usage_rows == 1, f"实际 {usage_rows} 行")
check("该券总用量也只记了 1", use_count == 1, f"实际 use_count={use_count}")


# ==================== 7. 卡码并发核销：只能发一份会员 ====================
# 旧实现是「先读 use_count 判断可用，再写 use_count + 1」（`codes.consume`）：
# 同一张 max_uses=1 的卡码被两个请求同时提交时，两边都读到 use_count=0，
# 于是各发一份会员天数（重复核销）。现在发奖前先用条件 UPDATE 原子占位
# （`codes.claim_code`），抢不到的一边直接拒绝。

print("\n--- 卡码并发核销 ---")

from backend import codes  # noqa: E402

with SessionLocal() as db:
    db.add(models.RegistrationCode(
        code="CC-REG-ONE", code_type=codes.CODE_TYPE_REGISTER, days=30,
        max_uses=1, use_count=0, is_active=True, note="并发回归用",
    ))
    db.commit()
    reg_code_id = db.query(models.RegistrationCode).filter(
        models.RegistrationCode.code == "CC-REG-ONE").first().id


def membership_days_total() -> int:
    """全库按订阅到期时间算出的「总天数」（不依赖哪个用户拿到）"""
    with SessionLocal() as db:
        rows = db.query(models.UserSubscription).all()
        total = 0
        for row in rows:
            if row.end_date and row.start_date:
                total += int((row.end_date - row.start_date).days)
        return total


before_days = membership_days_total()


def redeem_reg_code(i: int):
    """4 个线程同时核销同一张单次卡码（两个用户各两次，覆盖同码 + 同用户两种竞争）"""
    uid = alice_id if i % 2 == 0 else bob_id
    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(models.WebUser.id == uid).first()
        result = codes.redeem_code(db, user, "CC-REG-ONE")
        if result.get("success"):
            # codes 里不再自己提交：调用方提交（与 api/user.py 的核销端点同一口径）
            db.commit()
        else:
            db.rollback()
        return True, result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, exc
    finally:
        db.close()


results = run_parallel(redeem_reg_code, 4)
wins = [r for r in results if r and r[0] and r[1].get("success")]
errors = [r for r in results if r and not r[0]]

with SessionLocal() as db:
    code_row = db.query(models.RegistrationCode).filter(
        models.RegistrationCode.id == reg_code_id).first()
    use_count = code_row.use_count
    still_active = code_row.is_active
    used_by = code_row.used_by or ""

check("卡码并发核销：只有 1 个请求成功", len(wins) == 1,
      f"成功 {len(wins)} 次；失败原因 {[r[1].get('message') for r in results if r and r[0] and not r[1].get('success')][:3]}")
check("卡码并发核销：没有未处理异常（不 500）", not errors, f"{[str(r[1])[:60] for r in errors]}")
check("卡码并发核销：use_count 恰好 1", use_count == 1, f"use_count={use_count}")
check("卡码并发核销：用满后自动停用", still_active is False, f"is_active={still_active}")
check("卡码并发核销：只发了一份会员天数（30 天）",
      membership_days_total() - before_days == 30,
      f"新增 {membership_days_total() - before_days} 天")
check("卡码并发核销：使用者审计只记了一个账号",
      len([x for x in used_by.split(",") if x.strip()]) == 1, f"used_by={used_by!r}")

# 用尽的码再核销一次：必须被拒，且不再发天数
after_once = membership_days_total()
db = SessionLocal()
try:
    user = db.query(models.WebUser).filter(models.WebUser.id == invitee_id).first()
    again = codes.redeem_code(db, user, "CC-REG-ONE")
    db.rollback()
finally:
    db.close()
check("卡码并发核销：用尽后再核销被拒", again.get("success") is False,
      str(again.get("message")))
check("卡码并发核销：拒绝时没有再发会员", membership_days_total() == after_once,
      f"{membership_days_total()} vs {after_once}")

# ==================== 8. 订单退款并发：只能冲正一次 ====================
# 旧实现是「读状态 → 判断是不是 paid → 冲正积分 → 写回 refunded」：
# 同一笔已支付订单被并发退两次时，两边都读到 paid，于是各扣一次积分
# （`_reverse_points` 不是幂等的）→ 双重回滚。现在先做条件 UPDATE
# 把 paid 原子推进到 refunded（rowcount=0 的那一边直接返回「已处理」）。

print("\n--- 订单退款并发 ---")

from decimal import Decimal  # noqa: E402

from backend.api.orders_admin import RefundRequest, refund_order  # noqa: E402

REFUND_ORDER = "CC-REFUND-ONE"
REFUND_POINTS = 200

with SessionLocal() as db:
    buyer = models.WebUser(username="cc-refund-buyer", password_hash="not-used",
                           is_active=True, points=REFUND_POINTS)
    staff = models.WebUser(username="cc-refund-staff", password_hash="not-used",
                           is_active=True, is_staff=True, admin_role="super")
    db.add_all([buyer, staff])
    db.commit()
    buyer_id, staff_id = buyer.id, staff.id
    pkg = models.RechargePackage(name="并发退款用积分包", amount=REFUND_POINTS,
                                 price=10, bonus=0, is_active=True)
    db.add(pkg)
    db.flush()
    db.add(models.RechargeOrder(
        order_id=REFUND_ORDER, user_id=buyer_id, package_id=pkg.id, amount=REFUND_POINTS,
        price=Decimal("10.00"), payment_method="alipay", status="paid",
    ))
    # 履约时留下的账本记录：退款按它精确冲正（与真实充值链路一致）
    db.add(models.PointsLog(
        user_id=buyer_id, amount=REFUND_POINTS, balance_after=REFUND_POINTS,
        type="recharge", description="并发退款回归", ref_id=f"recharge:{REFUND_ORDER}",
    ))
    db.commit()


def refund_once(i: int):
    db = SessionLocal()
    try:
        order = db.query(models.RechargeOrder).filter(
            models.RechargeOrder.order_id == REFUND_ORDER).first()
        admin_user = db.query(models.WebUser).filter(models.WebUser.id == staff_id).first()
        out = run_async_endpoint(refund_order, REFUND_ORDER, RefundRequest(reason=f"并发 {i}"),
                                admin_user, db)
        if out[0] and isinstance(out[1], dict):
            db.commit()
        else:
            db.rollback()
        return out
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, exc
    finally:
        db.close()


results = run_parallel(refund_once, 4)
ok_bodies = [r[1] for r in results if r and r[0] and isinstance(r[1], dict)]
real_refunds = [b for b in ok_bodies if not b.get("already_processed")]
already = [b for b in ok_bodies if b.get("already_processed")]
rejected = [r for r in results if r and not r[0]]
bad_rejected = [getattr(exc, "status_code", 500) for _, exc in rejected
                if not (isinstance(exc, HTTPException) and exc.status_code in (400, 409))]

with SessionLocal() as db:
    order = db.query(models.RechargeOrder).filter(
        models.RechargeOrder.order_id == REFUND_ORDER).first()
    points = int(db.query(models.WebUser.points).filter(
        models.WebUser.id == buyer_id).scalar() or 0)
    refund_logs = db.query(models.PointsLog).filter(
        models.PointsLog.ref_id == f"refund:{REFUND_ORDER}",
        models.PointsLog.type == "refund").count()
    order_status = order.status
    refunded_at = order.refunded_at

check("并发退款：只有 1 个请求真正执行了冲正", len(real_refunds) == 1,
      f"真实退款 {len(real_refunds)} 次 / 已处理 {len(already)} 次 / 被拒 {len(rejected)} 次")
check("并发退款：其余请求返回「已处理」或被拒（不是 500）",
      not bad_rejected and len(already) + len(rejected) == 3,
      f"已处理 {len(already)} / 被拒状态码 {[getattr(e, 'status_code', None) for _, e in rejected]}")
check("并发退款：积分只扣回一次（没有双重回滚）", points == 0, f"余额={points}")
check("并发退款：冲正流水只记一笔", refund_logs == 1, f"台账条数={refund_logs}")
check("并发退款：订单落成已退款并留时间", order_status == "refunded" and refunded_at is not None,
      f"status={order_status} refunded_at={refunded_at}")

# ==================== 汇总 ====================

print()
if failures:
    print(f"FAILED  {len(failures)}/{TOTAL}：")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL PASS  {TOTAL}/{TOTAL}")

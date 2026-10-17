"""管理端 v2.4.0 新增端点冒烟测试

覆盖：
- GET /api/admin/users/{id}         用户 360° 详情
- GET /api/admin/stats/trend        趋势统计（按日补零）
- GET /api/admin/economy/subscriptions  订阅总览（状态筛选 + 概览）
- GET /api/admin/announcements      公告列表（补齐：此前前端无列表接口）
- GET /api/admin/tickets/{id}/messages  工单会话（补齐：此前前端无会话接口）
"""
import os
import random
import sys
from datetime import datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 不写死库文件名：交给 backend.database 解析（新装 aetrix_unified.db，老部署沿用原库）
os.environ.setdefault("DATABASE_TYPE", "sqlite")

from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.main import app
from backend.security import hash_password

init_db()
client = TestClient(app)
suf = str(random.randint(100000, 999999))
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ---------- 造数据 ----------
db = SessionLocal()
admin = models.WebUser(username=f"adm{suf}", password_hash=hash_password("admin12345"), is_staff=True, admin_role="super")
target = models.WebUser(username=f"usr{suf}", password_hash=hash_password("user12345"), points=320)
invitee = models.WebUser(username=f"inv{suf}", password_hash=hash_password("user12345"))
plan = models.SubscriptionPlan(
    name=f"测试套餐{suf}", price=Decimal("19.90"), duration_days=30, is_active=True,
)
db.add_all([admin, target, invitee, plan])
db.commit()
for row in (admin, target, invitee, plan):
    db.refresh(row)

now = datetime.now()
db.add_all([
    models.UserSubscription(
        user_id=target.id, plan_id=plan.id,
        start_date=now - timedelta(days=3), end_date=now + timedelta(days=27), status="active",
    ),
    models.UserSubscription(
        user_id=invitee.id, plan_id=plan.id,
        start_date=now - timedelta(days=40), end_date=now - timedelta(days=10), status="expired",
    ),
    models.PointsLog(user_id=target.id, amount=200, balance_after=200, type="checkin", description="签到"),
    models.PointsLog(user_id=target.id, amount=150, balance_after=350, type="rebate", description="充值返利"),
    models.PointsLog(user_id=target.id, amount=-30, balance_after=320, type="exchange", description="兑换消费"),
    models.CheckinRecord(user_id=target.id, checkin_date=now.replace(hour=0, minute=0, second=0, microsecond=0), points_awarded=15, streak=4),
    models.InvitationRecord(inviter_id=target.id, invitee_id=invitee.id, code_id=1, reward_points=50),
    models.RechargeOrder(
        order_id=f"SMOKE{suf}", user_id=target.id, package_id=1, amount=500,
        price=Decimal("50.00"), payment_method="alipay", status="paid", paid_at=now,
    ),
    models.Announcement(title=f"冒烟公告{suf}", content="内容", type="system", is_active=True),
    models.Announcement(title=f"停用公告{suf}", content="内容", type="system", is_active=False),
])
db.commit()

smoke_ticket = models.Ticket(
    user_id=target.id, title=f"冒烟工单{suf}", category="other", priority="high", status="open",
)
db.add(smoke_ticket)
db.commit()
db.add_all([
    models.TicketMessage(ticket_id=smoke_ticket.id, user_id=target.id, message="用户提问", is_admin=False),
    models.TicketMessage(ticket_id=smoke_ticket.id, user_id=admin.id, message="管理员答复", is_admin=True),
])
db.commit()
target_id, admin_name, ticket_id = target.id, admin.username, smoke_ticket.id
db.close()

r = client.post("/api/user/auth/login", json={"username": admin_name, "password": "admin12345"})
check("管理员登录", r.status_code == 200, str(r.status_code))
token = r.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
# 管理后台与门户同一套 JWT：管理员在门户登录后进后台无需二次登录
check("门户登录返回 is_staff", r.json()["user"].get("is_staff") is True)

try:
    # ---------- 用户详情 ----------
    r = client.get(f"/api/admin/users/{target_id}", headers=headers)
    check("用户详情 200", r.status_code == 200, str(r.status_code))
    data = r.json()
    check("详情含资料", data.get("profile", {}).get("username") == f"usr{suf}")
    # days_left 与用户端一致：截断取整（27 天有效期可能显示 26）
    active_days = (data.get("subscription", {}).get("active") or {}).get("days_left")
    check("详情含生效订阅", active_days is not None and 25 <= active_days <= 27, str(active_days))
    check("详情含积分收支", data["points"]["income"] == 350 and data["points"]["expense"] == 30,
          f"income={data['points']['income']} expense={data['points']['expense']}")
    check("详情含订单金额", data["orders"]["paid_total"] == 50.0, str(data["orders"]["paid_total"]))
    check("详情含邀请统计", data["invitation"]["count"] == 1 and data["invitation"]["rebate_total"] == 150,
          f"count={data['invitation']['count']} rebate={data['invitation']['rebate_total']}")
    check("详情含签到统计", data["checkin"]["total"] == 1 and data["checkin"]["streak"] == 4)
    check("详情含观看统计", "plays" in data["watch"] and "watched_items" in data["watch"])

    # 不存在的用户 → 404
    r = client.get("/api/admin/users/99999999", headers=headers)
    check("不存在用户 404", r.status_code == 404, str(r.status_code))

    # ---------- 趋势统计 ----------
    r = client.get("/api/admin/stats/trend?days=7", headers=headers)
    check("趋势 200", r.status_code == 200, str(r.status_code))
    trend = r.json()
    check("趋势按日补零", len(trend["series"]) == 7, str(len(trend["series"])))
    today_row = trend["series"][-1]
    check("趋势今日新增用户", today_row["new_users"] >= 3, str(today_row["new_users"]))
    check("趋势今日营收", today_row["revenue"] >= 50.0, str(today_row["revenue"]))
    check("趋势今日签到", today_row["checkins"] >= 1, str(today_row["checkins"]))
    check("趋势汇总一致", trend["totals"]["new_users"] >= 3)

    # ---------- 订阅总览 ----------
    r = client.get("/api/admin/economy/subscriptions", headers=headers)
    check("订阅总览 200", r.status_code == 200, str(r.status_code))
    subs = r.json()
    check("订阅概览计数", subs["summary"]["active"] >= 1 and subs["summary"]["expired"] >= 1,
          str(subs["summary"]))
    # 列表默认 limit=50 且按 end_date 升序：本机反复跑测试会把库撑大，刚创建的这条不一定
    # 还排得进前 50（CI 每次都是空库，只有本地会碰到）。这两条要钉的是**字段契约**
    # ——每一行都得带用户名与剩余天数，而不是「某条特定记录必须出现在第一页」。
    subs_rows = subs["subscriptions"]
    check("订阅列表非空且每行都带用户名",
          bool(subs_rows) and all(s.get("username") for s in subs_rows),
          f"{len(subs_rows)} 行")
    check("订阅列表每行都有剩余天数",
          all(isinstance(s.get("days_left"), int) and s["days_left"] >= 0 for s in subs_rows))

    r = client.get("/api/admin/economy/subscriptions?status_filter=expiring", headers=headers)
    check("筛选：7 天内到期", r.status_code == 200 and all(s["days_left"] <= 7 for s in r.json()["subscriptions"]))
    r = client.get("/api/admin/economy/subscriptions?status_filter=expired", headers=headers)
    check("筛选：已过期", r.status_code == 200 and all(s["status"] == "expired" for s in r.json()["subscriptions"]))

    # ---------- 公告列表（补齐缺口） ----------
    r = client.get("/api/admin/announcements", headers=headers)
    check("公告列表 200", r.status_code == 200, str(r.status_code))
    listed = r.json() if r.status_code == 200 else []
    check("公告含未启用项", any(a["title"] == f"停用公告{suf}" for a in listed))
    r = client.get("/api/admin/announcements?active_only=true", headers=headers)
    active_titles = [a["title"] for a in r.json()] if r.status_code == 200 else []
    check("公告 active_only 过滤", f"冒烟公告{suf}" in active_titles and f"停用公告{suf}" not in active_titles)

    # ---------- 工单会话（补齐缺口） ----------
    r = client.get(f"/api/admin/tickets/{ticket_id}/messages", headers=headers)
    check("工单会话 200", r.status_code == 200, str(r.status_code))
    msgs = r.json() if r.status_code == 200 else []
    check("工单会话顺序与内容", len(msgs) == 2 and msgs[0]["message"] == "用户提问", str(len(msgs)))
    check("工单会话标记管理员", msgs[1]["is_admin"] is True and msgs[1]["admin_name"] == admin_name,
          str(msgs[1].get("admin_name")))
    check("不存在工单会话 404", client.get("/api/admin/tickets/99999999/messages", headers=headers).status_code == 404)

    # ---------- 鉴权 ----------
    check("无 token 拒绝", client.get("/api/admin/stats/trend").status_code in (401, 403))

    # ---------- 门户免登（跨端共用同一 token） ----------
    r = client.get("/api/admin/auth/me", headers=headers)
    check("门户 token 直接进后台", r.status_code == 200 and r.json().get("username") == admin_name,
          str(r.status_code))
    r = client.post("/api/user/auth/login", json={"username": f"usr{suf}", "password": "user12345"})
    staff_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    check("门户登录返回非管理员 is_staff", r.json()["user"].get("is_staff") is False)
    check("非管理员门户 token 被拒",
          client.get("/api/admin/auth/me", headers=staff_headers).status_code == 403)
finally:
    db = SessionLocal()
    names = [f"adm{suf}", f"usr{suf}", f"inv{suf}"]
    ids = [u.id for u in db.query(models.WebUser).filter(models.WebUser.username.in_(names)).all()]
    if ids:
        for model, col in (
            (models.UserSubscription, models.UserSubscription.user_id),
            (models.PointsLog, models.PointsLog.user_id),
            (models.CheckinRecord, models.CheckinRecord.user_id),
            (models.RechargeOrder, models.RechargeOrder.user_id),
            (models.InvitationRecord, models.InvitationRecord.inviter_id),
        ):
            db.query(model).filter(col.in_(ids)).delete(synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id.in_(ids)).delete(synchronize_session=False)
    db.query(models.SubscriptionPlan).filter(models.SubscriptionPlan.name == f"测试套餐{suf}").delete(synchronize_session=False)
    for ann in db.query(models.Announcement).filter(
        models.Announcement.title.like(f"%公告{suf}")
    ).all():
        db.delete(ann)
    for t in db.query(models.Ticket).filter(models.Ticket.title == f"冒烟工单{suf}").all():
        db.query(models.TicketMessage).filter(models.TicketMessage.ticket_id == t.id).delete(synchronize_session=False)
        db.delete(t)
    db.commit()
    db.close()

print(f"\n{'=' * 50}\n{'全部通过 ✅' if not failures else '存在失败 ❌: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)

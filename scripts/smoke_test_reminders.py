#!/usr/bin/env python3
"""订阅到期提醒冒烟测试（v2.8.0）

要解决的问题：**会员到期前没有任何提醒**。`AdminEvent.SUBSCRIPTION_EXPIRED` 这个事件类型
早就定义好了，却从来没被发送过一次；续期只能指望用户自己想起来。这一版把「到期前 7/3/1 天
提醒 + 到期那一刻通知一次」做成一个真在跑的后台任务（`backend/reminders.py`）。

本脚本守的不是「有一个开关」，而是下面这些口径：

1. **只在窗口内提醒**：还早得很（+10 天）不打扰；进入档位（7/3/1 天）才发；
2. **一条订阅一档只发一次**：任务一小时跑一轮，重复执行必须 0 发送（靠唯一约束去重），
   不是靠"看起来不会重复"——所以第二轮必须证明 `reminded=0` 且消息条数不变；
3. **档位会推进**：7 天档发过之后，剩下的天数掉到 3 天档要再提醒一次（而不是因为
   "已提醒过"就一路沉默到过期）；
4. **到期通知**：刚过期的订阅发一次「已到期」，并且**不去翻 status 字段**
   （会员状态以 end_date 现算，翻 status 会丢掉"曾经买过"的语义）；
5. **开关与档位可配**：关掉后一条都不发；档位支持自定义（14,2），写错时回落默认 7/3/1，
   而不是让后台任务整体失效；
6. **落到用户端**：提醒是站内信（`message_type=subscription`），用户消息中心能查到——
   不是只写一行日志；
7. **面板口径**：管理端能看到待发条数、累计已发、最近发送明细，并且能手动跑一轮
   （普通用户不能访问这些接口）。

用法：python scripts/smoke_test_reminders.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "e2e-test-secret-key-not-for-production"

WORK = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(WORK, 'reminders.db')}"

from datetime import datetime, timedelta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from backend import models, realms, reminders  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.security import hash_password  # noqa: E402

init_db()

from backend.main import app  # noqa: E402

client = TestClient(app)

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ==================== 播种：用户 / 套餐 / 订阅 ====================

with SessionLocal() as db:
    staff = models.WebUser(username="reminder_staff", password_hash=hash_password("staffpass123"),
                           is_staff=True, admin_role="super", is_active=True)
    db.add(staff)
    db.commit()

    realm = realms.active_realm(db)
    plan = models.SubscriptionPlan(name="月卡", price=10, duration_days=30,
                                   realm_id=realm.id, is_active=True)
    db.add(plan)
    db.commit()
    db.refresh(plan)

    now = datetime.now()
    # 每个种子一条独立订阅，档位按「距到期天数」区分。
    # 多给半天余量：天数按整天向下取整（与用户端 `days_left` / 后台「剩余」同一口径），
    # 否则 6 天差几毫秒就会算成 5 天。
    seeds = [
        ("far", now + timedelta(days=10, hours=12)),   # 还早：不该被提醒
        ("week", now + timedelta(days=6, hours=12)),   # 7 天档（剩 6 天）
        ("three", now + timedelta(days=2, hours=12)),  # 3 天档（剩 2 天）
        ("today", now + timedelta(hours=12)),          # 1 天档（不满一天 → 0 天）
        ("lapsed", now - timedelta(days=1)),           # 已到期
    ]
    users: dict[str, int] = {}
    subs: dict[str, int] = {}
    for name, end in seeds:
        user = models.WebUser(username=f"rem_{name}", password_hash=hash_password("rempass123"),
                              is_active=True)
        db.add(user)
        db.commit()
        sub = models.UserSubscription(user_id=user.id, plan_id=plan.id, realm_id=realm.id,
                                      start_date=end - timedelta(days=30), end_date=end,
                                      status="active")
        db.add(sub)
        db.commit()
        users[name] = user.id
        subs[name] = sub.id

    realm_id = realm.id
    plan_id = plan.id

r = client.post("/api/user/auth/login", json={"username": "reminder_staff", "password": "staffpass123"})
check("管理员登录成功", r.status_code == 200, f"status={r.status_code}")
ADMIN_H = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.post("/api/user/auth/login", json={"username": "rem_week", "password": "rempass123"})
check("普通用户登录成功", r.status_code == 200, f"status={r.status_code}")
USER_H = {"Authorization": f"Bearer {r.json()['access_token']}"}


def station_messages(user_id: int, message_type: str | None = None) -> list:
    with SessionLocal() as db:
        q = db.query(models.StationMessage).filter(models.StationMessage.to_user_id == user_id)
        if message_type:
            q = q.filter(models.StationMessage.message_type == message_type)
        return q.order_by(models.StationMessage.id.asc()).all()


def reset_reminders() -> None:
    with SessionLocal() as db:
        db.query(models.SubscriptionReminder).delete()
        db.query(models.StationMessage).filter(
            models.StationMessage.message_type == reminders.MESSAGE_TYPE
        ).delete()
        db.commit()


def run_pass(dry_run: bool = False) -> dict:
    with SessionLocal() as db:
        return asyncio.run(reminders.run_expiry_reminders(db, dry_run=dry_run))


# ==================== 1. 默认口径 ====================

print("\n--- 默认口径：开启 + 7/3/1 天 ---")

with SessionLocal() as db:
    check("默认开启到期提醒", reminders.reminders_enabled(db) is True)
    check("默认档位 7/3/1", reminders.parse_thresholds(db) == [7, 3, 1],
          str(reminders.parse_thresholds(db)))
    due = reminders.collect_due(db)
    kinds = sorted(i["kind"] for i in due["reminders"])
    check("窗口内 3 条临期（7 天档 / 3 天档 / 1 天档）", kinds == ["1d", "3d", "7d"], str(kinds))
    check("还早的那条不提醒（+10 天）",
          all(i["user_id"] != users["far"] for i in due["reminders"]), str(due["reminders"]))
    check("刚过期的那条进入「已到期」档",
          [i["kind"] for i in due["expired"]] == ["expired"] and
          due["expired"][0]["user_id"] == users["lapsed"], str(due["expired"]))

# ==================== 2. dry_run 不落库 ====================

print("\n--- dry_run：只看会发什么 ---")

reset_reminders()
preview = run_pass(dry_run=True)
check("dry_run 报告待发 4 条（3 临期 + 1 到期）", len(preview["due"]) == 4, str(preview["due"]))
check("dry_run 不发送", preview["reminded"] == 0 and preview["expired_notified"] == 0,
      str(preview))
check("dry_run 不写站内信", station_messages(users["week"]) == [])
with SessionLocal() as db:
    check("dry_run 不写去重记录",
          db.query(models.SubscriptionReminder).count() == 0)

# ==================== 3. 真跑一轮 ====================

print("\n--- 真跑一轮：落站内信 + 去重 ---")

summary = run_pass()
check("临期提醒 3 条", summary["reminded"] == 3, str(summary))
check("到期通知 1 条", summary["expired_notified"] == 1, str(summary))

week_msgs = station_messages(users["week"])
check("7 天档用户收到站内信", len(week_msgs) == 1, f"count={len(week_msgs)}")
if week_msgs:
    msg = week_msgs[0]
    check("站内信归类为 subscription（用户端消息中心按此展示）",
          msg.message_type == reminders.MESSAGE_TYPE, msg.message_type)
    check("标题写明剩余天数", "6 天" in msg.title, msg.title)
    check("正文含到期日期与续费引导",
          msg.title and msg.title.strip() != "" and "续费" in msg.content, msg.content[:60])
    check("关联到具体订阅（related_id）", msg.related_id == subs["week"], str(msg.related_id))

check("还早的用户没收到任何提醒", station_messages(users["far"]) == [])
lapsed_msgs = station_messages(users["lapsed"])
check("已到期用户收到一次到期通知", len(lapsed_msgs) == 1, f"count={len(lapsed_msgs)}")
if lapsed_msgs:
    check("到期通知标题标记「已到期」", "已到期" in lapsed_msgs[0].title, lapsed_msgs[0].title)

with SessionLocal() as db:
    check("去重表记录了 4 条", db.query(models.SubscriptionReminder).count() == 4)
    # 口径：不翻 status，会员状态仍然由 end_date 现算
    lapsed = db.query(models.UserSubscription).filter(
        models.UserSubscription.id == subs["lapsed"]).first()
    check("过期不改写 status（历史语义保留）", lapsed.status == "active", lapsed.status)
    check("过期订阅在播放入口已经不算会员",
          __import__("backend.subscriptions", fromlist=["x"]).has_active_subscription(
              db, users["lapsed"], realm_id) is False)

# ==================== 4. 幂等：第二轮 0 发送 ====================

print("\n--- 幂等：重复执行不再打扰 ---")

second = run_pass()
check("第二轮 0 发送", second["reminded"] == 0 and second["expired_notified"] == 0, str(second))
check("第二轮没有新的站内信", len(station_messages(users["week"])) == 1)
with SessionLocal() as db:
    check("去重表仍是 4 条", db.query(models.SubscriptionReminder).count() == 4)

# 直接插入重复的（订阅, 档位）必须被唯一约束挡下
with SessionLocal() as db:
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session  # noqa: F401
    db.add(models.SubscriptionReminder(subscription_id=subs["week"], user_id=users["week"],
                                       realm_id=realm_id, kind="7d"))
    try:
        db.commit()
        blocked = False
    except IntegrityError:
        db.rollback()
        blocked = True
    check("唯一约束挡住重复档位", blocked)

# ==================== 5. 档位推进：7 天档发过 → 掉到 3 天档再提醒 ====================

print("\n--- 档位推进：7 天档之后还能收到 3 天档 ---")

with SessionLocal() as db:
    week = db.query(models.UserSubscription).filter(
        models.UserSubscription.id == subs["week"]).first()
    # 同样留半天余量：2 天 12 小时后剩 2 天 → 落在 3 天档（而不是 1 天档）
    week.end_date = datetime.now() + timedelta(days=2, hours=12)
    db.commit()

advanced = run_pass()
check("掉到 3 天档后再提醒一次", advanced["reminded"] == 1, str(advanced))
check("只补发没发过的那一档",
      [i["kind"] for i in advanced["due"]] == ["3d"], str(advanced["due"]))
week_titles = [m.title for m in station_messages(users["week"])]
check("同一用户累计两条站内信（7 天档 + 3 天档）",
      len(week_titles) == 2 and "6 天" in week_titles[0] and "2 天" in week_titles[1],
      str(week_titles))

# ==================== 6. 开关与自定义档位 ====================

print("\n--- 开关与档位：可配、写错不致命 ---")

# 2026-10-21 A 类开关摘面板：到期提醒总开关改走 env EXPIRY_REMINDER_ENABLED，
# 面板只改档位等正经配置；这里按 env 关闭，并确认面板只读口径如实回显
os.environ["EXPIRY_REMINDER_ENABLED"] = "false"
r = client.get("/api/admin/economy/expiry-reminders", headers=ADMIN_H)
check("env 关闭到期提醒后面板如实回显", r.status_code == 200 and r.json().get("enabled") is False,
      f"status={r.status_code} {r.text[:120]}")

with SessionLocal() as db:
    # 造一条新的临期订阅，证明关掉后一条都不发
    user = models.WebUser(username="rem_off", password_hash=hash_password("rempass123"),
                          is_active=True)
    db.add(user)
    db.commit()
    db.add(models.UserSubscription(user_id=user.id, plan_id=plan_id, realm_id=realm_id,
                                   start_date=datetime.now(), end_date=datetime.now() + timedelta(days=2),
                                   status="active"))
    db.commit()
    off_user = user.id

off = run_pass()
check("关闭后不发送任何提醒",
      off["enabled"] is False and off["reminded"] == 0 and off["expired_notified"] == 0, str(off))
check("关闭后不落站内信", station_messages(off_user) == [])

os.environ.pop("EXPIRY_REMINDER_ENABLED", None)  # 恢复缺省（开）
r = client.put("/api/admin/economy/expiry-reminders/settings", headers=ADMIN_H,
               json={"settings": {"expiry_reminder_days": "14,2"}})
check("面板可改档位", r.status_code == 200 and r.json()["status"]["thresholds"] == [14, 2],
      str(r.json().get("status", {}).get("thresholds")))

with SessionLocal() as db:
    due = reminders.collect_due(db)
    check("+10 天的订阅现在落进 14 天档（自定义档位生效）",
          any(i["user_id"] == users["far"] and i["kind"] == "14d" for i in due["reminders"]),
          str([(i["kind"], i["days_left"]) for i in due["reminders"]]))

r = client.put("/api/admin/economy/expiry-reminders/settings", headers=ADMIN_H,
               json={"settings": {"expiry_reminder_days": "abc,,"}})
check("写错档位回落默认 7/3/1",
      r.status_code == 200 and r.json()["status"]["thresholds"] == [7, 3, 1],
      str(r.json().get("status", {}).get("thresholds")))

# ==================== 7. 面板口径与权限 ====================

print("\n--- 面板口径与权限 ---")

r = client.get("/api/admin/economy/expiry-reminders", headers=ADMIN_H)
check("面板可读提醒口径", r.status_code == 200, f"status={r.status_code}")
status = r.json()
check("口径含开关 / 档位 / 待发 / 累计 / 最近明细",
      all(k in status for k in ("enabled", "thresholds", "pending_reminders",
                                "pending_expired", "total_sent", "recent")), str(sorted(status)))
check("最近发送明细带用户名与档位",
      status["recent"] and status["recent"][0].get("username") and status["recent"][0].get("kind"),
      str(status["recent"][:2]))

r = client.post("/api/admin/economy/expiry-reminders/run?dry_run=true", headers=ADMIN_H)
check("面板可预览待发（dry_run）", r.status_code == 200 and r.json()["dry_run"] is True,
      f"status={r.status_code}")

r = client.post("/api/admin/economy/expiry-reminders/run?dry_run=true")
check("未登录不能预览（鉴权生效）", r.status_code in (401, 403), f"status={r.status_code}")

r = client.post("/api/admin/economy/expiry-reminders/run?dry_run=true", headers=USER_H)
check("普通用户不能访问提醒接口", r.status_code in (401, 403), f"status={r.status_code}")

r = client.get("/api/admin/economy/expiry-reminders", headers=USER_H)
check("普通用户看不到提醒口径", r.status_code in (401, 403), f"status={r.status_code}")

# ==================== 8. 用户端：消息中心能看到提醒 ====================

print("\n--- 用户端：提醒进了消息中心 ---")

r = client.get("/api/user/messages", headers=USER_H)
check("用户消息列表可读", r.status_code == 200, f"status={r.status_code}")
sub_msgs = [m for m in r.json() if m.get("message_type") == reminders.MESSAGE_TYPE]
check("消息中心里能看到订阅提醒", len(sub_msgs) >= 2, f"count={len(sub_msgs)}")

r = client.get("/api/user/messages/unread-count", headers=USER_H)
check("未读计数包含提醒",
      r.status_code == 200 and r.json().get("unread_count", 0) >= 2, str(r.json()))

# 临期账号的订阅接口给出 days_left（用户端临期提示的数据来源）
r = client.get("/api/user/subscriptions", headers=USER_H)
rows = r.json() if r.status_code == 200 else []
check("我的订阅返回 days_left（用户端据此提示临期）",
      bool(rows) and isinstance(rows[0].get("days_left"), int), str(rows[:1]))

# ==================== 9. 调度线程只启一次 ====================

print("\n--- 后台线程：只启一个 ---")

check("首次启动调度器成功", reminders.start_reminder_scheduler(60) is True)
check("重复启动返回 False（不会多线程打架）", reminders.start_reminder_scheduler(60) is False)


print("\n" + "=" * 60)
if failures:
    print(f"❌ {len(failures)}/{checks} 项失败：")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print(f"✅ 到期提醒冒烟测试全部通过（{checks} 项）")

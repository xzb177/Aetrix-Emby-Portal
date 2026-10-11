"""前后台查漏补缺（v2.5.1）冒烟测试

覆盖本轮修复的行为缺口：
- VIP 状态派生：持有生效中订阅 → /auth/me 的 is_vip 为真；过期订阅 → 假（此前恒为 false，会员标识永不显示）
- 订阅列表按日期归一生效状态：end_date 已过且 status 仍为 active 的行返回 expired（此前直接透传库内字段，用户端显示「生效中」）
- 工单回复通知管理员：用户回复后管理员收到站内消息（此前是 TODO，只有新建工单才通知）
- 管理端可配置每日求片上限：PUT /api/admin/economy/settings 写入 media_seek_daily_limit 后用户端求片额度随之变化
"""
import os
import random
import sys
from datetime import datetime, timedelta

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
staff = models.WebUser(username=f"stf{suf}", password_hash=hash_password("staff12345"), is_staff=True, admin_role="super")
vip = models.WebUser(username=f"vip{suf}", password_hash=hash_password("pass12345"))
expired = models.WebUser(username=f"exp{suf}", password_hash=hash_password("pass12345"))
plain = models.WebUser(username=f"pln{suf}", password_hash=hash_password("pass12345"))
# 生产里订阅/套餐必带具体 realm_id（遗留 NULL 行在 init_db 时已回填到默认服）：
# 冒烟造数必须跟生产一致，否则服过滤会把 NULL 行排除。
from backend import realms as realm_lib  # noqa: E402
default_realm_id = realm_lib.active_realm_id(db)
plan = models.SubscriptionPlan(
    name=f"查缺套餐{suf}", price=19.9, duration_days=30, is_active=True,
    realm_id=default_realm_id,
)
db.add_all([staff, vip, expired, plain, plan])
db.commit()
for row in (staff, vip, expired, plain, plan):
    db.refresh(row)

now = datetime.now()
db.add_all([
    # 生效中
    models.UserSubscription(user_id=vip.id, plan_id=plan.id, realm_id=default_realm_id,
                            start_date=now - timedelta(days=2), end_date=now + timedelta(days=28),
                            status="active"),
    # 已过期但库内仍是 active（真实场景：无人翻转状态字段）
    models.UserSubscription(user_id=expired.id, plan_id=plan.id, realm_id=default_realm_id,
                            start_date=now - timedelta(days=40), end_date=now - timedelta(days=5),
                            status="active"),
])
ticket = models.Ticket(user_id=vip.id, title=f"查缺工单{suf}", category="other",
                       priority="normal", status="open")
db.add(ticket)
db.commit()
db.refresh(ticket)
ticket_id = ticket.id
vip_name, expired_name, plain_name, staff_name = vip.username, expired.username, plain.username, staff.username
db.close()


def login(username: str, password: str) -> dict:
    r = client.post("/api/user/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


vip_h = login(vip_name, "pass12345")
exp_h = login(expired_name, "pass12345")
plain_h = login(plain_name, "pass12345")
staff_h = login(staff_name, "staff12345")

# ---------- VIP 派生 ----------
r = client.get("/api/user/auth/me", headers=vip_h)
check("生效中订阅 → is_vip=true", r.status_code == 200 and r.json()["is_vip"] is True, str(r.json().get("is_vip")))

r = client.get("/api/user/auth/me", headers=exp_h)
check("已过期订阅 → is_vip=false", r.status_code == 200 and r.json()["is_vip"] is False, str(r.json().get("is_vip")))

r = client.get("/api/user/auth/me", headers=plain_h)
check("无订阅 → is_vip=false", r.status_code == 200 and r.json()["is_vip"] is False, str(r.json().get("is_vip")))

# ---------- 订阅列表状态归一 ----------
r = client.get("/api/user/subscriptions", headers=exp_h)
rows = r.json() if r.status_code == 200 else []
check("过期订阅读作 expired", r.status_code == 200 and rows and rows[0]["status"] == "expired"
      and rows[0]["is_current"] is False and rows[0]["days_left"] == 0, str(rows[:1]))

r = client.get("/api/user/subscriptions", headers=vip_h)
rows = r.json() if r.status_code == 200 else []
check("生效订阅读作 active + 剩余天数", r.status_code == 200 and rows and rows[0]["status"] == "active"
      and rows[0]["is_current"] is True and rows[0]["days_left"] > 0, str(rows[:1]))

# ---------- 工单回复通知管理员 ----------
r = client.post(f"/api/user/tickets/{ticket_id}/messages", json={"message": "我又补充了一些信息"}, headers=vip_h)
check("用户回复工单", r.status_code == 200, str(r.status_code))

r = client.get("/api/user/messages", headers=staff_h)
titles = [m["title"] for m in r.json()] if r.status_code == 200 else []
check("管理员收到工单回复通知", any("工单" in t and "回复" in t for t in titles), str(titles[:3]))

# ---------- 管理端可配每日求片上限 ----------
r = client.put("/api/admin/economy/settings",
               json={"settings": {"media_seek_daily_limit": "1"}}, headers=staff_h)
check("管理端写入求片上限", r.status_code == 200 and "media_seek_daily_limit" in r.json().get("changed", []),
      str(r.json() if r.status_code == 200 else r.status_code))

r = client.get("/api/admin/economy/settings", headers=staff_h)
check("求片上限回读", r.status_code == 200 and r.json()["settings"].get("media_seek_daily_limit") == "1",
      str(r.json().get("settings", {}).get("media_seek_daily_limit")))

r = client.post("/api/user/media-seek", json={"movie_name": f"限额一{suf}"}, headers=plain_h)
check("限额=1 时首条可提交", r.status_code == 200, str(r.status_code))
r = client.post("/api/user/media-seek", json={"movie_name": f"限额二{suf}"}, headers=plain_h)
check("限额=1 时第二条 429", r.status_code == 429, str(r.status_code))

r = client.get("/api/user/media-seek", headers=plain_h)
_q = r.json().get("quota", {}) if r.status_code == 200 else {}
check("用户端额度随配置变化",
      r.status_code == 200
      and _q.get("used_today") == 1 and _q.get("daily_limit") == 1 and _q.get("remaining") == 0
      and _q.get("kind") == "paid" and _q.get("monthly_limit") == 10
      and _q.get("monthly_used") == 1 and _q.get("monthly_remaining") == 9,
      str(_q))

# 还原默认，避免影响其它用例
client.put("/api/admin/economy/settings",
           json={"settings": {"media_seek_daily_limit": "5"}}, headers=staff_h)

# ---------- 求片：用户提交 → 管理端列表/审批 → 用户收到通知 ----------
seek_name = f"求片链路{suf}"
r = client.post("/api/user/media-seek", json={"movie_name": seek_name, "year": "2022"},
                headers=plain_h)
check("用户提交求片", r.status_code == 200 and r.json().get("request_id"), r.text[:120])
seek_id = r.json()["request_id"]

r = client.get("/api/admin/media-seek", headers=staff_h)
rows = r.json() if r.status_code == 200 else {}
rows = rows.get("requests", rows) if isinstance(rows, dict) else rows
check("管理端能列出这条求片",
      r.status_code == 200 and any(x.get("id") == seek_id for x in (rows or [])),
      f"HTTP {r.status_code} rows={len(rows or [])}")

r = client.put(f"/api/admin/media-seek/{seek_id}",
               json={"status": "approved", "admin_note": "已安排下载"}, headers=staff_h)
check("管理端审批求片", r.status_code == 200, r.text[:120])

r = client.get("/api/user/media-seek", headers=plain_h)
rows = (r.json() or {}).get("requests", []) if r.status_code == 200 else []
row = next((x for x in rows if x.get("id") == seek_id), None)
check("用户端看到审批后的状态", row is not None and row.get("status") == "approved",
      str(row.get("status") if row else None))

r = client.get("/api/user/messages", headers=plain_h)
inbox = r.json() if isinstance(r.json(), list) else (r.json() or {}).get("messages", [])
check("用户收到求片审批站内信",
      r.status_code == 200 and any("求片" in (m.get("title") or "") for m in inbox),
      str([m.get("title") for m in inbox][:3]))

print()
if failures:
    print(f"❌ {len(failures)} 项失败：" + "、".join(failures))
    sys.exit(1)
print("✅ 查漏补缺（v2.5.1）冒烟测试全部通过")

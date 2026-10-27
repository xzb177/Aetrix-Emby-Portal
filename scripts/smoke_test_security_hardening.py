"""安全加固冒烟测试（上线前 P0/P1 项）

用临时 SQLite 库 + 真实 app，不碰开发库、不访问外部服务。覆盖：

1. WebSocket 鉴权：匿名 / 伪造 token / token 与路径 user_id 不一致 / 禁用账号
   一律以 1008 拒绝；本人 token 可正常连接。
2. 通知管理接口：匿名 401、普通用户 403、管理员可发送与广播。
3. /api/health/detailed 的数据库检查真实反映状态（此前恒报 unhealthy）。
4. 「Emby 服务入口」配置驱动用户侧 base_url：分离 EA / 已有 Emby / 未配置回退 env，
   且外部模式下账号卡仍可读（只读信息），自建媒体库接口仍被 503 拦截。
5. 数字 token 兜底已移除：Bearer <user_id> 不再能冒充用户，JWT 与 Emby 客户端
   token 的正常通道不受影响。
6. 管理员登录审计与限流：失败/无权限/成功分别落登录日志，超限返回 429 并留痕。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["EMBY_PUBLIC_URL"] = "http://env-fallback.example.com:8000"
os.environ["SECRET_KEY"] = "e2e-test-secret-key-not-for-production"

DB = os.path.join(tempfile.mkdtemp(), "security-hardening.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from fastapi.testclient import TestClient  # noqa: E402

from backend import models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.auth import hash_emby_token  # noqa: E402
from backend.security import create_access_token, hash_password  # noqa: E402
from backend.integrations.store import invalidate  # 热缓存失效（测试直接写库需手动清）

init_db()

from backend.main import app  # noqa: E402

client = TestClient(app)

failures: list[str] = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ==================== 播种数据 ====================

with SessionLocal() as db:
    # 接线用户名不再由账号卡顺手生成；scheme 断言需要库里已经有用户名。
    alice = models.WebUser(
        username="sec-alice", password_hash=hash_password("alice-pw"), is_active=True,
        emby_username="sec_alice",
    )
    bob = models.WebUser(username="sec-bob", password_hash=hash_password("bob-pw"), is_active=True)
    staff = models.WebUser(
        username="sec-staff", password_hash=hash_password("staff-pw"), is_active=True, is_staff=True, admin_role="super"
    )
    disabled = models.WebUser(
        username="sec-disabled", password_hash=hash_password("disabled-pw"), is_active=False
    )
    db.add_all([alice, bob, staff, disabled])
    db.commit()
    for u in (alice, bob, staff, disabled):
        db.refresh(u)

    alice_id, bob_id, staff_id, disabled_id = alice.id, bob.id, staff.id, disabled.id

    # 查看权限 gating：入口测试断言 alice 能看到 base_url，给她一个有效订阅
    from datetime import datetime, timedelta  # noqa: E402
    from backend import realms as realm_lib  # noqa: E402

    _rid = realm_lib.active_realm_id(db)
    _plan = models.SubscriptionPlan(name="冒烟测试套餐", price=1, duration_days=30, realm_id=_rid)
    db.add(_plan)
    db.flush()
    db.add(models.UserSubscription(user_id=alice_id, plan_id=_plan.id, realm_id=_rid,
                                   status="active",
                                   end_date=datetime.now() + timedelta(days=30)))
    db.commit()

    emby_token = em.EmbyApiToken(token=hash_emby_token("emby-client-token-alice"), user_id=alice_id, device_id="dev-1")
    db.add(emby_token)
    db.commit()

alice_jwt = create_access_token(alice_id, {"username": "sec-alice"})
bob_jwt = create_access_token(bob_id, {"username": "sec-bob"})
staff_jwt = create_access_token(staff_id, {"username": "sec-staff", "staff": True})
disabled_jwt = create_access_token(disabled_id, {"username": "sec-disabled"})

AUTH_ALICE = {"Authorization": f"Bearer {alice_jwt}"}
AUTH_STAFF = {"Authorization": f"Bearer {staff_jwt}"}


def set_config(key: str, value: str) -> None:
    with SessionLocal() as db:
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if row:
            row.value = value
        else:
            db.add(models.SystemConfig(key=key, value=value))
        db.commit()
    invalidate(key)  # 热缓存失效：测试直接写库，手动清缓存


def del_config(key: str) -> None:
    with SessionLocal() as db:
        db.query(models.SystemConfig).filter(models.SystemConfig.key == key).delete()
        db.commit()


# ==================== 1. WebSocket 鉴权 ====================

def ws_connect(path: str):
    """尝试建立 WS 连接，返回 (是否成功, 载荷或异常)"""
    try:
        with client.websocket_connect(path) as ws:
            return True, ws.receive_json()
    except Exception as e:  # noqa: BLE001
        return False, e


ok, payload = ws_connect(f"/ws/{alice_id}")
check("WS 匿名连接被拒绝", not ok, f"结果为 {payload!r}")

ok, payload = ws_connect(f"/ws/{alice_id}?token=not-a-real-token")
check("WS 伪造 token 被拒绝", not ok, f"结果为 {payload!r}")

ok, payload = ws_connect(f"/ws/{bob_id}?token={alice_jwt}")
check("WS token 与路径 user_id 不一致被拒绝", not ok, f"结果为 {payload!r}")

ok, payload = ws_connect(f"/ws/{disabled_id}?token={disabled_jwt}")
check("WS 禁用账号被拒绝", not ok, f"结果为 {payload!r}")

ok, payload = ws_connect(f"/ws/{alice_id}?token={alice_jwt}")
check("WS 本人 token 可连接", ok and isinstance(payload, dict) and payload.get("user_id") == alice_id,
      f"结果为 {payload!r}")

ok, payload = ws_connect(f"/ws/{alice_id}")
check("WS 缺失 token 被拒绝", not ok, f"结果为 {payload!r}")

# ==================== 2. 通知管理接口鉴权 ====================

NOTIFY_BODY = {"user_id": alice_id, "title": "t", "message": "m", "notification_type": "system.announcement"}

r = client.get("/api/notifications/online-users")
check("通知：匿名查在线用户 401", r.status_code == 401, f"status={r.status_code}")

r = client.post("/api/notifications/send", json=NOTIFY_BODY)
check("通知：匿名发送 401", r.status_code == 401, f"status={r.status_code}")

r = client.post("/api/notifications/broadcast", json={
    "channel": "all", "title": "t", "message": "m", "notification_type": "system.announcement"})
check("通知：匿名广播 401", r.status_code == 401, f"status={r.status_code}")

r = client.post("/api/notifications/send", json=NOTIFY_BODY, headers=AUTH_ALICE)
check("通知：普通用户发送 403", r.status_code == 403, f"status={r.status_code}")

r = client.post("/api/notifications/send", json=NOTIFY_BODY, headers=AUTH_STAFF)
check("通知：管理员发送 200", r.status_code == 200, f"status={r.status_code}")

r = client.post("/api/notifications/broadcast", json={
    "channel": "all", "title": "t", "message": "m", "notification_type": "system.announcement"},
    headers=AUTH_STAFF)
check("通知：管理员广播 200", r.status_code == 200, f"status={r.status_code}")

r = client.get("/api/notifications/online-users", headers=AUTH_STAFF)
check("通知：管理员查在线用户 200", r.status_code == 200, f"status={r.status_code}")

# ==================== 3. 健康检查数据库状态 ====================

r = client.get("/api/health/detailed")
body = r.json() if r.status_code == 200 else {}
db_status = body.get("services", {}).get("database", {})
redis_status = body.get("services", {}).get("redis", {})
check("健康检查：数据库上报 healthy", db_status.get("status") == "healthy", f"database={db_status}")
check("健康检查：无 SQLAlchemy 报错", "error" not in db_status, f"database={db_status}")
# Redis 未启用时必须报 disabled（旧实现恒抛 AttributeError → 永远 unhealthy/degraded）
check("健康检查：未启用 Redis 时报 disabled 而非 unhealthy",
      redis_status.get("status") in ("disabled", "healthy"), f"redis={redis_status}")
check("健康检查：整体状态 healthy", body.get("status") == "healthy", f"status={body.get('status')}")

# ==================== 4. Emby 服务入口配置驱动 base_url ====================

for k in ("emby_active_mode", "emby_managed_url", "emby_external_url",
          "emby_managed_enabled", "emby_managed_reachable"):
    del_config(k)

r = client.get("/api/user/emby/server", headers=AUTH_ALICE)
card = r.json() if r.status_code == 200 else {}
check("入口：未配置时回退 EMBY_PUBLIC_URL",
      r.status_code == 200 and card.get("base_url") == "http://env-fallback.example.com:8000",
      f"base_url={card.get('base_url')}")
check("入口：默认模式为 managed_ea", card.get("mode") == "managed_ea", f"mode={card.get('mode')}")

# 分离部署 EA：网上保存的地址必须生效，且大小写不能被改写
set_config("emby_active_mode", "managed_ea")
set_config("emby_managed_url", "https://EA.Media-Example.com/Emby/")
set_config("emby_managed_enabled", "true")
set_config("emby_managed_reachable", "true")

r = client.get("/api/user/emby/server", headers=AUTH_ALICE)
card = r.json() if r.status_code == 200 else {}
check("入口：分离 EA 地址驱动 base_url（去尾斜杠、保留大小写）",
      card.get("base_url") == "https://EA.Media-Example.com/Emby",
      f"base_url={card.get('base_url')}")
check("入口：EA 模式提供一键导入 scheme", bool(card.get("import_schemes")), f"{card.get('import_schemes')}")
check("入口：EA 模式下自建接口可用",
      client.get("/api/user/emby/resume", headers=AUTH_ALICE).status_code == 200)

# EA 已配置但未测试通过 → 自建接口闸门拦截
set_config("emby_managed_reachable", "false")
r = client.get("/api/user/emby/resume", headers=AUTH_ALICE)
check("入口：EA 未连通时自建接口 503", r.status_code == 503, f"status={r.status_code}")
set_config("emby_managed_reachable", "true")

# 已有 Emby 服
set_config("emby_active_mode", "external")
set_config("emby_external_url", "https://emby.partner-example.org")

r = client.get("/api/user/emby/server", headers=AUTH_ALICE)
card = r.json() if r.status_code == 200 else {}
check("入口：已有 Emby 模式下账号卡仍可读", r.status_code == 200, f"status={r.status_code}")
check("入口：外部地址驱动 base_url", card.get("base_url") == "https://emby.partner-example.org",
      f"base_url={card.get('base_url')}")
check("入口：外部模式标记明确", card.get("external") is True and card.get("account_managed_by") == "external",
      f"external={card.get('external')} managed_by={card.get('account_managed_by')}")
check("入口：外部模式不提供本项目导入 scheme", card.get("import_schemes") == {},
      f"import_schemes={card.get('import_schemes')}")

r = client.get("/api/user/emby/resume", headers=AUTH_ALICE)
check("入口：外部模式用户端自建接口 503", r.status_code == 503, f"status={r.status_code}")
r = client.get("/api/admin/emby/libraries", headers=AUTH_STAFF)
check("入口：外部模式管理端自建接口 503", r.status_code == 503, f"status={r.status_code}")

# 复位，避免影响后续断言
for k in ("emby_active_mode", "emby_managed_url", "emby_external_url",
          "emby_managed_enabled", "emby_managed_reachable"):
    del_config(k)

# ==================== 5. 数字 token 兜底已移除 ====================

r = client.get("/api/user/emby/server", headers={"Authorization": f"Bearer {alice_id}"})
check("数字 token：门户接口 401", r.status_code == 401, f"status={r.status_code}")

r = client.get("/api/user/auth/me", headers={"Authorization": f"Bearer {alice_id}"})
check("数字 token：JWT 接口 401", r.status_code == 401, f"status={r.status_code}")

r = client.get("/api/user/auth/me", headers=AUTH_ALICE)
check("数字 token：正常 JWT 仍可用", r.status_code == 200, f"status={r.status_code}")

r = client.get("/api/user/emby/server", params={"api_key": "emby-client-token-alice"})
check("数字 token：Emby 客户端 token 通道不受影响", r.status_code == 200, f"status={r.status_code}")

# ==================== 6. 管理员登录审计与限流 ====================

def login_logs() -> list[dict]:
    with SessionLocal() as db:
        rows = (
            db.query(models.LoginLog)
            .order_by(models.LoginLog.id.desc())
            .limit(50)
            .all()
        )
        return [
            {"username": r.username, "ip": r.ip, "success": r.success, "reason": r.reason, "detail": r.detail}
            for r in rows
        ]


r = client.post("/api/admin/auth/login", json={"username": "sec-staff", "password": "wrong-pw"},
                headers={"X-Real-IP": "10.9.9.1", "X-Forwarded-For": "1.1.1.1, 10.0.0.1",
                         "User-Agent": "audit-test/1.0"})
check("后台登录：密码错误 401", r.status_code == 401, f"status={r.status_code}")
row = login_logs()[0]
check("后台登录：失败被记录",
      row["reason"] == "admin_login_failed" and row["success"] is False and row["username"] == "sec-staff",
      f"{row}")
# 安全加固（P0-0）：TestClient 直连 host 为 "testclient"，不在 TRUSTED_PROXIES 中，
# 因此伪造的 X-Real-IP / XFF 必须被忽略，记录直连 IP，防止日志 IP 伪造和限流绕过。
# 生产环境 nginx 为可信代理时，X-Real-IP 才会被采信（见 tests/test_ratelimit_ip.py）。
check("后台登录：不可信来源的伪造头被忽略（记录直连 IP）",
      row["ip"] == "testclient", f"ip={row['ip']}")

r = client.post("/api/admin/auth/login", json={"username": "sec-alice", "password": "alice-pw"},
                headers={"X-Forwarded-For": "10.9.9.2"})
check("后台登录：非管理员 403", r.status_code == 403, f"status={r.status_code}")
row = login_logs()[0]
check("后台登录：越权尝试被记录",
      row["reason"] == "admin_login_failed" and row["username"] == "sec-alice" and "管理员权限" in (row["detail"] or ""),
      f"{row}")

r = client.post("/api/admin/auth/login", json={"username": "sec-staff", "password": "staff-pw"},
                headers={"X-Forwarded-For": "10.9.9.3"})
check("后台登录：正确凭据 200 且签发 token",
      r.status_code == 200 and bool(r.json().get("access_token")), f"status={r.status_code}")
row = login_logs()[0]
check("后台登录：成功被记录", row["reason"] == "admin_login" and row["success"] is True, f"{row}")

# 限流：同一 IP 1 分钟内最多 8 次尝试，第 9 次 429
# 注意（P0-0 可信代理）：TestClient 直连 IP 为 "testclient"，不在 TRUSTED_PROXIES 中，
# 因此上面的 X-Forwarded-For 头都会被忽略——前面的 3 次登录尝试（密码错误/非管理员/
# 正确登录）已经占用了同一 admin_login:testclient 桶的 3 个名额。这里先清掉桶，
# 让本轮从干净状态开始，验证"8 次放行、第 9 次 429"的原始意图。
from backend.ratelimit import _limiter as _rate_limiter
_rate_limiter.reset("admin_login:testclient")
statuses = [
    client.post("/api/admin/auth/login", json={"username": "sec-staff", "password": "wrong-pw"},
                headers={"X-Forwarded-For": "10.9.9.99"}).status_code
    for _ in range(9)
]
check("后台登录：第 9 次尝试被限流 429",
      statuses[:8] == [401] * 8 and statuses[8] == 429, f"statuses={statuses}")
row = login_logs()[0]
check("后台登录：被限流也留痕",
      row["reason"] == "admin_login_failed" and "频繁" in (row["detail"] or ""), f"{row}")

# 日志事件类型可通过后台接口读取（前端下拉依赖）
r = client.get("/api/admin/login-logs", headers=AUTH_STAFF)
reasons = {item["value"] for item in (r.json().get("reasons") or [])} if r.status_code == 200 else set()
check("后台登录：新增事件类型在日志接口可见",
      {"admin_login", "admin_login_failed"} <= reasons, f"reasons={sorted(reasons)}")

# ==================== 汇总 ====================

print()
if failures:
    print(f"FAILED  {len(failures)}/{TOTAL}：")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL PASS  {TOTAL}/{TOTAL}")

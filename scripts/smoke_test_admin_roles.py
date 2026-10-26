#!/usr/bin/env python3
"""管理员与权限冒烟测试（v2.26.0）

后台此前的身份只有 ``is_staff``：给客服 / 运营 / 审计开号，就得把「改系统设置、改上游 Key、
管管理员」一起交出去。这里钉的是**角色真的生效**，而不只是界面上多了一个下拉框：

1. **fail closed（安全修复 S1）**：``admin_role`` 为空的管理员按只读（viewer）处理；升级上来的
   站长由启动期迁移 ``admin_roles.ensure_legacy_admin_roles`` 显式写成 super；
2. **三种角色**：super（全部）/ operator（日常运营，不能改设置、Key、策略、管理员）/
   viewer（只读：GET 放行、任何写操作 403）；
3. **判定只有一个入口**：``/api/admin/*`` 与 ``/api/admin/emby/*`` 两套鉴权走同一套规则；
4. **三条护栏**：不能改自己、不能没有超级管理员、被停用的账号不能当管理员；
5. **授权 / 改角色 / 撤销**都会留审计日志（AdminLog）。

用法：python scripts/smoke_test_admin_roles.py
"""
import os
import sys
from types import SimpleNamespace
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"

from fastapi.testclient import TestClient  # noqa: E402

from backend import admin_roles, models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.security import create_access_token, hash_password  # noqa: E402
from backend.emby_server.auth import ensure_emby_credentials  # noqa: E402

init_db()
client = TestClient(app)

FAILED = []
TOTAL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def make_user(username: str, *, is_staff: bool = False, role: str | None = None,
              is_active: bool = True) -> int:
    with SessionLocal() as db:
        user = models.WebUser(
            username=username, password_hash=hash_password("pass12345"),
            is_active=is_active, is_staff=is_staff, admin_role=role,
        )
        db.add(user)
        db.commit()
        # Emby 侧门用例要走 AuthenticateByName：直建用户没有 emby_password，
        # Emby 登录只验 emby_password（不回落 password_hash），这里先补齐，
        # 与门户首次登录的 fill_only 补齐结果一致。
        ensure_emby_credentials(db, user, password="pass12345")
        return user.id


def headers(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


# ==================== 1. 升级不变权 ====================
print("=== 1. 空角色管理员按只读处理（S1 fail closed），站长由迁移显式写成 super ===")
owner_id = make_user("owner", is_staff=True)          # 没有 admin_role 的管理员
operator_id = make_user("operator")
viewer_id = make_user("auditor")
disabled_id = make_user("disabled_guy", is_active=False)

H_OWNER, H_OP, H_VIEW = headers(owner_id), headers(operator_id), headers(viewer_id)

me = client.get("/api/admin/auth/me", headers=H_OWNER)
check("空角色管理员 /auth/me 报 viewer（不再默认 super）",
      me.status_code == 200 and me.json().get("admin_role") == "viewer"
      and me.json().get("is_super") is False, str(me.json()))
check("空角色管理员不能写（不能授权管理员）",
      client.post("/api/admin/admins", headers=H_OWNER,
                  json={"username": "operator", "role": "operator"}).status_code == 403)
# 模拟启动期迁移的结果：站长被显式写成 super（迁移本身见 tests/test_security_critical.py）
with SessionLocal() as db:
    db.query(models.WebUser).filter(models.WebUser.id == owner_id).update({"admin_role": "super"})
    db.commit()
me = client.get("/api/admin/auth/me", headers=H_OWNER)
check("站长（显式 super）/auth/me 报 super",
      me.status_code == 200 and me.json().get("admin_role") == "super"
      and me.json().get("is_super") is True and me.json().get("role_label") == "超级管理员",
      str(me.json()))

# ==================== 2. 授权：把用户提为管理员 ====================
print("\n=== 2. 授权 / 改角色 / 撤销 ===")
r = client.post("/api/admin/admins", headers=H_OWNER,
                json={"username": "operator", "role": "operator"})
check("授权运营角色 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")
r = client.post("/api/admin/admins", headers=H_OWNER,
                json={"username": "auditor", "role": "viewer"})
check("授权只读角色 → 200", r.status_code == 200, f"HTTP {r.status_code}")

check("重复授权被拒（已经是管理员）",
      client.post("/api/admin/admins", headers=H_OWNER,
                  json={"username": "operator", "role": "operator"}).status_code == 400)
check("不存在的账号被拒 404",
      client.post("/api/admin/admins", headers=H_OWNER,
                  json={"username": "nobody-here", "role": "operator"}).status_code == 404)
check("非法角色被拒 400",
      client.post("/api/admin/admins", headers=H_OWNER,
                  json={"username": "auditor", "role": "god"}).status_code == 400)
check("被停用的账号不能被授予管理员（免得开一个登不进来的号）",
      client.post("/api/admin/admins", headers=H_OWNER,
                  json={"username": "disabled_guy", "role": "operator"}).status_code == 400)

listing = client.get("/api/admin/admins", headers=H_OWNER).json()
check("清单里能看到角色、角色元数据与超级管理员数量",
      {a["admin_role"] for a in listing["admins"]} >= {"super", "operator", "viewer"}
      and len(listing["roles"]) == 3 and listing["super_count"] == 1,
      f"super_count={listing['super_count']}")

# ==================== 3. 运营角色：日常能写，设置与权限不能碰 ====================
print("\n=== 3. 运营角色（operator）===")
target_id = make_user("normal_user")
r = client.put(f"/api/admin/users/{target_id}", headers=H_OP, json={"is_active": False})
check("运营能做日常运营（改用户状态）→ 200", r.status_code == 200, f"HTTP {r.status_code}")
check("运营能读设置 200",
      client.get("/api/admin/economy/settings", headers=H_OP).status_code == 200)
r = client.put("/api/admin/economy/settings", headers=H_OP,
               json={"settings": {"checkin_enabled": "true"}})
check("运营改不了系统/经济设置 → 403", r.status_code == 403, f"HTTP {r.status_code} {r.text[:100]}")
check("403 说清缺什么角色", "超级管理员" in str(r.json().get("detail", "")), str(r.json()))
r = client.put("/api/admin/playback/policy", headers=H_OP, json={"policy": {"playback_max_bitrate_kbps": "4000"}})
check("运营改不了播放与客户端策略 → 403", r.status_code == 403, f"HTTP {r.status_code}")
check("运营改不了能力中心（上游 Key）→ 403",
      client.get("/api/admin/capabilities/proxy", headers=H_OP).status_code in (200, 404)
      and client.put("/api/admin/capabilities/proxy", headers=H_OP,
                     json={"config": {"target": "http://x"}}).status_code == 403)
check("运营不能管管理员（读也拦住？不——读放行、写拦住）",
      client.get("/api/admin/admins", headers=H_OP).status_code == 200
      and client.post("/api/admin/admins", headers=H_OP,
                      json={"username": "normal_user", "role": "viewer"}).status_code == 403)

# 切当前服是日常操作（顶栏那个切换器），运营必须能用
with SessionLocal() as db:
    realm = models.ServerRealm(slug="op-realm", name="运营测试服", is_active=True)
    db.add(realm)
    db.commit()
    realm_id = realm.id
r = client.post(f"/api/admin/realms/{realm_id}/activate", headers=H_OP)
check("运营仍然可以切当前服（作用域切换不算结构变更）", r.status_code == 200,
      f"HTTP {r.status_code} {r.text[:100]}")

# ==================== 4. 只读角色：GET 放行、写全拦 ====================
print("\n=== 4. 只读角色（viewer）===")
check("只读能看用户列表", client.get("/api/admin/users", headers=H_VIEW).status_code == 200)
check("只读能看管理员清单", client.get("/api/admin/admins", headers=H_VIEW).status_code == 200)
check("只读能看媒体库", client.get("/api/admin/emby/libraries", headers=H_VIEW).status_code == 200)
r = client.put(f"/api/admin/users/{target_id}", headers=H_VIEW, json={"is_active": True})
check("只读写用户 → 403", r.status_code == 403, f"HTTP {r.status_code}")
check("403 文案说明是只读角色", "只读" in str(r.json().get("detail", "")), str(r.json()))
check("只读在 emby 路由上也不能写（扫描要 403）",
      client.post("/api/admin/emby/mounts/health", headers=H_VIEW).status_code == 403)
check("只读也不能改自己的密码之外的任何东西（改密码是自助操作，另说）",
      client.put("/api/admin/realms/" + str(realm_id), headers=H_VIEW,
                 json={"name": "改名试试"}).status_code == 403)

# ==================== 5. 三条护栏 ====================
print("\n=== 5. 护栏 ===")
r = client.patch(f"/api/admin/admins/{owner_id}", headers=H_OWNER, json={"role": "viewer"})
check("不能改自己的角色（免得把自己关在门外）", r.status_code == 400, f"HTTP {r.status_code}")
check("不能撤销自己", client.request("DELETE", f"/api/admin/admins/{owner_id}",
                                     headers=H_OWNER).status_code == 400)
r = client.patch(f"/api/admin/admins/{owner_id}", headers=H_OP, json={"role": "viewer"})
check("运营也没法降级超级管理员（403 在角色判定就拦下了）", r.status_code == 403, f"HTTP {r.status_code}")

second_super = make_user("second_super", is_staff=True, role="super")
r = client.patch(f"/api/admin/admins/{second_super}", headers=H_OWNER, json={"role": "operator"})
check("有第二个超级管理员时可以降级", r.status_code == 200, f"HTTP {r.status_code} {r.text[:100]}")
with SessionLocal() as db:
    row = db.query(models.WebUser).filter(models.WebUser.id == second_super).first()
    check("降级后落库为 operator（不是只改内存）", row.admin_role == "operator", str(row.admin_role))
r = client.patch(f"/api/admin/admins/{owner_id}", headers=H_OWNER, json={"role": "viewer"})
check("仍然是最后一名超级管理员时，连自己也不许降级",
      r.status_code in (400, 403), f"HTTP {r.status_code}")
check("不能降级非管理员账号",
      client.patch(f"/api/admin/admins/{target_id}", headers=H_OWNER,
                   json={"role": "viewer"}).status_code == 400)

# ==================== 6. 撤销与审计 ====================
print("\n=== 6. 撤销与审计 ===")
r = client.request("DELETE", f"/api/admin/admins/{viewer_id}", headers=H_OWNER)
check("撤销管理员 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:100]}")
with SessionLocal() as db:
    row = db.query(models.WebUser).filter(models.WebUser.id == viewer_id).first()
    check("撤销后 is_staff 与角色都清空（账号本身还在）",
          row.is_staff is False and row.admin_role is None and row.username == "auditor")
check("撤销后该账号再也进不了后台",
      client.get("/api/admin/users", headers=H_VIEW).status_code == 403)
with SessionLocal() as db:
    actions = [log.action for log in db.query(models.AdminLog).all()]
check("授权 / 改角色 / 撤销都留了审计日志",
      {"admin_grant", "admin_update", "admin_revoke"} <= set(actions), str(sorted(set(actions))))

# ==================== 7. 角色工具函数口径 ====================
print("\n=== 7. 口径 ===")
check("normalize_role：空 / 未知 → viewer（S1 fail closed），super 必须显式写",
      (admin_roles.normalize_role(None), admin_roles.normalize_role(""),
       admin_roles.normalize_role("超级管理员"), admin_roles.normalize_role("super")) ==
      ("viewer", "viewer", "viewer", "super"))
check("normalize_role：大小写与空格收敛",
      admin_roles.normalize_role(" OPERATOR ") == "operator")
check("role_label 有中文标签", admin_roles.role_label("viewer") == "只读审计")
check("roles_payload 与 ROLES 一致",
      [r["value"] for r in admin_roles.roles_payload()] == list(admin_roles.ROLES))

# ==================== 8. 侧门：Emby 兼容面与通知接口 ====================
# 这些写入点原先各自判 ``is_staff``（不在 /api/admin/* 前缀下），只读角色能从这里绕道：
# 踢别人下线、触发全库重扫、向全站广播。它们现在都走同一处角色判定。
print("\n=== 8. 侧门（Emby 兼容面 / 通知接口）与登录响应 ===")
viewer2_id = make_user("auditor2", is_staff=True, role="viewer")
H_VIEW2 = headers(viewer2_id)

op_login = client.post("/api/admin/auth/login",
                       json={"username": "operator", "password": "pass12345"})
op_user = (op_login.json() or {}).get("user") or {}
check("登录响应直接带角色（不用先显示成「超级管理员」再等 /auth/me 纠正）",
      op_login.status_code == 200 and op_user.get("admin_role") == "operator"
      and op_user.get("is_super") is False and op_user.get("can_write") is True,
      str(op_user))
viewer_login = client.post("/api/admin/auth/login",
                           json={"username": "auditor2", "password": "pass12345"})
v_user = (viewer_login.json() or {}).get("user") or {}
check("只读登录响应里 can_write=False（界面据此置灰）",
      viewer_login.status_code == 200 and v_user.get("can_write") is False
      and v_user.get("is_super") is False, str(v_user))

# Emby 兼容面：触发全库重扫是写操作
# JWT 腿已删（#1290）：Emby 兼容面只认 Emby token，侧门用例改走
# AuthenticateByName 换 token；门户 JWT 本身应被挡在 401（第三项钉死）。
def emby_headers(username: str) -> dict:
    r = client.post("/emby/Users/AuthenticateByName",
                    json={"Username": username, "Pw": "pass12345"},
                    headers={"X-Emby-Authorization": 'MediaBrowser Client="Test", Device="Test", DeviceId="roles-smoke", Version="1"'})
    assert r.status_code == 200, r.text
    return {"X-Emby-Token": r.json()["AccessToken"]}

H_VIEW2_EMBY = emby_headers("auditor2")
H_OP_EMBY = emby_headers("operator")
check("只读触发全库重扫 → 403（不能从 Emby 接口绕道）",
      client.post("/Library/Refresh", headers=H_VIEW2_EMBY).status_code == 403,
      f"HTTP {client.post('/Library/Refresh', headers=H_VIEW2_EMBY).status_code}")
check("运营触发全库重扫 → 放行（这是日常运维动作）",
      client.post("/Library/Refresh", headers=H_OP_EMBY).status_code == 200)
check("门户 JWT 不再能进 Emby 兼容面（JWT 腿已删）",
      client.post("/Library/Refresh", headers=H_OP).status_code == 401)

# 踢别人的会话需要真实会话行；这里直接验角色判定入口（端点调的就是它）
fake_emby_delete = SimpleNamespace(url=SimpleNamespace(path="/emby/Sessions/abc"), method="DELETE")
with SessionLocal() as db:
    viewer_row = db.query(models.WebUser).filter(models.WebUser.id == viewer2_id).first()
    operator_row = db.query(models.WebUser).filter(models.WebUser.id == operator_id).first()
check("只读在 Emby 接口上踢人 / 释放全部转码会被角色判定拦下",
      admin_roles.blocked_reason(fake_emby_delete, viewer_row) is not None)
check("运营不受影响（踢人 / 重扫仍是他能做的日常事）",
      admin_roles.blocked_reason(fake_emby_delete, operator_row) is None)

# 通知接口：发送 / 广播是写操作
broadcast_body = {"channel": "system", "title": "t", "message": "m",
                  "notification_type": "system.announcement"}
check("只读广播全站通知 → 403（不能从这条侧门绕道）",
      client.post("/api/notifications/broadcast", headers=H_VIEW2,
                  json=broadcast_body).status_code == 403)
check("运营广播全站通知 → 放行",
      client.post("/api/notifications/broadcast", headers=H_OP,
                  json=broadcast_body).status_code == 200)
check("只读看在线用户（读操作不受限）",
      client.get("/api/notifications/online-users", headers=H_VIEW2).status_code == 200)

print()
if FAILED:
    print(f"❌ 管理员与权限冒烟失败 {len(FAILED)}/{TOTAL} 项：")
    for label in FAILED:
        print(f"   - {label}")
    sys.exit(1)
print(f"✅ 管理员与权限冒烟全部通过（{TOTAL} 项）")

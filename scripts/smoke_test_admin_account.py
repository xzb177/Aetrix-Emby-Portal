"""管理后台账号工具（`scripts/create_admin.py`）冒烟测试

覆盖：新建（指定密码 / 自动生成）、幂等升级、不改密码语义、重置密码、
不自动生成 Emby 用户名（给了密码则写入播放密码哈希）、真实登录 `/api/admin/auth/login` 与 `/api/admin/auth/me`、
非管理员被拒、非法用户名与过短密码被拒。

跑法：``python3 scripts/smoke_test_admin_account.py``（用临时库，不动开发库）
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "admin-account-smoke-secret-not-for-production"

DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from fastapi.testclient import TestClient  # noqa: E402

from backend.database import SessionLocal, init_db  # noqa: E402

init_db()

# 脚本不在包路径里，按文件路径加载，直接测它的 upsert_admin / 校验函数
_spec = importlib.util.spec_from_file_location("create_admin", ROOT / "scripts" / "create_admin.py")
create_admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(create_admin)

from backend import models  # noqa: E402
from backend.main import app  # noqa: E402

client = TestClient(app)
db = SessionLocal()
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def admin_login(username: str, password: str):
    return client.post("/api/admin/auth/login", json={"username": username, "password": password})


# ==================== 1. 新建管理员（指定密码）====================
PASSWORD = "BossPass!2345"
user, created, applied = create_admin.upsert_admin(db, "boss", password=PASSWORD)
check("新建管理员账号", created and applied == PASSWORD, f"created={created}")
check("新账号 is_staff / is_active", bool(user.is_staff) and bool(user.is_active))
check("不自动生成 Emby 用户名；给了密码则写入播放密码哈希",
      not user.emby_username and (user.emby_password or "").startswith(("$2", "$bcrypt-sha256$")),
      f"emby_username={user.emby_username!r}")

# ==================== 2. 真实登录管理后台 ====================
r = admin_login("boss", PASSWORD)
token = (r.json() or {}).get("access_token") if r.status_code == 200 else None
check("用新账号登录 /api/admin/auth/login", r.status_code == 200 and bool(token),
      f"HTTP {r.status_code}")

r = client.get("/api/admin/auth/me", headers={"Authorization": f"Bearer {token}"})
check("/api/admin/auth/me 返回同一账号",
      r.status_code == 200 and r.json().get("username") == "boss", f"HTTP {r.status_code}")

r = admin_login("boss", "wrong-password")
check("错误密码被拒", r.status_code == 401, f"HTTP {r.status_code}")

# ==================== 3. 幂等：不改密码时依然能登录 ====================
user2, created2, applied2 = create_admin.upsert_admin(db, "boss")
check("重复执行不新建账号", created2 is False and user2.id == user.id)
check("未给密码时不改密码", applied2 is None)
check("原密码仍然可用", admin_login("boss", PASSWORD).status_code == 200)

# ==================== 4. 把已注册的普通账号升级为管理员 ====================
r = client.post("/api/user/auth/register", json={"username": "member", "password": "member1234"})
check("先注册一个普通用户", r.status_code in (200, 201), f"HTTP {r.status_code}")
member_token = r.json()["access_token"]
r = client.get("/api/admin/auth/me", headers={"Authorization": f"Bearer {member_token}"})
check("普通用户访问管理端被拒", r.status_code == 403, f"HTTP {r.status_code}")

promoted, created3, applied3 = create_admin.upsert_admin(db, "member")
check("普通账号被升级为管理员",
      created3 is False and bool(promoted.is_staff) and applied3 is None)
check("升级后可用原密码登录后台", admin_login("member", "member1234").status_code == 200)

# ==================== 4b. 门户免登（SSO）：用户端 token 直接接管后台会话 ====================
# 后台前端在未持后台会话时会拿 localStorage 里的门户 access_token 调 /api/admin/auth/me。
# 这里是这条链的后端侧的硬保证：同一个 token、同一个人，登录过一次就不该再输一遍密码。
r = client.get("/api/admin/auth/me", headers={"Authorization": f"Bearer {member_token}"})
check("升级后，升级前的门户 token 直接免登后台",
      r.status_code == 200 and r.json().get("username") == "member", f"HTTP {r.status_code}")

r = client.post("/api/user/auth/login", json={"username": "member", "password": "member1234"})
fresh_portal = (r.json() or {}).get("access_token")
check("门户重新登录拿得到 token", r.status_code == 200 and bool(fresh_portal), f"HTTP {r.status_code}")
r = client.get("/api/admin/auth/me", headers={"Authorization": f"Bearer {fresh_portal}"})
check("门户重新登录的 token 同样免登后台", r.status_code == 200, f"HTTP {r.status_code}")

r = client.post("/api/user/auth/login", json={"username": "boss", "password": PASSWORD})
boss_portal = (r.json() or {}).get("access_token")
r = client.get("/api/admin/auth/me", headers={"Authorization": f"Bearer {boss_portal}"})
check("管理员从门户登录后也能免登后台",
      r.status_code == 200 and r.json().get("is_staff") is True, f"HTTP {r.status_code}")

r = client.get("/api/health/detailed")
check("免登链不影响公开健康检查", r.status_code == 200, f"HTTP {r.status_code}")

# ==================== 5. 重置密码 ====================
old_hash = db.query(models.WebUser).filter(models.WebUser.username == "boss").first().password_hash
user3, created4, applied4 = create_admin.upsert_admin(db, "boss", reset_password=True)
check("重置给出新的随机密码",
      applied4 is not None and applied4 != PASSWORD
      and len(applied4) >= create_admin.PASSWORD_RECOMMENDED_LENGTH,
      f"len={len(applied4)}")
check("重置后旧密码失效", admin_login("boss", PASSWORD).status_code == 401)
check("重置后新密码可登录", admin_login("boss", applied4).status_code == 200)
check("重置真的改了哈希",
      db.query(models.WebUser).filter(models.WebUser.username == "boss").first().password_hash
      != old_hash)

# ==================== 6. 入参校验（与门户注册同一口径）====================
for bad_username in ("ab", "带中文", "a" * 33, "has space"):
    try:
        create_admin.validate_username(bad_username)
        check(f"非法用户名被拒：{bad_username!r}", False)
    except create_admin.AdminAccountError:
        check(f"非法用户名被拒：{bad_username!r}", True)

try:
    create_admin.check_password_strength("abc")
    check("过短密码被拒", False)
except create_admin.AdminAccountError:
    check("过短密码被拒", True)

generated = create_admin.generate_password()
check("生成的密码含大小写 / 数字 / 符号",
      any(c.islower() for c in generated) and any(c.isupper() for c in generated)
      and any(c.isdigit() for c in generated)
      and any(c in "!@#$%^&*-_=+" for c in generated),
      f"len={len(generated)}")

try:
    create_admin.upsert_admin(db, "ghost_admin")
    check("不存在的账号且不给密码时报错", False)
except create_admin.AdminAccountError:
    check("不存在的账号且不给密码时报错", True)
check("报错时不留下半个账号",
      db.query(models.WebUser).filter(models.WebUser.username == "ghost_admin").first() is None)

# ==================== 收尾 ====================
db.close()
os.remove(DB)

print("\n" + "=" * 50)
if failures:
    print(f"❌ 管理后台账号冒烟测试失败 {len(failures)} 项: {failures}")
    raise SystemExit(1)
print("✅ 管理后台账号冒烟测试全部通过")

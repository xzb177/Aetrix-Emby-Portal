#!/usr/bin/env python3
"""管理后台写操作审计冒烟测试（v2.30.0）

**要钉住的问题**：媒体与交付域的端点挂在 ``/api/admin/emby/*`` 上，走的是另一套鉴权依赖
（``require_staff``），长期**一条审计都不写**。删掉一个十万条的媒体库、删掉已入库的条目、
删掉 115 账号、停掉全站转码，在「系统与审计 → 操作日志」里都查不到是谁干的。

现在由 ``backend/emby_server/audit.py`` 的中间件统一记录。这条护栏盯的是「中间件真的在记」，
以及三条容易做错的口径：

1. **只记成功的写操作**：``POST/PUT/PATCH/DELETE`` 且响应 < 400。被拒（403 / 400 / 404）
   的请求不应留下「已经做过」的记录——审计里出现失败操作等于误导排查。
2. **不记匿名管理员**：解析不出 Bearer 里的管理员就跳过（那种请求本来就会被鉴权依赖拒掉）。
3. **绝不记请求体**：115 账号 Cookie、挂载凭据都在请求体里，抄进日志等于把密钥又存了一份。
   这里真的建一个带 Cookie 的 115 账号，然后断言那条 Cookie **没有**出现在任何审计记录里。

顺带钉住「端点覆盖」这件结构性事实：把应用里所有 ``/api/admin/emby`` 的写端点枚举一遍，
每个都必须能被 ``lookup()`` 解析出动作名（具体动作或回落动作），否则新增端点就会静默丢失审计。

**另一半是中间件管不到的**：``/api/admin/*`` 的其它域靠端点自己调 ``_audit(...)``，
而「记得」不是一种机制。第 9 节就盯这条容易漏的路——``POST /api/admin/tickets/{id}/close``
关单并推送用户却一条审计都不写（两个兄弟端点都写了）。这条路的**静态**覆盖由
``scripts/check_admin_audit_coverage.py`` 守，本文件证明它**真的落库**（含重复关单要能看出
是谁又点了一次、能力测试的 payload 不得进日志）。

用法：python scripts/smoke_test_admin_audit.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"

from fastapi.testclient import TestClient  # noqa: E402

from backend import models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import audit  # noqa: E402
from backend.emby_server import portal_mount_routes  # noqa: E402,F401 — 挂载端点注册到同一个 router
from backend.emby_server.portal import admin_emby_router  # noqa: E402
from backend.api.emby_servers import router as emby_servers_router  # noqa: E402
from backend.main import app  # noqa: E402
from backend.security import create_access_token, hash_password  # noqa: E402

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
        user = models.WebUser(username=username, password_hash=hash_password("pass12345"),
                              is_active=is_active, is_staff=is_staff, admin_role=role)
        db.add(user)
        db.commit()
        return user.id


def headers(user_id: int) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def admin_logs() -> list[dict]:
    """当前全部操作日志（按 id 升序，带我们要断言的那几列）"""
    with SessionLocal() as db:
        rows = db.query(models.AdminLog).order_by(models.AdminLog.id).all()
        return [
            {"id": r.id, "admin_user_id": r.admin_user_id, "action": r.action,
             "target_type": r.target_type, "target_id": r.target_id,
             "details": r.details or {}, "ip": r.ip_address}
            for r in rows
        ]


def latest() -> dict | None:
    rows = admin_logs()
    return rows[-1] if rows else None


# ==================== 0. 路径归一与动作映射（纯逻辑）====================
print("=== 0. 路径模板与动作映射 ===")

check("id 段收敛成 {}",
      audit.normalize_path("/api/admin/emby/libraries/42") == "/libraries/{}",
      audit.normalize_path("/api/admin/emby/libraries/42"))
check("查询串不参与模板",
      audit.normalize_path("/api/admin/emby/libraries?realm=2") == "/libraries")
check("媒体库 id → 删媒体库",
      audit.lookup("DELETE", "/api/admin/emby/libraries/42") == ("emby_delete_library", "library"))
check("条目 guid → 删条目",
      audit.lookup("DELETE", "/api/admin/emby/items/a1b2c3d4e5f60718") ==
      ("emby_delete_item", "media_item"))
check("扫描队列取消",
      audit.lookup("DELETE", "/api/admin/emby/scan-queue/7") == ("emby_scan_cancel", "library"))
# 关键：`virtual` 不是 id，不能被换成 {}，否则「生成虚拟库」会被记成「改了某个库」
check("libraries/virtual 不被当成库 id",
      audit.lookup("POST", "/api/admin/emby/libraries/virtual") ==
      ("emby_generate_virtual_libraries", "library"))
check("未列出的写端点回落但仍带目标类型",
      audit.lookup("PUT", "/api/admin/emby/servers") == (audit.FALLBACK_ACTION, "servers"),
      str(audit.lookup("PUT", "/api/admin/emby/servers")))
# `115` 是字面量命名空间（115 网盘），不是 id：把它当成 id 会让下面这些动作名全部失效
check("115 不是 id（全数字段也可能是字面量）",
      audit.normalize_path("/api/admin/emby/115/accounts") == "/115/accounts",
      audit.normalize_path("/api/admin/emby/115/accounts"))
check("建 115 账号有专属动作名",
      audit.lookup("POST", "/api/admin/emby/115/accounts") ==
      ("emby_create_pan115", "pan115_account"),
      str(audit.lookup("POST", "/api/admin/emby/115/accounts")))
check("删 115 账号有专属动作名",
      audit.lookup("DELETE", "/api/admin/emby/115/accounts/3") ==
      ("emby_delete_pan115", "pan115_account"),
      str(audit.lookup("DELETE", "/api/admin/emby/115/accounts/3")))
check("验 115 账号有专属动作名",
      audit.lookup("POST", "/api/admin/emby/115/accounts/3/verify") ==
      ("emby_verify_pan115", "pan115_account"),
      str(audit.lookup("POST", "/api/admin/emby/115/accounts/3/verify")))
# 子动作不能被记成父动作的名字：测试一个还没保存的挂载 ≠ 创建了一个挂载
check("测试挂载不会被记成「创建挂载」",
      audit.lookup("POST", "/api/admin/emby/mounts/test") == (audit.FALLBACK_ACTION, "mounts"),
      str(audit.lookup("POST", "/api/admin/emby/mounts/test")))
check("挂载体检不会被记成「创建挂载」",
      audit.lookup("POST", "/api/admin/emby/mounts/health") == (audit.FALLBACK_ACTION, "mounts"),
      str(audit.lookup("POST", "/api/admin/emby/mounts/health")))

# ==================== 1. 端点覆盖：每个写端点都能解析出动作 ====================
print("\n=== 1. 端点覆盖（新增写端点不会静默丢失审计）===")

write_routes: list[tuple[str, str]] = []
for router in (admin_emby_router, emby_servers_router):
    for route in router.routes:
        path = getattr(route, "path", "")
        if not path.startswith(audit.PREFIX):
            continue
        for method in getattr(route, "methods", None) or ():
            if method in audit.WRITE_METHODS:
                write_routes.append((method, path))

write_routes = sorted(set(write_routes))
check("枚举到了写端点（不是空跑）", len(write_routes) >= 15, f"{len(write_routes)} 个")


# 路由里的 ``{lib_id}`` 是 FastAPI 的占位符，真实流量里是具体的 id；
# 两者都要能被 ``lookup`` 解析出同一个动作名。
def concrete(path: str) -> str:
    """把 ``{lib_id}`` 替换成一个长得像的标本值，模拟真实请求路径

    会话键用真实形状（``s`` + base64url），因为 ``secrets.token_urlsafe`` 会产生
    ``-`` / ``_`` / 大写字母——只按小写字母与数字识别的正则会把它们当成普通段。
    """
    return (path.replace("{session_key}", "sAb3-_xYz9Qk2LmNo")
                .replace("{item_id}", "a1b2c3d4e5f60718a1b2c3d4e5f60718")
                .replace("{lib_id}", "42").replace("{mount_id}", "7")
                .replace("{account_id}", "3"))


def templated(path: str) -> str:
    """把 ``{lib_id}`` 收敛成 ``{}``（与 ACTION_TABLE 的写法一致）"""
    return re.sub(r"\{[^}]*\}", "{}", path)


unmapped, drifted, named = [], [], []
for method, path in write_routes:
    real = concrete(path)
    action, target_type = audit.lookup(method, real)
    if not action or not target_type:
        unmapped.append(f"{method} {real}")
    # 真实请求（带具体 id）与路由模板（占位符）必须落到同一个动作名
    if audit.lookup(method, real) != audit.lookup(method, templated(path)):
        drifted.append(f"{method} {path}")
    if action != audit.FALLBACK_ACTION:
        named.append(f"{method} {real}")

check("每个写端点都解析出（动作名, 目标类型）", not unmapped, str(unmapped))
check("带具体 id 与带占位符解析结果一致", not drifted, str(drifted))
# 不是说 100%（没写进 ACTION_TABLE 的回落是对的），但不能只剩回落
check("多数写端点有语义化动作名（不是全靠回落）", len(named) >= 12,
      f"{len(named)}/{len(write_routes)}")

# 映射表里不该有死条目：ACTION_TABLE 的每条都必须能被某个真实端点命中，
# 否则那条规则写了等于没写（`/115/*` 曾因为「是数字就当 id」而全部变成死条目）
styles = {audit.normalize_path(templated(p)) for _, p in write_routes}
dead = [(m, t) for (m, t) in audit.ACTION_TABLE if t not in styles]
check("ACTION_TABLE 没有死条目（每条都对应一个真实端点）", not dead, str(dead))
# 反过来：ACTION_TABLE 里每条规则的路径模板都必须真的能收敛到它自己
bad_norm = [(m, t) for (m, t) in audit.ACTION_TABLE if audit.normalize_path(t) != t]
check("ACTION_TABLE 的模板自身可复现（normalize 不会改写字面量）", not bad_norm, str(bad_norm))

# ==================== 2. 读请求不留审计 ====================
print("\n=== 2. 读请求不写审计 ===")

admin_id = make_user("audit_admin", is_staff=True, role="super")
viewer_id = make_user("audit_viewer", is_staff=True, role="viewer")
plain_id = make_user("audit_user")
H_ADMIN, H_VIEWER, H_PLAIN = headers(admin_id), headers(viewer_id), headers(plain_id)

library_dir = tempfile.mkdtemp(prefix="aetrix-audit-lib-")

r = client.get("/api/admin/emby/libraries", headers=H_ADMIN)
check("读媒体库列表 → 200", r.status_code == 200, f"HTTP {r.status_code}")
check("读请求不产生审计记录", admin_logs() == [], f"{len(admin_logs())} 条")

# ==================== 3. 成功的写请求留审计 ====================
print("\n=== 3. 成功的写操作落进操作日志 ===")

r = client.post("/api/admin/emby/libraries", headers=H_ADMIN,
                json={"name": "审计测试库", "collection_type": "movies",
                      "paths": [library_dir]})
check("新建媒体库 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")
lib_id = r.json().get("id")
entry = latest()
check("留下审计记录", entry is not None and entry["action"] == "emby_create_library",
      str(entry and entry["action"]))
check("审计记到「谁」头上（admin_user_id 是发起人）",
      bool(entry) and entry["admin_user_id"] == admin_id,
      f"{entry and entry['admin_user_id']} vs {admin_id}")
check("审计带目标类型与来源 IP",
      bool(entry) and entry["target_type"] == "library" and bool(entry["ip"]),
      f"{entry and entry['target_type']} / {entry and entry['ip']}")
check("details 只记方法、路径模板与来源（不记全文）",
      bool(entry) and set(entry["details"]) <= {"method", "path", "client_ip", "admin_user_id"}
      and entry["details"].get("method") == "POST"
      and entry["details"].get("path") == "/libraries",
      str(entry and entry["details"]))

r = client.put(f"/api/admin/emby/libraries/{lib_id}", headers=H_ADMIN,
               json={"name": "审计测试库（改名）"})
check("改媒体库 → 200", r.status_code == 200, f"HTTP {r.status_code}")
check("动作名区分「改」与「建」",
      (latest() or {}).get("action") == "emby_update_library", str((latest() or {}).get("action")))

r = client.post("/api/admin/emby/transcodes/stop-all", headers=H_ADMIN)
check("停全站转码（转码已删除，兼容桩）→ 200", r.status_code == 200, f"HTTP {r.status_code}")
check("停转码也留下审计（以前查不到是谁停的）",
      (latest() or {}).get("action") == "emby_stop_all_transcodes",
      str((latest() or {}).get("action")))

# ==================== 4. 请求体里的密钥绝不进审计 ====================
print("\n=== 4. 请求体（Cookie 等密钥）不进审计 ===")

SECRET_COOKIE = "UID=aetrix_audit_probe; CID=deadbeefcafe1234; SEID=zzz9999"
r = client.post("/api/admin/emby/115/accounts", headers=H_ADMIN,
                json={"name": "审计探针账号", "cookie": SECRET_COOKIE})
check("新建 115 账号 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")
check("115 账号的写操作也被审计",
      (latest() or {}).get("action") == "emby_create_pan115", str((latest() or {}).get("action")))
leaked = [row for row in admin_logs() if SECRET_COOKIE in str(row["details"])]
check("Cookie 没有出现在任何审计记录里（不把密钥又存一份）", not leaked,
      f"{len(leaked)} 条泄露")

# ==================== 5. 失败与被拒的写请求不留记录 ====================
print("\n=== 5. 失败 / 被拒的写操作不写审计 ===")

before = len(admin_logs())

r = client.post("/api/admin/emby/libraries", headers=H_ADMIN,
                json={"name": "没有来源的库", "collection_type": "movies"})
check("少来源的建库 → 400", r.status_code == 400, f"HTTP {r.status_code}")
check("被拒的请求没有留下「已经做过」的记录", len(admin_logs()) == before,
      f"{len(admin_logs())} vs {before}")

r = client.delete("/api/admin/emby/libraries/999999", headers=H_ADMIN)
check("删不存在的库 → 404", r.status_code == 404, f"HTTP {r.status_code}")
check("404 同样不写审计", len(admin_logs()) == before, f"{len(admin_logs())} vs {before}")

r = client.put(f"/api/admin/emby/libraries/{lib_id}", headers=H_ADMIN,
               json={"paths": [os.path.join(library_dir, "不存在")]})
check("改库指向不存在的路径 → 400", r.status_code == 400, f"HTTP {r.status_code}")
check("400 同样不写审计", len(admin_logs()) == before, f"{len(admin_logs())} vs {before}")

# ==================== 6. 没有管理员身份就不该有「匿名管理员」 ====================
print("\n=== 6. 匿名 / 非管理员 / 只读角色的写操作 ===")

r = client.post("/api/admin/emby/libraries", json={"name": "匿名", "paths": [library_dir]})
check("匿名写 → 401/403", r.status_code in (401, 403), f"HTTP {r.status_code}")

r = client.post("/api/admin/emby/libraries", headers=H_PLAIN,
                json={"name": "普通用户", "paths": [library_dir]})
check("普通用户写 → 403", r.status_code == 403, f"HTTP {r.status_code}")

r = client.post("/api/admin/emby/libraries", headers={"Authorization": "Bearer not-a-real-token"},
                json={"name": "坏令牌", "paths": [library_dir]})
check("坏令牌写 → 401/403", r.status_code in (401, 403), f"HTTP {r.status_code}")

r = client.post("/api/admin/emby/transcodes/stop-all", headers=H_VIEWER)
check("只读管理员（viewer）写 → 403", r.status_code == 403, f"HTTP {r.status_code}")

check("以上全部没有写审计（审计里不该出现「匿名管理员」）", len(admin_logs()) == before,
      f"{len(admin_logs())} vs {before}")
check("所有审计记录都指向一个真实管理员",
      all(row["admin_user_id"] for row in admin_logs()),
      str([row["admin_user_id"] for row in admin_logs()]))

# ==================== 7. 其它前缀的写请求不归这条中间件管 ====================
print("\n=== 7. 只管媒体与交付域（前缀）===")

before = len(admin_logs())
client.post("/api/user/auth/login", json={"username": "audit_user", "password": "wrong"})
check("门户登录失败（非 /api/admin/emby 前缀）不落这条审计",
      len(admin_logs()) == before, f"{len(admin_logs())} vs {before}")

# ==================== 8. 审计不影响操作本身 ====================
print("\n=== 8. 审计是旁观者：操作结果不变 ===")

r = client.delete(f"/api/admin/emby/libraries/{lib_id}", headers=H_ADMIN)
check("删媒体库仍然成功 → 200", r.status_code == 200, f"HTTP {r.status_code}")
check("删除动作同样有审计",
      (latest() or {}).get("action") == "emby_delete_library", str((latest() or {}).get("action")))
with SessionLocal() as db:
    gone = db.query(models.AdminLog).filter(
        models.AdminLog.action == "emby_delete_library").count()
check("审计落库了（不是只打了日志）", gone == 1, f"{gone} 条")

# ==================== 9. 非 emby 域：端点自己写 _audit 的那条路 ====================
# 中间件只管 `/api/admin/emby/*`；其它管理域靠端点自己调用 `_audit(...)`。
# 这一节盯住的就是那条容易漏的路：`POST /api/admin/tickets/{id}/close` 关单并推送用户，
# 此前一条审计都不写（两个兄弟端点都写了）——用户收到通知，运营查不到是谁关的。
# 静态覆盖由 `scripts/check_admin_audit_coverage.py` 守，这里证明**真的落库**。
print("\n=== 9. 其它管理域（手动 _audit）===")

with SessionLocal() as db:
    ticket = models.Ticket(user_id=plain_id, title="审计测试工单", status="open")
    db.add(ticket)
    db.commit()
    ticket_id = ticket.id

r = client.post(f"/api/admin/tickets/{ticket_id}/close", headers=H_ADMIN)
check("关工单 → 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:100]}")
row = latest()
check("关工单留下审计（此前查不到是谁关的）",
      (row or {}).get("action") == "close_ticket" and (row or {}).get("target_type") == "ticket",
      str(row and (row["action"], row["target_type"])))
check("审计指向关单的人与那张工单",
      bool(row) and row["admin_user_id"] == admin_id and row["target_id"] == ticket_id,
      str(row and (row["admin_user_id"], row["target_id"])))
check("首次关单标记 already_closed=False",
      (row or {}).get("details", {}).get("already_closed") is False,
      str(row and row["details"]))

r = client.post(f"/api/admin/tickets/{ticket_id}/close", headers=H_ADMIN)
check("重复关单也留审计（能看出是谁又点了一次）",
      (latest() or {}).get("action") == "close_ticket", str((latest() or {}).get("action")))
check("重复关单标记 already_closed=True",
      (latest() or {}).get("details", {}).get("already_closed") is True,
      str((latest() or {}).get("details")))

# 能力测试不改配置，但**真的会发出去东西**（邮件 / Telegram 真实投递），
# 所以同样留审计，但只记能力名与结果——payload 里可能带收件人与凭据。
PROBE_VALUE = "aetrix-audit-probe-value"
r = client.post("/api/admin/capabilities/proxy/test", headers=H_ADMIN,
                json={"payload": {"probe_secret": PROBE_VALUE}})
check("能力测试 → 200（未配置时如实报未启用）", r.status_code == 200,
      f"HTTP {r.status_code} {r.text[:120]}")
row = latest()
check("能力测试留下审计（真实投递要能在日志里对上人）",
      (row or {}).get("action") == "capability_test"
      and (row or {}).get("details", {}).get("slug") == "proxy",
      str(row))
check("能力测试的审计不记 payload（收件人 / 凭据不外泄）",
      PROBE_VALUE not in str(row and row["details"])
      and "probe_secret" not in str(row and row["details"]),
      str(row and row["details"]))

# 静态覆盖护栏与这条行为验证是同一件事的两面：一个端点既不写 _audit 也不在中间件前缀下，
# 静态检查就会红。这里顺手把它跑一遍，确保两边的口径没有分叉。
import subprocess  # noqa: E402

coverage = subprocess.run(
    [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "check_admin_audit_coverage.py")],
    capture_output=True, text=True)
check("静态覆盖护栏与本文件口径一致（写端点都留审计）", coverage.returncode == 0,
      (coverage.stdout or coverage.stderr).strip().splitlines()[-1][:120])

print("\n" + "=" * 60)
print(f"通过 {TOTAL - len(FAILED)}/{TOTAL} 项")
if FAILED:
    print("失败项:")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("🎉 管理后台写操作审计冒烟测试全部通过")

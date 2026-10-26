"""公益服冒烟测试（v2.7.0）

要解决的问题：一个面板要同时运营**付费服**（卖会员）和**公益服**（免费开放）。
这一版要证明的不是「新建模板里能选一个下拉」，而是下面这些口径真的成立：

1. **升级行为不变**：没有配过接入方式的服一律按付费服处理——非会员播放仍然 403；
2. **公益服免费开放**：切到公益服后，同一个没有订阅的账号能拿 PlaybackInfo（200），
   用户端 `subscription_required=false`、`is_free_realm=true`，账号卡与套餐接口
   都下发公益口径（文案 + 不需要买会员）；
3. **按服判定，不是把付费墙全局关了**：切回付费服，同一个账号又回到 403；
4. **资源保护默认生效**：公益服默认禁止下载（下载接口 403，且提示是「公益服只提供
   在线观看」），管理端可以按服覆盖成允许；付费服的下载策略仍然跟随全局开关；
5. **面板口径一致**：服清单汇总给出公益服/付费服的服数，概览页每个服带 access_mode。

本脚本自己起一套临时 SQLite 库，走真 HTTP 接口（不是直接调函数）。
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
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(WORK, 'free-realm.db')}"

from fastapi.testclient import TestClient  # noqa: E402

from backend import models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.auth import hash_emby_token
from backend.security import hash_password  # noqa: E402

init_db()

from backend.main import app  # noqa: E402

client = TestClient(app)

failures: list[str] = []
checks = 0

FREE_NOTE = "公益服规则：免费开放，账号仅限本人使用，禁止下载与转卖。"


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ==================== 播种：一个可播放条目 + 一个管理员 ====================

MEDIA = os.path.join(WORK, "playable.mp4")
with open(MEDIA, "wb") as f:
    f.write(b"\x00\x00\x00\x18ftypmp42" + b"payload-" * 512)

with SessionLocal() as db:
    lib = em.Library(guid="free-lib", name="公益库", collection_type="movies", paths=WORK)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    item = em.MediaItem(
        guid="c" * 32, library_id=lib.id, item_type="movie", name="公益片",
        duration_ticks=600_000_000, file_path=MEDIA, container="mp4",
    )
    db.add(item)
    db.commit()

ITEM = "c" * 32

r = client.post("/api/user/auth/register",
                json={"username": "freeviewer", "password": "freepass123"})
check("注册普通用户成功", r.status_code == 201, f"status={r.status_code}")
USER_H = {"Authorization": f"Bearer {r.json()['access_token']}"}
# #1290 后 /emby/* 不再接受门户 JWT：本脚本的 /emby/* 走 Emby 令牌，
# /api/* 仍走门户 JWT。
_user_id = r.json()["user"]["id"]
with SessionLocal() as _db:
    _raw = "f" * 40
    _db.add(em.EmbyApiToken(token=hash_emby_token(_raw), user_id=_user_id,
                            device_id="smoke-test", is_revoked=False))
    # 播放门禁（2026-10-26 硬规矩）：公益服免的是订阅、不是绑定——
    # “公益服下非会员也能拿 PlaybackInfo”项须先有 active 绑定。
    _cred = em.MountCredential(mount_id=None, scope="user", owner_user_id=_user_id,
                               sealed_credential="", key_id="fernet-v1",
                               status="active")
    _db.add(_cred)
    _db.flush()
    _db.add(em.UserStorageBinding(user_id=_user_id, provider="drive",
                                  account_label="冒烟盘", credential_id=_cred.id,
                                  status="active"))
    _db.commit()
USER_E = {"X-Emby-Token": _raw}
check("新账号不是会员（没有任何订阅）", r.json()["user"]["is_vip"] is False)

with SessionLocal() as db:
    staff = models.WebUser(username="free_realm_staff", password_hash=hash_password("staffpass123"),
                           is_staff=True, admin_role="super", is_active=True)
    db.add(staff)
    db.commit()

r = client.post("/api/user/auth/login", json={"username": "free_realm_staff", "password": "staffpass123"})
check("管理员登录成功", r.status_code == 200, f"status={r.status_code}")
ADMIN_H = {"Authorization": f"Bearer {r.json()['access_token']}"}

# ==================== 1. 升级行为不变：默认服 = 付费服 ====================

print("\n--- 默认服（付费服）：与升级前一致 ---")

r = client.get("/api/admin/realms", headers=ADMIN_H)
default_realm = (r.json()["realms"] or [{}])[0]
check("新建的服默认是付费服（老库升级后的口径）",
      default_realm.get("access_mode") == "paid" and default_realm.get("is_free") is False,
      str({k: default_realm.get(k) for k in ("access_mode", "is_free")}))
check("付费服不带公益文案", not default_realm.get("access_note"), repr(default_realm.get("access_note")))
default_id = default_realm.get("id")

r = client.get("/api/user/auth/me", headers=USER_H)
check("付费服下 subscription_required=true", r.json().get("subscription_required") is True, str(r.json()))
check("付费服下 is_free_realm=false", r.json().get("is_free_realm") is False, str(r.json()))

r = client.post(f"/emby/Items/{ITEM}/PlaybackInfo", json={}, headers=USER_E)
check("付费服下非会员播放被拦截 403（付费墙照旧）", r.status_code == 403, f"status={r.status_code}")

# ==================== 2. 建公益服并切过去 ====================

print("\n--- 公益服：免费开放 ---")

r = client.post("/api/admin/realms", headers=ADMIN_H, json={
    "name": "公益服", "slug": "charity", "url": "https://free.example.com/",
    "access_mode": "free", "access_note": FREE_NOTE,
})
body = r.json() if r.status_code == 200 else {}
free_realm = body.get("realm", {})
FREE_ID = free_realm.get("id")
check("建公益服成功", r.status_code == 200 and bool(FREE_ID), f"status={r.status_code}")
check("公益服回读 access_mode=free / is_free=true",
      free_realm.get("access_mode") == "free" and free_realm.get("is_free") is True,
      str({k: free_realm.get(k) for k in ("access_mode", "is_free")}))
check("公益服回读自定义规则文案", free_realm.get("access_note") == FREE_NOTE, repr(free_realm.get("access_note")))
check("下载策略默认跟随（公益服由此得到“禁止下载”）",
      free_realm.get("allow_download") is None, repr(free_realm.get("allow_download")))

r = client.post(f"/api/admin/realms/{FREE_ID}/activate", headers=ADMIN_H)
check("切换当前服到公益服", r.status_code == 200 and r.json().get("active_realm_id") == FREE_ID,
      f"status={r.status_code}")

r = client.get("/api/user/auth/me", headers=USER_H)
me = r.json()
check("公益服下 subscription_required=false（不再宣传付费墙）",
      me.get("subscription_required") is False, str(me.get("subscription_required")))
check("公益服下 is_free_realm=true 且带规则文案",
      me.get("is_free_realm") is True and me.get("realm_access_note") == FREE_NOTE,
      str({k: me.get(k) for k in ("is_free_realm", "realm_access_note")}))
check("公益服下 download_allowed=false（资源保护默认生效）",
      me.get("download_allowed") is False, str(me.get("download_allowed")))

r = client.post(f"/emby/Items/{ITEM}/PlaybackInfo", json={}, headers=USER_E)
check("公益服下非会员也能拿 PlaybackInfo（200）", r.status_code == 200, f"status={r.status_code}")
check("公益服播放信息带可播放来源",
      bool((r.json() if r.status_code == 200 else {}).get("MediaSources")), str(r.status_code))

r = client.get(f"/emby/Items/{ITEM}/Download", headers=USER_E)
check("公益服下载被拦 403", r.status_code == 403, f"status={r.status_code}")
check("公益服下载提示是公益口径（只提供在线观看）",
      r.status_code == 403 and "在线观看" in str(r.json().get("detail", "")), str(r.json()))

# 账号卡：公益服的地址与口径照样下发（哪怕这个账号没有订阅）
r = client.get("/api/user/emby/server", headers=USER_E)
card = r.json() if r.status_code == 200 else {}
check("账号卡顶层是公益口径",
      card.get("access_mode") == "free" and card.get("is_free") is True, str(card.get("access_mode")))
check("账号卡带规则文案", card.get("access_note") == FREE_NOTE, repr(card.get("access_note")))
check("账号卡里的下载开关也是关闭的", card.get("allow_download") is False, str(card.get("allow_download")))
free_card = next((c for c in card.get("realms", []) if c.get("id") == FREE_ID), None)
check("公益服没订阅也会出现在「我的服」里（免费开放是承诺）",
      free_card is not None and free_card.get("is_free") is True and free_card.get("subscribed") is False,
      str(free_card))

# 套餐接口：公益服不推购买
r = client.get("/api/user/economy/payment/plans", headers=USER_H)
plans_body = r.json() if r.status_code == 200 else {}
check("套餐接口下发 is_free=true（前端据此换掉购买引导）",
      plans_body.get("is_free") is True and plans_body.get("access_mode") == "free",
      str({k: plans_body.get(k) for k in ("is_free", "access_mode")}))
check("套餐接口带公益规则文案", plans_body.get("access_note") == FREE_NOTE,
      repr(plans_body.get("access_note")))

# ==================== 3. 按服判定，不是全局关掉付费墙 ====================

print("\n--- 按服判定：切回付费服，付费墙立刻回来 ---")

r = client.post(f"/api/admin/realms/{default_id}/activate", headers=ADMIN_H)
check("切回默认服（付费服）", r.status_code == 200 and r.json().get("active_realm_id") == default_id,
      f"status={r.status_code}")

r = client.get("/api/user/auth/me", headers=USER_H)
check("付费服下 subscription_required 又变回 true",
      r.json().get("subscription_required") is True, str(r.json().get("subscription_required")))
r = client.post(f"/emby/Items/{ITEM}/PlaybackInfo", json={}, headers=USER_E)
check("付费服下非会员播放重新被拦 403（同一账号、同一台机器）",
      r.status_code == 403, f"status={r.status_code}")
r = client.get(f"/emby/Items/{ITEM}/Download", headers=USER_E)
# 下载与播放同一门槛：付费服下非会员仍然先撞付费墙（而不是撞到公益服那条下载策略）
check("付费服下载先撞付费墙（提示是要开会员，不是公益服口径）",
      r.status_code == 403 and "会员" in str(r.json().get("detail", "")), str(r.json()))

with SessionLocal() as db:
    from backend import subscriptions

    # 策略本身与付费墙是两件事：付费服的下载策略仍跟随全局开关
    check("付费服的下载策略仍跟随全局（默认允许，不受公益服影响）",
          subscriptions.download_allowed(db, default_id) is True)

# ==================== 4. 管理端：下载策略可按服覆盖 ====================

print("\n--- 管理端：公益服的下载策略可覆盖 ---")

r = client.put(f"/api/admin/realms/{FREE_ID}", headers=ADMIN_H, json={"download_policy": "allow"})
check("公益服改成允许下载", r.status_code == 200 and r.json()["realm"].get("allow_download") is True,
      f"status={r.status_code}")

client.post(f"/api/admin/realms/{FREE_ID}/activate", headers=ADMIN_H)
r = client.get(f"/emby/Items/{ITEM}/Download", headers=USER_E)
check("策略改成允许后，公益服下载放行 200", r.status_code == 200, f"status={r.status_code}")

r = client.put(f"/api/admin/realms/{FREE_ID}", headers=ADMIN_H, json={"download_policy": "follow"})
check("改回跟随（公益服默认禁止下载）",
      r.status_code == 200 and r.json()["realm"].get("allow_download") is None, f"status={r.status_code}")
r = client.get(f"/emby/Items/{ITEM}/Download", headers=USER_E)
check("改回跟随后下载再次被拦 403", r.status_code == 403, f"status={r.status_code}")

# 接入方式可以来回切换：改回付费服后不再免费开放
r = client.put(f"/api/admin/realms/{FREE_ID}", headers=ADMIN_H, json={"access_mode": "paid"})
check("公益服可以改回付费服", r.status_code == 200 and r.json()["realm"].get("is_free") is False,
      f"status={r.status_code}")
r = client.post(f"/emby/Items/{ITEM}/PlaybackInfo", json={}, headers=USER_E)
check("改回付费服后非会员播放被拦 403", r.status_code == 403, f"status={r.status_code}")
r = client.put(f"/api/admin/realms/{FREE_ID}", headers=ADMIN_H, json={"access_mode": "free"})
check("再切回公益服", r.status_code == 200 and r.json()["realm"].get("is_free") is True,
      f"status={r.status_code}")

# ==================== 5. 面板口径：汇总与概览 ====================

print("\n--- 面板口径：公益服/付费服分得清 ---")

r = client.get("/api/admin/realms", headers=ADMIN_H)
body = r.json()
check("汇总里给出公益服与付费服的服数",
      body["summary"].get("free_realms") == 1 and body["summary"].get("paid_realms") == 1,
      str({k: body["summary"].get(k) for k in ("free_realms", "paid_realms")}))

r = client.get("/api/admin/realms/overview", headers=ADMIN_H)
rows = {row["id"]: row for row in r.json().get("realms", [])}
check("概览页每个服带 access_mode（哪个是公益服一眼可见）",
      rows.get(FREE_ID, {}).get("access_mode") == "free" and rows.get(default_id, {}).get("access_mode") == "paid",
      str({k: v.get("access_mode") for k, v in rows.items()}))

# ==================== 6. 跨服不变式：会员仍然按服判定 ====================

print("\n--- 不变式：公益服不会让别的服变成免费 ---")

with SessionLocal() as db:
    from backend import subscriptions

    check("付费服：无订阅的账号不能播放",
          subscriptions.can_play(db, db.query(models.WebUser).filter(
              models.WebUser.username == "freeviewer").first(), default_id) is False)
    check("公益服：同一账号可以直接播放",
          subscriptions.can_play(db, db.query(models.WebUser).filter(
              models.WebUser.username == "freeviewer").first(), FREE_ID) is True)
    check("公益服的下载默认禁止（按服判定）",
          subscriptions.download_allowed(db, FREE_ID) is False)
    check("付费服的下载仍跟随全局（默认允许）",
          subscriptions.download_allowed(db, default_id) is True)

# ==================== 汇总 ====================

print()
if failures:
    print(f"FAILED  {len(failures)}/{checks}：")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL PASS  {checks}/{checks}")

"""运营能力冒烟测试（v2.6.0）

借鉴 twilight-kotomi 的核心运营能力，覆盖：
- 卡码体系：注册码 / 续期码 / 白名单码 / 诱饵码 / 指名码 的生成、预览、核销、停用与回收
- 卡码统计与列表筛选
- 设备风控：设备登记、设备上限（拒绝 / 自动踢最久未用）、封禁、移除（吊销令牌）
- 登录与安全日志：登录成功/失败、设备超限、诱饵码触发，以及按保留期清理
- 下载策略：站点关闭下载时 /Download 与 /Items/{id}/File 一并拦截（管理员放行）
"""
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 不写死库文件名：交给 backend.database 解析（新装 aetrix_unified.db，老部署沿用原库）
os.environ.setdefault("DATABASE_TYPE", "sqlite")

from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server.auth import ensure_emby_credentials, hash_emby_token
from backend.main import app
from backend.security import hash_password
from backend.integrations.store import invalidate

init_db()
client = TestClient(app)
suf = str(random.randint(100000, 999999))
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def set_config(key: str, value):
    db = SessionLocal()
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if value is None:
        if row:
            db.delete(row)
    elif row:
        row.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()
    invalidate(key)  # 热缓存失效：测试直接写库，手动清缓存
    db.close()


# 从默认状态开始（缺省 = 不限设备、允许下载、关闭自动踢人）
for key in ("device_limit_per_user", "device_limit_auto_evict",
            "allow_download", "subscription_required", "login_log_retention_days"):
    set_config(key, None)

# ---------- 造数据 ----------
media_file = os.path.join(tempfile.gettempdir(), f"ops_{suf}.mp4")
with open(media_file, "wb") as fh:
    fh.write(b"\x00" * 4096)

db = SessionLocal()
staff = models.WebUser(username=f"ops_stf{suf}", password_hash=hash_password("pass12345"), is_staff=True, admin_role="super")
user_a = models.WebUser(username=f"ops_a{suf}", password_hash=hash_password("pass12345"))
user_b = models.WebUser(username=f"ops_b{suf}", password_hash=hash_password("pass12345"))
plan = models.SubscriptionPlan(name=f"运营套餐{suf}", price=19.9, duration_days=30, is_active=True)
db.add_all([staff, user_a, user_b, plan])
db.commit()
for row in (staff, user_a, user_b, plan):
    db.refresh(row)

library = em.Library(guid=f"opslib{suf}", name=f"运营库{suf}", collection_type="movies")
db.add(library)
db.commit()
db.refresh(library)
item = em.MediaItem(
    guid=f"opsitem{suf}", library_id=library.id, item_type="movie",
    name=f"运营影片{suf}", file_path=media_file, container="mp4",
    duration_ticks=6_000_000_000, bitrate=2_000_000, size=os.path.getsize(media_file),
)
db.add(item)
db.commit()
item_guid = item.guid
staff_name, a_name, b_name = staff.username, user_a.username, user_b.username
plan_id = plan.id
# 设备风控用门户用户名走 AuthenticateByName。客户端只验 emby_password，
# 门户登录不再自动补这份哈希，这里显式写入，不发明接线用户名。
ensure_emby_credentials(db, user_a, password="pass12345")
db.close()


def portal_login(username: str) -> dict:
    r = client.post("/api/user/auth/login", json={"username": username, "password": "pass12345"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


staff_h, a_h, b_h = portal_login(staff_name), portal_login(a_name), portal_login(b_name)
# #1290 后 /emby/* 不再接受门户 JWT：第五节的 /emby/* 走 Emby 令牌，
# 其余 /api/* 仍走门户 JWT。
_db = SessionLocal()
_a_e = {"X-Emby-Token": "o" * 40}  # 顺序跑时付费墙已占 "s"*40（token 列 UNIQUE），此处须用未被任何冒烟脚本占用的 raw
_s_e = {"X-Emby-Token": "w" * 40}
for _name, _raw in ((a_name, "o" * 40), (staff_name, "w" * 40)):
    _u = _db.query(models.WebUser).filter(models.WebUser.username == _name).first()
    _db.add(em.EmbyApiToken(token=hash_emby_token(_raw), user_id=_u.id,
                            device_id="smoke-test", is_revoked=False))
_db.commit()
_db.close()
# 播放门禁（2026-10-26 硬规矩，管理员同样过门禁）：在线播放项与管理员
# File 项须 user_a 与 staff 都有 active 绑定，否则门禁 403 先于断言。
with SessionLocal() as _db2:
    for _name in (a_name, staff_name):
        _u = _db2.query(models.WebUser).filter(models.WebUser.username == _name).first()
        _cred = em.MountCredential(mount_id=None, scope="user", owner_user_id=_u.id,
                                   sealed_credential="", key_id="fernet-v1",
                                   status="active")
        _db2.add(_cred)
        _db2.flush()
        _db2.add(em.UserStorageBinding(user_id=_u.id, provider="drive",
                                       account_label="冒烟盘", credential_id=_cred.id,
                                       status="active"))
    _db2.commit()


def emby_login(username: str, device_id: str, password: str = "pass12345"):
    return client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": username, "Pw": password},
        headers={"X-Emby-Authorization":
                 f'MediaBrowser Client="SmokeClient", Device="{device_id}", '
                 f'DeviceId="{device_id}", Version="7.0"'},
    )


# ==================== 一、卡码生成 ====================

r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 1, "count": 2, "days": 30}, headers=staff_h)
body = r.json()
reg_codes = [c["code"] for c in body.get("codes", [])]
check("生成注册码 ×2", r.status_code == 200 and len(reg_codes) == 2, r.text[:120])
check("注册码带类型前缀 REG", all(c.startswith("REG") for c in reg_codes), str(reg_codes))
check("注册码文案为 30 天", body["codes"][0]["days_text"] == "30 天",
      str(body["codes"][0]["days_text"]))

r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 2, "count": 1, "days": 7}, headers=staff_h)
ren_code = r.json()["codes"][0]["code"]
check("生成续期码（7 天）", r.status_code == 200 and ren_code.startswith("REN"), ren_code)

r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 3, "count": 1}, headers=staff_h)
vip_body = r.json()
vip_code = vip_body["codes"][0]["code"]
check("生成白名单码（长期有效）", r.status_code == 200 and vip_code.startswith("VIP")
      and vip_body["codes"][0]["days_text"] in ("永久", "长期有效"), str(vip_body["codes"][0]))

r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 1, "count": 1, "is_decoy": True}, headers=staff_h)
decoy_code = r.json()["codes"][0]["code"]
check("生成诱饵码", r.status_code == 200 and decoy_code, r.text[:120])

r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 2, "count": 1, "days": 5, "target_username": a_name},
                headers=staff_h)
named_code = r.json()["codes"][0]["code"]
check("生成指名码（限定账号）", r.status_code == 200 and named_code.startswith("REN"),
      r.text[:120])

r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 1, "target_username": f"ghost{suf}"}, headers=staff_h)
check("指名不存在的账号 → 400", r.status_code == 400, str(r.status_code))

r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 1, "algorithm": "not-a-real-algo"}, headers=staff_h)
check("非法随机算法 → 400", r.status_code == 400, str(r.status_code))

r = client.post("/api/admin/registration-codes/generate", json={"code_type": 1}, headers=a_h)
check("非管理员生成卡码 → 403", r.status_code == 403, str(r.status_code))

r = client.get("/api/admin/registration-codes/stats", headers=staff_h)
stats = r.json()
check("卡码统计：总数与类型分布", r.status_code == 200 and stats["total"] >= 6
      and {t["code_type"] for t in stats["by_type"]} >= {1, 2, 3},
      f"total={stats.get('total')}")
check("卡码统计：诱饵计数", stats["decoy"]["total"] >= 1 and stats["decoy"]["triggered"] == 0,
      str(stats.get("decoy")))

r = client.get("/api/admin/registration-codes/list", params={"code_type": 3}, headers=staff_h)
listed = r.json()
check("卡码列表按类型筛选", r.status_code == 200 and listed["total"] >= 1
      and all(c["code_type"] == 3 for c in listed["codes"]), str(listed.get("total")))

# ==================== 二、用户核销 ====================

r = client.post("/api/user/membership/redeem/preview", json={"code": reg_codes[0]}, headers=a_h)
preview = r.json()
check("预览注册码：有效且识别类型", r.status_code == 200 and preview["valid"] is True
      and preview["kind"] == "code" and preview["days"] == 30, str(preview))

r = client.post("/api/user/membership/redeem/preview", json={"code": "NOSUCHCODE"}, headers=a_h)
check("预览不存在的卡码 → valid=false", r.status_code == 200 and r.json()["valid"] is False,
      str(r.json()))

r = client.post("/api/user/membership/redeem", json={"code": reg_codes[0]}, headers=a_h)
granted = r.json()
check("核销注册码 → 开通会员", r.status_code == 200 and granted.get("success") is True,
      r.text[:160])
end_after_register = datetime.fromisoformat(granted["end_date"]) if granted.get("end_date") else None
check("到期时间约 30 天后",
      bool(end_after_register) and 28 <= (end_after_register - datetime.now()).days <= 30,
      str(granted.get("end_date")))

r = client.get("/api/user/auth/me", headers=a_h)
check("核销后 /auth/me 为会员", r.json().get("is_vip") is True, str(r.json().get("is_vip")))

r = client.post("/api/user/membership/redeem", json={"code": reg_codes[0]}, headers=a_h)
check("同一注册码重复使用 → 400", r.status_code == 400, r.text[:120])

r = client.post("/api/user/membership/redeem", json={"code": reg_codes[1]}, headers=a_h)
check("已有会员再用注册码 → 400（提示用续期码）",
      r.status_code == 400 and "续期" in (r.json().get("detail") or ""),
      str(r.json().get("detail")))

r = client.post("/api/user/membership/redeem", json={"code": ren_code}, headers=a_h)
renewed = r.json()
end_after_renew = datetime.fromisoformat(renewed["end_date"]) if renewed.get("end_date") else None
delta = (end_after_renew - end_after_register).days if (end_after_renew and end_after_register) else -1
check("续期码在到期时间上叠加 7 天", r.status_code == 200 and delta == 7, f"delta={delta}")

r = client.post("/api/user/membership/redeem", json={"code": vip_code}, headers=a_h)
vip_result = r.json()
check("白名单码 → 长期有效（days=-1）", r.status_code == 200 and vip_result.get("days") == -1,
      str(vip_result))

r = client.post("/api/user/membership/redeem", json={"code": named_code}, headers=b_h)
check("指名码被他人使用 → 400", r.status_code == 400
      and "指定" in (r.json().get("detail") or ""), str(r.json().get("detail")))

before_named = (datetime.fromisoformat(
    client.get("/api/user/subscriptions", headers=a_h).json()[0]["end_date"]) - datetime.now()).days
r = client.post("/api/user/membership/redeem", json={"code": named_code}, headers=a_h)
after_named = (datetime.fromisoformat(
    client.get("/api/user/subscriptions", headers=a_h).json()[0]["end_date"]) - datetime.now()).days
check("指名码本人使用 → 成功且叠加 5 天",
      r.status_code == 200 and r.json().get("success") is True
      and after_named - before_named == 5,
      f"{before_named} -> {after_named}")

r = client.post("/api/user/membership/redeem", json={"code": decoy_code}, headers=b_h)
check("诱饵码不暴露身份（统一提示卡码无效）", r.status_code == 400
      and r.json().get("detail") == "卡码无效", str(r.json().get("detail")))

db = SessionLocal()
b_row = db.query(models.WebUser).filter(models.WebUser.username == b_name).first()
b_banned = not bool(b_row.is_active)
decoy_log = db.query(models.LoginLog).filter(
    models.LoginLog.user_id == b_row.id, models.LoginLog.reason == "decoy_code"
).first()
db.close()
check("诱饵码触发账号封禁", b_banned, f"is_active={not b_banned}")
check("诱饵码触发落安全日志", decoy_log is not None, str(decoy_log.reason if decoy_log else None))

# 停用 / 回收
r = client.get("/api/admin/registration-codes/list", params={"code_type": 2}, headers=staff_h)
ren_id = r.json()["codes"][0]["id"]
r = client.patch(f"/api/admin/registration-codes/{ren_id}",
                 json={"is_active": False}, headers=staff_h)
check("停用已使用的卡码", r.status_code == 200 and r.json()["code"]["state"] == "disabled",
      str(r.json().get("code", {}).get("state")))

r = client.delete(f"/api/admin/registration-codes/{ren_id}", headers=staff_h)
check("已使用的卡码不可删除 → 400", r.status_code == 400, str(r.status_code))

r = client.get("/api/admin/registration-codes/list", params={"state": "active"}, headers=staff_h)
unused = [c for c in r.json()["codes"] if c["use_count"] == 0]
r = client.delete(f"/api/admin/registration-codes/{unused[0]['id']}", headers=staff_h)
check("未使用的卡码可删除", r.status_code == 200 and r.json().get("success") is True, r.text[:120])

# ==================== 三、设备风控 ====================

r = emby_login(a_name, f"devA{suf}")
check("客户端登录成功", r.status_code == 200, r.text[:120])
dev_a1 = f"devA{suf}"

r = emby_login(b_name, f"devB{suf}")
check("被封禁账号客户端登录 → 401", r.status_code == 401, str(r.status_code))

db = SessionLocal()
a_row = db.query(models.WebUser).filter(models.WebUser.username == a_name).first()
a_id = a_row.id
device_row = db.query(models.UserDevice).filter(
    models.UserDevice.user_id == a_id, models.UserDevice.device_id == dev_a1
).first()
db.close()
check("设备成功登记（含客户端与 IP 字段）", device_row is not None and device_row.client == "SmokeClient",
      str(device_row.client if device_row else None))

r = client.put("/api/admin/economy/settings",
               json={"settings": {"device_limit_per_user": "1"}}, headers=staff_h)
check("管理端可配置设备上限", r.status_code == 200, r.text[:120])
r = client.get("/api/admin/economy/settings", headers=staff_h)
check("设备上限回读为 1", str(r.json()["settings"].get("device_limit_per_user")) == "1",
      str(r.json()["settings"].get("device_limit_per_user")))

set_config("device_limit_auto_evict", None)
r = emby_login(a_name, f"devA2{suf}")
check("超出设备上限 → 403 且提示清理", r.status_code == 403
      and "设备" in (r.text or ""), r.text[:160])

db = SessionLocal()
limit_log = db.query(models.LoginLog).filter(
    models.LoginLog.user_id == a_id, models.LoginLog.reason == "device_limit"
).first()
db.close()
check("设备超限落安全日志", limit_log is not None, str(limit_log.reason if limit_log else None))

set_config("device_limit_auto_evict", "true")
r = emby_login(a_name, f"devA3{suf}")
check("开启自动踢人后新设备可登录", r.status_code == 200, r.text[:120])

db = SessionLocal()
devices_now = [d.device_id for d in db.query(models.UserDevice).filter(
    models.UserDevice.user_id == a_id
).all()]
db.close()
check("最久未使用的设备被自动移除", dev_a1 not in devices_now and f"devA3{suf}" in devices_now,
      str(devices_now))

set_config("device_limit_per_user", None)
set_config("device_limit_auto_evict", None)

r = client.get("/api/admin/devices", headers=staff_h)
admin_devices = r.json()
mine = [d for d in admin_devices["devices"] if d["user_id"] == a_id]
check("管理端设备列表带用户名", r.status_code == 200 and bool(mine)
      and mine[0]["username"] == a_name, str(admin_devices.get("total")))

r = client.get("/api/admin/devices/stats", headers=staff_h)
check("设备统计可用", r.status_code == 200 and r.json()["total"] >= 1, str(r.json()))

target = mine[0]["device_id"]
r = client.put(f"/api/admin/devices/{target}", params={"user_id": a_id},
               json={"is_blocked": True}, headers=staff_h)
check("管理端封禁设备", r.status_code == 200, r.text[:120])

r = emby_login(a_name, target)
check("被封禁设备再登录 → 403", r.status_code == 403, str(r.status_code))

r = client.put(f"/api/admin/devices/{target}", params={"user_id": a_id},
               json={"is_blocked": False}, headers=staff_h)
check("管理端解封设备", r.status_code == 200, r.text[:120])
r = emby_login(a_name, target)
check("解封后可再次登录", r.status_code == 200, str(r.status_code))

r = client.get("/api/user/emby/devices", headers=a_h)
user_devices = r.json()
check("用户端可查看我的设备", r.status_code == 200 and user_devices["count"] >= 1
      and user_devices["limit"] == 0, str(user_devices.get("count")))

r = client.delete(f"/api/user/emby/devices/{target}", headers=a_h)
check("用户端移除自己的设备", r.status_code == 200, r.text[:120])

r = client.delete(f"/api/user/emby/devices/{target}", headers=a_h)
check("重复移除 → 404", r.status_code == 404, str(r.status_code))

r = emby_login(a_name, target)
check("被移除设备已吊销令牌（需重新认证）", r.status_code == 200, str(r.status_code))

# ==================== 四、登录与安全日志 ====================

client.post("/api/user/auth/login", json={"username": a_name, "password": "wrongpass"})

r = client.get("/api/admin/login-logs", params={"limit": 200}, headers=staff_h)
logs = r.json()
reasons = {entry["reason"] for entry in logs["logs"]}
check("登录日志可读取", r.status_code == 200 and logs["total"] >= 1, str(logs.get("total")))
check("含门户登录与登录失败记录",
      {"portal_login", "portal_login_failed"} <= reasons, str(sorted(reasons)))
check("含设备超限与诱饵码风控记录",
      {"device_limit", "decoy_code"} <= reasons, str(sorted(reasons)))
check("日志含中文标签与 24h 汇总",
      all(entry["reason_label"] for entry in logs["logs"])
      and "failed_24h" in logs["summary"], str(logs.get("summary")))

r = client.get("/api/admin/login-logs", params={"reason": "decoy_code"}, headers=staff_h)
check("日志按事件类型筛选", r.status_code == 200 and all(
    entry["reason"] == "decoy_code" for entry in r.json()["logs"]), str(r.json().get("total")))

r = client.post("/api/admin/login-logs/purge", json={"days": 0}, headers=staff_h)
check("清空日志", r.status_code == 200 and r.json()["deleted"] >= 1, r.text[:120])

r = client.get("/api/admin/login-logs", headers=staff_h)
check("清空后日志为空", r.json()["total"] == 0, str(r.json().get("total")))

# ==================== 五、下载策略兜底 ====================

set_config("allow_download", "false")
db = SessionLocal()
now = datetime.now()
db.add(models.UserSubscription(
    user_id=a_id, plan_id=plan_id,
    start_date=now - timedelta(days=1), end_date=now + timedelta(days=30), status="active",
))
db.commit()
db.close()

set_config("subscription_required", "false")  # 隔离下载策略，先关掉付费墙
r = client.get(f"/emby/Items/{item_guid}/Download", headers=_a_e)
check("关闭下载后 /Download → 403", r.status_code == 403, str(r.status_code))

r = client.get(f"/emby/Items/{item_guid}/File", headers=_a_e)
check("关闭下载后 /Items/{id}/File 同样被拦（网关兜底）", r.status_code == 403, str(r.status_code))

r = client.get(f"/emby/Items/{item_guid}/File", headers=_s_e)
check("管理员不受下载开关限制", r.status_code == 200, str(r.status_code))

r = client.get(f"/emby/Videos/{item_guid}/stream", headers=_a_e)
check("在线播放不受下载开关影响", r.status_code in (200, 206), str(r.status_code))

set_config("allow_download", None)
r = client.get(f"/emby/Items/{item_guid}/File", headers=_a_e)
check("重新允许下载后可访问", r.status_code == 200, str(r.status_code))

# ==================== 六、注册模式开关（注册码门禁已下线） ====================
# 注册码注册门禁已于 v2.7.x 彻底删除：只剩 open（开放注册）/ closed（关闭注册），
# DB 里残留的 "code" 值按 closed 处理（fail-closed，避免静默开放注册）。
# 卡码体系走核销路径，不受影响。

set_config("registration_mode", "open")
r = client.post("/api/user/auth/register", json={
    "username": f"reg_open{suf}", "password": "pass12345",
})
check("开放注册成功", r.status_code in (200, 201), str(r.status_code))
if r.status_code in (200, 201):
    db = SessionLocal()
    u = db.query(models.WebUser).filter(models.WebUser.username == f"reg_open{suf}").first()
    check("开放注册用户渠道记为 open", u is not None and u.register_channel == "open",
          f"register_channel={u.register_channel if u else None}")
    db.close()

set_config("registration_mode", "closed")
r = client.post("/api/user/auth/register", json={
    "username": f"reg_cls{suf}", "password": "pass12345",
})
check("关闭注册时注册 → 403", r.status_code == 403, str(r.status_code))

# 遗留值兼容：DB 里残留的 "code"（及任何未知值）按 closed 处理
# （fail-closed：当初设 code 是为了不对外开放，升级后不能静默变成开放注册）
set_config("registration_mode", "code")
r = client.post("/api/user/auth/register", json={
    "username": f"reg_legacy{suf}", "password": "pass12345",
})
check("遗留 code 模式按关闭注册处理（fail-closed）", r.status_code == 403, str(r.status_code))
# 后续用例（诱饵码受害者注册）需要开放注册，切回 open
set_config("registration_mode", "open")

r = client.put("/api/admin/settings/registration", json={"mode": "code", "message": ""},
               headers=staff_h)
check("管理接口拒绝 code 模式 → 400", r.status_code == 400, str(r.status_code))

# 诱饵码仍走核销路径：注册一个普通用户，核销诱饵码 → 400 + 封号 + 风控日志
r = client.post("/api/admin/registration-codes/generate",
                json={"code_type": 1, "count": 1, "is_decoy": True}, headers=staff_h)
signup_decoy = r.json()["codes"][0]["code"]
victim = f"decoy_vic{suf}"
r = client.post("/api/user/auth/register", json={
    "username": victim, "password": "pass12345",
})
vic_h = {"Authorization": f"Bearer {r.json()['access_token']}"}
r = client.post("/api/user/membership/redeem", json={"code": signup_decoy}, headers=vic_h)
check("诱饵码核销被拒", r.status_code == 400, str(r.status_code))
db = SessionLocal()
banned = db.query(models.WebUser).filter(models.WebUser.username == victim).first()
reg_decoy_log = db.query(models.LoginLog).filter(
    models.LoginLog.reason == "decoy_code", models.LoginLog.username == victim
).first()
banned_flag = banned is not None and not banned.is_active
db.close()
check("诱饵码核销后用户被封并落风控日志",
      banned_flag and reg_decoy_log is not None,
      f"banned={banned_flag} logged={reg_decoy_log is not None}")

set_config("registration_mode", None)

# ==================== 七、公告广播与通知渠道的"真投递" ====================

# 7.1 公告必须给**每个启用中的用户**落站内消息（不只是当时在线的人）。
# 这里两个用户都没有建立 WebSocket 连接，正是「离线用户」。
db = SessionLocal()
offline = models.WebUser(
    username=f"ops_off{suf}", password_hash=hash_password("pass12345"), is_active=True
)
disabled = models.WebUser(
    username=f"ops_dis{suf}", password_hash=hash_password("pass12345"), is_active=False
)
db.add_all([offline, disabled])
db.commit()
offline_id, disabled_id = offline.id, disabled.id
db.close()

title = f"运营公告{suf}"
r = client.post("/api/admin/announcements",
                json={"title": title, "content": "本节用于验证公告真的送达全部用户", "type": "system"},
                headers=staff_h)
body = r.json() if r.status_code == 200 else {}
ann_id = body.get("announcement_id")
check("发布公告成功", r.status_code == 200 and bool(ann_id), r.text[:120])
check("公告返回真实送达人数（而非无条件宣称全部用户）",
      isinstance(body.get("notified_users"), int) and body["notified_users"] >= 3,
      f"notified_users={body.get('notified_users')}")

db = SessionLocal()
messages = db.query(models.StationMessage).filter(models.StationMessage.title == f"📢 {title}").all()
recipients = {m.to_user_id for m in messages}
check("没连 WebSocket 的用户（离线）也收到公告站内消息",
      offline_id in recipients, f"offline_id={offline_id} recipients={sorted(recipients)}")
check("禁用账号不会被投递", disabled_id not in recipients, f"disabled_in={disabled_id in recipients}")
check("公告关联到具体公告 id",
      any(m.related_id == ann_id for m in messages),
      f"related={sorted({m.related_id for m in messages})[:4]}")
db.close()

r = client.get("/api/user/messages", headers=a_h)
inbox = r.json() if isinstance(r.json(), list) else (r.json() or {}).get("messages", [])
check("用户能在收件箱里读到公告消息",
      r.status_code == 200 and any((m.get("title") or "").endswith(title) for m in inbox),
      f"HTTP {r.status_code} inbox={len(inbox)}")

# 7.1b 后台「站内消息 → 广播」走的是同一条链路（/api/admin/messages/broadcast）
bcast_title = f"全员广播{suf}"
r = client.post("/api/admin/messages/broadcast",
                json={"title": bcast_title, "content": "广播正文", "message_type": "system"},
                headers=staff_h)
body = r.json() if r.status_code == 200 else {}
check("管理端广播接口可用且回报真实人数",
      r.status_code == 200 and isinstance(body.get("notified_users"), int)
      and body["notified_users"] >= 2, r.text[:120])

db = SessionLocal()
bcast_to = {m.to_user_id for m in db.query(models.StationMessage).filter(
    models.StationMessage.title == bcast_title).all()}
check("广播同样落到离线用户", offline_id in bcast_to, f"recipients={sorted(bcast_to)}")
check("广播不投递禁用账号", disabled_id not in bcast_to)
db.query(models.StationMessage).filter(models.StationMessage.title == bcast_title).delete()
db.commit()
db.close()

db = SessionLocal()
db.query(models.StationMessage).filter(models.StationMessage.title == f"📢 {title}").delete()
db.query(models.WebUser).filter(models.WebUser.id.in_([offline_id, disabled_id])).delete()
db.commit()
db.close()

# 7.2 邮件 / Telegram 渠道不能再"假成功"：投递失败必须如实记 failed + 原因
import asyncio  # noqa: E402

from backend import notifications as notif  # noqa: E402

mail_channel = notif.EmailChannel(
    smtp_host="127.0.0.1", smtp_port=9,  # 必然被拒的本地端口：不发外网、也不挂起
    smtp_user="nobody@example.invalid", smtp_password="x",
)
db = SessionLocal()
mail_user = db.query(models.WebUser).filter(models.WebUser.username == a_name).first()
mail_user.email = f"{a_name}@example.invalid"
db.commit()
mail_ok = asyncio.get_event_loop().run_until_complete(
    mail_channel.send(mail_user.id, "投递失败用例", "正文", db=db))
mail_row = db.query(models.NotificationHistory).filter(
    models.NotificationHistory.target == mail_user.email
).order_by(models.NotificationHistory.id.desc()).first()
check("SMTP 投递失败时 send() 返回 False", mail_ok is False, f"returned={mail_ok}")
check("SMTP 失败记 failed + 原因，而不是记 sent",
      mail_row is not None and mail_row.status == "failed" and bool(mail_row.error_message),
      f"status={getattr(mail_row, 'status', None)} err={getattr(mail_row, 'error_message', None)}")

import httpx  # noqa: E402


class _FakeResp:
    status_code = 500
    text = "internal error"


class _FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *a, **kw):
        return _FakeResp()


real_client = httpx.AsyncClient
httpx.AsyncClient = _FakeClient  # 拦下真实请求，只验证失败路径的状态码判定
tg_channel = notif.TelegramChannel(bot_token="123:FAKE")
# 绑定走 WebUser.telegram_id（旧 telegram_users 表已删除）
if not mail_user.telegram_id:
    mail_user.telegram_id = 987654321
    db.commit()
tg_ok = asyncio.get_event_loop().run_until_complete(
    tg_channel.send(mail_user.id, "TG 失败用例", "正文", db=db))
httpx.AsyncClient = real_client
tg_row = db.query(models.NotificationHistory).filter(
    models.NotificationHistory.notification_type == "telegram"
).order_by(models.NotificationHistory.id.desc()).first()
check("Telegram 返回非 200 时 send() 返回 False", tg_ok is False, f"returned={tg_ok}")
check("Telegram 失败记 failed + 原因，而不是记 sent",
      tg_row is not None and tg_row.status == "failed" and bool(tg_row.error_message),
      f"status={getattr(tg_row, 'status', None)}")

db.query(models.NotificationHistory).filter(
    models.NotificationHistory.target.in_([mail_user.email, "987654321"])
).delete(synchronize_session=False)
mail_user.email = None
mail_user.telegram_id = None
db.commit()
db.close()

# ==================== 结果 ====================

print()
if failures:
    print(f"FAILED {len(failures)}:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("ALL PASS")

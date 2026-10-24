"""付费墙（会员门禁）冒烟测试

覆盖：
- 下载策略网关判过的结论会被路由复用（同一请求不再查两遍策略，v2.14.0）
- 开关开启（默认）时：非会员无法获取播放地址（PlaybackInfo 403）、无法直连拉流（stream 403）、无法下载
- 会员可正常获取播放地址与拉流（206 分段）
- 管理员始终放行（不打墙自己人）
- 开关关闭后全员放行
- 自定义拦截文案生效（前端据此展示付费墙引导）
- /auth/me 暴露 is_vip 与 subscription_required，供前端提前提示
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


def set_config(key: str, value: str | None) -> None:
    db = SessionLocal()
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if value is None:
        if row:
            db.delete(row)
    elif row:
        row.value = value
    else:
        db.add(models.SystemConfig(key=key, value=value))
    db.commit()
    invalidate(key)  # 热缓存失效：测试直接写库，手动清缓存
    db.close()


# 确保从默认状态开始
set_config("subscription_required", None)
set_config("subscription_gate_message", None)

# ---------- 造数据 ----------
media_file = os.path.join(tempfile.gettempdir(), f"paywall_{suf}.mp4")
with open(media_file, "wb") as fh:
    fh.write(b"\x00" * 4096)

db = SessionLocal()
staff = models.WebUser(username=f"stf{suf}", password_hash=hash_password("pass12345"), is_staff=True, admin_role="super")
member = models.WebUser(username=f"mem{suf}", password_hash=hash_password("pass12345"))
free = models.WebUser(username=f"free{suf}", password_hash=hash_password("pass12345"))
plan = models.SubscriptionPlan(name=f"付费墙套餐{suf}", price=19.9, duration_days=30, is_active=True)
db.add_all([staff, member, free, plan])
db.commit()
for row in (staff, member, free, plan):
    db.refresh(row)

now = datetime.now()
db.add(models.UserSubscription(
    user_id=member.id, plan_id=plan.id,
    start_date=now - timedelta(days=1), end_date=now + timedelta(days=29), status="active",
))
library = em.Library(guid=f"pwlib{suf}", name=f"付费墙库{suf}", collection_type="movies")
db.add(library)
db.commit()
db.refresh(library)
item = em.MediaItem(
    guid=f"pwitem{suf}", library_id=library.id, item_type="movie",
    name=f"付费影片{suf}", file_path=media_file, container="mp4",
    duration_ticks=6_000_000_000, bitrate=2_000_000, size=os.path.getsize(media_file),
)
db.add(item)
db.commit()
item_guid = item.guid
member_name, free_name, staff_name = member.username, free.username, staff.username
db.close()


def login(username: str) -> dict:
    r = client.post("/api/user/auth/login", json={"username": username, "password": "pass12345"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


member_h, free_h, staff_h = login(member_name), login(free_name), login(staff_name)

# ---------- /auth/me 暴露付费墙状态 ----------
r = client.get("/api/user/auth/me", headers=free_h)
body = r.json() if r.status_code == 200 else {}
check("/auth/me 暴露付费墙开启且非会员", body.get("subscription_required") is True and body.get("is_vip") is False,
      f"required={body.get('subscription_required')} vip={body.get('is_vip')}")

r = client.get("/api/user/auth/me", headers=member_h)
check("/auth/me 会员侧 is_vip=true", r.json().get("is_vip") is True, str(r.json().get("is_vip")))

# ---------- 播放地址：非会员被拦 ----------
r = client.post(f"/emby/Items/{item_guid}/PlaybackInfo", headers=free_h, json={})
check("非会员 PlaybackInfo → 403", r.status_code == 403, str(r.status_code))
check("默认拦截文案可读", "会员" in (r.json().get("detail") or ""), str(r.json().get("detail")))

r = client.post(f"/emby/Items/{item_guid}/PlaybackInfo", headers=member_h, json={})
check("会员 PlaybackInfo → 200 且含播放地址",
      r.status_code == 200 and "/stream" in (r.json()["MediaSources"][0].get("DirectStreamUrl") or ""),
      str(r.status_code))

r = client.post(f"/emby/Items/{item_guid}/PlaybackInfo", headers=staff_h, json={})
check("管理员放行", r.status_code == 200, str(r.status_code))

# ---------- 直连拉流 / 下载：非会员被拦 ----------
r = client.get(f"/emby/Videos/{item_guid}/stream", headers=free_h)
check("非会员直连拉流 → 403", r.status_code == 403, str(r.status_code))

r = client.get(f"/emby/Videos/{item_guid}/stream", headers={**member_h, "Range": "bytes=0-99"})
check("会员拉流 → 206 分段", r.status_code in (200, 206), str(r.status_code))

r = client.get(f"/emby/Items/{item_guid}/Download", headers=free_h)
check("非会员下载 → 403", r.status_code == 403, str(r.status_code))

r = client.get(f"/emby/videos/{item_guid}/master.m3u8", headers=free_h)
check("转码地址已删除 → 404（不再走付费墙/转码）", r.status_code == 404, str(r.status_code))

r = client.get(f"/emby/Items/{item_guid}/Download", headers=member_h)
check("会员下载放行", r.status_code == 200, str(r.status_code))

# ---------- 自定义拦截文案 ----------
set_config("subscription_gate_message", "请开通会员后观看")
r = client.post(f"/emby/Items/{item_guid}/PlaybackInfo", headers=free_h, json={})
check("自定义拦截文案生效", r.status_code == 403 and r.json().get("detail") == "请开通会员后观看",
      str(r.json().get("detail")))
set_config("subscription_gate_message", None)

# ---------- 关闭付费墙：全员放行 ----------
set_config("subscription_required", "false")
r = client.post(f"/emby/Items/{item_guid}/PlaybackInfo", headers=free_h, json={})
check("关闭付费墙后非会员放行", r.status_code == 200, str(r.status_code))
r = client.get("/api/user/auth/me", headers=free_h)
check("/auth/me 同步反映关闭状态", r.json().get("subscription_required") is False,
      str(r.json().get("subscription_required")))

# ---------- 下载策略网关的结论被路由复用（v2.14.0） ----------
# 下载类请求会先过 DownloadGuardMiddleware（闸站开关 + 该服策略 + 管理员放行），
# 路由里的 ensure_download_allowed 直接复用这个结论，不再把同一件事查第二遍；
# 网关没给出结论时（拿不到身份 / 判定异常），路由自己照旧拦。
from starlette.requests import Request as StarletteRequest  # noqa: E402

from backend.subscriptions import ensure_download_allowed  # noqa: E402
from fastapi import HTTPException as FastAPIHTTPException  # noqa: E402

set_config("allow_download", "false")
with SessionLocal() as db:
    free_user = db.query(models.WebUser).filter(models.WebUser.username == free_name).first()
    guarded = StarletteRequest({"type": "http", "headers": [],
                                "state": {"download_allowed_by_guard": True}})
    try:
        ensure_download_allowed(db, free_user, request=guarded)
        reused = True
    except FastAPIHTTPException:
        reused = False
    try:
        ensure_download_allowed(db, free_user)  # 没带 request → 自然要自己判
        blocked_without_state = False
    except FastAPIHTTPException:
        blocked_without_state = True
check("网关已判「允许」时路由不再重复查下载策略", reused)
check("网关没给结论时路由自己照旧拦下", blocked_without_state)
set_config("allow_download", None)

# 保持默认开启，避免影响其它用例
set_config("subscription_required", "true")
set_config("subscription_gate_message", None)

try:
    os.remove(media_file)
except OSError:
    pass

print()
if failures:
    print(f"❌ {len(failures)} 项失败：" + "、".join(failures))
    sys.exit(1)
print("✅ 付费墙冒烟测试全部通过")

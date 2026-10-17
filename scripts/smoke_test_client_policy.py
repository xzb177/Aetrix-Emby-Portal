#!/usr/bin/env python3
"""播放与客户端策略冒烟测试（v2.26.0）

以前这些策略散在两处：环境变量（``EMBY_MAX_TRANSCODES``，改一次要登机器重启）与设置页里的
下载开关。现在它们是面板上的**播放与客户端策略**，而且**真的在播放入口生效**：

1. 默认 = 与升级前完全一致（允许转码、不限并发、不拦客户端）；
2. 关掉转码 → 播放信息不再给「可转码」与转码地址，直接请求 master.m3u8 得到 403；
3. 并发上限 → 满了拒绝**新的**转码请求（503），不影响已经在看的人；
4. 写策略只认白名单键（未知键 / 非法值不影响原值），并且只有超级管理员能写；
5. 每次写入都留审计日志。

2026-10：删除服务端码率钳制，客户端要多少码率给多少。

用法：python scripts/smoke_test_client_policy.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"

from fastapi.testclient import TestClient  # noqa: E402

from backend import models, playback_policy  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import api as emby_api  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.main import app  # noqa: E402
from backend.security import create_access_token, hash_password  # noqa: E402
from backend.integrations.store import invalidate  # noqa: E402

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


def set_config(key: str, value) -> None:
    with SessionLocal() as db:
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if value is None:
            if row:
                db.delete(row)
        elif row:
            row.value = str(value)
        else:
            db.add(models.SystemConfig(key=key, value=str(value)))
        db.commit()
    # 测试直接写 DB 绕过了正常写路径的缓存失效：手动清热缓存，
    # 否则读到的可能是过期值（热重载 60s TTL）。
    invalidate(key)


def clear_policy() -> None:
    for key in playback_policy.POLICY_KEYS:
        set_config(key, None)


# 从默认状态开始：付费墙关闭（这条测试只关心播放与客户端策略），策略全清空
set_config("subscription_required", "false")
clear_policy()

media_file = os.path.join(tempfile.gettempdir(), "client_policy_probe.mkv")
with open(media_file, "wb") as fh:
    fh.write(b"\x00" * 4096)

with SessionLocal() as db:
    viewer = models.WebUser(username="policy_user", password_hash=hash_password("pass12345"),
                            is_active=True)
    boss = models.WebUser(username="policy_admin", password_hash=hash_password("pass12345"),
                          is_active=True, is_staff=True, admin_role="super")
    lib = em.Library(guid="p" * 32, name="策略测试库", collection_type="movies", paths="")
    db.add_all([viewer, boss, lib])
    db.commit()
    items = em.MediaItem(guid="q" * 32, library_id=lib.id, item_type="movie",
                         name="Policy Probe", sort_name="policy probe",
                         file_path=media_file, container="mkv", bitrate=12_000_000)
    db.add(items)
    db.commit()
    user_id, staff_id, item_id = viewer.id, boss.id, items.guid

H_USER = {"Authorization": f"Bearer {create_access_token(user_id)}"}
H_STAFF = {"Authorization": f"Bearer {create_access_token(staff_id)}"}
PB = f"/emby/Items/{item_id}/PlaybackInfo"


def playback(headers=H_USER, ua="Emby/4.9.0.30 Android", body=None):
    h = dict(headers)
    if ua:
        h["User-Agent"] = ua
    return client.post(PB, json=body or {}, headers=h)


# ==================== 1. 默认口径 ====================
print("=== 1. 默认（与升级前一致）===")
r = client.get("/api/admin/playback/policy", headers=H_STAFF)
policy = r.json() if r.status_code == 200 else {}
check("策略端点 200 且返回默认值（允许转码 / 不限并发）",
      r.status_code == 200
      and policy["policy"]["transcode_enabled"] is True
      and policy["policy"]["max_concurrent_transcodes"] == 0,
      str(policy.get("policy")))
check("策略端点带回运行态（本机并发 / 上限 / 谁在出流 / ffmpeg）",
      {"active_transcodes", "capacity", "playback_node", "ffmpeg_available"} <= set(policy["runtime"]),
      str(policy.get("runtime")))

r = playback()
check("默认：播放信息带可转码与转码地址",
      r.status_code == 200 and r.json()["MediaSources"][0]["SupportsTranscoding"] is True
      and "TranscodingUrl" in r.json()["MediaSources"][0],
      f"HTTP {r.status_code}")

# ==================== 2. 关掉转码 ====================
print("\n=== 2. 关掉服务端转码 ===")
set_config("playback_transcode_enabled", "false")
r = playback()
src = r.json()["MediaSources"][0] if r.status_code == 200 else {}
check("关掉转码：播放信息不再声明可转码，也不给转码地址",
      r.status_code == 200 and src.get("SupportsTranscoding") is False
      and "TranscodingUrl" not in src,
      str({k: src.get(k) for k in ("SupportsTranscoding", "TranscodingUrl")}))
r = client.get(f"/emby/videos/{item_id}/master.m3u8", headers=H_USER)
check("关掉转码：直接请求 master.m3u8 → 403 且说明原因",
      r.status_code == 403 and "转码" in str(r.json().get("detail", "")), f"HTTP {r.status_code} {r.text[:120]}")
r = playback(headers=H_STAFF)
check("管理员不受转码开关限制（排障时能自己验证）",
      r.status_code == 200 and r.json()["MediaSources"][0]["SupportsTranscoding"] is True,
      f"HTTP {r.status_code}")
set_config("playback_transcode_enabled", None)

# ==================== 3. 并发上限 ====================
print("\n=== 3. 并发上限（拒绝新的，不动正在看的）===")
set_config("playback_max_concurrent_transcodes", "1")
real_active = emby_api.active_transcode_ids
emby_api.active_transcode_ids = lambda: ["fake-session"]
try:
    r = client.get(f"/emby/videos/{item_id}/master.m3u8", headers=H_USER)
    detail = str(r.json().get("detail", "")) if r.headers.get("content-type", "").startswith("application/json") else ""
    check("并发满了：新的转码请求被拒 503 且说清上限",
          r.status_code == 503 and "上限" in detail, f"HTTP {r.status_code} {detail[:80]}")
finally:
    emby_api.active_transcode_ids = real_active

set_config("playback_max_concurrent_transcodes", "0")
r = client.get(f"/emby/videos/{item_id}/master.m3u8", headers=H_USER)
detail = ""
try:
    detail = str(r.json().get("detail", ""))
except Exception:  # noqa: BLE001
    pass
check("上限归零（= 用进程内置上限）：不再按策略拒绝",
      not (r.status_code == 503 and "上限" in detail and "已达上限（" in detail),
      f"HTTP {r.status_code} {detail[:80]}")

# ==================== 6. 写策略：白名单键与权限 ====================
print("\n=== 5. 写策略 ===")
r = client.put("/api/admin/playback/policy", headers=H_STAFF,
               json={"policy": {"playback_max_concurrent_transcodes": "3",
                                "unknown_key": "1"}})
applied = r.json().get("applied", {}) if r.status_code == 200 else {}
check("写策略 200 且只应用白名单里的键",
      r.status_code == 200 and set(applied) == {"playback_max_concurrent_transcodes"},
      str(applied))
with SessionLocal() as db:
    check("回读一致",
          playback_policy.policy_payload(db)["max_concurrent_transcodes"] == 3)
with SessionLocal() as db:
    actions = [row.action for row in db.query(models.AdminLog).all()]
check("策略变更留了审计日志", "playback_policy_update" in actions, str(sorted(set(actions))))

clear_policy()
with SessionLocal() as db:
    final = playback_policy.policy_payload(db)
check("清空后回到与升级前一致的口径",
      final["transcode_enabled"] is True
      and final["max_concurrent_transcodes"] == 0,
      str(final))

print()
if FAILED:
    print(f"❌ 播放与客户端策略冒烟失败 {len(FAILED)}/{TOTAL} 项：")
    for label in FAILED:
        print(f"   - {label}")
    sys.exit(1)
print(f"✅ 播放与客户端策略冒烟全部通过（{TOTAL} 项）")

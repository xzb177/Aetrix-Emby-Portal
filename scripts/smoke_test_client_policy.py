#!/usr/bin/env python3
"""播放与客户端策略冒烟测试（v2.26.0；2026-10-24 转码删除后版）

转码删除后本脚本验证的是「删除后的形态」，不再验证转码策略本身：

1. 策略面不再含任何转码字段，运行态只剩出流节点；
2. PlaybackInfo 对普通用户与管理员一律报 SupportsTranscoding=False，且无 TranscodingUrl；
3. 直接请求 master.m3u8 / hls1 得到 404（转码地址已移除）；
4. 写策略时旧转码键不再是白名单键（不被应用），每次写入仍留审计日志。

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


# ==================== 1. 策略面：转码键已全部移除 ====================
print("=== 1. 策略面（转码删除后）===")
r = client.get("/api/admin/playback/policy", headers=H_STAFF)
policy = r.json() if r.status_code == 200 else {}
check("策略端点 200 且不再含任何转码字段",
      r.status_code == 200
      and "transcode_enabled" not in policy["policy"]
      and "max_concurrent_transcodes" not in policy["policy"]
      and "drive_direct_play_enabled" in policy["policy"],
      str(policy.get("policy")))
check("策略端点运行态不再含转码字段，只剩出流节点",
      set(policy["runtime"]) == {"playback_node"},
      str(policy.get("runtime")))

# ==================== 2. PlaybackInfo：恒报不可转码 ====================
print("\n=== 2. PlaybackInfo（转码删除后）===")
r = playback()
src = r.json()["MediaSources"][0] if r.status_code == 200 else {}
check("普通用户：PlaybackInfo 报 SupportsTranscoding=False 且无 TranscodingUrl",
      r.status_code == 200 and src.get("SupportsTranscoding") is False
      and "TranscodingUrl" not in src,
      str({k: src.get(k) for k in ("SupportsTranscoding", "TranscodingUrl")}))
r = playback(headers=H_STAFF)
src = r.json()["MediaSources"][0] if r.status_code == 200 else {}
check("管理员同样报不可转码（转码删除后不再有管理员例外）",
      r.status_code == 200 and src.get("SupportsTranscoding") is False
      and "TranscodingUrl" not in src,
      str({k: src.get(k) for k in ("SupportsTranscoding", "TranscodingUrl")}))

# ==================== 3. 转码地址 404 ====================
print("\n=== 3. 转码地址已移除 ===")
r = client.get(f"/emby/videos/{item_id}/master.m3u8", headers=H_USER)
check("直接请求 master.m3u8 → 404",
      r.status_code == 404, f"HTTP {r.status_code} {r.text[:120]}")
r = client.get(f"/emby/Videos/{item_id}/hls1", headers=H_USER)
check("hls1 入口同样 404",
      r.status_code == 404, f"HTTP {r.status_code}")

# ==================== 4. 写策略：旧转码键不再是白名单 ====================
print("\n=== 4. 写策略 ===")
r = client.put("/api/admin/playback/policy", headers=H_STAFF,
               json={"policy": {"playback_max_concurrent_transcodes": "3",
                                "unknown_key": "1"}})
applied = r.json().get("applied", {}) if r.status_code == 200 else {}
check("写策略 200 且旧转码键不再被应用",
      r.status_code == 200 and applied == {},
      str(applied))
with SessionLocal() as db:
    actions = [row.action for row in db.query(models.AdminLog).all()]
check("策略变更留了审计日志", "playback_policy_update" in actions, str(sorted(set(actions))))

clear_policy()
with SessionLocal() as db:
    final = playback_policy.policy_payload(db)
check("清空后策略面仍无转码字段",
      "transcode_enabled" not in final
      and "max_concurrent_transcodes" not in final,
      str(final))

print()
if FAILED:
    print(f"❌ 播放与客户端策略冒烟失败 {len(FAILED)}/{TOTAL} 项：")
    for label in FAILED:
        print(f"   - {label}")
    sys.exit(1)
print(f"✅ 播放与客户端策略冒烟全部通过（{TOTAL} 项）")

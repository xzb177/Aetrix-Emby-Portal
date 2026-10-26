"""播放入口链路冒烟测试（付费墙 → 播放信息 → 直连流 → 互动）

用临时 SQLite 库 + 真实本地媒体文件走 HTTP，验证用户侧真正的播放链路：

1. 付费墙**默认开启**：非会员拿 PlaybackInfo / 直连流都是 403，且提示可读；
   发会员后同一入口变 200（`me.is_vip` 由订阅派生）。
2. 有本地文件的条目：`PlaybackInfo` 返回 MediaSources 带直链，
   `GET /emby/Videos/{id}/stream` 返回真实字节，`Range` 请求返回 206。
3. 没有媒体路径的条目（虚拟库聚合条目 / 源文件已丢失）必须返回 **404**，
   而不是让内部 `.lower()` 抛 AttributeError 变成 500。
4. 收藏 / 已看 / 取消已看链路照常落库。
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
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(WORK, 'playback.db')}"

from fastapi.testclient import TestClient  # noqa: E402

from backend import codes, models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.auth import hash_emby_token  # noqa: E402

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


# ==================== 播种：一个真文件 + 一个没有路径的条目 ====================

MEDIA = os.path.join(WORK, "playable.mp4")
with open(MEDIA, "wb") as f:
    f.write(b"\x00\x00\x00\x18ftypmp42" + b"payload-" * 512)  # 约 4KB 假媒体体

with SessionLocal() as db:
    lib = em.Library(guid="pb-lib", name="播放库", collection_type="movies", paths=WORK)
    db.add(lib)
    db.commit()
    db.refresh(lib)

    playable = em.MediaItem(
        guid="a" * 32, library_id=lib.id, item_type="movie", name="可播放片",
        duration_ticks=600_000_000, file_path=MEDIA, container="mp4",
    )
    no_path = em.MediaItem(
        guid="b" * 32, library_id=lib.id, item_type="movie", name="无路径片",
        duration_ticks=600_000_000, file_path=None,
    )
    db.add_all([playable, no_path])
    db.commit()

PLAYABLE, NO_PATH = "a" * 32, "b" * 32

r = client.post("/api/user/auth/register", json={"username": "playuser", "password": "playpass123"})
check("注册用户成功", r.status_code == 201, f"status={r.status_code}")
user_id = r.json()["user"]["id"]
H = {"Authorization": f"Bearer {r.json()['access_token']}"}
# #1290 后 /emby/* 不再接受门户 JWT：本脚本的 /emby/* 一律走 Emby 令牌，
# /api/*（me）仍走门户 JWT。
with SessionLocal() as _db:
    _raw = "p" * 40
    _db.add(em.EmbyApiToken(token=hash_emby_token(_raw), user_id=user_id,
                            device_id="smoke-test", is_revoked=False))
    # 播放门禁（2026-10-26 硬规矩）：绑定≠会员——非会员 403 项仍由付费墙
    # 先行拦截，但会员放行项须先有 active 绑定，否则门禁 403 先于断言。
    _cred = em.MountCredential(mount_id=None, scope="user", owner_user_id=user_id,
                               sealed_credential="", key_id="fernet-v1",
                               status="active")
    _db.add(_cred)
    _db.flush()
    _db.add(em.UserStorageBinding(user_id=user_id, provider="drive",
                                  account_label="冒烟盘", credential_id=_cred.id,
                                  status="active"))
    _db.commit()
H_EMBY = {"X-Emby-Token": _raw}

# ==================== 1. 付费墙默认开启 ====================

check("新用户 is_vip 为 False（未开通）", r.json()["user"]["is_vip"] is False)

r = client.post(f"/emby/Items/{PLAYABLE}/PlaybackInfo", json={}, headers=H_EMBY)
check("非会员 PlaybackInfo 被付费墙拦截 403", r.status_code == 403, f"status={r.status_code}")
check("非会员提示可读（说明要开会员）",
      r.status_code == 403 and "会员" in str(r.json().get("detail", "")), f"{r.json()}")

r = client.get(f"/emby/Videos/{PLAYABLE}/stream", headers=H_EMBY)
check("非会员直连流也被拦截 403", r.status_code == 403, f"status={r.status_code}")

# ==================== 2. 开通会员后链路打通 ====================

with SessionLocal() as db:
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    codes.grant_membership_days(db, user, 30)
    # v2.38.0 起 grant_membership_days 不再自己提交：消耗与发奖同一个事务，由调用方提交
    db.commit()

r = client.get("/api/user/auth/me", headers=H)
check("开通后 me.is_vip 为 True（由订阅派生）", r.json().get("is_vip") is True, f"{r.json().get('is_vip')}")

r = client.post(f"/emby/Items/{PLAYABLE}/PlaybackInfo", json={}, headers=H_EMBY)
body = r.json() if r.status_code == 200 else {}
check("会员 PlaybackInfo 返回 200", r.status_code == 200, f"status={r.status_code}")
check("PlaybackInfo 带可播放来源", bool(body.get("MediaSources")), f"keys={list(body)[:5]}")
check("来源带直链地址",
      bool((body.get("MediaSources") or [{}])[0].get("DirectStreamUrl")
           or (body.get("MediaSources") or [{}])[0].get("Path")),
      f"{(body.get('MediaSources') or [{}])[0]}")

r = client.get(f"/emby/Videos/{PLAYABLE}/stream", headers=H_EMBY)
check("会员直连流返回 200", r.status_code == 200, f"status={r.status_code}")
check("直连流返回真实字节", len(r.content) > 0, f"bytes={len(r.content)}")

r = client.get(f"/emby/Videos/{PLAYABLE}/stream", headers={**H_EMBY, "Range": "bytes=0-9"})
check("Range 请求返回 206（播放器拖进度条依赖）", r.status_code == 206, f"status={r.status_code}")
check("Range 只返回 10 字节", len(r.content) == 10, f"bytes={len(r.content)}")

# ==================== 3. 没有媒体路径的条目：404 而不是 500 ====================

r = client.post(f"/emby/Items/{NO_PATH}/PlaybackInfo", json={}, headers=H_EMBY)
check("无路径条目 PlaybackInfo 不报 500", r.status_code == 200, f"status={r.status_code}")
check("无路径条目不发放必 404 的播放地址（MediaSources 为空）",
      r.status_code == 200 and r.json().get("MediaSources") == [], f"{str(r.json())[:80]}")

try:
    r = client.get(f"/emby/Videos/{NO_PATH}/stream", headers=H_EMBY)
    ok = r.status_code == 404
    detail = f"status={r.status_code}"
except Exception as exc:  # noqa: BLE001 — 未捕获异常就是 500 的表现
    ok, detail = False, f"抛异常 {type(exc).__name__}: {exc}"
check("无路径条目直连流返回 404（不是 500 / 不抛异常）", ok, detail)

# ==================== 4. 互动链路落库 ====================

r = client.post(f"/emby/Users/me/Items/{PLAYABLE}/Rating", json={"IsFavorite": True}, headers=H_EMBY)
check("收藏成功", r.status_code == 200 and r.json().get("IsFavorite") is True, f"{r.status_code}")

r = client.post(f"/emby/Users/me/PlayedItems/{PLAYABLE}", headers=H_EMBY)
check("标记已看成功", r.status_code == 200 and r.json().get("Played") is True, f"{r.status_code}")

r = client.delete(f"/emby/Users/me/PlayedItems/{PLAYABLE}", headers=H_EMBY)
check("取消已看成功", r.status_code == 200 and r.json().get("Played") is False, f"{r.status_code}")

with SessionLocal() as db:
    item_id = db.query(em.MediaItem.id).filter(em.MediaItem.guid == PLAYABLE).scalar()
    umd = db.query(em.UserMediaData).filter(em.UserMediaData.item_id == item_id).first()
    check("互动状态落库正确",
          umd is not None and umd.is_favorite is True and umd.played is False,
          f"is_favorite={umd and umd.is_favorite} played={umd and umd.played}")

# ==================== 汇总 ====================

print()
if failures:
    print(f"FAILED  {len(failures)}/{TOTAL}：")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL PASS  {TOTAL}/{TOTAL}")

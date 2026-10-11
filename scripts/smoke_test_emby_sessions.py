"""自建 Emby 会话端点（/Sessions）鉴权与会话键冒烟测试

用临时 SQLite 库 + 真实 app（含协议面与两处 installer），不碰开发库、不访问外部服务。
覆盖点：

- 匿名访问 ``GET /Sessions`` / ``GET /emby/Sessions`` 与两条 DELETE 一律 401，
  并且**真的没有**把别人的会话结束掉（旧实现是匿名 200 直接踢人）；
- 普通用户只能列出自己的会话，删自己的可以，删别人的 403，不存在的键 404；
- 管理员（is_staff）可列出全站会话、可结束任意会话；
- 会话键不再可预测：没带 PlaySessionId 时按「用户 | 设备 | 条目」HMAC 派生稳定键
  （``s<32位hex>``，无服务端密钥不可预测），
  非法键与「被别人占用的键」都会被换成派生键，且不会改写别人的会话行；
- 同一设备同一条目的多次上报（Playing / Progress / Stopped）复用同一行，
  不再每报新建一行（旧行为会刷爆会话表，还会让同播检测把正常播放自己 DoS 掉）；
- 上报路由（Playing / Progress）仍然走 api.py 的原逻辑（进度、已看状态照常落库）。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["EMBY_PUBLIC_URL"] = "http://media.example.com:8000"
os.environ["SECRET_KEY"] = "e2e-test-secret-key-not-for-production"

DB = os.path.join(tempfile.mkdtemp(), "emby-sessions.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from fastapi.testclient import TestClient  # noqa: E402

from backend import models  # noqa: E402
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


# ==================== 播种数据 ====================
ITEM_GUID = "0123456789abcdef0123456789abcdef"  # 32 位，与扫描器生成的 guid 同形
ALICE_KEY = "sess-alice"
BOB_KEY = "sess-bob"

with SessionLocal() as db:
    alice = models.WebUser(username="sess-alice", password_hash="not-used", is_active=True)
    bob = models.WebUser(username="sess-bob", password_hash="not-used", is_active=True)
    staff = models.WebUser(username="sess-staff", password_hash="not-used", is_active=True, is_staff=True, admin_role="super")
    db.add_all([alice, bob, staff])
    db.commit()
    for u in (alice, bob, staff):
        db.refresh(u)

    lib = em.Library(guid="lib-sessions", name="会话测试库", collection_type="movies", paths="/tmp")
    db.add(lib)
    db.commit()
    db.refresh(lib)

    movie = em.MediaItem(
        guid=ITEM_GUID, library_id=lib.id, item_type="movie",
        name="会话测试片", duration_ticks=60_000_000_000,
    )
    db.add(movie)
    db.commit()
    db.refresh(movie)

    for u in (alice, bob, staff):
        db.add(em.EmbyApiToken(token=hash_emby_token(f"tok-{u.username}"), user_id=u.id, device_id=f"dev-{u.username}"))
    db.add_all([
        em.PlaybackSession(session_key=ALICE_KEY, user_id=alice.id, item_id=movie.id),
        em.PlaybackSession(session_key=BOB_KEY, user_id=bob.id, item_id=movie.id),
    ])
    db.commit()

    alice_id, bob_id, staff_id, movie_id = alice.id, bob.id, staff.id, movie.id

H_ALICE = {"X-Emby-Token": "tok-sess-alice"}
H_BOB = {"X-Emby-Token": "tok-sess-bob"}
H_STAFF = {"X-Emby-Token": "tok-sess-staff"}
H_FAKE = {"X-Emby-Token": "deadbeef-not-a-token"}


def session_row(key: str):
    with SessionLocal() as db:
        return (
            db.query(em.PlaybackSession)
            .filter(em.PlaybackSession.session_key == key)
            .first()
        )


def list_keys(resp) -> set:
    return {s.get("Id") for s in resp.json()}


# ==================== 1. 匿名一律 401 ====================
for method, path in (
    ("get", "/emby/Sessions"),
    ("get", "/Sessions"),
    ("delete", f"/emby/Sessions/{ALICE_KEY}"),
    ("delete", f"/Sessions/{ALICE_KEY}"),
):
    r = getattr(client, method)(path)
    check(f"匿名 {method.upper()} {path} → 401", r.status_code == 401, f"实际 {r.status_code}")

check("匿名 DELETE 没有结束 alice 的会话", session_row(ALICE_KEY).ended_at is None)

r = client.get("/Sessions", headers=H_FAKE)
check("伪造 token → 401", r.status_code == 401, f"实际 {r.status_code}")

# ==================== 2. 普通用户：只看/只能停自己 ====================
r = client.get("/emby/Sessions", headers=H_ALICE)
check("alice GET /emby/Sessions → 200", r.status_code == 200, f"实际 {r.status_code}")
check("alice 只能看到自己的会话", list_keys(r) == {ALICE_KEY}, f"实际 {sorted(list_keys(r))}")

r = client.get(f"/Sessions?api_key=tok-sess-alice")
check("query api_key 传 token 也能用（客户端写法）", r.status_code == 200 and list_keys(r) == {ALICE_KEY},
      f"实际 {r.status_code}")

r = client.delete(f"/Sessions/{BOB_KEY}", headers=H_ALICE)
check("alice 删 bob 的会话 → 403", r.status_code == 403, f"实际 {r.status_code}")
check("bob 的会话没有被 alice 结束", session_row(BOB_KEY).ended_at is None)

r = client.delete("/emby/Sessions/no-such-session", headers=H_ALICE)
check("删不存在的会话 → 404", r.status_code == 404, f"实际 {r.status_code}")

r = client.delete(f"/emby/Sessions/{ALICE_KEY}", headers=H_ALICE)
check("alice 删自己的会话 → 200", r.status_code == 200, f"实际 {r.status_code} {r.text[:120]}")
check("alice 的会话已标记结束", session_row(ALICE_KEY).ended_at is not None)

r = client.get("/Sessions", headers=H_ALICE)
check("结束后不在列表里（新键不属于它）", ALICE_KEY not in list_keys(r), f"实际 {sorted(list_keys(r))}")

# ==================== 3. 管理员：全站可见可停 ====================
r = client.get("/Sessions", headers=H_STAFF)
check("staff GET /Sessions → 200", r.status_code == 200, f"实际 {r.status_code}")
check("staff 能看到别人的会话", BOB_KEY in list_keys(r), f"实际 {sorted(list_keys(r))}")

r = client.delete(f"/emby/Sessions/{BOB_KEY}", headers=H_STAFF)
check("staff 删任意会话 → 200", r.status_code == 200, f"实际 {r.status_code}")
check("bob 的会话已被 staff 结束", session_row(BOB_KEY).ended_at is not None)

# ==================== 4. 会话键不再可预测 ====================
PREDICTABLE_RE = re.compile(r"^s[A-Za-z0-9_\-]{20,}$")


def playing(user_id: int, headers: dict, body: dict) -> dict:
    r = client.post("/emby/Sessions/Playing", headers=headers, json=body)
    check(f"上报 Playing → 200（{body.get('PlaySessionId', '无 PlaySessionId')}）",
          r.status_code == 200, f"实际 {r.status_code} {r.text[:120]}")
    with SessionLocal() as db:
        rows = (
            db.query(em.PlaybackSession)
            .filter(em.PlaybackSession.user_id == user_id)
            .order_by(em.PlaybackSession.id.desc())
            .all()
        )
    return rows[0]


before_ids = set()
with SessionLocal() as db:
    before_ids = {s.id for s in db.query(em.PlaybackSession).all()}

row = playing(alice_id, H_ALICE, {"ItemId": ITEM_GUID})
check("未带 PlaySessionId 时会话键不可预测（派生键，非旧的可算出键）",
      bool(PREDICTABLE_RE.match(row.session_key or "")),
      f"实际 {row.session_key}")
check("旧的可预测回退键已不复存在",
      row.session_key != f"{alice_id}-{ITEM_GUID[:16]}" and ITEM_GUID[:16] not in (row.session_key or ""),
      f"实际 {row.session_key}")

row2 = playing(alice_id, H_ALICE, {"ItemId": ITEM_GUID})
check("同一设备再次上报复用同一行、同一会话键（不再每报新建一行）",
      row2.id == row.id and row2.session_key == row.session_key,
      f"{row.session_key} vs {row2.session_key}")
check("复用的键仍不可预测（不是旧的可算出回退键）",
      row2.session_key != f"{alice_id}-{ITEM_GUID[:16]}",
      f"实际 {row2.session_key}")

row3 = playing(bob_id, H_BOB, {"ItemId": ITEM_GUID, "PlaySessionId": "/etc/passwd"})
check("非法 PlaySessionId 被换成不可预测的派生键", bool(PREDICTABLE_RE.match(row3.session_key or "")) and "/" not in row3.session_key,
      f"实际 {row3.session_key}")

alice_before = session_row(ALICE_KEY)
row4 = playing(bob_id, H_BOB, {"ItemId": ITEM_GUID, "PlaySessionId": ALICE_KEY})
check("别的用户占用同键时不会改写对方会话（bob 拿到自己的新键）", row4.session_key != ALICE_KEY,
      f"实际 {row4.session_key}")
with SessionLocal() as db:
    alice_after = (
        db.query(em.PlaybackSession).filter(em.PlaybackSession.session_key == ALICE_KEY).first()
    )
check("alice 的会话行没被 bob 的上报污染",
      alice_after is not None
      and alice_after.user_id == alice_id
      and alice_after.item_id == movie_id
      and alice_after.ended_at == alice_before.ended_at)

row5 = playing(alice_id, H_ALICE, {"ItemId": ITEM_GUID, "PlaySessionId": "alice-own-key"})
row6 = playing(alice_id, H_ALICE, {"ItemId": ITEM_GUID, "PlaySessionId": "alice-own-key"})
check("本人重复用同一个 PlaySessionId 时复用同一条会话", row5.id == row6.id,
      f"{row5.id} vs {row6.id}")

r = client.delete("/emby/Sessions/alice-own-key", headers=H_ALICE)
check("自定义 PlaySessionId 的会话也能按该键结束", r.status_code == 200, f"实际 {r.status_code}")
check("该会话已结束", session_row("alice-own-key").ended_at is not None)

# ==================== 5. 上报路由仍走原逻辑（进度落库） ====================
r = client.post("/emby/Sessions/Playing/Progress", headers=H_ALICE,
                json={"ItemId": ITEM_GUID, "PositionTicks": 5_000_000})
check("Progress 上报 → 200", r.status_code == 200, f"实际 {r.status_code}")
with SessionLocal() as db:
    umd = (
        db.query(em.UserMediaData)
        .filter(em.UserMediaData.user_id == alice_id, em.UserMediaData.item_id == movie_id)
        .first()
    )
check("Progress 上报照旧写入用户媒体数据（未被鉴权包装破坏）",
      umd is not None and (umd.playback_position_ticks or 0) == 5_000_000,
      f"实际 {getattr(umd, 'playback_position_ticks', None)}")

r = client.post("/emby/Sessions/Playing/Stopped", headers=H_ALICE,
                json={"ItemId": ITEM_GUID, "PositionTicks": 6_000_000})
check("Stopped 上报 → 200", r.status_code == 200, f"实际 {r.status_code}")

# ==================== 6. 进度上报的写库节流（v2.14.0） ====================
# 客户端每 10s 一报（暂停、卡顿也照报），没有新信息的上报不该每次都写库；
# 但拖动（位置跳变）与暂停切换必须立刻落库，客户端“继续观看”也不能因此丢失。

import backend.emby_server.compat_routes as compat_routes  # noqa: E402

THROTTLE_KEY = "alice-throttle-key"
r = client.post("/emby/Sessions/Playing", headers=H_ALICE,
                json={"ItemId": ITEM_GUID, "PlaySessionId": THROTTLE_KEY, "PositionTicks": 0})
check("起播上报（新会话）→ 200", r.status_code == 200, f"实际 {r.status_code}")
start = session_row(THROTTLE_KEY)
check("新会话一定落库（列表里能看到）", start is not None)

r = client.post("/emby/Sessions/Playing/Progress", headers=H_ALICE,
                json={"ItemId": ITEM_GUID, "PlaySessionId": THROTTLE_KEY, "PositionTicks": 0})
same = session_row(THROTTLE_KEY)
check("无新信息的重复上报不写库（心跳时间不变）",
      r.status_code == 200 and same.last_update_at == start.last_update_at,
      f"{start.last_update_at} → {same.last_update_at}")

r = client.post("/emby/Sessions/Playing/Progress", headers=H_ALICE,
                json={"ItemId": ITEM_GUID, "PlaySessionId": THROTTLE_KEY,
                      "PositionTicks": 300_000_000})
seek = session_row(THROTTLE_KEY)
check("拖动进度（跳变 ≥ 30s）立刻落库",
      seek.position_ticks == 300_000_000 and seek.last_update_at > start.last_update_at,
      f"pos={seek.position_ticks} at={seek.last_update_at}")

r = client.post("/emby/Sessions/Playing/Progress", headers=H_ALICE,
                json={"ItemId": ITEM_GUID, "PlaySessionId": THROTTLE_KEY,
                      "PositionTicks": 300_000_000, "IsPaused": True})
paused = session_row(THROTTLE_KEY)
check("暂停切换立刻落库（面板上的暂停状态不滞后）",
      paused.is_paused is True and paused.last_update_at > seek.last_update_at,
      f"paused={paused.is_paused} at={paused.last_update_at}")

# 观看进度（用户媒体数据）不受节流影响：被节流的那次上报也要把位置记下来
with SessionLocal() as db:
    umd = (
        db.query(em.UserMediaData)
        .filter(em.UserMediaData.user_id == alice_id, em.UserMediaData.item_id == movie_id)
        .first()
    )
check("被节流的上报仍然记下了观看进度",
      umd is not None and (umd.playback_position_ticks or 0) == 300_000_000,
      f"实际 {getattr(umd, 'playback_position_ticks', None)}")

# 关掉节流（=0）时回到旧行为：每报必写（这里用 10s 的小跳变，默认口径下会被节流）
try:
    compat_routes.PROGRESS_WRITE_SECONDS = 0
    r = client.post("/emby/Sessions/Playing/Progress", headers=H_ALICE,
                    json={"ItemId": ITEM_GUID, "PlaySessionId": THROTTLE_KEY,
                          "PositionTicks": 310_000_000})
    again = session_row(THROTTLE_KEY)
    check("EMBY_PROGRESS_WRITE_SECONDS=0 时每报必写（旧行为可回退）",
          again.position_ticks == 310_000_000 and again.last_update_at > paused.last_update_at,
          f"pos={again.position_ticks} at={again.last_update_at}")
finally:
    compat_routes.PROGRESS_WRITE_SECONDS = 15

print()
if failures:
    print(f"{len(failures)}/{TOTAL} 项失败：{', '.join(failures)}")
    sys.exit(1)
print(f"ALL PASS（{TOTAL} 项）")

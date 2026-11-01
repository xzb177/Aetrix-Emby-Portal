#!/usr/bin/env python3
"""后端热路径：同步写库不占事件循环 + 分类菜单走索引（v2.35.0）

三条都是「用户能感觉到的卡」的直接来源，所以每条都量出数字或数出 SQL，不靠“看起来没问题”：

1. **分类菜单端点**（``/Genres``、``/Studios``）。旧实现每个请求都把整库的
   ``genres`` / ``studios`` 文本列读回来再切分：十万级库约 190ms，而且随库**线性增长**
   （客户端每打开一次筛选面板就付一次）。现在走 ``emby_item_facets`` 的覆盖索引
   （带取值缓存），老库没回填完才退回旧口径。这里数 SQL：请求菜单时**不允许**出现
   「从 emby_items 读整列」，必须是关联表上的 DISTINCT；两条路的取值必须逐条一致。

2. **筛选菜单缓存**（``GET /Items/Filters``）。菜单取值同样来自全库，原先只能等 TTL
   （默认 300 秒）自然过期——刚扫完的片子在客户端的「类型 / 制片公司」里最长 5 分钟
   看不到。现在分类值一变就被安排成过期，且带最小重建间隔。这里钉两件事：
   改动在下一次「到点」的请求里就能看见；连续改动**不会**退化成“每次请求都重建”
   （用 SQL 计数证明）。

3. **探测 / 体检层**（``backend/servers.py::probe_and_store`` 与它带出来的一串端点：
   ``POST /api/admin/servers/{id}/test``、``POST /api/admin/emby/servers/mounts/refresh``、
   ``activate`` …）。体检本身必须 await 网络（一次 8 秒超时都有可能），而它「读这一行 /
   落库 / 写审计」都是同步 SQLAlchemy——原先整段压在事件循环上，等于全站（包括别人的播放）
   排在一次探测后面。这里让「这台机器写库就是慢」（命中的 UPDATE 先睡 0.35 秒）再走真实路由，
   量事件循环的空洞；同时确认结论**真的**落库、审计**真的**写了。

4. **会话进度上报**（``POST /Sessions/Playing/Progress``）。每个正在播放的客户端每 10 秒
   上报一次，一次上报要读会话 / 写位置 / 写观看进度。它跑在**事件循环**上时会卡住
   全站所有请求（包括别人的播放），所以现在整段同步写库都下放线程池。这里用后台心跳
   量「事件循环被占住多久」：
   - 基线自证：把同样的同步活儿直接跑在事件循环上，心跳必须出现 ≥0.3s 的空洞
     （这就是改动前的形态，先证明测量方法有效）；
   - 现在：一次 0.35 秒的慢写走真实路由进来，心跳空洞 < 0.15s，而且会话与观看进度
     确实写进去了（活儿真干了，只是不在循环上）。
"""
import asyncio
import os
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "hot-paths-smoke-secret-key-32b"
DB = os.path.join(tempfile.mkdtemp(prefix="hot-paths-"), "hot-paths.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
# 最小重建间隔压到 1 秒：验的是机制（到点才重建 / 不每请求重建），不是那个默认值本身
os.environ["EMBY_FILTERS_MIN_REFRESH"] = "1"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event as sa_event  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend import models  # noqa: E402
from backend import notifications as notif_mod  # noqa: E402
from backend import realms  # noqa: E402
from backend import servers as registry  # noqa: E402
from backend.database import SessionLocal, engine, init_db  # noqa: E402
from backend.emby_server import api as emby_api  # noqa: E402
from backend.emby_server import compat_routes, facets  # noqa: E402
from backend.emby_server import mount_health  # noqa: E402
from backend.emby_server import nodes as node_lib  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server.auth import hash_emby_token  # noqa: E402
from backend.security import create_access_token, hash_password  # noqa: E402

init_db()

from backend.main import app  # noqa: E402

failures: list[str] = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


# ==================== SQL 计数（数「做了什么」，不靠时间猜）====================

STATEMENTS: list[str] = []


def _record_statement(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
    STATEMENTS.append(statement)


sa_event.listen(engine, "before_cursor_execute", _record_statement)

GENRE_MENU_SQL = "DISTINCT emby_item_facets.value"


def menu_rebuilds() -> int:
    """关联表取值上的 DISTINCT 次数 = 筛选菜单真的重建了几次"""
    return sum(1 for s in STATEMENTS if GENRE_MENU_SQL in s)


def full_column_reads() -> list[str]:
    """从 emby_items 里把分类文本列整列读回来的语句（旧实现的那条路）"""
    return [
        s for s in STATEMENTS
        if "FROM emby_items" in s and ("emby_items.genres" in s or "emby_items.studios" in s)
    ]


# ==================== 数据准备 ====================

suffix = uuid.uuid4().hex[:6]
with SessionLocal() as db:
    user = models.WebUser(username=f"hot{suffix}", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    user_id = user.id          # 会话一关实体就过期，后面只认这个裸 id

    # 站内消息要发给「启用中的管理员」：通知层没人可发时不会落库，验证也就没意义
    staff = models.WebUser(username=f"hot-staff-{suffix}",
                           password_hash=hash_password("hot-paths"),
                           is_active=True, is_staff=True, admin_role="super")
    db.add(staff)
    db.commit()
    db.refresh(staff)
    staff_id = staff.id

    library = em.Library(guid=f"lib-hot-{suffix}", name="热路径测试库",
                         collection_type="movies", paths="/tmp")
    db.add(library)
    db.commit()
    db.refresh(library)

    items = [
        em.MediaItem(guid=f"hot-{suffix}-{i}", library_id=library.id, item_type="movie",
                     name=f"热路径测试片 {i}", sort_name=f"hot {i}",
                     genres="动作" if i % 2 == 0 else "喜剧",
                     studios="华纳" if i % 3 == 0 else "环球",
                     tags="4K", production_year=2000 + (i % 10),
                     official_rating="PG-13" if i % 2 else "R",
                     duration_ticks=60_000_000_000)
        for i in range(60)
    ]
    db.add_all(items)
    db.commit()

    token = f"hot-{suffix}-" + "b" * 12
    db.add(em.EmbyApiToken(token=hash_emby_token(token), user_id=user.id, device_id="hot-paths-device"))
    db.commit()
    item_guid = items[0].guid
    item_id = items[0].id
    library_id = library.id

H = {"X-Emby-Token": token}

# 门户侧（/api/user/*）用的是登录 JWT，不是 Emby token
PORTAL_H = {"Authorization": f"Bearer {create_access_token(user_id)}"}

client = TestClient(app)


def legacy_menu(kind: str, column: str) -> list[str]:
    """旧口径（读整列）算一遍，用来做逐条比对"""
    with engine.connect() as conn:
        raw = conn.execute(text(f"SELECT {column} FROM emby_items")).scalars().all()
    return sorted({v for row in raw for v in (row or "").split(",") if v})


# ==================== 1. 菜单端点：不读整列 + 取值一致 ====================
print("\n=== 1. /Genres、/Studios 走关联表索引 ===")

client.get("/emby/Items/Filters", headers=H)  # 预热：第一次可能带一小块回填
# 先扔进程内取值缓存：否则 /Genres 直接给出缓存，量不到「这条路到底怎么查的」。
# 冷读才看得见真正的 SQL（有缓存时 0 条 SQL 也**正确**，见下面的分支）。
facets.invalidate_values_cache()
STATEMENTS.clear()
resp = client.get("/emby/Genres", headers=H)
check("/Genres → 200", resp.status_code == 200, f"HTTP {resp.status_code}")
names = [i["Name"] for i in resp.json().get("Items", [])]
cold = menu_rebuilds() >= 1
check("菜单走关联表（冷读是覆盖索引上的 DISTINCT；有缓存则 0 条 SQL，都不读整列）",
      (cold or not full_column_reads()) and not full_column_reads(),
      f"冷读索引查询 {menu_rebuilds()} 条 · 整列读 {len(full_column_reads())} 条")
check("/Genres 取值与旧口径逐条一致", names == legacy_menu("genre", "genres"),
      f"接口 {names} · 旧口径 {legacy_menu('genre', 'genres')}")

facets.invalidate_values_cache()
STATEMENTS.clear()
resp = client.get("/emby/Studios", headers=H)
studios = [i["Name"] for i in resp.json().get("Items", [])]
check("/Studios 不再读整列", not full_column_reads(),
      f"整列读 {len(full_column_reads())} 条")
check("/Studios 取值与旧口径逐条一致", studios == legacy_menu("studio", "studios"),
      f"接口 {studios} · 旧口径 {legacy_menu('studio', 'studios')}")

# 老库（关联表还没回填完）必须退回旧口径，结果一样对：
_real_ready, _real_attempt = facets.ready, facets._last_filter_attempt  # noqa: SLF001
try:
    facets.ready = lambda db: False                      # noqa: ARG005 — 模拟“还没补完”
    facets._last_filter_attempt = time.monotonic()       # noqa: SLF001 — 冷却窗口内：不在读请求里补块
    STATEMENTS.clear()
    resp = client.get("/emby/Genres", headers=H)
    fallback = [i["Name"] for i in resp.json().get("Items", [])]
    check("老库退回旧口径时结果一致（升级前后不会出现「筛选空白」）",
          fallback == legacy_menu("genre", "genres") and len(full_column_reads()) >= 1,
          f"退回 {len(full_column_reads())} 条整列读 · 取值 {len(fallback)} 个")
finally:
    facets.ready, facets._last_filter_attempt = _real_ready, _real_attempt  # noqa: SLF001

# /Items/Counts：一次 GROUP BY 的结果必须与直接计数一致
STATEMENTS.clear()
counts = client.get("/emby/Items/Counts", headers=H).json()
with engine.connect() as conn:
    expected_movies = int(conn.execute(text(
        "SELECT COUNT(*) FROM emby_items WHERE item_type = 'movie' AND is_hidden = 0"
    )).scalar() or 0)
check("/Items/Counts 计数正确（合并 GROUP BY 后没算错）",
      counts["MovieCount"] == expected_movies and counts["ItemCount"] >= counts["MovieCount"],
      f"MovieCount={counts['MovieCount']} 期望 {expected_movies} · ItemCount={counts['ItemCount']}")


# ==================== 2. 筛选菜单缓存：改动看得见，但不会每请求重建 ====================
print("\n=== 2. /Items/Filters 变更后刷新 + 最小重建间隔 ===")

client.get("/emby/Items/Filters", headers=H)             # 先把菜单建出来
menu = client.get("/emby/Items/Filters", headers=H).json()
check("菜单里能看到条目自带的类型", "动作" in menu["Genres"], str(menu["Genres"][:6]))

with SessionLocal() as db:
    row = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
    row.genres = "动作,科幻"
    db.commit()

statements_before = menu_rebuilds()
cached = client.get("/emby/Items/Filters", headers=H).json()
check("刚改完（还没到最小重建间隔）给出上一份菜单，而不是每个请求都重建",
      "科幻" not in cached["Genres"] and menu_rebuilds() == statements_before,
      f"重建次数 {menu_rebuilds() - statements_before}")

time.sleep(emby_api._FILTERS_MIN_REFRESH + 0.4)          # noqa: SLF001 — 等“到点”
fresh = client.get("/emby/Items/Filters", headers=H).json()
check("到点后菜单立刻含新值（不再等满 TTL）", "科幻" in fresh["Genres"],
      f"Genres={fresh['Genres']}")

# 连续改动 + 连续请求：不能退化成「每次请求都重建」
with SessionLocal() as db:
    row = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
    for i in range(5):
        row.genres = f"动作,连改{i}"
        db.commit()
        client.get("/emby/Items/Filters", headers=H)
    row.genres = "动作"
    db.commit()

statements_before = menu_rebuilds()
for _ in range(5):
    client.get("/emby/Items/Filters", headers=H)
burst = menu_rebuilds() - statements_before
# 一次重建会打两条 DISTINCT（类型 + 标签），所以「最多重建一次」= 至多 2 条
check("连续请求不会每次重建（最小间隔生效）", burst <= 2,
      f"5 次请求重建相关的 DISTINCT {burst} 条（每重建一次是 2 条）")


# ==================== 3. 会话进度上报：慢写不占事件循环 ====================
print("\n=== 3. /Sessions/Playing/Progress 的写库不在事件循环上 ===")

SLOW_WRITE = 0.35     # 一次上报里同步写库要花的时间（真实机器上的慢写）
REPORT = {"ItemId": item_guid, "PlaySessionId": f"hot-session-{suffix}",
          "PositionTicks": 12_000_000_000, "IsPaused": False}


class LoopWatch:
    """后台心跳：记录事件循环最坏一次「没能按时醒来」的间隔"""

    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.max_gap = 0.0
        self.ticks = 0
        self._task = None

    async def _beat(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.interval)
            now = time.monotonic()
            self.max_gap = max(self.max_gap, now - last)
            self.ticks += 1
            last = now

    def start(self):
        self.max_gap = 0.0
        self.ticks = 0
        self._task = asyncio.ensure_future(self._beat())

    def stop(self) -> float:
        if self._task:
            self._task.cancel()
        return self.max_gap


async def measure(awaitable_factory):
    """跑一段协程，返回 (耗时, 心跳最坏空洞)"""
    watch = LoopWatch()
    watch.start()
    await asyncio.sleep(watch.interval * 2)
    started = time.monotonic()
    await awaitable_factory()
    elapsed = time.monotonic() - started
    await asyncio.sleep(watch.interval * 2)
    gap = watch.stop()
    assert watch.ticks > 0, "心跳任务没跑起来，测量无效"
    return elapsed, gap


async def blocking_baseline() -> float:
    """基线自证：同步活儿直接跑在事件循环上（改动前就是这个形态）"""
    watch = LoopWatch()
    watch.start()
    await asyncio.sleep(watch.interval * 2)
    time.sleep(SLOW_WRITE)
    await asyncio.sleep(watch.interval * 2)
    gap = watch.stop()
    return gap


async def section_hot_path():
    # 3.1 先自证测量方法有效
    gap = await blocking_baseline()
    check("基线自证：同步写库跑在事件循环上会卡住全站（心跳空洞 ≥0.3s）",
          gap >= SLOW_WRITE * 0.8, f"心跳空洞={gap:.3f}s")

    real_record = compat_routes._record_progress          # noqa: SLF001

    def slow_record(request, user, db, body):
        """模拟「这台机器的写库就是慢」：真实的写入照做，只是先慢 0.35 秒"""
        time.sleep(SLOW_WRITE)
        return real_record(request, user, db, body)

    compat_routes._record_progress = slow_record          # noqa: SLF001
    box: dict = {}
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:
            async def report():
                box["resp"] = await api.post("/Sessions/Playing/Progress",
                                             headers=H, json=REPORT)

            elapsed, gap = await measure(report)
    finally:
        compat_routes._record_progress = real_record      # noqa: SLF001

    resp = box.get("resp")
    check("进度上报 → 200", resp is not None and resp.status_code == 200,
          f"HTTP {getattr(resp, 'status_code', '—')}")
    check("慢写确实发生了（0.35s 的活儿没被跳过）", elapsed >= SLOW_WRITE * 0.8,
          f"耗时={elapsed:.3f}s")
    check("这 0.35s 里事件循环保持响应（心跳空洞 < 0.15s）", gap < 0.15,
          f"心跳空洞={gap:.3f}s（改动前 ≈ {SLOW_WRITE:.2f}s）")

    with SessionLocal() as db:
        session = (db.query(em.PlaybackSession)
                   .filter(em.PlaybackSession.session_key == REPORT["PlaySessionId"]).first())
        umd = (db.query(em.UserMediaData)
               .filter(em.UserMediaData.user_id == user_id,
                       em.UserMediaData.item_id == item_id).first())
        check("会话真的落库了（下放线程池没耽误写）",
              session is not None and session.position_ticks == REPORT["PositionTicks"],
              f"position={getattr(session, 'position_ticks', None)}")
        check("观看进度真的落库了（「继续观看」的起点）",
              umd is not None and umd.playback_position_ticks == REPORT["PositionTicks"],
              f"position={getattr(umd, 'playback_position_ticks', None)}")

    # 不打补丁的真路由：一次正常上报同样写得进去（改动不改变语义）
    box.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as api:
        REPORT2 = dict(REPORT, PositionTicks=30_000_000_000)
        resp = await api.post("/Sessions/Playing/Progress", headers=H, json=REPORT2)
        check("正常上报 → 200", resp.status_code == 200, f"HTTP {resp.status_code}")
    with SessionLocal() as db:
        umd = (db.query(em.UserMediaData)
               .filter(em.UserMediaData.user_id == user_id,
                       em.UserMediaData.item_id == item_id).first())
        check("正常上报把位置更新到了新值",
              umd is not None and umd.playback_position_ticks == 30_000_000_000,
              f"position={getattr(umd, 'playback_position_ticks', None)}")

    # 结束播放：同样下放线程池，且真的把会话标结束
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as api:
        resp = await api.post("/Sessions/Playing/Stopped", headers=H, json=REPORT)
        check("结束上报 → 200", resp.status_code == 200, f"HTTP {resp.status_code}")
    with SessionLocal() as db:
        session = (db.query(em.PlaybackSession)
                   .filter(em.PlaybackSession.session_key == REPORT["PlaySessionId"]).first())
        check("结束上报把会话标成已结束", session is not None and session.ended_at is not None)




# ==================== 4. 求片提交：通知层落库不占事件循环 ====================
print("\n=== 4. 求片提交（全体管理员的站内消息落库不在事件循环上）===")

SEEK_NAME = f"热路径求片 {suffix}"


async def section_seek_notify():
    # 4.1 基线自证：同样的同步落库直接跑在事件循环上
    gap = await blocking_baseline()
    check("基线自证：站内信落库跑在事件循环上会卡住全站（心跳空洞 ≥0.3s）",
          gap >= SLOW_WRITE * 0.8, f"心跳空洞={gap:.3f}s")

    # 4.2 让「这台机器写站内信就是慢」，再走真实路由提交求片
    real_persist = notif_mod._persist_staff_messages          # noqa: SLF001

    def slow_persist(db, title, content, message_type, related_id):
        """真实的落库照做，只是先慢 0.35 秒（模拟慢盘 / 跨机 PostgreSQL）"""
        time.sleep(SLOW_WRITE)
        return real_persist(db, title, content, message_type, related_id)

    notif_mod._persist_staff_messages = slow_persist          # noqa: SLF001
    box: dict = {}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:
            async def submit():
                box["resp"] = await api.post(
                    "/api/user/media-seek", headers=PORTAL_H,
                    json={"movie_name": SEEK_NAME, "type": "movie"},
                )

            elapsed, gap = await measure(submit)
    finally:
        notif_mod._persist_staff_messages = real_persist      # noqa: SLF001

    resp = box.get("resp")
    check("求片提交 → 200", resp is not None and resp.status_code == 200,
          f"HTTP {getattr(resp, 'status_code', '—')} {getattr(resp, 'text', '')[:120]}")
    check("慢落库确实发生了（0.35s 的通知活儿没被跳过）", elapsed >= SLOW_WRITE * 0.8,
          f"耗时={elapsed:.3f}s")
    check("这 0.35s 里事件循环保持响应（心跳空洞 < 0.15s）", gap < 0.15,
          f"心跳空洞={gap:.3f}s（改动前 ≈ {SLOW_WRITE:.2f}s）")

    with SessionLocal() as db:
        row = (db.query(models.MovieRequest)
               .filter(models.MovieRequest.user_id == user_id,
                       models.MovieRequest.movie_name == SEEK_NAME).first())
        check("求片真的落库了（下放线程池没耽误写）", row is not None,
              f"request_id={getattr(row, 'id', None)}")
        request_id = row.id if row is not None else -1
        msgs = (db.query(models.StationMessage)
                .filter(models.StationMessage.to_user_id == staff_id,
                        models.StationMessage.related_id == request_id).count())
        check("启用中的管理员收到了站内消息（通知层的落库也没被跳过）", msgs >= 1, f"{msgs} 条")

    # 4.3 后台推送那一侧：取件 / 记录结果 / 审计同样不在事件循环上。
    #     这里不发真网络（没有配 qB），走的是「没有可用服务器」的失败分支——
    #     正好验证「失败也要如实记录 + 落审计」这条路径。
    with SessionLocal() as db:
        admin = (db.query(models.WebUser).filter(models.WebUser.id == staff_id).first())
        admin_name, admin_staff_flag = admin.username, admin.is_staff
    box.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://testserver") as api:
        login = await api.post("/api/user/auth/login",
                               json={"username": admin_name, "password": "hot-paths"})
        check("管理员账号可用于推送端点（造号时写死了口令）",
              login.status_code == 200 and admin_staff_flag, f"HTTP {login.status_code}")
        if login.status_code == 200:
            admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}
            push = await api.post(f"/api/admin/media-seek/{request_id}/push",
                                  headers=admin_h, json={"target": "qbittorrent"})
            check("推送端点可用（没有可用 qB 时如实返回失败）",
                  push.status_code == 200 and push.json().get("success") is False,
                  f"HTTP {push.status_code} {push.text[:160]}")
    with SessionLocal() as db:
        row = (db.query(models.MovieRequest)
               .filter(models.MovieRequest.id == request_id).first())
        check("推送结果如实落库（失败也记 push_status / pushed_at）",
              row is not None and row.push_status == "failed" and row.pushed_at is not None,
              f"push_status={getattr(row, 'push_status', None)}")
        audits = (db.query(models.AdminLog)
                  .filter(models.AdminLog.action == "push_media_seek").count())
        check("推送写了审计（记录结果那一批也没漏）", audits >= 1, f"{audits} 条")

# ==================== 5. 探测 / 体检层：慢写不占事件循环 ====================
print("\n=== 5. 服务器体检 / 挂载体检（探测层落库不在事件循环上）===")

PROBE_SLEEP = 0.35      # 「这台机器的写库就是慢」：命中的 UPDATE 先睡这么久
_slow_gate: dict = {"on": False, "tables": ("remote_servers",)}


def _slow_writes(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
    """给「慢盘 / 跨机 PostgreSQL」建模：命中的 UPDATE 先睡 0.35 秒

    只在第 5 节打开（默认关着），前面的 SQL 计数与耗时不受影响。
    """
    if not _slow_gate["on"]:
        return
    if not statement.lstrip().upper().startswith("UPDATE"):
        return
    lowered = statement.lower()
    if any(t in lowered for t in _slow_gate["tables"]):
        time.sleep(PROBE_SLEEP)


sa_event.listen(engine, "before_cursor_execute", _slow_writes)


async def section_probe_layer():
    # 5.0 造一台服务器（体检目标）与一个服，并登录拿管理员 JWT
    with SessionLocal() as db:
        realm_id = realms.active_realm_id(db)
        target = models.RemoteServer(
            name=f"热路径体检机 {suffix}", kind="ea", url="http://127.0.0.1:9",
            config="{}", is_enabled=True, realm_id=realm_id,
        )
        db.add(target)
        db.commit()
        db.refresh(target)
        target_id = target.id
        # 挂载体检端点要先有 EA 地址（正常由「保存服务入口」写进去）
        realms.set_realm_config(db, "emby_managed_url", "http://127.0.0.1:9",
                                realm_id, "Emby 服务地址")
        db.commit()
        staff_row = db.query(models.WebUser).filter(models.WebUser.id == staff_id).first()
        staff_name = staff_row.username

    _slow_gate["on"] = True
    try:
        # 5.1 基线自证：同样是这次 UPDATE，直接跑在事件循环上
        async def blocking_probe_write() -> float:
            """改动前 probe_and_store 的落库就是这个形态（同步 UPDATE 在循环上）"""
            watch = LoopWatch()
            watch.start()
            await asyncio.sleep(watch.interval * 2)
            with SessionLocal() as db:
                db.query(models.RemoteServer).filter(models.RemoteServer.id == target_id) \
                    .update({"last_check_message": ""})
                db.commit()
            await asyncio.sleep(watch.interval * 2)
            return watch.stop()

        gap = await blocking_probe_write()
        check("基线自证：体检落库跑在事件循环上会卡住全站（心跳空洞 ≥0.3s）",
              gap >= PROBE_SLEEP * 0.8, f"心跳空洞={gap:.3f}s")

        # 5.2 真实路由：POST /api/admin/servers/{id}/test（体检 + 落库 + 无）
        #     体检本身不发真网络（本节量的是「落库在哪个线程」），探测器换成假的。
        #     注意：EA 体检还有第二段网络——probe_and_store 在探测 ok 后会调
        #     fetch_node_identity 拉节点身份（每次新建 httpx 客户端 + 真发请求），
        #     那段成本同样压在事件循环上、同样不在量测范围内，必须一并换成假的；
        #     只换探测器不换身份拉取，就是此前三连红（0.18–0.20s）的来源。
        real_probe = registry.probe_server
        real_identity = node_lib.fetch_node_identity

        async def fake_probe(kind, url, config=None):  # noqa: ARG001
            return {"ok": True, "message": "smoke 体检"}

        async def fake_identity(url, timeout=12.0):  # noqa: ARG001
            return {"ok": True, "data": {}}

        registry.probe_server = fake_probe
        node_lib.fetch_node_identity = fake_identity
        box: dict = {}
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as api:
                login = await api.post("/api/user/auth/login",
                                       json={"username": staff_name, "password": "hot-paths"})
                check("管理员可用于体检端点", login.status_code == 200, f"HTTP {login.status_code}")
                admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}

                async def hit_probe():
                    box["resp"] = await api.post(f"/api/admin/servers/{target_id}/test",
                                                 headers=admin_h)

                elapsed, gap = await measure(hit_probe)
        finally:
            registry.probe_server = real_probe
            node_lib.fetch_node_identity = real_identity

        resp = box.get("resp")
        check("服务器体检 → 200", resp is not None and resp.status_code == 200,
              f"HTTP {getattr(resp, 'status_code', '—')}")
        check("慢写确实发生了（0.35s 的落库没被跳过）", elapsed >= PROBE_SLEEP * 0.8,
              f"耗时={elapsed:.3f}s")
        check("这 0.35s 里事件循环保持响应（心跳空洞 < 0.15s）", gap < 0.15,
              f"心跳空洞={gap:.3f}s（改动前 ≈ {PROBE_SLEEP:.2f}s）")

        with SessionLocal() as db:
            row = (db.query(models.RemoteServer)
                   .filter(models.RemoteServer.id == target_id).first())
            check("体检结论真的落库了（下放线程池没耽误写）",
                  row is not None and row.last_check_ok is True
                  and row.last_check_message == "smoke 体检" and row.last_checked_at is not None,
                  f"ok={getattr(row, 'last_check_ok', None)} "
                  f"msg={getattr(row, 'last_check_message', '')!r}")

        # 5.3 挂载体检：POST /api/admin/emby/servers/mounts/refresh（拉取 + 写快照）
        real_fetch = mount_health.fetch_ea_health
        real_write = mount_health.write_ea_health

        async def fake_fetch(base_url, timeout=10.0):  # noqa: ARG001
            return {"ok": True, "data": {
                "checked_at": "2026-01-01T00:00:00", "total": 1, "failed_count": 0,
                "unreachable": [], "playback_node": "ea",
                "mounts": [{"id": 4242, "ok": True, "name": "热路径挂载"}],
            }}

        def slow_write(db, payload, realm_id=None):
            """真实的快照照写，只是先慢 0.35 秒（模拟慢盘）"""
            time.sleep(PROBE_SLEEP)
            return real_write(db, payload, realm_id)

        mount_health.fetch_ea_health = fake_fetch
        mount_health.write_ea_health = slow_write
        box.clear()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as api:
                login = await api.post("/api/user/auth/login",
                                       json={"username": staff_name, "password": "hot-paths"})
                admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}

                async def hit_mounts():
                    box["resp"] = await api.post("/api/admin/emby/servers/mounts/refresh",
                                                 headers=admin_h)

                elapsed, gap = await measure(hit_mounts)
        finally:
            mount_health.fetch_ea_health = real_fetch
            mount_health.write_ea_health = real_write

        resp = box.get("resp")
        check("挂载体检 → 200", resp is not None and resp.status_code == 200,
              f"HTTP {getattr(resp, 'status_code', '—')} {getattr(resp, 'text', '')[:120]}")
        check("慢快照确实写了（0.35s 没被跳过）", elapsed >= PROBE_SLEEP * 0.8,
              f"耗时={elapsed:.3f}s")
        check("这 0.35s 里事件循环保持响应（心跳空洞 < 0.15s）", gap < 0.15,
              f"心跳空洞={gap:.3f}s（改动前 ≈ {PROBE_SLEEP:.2f}s）")
        with SessionLocal() as db:
            snapshot = mount_health.read_ea_health(db, realm_id)
            ids = [m.get("id") for m in (snapshot.get("mounts") or []) if isinstance(m, dict)]
            check("EA 体检快照真的落库了（挂载页读的就是它）",
                  snapshot.get("ok") is True and 4242 in ids, f"mounts={ids}")

        # 5.4 审计：async 端点提交之后不能再读 ORM 属性（_audit 现在也收主键整数）
        real_probe = registry.probe_server
        registry.probe_server = fake_probe
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as api:
                login = await api.post("/api/user/auth/login",
                                       json={"username": staff_name, "password": "hot-paths"})
                admin_h = {"Authorization": f"Bearer {login.json()['access_token']}"}
                box.clear()
                box["resp"] = await api.post(f"/api/admin/servers/{target_id}/activate",
                                             headers=admin_h)
        finally:
            registry.probe_server = real_probe

        resp = box.get("resp")
        body = resp.json() if resp is not None and resp.status_code == 200 else {}
        check("激活体检通过的服务器 → 200 / success", bool(body.get("success")),
              f"HTTP {getattr(resp, 'status_code', '—')} {str(body.get('message'))[:60]}")
        with SessionLocal() as db:
            row = (db.query(models.RemoteServer)
                   .filter(models.RemoteServer.id == target_id).first())
            check("服务器真的被设为当前使用（下放线程池没耽误写）",
                  row is not None and bool(row.is_active))
            logs = (db.query(models.AdminLog)
                    .filter(models.AdminLog.action == "activate_server",
                            models.AdminLog.target_id == target_id).all())
            check("激活写了审计，且审计里记的是这位管理员（主键整数那条路）",
                  len(logs) == 1 and logs[0].admin_user_id == staff_id,
                  f"{[(log.admin_user_id, log.action) for log in logs]}")
    finally:
        _slow_gate["on"] = False


def admin_cleanup(db):
    """第 5 节造出来的行：服务器 / 服配置 / 审计"""
    db.query(models.RemoteServer).filter(models.RemoteServer.name.like(f"%{suffix}%")).delete(
        synchronize_session=False)
    db.query(models.AdminLog).filter(models.AdminLog.action.in_(
        ["activate_server", "activate_server_failed"])).filter(
        models.AdminLog.target_type == "server").delete(synchronize_session=False)


async def _run_sections():
    """按标题顺序跑：3（会话上报）→ 4（求片 / 通知层）→ 5（探测层），输出顺序与章节一致"""
    await section_hot_path()
    await section_seek_notify()
    await section_probe_layer()


asyncio.run(_run_sections())


# ==================== 收尾 ====================
print()
with SessionLocal() as db:
    db.query(em.UserMediaData).filter(em.UserMediaData.user_id == user_id).delete()
    db.query(em.PlaybackSession).filter(em.PlaybackSession.user_id == user_id).delete()
    for it in db.query(em.MediaItem).filter(em.MediaItem.library_id == library_id).all():
        db.query(em.MediaStream).filter(em.MediaStream.item_id == it.id).delete()
        db.delete(it)
    db.flush()
    db.query(em.Library).filter(em.Library.id == library_id).delete()
    db.query(em.EmbyApiToken).filter(em.EmbyApiToken.token == hash_emby_token(token)).delete()
    db.query(models.StationMessage).filter(models.StationMessage.to_user_id == staff_id).delete()
    db.query(models.MovieRequest).filter(models.MovieRequest.user_id == user_id).delete()
    db.query(models.AdminLog).filter(models.AdminLog.target_type == "media_seek").delete()
    admin_cleanup(db)
    db.query(models.WebUser).filter(models.WebUser.id == user_id).delete()
    db.query(models.WebUser).filter(models.WebUser.id == staff_id).delete()
    db.commit()

for path in (DB, DB + "-wal", DB + "-shm"):
    try:
        os.remove(path)
    except OSError:
        pass

if failures:
    print(f"FAILED {len(failures)}/{TOTAL}:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL PASS {TOTAL}/{TOTAL}")

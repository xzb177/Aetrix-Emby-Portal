"""扫描队列与实时进度冒烟测试（v2.27.0）

线上症状：四个媒体库在同一分钟里依次点「扫描」，四个任务同时跑起来——同一个远程挂载
被四个任务反复 PROPFIND（日志里「同一路径每 5 秒一次」），CPU 打满而 item_count 一直是 0。

本测试覆盖这一版加的四层保护与两处可见性：

1. **按远程挂载串行化**：引用同一挂载的库排队，不并发打远端；只读本机目录的库照常并行；
2. **全局并发上限**：最多 N 个扫描同时跑（默认 2）；
3. **排队不报错**：重复点同一个库合并成一条（不 409）；排队中可以取消，正在跑的取消返回 409；
4. **远程并发上限**：同一时刻最多 MOUNT_REMOTE_CONCURRENCY 个远程请求在飞；
5. **会话内列举复用**：一轮扫描期间同一个远程目录不再受 5 秒 TTL 影响（日志里那条重复
   PROPFIND 就是这么来的）；
6. **实时进度**：阶段 / 已发现 / 已处理 / 当前目录 / 本轮远程请求数，管理端能查到。

不联网、不扫真文件：需要「扫得慢一点」的地方用替身函数，其余用真实队列与真实 HTTP 路由。
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"
# 空值也算没设（配置自愈会往 .env 物化出空的 SECRET_KEY，setdefault 只认「不存在」）
os.environ["SECRET_KEY"] = (os.environ.get("SECRET_KEY") or "").strip() or \
    "smoke-test-only-secret-key-not-for-production"

from fastapi.testclient import TestClient  # noqa: E402

from backend import models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import mounts as mnt  # noqa: E402
from backend.emby_server import scan_instrument, scan_progress, scan_queue  # noqa: E402
from backend.emby_server import scanner as sc  # noqa: E402
from backend.main import app  # noqa: E402
from backend.security import create_access_token, hash_password  # noqa: E402

init_db()
client = TestClient(app)

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def wait_until(pred, timeout: float = 8.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def make_admin() -> dict:
    with SessionLocal() as db:
        user = models.WebUser(username="queue_admin", password_hash=hash_password("pass12345"),
                              is_active=True, is_staff=True, admin_role="super")
        db.add(user)
        db.commit()
        return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def make_library(name: str, *, mounts: tuple = (), paths: tuple = ()) -> int:
    with SessionLocal() as db:
        lib = em.Library(guid=f"guid-{name}", name=name, collection_type="movies",
                         paths=",".join(paths), mount_ids=",".join(str(m) for m in mounts))
        db.add(lib)
        db.commit()
        return lib.id


def make_mount(mount_type: str, label: str) -> int:
    with SessionLocal() as db:
        mount = em.StorageMount(name=label, mount_type=mount_type,
                                config='{"root": "/media"}', is_enabled=True)
        db.add(mount)
        db.commit()
        return mount.id


# ==================== 0. 安装与口径 ====================
print("=== 0. 进度上报接入（幂等）===")
check("首次安装返回 True", scan_instrument.install() is True)
check("重复安装返回 False（不会套两层壳）", scan_instrument.install() is False)
check("_FileCounter.__iter__ 已被包装",
      getattr(sc._FileCounter.__iter__, "_rb_progress_wrapped", False) is True)
check("_iter_prepared 已被包装",
      getattr(sc._iter_prepared, "_rb_progress_wrapped", False) is True)
check("开关默认值：并发上限 ≥1、挂载串行化默认开",
      scan_queue.SCAN_MAX_PARALLEL >= 1 and scan_queue.SCAN_MOUNT_SERIAL is True,
      f"max_parallel={scan_queue.SCAN_MAX_PARALLEL}")

# ==================== 1. 替身扫描：按挂载串行化 + 并发上限 ====================
print("\n=== 1. 同一远程挂载串行化（核心）===")
shared_mount = make_mount("rclone", "MP媒体库")
other_mount = make_mount("rclone", "另一个挂载")
tmp_dir = tempfile.mkdtemp(prefix="queue-local-")
lib_a = make_library("剧集", mounts=(shared_mount,))
lib_b = make_library("电影", mounts=(shared_mount,))
lib_c = make_library("演唱会", mounts=(other_mount,))
lib_d = make_library("音乐片", paths=(tmp_dir,))

real_scan_sync = sc.scan_library_sync
live = {"now": 0, "peak": 0, "mounts_in_use": set(), "conflict": False, "runs": []}
guard = threading.Lock()


def fake_scan(db, library, snapshot=None, trigger="manual"):
    """替身：把「扫描」变成一段可控的等待，并盯住挂载是否被并发占用"""
    with guard:
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        for mount_id in (getattr(snapshot, "mount_ids", ()) or ()):
            if mount_id in live["mounts_in_use"]:
                live["conflict"] = True
            live["mounts_in_use"].add(mount_id)
        live["runs"].append((library.id, time.monotonic()))
    try:
        time.sleep(0.45)
        library.scan_status = "success"
        return {"added": 0, "duration_ms": 450}
    finally:
        with guard:
            live["now"] -= 1
            for mount_id in (getattr(snapshot, "mount_ids", ()) or ()):
                live["mounts_in_use"].discard(mount_id)


sc.scan_library_sync = fake_scan
try:
    scan_queue.SCAN_MAX_PARALLEL = 2
    with SessionLocal() as db:
        order = []
        for lib_id in (lib_a, lib_b, lib_c, lib_d):
            lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
            order.append((lib_id, scan_queue.enqueue(lib, trigger="manual")))

    snap = scan_queue.snapshot()
    states = {t["library_id"]: t["state"] for t in snap["running"] + snap["waiting"]}
    check("四个库都进了队列（不是直接起线程）",
          len(snap["running"]) + len(snap["waiting"]) == 4, str(states))
    check("同时最多跑 2 个（并发上限生效）",
          len(snap["running"]) == 2 and live["peak"] <= 2, f"running={len(snap['running'])} peak={live['peak']}")

    # 剧集 与 电影 抢同一个挂载：只能有一个在跑，另一个必须排队并写清在等谁
    holders = [t for t in snap["running"] if shared_mount in (t["mount_ids"] or [])]
    waiting_on_shared = [
        t for t in snap["waiting"]
        if shared_mount in (t["mount_ids"] or []) and shared_mount in (t["waiting_for"] or [])
    ]
    check("引用同一挂载的两个库：一个在跑、另一个在排队",
          len(holders) == 1 and len(waiting_on_shared) == 1,
          f"running={[t['library_id'] for t in snap['running']]} waiting={[t['library_id'] for t in snap['waiting']]}")
    check("排队的那条写明了「在等哪个挂载」",
          waiting_on_shared and waiting_on_shared[0]["waiting_for"] == [shared_mount],
          str(waiting_on_shared[0]["waiting_for"] if waiting_on_shared else None))
    check("挂载占用表反映当前归属",
          str(shared_mount) in snap["mount_owners"],
          str(snap["mount_owners"]))
    check("队列里给出了位置（第几位）",
          all(t.get("position") for t in snap["waiting"]), str([t.get("position") for t in snap["waiting"]]))

    # 只读本机目录的库不受远程挂载串行化影响：它要么在跑、要么已经跑完
    local_task = next(t for t in (snap["running"] + snap["waiting"] + snap["history"])
                      if t["library_id"] == lib_d)
    check("只读本机目录的库不吃挂载串行化（照常跑）",
          local_task["waiting_for"] == [], str(local_task["waiting_for"]))

    check("两轮之后共享挂载没有被并发占用过", live["conflict"] is False)

    done = wait_until(lambda: not scan_queue.snapshot()["running"]
                      and not scan_queue.snapshot()["waiting"], timeout=12)
    check("四轮全部跑完（排队没有卡死）", done, str(scan_queue.snapshot()))
    history = {t["library_id"]: t for t in scan_queue.snapshot()["history"]}
    check("排队过的任务记下了排队时长（>0）",
          history[lib_b]["queued_ms"] > 0 or history[lib_c]["queued_ms"] > 0,
          str({k: v["queued_ms"] for k, v in history.items()}))
    check("任务终态是 done + success",
          all(t["state"] == "done" and t["result"] == "success" for t in history.values()),
          str({k: (v["state"], v["result"]) for k, v in history.items()}))
finally:
    sc.scan_library_sync = real_scan_sync

# ==================== 2. 重复点击合并 + 取消排队 ====================
print("\n=== 2. 重复点击不报错 / 排队可取消 ===")
scan_queue.reset_for_tests()
sc.scan_library_sync = fake_scan
try:
    scan_queue.SCAN_MAX_PARALLEL = 1
    with SessionLocal() as db:
        lib_e = make_library("库E", mounts=(other_mount,))
        target = db.query(em.Library).filter(em.Library.id == lib_e).first()
        first = scan_queue.enqueue(target, trigger="manual")
        second = scan_queue.enqueue(target, trigger="manual")
    check("重复点同一个库被合并（created=False，不报错）",
          first["created"] is True and second["created"] is False)
    check("合并时记下被点了几次", second["task"]["request_count"] == 2,
          str(second["task"]["request_count"]))
    check("队列里只有一条", len(scan_queue.snapshot()["running"]) + len(scan_queue.snapshot()["waiting"]) == 1)

    with SessionLocal() as db:
        lib_f = make_library("库F", mounts=(other_mount,))
        lib_g = make_library("库G", mounts=(other_mount,))
        for lib_id in (lib_f, lib_g):
            row = db.query(em.Library).filter(em.Library.id == lib_id).first()
            scan_queue.enqueue(row, trigger="manual")
    snap = scan_queue.snapshot()
    check("并发上限=1 时后两个在排队", len(snap["running"]) == 1 and len(snap["waiting"]) == 2,
          f"running={len(snap['running'])} waiting={len(snap['waiting'])}")
    queued_one = snap["waiting"][0]
    check("取消排队中的任务 → canceled",
          scan_queue.cancel(queued_one["library_id"]) == "canceled")
    check("取消后排到它后面的任务位置前移",
          [t["position"] for t in scan_queue.snapshot()["waiting"]] == [1],
          str([t["position"] for t in scan_queue.snapshot()["waiting"]]))
    running_id = scan_queue.snapshot()["running"][0]["library_id"]
    check("正在跑的不能取消（会留下半个库的状态）",
          scan_queue.cancel(running_id) == "running")
    check("取消后的任务留在历史里，结果是 canceled",
          any(t["result"] == "canceled" for t in scan_queue.snapshot()["history"]))
    wait_until(lambda: not scan_queue.snapshot()["running"]
               and not scan_queue.snapshot()["waiting"], timeout=12)
finally:
    sc.scan_library_sync = real_scan_sync
    scan_queue.reset_for_tests()

# ==================== 3. 实时进度（真实扫描 + 包装层）====================
print("\n=== 3. 实时进度：枚举 / 处理 / 阶段 / 当前目录 ===")
media_dir = tempfile.mkdtemp(prefix="queue-progress-")
FILES = 8
for index in range(FILES):
    with open(os.path.join(media_dir, f"Movie.{index}.2024.1080p.mp4"), "wb") as handle:
        handle.write(b"\x00" * 512)
lib_p = make_library("进度库", paths=(media_dir,))

real_side_info = sc._side_info
seen_progress: list[dict] = []
persisted_row: list[dict] = []


def slow_side_info(ctx, scan_file):
    """让每个文件的处理慢一点，好在扫描过程中观察进度（不改扫描逻辑）"""
    time.sleep(0.35)
    return real_side_info(ctx, scan_file)


sc._side_info = slow_side_info
try:
    with SessionLocal() as db:
        row = db.query(em.Library).filter(em.Library.id == lib_p).first()
        scan_queue.enqueue(row, trigger="manual")

    # 采样：直接问队列的实时状态（与面板同一个入口），并在拿到「处理中」时手动刷一次盘，
    # 读回数据库里那一行——那是「刷新页面也能看到进度」的保证。
    #
    # 两条口径不能省（CI 上真的偶发失败过一次，失败详情是 `[{'raw': None}]`）：
    #   1. **以 `flush_once()` 的返回值判断这次刷盘是不是真的写了东西**。扫描一结束
    #      `clear_progress` 就会把进度清成 None，而「上一条查询」与「刷盘」之间有个窗口——
    #      快照可能已经空了，那就是「刷了 0 条」，不是「进度没落盘」。
    #   2. **读回必须另开一个会话**：在同一条查询身上 refresh，拿到的可能是刷新前的快照。
    # 所以：刷到 0 条就下一轮再试，只把真正写进去过的那次记下来。
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            lib = db.query(em.Library).filter(em.Library.id == lib_p).first()
            payload = scan_queue.live_payload(lib)
            running = bool(payload) and payload.get("state") == "running"
            if running:
                seen_progress.append(payload)
        if running and not persisted_row and scan_queue.flush_once():
            with SessionLocal() as db:  # 新会话：读的是刷盘之后的值
                row = db.query(em.Library).filter(em.Library.id == lib_p).first()
                if row.scan_progress:
                    persisted_row.append({"raw": row.scan_progress})
        if scan_queue.snapshot()["history"]:
            break
        time.sleep(0.01)

    check("扫描过程中拿到过实时进度", bool(seen_progress), f"{len(seen_progress)} 次采样")
    parts = [p.get("progress") or {} for p in seen_progress]
    phases = {part.get("phase") for part in parts}
    processed_max = max((part.get("processed", 0) for part in parts), default=0)
    enumerated_max = max((part.get("enumerated", 0) for part in parts), default=0)
    check("进度里带阶段（枚举 → 处理 → 清理）",
          phases & {"processing", "cleanup"}, str(sorted(phases)))
    check("进度里能看出「已处理几个」", processed_max >= 1, f"processed_max={processed_max}")
    check("进度里能看出「已发现几个」（枚举数=文件数）",
          enumerated_max == FILES, f"enumerated_max={enumerated_max}")
    check("进度里带中文阶段名",
          any(part.get("phase_label") for part in parts), str(sorted({p.get('phase_label') for p in parts})))
    check("当前处理的目录可查（不是只有一个数字）",
          any(part.get("current") for part in parts),
          str([part.get("current") for part in parts][:3]))

    # 收尾：进度不该留在库上（否则面板会一直显示「扫描中 已处理 N」）
    with SessionLocal() as db:
        lib = db.query(em.Library).filter(em.Library.id == lib_p).first()
    check("进度快照落进库里（不是只在内存里）",
          bool(persisted_row) and "processed" in (persisted_row[0]["raw"] or {}),
          str(persisted_row)[:160] or "一次都没刷进库（flush 始终刷到 0 条：扫描太快或清空抢在前面）")
    check("扫描结束后清掉了进度快照，且 scan_live 为空",
          lib.scan_progress is None and scan_queue.live_payload(lib) is None,
          f"scan_progress={lib.scan_progress!r}")
    check("真实扫描留下成功状态与条目",
          lib.scan_status == "success" and (lib.item_count or 0) >= 1,
          f"status={lib.scan_status} items={lib.item_count}")
finally:
    sc._side_info = real_side_info
    scan_queue.reset_for_tests()

# ==================== 4. 包装层单独口径 ====================
print("\n=== 4. 包装层口径（没有绑定则完全空转）===")
counter = sc._FileCounter(iter([1, 2, 3, 4, 5, 6, 7]))
check("未绑定扫描上下文时不报进度（单测直接调 scanner 与升级前一致）",
      list(counter) == [1, 2, 3, 4, 5, 6, 7] and scan_progress.progress_of(999) is None)

scan_progress.unbind()
entry = scan_progress.begin_scan(999, "口径库")
check("begin_scan 绑定当前线程并登记进度",
      scan_progress.current_library_id() == 999 and entry["phase"] == "enumerating")
counter2 = sc._FileCounter(iter([1, 2, 3]))
list(counter2)
check("枚举计数在来源流收尾时补齐（3 个文件不是 25 的整数倍）",
      (scan_progress.progress_of(999) or {}).get("enumerated") == 3,
      str(scan_progress.progress_of(999)))


class _FakeFile:
    stored_path = "/media/电影/Some.Movie.2024.mkv"


def fake_iter_prepared(ctx, files, pool, db, process_one, label, counted, stats, failed_roots):
    # 与 scanner._iter_prepared 新签名一致：回调驱动，不再 yield
    for _ in range(2):
        process_one(_FakeFile(), None)


wrapped = scan_instrument._wrap_iter_prepared(fake_iter_prepared)
driven = []
wrapped({}, None, None, None, lambda sf, pending: driven.append(sf), "口径库", None, {}, [])
progress_now = scan_progress.progress_of(999) or {}
check("处理计数与阶段随写库循环推进（原样透传，不改扫描主体）",
      len(driven) == 2 and progress_now.get("processed") == 2
      and progress_now.get("phase") == "processing", str(progress_now))
check("「当前目录」跟着写库循环走",
      progress_now.get("current") == "/media/电影", f"current={progress_now.get('current')!r}")
scan_instrument._wrap_body(lambda db, library, snap: "body")(None, type("L", (), {"id": 999})(), None)
check("阶段可以从处理切回枚举（新一轮）",
      (scan_progress.progress_of(999) or {}).get("phase") == "enumerating")
scan_progress.set_phase(999, "processing")
scan_instrument._wrap_cleanup(lambda db, library, seen: 7)(None, type("L", (), {"id": 999})(), set())
check("清理阶段单独上报", (scan_progress.progress_of(999) or {}).get("phase") == "cleanup")
scan_progress.end_scan(999)
check("end_scan 解绑并清掉进度",
      scan_progress.current_library_id() is None and scan_progress.progress_of(999) is None)
check("扫描会话深度回到 0（不再吃挂载缓存的 TTL 豁免）",
      scan_progress.session_depth() == 0 and scan_progress.in_scan_session() is False)

# ==================== 5. 远程并发上限 + 会话内列举复用 ====================
print("\n=== 5. 远程限流与「一轮扫描内只列一次」===")


class CountingProvider(mnt.MountProvider):
    """远程提供者替身：只计数，不发网络请求"""

    def __init__(self, mount, counter, delay=0.0):
        super().__init__(mount)
        self._counter = counter
        self._delay = delay

    @mnt.cached_listing
    def list_dir(self, rel: str = "/"):
        with self._counter["lock"]:
            self._counter["calls"] += 1
            self._counter["inflight"] += 1
            self._counter["peak"] = max(self._counter["peak"], self._counter["inflight"])
        try:
            if self._delay:
                time.sleep(self._delay)
            return [mnt.MountEntry(name="a.mkv", rel=f"{rel.rstrip('/')}/a.mkv", is_dir=False, size=1)]
        finally:
            with self._counter["lock"]:
                self._counter["inflight"] -= 1


class FakeRemoteMount:
    def __init__(self, mid: int, mount_type: str = "rclone"):
        self.id = mid
        self.mount_type = mount_type
        self.config = '{"mode": "rc"}'


mnt.invalidate_list_cache()
mnt.MOUNT_LIST_CACHE_SECONDS = 0.15      # 把 TTL 调短，好在测试里观察到「过期重列」
mnt.MOUNT_REMOTE_CONCURRENCY = 2
scan_progress.reset()

counter_one = {"calls": 0, "inflight": 0, "peak": 0, "lock": threading.Lock()}
provider = CountingProvider(FakeRemoteMount(101), counter_one, delay=0.05)
provider.list_dir("/电影")
time.sleep(0.25)                          # 超过 TTL
provider.list_dir("/电影")
check("普通情况（不在扫描会话里）：TTL 到期后重新列举",
      counter_one["calls"] == 2, f"calls={counter_one['calls']}")

mnt.invalidate_list_cache()
scan_progress.begin_scan(4242, "会话库")
counter_two = {"calls": 0, "inflight": 0, "peak": 0, "lock": threading.Lock()}
provider_two = CountingProvider(FakeRemoteMount(102), counter_two, delay=0.05)
try:
    provider_two.list_dir("/电影")
    time.sleep(0.25)                      # 明显超过 TTL
    provider_two.list_dir("/电影")
    check("扫描会话期间：同一个目录不再重复 PROPFIND（TTL 豁免）",
          counter_two["calls"] == 1, f"calls={counter_two['calls']}")
    check("复用被记进远程计数（管理端能看到复用率）",
          scan_progress.remote_stats()["reused"] >= 1, str(scan_progress.remote_stats()))
finally:
    scan_progress.end_scan(4242)

mnt.invalidate_list_cache()
counter_three = {"calls": 0, "inflight": 0, "peak": 0, "lock": threading.Lock()}
providers = [CountingProvider(FakeRemoteMount(200 + i), counter_three, delay=0.12) for i in range(6)]
threads = [threading.Thread(target=prov.list_dir, args=(f"/dir{i}",)) for i, prov in enumerate(providers)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("远程并发上限生效：6 个并发请求最多 2 个在飞",
      counter_three["peak"] <= mnt.MOUNT_REMOTE_CONCURRENCY and counter_three["calls"] == 6,
      f"peak={counter_three['peak']} calls={counter_three['calls']}")
check("在飞峰值也被记进远程计数", scan_progress.remote_stats()["peak_inflight"] <= 2,
      str(scan_progress.remote_stats()))

# 本机挂载不吃远程名额（否则本地扫描会被远程限流拖住）
local_counter = {"calls": 0, "inflight": 0, "peak": 0, "lock": threading.Lock()}
local_provider = CountingProvider(FakeRemoteMount(300, mount_type="local"), local_counter, delay=0.05)
scan_progress.note_remote_inflight(-scan_progress.remote_stats()["inflight"])  # 归零，便于观察
before_lists = scan_progress.remote_stats()["lists"]
local_provider.list_dir("/电影")
check("本机挂载的列举不计入远程请求数",
      scan_progress.remote_stats()["lists"] == before_lists,
      f"before={before_lists} after={scan_progress.remote_stats()['lists']}")

mnt.MOUNT_LIST_CACHE_SECONDS = 5
mnt.invalidate_list_cache()

# ==================== 6. HTTP 面 ====================
print("\n=== 6. 管理端接口 ===")
headers = make_admin()
scan_queue.reset_for_tests()
sc.scan_library_sync = fake_scan
try:
    with SessionLocal() as db:
        lib_h = make_library("接口库", mounts=(other_mount,))
        lib_i = make_library("接口库2", mounts=(other_mount,))
    scan_queue.SCAN_MAX_PARALLEL = 1

    first = client.post(f"/api/admin/emby/libraries/{lib_h}/scan", headers=headers)
    payload = first.json()
    # 入队即就地派发：没被挡住时响应必须说「已启动」，而不是谎报「排队第 1 位」
    check("POST 扫描：没被挡住时如实说「扫描已启动」",
          first.status_code == 200 and payload.get("queued") is True
          and payload.get("started") is True and payload.get("task", {}).get("state") == "running",
          str(payload)[:160])
    second = client.post(f"/api/admin/emby/libraries/{lib_h}/scan", headers=headers)
    check("重复 POST 不报 409，只说明「已在扫描中/队列中」",
          second.status_code == 200 and second.json().get("already") is True,
          f"HTTP {second.status_code} {str(second.json())[:120]}")

    queued = client.post(f"/api/admin/emby/libraries/{lib_i}/scan", headers=headers).json()
    check("引用同一挂载的第二个库入队时就写明「在等哪个挂载」",
          queued["task"]["waiting_for"] == [other_mount] and queued["task"].get("position")
          == 1, str(queued["task"])[:200])
    check("入队消息里带挂载名字（不是只给 id）",
          "另一个挂载" in (queued.get("message") or ""), str(queued.get("message")))

    snapshot = client.get("/api/admin/emby/scan-queue", headers=headers).json()
    check("GET 扫描队列：running / waiting / history / 远程计数都在",
          all(key in snapshot for key in ("running", "waiting", "history", "remote", "mount_owners")),
          str(sorted(snapshot.keys())))
    check("队列快照带挂载名字表", str(other_mount) in (snapshot.get("mount_names") or {}),
          str(snapshot.get("mount_names")))

    libs = client.get("/api/admin/emby/libraries", headers=headers).json()["libraries"]
    row = next(item for item in libs if item["id"] == lib_h)
    check("媒体库列表带 scan_live（实时状态）",
          isinstance(row.get("scan_live"), dict) and row["scan_live"]["state"] in ("queued", "running"),
          str(row.get("scan_live"))[:160])

    live_resp = client.get(f"/api/admin/emby/libraries/{lib_h}/scan-live", headers=headers)
    check("单库实时状态接口可用", live_resp.status_code == 200
          and live_resp.json().get("library_id") == lib_h, f"HTTP {live_resp.status_code}")

    cancel = client.delete(f"/api/admin/emby/scan-queue/{lib_i}", headers=headers)
    check("取消排队中的扫描 → 200", cancel.status_code == 200, f"HTTP {cancel.status_code}")
    still_running = client.delete(f"/api/admin/emby/scan-queue/{lib_h}", headers=headers)
    check("取消正在跑 → 409（并说清原因）",
          still_running.status_code == 409 and "正在扫描" in still_running.json()["detail"],
          f"HTTP {still_running.status_code} {still_running.text[:120]}")
    missing = client.delete("/api/admin/emby/scan-queue/999999", headers=headers)
    check("取消不在队列里的库 → 404", missing.status_code == 404, f"HTTP {missing.status_code}")

    check("未登录访问扫描队列 → 401/403",
          client.get("/api/admin/emby/scan-queue").status_code in (401, 403))

    wait_until(lambda: not scan_queue.snapshot()["running"]
               and not scan_queue.snapshot()["waiting"], timeout=12)
    idle = client.get(f"/api/admin/emby/libraries/{lib_h}/scan-live", headers=headers)
    check("空闲时单库实时状态返回 204（没有进度可报）", idle.status_code == 204,
          f"HTTP {idle.status_code}")
finally:
    sc.scan_library_sync = real_scan_sync
    scan_queue.reset_for_tests()

# ==================== 7. 升级不变：队列关掉仍可用 ====================
print("\n=== 7. 开关：队列关掉 = 升级前行为（不串行化、不设上限）===")
scan_queue.SCAN_QUEUE_ENABLED = False
sc.scan_library_sync = fake_scan
live["now"] = 0
live["peak"] = 0
try:
    with SessionLocal() as db:
        ids = []
        for name in ("关闭A", "关闭B", "关闭C"):
            lib_id = make_library(name, mounts=(shared_mount,))
            ids.append(lib_id)
        for lib_id in ids:
            row = db.query(em.Library).filter(em.Library.id == lib_id).first()
            scan_queue.enqueue(row, trigger="manual")
    wait_until(lambda: live["peak"] >= 2, timeout=5)
    check("队列关闭时不再按挂载串行化（同一挂载可并发，退役行为一致）",
          live["peak"] >= 2, f"peak={live['peak']}")
    wait_until(lambda: not scan_queue.snapshot()["running"]
               and not scan_queue.snapshot()["waiting"], timeout=12)
finally:
    sc.scan_library_sync = real_scan_sync
    scan_queue.SCAN_QUEUE_ENABLED = True
    scan_queue.reset_for_tests()

print()
if failures:
    print(f"❌ 扫描队列冒烟失败 {len(failures)} 项：")
    for name in failures:
        print(f"   - {name}")
    sys.exit(1)
print("✅ 扫描队列冒烟全部通过")

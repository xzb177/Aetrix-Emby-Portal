#!/usr/bin/env python3
"""扫描结果与流水可查询（v2.22.0 / v2.23.0）冒烟测试

扫描统计以前只在**返回值**与日志里：管理端刷新一下就没了，「上一轮到底扫到什么」只能去
服务器日志翻。现在两层落库：

- **最近一次**写回媒体库（`scan_status` / `scan_stats` / `scan_error`），由
  `GET /api/admin/emby/libraries` 带回（`last_scan`）；
- **最近若干轮**记进 `emby_scan_runs` 流水（含触发方），由
  `GET /api/admin/emby/libraries/{id}/scans` 带回。

这条测试钉的是**可查且诚实**：

1. 从没扫过 → `last_scan` 为 `None`（「没扫过」和「扫了但都是 0」不是一回事）；
2. 成功一轮 → 新增/更新/删除/耗时落库，换个 Session 还能读到（是落库不是内存）；
3. 重扫 → 覆盖上一轮（不是累加），删除的文件计入 removed；
4. 来源读不到 → partial，点名读不到的来源，并说明「已跳过清理」；
5. 来源恢复 → 回到 success、补上那一轮被跳过的清理、error 清空；
6. 扫描抛异常 → failed + 原因落库，互斥与 is_scanning 都要复位（否则这个库再也扫不动）；
7. 扫描中 → running（`is_scanning` 为真），收尾后复位；
8. 虚拟库（没有自己的目录）算成功；**完全没配来源**算 partial（不是「一切正常」）；
9. 流水：每轮一条（新的在前）、带触发方与耗时、失败/部分失败的记录带原因、
   每库只留最近 N 条、`EMBY_SCAN_HISTORY=0` 可关闭、媒体库删除后由维护周期回收、
   「最近一次」与最新一条流水口径一致；
10. 按来源拆分：每条来源各自记账（文件数 / 新增 / 探测），空目录不算失败，
    读不到的来源带 `kind=unavailable` 与原因，挂载半路炸只影响它自己那条明细。

用法：python scripts/smoke_test_scan_result.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.database import engine, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import portal as emby_portal  # noqa: E402
from backend.emby_server import scanner as sc  # noqa: E402
from backend.emby_server import soft_delete  # noqa: E402

init_db()
Session = sessionmaker(bind=engine)

FAILED = []
TOTAL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


def fake_probe(path, headers=None, size=0):  # noqa: ANN001, ARG001
    return {
        "duration_ticks": 600_000_000, "bitrate": 1_000_000, "width": 1920, "height": 1080,
        "video_codec": "H264", "audio_codec": "AAC", "audio_languages": "chi",
        "subtitle_languages": "", "size": size or 2048,
        "streams": [
            {"stream_index": 0, "stream_type": "Video", "codec": "h264", "language": "",
             "display_title": None, "title": None, "channels": None, "bit_rate": 1_000_000},
        ],
    }


class FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class FakeTmdbSession:
    def get(self, url, params=None):  # noqa: ANN001, ARG002
        if "/search/" in url:
            query = (params or {}).get("query") or ""
            return FakeResp({"results": [{"id": abs(hash(query)) % 900000 + 1,
                                          "title": query, "name": query, "vote_average": 7.0}]})
        return FakeResp({"poster_path": "/poster.jpg", "backdrop_path": "/backdrop.jpg",
                         "external_ids": {"imdb_id": "tt1"}, "alternative_titles": {"titles": []}})


MOVIES = 4

lib_dir = tempfile.mkdtemp(prefix="scanresult_")
movie_dir = os.path.join(lib_dir, "Movies")
os.makedirs(movie_dir, exist_ok=True)


def movie_path(i: int) -> str:
    return os.path.join(movie_dir, f"Result Movie {i:02d} (2021).mkv")


for i in range(MOVIES):
    with open(movie_path(i), "wb") as f:
        f.write(b"\x00" * 2048)

sc.probe_metadata = fake_probe
sc.tmdb_client.session = FakeTmdbSession()
sc.tmdb_client.api_keys = ["fake-key"]
sc.tmdb_client.api_key = "fake-key"

with Session() as db:
    lib = em.Library(guid="r" * 32, name="扫描结果测试库", collection_type="movies", paths=lib_dir)
    db.add(lib)
    db.commit()
    lib_id = lib.id


def scan() -> dict:
    with Session() as db:
        lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
        return sc.scan_library_sync(db, lib)


def snapshot() -> dict:
    """换个 Session 读库：读到什么就是真落库了什么（内存里的对象不算数）"""
    with Session() as db:
        lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
        return {
            "status": lib.scan_status,
            "stats": sc.decode_scan_stats(lib.scan_stats),
            "error": lib.scan_error,
            "last_scan_at": lib.last_scan_at,
            "is_scanning": lib.is_scanning,
            "payload": sc.scan_result_payload(lib),
        }


def item_count() -> int:
    """库里**看得见**的条目数

    清理自 v2.48.0 起是软删除（消失的条目只标记 deleted_at，行还在），所以这里数可见行：
    ``Query.count()`` 会把语句包成子查询，全局可见性过滤下不到那里去，会把已下架的行也算上，
    而这个脚本要验的正是「用户还能浏览到多少条目」。
    """
    with Session() as db:
        return soft_delete.count_visible(db, em.MediaItem.library_id == lib_id)


# ==================== 1. 从没扫过 ====================
print("=== 1. 从没扫过 ===")
before = snapshot()
check("从未扫描：状态与时间都是空", before["status"] is None and before["last_scan_at"] is None,
      f"status={before['status']} last_scan_at={before['last_scan_at']}")
check("从未扫描：last_scan 是 None（不是「全 0 的结果」）", before["payload"] is None,
      str(before["payload"]))

with Session() as db:
    listed = emby_portal.list_libraries(staff=None, db=db, realm_id=0)["libraries"]
row = next((x for x in listed if x["id"] == lib_id), None)
check("管理端列表带 last_scan 字段（未扫描时为 None）",
      row is not None and "last_scan" in row and row["last_scan"] is None,
      str(row.get("last_scan") if row else row))

# ==================== 2. 成功一轮：结果落库 ====================
print("\n=== 2. 成功一轮：结果落库 ===")
first = scan()
snap1 = snapshot()
check("首次扫描：状态 success", snap1["status"] == "success", str(snap1["status"]))
check("首次扫描：统计落库与返回值一致",
      snap1["stats"].get("added") == MOVIES and first["added"] == MOVIES,
      f"落库 added={snap1['stats'].get('added')} 返回 added={first['added']}")
check("统计落库：duration_ms 在（不被白名单丢掉）",
      isinstance(snap1["stats"].get("duration_ms"), int) and snap1["stats"]["duration_ms"] >= 0,
      f"duration_ms={snap1['stats'].get('duration_ms')!r}")
check("统计落库：没有失败来源、没有跳过清理",
      snap1["stats"].get("failed_roots") == [] and snap1["stats"].get("removal_skipped") is False,
      f"{snap1['stats'].get('failed_roots')} / {snap1['stats'].get('removal_skipped')}")
check("扫描结束 is_scanning 复位", snap1["is_scanning"] is False)

payload = snap1["payload"]
check("last_scan：状态 / 完成时间 / 新增数齐全",
      bool(payload) and payload["status"] == "success" and payload["finished_at"]
      and payload["added"] == MOVIES, str(payload))
check("last_scan：耗时为整数毫秒",
      isinstance(payload["duration_ms"], int) and payload["duration_ms"] >= 0,
      f"duration_ms={payload['duration_ms']!r}")
check("last_scan：成功时没有失败原因",
      payload["error"] is None and payload["failed_roots"] == [] and payload["removal_skipped"] is False,
      f"error={payload['error']!r} failed_roots={payload['failed_roots']}")

with Session() as db:
    listed = emby_portal.list_libraries(staff=None, db=db, realm_id=0)["libraries"]
row = next(x for x in listed if x["id"] == lib_id)
check("管理端列表里的 last_scan 与库内一致",
      row["last_scan"] == payload,
      "" if row["last_scan"] == payload else f"{row['last_scan']} != {payload}")

# ==================== 3. 重扫：覆盖上一轮（不是累加） ====================
print("\n=== 3. 重扫：覆盖上一轮 ===")
os.remove(movie_path(0))
# 缺失宽限（2026-10-24 批一 #6）：连扫满宽限轮数才计入 removed，
# 下面的快照断言全部看最后一轮（中间轮条目仍在、removed=0）
second = None
for _ in range(sc.MISSING_GRACE_SCANS):
    second = scan()
snap2 = snapshot()
check("重扫：删掉的文件计入 removed", second["removed"] == 1 and snap2["stats"].get("removed") == 1,
      f"removed={second['removed']} / 落库 {snap2['stats'].get('removed')}")
check("重扫：新增数被清零（是覆盖不是累加）", snap2["stats"].get("added") == 0,
      f"added={snap2['stats'].get('added')}")
check("重扫：未变动与更新的条目合计 = 剩下的文件数",
      (snap2["stats"].get("updated") or 0) + (snap2["stats"].get("unchanged") or 0) == MOVIES - 1,
      f"updated={snap2['stats'].get('updated')} unchanged={snap2['stats'].get('unchanged')}")
check("重扫：状态仍是 success、removed 出现在 last_scan 里",
      snap2["payload"]["status"] == "success" and snap2["payload"]["removed"] == 1,
      str(snap2["payload"]))
check("重扫：完成时间向前推进", snap2["last_scan_at"] >= snap1["last_scan_at"],
      f"{snap1['last_scan_at']} → {snap2['last_scan_at']}")

# ==================== 4. 来源读不到：partial + 点名来源 ====================
print("\n=== 4. 来源读不到：partial ===")
bogus = os.path.join(lib_dir, "does-not-exist")
with Session() as db:
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    lib.paths = f"{lib_dir},{bogus}"
    db.commit()

# 同一个来源里再加一个新文件、同时删掉一个旧文件：
# 可读来源必须照常扫描（新文件入库），而清理阶段要跳过（旧文件的行留着）
with open(movie_path(9), "wb") as f:
    f.write(b"\x00" * 2048)
os.remove(movie_path(1))

third = scan()
snap3 = snapshot()
check("部分失败：状态 partial", snap3["status"] == "partial", str(snap3["status"]))
check("部分失败：读不到的来源被点名",
      any(bogus in r for r in snap3["stats"].get("failed_roots") or []),
      str(snap3["stats"].get("failed_roots")))
check("部分失败：removal_skipped 落库、payload 也标记",
      snap3["stats"].get("removal_skipped") is True and snap3["payload"]["removal_skipped"] is True,
      f"{snap3['stats'].get('removal_skipped')} / {snap3['payload']['removal_skipped']}")
check("部分失败：原因进了 last_scan.error（面板不用猜）",
      "does-not-exist" in (snap3["payload"]["error"] or ""), repr(snap3["payload"]["error"]))
check("部分失败：可读来源照常扫描（新文件入库）",
      third["added"] == 1 and third["removed"] == 0,
      f"added={third['added']} removed={third['removed']}")
check("部分失败：跳过清理 → 磁盘上已消失的条目仍留在库里（宁肯多留不误删）",
      item_count() == MOVIES, f"条目={item_count()} 期望={MOVIES}")

# ==================== 5. 来源恢复：回到 success 并补上清理 ====================
print("\n=== 5. 来源恢复 ===")
with Session() as db:
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    lib.paths = lib_dir
    db.commit()

# 被跳过的那轮清理从未给消失条目计过缺失数（清理压根没跑），
# 恢复后同样要走满宽限轮数才下架——逐轮累计，最后一轮的快照才作数
fourth = None
for _ in range(sc.MISSING_GRACE_SCANS):
    fourth = scan()
snap4 = snapshot()
check("来源恢复后：状态回到 success", snap4["status"] == "success", str(snap4["status"]))
check("来源恢复后：补上上一轮被跳过的清理",
      fourth["removed"] == 1 and snap4["stats"].get("removed") == 1,
      f"removed={fourth['removed']} 条目={item_count()}")
check("来源恢复后：error 清空、failed_roots 清空",
      snap4["payload"]["error"] is None and snap4["payload"]["failed_roots"] == [],
      f"error={snap4['payload']['error']!r}")
check("来源恢复后：条目数与磁盘一致", item_count() == MOVIES - 1,
      f"条目={item_count()} 期望={MOVIES - 1}")

# ==================== 6. 扫描抛异常：failed + 原因落库 + 互斥复位 ====================
print("\n=== 6. 扫描抛异常 ===")


def boom(db, library, snap):  # noqa: ANN001, ARG001
    raise RuntimeError("模拟扫描崩溃")


real_body = sc._scan_library_body
sc._scan_library_body = boom
raised = False
try:
    scan()
except RuntimeError:
    raised = True
finally:
    sc._scan_library_body = real_body

snap5 = snapshot()
check("异常：向上抛出（调用方要知道这一轮失败了）", raised)
check("异常：状态 failed 落库", snap5["status"] == "failed", str(snap5["status"]))
check("异常：原因含异常类型与消息（不吞异常）",
      "RuntimeError" in (snap5["error"] or "") and "模拟扫描崩溃" in (snap5["error"] or ""),
      repr(snap5["error"]))
check("异常：last_scan 带 failed 与原因",
      snap5["payload"]["status"] == "failed"
      and "模拟扫描崩溃" in (snap5["payload"]["error"] or ""), str(snap5["payload"]))
check("异常：is_scanning 复位（不会永远显示「扫描中」）", snap5["is_scanning"] is False)
check("异常：进程内互斥已释放（这个库还能再扫）", sc.is_scan_active(lib_id) is False)

recovered = scan()
snap6 = snapshot()
check("异常之后能正常重扫并回到 success",
      recovered["added"] == 0 and snap6["status"] == "success" and snap6["payload"]["error"] is None,
      f"added={recovered['added']} status={snap6['status']} error={snap6['payload']['error']!r}")
check("重扫后 last_scan 不再残留上一轮的失败",
      snap6["payload"]["status"] == "success" and snap6["payload"]["failed_roots"] == [],
      str(snap6["payload"]))

# ==================== 7. 扫描中：running ====================
print("\n=== 7. 扫描中：running ===")
with Session() as db:
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    open_run = sc.begin_scan(db, lib)
pending = snapshot()
check("扫描中：状态 running 且 is_scanning 落库",
      pending["status"] == "running" and pending["is_scanning"] is True,
      f"status={pending['status']} is_scanning={pending['is_scanning']}")
check("扫描中：last_scan 报 running（前端据此显示「未完成」）",
      pending["payload"]["status"] == "running", str(pending["payload"]))
with Session() as db:
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    sc.finish_scan(db, lib, {"added": 0, "updated": 0, "removed": 0}, open_run)
done = snapshot()
check("收尾后：状态 success 且 is_scanning 复位",
      done["status"] == "success" and done["is_scanning"] is False,
      f"status={done['status']} is_scanning={done['is_scanning']}")

# ==================== 8. 虚拟库与「没有来源」 ====================
print("\n=== 8. 虚拟库 / 没有来源 ===")
with Session() as db:
    vlib = em.Library(guid="v" * 32, name="平台虚拟库", collection_type="movies",
                      paths="", is_virtual=True)
    elib = em.Library(guid="e" * 32, name="没有来源的库", collection_type="movies", paths="")
    db.add_all([vlib, elib])
    db.commit()
    vid, eid = vlib.id, elib.id

with Session() as db:
    vrow = db.query(em.Library).filter(em.Library.id == vid).first()
    sc.scan_library_sync(db, vrow)
with Session() as db:
    vrow = db.query(em.Library).filter(em.Library.id == vid).first()
    vpay = sc.scan_result_payload(vrow)
check("虚拟库（没有自己的目录）算成功，不会被报成「来源不可用」",
      vpay["status"] == "success" and vpay["failed_roots"] == [],
      str(vpay))

with Session() as db:
    erow = db.query(em.Library).filter(em.Library.id == eid).first()
    sc.scan_library_sync(db, erow)
with Session() as db:
    erow = db.query(em.Library).filter(em.Library.id == eid).first()
    epay = sc.scan_result_payload(erow)
check("完全没配来源：算 partial（清理被跳过，不能报「一切正常」）",
      epay["status"] == "partial" and epay["removal_skipped"] is True, str(epay))
check("没有来源时的原因说清楚（不是含糊的「失败」）",
      "来源" in (epay["error"] or ""), repr(epay["error"]))

# ==================== 9. 扫描流水（最近若干轮） ====================
print("\n=== 9. 扫描流水 ===")


def runs(limit: int = 50) -> list:
    with Session() as db:
        return sc.scan_runs_payload(db, lib_id, limit=limit)


history = runs()
check("每一轮扫描都留下一条流水", len(history) >= 6, f"{len(history)} 条")
check("流水按时间倒序（最新在前）",
      [r["id"] for r in history] == sorted((r["id"] for r in history), reverse=True),
      str([r["id"] for r in history])[:80])
check("每条流水都有开始时间，已结束的有结束时间",
      all(r["started_at"] for r in history)
      and all(r["finished_at"] for r in history if r["status"] != "running"),
      str(history[0]))
check("流水里的失败轮次带原因",
      any(r["status"] == "failed" and "模拟扫描崩溃" in (r["error"] or "") for r in history),
      str([(r["status"], r["error"]) for r in history if r["status"] == "failed"]))
check("流水里的部分失败轮次带来源",
      any(r["status"] == "partial" and r["failed_roots"] for r in history),
      str([r["status"] for r in history]))
check("流水记录了耗时（跑过的那几轮都有）",
      all(isinstance(r["duration_ms"], int) and r["duration_ms"] >= 0
          for r in history if r["status"] == "success"),
      str([r["duration_ms"] for r in history[:3]]))

newest = history[0]
latest = snapshot()
check("「最近一次」与最新一条流水口径一致（状态 / 新增 / 删除）",
      newest["status"] == latest["payload"]["status"]
      and newest["added"] == latest["payload"]["added"]
      and newest["removed"] == latest["payload"]["removed"],
      f"流水 {newest['status']}/{newest['added']} 最近一次 {latest['payload']['status']}/{latest['payload']['added']}")

check("limit 生效（只要最近 2 条）", len(runs(limit=2)) == 2, f"{len(runs(limit=2))}")
with Session() as db:
    never = em.Library(guid="n" * 32, name="从未扫描过的库", collection_type="movies",
                       paths=lib_dir)
    db.add(never)
    db.commit()
    never_id = never.id
with Session() as db:
    empty_runs = sc.scan_runs_payload(db, never_id, limit=5)
check("从未扫描过的库：流水是空列表（不是报错，也不是占位结构）", empty_runs == [], str(empty_runs))

# 触发方：面板 / 客户端 / 节点 / 修复队列各记各的，未知值收敛成 manual
for trig, expect in (("client", "client"), ("node", "node"), ("repair", "repair"),
                     ("pinky", "manual"), (None, "manual")):
    with Session() as db:
        row = db.query(em.Library).filter(em.Library.id == lib_id).first()
        sc.scan_library_sync(db, row, sc.LibrarySnapshot.of(row), trig)
    check(f"触发方 {trig!r} 记录为 {expect}", runs(1)[0]["trigger"] == expect,
          f"记录成 {runs(1)[0]['trigger']!r}")
check("触发方枚举收敛（写错也不丢记录）",
      (sc.normalize_scan_trigger(""), sc.normalize_scan_trigger("MANUAL"),
       sc.normalize_scan_trigger("nonsense"), sc.normalize_scan_trigger(None)) ==
      ("manual", "manual", "manual", "manual"),
      str([sc.normalize_scan_trigger(v) for v in ("", "MANUAL", "nonsense", None)]))

# 每库上限：超出就按库回收最旧的行
before_keep = sc.SCAN_RUN_KEEP
sc.SCAN_RUN_KEEP = 3
try:
    with Session() as db:
        row = db.query(em.Library).filter(em.Library.id == lib_id).first()
        sc.scan_library_sync(db, row, sc.LibrarySnapshot.of(row))
    capped = runs(50)
    check("每库只留最近 N 条流水（旧行就地回收）", len(capped) == 3,
          f"上限 3 → 实际 {len(capped)} 条")
    check("回收的是旧行（剩下的 3 条是最近的）",
          capped[0]["id"] > capped[-1]["id"] and all(r["finished_at"] for r in capped),
          str([r["id"] for r in capped]))
finally:
    sc.SCAN_RUN_KEEP = before_keep

# EMBY_SCAN_HISTORY=0：不记流水，但扫描本身照常（结果仍写回媒体库）
sc.SCAN_RUN_KEEP = 0
try:
    with Session() as db:
        row = db.query(em.Library).filter(em.Library.id == lib_id).first()
        stats_off = sc.scan_library_sync(db, row, sc.LibrarySnapshot.of(row))
    check("EMBY_SCAN_HISTORY=0：不记流水", len(runs(50)) == 3, f"{len(runs(50))} 条")
    check("EMBY_SCAN_HISTORY=0：扫描本身照常，结果仍写回媒体库",
          stats_off["added"] == 0 and snapshot()["status"] == "success",
          f"added={stats_off['added']} status={snapshot()['status']}")
finally:
    sc.SCAN_RUN_KEEP = before_keep

# 进程被强杀：标志与流水都要收尾（否则刷新页面永远显示「扫描中」）
from backend.emby_server import maintenance as maint  # noqa: E402

with Session() as db:
    crashed = em.Library(guid="c" * 32, name="扫描中被强杀的库", collection_type="movies",
                         paths=lib_dir)
    db.add(crashed)
    db.commit()
    crashed_id = crashed.id
with Session() as db:
    row = db.query(em.Library).filter(em.Library.id == crashed_id).first()
    open_run_crash = sc.begin_scan(db, row)
# 把开始时间推回 7 小时前：阈值（SCAN_STALE_HOURS，默认 6）之外才算崩溃残留
from datetime import datetime, timedelta  # noqa: E402

with Session() as db:
    old = datetime.now() - timedelta(hours=7)
    db.query(em.Library).filter(em.Library.id == crashed_id).update(
        {"updated_at": old, "last_scan_at": old, "scan_started_at": old},
        synchronize_session=False)
    db.query(em.ScanRun).filter(em.ScanRun.id == open_run_crash).update(
        {"started_at": old}, synchronize_session=False)
    db.commit()
with Session() as db:
    closed = maint.close_stale_scan_runs(db)
    reset = maint.reset_stale_scan_flags(db)
with Session() as db:
    crash_lib = db.query(em.Library).filter(em.Library.id == crashed_id).first()
    crash_run = db.query(em.ScanRun).filter(em.ScanRun.id == open_run_crash).first()
check("崩溃残留：is_scanning 复位、最近一次状态收尾成 interrupted（已中断，不是失败）",
      reset >= 1 and crash_lib.is_scanning is False and crash_lib.scan_status == "interrupted",
      f"reset={reset} is_scanning={crash_lib.is_scanning} status={crash_lib.scan_status}")
check("崩溃残留：原因写明是进程重启（不是含糊的“失败”）",
      "进程重启" in (crash_lib.scan_error or ""), repr(crash_lib.scan_error))
check("崩溃残留：流水里那条「还在跑」被收尾成 interrupted 且有结束时间",
      closed >= 1 and crash_run.status == "interrupted" and crash_run.finished_at is not None,
      f"closed={closed} status={crash_run.status} finished={crash_run.finished_at}")
# 阈值内的「正在跑」不能动：多机部署时另一个节点可能正在扫这个库
with Session() as db:
    peer = em.Library(guid="g" * 32, name="另一台节点正在扫的库", collection_type="movies",
                      paths=lib_dir)
    db.add(peer)
    db.commit()
    peer_id = peer.id
with Session() as db:
    row = db.query(em.Library).filter(em.Library.id == peer_id).first()
    peer_run = sc.begin_scan(db, row)
with Session() as db:
    kept = maint.close_stale_scan_runs(db)
with Session() as db:
    peer_row = db.query(em.Library).filter(em.Library.id == peer_id).first()
    peer_run_row = db.query(em.ScanRun).filter(em.ScanRun.id == peer_run).first()
check("阈值内（刚开跑/别的节点正在扫）不误判",
      kept == 0 and peer_run_row.status == "running" and peer_row.is_scanning is True,
      f"closed={kept} status={peer_run_row.status} is_scanning={peer_row.is_scanning}")
with Session() as db:  # 收尾：别给后面的断言留一条永久 running
    row = db.query(em.Library).filter(em.Library.id == peer_id).first()
    sc.fail_scan(db, row, RuntimeError("清理测试残留"), peer_run)

# 媒体库删除后由维护周期回收（流水不能用外键挡住删库）
with Session() as db:
    fresh = em.Library(guid="h" * 32, name="建完就删的库", collection_type="movies",
                       paths=lib_dir)
    db.add(fresh)
    db.commit()
    gone_id = fresh.id
with Session() as db:
    row = db.query(em.Library).filter(em.Library.id == gone_id).first()
    sc.scan_library_sync(db, row, sc.LibrarySnapshot.of(row))
with Session() as db:
    before_prune = db.query(em.ScanRun).filter(em.ScanRun.library_id == gone_id).count()
with Session() as db:
    db.query(em.Library).filter(em.Library.id == gone_id).delete(synchronize_session=False)
    db.commit()
with Session() as db:
    pruned = maint.prune_scan_runs(db)
with Session() as db:
    after_prune = db.query(em.ScanRun).filter(em.ScanRun.library_id == gone_id).count()
check("媒体库删除后流水被维护周期回收",
      before_prune == 1 and pruned >= 1 and after_prune == 0,
      f"删前 {before_prune} 条 → 回收 {pruned} → 剩 {after_prune} 条")
check("其他库的流水不受影响",
      all(r["status"] for r in runs(50)) and len(runs(50)) >= 3, f"{len(runs(50))} 条")

# 接口形态：字段齐全（管理端抽屉直接用）
with Session() as db:
    payload = emby_portal.list_library_scans(lib_id, limit=5, staff=None, db=db)
check("接口返回 library_id / keep / runs",
      set(payload) == {"library_id", "keep", "runs"} and payload["library_id"] == lib_id,
      str(sorted(payload)))
check("接口的 runs 与 scanner 口径一致、带上限",
      payload["runs"] == runs(5) and payload["keep"] == sc.SCAN_RUN_KEEP,
      f"keep={payload['keep']}")

# ==================== 10. 按来源拆分的统计 ====================
print("\n=== 10. 按来源拆分 ===")
from backend.emby_server import mounts as mnt  # noqa: E402

src_a = tempfile.mkdtemp(prefix="scansrc_a_")
src_b = tempfile.mkdtemp(prefix="scansrc_b_")
src_empty = tempfile.mkdtemp(prefix="scansrc_empty_")
for i in range(2):
    with open(os.path.join(src_a, f"Source A {i} (2020).mkv"), "wb") as f:
        f.write(b"\x00" * 1024)
with open(os.path.join(src_b, "Source B (2021).mkv"), "wb") as f:
    f.write(b"\x00" * 1024)
missing_src = os.path.join(src_a, "does-not-exist-either")


def runs_for(lid: int, limit: int = 5) -> list:
    with Session() as db:
        return sc.scan_runs_payload(db, lid, limit=limit)


with Session() as db:
    slib = em.Library(guid="s" * 32, name="来源明细库", collection_type="movies",
                      paths=f"{src_a},{src_b},{src_empty}")
    db.add(slib)
    db.commit()
    sid = slib.id


def scan_sid(trigger: str = "manual") -> dict:
    with Session() as db:
        row = db.query(em.Library).filter(em.Library.id == sid).first()
        return sc.scan_library_sync(db, row, sc.LibrarySnapshot.of(row), trigger)


def last_scan(lid: int) -> dict:
    with Session() as db:
        row = db.query(em.Library).filter(em.Library.id == lid).first()
        return sc.scan_result_payload(row)


first = scan_sid()
check("按来源逐条记账（顺序 = 媒体库里的配置顺序）",
      [s["label"] for s in first["sources"]] == [src_a, src_b, src_empty],
      str([s["label"] for s in first["sources"]]))
check("每条来源的文件数就是它自己扫到的文件数（含空目录那条 0）",
      [s["files"] for s in first["sources"]] == [2, 1, 0],
      str([(s["label"].rsplit("/", 1)[-1], s["files"]) for s in first["sources"]]))
check("来源明细的新增 / 探测加起来正好是总计（不重不漏）",
      sum(s["added"] for s in first["sources"]) == first["added"] == 3
      and sum(s["probed"] for s in first["sources"]) == first["probed"] == 3,
      f"详细 {[s['added'] for s in first['sources']]} 总计 {first['added']}")
stored_latest = last_scan(sid)
check("空目录是一条正常来源（0 个文件、没有错误），不算来源失败",
      stored_latest["sources"][2]["files"] == 0
      and "error" not in stored_latest["sources"][2]
      and stored_latest["failed_roots"] == []
      and stored_latest["status"] == "success",
      str(stored_latest["sources"][2]))
check("来源明细落库（换个 Session 读得到，刷新页面不丢）",
      [s["label"] for s in stored_latest["sources"]] == [src_a, src_b, src_empty]
      and stored_latest["sources"][0]["added"] == 2
      and stored_latest["sources"][1]["added"] == 1,
      str([(s["label"].rsplit("/", 1)[-1], s["added"]) for s in stored_latest["sources"]]))
check("流水里也带当轮的来源拆解（抽屉里能看历史）",
      [s["files"] for s in runs_for(sid)[0]["sources"]] == [2, 1, 0],
      str([s["files"] for s in runs_for(sid)[0]["sources"]]))

# 读不到的来源：明细里也要有一条（不是凭空少一条）
with Session() as db:
    row = db.query(em.Library).filter(em.Library.id == sid).first()
    row.paths = f"{src_a},{missing_src},{src_empty}"
    db.commit()
partial_src = scan_sid()
check("读不到的来源也在明细里：kind=unavailable + 原因 + 文件数 0",
      [s["label"] for s in partial_src["sources"]] == [src_a, src_empty, missing_src]
      and partial_src["sources"][2].get("kind") == "unavailable"
      and "不存在" in (partial_src["sources"][2].get("error") or "")
      and partial_src["sources"][2]["files"] == 0,
      str(partial_src["sources"]))
check("增量跳过未变文件时，来源明细报的是**发现数**不是写库数（全不变的来源也要报全）",
      partial_src["sources"][0]["files"] == 2 and partial_src["sources"][0]["added"] == 0
      and partial_src["sources"][1]["files"] == 0,
      str([(s["label"].rsplit("/", 1)[-1], s["files"], s.get("added")) for s in partial_src["sources"]]))
check("不可用来源的明细形状与可用来源一致（计数器都在，消费方不用分支）",
      all(sc.SCAN_SOURCE_COUNTERS and set(sc.SCAN_SOURCE_COUNTERS) <= set(s)
          for s in partial_src["sources"]),
      str(sorted(partial_src["sources"][2])))
check("该来源仍照旧计入 failed_roots、整轮仍是 partial（原有护栏不变）",
      partial_src["removal_skipped"] is True
      and any(missing_src in r for r in partial_src["failed_roots"]),
      str(partial_src["failed_roots"]))

# 挂载半路炸：只影响它自己那一条明细
mount_dir = tempfile.mkdtemp(prefix="scansrc_mount_")
with open(os.path.join(mount_dir, "Mounted (2022).mkv"), "wb") as f:
    f.write(b"\x00" * 1024)
with Session() as db:
    mount_row = em.StorageMount(name="半路炸挂载", mount_type="local", path=mount_dir,
                                config=mnt.dump_config({}), is_enabled=True)
    db.add(mount_row)
    db.commit()
    mid = mount_row.id
    row = db.query(em.Library).filter(em.Library.id == sid).first()
    row.paths = src_a
    row.mount_ids = str(mid)
    db.commit()

real_mount_files = sc._mount_files


def boom_mount_files(src, provider, failed_roots, prefixes=()):  # noqa: ANN001, ARG001
    raise mnt.MountError("模拟挂载读一半炸了")
    yield  # pragma: no cover


sc._mount_files = boom_mount_files
try:
    broken = scan_sid()
finally:
    sc._mount_files = real_mount_files

check("挂载半路炸：这条来源的明细带原因、文件数按实际算",
      broken["sources"][1]["label"].startswith("半路炸挂载")
      and "模拟挂载读一半炸了" in (broken["sources"][1].get("error") or "")
      and broken["sources"][1]["files"] == 0,
      str(broken["sources"][1]))
check("挂载半路炸：同一轮里别的来源照常记账（一个坏掉不连坐）",
      broken["sources"][0]["files"] == 2 and broken["sources"][0]["added"] == 0,
      str(broken["sources"][0]))
check("挂载半路炸：明细里的文件数归到正确的那条来源（不串到下一条头上）",
      broken["sources"][1]["files"] == 0
      and sum(s["files"] for s in broken["sources"]) == 2,
      str([(s["label"][:12], s["files"]) for s in broken["sources"]]))

# 恢复：明细跟着回到干净状态（不残留上一轮的错误）
with Session() as db:
    row = db.query(em.Library).filter(em.Library.id == sid).first()
    row.mount_ids = ""
    row.paths = src_a
    db.commit()
healed = scan_sid()
check("来源恢复后：明细只剩可用来源，且不残留上一轮的错误",
      [s["label"] for s in healed["sources"]] == [src_a]
      and all("error" not in s for s in healed["sources"]),
      str(healed["sources"]))

# 纯函数：条数封顶与坏数据清洗（写进库的 JSON 不该被一条烂数据或几百条来源撑爆）
big = [{"label": f"来源 {i}", "files": i, "added": 1} for i in range(sc.SCAN_SOURCES_MAX + 5)]
check("来源条数封顶到 SCAN_SOURCES_MAX",
      len(sc._encode_scan_sources(big)) == sc.SCAN_SOURCES_MAX,
      f"{len(sc._encode_scan_sources(big))} 条")
mixed = sc._encode_scan_sources([
    "垃圾", None, {"label": "好的", "files": 3, "added": -2, "kind": "local",
                   "error": "x" * 500}, {"files": 1}, {"label": "坏的", "files": "abc"},
])
check("坏条目只丢自己（非字典 / 没标签 / 计数不是数字都不带走整轮统计）",
      [s["label"] for s in mixed] == ["好的"], str([s.get("label") for s in mixed]))
check("明细清洗：负数归零、原因截断",
      mixed[0]["added"] == 0 and mixed[0]["files"] == 3
      and len(mixed[0]["error"]) == sc.SCAN_SOURCE_TEXT_MAX,
      str(mixed[0]))
check("来源明细随统计一起落库、原样读回",
      sc.decode_scan_stats(sc.encode_scan_stats(
          {"added": 1, "sources": mixed, "failed_roots": []}))["sources"] == mixed,
      str(sc.decode_scan_stats(sc.encode_scan_stats({"sources": mixed}))))
check("虚拟库没有来源 → sources 是空列表（前端不用特判 None）",
      last_scan(vid)["sources"] == [], str(last_scan(vid)["sources"]))

# ==================== 汇总 ====================
print()
if FAILED:
    print(f"❌ 扫描结果冒烟测试失败 {len(FAILED)}/{TOTAL} 项：")
    for label in FAILED:
        print(f"   - {label}")
    sys.exit(1)
print(f"✅ 扫描结果冒烟测试全部通过（{TOTAL} 项）")

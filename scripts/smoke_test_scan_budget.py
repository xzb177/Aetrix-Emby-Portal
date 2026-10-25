#!/usr/bin/env python3
"""扫描资源预算冒烟测试（扫描器不再「每个文件查库 + 串行等网络 + 每个文件一次提交」）

这个脚本守的是**资源行为**，不是功能正确性（功能在 smoke_test_emby / mounts / media_search 里）：

1. 批量查库 / 批量提交：处理 N 个文件时，条目查询只有个位数、提交只有个位数
   （老实现是“每个文件查一两次 + 每个文件一次 commit”，十万文件就是十万次 fsync）；
2. 并行探测：ffprobe 类调用同时在跑（老实现是逐条串行，一个 90 秒超时就能拖死整库）；
3. 并行刮削 + 不重复请求：TMDB 搜索/详情并发发起，写库线程随后调用的同一请求命中缓存，
   不会因为「预热 + 写库」各调一次就双倍消耗配额；
4. 目录只列一次：同一目录下几十个文件只 os.listdir 一次（老实现每个文件列两次：找图 + 找字幕），
   并发撞上同一目录（同目录文件分发到不同工作线程）也只列一次——单飞语义由脚本自己验证；
5. 清理阶段仍然工作（删掉文件 → 重扫 → removed 计数正确）。

用法：python scripts/smoke_test_scan_budget.py
"""
import os
import sys
import tempfile
import threading
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
# 本脚本数的是真实 TMDB 调用次数：磁盘缓存（第 7 批）会让第二次运行少调甚至不调，
# 调用计数断言就不再确定——这里显式关掉（缓存的正确性由 tests/test_tmdb_cache.py 守）。
os.environ["EMBY_TMDB_CACHE"] = "0"
DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.database import engine, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import scanner as sc  # noqa: E402

init_db()
Session = sessionmaker(bind=engine)

FAILED = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


# ==================== 造一批假媒体 ====================
MOVIES = 150
EPISODES_PER_SERIES = 6
SERIES = 2

lib_dir = tempfile.mkdtemp(prefix="scanbudget_")
movie_dir = os.path.join(lib_dir, "Movies")
os.makedirs(movie_dir, exist_ok=True)
for i in range(MOVIES):
    name = f"Budget Movie {i:03d} (2020)"
    with open(os.path.join(movie_dir, f"{name}.mkv"), "wb") as f:
        f.write(b"\x00" * 2048)
    with open(os.path.join(movie_dir, f"{name}.chi.srt"), "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    if i == 0:  # 本地海报（同目录，验证「目录只列一次」）
        with open(os.path.join(movie_dir, "poster.jpg"), "wb") as f:
            f.write(b"jpg")

series_dirs = []
for s in range(SERIES):
    sdir = os.path.join(lib_dir, f"Budget Show {s}", "Season 1")
    os.makedirs(sdir, exist_ok=True)
    series_dirs.append(sdir)
    for e in range(1, EPISODES_PER_SERIES + 1):
        path = os.path.join(sdir, f"Budget Show {s} S01E{e:02d}.mkv")
        with open(path, "wb") as f:
            f.write(b"\x00" * 2048)

db = Session()
lib = em.Library(guid="b" * 32, name="预算库", collection_type="movies", paths=lib_dir)
db.add(lib)
db.commit()
lib_id = lib.id
db.close()

TOTAL = MOVIES + SERIES * EPISODES_PER_SERIES

# ==================== 观测装置 ====================
queries = {"n": 0, "item_selects": 0, "guid_point": 0}
commits = {"n": 0}
_watching = {"on": False}


@event.listens_for(engine, "before_cursor_execute")
def _count_queries(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    if not _watching["on"]:
        return
    queries["n"] += 1
    head = statement.lstrip()[:6].upper()
    if head == "SELECT" and "emby_items" in statement:
        queries["item_selects"] += 1
        # 「剧集/季」这一类按 guid 的单条点查：批量预取之后不应该再出现
        # （批次加载与父级扇入用的是 IN (...)，不会匹配到这里）
        if "guid = ?" in statement:
            queries["guid_point"] += 1


@event.listens_for(engine, "commit")
def _count_commits(conn):  # noqa: ANN001
    if _watching["on"]:
        commits["n"] += 1


class Tracker:
    """记录「同时在跑」的峰值：用来证明 IO 是并行的，而不是串行等待"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.calls = 0

    def __enter__(self):
        with self.lock:
            self.live += 1
            self.calls += 1
            self.peak = max(self.peak, self.live)

    def __exit__(self, *exc):
        with self.lock:
            self.live -= 1
        return False


probe = Tracker()
tmdb = Tracker()
listdir_calls = {"n": 0}
real_listdir = os.listdir


def fake_probe_metadata(path, headers=None, size=0):  # noqa: ANN001
    with probe:
        time.sleep(0.01)  # 模拟 ffprobe 的等待（真实场景是几十到几百毫秒）
    return {
        "duration_ticks": 600_000_000, "bitrate": 1_000_000, "width": 1920, "height": 1080,
        "video_codec": "H264", "audio_codec": "AAC", "audio_languages": "chi",
        "subtitle_languages": "", "size": size or 2048,
        "streams": [
            {"stream_index": 0, "stream_type": "Video", "codec": "h264", "language": "",
             "display_title": None, "title": None, "channels": None, "bit_rate": 1_000_000},
            {"stream_index": 1, "stream_type": "Audio", "codec": "aac", "language": "chi",
             "display_title": None, "title": None, "channels": 2, "bit_rate": 128_000},
        ],
    }


class FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class FakeTmdbSession:
    def get(self, url, params=None):  # noqa: ANN001
        with tmdb:
            time.sleep(0.01)
        if "/search/" in url:
            query = (params or {}).get("query") or ""
            return FakeResp({"results": [{"id": abs(hash(query)) % 900000 + 1,
                                          "title": query, "name": query, "vote_average": 7.0}]})
        return FakeResp({
            "poster_path": "/poster.jpg", "backdrop_path": "/backdrop.jpg",
            "external_ids": {"imdb_id": "tt0000001"},
            "alternative_titles": {"titles": [{"title": "别名"}]},
        })


def counting_listdir(path):
    listdir_calls["n"] += 1
    return real_listdir(path)


# ==================== 跑扫描 ====================
sc.probe_metadata = fake_probe_metadata
os.listdir = counting_listdir
sc.tmdb_client.session = FakeTmdbSession()
sc.tmdb_client.api_keys = ["fake-key"]
sc.tmdb_client.api_key = "fake-key"
sc.tmdb_client._cache.clear()

db = Session()
lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
tracemalloc.start()
_watching["on"] = True
started = time.time()
stats = sc.scan_library_sync(db, lib)
elapsed = time.time() - started
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
_watching["on"] = False
db.close()
os.listdir = real_listdir

items = Session().query(em.MediaItem).all()
counts = {}
for it in items:
    counts[it.item_type] = counts.get(it.item_type, 0) + 1
subs = Session().query(em.MediaStream).filter(em.MediaStream.is_external.is_(True)).count()

print("\n—— 扫描结果 ——")
print(f"  统计: {stats}")
print(f"  条目: {counts} | 外挂字幕轨: {subs}")
print(f"  耗时: {elapsed:.2f}s | 查询: {queries['n']} 次（其中条目表 SELECT {queries['item_selects']} 次）"
      f" | 提交: {commits['n']} 次 | 探测峰值并发: {probe.peak} | 刮削峰值并发: {tmdb.peak}"
      f" | TMDB 调用: {tmdb.calls} | os.listdir: {listdir_calls['n']}"
      f" | Python 峰值内存: {peak / 1024 / 1024:.1f} MB\n")

print("—— 资源预算断言 ——")
check(stats["added"] == TOTAL, "整库入库条目数正确", f"added={stats['added']} 期望={TOTAL}")
check(counts.get("movie") == MOVIES, "电影条目数正确", str(counts.get("movie")))
check(counts.get("series") == SERIES, "剧集条目数正确", str(counts.get("series")))
check(counts.get("episode") == SERIES * EPISODES_PER_SERIES, "集条目数正确", str(counts.get("episode")))

# 批量查库：老实现每个文件至少 1 次条目查询 → 至少 162 次；现在只有批次加载（每 400 个文件 1 次）。
check(queries["item_selects"] <= 12, "条目按批加载（不再每个文件查一次）",
      f"SELECT emby_items={queries['item_selects']} 上限=12（共 {TOTAL} 个文件）")
# 剧集/季：老实现在写库循环里对**每一集**点查剧集、再点查季（{SERIES*EPISODES_PER_SERIES} 集 = 24 次）。
# 现在是批次开头一次 IN 查询 + 本次扫描内复用，循环里不再有任何按 guid 的点查。
check(queries["guid_point"] == 0, "逐集查剧集/季已消除（循环里没有 guid 点查）",
      f"guid 点查={queries['guid_point']} 次（旧实现≥{SERIES * EPISODES_PER_SERIES * 2} 次）")
# 批量提交：老实现每个文件一次 → 162+ 次 fsync；批次化之后只有开跑 / 每批 / 收尾几次。
check(commits["n"] <= 10, "按批提交（不是每个文件一次）", f"commit={commits['n']}")
check(probe.peak >= 2, "探测（ffprobe）并行执行", f"峰值并发={probe.peak}")
if sc.SCAN_LAYERED:
    # 分层 L1 不做 TMDB（慢网络 IO 移到 enrich_worker 后台补），这里不断言并行度；
    # TMDB 并行能力由 enrich_worker 的令牌桶 + 多线程保证，另有专项测试覆盖。
    check(tmdb.calls == 0, "分层 L1 不调 TMDB（留给后台 worker）", f"调用={tmdb.calls}")
else:
    check(tmdb.peak >= 2, "刮削（TMDB）并行执行", f"峰值并发={tmdb.peak}")
# 预热 + 写库各调一次，但缓存让网络只发生一次：调用数 ≈ 唯一片名的 2 倍（搜索 + 详情）
check(tmdb.calls <= (MOVIES + SERIES) * 2 + 4, "同一片名不重复请求 TMDB（缓存生效）",
      f"调用={tmdb.calls} 上限={(MOVIES + SERIES) * 2 + 4}")

# 本机目录：1 个电影目录 + 2 个季目录（每目录只应列一次；旧实现每文件两次）
expected_dirs = 1 + SERIES
check(listdir_calls["n"] <= expected_dirs + 2, "同一目录只列举一次",
      f"listdir={listdir_calls['n']} 上限={expected_dirs + 2}")

# 同一个目录下的多个文件会分发到不同工作线程：并发撞上同一目录时也必须只真列一次。
# 用屏障把 8 个线程同时放进去，不靠运气（CI 上曾出现 3 个目录列了 6 次）。
one_dir = tempfile.mkdtemp(prefix="scanbudget_shared_")
with open(os.path.join(one_dir, "a.mkv"), "wb") as f:
    f.write(b"\x00" * 128)
baseline = listdir_calls["n"]
sc.clear_dir_cache()
gate = threading.Barrier(8)
raced: list = []


def _list_same_dir():
    try:
        gate.wait(timeout=10)
        sc._list_dir_cached(one_dir)
    except Exception as exc:  # noqa: BLE001 — 记下来，下方断言会暴露
        raced.append(exc)


os.listdir = counting_listdir  # 这一小段重新装观测装置（大扫描那段已还原）
try:
    workers = [threading.Thread(target=_list_same_dir) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
    delta = listdir_calls["n"] - baseline
finally:
    os.listdir = real_listdir
check(not raced and delta == 1, "多线程同时要同一目录也只列一次",
      f"listdir={delta} 期望=1" + (f" 异常={raced}" if raced else ""))
check(subs == MOVIES, "外挂字幕按文件识别入库", f"字幕轨={subs}")

# ==================== 清理阶段仍然工作 ====================
# 缺失宽限（2026-10-24 批一 #6）：文件消失后不再一轮就下架，要连续缺失
# SCAN_MISSING_GRACE_SCANS 轮（默认 3）才清理。这里按真实默认口径
# 端到端验：重扫满宽限轮数，累计 removed 必须恰好是那 1 条——
# 少了是清理坏了，多了是误删了别的。
victim = os.path.join(movie_dir, "Budget Movie 000 (2020).mkv")
os.remove(victim)
total_removed = 0
for _ in range(sc.MISSING_GRACE_SCANS):
    db = Session()
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    again = sc.scan_library_sync(db, lib)
    db.close()
    total_removed += again["removed"]
check(total_removed == 1, "文件消失后重扫只清理该条目",
      f"removed={total_removed}（宽限 {sc.MISSING_GRACE_SCANS} 轮累计）")

print()
if FAILED:
    print(f"❌ 扫描资源预算冒烟失败（{len(FAILED)} 项）：" + "；".join(FAILED))
    sys.exit(1)
print("✅ 扫描资源预算冒烟测试全部通过")

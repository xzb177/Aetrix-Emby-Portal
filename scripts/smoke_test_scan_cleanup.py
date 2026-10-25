#!/usr/bin/env python3
"""清理阶段冒烟测试（快速路径与它的边界）

清理阶段要回答的问题是：「来源里已经没有的条目有哪些」。老实现在**每一轮扫描**都遍历
整库条目（十万条目：约 2.2 秒，即使什么都没变）。现在多了一条快速路径：两次计数就能证明
「不可能有要清理的条目」（十万条目：约 40 ms）。

快速路径的**安全性**是这条测试的重点——它必须只在确实没有可清理条目时才生效：

1. 稳定库（文件与条目一一对应）→ 走快速路径、不遍历、removed=0；
2. 删掉一个文件 → 不走快速路径、正确删掉那一条、其余完好；
3. 残留的**无子条目**季 / 剧（历史扫描留下的） → 不走快速路径、被清掉（这是条件 2 守的）；
4. 条目行被外部删掉但文件还在 → 不走快速路径、重新入库，不会误删任何东西；
5. 强制小批次（CLEANUP_BATCH=2）→ 游标跨批推进仍然正确。

用法：python scripts/smoke_test_scan_cleanup.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.database import engine, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import scanner as sc  # noqa: E402
from backend.emby_server import soft_delete  # noqa: E402

init_db()
Session = sessionmaker(bind=engine)

FAILED = []


def check(label: str, ok: bool, detail: str = "") -> None:
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


MOVIES = 6
SERIES = 2
EPISODES = 3

lib_dir = tempfile.mkdtemp(prefix="scancleanup_")
movie_dir = os.path.join(lib_dir, "Movies")
os.makedirs(movie_dir, exist_ok=True)
for i in range(MOVIES):
    with open(os.path.join(movie_dir, f"Cleanup Movie {i:02d} (2021).mkv"), "wb") as f:
        f.write(b"\x00" * 2048)
for s in range(SERIES):
    sdir = os.path.join(lib_dir, f"Cleanup Show {s}", "Season 1")
    os.makedirs(sdir, exist_ok=True)
    for e in range(1, EPISODES + 1):
        with open(os.path.join(sdir, f"Cleanup Show {s} S01E{e:02d}.mkv"), "wb") as f:
            f.write(b"\x00" * 2048)

TOTAL = MOVIES + SERIES * EPISODES          # 文件类条目（movie + episode）
PARENTS = SERIES + SERIES                   # 剧 + 季（无实体文件，随集建立）

sc.probe_metadata = fake_probe
sc.tmdb_client.session = FakeTmdbSession()
sc.tmdb_client.api_keys = ["fake-key"]
sc.tmdb_client.api_key = "fake-key"

with Session() as db:
    lib = em.Library(guid="c" * 32, name="清理测试库", collection_type="movies", paths=lib_dir)
    db.add(lib)
    db.commit()
    lib_id = lib.id


def scan() -> dict:
    with Session() as db:
        lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
        return sc.scan_library_sync(db, lib)


def item_count() -> int:
    """库里**看得见**的条目数

    清理阶段自 v2.48.0 起是软删除：消失的条目只标记 `deleted_at`，行还在。因此这里
    必须数可见行（``count_visible``）——``Query.count()`` 会把语句包成子查询，全局可见性
    过滤下不到那里去，用它会把已下架的行也算进来，而这个脚本要验的正是「用户还剩多少条目」。
    """
    with Session() as db:
        return soft_delete.count_visible(db, em.MediaItem.library_id == lib_id)


def visible_count(*conditions) -> int:
    """给定条件下看得见的条目数（下架的行不算）"""
    with Session() as db:
        return soft_delete.count_visible(db, *conditions)


# ==================== 1. 首次扫描（本来就是干净的） ====================
print("=== 1. 首次扫描 ===")
first = scan()
check("整库入库条目数正确", first["added"] == TOTAL,
      f"added={first['added']} 期望={TOTAL}（文件类条目）")
check("剧与季随集自动建立", item_count() == TOTAL + PARENTS,
      f"条目={item_count()} 期望={TOTAL + PARENTS}")
check("首次扫描后库就是干净的 → 走快速路径", sc._CLEANUP_LAST["fast_path"] is True,
      f"cleanup={sc._CLEANUP_LAST}")
check("快速路径不遍历整库", sc._CLEANUP_LAST["walked"] == 0, f"walked={sc._CLEANUP_LAST['walked']}")

# ==================== 2. 稳定库重扫：快速路径且不删任何东西 ====================
print("\n=== 2. 稳定库重扫 ===")
started = time.perf_counter()
second = scan()
elapsed_ms = (time.perf_counter() - started) * 1000
check("稳定库没有删除", second["removed"] == 0, f"removed={second['removed']}")
check("重扫仍走快速路径、不遍历整库",
      sc._CLEANUP_LAST["fast_path"] is True and sc._CLEANUP_LAST["walked"] == 0,
      f"cleanup={sc._CLEANUP_LAST}")
check("清理阶段耗时可忽略（不是遍历整库）", sc._CLEANUP_LAST["elapsed_ms"] < 200,
      f"{sc._CLEANUP_LAST['elapsed_ms']:.0f} ms（整轮扫描 {elapsed_ms:.0f} ms）")

# ==================== 3. 文件消失：宽限累计后精确删除 ====================
print("\n=== 3. 文件消失 ===")
victim = os.path.join(movie_dir, "Cleanup Movie 00 (2021).mkv")
os.remove(victim)
# 缺失宽限（2026-10-24 批一 #6）：前 GRACE-1 轮只累计缺失计数、条目仍在；
# 第 GRACE 轮才真正下架。逐轮验，防「宽限被绕过」与「永远不下架」两头坏。
third = None
for round_no in range(1, sc.MISSING_GRACE_SCANS + 1):
    third = scan()
    if round_no < sc.MISSING_GRACE_SCANS:
        check(f"缺失第 {round_no} 轮仍在宽限期（不下架、条目仍在）",
              third["removed"] == 0 and item_count() == TOTAL + PARENTS,
              f"removed={third['removed']} 条目={item_count()}")
check("消失的条目被删除（且只删它）", third["removed"] == 1, f"removed={third['removed']}")
check("这一轮没有走快速路径（有东西要清理）", sc._CLEANUP_LAST["fast_path"] is False,
      f"cleanup={sc._CLEANUP_LAST}")
# 遍历走过的行数 = 清理结束后的条目数 + 本轮删掉的条数（遍历在扫描写真之后、删除之前）
check("遍历确实走过整库（库内全部条目，含剧/季）",
      sc._CLEANUP_LAST["walked"] == item_count() + third["removed"],
      f"walked={sc._CLEANUP_LAST['walked']} 剩余={item_count()} removed={third['removed']}")
check("其余条目完好（只少了那一条）", item_count() == TOTAL + PARENTS - 1,
      f"剩余={item_count()} 期望={TOTAL + PARENTS - 1}")

# 再扫一次应当又回到快速路径
fourth = scan()
check("清理干净后重扫回到快速路径",
      fourth["removed"] == 0 and sc._CLEANUP_LAST["fast_path"] is True,
      f"cleanup={sc._CLEANUP_LAST}")

# ==================== 4. 残留的无子条目季 / 剧 ====================
print("\n=== 4. 残留的无子条目季 / 剧（历史上可能留下） ===")
with Session() as db:
    series = db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib_id, em.MediaItem.item_type == "series").first()
    orphan_season = em.MediaItem(
        guid="orphan-season-1", library_id=lib_id, item_type="season", name="第 9 季",
        parent_id=series.id, series_id=series.id, season_number=9,
    )
    db.add(orphan_season)
    db.commit()
fifth = scan()
check("无子条目的季被识别并清理（不走快速路径）",
      fifth["removed"] == 1 and sc._CLEANUP_LAST["fast_path"] is False,
      f"removed={fifth['removed']} fast_path={sc._CLEANUP_LAST['fast_path']}")
with Session() as db:
    left = db.query(em.MediaItem).filter(em.MediaItem.guid == "orphan-season-1").first()
check("那条残留季确实没了、同剧的集没被牵连",
      left is None and item_count() == TOTAL + PARENTS - 1,
      f"剩余季={0 if left is None else 1} 条目={item_count()}")

with Session() as db:
    db.add(em.MediaItem(guid="orphan-series-1", library_id=lib_id, item_type="series",
                        name="孤儿剧", sort_name="orphan"))
    db.commit()
sixth = scan()
check("无子条目的剧也被清理", sixth["removed"] == 1, f"removed={sixth['removed']}")
with Session() as db:
    left_series = db.query(em.MediaItem).filter(
        em.MediaItem.guid == "orphan-series-1").first()
check("孤儿剧已删除、其余条目不受影响",
      left_series is None and item_count() == TOTAL + PARENTS - 1, f"剩余={item_count()}")

# ==================== 5. 行被外部删掉（文件还在）：扫描先把它补回来，不会误删 ====================
print("\n=== 5. 条目行缺失但文件还在 ===")
with Session() as db:
    row = db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib_id, em.MediaItem.item_type == "movie").first()
    missing_guid, missing_name = row.guid, row.name
    db.delete(row)
    db.commit()
seventh = scan()
check("文件还在 → 条目被重新入库（不会当成删除）",
      seventh["added"] == 1 and seventh["removed"] == 0, f"{seventh}")
with Session() as db:
    restored = db.query(em.MediaItem).filter(em.MediaItem.guid == missing_guid).first()
check("重新入库的是同一个 guid（路径哈希稳定）", restored is not None,
      f"guid={missing_guid} {missing_name}")
check("行补回之后库又是干净的 → 回到快速路径",
      sc._CLEANUP_LAST["fast_path"] is True and sc._CLEANUP_LAST["walked"] == 0,
      f"cleanup={sc._CLEANUP_LAST}")
check("条目总数回到删除前", item_count() == TOTAL + PARENTS - 1, f"剩余={item_count()}")

# ==================== 6. 强制小批次：游标跨批推进 ====================
print("\n=== 6. 强制小批次（CLEANUP_BATCH=2） ===")
os.remove(os.path.join(movie_dir, "Cleanup Movie 03 (2021).mkv"))
real_batch = sc.CLEANUP_BATCH
sc.CLEANUP_BATCH = 2
eighth = None
eighth_removed = 0
try:
    # 同 §3：小批设置下连扫满宽限轮数，累计 removed 恰为 1
    for _ in range(sc.MISSING_GRACE_SCANS):
        eighth = scan()
        eighth_removed += eighth["removed"]
finally:
    sc.CLEANUP_BATCH = real_batch
check("小批次下依然精确删除那一条", eighth_removed == 1, f"removed={eighth_removed}")
check("小批次下遍历走完全库（跨批推进）",
      sc._CLEANUP_LAST["walked"] == item_count() + eighth["removed"],
      f"walked={sc._CLEANUP_LAST['walked']} 剩余={item_count()} removed={eighth['removed']}")

print()
if FAILED:
    print(f"❌ 清理阶段冒烟测试失败 {len(FAILED)} 项：")
    for label in FAILED:
        print(f"   - {label}")
    sys.exit(1)
print("✅ 清理阶段冒烟测试全部通过")

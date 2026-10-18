#!/usr/bin/env python3
"""长期运行稳定性冒烟（维护看护线程 + 扫描清理的资源与数据完整性）

这个脚本守的是「跑得久、不越跑越重」，不是新功能：

1. **崩溃残留的收尾**：进程被强杀后 `is_scanning` 会停在 True（界面永远“扫描中”、
   「刷新全部」永远跳过这台库），启动维护要能把**真正残留**的复位，同时
   **绝不误清正在跑的扫描**（本进程注册在扫的、或刚在别的机器上开始的）；
2. **播放会话回收**：客户端异常断开不会上报 Stopped，靠心跳过期收掉，
   且结束时间要贴近事实（用上次心跳，而不是收尾那一刻）；
3. **字幕临时文件**：只增不减的字幕缓存；
4. **健康检查是常数级**：目录统计有上限，残留再多也不会把探针拖慢；
5. **扫描清理阶段**：十万级库也不整库载入内存（游标分批，置入更小的批大小仍正确），
   删条目时连 `UserMediaData`（播放进度/收藏）一起清——它没有 ORM 级联，
   老实现留下的是永远指向不存在条目的孤儿行。
6. **分类关联表的兜底**（v2.15.0）：关联行由 ORM flush 钩子在业务事务里同步；批量 Core 删除
   （扫描清理就是这条路径）走不到钩子，孤儿行得靠看护周期清掉——这里验证兜底真的会发生。

用法：python scripts/smoke_test_maintenance.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_TYPE"] = "sqlite"
os.environ["REDIS_ENABLED"] = "false"
_DB = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
# 空值也算没设（配置自愈会往 .env 物化出空的 SECRET_KEY，setdefault 只认「不存在」）
os.environ["SECRET_KEY"] = (os.environ.get("SECRET_KEY") or "").strip() or \
    "smoke-maintenance-secret-key-0123456789abcdef"

from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import models as web  # noqa: E402
from backend.database import engine, init_db  # noqa: E402
from backend.emby_server import maintenance as maint  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import scanner as sc  # noqa: E402
from backend.emby_server import soft_delete  # noqa: E402
from backend.emby_server import subtitles  # noqa: E402

init_db()
Session = sessionmaker(bind=engine)

FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


# ==================== 1. 残留「扫描中」标志的复位口径 ====================

now = datetime.now()
db = Session()
fresh = em.Library(guid="f" * 32, name="正在扫（本进程）", paths="/tmp/none",
                   is_scanning=True, updated_at=now - timedelta(hours=10))
stale = em.Library(guid="s" * 32, name="崩溃残留", paths="/tmp/none",
                   is_scanning=True, updated_at=now - timedelta(hours=10))
just_started = em.Library(guid="j" * 32, name="别处刚开始扫", paths="/tmp/none",
                          is_scanning=True, updated_at=now)
idle = em.Library(guid="i" * 32, name="没在扫", paths="/tmp/none",
                  is_scanning=False, updated_at=now - timedelta(days=3))
db.add_all([fresh, stale, just_started, idle])
db.commit()
ids = {x.name: x.id for x in (fresh, stale, just_started, idle)}
db.close()

# 本进程确实在扫（注册表登记）——即便行看起来很久没动，也不能碰
sc._ACTIVE_SCANS[ids["正在扫（本进程）"]] = now - timedelta(hours=10)
try:
    db = Session()
    reset = maint.reset_stale_scan_flags(db, stale_hours=6)
    flags = {lib.id: lib.is_scanning for lib in db.query(em.Library).all()}
    db.close()
    check(reset == 2, "复位所有非本进程的残留标志（v2：单节点启动即复位）", f"复位={reset}")
    check(flags[ids["正在扫（本进程）"]] is True, "本进程正在扫的库不被误清")
    check(flags[ids["崩溃残留"]] is False, "崩溃残留的标志被复位")
    check(flags[ids["别处刚开始扫"]] is False, "刚开始不久的也复位（单节点，启动时本进程不可能在扫）")
    check(flags[ids["没在扫"]] is False, "本来没在扫的库保持原样")

    # 幂等：正在扫的那台还在跑，再跑一次维护不应该又找到“残留”
    db = Session()
    check(maint.reset_stale_scan_flags(db, stale_hours=6) == 0,
          "复位是幂等的（正在扫的库仍在扫）")
    db.close()
finally:
    sc._ACTIVE_SCANS.pop(ids["正在扫（本进程）"], None)


# ==================== 2. 过期播放会话回收 ====================

db = Session()
db.add_all([
    em.PlaybackSession(session_key="s-ghost", last_update_at=now - timedelta(minutes=30)),
    em.PlaybackSession(session_key="s-live", last_update_at=now),
    em.PlaybackSession(session_key="s-done", last_update_at=now - timedelta(hours=2),
                       ended_at=now - timedelta(hours=2)),
])
db.commit()
reaped = maint.reap_stale_playback_sessions(db, stale_minutes=10)
rows = {s.session_key: s for s in db.query(em.PlaybackSession).all()}
db.close()

check(reaped == 1, "只回收心跳过期的会话", f"回收={reaped}")
check(rows["s-live"].ended_at is None, "心跳正常的会话不被动")
check(rows["s-ghost"].ended_at is not None, "过期的会话被标记结束")
check(rows["s-ghost"].ended_at <= now - timedelta(minutes=29),
      "结束时间贴近事实（用上次心跳，不是收尾那一刻）",
      f"ended_at={rows['s-ghost'].ended_at}")

# 会话表默认永久保留（历史统计口径不变）；显式设了保留期才裁
check(maint.SESSION_RETENTION_DAYS == 0, "默认不裁会话历史（保留期 0 = 永久）",
      f"SESSION_RETENTION_DAYS={maint.SESSION_RETENTION_DAYS}")
db = Session()
check(maint.prune_playback_sessions(db, retention_days=0) == 0,
      "保留期为 0 时什么都不做")
check(maint.prune_playback_sessions(db, retention_days=90) == 0,
      "保留期内不动历史会话")
db.close()

db = Session()
old = em.PlaybackSession(session_key="s-old", start_time=now - timedelta(days=200),
                         last_update_at=now - timedelta(days=200),
                         ended_at=now - timedelta(days=200))
still_playing = em.PlaybackSession(session_key="s-ancient-live",
                                   start_time=now - timedelta(days=200),
                                   last_update_at=now)
db.add_all([old, still_playing])
db.commit()
pruned = maint.prune_playback_sessions(db, retention_days=90)
keys = {s.session_key for s in db.query(em.PlaybackSession).all()}
db.close()
check(pruned == 1, "超出保留期的已结束会话被裁掉", f"裁掉={pruned}")
check("s-ancient-live" in keys, "正在播的会话永远不裁（即便开始很久）")
check("s-old" not in keys, "过老的已结束会话被清掉")


# ==================== 3. 字幕缓存淘汰（转码已删除） ====================

# 字幕缓存：过期先删，再按数量删最旧的
sub_dir = tempfile.mkdtemp(prefix="maint_subs_")
real_sub_dir = subtitles.SUB_CACHE_DIR
subtitles.SUB_CACHE_DIR = sub_dir
for i in range(5):
    p = os.path.join(sub_dir, f"{i}.vtt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n")
    ts = time.time() - (10 * 86400 if i == 0 else i * 60)
    os.utime(p, (ts, ts))
expired = maint.prune_subtitle_cache(max_files=3, max_age_days=7)
left = sorted(os.listdir(sub_dir))
check(expired == 2, "字幕缓存：过期的 1 个 + 超量的 1 个被淘汰",
      f"淘汰={expired} 剩余={left}")
check(left == ["1.vtt", "2.vtt", "3.vtt"], "保留的是较新的 3 个", f"剩余={left}")
subtitles.SUB_CACHE_DIR = real_sub_dir

# 健康检查：字幕缓存与磁盘余量
report = maint.resource_report()
check("subtitle_cache_files" in report, "运行期资源含字幕缓存统计")
check(report["disk_free_percent"] is not None, "运行期资源含磁盘余量")
check(maint.active_scan_count() == 0, "报告本进程正在扫描的库数")


# ==================== 5. 维护周期可反复执行且不抛异常 ====================

tick = maint.janitor_tick()
check(set(tick) == {"sessions_reaped", "sessions_pruned", "subtitle_cache_pruned",
                    # v2.42.9 第 7 批：TMDB 磁盘缓存巡检删掉的文件数（干净时 0，
                    # 参与全零断言），见 tmdb_cache.prune。
                    "tmdb_cache_pruned",
                    "item_facets_backfilled", "item_facets_orphans",
                    "scan_dir_states_pruned", "scan_runs_pruned",
                    "emby_tokens_purged",
                    "images_pruned", "images_freed_bytes",
                    "thumbs_backfilled",
                    # 缩略图补生成是布尔（干净时 True=已补完），不参与全零断言，
                    # 与 query_plans_optimized 同口径。
                    "thumbs_backfill_done",
                    "ai_usage_pruned",
                    # v2.35.0：SQLite 上周期性 PRAGMA optimize（查询计划不随库增长退化，见
                    # maintenance.optimize_query_plans）。它是一个布尔，所以不参与下面
                    # 「干净时什么都不做」的全零断言：SQLite 上跑了就是 True，非 SQLite 是 False。
                    "query_plans_optimized",
                    # perf/server-resource：空闲 GC 是否执行（布尔标记，非计数，同样不参与全零断言）
                    "idle_gc_done"},
      # 注 2026-10-17：strm_sig_refresh 已随 strm_gen/strm_sign 删除
      "维护周期返回可观测的计数", f"{tick}")
second = maint.janitor_tick()
check(all(v == 0 for key, v in second.items()
          if key not in ("query_plans_optimized", "thumbs_backfill_done", "idle_gc_done")),
      "维护周期可反复执行（干净时什么都不做）", f"{second}")


# ==================== 6. EM 启动维护与健康检查 ====================

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402

with TestClient(app) as client:  # with：跑一遍 lifespan（含启动维护 + 看护线程）
    health = client.get("/api/health")
    body = health.json() if health.status_code == 200 else {}
    check(health.status_code == 200, "健康检查返回 200（启动维护不会让它 500）",
          f"HTTP {health.status_code}")
    check("runtime" in body, "健康检查报告运行期资源")
    check(isinstance(body.get("runtime"), dict) and "active_scans" in body["runtime"],
          "运行期资源含正在扫描的库数", f"{body.get('runtime')}")


# ==================== 7. 扫描清理：分批游标 + 不留孤儿行 ====================

lib_dir = tempfile.mkdtemp(prefix="maint_lib_")
mov_dir = os.path.join(lib_dir, "Movies")
os.makedirs(mov_dir, exist_ok=True)
for i in range(12):
    with open(os.path.join(mov_dir, f"Maint Movie {i:03d} (2020).mkv"), "wb") as f:
        f.write(b"\x00" * 512)
season_dir = os.path.join(lib_dir, "Maint Show", "Season 1")
os.makedirs(season_dir, exist_ok=True)
for e in range(1, 4):
    with open(os.path.join(season_dir, f"Maint Show S01E{e:02d}.mkv"), "wb") as f:
        f.write(b"\x00" * 512)


def fake_probe_metadata(path, headers=None, size=0):  # noqa: ANN001
    return {
        "duration_ticks": 600_000_000, "bitrate": 1_000_000, "width": 1920, "height": 1080,
        "video_codec": "H264", "audio_codec": "AAC", "audio_languages": "chi",
        "subtitle_languages": "", "size": size or 512,
        "streams": [
            {"stream_index": 0, "stream_type": "Video", "codec": "h264", "language": "",
             "display_title": None, "title": None, "channels": None, "bit_rate": 1_000_000},
        ],
    }


real_probe = sc.probe_metadata
real_session = sc.tmdb_client.session
sc.probe_metadata = fake_probe_metadata
sc.tmdb_client.session = None  # 不联网刮削：本用例只关心清理阶段

db = Session()
lib = em.Library(guid="m" * 32, name="稳定性库", collection_type="mixed", paths=lib_dir)
db.add(lib)
db.commit()
lib_id = lib.id
db.close()

db = Session()
lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
first = sc.scan_library_sync(db, lib)
db.close()
check(first["added"] == 12 + 3, "首扫入库（12 电影 + 3 集；剧/季随集建立）",
      f"added={first['added']}")

# 给其中一个条目造出从属数据（播放进度 + 媒体流），随后让它消失
db = Session()
watcher = web.WebUser(username="maint_smoke_user", password_hash="x")
db.add(watcher)
db.commit()
victim = db.query(em.MediaItem).filter(
    em.MediaItem.library_id == lib_id, em.MediaItem.item_type == "movie").first()
victim_path = victim.file_path
victim_id = victim.id
db.add(em.UserMediaData(user_id=watcher.id, item_id=victim_id, playback_position_ticks=123,
                        play_count=1, is_favorite=True))
db.commit()
streams_before = db.query(em.MediaStream).filter(em.MediaStream.item_id == victim_id).count()
db.close()
check(streams_before >= 1, "条目带媒体流（用于验证清理时一并删除）", f"流={streams_before}")

os.remove(victim_path)
# 批大小刻意小于待清理条目数：游标必须能跨批推进并正常收尾
real_batch = sc.SCAN_BATCH
real_cleanup_batch = sc.CLEANUP_BATCH
sc.SCAN_BATCH = 4
sc.CLEANUP_BATCH = 4        # 清理阶段的批大小也要小于待清理条目数，才能真的跨批
try:
    db = Session()
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    second = sc.scan_library_sync(db, lib)
    db.close()
finally:
    sc.SCAN_BATCH = real_batch
    sc.CLEANUP_BATCH = real_cleanup_batch

db = Session()
left_item = db.query(em.MediaItem).filter(em.MediaItem.id == victim_id).first()
# count_visible 而不是 Query.count()：后者会把语句包成子查询，全局可见性过滤下不到那里去
remaining = soft_delete.count_visible(db, em.MediaItem.library_id == lib_id)
item_count = db.query(em.Library).filter(em.Library.id == lib_id).first().item_count
# 从属数据必须**留着**（v2.48.0 软删除：下架不是删除，误删还能连播放进度一起回来）
with soft_delete.include_deleted():
    hidden = db.query(em.MediaItem).filter(em.MediaItem.id == victim_id).first()
    left_stream = db.query(em.MediaStream).filter(em.MediaStream.item_id == victim_id).count()
    left_progress = db.query(em.UserMediaData).filter(
        em.UserMediaData.item_id == victim_id).count()
db.close()

sc.probe_metadata = real_probe
sc.tmdb_client.session = real_session

check(second["removed"] == 1, "小批大小下清理仍然正确（游标分批）", f"removed={second['removed']}")
check(left_item is None, "消失的条目不再可见（软删除）")
check(hidden is not None and hidden.deleted_at is not None, "下架的行仍然保留（可恢复）")
check(left_stream == streams_before, "媒体流保留（文件回来即可用）", f"流={left_stream}")
check(left_progress == 1, "播放进度/收藏保留（不再因为一次下架就丢）")
check(remaining == 12 + 1 + 1 + 3 - 1, "其余条目完好", f"剩余={remaining}")
# 计数口径是「电影 + 剧集」：11 部幸存的电影 + 1 部剧
check(item_count == 11 + 1, "库计数按电影/剧集口径更新", f"item_count={item_count}")

# ==================== 7b. 回收：隐藏够久才物理删除（连同从属数据）====================
db = Session()
with soft_delete.include_deleted():
    db.query(em.MediaItem).filter(em.MediaItem.id == victim_id).update(
        {"deleted_at": datetime.now() - timedelta(days=soft_delete.soft_delete_purge_days() + 1)},
        synchronize_session=False)
db.commit()
purged = soft_delete.purge_expired(db, library_id=lib_id)
left_stream = db.query(em.MediaStream).filter(em.MediaStream.item_id == victim_id).count()
left_progress = db.query(em.UserMediaData).filter(
    em.UserMediaData.item_id == victim_id).count()
with soft_delete.include_deleted():
    gone_row = db.query(em.MediaItem).filter(em.MediaItem.id == victim_id).first()
db.close()

check(purged == 1, "过期下架条目被物理回收", f"purged={purged}")
check(gone_row is None, "回收后行真的没了")
check(left_stream == 0, "媒体流一并回收（不留孤儿行）", f"流={left_stream}")
check(left_progress == 0, "播放进度/收藏一并回收（不留孤儿行）")


# ==================== 8. 分类关联表的兜底维护（v2.15.0） ====================
print("\n=== 分类关联表的兜底维护 ===")

# 业务写入路径（改条目分类值）走 ORM flush 钩子，同一事务里把关联行建好
db = Session()
facet_item = db.query(em.MediaItem).filter(em.MediaItem.library_id == lib_id).first()
facet_item.genres = f"稳定性流派{facet_item.id}"
face_item_id = facet_item.id
db.commit()
hook_rows = db.query(em.ItemFacet).filter(em.ItemFacet.item_id == face_item_id).count()
check(hook_rows == 1, "改条目分类值 → 关联行随同一事务自动建立（ORM flush 钩子）",
      f"{hook_rows} 行")
# 批量 Core 删除（扫描清理走的就是这条）：绕过 ORM 事件，故意留下孤儿关联行
db.query(em.MediaItem).filter(em.MediaItem.id == face_item_id).delete(synchronize_session=False)
db.commit()
orphans = db.query(em.ItemFacet).filter(em.ItemFacet.item_id == face_item_id).count()
db.close()
check(orphans == 1, "批量删除会留下孤儿关联行（这就是需要兜底的场景）", f"{orphans} 行")

# 看护周期兜底：清掉孤儿行，并给出可观测计数
tick_facets = maint.janitor_tick()
db = Session()
facet_left = db.query(em.ItemFacet).filter(em.ItemFacet.item_id == face_item_id).count()
db.close()
check(facet_left == 0, "看护周期清掉孤儿关联行", f"剩余={facet_left}")
check(tick_facets.get("item_facets_orphans", 0) >= 1,
      "看护周期报告清理数量（可观测）", f"{tick_facets}")

print()
if FAILED:
    print(f"❌ 长期运行稳定性冒烟失败（{len(FAILED)} 项）：" + "；".join(FAILED))
    sys.exit(1)
print("✅ 长期运行稳定性冒烟测试全部通过")

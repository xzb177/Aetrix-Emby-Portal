"""运行期维护（janitor）：把「跑久了会积累」的东西定期收掉

自建后端要能长期稳定跑在小机器上，除了扫描器的资源预算，还有几类**会随运行时间累积**的东西：

1. **崩溃残留的状态标志**：进程被 kill 后 `emby_libraries.is_scanning` 会停在 True，
   于是「刷新全部」永远跳过这台库、界面一直显示“扫描中”。启动时复位。
2. **崩溃残留的播放会话**：客户端异常退出不会上报 Stopped，`ended_at IS NULL` 的行会一直
   算作“正在播放”（后台首页、我的会话里的数字就永远是错的）。按心跳时间收掉。
3. **临时文件**：字幕缓存是只增不减的，定期淘汰。

这些都不改变任何用户可见功能，只是让「稳定」这件事可验证：`/api/health` 会报告
正在扫描的库数、字幕缓存与磁盘余量。
"""
from __future__ import annotations

import gc
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.db_retry import commit_with_retry

from backend.emby_server import facets
from backend.emby_server import image_store
from backend.emby_server import models as em
from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

# 扫描标志超过这个时间还挂着 True，就认为是崩溃残留（正常扫描不会跑这么久）
STALE_SCAN_HOURS = env_float("SCAN_STALE_HOURS", 6.0)
# 播放会话心跳超过这个时间没更新，就当作已经结束
STALE_SESSION_MINUTES = env_int("SESSION_STALE_MINUTES", 10)
# 播放会话保留天数：0 = 永久保留（默认，与历史统计口径一致）。
# 这张表每播一次多一行，机器小、跑得久会明显变大；设成比如 90 就会自动裁掉更早的记录，
# 代价是后台的「总播放次数 / 按日趋势」只剩保留期内的数据。
SESSION_RETENTION_DAYS = env_float("SESSION_RETENTION_DAYS", 0.0)
# 字幕缓存上限（文件数与天数），超过就按最旧先删
SUB_CACHE_MAX_FILES = env_int("SUB_CACHE_MAX_FILES", 2000)
SUB_CACHE_MAX_DAYS = env_float("SUB_CACHE_MAX_DAYS", 7.0)
# 维护周期（秒）：转码回收 / 会话过期 / 字幕缓存
MAINTENANCE_INTERVAL = env_int("MAINTENANCE_INTERVAL", 600, 60)


# ==================== 数据库侧 ====================

def reset_stale_scan_flags(db, stale_hours=None) -> int:
    """复位「扫描中」残留标志（进程重启后调用）

    前提：worker 启动时调用，那时本进程内存里不可能有正在跑的扫描。
    库里还标着 is_scanning=True 的，就是上一个进程被 SIGTERM 杀掉的残留。
    单节点部署：不需要「别处可能在扫」的阈值判断，全部复位。

    收尾状态记 interrupted（已中断）而不是 failed：被重启打断不是扫描失败，
    记 failed 会在健康快照里冒充失败告警；随后 resume_interrupted_scans 会
    把这些库自动重新入队续扫。
    """
    from backend.emby_server import scanner

    rows = db.query(em.Library).filter(em.Library.is_scanning == True).all()  # noqa: E712
    reset = []
    for lib in rows:
        if scanner.is_scan_active(lib.id):   # 本进程真在扫：绝不碰
            continue
        lib.is_scanning = False
        lib.scan_started_at = None
        lib.scan_progress = None
        if lib.scan_status == scanner.SCAN_STATUS_RUNNING:
            lib.scan_status = scanner.SCAN_STATUS_INTERRUPTED
            lib.scan_error = "进程重启，本轮扫描未完成"
        reset.append(lib.id)
    if reset:
        commit_with_retry(db, label="复位残留扫描标志")
        logger.warning("复位 %d 个残留的“扫描中”标志（崩溃/强杀遗留）: %s",
                       len(reset), ", ".join(str(i) for i in reset))
    return len(reset)


def close_stale_scan_runs(db: Session, stale_hours: Optional[float] = None,
                          library_ids: Optional[Iterable[int]] = None) -> int:
    """把崩溃残留的「还在跑」扫描流水收尾成 interrupted（被重启打断，不是失败）

    先跳过本进程真的在扫的库，再看开始时间是否超过阈值。
    （``reset_stale_scan_flags`` 已改为启动即全量复位；这里保留阈值，
    因为流水行没有 scan_started_at 这样的权威字段。）
    只是对象换成流水行：进程被强杀时那行会永远停在 ``running``——历史里挂着一条“扫描中”，
    而且永远没有结果。

    ``library_ids``：启动维护刚被 ``reset_stale_scan_flags`` 坐实打断的库。
    这些库的 running 流水就是被打断的那一轮，不受阈值限制直接收尾——
    否则刚开扫几分钟就被重启的流水太「新」，会被阈值跳过，继续挂着
    “运行中”直到下一次重启（2026-10-16 活体实证）。单节点部署下这些库
    不存在「别处在扫」的可能；本进程真在扫的仍由 is_scan_active 挡住。
    """
    from backend.emby_server import scanner

    hours = STALE_SCAN_HOURS if stale_hours is None else stale_hours
    cutoff = datetime.now() - timedelta(hours=hours)
    forced = set(library_ids or ())
    now = datetime.now()
    stuck: list = []
    for run in db.query(em.ScanRun).filter(em.ScanRun.status == scanner.SCAN_STATUS_RUNNING).all():
        if run.library_id is not None and scanner.is_scan_active(run.library_id):
            continue  # 本进程真的在扫：绝不碰
        if run.library_id not in forced and run.started_at is not None and run.started_at >= cutoff:
            continue  # 刚开始不久：可能正在另一台机器上扫
        run.status = scanner.SCAN_STATUS_INTERRUPTED
        run.finished_at = now
        run.error = (run.error or "进程重启，本轮扫描未完成")[:500]
        stuck.append(run)
    if stuck:
        commit_with_retry(db, label="收尾中断扫描流水")
        logger.warning("收尾 %d 条中断的扫描流水（崩溃/强杀遗留）: %s",
                       len(stuck), ", ".join(str(run.id) for run in stuck))
    return len(stuck)


def resume_interrupted_scans(db) -> int:
    """把「已中断」的库重新入队续扫（启动维护在复位/收尾之后调用）

    被进程重启打断的扫描数据没坏、任务也没错，缺的只是一次重新执行：
    逐库增量入队（trigger=resume），让它自己接着跑完，管理员不用再手动点。
    已在本进程队列里 / 已推入 Redis 队列的库跳过——队列本身有去重，
    这里再挡一道，避免 API 与 worker 两个进程启动时各入一次。
    单库入队失败只记日志，不拖累其他库，也不阻塞启动。
    """
    from backend.emby_server import scan_queue, scanner

    rows = (
        db.query(em.Library)
        .filter(
            em.Library.scan_status == scanner.SCAN_STATUS_INTERRUPTED,
            em.Library.is_scanning == False,  # noqa: E712
        )
        .all()
    )
    resumed: list[int] = []
    for lib in rows:
        try:
            if scan_queue.is_busy(lib.id):
                continue
            scan_queue.enqueue(lib, trigger="resume", force_full=False)
            resumed.append(lib.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自动续扫入队失败：库 id=%s: %s", lib.id, exc)
    if resumed:
        logger.info("自动续扫 %d 个被重启打断的媒体库: %s",
                    len(resumed), ", ".join(str(i) for i in resumed))
    return len(resumed)


def reap_stale_playback_sessions(db: Session, stale_minutes: Optional[int] = None) -> int:
    """把心跳过期的播放会话标记为已结束（客户端异常断开不会上报 Stopped）"""
    minutes = STALE_SESSION_MINUTES if stale_minutes is None else stale_minutes
    cutoff = datetime.now() - timedelta(minutes=minutes)
    now = datetime.now()
    rows = (
        db.query(em.PlaybackSession)
        .filter(
            em.PlaybackSession.ended_at.is_(None),
            (em.PlaybackSession.last_update_at.is_(None))
            | (em.PlaybackSession.last_update_at < cutoff),
        )
        .all()
    )
    for session in rows:
        # ended_at 用心跳时间更贴近事实（否则“结束时间”会是收尾那一刻）
        session.ended_at = session.last_update_at or now
    if rows:
        commit_with_retry(db, label="回收过期播放会话")
        logger.info("回收 %d 个心跳过期的播放会话（停在上次心跳时间）", len(rows))
    return len(rows)


def prune_playback_sessions(db: Session, retention_days: Optional[float] = None) -> int:
    """裁掉超出保留期的已结束播放会话（默认关闭：`SESSION_RETENTION_DAYS=0`）

    只在显式设了保留期时才动手，且只删**已经结束**的会话：正在播的那条永远不碰。
    没设保留期就是老行为（历史全留），小机器想控制库体量再打开。
    """
    days = SESSION_RETENTION_DAYS if retention_days is None else retention_days
    if not days or days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    deleted = (
        db.query(em.PlaybackSession)
        .filter(
            em.PlaybackSession.ended_at.isnot(None),
            em.PlaybackSession.start_time.isnot(None),
            em.PlaybackSession.start_time < cutoff,
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        commit_with_retry(db, label="裁剪历史播放会话")
        logger.info("裁掉 %d 条超出 %s 天的播放会话", deleted, days)
    return deleted


# ==================== 文件侧 ====================


def purge_expired_emby_tokens(db: Session) -> int:
    """清理已过期的 Emby 客户端 token（expires_at 非空且已过期）。

    新签发的 token 默认 30 天有效（见 play_sign.token_expiry_default）；
    过期 token 校验时已视为无效，这里只是把行删掉防止表无限增长。
    由 janitor_tick 定期调用。
    """
    from backend.emby_server import play_sign

    return play_sign.purge_expired_tokens(db)


REPORT_MAX_FILES = 5000


def prune_subtitle_cache(max_files: Optional[int] = None,
                         max_age_days: Optional[float] = None) -> int:
    """字幕缓存淘汰：先按天数删过期的，再按数量删最旧的

    字幕缓存（外挂/内封字幕转 VTT）是只增不减的：每个条目一份文件，
    追新久了就是几万个文件躺在磁盘上。
    """
    from backend.emby_server import subtitles

    cache_dir = getattr(subtitles, "SUB_CACHE_DIR", "")
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0

    keep_files = SUB_CACHE_MAX_FILES if max_files is None else max_files
    days = SUB_CACHE_MAX_DAYS if max_age_days is None else max_age_days
    cutoff = datetime.now().timestamp() - days * 86400
    removed = 0
    entries: list[tuple[float, str]] = []
    for entry in os.scandir(cache_dir):
        try:
            if not entry.is_file():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            if _unlink(entry.path):
                removed += 1
            continue
        entries.append((mtime, entry.path))

    if len(entries) > keep_files:
        entries.sort()  # 最旧在前
        for _mtime, path in entries[: len(entries) - keep_files]:
            if _unlink(path):
                removed += 1

    if removed:
        logger.info("字幕缓存淘汰 %d 个文件（上限 %d 个 / %.0f 天）",
                    removed, keep_files, days)
    return removed


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


# ==================== 可观测性（/api/health 用） ====================

# 健康检查里的目录统计上限：健康检查会被容器探针频繁打到，
# 不能让它在「残留了几百个会话目录、每个几千个分片」时越走越慢。
REPORT_MAX_FILES = 5000


def resource_report() -> dict:
    """轻量的运行期资源快照：字幕缓存 / 磁盘余量

    转码已删除（2026-10-17）：不再统计转码会话与转码目录。
    刻意只做常数级统计：健康检查的响应时间不随文件数增长。
    """
    report: dict = {
        "subtitle_cache_files": 0,
        "disk_free_bytes": None,
        "disk_free_percent": None,
    }
    try:
        usage = shutil.disk_usage("/")
        report["disk_free_bytes"] = usage.free
        report["disk_free_percent"] = round(usage.free / usage.total * 100, 1)
    except OSError:
        pass
    from backend.emby_server import subtitles

    cache_dir = getattr(subtitles, "SUB_CACHE_DIR", "")
    if cache_dir and os.path.isdir(cache_dir):
        try:
            count = 0
            for entry in os.scandir(cache_dir):
                if entry.is_file():
                    count += 1
                    if count >= REPORT_MAX_FILES:
                        break
            report["subtitle_cache_files"] = count
        except OSError:
            pass
    return report


def active_scan_count() -> int:
    """本进程正在扫描的媒体库数"""
    from backend.emby_server import scanner

    return len(scanner._ACTIVE_SCANS)  # noqa: SLF001 — 只读计数，给健康检查用


# ==================== 调度 ====================

_JANITOR_STARTED = False
_JANITOR_LOCK = threading.Lock()


def run_startup_maintenance() -> dict:
    """启动时的一次性维护（必须在 init_db 之后调用）

    不抛异常：任何一项失败都只记日志，绝不阻塞服务启动——「带病启动」在这里是**对的**，
    因为维护没做完不会让服务不可用，而启动失败会让整个站点挂掉。
    """
    from backend.database import SessionLocal

    result = {"scan_flags_reset": 0, "scan_runs_closed": 0, "scan_resumed": 0,
              "sessions_reaped": 0,
              "subtitle_cache_pruned": 0,
              "tmdb_cache_pruned": 0, "mount_file_ids_pruned": 0,
              "item_facets_backfilled": 0, "item_facets_pruned": 0,
              "item_facets_ready": False}
    db = SessionLocal()
    try:
        result["scan_flags_reset"] = reset_stale_scan_flags(db)
        # 刚被复位坐实打断的库（状态已记 interrupted 且不在扫）：它们的
        # running 流水就是被打断的那一轮，收尾时不受 6 小时阈值限制。
        from backend.emby_server import scanner as _scanner
        _interrupted_lib_ids = [
            lib.id for lib in db.query(em.Library).filter(
                em.Library.scan_status == _scanner.SCAN_STATUS_INTERRUPTED,
                em.Library.is_scanning == False,  # noqa: E712
            ).all()
        ]
        result["scan_runs_closed"] = close_stale_scan_runs(
            db, library_ids=_interrupted_lib_ids)
        # 复位刚把被重启打断的库收尾成 interrupted：紧接着自动续扫，
        # 不等管理员发现失败牌再手动点（续扫失败不影响其余维护项）。
        try:
            result["scan_resumed"] = resume_interrupted_scans(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("启动维护（自动续扫）失败: %s", exc)
        result["sessions_reaped"] = reap_stale_playback_sessions(db, stale_minutes=1)
        # 升级上来的老库：分类关联表刚建出来时是空的，这里补上（按 id 水位增量）。
        # 补不完也不影响启动：筛选路径会退回旧的全表匹配并接着补。
        result["item_facets_backfilled"] = facets.ensure_backfill(db)
        result["item_facets_pruned"] = facets.prune_orphans(db)
        result["item_facets_ready"] = facets.ready(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动维护（数据库部分）失败: %s", exc)
        db.rollback()
    finally:
        db.close()

    try:
        result["subtitle_cache_pruned"] = prune_subtitle_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动维护（字幕缓存）失败: %s", exc)
    try:
        # TMDB 磁盘缓存巡检（第 7 批）：删过期文件 + 超容量按最旧淘汰。
        # 写入侧也有自己的触发条件，这里是保底——刮削停了缓存也别一直涨。
        # 值是「删掉的文件数」（干净时 0），与其它计数同口径。
        from backend.emby_server import tmdb_cache
        _pruned = tmdb_cache.prune(force=True) or {}
        result["tmdb_cache_pruned"] = int(_pruned.get("expired_removed", 0)) \
            + int(_pruned.get("evicted", 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动维护（TMDB 缓存巡检）失败: %s", exc)

    logger.info("启动维护完成: %s", result)
    return result


def prune_ai_usage(db, keep_days: int = 90) -> int:
    """删掉过期的 AI 助手每日用量行（一个用户一天一行，不清就会一直涨）

    配额只看当天（``user_id + day`` 上还有唯一索引），保留 90 天只是为了回看用量时
    不至于查不到东西。
    """
    from datetime import datetime, timedelta

    from backend import models

    cutoff = (datetime.now() - timedelta(days=max(1, keep_days))).strftime("%Y-%m-%d")
    pruned = db.query(models.AiUsage).filter(models.AiUsage.day < cutoff).delete(
        synchronize_session=False)
    if pruned:
        commit_with_retry(db, label="清理 AI 用量")
    return int(pruned or 0)


def prune_scan_dir_states(db) -> int:
    """删掉媒体库已不存在的扫描目录指纹（增量扫描的副作用清理）"""
    from backend.emby_server import models as emby_models

    alive = db.query(emby_models.Library.id)
    pruned = db.query(emby_models.ScanDirState).filter(
        ~emby_models.ScanDirState.library_id.in_(alive)
    ).delete(synchronize_session=False)
    if pruned:
        commit_with_retry(db, label="清理扫描目录指纹")
    return int(pruned or 0)


def prune_scan_runs(db, keep: Optional[int] = None) -> int:
    """回收扫描流水：媒体库已不存在的行 + 每库超出上限的旧行

    每轮扫描结束时已经就地回收过一次（见 ``scanner.prune_library_scan_runs``），
    这里是兼底：历史上限调小过、媒体库被删除（不可逆，所以不能用外键挡删除），
    以及建库→删库反复造成的残留。
    """
    from backend.emby_server import models as emby_models
    from backend.emby_server import scanner as scanner_lib

    limit = scanner_lib.SCAN_RUN_KEEP if keep is None else max(0, int(keep))
    pruned = 0

    # 先把要删的 id 全查出来（此时还没有任何写），再一次性删 + 提交。
    # 旧实现是「先删孤儿行（写事务由此打开）→ 再循环查每个库的第 N 行之后 → 才提交」：
    # 那段查询全程占着写锁，别的写请求只能等（见 docs/performance.md 的「事务范围」）。
    alive = db.query(emby_models.Library.id)
    stale: list[int] = [row[0] for row in db.query(emby_models.ScanRun.id)
                        .filter(~emby_models.ScanRun.library_id.in_(alive)).all()]
    if limit > 0:
        # 每个库只留最近 limit 行：用「按库取第 limit 行之后的 id」实现，
        # SQLite / MySQL / PG 通用（窗口函数各家语法与版本要求不一）。
        # 有流水的库只有十几到几十个，一次每库一查，代价可忽略。
        for (lib_id,) in db.query(emby_models.ScanRun.library_id).distinct().all():
            stale.extend(
                row[0] for row in db.query(emby_models.ScanRun.id)
                .filter(emby_models.ScanRun.library_id == lib_id)
                .order_by(emby_models.ScanRun.id.desc())
                .offset(limit)
                .all()
            )
    if stale:
        pruned = int(db.query(emby_models.ScanRun)
                     .filter(emby_models.ScanRun.id.in_(stale))
                     .delete(synchronize_session=False) or 0)

    if pruned:
        commit_with_retry(db, label="清理扫描流水")
    return int(pruned)


def optimize_query_plans(db: Session) -> bool:
    """让 SQLite 按需更新统计信息（``PRAGMA optimize``）；非 SQLite 直接跳过

    长期运行的第二类退化：库从小长到大、数据分布也变了（扫描增删几万条），
    而查询计划还是按很久以前的统计信息估行数——估错就是全表扫。SQLite 官方
    推荐的做法就是周期性 ``PRAGMA optimize``：绝大多数时候它什么都不做
    （毫秒级），只在某张表确实该 ANALYZE 时才动手。

    返回是否执行过（非 SQLite / 失败都是 False：维护里一项失败不该影响别的项）。
    """
    from backend.database import DATABASE_TYPE

    if DATABASE_TYPE != "sqlite":
        return False
    try:
        db.execute(text("PRAGMA optimize"))
        return True
    except Exception as exc:  # noqa: BLE001 — 优化查询计划失败不影响服务
        logger.warning("PRAGMA optimize 失败: %s", exc)
        db.rollback()
        return False



def janitor_tick(heavy: bool = True) -> dict:
    """一次维护动作（启动后由后台线程按 MAINTENANCE_INTERVAL 周期执行）。

    heavy=True（默认）：全套维护，worker 进程用。
    heavy=False：只做播放相关的轻量清理（转码/播放会话/token/字幕缓存/GC），
    跳过图片/缩略图/分类回填/扫描指纹/流水/AI 用量/查询计划/TMDB 缓存等重活——
    EA 是播放前台进程，重活每 600 秒在它进程里跑会跟播放请求抢 DB/磁盘/CPU，
    且这些活 worker 进程本来就会做（幂等，双跑纯浪费）。
    """
    from backend.database import SessionLocal

    result = {"sessions_reaped": 0, "sessions_pruned": 0,
              "subtitle_cache_pruned": 0,
              "tmdb_cache_pruned": 0,
              "item_facets_backfilled": 0, "item_facets_orphans": 0,
              "scan_dir_states_pruned": 0, "scan_runs_pruned": 0,
              "emby_tokens_purged": 0,
              "images_pruned": 0,
              "images_freed_bytes": 0, "ai_usage_pruned": 0,
              "thumbs_backfilled": 0, "thumbs_backfill_done": False,
              "query_plans_optimized": False}

    db = SessionLocal()
    try:
        result["sessions_reaped"] = reap_stale_playback_sessions(db)
        result["sessions_pruned"] = prune_playback_sessions(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回收过期播放会话失败: %s", exc)
        db.rollback()
    finally:
        db.close()

    # 过期 Emby token：校验时已视为无效，这里定期把行删掉
    db = SessionLocal()
    try:
        result["emby_tokens_purged"] = purge_expired_emby_tokens(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理过期 Emby token 失败: %s", exc)
        db.rollback()
    finally:
        db.close()

    # ---- 重活区（heavy）：图片/缩略图/分类回填/扫描指纹/流水/AI 用量/查询计划 ----
    # worker 进程跑全套；EA（播放前台）传 heavy=False 跳过，把 DB/磁盘/CPU 留给播放请求。
    if heavy:
        # 分类关联表：把老库还没回填的条目补齐（增量，按 id 水位），
        # 并清掉指向已删除条目的孤儿行（批量删除走 Core 语句，ORM 事件看不到）
        db = SessionLocal()
        try:
            result["item_facets_backfilled"] = facets.ensure_backfill(db)
            result["item_facets_orphans"] = facets.prune_orphans(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("维护分类关联表失败: %s", exc)
            db.rollback()
        finally:
            db.close()

        # 增量扫描的目录指纹：媒体库删了之后那批行没人会再用（指纹键含库 id），
        # 留着只会随「建库→删库」慢慢涨
        db = SessionLocal()
        try:
            result["scan_dir_states_pruned"] = prune_scan_dir_states(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理扫描目录指纹失败: %s", exc)
            db.rollback()
        finally:
            db.close()

        # 扫描流水：同一个原因（媒体库删了没人再看），+ 每个库超出上限的旧行兜底
        db = SessionLocal()
        try:
            result["scan_runs_pruned"] = prune_scan_runs(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理扫描流水失败: %s", exc)
            db.rollback()
        finally:
            db.close()

        # 刮削图片本地化的缓存：换过海报的旧文件、已被删除的条目留下的图都要回收，
        # 超出上限时再从旧到新淘汰（有引用的图一张不动）
        db = SessionLocal()
        try:
            image_result = image_store.prune(db)
            result["images_pruned"] = image_result["removed"]
            result["images_freed_bytes"] = image_result["freed_bytes"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理图片缓存失败: %s", exc)
            db.rollback()
        finally:
            db.close()

        # 缩略图历史补生成：每天维护周期跑一批（限速+断点续跑），补完为止。
        # 扫描正在跑时跳过：转码吃 CPU，别跟扫描抢资源（水位已提交，下次继续）。
        # 手动触发（管理后台 POST）不受影响：那是管理员明确点的。
        db = SessionLocal()
        try:
            scanning = db.query(em.Library.id).filter(
                em.Library.is_scanning.is_(True)).first() is not None
            if scanning:
                logger.info("缩略图补生成：有扫描在跑，本轮跳过")
            else:
                # 前台活跃让路：最近有用户在看/刷，本轮重活跳过（任务留到下个周期，不丢）
                # r42：非法值解析走 env_util 统一入口（非法值记 warning 回落 60.0，下限 0.0）
                from backend.emby_server import foreground_activity as _fa
                _window = env_float("FOREGROUND_ACTIVE_SECONDS", 60.0, 0.0)
                if _window > 0 and _fa.active(_window):
                    _ago = _fa.last_seen_ago()
                    logger.info("缩略图补生成：前台 %d 秒前有用户请求，本轮跳过",
                                int(_ago) if _ago is not None else 0)
                    result["thumbs_backfill_skipped_active"] = True
                else:
                    thumb_result = image_store.backfill_thumbnails(db)
                    result["thumbs_backfilled"] = thumb_result["generated"]
                    result["thumbs_backfill_done"] = thumb_result["done"]
                    if thumb_result["generated"]:
                        logger.info("缩略图补生成：本轮 %d 张，水位 id=%d%s",
                                    thumb_result["generated"], thumb_result["last_id"],
                                    "（已补完）" if thumb_result["done"] else "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("缩略图补生成失败: %s", exc)
            db.rollback()
        finally:
            db.close()

        # AI 助手每日用量（能力：AI 模型设置）：一个用户一天一行，不清就会一直涨。
        # 只保留近 90 天——配额看的是「今天」，历史行只在回看用量时有意义。
        db = SessionLocal()
        try:
            result["ai_usage_pruned"] = prune_ai_usage(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理 AI 用量记录失败: %s", exc)
            db.rollback()
        finally:
            db.close()

        # 查询计划：库长到几十万行之后，SQLite 可能还在按老统计信息估行数（选错索引就是
        # 全表扫描）。官方建议周期性跑 `PRAGMA optimize`，成本几乎为零，见 optimize_query_plans。
        db = SessionLocal()
        try:
            result["query_plans_optimized"] = optimize_query_plans(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("优化查询计划失败: %s", exc)
            db.rollback()
        finally:
            db.close()

    try:
        result["subtitle_cache_pruned"] = prune_subtitle_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("字幕缓存淘汰失败: %s", exc)
    if heavy:
        # TMDB 响应缓存巡检：enrich 侧的活，EA 不跑
        try:
            from backend.emby_server import tmdb_cache
            _pruned = tmdb_cache.prune(force=True) or {}
            result["tmdb_cache_pruned"] = int(_pruned.get("expired_removed", 0)) \
                + int(_pruned.get("evicted", 0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("TMDB 缓存巡检失败: %s", exc)
    try:
        result["idle_gc_done"] = bool(_idle_gc_if_quiet())
    except Exception as exc:  # noqa: BLE001
        logger.warning("空闲 GC 失败: %s", exc)
    return result


def _idle_gc_if_quiet() -> int:
    """空闲时回收内存（借鉴 go-emby 的 FreeOSMemory 思路）：

    只在没有扫描运行时做，避免跟扫描抢 CPU；gc.collect() 清 Python
    循环引用垃圾，再调 malloc_trim 把空闲堆内存还给 OS（Linux）。
    绝不在请求热路径调用，只在维护线程里跑。
    """
    import gc

    from backend.database import SessionLocal
    from backend.emby_server import models as em

    db = SessionLocal()
    try:
        running = (
            db.query(em.ScanRun.id)
            .filter(em.ScanRun.status == "running")
            .first()
        )
        if running is not None:
            return 0  # 有扫描在跑：不动
    finally:
        db.close()

    before = gc.get_count()
    gc.collect()
    freed = 0
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        # malloc_trim(0)：把堆顶空闲内存还给 OS，返回 1 表示成功
        if libc.malloc_trim(0):
            freed = 1
    except Exception:
        pass
    logger.info("空闲 GC 完成（代际计数 %s→%s）", before, gc.get_count())
    return freed


def start_janitor(interval_seconds: Optional[int] = None, heavy: bool = True) -> bool:
    """启动后台维护线程（同一进程只启动一次；daemon，随进程退出）

    heavy 透传给 janitor_tick：EA（播放前台）传 heavy=False 只跑轻量清理。
    """
    global _JANITOR_STARTED
    interval = MAINTENANCE_INTERVAL if interval_seconds is None else max(30, interval_seconds)
    with _JANITOR_LOCK:
        if _JANITOR_STARTED:
            return False
        _JANITOR_STARTED = True

    def _one_tick() -> None:
        try:
            summary = janitor_tick(heavy=heavy)
            if any(summary.values()):
                logger.info("维护周期完成: %s", summary)
            gc.collect()  # 2026-10-07 压力测试：强制回收防内存不回落
        except Exception as exc:  # noqa: BLE001 — 维护线程绝不能因为一次失败而退出
            logger.warning("维护周期异常: %s", exc)

    def _loop() -> None:
        # 首拍不等：原来先 wait(interval) 再跑第一轮，容器重启后要等满 600 秒；
        # 夜间频繁部署时短命容器（存活 <10 分钟）永远跑不到 janitor，签名刷新
        # 会被漏掉（2026-10-13 实锤：5 次重建造成 ≥40 分钟刷新真空）。启动即跑一次。
        _one_tick()
        while True:
            threading.Event().wait(interval)
            _one_tick()

    threading.Thread(target=_loop, daemon=True, name="maintenance-janitor").start()
    logger.info("后台维护已启动（每 %s 秒，%s）", interval,
                "全量" if heavy else "轻量（EA 播放前台，重活由 worker 进程做）")
    return True


"""运行期维护（janitor）：把「跑久了会积累」的东西定期收掉

自建后端要能长期稳定跑在小机器上，除了扫描器的资源预算，还有三类**会随运行时间累积**的东西：

1. **崩溃残留的状态标志**：进程被 kill 后 `emby_libraries.is_scanning` 会停在 True，
   于是「刷新全部」永远跳过这台库、界面一直显示“扫描中”。启动时复位。
2. **崩溃残留的播放会话**：客户端异常退出不会上报 Stopped，`ended_at IS NULL` 的行会一直
   算作“正在播放”（后台首页、我的会话里的数字就永远是错的）。按心跳时间收掉。
3. **临时文件**：HLS 转码目录（默认 `/tmp/emby_transcode`，很多机器上 `/tmp` 是 tmpfs＝内存）
   与字幕缓存。重启后注册表是空的，旧会话目录再也没人管；字幕缓存则是只增不减。

外加一个**关闭时不留孤儿进程**的收尾：ffmpeg 是子进程，父进程被 SIGTERM 时不会自动带走它。

这些都不改变任何用户可见功能，只是让「稳定」这件事可验证：`/api/health` 会报告
转码会话数、正在扫描的库数、临时目录占用与磁盘余量。
"""
from __future__ import annotations

import gc
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.db_retry import commit_with_retry

from backend.emby_server import facets
from backend.emby_server import image_store
from backend.emby_server import models as em

logger = logging.getLogger(__name__)

# 扫描标志超过这个时间还挂着 True，就认为是崩溃残留（正常扫描不会跑这么久）
STALE_SCAN_HOURS = float(os.getenv("SCAN_STALE_HOURS", "6") or 6)
# 播放会话心跳超过这个时间没更新，就当作已经结束
STALE_SESSION_MINUTES = int(os.getenv("SESSION_STALE_MINUTES", "10") or 10)
# 播放会话保留天数：0 = 永久保留（默认，与历史统计口径一致）。
# 这张表每播一次多一行，机器小、跑得久会明显变大；设成比如 90 就会自动裁掉更早的记录，
# 代价是后台的「总播放次数 / 按日趋势」只剩保留期内的数据。
SESSION_RETENTION_DAYS = float(os.getenv("SESSION_RETENTION_DAYS", "0") or 0)
# 字幕缓存上限（文件数与天数），超过就按最旧先删
SUB_CACHE_MAX_FILES = int(os.getenv("SUB_CACHE_MAX_FILES", "2000") or 2000)
SUB_CACHE_MAX_DAYS = float(os.getenv("SUB_CACHE_MAX_DAYS", "7") or 7)
# 维护周期（秒）：转码回收 / 会话过期 / 字幕缓存
MAINTENANCE_INTERVAL = max(60, int(os.getenv("MAINTENANCE_INTERVAL", "600") or 600))

# 转码根目录下这些子目录是**共享缓存**，不属于某个会话，清理时要留下
# （115 转存的状态目录 pan115 已在 v2.18.0 移除：它不再受保护，遗留的会被本清理回收）
_SHARED_DIRS = {"subs"}


# ==================== 数据库侧 ====================

def reset_stale_scan_flags(db, stale_hours=None) -> int:
    """复位「扫描中」残留标志（进程重启后调用）

    前提：worker 启动时调用，那时本进程内存里不可能有正在跑的扫描。
    库里还标着 is_scanning=True 的，就是上一个进程被 SIGTERM 杀掉的残留。
    单节点部署：不需要「别处可能在扫」的阈值判断，全部复位。
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
            lib.scan_status = scanner.SCAN_STATUS_FAILED
            lib.scan_error = "进程重启，本轮扫描未完成"
        reset.append(lib.id)
    if reset:
        commit_with_retry(db, label="复位残留扫描标志")
        logger.warning("复位 %d 个残留的“扫描中”标志（崩溃/强杀遗留）: %s",
                       len(reset), ", ".join(str(i) for i in reset))
    return len(reset)


def close_stale_scan_runs(db: Session, stale_hours: Optional[float] = None) -> int:
    """把崩溃残留的「还在跑」扫描流水收尾成 failed

    先跳过本进程真的在扫的库，再看开始时间是否超过阈值。
    （``reset_stale_scan_flags`` 已改为启动即全量复位；这里保留阈值，
    因为流水行没有 scan_started_at 这样的权威字段。）
    只是对象换成流水行：进程被强杀时那行会永远停在 ``running``——历史里挂着一条“扫描中”，
    而且永远没有结果。
    """
    from backend.emby_server import scanner

    hours = STALE_SCAN_HOURS if stale_hours is None else stale_hours
    cutoff = datetime.now() - timedelta(hours=hours)
    now = datetime.now()
    stuck: list = []
    for run in db.query(em.ScanRun).filter(em.ScanRun.status == scanner.SCAN_STATUS_RUNNING).all():
        if run.library_id is not None and scanner.is_scan_active(run.library_id):
            continue  # 本进程真的在扫：绝不碰
        if run.started_at is not None and run.started_at >= cutoff:
            continue  # 刚开始不久：可能正在另一台机器上扫
        run.status = scanner.SCAN_STATUS_FAILED
        run.finished_at = now
        run.error = (run.error or "进程重启，本轮扫描未完成")[:500]
        stuck.append(run)
    if stuck:
        commit_with_retry(db, label="收尾中断扫描流水")
        logger.warning("收尾 %d 条中断的扫描流水（崩溃/强杀遗留）: %s",
                       len(stuck), ", ".join(str(run.id) for run in stuck))
    return len(stuck)


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


def _transcode_root() -> str:
    from backend.emby_server import streaming

    return streaming.TRANSCODE_DIR


def cleanup_transcode_orphans(min_age_seconds: float = 0) -> int:
    """清掉转码根目录下不属于任何「在跑的会话」的遗留目录

    重启后注册表为空，上一进程留下的 HLS 目录再也没人清理；`/tmp` 是 tmpfs 的机器上
    这些分片就是内存占用。`subs` 这类共享缓存在这里不动（字幕缓存另有淘汰）。
    """
    from backend.emby_server import streaming

    root = _transcode_root()
    if not root or not os.path.isdir(root):
        return 0
    live = set(streaming.active_transcode_ids())
    removed = 0
    for entry in os.scandir(root):
        if not entry.is_dir() or entry.name in _SHARED_DIRS:
            continue
        if entry.name in live:
            continue
        if min_age_seconds > 0:
            age = datetime.now().timestamp() - entry.stat().st_mtime
            if age < min_age_seconds:
                continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("清理 %d 个遗留的 HLS 转码目录", removed)
    return removed


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
    """轻量的运行期资源快照：转码会话 / 临时目录占用 / 磁盘余量

    刻意只统计顶层与在跑的会话目录，且**有上限**：健康检查必须是常数级的，
    统计到这里就停（数字是下界，但探针的响应时间不会随残留目录增长）。
    """
    from backend.emby_server import streaming

    root = _transcode_root()
    report: dict = {
        "transcode_sessions": len(streaming.active_transcode_ids()),
        "transcode_dir": root,
        "transcode_dir_bytes": 0,
        "transcode_dir_files": 0,
        "subtitle_cache_files": 0,
        "transcode_scan_truncated": False,
        "disk_free_bytes": None,
        "disk_free_percent": None,
    }
    if root and os.path.isdir(root):
        try:
            for entry in os.scandir(root):
                if report["transcode_dir_files"] >= REPORT_MAX_FILES:
                    report["transcode_scan_truncated"] = True
                    break
                if entry.is_file():
                    report["transcode_dir_files"] += 1
                    report["transcode_dir_bytes"] += entry.stat().st_size
                elif entry.is_dir() and entry.name not in _SHARED_DIRS:
                    for dirpath, _dirs, files in os.walk(entry.path):
                        for name in files:
                            if report["transcode_dir_files"] >= REPORT_MAX_FILES:
                                report["transcode_scan_truncated"] = True
                                break
                            try:
                                report["transcode_dir_bytes"] += os.stat(
                                    os.path.join(dirpath, name)
                                ).st_size
                                report["transcode_dir_files"] += 1
                            except OSError:
                                continue
                        if report["transcode_scan_truncated"]:
                            break
        except OSError as exc:  # noqa: BLE001 — 健康检查不能被统计拖垮
            logger.debug("统计转码目录失败: %s", exc)
        try:
            usage = shutil.disk_usage(root)
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

    result = {"scan_flags_reset": 0, "scan_runs_closed": 0, "sessions_reaped": 0,
              "transcode_orphans": 0, "subtitle_cache_pruned": 0,
              "tmdb_cache_pruned": 0, "mount_file_ids_pruned": 0,
              "item_facets_backfilled": 0, "item_facets_pruned": 0,
              "item_facets_ready": False}
    db = SessionLocal()
    try:
        result["scan_flags_reset"] = reset_stale_scan_flags(db)
        result["scan_runs_closed"] = close_stale_scan_runs(db)
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

    # 启动时把上一进程留下的转码目录全部清掉（此刻还没有任何“在跑的会话”）
    try:
        result["transcode_orphans"] = cleanup_transcode_orphans()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动维护（转码目录）失败: %s", exc)
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


# .strm 签名刷新：节流间隔上限（秒，默认 3000 = 50 分钟）与上次运行时间记录文件。
# 实际节流 = min(上限, 重签阈值 − tick)，重签阈值 = max(TTL/3, 2×tick)（见
# strm_sign.refresh_threshold_seconds）：保证任何文件过期前至少被扫到一次，
# 不再出现"TTL 短于 50 分钟、两次刷新之间全库过期"。
STRM_REFRESH_INTERVAL_SECONDS = max(600, int(os.getenv("STRM_REFRESH_INTERVAL_SECONDS", "3000") or 3000))
STRM_REFRESH_LAST_FILE = "/tmp/strm_sig_refresh.last"


def _strm_refresh_settings(db=None) -> tuple[str, int]:
    """(容器内 .strm 根目录, 签名 TTL)：目录走 strm_config.container_path，TTL 走
    play_sign.strm_sig_ttl_seconds；db 为 None 时自开会话，读失败回落 env/默认。"""
    from backend.emby_server import play_sign, strm_config

    if db is not None:
        return strm_config.container_path(db), play_sign.strm_sig_ttl_seconds(db)
    try:
        from backend.database import SessionLocal

        own = SessionLocal()
        try:
            return strm_config.container_path(own), play_sign.strm_sig_ttl_seconds(own)
        finally:
            own.close()
    except Exception:  # noqa: BLE001 — 读配置失败不能拖死维护
        logger.debug("strm_sig_refresh: 读取配置失败，回落环境变量/默认值", exc_info=True)
        return strm_config.env_container_path(), play_sign.STRM_TTL_DEFAULT


def strm_refresh_cadence(ttl_seconds: int, tick_seconds: Optional[int] = None) -> tuple[int, int]:
    """(重签阈值, 扫盘节流间隔)，都由 TTL 与 janitor tick 推导。"""
    from backend.emby_server.strm_sign import refresh_threshold_seconds

    tick = int(MAINTENANCE_INTERVAL if tick_seconds is None else tick_seconds)
    threshold = refresh_threshold_seconds(ttl_seconds, tick)
    interval = max(0, min(STRM_REFRESH_INTERVAL_SECONDS, threshold - tick))
    return threshold, interval


def strm_sig_refresh_tick(db=None) -> dict:
    """刷新 .strm 文件签名（节流随 TTL 推导，见 strm_refresh_cadence）：只重签 needs_refresh 的文件。

    - legacy（无签名）→ 补签名；ok 但剩余有效期 < max(TTL/3, 2×tick) → 用后台配置的 TTL 重签；
      bad（签名损坏/过期）→ 跳过计数，不删除不重写，等人工处理。
    - 只读前 4KB 判断（签名只在第一行），超 4KB 的文件重签时才读全文，
      其余行（注释/空行）原样保留；写入走 tmp + os.replace 原子替换。
    - 单个文件异常隔离（记 debug、计数 errors），不影响整轮。
    - 永不抛异常：janitor 是后台线程，一次维护失败不能把整轮拖死。
    """
    started = time.time()
    now = time.time()
    refreshed = 0
    skipped_files = 0
    bad_files = 0
    errors = 0

    # 1) 配置：.strm 目录走唯一事实源 strm_config.container_path(db)
    #    （后台配置 > 环境变量 STRM_CONTAINER_PATH > /strm），TTL 走后台配置
    strm_dir, ttl = _strm_refresh_settings(db)
    threshold, interval = strm_refresh_cadence(ttl)

    # 2) 节流：上次运行时间（读不到/非法视为从未运行）；留 30 秒余量吸收 tick 抖动
    try:
        with open(STRM_REFRESH_LAST_FILE, "r", encoding="utf-8") as f:
            last = float(f.read().strip())
    except (OSError, ValueError):
        last = 0.0
    if interval > 0 and now - last < interval - 30:
        return {"skipped": True, "reason": "interval"}

    if not os.path.isdir(strm_dir):
        logger.info("strm_sig_refresh: .strm 目录不存在，跳过: %s", strm_dir)
        return {"skipped": True, "reason": "no_dir"}

    # 只读挂载提前跳过（EA 容器的 /strm 是 ro）：先探写一次，避免全树 walk
    # 之后每个需刷新的文件写 .tmp 都 OSError，最后汇总出一条误导性的
    # "刷新失败请检查日志" warning。探针文件名带 pid+tid，best-effort 清理。
    _ro_probe = os.path.join(
        strm_dir, f".write_probe_{os.getpid()}.{threading.get_ident():x}")
    try:
        with open(_ro_probe, "w", encoding="utf-8") as _pf:
            _pf.write("ok")
    except OSError:
        logger.info("strm_sig_refresh: .strm 目录只读，跳过: %s", strm_dir)
        return {"skipped": True, "reason": "read_only"}
    try:
        os.unlink(_ro_probe)
    except OSError:
        pass

    # 3) 延迟 import，避免顶层循环导入
    from backend.emby_server.mounts import strm_url as _first_strm_url
    from backend.emby_server.strm_sign import needs_refresh, sign_strm_url

    def _replace_first_url(text: str, signed: str, first_url: str) -> str:
        """把文本中第一行有效 URL 替换为签名后的 URL，其余行（含注释/空行/BOM）原样保留。"""
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line.replace("\ufeff", "").strip() == first_url:
                lines[i] = signed
                break
        return "\n".join(lines)

    # 4) 全量 walk：.strm 文件（大小写不敏感后缀）；只读前 4KB 判断
    for root, _dirs, files in os.walk(strm_dir):
        for fname in files:
            if not fname.lower().endswith(".strm"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(4096)
                if not head.strip():
                    skipped_files += 1
                    continue
                if not needs_refresh(head, threshold_seconds=threshold):
                    skipped_files += 1
                    continue
                # 首个有效 URL 行（跳过 BOM/注释/空行，与 mounts.strm_url 同语义）
                first_url = _first_strm_url(head)
                if not first_url:
                    skipped_files += 1
                    continue
                signed = sign_strm_url(first_url, ttl_seconds=ttl)
                if not signed or signed == first_url:
                    # 极端情况（空串/签名无变化）：不重写，记 bad 备查
                    bad_files += 1
                    continue
                if len(head) >= 4096:
                    # 罕见：文件超过 4KB，重读全文再替换（其余行原样保留）
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        full = f.read()
                    new_text = _replace_first_url(full, signed, first_url)
                else:
                    new_text = _replace_first_url(head, signed, first_url)
                # tmp 文件名带 pid+线程 id：.strm 生成器与签名刷新都跑在 api 进程
                # 的不同线程（#605 后刷新线程为 strm-sig-refresh），可能并发写同一文件；
                # 确定性的 "<file>.tmp" 会让双方交错写入同一个 tmp，再由 os.replace
                # 发布撕裂内容。唯一 tmp 名保证每次发布的都是完整文件。
                tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident():x}"
                try:
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        f.write(new_text)
                    # CAS：replace 前确认文件首行仍是我们签名的那个 URL。
                    # 若生成器在本轮处理期间重写了该文件，以新内容为准、本轮跳过
                    # （下个 tick 再处理），避免用过期读覆盖生成器的新文件。
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        cur_head = f.read(4096)
                    if _first_strm_url(cur_head) != first_url:
                        logger.debug("strm_sig_refresh: %s 在本轮处理期间被改写，跳过", path)
                        skipped_files += 1
                    else:
                        os.replace(tmp_path, path)
                        refreshed += 1
                finally:
                    # best-effort 清理自己的 tmp（replace 成功后文件已不存在，忽略报错）
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            except Exception:  # noqa: BLE001 — 单个文件异常隔离
                logger.debug("strm_sig_refresh: 处理文件失败 %s", path, exc_info=True)
                errors += 1
                continue

    # 5) 记录本次运行时间（写失败只记 warning）
    try:
        with open(STRM_REFRESH_LAST_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(now)))
    except OSError:
        logger.warning("strm_sig_refresh: 上次运行时间写入失败 %s", STRM_REFRESH_LAST_FILE)

    # 6) 汇总
    result = {
        "refreshed": refreshed,
        "skipped_files": skipped_files,
        "bad_files": bad_files,
        "errors": errors,
        "duration_s": round(time.time() - started, 3),
    }
    logger.info("strm_sig_refresh: %s", result)
    if errors > 0:
        logger.warning(
            "strm_sig_refresh: %d 个文件刷新失败（refreshed=%d, bad=%d），请检查日志",
            errors,
            refreshed,
            bad_files,
        )
    return result


def janitor_tick() -> dict:
    """一次维护动作（启动后由后台线程按 MAINTENANCE_INTERVAL 周期执行）"""
    from backend.database import SessionLocal
    from backend.emby_server import streaming

    result = {"sessions_reaped": 0, "sessions_pruned": 0, "transcodes_reaped": 0,
              "transcode_orphans": 0, "subtitle_cache_pruned": 0,
              "tmdb_cache_pruned": 0,
              "item_facets_backfilled": 0, "item_facets_orphans": 0,
              "scan_dir_states_pruned": 0, "scan_runs_pruned": 0,
              "emby_tokens_purged": 0,
              "images_pruned": 0,
              "images_freed_bytes": 0, "ai_usage_pruned": 0,
              "thumbs_backfilled": 0, "thumbs_backfill_done": False,
              "query_plans_optimized": False}
    try:
        result["transcodes_reaped"] = streaming.reap_stale_transcodes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("回收转码会话失败: %s", exc)

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
        # 只清「已经不活跃」的目录：给刚启动、还没登记进注册表的会话留出余量
        result["transcode_orphans"] = cleanup_transcode_orphans(min_age_seconds=300)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理遗留转码目录失败: %s", exc)
    try:
        result["subtitle_cache_pruned"] = prune_subtitle_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("字幕缓存淘汰失败: %s", exc)
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
    # .strm 签名刷新（内部按 TTL 推导的节奏节流；异常隔离，不影响整轮维护）
    try:
        result["strm_sig_refresh"] = strm_sig_refresh_tick()
    except Exception as exc:  # noqa: BLE001
        logger.warning(".strm 签名刷新失败: %s", exc)
        result["strm_sig_refresh"] = {"skipped": True, "reason": "error"}
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


def start_janitor(interval_seconds: Optional[int] = None) -> bool:
    """启动后台维护线程（同一进程只启动一次；daemon，随进程退出）"""
    global _JANITOR_STARTED
    interval = MAINTENANCE_INTERVAL if interval_seconds is None else max(30, interval_seconds)
    with _JANITOR_LOCK:
        if _JANITOR_STARTED:
            return False
        _JANITOR_STARTED = True

    def _loop() -> None:
        while True:
            threading.Event().wait(interval)
            try:
                summary = janitor_tick()
                if any(summary.values()):
                    logger.info("维护周期完成: %s", summary)
                gc.collect()  # 2026-10-07 压力测试：强制回收防内存不回落
            except Exception as exc:  # noqa: BLE001 — 维护线程绝不能因为一次失败而退出
                logger.warning("维护周期异常: %s", exc)

    threading.Thread(target=_loop, daemon=True, name="maintenance-janitor").start()
    logger.info("后台维护已启动（每 %s 秒）", interval)
    return True


def shutdown_cleanup() -> dict:
    """进程退出前的收尾：不留孤儿 ffmpeg、不留半截临时目录"""
    from backend.emby_server import streaming

    result = {"transcodes_stopped": 0, "transcode_dirs_removed": 0}
    try:
        result["transcodes_stopped"] = streaming.stop_all_transcodes()
    except Exception as exc:  # noqa: BLE001
        logger.warning("停止转码会话失败: %s", exc)
    try:
        result["transcode_dirs_removed"] = cleanup_transcode_orphans()
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理转码目录失败: %s", exc)
    if any(result.values()):
        logger.info("退出收尾完成: %s", result)
    return result


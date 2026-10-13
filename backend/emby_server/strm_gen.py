"""Google Drive 视频 → .strm 文件生成器（内置版）。

把原来独立脚本 ``/opt/aetrix-portal/scripts/gen_strm_from_drive.py`` 的逻辑
收进后端，做成管理后台可配置、可手动触发、可看进度的能力。

与旧脚本的关键差异（刻意为之）：
- **Drive 保持私有**：旧脚本生成前会把文件设为公开分享（``ensure_shared``），
  与当前「Drive 私有 + SA 服务端中转」架构冲突。本模块**不做任何分享变更**，
  .strm 里只写文件 ID 派生的直链，播放时由 ``drive_auth`` 用 SA 凭据中转。
- **走 Drive API 而非 rclone**：复用 ``drive_changes`` 的 SA 发现与 token 逻辑
  （横切能力只许一套），``files.list`` 分页拉取，比 ``rclone lsf`` 更可靠，
  不会因单次超长列举超时而丢文件。
- **状态落库**：旧脚本用 ``/opt/strm/.gen_strm_state.db``（sqlite 文件），
  容器重建即丢。本模块用 ``strm_gen_files`` 表，随 PG 备份走。
- **完整性校验**：生成后按剧集比对「Drive 文件数 vs .strm 数」，缺失的上报，
  不再静默丢集。

.strm 内容格式：与旧脚本一致的单行直链
``https://drive.google.com/uc?export=download&id=<FILE_ID>&confirm=t``，
播放链路由 ``drive_auth.drive_api_media_url`` 统一转成 ``alt=media``。
保持格式一致，85k 存量文件与新文件走同一套解析，无需迁移。

配置项（SystemConfig，全部热读）：
- strm_gen_enabled：总开关，默认 true
- strm_gen_source_dir：Drive 上的源目录，逗号分隔支持多目录
  （相对网盘根，如 "MoviePilot/,nastool/"），默认 "MoviePilot/"
- strm_gen_drive_id：指定单个 Drive ID；空或 "all" = 所有发现的盘（含个人盘）。
  默认空（自动发现）。改动此配置不影响已生成 .strm，只影响后续轮次列举范围。
- strm_gen_interval_minutes：增量间隔（分钟），默认 60；0 = 关闭定时增量
- strm_gen_lock_timeout_minutes：运行锁僵死超时（分钟），默认 180；0=关闭自愈
- strm_gen_prune：是否删除 Drive 上已不存在的 .strm 文件及状态行，默认 true
  （删文件不可逆：配合 strm_gen_prune_dry_run 默认 true，默认只演习、
  把 would-delete 清单记进缺集报告；管理员确认后手动关闭 dry_run 才真正 armed）
- strm_gen_prune_dry_run：演习开关，默认 true（只记 would-delete，不真删）
- strm_gen_prune_max_ratio：单轮删除 ≥ 状态总行数此比例 → 整批中止，默认 0.2
- strm_gen_prune_max_absolute：删除绝对上限 max(50, total*0.2)，默认 50
- strm_gen_verify_new_ids：增量中新文件写盘前验 file_id，默认 true
- strm_orphan_check_enabled：反向僵尸校验任务开关，默认 true
- strm_orphan_check_interval_days：校验周期（天，±20% 抖动），默认 7
- strm_orphan_check_batch：每轮最多校验 file_id 数，默认 2000
- strm_orphan_check_qps：per-SA QPS 上限，默认 2.0
- strm_gen_last_run：上次执行完成时间（ISO）
- strm_gen_last_stats：上次执行统计（JSON，含 tombstone/would-delete/prune_blocked）
- strm_gen_scan_trigger_failures：扫库触发器失败记录（JSON），供 system_alerts 的
  strm_scan_trigger_failed 事件读取
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG_ENABLED = "strm_gen_enabled"
CONFIG_SOURCE_DIR = "strm_gen_source_dir"
CONFIG_LAST_RUN = "strm_gen_last_run"
CONFIG_LAST_STATS = "strm_gen_last_stats"
# dry_run 演习模式 would-delete 积压起始时间（UTC ISO）：R10 长期提醒用——
# 积压超过 DRY_RUN_STALE_WARN_DAYS 天未处理，missing_report 给黄条提醒字段。
CONFIG_DRY_RUN_BACKLOG_SINCE = "strm_gen_dry_run_backlog_since"
DRY_RUN_STALE_WARN_DAYS = 3
CONFIG_DRIVE_ID = "strm_gen_drive_id"
CONFIG_PRUNE = "strm_gen_prune"
# 删除保护（块2）：演习开关 / 比例阈值 / 绝对上限，全部热读。
CONFIG_PRUNE_DRY_RUN = "strm_gen_prune_dry_run"
CONFIG_PRUNE_MAX_RATIO = "strm_gen_prune_max_ratio"
CONFIG_PRUNE_MAX_ABSOLUTE = "strm_gen_prune_max_absolute"
# 生成时校验（块4 §6.3）：增量中新文件写盘前验一次 file_id
CONFIG_VERIFY_NEW_IDS = "strm_gen_verify_new_ids"
# 反向僵尸校验（块4 §6.2）：开关 / 周期天数 / 每轮上限 / per-SA QPS
CONFIG_ORPHAN_ENABLED = "strm_orphan_check_enabled"
CONFIG_ORPHAN_INTERVAL_DAYS = "strm_orphan_check_interval_days"
CONFIG_ORPHAN_BATCH = "strm_orphan_check_batch"
CONFIG_ORPHAN_QPS = "strm_orphan_check_qps"
CONFIG_ORPHAN_LAST_RUN = "strm_orphan_check_last_run"
CONFIG_ORPHAN_NEXT_RUN = "strm_orphan_check_next_run"
# 增量间隔（分钟）：两次增量生成之间的间隔，用户可在后台自由设置。
# 0 = 关闭定时增量（只手动触发）。默认 60。
CONFIG_INTERVAL = "strm_gen_interval_minutes"
# 运行锁僵死超时（分钟）：锁被持有超过此时长视为僵死（持有者崩溃/卡死），
# 新轮次可抢占。默认 180；0=关闭自愈（保持旧行为：锁永久有效）。
CONFIG_LOCK_TIMEOUT_MIN = "strm_gen_lock_timeout_minutes"
DEFAULT_LOCK_TIMEOUT_MIN = 180
# 数据源类型：drive=Google Drive，local=本地目录。默认 drive（向后兼容）。
CONFIG_SOURCE_TYPE = "strm_gen_source_type"
# 本地数据源的根目录（source_type=local 时有效）
CONFIG_LOCAL_ROOT = "strm_gen_local_root"
# 抖动比例：调度间隔按 uniform(1-ratio, 1+ratio) 打散。
# 反风控：固定节拍容易被识别为机器行为（Google 风控/本地 IO burst），
# 每次等待都重新随机化。0=关闭抖动（便于测试）。钳制在 [0, 0.5]。
CONFIG_JITTER_RATIO = "strm_gen_jitter_ratio"
# 多盘并行列举的并发数（2026-10-12）：ThreadPoolExecutor 的 max_workers。
# 默认 3（保守），范围 [1, 10]。Drive API 并发不超过 10，留余量防风控。
CONFIG_CONCURRENCY = "strm_gen_drive_concurrency"
# Drive API 全局速率上限（QPS，2026-10-12）：令牌桶硬上限，
# 并发 + 重试叠加时总请求速率不能超过它。默认 8.0（保守，Drive per-user 配额约 10 QPS）。
CONFIG_API_QPS = "strm_gen_api_qps"
# 子树限定列举开关（2026-10-13，S1）：全量回退时只拉源目录子树的目录+文件，
# 不再全盘扁平拉取。默认开；关掉则回退到旧的扁平全量拉取（应急回滚用）。
CONFIG_SUBTREE_LISTING = "strm_gen_subtree_listing"

DEFAULT_SOURCE_DIR = "MoviePilot/"
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_JITTER_RATIO = 0.2
DEFAULT_CONCURRENCY = 3
DEFAULT_API_QPS = 8.0
#: 子树限定列举默认开（与 CONFIG_SUBTREE_LISTING 缺省值一致）
DEFAULT_SUBTREE_LISTING = True

# token 401 后刷新重试的最大次数
TOKEN_RETRY_MAX = 3

VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".mts", ".vob",
    ".wmv", ".flv", ".rmvb", ".rm", ".mov", ".3gp", ".webm",
    ".mpg", ".mpeg", ".iso", ".divx", ".asf",
})
SKIP_DIR_NAMES = frozenset({"@eaDir", "#recycle", "#snapshot", ".stversions", ".DS_Store"})

# Drive files.list 单页大小
PAGE_SIZE = 1000


def _db_config(db, key: str, default: str = "") -> str:
    """读 SystemConfig（热读，短 TTL 缓存）。"""
    try:
        from backend.integrations import store
        return store.get_value(db, key, default) or default
    except Exception:
        return default


def enabled(db) -> bool:
    return _db_config(db, CONFIG_ENABLED, "true").strip().lower() in ("1", "true", "yes", "on")


def source_dirs(db) -> list[str]:
    """Drive 源目录列表，逗号分隔；每个归一化为以 / 结尾的相对路径。

    如 "MoviePilot/,nastool/"。空字符串条目被忽略；全空（显式配空）= 网盘根
    （[]，与旧 source_dir 返回 "" 的语义一致）。
    未配置时默认 [DEFAULT_SOURCE_DIR]，保持单目录行为不变。
    """
    raw = _db_config(db, CONFIG_SOURCE_DIR, DEFAULT_SOURCE_DIR)
    if not raw or not raw.strip():
        return []
    dirs = []
    for part in raw.split(","):
        p = part.strip().replace("\\", "/").strip("/")
        if p:
            dirs.append(p + "/")
    return dirs or [DEFAULT_SOURCE_DIR]


def source_dir(db) -> str:
    """主源目录（向后兼容）：取 source_dirs 的第一项；空列表时返回 ""（网盘根）。"""
    dirs = source_dirs(db)
    return dirs[0] if dirs else ""


def source_dir_raw(db) -> str:
    """源目录配置原文（逗号分隔），供管理后台回显/回写，避免多目录被截断。"""
    return _db_config(db, CONFIG_SOURCE_DIR, DEFAULT_SOURCE_DIR)


# strm_gen_drive_id 取值 "all"（大小写不敏感）：显式选择"所有发现的盘"，
# 等价于留空（自动发现）。留空与 "all" 的唯一区别是可观测性：
# 配置成 "all" 时意图明确，不会被误认为"忘配了"。
DRIVE_ID_ALL = "all"


def drive_id_config(db) -> str:
    """配置指定的 Drive ID（空或 "all" = 自动选择所有发现的盘）。"""
    return _db_config(db, CONFIG_DRIVE_ID, "").strip()


# 源失败记录（供 system_alerts 的 strm_source_failed 事件读取）。
CONFIG_SOURCE_FAILURES = "strm_gen_source_failures"

# 扫库触发器失败记录：_trigger_strm_scan 失败时写入，供 system_alerts 的
# strm_scan_trigger_failed 事件读取（沿用 strm_source_failed 通道模式）。
CONFIG_SCAN_TRIGGER_FAILURES = "strm_gen_scan_trigger_failures"

def _record_source_failures(db, failed: list[dict]) -> None:
    """把本轮列举失败的源记入 SystemConfig，供告警系统读取。

    格式：[{"did":..., "error":..., "at": ISO}]。只保留最近 20 条。
    写入失败不影响主流程（告警是辅助能力）。
    """
    try:
        from backend.integrations import store
        raw = store.get_value(db, CONFIG_SOURCE_FAILURES, "") or "[]"
        try:
            records = json.loads(raw)
        except Exception:
            records = []
        now = datetime.now(timezone.utc).isoformat()
        for f in failed:
            records.append({
                "did": f.get("did", ""),
                "error": str(f.get("error", ""))[:300],
                "at": now,
            })
        records = records[-20:]
        store.write_values(db, {CONFIG_SOURCE_FAILURES: json.dumps(records)}, {})
        # 不在这里 commit：提交由调用方统一处理（run_owned 的 finally →
        # _release_progress 落盘统计时会提交），中途提前提交会固化调用方
        # 未预期的中间状态。
    except Exception as exc:
        logger.warning("strm_gen: 记录源失败失败: %s", exc)


def _record_scan_trigger_failures(db, failures: list[tuple]) -> None:
    """把扫库触发器失败记入 SystemConfig，供告警系统读取。

    failures: [(lib_id|"-", error)]，lib_id="-" 表示"还没到逐库阶段"
    （模块加载/查库失败）。格式：[{"lib":..., "error":..., "at": ISO}]，
    只保留最近 20 条。写入失败不影响主流程（沿用 _record_source_failures
    的"不提前 commit"约定）。
    """
    try:
        from backend.integrations import store
        raw = store.get_value(db, CONFIG_SCAN_TRIGGER_FAILURES, "") or "[]"
        try:
            records = json.loads(raw)
        except Exception:
            records = []
        now = datetime.now(timezone.utc).isoformat()
        for lib_id, err in failures:
            records.append({
                "lib": str(lib_id),
                "error": str(err)[:300],
                "at": now,
            })
        records = records[-20:]
        store.write_values(db, {CONFIG_SCAN_TRIGGER_FAILURES: json.dumps(records)}, {})
    except Exception as exc:
        logger.warning("strm_gen: 记录扫库触发失败失败: %s", exc)


def list_drives() -> list[dict]:
    """列出发现的 Drive，供管理后台展示/选择。

    返回 [{"drive_id": ..., "remotes": [...], "is_personal": bool}]，
    按 drive_id 排序，保证展示顺序稳定。
    """
    drives = _discover_drives()
    dc = _drive_modules()
    out = []
    for did in sorted(drives.keys()):
        try:
            personal = dc._is_personal_drive_key(did)
        except Exception:
            personal = False
        out.append({"drive_id": did, "remotes": drives[did], "is_personal": personal})
    return out
def prune_enabled(db) -> bool:
    """是否删除 Drive 上已不存在的 .strm 文件及状态行。

    默认开启（true），但配合 strm_gen_prune_dry_run=true 默认只演习：
    把 would-delete 清单记进缺集报告，不真删。管理员确认清单无误后，
    手动关闭 dry_run 才真正 armed。

    存量部署：已显式写过 false 的保持 false（只改默认值，不改已存值）；
    从未配置过的自动启用（演习模式）。
    """
    return _db_config(db, CONFIG_PRUNE, "true").strip().lower() in ("1", "true", "yes", "on")


def prune_dry_run(db) -> bool:
    """演习开关：true=只统计 would-delete 进报告，不真删。默认 true。"""
    return _db_config(db, CONFIG_PRUNE_DRY_RUN, "true").strip().lower() in ("1", "true", "yes", "on")


def _dry_run_backlog_touch(db, has_backlog: bool) -> None:
    """维护 dry_run 积压起始时间戳（R10 长期提醒）。

    has_backlog=True：首次出现 would-delete 积压时写入当前 UTC ISO 时间；
    has_backlog=False：本轮无待删项，积压清零则清掉该时间戳。
    注意：store.write_values 不自动提交，由调用方事务提交；读写包在 SAVEPOINT
    里，失败只回滚保存点——不污染调用方事务、不抛异常。
    """
    try:
        from backend.integrations import store
        try:
            with db.begin_nested():
                current = store.get_value(db, CONFIG_DRY_RUN_BACKLOG_SINCE, "") or ""
                if has_backlog:
                    if current:
                        return
                    iso = datetime.now(timezone.utc).isoformat()
                    store.write_values(
                        db,
                        {CONFIG_DRY_RUN_BACKLOG_SINCE: iso},
                        {CONFIG_DRY_RUN_BACKLOG_SINCE: "dry_run 删除积压起始时间（UTC ISO）"},
                    )
                else:
                    if not current:
                        return
                    store.write_values(
                        db,
                        {CONFIG_DRY_RUN_BACKLOG_SINCE: ""},
                        {CONFIG_DRY_RUN_BACKLOG_SINCE: "dry_run 删除积压起始时间（UTC ISO）"},
                    )
        except Exception:
            pass  # 保存点已回滚，外层事务不受影响
    except Exception as exc:
        logger.warning("strm_gen: 更新 dry_run 积压时间戳失败: %s", exc)


def _dry_run_stale_info(db, stats: dict) -> dict:
    """纯计算（不写库）：dry_run 长期开着且 would-delete 积压超阈值时的提醒字段。

    返回 {"dry_run_stale_warning", "dry_run_stale_text", "dry_run_backlog_since"}——
    管理后台配置页"删除保护"分组据此渲染黄条（前端 hook 点）。
    """
    would = stats.get("prune_would_delete") or {}
    try:
        count = int(would.get("count") or 0)
    except (ValueError, TypeError):
        count = 0
    dry_run_on = prune_dry_run(db)
    try:
        from backend.integrations import store
        try:
            with db.begin_nested():
                since_raw = store.get_value(db, CONFIG_DRY_RUN_BACKLOG_SINCE, "") or ""
        except Exception:
            since_raw = ""  # 保存点已回滚，外层事务不受影响
    except Exception:
        since_raw = ""

    stale = False
    if dry_run_on and count > 0 and since_raw:
        try:
            since = datetime.fromisoformat(since_raw)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            stale = (datetime.now(timezone.utc) - since).days >= DRY_RUN_STALE_WARN_DAYS
        except (ValueError, TypeError):
            stale = False  # 解析失败按"不提醒"处理

    return {
        "dry_run_stale_warning": bool(stale),
        "dry_run_stale_text": ("演习模式已拦截 %d 个删除，请确认后关闭 dry_run" % count) if stale else "",
        "dry_run_backlog_since": since_raw or None,
    }


def dry_run_stale_info(db, stats: dict) -> dict:
    """公开包装：供管理后台等外部模块调用的 dry_run 长期积压提醒字段。

    R18-P3：原先 admin_strm_gen 直接跨模块调私有函数 _dry_run_stale_info，
    破坏封装。外部一律走这个公开入口。
    """
    return _dry_run_stale_info(db, stats)


def prune_max_ratio(db) -> float:
    """单轮删除 ≥ 状态总行数此比例 → 整批中止。默认 0.2，钳制在 (0, 1]。"""
    try:
        v = float(_db_config(db, CONFIG_PRUNE_MAX_RATIO, "0.2").strip())
    except (ValueError, TypeError):
        v = 0.2
    return min(1.0, max(0.01, v))


def prune_max_absolute(db) -> int:
    """删除绝对上限：max(absolute, total*ratio)。默认 50，最小 1。"""
    try:
        v = int(float(_db_config(db, CONFIG_PRUNE_MAX_ABSOLUTE, "50").strip()))
    except (ValueError, TypeError):
        v = 50
    return max(1, v)


def verify_new_ids(db) -> bool:
    """增量中新文件写盘前是否验一次 file_id（files.get）。默认 true。"""
    return _db_config(db, CONFIG_VERIFY_NEW_IDS, "true").strip().lower() in ("1", "true", "yes", "on")


def orphan_check_enabled(db) -> bool:
    """反向僵尸校验任务开关。默认 true。"""
    return _db_config(db, CONFIG_ORPHAN_ENABLED, "true").strip().lower() in ("1", "true", "yes", "on")


def orphan_check_interval_days(db) -> float:
    """反向僵尸校验周期（天），实际间隔带 ±20% 抖动。默认 7，最小 1。"""
    try:
        v = float(_db_config(db, CONFIG_ORPHAN_INTERVAL_DAYS, "7").strip())
    except (ValueError, TypeError):
        v = 7.0
    return max(1.0, v)


def orphan_check_batch(db) -> int:
    """每轮最多校验的 file_id 数。默认 2000，钳制 [100, 10000]。"""
    try:
        v = int(float(_db_config(db, CONFIG_ORPHAN_BATCH, "2000").strip()))
    except (ValueError, TypeError):
        v = 2000
    return min(10000, max(100, v))


def orphan_check_qps(db) -> float:
    """反向校验 per-SA QPS 上限。默认 2.0，钳制 [0.5, 10]。"""
    try:
        v = float(_db_config(db, CONFIG_ORPHAN_QPS, "2.0").strip())
    except (ValueError, TypeError):
        v = 2.0
    return min(10.0, max(0.5, v))


def interval_minutes(db) -> int:
    """增量间隔（分钟）。用户可在后台自由设置，0=关闭定时增量（只手动触发）。

    非法值回退 60，钳制在 [0, 1440]。0 表示关闭。
    """
    raw = _db_config(db, CONFIG_INTERVAL, str(DEFAULT_INTERVAL_MINUTES)).strip()
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return DEFAULT_INTERVAL_MINUTES
    return max(0, min(1440, v))


# 运行锁超时读缓存：try_acquire 可能无 db（如纯内存调用），此时用上次热读到的值。
_lock_timeout_min_cache = DEFAULT_LOCK_TIMEOUT_MIN


def lock_timeout_minutes(db=None) -> int:
    """运行锁僵死超时（分钟）。0=关闭自愈。

    默认 180：增量轮目标 <2 分钟、全量轮历史约 1–2 小时（8.6 万文件），
    180 分钟是 90 倍余量；此前曾出现锁死 26 小时的真实故障（2026-10-13）。
    非法值回退默认，钳制在 [0, 1440]。
    db=None 时返回上次热读缓存（供无 db 的 try_acquire 调用）。
    """
    global _lock_timeout_min_cache
    if db is None:
        return _lock_timeout_min_cache
    raw = _db_config(db, CONFIG_LOCK_TIMEOUT_MIN,
                     str(DEFAULT_LOCK_TIMEOUT_MIN)).strip()
    try:
        v = int(raw)
    except (ValueError, TypeError):
        v = DEFAULT_LOCK_TIMEOUT_MIN
    v = max(0, min(1440, v))
    _lock_timeout_min_cache = v
    return v


def source_type(db) -> str:
    """数据源类型：drive=Google Drive，local=本地目录。默认 drive（向后兼容）。"""
    raw = _db_config(db, CONFIG_SOURCE_TYPE, "drive").strip().lower()
    return raw if raw in ("drive", "local") else "drive"


def local_root(db) -> str:
    """本地数据源的根目录（source_type=local 时有效）。"""
    return _db_config(db, CONFIG_LOCAL_ROOT, "/media").strip()


def jitter_ratio(db) -> float:
    """调度抖动比例，钳制在 [0, 0.5]。

    反风控：固定节拍容易被识别为机器行为，每次等待按
    uniform(1-ratio, 1+ratio) 重新随机化。0=关闭抖动（便于测试）。
    """
    raw = _db_config(db, CONFIG_JITTER_RATIO, str(DEFAULT_JITTER_RATIO)).strip()
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return DEFAULT_JITTER_RATIO
    return max(0.0, min(0.5, v))


def drive_concurrency(db) -> int:
    """多盘并行列举的并发数，钳制在 [1, 10]。

    速度与风控的平衡：Drive API 并发不超过 10，默认 3（保守）。
    管理员可在后台调整，调整后下次生成生效（热读）。
    """
    raw = _db_config(db, CONFIG_CONCURRENCY, str(DEFAULT_CONCURRENCY)).strip()
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return DEFAULT_CONCURRENCY
    return max(1, min(10, v))


def api_qps(db) -> float:
    """Drive API 全局速率上限（QPS），钳制在 [1.0, 20.0]。

    令牌桶硬上限：并发 + 重试叠加时总请求速率不能超过它。
    默认 8.0（保守，Drive per-user 配额约 10 QPS，留余量）。
    """
    raw = _db_config(db, CONFIG_API_QPS, str(DEFAULT_API_QPS)).strip()
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return DEFAULT_API_QPS
    return max(1.0, min(20.0, v))


def subtree_listing_enabled(db) -> bool:
    """子树限定列举开关（S1）：全量回退时只拉源目录子树的条目。

    默认开。关闭时回退到旧的扁平全量拉取——应急回滚口，不用重新部署。
    管理员可在后台调整，调整后下次生成生效（热读）。
    """
    raw = _db_config(db, CONFIG_SUBTREE_LISTING,
                     "1" if DEFAULT_SUBTREE_LISTING else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _jittered_interval_sec(db) -> float:
    """本次增量等待秒数：interval 分钟 ± 抖动，每次调用重新随机化。

    反风控：用户担心缩短间隔触发 Google 风控。这里做了三层防护：
    1. 间隔抖动：避免机械的固定节拍（比如精确每小时整点）被识别为机器行为。
    2. Changes API 本身只拉变化，配额消耗远小于全量 files.list。
    3. Lister 内部对 burst 操作（多页拉取/大量 stat）也有微抖动限速。
    """
    import random as _random
    base = interval_minutes(db) * 60.0
    ratio = jitter_ratio(db)
    if ratio <= 0:
        return base
    return base * _random.uniform(1.0 - ratio, 1.0 + ratio)


def _safe_relpath(rel: str) -> str:
    """清理相对路径中的危险分量（纵深防御）。

    Drive 本身不允许 "/" 和纯 ".." 文件名，正常走不到这里；
    但输出路径直接拼接到 /strm 下，必须保证拼不出目录穿越。
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


# ---------------------------------------------------------------------------
# 路径映射（与旧脚本一致）
# ---------------------------------------------------------------------------

def strm_relpath(root_relpath: str, src_prefix: str) -> str:
    """网盘根相对路径 -> 输出相对路径（去掉源目录前缀，换扩展名为 .strm）。"""
    rel = root_relpath
    if src_prefix and rel.startswith(src_prefix):
        rel = rel[len(src_prefix):]
    rel = _safe_relpath(rel)  # 防目录穿越（纵深防御）
    base, _ext = os.path.splitext(rel)
    return base + ".strm"


def is_video_path(relpath: str) -> bool:
    """相对路径是否为视频文件（跳过系统目录/隐藏文件）。"""
    parts = relpath.replace("\\", "/").split("/")
    for p in parts[:-1]:
        if not p or p.startswith(".") or p in SKIP_DIR_NAMES:
            return False
    base = parts[-1]
    if base.startswith("."):
        return False
    ext = os.path.splitext(base)[1].lower()
    return ext in VIDEO_EXTS


def drive_url(file_id: str) -> str:
    """单行直链格式。

    与旧独立脚本 gen_strm_from_drive.py 一致的格式，85k 存量文件与新文件走
    同一套解析（backend/emby_server/drive_auth.drive_api_media_url 统一转成
    alt=media）。旧脚本不在仓库中，格式一致性基于存量文件实测确认。
    """
    return "https://drive.google.com/uc?export=download&id=%s&confirm=t" % file_id


#: .strm 第一行 URL 里的 file_id 提取。本地源 .strm 也是同一格式
#: （id=local:sha…），数据源无关。
_STRM_FILE_ID_RE = re.compile(r"id=([^&\s]+)")


def _extract_strm_file_id(first_line: str) -> Optional[str]:
    """从 .strm 文件第一行 URL 提取 file_id。

    纯函数：drive_url() 写出的单行格式 `…?export=download&id=<FILE_ID>&confirm=t`
    中取 id= 到下一个 & 之间的部分。行里没有 id= 时返回 None。
    """
    if not first_line:
        return None
    m = _STRM_FILE_ID_RE.search(first_line)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Drive API 列举（复用 drive_changes 的 SA/凭据能力）
# ---------------------------------------------------------------------------

#: Drive files.list 地址（_list_files_page 列举文件/目录用）
FILES_LIST_URL = "https://www.googleapis.com/drive/v3/files"


def _drive_modules():
    """惰性导入 drive_changes（重依赖，避免循环导入）。"""
    try:
        from backend.emby_server import drive_changes as dc
    except ImportError:
        import drive_changes as dc  # type: ignore[no-redef]
    return dc


def _get_token() -> Optional[str]:
    dc = _drive_modules()
    try:
        return dc._get_token()
    except Exception:
        return None


def _discover_drives() -> dict:
    """复用 drive_changes 的共享盘发现。返回 {drive_id: [sa_file, ...]}。"""
    dc = _drive_modules()
    try:
        return dc.discover_drives() or {}
    except Exception as exc:
        logger.warning("strm_gen: 发现共享盘失败: %s", exc)
        return {}


def _api_get(url: str, token: str, params: dict) -> dict:
    dc = _drive_modules()
    return dc._api_get(url, token, params)


def _safe_name(name: str) -> str:
    """清理 Drive 返回的文件/目录名（纵深防御）。

    Drive 文件名理论上不含 "/"，但防御性地过滤掉空段、"." 和 ".."，
    防止恶意或异常文件名导致路径穿越。
    """
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)






def _list_files_page(token: str, drive_id: str, query: str,
                     page_token: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
    """单页 files.list。返回 (files, nextPageToken)。"""
    params = {
        "q": query,
        "pageSize": PAGE_SIZE,
        "fields": "nextPageToken,files(id,name,mimeType,size,parents,trashed)",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if page_token:
        params["pageToken"] = page_token
    # 个人盘 vs 共享盘
    dc = _drive_modules()
    try:
        is_personal = dc._is_personal_drive_key(drive_id)
    except Exception:
        is_personal = False
    if is_personal:
        params["spaces"] = "drive"
    else:
        params["driveId"] = drive_id
        params["corpora"] = "drive"
    body = _api_get(FILES_LIST_URL, token, params)
    return body.get("files", []), body.get("nextPageToken")


def _page_fetcher(drive_id: str):
    """返回一个带 401 自动刷新的一页抓取闭包 fetch(query, page_token)。

    SA token 有效期约 1 小时：8.5 万文件的列举要发数千次 API，耗时很可能
    超过 1 小时。原来 token 只在列举开始取一次，中途 401 整个列举作废；
    现在遇到 PermissionError(401) 时刷新一次 token 续跑，仍失败才抛。
    """
    box = {"token": _get_token()}
    if not box["token"]:
        raise RuntimeError("无可用 SA 凭据，无法访问 Drive API")

    def fetch(query: str, page_token: Optional[str] = None):
        try:
            return _list_files_page(box["token"], drive_id, query, page_token)
        except PermissionError:
            fresh = _get_token()
            if not fresh or fresh == box["token"]:
                raise
            box["token"] = fresh
            logger.info("strm_gen: Drive token 已刷新，继续列举")
            return _list_files_page(fresh, drive_id, query, page_token)

    return fetch


def _resolve_dir_id(fetch, drive_id: str, dir_path: str) -> Optional[str]:
    """把 "MoviePilot/剧集" 这样的相对路径解析成 Drive folder ID。

    逐级用 files.list 查 name + parents 定位，每级翻完所有页
    （原来只读第一页：某级目录 >1000 条且目标文件夹排在后面时会误报找不到）。
    找不到返回 None。
    """
    parts = [p for p in dir_path.strip("/").split("/") if p]
    if not parts:
        return None  # 空 = 网盘根，不需要 ID（用 parents 限定即可）
    parent_id: Optional[str] = None
    for part in parts:
        # 转义单引号
        safe = part.replace("'", "\\'")
        q = (
            "mimeType='application/vnd.google-apps.folder' "
            "and trashed=false "
            f"and name='{safe}'"
        )
        if parent_id:
            q += f" and '{parent_id}' in parents"
        found = None
        page_token: Optional[str] = None
        while True:
            files, page_token = fetch(q, page_token)
            if files:
                found = files[0]["id"]
                break
            if not page_token:
                break
        if not found:
            return None
        # 同名多个取第一个（与旧脚本 rclone 行为一致）
        parent_id = found
    return parent_id


def iter_drive_videos(drive_id: str, dir_path, strict: bool = True,
                      subtree_listing: bool = True):
    """流式列出源目录下所有视频文件。

    dir_path: 单个目录（str，向后兼容）或目录列表（list[str]，两盘全挂）。
    多目录时目录树只拉一次（drive 级复用），各源目录分别展开，API 调用
    不随目录数线性增加。

    yield (root_relpath, file_id, size)。root_relpath 为相对网盘根路径
    （含源目录前缀本身，与旧实现一致，状态键保持兼容）。

    strict=True（默认）：源目录不存在时抛 RuntimeError（显式配置的盘，
    管理员需要知道配错了）。
    strict=False：源目录不存在时记 warning 并返回空（自动发现模式下，
    不是每个盘都有媒体目录，缺目录是预期情况，不应报错）。

    subtree_listing=True（默认，S1）：两阶段子树限定列举——阶段一隻拉
    目录项建树并展开源子树，阶段二按源子树目录分批（'id' in parents）
    查文件。API 请求数从「全盘条目数/1000」降到「目录数/1000 +
    子树目录数/30」，请求总数更少（零风控影响）；产出与扁平拉取等价
    （同一文件集合、同一 rel 构造、同一跳过规则）。
    subtree_listing=False：旧的扁平全量拉取（应急回滚用，
    见 strm_gen_subtree_listing 配置）。

    实现演进注记：最早是 BFS 每个目录一次 files.list（数千次 API，每次
    200~500ms，且全程不更新进度，前端看起来像卡死）；后改扁平全量拉取
    （约 100~1000 次请求，但为源子树 10 万条目拉全盘 60~100 万条目）；
    S1 改为子树限定两阶段（约 100~200 次请求）。

    （修 P1-1：旧实现只在开始时取一次 token，8 万级文件的长列举
    会因 SA token 过期而整体作废——_page_fetcher 的 401 自动刷新保留）。
    """
    if isinstance(dir_path, (list, tuple)):
        dir_paths = [d.strip().strip("/") for d in dir_path if d and d.strip().strip("/")]
    else:
        dir_paths = [dir_path.strip().strip("/")] if (dir_path or "").strip().strip("/") else []
    if subtree_listing:
        yield from _iter_drive_videos_subtree(drive_id, dir_paths, strict=strict)
    else:
        yield from _iter_drive_videos_flat(drive_id, dir_paths, strict=strict)


#: S1：子树列举阶段二每批 in parents 查询的目录数。
#: Drive 查询字符串约 2KB 上限；目录 ID 约 33 字节 → "'id' in parents"
#: 每 ID 约 51 字符（含 or 连接符），30 个 ≈ 1.6KB，留足余量。
SUBTREE_LIST_BATCH = 30


def _iter_drive_videos_subtree(drive_id: str, dir_paths: list, strict: bool = True):
    """S1：两阶段子树限定列举（见 iter_drive_videos 文档）。

    多目录版：阶段一的目录树全盘只拉一次（drive 级复用）；阶段一之二从各源
    目录分别 BFS 展开；阶段二按合并后的目录 ID 分批查文件，产出时带各自的
    源目录前缀（root_prefix），遍历语义与单目录版完全一致。
    """
    fetch = _page_fetcher(drive_id)  # 内部已做 SA 凭据检查 + 401 自动刷新
    # 逐个解析源目录 id（strict 语义按目录独立：自动发现模式缺目录只跳过该目录）
    resolved: list[tuple[str, str]] = []  # (norm_dir, dir_id)；空列表=网盘根
    if not dir_paths:
        resolved = [("", "")]
    for dp in dir_paths:
        dir_id = _resolve_dir_id(fetch, drive_id, dp)
        if dp and not dir_id:
            if not strict:
                logger.warning("strm_gen: drive=%s 上找不到源目录 %r，已跳过该目录",
                               drive_id, dp)
                continue
            raise RuntimeError(f"Drive 上找不到源目录: {dp!r}")
        resolved.append((dp, dir_id))
    if not resolved:
        return

    # 阶段一：只拉目录项（drive 级一次，多目录复用）。
    folder_children: dict[str, list[dict]] = {}
    listed_folders = 0
    page_token: Optional[str] = None
    while True:
        files, page_token = fetch(
            "mimeType='application/vnd.google-apps.folder' and trashed=false",
            page_token)
        for f in files:
            if f.get("trashed"):
                continue
            if f.get("mimeType") != "application/vnd.google-apps.folder":
                continue  # 防御：查询已限定，服务端偶发混入时忽略
            for pid in f.get("parents") or []:
                folder_children.setdefault(pid, []).append(f)
        listed_folders += len(files)
        _set_progress(total=listed_folders,
                      current=f"正在列举 Drive 目录…（已拉取 {listed_folders} 个目录）")
        if not page_token:
            break
    logger.info("strm_gen: drive=%s 子树列举阶段一：拉取 %d 个目录项",
                drive_id, listed_folders)

    # 阶段一之二：本地 BFS，从各源目录分别展开（遍历语义与扁平实现一致：
    # 多 parent 的目录每个 parent 链各展开一次；每个条目记所属源目录前缀）。
    dir_entries: dict[str, list[tuple[str, str]]] = {}  # folder_id -> [(root_prefix, rel_prefix)]
    stack: list[tuple[str, str, str]] = []
    dc = _drive_modules()
    try:
        is_personal = dc._is_personal_drive_key(drive_id)
    except Exception:
        is_personal = False
    for norm, dir_id in resolved:
        if norm:
            stack.append((dir_id, "", norm + "/"))
        else:
            # 与扁平实现一致：共享盘根目录的顶层 parents 是 drive_id，
            # 个人盘用 'root' 别名（'root' in parents 是合法查询）。
            stack.append(("root" if is_personal else drive_id, "", ""))
    while stack:
        folder_id, rel_prefix, root_prefix = stack.pop()
        dir_entries.setdefault(folder_id, []).append((root_prefix, rel_prefix))
        for f in folder_children.get(folder_id, []):
            name = _safe_name(f.get("name", ""))
            fid = f.get("id", "")
            if not name or not fid:
                continue
            if name.startswith(".") or name in SKIP_DIR_NAMES:
                continue
            stack.append((fid, rel_prefix + name + "/", root_prefix))

    # 阶段二：按批查文件。
    dir_ids = list(dir_entries.keys())
    listed_files = 0
    for bi in range(0, len(dir_ids), SUBTREE_LIST_BATCH):
        batch = dir_ids[bi:bi + SUBTREE_LIST_BATCH]
        cond = " or ".join(f"'{d}' in parents" for d in batch)
        q = f"trashed=false and ({cond})"
        page_token = None
        while True:
            files, page_token = fetch(q, page_token)
            for f in files:
                if f.get("trashed"):
                    continue
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    continue  # 目录项只在阶段一处理；这里只收文件（与扁平实现一致：
                # 扁平实现里目录走 stack 分支，不会当文件 yield）
                fid = f.get("id", "")
                name = _safe_name(f.get("name", ""))
                if not name or not fid:
                    continue
                for pid in f.get("parents") or []:
                    for rroot, rp in dir_entries.get(pid) or ():
                        rel = rroot + rp + name
                        if not is_video_path(rel):
                            continue
                        try:
                            size = int(f.get("size") or 0)
                        except (ValueError, TypeError):
                            size = 0
                        yield rel, fid, size
            listed_files += len(files)
            _set_progress(total=listed_folders + listed_files,
                          current=f"正在列举 Drive 文件…（已拉取 {listed_files} 个文件）")
            if not page_token:
                break
    logger.info("strm_gen: drive=%s 子树列举完成：%d 个目录、%d 个文件条目",
                drive_id, len(dir_ids), listed_files)


def _iter_drive_videos_flat(drive_id: str, dir_paths: list, strict: bool = True):
    """旧的扁平全量拉取实现（S1 之前的主路径）。

    保留作应急回滚（strm_gen_subtree_listing=0 时走这里）。
    多目录版：一次 files.list 扁平拉完全盘条目，本地按 parents 建树，
    再从各源目录分别 BFS 展开（只取源目录子树）。
    """
    fetch = _page_fetcher(drive_id)  # 内部已做 SA 凭据检查 + 401 自动刷新
    # 逐个解析目录 id（strict 语义按目录独立：自动发现模式缺目录只跳过该目录）
    resolved: list[tuple[str, str]] = []  # (norm_dir, dir_id)；网盘根用 ("", "")
    if not dir_paths:
        resolved = [("", "")]
    for dp in dir_paths:
        dir_id = _resolve_dir_id(fetch, drive_id, dp)
        if dp and not dir_id:
            if not strict:
                logger.warning("strm_gen: drive=%s 上找不到源目录 %r，已跳过该目录",
                               drive_id, dp)
                continue
            raise RuntimeError(f"Drive 上找不到源目录: {dp!r}")
        resolved.append((dp, dir_id))
    if not resolved:
        return

    # 1. 扁平拉取：全盘条目（文件+目录）一次拉完，本地按 parents 建树。
    #    只取源目录子树在第 2 步过滤，这里不加 in parents 限定。
    children: dict[str, list[dict]] = {}
    listed = 0
    page_token: Optional[str] = None
    while True:
        files, page_token = fetch("trashed=false", page_token)
        for f in files:
            if f.get("trashed"):
                continue
            for pid in f.get("parents") or []:
                children.setdefault(pid, []).append(f)
        listed += len(files)
        _set_progress(total=listed,
                      current=f"正在列举 Drive 文件…（已拉取 {listed} 个条目）")
        if not page_token:
            break
    logger.info("strm_gen: drive=%s 扁平拉取 %d 个条目", drive_id, listed)

    # 2. 本地 BFS：从各源目录（或网盘根）逐层展开，只取源目录子树。
    #    网盘根相对路径前缀：dir_path 本身（与旧实现一致，状态键保持兼容）。
    stack: list[tuple[str, str, str]] = []  # (folder_id, rel_prefix, root_prefix)
    for norm, did in resolved:
        if norm:
            stack.append((did, "", norm + "/"))
        else:
            # 共享盘根目录：'root' 别名只对个人盘有效，共享盘顶层用 drive_id。
            # 原来无条件用 'root' in parents，共享盘配空源目录时列举结果恒为空
            # （静默丢全部）——这里换成本地建树的等价逻辑。
            dc = _drive_modules()
            try:
                is_personal = dc._is_personal_drive_key(drive_id)
            except Exception:
                is_personal = False
            stack.append(("root" if is_personal else drive_id, "", ""))

    while stack:
        folder_id, rel_prefix, root_prefix = stack.pop()
        for f in children.get(folder_id, []):
            name = _safe_name(f.get("name", ""))
            fid = f.get("id", "")
            if not name or not fid:
                continue
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                if name.startswith(".") or name in SKIP_DIR_NAMES:
                    continue
                stack.append((fid, rel_prefix + name + "/", root_prefix))
            else:
                rel = root_prefix + rel_prefix + name
                if not is_video_path(rel):
                    continue
                try:
                    size = int(f.get("size") or 0)
                except (ValueError, TypeError):
                    size = 0
                yield rel, fid, size


# ---------------------------------------------------------------------------
# 进度状态（进程内 + 可查询）
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_progress: dict[str, Any] = {
    "running": False,
    # acquired_at：运行锁获取时刻（time.monotonic 秒，防系统时间跳变），
    # B1 自愈用。release 时清 None。
    "acquired_at": None,
    # lock_token：每次获取运行权换一个新的（uuid hex）。B1 抢占后，
    # 被抢占的旧持有者结束时凭 token 发现锁已易主，不再清除新持有者的 running。
    "lock_token": None,
    "started_at": None,
    "finished_at": None,
    "phase": "idle",          # idle | listing | generating | verifying | done | error
    "total": 0,
    "done": 0,
    "generated": 0,
    "skipped": 0,
    "failed": 0,
    "errors": [],             # 最近的错误信息（最多 20 条）
    "current": "",            # 当前处理的文件
}


def get_progress(db: Any = None) -> dict[str, Any]:
    """返回当前生成进度。

    db 可选：当进程内状态为"从未运行过"（phase=="idle" 且 running 为 False
    且 started_at 为 None——进程重启后就是这个状态）且传入 db 时，
    从 DB 的 strm_gen_last_stats / strm_gen_last_run 回填上次执行的真实统计，
    phase 置为 "done"，并写回 _progress 缓存（前端轮询不再每次查 DB）。

    回填绝不会覆盖正在运行的任务：只有"从未运行过"才回填。
    DB 读失败 / 无统计 / JSON 损坏时降级返回内存 idle 拷贝，不抛异常。
    """
    with _lock:
        if (db is not None
                and not _progress["running"]
                and _progress["phase"] == "idle"
                and _progress["started_at"] is None):
            try:
                _backfill_last_stats(db)
            except Exception:
                logger.warning("strm_gen: 从 DB 回填上次生成统计失败", exc_info=True)
        return dict(_progress)


def _backfill_last_stats(db) -> None:
    """从 DB 回填上次执行统计到 _progress。

    调用方必须持有 _lock 且已确认进程内状态为"从未运行过"。
    任何异常都由调用方捕获并降级，本函数只做"无数据/坏数据时静默跳过"。
    """
    raw_stats = _db_config(db, CONFIG_LAST_STATS, "")
    if not raw_stats:
        return
    try:
        stats = json.loads(raw_stats)
    except Exception:
        return
    if not isinstance(stats, dict):
        return
    try:
        generated = int(stats.get("generated", 0) or 0)
        skipped = int(stats.get("skipped", 0) or 0)
        failed = int(stats.get("failed", 0) or 0)
        _progress.update({
            "phase": "done",
            "finished_at": _db_config(db, CONFIG_LAST_RUN, "") or None,
            "total": int(stats.get("total", 0) or 0),
            "generated": generated,
            "skipped": skipped,
            "failed": failed,
            "pruned": int(stats.get("pruned", 0) or 0),
            "done": generated + skipped + failed,
            "current": "",
            "errors": [],
        })
    except Exception:
        pass


def _set_progress(**kwargs):
    with _lock:
        _progress.update(kwargs)


def _add_error(msg: str):
    with _lock:
        errs = _progress.get("errors") or []
        errs.append(msg)
        _progress["errors"] = errs[-20:]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _container_strm_root(db) -> str:
    """容器内 .strm 根目录（走 strm_config 唯一事实源）。"""
    try:
        from backend.emby_server import strm_config
        return strm_config.container_path(db)
    except Exception:
        return "/strm"


def _load_state() -> dict[tuple[str, str], tuple[str, int, str]]:
    """读 strm_gen_files 表：{(drive_id, remote_path): (file_id, size, strm_path)}。

    多盘模式下同一相对路径可出现在多个盘上，键必须带 drive_id。
    用独立短会话（不在生成主事务里，避免长事务占连接）。
    """
    try:
        from backend.emby_server import models as em
        from backend.database import SessionLocal
    except Exception:
        return {}
    sess = SessionLocal()
    try:
        rows = sess.query(em.StrmGenFile).all()
        return {(r.drive_id or "", r.remote_path): (r.file_id, r.size or 0, r.strm_path)
                for r in rows}
    except Exception as exc:
        logger.warning("strm_gen: 读状态表失败: %s", exc)
        return {}
    finally:
        sess.close()


def _save_state_rows(rows: list[tuple[str, str, str, int, str]]):
    """批量 upsert 状态。rows: [(drive_id, remote_path, file_id, size, strm_path)]。

    原来逐行 SELECT+add：8.5 万行 = 8.5 万次 point 查询。现在按批
    "先删后插"（DELETE WHERE (drive_id, remote_path) IN (...) + bulk_insert_mappings），
    每批 2 条 SQL，与 PG/SQLite 都兼容。批内按 (drive_id, remote_path) 去重保留最后一条。
    删除必须按盘过滤：同一 rel 在不同盘是不同行，不能误删别的盘。
    """
    if not rows:
        return
    try:
        from backend.emby_server import models as em
        from backend.database import SessionLocal
    except Exception:
        return
    # 批内去重：同 (drive_id, remote_path) 保留最后一条（同目录同名文件的极限情况）
    dedup: dict[tuple[str, str], tuple[str, str, str, int, str]] = {}
    for r in rows:
        dedup[(r[0], r[1])] = r
    rows = list(dedup.values())
    sess = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        # 按盘分组删除（SQLite 不支持多列 IN，逐盘删）
        by_drive: dict[str, list[str]] = {}
        for did, rp, _fid, _size, _sp in rows:
            by_drive.setdefault(did, []).append(rp)
        for did, rps in by_drive.items():
            sess.query(em.StrmGenFile).filter(
                em.StrmGenFile.drive_id == did,
                em.StrmGenFile.remote_path.in_(rps)).delete(synchronize_session=False)
        sess.bulk_insert_mappings(em.StrmGenFile, [
            {"drive_id": did, "remote_path": rp, "file_id": fid, "size": size,
             "strm_path": sp, "updated_at": now}
            for did, rp, fid, size, sp in rows
        ])
        sess.commit()
    except Exception as exc:
        sess.rollback()
        logger.warning("strm_gen: 写状态表失败: %s", exc)
    finally:
        sess.close()


def _delete_state_rows(pairs: list[tuple[str, str]], db=None):
    """删除状态行（给 prune 用）。pairs: [(drive_id, remote_path)]。

    db 可选：传入则用它，否则开独立短会话。必须按盘过滤，
    同一 rel 在不同盘是不同行。
    """
    if not pairs:
        return
    try:
        from backend.emby_server import models as em
        from backend.database import SessionLocal
    except Exception:
        return
    sess = db
    own = False
    if sess is None:
        sess = SessionLocal()
        own = True
    try:
        by_drive: dict[str, list[str]] = {}
        for did, rp in pairs:
            by_drive.setdefault(did, []).append(rp)
        for did, rps in by_drive.items():
            sess.query(em.StrmGenFile).filter(
                em.StrmGenFile.drive_id == did,
                em.StrmGenFile.remote_path.in_(rps)).delete(synchronize_session=False)
        if own:
            sess.commit()
        else:
            sess.flush()
    except Exception as exc:
        if own:
            sess.rollback()
        logger.warning("strm_gen: 删状态表失败: %s", exc)
    finally:
        if own:
            sess.close()


def _backfill_state_from_disk(all_videos: list[tuple[str, str, str, int]],
                              state: dict, strm_root: str) -> dict:
    """A1：从磁盘 .strm 内容反推，重建状态表缺行（零 API 调用）。

    背景：状态表只有 4.9 万行、实际 8.6 万文件，缺行文件每轮都被当成
    "新增"逐个调 Drive API 校验，一轮跑 2.5 小时+。而每个 .strm 文件第
    一行 URL 里本来就带着 `id=<FILE_ID>`，读本地文件就能补全状态表。

    为什么用 file_id 内容匹配、而不用路径反推 rel：strm_relpath 把原扩展名
    换成 .strm，且撞名时加 -2/-3 后缀（_resolve_collision），路径是不可逆
    的——从 .strm 路径反推不出唯一的 rel。file_id 是内容级键：每个视频文件
    恰好对应一个 file_id，碰撞自动对上，不会错配。

    只在增量模式调用（全量模式故意重置状态，不补）。无缺行时直接返回，
    零开销。补上的行会同步写进内存 state，主流程走已有 skip 路径。
    返回 {"backfilled": N, "unmatched": M}。
    """
    missing = [(did, rel, fid, size) for did, rel, fid, size in all_videos
               if (did, rel) not in state]
    if not missing:
        return {"backfilled": 0, "unmatched": 0}
    # 建磁盘索引 {file_id: strm 相对路径}：只读每文件第一行，纯本地 IO。
    disk_index: dict[str, str] = {}
    scanned = 0
    for _root, _dirs, _files in os.walk(strm_root):
        for _fn in _files:
            if not _fn.endswith(".strm"):
                continue
            scanned += 1
            _full = os.path.join(_root, _fn)
            try:
                with open(_full, "r", encoding="utf-8", errors="replace") as _fh:
                    _fid = _extract_strm_file_id(_fh.readline())
            except OSError:
                continue
            # 同一 file_id 极端情况下可能写出多份 .strm（历史残留），
            # 只保留第一份：反推目标是"有状态可跳过"，多一份不影响正确性。
            if _fid and _fid not in disk_index:
                disk_index[_fid] = os.path.relpath(_full, strm_root)
    rows: list[tuple[str, str, str, int, str]] = []
    for did, rel, fid, size in missing:
        spath = disk_index.get(fid)
        if spath:
            rows.append((did, rel, fid, size, spath))
    if rows:
        _save_state_rows(rows)
        for did, rel, fid, size, spath in rows:
            state[(did, rel)] = (fid, size, spath)
    backfilled = len(rows)
    unmatched = len(missing) - backfilled
    logger.info("strm_gen: 磁盘反推补全状态 %d 行（扫描 .strm %d 个，"
                "索引命中 %d 个 file_id），仍有 %d 个缺行反推不到（走 API 校验）",
                backfilled, scanned, len(disk_index), unmatched)
    return {"backfilled": backfilled, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# 墓碑消费：Drive 删文件 → 删 .strm → 删状态行（块1+块2）
#
# 墓碑只做"信号"：消费删磁盘 .strm + 删状态行；库条目的 deleted_at 交给
# 扫描器下一轮按已有删除保护自然处理，不造第二套删除语义。
# ---------------------------------------------------------------------------

def _split_lister_output(items) -> tuple[list, list]:
    """把 lister 产出拆成 (videos, tombstones)。

    videos: [(relpath, file_id, size)]；tombstones: [Tombstone]。
    """
    from backend.emby_server.strm_source import Tombstone
    videos, tombstones = [], []
    for it in items:
        (tombstones if isinstance(it, Tombstone) else videos).append(it)
    return videos, tombstones


def _prune_threshold(total: int, ratio: float, absolute: int) -> int:
    """删除阈值：max(absolute, int(total * ratio))。"""
    return max(absolute, int(total * ratio))


def _prune_budget_ok(db, doomed_count: int, videos_seen: int) -> tuple[bool, str]:
    """删除预算检查（strm_gen 专用，比扫描器更保守）。

    prune 删的是磁盘物理文件（不可逆），扫描器删的是软删除（可恢复），
    所以比例阈值取 0.2（扫描器 0.5），绝对上限 50（扫描器 20）。
    返回 (能不能删, 不能的原因)。只做计数，不删。
    """
    try:
        from backend.emby_server import models as em
        total = db.query(em.StrmGenFile).count()
    except Exception:
        total = 0
    if total <= 0:
        return True, ""
    # 零结果守卫：本轮一个正常文件都没列举到，但收到墓碑——
    # 疑似 changes token 失效/盘被移除，本轮禁止清理。
    if videos_seen < 1 and doomed_count > 0:
        return False, ("本轮一个正常文件都没列举到，但收到 %d 个墓碑——"
                       "疑似 changes token 失效/盘被移除，本轮禁止清理" % doomed_count)
    limit = _prune_threshold(total, prune_max_ratio(db), prune_max_absolute(db))
    if doomed_count >= limit:
        return False, ("本轮待删 %d 个，状态表共 %d 行，删除量达阈值 %d——"
                       "疑似盘被清空/共享被收回，本轮禁止清理" % (doomed_count, total, limit))
    return True, ""


def _apply_tombstone_deletions(doomed: list[tuple[str, str, str]],
                               strm_root: str, stats: dict, db=None) -> None:
    """执行删除：删磁盘 .strm → 删状态行 → stats.tombstoned。

    doomed: [(drive_id, remote_path, strm_path)]。删文件 OSError 容忍，
    记 stats["tombstone_rm_failed"]（状态行照删，下轮不再重试该条）。
    """
    for drive_id, remote_path, strm_path in doomed:
        spath = os.path.join(strm_root, strm_path)
        try:
            if os.path.exists(spath):
                os.remove(spath)
        except OSError as exc:
            logger.warning("strm_gen: 删除过期 .strm 失败 %s: %s", spath, exc)
            stats["tombstone_rm_failed"] = stats.get("tombstone_rm_failed", 0) + 1
        _delete_state_rows([(drive_id, remote_path)], db=db)
        stats["tombstoned"] = stats.get("tombstoned", 0) + 1


def _prune_doomed(db, doomed: list[tuple[str, str, str]], state: dict,
                  strm_root: str, stats: dict, videos_seen: int,
                  reason_kind: str = "tombstone",
                  track_backlog: bool = True) -> None:
    """统一删除流水线：预算检查 → dry_run → 真删。

    reason_kind: "tombstone"（墓碑/orphan_check）或 "stale_scan"（全量 stale 对比），
    只用于日志与 would-delete 报告区分来源。
    预算不通过 → 整批中止，一条都不删（all-or-nothing per run）。
    track_backlog: 是否维护 dry_run 积压时间戳（R10）——只有生成管线设 True，
    orphan 复核路径不碰（它的 would-delete 不进缺集报告，别干扰"持续"口径）。
    注意：R18-P2 起语义收敛——这里只做 touch(True)（出现积压即记起始时间）；
    清零统一由 _run_generation_inner 在轮尾根据本轮 stats 一次性判断，避免
    全量轮里"墓碑路径先 touch(True)、stale 路径空分支又误清"重启 3 天时钟。
    """
    doomed_count = len(doomed)
    if doomed_count == 0:
        return
    ok, reason = _prune_budget_ok(db, doomed_count, videos_seen)
    if not ok:
        stats["tombstone_blocked"] = stats.get("tombstone_blocked", 0) + doomed_count
        stats["prune_blocked"] = {
            "reason": reason,
            "doomed": doomed_count,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        logger.error("strm_gen: 删除保护拦截本轮清理（%s）：%s", reason_kind, reason)
        return
    if prune_dry_run(db):
        stats["prune_would_delete"] = {
            "count": doomed_count,
            "reason_kind": reason_kind,
            "sample": [{"drive_id": did, "remote_path": rp, "strm_path": sp}
                       for did, rp, sp in doomed[:50]],
        }
        if track_backlog:
            _dry_run_backlog_touch(db, True)  # 首次出现积压时记录起始时间（R10）
        logger.info("strm_gen: [演习] 本轮会删除 %d 个 .strm（%s，dry_run，未执行）",
                    doomed_count, reason_kind)
        return
    _apply_tombstone_deletions(doomed, strm_root, stats, db=db)
    for did, rp, _sp in doomed:
        state.pop((did, rp), None)  # 内存状态同步（只有真删才动；dry_run/拦截不碰）
    logger.info("strm_gen: 清理过期条目 %d 个（%s）", doomed_count, reason_kind)


def _normalize_tombstone_drives(db, tombstones: list) -> list:
    """补齐墓碑的 drive_id：为空/"unknown" 的按 file_id 批量反查状态表。

    file_id 在 Drive 全局唯一，反查无歧义。查不到的保留原值（调用方走
    unknown 疑点路径，不删除）。返回新列表（Tombstone 是 frozen dataclass，
    用 dataclasses.replace 产出补齐后的副本）。
    """
    need = [t for t in tombstones if not t.drive_id or t.drive_id == "unknown"]
    if not need:
        return tombstones
    try:
        from dataclasses import replace as _dc_replace
        from backend.emby_server import models as em
        fid_to_did: dict[str, str] = {}
        fids = [t.file_id for t in need]
        for i in range(0, len(fids), 200):
            chunk = fids[i:i + 200]
            for fid, did in db.query(em.StrmGenFile.file_id,
                                     em.StrmGenFile.drive_id).filter(
                    em.StrmGenFile.file_id.in_(chunk)).all():
                if did:
                    fid_to_did[fid] = did
        if not fid_to_did:
            return tombstones
        return [_dc_replace(t, drive_id=fid_to_did[t.file_id])
                if (not t.drive_id or t.drive_id == "unknown")
                and t.file_id in fid_to_did else t
                for t in tombstones]
    except Exception as exc:
        logger.warning("strm_gen: 墓碑 drive_id 反查失败: %s", exc)
        return tombstones


def _consume_tombstones(db, tombstones: list, state: dict,
                        strm_root: str, stats: dict,
                        drive_ids: list[str], videos_seen: int,
                        track_backlog: bool = True,
                        ok_drives: frozenset | None = None) -> None:
    """消费墓碑：删磁盘 .strm + 删状态行。库条目不动（交扫描器下一轮）。

    - 去重：按 (drive_id, file_id)
    - ok_drives（#809）：允许消费其墓碑的 drive_id 集合；None=不做按盘门控
      （调用方已对每个墓碑单独验过 Drive，如 orphan_check 的逐个 files.get
      复核——逐个复核本身就是信任锚，不需要"本轮列举成功"门控）。
      传入集合时，只消费来源盘在本轮列举成功（无 error）的墓碑，失败盘的
      墓碑跳过并计数（tombstone_skipped_drive_failed），不误删。
    - 多盘（两盘全挂）：按墓碑自带的 drive_id 逐盘消费（state 主键为
      (drive_id, remote_path)，跨盘可判定归属——旧的"多盘整批跳过"
      （tombstone_skipped_multi）已删除，生产多盘部署下墓碑删除路径
      不再永久死亡）。墓碑的 drive_id 不在本轮 drive_ids 里：不属于本轮
      管辖范围，记疑点（health unknown）供反向僵尸校验复核，不静默丢弃。
    - 解析：SELECT drive_id, remote_path, strm_path FROM strm_gen_files
      WHERE drive_id = :did AND file_id IN (...)（分片 200，需 file_id 索引）；
      file_id 在 Drive 全局唯一，跨盘归属无歧义，按盘过滤是纵深防御。
    - 查不到 file_id → stats["tombstone_unknown"]++，同时记入 strm_file_health
      （status='unknown'），供反向僵尸校验复核
    - drive_id 归一化（两盘全挂）：drive_id 为空/"unknown" 的墓碑（如 orphan_check
      的 MediaItem 候选），按 file_id 批量反查状态表补齐真实 drive_id
      （file_id 全局唯一）；查不到的保留原值，后续走 unknown 疑点路径。
    """
    if not tombstones:
        return
    tombstones = _normalize_tombstone_drives(db, tombstones)
    consumable = []
    for t in tombstones:
        if ok_drives is not None and t.drive_id not in ok_drives:
            stats["tombstone_skipped_drive_failed"] = (
                stats.get("tombstone_skipped_drive_failed", 0) + 1)
            continue
        consumable.append(t)
    if not consumable:
        if tombstones:
            logger.info("strm_gen: %d 个墓碑因来源盘本轮列举失败被跳过（下轮重放）",
                        len(tombstones))
        return
    # 去重：按 (drive_id, file_id)
    uniq: dict[tuple[str, str], Any] = {}
    for t in consumable:
        uniq[(t.drive_id, t.file_id)] = t
    tombstones = list(uniq.values())
    stats["tombstone_pending"] = stats.get("tombstone_pending", 0) + len(tombstones)

    # 墓碑按盘分组；不在本轮 drive_ids 的盘不归本轮管辖
    in_scope: list = []
    for t in tombstones:
        if t.drive_id not in drive_ids:
            stats["tombstone_unknown"] = stats.get("tombstone_unknown", 0) + 1
            _record_health_unknown(db, t.file_id, t.drive_id,
                                   note="tombstone %s 的盘不在本轮范围" % t.reason)
            continue
        in_scope.append(t)

    # file_id IN 回查状态表（分片 200，按盘过滤）
    fid_to_entry: dict[tuple[str, str], tuple[str, str, str]] = {}
    try:
        from backend.emby_server import models as em
        by_drive: dict[str, list] = {}
        for t in in_scope:
            by_drive.setdefault(t.drive_id, []).append(t)
        for did, dts in by_drive.items():
            fids = [t.file_id for t in dts]
            for i in range(0, len(fids), 200):
                chunk = fids[i:i + 200]
                rows = db.query(em.StrmGenFile.remote_path,
                                em.StrmGenFile.strm_path,
                                em.StrmGenFile.file_id).filter(
                    em.StrmGenFile.drive_id == did,
                    em.StrmGenFile.file_id.in_(chunk)).all()
                for rp, sp, fid in rows:
                    fid_to_entry[(did, fid)] = (did, rp, sp)
    except Exception as exc:
        logger.warning("strm_gen: 墓碑回查状态表失败: %s", exc)
        return

    doomed: list[tuple[str, str, str]] = []
    doomed_tombstones: list = []  # 与 doomed 逐项对应（带 file_id/drive_id）
    for t in in_scope:
        hit = fid_to_entry.get((t.drive_id, t.file_id))
        if not hit:
            # state 里没有：可能 state 已清 / 不在源子树，不管；记疑点供复核
            stats["tombstone_unknown"] = stats.get("tombstone_unknown", 0) + 1
            _record_health_unknown(db, t.file_id, t.drive_id,
                                   note="tombstone %s 查不到状态行" % t.reason)
            continue
        doomed.append(hit)
        doomed_tombstones.append(t)
    blocked_before = stats.get("prune_blocked")
    _prune_doomed(db, doomed, state, strm_root, stats, videos_seen,
                  reason_kind="tombstone", track_backlog=track_backlog)
    if doomed_tombstones and stats.get("prune_blocked") is not blocked_before:
        # 预算/零结果守卫整批拦截：本批墓碑改记 health（unknown），转有界
        # orphan check 逐个对 Drive 复核——拦截后 tombstone 不能丢弃，
        # 否则 checkpoint 照常推进、下轮 changes 不再重发，.strm 永久残留。
        for t in doomed_tombstones:
            _record_health_unknown(db, t.file_id, t.drive_id,
                                   note="tombstone %s 被 prune 守卫拦截，转 orphan check 复核" % t.reason)
        stats["tombstone_blocked_deferred"] = (
            stats.get("tombstone_blocked_deferred", 0) + len(doomed_tombstones))


def _record_health_unknown(db, file_id: str, drive_id: str, note: str = "") -> None:
    """把疑点 file_id 记入 strm_file_health（status='unknown'），供反向校验复核。"""
    if not file_id:
        return
    try:
        from backend.emby_server import models as em
        row = db.query(em.StrmFileHealth).filter(
            em.StrmFileHealth.file_id == file_id).first()
        if row is None:
            row = em.StrmFileHealth(file_id=file_id, drive_id=drive_id or "",
                                    status="unknown", note=note[:512])
            db.add(row)
        elif row.status in ("unknown", "error"):
            row.drive_id = drive_id or row.drive_id
            row.note = note[:512]
        db.flush()
    except Exception as exc:
        logger.warning("strm_gen: 记录疑点 file_id 失败: %s", exc)


def _drive_file_id_valid(fid: str) -> bool:
    """生成前验证新增 file_id 是否有效（files.get，fields=id）。

    失败一律返回 False（调用方跳过 + 计数，不重试）。
    拿不到 SA token 时返回 True（宁可放行，不可误杀）。
    反风控：只验增量新增文件（通常 <100/轮），走现有 _get_token/_api_get
    （SA 轮换/401 刷新复用，不另起第二套）。
    """
    try:
        from backend.emby_server import drive_changes as dc
        token = dc._get_token()
        if not token:
            return True
        dc._api_get("https://www.googleapis.com/drive/v3/files/%s" % fid, token,
                    {"fields": "id", "supportsAllDrives": "true"})
        return True
    except Exception:
        return False


#: Drive v3 batch 端点：一次 HTTP 验一批 file_id（multipart/mixed）
DRIVE_BATCH_URL = "https://www.googleapis.com/batch/drive/v3"
#: 单批子请求数（保守值：出错时整批回退逐个的代价可控）
DRIVE_BATCH_SIZE = 50


def _drive_batch_check(fids: list[str], token: str) -> set[str]:
    """一次 batch 调用验一批 file_id，返回无效 fid 集合。

    每个 part 为 `GET /drive/v3/files/<fid>?fields=id&supportsAllDrives=true`，
    用 Content-ID: item-<序号> 把子响应映射回 fid。任一子 part 非 200
    （404/403 等）→ 该 fid 判无效，与 _drive_file_id_valid"失败一律 False"
    语义一致。HTTP 层整体异常（非 200、网络错误）直接抛，由调用方回退逐个。
    """
    boundary = "batch_strm_%s" % uuid.uuid4().hex
    lines: list[str] = []
    for i, fid in enumerate(fids):
        lines.append("--" + boundary)
        lines.append("Content-Type: application/http")
        lines.append("Content-Transfer-Encoding: binary")
        lines.append("Content-ID: item-%d" % i)
        lines.append("")
        lines.append("GET /drive/v3/files/%s?fields=id&supportsAllDrives=true" % fid)
        lines.append("Authorization: Bearer %s" % token)
        lines.append("")
    lines.append("--" + boundary + "--")
    body = "\r\n".join(lines).encode("utf-8")
    resp = httpx.post(
        DRIVE_BATCH_URL, content=body, timeout=60,
        headers={"Content-Type": "multipart/mixed; boundary=%s" % boundary},
    )
    if resp.status_code != 200:
        raise RuntimeError("batch 整体返回 %d" % resp.status_code)
    return _parse_drive_batch_response(resp.text, fids)


#: batch 子响应解析：Content-ID: item-<序号> + HTTP 状态行
_BATCH_PART_ID_RE = re.compile(r"Content-ID:\s*item-(\d+)", re.IGNORECASE)
_BATCH_STATUS_RE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})", re.MULTILINE)


def _parse_drive_batch_response(text: str, fids: list[str]) -> set[str]:
    """解析 Drive batch multipart 响应，返回无效 fid 集合（纯函数，可单测）。

    规则：子 part 缺失（Content-ID 对不上）或状态码非 200 → 该 fid 无效。
    """
    statuses: dict[int, int] = {}
    for part in text.split("--"):
        m_id = _BATCH_PART_ID_RE.search(part)
        if not m_id:
            continue
        m_st = _BATCH_STATUS_RE.search(part)
        statuses[int(m_id.group(1))] = int(m_st.group(1)) if m_st else 0
    return {fid for i, fid in enumerate(fids) if statuses.get(i) != 200}


def _drive_file_ids_invalid_batch(fids: list[str]) -> set[str]:
    """批量校验 file_id 有效性，返回无效 fid 集合。

    反风控铁律（与 _drive_file_id_valid 同款，不另起第二套）：
    - token 复用 drive_changes._get_token()（同一套 SA 轮换）
    - 发送前按子请求数 acquire 全局限速器（保守，不占便宜）
    - 任一子 part 非 200 → 该 fid 无效（"失败一律无效"语义一致）
    - batch 整体异常（网络/401 等）→ 回退逐个 _drive_file_id_valid（只慢不坏）
    - 拿不到 token → 返回空集（"宁可放行"语义一致）
    """
    uniq = list(dict.fromkeys(f for f in fids if f))
    if not uniq:
        return set()
    from backend.emby_server import drive_changes as dc
    try:
        token = dc._get_token()
    except Exception:
        token = None
    if not token:
        return set()
    invalid: set[str] = set()
    for start in range(0, len(uniq), DRIVE_BATCH_SIZE):
        chunk = uniq[start:start + DRIVE_BATCH_SIZE]
        try:
            # 保守：子请求数个令牌，一个不少（batch 只是一次 HTTP，
            # 但配额按子请求计，不占便宜）
            for _ in chunk:
                dc._api_limiter.acquire()
            invalid |= _drive_batch_check(chunk, token)
        except Exception as exc:
            logger.warning("strm_gen: 批量校验失败，回退逐个校验（%d 个）: %s",
                           len(chunk), exc)
            invalid |= {fid for fid in chunk if not _drive_file_id_valid(fid)}
    return invalid


def _clear_gone_marks(file_ids: list[str]) -> None:
    """清除本轮实际写入 .strm 的 file_id 的 source_invalid_at 标记。

    文件恢复/重新分享后，下次增量即重建 .strm → 标记自动清除 → 播放恢复。闭环。
    播放成功不清标记（热路径不写库，纪律）。
    分片 200，随 _save_state_rows 同一批次触发，零新增往返（独立短会话）。
    """
    uniq = sorted({f for f in file_ids if f and not f.startswith("local:")})
    if not uniq:
        return
    try:
        from sqlalchemy import bindparam, text
        from backend.database import SessionLocal
        sess = SessionLocal()
        try:
            stmt = text(
                "UPDATE emby_items SET source_invalid_at = NULL "
                "WHERE drive_file_id IN :fids AND source_invalid_at IS NOT NULL"
            ).bindparams(bindparam("fids", expanding=True))
            for i in range(0, len(uniq), 200):
                sess.execute(stmt, {"fids": uniq[i:i + 200]})
            sess.commit()
        except Exception as exc:
            sess.rollback()
            logger.warning("strm_gen: 清除 gone 标记失败: %s", exc)
        finally:
            sess.close()
    except Exception as exc:
        # best-effort：清理失败不影响生成主流程
        logger.warning("strm_gen: 清除 gone 标记跳过: %s", exc)


def _write_strm_file(full_path: str, url: str) -> bool:
    """原子写入 .strm 文件。"""
    tmp = None
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        # tmp 文件名带 pid+线程 id：签名刷新线程（strm-sig-refresh）也可能并发写
        # 同一文件，确定性的 "<file>.tmp" 会让双方交错写入同一个 tmp 造成撕裂文件。
        tmp = f"{full_path}.tmp.{os.getpid()}.{threading.get_ident():x}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(url + "\n")
        os.replace(tmp, full_path)
        return True
    except OSError as exc:
        logger.warning("strm_gen: 写入失败 %s: %s", full_path, exc)
        return False
    finally:
        # best-effort 清理自己的 tmp（replace 成功后文件已不存在，忽略报错）
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _resolve_collision(desired: str, used: set[str]) -> str:
    if desired not in used:
        return desired
    base, ext = os.path.splitext(desired)
    i = 2
    while f"{base}-{i}{ext}" in used:
        i += 1
    return f"{base}-{i}{ext}"


def _reset_progress():
    """重置一次运行的进度计数（调用方已持有运行权）。"""
    with _lock:
        _progress.update({
            "phase": "listing",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None, "total": 0, "done": 0,
            "generated": 0, "skipped": 0, "failed": 0, "pruned": 0,
            "errors": [], "current": "",
            # B1：直接调 run_owned（管理后台手动触发）不经过 try_acquire，
            # 这里也刷新获取时刻与 token，保证僵死判定口径一致。
            "acquired_at": time.monotonic(),
            "lock_token": uuid.uuid4().hex,
        })


def _lock_stale(db=None) -> bool:
    """运行锁是否处于僵死状态（持有中且超过超时阈值）。

    供"只看不抢"的调用方（如调度循环）用：判断要不要继续等。
    超时阈值走 lock_timeout_minutes；0=关闭自愈时永远返回 False。
    调用方必须在判断后经 try_acquire 原子抢占，不可自行置位。
    """
    timeout_min = lock_timeout_minutes(db)
    if timeout_min <= 0:
        return False
    with _lock:
        if not _progress["running"]:
            return False
        acquired = _progress.get("acquired_at")
        if acquired is None:
            # 老版本/异常路径置 running 但没记时间：按不僵死处理，
            # 等持有者自己释放，避免误杀正在跑的轮次。
            return False
        return (time.monotonic() - acquired) > timeout_min * 60


def try_acquire(db=None) -> bool:
    """原子尝试获取运行权：成功返回 True（已置 running），失败返回 False。

    给管理后台手动触发用——原来是"查 running → 起线程 → 线程里再置 running"，
    锁在起线程前就释放了，快速连点两次都会返回"已开始"，第二次被静默吞掉。
    现在获取与置位在同一把锁内完成，无竞态。

    B1 锁超时自愈：锁被持有超过 strm_gen_lock_timeout_minutes（默认 180 分钟，
    0=关闭）时视为僵死（持有线程崩溃/卡死但内存锁没释放），新轮次可抢占并记
    warning。抢占是原子的：并发两个抢占者只有一个能成功。
    """
    timeout_min = lock_timeout_minutes(db)
    with _lock:
        if _progress["running"]:
            if timeout_min > 0:
                acquired = _progress.get("acquired_at")
                if (acquired is not None
                        and (time.monotonic() - acquired) > timeout_min * 60):
                    held_min = (time.monotonic() - acquired) / 60
                    logger.warning(
                        "strm_gen: 运行锁疑似僵死（已持有 %.1f 分钟，超阈值 %d 分钟），"
                        "新轮次抢占", held_min, timeout_min)
                    _progress["running"] = True
                    _progress["acquired_at"] = time.monotonic()
                    _progress["lock_token"] = uuid.uuid4().hex
                    return True
            return False
        _progress["running"] = True
        _progress["acquired_at"] = time.monotonic()
        _progress["lock_token"] = uuid.uuid4().hex
        return True


def _release_progress(stats: dict, db, expect_token=None):
    """释放运行权：phase 复位 + 落盘上次执行统计。

    expect_token：调用方获取运行权时的 lock_token。B1 抢占场景下，被抢占的
    旧持有者结束时 token 已对不上——此时只落盘统计，不清除新持有者的 running
    （否则新一轮会被误判为空闲，导致两轮并发）。expect_token=None 时保持旧
    行为（无条件释放，供测试/兼容调用）。
    """
    with _lock:
        # 非预期异常时 phase 可能卡在 generating：统一收敛到 error，
        # 前端不会一直显示"生成中"但实际已停。
        if _progress.get("phase") not in ("done", "idle"):
            _progress["phase"] = "error"
        if expect_token is not None and _progress.get("lock_token") != expect_token:
            logger.warning("strm_gen: 释放运行权时发现锁已被抢占（token 对不上），"
                           "不清除新持有者的 running，只落盘本轮统计")
        else:
            _progress["running"] = False
            _progress["finished_at"] = datetime.now(timezone.utc).isoformat()
            _progress["acquired_at"] = None  # B1：释放运行锁时清掉获取时刻
            _progress["lock_token"] = None
    try:
        from backend.integrations import store
        # 注意：store 只有 write_values（批量写），没有 set_value；
        # 且 write_values 不自动提交，提交时机由调用方决定。
        store.write_values(db, {
            CONFIG_LAST_RUN: datetime.now(timezone.utc).isoformat(),
            CONFIG_LAST_STATS: json.dumps(stats, ensure_ascii=False),
        }, {
            CONFIG_LAST_RUN: ".strm 生成器上次执行完成时间",
            CONFIG_LAST_STATS: ".strm 生成器上次执行统计",
        })
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def run_generation(db, full: bool = False, drive_id: Optional[str] = None) -> dict[str, Any]:
    """执行一次生成。full=True 时忽略增量状态全量重建（仍以 file_id 去重）。

    drive_id=None/""/"all" 时遍历所有发现的盘（含个人盘）；指定则只跑那一个盘。
    同一时间只允许一个任务运行（原子获取，见 try_acquire）。
    返回统计 dict。
    """
    if not try_acquire(db):
        return {"ok": False, "error": "已有生成任务在运行中"}
    return run_owned(db, full=full, drive_id=drive_id)


def run_owned(db, full: bool = False, drive_id: Optional[str] = None) -> dict[str, Any]:
    """已持有运行权时执行一次生成（try_acquire 成功后调用）。"""
    _reset_progress()
    with _lock:
        _my_token = _progress.get("lock_token")
    stats = {"generated": 0, "skipped": 0, "failed": 0, "pruned": 0, "total": 0}
    try:
        return _run_generation_inner(db, full, drive_id, stats)
    except Exception as exc:
        # 修 P3-3：旧实现非预期异常时 running=False 但 phase 停在 "generating"，
        # 前端会一直显示"生成中"。这里显式置为 error。
        _set_progress(phase="error")
        _add_error(f"生成异常: {exc}")
        logger.exception("strm_gen: 生成异常")
        return {"ok": False, "error": str(exc)}
    finally:
        _release_progress(stats, db, expect_token=_my_token)


def _run_generation_inner(db, full: bool, drive_id: Optional[str], stats: dict) -> dict[str, Any]:
    # 多盘多目录（2026-10-13 两盘全挂）：源目录支持逗号分隔多目录；
    # all_videos 统一为 (drive_id, rel, file_id, size) 四元组，rel 为盘根相对路径。
    src_prefixes = source_dirs(db)
    src_prefix = src_prefixes[0] if src_prefixes else ""  # 主目录：日志/兼容旧语义用
    strm_root = _container_strm_root(db)

    # 确定数据源：通用增量接口（SourceLister），所有挂载方式都能用。
    # drive: 用 Changes API 做增量（只拉变化），全量回退到 files.list
    # local: 用 (mtime_ns, size) 指纹做增量检测
    stype = source_type(db)
    all_videos: list[tuple[str, str, str, int]] = []  # (drive_id, rel, file_id, size)
    all_tombstones: list = []  # 本轮各 lister 产出的删除信号（块1）
    listers: list = []  # 成功列举的检查点：本地模式存 Lister 对象，drive 并行模式存 (did, new_token)

    if stype == "local":
        root = local_root(db)
        if not os.path.isdir(root):
            _set_progress(phase="error")
            _add_error(f"本地源目录不存在: {root}")
            return {"ok": False, "error": f"本地源目录不存在: {root}"}
        from backend.emby_server import strm_source
        lister = strm_source.LocalFsLister(root)
        listers.append(lister)
        drive_ids = [lister.drive_key]
        logger.info("strm_gen: 开始生成 local=%s full=%s", root, full)
        _set_progress(phase="listing", current=f"正在列举本地文件…（{root}）")
        videos: list[tuple[str, str, int]] = []
        tombstones: list = []
        try:
            # 增量模式走指纹比对；全量模式（full=True）走全量 walk
            raw_items = list(lister.iter_videos(incremental=not full))
            videos, tombstones = _split_lister_output(raw_items)
            all_tombstones.extend(tombstones)
        except Exception as exc:
            _set_progress(phase="error")
            _add_error(f"列举失败 [{root}]: {exc}")
            return {"ok": False, "error": str(exc)}
        logger.info("strm_gen: local=%s 列举到 %d 个视频文件（墓碑 %d）",
                    root, len(videos), len(tombstones))
        _lid = lister.drive_key
        all_videos.extend((_lid, rel, fid, size) for rel, fid, size in videos)
        # 本地模式下 rel 已是相对 root 的路径，src_prefix 为空
        src_prefix = ""
        src_prefixes = [""]
    else:
        # drive 模式：确定要跑的盘（显式参数 > 配置 > 遍历所有发现的盘）。
        # 配置为 "all"（DRIVE_ID_ALL）等价于留空：显式声明"全盘"，意图可观测。
        cfg_drive = (drive_id or drive_id_config(db) or "").strip()
        pinned = bool(cfg_drive) and cfg_drive.lower() != DRIVE_ID_ALL
        if pinned:
            drive_ids = [cfg_drive]
        else:
            drives = _discover_drives()
            if not drives:
                _set_progress(phase="error")
                _add_error("未发现可用 Drive")
                return {"ok": False, "error": "未发现可用 Drive"}
            drive_ids = sorted(drives.keys())
            if len(drive_ids) > 1:
                logger.info("strm_gen: 发现 %d 个盘，并行生成: %s",
                            len(drive_ids), drive_ids)
        from backend.emby_server import strm_source
        from backend.emby_server import drive_changes as dc

        # 全局 API 速率按配置调整（热读，默认 8 QPS 保守值）
        dc.set_api_qps(api_qps(db))
        # 并发数按配置（默认 3，钳制 [1, 10]）
        concurrency = drive_concurrency(db)

        # 自动发现模式（未显式 pin 单个 drive_id）时，源目录不存在的盘直接跳过
        # 而非报错——不是每个盘都有媒体目录。
        auto_discover = not pinned

        def _list_one_drive(did: str) -> dict:
            """单个 drive 的列举 worker（线程池内执行）。

            返回 {"did":..., "videos":[...], "tombstones":[...], "retries":N,
            "new_token":..., "error":None/str}。
            异常不向外抛，记进 error 字段由主线程统一处理——某盘失败不影响
            其他盘的列举结果收集（失败的盘被跳过并告警）。

            增量检查点（changes page token）也在这里带回：主线程在整轮成功后
            统一保存（失败时不保存，下轮用旧 token 重放）。
            """
            dc.reset_total_retry_count()
            lister = strm_source.DriveChangesLister(
                did, src_prefixes, strict_dir=not auto_discover,
                # S1：子树限定列举开关（热读，默认开；关掉回退扁平全量拉取）
                subtree_listing=subtree_listing_enabled(db))
            videos: list[tuple[str, str, int]] = []
            tombstones: list = []
            try:
                # 增量模式走 Changes API；全量模式（full=True）回退到 files.list
                # （2026-10-12 起 list_changes 不再吞错：API 失败直接抛，
                # 这里捕获后记 error，绝不静默当成 0 文件）
                # 注意：iter_videos 可能 yield Tombstone（删除信号），不能直接
                # 解包，必须先经 _split_lister_output 拆分
                raw_items = list(lister.iter_videos(incremental=not full))
                videos, tombstones = _split_lister_output(raw_items)
            except Exception as exc:
                return {
                    "did": did,
                    "videos": [],
                    "tombstones": [],
                    "retries": dc.get_total_retry_count(),
                    "path_skipped": lister.path_resolve_skipped,
                    "new_token": None,
                    "incremental_listing": False,
                    "error": str(exc)[:300],
                }
            return {
                "did": did,
                "videos": videos,
                "tombstones": tombstones,
                "retries": dc.get_total_retry_count(),
                "path_skipped": lister.path_resolve_skipped,
                "new_token": lister._new_token,
                "error": None,
                # A3：本轮是否走了 Changes 增量（供免验决策）
                "incremental_listing": bool(lister.used_incremental_path),
            }

        import concurrent.futures as _futures
        _set_progress(phase="listing",
                      current=f"正在列举 Drive 文件…（{len(drive_ids)} 个盘并行）")
        drive_results: list[dict] = []
        with _futures.ThreadPoolExecutor(max_workers=concurrency,
                                         thread_name_prefix="strm-list") as _pool:
            # 线程安全说明：
            # - 每个 worker 用独立的 lister + videos 列表，无共享可变状态
            # - DriveChangesLister._db() 每次新建 SessionLocal，线程安全
            # - _set_progress 走 _lock；dc 的重试计数器/速率限制器都是线程本地/加锁的
            future_map = {_pool.submit(_list_one_drive, did): did for did in drive_ids}
            for fut in _futures.as_completed(future_map):
                did = future_map[fut]
                try:
                    res = fut.result()
                except Exception as exc:  # 防御性：worker 本身不应抛，但兜底
                    res = {"did": did, "videos": [], "retries": 0,
                           "path_skipped": 0, "new_token": None,
                           "error": f"列举线程异常: {exc}"[:300]}
                drive_results.append(res)

        # 按 drive_ids 原顺序整理结果（日志顺序稳定）
        drive_results.sort(key=lambda r: drive_ids.index(r["did"]))
        failed = [r for r in drive_results if r["error"]]
        for r in drive_results:
            did = r["did"]
            if r["error"]:
                logger.error("strm_gen: drive=%s 列举失败（重试 %d 次）: %s",
                             did, r["retries"], r["error"])
            else:
                # 成功但 0 文件：明确打 warning，与"报错"区分开
                if not r["videos"]:
                    logger.warning("strm_gen: drive=%s 列举成功但 0 个视频文件"
                                   "（源目录 %r 可能为空；重试 %d 次）",
                                   did, src_prefix, r["retries"])
                else:
                    logger.info("strm_gen: drive=%s 列举到 %d 个视频文件（墓碑 %d）"
                                "（API 重试 %d 次，路径解析跳过 %d）",
                                did, len(r["videos"]), len(r.get("tombstones", [])),
                                r["retries"], r["path_skipped"])
            stats.setdefault("sources", {})[did] = {
                "listed": len(r["videos"]),
                "tombstones": len(r.get("tombstones", [])),
                "retries": r["retries"],
                "path_skipped": r["path_skipped"],
                "error": r["error"],
            }
            # 多盘：给视频打上 drive_id 标签（四元组），下游状态键为 (drive_id, rel)。
            # 同一盘内去重（源目录重叠配置时同一文件可能被两个前缀同时命中）。
            seen_v: set[tuple[str, str]] = set()
            for rel, fid, size in r["videos"]:
                key = (rel, fid)
                if key in seen_v:
                    continue
                seen_v.add(key)
                all_videos.append((r["did"], rel, fid, size))
            # 块1：收集各盘的墓碑（删除信号），失败的盘没有墓碑
            all_tombstones.extend(r.get("tombstones", []))
            # 收集增量检查点（成功时才有 new_token）
            if r["new_token"]:
                listers.append((did, r["new_token"]))
        if failed:
            # 单个来源失败 → 跳过该盘、记录并告警，其他盘继续生成。
            # 只有当所有来源都失败时整轮才标记失败（之前是任一失败就整轮失败，
            # 导致一个配错的盘拖死整个生成器）。
            succeeded = [r for r in drive_results if not r["error"]]
            for f in failed:
                logger.warning("strm_gen: drive=%s 列举失败，已跳过该盘: %s",
                               f["did"], f["error"])
                _add_error(f"列举失败 [{f['did']}]: {f['error']}（已跳过该盘）")
            _record_source_failures(db, failed)
            if not succeeded:
                first = failed[0]
                _set_progress(phase="error")
                return {"ok": False,
                        "error": f"列举失败 [{first['did']}]: {first['error']}"}

    # 增量状态与已用路径在各数据源之间共享（_resolve_collision 跨盘防撞车）
    # loaded_state 保留一份：全量模式 state 会被重置（重建），但 prune 的
    # stale 对比需要旧状态做基准（R2：全量 stale 对比路径保留，单盘）
    loaded_state = _load_state()
    state = {} if full else loaded_state
    if not full:
        # A1：增量模式下，从磁盘 .strm 内容反推补全状态表缺行（零 API 调用）。
        # 放在 used 计算之前：补上的行一并进入 used，主循环走已有 skip 路径。
        # 全量模式故意重置状态，不补。
        _backfill_state_from_disk(all_videos, state, strm_root)
    used: set[str] = set()
    if full:
        # 全量模式：从磁盘加载已有的 .strm 路径，避免不同源文件撞车时静默覆盖
        for _root, _dirs, _files in os.walk(strm_root):
            for _f in _files:
                if _f.endswith(".strm"):
                    _full = os.path.join(_root, _f)
                    used.add(os.path.relpath(_full, strm_root))
    else:
        used = {spath for (_fid, _size, spath) in state.values()}

    # 2. 增量比对 + 生成
    _set_progress(phase="generating", total=len(all_videos))
    pending_rows: list[tuple[str, str, str, int, str]] = []  # (did, rel, fid, size, spath)
    # 块4 §6.3：仅增量中新文件（state 里没有）写盘前验一次 file_id。
    # 全量跳过（数万个逐一 files.get 是配额浪费）；本地源无 Drive id，跳过。
    verify_new = (stype != "local") and (not full) and verify_new_ids(db)
    if verify_new:
        # A3：增量文件免验——Changes API 返回的本来就是 Google 刚确认的变化，
        # 再逐个调 API 验"它存不存在"纯属重复。只有任一成功列举的盘走了
        # 全量回退（检查点丢失/首次建基线）时才保留校验；保守起见多盘不做
        # 按盘精细拆分。local 源 verify_new 本来就是 False，不受影响。
        # （drive_results 只在 drive 分支定义；verify_new 为 True 必然在 drive 模式）
        _succeeded = [r for r in drive_results if not r["error"]]
        if _succeeded and all(r.get("incremental_listing") for r in _succeeded):
            logger.info("strm_gen: 本轮 %d 个盘全部走 Changes 增量，"
                        "新增文件免 file_id 校验", len(_succeeded))
            verify_new = False
        else:
            _fell_back = [r["did"] for r in _succeeded
                          if not r.get("incremental_listing")]
            logger.warning("strm_gen: 本轮有盘走全量回退（%s），"
                           "保留新增 file_id 校验", _fell_back)
    # A2：校验改批量——循环前一次 batch 拿到无效集，循环里只查集合。
    # 无新增或全有效时 verify_new 置 False，循环里零校验开销。
    invalid_fids: set[str] = set()
    if verify_new:
        _new_fids = list(dict.fromkeys(
            fid for did, rel, fid, size in all_videos if (did, rel) not in state))
        if _new_fids:
            invalid_fids = _drive_file_ids_invalid_batch(_new_fids)
            logger.info("strm_gen: 批量校验 %d 个新增 file_id，无效 %d 个",
                        len(_new_fids), len(invalid_fids))
        verify_new = bool(invalid_fids)
    # 本轮实际写入 .strm 的 file_id：生成完清除其 source_invalid_at 标记（块3）
    written_file_ids: list[str] = []
    done = get_progress()["done"]
    for did, rel, fid, size in all_videos:
        done += 1
        _set_progress(done=done, current=f"[{did}] {rel}")
        old = state.get((did, rel))
        # file_id 或 size 任一变化都重建（Drive 上替换文件换 file_id，原地覆盖只变 size；
        # 原来只比 file_id，size 字段存而不用）
        if old and old[0] == fid and old[1] == size and not full:
            spath = os.path.join(strm_root, old[2])
            if os.path.exists(spath):
                stats["skipped"] += 1
                _set_progress(skipped=stats["skipped"])
                continue
            strm_rel = old[2]  # .strm 被删了，按原路径重建
        else:
            # 多版本保留（用户硬要求）：同一集的不同版本（不同 rel）各自生成独立 .strm，
            # 输出路径由 rel 派生天然区分；真撞名才加 -2 后缀，版本间永不互相淘汰。
            matched = next((p for p in src_prefixes if p and rel.startswith(p)), "")
            strm_rel = _resolve_collision(strm_relpath(rel, matched), used)

        if verify_new and old is None and fid in invalid_fids:
            # 批量校验已判无效：跳过 + 计数，不重试（下轮增量会重新发现）
            stats["failed_verify"] = stats.get("failed_verify", 0) + 1
            _add_error(f"file_id 校验失败，跳过: {rel}")
            continue

        url = drive_url(fid)
        full_path = os.path.join(strm_root, strm_rel)
        if _write_strm_file(full_path, url):
            used.add(strm_rel)
            pending_rows.append((did, rel, fid, size, strm_rel))
            state[(did, rel)] = (fid, size, strm_rel)
            written_file_ids.append(fid)
            stats["generated"] += 1
        else:
            stats["failed"] += 1
            _add_error(f"写入失败: {strm_rel}")
        _set_progress(generated=stats["generated"], failed=stats["failed"])

        # 每 500 条落盘一次，避免中途崩溃全丢
        if len(pending_rows) >= 500:
            _save_state_rows(pending_rows)
            pending_rows = []
    if pending_rows:
        _save_state_rows(pending_rows)

    stats["total"] = len(all_videos)
    _set_progress(total=len(all_videos))

    # 块3 清除路径：本轮实际写入 .strm 的 file_id，其 source_invalid_at 标记
    # 自动清除（文件恢复/重新分享 → 标记清除 → 播放恢复，闭环）
    if written_file_ids:
        _clear_gone_marks(written_file_ids)

    # 3. 完整性校验：按顶层目录统计 Drive vs .strm（校验磁盘真实存在，
    # 原来只比"Drive 枚举 vs DB"，DB 有行但文件被删会误报完整）
    # S2（2026-10-13）：全增量轮次跳过 8.6 万次磁盘 stat 的全量校验，
    # 只对本轮 touch 过的目录做轻量校验（delta 闭环 + 受影响目录的僵尸扫描）。
    # 判定口径与 A3 免检一致：全部成功列举的盘都走了 Changes 增量路径。
    _set_progress(phase="verifying", current="正在校验完整性…")
    if _round_is_fully_incremental(stype, full,
                                   drive_results if stype != "local" else []):
        verify = _verify_completeness_light(all_videos, state, src_prefixes,
                                            strm_root=strm_root)
    else:
        verify = _verify_completeness(all_videos, state, src_prefixes,
                                      strm_root=strm_root)
    stats["verify"] = verify

    # 4. 墓碑消费：Drive/本地源删文件 → 删 .strm + 删状态行（块1+块2）。
    # 库条目不动：下一轮扫描器按已有删除保护自然处理（不造第二套删除语义）。
    # 按盘成功门控（ok_drives，#809）：只消费本轮列举成功的盘的墓碑——file_id 全局
    # 唯一，跨盘归属无歧义；失败盘的墓碑跳过（下轮 changes token 重放）。
    # 预算、dry_run 都在 _consume_tombstones/_prune_doomed 里。
    if prune_enabled(db):
        if stype == "local":
            _ok_drives = frozenset(drive_ids)  # 本地分支能走到这里即列举成功
        else:
            _ok_drives = frozenset(r["did"] for r in drive_results
                                   if not r.get("error"))
        _consume_tombstones(db, all_tombstones, state, strm_root, stats,
                            drive_ids, len(all_videos), ok_drives=_ok_drives)
        # 全量 stale 对比保留（单盘）：旧状态有但本轮全量列举没见到。
        # 增量不跑这条：增量列举只有 delta，拿 delta 做全量对比会误杀；
        # 增量的删除信号走墓碑。
        if full and len(drive_ids) == 1:
            seen = {(did, rel) for did, rel, _fid, _size in all_videos}
            stale = [(did, rel, loaded_state[(did, rel)][2])
                     for (did, rel) in loaded_state if (did, rel) not in seen]
            _prune_doomed(db, stale, state, strm_root, stats, len(all_videos),
                          reason_kind="stale_scan")
        # 兼容旧统计口径：pruned = 本轮实际删除数
        stats["pruned"] = stats.get("tombstoned", 0)

        # R18-P2：dry_run 积压时间戳清零收敛到轮级别 —— 本轮没有任何
        # would-delete（墓碑路径与 stale 路径都没产生）才清掉起始时间。
        # 旧实现在 _prune_doomed 按"调用"清零：全量轮里墓碑路径 touch(True)
        # 后、stale 路径走空分支被误清，R10 黄条提醒的 3 天时钟被错误重启。
        # 真删/干关（prune_would_delete 未写入）同样落到这里清掉旧时间戳，
        # 顺带消掉 CONFIG_DRY_RUN_BACKLOG_SINCE 残留脏数据（R18 后端 P3）。
        _would = stats.get("prune_would_delete") or {}
        if not (_would.get("count") or 0):
            _dry_run_backlog_touch(db, False)

    _set_progress(phase="done", current="")
    logger.info("strm_gen: 完成 %s", stats)

    # 持久化增量检查点（Changes API 的 page token / 本地指纹库）
    # 只有成功时才保存，失败时保留旧检查点下次重试
    # listers 里有两种：本地模式的 Lister 对象（调 save_checkpoint()），
    # drive 并行模式的 (did, new_token) 元组（直接写 token）
    for item in listers:
        try:
            if isinstance(item, tuple):
                did, new_token = item
                if new_token:
                    from backend.emby_server import strm_source as _ss
                    _lister = _ss.DriveChangesLister(did, src_prefixes)
                    _lister._new_token = new_token
                    _lister.save_checkpoint()
            else:
                item.save_checkpoint()
        except Exception as exc:
            logger.warning("strm_gen: 保存增量检查点失败: %s", exc)

    # 打通扫库链路：生成完自动触发 /strm 相关库的增量扫描
    # （之前生成和扫库是脱节的，中间有断档，新文件要等扫描器下一轮）
    if stats.get("generated", 0) > 0:
        _trigger_strm_scan(db)

    return {"ok": True, "stats": stats}


def _trigger_strm_scan(db) -> None:
    """生成完自动触发 /strm 相关库的增量扫描。

    用 scan_queue_redis.push_scan_request 入队，复用 Redis 去重机制：
    - 已在队列中的库不会重复入队
    - 不会和用户手动扫描冲突（扫描器有库级互斥 _acquire_scan）
    失败只记 warning（扫库是 best-effort，不能影响生成主流程）。
    2026-10-13 起：失败同时写入 strm_gen_scan_trigger_failures，供
    system_alerts 的 strm_scan_trigger_failed 事件告警——不再静默。
    """
    try:
        from backend.emby_server import models as em
        from backend.emby_server import scan_queue_redis
    except Exception as exc:
        logger.warning("strm_gen: 扫库触发器加载失败: %s", exc)
        _record_scan_trigger_failures(db, [("-", "触发器加载失败: %s" % exc)])
        return

    strm_root = _container_strm_root(db)
    # 归一化：/strm 结尾不带斜杠，用于前缀匹配
    strm_prefix = strm_root.rstrip("/")
    try:
        libs = db.query(em.Library).filter(em.Library.is_enabled == True).all()  # noqa: E712
    except Exception as exc:
        logger.warning("strm_gen: 查询媒体库失败: %s", exc)
        _record_scan_trigger_failures(db, [("-", "查询媒体库失败: %s" % exc)])
        return

    triggered = 0
    for lib in libs:
        if getattr(lib, "is_virtual", False):
            continue
        paths = (getattr(lib, "paths", "") or "").split(",")
        hit = any(p.strip() == strm_prefix or p.strip().startswith(strm_prefix + "/")
                  for p in paths if p.strip())
        if not hit:
            continue
        try:
            r = scan_queue_redis.push_scan_request(lib.id, trigger="strm-gen")
            if r.get("created"):
                triggered += 1
                logger.info("strm_gen: 已触发扫库 lib=%s(%s)", lib.id, getattr(lib, "name", ""))
        except Exception as exc:
            logger.warning("strm_gen: 触发扫库失败 lib=%s: %s", lib.id, exc)
            _record_scan_trigger_failures(db, [(lib.id, "触发扫库失败: %s" % exc)])
    if triggered:
        logger.info("strm_gen: 共触发 %d 个库的增量扫描", triggered)


def _round_is_fully_incremental(stype: str, full: bool, drive_results: list) -> bool:
    """本轮是否全走增量路径（供 S2 免全量校验决策）。

    判定口径与 A3 新增 file_id 免检一致：非全量轮、drive 模式、且所有
    成功列举的盘的 incremental_listing 均为 True。
    """
    if full or stype == "local":
        return False
    succeeded = [r for r in (drive_results or []) if not r.get("error")]
    return bool(succeeded) and all(r.get("incremental_listing") for r in succeeded)


def _verify_completeness(videos: list[tuple[str, str, str, int]],
                         state: dict[tuple[str, str], tuple[str, int, str]],
                         src_prefixes: list[str],
                         strm_root: Optional[str] = None) -> dict[str, Any]:
    """按目录（季级别）比对 Drive 文件数 vs 已生成 .strm 数。

    videos 为 (drive_id, rel, file_id, size) 四元组；分组键为
    (drive_id, 视频文件所在的父目录)（如 ("did", "剧集/动漫/妖神记 (2017)/Season 1")），
    缺集通常发生在同一季内。返回 {total_series, complete_series,
    incomplete: [{series, drive_count, strm_count, missing}]}。

    strm_root 传入时还会校验 .strm 文件真实存在于磁盘（原来只比
    "Drive 枚举 vs DB"，DB 有行但文件被删会误报完整）；磁盘缺失的条目
    同时收集为 orphan_disk（块4 §6.1：零 API 成本，纯 os.path.exists，
    下次生成即重建自愈，报告只用于观察）。
    """
    from collections import Counter

    def _strip(rel: str) -> str:
        for p in src_prefixes:
            if p and rel.startswith(p):
                return rel[len(p):]
        return rel

    def _group(did: str, rel: str) -> tuple[str, str]:
        short = _strip(rel)
        parent = os.path.dirname(short)
        return (did, parent or short)

    def _strm_exists(spath: str) -> bool:
        if strm_root is None:
            return True
        return os.path.exists(os.path.join(strm_root, spath))

    drive_counter: Counter[tuple[str, str]] = Counter()
    drive_files: dict[tuple[str, str], set[str]] = {}
    for did, rel, _fid, _size in videos:
        g = _group(did, rel)
        drive_counter[g] += 1
        drive_files.setdefault(g, set()).add(rel)
    strm_counter: Counter[tuple[str, str]] = Counter()
    strm_files: dict[tuple[str, str], set[str]] = {}
    orphan_disk_count = 0
    orphan_disk_sample: list[dict[str, str]] = []
    for (did, rel), (_fid, _size, spath) in state.items():
        if not _strm_exists(spath):
            # 块4 §6.1 orphan_disk：状态认为有，但磁盘没了
            orphan_disk_count += 1
            if len(orphan_disk_sample) < 50:
                orphan_disk_sample.append({
                    "drive_id": did, "remote_path": rel,
                    "strm_path": spath, "file_id": _fid,
                })
            continue
        g = _group(did, rel)
        strm_counter[g] += 1
        strm_files.setdefault(g, set()).add(rel)

    incomplete = []
    for group, dcnt in drive_counter.items():
        scnt = strm_counter.get(group, 0)
        if scnt < dcnt:
            gid = group[0]  # 分组键为 (drive_id, 父目录)
            missing_rels = sorted(drive_files[group] - strm_files.get(group, set()))
            # 缺集归因（2026-10-12）：区分"源上有但没生成"和".strm 文件丢了"
            not_generated = 0  # Drive 上有，state 里没有 → 生成漏了/还没跑到
            strm_lost = 0      # state 里有，但磁盘上 .strm 没了 → 文件丢失
            for rel in missing_rels:
                if (gid, rel) not in state:
                    not_generated += 1
                elif not _strm_exists(state[(gid, rel)][2]):
                    strm_lost += 1
            incomplete.append({
                "series": group,
                "drive_id": gid,
                "drive_count": dcnt,
                "strm_count": scnt,
                "missing_count": dcnt - scnt,
                "missing_sample": missing_rels[:50],
                "missing_breakdown": {
                    "not_generated": not_generated,
                    "strm_lost": strm_lost,
                },
            })
    incomplete.sort(key=lambda x: -x["missing_count"])
    return {
        "verify_mode": "full",
        "total_series": len(drive_counter),
        "complete_series": len(drive_counter) - len(incomplete),
        "incomplete_series": len(incomplete),
        "incomplete": incomplete[:100],
        # 块4 §6.1：状态有行但磁盘无文件的僵尸（零 API 成本，随本循环收集）
        "orphan_disk": {"count": orphan_disk_count, "sample": orphan_disk_sample},
    }


def _verify_completeness_light(videos: list[tuple[str, str, str, int]],
                               state: dict[tuple[str, str], tuple[str, int, str]],
                               src_prefixes: list[str],
                               strm_root: Optional[str] = None) -> dict[str, Any]:
    """S2：增量轮轻量校验——只查本轮 touch 过的目录。

    增量轮的 videos 只是 delta（拿 delta 做全量对比会误杀，见
    _run_generation_inner 注释），_verify_completeness 的 drive_count vs
    strm_count 在 delta 数据上无意义，且它要对全部 state 行做 os.path.exists
    （8.6 万次磁盘 stat）。轻量版只做两件事：
    1. delta 闭环：本轮 delta 的每个文件在 state 里有行且 .strm 在磁盘上；
    2. 受影响目录的僵尸扫描：touch 过的目录下 state 行的磁盘存在性。
    返回与 _verify_completeness 同形（total_series/complete_series/
    incomplete_series/incomplete/orphan_disk），外加 verify_mode 标记。

    多盘多目录版：videos 为 (drive_id, rel, file_id, size) 四元组，
    state 键为 (drive_id, remote_path)，分组键为 (drive_id, 父目录)。
    """
    def _strip(rel: str) -> str:
        for p in src_prefixes:
            if p and rel.startswith(p):
                return rel[len(p):]
        return rel

    def _group(did: str, rel: str) -> tuple[str, str]:
        short = _strip(rel)
        parent = os.path.dirname(short)
        return (did, parent or short)

    def _strm_exists(spath: str) -> bool:
        if strm_root is None:
            return True
        return os.path.exists(os.path.join(strm_root, spath))

    # 一遍 videos：touched 目录 + 每目录的 delta rel 集合
    touched: set[tuple[str, str]] = set()
    delta_rels: dict[tuple[str, str], set[str]] = {}
    for did, rel, _fid, _size in videos:
        g = _group(did, rel)
        touched.add(g)
        delta_rels.setdefault(g, set()).add(rel)

    # 一遍 state：只保留 touched 目录的行（僵尸扫描范围限定在此）
    state_in_touched: dict[tuple[str, str], list] = {}
    for (did, rel), val in state.items():
        g = _group(did, rel)
        if g in touched:
            state_in_touched.setdefault(g, []).append(((did, rel), val))

    incomplete = []
    orphan_disk_count = 0
    orphan_disk_sample: list[dict[str, str]] = []
    for group in sorted(touched):
        # 僵尸扫描（范围限定在本目录）
        for (did, rel), (_fid, _size, spath) in state_in_touched.get(group, []):
            if not _strm_exists(spath):
                orphan_disk_count += 1
                if len(orphan_disk_sample) < 50:
                    orphan_disk_sample.append({
                        "drive_id": did, "remote_path": rel,
                        "strm_path": spath, "file_id": _fid,
                    })
        # delta 闭环
        missing = []
        for rel in sorted(delta_rels.get(group, ())):
            key = (group[0], rel)
            if key not in state:
                missing.append((rel, "not_generated"))
            elif not _strm_exists(state[key][2]):
                missing.append((rel, "strm_lost"))
        if missing:
            not_generated = sum(1 for _, kind in missing if kind == "not_generated")
            strm_lost = len(missing) - not_generated
            incomplete.append({
                "series": group,
                "drive_count": len(delta_rels[group]),
                "strm_count": len(delta_rels[group]) - len(missing),
                "missing_count": len(missing),
                "missing_sample": [rel for rel, _ in missing[:50]],
                "missing_breakdown": {
                    "not_generated": not_generated,
                    "strm_lost": strm_lost,
                },
            })
    incomplete.sort(key=lambda x: -x["missing_count"])
    return {
        "verify_mode": "light",
        "total_series": len(touched),
        "complete_series": len(touched) - len({i["series"] for i in incomplete}),
        "incomplete_series": len(incomplete),
        "incomplete": incomplete[:100],
        "orphan_disk": {"count": orphan_disk_count, "sample": orphan_disk_sample},
    }


# ---------------------------------------------------------------------------
# 反向僵尸校验（块4 §6.2）：.strm/库条目有，但 Drive 文件已死
# ---------------------------------------------------------------------------

def _orphan_drive_summary(db) -> dict[str, Any]:
    """反向僵尸校验摘要：gone 数量 + 样本 + 上次完成时间。

    只查 strm_file_health 小表，零 Drive API 调用。表不存在/查询失败时
    降级返回空摘要（不影响缺集报告主体）。
    """
    try:
        from backend.emby_server import models as em
        total_gone = db.query(em.StrmFileHealth).filter(
            em.StrmFileHealth.status == "gone").count()
        gone_rows = db.query(em.StrmFileHealth).filter(
            em.StrmFileHealth.status == "gone").order_by(
            em.StrmFileHealth.checked_at.desc()).limit(50).all()
        last = db.query(em.StrmFileHealth.checked_at).order_by(
            em.StrmFileHealth.checked_at.desc()).first()
        checked_at = None
        if last and last[0] is not None:
            checked_at = last[0].isoformat() if hasattr(last[0], "isoformat") else str(last[0])
        return {
            "count": total_gone,
            "checked_at": checked_at,
            "sample": [{"file_id": r.file_id, "drive_id": r.drive_id,
                        "status": r.status} for r in gone_rows],
        }
    except Exception:
        return {"count": 0, "checked_at": None, "sample": []}


def run_orphan_drive_check(db) -> dict:
    """有界反向校验：抽查 file_id 在 Drive 上是否还活着。

    候选优先级：① 播放实锤 source_invalid_at 非空（排除 health 已判 gone 的）；
    ② health 表 status='unknown'/'error' 的疑点；③ checked_at 最老的 alive。
    每轮 ≤ strm_orphan_check_batch 个，per-SA QPS ≤ strm_orphan_check_qps
    （每次 sleep 独立抖动 uniform(0.8, 1.2)），每 20 个换一次 SA 分摊配额。

    判定：404（DriveNotFoundError）→ gone；200 且 trashed → gone；
    200 → alive；异常 → error（不记 gone，checked_at 照写避免死循环）。
    alive 的同时清掉其 source_invalid_at 播放标记（文件恢复 → 标记清除）。

    gone 的 file_id 视同墓碑：传入真实 drive 列表走 _consume_tombstones
    同一预算/同一删除路径（多盘按墓碑自带 drive_id 逐盘消费，state 主键已带
    drive_id，防跨盘误删）——这是 changes 漏事件（token 过期/积压丢事件）的兜底。

    反风控：token 走 drive_changes._get_token()，API 走 _api_get
    （SA 轮换/401 刷新复用现有逻辑，不另起第二套）。
    返回 {ok, checked, gone, alive, error, at}。
    """
    from backend.emby_server import models as em
    from backend.emby_server import drive_changes as dc
    from backend.emby_server.strm_source import Tombstone

    batch = orphan_check_batch(db)
    qps = orphan_check_qps(db)
    result: dict[str, Any] = {"checked": 0, "gone": 0, "alive": 0, "error": 0,
                              "at": datetime.now(timezone.utc).isoformat()}

    # ---- 候选集 ----
    cands: list[tuple[str, str]] = []  # (file_id, drive_id)
    seen_ids: set[str] = set()

    def _add(fid: Optional[str], did: Optional[str]) -> None:
        if fid and fid not in seen_ids and len(cands) < batch:
            seen_ids.add(fid)
            cands.append((fid, did or ""))

    try:
        # ① 播放实锤的优先复核（排除 health 已判 gone 的）
        gone_q = db.query(em.StrmFileHealth.file_id).filter(
            em.StrmFileHealth.status == "gone")
        rows = db.query(em.MediaItem.drive_file_id).filter(
            em.MediaItem.drive_file_id.isnot(None),
            em.MediaItem.drive_file_id != "",
            em.MediaItem.source_invalid_at.isnot(None),
            ~em.MediaItem.drive_file_id.in_(gone_q)).limit(batch).all()
        for (fid,) in rows:
            _add(fid, "")
        # ② 墓碑积压的疑点（unknown / error）
        if len(cands) < batch:
            for r in db.query(em.StrmFileHealth).filter(
                    em.StrmFileHealth.status.in_(["unknown", "error"])).order_by(
                    em.StrmFileHealth.checked_at.asc()).limit(
                    batch - len(cands)).all():
                _add(r.file_id, r.drive_id)
        # ③ checked_at 最老的 alive（兜底全量轮查）
        if len(cands) < batch:
            for r in db.query(em.StrmFileHealth).filter(
                    em.StrmFileHealth.status == "alive").order_by(
                    em.StrmFileHealth.checked_at.asc()).limit(
                    batch - len(cands)).all():
                _add(r.file_id, r.drive_id)
    except Exception as exc:
        logger.warning("strm_gen: 僵尸校验候选集查询失败: %s", exc)
        return dict(result, ok=False, error_msg=str(exc))

    if not cands:
        return dict(result, ok=True)

    token = dc._get_token()
    if not token:
        logger.warning("strm_gen: 僵尸校验无可用 SA token，本轮跳过")
        return dict(result, ok=False, error_msg="无可用 SA token")

    sleep_base = 1.0 / qps
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    gone_tombstones: list[Tombstone] = []

    for idx, (fid, did) in enumerate(cands):
        if idx and idx % 20 == 0:
            # 每 20 个换 SA，分摊配额（反风控）
            token = dc._get_token() or token
        status, note = "error", ""
        for attempt in range(2):
            try:
                body = dc._api_get(
                    "https://www.googleapis.com/drive/v3/files/%s" % fid, token,
                    {"fields": "id,trashed", "supportsAllDrives": "true"})
                # 200 且 trashed → gone；200 → alive
                status = "gone" if body.get("trashed") else "alive"
                break
            except dc.DriveNotFoundError:
                # 404：可能是真删除，也可能是单个 SA 掉权限（Drive 对掉权限
                # 的 SA 也回 404 而非 401）——换一个 SA 交叉确认一次，
                # 两次都 404 才判 gone，避免误杀。
                if attempt == 1:
                    status = "gone"
                    break
                token = dc._get_token()
                if not token:
                    status = "gone"
                    break
                continue
            except PermissionError:
                # 401：token 失效，换 SA 重试一次
                # R18-P3：只在"还要重试"时才取新 token——旧实现 attempt==1 时
                # 先取了第三个 SA 的 token 又直接 break，白白烧掉一次 SA 轮换位。
                if attempt == 1:
                    note = "token 失效"
                    break
                token = dc._get_token()
                if not token:
                    note = "token 失效"
                    break
                continue
            except Exception as exc:  # noqa: BLE001 — 异常不记 gone，避免误杀
                note = str(exc)[:200]
                break
        # 落库（upsert）
        try:
            row = db.query(em.StrmFileHealth).filter(
                em.StrmFileHealth.file_id == fid).first()
            if row is None:
                row = em.StrmFileHealth(file_id=fid, drive_id=did)
                db.add(row)
            row.status = status
            row.checked_at = now_naive
            row.note = note[:512]
            if did:
                row.drive_id = did
            db.flush()
        except Exception as exc:
            logger.warning("strm_gen: 写 file_health 失败 %s: %s", fid, exc)
        result["checked"] += 1
        # 墓碑 drive_id 归一化由 _consume_tombstones 统一做（按 file_id 反查
        # 状态表；查不到的记 "unknown"，消费侧记疑点不删除，防跨盘误删）。
        if status == "gone":
            result["gone"] += 1
            gone_tombstones.append(Tombstone(
                file_id=fid, drive_id=did or "unknown",
                reason="orphan_check", detected_at=result["at"]))
        elif status == "alive":
            result["alive"] += 1
            # 复活：清掉播放实锤标记（文件恢复/重新分享 → 标记清除 → 播放恢复）
            try:
                db.query(em.MediaItem).filter(
                    em.MediaItem.drive_file_id == fid,
                    em.MediaItem.source_invalid_at.isnot(None)).update(
                    {"source_invalid_at": None}, synchronize_session=False)
                db.flush()
            except Exception:
                pass
        else:
            result["error"] += 1
        # 限速：per-SA QPS ≤ qps，每次 sleep 独立抖动（反风控）
        time.sleep(sleep_base * random.uniform(0.8, 1.2))

    # gone 视同墓碑：走 _consume_tombstones 同一预算/同一删除路径。
    # ok_drives=None（#809）：每个候选都单独对 Drive 验过（404/trashed），逐个复核
    # 本身就是信任锚，不需要"本轮列举成功"门控。drive_id 从状态行按 file_id
    # 反查（两盘全挂；file_id 全局唯一）；查不到的记 "unknown"，消费侧记疑点
    # 不删除（防跨盘误删）。
    # videos_seen 取状态总行数：零结果守卫针对的是 changes token 失效，
    # 这里每个文件都单独验过，不适用。
    if gone_tombstones:
        state = _load_state()
        strm_root = _container_strm_root(db)
        stats: dict[str, Any] = {}
        drive_ids = sorted(_discover_drives().keys())
        _consume_tombstones(db, gone_tombstones, state, strm_root, stats,
                            drive_ids, len(state), track_backlog=False,
                            ok_drives=None)
        result["prune_stats"] = stats

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("strm_gen: 僵尸校验提交失败: %s", exc)
        return dict(result, ok=False, error_msg=str(exc))
    logger.info("strm_gen: 僵尸校验完成 checked=%d gone=%d alive=%d error=%d",
                result["checked"], result["gone"], result["alive"], result["error"])
    return dict(result, ok=True)


# ---------------------------------------------------------------------------
# 缺集报告（供管理后台展示）
# ---------------------------------------------------------------------------

def missing_report(db, limit: int = 100) -> dict[str, Any]:
    """基于上次生成统计的缺集报告。如无统计，返回空。

    块4：返回值扩展（不新增接口，前端在现有缺集报告页加区块）：
    - orphan_disk：状态有行但磁盘无文件的僵尸（零 API，随校验循环收集）
    - orphan_drive：反向校验摘要（gone 数量/样本/上次完成时间）
    - tombstone_pending：上轮收到的墓碑总数
    - prune_blocked：删除保护拦截信息（None=本轮未拦截）
    - prune_would_delete：dry_run 演习清单（count/sample）
    - dry_run_stale_warning / dry_run_stale_text / dry_run_backlog_since：
      R10 黄条提醒 hook 点（dry_run 长期开着 + would-delete 持续积压超阈值）
    """
    try:
        from backend.integrations import store
        raw = store.get_value(db, CONFIG_LAST_STATS, "")
        if not raw:
            return {"ok": True, "has_data": False, "incomplete": [],
                    **_dry_run_stale_info(db, {})}
        stats = json.loads(raw)
        stale_info = _dry_run_stale_info(db, stats)
        verify = stats.get("verify", {})
        # 生成失败/未完成校验时 stats 里没有 verify，此时视为无数据，
        # 否则前端会显示"剧集总数 0、完整 0、缺集 0"的误导性空报告。
        if not verify:
            return {"ok": True, "has_data": False, "incomplete": [],
                    **stale_info}
        return {
            "ok": True,
            "has_data": True,
            "verify_mode": verify.get("verify_mode", "full"),
            "total_series": verify.get("total_series", 0),
            "complete_series": verify.get("complete_series", 0),
            "incomplete_series": verify.get("incomplete_series", 0),
            "incomplete": verify.get("incomplete", [])[:limit],
            "orphan_disk": verify.get("orphan_disk", {"count": 0, "sample": []}),
            "orphan_drive": _orphan_drive_summary(db),
            "tombstone_pending": stats.get("tombstone_pending", 0),
            "prune_blocked": stats.get("prune_blocked"),
            "prune_would_delete": stats.get(
                "prune_would_delete", {"count": 0, "sample": []}),
            # R10：dry_run 长期提醒（黄条 hook 点，配置页"删除保护"分组用）
            **stale_info,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 定时调度（daemon 线程，按增量间隔跑）
# ---------------------------------------------------------------------------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _scheduler_loop():
    """调度循环：按配置的增量间隔跑增量生成。

    反风控设计（用户明确要求）：
    - 间隔等待加 ±20% 随机抖动（默认），避免机械的固定节拍被 Google 识别为机器行为。
    - 抖动比例可配置（strm_gen_jitter_ratio），0=关闭（便于测试）。
    - Changes API 本身只拉变化，配额消耗远小于全量，缩短间隔不会打爆配额。
    - interval_minutes=0 时关闭定时增量（只手动触发）。
    """
    # 上次间隔增量执行时间（monotonic，避免系统时间跳变影响）
    last_interval_ts = 0.0
    while True:
        try:
            from backend.database import SessionLocal
            db = SessionLocal()
            try:
                if not enabled(db):
                    time.sleep(300)
                    continue

                # 间隔增量：按用户配置的分钟数跑，带抖动防风控
                iv_min = interval_minutes(db)
                if iv_min <= 0:
                    # 0=关闭定时增量，只手动触发
                    time.sleep(300)
                    continue
                wait_sec = _jittered_interval_sec(db)
                elapsed = time.monotonic() - last_interval_ts
                if last_interval_ts > 0 and elapsed < wait_sec:
                    # 还没到间隔，短睡后继续
                    time.sleep(min(60, wait_sec - elapsed))
                    continue
                if get_progress()["running"] and not _lock_stale(db):
                    time.sleep(60)
                    continue
                # 首次启动时 last_interval_ts=0，延迟 5 分钟再跑，避免启动即 burst
                if last_interval_ts == 0:
                    logger.info("strm_gen: 调度器启动，%.0f 秒后开始首次间隔增量", wait_sec)
                    last_interval_ts = time.monotonic()
                    time.sleep(min(60, wait_sec))
                    continue
                logger.info("strm_gen: 间隔增量触发（配置 %d 分钟，抖动后 %.0f 秒）",
                            iv_min, wait_sec)
                run_generation(db, full=False)
                last_interval_ts = time.monotonic()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("strm_gen: 定时循环异常: %s", exc)
        time.sleep(60)


def start_scheduler() -> bool:
    """启动定时调度线程（幂等）。"""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return True
        t = threading.Thread(target=_scheduler_loop, daemon=True, name="strm-gen-scheduler")
        t.start()
        _scheduler_started = True
        logger.info("strm_gen: 定时调度已启动")
        return True


# ---------------------------------------------------------------------------
# 反向僵尸校验调度（独立 daemon 线程，块4 §8）
# ---------------------------------------------------------------------------

_orphan_started = False
_orphan_lock = threading.Lock()


def _orphan_loop():
    """僵尸校验调度循环：每小时检查一次是否到期，到期则跑一轮。

    反风控：间隔 = interval_days × uniform(0.8, 1.2)（±20% 抖动），
    下次运行时间持久化在 SystemConfig（strm_orphan_check_next_run），
    进程重启不丢失进度、不重复 burst。
    与 strm_gen 主任务互斥无关（只读 + 小表写）。
    """
    # 启动后先等 0.5–1 小时再检查，避免启动即 burst
    next_wake = time.monotonic() + random.uniform(1800, 3600)
    while True:
        try:
            if time.monotonic() >= next_wake:
                next_wake = time.monotonic() + 3600
                from backend.database import SessionLocal
                db = SessionLocal()
                try:
                    if not orphan_check_enabled(db):
                        continue
                    now = datetime.now(timezone.utc)
                    due = True
                    nxt_raw = _db_config(db, CONFIG_ORPHAN_NEXT_RUN, "")
                    if nxt_raw:
                        try:
                            nxt = datetime.fromisoformat(nxt_raw)
                            if nxt.tzinfo is None:
                                nxt = nxt.replace(tzinfo=timezone.utc)
                            due = now >= nxt
                        except ValueError:
                            due = True
                    if not due:
                        continue
                    logger.info("strm_gen: 反向僵尸校验到期，开始一轮")
                    run_orphan_drive_check(db)
                    interval = orphan_check_interval_days(db) * 86400
                    jittered = interval * random.uniform(0.8, 1.2)
                    nxt = now + timedelta(seconds=jittered)
                    try:
                        from backend.integrations import store
                        store.write_values(db, {
                            CONFIG_ORPHAN_LAST_RUN: now.isoformat(),
                            CONFIG_ORPHAN_NEXT_RUN: nxt.isoformat(),
                        }, {
                            CONFIG_ORPHAN_LAST_RUN: "反向僵尸校验上次执行时间",
                            CONFIG_ORPHAN_NEXT_RUN: "反向僵尸校验下次执行时间",
                        })
                        db.commit()
                    except Exception:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                finally:
                    db.close()
        except Exception as exc:
            logger.warning("strm_gen: 僵尸校验调度异常: %s", exc)
        time.sleep(60)


def start_orphan_scheduler() -> bool:
    """启动反向僵尸校验调度线程（幂等）。"""
    global _orphan_started
    with _orphan_lock:
        if _orphan_started:
            return True
        t = threading.Thread(target=_orphan_loop, daemon=True, name="strm-orphan-check")
        t.start()
        _orphan_started = True
        logger.info("strm_gen: 反向僵尸校验调度已启动")
        return True


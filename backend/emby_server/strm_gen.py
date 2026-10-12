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
- strm_gen_source_dir：Drive 上的源目录（相对网盘根，如 "MoviePilot/"），默认 "MoviePilot/"
- strm_gen_interval_minutes：增量间隔（分钟），默认 60；0 = 关闭定时增量
- strm_gen_prune：是否删除 Drive 上已不存在的 .strm 文件及状态行，默认 false
  （删文件不可逆：默认只上报不删，先在缺集报告确认再手动开启）
- strm_gen_last_run：上次执行完成时间（ISO）
- strm_gen_last_stats：上次执行统计（JSON）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG_ENABLED = "strm_gen_enabled"
CONFIG_SOURCE_DIR = "strm_gen_source_dir"
CONFIG_LAST_RUN = "strm_gen_last_run"
CONFIG_LAST_STATS = "strm_gen_last_stats"
CONFIG_DRIVE_ID = "strm_gen_drive_id"
CONFIG_PRUNE = "strm_gen_prune"
# 增量间隔（分钟）：两次增量生成之间的间隔，用户可在后台自由设置。
# 0 = 关闭定时增量（只手动触发）。默认 60。
CONFIG_INTERVAL = "strm_gen_interval_minutes"
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

DEFAULT_SOURCE_DIR = "MoviePilot/"
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_JITTER_RATIO = 0.2
DEFAULT_CONCURRENCY = 3
DEFAULT_API_QPS = 8.0

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


def source_dir(db) -> str:
    """Drive 源目录，归一化为以 / 结尾的相对路径（"" = 网盘根）。"""
    raw = _db_config(db, CONFIG_SOURCE_DIR, DEFAULT_SOURCE_DIR).strip().replace("\\", "/")
    raw = raw.strip("/")
    return (raw + "/") if raw else ""


def drive_id_config(db) -> str:
    """配置指定的 Drive ID（空 = 自动选择）。"""
    return _db_config(db, CONFIG_DRIVE_ID, "").strip()


# 源失败记录（供 system_alerts 的 strm_source_failed 事件读取）。
CONFIG_SOURCE_FAILURES = "strm_gen_source_failures"


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

    默认关闭（只上报不删）：删文件是不可逆操作，先让用户在缺集报告里
    确认确实不需要了再手动开启。
    """
    return _db_config(db, CONFIG_PRUNE, "false").strip().lower() in ("1", "true", "yes", "on")


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


def iter_drive_videos(drive_id: str, dir_path: str, strict: bool = True):
    """流式列出源目录下所有视频文件。

    yield (root_relpath, file_id, size)。root_relpath 为相对网盘根路径。

    strict=True（默认）：源目录不存在时抛 RuntimeError（显式配置的盘，
    管理员需要知道配错了）。
    strict=False：源目录不存在时记 warning 并返回空（自动发现模式下，
    不是每个盘都有媒体目录，缺目录是预期情况，不应报错）。

    实现：扁平全量拉取 + 本地建树。
    旧实现是 BFS、每个目录发一次 files.list（"'{folder_id}' in parents"）：
    8.5 万文件 / 数千目录 = 数千次 API，每次 200~500ms，列举阶段要
    10~30 分钟，且全程不更新进度——前端一直显示"列举 Drive 文件…"
    且计数全 0，看起来像卡死（用户实报）。
    新实现一次 files.list（无 parents 限定，pageSize=1000）分页拉完全盘条目，
    本地按 parents 建树再从源目录 BFS 展开：API 调用从数千降到约 100，
    且每拉完一页就汇报进度，前端能看到条目数在涨。
    代价：会拉取源目录之外的条目元数据（仅 id/name/parents，不下载内容）；
    对 MoviePilot 这类单用途盘可忽略。

    （修 P1-1：旧实现只在开始时取一次 token，8 万级文件的长列举
    会因 SA token 过期而整体作废——_page_fetcher 的 401 自动刷新保留）。
    """
    fetch = _page_fetcher(drive_id)  # 内部已做 SA 凭据检查 + 401 自动刷新
    dir_id = _resolve_dir_id(fetch, drive_id, dir_path)
    if dir_path and not dir_id:
        if not strict:
            logger.warning("strm_gen: drive=%s 上找不到源目录 %r，已跳过该盘",
                           drive_id, dir_path)
            return
        raise RuntimeError(f"Drive 上找不到源目录: {dir_path!r}")

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

    # 2. 本地 BFS：从源目录（或网盘根）逐层展开，只取源目录子树。
    #    网盘根相对路径前缀：dir_path 本身（与旧实现一致，状态键保持兼容）。
    norm = dir_path.strip("/")
    if norm:
        stack: list[tuple[str, str]] = [(dir_id, "")]
        root_prefix = norm + "/"
    else:
        # 共享盘根目录：'root' 别名只对个人盘有效，共享盘顶层用 drive_id。
        # 原来无条件用 'root' in parents，共享盘配空源目录时列举结果恒为空
        # （静默丢全部）——这里换成本地建树的等价逻辑。
        dc = _drive_modules()
        try:
            is_personal = dc._is_personal_drive_key(drive_id)
        except Exception:
            is_personal = False
        stack = [("root" if is_personal else drive_id, "")]
        root_prefix = ""

    while stack:
        folder_id, rel_prefix = stack.pop()
        for f in children.get(folder_id, []):
            name = _safe_name(f.get("name", ""))
            fid = f.get("id", "")
            if not name or not fid:
                continue
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                if name.startswith(".") or name in SKIP_DIR_NAMES:
                    continue
                stack.append((fid, rel_prefix + name + "/"))
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


def _load_state() -> dict[str, tuple[str, int, str]]:
    """读 strm_gen_files 表：{remote_path: (file_id, size, strm_path)}。

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
        return {r.remote_path: (r.file_id, r.size or 0, r.strm_path) for r in rows}
    except Exception as exc:
        logger.warning("strm_gen: 读状态表失败: %s", exc)
        return {}
    finally:
        sess.close()


def _save_state_rows(rows: list[tuple[str, str, int, str]]):
    """批量 upsert 状态。rows: [(remote_path, file_id, size, strm_path)]。

    原来逐行 SELECT+add：8.5 万行 = 8.5 万次 point 查询。现在按批
    "先删后插"（DELETE WHERE remote_path IN (...) + bulk_insert_mappings），
    每批 2 条 SQL，与 PG/SQLite 都兼容。批内按 remote_path 去重保留最后一条。
    """
    if not rows:
        return
    try:
        from backend.emby_server import models as em
        from backend.database import SessionLocal
    except Exception:
        return
    # 批内去重：同 remote_path 保留最后一条（同目录同名文件的极限情况）
    dedup: dict[str, tuple[str, str, int, str]] = {}
    for r in rows:
        dedup[r[0]] = r
    rows = list(dedup.values())
    sess = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        keys = [r[0] for r in rows]
        sess.query(em.StrmGenFile).filter(
            em.StrmGenFile.remote_path.in_(keys)).delete(synchronize_session=False)
        sess.bulk_insert_mappings(em.StrmGenFile, [
            {"remote_path": rp, "file_id": fid, "size": size,
             "strm_path": sp, "updated_at": now}
            for rp, fid, size, sp in rows
        ])
        sess.commit()
    except Exception as exc:
        sess.rollback()
        logger.warning("strm_gen: 写状态表失败: %s", exc)
    finally:
        sess.close()


def _delete_state_rows(remote_paths: list[str]):
    """删除状态行（给 prune 用）。"""
    if not remote_paths:
        return
    try:
        from backend.emby_server import models as em
        from backend.database import SessionLocal
    except Exception:
        return
    sess = SessionLocal()
    try:
        sess.query(em.StrmGenFile).filter(
            em.StrmGenFile.remote_path.in_(remote_paths)).delete(synchronize_session=False)
        sess.commit()
    except Exception as exc:
        sess.rollback()
        logger.warning("strm_gen: 删状态表失败: %s", exc)
    finally:
        sess.close()


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
        })


def try_acquire() -> bool:
    """原子尝试获取运行权：成功返回 True（已置 running），失败返回 False。

    给管理后台手动触发用——原来是"查 running → 起线程 → 线程里再置 running"，
    锁在起线程前就释放了，快速连点两次都会返回"已开始"，第二次被静默吞掉。
    现在获取与置位在同一把锁内完成，无竞态。
    """
    with _lock:
        if _progress["running"]:
            return False
        _progress["running"] = True
        return True


def _release_progress(stats: dict, db):
    """释放运行权：phase 复位 + 落盘上次执行统计。"""
    with _lock:
        # 非预期异常时 phase 可能卡在 generating：统一收敛到 error，
        # 前端不会一直显示"生成中"但实际已停。
        if _progress.get("phase") not in ("done", "idle"):
            _progress["phase"] = "error"
        _progress["running"] = False
        _progress["finished_at"] = datetime.now(timezone.utc).isoformat()
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

    drive_id=None 时遍历所有发现的共享盘；指定则只跑那一个盘。
    同一时间只允许一个任务运行（原子获取，见 try_acquire）。
    返回统计 dict。
    """
    if not try_acquire():
        return {"ok": False, "error": "已有生成任务在运行中"}
    return run_owned(db, full=full, drive_id=drive_id)


def run_owned(db, full: bool = False, drive_id: Optional[str] = None) -> dict[str, Any]:
    """已持有运行权时执行一次生成（try_acquire 成功后调用）。"""
    _reset_progress()
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
        _release_progress(stats, db)


def _run_generation_inner(db, full: bool, drive_id: Optional[str], stats: dict) -> dict[str, Any]:
    src_prefix = source_dir(db)
    strm_root = _container_strm_root(db)

    # 确定数据源：通用增量接口（SourceLister），所有挂载方式都能用。
    # drive: 用 Changes API 做增量（只拉变化），全量回退到 files.list
    # local: 用 (mtime_ns, size) 指纹做增量检测
    stype = source_type(db)
    all_videos: list[tuple[str, str, int]] = []
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
        logger.info("strm_gen: 开始生成 local=%s full=%s", root, full)
        _set_progress(phase="listing", current=f"正在列举本地文件…（{root}）")
        videos: list[tuple[str, str, int]] = []
        try:
            # 增量模式走指纹比对；全量模式（full=True）走全量 walk
            for rel, fid, size in lister.iter_videos(incremental=not full):
                videos.append((rel, fid, size))
        except Exception as exc:
            _set_progress(phase="error")
            _add_error(f"列举失败 [{root}]: {exc}")
            return {"ok": False, "error": str(exc)}
        logger.info("strm_gen: local=%s 列举到 %d 个视频文件", root, len(videos))
        all_videos.extend(videos)
        # 本地模式下 rel 已是相对 root 的路径，src_prefix 为空
        src_prefix = ""
    else:
        # drive 模式：确定要跑的盘（显式参数 > 配置 > 遍历所有发现的共享盘）
        drive_id = drive_id or drive_id_config(db)
        if drive_id:
            drive_ids = [drive_id]
        else:
            drives = _discover_drives()
            if not drives:
                _set_progress(phase="error")
                _add_error("未发现可用 Drive（共享盘）")
                return {"ok": False, "error": "未发现可用 Drive"}
            drive_ids = sorted(drives.keys())
            if len(drive_ids) > 1:
                logger.info("strm_gen: 发现 %d 个共享盘，并行生成: %s",
                            len(drive_ids), drive_ids)
        from backend.emby_server import strm_source
        from backend.emby_server import drive_changes as dc

        # 全局 API 速率按配置调整（热读，默认 8 QPS 保守值）
        dc.set_api_qps(api_qps(db))
        # 并发数按配置（默认 3，钳制 [1, 10]）
        concurrency = drive_concurrency(db)

        # 自动发现模式（未显式配置 drive_id）时，源目录不存在的盘直接跳过
        # 而非报错——不是每个盘都有媒体目录。
        auto_discover = not bool(drive_id)

        def _list_one_drive(did: str) -> dict:
            """单个 drive 的列举 worker（线程池内执行）。

            返回 {"did":..., "videos":[...], "retries":N, "new_token":..., "error":None/str}。
            异常不向外抛，记进 error 字段由主线程统一处理——某盘失败不影响
            其他盘的列举结果收集（失败的盘被跳过并告警）。

            增量检查点（changes page token）也在这里带回：主线程在整轮成功后
            统一保存（失败时不保存，下轮用旧 token 重放）。
            """
            dc.reset_total_retry_count()
            lister = strm_source.DriveChangesLister(
                did, src_prefix, strict_dir=not auto_discover)
            videos: list[tuple[str, str, int]] = []
            try:
                # 增量模式走 Changes API；全量模式（full=True）回退到 files.list
                # （2026-10-12 起 list_changes 不再吞错：API 失败直接抛，
                # 这里捕获后记 error，绝不静默当成 0 文件）
                for rel, fid, size in lister.iter_videos(incremental=not full):
                    videos.append((rel, fid, size))
            except Exception as exc:
                return {
                    "did": did,
                    "videos": [],
                    "retries": dc.get_total_retry_count(),
                    "path_skipped": lister.path_resolve_skipped,
                    "new_token": None,
                    "error": str(exc)[:300],
                }
            return {
                "did": did,
                "videos": videos,
                "retries": dc.get_total_retry_count(),
                "path_skipped": lister.path_resolve_skipped,
                "new_token": lister._new_token,
                "error": None,
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
                    logger.info("strm_gen: drive=%s 列举到 %d 个视频文件"
                                "（API 重试 %d 次，路径解析跳过 %d）",
                                did, len(r["videos"]), r["retries"], r["path_skipped"])
            stats.setdefault("sources", {})[did] = {
                "listed": len(r["videos"]),
                "retries": r["retries"],
                "path_skipped": r["path_skipped"],
                "error": r["error"],
            }
            all_videos.extend(r["videos"])
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
    state = {} if full else _load_state()
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
        pending_rows: list[tuple[str, str, int, str]] = []
        done = get_progress()["done"]
        for rel, fid, size in all_videos:
            done += 1
            _set_progress(done=done, current=rel)
            old = state.get(rel)
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
                strm_rel = _resolve_collision(strm_relpath(rel, src_prefix), used)

            url = drive_url(fid)
            full_path = os.path.join(strm_root, strm_rel)
            if _write_strm_file(full_path, url):
                used.add(strm_rel)
                pending_rows.append((rel, fid, size, strm_rel))
                state[rel] = (fid, size, strm_rel)
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

    # 3. 完整性校验：按顶层目录统计 Drive vs .strm（校验磁盘真实存在，
    # 原来只比"Drive 枚举 vs DB"，DB 有行但文件被删会误报完整）
    _set_progress(phase="verifying", current="正在校验完整性…")
    verify = _verify_completeness(all_videos, state, src_prefix, strm_root=strm_root)
    stats["verify"] = verify

    # 4. 清理 Drive 上已不存在的条目（默认关闭，只上报不删）。
    # 多盘时跳过：A 盘删掉的文件在 B 盘可能还存在，跨盘无法判定。
    if prune_enabled(db):
        if len(drive_ids) == 1:
            seen = {rel for rel, _fid, _size in all_videos}
            stale = [rel for rel in state if rel not in seen]
            for rel in stale:
                spath = os.path.join(strm_root, state[rel][2])
                try:
                    if os.path.exists(spath):
                        os.remove(spath)
                except OSError as exc:
                    logger.warning("strm_gen: 删除过期 .strm 失败 %s: %s", spath, exc)
                del state[rel]
            _delete_state_rows(stale)
            stats["pruned"] = len(stale)
            logger.info("strm_gen: 清理过期条目 %d 个", len(stale))
        else:
            logger.info("strm_gen: 多盘模式跳过清理（跨盘无法判定过期）")

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
                    _lister = _ss.DriveChangesLister(did, src_prefix)
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
    """
    try:
        from backend.emby_server import models as em
        from backend.emby_server import scan_queue_redis
    except Exception as exc:
        logger.warning("strm_gen: 扫库触发器加载失败: %s", exc)
        return

    strm_root = _container_strm_root(db)
    # 归一化：/strm 结尾不带斜杠，用于前缀匹配
    strm_prefix = strm_root.rstrip("/")
    try:
        libs = db.query(em.Library).filter(em.Library.is_enabled == True).all()  # noqa: E712
    except Exception as exc:
        logger.warning("strm_gen: 查询媒体库失败: %s", exc)
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
    if triggered:
        logger.info("strm_gen: 共触发 %d 个库的增量扫描", triggered)


def _verify_completeness(videos: list[tuple[str, str, int]],
                         state: dict[str, tuple[str, int, str]],
                         src_prefix: str,
                         strm_root: Optional[str] = None) -> dict[str, Any]:
    """按目录（季级别）比对 Drive 文件数 vs 已生成 .strm 数。

    分组键为视频文件所在的父目录（如 "剧集/动漫/妖神记 (2017)/Season 1"），
    缺集通常发生在同一季内。返回 {total_series, complete_series,
    incomplete: [{series, drive_count, strm_count, missing}]}。

    strm_root 传入时还会校验 .strm 文件真实存在于磁盘（原来只比
    "Drive 枚举 vs DB"，DB 有行但文件被删会误报完整）。
    """
    from collections import Counter

    def _group(rel: str) -> str:
        short = rel[len(src_prefix):] if src_prefix and rel.startswith(src_prefix) else rel
        parent = os.path.dirname(short)
        return parent or short

    def _strm_exists(spath: str) -> bool:
        if strm_root is None:
            return True
        return os.path.exists(os.path.join(strm_root, spath))

    drive_counter: Counter[str] = Counter()
    drive_files: dict[str, set[str]] = {}
    for rel, _fid, _size in videos:
        g = _group(rel)
        drive_counter[g] += 1
        drive_files.setdefault(g, set()).add(rel)
    strm_counter: Counter[str] = Counter()
    strm_files: dict[str, set[str]] = {}
    for rel, (_fid, _size, spath) in state.items():
        if not _strm_exists(spath):
            continue
        g = _group(rel)
        strm_counter[g] += 1
        strm_files.setdefault(g, set()).add(rel)

    incomplete = []
    for group, dcnt in drive_counter.items():
        scnt = strm_counter.get(group, 0)
        if scnt < dcnt:
            missing_rels = sorted(drive_files[group] - strm_files.get(group, set()))
            # 缺集归因（2026-10-12）：区分"源上有但没生成"和".strm 文件丢了"
            not_generated = 0  # Drive 上有，state 里没有 → 生成漏了/还没跑到
            strm_lost = 0      # state 里有，但磁盘上 .strm 没了 → 文件丢失
            for rel in missing_rels:
                if rel not in state:
                    not_generated += 1
                elif not _strm_exists(state[rel][2]):
                    strm_lost += 1
            incomplete.append({
                "series": group,
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
        "total_series": len(drive_counter),
        "complete_series": len(drive_counter) - len(incomplete),
        "incomplete_series": len(incomplete),
        "incomplete": incomplete[:100],
    }


# ---------------------------------------------------------------------------
# 缺集报告（供管理后台展示）
# ---------------------------------------------------------------------------

def missing_report(db, limit: int = 100) -> dict[str, Any]:
    """基于上次生成统计的缺集报告。如无统计，返回空。"""
    try:
        from backend.integrations import store
        raw = store.get_value(db, CONFIG_LAST_STATS, "")
        if not raw:
            return {"ok": True, "has_data": False, "incomplete": []}
        stats = json.loads(raw)
        verify = stats.get("verify", {})
        # 生成失败/未完成校验时 stats 里没有 verify，此时视为无数据，
        # 否则前端会显示"剧集总数 0、完整 0、缺集 0"的误导性空报告。
        if not verify:
            return {"ok": True, "has_data": False, "incomplete": []}
        return {
            "ok": True,
            "has_data": True,
            "total_series": verify.get("total_series", 0),
            "complete_series": verify.get("complete_series", 0),
            "incomplete_series": verify.get("incomplete_series", 0),
            "incomplete": verify.get("incomplete", [])[:limit],
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
                if get_progress()["running"]:
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


"""开箱即用的播放优化通用能力。

用户部署 Aetrix、挂载存储后，不用手动调任何参数，播放就好好工作。
本模块收敛四块能力，全项目只此一处实现（横切能力只许一套）：

1. rclone 挂载参数自动优化（``DEFAULT_RCLONE_VFS_ARGS``）
1b. rclone 缓存配置与磁盘压力保护（``build_rclone_cache_args`` /
    ``check_disk_pressure``）
2. SA 应用层轮换（``ensure_sa_rotation``）
3. 单文件并发 Range 限流（``RangeConcurrencyLimiter``）
"""

from __future__ import annotations

import configparser
import json
import logging
import random as _random
import shutil
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. rclone 挂载参数自动优化
# ---------------------------------------------------------------------------
# 实测结论（2026-10-07 压力排查）：
# - vfs-read-chunk-size 默认 128M：起播要先抓满 128MB 才出首字节，手机端
#   体感就是"点了没反应"。32M 让首字节快约 4 倍。
# - vfs-read-ahead 128M：顺序播放时预读隐藏网络延迟，拖动进度条更跟手。
# - chunk-size-limit 256M：长顺序读自动放大分块，吞吐不掉。
# - buffer-size 32M / drive-chunk-size 128M：上传/缓冲配平，避免小水管被打满。
DEFAULT_RCLONE_VFS_ARGS: list[str] = [
    "--vfs-cache-mode", "full",
    "--vfs-read-chunk-size", "32M",
    "--vfs-read-chunk-size-limit", "256M",
    "--vfs-read-ahead", "128M",
    "--buffer-size", "32M",
    "--drive-chunk-size", "128M",
]

# 通用挂载开关（与性能无关，但开箱即用必须有）
DEFAULT_RCLONE_MOUNT_FLAGS: list[str] = [
    "--daemon",
    "--allow-other",
]

# ---------------------------------------------------------------------------
# 1b. rclone 缓存配置（磁盘压力保护）
# ---------------------------------------------------------------------------
# VFS 缓存是播放流畅的关键，但无上限的缓存会吃光磁盘。这里全部可配置：
# - max_size：缓存上限（None = 按磁盘自动算，见 auto_vfs_cache_size）
# - max_age：缓存文件最长保留（LRU 淘汰冷数据）
# - min_free_space：磁盘剩余小于此值时 rclone 自动清理缓存（磁盘保护线）
# - dir_cache_time：目录结构缓存时间（减少 Drive API 调用）
# - cache_dir：缓存落盘目录
DEFAULT_VFS_CACHE_MAX_AGE = "72h"
DEFAULT_VFS_CACHE_MIN_FREE_SPACE = "10G"
DEFAULT_DIR_CACHE_TIME = "5m"
DEFAULT_CACHE_DIR = "/var/cache/rclone"


def build_rclone_cache_args(
    cache_dir: str = DEFAULT_CACHE_DIR,
    max_size: str | None = None,
    max_age: str = DEFAULT_VFS_CACHE_MAX_AGE,
    min_free_space: str = DEFAULT_VFS_CACHE_MIN_FREE_SPACE,
    dir_cache_time: str = DEFAULT_DIR_CACHE_TIME,
) -> list[str]:
    """拼出 rclone 缓存相关参数。

    ``max_size=None`` 时按 ``cache_dir`` 所在磁盘自动计算（见
    ``auto_vfs_cache_size``）；显式传入则直接使用（如 ``"5G"``）。
    返回去重前的参数列表（调用方用 build_rclone_mount_args 合并去重）。
    """
    size = max_size if max_size else auto_vfs_cache_size(cache_dir)
    return [
        "--cache-dir", cache_dir,
        "--vfs-cache-max-size", size,
        "--vfs-cache-max-age", max_age,
        "--vfs-cache-min-free-space", min_free_space,
        "--dir-cache-time", dir_cache_time,
    ]


def check_disk_pressure(cache_dir: str = DEFAULT_CACHE_DIR,
                        min_free_gb: float = 10.0) -> dict:
    """检查缓存目录所在磁盘压力。

    返回 ``{"ok": bool, "free_gb": float, "total_gb": float,
    "use_pct": float, "warning": str|None}``。
    ``ok=False`` 表示剩余空间低于 ``min_free_gb``，建议清理或调小缓存。
    检测失败时 ``ok=True``（不阻断主流程，只告警）。
    """
    result: dict = {"ok": True, "free_gb": 0.0, "total_gb": 0.0,
                    "use_pct": 0.0, "warning": None}
    try:
        total, used, free = shutil.disk_usage(cache_dir)
        total_gb = total / (1024 ** 3)
        free_gb = free / (1024 ** 3)
        result["total_gb"] = round(total_gb, 1)
        result["free_gb"] = round(free_gb, 1)
        result["use_pct"] = round(used / total * 100, 1) if total else 0.0
        if free_gb < min_free_gb:
            result["ok"] = False
            result["warning"] = (
                f"磁盘压力高：{cache_dir} 仅剩 {free_gb:.1f}GB（阈值 {min_free_gb}GB），"
                "rclone 会按 --vfs-cache-min-free-space 自动清理缓存；"
                "如持续告警请调小 --vfs-cache-max-size 或扩容磁盘"
            )
    except Exception as e:  # noqa: BLE001
        result["warning"] = f"磁盘检测失败：{e}"
    return result


def build_rclone_mount_args(extra: Optional[Iterable[str]] = None,
                            cache_args: Optional[Iterable[str]] = None) -> list[str]:
    """合并默认优化参数与用户自定义参数，用户显式给的胜出。

    ``extra`` 形如 ``["--vfs-read-chunk-size", "64M", "--daemon"]``。
    ``cache_args`` 为缓存相关参数（见 ``build_rclone_cache_args``），
    不传则不含缓存配置（保持向后兼容）。
    返回去重后的完整参数列表（不含 ``rclone mount`` 本体与 remote/挂载点）。
    """
    merged: dict[str, str | None] = {}
    order: list[str] = []

    def _feed(argv: Iterable[str]) -> None:
        argv = list(argv)
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok.startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                key, val = tok, argv[i + 1]
                i += 2
            else:
                key, val = tok, None
                i += 1
            if key not in merged:
                order.append(key)
            merged[key] = val

    _feed(DEFAULT_RCLONE_VFS_ARGS)
    _feed(DEFAULT_RCLONE_MOUNT_FLAGS)
    if cache_args:
        _feed(cache_args)
    if extra:
        _feed(extra)

    out: list[str] = []
    for key in order:
        out.append(key)
        if merged[key] is not None:
            out.append(merged[key])
    return out


def build_rclone_mount_cmd(remote: str, mountpoint: str,
                           extra: Optional[Iterable[str]] = None,
                           bin_path: str = "rclone",
                           config: str = "",
                           cache_args: Optional[Iterable[str]] = None) -> list[str]:
    """拼出开箱即用的 ``rclone mount`` 完整命令（含优化参数）。"""
    cmd = [bin_path, "mount", remote, mountpoint]
    if config:
        cmd += ["--config", config]
    cmd += build_rclone_mount_args(extra, cache_args)
    return cmd


# ---------------------------------------------------------------------------
# 2. SA 自动轮换（应用层）
# ---------------------------------------------------------------------------
# Google Drive 单 SA 有下载配额，热门文件会被 403（downloadQuotaExceeded）。
#
# 历史：rclone 1.55+ 曾支持 service_account_file_path（目录）由 rclone 内核
# 自动轮换，但该选项在后续版本中被移除（v1.68/v1.75 实测已无此选项，
# 配置会被静默忽略，导致 "empty token found"）。
#
# 现方案（应用层轮换）：本函数每次调用时按带随机抖动的步长从 SA 目录
# 挑选一个 SA，写入 ``service_account_file``（单文件模式，rclone 一直支持）。
# 步长取 1..n 随机整数（反风控：避免完全规律的轮换模式）。
# 轮换位置持久化在配置文件同目录的 ``.sa_rotation.json``，重启/重部署后
# 继续轮换。调用时机：部署时、挂载前（不需要在播放中热切换）。
#
# 配额效果：N 个 SA 轮流承担挂载，长期看配额 ≈ × N。
SA_ROTATION_STATE_FILENAME = ".sa_rotation.json"


def _sa_state_path(conf_path: str | Path) -> Path:
    """轮换状态文件路径（与 rclone 配置同目录）。"""
    return Path(conf_path).parent / SA_ROTATION_STATE_FILENAME


def _next_sa_index(sa_files: list[Path], state_path: Path) -> int:
    """返回本次轮换选中的 SA 索引，并持久化。

    反风控：步长取 1..n 的随机整数（n = 文件数，至少 1；与
    drive_changes._sa_rotation_start 同口径），打破严格 +1 的完全规律循环。
    状态文件损坏/缺失时从 0 开始。索引对文件数取模，SA 增删后自动适应。
    """
    n = max(1, len(sa_files))
    idx = 0
    try:
        if state_path.is_file():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            idx = int(data.get("index", 0))
    except Exception:  # noqa: BLE001 — 状态损坏就从头开始，不影响主流程
        idx = 0
    idx = idx % n
    try:
        state_path.write_text(
            json.dumps({"index": idx + _random.randint(1, n), "file": sa_files[idx].name}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — 写失败只打日志，不阻断
        logger.warning("SA 轮换状态写入失败：%s", state_path)
    return idx


def _sa_json_files(sa_dir: str | Path) -> list[Path]:
    """SA 目录下的 *.json；顶层没有时看 ``sa/`` 子目录（与 drive_changes 同口径，
    生产 /opt/rclone-sa 的布局是 ``sa/*.json``）。"""
    p = Path(sa_dir)
    if not p.is_dir():
        return []
    files = sorted(f for f in p.glob("*.json") if f.is_file())
    if not files and (p / "sa").is_dir():
        files = sorted(f for f in (p / "sa").glob("*.json") if f.is_file())
    return files


#: 轮换是「读状态 → +1 → 写状态 → 改 rclone.conf」的读改写，并发调用
#: （管理端连点 / 部署脚本同时跑）会两次拿到同一个下标、或交错写坏配置
_SA_ROTATE_LOCK = threading.Lock()


def ensure_sa_rotation(conf_path: str | Path,
                       sa_dir: str | Path,
                       state_path: str | Path | None = None) -> dict:
    """让 rclone 配置用上 SA 轮换（幂等思想：每次调用推进一次轮换）。

    - SA 目录里有 >=1 个 json：按带随机抖动的步长挑选一个，写
      ``service_account_file = <选中文件>``（单文件模式，各版本 rclone 通用）。
    - SA 目录为空/不存在：不动，返回 warning。
    - 配置文件不存在：不动，返回 warning。

    ``state_path`` 可指定轮换状态文件位置（默认与配置同目录的
    ``.sa_rotation.json``），测试时可传入临时路径。

    返回 ``{"changed": bool, "needs_remount": bool, "mode": "rotated"|"single"|"none",
    "warning": str|None, "sa_file": str|None}``。
    ``needs_remount`` = 配置确实改了：本函数只改 rclone.conf，代码库没有应用内重挂
    rclone 挂载的能力，调用方必须把"需重新挂载/重启 rclone 才生效"告诉操作者。
    只改 [remote] 里原来就配了 service_account_* 的节，不碰 OAuth 的节。

    注意：rclone 已移除内核目录轮换（service_account_file_path），本函数
    改为应用层轮换；调用后需要重启挂载生效（SA 在挂载时加载）。
    """
    with _SA_ROTATE_LOCK:
        return _ensure_sa_rotation_locked(conf_path, sa_dir, state_path)


def _ensure_sa_rotation_locked(conf_path, sa_dir, state_path) -> dict:
    result: dict = {"changed": False, "needs_remount": False, "mode": "none",
                    "warning": None, "sa_file": None}
    conf = Path(conf_path)
    if not conf.is_file():
        result["warning"] = f"rclone 配置不存在：{conf}"
        return result

    sa_files = _sa_json_files(sa_dir)
    if not sa_files:
        result["warning"] = f"SA 目录为空或不存在：{sa_dir}，未启用轮换"
        return result

    st_path = Path(state_path) if state_path else _sa_state_path(conf)
    idx = _next_sa_index(sa_files, st_path)
    want = str(sa_files[idx].resolve())
    result["sa_file"] = want
    result["mode"] = "rotated" if len(sa_files) >= 2 else "single"

    parser = configparser.ConfigParser()
    parser.read(conf, encoding="utf-8")

    changed = False
    for section in parser.sections():
        has_single = parser.has_option(section, "service_account_file")
        has_dir = parser.has_option(section, "service_account_file_path")
        if not (has_single or has_dir):
            continue
        # 清理已废弃的目录模式配置（rclone 新版会静默忽略，导致 empty token）
        if has_dir:
            parser.remove_option(section, "service_account_file_path")
            changed = True
            result["warning"] = (
                "已清理废弃的 service_account_file_path（rclone 已移除内核目录轮换），"
                "改用应用层轮换"
            )
        if (not has_single) or parser.get(section, "service_account_file") != want:
            parser.set(section, "service_account_file", want)
            changed = True

    if changed:
        with open(conf, "w", encoding="utf-8") as fh:
            parser.write(fh)
    result["changed"] = changed
    result["needs_remount"] = changed
    return result


# ---------------------------------------------------------------------------
# 3. 单文件并发 Range 限流
# ---------------------------------------------------------------------------
# 播放器（尤其手机端）对一个大文件会开 5~7 个并发 Range 请求，7 路同时回源
# Drive 直接把带宽打满，谁都播不动。这里按文件限流：同一文件最多
# ``MAX_CONCURRENT_RANGES_PER_FILE`` 路并发，超出的直接 503（播放器会重试，
# 效果等同排队）。注意：事件循环里必须用非阻塞的 try_acquire。
MAX_CONCURRENT_RANGES_PER_FILE = 3
RANGE_ACQUIRE_TIMEOUT_SEC = 60.0


class RangeConcurrencyLimiter:
    """按文件路径限流的线程信号量池（streaming 的同步 generator 跑在线程池，
    这里必须用 threading 而非 asyncio）。"""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_RANGES_PER_FILE,
                 acquire_timeout: float = RANGE_ACQUIRE_TIMEOUT_SEC):
        self._max = max_concurrent
        self._timeout = acquire_timeout
        self._lock = threading.Lock()
        self._sems: dict[str, threading.Semaphore] = {}
        self._last_used: dict[str, float] = {}

    def _get(self, path: str) -> threading.Semaphore:
        now = time.monotonic()
        with self._lock:
            sem = self._sems.get(path)
            if sem is None:
                sem = threading.Semaphore(self._max)
                self._sems[path] = sem
            self._last_used[path] = now
            # 惰性清理：条目太多时清掉 1 小时没用过的，避免无限增长
            if len(self._sems) > 2000:
                cutoff = now - 3600
                stale = [k for k, ts in self._last_used.items() if ts < cutoff]
                for k in stale:
                    self._sems.pop(k, None)
                    self._last_used.pop(k, None)
            return sem

    def acquire(self, path: str) -> bool:
        """阻塞最多 ``acquire_timeout`` 秒拿一个名额，拿到返回 True。"""
        return self._get(path).acquire(timeout=self._timeout)

    def try_acquire(self, path: str) -> bool:
        """非阻塞拿名额，拿到返回 True，拿不到立即返回 False。

        在 async 事件循环里必须用这个（blocking 的 acquire 会卡住整个循环）。
        拿不到时调用方直接 503，播放器会重试，效果等同排队。"""
        return self._get(path).acquire(blocking=False)

    def release(self, path: str) -> None:
        sem = self._sems.get(path)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass  # 重复释放时忽略


# 全站单例：所有 worker 线程共享同一份限流状态
RANGE_LIMITER = RangeConcurrencyLimiter()


# ---------------------------------------------------------------------------
# 4. 大盘自动放大 VFS 缓存（分离架构 / 流节点）
# ---------------------------------------------------------------------------
# 磁盘总量 >= BIG_DISK_THRESHOLD_GB（默认 500GB）时，VFS 缓存自动给到
# 空闲空间的 CACHE_DISK_RATIO（默认 70%）：热门内容常驻本地，回源大幅减少。
# rclone VFS 缓存自带类 LRU 淘汰（--vfs-cache-max-age），冷数据自动腾地方，
# 热门优先保留——不需要我们再写一套淘汰。
# 小盘走默认 DEFAULT_SMALL_CACHE（20G），行为与以前一致。
BIG_DISK_THRESHOLD_GB = 500
CACHE_DISK_RATIO = 0.70
DEFAULT_SMALL_CACHE = "20G"


def auto_vfs_cache_size(cache_dir: str) -> str:
    """按磁盘大小自动决定 ``--vfs-cache-max-size``。

    返回如 ``"420G"`` / ``"20G"`` 的 rclone 可接受写法。
    检测失败（权限/路径不存在）时回退默认小盘值，绝不抛异常。
    """
    try:
        total, _used, free = shutil.disk_usage(cache_dir)
        total_gb = total // (1024 ** 3)
        if total_gb >= BIG_DISK_THRESHOLD_GB:
            size_gb = max(20, int((free // (1024 ** 3)) * CACHE_DISK_RATIO))
            logger.info("大盘自动缓存：磁盘 %dGB，VFS 缓存给到 %dGB",
                        total_gb, size_gb)
            return f"{size_gb}G"
    except Exception:  # noqa: BLE001 — 检测失败就走默认
        logger.warning("磁盘检测失败，用默认 VFS 缓存 %s", DEFAULT_SMALL_CACHE)
    return DEFAULT_SMALL_CACHE


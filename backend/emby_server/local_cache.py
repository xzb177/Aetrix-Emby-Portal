"""VPS 本地缓存（中转路径下的自动缓存层，2026-10 起不再是可选线路）

**定位**：把「热门」的远程挂载片子提前拉到 VPS 本机磁盘上，播放时自动优先
读本地副本，没命中才回源 —— 减少对云盘配额的依赖，拖动进度条也不再受
源站抖动影响。

- 配置落在 ``SystemConfig``（与播放策略 / CDN 同一套机制：热读短 TTL + 保存即失效），
  EM 与 EA 共用同一个库 → 后台改完两边同时生效；
- **默认关闭**（``local_cache_enabled=false``）：关闭时播放路径不发生任何变化，
  relay / cdn 两条线路的行为与升级前逐字节一致；
- 只缓存 ``mount://`` 远程挂载来源（115 / WebDAV / AList / STRM 直链）的条目：
  这些条目播放时要经过网络回源，副本才有意义；本机盘上的文件（含已用 rclone
  挂到本机目录的云盘）本来就由内核直接读，不需要副本，也不该悄悄复制一份；
- 下载由 ``local_cache_worker`` 单线程执行（一次只下一个）：
  - **限速**：默认 20MB/s（后台可改，0 = 不限），令牌桶按字节扣；有人在播放时
    自动降到 ``BUSY_RATE_MBPS``（默认 2MB/s）——让路，但不是完全停（不然一台
    整天有人在看的服务器永远缓存不上任何东西）；
  - 失败重试：``attempts`` 计数 + 上限，超限转 ``failed``（热门轮询会再给机会）；
- 淘汰：LRU，超配额按 ``last_accessed_at`` 从旧到新删（条目的 ``hits`` /
  ``last_accessed_at`` 由播放命中刷新）；
- 统计：占用 / 条目数 / 命中率（``emby_local_cache_stats`` 单行计数，跨重启存活），
  以及手动清理（按状态删条目与文件）。

播放路径只调用三个函数：``lookup``（命中读本机）、``enqueue``（未命中回源后入队）、
``PLAY_PRIORITY``。它们全部 try/except 兜底：缓存只是加速，任何异常都不能让播放失败。
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from typing import Iterator, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.integrations import store

logger = logging.getLogger(__name__)

# ==================== SystemConfig 键与默认值 ====================

CONFIG_ENABLED = "local_cache_enabled"
CONFIG_DIR = "local_cache_dir"
CONFIG_MAX_GB = "local_cache_max_gb"
CONFIG_HOT_DAYS = "local_cache_hot_days"
CONFIG_HOT_PLAYS = "local_cache_hot_plays"
CONFIG_RATE_MBPS = "local_cache_rate_mbps"

CONFIG_KEYS = (
    CONFIG_ENABLED, CONFIG_DIR, CONFIG_MAX_GB,
    CONFIG_HOT_DAYS, CONFIG_HOT_PLAYS, CONFIG_RATE_MBPS,
)

DEFAULT_MAX_GB = 500        # 最大占用 500GB（0 = 不限）
DEFAULT_HOT_DAYS = 7        # 热门判定窗口：近 7 天
DEFAULT_HOT_PLAYS = 3       # 窗口内播放达到 3 次即视为热门
DEFAULT_RATE_MBPS = 20      # 缓存下载限速 20MB/s（0 = 不限速）

MAX_GB_LIMIT = 100_000
MAX_HOT_DAYS = 90
MAX_HOT_PLAYS = 1_000
MAX_RATE_MBPS = 10_000

# 优先级（与探测 worker 的 BOOST_PRIORITY 同口径：越大越先下）
PLAY_PRIORITY = 1_000       # 用户正在点播：最高优先级
HOT_PRIORITY = 100          # 热门自动入队

MAX_ATTEMPTS = 3            # 下载失败重试上限（超限转 failed，等下一轮热门轮询再给机会）

MB = 1024 * 1024
DOWNLOAD_CHUNK = 512 * 1024      # 单次读写块（也是限速的扣费粒度）
BUSY_RATE_MBPS = 2.0             # 有人在播放时降到这个速度
ACTIVE_PLAYBACK_WINDOW_SEC = 300  # 多久内上报过进度算「正在播放」（客户端异常退出不会上报停止）
BUSY_CHECK_INTERVAL_SEC = 5.0     # 下载中检查「是否有人在播放」的间隔
EVICT_PROTECT_SEC = 60           # 刚访问过的副本不参与淘汰（避免删掉正在播的文件）
STAT_ROW = "global"

_CACHE_SUBDIR = "media_cache"
_PART_SUFFIX = ".part"


# ==================== 配置读取（热读，统一入口） ====================

def _raw(db: Session, key: str, default: str = "") -> str:
    return store.get_value(db, key, default)


def _flag(raw: str) -> bool:
    return (raw or "").strip().lower() in ("true", "1", "yes", "on")


def _as_int(raw: str, default: int, lo: int, hi: int) -> int:
    """脏配置一律回默认值：一个写错的数字绝不能把播放或 worker 打挂"""
    try:
        value = int(str(raw or "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def default_dir() -> str:
    """默认缓存目录：与 tmdb 缓存 / 字幕缓存同口径（转码目录下的子目录）"""
    return os.path.join(os.getenv("EMBY_TRANSCODE_DIR", "/tmp/emby_transcode"), _CACHE_SUBDIR)


def configured_dir(db: Session) -> str:
    """管理员配置的缓存目录（留空 = 默认目录）"""
    return (_raw(db, CONFIG_DIR) or "").strip() or default_dir()


def enabled(db: Session) -> bool:
    """本地缓存是否启用（默认关闭）"""
    return _flag(_raw(db, CONFIG_ENABLED, "false"))


def max_gb(db: Session) -> int:
    return _as_int(_raw(db, CONFIG_MAX_GB, str(DEFAULT_MAX_GB)), DEFAULT_MAX_GB, 0, MAX_GB_LIMIT)


def max_bytes(db: Session) -> int:
    """配额（字节）；0 = 不限（此时不做 LRU 淘汰）"""
    return max_gb(db) * 1024 ** 3


def hot_days(db: Session) -> int:
    return _as_int(_raw(db, CONFIG_HOT_DAYS, str(DEFAULT_HOT_DAYS)), DEFAULT_HOT_DAYS, 1, MAX_HOT_DAYS)


def hot_plays(db: Session) -> int:
    return _as_int(_raw(db, CONFIG_HOT_PLAYS, str(DEFAULT_HOT_PLAYS)), DEFAULT_HOT_PLAYS, 1, MAX_HOT_PLAYS)


def rate_mbps(db: Session) -> int:
    return _as_int(_raw(db, CONFIG_RATE_MBPS, str(DEFAULT_RATE_MBPS)), DEFAULT_RATE_MBPS, 0, MAX_RATE_MBPS)


def rate_bytes_per_sec(db: Session) -> int:
    """下载限速（字节/秒）；0 = 不限速"""
    return rate_mbps(db) * MB


def cache_dir(db: Session, create: bool = False) -> str:
    """本机缓存目录（create=True 时确保存在且可写，不可写抛 OSError）"""
    path = os.path.expanduser(configured_dir(db))
    if create and path:
        os.makedirs(path, exist_ok=True)
        # 目录存在≠可写（只读挂载时 makedirs 不报错，下载时才失败）。
        # 这里探一次，调用方已有 except OSError 兜底（下载任务记 failed）。
        probe = os.path.join(path, f".write_probe_{os.getpid()}")
        try:
            with open(probe, "w") as fh:
                fh.write("ok")
        except OSError as exc:
            raise OSError(f"缓存目录不可写：{path}（{exc}）") from exc
        try:
            os.unlink(probe)
        except OSError:
            pass
    return path


# ==================== 配置回显与写入（管理后台） ====================

def config_payload(db: Session) -> dict:
    """管理后台回显（不含任何敏感信息）"""
    return {
        "enabled": enabled(db),
        "dir": configured_dir(db),
        "default_dir": default_dir(),
        "max_gb": max_gb(db),
        "max_bytes": max_bytes(db),
        "hot_days": hot_days(db),
        "hot_plays": hot_plays(db),
        "rate_mbps": rate_mbps(db),
        "busy_rate_mbps": BUSY_RATE_MBPS,
        "active_playback_window_sec": ACTIVE_PLAYBACK_WINDOW_SEC,
        "max_attempts": MAX_ATTEMPTS,
    }


def _probe_writable(directory: str) -> None:
    """往目录里写一个临时文件再删掉，验证真的可写。

    只抛 ``ValueError``（调用方转 400）：目录存在但不可写时，makedirs 不会报错，
    不探这一下，保存会"成功"但下载时才失败。
    """
    probe = os.path.join(directory, f".write_probe_{os.getpid()}")
    try:
        with open(probe, "w") as fh:
            fh.write("ok")
    except OSError as exc:
        raise ValueError(f"缓存目录不可写：{directory}（{exc}）；请填一个有写权限的路径") from exc
    try:
        os.unlink(probe)
    except OSError:
        pass


def write_config(db: Session, *, enabled: bool, dir: str, max_gb_value: int,
                 hot_days_value: int, hot_plays_value: int, rate_mbps_value: int) -> dict:
    """写回配置（管理后台 PUT）。

    校验失败抛 ``ValueError``（路由转 400 并说清怎么改）；写入失败不留半个坏配置。
    保存即失效热缓存（EM/EA 同库，两边同时生效）。
    """
    dir_text = (dir or "").strip()
    if dir_text:
        dir_text = os.path.expanduser(dir_text)
        if not os.path.isabs(dir_text):
            raise ValueError("缓存目录要填绝对路径（例如 /mnt/cache/media）；留空则用「默认目录」")
        if os.path.exists(dir_text) and not os.path.isdir(dir_text):
            raise ValueError(f"缓存目录位置已存在同名文件：{dir_text}，请换一个目录")
        try:
            os.makedirs(dir_text, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"缓存目录建不出来：{dir_text}（{exc}）；请填一个有写权限的路径") from exc
        # 建出来还不够：目录可能存在但不可写（只读挂载、权限不足）。现在就探一次，
        # 写不进去直接 400 说清楚，别等下载时才失败让用户一头雾水。
        _probe_writable(dir_text)

    numbers = (
        ("最大占用（GB）", max_gb_value, 0, MAX_GB_LIMIT),
        ("热门判定窗口（天）", hot_days_value, 1, MAX_HOT_DAYS),
        ("热门判定播放次数", hot_plays_value, 1, MAX_HOT_PLAYS),
        ("下载限速（MB/s）", rate_mbps_value, 0, MAX_RATE_MBPS),
    )
    for label, value, lo, hi in numbers:
        if value is None:
            raise ValueError(f"{label}不能为空")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{label}要填整数")
        if value < lo or value > hi:
            raise ValueError(f"{label}要在 {lo} ~ {hi} 之间（收到 {value}）")

    store.write_values(db, {
        CONFIG_ENABLED: "true" if enabled else "false",
        CONFIG_DIR: dir_text,
        CONFIG_MAX_GB: str(int(max_gb_value)),
        CONFIG_HOT_DAYS: str(int(hot_days_value)),
        CONFIG_HOT_PLAYS: str(int(hot_plays_value)),
        CONFIG_RATE_MBPS: str(int(rate_mbps_value)),
    })
    db.commit()
    store.invalidate(*CONFIG_KEYS)
    return config_payload(db)


# ==================== 条目认定 ====================

def _guid_of(item) -> str:
    return str(getattr(item, "guid", "") or "").strip()


def cacheable(item) -> bool:
    """这条条目值不值得缓存：必须是远程挂载来源（mount://），本机文件不需要副本"""
    return bool(_guid_of(item)) and mount_lib.is_mount_path(getattr(item, "file_path", None))


def _source_snapshot(item) -> tuple[str, int]:
    return (str(getattr(item, "file_path", "") or ""), int(getattr(item, "size", 0) or 0))


def _entry_matches(entry: "em.LocalCacheEntry", item) -> bool:
    """本机副本是否仍然是这条条目的内容：路径与大小快照都要对得上

    换源（路径变了）或源文件大小变了（重压 / 换版本）→ 旧副本立刻失去意义，
    不当命中（否则播放器会拿到另一个版本的文件）。
    """
    path, size = _source_snapshot(item)
    if not path or (entry.source_path or "") != path:
        return False
    src_size = int(entry.source_size or 0)
    if src_size and size and src_size != size:
        return False
    dst = int(entry.file_size or 0)
    if dst and src_size and dst != src_size:
        return False
    return True


def _dest_path(directory: str, item) -> Optional[str]:
    """本机副本的落点：``<目录>/<guid><原扩展名>``；扩展名异常时不缓存"""
    ext = os.path.splitext(str(getattr(item, "file_path", "") or ""))[1].lower()
    if not ext or len(ext) > 10 or not ext[1:].isalnum():
        return None
    name = f"{_guid_of(item)}{ext}"
    try:
        return os.path.join(directory, name)
    except Exception:  # noqa: BLE001 — 目录异常按不可缓存处理
        return None


def _item_of(db: Session, entry: "em.LocalCacheEntry"):
    item = None
    if entry.item_id:
        item = db.query(em.MediaItem).filter(em.MediaItem.id == entry.item_id).first()
    if item is None:
        item = db.query(em.MediaItem).filter(em.MediaItem.guid == entry.item_guid).first()
    return item


# ==================== 统计计数（单行表，原子自增） ====================

def _bump_stat(db: Session, *, hit: bool) -> None:
    """命中 / 未命中计数 +1；失败只记日志（计数不该影响播放）"""
    column = "hits" if hit else "misses"
    try:
        rows = (
            db.query(em.LocalCacheStat)
            .filter(em.LocalCacheStat.name == STAT_ROW)
            .update({column: getattr(em.LocalCacheStat, column) + 1},
                    synchronize_session=False)
        )
        if not rows:
            db.add(em.LocalCacheStat(name=STAT_ROW,
                                     hits=1 if hit else 0, misses=0 if hit else 1))
        db.commit()
    except IntegrityError:
        # 并发下两个进程同时建行：回滚后重试一次自增
        db.rollback()
        try:
            db.query(em.LocalCacheStat).filter(em.LocalCacheStat.name == STAT_ROW).update(
                {column: getattr(em.LocalCacheStat, column) + 1}, synchronize_session=False)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    except Exception:  # noqa: BLE001
        db.rollback()


def stat_counts(db: Session) -> tuple[int, int]:
    row = db.query(em.LocalCacheStat).filter(em.LocalCacheStat.name == STAT_ROW).first()
    if row is None:
        return 0, 0
    return int(row.hits or 0), int(row.misses or 0)


def reset_stats(db: Session) -> None:
    """命中率统计清零（手动清理用：清理后重新观察水位才有意义）"""
    db.query(em.LocalCacheStat).filter(em.LocalCacheStat.name == STAT_ROW).update(
        {"hits": 0, "misses": 0}, synchronize_session=False)
    db.commit()


# ==================== 热门判定与入队 ====================

def hot_item_ids(db: Session, days: Optional[int] = None,
                 plays: Optional[int] = None, limit: int = 50) -> list[int]:
    """近 N 天播放达到 M 次的条目 id（只取远程挂载来源）"""
    days = hot_days(db) if days is None else days
    plays = hot_plays(db) if plays is None else plays
    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    rows = (
        db.query(em.PlaybackSession.item_id,
                 func.count(em.PlaybackSession.id).label("plays"))
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(
            em.PlaybackSession.start_time >= cutoff,
            em.PlaybackSession.item_id.isnot(None),
            em.MediaItem.file_path.like(f"{mount_lib.MOUNT_PATH_PREFIX}%"),
        )
        .group_by(em.PlaybackSession.item_id)
        .having(func.count(em.PlaybackSession.id) >= int(plays))
        .order_by(func.count(em.PlaybackSession.id).desc())
        .limit(max(1, int(limit)))
        .all()
    )
    return [int(item_id) for item_id, _plays in rows if item_id]


def enqueue(db: Session, item, priority: int = HOT_PRIORITY) -> Optional["em.LocalCacheEntry"]:
    """把条目排进缓存队列（已缓存则只刷新优先级）。

    - 未启用 / 不是远程来源 / 条目异常：返回 None，绝不抛给播放路径；
    - 已 ``ready`` 但源快照对不上：打回 ``pending`` 重新缓存；
    - 已 ``failed`` 且还有重试额度：给一次机会（回 pending）。

    整个函数体都在 try 里：入队只是加速，任何异常（含 db 异常）都不能传回播放路径。
    """
    if not cacheable(item):
        return None
    guid = _guid_of(item)
    now = datetime.now()
    try:
        if not enabled(db):
            return None
        entry = (db.query(em.LocalCacheEntry)
                 .filter(em.LocalCacheEntry.item_guid == guid).first())
        if entry is None:
            path, size = _source_snapshot(item)
            entry = em.LocalCacheEntry(
                item_guid=guid, item_id=getattr(item, "id", None),
                source_path=path, source_size=size, state="pending",
                priority=int(priority), created_at=now, updated_at=now,
            )
            db.add(entry)
            try:
                db.commit()
            except IntegrityError:
                # 并发下（播放 + 热门轮询）另一头先insert了：回滚后按已有行处理
                db.rollback()
                entry = (db.query(em.LocalCacheEntry)
                         .filter(em.LocalCacheEntry.item_guid == guid).first())
                if entry is None:
                    return None
                # 重新加载：并发插入的行不在本会话
                db.refresh(entry)
        if entry.state == "ready":
            if _entry_matches(entry, item):
                entry.priority = max(int(entry.priority or 0), int(priority))
                db.commit()
                return entry
            _invalidate(db, entry, "源文件已变化，重新缓存")
            entry.priority = max(int(entry.priority or 0), int(priority))
            db.commit()
            return entry
        if entry.state == "failed" and int(entry.attempts or 0) < MAX_ATTEMPTS:
            entry.state = "pending"
            entry.last_error = None
        path, size = _source_snapshot(item)
        entry.source_path = path
        entry.source_size = size
        entry.item_id = getattr(item, "id", None) or entry.item_id
        entry.priority = max(int(entry.priority or 0), int(priority))
        entry.updated_at = now
        db.commit()
        return entry
    except Exception:  # noqa: BLE001 — 入队失败只是不缓存，不能影响播放
        db.rollback()
        logger.warning("本地缓存入队失败（忽略）: %s", guid, exc_info=True)
        return None


def _invalidate(db: Session, entry: "em.LocalCacheEntry", reason: str) -> None:
    """把一条已失效的副本打回 pending（文件留着由下次下载覆盖）"""
    entry.state = "pending" if int(entry.attempts or 0) < MAX_ATTEMPTS else "failed"
    entry.last_error = reason[:500]
    entry.file_path = None
    entry.file_size = 0
    entry.updated_at = datetime.now()


def enqueue_hot(db: Session, limit: int = 5) -> dict:
    """把热门条目排进队列（每轮最多 limit 条）。返回计数（可测试）。"""
    counts = {"queued": 0, "skipped": 0, "hot": 0}
    if not enabled(db):
        return counts
    try:
        ids = hot_item_ids(db, limit=max(1, int(limit)) * 5)
    except Exception:  # noqa: BLE001
        logger.warning("本地缓存热门查询失败", exc_info=True)
        return counts
    counts["hot"] = len(ids)
    for item_id in ids:
        if counts["queued"] >= limit:
            break
        item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        if item is None:
            continue
        entry = enqueue(db, item, priority=HOT_PRIORITY)
        if entry is None:
            counts["skipped"] += 1
        elif entry.state in ("pending", "downloading"):
            counts["queued"] += 1
        else:
            counts["skipped"] += 1
    return counts


# ==================== 播放路径：命中查询 ====================

def lookup(db: Session, item) -> Optional[str]:
    """查询本机副本；命中返回文件路径，未命中返回 None（并入未命中计数）。

    只应在播放线路 = cache 时调用（它会把这次取用计入命中率）。任何异常都按
    未命中处理：缓存只是加速，不能让播放 500。
    """
    guid = _guid_of(item)
    if not guid:
        return None
    try:
        if not enabled(db) or not cacheable(item):
            return None
        entry = (db.query(em.LocalCacheEntry)
                 .filter(em.LocalCacheEntry.item_guid == guid).first())
        path = None
        if entry is not None and entry.state == "ready" and _entry_matches(entry, item):
            candidate = entry.file_path or ""
            if candidate and os.path.isfile(candidate):
                path = candidate
        if path:
            entry.hits = int(entry.hits or 0) + 1
            entry.last_accessed_at = datetime.now()
            entry.updated_at = entry.last_accessed_at
            db.commit()
            _bump_stat(db, hit=True)
            return path
        if entry is not None and entry.state == "ready":
            # 文件被删 / 源变了：副本已失效，打回队列重新缓存
            _invalidate(db, entry, "本地副本已失效（源文件变化或文件被删除）")
            db.commit()
        _bump_stat(db, hit=False)
        return None
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("本地缓存命中查询失败（按未命中处理）", exc_info=True)
        return None


# ==================== 下载（worker 调用，阻塞、限速） ====================

class _RemoteError(RuntimeError):
    """下载过程中的远端错误（源站 4xx/5xx、下载不完整等）"""


class _ByteLimiter:
    """按字节限速的令牌桶（rate<=0 = 不限速）"""

    def __init__(self, rate_bytes_per_sec: int):
        self._lock = threading.Lock()
        self._rate = max(0, int(rate_bytes_per_sec))
        self._tokens = float(self._rate)
        self._updated = time.monotonic()

    def set_rate(self, rate_bytes_per_sec: int) -> None:
        with self._lock:
            self._rate = max(0, int(rate_bytes_per_sec))
            self._updated = time.monotonic()
            self._tokens = min(self._tokens, float(self._rate))

    def take(self, nbytes: int) -> None:
        """扣掉 nbytes 的额度，不够就睡够为止

        桶容量取 ``max(rate, nbytes)``：容量若死板地等于 rate，一次取比 rate 还大的
        块（调低限速 + 大块）就永远凑不齐额度，下载线程会在这里空转到天荒地老。
        """
        if self._rate <= 0 or nbytes <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                cap = max(float(self._rate), float(nbytes))
                self._tokens = min(cap,
                                   self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= nbytes:
                    self._tokens -= nbytes
                    return
                wait = (nbytes - self._tokens) / self._rate
            time.sleep(min(max(wait, 0.01), 0.5))


def playback_busy(db: Session) -> bool:
    """现在有人在播放吗（近期上报过进度且未结束的会话）

    播放让路的判据。客户端异常退出不会上报 Stopped，所以只认「窗口内还在上报」
    的会话（``last_update_at`` 由客户端进度上报刷新）。
    """
    cutoff = datetime.now() - timedelta(seconds=ACTIVE_PLAYBACK_WINDOW_SEC)
    try:
        return bool(
            db.query(em.PlaybackSession.id)
            .filter(em.PlaybackSession.ended_at.is_(None),
                    em.PlaybackSession.last_update_at >= cutoff)
            .first()
        )
    except Exception:  # noqa: BLE001 — 判定失败按「有人在看」处理（保守）
        logger.warning("本地缓存：播放态判定失败，按让路处理", exc_info=True)
        return True


def _open_remote_stream(url: str, headers: Optional[dict], offset: int):
    """打开远程媒体流：返回 (字节迭代器, 远端总大小或 None, 实际起始偏移)

    - ``offset > 0`` 时带 ``Range`` 续传；源站不支持（回 200）则从头写（起始偏移 0）；
    - 重定向自己追、逐跳校验目标并跨主机剥凭据——与 ``streaming.serve_remote``
      同一套口径（下载器也不能被一个公开直链带去内网）。
    """
    import httpx

    from backend.emby_server.streaming import (
        MAX_REDIRECTS,
        REMOTE_UA,
        _next_redirect,
        _redirect_headers,
    )

    forward = {k: v for k, v in (headers or {}).items()}
    forward.setdefault("User-Agent", REMOTE_UA)
    if offset > 0:
        forward["Range"] = f"bytes={offset}-"
    client = httpx.Client(
        # P1 修复（审查）：此前 read=None 禁用读超时，源站中途卡住（不发送也不断开）
        # 时 iter_bytes 永久阻塞，单线程 worker 整个缓存管线停摆。现设块间空闲上限 60s。
        timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=False)
    resp = None
    try:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            resp = client.send(client.build_request("GET", current, headers=forward), stream=True)
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            location = resp.headers.get("location")
            if not location:
                break
            target = _next_redirect(current, location)
            resp.close()
            forward = _redirect_headers(forward, current, target)
            current = target
        else:
            raise _RemoteError("源站重定向次数过多")
        if resp.status_code >= 400:
            code = resp.status_code
            resp.close()
            client.close()
            raise _RemoteError(f"源站返回 {code}")
        total = None
        content_range = resp.headers.get("content-range") or ""
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                total = int(tail)
        elif resp.status_code == 200:
            length = (resp.headers.get("content-length") or "").strip()
            if length.isdigit():
                total = int(length)
        start_offset = offset if resp.status_code == 206 else 0
    except _RemoteError:
        # 与 streaming.serve_remote（#584/#587）同口径：先关可能仍打开的流式响应再关 client
        if resp is not None:
            resp.close()
        client.close()
        raise
    except Exception as exc:  # noqa: BLE001 — 源站不可达：给出可读原因
        if resp is not None:
            resp.close()
        client.close()
        raise _RemoteError(f"源站不可达：{exc}") from exc

    def iterator() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_bytes(DOWNLOAD_CHUNK):
                if chunk:
                    yield chunk
        finally:
            try:
                resp.close()
            finally:
                client.close()

    return iterator(), total, start_offset


def _download_to_file(db: Session, entry: "em.LocalCacheEntry", item,
                      url: str, headers: Optional[dict], part_path: str) -> int:
    """把远端内容写到 ``part_path``（支持断点续传 + 限速 + 播放让路），返回字节数"""
    base_rate = rate_bytes_per_sec(db)
    busy_rate = int(min(BUSY_RATE_MBPS * MB, base_rate)) if base_rate > 0 else 0
    limiter = _ByteLimiter(base_rate)
    offset = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
    stream, total, start_offset = _open_remote_stream(url, headers, offset)
    mode = "ab" if offset > 0 and start_offset > 0 else "wb"
    written = 0 if mode == "wb" else os.path.getsize(part_path)
    last_check = 0.0
    busy = False
    with open(part_path, mode) as handle:
        for chunk in stream:
            handle.write(chunk)
            written += len(chunk)
            limiter.take(len(chunk))
            now = time.monotonic()
            if busy_rate and now - last_check >= BUSY_CHECK_INTERVAL_SEC:
                last_check = now
                was_busy = busy
                busy = playback_busy(db)
                limiter.set_rate(busy_rate if busy else base_rate)
                if busy and not was_busy:
                    logger.info("本地缓存：检测到播放中的会话，下载降到 %.1fMB/s 让路", BUSY_RATE_MBPS)
    size = os.path.getsize(part_path)
    expected = int(getattr(item, "size", 0) or 0)
    if total is not None and size != int(total):
        raise _RemoteError(f"下载不完整（{size}/{total} 字节）")
    if expected > 0 and size != expected:
        raise _RemoteError(f"下载大小与条目不一致（{size}/{expected} 字节），可能源文件正在变化")
    return size


def download_entry(db: Session, entry_id: int) -> str:
    """下载一条缓存（阻塞；一次只该被一个 worker 调用）。

    返回 ``ready`` / ``failed`` / ``skipped`` / ``disabled``（可测试）。
    """
    entry = (db.query(em.LocalCacheEntry)
             .filter(em.LocalCacheEntry.id == entry_id).first())
    if entry is None:
        return "skipped"
    if entry.state not in ("pending", "downloading"):
        return "skipped"
    if not enabled(db):
        return "disabled"
    item = _item_of(db, entry)
    if item is None or not cacheable(item):
        _fail(db, entry, "条目不存在或不是远程挂载来源")
        return "failed"
    try:
        target = mount_lib.resolve_play_target(item.file_path, db, getattr(item, "library", None))
    except Exception as exc:  # noqa: BLE001 — 来源不可用：重试额度内下一轮再来
        _fail(db, entry, f"解析媒体来源失败：{exc}")
        return "failed"
    if target.kind != "url":
        _fail(db, entry, "该条目是本机文件，无需缓存")
        return "skipped"
    try:
        directory = cache_dir(db, create=True)
    except OSError as exc:
        _fail(db, entry, f"缓存目录不可用：{exc}")
        return "failed"
    dest = _dest_path(directory, item)
    if not dest:
        _fail(db, entry, "无法生成本机副本路径（扩展名异常）")
        return "skipped"
    part_path = dest + _PART_SUFFIX
    entry.state = "downloading"
    entry.attempts = int(entry.attempts or 0) + 1
    entry.source_path = str(item.file_path or "")
    entry.source_size = int(getattr(item, "size", 0) or 0)
    entry.file_path = dest
    entry.updated_at = datetime.now()
    db.commit()
    try:
        size = _download_to_file(db, entry, item, target.value, target.headers, part_path)
        os.replace(part_path, dest)
        now = datetime.now()
        entry.state = "ready"
        entry.file_path = dest
        entry.file_size = int(size)
        entry.cached_at = now
        entry.last_accessed_at = now
        entry.last_error = None
        entry.priority = 0
        entry.updated_at = now
        db.commit()
        evicted = evict(db)
        logger.info("本地缓存：%s 已缓存到本机（%.1fMB）%s", item.name, size / MB,
                    f"，淘汰 {evicted['removed']} 条旧记录" if evicted["removed"] else "")
        return "ready"
    except Exception as exc:  # noqa: BLE001 — 下载失败按重试额度回队列
        part_bytes = os.path.getsize(part_path) if os.path.isfile(part_path) else 0
        entry.file_path = None
        entry.file_size = 0
        _fail(db, entry, f"{exc}")
        logger.warning("本地缓存：%s 下载失败（第 %s 次，已下 %.1fMB）：%s",
                       getattr(item, "name", entry.item_guid), entry.attempts,
                       part_bytes / MB, exc)
        return "failed"


def _fail(db: Session, entry: "em.LocalCacheEntry", reason: str) -> None:
    """记一次失败：额度内回 pending（下一轮重试），超限转 failed"""
    attempts = int(entry.attempts or 0)
    entry.state = "pending" if attempts < MAX_ATTEMPTS else "failed"
    entry.last_error = str(reason)[:500]
    entry.updated_at = datetime.now()
    db.commit()


def claim_next(db: Session) -> Optional[int]:
    """取一条待下载的缓存（优先级高的先下，同优先级按入队时间）"""
    row = (
        db.query(em.LocalCacheEntry.id)
        .filter(em.LocalCacheEntry.state.in_(("pending", "downloading")))
        .order_by(em.LocalCacheEntry.priority.desc(),
                  em.LocalCacheEntry.created_at.asc(),
                  em.LocalCacheEntry.id.asc())
        .first()
    )
    return int(row[0]) if row else None


# ==================== LRU 淘汰 ====================

def ready_bytes(db: Session) -> int:
    """本机副本占用（按记录的 ``file_size`` 汇总）"""
    total = (db.query(func.coalesce(func.sum(em.LocalCacheEntry.file_size), 0))
             .filter(em.LocalCacheEntry.state == "ready").scalar())
    return int(total or 0)


def evict(db: Session) -> dict:
    """超配额时按 LRU 删最久未访问的副本（文件 + 记录）。

    - 只淘汰 ``ready`` 的条目：``pending`` / ``downloading`` 的产物还没算进占用
      （半成品文件由下载失败的重试覆盖）；
    - 近 ``EVICT_PROTECT_SEC`` 秒内被访问过的副本不删（正在播放的文件每段请求
      都会刷新 ``last_accessed_at``，正常轮不到它被淘汰——这条是额外保险，
      避免「命中 → 删除」的毫秒级竞态把正在播的片子断掉）；
    - 配额 0 = 不限，直接返回。
    """
    quota = max_bytes(db)
    counts = {"removed": 0, "freed_bytes": 0, "quota_bytes": quota, "used_bytes": 0}
    if quota <= 0:
        counts["used_bytes"] = ready_bytes(db)
        return counts
    used = ready_bytes(db)
    counts["used_bytes"] = used
    if used <= quota:
        return counts
    protect_before = datetime.now() - timedelta(seconds=EVICT_PROTECT_SEC)
    rows = (
        db.query(em.LocalCacheEntry)
        .filter(em.LocalCacheEntry.state == "ready")
        .order_by(func.coalesce(em.LocalCacheEntry.last_accessed_at,
                                em.LocalCacheEntry.cached_at,
                                em.LocalCacheEntry.created_at).asc(),
                  em.LocalCacheEntry.id.asc())
        .all()
    )
    for entry in rows:
        if used <= quota:
            break
        if (entry.last_accessed_at or entry.cached_at or datetime.min) >= protect_before:
            continue  # 刚访问过（可能正在播）：不删
        freed = int(entry.file_size or 0)
        remove_entry(db, entry, delete_files=True, commit=False)
        used = max(0, used - freed)
        counts["removed"] += 1
        counts["freed_bytes"] += freed
    db.commit()
    counts["used_bytes"] = ready_bytes(db)
    if counts["removed"]:
        logger.info("本地缓存：超配额（%d/%d 字节），按 LRU 淘汰 %d 条，释放 %.1fGB",
                    counts["used_bytes"], quota, counts["removed"], counts["freed_bytes"] / 1024 ** 3)
    return counts


def remove_entry(db: Session, entry: "em.LocalCacheEntry", *,
                 delete_files: bool = True, commit: bool = True) -> int:
    """删掉一条缓存记录（可选删本机文件）；返回释放的字节数"""
    freed = 0
    if delete_files:
        candidates = []
        if entry.file_path:
            candidates.append(entry.file_path)
        if entry.item_guid and entry.file_path:
            candidates.append(str(entry.file_path) + _PART_SUFFIX)
        for path in candidates:
            try:
                if path and os.path.isfile(path):
                    freed += os.path.getsize(path)
                    os.remove(path)
            except OSError as exc:  # 删不掉（权限 / 正在被读）也不留坏记录
                logger.warning("本地缓存：删除文件失败 %s: %s", path, exc)
    db.delete(entry)
    if commit:
        db.commit()
    return freed


# ==================== 命中率 / 占用 / 手动清理 ====================

def _disk_usage(directory: str) -> tuple[int, int]:
    """缓存目录所在磁盘的 (总容量, 剩余)；目录不存在时看它的父目录"""
    probe = directory
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe.rstrip("/")) or "/"
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe)
        return int(usage.total), int(usage.free)
    except OSError:
        return 0, 0


def stats(db: Session) -> dict:
    """缓存占用 / 条目数 / 命中率（管理后台用）"""
    rows = (
        db.query(em.LocalCacheEntry.state, func.count(em.LocalCacheEntry.id))
        .group_by(em.LocalCacheEntry.state).all()
    )
    by_state = {str(state or ""): int(count) for state, count in rows}
    used = ready_bytes(db)
    quota = max_bytes(db)
    hits, misses = stat_counts(db)
    total_lookups = hits + misses
    directory = configured_dir(db)
    disk_total, disk_free = _disk_usage(directory)
    part_bytes = 0
    for (path,) in (db.query(em.LocalCacheEntry.file_path)
                    .filter(em.LocalCacheEntry.state == "downloading").all()):
        if path and os.path.isfile(str(path) + _PART_SUFFIX):
            try:
                part_bytes += os.path.getsize(str(path) + _PART_SUFFIX)
            except OSError:
                pass
    return {
        "enabled": enabled(db),
        "dir": directory,
        "dir_exists": os.path.isdir(directory),
        "bytes_used": used,
        "part_bytes": part_bytes,
        "max_bytes": quota,
        "used_ratio": round(used / quota, 4) if quota > 0 else 0.0,
        "entries": by_state,
        "entries_total": sum(by_state.values()),
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / total_lookups, 4) if total_lookups else None,
        "disk_total_bytes": disk_total,
        "disk_free_bytes": disk_free,
        "hot_days": hot_days(db),
        "hot_plays": hot_plays(db),
        "rate_mbps": rate_mbps(db),
    }


def entry_list(db: Session, limit: int = 100) -> list[dict]:
    """缓存条目列表（按状态 + 最近访问，管理后台展示用）"""
    rows = (
        db.query(em.LocalCacheEntry, em.MediaItem.name, em.MediaItem.file_path)
        .outerjoin(em.MediaItem, em.MediaItem.id == em.LocalCacheEntry.item_id)
        .order_by(em.LocalCacheEntry.state.asc(),
                  func.coalesce(em.LocalCacheEntry.last_accessed_at,
                                em.LocalCacheEntry.updated_at).desc())
        .limit(max(1, int(limit)))
        .all()
    )
    out = []
    for entry, name, file_path in rows:
        out.append({
            "item_guid": entry.item_guid,
            "item_id": entry.item_id,
            "name": name or "",
            "source_path": entry.source_path or file_path or "",
            "state": entry.state,
            "file_size": int(entry.file_size or 0),
            "source_size": int(entry.source_size or 0),
            "hits": int(entry.hits or 0),
            "priority": int(entry.priority or 0),
            "attempts": int(entry.attempts or 0),
            "last_error": entry.last_error or "",
            "cached_at": entry.cached_at.isoformat() if entry.cached_at else None,
            "last_accessed_at": (entry.last_accessed_at.isoformat()
                                 if entry.last_accessed_at else None),
        })
    return out


def clean(db: Session, mode: str = "ready") -> dict:
    """手动清理。

    - ``ready``：删掉所有本机副本（释放空间，记录一并删除）；
    - ``failed``：只清失败记录（不动可用副本）；
    - ``all``：ready + failed + pending（``downloading`` 的留给 worker 收尾，
      并发删一条正在写的记录会让下载线程回写失败）。
    """
    mode = (mode or "ready").strip().lower()
    if mode not in ("ready", "failed", "all"):
        raise ValueError("mode 只能是 ready / failed / all 之一")
    wanted = {
        "ready": ("ready",),
        "failed": ("failed",),
        "all": ("ready", "failed", "pending"),
    }[mode]
    rows = (db.query(em.LocalCacheEntry)
            .filter(em.LocalCacheEntry.state.in_(wanted)).all())
    freed = 0
    for entry in rows:
        freed += remove_entry(db, entry, delete_files=True, commit=False)
    db.commit()
    counts = {"mode": mode, "removed": len(rows), "freed_bytes": freed}
    logger.info("本地缓存：手动清理（%s）删除 %d 条，释放 %.2fGB",
                mode, counts["removed"], freed / 1024 ** 3)
    return counts

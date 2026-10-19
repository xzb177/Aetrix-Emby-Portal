"""本机媒体目录实时监听（inotify）：新片入库不用等定时扫描

为什么做这个：以前只有一个**轮询**版（``change_watcher``，每 N 分钟列一次目录）。
本地挂载（/mnt/...）改成宿主机直接映射之后，文件一落地就有 inotify 事件，
等下一轮轮询（分钟级）纯属白等。

## 设计取舍

- **只管入队，不管扫描**：监听到事件只做一件事——给对应媒体库入队一轮**增量**扫描
  （``trigger="fs-event"``）。真正的遍历/入库还是走扫描器，它已经会按目录/文件指纹
  秒跳。监听线程因此永远不做重活，也就不会抢播放与 API 的资源。
- **1 秒防抖 + 按库合并**：转场一个目录（复制一批剧集）会产生成百上千个事件，
  逐个处理等于把磁盘读烂。这里按「库」聚合，1 秒内的所有事件只触发**一次**入队；
  入队本身对同一库是幂等的（已在队列里就返回已有任务），所以重复触发天然安全。
  同库 60 秒内只入队一轮：窗口内后到的事件**延迟**到窗口结束补发一轮，而不是丢弃
  （增量扫描是整目录枚举，补发的那一轮把窗口内所有事件一起覆盖）。
- **独立线程 + 事件回调只做记账**：watchdog 的回调线程只把 (库 id → 计数) 记进字典，
  真正的入队放在自己的线程里按防抖窗口做，不碰数据库连接。
- **降级而不是失败**：路径不可读 / watch 数超上限 / inotify 实例耗尽时，把该库记成
  ``degraded`` 并给出人话原因，界面能看到；此时该库退回轮询（定时扫描照跑），
  不会少入库，只是慢一点。监听**永远不能影响扫描/播放/接口**。
- **容器重启自动恢复**：``start_fs_watcher()`` 在应用 lifespan 里调（与追新线程同一处），
  从数据库读「当前所有启用库的本地路径」重建监听，不写死任何路径。
- **启动不等同步**：``start_fs_watcher()`` 只起 observer + 防抖线程就立即返回，
  初始的 ``sync_from_db()`` 在后台线程里做。recursive 监听在大目录树
  （/strm 8.5 万文件）下要几十分钟，同步等它会把 worker 主线程卡死、
  后面的 Redis 扫描队列消费线程永远起不来。后台建，建完即用。
- **大树直接降级**：单个路径子目录超过 ``FS_WATCH_MAX_SUBDIRS``（默认 2000，
  环境变量可调）就不做 recursive 监听，记成 degraded 走定时扫描——
  建 watch 本身在这种规模下就是灾难，还会吃光 inotify 上限。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("aetrix.fs_watcher")

#: 事件防抖窗口（秒）：这之内同一媒体库的多个事件只触发一次入队
FS_EVENT_DEBOUNCE_SEC = 1.0
#: 一次合并处理里最多触发多少个库（其余下一轮再处理；转场大目录时不让一轮就把
#: 扫描队列灌满——待处理集合不会丢，下一轮接着出）
FS_EVENT_MAX_TRIGGERS = 8
#: 全局最小入队间隔（秒）：防抖窗口之上的第二道闸——磁盘抖动导致事件断断续续时，
#: 不会把扫描队列灌满（同库入队本就幂等，但队列长度会虚高、界面一直在闪）。
#: 窗口内被合并的事件记入 ``_RETRY_AT``，窗口结束后由 drain 线程补发一轮——
#: 合并是延迟，不是丢弃。
FS_EVENT_MIN_INTERVAL_SEC = 60.0
#: 同时监听的最大目录数（watchdog/inotify 每个目录占一个 watch，实例数有限）
FS_WATCH_MAX_DIRS = 4000
#: 单个监听路径的子目录预算：超过就降级为定时扫描。
#: watchdog 的 recursive=True 会给**每个子目录**建一个 inotify watch；/strm 这种
#: 8.5 万文件的大树建 watch 本身就要几十分钟，还可能吃光 max_user_watches。
#: 预算内快速建，超预算直接降级（理由会写进 _DEGRADED，界面看得见）。
#: 非法值回落默认。
try:
    FS_WATCH_MAX_SUBDIRS = max(100, int(
        (os.getenv("FS_WATCH_MAX_SUBDIRS") or "2000").strip() or 2000))
except ValueError:
    FS_WATCH_MAX_SUBDIRS = 2000
#: 周期性对账间隔（秒）：inotify 会漏事件（watch 上限 / 容器重建 / 目录被换掉），
#: 定期与数据库对一次差才能发现。**只重建监听集合，不遍历目录、不列文件**，
#: 所以它不会退化成周期性全量读取。
#: 非法值回落默认（写错一个环境变量不应该让整个服务起不来）。
try:
    RECONCILE_INTERVAL_SEC = max(60, int(
        (os.getenv("FS_RECONCILE_INTERVAL_SEC") or "900").strip() or 900))
except ValueError:
    RECONCILE_INTERVAL_SEC = 900

try:  # watchdog 是可选依赖：装不上时整个模块退化为「不监听」，不影响任何其他功能
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    WATCHDOG_AVAILABLE = True
except Exception as _watchdog_import_error:  # noqa: BLE001
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]
    WATCHDOG_AVAILABLE = False
    logger.warning("未安装 watchdog，本机目录实时监听不可用（已降级为定时扫描）：%s",
                   _watchdog_import_error)

_STATE_LOCK = threading.RLock()
#: 库 id → 该库本机路径列表（监听与状态查询共用这一份）
_LIBRARY_PATHS: dict[int, tuple[str, ...]] = {}
#: 库 id → 降级原因（空串 = 正常监听中）
_DEGRADED: dict[int, str] = {}
#: 库 id → {路径: watchdog watch 句柄}
_WATCHES: dict[int, dict] = {}
#: 库 id → 最近一次入队时间（限流用）
_LAST_TRIGGER: dict[int, float] = {}
#: 待处理事件：库 id → 最近一次事件时刻
_PENDING: dict[int, float] = {}
#: 延迟补发点：库 id → 最早允许补触发的时刻。合并窗口内被合并的事件不再丢弃，
#: 而是记在这里，窗口结束后由 drain 线程补发一轮增量扫描（合并=延迟，不是丢弃）。
_RETRY_AT: dict[int, float] = {}

#: 最近一次对账的结果（状态接口回显用）：at=单调时刻, changed=是否有差异
_LAST_RECONCILE: dict = {"at": 0.0, "changed": None}

_OBSERVER = None
_THREAD: Optional[threading.Thread] = None
_STOP: Optional[threading.Event] = None
#: 初始同步线程（start_fs_watcher 里起，sync_from_db 在它里面跑）
_SYNC_THREAD: Optional[threading.Thread] = None
#: 同步串行锁：初始后台同步和对账线程都调 sync_from_db，不能重叠跑
#: （重叠会导致 _WATCHES 被两个线程同时 unschedule/schedule）。
#: 持有它的都是后台线程，阻塞等待是安全的。
_SYNC_LOCK = threading.Lock()
_STATS = {"events": 0, "triggers": 0, "coalesced": 0, "errors": 0}


def _now() -> float:
    return time.monotonic()


# ==================== 目标路径计算（只算本机路径，不写死）====================

def local_paths_for_library(library) -> tuple[str, ...]:
    """这个库要监听哪些**本机目录**

    从媒体库配置动态算：``paths`` 里的绝对路径 + 绑定的 local 挂载根。
    远程挂载（``mount://`` / ``115:/`` / ``rclone:``）拿不到可靠的事件源，**不监听**
    ——它们继续由轮询追新兜底。
    """
    from backend.emby_server import mounts as mount_lib

    out: list[str] = []
    for raw in mount_lib.split_library_paths(getattr(library, "paths", "")):
        if mount_lib.parse_mount_path(raw) is not None:
            continue                       # mount://<id>/... = 远程挂载子目录
        if raw.startswith("/"):
            out.append(raw.rstrip("/") or "/")
    return tuple(dict.fromkeys(out))


#: FUSE 文件系统类型：这些挂载上**不能**挂 inotify 监听。
#: rclone mount / sshfs / ntfs-3g 都能通过 ``isdir`` + ``access`` 检查，但 inotify
#: 在它们上面要么根本收不到事件，要么在遍历时把整个挂载点拖死（内核线程卡在 FUSE
#: 用户态守护进程上）。所以必须**按文件系统类型**判，不能只看路径存不存在。
FUSE_FSTYPES = frozenset({
    "fuse", "fuseblk", "fuse.sshfs", "fuse.rclone", "fuse.rclonefs", "fuse.glusterfs",
    "fuse.portal", "rclone", "sshfs", "ntfs-3g", "ntfs3", "exfat", "cifs", "smb3",
    "afpfs", "davfs", "gfs2", "ceph", "9p", "virtiofs",
})

#: 挂载表缓存：``/proc/self/mountinfo`` 解析一次几毫秒，而 ``is_watchable`` 每建一个
#: watch 就要调一次。挂载很少变，缓存 60s 足够；容器里重新挂载后最多一分钟内跟上。
_MOUNTINFO_TTL_SEC = 60.0
_mountinfo_cache: tuple[float, list[tuple[str, str]]] | None = None


def _read_mount_table() -> list[tuple[str, str]]:
    """``[(挂载点, 文件系统类型), ...]``，按挂载点长度倒序（最长前缀优先匹配）

    读 ``/proc/self/mountinfo``（Debian 上稳定存在）；读不到就返回空表并让调用方
    **继续按老路走**——拿不到信息不等于“不能监听”，不能因为这个把正常本机目录全降级。
    """
    global _mountinfo_cache
    now = time.monotonic()
    cached = _mountinfo_cache
    if cached is not None and now - cached[0] < _MOUNTINFO_TTL_SEC:
        return cached[1]
    rows: list[tuple[str, str]] = []
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # mountinfo: id parent maj:min root mount_point options... - fstype source super_options
                try:
                    left, sep, right = line.partition(" - ")
                    if not sep:
                        continue
                    fstype = right.split(" ", 1)[0].strip()
                    mount_point = left.split(" ")[4]
                except (IndexError, ValueError):
                    continue
                # 内核把 \040 之类的转义留在挂载点里，解回来才能和真实路径前缀对上
                mount_point = (
                    mount_point.replace("\\040", " ")
                    .replace("\\011", "\t")
                    .replace("\\012", "\n")
                    .replace("\\134", "\\")
                )
                rows.append((mount_point.rstrip("/") or "/", fstype.lower()))
    except OSError as exc:
        logger.debug("读 /proc/self/mountinfo 失败（按「无 FUSE 信息」继续）: %s", exc)
        rows = []
    rows.sort(key=lambda item: len(item[0]), reverse=True)
    _mountinfo_cache = (now, rows)
    return rows


def fstype_of(path: str) -> str:
    """这个路径所在文件系统的类型；认不出来就返回空串（调用方据此**不要**判成 FUSE）"""
    best = ""
    best_len = 0
    for mount_point, fstype in _read_mount_table():
        if path == mount_point or path.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) >= best_len:
                best, best_len = fstype, len(mount_point)
    return best


def is_watchable(path: str) -> tuple[bool, str]:
    """这个目录能不能监听 → ``(能不能, 不能的原因)``

    FUSE / 网络文件系统一律拒掉（v2.46.0）：``isdir`` + ``access`` 在它们上面会返回
    “一切正常”，而真去 watch 的代价是整个挂载点卡死。这类目录继续由定时扫描 +
    追新轮询管，不会少入库，只是慢一点。
    """
    if not path:
        return False, "路径为空"
    if not path.startswith("/"):
        return False, "不是绝对路径"
    fstype = fstype_of(path)
    if fstype and fstype in FUSE_FSTYPES:
        return False, f"{fstype} 挂载不支持实时监听（已降级为定时扫描）"
    if not os.path.isdir(path):
        return False, "目录不存在或不可读"
    if not os.access(path, os.R_OK):
        return False, "没有读权限"
    return True, ""


def _count_subdirs_capped(path: str, cap: int) -> int:
    """数 ``path`` 下的子目录个数，到 ``cap`` 就停

    只为「要不要 recursive 监听」做决策：watchdog 的 recursive=True 会给每个子目录
    建一个 inotify watch，大树下建 watch 本身就要几十分钟。cap 上限让这个判断是
    O(cap) 而不是 O(整棵树)——8.5 万文件的树也不用全走完。
    """
    count = 0
    stack = [path]
    seen: set[tuple[int, int]] = set()
    while stack:
        cur = stack.pop()
        try:
            st = os.stat(cur)
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen:          # 防 symlink 循环
            continue
        seen.add(key)
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            count += 1
                            if count >= cap:
                                return count
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return count


# ==================== 事件回调（只记账，不做重活）====================

class _CoalescingHandler(FileSystemEventHandler):  # type: ignore[misc]
    """把 inotify 事件按「库」记到 ``_PENDING``，什么都不做

    真正的入队在 ``_drain_loop`` 里按防抖窗口统一做。这里如果直接查库/入队，
    watchdog 的回调线程就会被数据库与磁盘 IO 拖住——而它是个共享的 emitter 线程，
    拖住它等于拖住所有被监听目录。
    """

    def on_any_event(self, event) -> None:  # noqa: ANN001 — watchdog 的事件对象
        if getattr(event, "is_directory", False):
            return                      # 目录级事件（新建/改名目录）后面由扫描器发现
        # 事件类型不筛：inotify 只报「有变动」，靠扫描器按指纹判断到底变没变。
        # 这里越宽越安全（宁可多触发一次增量扫描，也不能漏），代价是防抖吸收了抖动。
        src = getattr(event, "src_path", "") or ""
        with _STATE_LOCK:
            for lib_id, paths in _LIBRARY_PATHS.items():
                if any(src.startswith(p.rstrip("/") + "/") for p in paths):
                    _PENDING[lib_id] = _now()
                    _STATS["events"] += 1
                    return


# ==================== 入队（独立线程 + 防抖 + 限流）====================

def _trigger_scan(library_id: int) -> None:
    """给一个库入队一轮增量扫描

    自己开 Session：回调线程 / 防抖线程都不能复用别人的连接。失败只记日志——
    监听失败不该拖垮扫描本身。
    """
    now = _now()
    with _STATE_LOCK:
        last = _LAST_TRIGGER.get(library_id, 0.0)
        if now - last < FS_EVENT_MIN_INTERVAL_SEC:
            _STATS["coalesced"] += 1
            # 合并窗口内被合并的事件改成延迟补发：窗口结束后由 drain 线程补触发一轮。
            # （之前是直接丢弃——落单文件 + 后续无事件时，会静默漏到定时扫描才被发现。）
            _RETRY_AT[library_id] = last + FS_EVENT_MIN_INTERVAL_SEC
            return
        _LAST_TRIGGER[library_id] = now
    try:
        from backend.database import SessionLocal
        from backend.emby_server import models as em
        from backend.emby_server import scan_queue

        db = SessionLocal()
        try:
            lib = db.query(em.Library).filter(em.Library.id == library_id).first()
            if lib is None or not lib.is_enabled:
                with _STATE_LOCK:
                    _RETRY_AT.pop(library_id, None)
                return
            if getattr(lib, "fs_watch", True) is False:
                with _STATE_LOCK:
                    _RETRY_AT.pop(library_id, None)
                return
            # 一定是增量：监听到的只是「有变动」，到底变没变由扫描器的指纹判断
            scan_queue.enqueue(lib, trigger="fs-event")
            with _STATE_LOCK:
                _STATS["triggers"] += 1
                # 这轮扫描是整目录枚举：窗口内所有被合并的事件都被它覆盖，
                # 延迟补发的"债"已还清，不再补发。
                _RETRY_AT.pop(library_id, None)
            logger.info("fs-watch: 库「%s」(id=%s) 有文件变动，已入队增量扫描",
                        lib.name, library_id)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — 入队失败只丢这一轮，下一次事件会再来
        with _STATE_LOCK:
            _STATS["errors"] += 1
        logger.warning("fs-watch: 触发扫描失败 library_id=%s", library_id, exc_info=True)


def _due_retries(now: float) -> list[int]:
    """窗口到期的延迟补发：返回此刻应该补触发的库 id（排序后，确定性）

    只读不弹：真正调用 ``_trigger_scan`` 时它会自己处理——成功/早退清掉，
    再次被合并就重新记。剩下的留给下一轮 drain，保证合并窗口的限流不被补发冲破。
    """
    with _STATE_LOCK:
        return [lib_id for lib_id in sorted(_RETRY_AT)
                if now >= _RETRY_AT[lib_id]]


def _drain_loop(stop: threading.Event) -> None:
    """防抖线程：把 ``_PENDING`` 里的库按窗口合并后逐个入队

    兼做**周期性对账**（v2.48.0）：inotify 会漏事件（watch 上限、容器重建、驱动
    重启、目录被换掉），而这些变更只有靠定期与数据库对一下才能被发现。这里只在
    **监听集合与数据库不一致**时重建——不遍历目录、不列文件、不做全量读取，
    代价就是一次库查询 + 内存集合比较。
    """
    last_reconcile = _now()
    while not stop.is_set():
        time.sleep(FS_EVENT_DEBOUNCE_SEC)
        now = _now()
        if now - last_reconcile >= RECONCILE_INTERVAL_SEC:
            last_reconcile = now
            _reconcile_once()
        ready: list[int] = []
        with _STATE_LOCK:
            for lib_id, seen_at in list(_PENDING.items()):
                if now - seen_at >= FS_EVENT_DEBOUNCE_SEC:
                    _PENDING.pop(lib_id, None)
                    ready.append(lib_id)
        for lib_id in ready[:FS_EVENT_MAX_TRIGGERS]:
            _trigger_scan(lib_id)
        # 延迟补发：合并窗口内被合并的事件，窗口结束后补触发一轮。
        # 同样受每轮上限约束——合并窗口的限流不能因为补发就被冲破。
        for lib_id in _due_retries(now)[:FS_EVENT_MAX_TRIGGERS]:
            _trigger_scan(lib_id)


# ==================== 监听生命周期 ====================

def _watch_now(library_id: int, paths: tuple[str, ...]) -> None:
    """给一个库建立/刷新监听；失败只降级不抛"""
    with _STATE_LOCK:
        observer = _OBSERVER
        unwatch = _WATCHES.pop(library_id, None)
        if unwatch and observer is not None:
            for handle in unwatch.values():
                try:
                    observer.unschedule(handle)
                except Exception:  # noqa: BLE001 — 旧的 watch 可能已经失效
                    pass
        _DEGRADED.pop(library_id, None)
        if observer is None:
            # stop_fs_watcher() 在后台同步线程跑一半时被调（重启/测试竞态）：
            # 没有 observer 就建不了 watch，记降级而不是抛 AttributeError。
            _DEGRADED[library_id] = "监听器已停止"
            return
        handles: dict = {}
        reasons: list[str] = []
        for path in paths:
            ok, why = is_watchable(path)
            if not ok:
                reasons.append(f"{path}：{why}")
                continue
            # 大树保护：子目录超过预算就不做 recursive 监听。建 watch 本身在这种
            # 规模下就要几十分钟，还会吃光 inotify 上限；直接降级为定时扫描，
            # 该库不会少入库，只是新片发现慢一点（分钟级轮询兜底）。
            ndirs = _count_subdirs_capped(path, FS_WATCH_MAX_SUBDIRS + 1)
            if ndirs > FS_WATCH_MAX_SUBDIRS:
                reasons.append(
                    f"{path}：子目录过多（>{FS_WATCH_MAX_SUBDIRS}），已降级为定时扫描")
                continue
            try:
                handles[path] = observer.schedule(
                    _CoalescingHandler(), path, recursive=True)
            except OSError as exc:
                # inotify 实例耗尽（fs.inotify.max_user_watches）是这里最常见的失败
                reasons.append(f"{path}：监听失败（{exc}）")
        if handles:
            _WATCHES[library_id] = handles
        if reasons:
            _DEGRADED[library_id] = "；".join(reasons[:3])
            logger.warning("fs-watch: 库 id=%s 部分目录监听失败，已降级为定时扫描：%s",
                           library_id, _DEGRADED[library_id])


def _expected_libraries() -> dict:
    """库里**当前**应该被监听的库 → ``{lib_id: 本机路径元组}``（只查库，不碰磁盘）

    过滤口径与 :func:`sync_from_db` **完全一致**（包括“没有本机路径的库不进结果”）：
    对账比的就是「库里的现状」与「上次建出来的监听」，两边口径不一致会每轮都判成
    「有差异」——那就不是补漏，而是每 15 分钟无意义地重建一次全部监听。
    """
    from backend.database import SessionLocal
    from backend.emby_server import change_watcher
    from backend.emby_server import models as em

    db = SessionLocal()
    try:
        excluded = set(change_watcher.resolve_excluded(db))
        libs = db.query(em.Library).filter(em.Library.is_enabled == True).all()
        wanted: dict = {}
        for lib in libs:
            if getattr(lib, "fs_watch", True) is False or lib.id in excluded:
                continue
            paths = local_paths_for_library(lib)
            if not paths:
                continue            # 纯远程来源的库本来就不监听（与 sync_from_db 同口径）
            wanted[lib.id] = paths
        return wanted
    finally:
        db.close()


def _reconcile_once() -> dict:
    """对账一次：**只在有差异时**重建监听

    - 没有差异 → 一条日志都不打（绝大多数时候它就是空转）
    - 有差异 → 重建，并把差异说清楚（新加了库 / 移除了库 / 路径改了）

    刻意**不**在这里触发扫描：inotify 漏掉的事件无法可靠地判定“漏了哪些文件”，
    凭猜测补扫描要么重复要么遗漏。补漏靠增量扫描的目录指纹，它本来就只处理变化。
    """
    if not WATCHDOG_AVAILABLE:
        return {"changed": False}
    _LAST_RECONCILE["at"] = _now()
    try:
        wanted = _expected_libraries()
    except Exception:  # noqa: BLE001 — 读库失败保持现状，下轮再对
        logger.debug("fs-watch: 对账读库失败，下轮重试", exc_info=True)
        _LAST_RECONCILE["changed"] = None
        return {"changed": False}
    with _STATE_LOCK:
        current = {lib_id: tuple(paths) for lib_id, paths in _LIBRARY_PATHS.items()}
    if current == wanted:
        _LAST_RECONCILE["changed"] = False
        return {"changed": False}
    _LAST_RECONCILE["changed"] = True
    added = sorted(set(wanted) - set(current))
    removed = sorted(set(current) - set(wanted))
    retuned = sorted(k for k in set(wanted) & set(current) if wanted[k] != current[k])
    logger.info("fs-watch: 对账发现差异，重建监听（新增=%s 移除=%s 路径变更=%s）",
                added, removed, retuned)
    sync_from_db()
    return {"changed": True, "added": added, "removed": removed, "retuned": retuned}


def sync_from_db() -> dict:
    """按库里**当前**的启用媒体库重建监听（启动时调一次；之后可重复调）

    不写死任何路径：每次都以数据库里启用的库为准，路径改了再调一次就跟着变。

    **排除清单与追新用同一套语义**（v2.46.0）：被排除的库既不进追新轮询，也不被
    inotify 监听。之前这里只看 ``is_enabled``/``fs_watch``，于是“已排除追新”的库
    仍然会因为本机文件变动被触发增量扫描——用户关掉的东西还在后台跑。

    **串行执行**：初始后台同步和对账线程都会调这里，``_SYNC_LOCK`` 保证同一时间
    只有一个同步在跑（重叠跑会导致 _WATCHES 被两个线程同时改）。
    """
    with _SYNC_LOCK:
        return _sync_from_db()


def _sync_from_db() -> dict:
    """sync_from_db 的实现本体（调用方只走带锁的 sync_from_db）"""
    status = {"watched": 0, "degraded": 0, "dirs": 0, "available": WATCHDOG_AVAILABLE}
    if not WATCHDOG_AVAILABLE:
        return status
    try:
        from backend.database import SessionLocal
        from backend.emby_server import change_watcher
        from backend.emby_server import models as em

        db = SessionLocal()
        try:
            # 与追新共用同一个解析函数（含旧包含清单的一次性迁移），不自己再写一套
            excluded = set(change_watcher.resolve_excluded(db))
            libs = db.query(em.Library).filter(em.Library.is_enabled == True).all()
            wanted = {
                lib.id: local_paths_for_library(lib)
                for lib in libs
                if getattr(lib, "fs_watch", True) is not False and lib.id not in excluded
            }
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — 读库失败就保持现状，不清空已有监听
        logger.warning("fs-watch: 读取媒体库列表失败，沿用当前监听", exc_info=True)
        return status

    with _STATE_LOCK:
        observer = _OBSERVER
        for lib_id in list(_LIBRARY_PATHS):
            if lib_id not in wanted:
                unwatch = _WATCHES.pop(lib_id, None)
                if unwatch and observer is not None:
                    for handle in unwatch.values():
                        try:
                            observer.unschedule(handle)
                        except Exception:  # noqa: BLE001
                            pass
                _LIBRARY_PATHS.pop(lib_id, None)
                _DEGRADED.pop(lib_id, None)
    for lib_id, paths in wanted.items():
        if not paths:
            continue
        with _STATE_LOCK:
            _LIBRARY_PATHS[lib_id] = paths
            status["dirs"] += len(paths)
        _watch_now(lib_id, paths)
    with _STATE_LOCK:
        status["watched"] = len(_WATCHES)
        status["degraded"] = len(_DEGRADED)
    return status


def _initial_sync_async() -> None:
    """初始同步的后台入口：start_fs_watcher 里起线程调这个，主线程不等它

    sync_from_db 会对每个本机路径做 recursive 监听；大目录树（/strm 8.5 万文件）
    下这一步要几十分钟。同步等它等于把整个 worker 启动卡死——排在后面的
    Redis 扫描队列消费线程起不来，扫描就一直「等待调度」。
    """
    try:
        status = sync_from_db()
        logger.info("fs-watch: 初始同步完成，监听 %s 个库 / %s 个目录（降级 %s 个）",
                    status["watched"], status["dirs"], status["degraded"])
    except Exception:  # noqa: BLE001 — 同步失败只影响实时监听，定时扫描照跑
        logger.warning("fs-watch: 初始同步失败（已降级为定时扫描）", exc_info=True)


def start_fs_watcher() -> bool:
    """启动监听（应用 lifespan / worker 启动里调；重复调用是安全的）

    返回是否真的起了监听。**失败返回 False 而不是抛异常**：启动监听失败绝不能
    让整个服务起不来——最坏情况就是退回定时扫描。

    **调用立即返回**：初始的 sync_from_db 在后台线程里做。之前它是同步的，
    大目录树下会把调用方（worker 主线程）卡住几十分钟，导致后面的启动步骤
    （如 Redis 扫描队列消费）永远执行不到。
    """
    global _OBSERVER, _THREAD, _STOP, _SYNC_THREAD
    with _STATE_LOCK:
        if not WATCHDOG_AVAILABLE or _THREAD is not None:
            return False
        try:
            _OBSERVER = Observer()
            _OBSERVER.daemon = True
            _OBSERVER.start()
        except Exception:  # noqa: BLE001 — inotify 实例拿不到就降级
            logger.warning("fs-watch: 启动监听线程失败，已降级为定时扫描", exc_info=True)
            _OBSERVER = None
            return False
        _STOP = threading.Event()
        thread = threading.Thread(target=_drain_loop, args=(_STOP,),
                                  name="fs-watch-drain", daemon=True)
        thread.start()
        _THREAD = thread
        sync_thread = threading.Thread(target=_initial_sync_async,
                                       name="fs-watch-init-sync", daemon=True)
        sync_thread.start()
        _SYNC_THREAD = sync_thread
    logger.info("fs-watch: 监听线程已启动，初始同步在后台进行（完成后即生效）")
    return True


def stop_fs_watcher() -> None:
    """停掉监听（测试与优雅退出用）"""
    global _OBSERVER, _THREAD, _STOP, _SYNC_THREAD
    with _STATE_LOCK:
        observer, _THREAD = _OBSERVER, None
        stop_event, _STOP = _STOP, None
        _SYNC_THREAD = None
        _WATCHES.clear()
        _LIBRARY_PATHS.clear()
        _DEGRADED.clear()
        _PENDING.clear()
        _RETRY_AT.clear()
        _OBSERVER = None
    if stop_event is not None:
        stop_event.set()
    if observer is not None:
        try:
            observer.stop()
            observer.join(timeout=5)
        except Exception:  # noqa: BLE001
            pass


def watcher_status() -> dict:
    """当前监听状态（管理端 Dashboard / 设置页回显用）

    含每个库的降级原因——「监听失败」这件事必须让人看得见，否则就是默默不工作。
    """
    with _STATE_LOCK:
        return {
            "available": WATCHDOG_AVAILABLE,
            "running": _THREAD is not None,
            # 初始同步是否还在后台跑：跑的时候 watched_libraries 为空是正常的，
            # 不是“没起作用”。界面可以用这个给一个“同步中”的提示。
            "sync_running": _SYNC_THREAD is not None and _SYNC_THREAD.is_alive(),
            "debounce_sec": FS_EVENT_DEBOUNCE_SEC,
            "min_interval_sec": FS_EVENT_MIN_INTERVAL_SEC,
            "reconcile_interval_sec": RECONCILE_INTERVAL_SEC,
            "last_reconcile_at": _LAST_RECONCILE.get("at"),
            "last_reconcile_changed": _LAST_RECONCILE.get("changed"),
            "watched_libraries": sorted(_WATCHES.keys()),
            "degraded": {str(k): v for k, v in _DEGRADED.items()},
            # 等待延迟补发的库：窗口内被合并的事件还没等到补发，界面可提示
            "pending_retries": sorted(_RETRY_AT.keys()),
            "stats": dict(_STATS),
        }

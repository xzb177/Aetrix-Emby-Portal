"""扫描队列（v2.27.0）：按远程挂载串行化 + 全局并发上限 + 队列状态与进度

问题是什么（一次线上日志审计的结论）：四个媒体库在同一分钟里依次点「扫描」，四个任务
**合法地**同时跑起来——单个媒体库的互斥只防「同一个库重复扫描」，它不防「不同的库同时打
同一个远程挂载」。于是同一个 WebDAV / rclone 端点被四个任务反复 PROPFIND（日志里那条
每 5 秒一次的同一路径，就是扫描在重新列目录），远端延迟被放大、SQLite 被四个写入方争抢，
CPU 打满而进度一动不动。

这里做四件事：

1. **按远程挂载串行化**：一个库扫描期间锁住它要读的远程挂载（``parse_mount_ids`` 出来的 id），
   其它引用同一挂载的库**排队**而不是同时打远端；只读本机目录的库不受影响，照常并行。
2. **全局并发上限**（``EMBY_SCAN_MAX_PARALLEL``，默认 2）：扫描是资源大户（ffprobe + 写库 +
   远端 IO），不设上限时「多点几下」就能把机器打满。
3. **排队而不是报错**：重复点同一个库的扫描不报 409，直接告诉你「已经在队列里/正在扫」；
   排队的任务可以取消（正在跑的不能，中途停会留下半个库的状态）。
4. **状态可查**：排队中（第几位、在等哪个挂载）+ 运行中（阶段、已发现、已处理、当前目录、
   本轮远程列举次数）都在管理端可见，见 ``snapshot`` 与 ``live_payload``。

线程模型：一个调度线程（没有任务时退出，下次入队再起）+ 每个被派发的任务一个工作线程。
**入队时就在调用方线程里派发一次**（``_pump_locked``）：排队面板点完扫描拿到的响应就是
真实状态，而不是等调度线程醒来的猜测；调度线程负责「有任务释放资源后把等在后面的接上」。
任务在自己的线程里用**独立 Session**跑 ``scanner.scan_library_sync``（与升级前的后台线程一样），
所以请求级 Session 的生命周期问题不受影响。进程内状态，EM / EA 各自单进程部署。
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
from backend.emby_server.env_util import env_float, env_int

# 后端拆分（v2.41.0）：AETRIX_ROLE 环境变量
# - "api"：API 进程，enqueue() 走 Redis 桥接推给 worker
# - "worker"：Worker 进程，走原有进程内队列
# - 未设置：单体模式，走原有进程内队列（向后兼容）
AETRIX_ROLE = os.getenv("AETRIX_ROLE", "").strip().lower()

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backend.database import SessionLocal
from backend.emby_server import models as em
from backend.emby_server import scan_instrument
from backend.emby_server import scan_progress as progress

logger = logging.getLogger(__name__)

# 扫描队列开关（0 = 回到升级前「谁点谁起线程」，但仍会上报进度）
SCAN_QUEUE_ENABLED = (os.getenv("EMBY_SCAN_QUEUE", "1") or "1").strip().lower() not in ("0", "false", "no")
# 同时最多跑几个扫描（默认 2：一个在扫 + 一个可以立即接上，再多就是互相抢资源）
SCAN_MAX_PARALLEL = env_int("EMBY_SCAN_MAX_PARALLEL", 2, 1, 8)
# 同一个远程挂载同时只允许一个扫描任务（0 = 关闭这条串行化，不建议）
SCAN_MOUNT_SERIAL = (os.getenv("EMBY_SCAN_MOUNT_SERIAL", "1") or "1").strip().lower() not in ("0", "false", "no")
# 队列与最近完成的任务保留多少条（给管理端看「刚才那几轮排队等了多久」）
SCAN_QUEUE_HISTORY = env_int("EMBY_SCAN_QUEUE_HISTORY", 12, 0, 50)
# 进度刷盘间隔（秒）：管理端刷新页面时即使不在同一个进程也能看到进度
SCAN_PROGRESS_FLUSH_SECONDS = env_float("EMBY_SCAN_PROGRESS_FLUSH", 2.0, 0.5)

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

# 进度落库时的键（与 scan_progress.progress_of 的输出一致）
PROGRESS_COLUMN_KEYS = (
    "phase", "phase_label", "enumerated", "processed", "current", "elapsed_ms",
    "started_at", "phase_changed_at", "updated_at",
    "remote_lists", "remote_reused", "remote_last_at",
)


def enabled() -> bool:
    return bool(SCAN_QUEUE_ENABLED)


@dataclass
class ScanTask:
    """一个排队中 / 正在跑 / 刚跑完的扫描任务"""

    library_id: int
    name: str
    trigger: str
    mount_ids: tuple[int, ...] = ()
    local_paths: tuple[int, ...] = ()     # 只用于展示（本机来源条数）
    snapshot: object = None               # scanner.LibrarySnapshot
    state: str = STATE_QUEUED
    requested_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    queued_ms: int = 0
    duration_ms: Optional[int] = None
    waiting_for: tuple[int, ...] = ()     # 正在等哪些挂载（挂载 id）
    result: Optional[str] = None          # 终态：success / partial / failed / canceled
    error: Optional[str] = None
    request_count: int = 1                # 同一个库被连续点了几次（合并成一条）
    # 这一轮期间发生的远程目录列举（真请求 / 内存复用）：回答「慢在远端吗」
    remote_lists: int = 0
    remote_reused: int = 0
    owner_thread: Optional[threading.Thread] = None
    # Redis 桥接携带的原始任务 payload（bytes/str，BLMOVE 时的 _raw）：
    # P0-0c 修复——必须在扫描真正完成后才 ACK，提前确认会导致进程崩溃时任务无声丢失。
    # 单体模式（非 Redis 桥接）下为空列表。
    redis_raws: list = field(default_factory=list)
    # v2.57.1：运行中被合并了更宽范围的请求时，登记在这里；本任务跑完后按此
    # 快照自动补扫一轮（见 _try_merge_scan_task / _run_task 收尾）。
    pending_rescan_snapshot: object = None
    pending_rescan_trigger: str = "manual"

    def as_dict(self, *, position: Optional[int] = None) -> dict:
        data = {
            "library_id": self.library_id,
            "name": self.name,
            "trigger": self.trigger,
            "state": self.state,
            "mount_ids": list(self.mount_ids),
            "local_sources": len(self.local_paths),
            "requested_at": self.requested_at.isoformat(timespec="seconds"),
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "queued_ms": int(self.queued_ms or 0),
            "duration_ms": self.duration_ms,
            "waiting_for": list(self.waiting_for or ()),
            "result": self.result,
            "error": self.error,
            "request_count": int(self.request_count or 1),
            "remote_lists": int(self.remote_lists or 0),
            "remote_reused": int(self.remote_reused or 0),
        }
        if position is not None:
            data["position"] = position
        live = progress.progress_of(self.library_id)
        if live is not None:
            data["progress"] = live
        return data


_LOCK = threading.RLock()
_COND = threading.Condition(_LOCK)
# 进度刷盘与「结束清空」互斥（v2.27.0）：只加在库队列上不够——刷盘线程可能**先**取到
# 一份旧快照（还在扫）、再被 SQLite 写锁挡住，扫描结束时它才提交，于是
# 「已跑完」的那一行又被写回一份旧进度，面板就永远显示「扫描中 已处理 N」。
# 两者共用一把锁，最终落地的一定是清空。
_PERSIST_LOCK = threading.Lock()

_QUEUE: list[ScanTask] = []              # 等待派发（FIFO）
_RUNNING: dict[int, ScanTask] = {}       # 库 id → 正在跑的任务
_HISTORY: list[ScanTask] = []
_MOUNT_OWNER: dict[int, int] = {}        # 挂载 id → 正持有它的库 id
_SCHEDULER: Optional[threading.Thread] = None
_FLUSHER: Optional[threading.Thread] = None
# R33 P3-7：补扫入队连续失败计数（库 id → 次数）。连续 5 次失败则毒快照进死信
# （ACK raws，不再无限重投），成功一次即清零。
_RESCAN_FAIL_COUNTS: dict[int, int] = {}
_RESCAN_FAIL_DEAD_LETTER_AFTER = 5


# ==================== 入队 / 取消 ====================

def enqueue(library, *, trigger: str = "manual", force_full: bool = False) -> dict:
    """把一个媒体库的扫描放进队列（角色分发版）

    - API 进程（AETRIX_ROLE=api 且 Redis 可用）：推入 Redis 队列，由 worker 消费执行
    - 其他：走进程内队列（enqueue_local，原有逻辑）
    """
    if AETRIX_ROLE == "api":
        try:
            from backend.emby_server import scan_queue_redis as _rq
            if _rq.is_redis_mode():
                return _rq.push_scan_request(int(library.id), trigger=trigger,
                                             force_full=force_full)
        except Exception as e:
            logger.warning(f"Redis 入队失败，回退进程内队列：{e}")
    return enqueue_local(library, trigger=trigger, force_full=force_full)




def enqueue_targeted(library, prefixes: list[str],
                     trigger: str = "drive-changes") -> dict:
    """v2.52.0: 定向扫描——只扫指定路径前缀下的文件（Drive Changes API 用）。

    实现：快照的 paths 保持原样（挂载互斥/来源解析要用），但加上
    ``limit_prefixes`` 让文件迭代器只产出命中前缀的文件，并置
    ``targeted=True`` 跳过删除清理阶段（看不到的目录不能当成"已删除"）。

    与整库增量扫描共用同一队列与挂载互斥，不会并发打架。
    """
    import dataclasses
    from backend.emby_server.scanner import LibrarySnapshot

    snapshot = LibrarySnapshot.of(library)
    snapshot = dataclasses.replace(
        snapshot,
        limit_prefixes=tuple(prefixes or ()),
        targeted=True,
    )
    # 复用 enqueue_local 的入队逻辑，但用我们改好的快照
    return _enqueue_with_snapshot(library, snapshot, trigger=trigger)


def _merge_scope_into(base_snapshot, new_snapshot):
    """把 new_snapshot 的扫描范围并入 base_snapshot（可为 None），返回合并后的快照。

    整库（无 limit_prefixes）是超集；定向取前缀并集。快照是 frozen dataclass，
    用 dataclasses.replace 产生新的，不改原对象。
    """
    import dataclasses
    if base_snapshot is None:
        return new_snapshot
    base_prefixes = tuple(getattr(base_snapshot, "limit_prefixes", ()) or ())
    new_prefixes = tuple(getattr(new_snapshot, "limit_prefixes", ()) or ())
    base_is_whole = not base_prefixes and not getattr(base_snapshot, "targeted", False)
    # P3(r32)：空前缀 + targeted=True 不是整库（当前调用方已 guard，此处防御）。
    new_is_whole = not new_prefixes and not getattr(new_snapshot, "targeted", False)
    if new_is_whole:
        # 新请求是整库 → 结果整库
        if base_is_whole:
            return base_snapshot
        return dataclasses.replace(base_snapshot, limit_prefixes=(), targeted=False)
    if base_is_whole:
        return base_snapshot  # 整库已是超集
    merged = tuple(sorted(set(base_prefixes) | set(new_prefixes)))
    if merged == base_prefixes:
        return base_snapshot
    return dataclasses.replace(base_snapshot, limit_prefixes=merged)


def _scope_covered_by(cover_snapshot, new_snapshot) -> bool:
    """new_snapshot 的范围是否已被 cover_snapshot 完全覆盖。"""
    new_prefixes = tuple(getattr(new_snapshot, "limit_prefixes", ()) or ())
    if not new_prefixes:
        # 新请求是整库：只有整库能覆盖它
        cover_prefixes = tuple(getattr(cover_snapshot, "limit_prefixes", ()) or ())
        return not cover_prefixes and not getattr(cover_snapshot, "targeted", False)
    cover_prefixes = tuple(getattr(cover_snapshot, "limit_prefixes", ()) or ())
    if not cover_prefixes and not getattr(cover_snapshot, "targeted", False):
        return True  # 覆盖方是整库
    # P3(r32)：边界感知的嵌套前缀判定——/a 覆盖 /a/b，但不覆盖 /ab
    # （裸 startswith 会误判 /ab，必须带 "/" 边界）。
    cover_set = set(cover_prefixes)
    return all(any(p == c or p.startswith(c + "/") for c in cover_set)
               for p in new_prefixes)


def _try_merge_scan_task(existing: "ScanTask", new_snapshot, new_trigger: str = "manual") -> bool:
    """把新扫描请求合并进已有任务（保持「一库一任务」与 created=False 的 UX）。

    v2.57.1 修复（2026-10-13 生产实锤）：以前合并时无条件丢弃新请求的扫描范围——
    生成器的整库扫描经常被追新（chase-new）的定向扫描吞掉，新文件目录永不被
    遍历（added 恒 0）。现在：
    - 已有任务还在排队（QUEUED）：直接把范围并入其快照（整库升级 / 前缀并集）；
    - 已有任务正在跑（RUNNING）：快照已冻结不能改。若新范围未被当前任务覆盖，
      登记到 ``pending_rescan_snapshot``，当前任务跑完后自动按该快照补扫一轮。
    恒返回 True（合并语义与 UX 不变：重复请求不报错、不另起任务）。
    """
    try:
        if existing.state == STATE_QUEUED:
            existing.snapshot = _merge_scope_into(existing.snapshot, new_snapshot)
            return True
        # RUNNING：范围已冻结。若新范围已被当前任务覆盖，无需补扫；
        # 否则登记补扫（与已登记的补扫范围再合并，避免多次登记膨胀）。
        if _scope_covered_by(existing.snapshot, new_snapshot):
            return True
        pending = existing.pending_rescan_snapshot
        if pending is not None and _scope_covered_by(pending, new_snapshot):
            return True
        existing.pending_rescan_snapshot = _merge_scope_into(pending, new_snapshot)
        existing.pending_rescan_trigger = str(new_trigger or "manual")
        logger.info("扫描范围补登记：库 id=%s 正在跑，新请求范围更宽，跑完后补扫（触发=%s）",
                    existing.library_id, existing.pending_rescan_trigger)
        return True
    except Exception:
        # P2-4(r32)：合并异常不再静默丢弃新范围。QUEUED：新快照直接替换
        # （不再调可能抛错的 _merge_scope_into）；RUNNING：新快照直接登记补扫。
        # 直接赋值基本不抛错；异常只应在快照畸形时触发（内部调用方均为合法快照），
        # 此时日志已记录，可观测。注：此前"整库兜底"注释无对应代码，已修正。
        logger.exception("合并扫描范围异常（库 id=%s），新范围直接采用",
                         getattr(existing, "library_id", "?"))
        if existing.state == STATE_QUEUED:
            existing.snapshot = new_snapshot
        else:
            existing.pending_rescan_snapshot = new_snapshot
            existing.pending_rescan_trigger = str(new_trigger or "manual")
        return True


def _enqueue_with_snapshot(library, snapshot, trigger: str = "manual") -> dict:
    """用外部给定的快照入队（enqueue_local 的快照可替换版）。"""
    from backend.emby_server import scan_instrument  # 延迟导入

    scan_instrument.install()
    with _LOCK:
        existing = _task_of_locked(library.id)
        if existing is not None:
            _try_merge_scan_task(existing, snapshot, trigger)
            existing.request_count += 1
            return {"created": False, "task": existing.as_dict(
                position=_position_locked(existing))}
        # 无已有任务：新建排队任务
        task = ScanTask(
            library_id=int(library.id),
            name=str(snapshot.name or ""),
            trigger=str(trigger or "manual"),
            mount_ids=tuple(int(m) for m in (snapshot.mount_ids or ())),
            local_paths=tuple(snapshot.paths or ()),
            snapshot=snapshot,
            redis_raws=[],
        )
        _QUEUE.append(task)
        task.waiting_for = _conflicts_locked(task)
        logger.info("定向扫描入队：库「%s」(id=%s) 触发=%s；目录数=%d；队列长度 %s",
                    task.name or "?", task.library_id, task.trigger,
                    len(getattr(snapshot, "limit_prefixes", ()) or ()),
                    len(_QUEUE))
        _ensure_scheduler_locked()
        _pump_locked()
        _COND.notify_all()
        return {"created": True, "task": task.as_dict(
            position=_position_locked(task))}


def start_redis_consumer() -> bool:
    """Worker 进程：启动 Redis 扫描队列消费（backend/worker.py 调用）"""
    from backend.emby_server import scan_queue_redis as _rq
    return _rq.start_redis_consumer()


def stop_redis_consumer(timeout: float = 5.0) -> None:
    """停止 Redis 扫描队列消费"""
    from backend.emby_server import scan_queue_redis as _rq
    _rq.stop_redis_consumer(timeout=timeout)


def enqueue_local(library, *, trigger: str = "manual", redis_raw=None,
                  force_full: bool = False) -> dict:
    """把一个媒体库的扫描放进队列（已在队列里 / 正在跑就返回那一条，不报错）

    快照在这一刻拍下（``LibrarySnapshot.of``）：排队期间管理员改了路径 / 策略，
    这一轮仍按点击那一刻的配置跑——与「点了就开扫」的语义一致，不会出现
    「排了十分钟，跑的是十分钟后改过的配置」。

    ``redis_raw``：Redis 桥接模式下 ``pop_scan_request`` 取出的原始 payload。
    随 ``ScanTask`` 携带，**扫描真正完成后才 ACK**（P0-0c：提前确认会导致
    worker 崩溃时任务无声丢失）。单体模式传 ``None``。

    ``force_full``：这一轮无视所有指纹，完整处理一遍（管理员手动点「全量扫描」）。

    返回 ``{"created": bool, "task": {...}}``：``created=False`` 表示这次点击被合并到已有任务。
    """
    from backend.emby_server.scanner import LibrarySnapshot  # 延迟导入：避免与 scanner 互相导入

    scan_instrument.install()   # 进度上报点（幂等；见 scan_instrument 的模块说明）
    snapshot = LibrarySnapshot.of(library)
    # 手动「全量扫描」只影响这一轮：宁可多处理一遍，也不能让别的库跟着变全量
    if force_full:
        # 快照是 frozen dataclass，不能直接赋值；用 replace 造一份新的
        snapshot = dataclasses.replace(snapshot, force_full=True)
    with _LOCK:
        existing = _task_of_locked(library.id)
        if existing is not None:
            _try_merge_scan_task(existing, snapshot, trigger)
            existing.request_count += 1
            if redis_raw is not None:
                # 合并进已有任务：这个 Redis 任务同样要等已有任务跑完才 ACK，
                # 不能在这里提前确认，否则崩溃会丢任务。
                existing.redis_raws.append(redis_raw)
            return {"created": False, "task": existing.as_dict(position=_position_locked(existing))}
        # 无已有任务：新建排队任务
        task = ScanTask(
            library_id=int(library.id),
            name=str(snapshot.name or ""),
            trigger=str(trigger or "manual"),
            mount_ids=tuple(int(m) for m in (snapshot.mount_ids or ())),
            local_paths=tuple(snapshot.paths or ()),
            snapshot=snapshot,
            redis_raws=[redis_raw] if redis_raw is not None else [],
        )
        _QUEUE.append(task)
        # 入队时就把「在等哪个挂载」算出来：接口要立刻把理由写进提示（
        # 「已加入队列，正在等挂载：MP媒体库」），不能等调度线程下一次迭代才填上
        task.waiting_for = _conflicts_locked(task)
        # 入队/开始/结束都记一行 INFO：出问题时（“点了扫描没反应”）运维能直接从日志看出
        # 是入队了、在等哪个挂载，还是已经跑起来了——而不是只有面板上一个“扫描中”
        logger.info("扫描入队：库「%s」(id=%s) 触发=%s；等挂载 %s；队列长度 %s",
                    task.name or "?", task.library_id, task.trigger,
                    _mounts_text(task.waiting_for), len(_QUEUE))
        _ensure_scheduler_locked()
        # 就地派发一次（不等调度线程醒来）：点完扫描的响应就是**真实状态**
        # （已开扫 / 排队第几位 / 在等谁），而不是「已入队」的猜测；
        # 否则「点了扫描界面没反应」会变成一件要靠线程调度时机决定的事。
        _pump_locked()
        _COND.notify_all()
        return {"created": True, "task": task.as_dict(position=_position_locked(task))}


def cancel(library_id: int) -> str:
    """取消一个**还在排队**的任务

    返回 ``canceled`` / ``running`` / ``missing``：正在跑的不能取消——中途停下会留下
    「扫了一半的库」（条目已入库、清理没做），比让它跑完更糟；要停就等它跑完或重启进程。
    """
    # 后端拆分：API 进程走 Redis 取消
    if AETRIX_ROLE == "api":
        try:
            from backend.emby_server import scan_queue_redis as _rq
            if _rq.is_redis_mode():
                return _rq.cancel_scan_request(int(library_id))
        except Exception as e:
            logger.warning(f"Redis 取消失败，回退进程内取消：{e}")
    canceled_task = None
    with _LOCK:
        for index, task in enumerate(_QUEUE):
            if task.library_id == int(library_id):
                _QUEUE.pop(index)
                task.state = STATE_CANCELED
                task.result = STATE_CANCELED
                task.finished_at = datetime.now()
                _push_history_locked(task)
                logger.info("取消排队：库「%s」(id=%s)", task.name or "?", task.library_id)
                _COND.notify_all()
                canceled_task = task
                break
        else:
            if int(library_id) in _RUNNING:
                return "running"
            return "missing"
    # 锁外 ACK：被取消的任务不会再跑，必须把 Redis 侧的任务确认掉；
    # 否则它一直占着 processing，worker 重启后会被捞回来重新扫描——用户明明取消了。
    if canceled_task is not None:
        _ack_redis_raws(canceled_task)
    return "canceled"


def is_busy(library_id: int) -> bool:
    # 后端拆分：API 进程查 Redis 去重集合
    if AETRIX_ROLE == "api":
        try:
            from backend.emby_server import scan_queue_redis as _rq
            r = _rq._redis()
            if r is not None and r.sismember(_rq.REDIS_SCAN_DEDUP_KEY, int(library_id)):
                return True
        except Exception:
            pass
    with _LOCK:
        return _task_of_locked(library_id) is not None


def _db_task_base(lib) -> dict:
    """库里合成的队列条目：字段名与 ``ScanTask.as_dict`` 对齐（前端两列不用分叉）

    库里没记「谁触发的这一轮」「排了多久 / 被点了几次」——那是进程内任务才有的东西，
    这里一律留空 / 0，面板显示成「—」，不编一个看起来像真的数字出来。
    """
    return {
        "library_id": int(lib.id),
        "name": str(lib.name or ""),
        "trigger": "",
        "state": STATE_RUNNING,
        "mount_ids": [],
        "local_sources": 0,
        "requested_at": "",
        "started_at": None,
        "finished_at": None,
        "queued_ms": 0,
        "duration_ms": None,
        "waiting_for": [],
        "result": None,
        "error": None,
        "request_count": 1,
        "remote_lists": 0,
        "remote_reused": 0,
        "progress": None,
        # 数据来自执行扫描的节点写进库里的状态，不是本进程的实时队列
        "via": "db",
    }


def _executor_view(redis_waiting: list) -> dict:
    """执行扫描的不是本进程时，队列面板三列从库里合成（见 ``snapshot``）

    为什么需要（v2.41 拆分扫描之后暴露）：扫描搬到 worker 进程执行后，API 进程的
    ``_RUNNING`` / ``_HISTORY`` **永远是空的**，面板的「正在扫描」「最近完成」两列
    整片空白（空闲时连整个「扫描队列」卡片都不显示）。但 ``Library.scan_status`` /
    ``scan_progress`` / ``last_scan_at`` 是**跨进程**的：worker 每
    ``SCAN_PROGRESS_FLUSH_SECONDS`` 秒把进度刷进库，结束时写结果——所以这两列可以从
    库里合成，每条标 ``via="db"``，面板据此说明「进度来自执行扫描的那台机器」。

    顺带补上 Redis 排队项缺失的库名：Redis 里只存 library_id / trigger，
    面板的「排队中」列于是只有位置、没有名字。

    返回 ``{"running": [...], "history": [...], "waiting": [...]}``。读库失败时三个
    列表都退化（不抛）：前端每 3 秒轮询一次这个接口，少两列也比整个面板 500 强。
    """
    from backend.emby_server import scanner   # 延迟导入：scanner 反过来依赖本模块

    running: list[dict] = []
    history: list[dict] = []
    waiting = [dict(item) for item in (redis_waiting or [])]
    names: dict[int, str] = {}
    try:
        with SessionLocal() as db:
            running_rows = (
                db.query(em.Library)
                .filter(em.Library.scan_status == STATE_RUNNING)
                .order_by(em.Library.scan_started_at.asc(), em.Library.id.asc())
                .all()
            )
            history_rows = (
                db.query(em.Library)
                .filter(em.Library.last_scan_at.isnot(None))
                .order_by(em.Library.last_scan_at.desc())
                .limit(SCAN_QUEUE_HISTORY)
                .all()
            )
            for lib in (*running_rows, *history_rows):
                names[int(lib.id)] = str(lib.name or "")

            for lib in running_rows:
                data = _db_task_base(lib)
                # 进度快照是执行节点刷进来的（刷盘间隔见 SCAN_PROGRESS_FLUSH_SECONDS），
                # 阶段 / 已发现 / 已处理 / 当前目录 / 本轮远程请求数都在里面
                stored = _decode_progress(getattr(lib, "scan_progress", None)) or {}
                started = getattr(lib, "scan_started_at", None)
                data.update({
                    "state": STATE_RUNNING,
                    "requested_at": started.isoformat(timespec="seconds") if started else "",
                    "started_at": started.isoformat(timespec="seconds") if started else None,
                    "duration_ms": int((datetime.now() - started).total_seconds() * 1000) if started else None,
                    "remote_lists": int(stored.get("remote_lists") or 0),
                    "remote_reused": int(stored.get("remote_reused") or 0),
                    "progress": stored or None,
                })
                running.append(data)

            for lib in history_rows:
                status = str(getattr(lib, "scan_status", None) or scanner.SCAN_STATUS_SUCCESS)
                stats = scanner.decode_scan_stats(getattr(lib, "scan_stats", None))
                finished = getattr(lib, "last_scan_at", None)
                finished_iso = finished.isoformat(timespec="seconds") if finished else None
                data = _db_task_base(lib)
                data.update({
                    "state": STATE_FAILED if status == scanner.SCAN_STATUS_FAILED else STATE_DONE,
                    "result": status,
                    "error": getattr(lib, "scan_error", None),
                    # 库里不留「请求时刻 / 开始时刻」（收尾时 scan_started_at 被清空），
                    # requested_at 只用于前端列表 key，这里用完成时刻占位
                    "requested_at": finished_iso or "",
                    "finished_at": finished_iso,
                    "duration_ms": stats.get("duration_ms"),
                    "added": int(stats.get("added") or 0),
                    "updated": int(stats.get("updated") or 0),
                    "removed": int(stats.get("removed") or 0),
                })
                history.append(data)

            missing = [
                int(item.get("library_id") or 0) for item in waiting
                if not names.get(int(item.get("library_id") or 0))
            ]
            if missing:
                for lib in db.query(em.Library).filter(em.Library.id.in_(missing)).all():
                    names[int(lib.id)] = str(lib.name or "")
    except Exception:  # noqa: BLE001 — 面板少两列，也不能让轮询接口 500
        logger.warning("从库里合成扫描队列视图失败（面板这两列暂时为空）", exc_info=True)
        return {"running": [], "history": [], "waiting": waiting}

    for item in waiting:
        item["name"] = names.get(int(item.get("library_id") or 0), "")
    return {"running": running, "history": history, "waiting": waiting}


def snapshot() -> dict:
    """整个队列的快照（管理端「扫描队列」面板）

    **谁在执行扫描，决定了这两列从哪来**：

    - 本进程就是执行者（单体模式 / worker）：用进程内队列，实时、有排队原因；
    - 本进程不是执行者（API 角色 + Redis 桥接可用）：``_RUNNING`` / ``_HISTORY``
      结构上永远是空的（任务在 worker 的内存里），这时「正在扫描」「最近完成」
      改从库里的跨进程状态合成，见 ``_executor_view``。
    """
    # 后端拆分：API 进程的排队列表从 Redis 读（进程内队列是空的）
    redis_waiting = None
    if AETRIX_ROLE == "api":
        try:
            from backend.emby_server import scan_queue_redis as _rq
            import json as _json
            r = _rq._redis()
            if r is not None:
                redis_waiting = []
                for idx, raw in enumerate(r.lrange(_rq.REDIS_SCAN_QUEUE_KEY, 0, -1)):
                    try:
                        data = _json.loads(raw)
                    except Exception:
                        continue
                    redis_waiting.append({
                        "library_id": int(data.get("library_id", 0)),
                        "trigger": data.get("trigger", "manual"),
                        "state": "queued",
                        "position": idx + 1,
                        "via": "redis",
                    })
        except Exception:
            redis_waiting = None

    # 执行扫描的不是本进程：三列都从库里合成，并补上 Redis 排队项缺的库名
    executor_view = None
    if redis_waiting is not None:
        executor_view = _executor_view(redis_waiting)
        redis_waiting = executor_view["waiting"]

    with _LOCK:
        waiting = [
            task.as_dict(position=index + 1)
            for index, task in enumerate(_QUEUE)
        ]
        if redis_waiting is not None:
            waiting = redis_waiting
        running = [task.as_dict() for task in _RUNNING.values()]
        history = [task.as_dict() for task in reversed(_HISTORY)][:SCAN_QUEUE_HISTORY]
        mounts = {str(mount_id): library_id for mount_id, library_id in _MOUNT_OWNER.items()}
        if executor_view is not None:
            running = executor_view["running"]
            history = executor_view["history"]
        return {
            "enabled": bool(SCAN_QUEUE_ENABLED),
            "max_parallel": int(SCAN_MAX_PARALLEL),
            "mount_serial": bool(SCAN_MOUNT_SERIAL),
            "running": running,
            "waiting": waiting,
            "history": history,
            "mount_owners": mounts,
            "remote": progress.remote_stats(),
            # 这两列的数据来源：panel = 本进程的队列（实时）；db = 执行节点的落库状态
            "view": "db" if executor_view is not None else "panel",
        }


def live_payload(library) -> Optional[dict]:
    """某个媒体库的实时扫描状态（排队中 / 正在扫 / 由别的进程在扫）；空闲返回 None

    三种来源合成一个口径：

    - 本进程队列里的任务（含实时进度与「在等哪个挂载」）；
    - 库里落盘的进度快照（``Library.scan_progress``）——刷新页面时接上刚才那几秒；
    - 库里状态是 ``running`` 但本进程没有任务：说明是**归属节点**在扫（多机部署），
      这时只说明「由别的节点执行」，不谎报一个自己看不到的进度。
    """
    task = None
    with _LOCK:
        task = _task_of_locked(library.id)
        task_payload = task.as_dict(position=_position_locked(task)) if task is not None else None

    persisted = _decode_progress(getattr(library, "scan_progress", None))
    if task_payload is not None:
        payload = dict(task_payload)
        payload["source"] = "panel"
        if "progress" not in payload and persisted:
            payload["progress"] = persisted
        return payload

    if (getattr(library, "scan_status", None) or "") == "running":
        return {
            "library_id": library.id,
            "name": getattr(library, "name", "") or "",
            "state": STATE_RUNNING,
            "source": "other",
            "message": "正在由归属节点扫描（进度只在执行那台机器上）",
            "progress": persisted,
        }
    return None


# ==================== 状态落库 ====================

def persist_progress(library_id: int, payload: Optional[dict]) -> None:
    """把进度快照写进媒体库行（None = 清空）

    单独开一个短事务：扫描线程正把 ``db.commit`` 换成 ``flush`` 做批量提交，
    进度不能共用它的会话（否则会提前把半批数据提交出去，或把批次边界打乱）。
    """
    try:
        text = json.dumps({k: payload.get(k) for k in PROGRESS_COLUMN_KEYS if k in payload},
                          ensure_ascii=False) if payload else None
        with SessionLocal() as db:
            row = db.query(em.Library).filter(em.Library.id == int(library_id)).first()
            if row is None:
                return
            row.scan_progress = text
            db.commit()
    except Exception as exc:  # noqa: BLE001 — 进度写不进去不影响扫描
        # 清空失败要比写不进去严重：面板会把一台已经跑完的机器一直显示成「扫描中 已处理 N」，
        # 而且换一轮扫描才会被覆盖。所以清空用 warning，写进度用 debug（失败也无害）。
        if payload is None:
            logger.warning("清空扫描进度失败（面板可能显示一份过期的进度）: %s", exc)
        else:
            logger.debug("写入扫描进度失败（不影响扫描）: %s", exc)


def flush_once() -> int:
    """把当前所有正在跑的进度刷一次盘（返回写入条数）

    正常由后台 flusher 每 ``SCAN_PROGRESS_FLUSH_SECONDS`` 秒调用一次；测试里直接调它，
    不用等定时器。取快照与写入必须在同一把锁里（见 ``_PERSIST_LOCK``），
    否则一份「取的时候还在扫、写的时候已经结束」的旧快照会盖掉清空。
    """
    written = 0
    with _PERSIST_LOCK:
        for payload in progress.snapshot():
            persist_progress(payload.get("library_id"), payload)
            written += 1
    return written


def clear_progress(library_id: int) -> None:
    """一轮扫描结束后清掉进度快照（与刷盘互斥；见 ``_PERSIST_LOCK``）

    内存里已经没有这个库的进度时才算「真的结束了」；如果它已经在被新一轮扫描占用
    （``progress.progress_of`` 拿得到），就不动——那是新一轮的进度，不能清掉。
    """
    with _PERSIST_LOCK:
        if progress.progress_of(library_id) is not None:
            return
        persist_progress(library_id, None)


def _decode_progress(raw) -> Optional[dict]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, dict) else None


# ==================== 调度 ====================

def _task_of_locked(library_id: int) -> Optional[ScanTask]:
    running = _RUNNING.get(int(library_id))
    if running is not None:
        return running
    for task in _QUEUE:
        if task.library_id == int(library_id):
            return task
    return None


def _position_locked(task: ScanTask) -> Optional[int]:
    if task.state != STATE_QUEUED:
        return None
    for index, item in enumerate(_QUEUE):
        if item is task:
            return index + 1
    return None


def _ensure_scheduler_locked() -> None:
    global _SCHEDULER
    if _SCHEDULER is not None and _SCHEDULER.is_alive():
        return
    _SCHEDULER = threading.Thread(target=_scheduler_loop, name="scan-scheduler", daemon=True)
    _SCHEDULER.start()
    _ensure_flusher_locked()


def _ensure_flusher_locked() -> None:
    global _FLUSHER
    if _FLUSHER is not None and _FLUSHER.is_alive():
        return
    _FLUSHER = threading.Thread(target=_flusher_loop, name="scan-progress-flusher", daemon=True)
    _FLUSHER.start()


def _limits_locked() -> tuple[int, bool]:
    """当前生效的并发上限与挂载串行化开关（队列关掉时都不限制 = 升级前行为）"""
    if not SCAN_QUEUE_ENABLED:
        return 1_000_000, False
    return SCAN_MAX_PARALLEL, bool(SCAN_MOUNT_SERIAL)


def _conflicts_locked(task: ScanTask) -> tuple[int, ...]:
    """这个任务在等哪些挂载（同一挂载被别的库占着；自己的任务不可能重复占）"""
    if not SCAN_MOUNT_SERIAL:
        return ()
    busy = {
        mount_id for mount_id, owner in _MOUNT_OWNER.items()
        if owner != task.library_id
    }
    return tuple(sorted(m for m in (task.mount_ids or ()) if m in busy))


def _pick_locked() -> Optional[ScanTask]:
    """挑下一个可以跑的任务（跳过被挂载挡住的，别让队头阻塞别的挂载）"""
    limit, mount_serial = _limits_locked()
    for task in _QUEUE:
        task.waiting_for = _conflicts_locked(task) if mount_serial else ()
        if task.waiting_for:
            continue
        if len(_RUNNING) >= limit:
            return None
        return task
    return None


def waiting_dispatchable_count() -> int:
    """等待队列中"现在就能派发"的任务数（公开 API，供统一调度器用）

    排除被挂载串行挡住（waiting_for 非空）、当前根本跑不起来的任务——
    这些任务不应触发 enrich 让路，否则优先级倒置。

    只统计"未被挂载串行挡住"的任务：队头被挡住时 _pick_locked 会跳过它
    让后面的任务先跑，所以"槽位已满"不影响计数——只要有没被挡住的等待任务，
    就说明扫描侧想要资源，enrich 就该降并发让路。

    只读：内部持有 _LOCK，不抛异常（异常时返回 0）。
    """
    try:
        with _LOCK:
            n = 0
            for task in _QUEUE:
                if _conflicts_locked(task):
                    continue
                n += 1
            return n
    except Exception:
        return 0


def _pump_locked() -> int:
    """立刻派发所有「现在就能跑」的任务（调用方必须已持有 _COND 的锁），返回派发数

    并发满了、或队头的任务在等挂载时停下（不等它——让后面不冲突的任务先跑）。
    """
    dispatched = 0
    while True:
        task = _pick_locked()
        if task is None:
            return dispatched
        _dispatch_locked(task)
        dispatched += 1


def _dispatch_locked(task: ScanTask) -> None:
    _QUEUE.remove(task)
    task.state = STATE_RUNNING
    task.started_at = datetime.now()
    task.queued_ms = int((task.started_at - task.requested_at).total_seconds() * 1000)
    task.waiting_for = ()
    _RUNNING[task.library_id] = task
    if SCAN_MOUNT_SERIAL:
        for mount_id in task.mount_ids or ():
            _MOUNT_OWNER[int(mount_id)] = task.library_id
    logger.info("扫描开始：库「%s」(id=%s) 排队 %.1fs；占用挂载 %s；同时 %s 个在跑",
                task.name or "?", task.library_id, (task.queued_ms or 0) / 1000,
                _mounts_text(task.mount_ids), len(_RUNNING))
    task.owner_thread = threading.Thread(
        target=_run_task, args=(task,), name=f"scan-lib{task.library_id}", daemon=True,
    )
    task.owner_thread.start()


def _scheduler_loop() -> None:
    global _SCHEDULER
    while True:
        with _COND:
            if _pump_locked() == 0:
                if not _QUEUE:
                    _SCHEDULER = None
                    _COND.notify_all()
                    return
                # 队伍里有活但都被挂载挡住（或并发已满）：等释放 / 等新任务
                _COND.wait(timeout=0.25)


def _release_locked(task: ScanTask) -> None:
    _RUNNING.pop(task.library_id, None)
    for mount_id in task.mount_ids or ():
        if _MOUNT_OWNER.get(int(mount_id)) == task.library_id:
            _MOUNT_OWNER.pop(int(mount_id), None)
    _push_history_locked(task)
    _COND.notify_all()


@contextlib.contextmanager
def mount_serial_guard(library_id: int, mount_ids):
    """队列外扫描与排队扫描共用同一套挂载串行化（上下文管理器）。

    rescrape（``api/admin_scrape._run_library_rescrape``）不走 enqueue：它在
    API 进程里直接起线程跑扫描，过去不登记 ``_MOUNT_OWNER``——两个用同一
    挂载的库同时重刮就会并发打远端，正是本队列串行化要防的事（2026-10-17
    生产 run 4811/4814 就是两条 rescrape 同进程并发跑了十几分钟）。

    语义与排队任务一致：挂载被占用时**阻塞等待**（排队而不是报错），全部
    空闲后一次性登记占用（多挂载不会拿一半等一半，不存在交叉死锁）；退出
    时只释放登记在自己名下的，并唤醒等待者。边界语义：同库排队任务被派发时
    它的 ``_release_locked`` 只释放 ``task.mount_ids`` 里的挂载；若守卫的 ids
    与任务的 mount_ids 不一致（并发窗口内改了库挂载配置），多出的挂载不交
    给它，守卫自己收回——否则多余挂载在 ``_MOUNT_OWNER`` 里永久残留、
    等待者挂起。与 ``_limits_locked`` 同口径：
    队列关（``SCAN_QUEUE_ENABLED=0``）或挂载串行关（``SCAN_MOUNT_SERIAL=0``）
    时不限制、直接放行；没有远程挂载的库也直接放行。

    占用是进程内状态：它串行化本进程内的扫描（rescrape 之间；单体模式下
    还有 rescrape 与排队扫描之间）。拆分模式下 worker 进程的排队扫描有它
    自己进程内的占用表，两边互不可见——那是队列既有的进程内语义，本函数
    不改变它。

    嵌套说明：同库嵌套 guard 允许进入（等待条件自排除，不自死锁），但内层
    退出会收回它名下的挂载（外层若仍持有同挂载会被提前释放）——当前生产
    只有一个调用点（admin_scrape rescrape），无嵌套路径；需要嵌套的调用方
    先来对齐语义再动手。
    """
    ids = tuple(sorted({int(m) for m in (mount_ids or ())}))
    if not ids or not SCAN_QUEUE_ENABLED or not SCAN_MOUNT_SERIAL:
        yield
        return
    owner = int(library_id)
    with _COND:
        while any(_MOUNT_OWNER.get(m) not in (None, owner) for m in ids):
            _COND.wait(timeout=0.25)
        for m in ids:
            _MOUNT_OWNER[m] = owner
    logger.info("队列外扫描占用挂载 %s（库 id=%s）", _mounts_text(ids), owner)
    try:
        yield
    finally:
        with _COND:
            # 同库的排队任务若在本守卫持有期间被派发（``_conflicts_locked``
            # 不把本库自己的占用算冲突），它覆盖到的挂载已归它管，释放
            # 交给它的 ``_release_locked``；守卫只收回「任务 mount_ids 没覆盖」
            # 的那些挂载（并发窗口内改了库挂载配置时 guard 的 ids 与任务
            # mount_ids 不一致，多出的挂载不许让给 _release_locked——它只
            # pop task.mount_ids 里的，多余的会永久泄漏挂起等待者）。
            covered: set = set()
            running = _RUNNING.get(owner)
            if running is not None:
                covered = {int(m) for m in (running.mount_ids or ())}
            for m in ids:
                if m in covered:
                    continue
                if _MOUNT_OWNER.get(m) == owner:
                    _MOUNT_OWNER.pop(m, None)
            _COND.notify_all()


def _mounts_text(mount_ids) -> str:
    """日志里的挂载：没有就写「无」，有就写成 [1, 3]（名字由调用方 / 面板负责）"""
    ids = [int(m) for m in (mount_ids or ())]
    return "[" + ", ".join(str(mid) for mid in ids) + "]" if ids else "无"


def _push_history_locked(task: ScanTask) -> None:
    if SCAN_QUEUE_HISTORY <= 0:
        return
    _HISTORY.append(task)
    while len(_HISTORY) > SCAN_QUEUE_HISTORY:
        _HISTORY.pop(0)


def _ack_redis_raws(task: ScanTask) -> None:
    """把任务携带的 Redis 原始 payload 全部 ACK（确认完成）

    P0-0c 修复的核心：ACK 只能发生在扫描真正完成后（``_run_task`` 的 finally、
    或 ``cancel()`` 取消排队时），绝不能在「转入内存队列」时就确认——
    否则 worker 进程在扫描完成前崩溃，任务会无声丢失。

    在锁外调用：Redis I/O 不可预测，绝不能在持有 ``_LOCK``/``_COND`` 时做。
    所有异常内部消化：ACK 失败时任务仍留在 Redis ``processing`` 里，
    worker 重启后 ``recover_processing_queue`` 会把它捞回来重扫（at-least-once），
    不会丢，只会多扫一次。
    """
    raws = list(task.redis_raws or [])
    task.redis_raws.clear()
    if not raws:
        return
    try:
        from backend.emby_server import scan_queue_redis as _rq
        for _raw in raws:
            _rq.ack_scan_request(_raw)
        logger.info("Redis 扫描任务已 ACK（扫描完成）：库 id=%s，确认 %d 个",
                    task.library_id, len(raws))
    except Exception:
        logger.exception("Redis 扫描任务 ACK 失败：库 id=%s（任务仍在 processing，重启后会恢复）",
                         task.library_id)


def _run_task(task: ScanTask) -> None:
    """在工作线程里真正跑一轮扫描（独立 Session，与升级前的后台线程一致）"""
    from backend.emby_server.scanner import scan_library_sync  # 延迟导入，见模块 docstring

    db = SessionLocal()
    # expire_on_commit=False：扫描是"一批一提交"的长事务，ctx 里还缓存着
    # series/season 的 ORM 对象。默认的 expire_on_commit=True 会在每次
    # commit_batch() 后把它们全部标过期，下次访问触发懒加载；若此时恰好有
    # 别的线程（共享进程级 _SCAN_POOL 的 IO 任务）碰到这些对象或 Session，
    # 就会撞上 "provisioning a new connection; concurrent operations are
    # not permitted"。关掉过期后对象保持内存值，扫描只读写自己刚写的数据，
    # 不依赖提交后重读，因此安全。
    # （用属性赋值而非 SessionLocal(expire_on_commit=False)，兼容测试里的
    # SessionLocal 打桩。）
    db.expire_on_commit = False
    # 开扫前绑定「当前线程在扫哪个库」并建进度条目：绑定之后 scanner 的包装层
    # （枚举 / 处理 / 阶段）才知道该把数字记到谁名下；结束无论成败都解绑
    progress.begin_scan(task.library_id, task.name)
    remote_before = progress.remote_stats()
    try:
        library = db.query(em.Library).filter(em.Library.id == task.library_id).first()
        if library is None:
            task.state = STATE_FAILED
            task.result = STATE_FAILED
            task.error = "媒体库已被删除"
            return
        scan_stats = scan_library_sync(db, library, task.snapshot, trigger=task.trigger)
        task.state = STATE_DONE
        task.result = (library.scan_status or "success")
        # 新片入库通知：只在成功/部分成功且本轮有新增时触发。
        # maybe_notify_new_media 只是起一个 daemon 线程就返回，TG 投递再慢
        # 也不会拖住扫描收尾；通知内部用独立 Session，不碰扫描的 Session。
        if task.result in ("success", "partial"):
            try:
                from backend.emby_server import new_media_notify  # 延迟导入，见模块 docstring

                new_media_notify.maybe_notify_new_media(
                    library_id=task.library_id,
                    library_name=task.name,
                    started_at=task.started_at,
                    added_count=(scan_stats or {}).get("added", 0),
                )
            except Exception:  # noqa: BLE001 — 通知入口本身异常也不能影响扫描
                logger.warning("新片入库通知入口异常（库 id=%s）", task.library_id, exc_info=True)
    except Exception as exc:  # noqa: BLE001 — 后台线程的异常必须落到队列状态里
        # 诊断字段：线程名 + Session id，用于定位 Session 跨线程问题
        # （logger.exception 自带 traceback，级别为 error）
        logger.exception(
            "媒体库 %s 扫描失败 trigger=_run_task thread=%s session_id=%s",
            task.library_id, threading.current_thread().name, id(db),
        )
        task.state = STATE_FAILED
        task.result = STATE_FAILED
        task.error = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        task.finished_at = datetime.now()
        # r46补遗：BaseException（部署重启时的 KeyboardInterrupt/SystemExit，
        # scanner.py 注释证实真实发生过）会穿透上面的 except Exception——
        # state 会永远卡在 RUNNING，队列面板永久显示"运行中"。
        # 兜底：只要还没落到终态，就诚实记成失败，不谎报运行中。
        if task.state == STATE_RUNNING:
            task.state = STATE_FAILED
            task.result = STATE_FAILED
            task.error = "任务被中断（进程退出/重启），未完成"
        if task.started_at is not None:
            task.duration_ms = int((task.finished_at - task.started_at).total_seconds() * 1000)
        # 这一轮期间的远程列举次数（全局计数器的差值）：并发时两轮共用一个计数器，
        # 所以它是「这一轮跑的时间里发生了多少次」，不是「只因这一轮发生」——足够回答
        # 「慢在网络吗」，不需要为此把计数器拆到每个扫描线程（远程请求发生在 IO 线程池里）
        after = progress.remote_stats()
        task.remote_lists = max(0, int(after["lists"]) - int(remote_before["lists"]))
        task.remote_reused = max(0, int(after["reused"]) - int(remote_before["reused"]))
        progress.end_scan(task.library_id)
        try:
            db.close()   # 先关扫描会话：它可能还攥着 SQLite 的读事务，边上有写就会 "database is locked"
        except Exception:  # noqa: BLE001
            pass
        # 清进度快照要在「标成已结束」之前：反过来会留下一个「队列面板已经显示跑完、
        # 库里却还挂着上一轮进度」的瞬间（面板就会显示成「完成 + 正在处理 1234」）
        clear_progress(task.library_id)
        logger.info("扫描结束：库「%s」(id=%s) → %s；耗时 %.1fs；本轮远程列举 %s 次（复用 %s）",
                    task.name or "?", task.library_id, task.result or task.state,
                    (task.duration_ms or 0) / 1000, task.remote_lists, task.remote_reused)
        with _COND:
            _release_locked(task)
        # v2.57.1：运行中被合并了更宽范围的请求 → 按登记的快照补扫一轮。
        # 放在 _release_locked 之后：此时本库已无在跑任务，补扫按正常路径入队。
        pending_snapshot = task.pending_rescan_snapshot
        pending_trigger = task.pending_rescan_trigger
        _rescan_ok = True
        if pending_snapshot is not None:
            _lib_id = int(task.library_id)
            try:
                import types
                _enqueue_with_snapshot(
                    types.SimpleNamespace(id=_lib_id),
                    pending_snapshot, trigger=pending_trigger)
                logger.info("扫描补扫已入队：库 id=%s（触发=%s，运行中被合并的更宽范围）",
                            task.library_id, pending_trigger)
                # P1-2(r32)：成功后才清快照。
                task.pending_rescan_snapshot = None
                # R33 P3-7：成功清零连续失败计数。
                _RESCAN_FAIL_COUNTS.pop(_lib_id, None)
            except Exception:
                # R33 P3-7：补扫入队持久失败 → 死信。5 次以内沿用 r32 语义
                # （快照保留、raw 不确认，等 Redis 重投）；连续 5 次失败则
                # ACK raws（毒快照进死信，不再每次 worker 重启全量重扫），
                # error 日志留痕，人工可在队列面板重触发，计数清零。
                _fails = _RESCAN_FAIL_COUNTS.get(_lib_id, 0) + 1
                if _fails >= _RESCAN_FAIL_DEAD_LETTER_AFTER:
                    _RESCAN_FAIL_COUNTS.pop(_lib_id, None)
                    task.pending_rescan_snapshot = None
                    logger.error(
                        "扫描补扫入队连续 %d 次失败（库 id=%s）：毒快照进死信，"
                        "Redis raw 已确认，人工可在队列面板重触发",
                        _fails, task.library_id)
                else:
                    _RESCAN_FAIL_COUNTS[_lib_id] = _fails
                    _rescan_ok = False
                    # P1-2(r32)：失败时 task 随收尾丢弃，内存快照无用；真正的恢复
                    # 靠 Redis raw 重投（重投后按 raw 重建快照重新入队），扫描范围
                    # 不静默丢失。
                    logger.exception("扫描补扫入队失败（库 id=%s），Redis raw 不确认等待重投",
                                     task.library_id)
        # P0-0c：Redis 桥接的任务在这里才 ACK——扫描真正完成了。
        # 注意在 _COND 锁之外做（Redis I/O 不可预测），_ack_redis_raws 内部清掉已携带的 raw。
        # P1-2(r32)：补扫入队失败时不 ACK——被合并请求的 raw 由 Redis 重投恢复
        # （本任务自己的 raw 也会重投，多扫一次，不丢数据）。
        if _rescan_ok:
            _ack_redis_raws(task)
        else:
            logger.warning("扫描补扫入队失败：%d 个 Redis raw 暂不确认，等待重投（库 id=%s）",
                           len(task.redis_raws or []), task.library_id)


def _flusher_loop() -> None:
    """把正在跑的进度定期刷盘（刷新页面 / 另一个进程也能看到）"""
    while True:
        time.sleep(SCAN_PROGRESS_FLUSH_SECONDS)
        try:
            if progress.session_depth() > 0:
                flush_once()
        except Exception as exc:  # noqa: BLE001 — 刷盘失败不影响扫描
            logger.debug("扫描进度刷盘失败: %s", exc)


def reset_for_tests() -> None:
    """清空队列状态（测试用；生产代码不调用）"""
    global _SCHEDULER
    with _COND:
        _QUEUE.clear()
        _RUNNING.clear()
        _HISTORY.clear()
        _MOUNT_OWNER.clear()
        _RESCAN_FAIL_COUNTS.clear()
        _SCHEDULER = None
        progress.reset()

"""
扫描队列 Redis 桥接（后端拆分 v2.41.0）

API 进程与 Worker 进程分离后，扫描触发必须跨进程：
- API 进程（AETRIX_ROLE=api）：enqueue() 把 {library_id, trigger} 推入 Redis list
- Worker 进程（AETRIX_ROLE=worker）：start_redis_consumer() 用 BLPOP 消费，
  拿到 library_id 后查 DB 取 library 对象，再走 scan_queue 原有的进程内入队逻辑

Redis key：
- aetrix:scan:queue      扫描请求队列（list，FIFO：RPUSH 入队 / BLMOVE 原子转移）
- aetrix:scan:processing 处理中队列（list，worker 崩溃时任务在此，可恢复）
- aetrix:scan:dedup      去重集合（set，library_id，防止重复入队）

可靠性：BLMOVE 原子地把任务从 queue 移到 processing，worker 崩溃后
任务仍在 processing 中；worker 启动时自动把 processing 的任务移回 queue。

单体模式（不设 AETRIX_ROLE）下本模块不启用，走原有进程内队列。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

REDIS_SCAN_QUEUE_KEY = "aetrix:scan:queue"
REDIS_SCAN_DEDUP_KEY = "aetrix:scan:dedup"
REDIS_SCAN_PROCESSING_KEY = "aetrix:scan:processing"
# 去重集合的过期时间（秒）：防止 worker 崩溃后 dedup 残留导致永远入队失败
DEDUP_TTL_SECONDS = 3600

_consumer_thread: threading.Thread | None = None
_consumer_stop = threading.Event()


def _redis():
    """拿 Redis 客户端（backend.database.redis_client）"""
    from backend.database import redis_client
    return redis_client


def is_redis_mode() -> bool:
    """当前进程是否应该用 Redis 桥接（API 角色且 Redis 可用）"""
    role = os.getenv("AETRIX_ROLE", "").strip().lower()
    if role != "api":
        return False
    return _redis() is not None


def push_scan_request(library_id: int, trigger: str = "manual",
                      force_full: bool = False) -> dict:
    """API 进程：把扫描请求推入 Redis 队列

    返回 {"created": bool, "task": {...}}，与 scan_queue.enqueue() 的返回形状一致。
    ``force_full`` 随 payload 带到 worker（手动「全量扫描」在多进程部署下也得算数）。
    """
    r = _redis()
    if r is None:
        raise RuntimeError("Redis 不可用，无法跨进程入队")
    library_id = int(library_id)
    # 去重：已在队列里就不再推
    added = r.sadd(REDIS_SCAN_DEDUP_KEY, library_id)
    if not added:
        # 已在队列中，返回当前位置
        position = _position_of(library_id)
        return {
            "created": False,
            "task": {
                "library_id": library_id,
                "trigger": trigger,
                "state": "queued",
                "position": position,
                "via": "redis",
            },
        }
    payload = json.dumps({
        "library_id": library_id,
        "trigger": str(trigger or "manual"),
        "force_full": bool(force_full),
        "enqueued_at": time.time(),
    })
    r.rpush(REDIS_SCAN_QUEUE_KEY, payload)
    r.expire(REDIS_SCAN_DEDUP_KEY, DEDUP_TTL_SECONDS)
    position = r.llen(REDIS_SCAN_QUEUE_KEY)
    logger.info("扫描请求已推入 Redis 队列：库 id=%s 触发=%s（队列长度 %s）",
                library_id, trigger, position)
    return {
        "created": True,
        "task": {
            "library_id": library_id,
            "trigger": trigger,
            "state": "queued",
            "position": position,
            "via": "redis",
        },
    }


def _position_of(library_id: int) -> int | None:
    """查某个库在 Redis 队列中的位置（1-based），不在队列返回 None"""
    r = _redis()
    if r is None:
        return None
    try:
        items = r.lrange(REDIS_SCAN_QUEUE_KEY, 0, -1)
        for idx, raw in enumerate(items):
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if int(data.get("library_id", -1)) == int(library_id):
                return idx + 1
    except Exception as e:
        logger.warning(f"查询 Redis 队列位置失败：{e}")
    return None


def cancel_scan_request(library_id: int) -> str:
    """API 进程：从 Redis 队列取消排队中的扫描

    返回 canceled / running / missing（语义与 scan_queue.cancel 一致）。
    """
    r = _redis()
    if r is None:
        return "missing"
    library_id = int(library_id)
    try:
        # worker 消费时会先从 dedup 集合删除；如果 dedup 里没有，说明已在跑或不在队列
        if not r.sismember(REDIS_SCAN_DEDUP_KEY, library_id):
            return "missing"
        # 从 list 里删除该库的所有排队项
        items = r.lrange(REDIS_SCAN_QUEUE_KEY, 0, -1)
        removed = 0
        for raw in items:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if int(data.get("library_id", -1)) == library_id:
                r.lrem(REDIS_SCAN_QUEUE_KEY, 1, raw)
                removed += 1
        r.srem(REDIS_SCAN_DEDUP_KEY, library_id)
        if removed:
            logger.info(f"已从 Redis 队列取消扫描：库 id={library_id}")
            return "canceled"
        # dedup 里有但 list 里没有：worker 刚取走正在跑
        return "running"
    except Exception as e:
        logger.warning(f"从 Redis 队列取消扫描失败：{e}")
        return "missing"


def _blocking_redis(timeout: int):
    """拿一个 socket 超时大于阻塞时长的 Redis 客户端。

    共享的 ``redis_client`` 把 ``socket_timeout`` 设成 5s，而 BLMOVE 的阻塞
    时长也是 5s：队列一空，两边同时到点，客户端先抛
    ``Timeout reading from socket``，于是空转轮询每 10s 报一次假警。
    阻塞读必须让 socket 超时大于阻塞时长 + 余量。

    连接参数从**共享客户端**复制（而不是重新解析 REDIS_URL 直连）：
    单测给 ``_redis`` 打的桩（FakeRedis 等）原样复用——绕过它去连真实
    Redis 会让 push 走桩、pop 连真库，无 Redis 的 CI 上冒烟全灭
    （v2.42.4 修的回归）。

    返回 (client, owned)：owned=True 表示本调用新建的客户端（用完要 close）。

    P0（2026-10-01）：``connection_class`` 是 **ConnectionPool** 的参数，不是
    ``Redis()`` 的 —— 之前直接传给 ``Redis()``，每次阻塞读都抛
    ``TypeError: unexpected keyword argument 'connection_class'``，被 except 吞掉后
    退回共享客户端（socket_timeout 又是 5s），于是 BLMOVE 的空转超时原样复发
    ——修法等于没上（生产每 10 秒一条告警）。这里改为先把连接池建出来，
    再把池交给 Redis()。
    """
    base = _redis()
    if base is None:
        return None, False
    pool = getattr(base, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None)
    if not isinstance(kwargs, dict):
        # 测试桩或非常规客户端：没有连接参数可调，直接复用
        return base, False
    import redis as _redis_mod

    client_kwargs = dict(kwargs)
    client_kwargs["socket_timeout"] = timeout + 5
    client_kwargs.setdefault("socket_connect_timeout", 5)
    try:
        conn_class = getattr(pool, "connection_class", None) or _redis_mod.Connection
        blocking_pool = _redis_mod.ConnectionPool(
            connection_class=conn_class, **client_kwargs)
        return _redis_mod.Redis(connection_pool=blocking_pool), True
    except Exception as e:  # noqa: BLE001 — 专用客户端拿不到就退回共享的
        logger.warning(f"构造阻塞读专用 Redis 客户端失败，退回共享客户端：{e}")
        return base, False


def pop_scan_request(timeout: int = 5) -> Optional[dict]:
    """Worker 进程：原子取出一个扫描请求（BLMOVE）

    用 BLMOVE 原子地把任务从 queue 移到 processing，worker 崩溃后
    任务仍在 processing 中，重启时可恢复，不丢任务。

    返回 {"library_id": int, "trigger": str, "force_full": bool, "_raw": str}，超时返回 None。
    _raw 用于 ack 时从 processing 队列删除。
    """
    r, owned = _blocking_redis(timeout)
    if r is None:
        return None
    try:
        # BLMOVE 原子操作：queue -> processing，超时返回 None
        # Redis 6.2+ 支持 BLMOVE，旧版用 brpoplpush
        try:
            raw = r.blmove(REDIS_SCAN_QUEUE_KEY, REDIS_SCAN_PROCESSING_KEY, timeout=timeout)
        except AttributeError:
            raw = r.brpoplpush(REDIS_SCAN_QUEUE_KEY, REDIS_SCAN_PROCESSING_KEY, timeout=timeout)
        if not raw:
            return None
        # raw 可能是 bytes 或 str（取决于 redis client 配置），保持原样用于 ack
        data = json.loads(raw)
        library_id = int(data["library_id"])
        return {
            "library_id": library_id,
            "trigger": data.get("trigger", "manual"),
            "force_full": bool(data.get("force_full")),
            "_raw": raw,
        }
    except Exception as e:
        logger.warning(f"从 Redis 取扫描请求失败：{e}")
        return None
    finally:
        if owned:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass


def ack_scan_request(raw) -> None:
    """确认任务完成：从 processing 队列删除，并清除 dedup 标记
    
    raw: pop_scan_request 返回的 _raw（bytes 或 str，保持原样传入）
    """
    r = _redis()
    if r is None:
        return
    try:
        # 从 processing 删除（只删一个）；lrem 能处理 bytes/str
        r.lrem(REDIS_SCAN_PROCESSING_KEY, 1, raw)
        # 清除去重标记，允许再次入队
        try:
            data = json.loads(raw)
            r.srem(REDIS_SCAN_DEDUP_KEY, int(data["library_id"]))
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"确认扫描请求失败：{e}")


def recover_processing_queue() -> int:
    """Worker 启动时：把 processing 中的未完成任务移回 queue

    返回恢复的任务数。
    """
    r = _redis()
    if r is None:
        return 0
    try:
        count = 0
        # 把 processing 的任务逐个移回 queue（RPUSH 保持顺序）
        while True:
            raw = r.rpoplpush(REDIS_SCAN_PROCESSING_KEY, REDIS_SCAN_QUEUE_KEY)
            if not raw:
                break
            count += 1
        if count > 0:
            logger.info(f"从 processing 队列恢复 {count} 个未完成扫描任务")
        return count
    except Exception as e:
        logger.warning(f"恢复 processing 队列失败：{e}")
        return 0


def start_redis_consumer() -> bool:
    """Worker 进程：启动 Redis 扫描队列消费线程

    循环 BLPOP，拿到请求后查 DB 取 library，再调 scan_queue 的进程内入队。
    返回 True 表示消费线程已启动。
    """
    global _consumer_thread
    if _consumer_thread is not None and _consumer_thread.is_alive():
        return True
    r = _redis()
    if r is None:
        logger.error("Redis 不可用，无法启动扫描队列消费")
        return False
    _consumer_stop.clear()
    # 启动时恢复未完成的任务（worker 崩溃后残留）
    try:
        recovered = recover_processing_queue()
        if recovered > 0:
            logger.info(f"Worker 启动：恢复 {recovered} 个未完成任务")
    except Exception as e:
        logger.warning(f"恢复 processing 队列失败：{e}")
    _consumer_thread = threading.Thread(
        target=_consume_loop, daemon=True, name="scan-redis-consumer"
    )
    _consumer_thread.start()
    logger.info("Redis 扫描队列消费线程已启动")
    return True


def stop_redis_consumer(timeout: float = 5.0) -> None:
    """停止消费线程（优雅关闭时调用）"""
    _consumer_stop.set()
    if _consumer_thread is not None and _consumer_thread.is_alive():
        _consumer_thread.join(timeout=timeout)


def _consume_loop():
    """消费循环：BLPOP → 查库 → 进程内入队

    P0-0c：这里**不再**提前 ACK。raw 随任务传给 ``enqueue_local``，
    由 ``scan_queue`` 在扫描真正完成后（或取消）才确认——worker 在扫描完成前
    崩溃时，任务仍在 Redis ``processing`` 里，重启后自动恢复重扫，不丢任务。
    """
    logger.info("扫描队列消费循环开始")
    # 延迟导入：避免循环导入
    from backend.database import SessionLocal
    from backend.emby_server import models as em
    from backend.emby_server import scan_queue

    while not _consumer_stop.is_set():
        try:
            req = pop_scan_request(timeout=5)
        except Exception as e:
            logger.warning(f"消费循环取请求异常：{e}")
            _consumer_stop.wait(5)
            continue
        if req is None:
            continue
        library_id = req["library_id"]
        trigger = req["trigger"]
        raw = req.get("_raw", "")
        try:
            with SessionLocal() as db:
                lib = db.get(em.Library, library_id)
                if lib is None:
                    logger.warning(f"Redis 扫描请求：库 id={library_id} 不存在，跳过")
                    # 库不存在也要 ACK，否则会一直残留在 processing
                    if raw:
                        ack_scan_request(raw)
                    continue
                # 走进程内入队（worker 进程内的 scan_queue 完整逻辑：串行化/并发上限）。
                # raw 随任务携带，扫描真正完成后才 ACK（见 _ack_redis_raws）。
                result = scan_queue.enqueue_local(lib, trigger=trigger, redis_raw=raw or None,
                                                force_full=bool(req.get("force_full")))
                logger.info(f"Redis 扫描请求已转进程内队列：库「{lib.name}」(id={library_id}) "
                            f"触发={trigger} created={result.get('created')}")
                # expunge：enqueue_local 内部会拍快照，lib 不能随 session 关闭失效
                db.expunge(lib)
        except Exception:
            # P2 修复（完美审查 C-P2-2）：消费失败必须 ACK（承认本次消费失败），
            # 否则任务永久滞留 processing——只能靠 worker 重启时的
            # recover_processing_queue 捞回；确定性失败（如库配置损坏）则卡死，
            # 且 dedup 集合 TTL 3600s 内 API 侧重入队还会被去重挡掉。
            # （注意：这里 ACK 不违背 P0-0c——异常意味着任务根本没进本地队列，
            # 没有"扫描完成前崩溃可恢复"的对象；下次 API 入队可重试。）
            logger.exception(f"处理 Redis 扫描请求失败：库 id={library_id}")
            if raw:
                ack_scan_request(raw)
    logger.info("扫描队列消费循环结束")

"""
Aetrix Worker - 后台任务独立进程入口

与 API 进程（backend/main.py）分离：只跑后台任务，不监听 HTTP。
通过环境变量 AETRIX_ROLE=worker 标识身份。

启动的后台任务：
- maintenance：崩溃恢复 + 定时维护（janitor）
- enrich_worker：分层扫描 L2/L3 补全
- reminders：订阅到期提醒
- auto_scan：定时扫描调度
- change_watcher：追新
- scan_queue：Redis 扫描队列消费（API 进程入队，worker 消费执行）

单实例保护：Redis 分布式锁（SET NX PX），防止多 worker 重复消费。
优雅关闭：SIGTERM/SIGINT → 停止接受新任务 → 等待当前任务完成 → 退出。

用法：
    AETRIX_ROLE=worker python3 -m backend.worker
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("aetrix-worker")

# 优雅关闭标志
_shutdown_event = threading.Event()

# Redis 单实例锁
_WORKER_LOCK_KEY = "aetrix:worker:lock"
_WORKER_LOCK_TTL_MS = 30000  # 30 秒，靠心跳续期
# 抢锁最多等多久：必须 > TTL，否则部署时旧实例还没走到释放，新实例就会
# 误判「已有 worker 在跑」直接退出（worker 退出 = 扫描/探测全停摆）。
_WORKER_LOCK_WAIT_SEC = max(0, int(os.getenv("WORKER_LOCK_WAIT_SEC", "45") or 45))
_worker_lock_token: str | None = None
_lock_renew_thread: threading.Thread | None = None


def _handle_signal(signum, frame):
    """信号处理：优雅关闭"""
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.info(f"收到信号 {sig_name}，开始优雅关闭...")
    _shutdown_event.set()


#: 等 Redis 的重试间隔上限（秒）
_REDIS_WAIT_MAX_SEC = max(1.0, float(os.getenv("WORKER_REDIS_WAIT_MAX", "30") or 30))


def _wait_for_redis(max_wait: float | None = None):
    """等到 Redis 可用为止（指数退避 1s→2s→…→WORKER_REDIS_WAIT_MAX），返回客户端

    - ``REDIS_ENABLED=false``：配置错误，等也等不来 → 记错误返回 None；
    - 收到关闭信号 / 超过 ``max_wait``（测试用）→ 返回 None。
    读的是 ``backend.database.redis_client`` 的**当前值**（后台重连线程连上后会更新它），
    同时自己也按退避间隔主动 ping 一次。
    """
    from backend import database as dbmod

    if not dbmod.REDIS_ENABLED:
        logger.error("REDIS_ENABLED=false：worker 依赖 Redis（单实例锁 + 扫描队列），无法启动")
        return None
    delay = 1.0
    started = time.monotonic()
    warned = False
    while not _shutdown_event.is_set():
        client = dbmod.redis_client
        if client is None:
            try:
                candidate = dbmod._make_redis_client()
                candidate.ping()
                dbmod.redis_client = client = candidate
                dbmod.redis_breaker.reset()
            except Exception as exc:  # noqa: BLE001
                client = None
                last_error = exc
        else:
            try:
                client.ping()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                client = None
        if client is not None:
            if warned:
                logger.info("Redis 已可用（等待 %.0fs）", time.monotonic() - started)
            return client
        if max_wait is not None and time.monotonic() - started >= max_wait:
            return None
        if not warned:
            logger.warning("Redis 暂不可用（%s），worker 等待重连…", last_error)
            warned = True
        else:
            logger.info("仍在等待 Redis（%.0fs）：%s", time.monotonic() - started, last_error)
        _shutdown_event.wait(delay)
        delay = min(delay * 2, _REDIS_WAIT_MAX_SEC)
    return None


def _acquire_worker_lock(redis_client) -> bool:
    """获取 worker 单实例锁（Redis SET NX PX）。

    抢不到时最多轮询 ``_WORKER_LOCK_WAIT_SEC``，等的是旧实例的锁按 TTL
    自然过期，而不是死等；超时仍失败才判定真的存在第二个实例并退出。
    """
    global _worker_lock_token
    import uuid

    deadline = time.monotonic() + _WORKER_LOCK_WAIT_SEC
    warned = False
    while True:
        token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            acquired = redis_client.set(
                _WORKER_LOCK_KEY, token, nx=True, px=_WORKER_LOCK_TTL_MS
            )
            if acquired:
                _worker_lock_token = token
                logger.info(f"已获取 worker 单实例锁（token={token}）")
                return True
            holder = redis_client.get(_WORKER_LOCK_KEY)
        except Exception as e:  # noqa: BLE001
            logger.error(f"获取 worker 锁失败：{e}")
            return False
        if time.monotonic() >= deadline:
            logger.error(f"已有 worker 实例在运行（锁持有者={holder}），本实例退出")
            return False
        if not warned:
            logger.warning(
                f"worker 锁被占用（持有者={holder}），"
                f"最多等待 {_WORKER_LOCK_WAIT_SEC}s 让其释放…"
            )
            warned = True
        time.sleep(2)


def _renew_worker_lock(redis_client):
    """后台线程：定期续期 worker 锁"""
    global _worker_lock_token
    while not _shutdown_event.is_set():
        try:
            # 只续自己的锁（Lua 脚本保证原子性）
            # 注意：命令名必须是字符串字面量。写成 redis.call(get, ...) 会被
            # Redis 7.x 判为「访问不存在的全局变量 get」而整段失败，
            # 结果续期静默失效 → 锁 30s 到期自动过期 → 单实例保护形同虚设。
            renewed = redis_client.eval(
                'if redis.call("GET", KEYS[1]) == ARGV[1] then '
                'return redis.call("PEXPIRE", KEYS[1], ARGV[2]) else return 0 end',
                1, _WORKER_LOCK_KEY, _worker_lock_token, _WORKER_LOCK_TTL_MS,
            )
            if not renewed:
                logger.error("worker 锁续期失败（锁已丢失），准备退出")
                _shutdown_event.set()
                break
        except Exception as e:
            logger.warning(f"worker 锁续期异常：{e}")
        # 每 TTL/3 续期一次
        _shutdown_event.wait(_WORKER_LOCK_TTL_MS / 3000.0)


def _release_worker_lock(redis_client):
    """释放 worker 锁（只释放自己的）"""
    global _worker_lock_token
    if not _worker_lock_token:
        return
    try:
        redis_client.eval(
            'if redis.call("GET", KEYS[1]) == ARGV[1] then '
            'return redis.call("DEL", KEYS[1]) else return 0 end',
            1, _WORKER_LOCK_KEY, _worker_lock_token,
        )
        logger.info("已释放 worker 单实例锁")
    except Exception as e:
        logger.warning(f"释放 worker 锁失败：{e}")
    finally:
        _worker_lock_token = None


_WORKER_STARTED_AT = time.time()

def _write_heartbeat(redis_client):
    """写 worker 心跳（供监控/健康检查）"""
    try:
        import json
        heartbeat = {
            "pid": os.getpid(),
            "started_at": _WORKER_STARTED_AT,
            "updated_at": time.time(),
            "role": "worker",
        }
        redis_client.setex("aetrix:worker:heartbeat", 60, json.dumps(heartbeat))
    except Exception as e:
        logger.warning(f"写 worker 心跳失败：{e}")


def main() -> int:
    """Worker 主入口"""
    logger.info("🚀 Aetrix Worker 正在启动...")

    # 1. 密钥校验（fail-closed）
    from backend.security import validate_secret_key
    try:
        validate_secret_key()
    except Exception:
        logger.exception("JWT 密钥校验失败，worker 拒绝启动")
        return 1

    # 2. 数据库初始化
    from backend.database import init_db, DATABASE_TYPE
    logger.info(f"📊 数据库类型: {DATABASE_TYPE}")
    try:
        init_db()
    except Exception:
        logger.exception("数据库初始化失败，worker 拒绝启动")
        return 1

    # 配置自愈（backend/config_self_heal.py）：只补缺失、不覆盖；失败只记日志，不阻断启动
    try:
        from backend import config_self_heal
        config_self_heal.run_config_self_heal()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"配置自愈失败（可忽略）: {e}")

    # 信号处理提前装：等 Redis 期间也要能被 SIGTERM 优雅叫停
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # 3. Redis 连接（worker 必须有 Redis：单实例锁 + 扫描队列都依赖它）
    # S5：Redis 启动慢 / 短暂不可用时**等它**，而不是立刻退出——旧实现 20 秒内退出
    # 4 次就让 run_all 把整个容器（含 API 与 EA）拉停。
    redis_client = _wait_for_redis()
    if redis_client is None:
        return 0 if _shutdown_event.is_set() else 1
    logger.info("✅ Redis 连接正常")

    # 4. 单实例锁
    if not _acquire_worker_lock(redis_client):
        return 1

    # 6. 启动锁续期线程
    global _lock_renew_thread
    _lock_renew_thread = threading.Thread(
        target=_renew_worker_lock, args=(redis_client,), daemon=True, name="worker-lock-renew"
    )
    _lock_renew_thread.start()

    # 7. 启动所有后台任务（与 main.py lifespan 的 worker 部分保持一致）
    started = []
    try:
        from backend.emby_server import maintenance
        try:
            maintenance.run_startup_maintenance()
            if maintenance.start_janitor():
                started.append("janitor")
                logger.info("✅ 维护任务已启动")
        except Exception as e:
            logger.warning(f"启动维护任务失败（可忽略）: {e}")


        try:
            from backend.emby_server import enrich_worker
            enrich_worker.start()
            started.append("enrich_worker")
            logger.info("✅ 后台补全 worker 已启动")
        except Exception as e:
            logger.warning(f"启动补全 worker 失败（可忽略）: {e}")

        try:
            from backend.emby_server import probe_worker
            if probe_worker.start():
                started.append("probe_worker")
                logger.info("✅ 媒体信息探测 worker 已启动")
        except Exception as e:
            logger.warning(f"启动按需探测 worker 失败（可忽略）: {e}")

        try:
            from backend.emby_server import subtitle_scan_worker
            if subtitle_scan_worker.start():
                started.append("subtitle_scan_worker")
                logger.info("✅ 外挂字幕扫描 worker 已启动")
        except Exception as e:
            logger.warning(f"启动字幕扫描 worker 失败（可忽略）: {e}")

        try:
            from backend.emby_server import merge_versions_worker
            if merge_versions_worker.start():
                started.append("merge_versions_worker")
                logger.info("✅ 多版本合并 worker 已启动")
        except Exception as e:
            logger.warning(f"启动多版本合并 worker 失败（可忽略）: {e}")

        try:
            from backend.emby_server import thumbnail_worker
            if thumbnail_worker.start():
                started.append("thumbnail_worker")
                logger.info("✅ 视频缩略图 worker 已启动")
        except Exception as e:
            logger.warning(f"启动缩略图 worker 失败（可忽略）: {e}")

        try:
            from backend.emby_server import refresh_person_worker
            if refresh_person_worker.start():
                started.append("refresh_person_worker")
                logger.info("✅ 演员刷新 worker 已启动")
        except Exception as e:
            logger.warning(f"启动演员刷新 worker 失败（可忽略）: {e}")

        try:
            from backend import reminders
            if reminders.start_reminder_scheduler():
                started.append("reminders")
                logger.info("✅ 到期提醒已启动")
        except Exception as e:
            logger.warning(f"启动到期提醒失败（可忽略）: {e}")

        # C1 活力值：每日 00:00 扣减公益服用户活力值（daemon 线程，失败可忽略）
        try:
            from backend import vitality
            if vitality.start_vitality_scheduler():
                started.append("vitality")
                logger.info("✅ 活力值调度已启动")
        except Exception as e:
            logger.warning(f"启动活力值调度失败（可忽略）: {e}")

        # 红包过期自动退款（daemon 线程，失败可忽略）
        try:
            from backend import welfare_redpacket
            if welfare_redpacket.start_redpacket_refund_scheduler():
                started.append("redpacket_refund")
                logger.info("✅ 红包过期退款调度已启动")
        except Exception as e:
            logger.warning(f"启动红包退款调度失败（可忽略）: {e}")

        # 群抽奖自动开奖（daemon 线程，失败可忽略）
        try:
            from backend import lottery
            if lottery.start_lottery_auto_draw_scheduler():
                started.append("lottery_auto_draw")
                logger.info("✅ 群抽奖自动开奖调度已启动")
        except Exception as e:
            logger.warning(f"启动群抽奖自动开奖调度失败（可忽略）: {e}")

        try:
            from backend.emby_server import auto_scan
            if auto_scan.start_auto_scan_scheduler():
                started.append("auto_scan")
                logger.info("✅ 定时扫描已启动")
        except Exception as e:
            logger.warning(f"启动定时扫描失败（可忽略）: {e}")

        try:
            from backend.emby_server import db_backup
            if db_backup.start_backup_scheduler():
                started.append("db_backup")
                logger.info("✅ 数据库定时备份已启动")
        except Exception as e:
            logger.warning(f"启动数据库备份调度失败（可忽略）: {e}")

        try:
            from backend.tg_bot import scheduler as tg_bot_scheduler
            if tg_bot_scheduler.start_tg_bot_poller():
                started.append("tg_bot_poller")
                logger.info("✅ TG Bot 轮询器已启动")
        except Exception as e:
            logger.warning(f"启动 TG Bot 轮询器失败（可忽略）: {e}")

        try:
            from backend.emby_server import change_watcher
            change_watcher.start_chase_new_watcher()
            started.append("change_watcher")
            logger.info("✅ 追新已启动")
        except Exception as e:
            logger.warning(f"启动追新失败（可忽略）: {e}")

        # v2.52.0: Drive Changes API 增量发现（O(变化量) 代替 O(总量)）
        try:
            from backend.emby_server import drive_changes
            if drive_changes.start():
                started.append("drive_changes")
                logger.info("✅ Drive 增量发现已启动")
        except Exception as e:
            logger.warning(f"启动 Drive 增量发现失败（可忽略）: {e}")

        # VPS 本地缓存（播放线路 cache）：默认关闭；开启后串行、限速地拉热门片到本机
        try:
            from backend.emby_server import local_cache_worker
            if local_cache_worker.start_local_cache_worker():
                started.append("local_cache_worker")
                logger.info("✅ 本地缓存 worker 已启动")
        except Exception as e:
            logger.warning(f"启动本地缓存 worker 失败（可忽略）: {e}")

        # 本机目录实时监听（v2.46.0）：必须在这里也起一次。
        # main.py 里的同一段包在 `if not _is_api_role` 里，而 run_all.py 会把 serve.py
        # 子进程改写成 AETRIX_ROLE=api —— API 进程整块跳过、后台任务只由本进程承担。
        # 漏掉这一行 = 生产的 inotify 从来没启动过（新片只能等定时扫描）。
        try:
            from backend.emby_server import fs_watcher
            if fs_watcher.start_fs_watcher():
                started.append("fs_watcher")
                logger.info("✅ 本机目录实时监听已启动")
            else:
                logger.info("本机目录实时监听未启用，已降级为定时扫描")
        except Exception as e:
            logger.warning(f"启动目录监听失败（已降级为定时扫描）: {e}")

        # 8. 启动 Redis 扫描队列消费（API 进程入队，worker 消费执行）
        try:
            from backend.emby_server import scan_queue
            scan_queue.start_redis_consumer()
            started.append("scan_queue_consumer")
            logger.info("✅ Redis 扫描队列消费已启动")
        except Exception as e:
            logger.warning(f"启动扫描队列消费失败（可忽略）: {e}")

    except Exception:
        logger.exception("启动后台任务时发生未预期错误")
        _release_worker_lock(redis_client)
        return 1

    logger.info("✅ Aetrix Worker 启动完成（已启动 %d 个后台任务）" % len(started))
    _write_heartbeat(redis_client)

    # 9. 主循环：等关闭信号，定期写心跳
    try:
        while not _shutdown_event.is_set():
            _write_heartbeat(redis_client)
            _shutdown_event.wait(30)  # 每 30 秒写一次心跳
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt，开始关闭...")
        _shutdown_event.set()

    # 10. 优雅关闭：停后台任务
    logger.info("👋 Aetrix Worker 正在关闭...")
    try:
        from backend.emby_server import scan_queue as _sq
        _sq.stop_redis_consumer(timeout=5.0)
    except Exception as e:
        logger.warning(f"停止 Redis 扫描消费失败：{e}")
    # 探测 worker：把已抢未处理的条目放回 pending，下个实例立刻能接着做
    try:
        from backend.emby_server import probe_worker as _pw
        _pw.stop(timeout=5.0)
    except Exception as e:
        logger.warning(f"停止探测 worker 失败：{e}")


if __name__ == "__main__":
    import sys
    sys.exit(main())

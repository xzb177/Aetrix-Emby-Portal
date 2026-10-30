"""TG Bot 轮询器调度器：daemon 线程入口。"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_STARTED = False
_LOCK = threading.Lock()


def start_tg_bot_poller() -> bool:
    """启动 TG Bot 长轮询线程。同一进程只启动一次。

    返回 True 表示本次调用成功启动，False 表示已在运行或未配置。
    """
    global _STARTED
    with _LOCK:
        if _STARTED:
            return False
        _STARTED = True

    # 启动前检查 token 是否配置（未配置则静默不启动）
    try:
        from backend.database import SessionLocal
        from backend.integrations.telegram import token as get_token

        db = SessionLocal()
        try:
            if not get_token(db):
                logger.info("tg_bot_poller: 未配置 Bot Token，跳过启动")
                with _LOCK:
                    _STARTED = False
                return False
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tg_bot_poller: 启动前检查失败（可忽略）: %s", exc)
        with _LOCK:
            _STARTED = False
        return False

    from backend.tg_bot import poller

    t = threading.Thread(
        target=poller.poll_forever,
        name="tg-bot-poller",
        daemon=True,
    )
    t.start()
    logger.info("tg_bot_poller started")
    # 顺带启动观影榜海报日推调度（失败只记日志，不拖垮 poller）
    try:
        start_rankpush_scheduler()
    except Exception:
        logger.exception("rankpush 调度启动失败（可忽略）")
    return True


# ---------------- 观影榜海报日推调度（定时任务区） ----------------
# 日榜每日 20:30 / 周榜周日 21:00（Asia/Shanghai），时间与防重复推逻辑在
# tg_bot.rankpush.run_scheduled_tick；这里只负责起线程按分钟巡检。

_rankpush_started = False


def start_rankpush_scheduler() -> bool:
    """启动海报日推调度线程（进程内只起一次）。"""
    global _rankpush_started
    if _rankpush_started:
        return False
    _rankpush_started = True
    interval = 60

    def _tick() -> None:
        from backend.database import SessionLocal
        from backend.tg_bot import rankpush

        db = SessionLocal()
        try:
            actions = rankpush.run_scheduled_tick(db)
            if actions:
                logger.info("rankpush 调度动作：%s", actions)
        except Exception:
            logger.exception("rankpush 调度 tick 失败")
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    def _loop() -> None:
        waiter = threading.Event()
        while True:
            waiter.wait(interval)
            _tick()

    threading.Thread(target=_loop, daemon=True, name="tg-rankpush").start()
    logger.info("TG 观影榜海报日推调度已启动（日榜 20:30 / 周榜周日 21:00）")
    return True

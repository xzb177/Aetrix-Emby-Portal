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
    # 顺带启动「更新入库」攒批播报调度（同上，失败不拖垮 poller）
    try:
        start_update_digest_scheduler()
    except Exception:
        logger.exception("updatedigest 调度启动失败（可忽略）")
    try:
        from backend.tg_bot.proactive import wake_flusher

        wake_flusher()
    except Exception:
        logger.exception("主动私聊提醒调度启动失败（可忽略）")
    try:
        start_duel_scheduler()
    except Exception:
        logger.exception("duel 调度启动失败（可忽略）")
    try:
        start_rain_scheduler()
    except Exception:
        logger.exception("rain 调度启动失败（可忽略）")
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
            from backend.tg_bot import gamerank

            game_actions = gamerank.run_scheduled_tick(db)
            if game_actions:
                logger.info("gamerank 调度动作：%s", game_actions)
            # 每日播放排行榜（文字三榜）搭同一条分钟巡检（与 gamerank
            # 同路，不新起线程）；自带 try 隔离，失败不拖累同拍其他榜。
            try:
                from backend.tg_bot import playrank

                play_actions = playrank.run_scheduled_tick(db)
                if play_actions:
                    logger.info("playrank 调度动作：%s", play_actions)
            except Exception:
                logger.exception("playrank 调度 tick 失败")
                try:
                    db.rollback()
                except Exception:
                    pass
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


# ---------------- 更新入库播报调度（定时任务区） ----------------
# 每分钟巡检一拍，攒批间隔（默认 30 分钟）与防重复逻辑在
# tg_bot.updatedigest.run_scheduled_tick；这里只负责起线程。

_update_digest_started = False


def start_update_digest_scheduler() -> bool:
    """启动更新入库播报调度线程（进程内只起一次）。"""
    global _update_digest_started
    if _update_digest_started:
        return False
    _update_digest_started = True
    interval = 60

    def _tick() -> None:
        from backend.database import SessionLocal
        from backend.tg_bot import updatedigest

        db = SessionLocal()
        try:
            actions = updatedigest.run_scheduled_tick(db)
            if actions:
                logger.info("updatedigest 调度动作：%s", actions)
        except Exception:
            logger.exception("updatedigest 调度 tick 失败")
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

    threading.Thread(target=_loop, daemon=True, name="tg-updatedigest").start()
    logger.info("TG 更新入库播报调度已启动（攒批间隔见 bot_update_digest_interval_minutes）")
    return True


# ---------------- 骰子对决调度（定时任务区） ----------------
# 30 秒无人应战要退回，倒计时要改面板，分钟级巡检来不及。
# 这里每 5 秒一拍，具体逻辑在 tg_bot.duel_ui.tick。

_duel_started = False


def start_duel_scheduler() -> bool:
    """启动骰子对决调度线程（进程内只起一次）。"""
    global _duel_started
    if _duel_started:
        return False
    _duel_started = True
    interval = 5

    def _tick() -> None:
        from backend.database import SessionLocal
        from backend.tg_bot import duel_ui

        db = SessionLocal()
        try:
            actions = duel_ui.tick(db)
            if actions:
                logger.info("duel 调度动作：%s", actions)
        except Exception:
            logger.exception("duel 调度 tick 失败")
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

    threading.Thread(target=_loop, daemon=True, name="tg-duel").start()
    logger.info("TG 骰子对决调度已启动（每 %s 秒巡检超时与倒计时）", interval)
    return True


# ---------------- 红包雨调度（定时任务区） ----------------
# 份数按间隔落下，分钟级巡检会把间隔吃掉。这里每 2 秒看一次。

_rain_started = False


def start_rain_scheduler() -> bool:
    """启动红包雨调度线程（进程内只起一次）。"""
    global _rain_started
    if _rain_started:
        return False
    _rain_started = True
    interval = 2

    def _tick() -> None:
        from backend.database import SessionLocal
        from backend.tg_bot import rain_ui

        db = SessionLocal()
        try:
            actions = rain_ui.tick(db)
            if actions:
                logger.info("rain 调度动作：%s", actions)
        except Exception:
            logger.exception("rain 调度 tick 失败")
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

    threading.Thread(target=_loop, daemon=True, name="tg-rain").start()
    logger.info("TG 红包雨调度已启动（每 %s 秒巡检落下与定时开场）", interval)
    return True

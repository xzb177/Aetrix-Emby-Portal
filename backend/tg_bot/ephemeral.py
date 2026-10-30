"""TG Bot 消息自毁调度（用户 2026-10-30 口径）。

机器人群内消息默认 30 秒自毁、用户发的命令消息默认 3 秒自毁，
红包消息本体是明示例外（永不由本模块调度）。一期（本模块）只提供
延时读取与调度能力；router 的具体接线在二期统一挂。

实现说明：
- threading.Timer 进程内定时；进程重启会丢失未到期的定时器，
  这是可接受降级（自毁只是群面整洁手段，丢一两条不影响业务）。
- 到期触发时新开一个 SessionLocal 会话读 token 并调 deleteMessage，
  失败只记日志、绝不抛给调用方。
- 延时读 SystemConfig（store.get_value 热读口径）：
  bot_msg_autodelete_seconds 默认 30、bot_cmd_autodelete_seconds 默认 3，
  0 表示关闭（不调度）；配置写坏（非数字/负数）时回落默认值，
  不让脏配置把自毁变成即时删除或永不删除。
"""
from __future__ import annotations

import logging
from threading import Timer

from backend.integrations import store

logger = logging.getLogger(__name__)

BOT_MSG_CONFIG_KEY = "bot_msg_autodelete_seconds"
BOT_CMD_CONFIG_KEY = "bot_cmd_autodelete_seconds"
DEFAULT_BOT_MSG_SECONDS = 30
DEFAULT_BOT_CMD_SECONDS = 3


def _delay_seconds(db, key: str, default: int) -> int:
    """读一个延时配置（秒）；缺失/非法/负数时的口径见模块 docstring。"""
    try:
        raw = store.get_value(db, key, str(default))
        value = int(str(raw).strip())
    except Exception:
        return default
    if value < 0:
        return default
    return value


def bot_msg_delay_seconds(db) -> int:
    """机器人消息自毁延时（秒，默认 30；0=关闭）。"""
    return _delay_seconds(db, BOT_MSG_CONFIG_KEY, DEFAULT_BOT_MSG_SECONDS)


def bot_cmd_delay_seconds(db) -> int:
    """用户命令消息自毁延时（秒，默认 3；0=关闭）。"""
    return _delay_seconds(db, BOT_CMD_CONFIG_KEY, DEFAULT_BOT_CMD_SECONDS)


def _delete_now(chat_id: int, message_id: int) -> None:
    """定时器到期执行体：新开会话调 deleteMessage，失败只记日志。"""
    from backend.database import SessionLocal
    from backend.tg_bot import sender

    db = SessionLocal()
    try:
        ok, err = sender.delete_message(db, chat_id, message_id)
        if not ok:
            logger.warning(
                "tg ephemeral delete failed: chat=%s message=%s err=%s",
                chat_id, message_id, err)
    except Exception:
        logger.warning(
            "tg ephemeral delete error: chat=%s message=%s",
            chat_id, message_id, exc_info=True)
    finally:
        try:
            db.close()
        except Exception:
            pass


def schedule_delete(chat_id: int, message_id: int, delay_seconds: int) -> bool:
    """登记一条消息在 delay_seconds 秒后自毁。

    :return: True 表示已起定时器；延时 <=0（关闭）或标识缺失时返回
        False 且不起定时器。红包消息本体不许经此调度（调用方纪律，
        测试有断言兜底）。
    """
    if not chat_id or not message_id or delay_seconds <= 0:
        return False
    timer = Timer(delay_seconds, _delete_now, args=(chat_id, message_id))
    timer.daemon = True
    timer.start()
    return True

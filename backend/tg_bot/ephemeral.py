"""TG Bot 消息自毁调度（用户 2026-10-30 口径）。

机器人群内消息默认 30 秒自毁、用户发的命令消息默认 3 秒自毁，
红包消息本体是明示例外（永不由本模块调度）。一期（本模块）只提供
延时读取与调度能力；router 的具体接线在二期统一挂。

实现说明：
- threading.Timer 进程内定时；进程重启会丢失未到期的定时器，
  这是可接受降级（自毁只是群面整洁手段，丢一两条不影响业务）。
- 到期触发时新开一个 SessionLocal 会话读 token 并调 deleteMessage，
  失败只记日志、绝不抛给调用方。
- 调度时把当时的 sender.delete_message 快照进定时器闭包，到期调快照
  而非现查 sender 模块属性：迟到的定时器不会串到后来被替换的 callable
  上（测试里 monkeypatch 串扰的根因）。生产 sender 不热替换删除函数，
  快照与旧的到期现查行为等价。
- 延时读 SystemConfig（store.get_value 热读口径）：
  bot_msg_autodelete_seconds 默认 30、bot_cmd_autodelete_seconds 默认 3，
  0 表示关闭（不调度）；配置写坏（非数字/负数）时回落默认值，
  不让脏配置把自毁变成即时删除或永不删除。
- 定时器按 (chat_id, message_id) 登记在册（_timers），支持取消与
  重挂：群面板滑动自毁（PR-2）靠 rearm_delete 在每次成功互动后
  把面板消息的自毁顺延为「最后互动＋bot_msg 延时」。登记条目在
  定时器到期、被取消或被同键新定时器顶替时一律摘除，不留死条目。
  对同一消息重复 schedule_delete 视为顶替（先取消旧的再起新的），
  既有调用点都是一消息只调度一次，行为不变。
"""
from __future__ import annotations

import logging
import threading
from threading import Timer

from backend.integrations import store

logger = logging.getLogger(__name__)

# (chat_id, message_id) → Timer 登记表（进程内，锁保护）
_registry_lock = threading.Lock()
_timers: dict[tuple[int, int], Timer] = {}

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


def _delete_now(chat_id: int, message_id: int, delete_message=None) -> None:
    """定时器到期执行体：新开会话调 deleteMessage，失败只记日志。

    delete_message 为调度时快照的 sender.delete_message；缺省（直接
    调用本函数时）才现查 sender 模块属性，保持旧口径。
    """
    from backend.database import SessionLocal
    from backend.tg_bot import sender

    if delete_message is None:
        delete_message = sender.delete_message
    db = SessionLocal()
    try:
        ok, err = delete_message(db, chat_id, message_id)
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


def _fire_delete(chat_id: int, message_id: int, delete_message=None) -> None:
    """定时器到期执行体：先摘登记再按调度时快照删消息，不留死条目。"""
    with _registry_lock:
        _timers.pop((chat_id, message_id), None)
    _delete_now(chat_id, message_id, delete_message=delete_message)


def schedule_delete(chat_id: int, message_id: int, delay_seconds: int) -> bool:
    """登记一条消息在 delay_seconds 秒后自毁。

    :return: True 表示已起定时器；延时 <=0（关闭）或标识缺失时返回
        False 且不起定时器。红包消息本体不许经此调度（调用方纪律，
        测试有断言兜底）。
    同一 (chat_id, message_id) 已有未到期定时器时，新调度顶替旧的
    （旧的先取消）：同键永远只留一个活定时器。
    """
    if not chat_id or not message_id or delay_seconds <= 0:
        return False
    # 快照调度这一刻的删除函数：到期时只调它，不再现查 sender 属性，
    # 避免迟到定时器落到后来替换的 callable（见模块 docstring）。
    from backend.tg_bot import sender

    timer = Timer(
        delay_seconds, _fire_delete, args=(chat_id, message_id),
        kwargs={"delete_message": sender.delete_message})
    timer.daemon = True
    timer.start()
    key = (chat_id, message_id)
    with _registry_lock:
        old = _timers.pop(key, None)
        _timers[key] = timer
    if old is not None and old is not timer:
        old.cancel()
    return True


def cancel_delete(chat_id: int, message_id: int) -> bool:
    """取消一条消息已登记的自毁定时器。

    :return: True 表示确有定时器被取消；没有登记（已到期/从未调度/
        标识缺失）时返回 False，不抛错。
    """
    if not chat_id or not message_id:
        return False
    with _registry_lock:
        timer = _timers.pop((chat_id, message_id), None)
    if timer is None:
        return False
    timer.cancel()
    return True


def rearm_delete(db, chat_id: int, message_id: int) -> bool:
    """面板滑动自毁：把一条消息的自毁重挂为「现在＋bot_msg 延时」。

    供群面板在每次成功互动后调用（callbacks 面板编辑成功路径）：
    先取消既有定时器，再按当前 bot_msg_autodelete_seconds 重新调度；
    配置为 0（关闭）时只取消不重挂，该消息转为常驻（与发送口
    「延时 0 不调度、0=关则常驻」同语义）。标识缺失静默返回 False；
    消息是否还存在由调用方在互动成功后才调用来保证，本函数不查存活。
    """
    if not chat_id or not message_id:
        return False
    cancel_delete(chat_id, message_id)
    delay = bot_msg_delay_seconds(db)
    if delay <= 0:
        return False
    return schedule_delete(chat_id, message_id, delay)

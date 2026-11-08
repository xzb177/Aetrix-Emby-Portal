"""用户级 Telegram 通知偏好（私聊通报优化批，2026-11-08）。

三类开关，只管 **TG 这一份**，站内信永远不受影响：

- ``seek`` 求片到货类：proactive 管道（求片到货 / 转存终态 / 管理员
  手动标记入库）与求片审批结果的 TG 份；
- ``expiry`` 到期提醒类：订阅到期提醒、公益资格到期提醒、活力
  归零/恢复通知的 TG 份；
- ``lottery`` 活动中奖类：抽奖中奖私聊与补发到账私聊。

存储：``web_users`` 三个布尔列（``tg_notify_seek`` 等），默认 True
（收）。读取失败 / 用户不存在一律按「收」处理——偏好查询出问题时
不能把通知吞掉，通知丢了比多发一条更糟。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger(__name__)

CAT_SEEK = "seek"
CAT_EXPIRY = "expiry"
CAT_LOTTERY = "lottery"

CATEGORIES = (CAT_SEEK, CAT_EXPIRY, CAT_LOTTERY)

_FIELDS = {
    CAT_SEEK: "tg_notify_seek",
    CAT_EXPIRY: "tg_notify_expiry",
    CAT_LOTTERY: "tg_notify_lottery",
}


def field_for(category: str) -> str:
    try:
        return _FIELDS[category]
    except KeyError:
        raise ValueError(f"未知通知类别: {category!r}") from None


def tg_allowed(db: Session, user_id: int | None, category: str) -> bool:
    """该用户是否愿意收这一类的 TG 私聊。查不到/异常 → True（默认收）。"""
    if user_id is None:
        return True
    field = field_for(category)
    try:
        row = (
            db.query(getattr(models.WebUser, field))
            .filter(models.WebUser.id == int(user_id))
            .first()
        )
    except Exception:
        logger.warning("通知偏好读取失败 user=%s cat=%s", user_id, category,
                       exc_info=True)
        return True
    if row is None:
        return True
    return bool(row[0])


def get_prefs(user) -> dict:
    """从 WebUser 行读三类偏好（缺列/空值按 True）。"""
    return {
        cat: bool(getattr(user, field, True))
        for cat, field in _FIELDS.items()
    }

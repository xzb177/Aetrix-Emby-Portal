"""永久账号白名单（2026-11-01 用户亲令）——判定单口。

名单里的账号是**永久账号**：没有过期、不用续费。落到现行机制上，就是
三处过期判定对白名单用户恒有效：

1. **公益资格**：``emby_server/portal.is_welfare_user`` 命中白名单时，
   ``welfare_expires_at`` 已过也视为有效；到期 worker
   （``welfare_expiry``）对白名单用户不取消资格、不活跃禁用。
2. **查看凭证**：``subscriptions.view_grant`` 公益服分支命中白名单时，
   ``EmbyViewUnlock`` 已过期（甚至从未解锁）也视为已授权；购买入口
   ``portal.unlock_emby_view`` 对白名单直接返回已解锁、不扣费。
3. **订阅**：``subscriptions.has_active_subscription`` 命中白名单时，
   订阅已过期（甚至从未订阅）也视为有效——两服（realm）通用。

配套口径（同属「不用续费」的题中之义，一并在此收口）：

- 续费提醒（``reminders.collect_due``）跳过白名单用户，不提示续费；
- 自动续订（``subscriptions.auto_renew_subscriptions``）跳过白名单
  用户，不再为他们扣能量续订；
- 全员赠送的公益口径（``gift_all.eligible_ids_query``）包含白名单
  用户（他们的公益资格恒有效）。

本模块只回答一个问题——「这个用户是不是永久账号」（``is_permanent``），
三处判定仍在各自原判定点完成，**不新造第二套判定**。非白名单用户所有
行为逐字不变。

总开关 ``permanent_whitelist_enabled``（SystemConfig，默认开）：关掉后
名单保留但全站回旧口径（``is_permanent`` 恒 False）。读取走直查
SystemConfig（同 ``subscriptions._config_value`` 口径，无行/空值 = 开，
word 风格只认 ``"true"``）；保存走 config_service 统一通道
（schema 家族 ``permanent_whitelist``）。

**不含活力值**：活力扣减/归零封禁不读本模块——白名单公益用户仍会被
每日扣活力、低于阈值仍被活力门拦播放（见 PR 说明的待定项）。
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger(__name__)

CONFIG_KEY = "permanent_whitelist_enabled"


def whitelist_enabled(db: Session) -> bool:
    """总开关是否开启（缺省开启：无配置行/空值按开处理）。"""
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == CONFIG_KEY)
        .first()
    )
    if row is None or row.value is None or str(row.value).strip() == "":
        return True
    return str(row.value).strip().lower() == "true"


def is_permanent(db: Session, user_id: int | None) -> bool:
    """该用户是不是永久账号（总开关开 + 在名单里）。

    判定失败（表未建等异常）时按「不是」处理并记错误日志：白名单是
    豁免层，它坏掉时必须退回原判定口径，绝不能反过来把所有人拦住、
    也不能把所有人放行。
    """
    if not user_id:
        return False
    try:
        if not whitelist_enabled(db):
            return False
        row = (
            db.query(models.PermanentWhitelistEntry.id)
            .filter(models.PermanentWhitelistEntry.user_id == user_id)
            .first()
        )
        return row is not None
    except Exception as exc:  # noqa: BLE001 — 豁免层故障必须回落原口径
        logger.error("永久白名单判定失败（按非白名单处理）user=%s: %s",
                     user_id, exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def permanent_ids(db: Session, user_ids: Iterable[int]) -> set[int]:
    """批量版：给定的 user_id 里哪些是永久账号（供分页扫描类调用方去 N+1）。

    总开关关时恒为空集；判定失败同样回落空集（原口径）。
    """
    ids = [int(u) for u in user_ids if u]
    if not ids:
        return set()
    try:
        if not whitelist_enabled(db):
            return set()
        rows = (
            db.query(models.PermanentWhitelistEntry.user_id)
            .filter(models.PermanentWhitelistEntry.user_id.in_(ids))
            .all()
        )
        return {r[0] for r in rows}
    except Exception as exc:  # noqa: BLE001 — 同 is_permanent 的回落口径
        logger.error("永久白名单批量判定失败（按空集处理）: %s", exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return set()


def permanent_id_subquery(db: Session):
    """名单 user_id 的子查询（供 gift_all 等 SQL 层口径拼 or_ 条件）。

    调用方须先自行判 ``whitelist_enabled``——子查询本身不看总开关。
    """
    return db.query(models.PermanentWhitelistEntry.user_id)


__all__ = [
    "CONFIG_KEY",
    "whitelist_enabled",
    "is_permanent",
    "permanent_ids",
    "permanent_id_subquery",
]

"""推广奖励（v2.44.0 第一阶段）：邀请成功后给邀请人的那一笔，阈值全走配置

## 与邀请奖励的关系（2026-10-23 去重后：本模块是唯一发放方）

此前邀请成功会发两笔邀请人奖励：``invitation.py`` 按
``invitation_reward_points`` 发一笔、本模块按 ``promotion_reward_amount``
再发一笔——同一个事件、两套配置、两套账，管理员在同一张卡上看到两个
「邀请奖励」字段（运营参数去重 #3，用户拍板合并）。

合并后口径：
- **邀请人奖励只从本模块发**：数值 = 码上专属奖励（``InvitationCode.
  reward_points > 0``，渠道码用）优先，否则按本模块配置（余额积分
  ``promotion_reward_amount`` 或会员天数 ``promotion_reward_days``）。
- ``invitation_reward_points`` 键退役（配置注册表/后台字段/库行一并清）。
- 不是重复的部分不动：被邀请人奖励 ``invitation_invitee_reward_points``
  与消费返利 ``invitation_rebate_percent`` 仍在 ``invitation.py`` 发放
  （对象/事件都不同）；邀请关系与经验值也仍归 invitation 管。
- 本模块的落表（``promotion_rewards``）与明细接口保留，是邀请人奖励
  的唯一明细。

## 默认关闭

``promotion_reward_enabled`` 缺省 ``true``（与生产现值一致），金额/天数出厂都是 0，
所以即使开关开着也不会发出奖励。
**邀请成功不会自动发任何东西**——这是「自动执行一律默认关、由管理员手动开」
那条纪律在本期唯一的落点（本期没有其它自动处置动作）。

## 两种奖励类型，值都不写死

- ``balance``：加积分余额，数值 = ``promotion_reward_amount``；
- ``days``：加会员有效期，天数 = ``promotion_reward_days``（归属服走当前服）。

写库前一律 ``> 0`` 才发：配置成 0 = 只记关系、不发奖，但开关还开着时
不该写出一条「+0」的明细去骗人。

## 事务口径

``grant`` **不提交**：调用方（``apply_invitation``）把「邀请关系 + 奖励」放在
同一个事务里，要么一起成、要么一起不留痕——与注册流程里「码烧了会员没到账」
那个老问题是同一类，见 docs/performance.md。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from backend import models
from backend.integrations import store

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "promotion_reward_enabled"
CONFIG_TYPE = "promotion_reward_type"
CONFIG_AMOUNT = "promotion_reward_amount"
CONFIG_DAYS = "promotion_reward_days"

TYPE_BALANCE = "balance"
TYPE_DAYS = "days"
REWARD_TYPES = (TYPE_BALANCE, TYPE_DAYS)

TYPE_LABELS = {
    TYPE_BALANCE: "余额（积分）",
    TYPE_DAYS: "有效期（天）",
}

#: 出厂默认：开关开 + 0 + 0（金额/天数为 0 时不发任何奖励；
#: 2026-10-21 摘面板对齐：开关缺省由关改为开，与生产现值一致）
DEFAULTS: dict[str, str] = {
    CONFIG_ENABLED: "true",
    CONFIG_TYPE: TYPE_BALANCE,
    CONFIG_AMOUNT: "0",
    CONFIG_DAYS: "0",
}

DESCRIPTIONS = {
    CONFIG_ENABLED: "推广奖励总开关（关闭 = 邀请成功只发原有的双向积分，不发推广奖励）",
    CONFIG_TYPE: "推广奖励类型（balance=余额积分 / days=会员有效期）",
    CONFIG_AMOUNT: "推广奖励·余额数值（reward_type=balance 时生效，0 = 不发）",
    CONFIG_DAYS: "推广奖励·有效期天数（reward_type=days 时生效，0 = 不发）",
}

#: 数值上下限：防一条手写坏的配置把「发 10 分」变成「发 100 万」
AMOUNT_MIN, AMOUNT_MAX = 0, 1_000_000
DAYS_MIN, DAYS_MAX = 0, 3650


def _raw(db: Session, key: str) -> str:
    """统一热读（与 playback_policy / share_guard 同一套）"""
    return (store.get_value(db, key, DEFAULTS.get(key, "")) or "").strip()


def _int(db: Session, key: str, low: int, high: int) -> int:
    try:
        number = int(float(_raw(db, key) or DEFAULTS[key]))
    except (TypeError, ValueError):
        number = int(DEFAULTS[key])
    return max(low, min(high, number))


def reward_type(db: Session) -> str:
    value = _raw(db, CONFIG_TYPE).lower()
    return value if value in REWARD_TYPES else TYPE_BALANCE


def config(db: Session) -> dict:
    return {
        "enabled": _raw(db, CONFIG_ENABLED).lower() in ("1", "true", "yes", "on"),
        "reward_type": reward_type(db),
        "amount": _int(db, CONFIG_AMOUNT, AMOUNT_MIN, AMOUNT_MAX),
        "days": _int(db, CONFIG_DAYS, DAYS_MIN, DAYS_MAX),
        "reward_types": list(REWARD_TYPES),
        "reward_type_labels": dict(TYPE_LABELS),
    }


def policy_payload(db: Session) -> dict:
    """后台读一份（前端不维护默认值）"""
    cfg = config(db)
    # 当前档位**真的会发东西吗**：开关开着但值是 0，等同没开，后台要看得见
    value = cfg["amount"] if cfg["reward_type"] == TYPE_BALANCE else cfg["days"]
    return {**cfg, "active": bool(cfg["enabled"] and value > 0)}


def write_policy(db: Session, values: dict) -> dict:
    """写回配置（只认白名单键；非法值保持原值不动，不存半个坏配置）"""
    applied: dict = {}
    for key, value in (values or {}).items():
        if key not in DEFAULTS:
            continue
        if key == CONFIG_ENABLED:
            text = "true" if str(value).lower() in ("1", "true", "yes", "on") else "false"
        elif key == CONFIG_TYPE:
            text = str(value or "").strip().lower()
            if text not in REWARD_TYPES:
                continue
        elif key == CONFIG_AMOUNT:
            low, high = AMOUNT_MIN, AMOUNT_MAX
            try:
                text = str(max(low, min(high, int(float(str(value).strip() or "0")))))
            except (TypeError, ValueError):
                continue
        else:  # CONFIG_DAYS
            low, high = DAYS_MIN, DAYS_MAX
            try:
                text = str(max(low, min(high, int(float(str(value).strip() or "0")))))
            except (TypeError, ValueError):
                continue
        applied[key] = text

    if applied:
        store.write_values(db, applied, DESCRIPTIONS)
        store.invalidate(*applied.keys())
    return applied


# ==================== 发奖 ====================


def grant(db: Session, inviter: models.WebUser, invitee: models.WebUser,
          code: Optional[models.InvitationCode] = None) -> Optional[models.PromotionReward]:
    """邀请成功时给邀请人发一笔推广奖励；返回写下的那行（没发返回 ``None``）

    **不提交、也不 flush**：行只进会话，由调用方（``apply_invitation``）的
    那一次 ``flush`` 一起落盘。这样一旦 ``promotion_rewards`` 的唯一索引
    拦下重复发奖，连邀请关系一起整体回滚——不会出现「关系建了、奖也发了、
    但明细丢了」或者反过来的半截状态。

    静默跳过的三种情况（每一种都只是「本次不发」，绝不抛错）：
    开关关着、值是 0、邀请人已经拿过这个被邀请人的奖（预查 + 唯一索引双保险）。
    """
    try:
        cfg = config(db)
        if not cfg["enabled"]:
            return None
        # 码上专属奖励（渠道码）优先于全局配置，恒为余额积分（r31 语义，
        # 2026-10-23 去重后改由本模块发放）；不绕过本开关——开关关着
        # 就是邀请人奖励整体关着，不存在第二条发放路径。
        override = int(getattr(code, "reward_points", 0) or 0)
        if override > 0:
            reward_type = TYPE_BALANCE
            value = override
        else:
            reward_type = cfg["reward_type"]
            value = cfg["amount"] if reward_type == TYPE_BALANCE else cfg["days"]
        if value <= 0:
            return None

        # 幂等：一个被邀请人只给邀请人发一笔（与 invitation_records 同一道门）
        existing = db.query(models.PromotionReward).filter(
            models.PromotionReward.invitee_id == invitee.id
        ).first()
        if existing:
            return None

        realm_id = None
        if reward_type == TYPE_DAYS:
            from backend import codes

            # 归属服 = 当前服（realm_id 传 None）；天发到了哪一服要记进明细，
            # 否则事后看一条「+7 天」根本不知道加在哪张卡上
            realm_id = codes.grant_membership_days(db, inviter, value, None).realm_id
        else:
            from backend.api.economy import _add_points

            _add_points(db, inviter, value, "promotion",
                        f"推广奖励：成功邀请 {invitee.username}",
                        f"promotion:{invitee.id}")

        row = models.PromotionReward(
            inviter_id=inviter.id,
            invitee_id=invitee.id,
            code_id=getattr(code, "id", None),
            reward_type=reward_type,
            reward_value=value,
            realm_id=realm_id,
            config_snapshot=json.dumps(
                {"enabled": True, "type": reward_type,
                 "amount": cfg["amount"], "days": cfg["days"],
                 "code_override": override},
                ensure_ascii=False,
            )[:500],
        )
        db.add(row)
        logger.info("推广奖励已发放: inviter=%s invitee=%s type=%s value=%s",
                    inviter.id, invitee.id, cfg["reward_type"], value)
        return row
    except Exception:  # noqa: BLE001 — 奖励失败绝不阻塞注册/邀请关系
        logger.warning("推广奖励发放失败，已跳过: inviter=%s invitee=%s",
                       getattr(inviter, "id", None), getattr(invitee, "id", None),
                       exc_info=True)
        return None


# ==================== 明细 ====================


def rewards_for(db: Session, user_id: int, limit: int = 100) -> list[dict]:
    """用户自己的推广奖励明细（「我的」用）"""
    rows = (
        db.query(models.PromotionReward)
        .filter(models.PromotionReward.inviter_id == user_id)
        .order_by(models.PromotionReward.created_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    users = {}
    ids = {r.invitee_id for r in rows}
    if ids:
        users = {u.id: u.username for u in db.query(models.WebUser).filter(
            models.WebUser.id.in_(ids)).all()}
    return [dto(r, users.get(r.invitee_id, "已注销")) for r in rows]


def dto(row: models.PromotionReward, invitee_name: str = "") -> dict:
    return {
        "id": row.id,
        "invitee_id": row.invitee_id,
        "invitee_username": invitee_name,
        "reward_type": row.reward_type,
        "reward_type_label": TYPE_LABELS.get(row.reward_type, row.reward_type),
        "reward_value": int(row.reward_value or 0),
        "realm_id": row.realm_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


__all__ = [
    "AMOUNT_MAX", "AMOUNT_MIN", "DAYS_MAX", "DAYS_MIN", "DEFAULTS", "DESCRIPTIONS",
    "REWARD_TYPES", "TYPE_BALANCE", "TYPE_DAYS", "TYPE_LABELS",
    "CONFIG_AMOUNT", "CONFIG_DAYS", "CONFIG_ENABLED", "CONFIG_TYPE",
    "config", "dto", "grant", "policy_payload", "rewards_for", "reward_type",
    "write_policy",
]

"""订阅分档权益：套餐权益表读取的唯一入口（横切只许一套）。

设计：``reports/subscription-tier-design-20261024.md``（订阅分档 PR-1）。

套餐（``subscription_plans``）可以带一张权益表（``entitlements`` JSON 列：
``{权益键: 整数值}``），声明「买这个套餐的人，这项额度是多少」。全站任何
地方要问「这个用户的某项权益是多少」，只许调本模块的
``get_user_entitlement``——不许私自读 ``plan.entitlements``，更不许私自判
「用户买的哪个套餐」。

核心语义（回退地基，改动前先读设计 §1.4）：

- helper 只回答「该用户的当前套餐对这项权益**声明了什么**」；没声明
  （无订阅 / 套餐 entitlements 为 NULL / 缺键 / 值非法）一律返回 ``None``，
  **回退动作由各消费方走自己现行的全局配置**——helper 不内置任何业务
  默认值。这样权益表全空时，全站行为与没有本模块时逐字一致。
- 权益键必须先在 ``ENTITLEMENT_SPECS`` 注册（类型/安全区间/回退目标），
  未登记的键：写路径拒绝（``validate_entitlements`` 抛 ValueError）、
  读路径忽略、按未登记键调 helper 抛 KeyError（编程错误要响亮）。
- 「当前有效套餐」不另立标准：直接复用
  ``subscriptions.active_subscription`` 的现行口径（status=active 且
  end_date 现算未过期、按服过滤、end_date 倒序取第一份）。公益 welfare
  不是套餐，本模块**不看 is_welfare**（两条轴正交）；管理员豁免也留在
  各消费方，本模块只管「套餐声明了什么」。
- 坏数据（人工改库写坏的 JSON）整体降级为「无声明」并打 warning，
  绝不抛、绝不把脏值当权益。但 helper 内部的 DB 查询异常**不吞**：
  那是本地索引查询，异常即真故障，应响亮失败让上层 500，而不是
  静默按错档位放行造成权益错乱（设计 §2.4）。

缓存：进程内两级短 TTL（60 秒，与 ``integrations.store`` 的既定口径
对齐）——套餐权益快照（plan_id → 解析结果）与用户→当前套餐解析
（(user_id, realm_id) → (plan_id, end_date)）。解析条目自带 end_date，
命中时先验过期，**到期精度不被缓存拖延**。写路径调 ``invalidate``
同进程立即失效，跨进程靠 TTL 兜底（最迟 60 秒）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend import models
from backend import subscriptions

logger = logging.getLogger(__name__)

#: 权益键注册表（一期两键）。区间是**安全栏**不是业务数值——与既有先例
#: 同性质：share_guard 的 LIMIT_MIN/LIMIT_MAX=1/20、PlanUpsertRequest 的
#: duration_days le=3650。代码里不许出现「某套餐=某数值」的业务数据，
#: 档位与数值只存在于 subscription_plans.entitlements 数据里。
#: ``fallback`` 只写明缺键时消费方现行走哪条全局配置（文档性字段），
#: helper 本人不读它、更不拿它当默认值（设计 §1.4 第 4 条）。
ENTITLEMENT_SPECS: dict[str, dict] = {
    # 每月可提交的求片条数；welfare 用户不经此键（media_seek 的判定
    # 顺序里 welfare 分支在前，设计 §3.2）
    "media_seek_monthly_limit": {
        "min": 1, "max": 1000, "fallback": "paid_request_monthly",
    },
    # 可同时播放的路数；上限与 share_guard.LIMIT_MAX 对齐
    "concurrent_streams": {
        "min": 1, "max": 20, "fallback": "share_concurrent_limit",
    },
}

#: 进程内缓存 TTL（秒），与 integrations.store 的 60 秒口径对齐（设计 §2.3）。
#: 不做成 env 可配：权益是管理员低频改动的数据，没必要扩张配置面。
CACHE_TTL_SEC = 60.0

_CACHE_LOCK = threading.Lock()
# plan_id -> (取回时刻 monotonic, parse_entitlements 结果)
_PLAN_SNAPSHOT_CACHE: dict[int, tuple[float, dict]] = {}
# (user_id, realm_id) -> (取回时刻 monotonic, plan_id 或 None, end_date 或 None)
_USER_PLAN_CACHE: dict[tuple[int, Optional[int]], tuple[float, Optional[int], Optional[datetime]]] = {}


def _is_valid_value(key: str, value) -> bool:
    """值是否合法：真整数（bool 不算整数）、落在注册表安全区间内。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    spec = ENTITLEMENT_SPECS[key]
    return spec["min"] <= value <= spec["max"]


def parse_entitlements(raw) -> dict:
    """把套餐行上的原始 JSON 归一成 ``{已登记键: 合法整数}``（纯函数）。

    读路径专用：未登记键/非法值逐项剔除并 warning；raw 非 dict（含
    None、被人工改坏的 JSON）整体按「无声明」处理。永不抛、永不回写 raw。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("entitlements 不是对象，按无声明处理: %r", raw)
        return {}
    parsed: dict[str, int] = {}
    for key, value in raw.items():
        if key not in ENTITLEMENT_SPECS:
            logger.warning("entitlements 含未登记键 %r，已忽略", key)
            continue
        if not _is_valid_value(key, value):
            spec = ENTITLEMENT_SPECS[key]
            logger.warning(
                "entitlements[%r]=%r 非法（需 %d–%d 的整数），按缺键处理",
                key, value, spec["min"], spec["max"],
            )
            continue
        parsed[key] = value
    return parsed


def validate_entitlements(raw) -> dict:
    """管理端写路径用：与 parse 同样的归一，但非法即抛 ValueError。

    必须说清哪个键错在哪，不许把半个坏配置存进库。raw=None 视为
    「无声明」返回 {}；合法输入返回只含已登记键的归一 dict。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"entitlements 必须是对象（键值对），收到 {type(raw).__name__}"
        )
    validated: dict[str, int] = {}
    for key, value in raw.items():
        if key not in ENTITLEMENT_SPECS:
            raise ValueError(
                f"未知权益键: {key}（已登记: {', '.join(sorted(ENTITLEMENT_SPECS))}）"
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"权益 {key} 的值必须是整数，收到 {value!r}")
        spec = ENTITLEMENT_SPECS[key]
        if not (spec["min"] <= value <= spec["max"]):
            raise ValueError(
                f"权益 {key} 的值 {value} 超出允许区间 {spec['min']}–{spec['max']}"
            )
        validated[key] = value
    return validated


def current_plan(
    db: Session, user_id: int, realm_id: Optional[int] = None
) -> Optional[models.SubscriptionPlan]:
    """用户当前有效套餐（无有效订阅 → None）。

    口径直接复用 ``subscriptions.active_subscription``（status=active 且
    end_date 现算未过期、按服过滤、end_date 倒序取第一份），不另立标准。
    订阅行指向的套餐已被删除时返回 None（按无声明处理）。
    """
    sub = subscriptions.active_subscription(db, user_id, realm_id)
    if sub is None or sub.plan_id is None:
        return None
    return (
        db.query(models.SubscriptionPlan)
        .filter(models.SubscriptionPlan.id == sub.plan_id)
        .first()
    )


def _resolve_user_plan(db: Session, user_id: int, realm_id: Optional[int]):
    """解析用户当前套餐：返回 (plan_id, end_date)，带进程内缓存。

    命中缓存时先比 end_date：缓存里那份订阅已到期即按无套餐——**到期
    精度不被缓存拖延**（不会出现订阅已过期、缓存还按旧套餐放行一分钟）。
    未命中时查 ``active_subscription``（走 idx_sub_user 索引）。
    """
    cache_key = (user_id, realm_id)
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        entry = _USER_PLAN_CACHE.get(cache_key)
    if entry is not None and now_mono - entry[0] < CACHE_TTL_SEC:
        _, plan_id, end_date = entry
        if plan_id is None:
            return None, None
        if end_date is not None and end_date > datetime.now():
            return plan_id, end_date
        return None, None  # 缓存里那份订阅已经到期
    sub = subscriptions.active_subscription(db, user_id, realm_id)
    plan_id = sub.plan_id if sub is not None else None
    end_date = sub.end_date if sub is not None else None
    with _CACHE_LOCK:
        _USER_PLAN_CACHE[cache_key] = (now_mono, plan_id, end_date)
    return plan_id, end_date


def _plan_entitlements(db: Session, plan_id: int) -> dict:
    """套餐权益快照（parse_entitlements 归一后），带进程内缓存。

    套餐不存在（已被删）→ {}（无声明）。命中缓存后读权益是纯内存查。
    """
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        entry = _PLAN_SNAPSHOT_CACHE.get(plan_id)
    if entry is not None and now_mono - entry[0] < CACHE_TTL_SEC:
        return entry[1]
    plan = (
        db.query(models.SubscriptionPlan)
        .filter(models.SubscriptionPlan.id == plan_id)
        .first()
    )
    parsed = parse_entitlements(plan.entitlements) if plan is not None else {}
    with _CACHE_LOCK:
        _PLAN_SNAPSHOT_CACHE[plan_id] = (now_mono, parsed)
    return parsed


def get_user_entitlement(
    db: Session, user, key: str, *, realm_id: Optional[int] = None
) -> Optional[int]:
    """单口：该用户当前套餐对 ``key`` 的声明值。

    ``user`` 可传 WebUser 对象或 user_id（int）。返回 None = 没声明
    （无订阅 / 套餐未声明该键 / 值非法），消费方据此回退自己现行的
    全局配置——helper 不内置任何业务默认值。``key`` 未在注册表登记
    → KeyError（编程错误要响亮，不许静默当缺键）。不看 welfare、
    不特判 staff：helper 只回答「套餐声明了什么」。
    """
    if key not in ENTITLEMENT_SPECS:
        raise KeyError(f"未登记的权益键: {key}")
    user_id = user if isinstance(user, int) else user.id
    plan_id, _end_date = _resolve_user_plan(db, user_id, realm_id)
    if plan_id is None:
        return None
    return _plan_entitlements(db, plan_id).get(key)


def invalidate(user_id: Optional[int] = None) -> None:
    """写路径失效缓存（同进程立即生效，跨进程靠 60 秒 TTL 兜底）。

    - 传 user_id：清该用户的「当前套餐解析」缓存——订阅发放/续订/顺延/
      到期变更/退款回滚改了他的套餐归属或到期时间后调（发放口见
      ``economy._grant_subscription`` 与 ``codes.grant_membership_days``）。
    - 不传：清全部套餐权益快照——管理端套餐增/改/删后调。用户解析缓存
      里存的是 plan_id，权益值走快照，所以改套餐不必逐个清用户。
    """
    with _CACHE_LOCK:
        if user_id is None:
            _PLAN_SNAPSHOT_CACHE.clear()
        else:
            for cache_key in [k for k in _USER_PLAN_CACHE if k[0] == user_id]:
                del _USER_PLAN_CACHE[cache_key]

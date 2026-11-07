"""公共贡献池判定层（goal-33 PR-1 地基，standalone、不接线）。

本单只落地基：配置读取、池资格判定（``pool_eligible``）、新判定
入口（``ensure_playback_allowed_for_item``）、池候选资格服务函数
（``grant_pool_candidate``）与绑定上限新口径
（``binding_limit_for_user``）。

硬闸（GOAL 通审决议 1）：本模块的新入口**不接线到任何端点**，
既有 ``playback_gate.ensure_binding_playback_allowed*`` 的行为
逐字不变——播出链路（PR-3）齐备前，线上行为必须零变化。池开
关在配置里打开时，现网未绑用户仍走既有门禁原 403（回归测试
test_pool_foundation.test_redline_existing_gate_unchanged_when_
pool_enabled 钉住）。

判定顺序（design §4.1，新入口内部）：
① CMS 免门禁 → not_applicable；② 已下发 play_sign 消费不追溯；
③ 已绑定 → bound；④ 池 eligible → pool；⑤ 都不满足 → 抛原 403
（文案选择与 playback_gate 逐字一致，含下线 provider 专用文案）。
本入口不查软件授权：授权前置是既有 ensure_* 的职责且接线在
PR-3 一并处理，本单不碰 #1883 的授权逻辑。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import playback_gate as _gate
from backend.integrations import store

logger = logging.getLogger(__name__)

_GIB = 1024 ** 3

#: 配置家族 pool 的代码默认值（与 config_schema.POOL_CONFIG_SCHEMA
#: 同口径；字节默认值按 25TB 补充节修正值，不是 design §3 旧默认）。
POOL_CONFIG_DEFAULTS: dict = {
    "pool_enabled": False,
    "pool_audience": "all",
    "pool_join_mode": "apply_review",
    "pool_enabled_providers": "drive",
    "pool_member_capacity_bytes": 4_500_000_000_000,
    "pool_member_reserve_free_bytes": 300 * _GIB,
    "pool_member_daily_transfer_bytes": 600 * _GIB,
    "pool_member_daily_transfer_jobs": 0,
    "pool_item_max_copies": 2,
    "pool_copy_idle_evict_days": 30,
    "pool_global_concurrency": 2,
    "pool_enqueue_jitter_sec": 120,
    "pool_breaker_quota_hits_per_day": 3,
    "pool_breaker_cooldown_sec": 3600,
    "pool_pick_top_k": 3,
    "pool_member_max_bindings_per_user": 10,
}

#: 视为「有池候选/成员资格」的成员状态集合：这些状态下绑定上限
#: 走 pool_member_max_bindings_per_user。终态（exited / rejected /
#: ejected）不在集合内——退出后回落个人上限（任务书口径写死）。
POOL_LIMIT_MEMBER_STATUSES = ("candidate", "pending", "active", "suspended")


def _raw_config(db: Session) -> dict[str, str]:
    defaults = {
        key: ("true" if value is True else "false" if value is False
              else str(value))
        for key, value in POOL_CONFIG_DEFAULTS.items()
    }
    try:
        return store.get_values(db, list(POOL_CONFIG_DEFAULTS), defaults)
    except Exception:  # noqa: BLE001 配置层异常不许把判定打成 500
        logger.warning("公共池配置读取失败，回落代码默认", exc_info=True)
        return defaults


def pool_config_payload(db: Session) -> dict:
    """池配置生效值（类型化）：存量行优先，缺行/非法回代码默认。

    管理端 GET /api/admin/pool/config 与判定层共用这一份 payload，
    不许两处各解析一遍。
    """
    raw = _raw_config(db)
    out: dict = {}
    for key, default in POOL_CONFIG_DEFAULTS.items():
        value = raw.get(key, "")
        if isinstance(default, bool):
            out[key] = str(value).strip().lower() == "true"
        elif isinstance(default, int):
            try:
                out[key] = int(str(value).strip())
            except (TypeError, ValueError):
                out[key] = default
        else:
            out[key] = str(value) if value not in (None, "") else default
    return out


def pool_enabled(db: Session) -> bool:
    return bool(pool_config_payload(db)["pool_enabled"])


def _audience_eligible(db: Session, user) -> bool:
    """人群判定（design §3 pool_audience）：只走既有原语，不另立名单。

    - all：全部登录用户（付费墙在门禁之前，本判定不越付费墙）；
    - whitelist：codes.is_whitelist_account 唯一口径；
    - subscribers：subscriptions.has_active_subscription 口径。
    判定异常一律按人群外处理（fail-closed 回 off，不许把人放进池）。
    """
    audience = pool_config_payload(db)["pool_audience"]
    if audience == "all":
        return True
    if audience == "whitelist":
        from backend import codes

        return bool(codes.is_whitelist_account(db, user))
    if audience == "subscribers":
        from backend import subscriptions

        return bool(subscriptions.has_active_subscription(db, user.id))
    return False


def pool_eligible(db: Session, user, item) -> str:
    """池资格判定（纯函数）：返回 "off" 或 "eligible"。

    off 的三种情形（design §4.1）：池总开关关、条目免门禁（CMS
    库——池不介入）、人群外（含未登录/取不到用户 id）。异常同样
    回 off（fail-closed）。
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return "off"
    try:
        if not pool_enabled(db):
            return "off"
        if item is not None and not _gate.binding_required_for_item(db, item):
            return "off"
        if not _audience_eligible(db, user):
            return "off"
    except Exception:  # noqa: BLE001 判定异常不许放行
        logger.warning("公共池资格判定异常，按 off 处理", exc_info=True)
        return "off"
    return "eligible"


@dataclass
class PoolDecision:
    """新判定入口的裁决结果（PR-3 接线时消费）。

    kind：not_applicable（CMS 免门禁）/ issued_sign（消费已下发
    链接）/ bound（已绑定）/ pool（池放行）。blocked 不返回
    decision——直接抛 403（与既有门禁同口径）。
    """

    allowed: bool
    kind: str


def ensure_playback_allowed_for_item(
    db: Session, user, item, request=None
) -> PoolDecision:
    """新判定入口（standalone，PR-1 不接线任何端点）。

    顺序见模块头。都不满足时抛 HTTPException 403，文案选择与
    playback_gate.ensure_binding_playback_allowed 逐字一致。
    """
    if not _gate.binding_required_for_item(db, item):
        return PoolDecision(allowed=True, kind="not_applicable")
    if request is not None and _gate._request_uses_issued_play_sign(
            request, db):
        return PoolDecision(allowed=True, kind="issued_sign")
    user_id = getattr(user, "id", None)
    if user_id is not None and _gate.has_active_binding(db, user_id):
        return PoolDecision(allowed=True, kind="bound")
    if pool_eligible(db, user, item) == "eligible":
        return PoolDecision(allowed=True, kind="pool")
    if user_id is not None and _gate.has_removed_provider_binding(
            db, user_id):
        raise HTTPException(
            status_code=403,
            detail=_gate.GATE_MESSAGE_REMOVED_PROVIDER)
    raise HTTPException(status_code=403, detail=_gate.GATE_MESSAGE)


# ---------------------------------------------------------------- 成员资格


def grant_pool_candidate(db: Session, user_id: int) -> em.PoolMember:
    """给站内账号建池候选资格行（管理员预批，通审决议 2）。

    幂等：已有状态 ∈ POOL_LIMIT_MEMBER_STATUSES 的候选/成员行时
    直接返回该行，不重复建。本单只落服务函数，管理端入口 PR-4
    再接。候选行 binding_id=NULL、provider=NULL（还没绑盘）。
    """
    if user_id is None:
        raise ValueError("grant_pool_candidate 需要 user_id")
    existing = (
        db.query(em.PoolMember)
        .filter(
            em.PoolMember.user_id == user_id,
            em.PoolMember.status.in_(POOL_LIMIT_MEMBER_STATUSES),
        )
        .order_by(em.PoolMember.id.asc())
        .first()
    )
    if existing is not None:
        return existing
    now = datetime.now()
    row = em.PoolMember(
        binding_id=None, user_id=user_id, provider=None,
        status="candidate", status_changed_at=now)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def binding_limit_for_user(db: Session, user) -> int:
    """绑定上限新口径（drive_oauth 建绑校验唯一判定）。

    用户有 status ∈ POOL_LIMIT_MEMBER_STATUSES 的 pool_members
    行 → 上限取配置 pool_member_max_bindings_per_user（默认 10，
    改配置即生效）；否则个人上限不变（user_bindings
    .binding_limit_per_user：env BYOS_BINDING_LIMIT_PER_USER，
    默认 3）。``user`` 可传 WebUser 或用户 id。
    """
    user_id = getattr(user, "id", user)
    if user_id is not None:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            hit = (
                db.query(em.PoolMember.id)
                .filter(
                    em.PoolMember.user_id == uid,
                    em.PoolMember.status.in_(POOL_LIMIT_MEMBER_STATUSES),
                )
                .first()
            )
            if hit is not None:
                return int(pool_config_payload(db)[
                    "pool_member_max_bindings_per_user"])
    from backend.emby_server import user_bindings as _ub

    return _ub.binding_limit_per_user()

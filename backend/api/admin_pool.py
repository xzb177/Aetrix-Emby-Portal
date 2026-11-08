"""
管理后台 · 公共贡献池配置（goal-33 PR-1 地基）

池本体在 backend/emby_server/pool_gate.py；本文件只做配置运营面：
读生效值（缺行回代码默认）、保存（走 config_service 家族 pool 的
统一校验通道，任一非法整批拒绝）。鉴权口径与 admin_player_blacklist
一致（get_current_admin + _audit；只读角色能不能写由
get_current_admin 里的 admin_roles 集中判定，端点不自造鉴权）。

成员管理（审核/暂停/清退等）在 PR-4 的池管理页，本文件只管配置。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import config_service, models
from backend.api.admin_core import _audit, get_current_admin
from backend.config_schema import ConfigValueError
from backend.database import get_db
from backend.emby_server import pool_gate

logger = logging.getLogger(__name__)

admin_pool_router = APIRouter(
    prefix="/api/admin/pool", tags=["管理后台·公共贡献池"])


class PoolConfigRequest(BaseModel):
    # 全部可选：只改传了的键（model_fields_set 区分未传/传值）。
    # 类型与范围校验在 config_service 家族 pool 里把关，这里只透传。
    pool_enabled: Optional[bool] = None
    pool_audience: Optional[str] = None
    pool_join_mode: Optional[str] = None
    pool_enabled_providers: Optional[str] = None
    pool_member_capacity_bytes: Optional[int] = None
    pool_member_reserve_free_bytes: Optional[int] = None
    pool_member_daily_transfer_bytes: Optional[int] = None
    pool_member_daily_transfer_jobs: Optional[int] = None
    pool_item_max_copies: Optional[int] = None
    pool_copy_idle_evict_days: Optional[int] = None
    pool_global_concurrency: Optional[int] = None
    pool_enqueue_jitter_sec: Optional[int] = None
    pool_breaker_quota_hits_per_day: Optional[int] = None
    pool_breaker_cooldown_sec: Optional[int] = None
    pool_pick_top_k: Optional[int] = None
    pool_member_max_bindings_per_user: Optional[int] = None


class PoolRewardConfigRequest(BaseModel):
    # 激励子家族 pool_reward_*（PR-6）：与本体 16 键分开一份端点，
    # 校验走 config_service 家族 pool_reward（与本体同一体系）。
    pool_reward_enabled: Optional[bool] = None
    pool_reward_payout_enabled: Optional[bool] = None
    pool_reward_period: Optional[str] = None
    pool_reward_points_per_play: Optional[int] = None
    pool_reward_points_per_active_day: Optional[int] = None
    pool_reward_points_cap: Optional[int] = None
    pool_reward_min_active_days: Optional[int] = None
    pool_reward_min_counted_plays: Optional[int] = None
    pool_reward_play_dedupe_window_sec: Optional[int] = None
    pool_reward_daily_play_freeze: Optional[int] = None
    pool_reward_welfare_days: Optional[int] = None
    pool_reward_welfare_min_plays: Optional[int] = None
    pool_reward_xp_per_play: Optional[int] = None
    pool_reward_xp_cap: Optional[int] = None
    pool_reward_subscription_enabled: Optional[bool] = None
    pool_reward_title_enabled: Optional[bool] = None


@admin_pool_router.get("/config")
def pool_get_config(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """池配置生效值：16 键（存量行优先，缺行回代码默认）。"""
    return pool_gate.pool_config_payload(db)


@admin_pool_router.get("/reward-config")
def pool_get_reward_config(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """激励配置生效值：pool_reward_* 16 键（存量行优先，缺行回
    代码默认；总开关与真发开关默认关）。"""
    return pool_gate.reward_config_payload(db)


@admin_pool_router.put("/reward-config")
def pool_update_reward_config(
    request: PoolRewardConfigRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存激励配置：走 config_service 家族 pool_reward 校验
    通道（与本体同一体系），非法整批 400、不落库；写时失效，
    结算侧即时生效。"""
    values: dict[str, Any] = {
        key: getattr(request, key) for key in request.model_fields_set
    }
    saved: list[str] = []
    if values:
        try:
            saved = config_service.save_config(
                db, values, schema="pool_reward",
                descriptions={
                    key: "公共贡献池激励配置（goal-33 PR-6）"
                    for key in values
                })
        except ConfigValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, current_admin, "pool_update_reward_config", "system",
           None, {"keys": saved},
           ip=raw_request.client.host if raw_request.client else None)
    db.commit()
    return pool_gate.reward_config_payload(db)


@admin_pool_router.put("/config")
def pool_update_config(
    request: PoolConfigRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存池配置：非法值整批 400、不落库；写时失效，判定侧即时生效。"""
    values: dict[str, Any] = {
        key: getattr(request, key) for key in request.model_fields_set
    }
    saved: list[str] = []
    if values:
        try:
            saved = config_service.save_config(
                db, values, schema="pool",
                descriptions={
                    key: "公共贡献池配置（goal-33）" for key in values
                })
        except ConfigValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
    _audit(db, current_admin, "pool_update_config", "system", None,
           {"keys": saved},
           ip=raw_request.client.host if raw_request.client else None)
    db.commit()
    return pool_gate.pool_config_payload(db)

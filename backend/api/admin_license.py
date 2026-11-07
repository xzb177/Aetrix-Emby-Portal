"""
管理后台 · 软件授权

校验与执法本体在 backend/license_client.py；本文件只做运营面：
看授权状态（档位 / 到期 / 绑定域名 / 上次校验 / 是否宽限中 / 到期
提醒）、填授权码与绑定域名、点「立即校验」。鉴权口径与
admin_player_blacklist 一致（get_current_admin + _audit；角色写
权限由 get_current_admin 依赖集中判定）。

口径：
- 保存即校验：填入有效码保存后立刻回连一次（force——管理员显式
  动作，不受 enforce 开关限制），校验通过播放当场恢复，不用等
  24 小时复验或重启；
- enforce 总开关不进后台（走 env / 发行包，遵「功能开关从简」）；
  这里只展示它当前是否生效，让管理员心里有数。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import license_client as lc
from backend import models
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db
from backend.integrations import store

logger = logging.getLogger(__name__)

admin_license_router = APIRouter(
    prefix="/api/admin/license", tags=["管理后台·软件授权"])


class LicenseConfigRequest(BaseModel):
    # None = 该字段不改；传字符串（含空串）= 改成该值（空串 = 清除）
    code: Optional[str] = None
    domain: Optional[str] = None


@admin_license_router.get("/status")
def license_get_status(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """授权状态全量回显：状态、档位、到期、绑定、宽限与到期提醒。"""
    return lc.admin_status(db)


@admin_license_router.put("/config")
def license_update_config(
    request: LicenseConfigRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存授权码 / 绑定域名并立即校验一次，返回最新状态。"""
    values: dict[str, str] = {}
    if request.code is not None:
        values[lc.KEY_CODE] = request.code.strip().upper()
    if request.domain is not None:
        values[lc.KEY_DOMAIN] = lc.normalize_domain(request.domain)
    if values:
        store.write_values(db, values, descriptions={
            lc.KEY_CODE: "软件授权码（管理端填，env AETRIX_LICENSE_CODE 兜底）",
            lc.KEY_DOMAIN: "软件授权绑定域名（管理端填，env AETRIX_LICENSE_DOMAIN 兜底）",
        })
        db.commit()
        lc.invalidate_state_cache()
    _audit(db, current_admin, "license_update_config", "system", None,
           {"keys": sorted(values)},
           ip=raw_request.client.host if raw_request.client else None)
    db.commit()
    # 保存即校验：显式动作走 force，enforce 关时也真连一次，
    # 让管理员当场知道码对不对；连不上不报错，状态里如实显示。
    try:
        lc.refresh(db, force=True)
    except Exception as exc:  # noqa: BLE001 — 校验失败不许把保存接口打挂
        logger.warning("保存授权配置后立即校验异常（可忽略）: %s", exc)
    return lc.admin_status(db)


@admin_license_router.post("/verify")
def license_verify_now(
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """立即校验：回连授权服务一次（无令牌先激活），返回最新状态。"""
    try:
        lc.refresh(db, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("立即校验异常（可忽略）: %s", exc)
    _audit(db, current_admin, "license_verify_now", "system", None, {},
           ip=raw_request.client.host if raw_request.client else None)
    db.commit()
    return lc.admin_status(db)

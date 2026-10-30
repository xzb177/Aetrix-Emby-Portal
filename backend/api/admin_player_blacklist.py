"""
管理后台 · 播放器登录黑名单

规则本体在 SystemConfig player_login_blacklist，匹配/执法在
backend/emby_server/player_blacklist.py；本文件只做运营面：
读当前生效规则、保存（走 config_service 统一校验通道，非法结构 400）。

为什么单独成文件：同 coupons_admin.py 的理由——backend/api/admin.py
已经很大，新功能独立成文件、鉴权口径一致（get_current_admin + _audit）。

保存口径：
- rules 传规则数组 = 覆盖名单（空数组 = 清空名单、功能停用）；
- rules 传 null 或不传 = 删行，恢复代码级默认名单（见 player_blacklist
  的 DEFAULT_RULES）；

管理端设置页的编辑卡片本批未做：Settings.vue / api/economy.ts 正在被
在飞 PR #1496 修改，避撞，UI 另行补（见本功能 PR 正文）。
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
from backend.emby_server import player_blacklist as pb

logger = logging.getLogger(__name__)

admin_player_blacklist_router = APIRouter(
    prefix="/api/admin/player-blacklist", tags=["管理后台·播放器登录黑名单"])


def _payload(db: Session) -> dict:
    """当前生效口径：库中有行用库中值，无行用代码默认名单。"""
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == pb.CONFIG_KEY)
        .first()
    )
    return {
        "rules": pb.load_rules(db),
        "using_default": row is None,
        "block_message": pb.BLOCK_MESSAGE,
    }


class PlayerBlacklistConfigRequest(BaseModel):
    # None = 删行恢复默认；list = 覆盖（含空数组=停用）。结构在 save_config
    # 的 json_rules 校验里把关，这里不预设形状。
    rules: Optional[Any] = None


@admin_player_blacklist_router.get("/config")
def player_blacklist_get_config(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """读取当前生效的黑名单规则（含是否在用代码默认名单）。"""
    return _payload(db)


@admin_player_blacklist_router.put("/config")
def player_blacklist_update_config(
    request: PlayerBlacklistConfigRequest,
    raw_request: Request,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存黑名单规则：非法结构 400；写时失效，登录侧即时生效。"""
    try:
        saved = config_service.save_config(
            db, {pb.CONFIG_KEY: request.rules}, schema="player_blacklist",
            descriptions={pb.CONFIG_KEY: "播放器登录黑名单规则（JSON 数组）"},
        )
    except ConfigValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    payload = _payload(db)
    _audit(db, current_admin, "player_blacklist_update_config", "system", None,
           {"keys": saved, "rule_count": len(payload["rules"]),
            "using_default": payload["using_default"]},
           ip=raw_request.client.host if raw_request.client else None)
    db.commit()
    return payload

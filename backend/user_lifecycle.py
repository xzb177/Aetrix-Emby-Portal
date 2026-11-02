# -*- coding: utf-8 -*-
"""用户启用/停用生命周期（横切只许一套）。

网站管理端（backend/api/admin.py 的 update_user）与 TG 机器人
/whois 管理员面板共用 ``set_user_active`` 这一个实现，不许各写一套：

- 停用（True→False）：同一事务内批量吊销该用户全部 Emby 客户端
  令牌（EmbyApiToken.is_revoked=True）并 token_version+1，使此前
  签发的全部会话立即失效（PA-002 链，完美审计 R1·B1 修复口径；
  写法源自 share_guard._disable_user 并补 token_version）。
- 启用（False→True）：只翻 is_active，不自动恢复任何旧会话
  （旧令牌保持吊销、token_version 不回退）。
- 状态未变化时不动任何数据，返回 False。

调用方负责前置守卫（操作者权限、不能停用自己、保留超级管理员等
各入口自己的规则）与最终 commit；本函数只做状态切换本身。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger(__name__)


def set_user_active(db: Session, user: models.WebUser, active: bool) -> bool:
    """切换用户启用状态；返回是否真的发生了变化（未 commit）。"""
    active = bool(active)
    if bool(user.is_active) == active:
        return False
    if not active:
        from backend.emby_server import models as emby_models

        db.query(emby_models.EmbyApiToken).filter(
            emby_models.EmbyApiToken.user_id == user.id
        ).update(
            {emby_models.EmbyApiToken.is_revoked: True},
            synchronize_session=False,
        )
        user.token_version = int(user.token_version or 0) + 1
    user.is_active = active
    return True

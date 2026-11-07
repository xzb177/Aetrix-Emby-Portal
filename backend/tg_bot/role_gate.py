# -*- coding: utf-8 -*-
"""TG Bot 管理角色门：对齐网站三级角色（backend/admin_roles.py）。

网站端早已分级：super 全部、operator 日常可写、viewer 只读
（can_write 为假）。机器人此前所有管理入口只查 is_staff、不查
admin_role，viewer 只要绑了 TG 就能在机器人里封号/赠送/处置，
等于绕过网站只读限制。本模块是机器人侧唯一的角色门：所有管理
写操作在执行层调 readonly_refusal 先过一遍（不靠藏按钮，手输
命令与旧回调都拦得住）；读口不经此门，对 viewer 原样放行。
"""
from __future__ import annotations

from backend import admin_roles

#: 只读角色尝试写操作时的统一拒绝文案（说人话，与网站端口径一致）。
READONLY_TEXT = (
    "当前账号是「只读审计」角色，只能查看，不能修改；"
    "需要写权限请让超级管理员调整角色"
)


def can_write(user) -> bool:
    """该管理员是否可写：复用网站端 admin_roles.can_write 单一口径。"""
    return user is not None and admin_roles.can_write(user)


def readonly_refusal(user) -> str | None:
    """写操作执行层门：只读角色返回拒绝文案，其余返回 None 放行。"""
    if user is None or admin_roles.can_write(user):
        return None
    return READONLY_TEXT

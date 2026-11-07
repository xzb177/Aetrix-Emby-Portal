# -*- coding: utf-8 -*-
"""TG Bot 机器人自身群权限自检（管理面板「🛡️ 群权限自检」）。

频道马甲治理删消息、消息自毁、抽奖公告置顶都要求机器人是名单群
的管理员且有对应权限；此前缺权时多为静默失败，管理员无从知道。
本模块对 policy 名单群（bot_group_ids）逐群查机器人自身成员状态
（sender.get_me 取机器人 id → sender.get_chat_member_raw 取原始
成员信息，含 administrator 的 can_delete_messages /
can_pin_messages），渲染成卡：缺权明说影响，查询失败如实写查询
失败，绝不猜成有权限。
"""
from __future__ import annotations

import logging

from backend.tg_bot import layout, policy, sender

logger = logging.getLogger(__name__)

#: 不是管理员或缺删消息权时的影响说明（与现行依赖点逐项对应）。
IMPACT_TEXT = (
    "频道马甲消息删不掉、消息自毁删不掉、抽奖公告置顶会失败，"
    "请在群设置里把机器人设为管理员并给删消息权限"
)

_PIN_IMPACT_TEXT = "抽奖公告置顶会失败，请在群设置里给机器人置顶消息权限"


def check_one(db, group_id: int, bot_id: int) -> dict:
    """查机器人在单个群里的身份与关键权限（原始成员信息口径）。"""
    ok, _err, result = sender.get_chat_member_raw(db, group_id, bot_id)
    if not ok or not isinstance(result, dict):
        return {"group_id": group_id, "status": "error",
                "can_delete": None, "can_pin": None}
    status = str(result.get("status") or "")
    if status == "creator":
        return {"group_id": group_id, "status": "creator",
                "can_delete": True, "can_pin": True}
    if status == "administrator":
        return {"group_id": group_id, "status": "administrator",
                "can_delete": bool(result.get("can_delete_messages")),
                "can_pin": bool(result.get("can_pin_messages"))}
    return {"group_id": group_id, "status": status or "unknown",
            "can_delete": False, "can_pin": False}


def _mark(flag) -> str:
    return "✅" if flag else "❌"


def render_card(db) -> str:
    """自检卡正文：逐群列身份/删消息/置顶，缺权附影响说明。"""
    title = layout.title("机器人群权限自检")
    cfg = policy.read_bot_config(db)
    groups = sorted(policy.parse_group_ids(cfg.get("bot_group_ids", "")))
    if not groups:
        return layout.card(title, [
            layout.row("⚠️", "名单群", "未配置（bot_group_ids 为空），无群可自检"),
        ])
    ok, _err, me = sender.get_me(db)
    bot_id = (me or {}).get("id") if ok and isinstance(me, dict) else None
    if not bot_id:
        return layout.card(title, [
            layout.row("❓", f"群 {gid}", "查询失败，权限未知")
            for gid in groups
        ], notes=["机器人自身信息获取失败，本次查不到任何群的权限。"])
    rows = []
    notes = []
    lacking_delete = False
    lacking_pin = False
    for gid in groups:
        r = check_one(db, gid, int(bot_id))
        status = r["status"]
        if status == "error":
            rows.append(layout.row("❓", f"群 {gid}", "查询失败，权限未知"))
            continue
        if status == "creator":
            label = "群主（全权）"
        elif status == "administrator":
            label = "群管理员"
        elif status in ("left", "kicked"):
            label = "不在群里"
        else:
            label = "普通成员"
        rows.append(layout.row(
            "👥", f"群 {gid}",
            f"身份：{label}｜删消息 {_mark(r['can_delete'])}"
            f"｜置顶 {_mark(r['can_pin'])}"))
        if not r["can_delete"]:
            lacking_delete = True
        elif not r["can_pin"]:
            lacking_pin = True
    if lacking_delete:
        notes.append(f"⚠️ 影响：{IMPACT_TEXT}")
    elif lacking_pin:
        notes.append(f"⚠️ 影响：{_PIN_IMPACT_TEXT}")
    return layout.card(title, rows, notes)

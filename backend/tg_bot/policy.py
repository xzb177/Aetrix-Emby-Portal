# -*- coding: utf-8 -*-
"""TG Bot B4 策略中心：总开关、群白名单、命令开关、限流阈值的集中读取与判定。

所有阈值/开关/群名单都走 SystemConfig，可在管理后台修改；本模块只做
"一次批量读取 + 纯函数判定"，dispatch 每条 update 只查一次 DB。
"""
from __future__ import annotations

from backend import models
from backend.integrations.store import switch_env_value as _switch_env_value

# 配置键 → 默认值（2026-10-21 摘面板后 bot_* 开关的缺省以本表为准；
# 面板注册表 WELFARE_CONFIG_KEYS 已不再含这些开关）
DEFAULTS: dict[str, str] = {
    # 总开关：关闭后 Bot 不响应任何消息（poller 继续跑）
    "bot_enabled": "true",
    # 群白名单：逗号分隔的群 ID；空=所有群都启用
    "bot_group_ids": "",
    # 限流：每用户每命令最小间隔秒数
    "bot_rate_limit_seconds": "3",
    # 限流：每群每分钟最多处理消息数
    "bot_group_rate_limit": "20",
    # 各命令开关（/start /help 常开，不在表里）
    "bot_cmd_checkin": "true",
    "bot_cmd_points": "true",
    "bot_cmd_chatpoints": "true",
    "bot_cmd_redeem": "true",
    "bot_cmd_bind": "true",
    "bot_cmd_account": "true",
    "bot_cmd_request": "true",
    "bot_cmd_myrequests": "true",
    "bot_cmd_rank": "true",
    "bot_cmd_requestboard": "true",
    "bot_cmd_rules": "true",
    "bot_cmd_login": "true",
    # P1 自助查询开关（默认开，关闭后该命令回复"该功能已关闭"）
    "bot_cmd_bindings": "true",
    "bot_cmd_transfers": "true",
    "bot_cmd_whyplay": "true",
    "bot_cmd_wants": "true",
    # 管理员群号对账开关（借鉴第 3 项，仅私聊 + is_staff 可用）
    "bot_cmd_reconcile": "true",
    # 红包功能开关（/redpacket 发红包 + 抢红包按钮）
    "bot_redpacket_enabled": "true",
    # 内联搜片开关（@bot 片名，InlineQuery；默认开）
    "bot_inline_search_enabled": "true",
    # 频道马甲治理（2026-10-30 借鉴 embyboss 第 1 项）：群里以外部频道
    # 身份发言的消息默认删除＋回说明；匿名管理员（sender_chat=本群）
    # 不在此列。白名单存频道 chat id（逗号分隔），命中的频道放行。
    "bot_channel_guard_enabled": "true",
    "bot_channel_whitelist": "",
    # 退群处置：cancel_welfare=已绑定用户退群/被踢时只取消公益资格
    # （不动账号/能量/查看凭证）；none=不处置。非法值回落默认。
    "bot_leave_action": "cancel_welfare",
}

# 命令 → 开关配置键（不在表中的命令视为常开）
COMMAND_SWITCHES: dict[str, str] = {
    "/checkin": "bot_cmd_checkin",
    "/points": "bot_cmd_points",
    "/chatpoints": "bot_cmd_chatpoints",
    "/redeem": "bot_cmd_redeem",
    "/bind": "bot_cmd_bind",
    "/account": "bot_cmd_account",
    "/me": "bot_cmd_account",
    "/request": "bot_cmd_request",
    "/myrequests": "bot_cmd_myrequests",
    "/rank": "bot_cmd_rank",
    "/requestboard": "bot_cmd_requestboard",
    "/rules": "bot_cmd_rules",
    "/login": "bot_cmd_login",
    "/renew": "bot_cmd_login",
    "/bindings": "bot_cmd_bindings",
    "/transfers": "bot_cmd_transfers",
    "/whyplay": "bot_cmd_whyplay",
    "/wants": "bot_cmd_wants",
    "/reconcile": "bot_cmd_reconcile",
}

_DEFAULT_RATE_LIMIT_SECONDS = 3.0
_DEFAULT_GROUP_RATE_LIMIT = 20


def read_bot_config(db) -> dict[str, str]:
    """一次 IN 查询批量读取全部 bot_* 配置，缺失的键填默认值。"""
    keys = list(DEFAULTS.keys())
    rows = (
        db.query(models.SystemConfig.key, models.SystemConfig.value)
        .filter(models.SystemConfig.key.in_(keys))
        .all()
    )
    result = dict(DEFAULTS)
    for k, v in rows:
        # value 为 NULL 时回退默认值，避免判定函数拿到 None
        result[k] = v if v is not None else DEFAULTS[k]
    # A 类摘面板开关（2026-10-21）：env 显式设置即接管，口径见 integrations.store
    for k in result:
        _env_v = _switch_env_value(k)
        if _env_v is not None:
            result[k] = _env_v
    return result


def _is_true(value: str | None) -> bool:
    """开关值判定：strip/lower 后 == "true"，容忍 "True"/" TRUE " 等写法。"""
    return str(value or "").strip().lower() == "true"


def is_enabled(cfg: dict[str, str]) -> bool:
    """Bot 总开关是否开启。"""
    return _is_true(cfg.get("bot_enabled", DEFAULTS["bot_enabled"]))


def parse_group_ids(value: str | None) -> set[int]:
    """解析逗号分隔的群 ID；空→空集合；非法片段跳过。"""
    result: set[int] = set()
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def is_group_allowed(cfg: dict[str, str], chat_id: int | None) -> bool:
    """群白名单判定：白名单为空→所有群都允许；否则 chat_id 必须在名单内。"""
    if chat_id is None:
        return False
    allowed = parse_group_ids(cfg.get("bot_group_ids", ""))
    if not allowed:
        return True
    return chat_id in allowed


def is_command_enabled(cfg: dict[str, str], cmd: str) -> bool:
    """单命令开关判定；不在开关表中的命令（/start /help 等）视为常开。"""
    key = COMMAND_SWITCHES.get(cmd)
    if key is None:
        return True
    return _is_true(cfg.get(key, "true"))


def is_inline_search_enabled(cfg: dict[str, str]) -> bool:
    """内联搜片开关是否开启（@bot 片名）。"""
    return _is_true(cfg.get("bot_inline_search_enabled",
                            DEFAULTS["bot_inline_search_enabled"]))


def is_redpacket_enabled(cfg: dict[str, str]) -> bool:
    """红包功能开关是否开启（发红包/抢红包）。"""
    return _is_true(cfg.get("bot_redpacket_enabled", DEFAULTS["bot_redpacket_enabled"]))


def _to_float(value: str | None, default: float) -> float:
    """字符串转 float；非法或非正数时返回默认值。"""
    try:
        num = float(str(value or "").strip())
    except (ValueError, TypeError):
        return default
    return num if num > 0 else default


def _to_int(value: str | None, default: int) -> int:
    """字符串转 int；非法或非正数时返回默认值。"""
    try:
        num = int(str(value or "").strip())
    except (ValueError, TypeError):
        return default
    return num if num > 0 else default


def rate_limit_seconds(cfg: dict[str, str]) -> float:
    """每用户每命令最小间隔秒数（可配置，默认 3.0）。"""
    return _to_float(cfg.get("bot_rate_limit_seconds"), _DEFAULT_RATE_LIMIT_SECONDS)


def group_rate_limit(cfg: dict[str, str]) -> int:
    """每群每分钟最多处理消息数（可配置，默认 20）。"""
    return _to_int(cfg.get("bot_group_rate_limit"), _DEFAULT_GROUP_RATE_LIMIT)


# 退群处置的合法取值
LEAVE_ACTIONS = ("cancel_welfare", "none")


def is_channel_guard_enabled(cfg: dict[str, str]) -> bool:
    """频道马甲治理总开关是否开启（默认开）。"""
    return _is_true(cfg.get("bot_channel_guard_enabled",
                            DEFAULTS["bot_channel_guard_enabled"]))


def channel_whitelist(cfg: dict[str, str]) -> set[int]:
    """频道白名单：逗号分隔的频道 chat id 集合（与群 id 同解析口径）。"""
    return parse_group_ids(cfg.get("bot_channel_whitelist", ""))


def is_channel_whitelisted(cfg: dict[str, str], sender_chat_id) -> bool:
    """某频道是否在白名单内放行以频道身份发言。"""
    if sender_chat_id is None:
        return False
    try:
        return int(sender_chat_id) in channel_whitelist(cfg)
    except (TypeError, ValueError):
        return False


def leave_action(cfg: dict[str, str]) -> str:
    """退群处置动作：cancel_welfare / none；非法值回落默认 cancel_welfare。"""
    value = str(cfg.get("bot_leave_action") or "").strip().lower()
    return value if value in LEAVE_ACTIONS else DEFAULTS["bot_leave_action"]


def mask_sender_chat(msg: dict, chat_id: int | None) -> dict | None:
    """频道马甲判别：命中马甲返回 sender_chat，否则返回 None。

    - 无 sender_chat：普通用户消息，不是马甲；
    - sender_chat.id == chat_id：匿名管理员形态（群本身在发言），
      属 #1516 的匿名说明口径，绝不按马甲处理；
    - sender_chat.type 不是 channel（以群/私聊身份发言）：不按马甲处理；
    - is_automatic_forward：关联频道帖子自动转发进讨论群，是 Telegram
      的系统行为（群主自己的频道），删了会打断正常讨论，不处理。
    """
    sender_chat = msg.get("sender_chat") or {}
    sc_id = sender_chat.get("id")
    if sc_id is None:
        return None
    if chat_id is not None and sc_id == chat_id:
        return None
    if sender_chat.get("type") != "channel":
        return None
    if msg.get("is_automatic_forward"):
        return None
    return sender_chat

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

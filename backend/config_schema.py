"""SystemConfig 数值/开关类配置的类型校验（公益配置 / 经济系统设置共用）。

管理端保存配置原先 str(value) 直接落库，任何类型都收：手续费比例写 -5、
上限写 "abc" 都能存进去，读取侧要么悄悄回退默认值、要么按负数参与计算。
这里给已知数值/开关键声明类型与范围，非法值由调用方转成 400。

约定：空字符串 / None = 未设置（调用方删除该行，读取侧回退默认值）。
"""
from __future__ import annotations

from typing import Any, Optional

_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")
_BIG = 10**9


class ConfigValueError(ValueError):
    pass


def normalize(key: str, spec: tuple, value: Any, bool_style: str = "word") -> Optional[str]:
    """按 spec 校验并返回要落库的字符串；None 表示「未设置」。

    spec: ("int", lo, hi) | ("float", lo, hi) | ("bool",) | ("int_list", lo, hi)
    bool_style: "word" → true/false，"digit" → 1/0（仅在传入 Python bool 时决定写法；
    字符串原样保留大小写外的写法，避免改变读取侧既有判断口径）。
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    kind = spec[0]
    if kind == "bool":
        if isinstance(value, bool):
            if bool_style == "digit":
                return "1" if value else "0"
            return "true" if value else "false"
        if isinstance(value, int) and value in (0, 1):
            return str(value)
        s = str(value).strip().lower()
        if s in _BOOL_TRUE or s in _BOOL_FALSE:
            return s
        raise ConfigValueError(f"{key} 必须是开关值（true/false 或 1/0）")
    lo, hi = spec[1], spec[2]
    if kind == "int":
        if isinstance(value, bool):
            raise ConfigValueError(f"{key} 必须是整数")
        if isinstance(value, float):
            if not value.is_integer():
                raise ConfigValueError(f"{key} 必须是整数")
            value = int(value)
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            raise ConfigValueError(f"{key} 必须是整数")
        if not lo <= n <= hi:
            raise ConfigValueError(f"{key} 需在 {lo}~{hi} 之间")
        return str(n)
    if kind == "float":
        if isinstance(value, bool):
            raise ConfigValueError(f"{key} 必须是数字")
        try:
            f = float(str(value).strip())
        except (TypeError, ValueError):
            raise ConfigValueError(f"{key} 必须是数字")
        if f != f or not lo <= f <= hi:  # NaN 也拒绝
            raise ConfigValueError(f"{key} 需在 {lo}~{hi} 之间")
        return str(value).strip()
    if kind == "int_list":
        parts = [p.strip() for p in str(value).replace("，", ",").split(",") if p.strip()]
        if not parts:
            return None
        out = []
        for p in parts:
            try:
                n = int(p)
            except ValueError:
                raise ConfigValueError(f"{key} 必须是逗号分隔的整数")
            if not lo <= n <= hi:
                raise ConfigValueError(f"{key} 每项需在 {lo}~{hi} 之间")
            out.append(str(n))
        return ",".join(out)
    raise ConfigValueError(f"{key} 类型未知")


# 公益配置（welfare_admin.WELFARE_CONFIG_KEYS）里的数值/开关键；未列出的（群 id 列表等）按字符串保存
WELFARE_CONFIG_SCHEMA: dict[str, tuple] = {
    "points_chat_daily_cap": ("int", 0, _BIG),
    "points_redeem_7d": ("int", 0, _BIG),
    "points_redeem_30d": ("int", 0, _BIG),
    "welfare_grace_days": ("int", 0, 3650),
    "welfare_inactive_days": ("int", 0, 3650),
    "welfare_request_monthly": ("int", 0, 10000),
    "recharge_ratio": ("float", 0.0001, 1000000),
    "recharge_quick_amounts": ("int_list", 1, 1000000),
    "bot_enabled": ("bool",),
    "bot_rate_limit_seconds": ("int", 0, 3600),
    "bot_group_rate_limit": ("int", 0, 100000),
    "bot_cmd_checkin": ("bool",),
    "bot_cmd_points": ("bool",),
    "bot_cmd_redeem": ("bool",),
    "bot_cmd_bind": ("bool",),
    "bot_redpacket_enabled": ("bool",),
    "redpacket_fee_pct": ("int", 0, 100),
    "redpacket_send_limit_7d": ("int", 0, 100000),
    "redpacket_recv_limit_7d": ("int", 0, 100000),
    "redpacket_refund_enabled": ("bool",),
    "redpacket_refund_interval_sec": ("int", 60, 86400),
    "welfare_require_tg_bind": ("bool",),
    "lottery_enabled": ("bool",),
    "lottery_auto_draw_enabled": ("bool",),
    "lottery_draw_interval_sec": ("int", 30, 86400),
    "lottery_notify_winners": ("bool",),
    "tg_bind_guide_enabled": ("bool",),
    "media_seek_enabled": ("bool",),
    "media_seek_vote_enabled": ("bool",),
    "media_seek_notify_voters": ("bool",),
    "chat_points_enabled": ("bool",),
    "points_transfer_enabled": ("bool",),
    "points_transfer_fee_pct": ("int", 0, 100),
    "points_transfer_min": ("int", 1, _BIG),
    "points_transfer_max": ("int", 0, _BIG),
    "points_transfer_daily_cap": ("int", 0, _BIG),
    "chat_points_per_message": ("int", 0, 100000),
    "chat_points_min_len": ("int", 0, 10000),
    "chat_points_minute_window": ("int", 0, 86400),
    "chat_points_daily_cap": ("int", 0, _BIG),
    "chat_points_points_per_day": ("int", 0, _BIG),
}

# 经济系统设置（admin_economy.ECONOMY_CONFIG_KEYS）里 int 键的范围；未列出的 int 键默认 0~10^9
ECONOMY_INT_RANGES: dict[str, tuple[int, int]] = {
    "invitation_rebate_percent": (0, 100),
    "checkin_penalty_pct": (0, 100),
    # Q3 同剧合并：与 merge_versions_worker.series_merge_config 的钳制一致
    "series_merge_interval_sec": (3600, 604800),
    "series_merge_batch_limit": (10, 100000),
}


def economy_spec(value_type: str, key: str) -> Optional[tuple]:
    if value_type == "int":
        lo, hi = ECONOMY_INT_RANGES.get(key, (0, _BIG))
        return ("int", lo, hi)
    if value_type == "bool":
        return ("bool",)
    return None

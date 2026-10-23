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

    spec: ("int", lo, hi) | ("float", lo, hi) | ("bool",) | ("bool", "digit"|"word")
        | ("int_list", lo, hi) | ("str",) | ("enum", (allowed...)) | ("raw",)
    bool_style: "word" → true/false，"digit" → 1/0（仅在传入 Python bool 时决定写法；
    字符串原样保留大小写外的写法，避免改变读取侧既有判断口径）。
    spec 里显式带风格位（("bool", "digit")）时，spec 优先于 bool_style 参数——
    同一个家族里不同键可能用不同存储风格（如告警总开关存 1/0），风格必须跟键走。
    ("str",): 自由字符串，不做类型校验（空串/None → None = 未设置）；
    ("enum", allowed): 白名单枚举，命中才收。
    ("raw",): 原样存库的字符串：str(value) 直接落库，**空串/None 也照存**——
    与 ("str",) 的"空串=未设置（删行）"不同。专为 economy 家族的 str/secret
    键保留：历史行为就是 str(value) 原样存（含空串存行），迁移时一字不变。
    """
    kind = spec[0]
    if kind == "raw":
        # 注意：必须在下面的"空串/None → None"早退之前处理——raw 的空串是要存库的。
        return str(value)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if kind == "bool":
        _style = spec[1] if len(spec) > 1 else bool_style
        if isinstance(value, bool):
            if _style == "digit":
                return "1" if value else "0"
            return "true" if value else "false"
        if isinstance(value, int) and value in (0, 1):
            return str(value)
        s = str(value).strip().lower()
        if s in _BOOL_TRUE or s in _BOOL_FALSE:
            return s
        raise ConfigValueError(f"{key} 必须是开关值（true/false 或 1/0）")
    if kind == "str":
        return str(value)
    if kind == "enum":
        allowed = spec[1]
        s = str(value).strip()
        if s in allowed:
            return s
        raise ConfigValueError(f"{key} 必须是 {allowed} 之一")
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
    "bot_rate_limit_seconds": ("int", 0, 3600),
    "bot_group_rate_limit": ("int", 0, 100000),
    "redpacket_fee_pct": ("int", 0, 100),
    "redpacket_send_limit_7d": ("int", 0, 100000),
    "redpacket_recv_limit_7d": ("int", 0, 100000),
    "redpacket_refund_interval_sec": ("int", 60, 86400),
    # 2026-10-17 第 4 批 4-4：以下 9 键在 WELFARE_CONFIG_KEYS 里的默认值是 "1"/"0"，
    # 端点现行的 normalize 按 ("bool", "digit") 存 "1"/"0"。风格必须跟键走，
    # 注册为 digit 才能与今天逐字一致（读侧 lottery.py 等是宽容口径，本应无感，
    # 但存字节必须相同）。
    "welfare_require_tg_bind": ("bool", "digit"),
    "lottery_draw_interval_sec": ("int", 30, 86400),
    "chat_points_enabled": ("bool",),  # 默认关的功能开关保留面板（2026-10-21）
    "points_transfer_fee_pct": ("int", 0, 100),
    "points_transfer_min": ("int", 1, _BIG),
    "points_transfer_max": ("int", 0, _BIG),
    "points_transfer_daily_cap": ("int", 0, _BIG),
    "chat_points_per_message": ("int", 0, 100000),
    "chat_points_min_len": ("int", 0, 10000),
    "chat_points_minute_window": ("int", 0, 86400),
    "chat_points_daily_cap": ("int", 0, _BIG),
    "chat_points_points_per_day": ("int", 0, _BIG),
    # 2026-10-17 第 4 批 4-2：补上 WELFARE_CONFIG_KEYS 里漏登记的 3 个
    # （读侧 lottery.py 有明确口径：bool 默认开、int max(0,·) 钳制）
    "lottery_require_group_member": ("bool", "digit"),
    "lottery_min_account_age_days": ("int", 0, _BIG),
    "lottery_max_joins_per_day": ("int", 0, _BIG),
    # 2026-10-17 第 4 批 4-4：白名单里但此前无 spec 的群 id 列表键（端点现行按原文
    # 存字符串）。注册为 ("str",) 与今天逐字一致：原文直存，None/空串→删行
    # （读侧 _get_str_config 缺行/空行都回默认 ""，等价）。
    "bot_group_ids": ("str",),
    "chat_points_group_ids": ("str",),
    "lottery_group_ids": ("str",),
}

# 告警设置（admin_alerts.update_alert_settings 的 4 个写键）。
# 注意：alert_check_interval_sec 的钳制（60~86400）保留在端点层（业务行为不变），
# 注册表范围与钳制边界对齐；save_config 只是纵深防御。
ALERTS_CONFIG_SCHEMA: dict[str, tuple] = {
    "system_alerts_enabled": ("bool", "digit"),  # 默认关的功能开关保留面板（2026-10-21）
    "alert_check_interval_sec": ("int", 60, 86400),
    "webhook_alert_url": ("str",),
    "webhook_alert_secret": ("str",),
}

# 到期提醒（reminders_admin.REMINDER_CONFIG_KEYS）。
# 2026-10-17 第 4 批 4-4 裁决：expiry_reminder_days 今天的端点**原文存储、从不拒绝**
# （非法档位靠读侧 reminders.py 逐个跳过、空则回落默认 7/3/1）。严格的
# ("int_list", 1, 365) 会把今天能存的值变成 400，是行为变化；宽容归一又会改动
# 存值。零行为变化要求原文直存，故注册为 ("str",)。
REMINDERS_CONFIG_SCHEMA: dict[str, tuple] = {
    "expiry_reminder_days": ("str",),
}

# 优惠券（coupons_admin.update_coupon_settings；reserve_hours 端点层已有
# Pydantic Field(ge=0, le=720)，注册表范围与之一致；0=不自动清理）
COUPONS_CONFIG_SCHEMA: dict[str, tuple] = {
    "coupon_reserve_hours": ("int", 0, 720),
}

# 注册模式（admin.set_registration_mode；mode 端点层只认 open/closed；
# ratelimit_max≥1、ratelimit_window≥60 是端点现有校验，上限沿用 _BIG 口径）
REGISTRATION_CONFIG_SCHEMA: dict[str, tuple] = {
    "registration_mode": ("enum", ("open", "closed")),
    "registration_closed_message": ("str",),
    "register_ratelimit_max": ("int", 1, _BIG),
    "register_ratelimit_window": ("int", 60, _BIG),
}

# 查看解锁（portal.admin_view_unlock_put；points/days≥0，days=0=永久）
VIEW_UNLOCK_CONFIG_SCHEMA: dict[str, tuple] = {
    "emby_view_unlock_points": ("int", 0, _BIG),
    "emby_view_unlock_days": ("int", 0, _BIG),
}

# 防盗链（r36：play_sign.write_config / write_allowed_referers 迁移到
# config_service 统一编排层。用户 2026-10-17 18:29 拍板：防盗链保留，
# 此前搁置的 play_sign config_service 迁移恢复排期。）
# - play_sign_ttl_seconds 范围 60~86400 与 play_sign.PLAY_TTL_MIN/MAX 同口径；
# - hotlink_protection_enabled 已于 2026-10-21 摘面板（env 治理），不在本表；
# - play_allowed_referers 用 ("raw",)：逗号拼接的归一化域名串，空串也存行
#   （旧 write_allowed_referers 同口径：空白名单落库 ""，读侧空=不检查）。
HOTLINK_CONFIG_SCHEMA: dict[str, tuple] = {
    "play_sign_ttl_seconds": ("int", 60, 86400),
    "play_allowed_referers": ("raw",),
}

# 列表去重缓存（2026-10-18）：Emby 列表接口 dropped 集合的服务端缓存。
# enabled 默认开（纯性能优化、零行为变化）；ttl=0 或关 = 旧路径。
ITEMS_CACHE_CONFIG_SCHEMA: dict[str, tuple] = {
    "items_dedup_cache_ttl_seconds": ("int", 10, 3600),
    "items_dedup_cache_max_entries": ("int", 100, 20000),
}

# （原 ECONOMY_INT_RANGES / economy_spec 已并入下面的 ECONOMY_CONFIG_SCHEMA，
# 4-6 迁移后不再有任何调用方，此处删除。）


# 经济系统设置（admin_economy.ECONOMY_CONFIG_KEYS，共 49 键，2026-10-17 实查）。
# spec 口径与迁移前的 economy_spec() 历史行为逐字一致（4-6 已将其并入此表删除）：
# - int：原 ECONOMY_INT_RANGES 里的 4 个用其范围，其余 (0, _BIG)；
# - bool：("bool",)（word 风格；save_config 默认口径与端点旧调用的默认 bool_style="word" 一致）；
# - str/secret：("raw",) 原样存库——历史行为是 str(value) 直接存（含空串存行，
#   而不是删行）；复合校验（stream_accel 开关+域名联动、签到上下限、活力阈值）
#   保留在端点层，注册表只表达"类型+范围"。
ECONOMY_CONFIG_SCHEMA: dict[str, tuple] = {
    "checkin_enabled": ("bool",),
    "checkin_streak_bonus": ("int", 0, _BIG), "checkin_streak_max_bonus": ("int", 0, _BIG),
    "checkin_base_min": ("int", 0, _BIG), "checkin_base_max": ("int", 0, _BIG),
    "checkin_penalty_pct": ("int", 0, 100),
    "checkin_penalty_min": ("int", 0, _BIG), "checkin_penalty_max": ("int", 0, _BIG),
    "media_seek_daily_limit": ("int", 0, _BIG),
    # r40：卡码授予天数所用默认套餐（codes._resolve_plan 读取；无行=按服自动推导）
    "code_default_plan_id": ("int", 0, _BIG),
    "recharge_enabled": ("bool",), "subscription_purchase_enabled": ("bool",),
    "payment_gateway_url": ("raw",), "payment_partner_id": ("raw",),
    "payment_partner_key": ("raw",), "payment_qqpay_enabled": ("bool",),
    "site_url": ("raw",),
    "subscription_required": ("bool",), "subscription_gate_message": ("raw",),
    "allow_download": ("bool",),
    "device_limit_per_user": ("int", 0, _BIG), "device_limit_auto_evict": ("bool",),
    "login_log_retention_days": ("int", 0, _BIG),
    "max_pending_orders_per_user": ("int", 0, _BIG),
    "invitation_invitee_reward_points": ("int", 0, _BIG),
    "invitation_rebate_percent": ("int", 0, 100),
    "promotion_reward_enabled": ("bool",),
    "promotion_reward_type": ("raw",),
    "promotion_reward_amount": ("int", 0, _BIG),
    "promotion_reward_days": ("int", 0, _BIG),
    "stream_accel_enabled": ("bool",),
    "stream_accel_domain": ("raw",),
    "vitality_daily_cost": ("int", 0, _BIG),
    "vitality_max": ("int", 0, _BIG),
    "vitality_limit_threshold": ("int", 0, _BIG),
    "vitality_point_cost": ("int", 0, _BIG),
    "vitality_watch_reward": ("int", 0, _BIG),
    "vitality_daily_gain_limit": ("int", 0, _BIG),
    "vitality_archive_clamp": ("int", 0, _BIG),
    "series_merge_interval_sec": ("int", 3600, 604800),
    "series_merge_batch_limit": ("int", 10, 100000),
}

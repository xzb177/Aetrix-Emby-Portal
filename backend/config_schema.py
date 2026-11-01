"""SystemConfig 数值/开关类配置的类型校验（公益配置 / 经济系统设置共用）。

管理端保存配置原先 str(value) 直接落库，任何类型都收：手续费比例写 -5、
上限写 "abc" 都能存进去，读取侧要么悄悄回退默认值、要么按负数参与计算。
这里给已知数值/开关键声明类型与范围，非法值由调用方转成 400。

约定：空字符串 / None = 未设置（调用方删除该行，读取侧回退默认值）。
"""
from __future__ import annotations

import json

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")
_BIG = 10**9


class ConfigValueError(ValueError):
    pass


def normalize(key: str, spec: tuple, value: Any, bool_style: str = "word") -> Optional[str]:
    """按 spec 校验并返回要落库的字符串；None 表示「未设置」。

    spec: ("int", lo, hi) | ("float", lo, hi) | ("float", lo, hi, places) | ("bool",) | ("bool", "digit"|"word")
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
    if kind == "json_rules":
        # 结构化 JSON 规则（如播放器黑名单）：入参可为 JSON 文本或已解析的
        # list，归一化为 canonical JSON 文本落库；结构非法直接拒绝。
        return normalize_player_blacklist_rules(value)
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
        if len(spec) > 3:
            # 可选第 4 位 places：小数位数上限（发言每条奖励 0.01 级精度用）。
            # 命中时按 Decimal 校验位数并归一化落库（"0.10"→"0.1"、"2.00"→"2"），
            # 读侧按字符串解析 Decimal，不经 float 中转、无二进制浮点误差。
            places = spec[3]
            try:
                d = Decimal(str(value).strip())
            except (InvalidOperation, ValueError):
                raise ConfigValueError(f"{key} 必须是数字")
            if -d.as_tuple().exponent > places:
                raise ConfigValueError(f"{key} 最多 {places} 位小数")
            s = format(d, "f")
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s or "0"
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
    # 发言每条奖励可为小数（2026-10-31 用户亲令默认 0.1）：0~100、最多 2 位小数
    "points_chat_per_msg": ("float", 0, 100, 2),
    "points_chat_daily_cap": ("int", 0, _BIG),
    "points_redeem_7d": ("int", 0, _BIG),
    "points_redeem_30d": ("int", 0, _BIG),
    # G1 资格码月配额：0 在读侧是「不限」，故下限 0 合法
    "welfare_code_monthly_quota": ("int", 0, _BIG),
    # 兑换资格门槛：来源限定开关 word 风格 ("bool",)（读侧 _get_bool_config
    # 只认 "true"）；号龄上限 365 天；活跃天数窗口固定近 7 天，故钳 0~7
    "welfare_redeem_self_earned_only": ("bool",),
    "welfare_redeem_min_account_age_days": ("int", 0, 365),
    "welfare_redeem_min_active_days": ("int", 0, 7),
    "welfare_grace_days": ("int", 0, 3650),
    "welfare_streak_renew_min": ("int", 0, 3650),
    # 连签保资格（streak_renew）总开关＋续期天数：此前无开关、续期天数在
    # welfare_expiry 里写死 30。开关 word 风格 ("bool",) 存 "true"/"false"
    # （同 register_gift_* 口径；读侧 economy._get_bool_config 只认 "true"）。
    "welfare_streak_renew_enabled": ("bool",),
    "welfare_streak_renew_days": ("int", 1, 365),
    "welfare_inactive_days": ("int", 0, 3650),
    "welfare_request_monthly": ("int", 0, 10000),
    # 新注册冷启动赠送：福利天数 0=永不过期（grant_welfare 口径）；查看天数最小 1
    #（0 天按 timedelta 算是“立即过期”，不是永久，管理端直接禁掉）
    "register_gift_welfare_days": ("int", 0, _BIG),
    "register_gift_view_days": ("int", 1, _BIG),
    # 新注册送公益资格总开关（2026-10-29 用户拍板默认关）：word 风格
    # ("bool",) 存 "true"/"false"（同 ECONOMY 家族口径）。关≠天数 0——
    # 0 在 grant_welfare 口径是"永不过期"，必须独立布尔键，不能复用天数键。
    "register_gift_welfare_enabled": ("bool",),
    # 新注册送查看凭证总开关（2026-10-29 用户拍板默认关）：与资格开关对称的
    # 独立布尔键，同为 word 风格 ("bool",)。关≠天数 0——查看天数 schema
    # 下限是 1，0 天只会“立即过期”，不是关闭，不能复用天数键。
    "register_gift_view_enabled": ("bool",),
    "recharge_ratio": ("float", 0.0001, 1000000),
    "recharge_quick_amounts": ("int_list", 1, 1000000),
    "bot_rate_limit_seconds": ("int", 0, 3600),
    "bot_group_rate_limit": ("int", 0, 100000),
    # 频道马甲治理开关：word 风格 ("bool",) 存 "true"/"false"
    # （读侧 tg_bot.policy._is_true 只认 "true"，同 welfare_streak_renew 口径）；
    # 白名单是逗号分隔的频道 id 列表，原文直存（同 bot_group_ids）；
    # 退群动作是封闭枚举，非法值在端点层 400，不许落库后靠读侧猜。
    "bot_channel_guard_enabled": ("bool",),
    "bot_channel_whitelist": ("str",),
    "bot_leave_action": ("enum", ("cancel_welfare", "none")),
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
    # chat_points_* 已于 2026-10-23 去重退役（机器人那套群发言积分，
    # 与 points_chat_* 重复），此处不再登记
    "points_transfer_fee_pct": ("int", 0, 100),
    "points_transfer_min": ("int", 1, _BIG),
    "points_transfer_max": ("int", 0, _BIG),
    "points_transfer_daily_cap": ("int", 0, _BIG),
    # 2026-10-17 第 4 批 4-2：补上 WELFARE_CONFIG_KEYS 里漏登记的 3 个
    # （读侧 lottery.py 有明确口径：bool 默认开、int max(0,·) 钳制）
    "lottery_require_group_member": ("bool", "digit"),
    "lottery_min_account_age_days": ("int", 0, _BIG),
    "lottery_max_joins_per_day": ("int", 0, _BIG),
    # 公告根治（2026-10-31）：置顶开关 digit 风格（读侧 lottery.py 宽容口径，
    # 默认开）；提醒台阶/提前分钟/编辑节流为非负整数，0=关（读侧同口径钳制）。
    "lottery_announce_pin_enabled": ("bool", "digit"),
    "lottery_reminder_step": ("int", 0, _BIG),
    "lottery_final_call_minutes": ("int", 0, _BIG),
    "lottery_announce_edit_min_interval_sec": ("int", 0, 3600),
    # 能量老虎机（backend/slots.py 读取，2026-10-30 用户亲批默认值）：
    # 总开关 word 风格 ("bool",)（同 register_gift_* 口径，读侧
    # economy._get_bool_config 只认 "true"）；注额档位逗号列表走 int_list
    # 校验；赔付倍数/种子/封顶为非负整数；抽水百分比 0-100。
    "slots_enabled": ("bool",),
    "slots_group_enabled": ("bool",),
    "slots_bet_tiers": ("int_list", 1, 100000),
    "slots_pay_777": ("int", 0, _BIG),
    "slots_pay_bar": ("int", 0, _BIG),
    "slots_pay_fruit": ("int", 0, _BIG),
    "slots_pay_two7": ("int", 0, _BIG),
    "slots_pool_rake_pct": ("int", 0, 100),
    "slots_pool_seed": ("int", 0, _BIG),
    "slots_daily_bet_cap": ("int", 0, _BIG),
    "slots_daily_win_cap": ("int", 0, _BIG),
    # 2026-10-17 第 4 批 4-4：白名单里但此前无 spec 的群 id 列表键（端点现行按原文
    # 存字符串）。注册为 ("str",) 与今天逐字一致：原文直存，None/空串→删行
    # （读侧 _get_str_config 缺行/空行都回默认 ""，等价）。
    "bot_group_ids": ("str",),
    # 观影榜海报日推（tg_bot.rankpush 读侧宽容解析）：开关 word 风格存 true/false
    "bot_rankpush_enabled": ("bool",),
    "bot_rankpush_reward_enabled": ("bool",),
    "bot_rankpush_reward_table": ("int_list", 0, _BIG),

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
# ratelimit_max≥1、ratelimit_window≥60 是端点现有校验，上限沿用 _BIG 口径；
# A-112：register_ratelimit_enabled 恢复登记（bool word 风格存 true/false，
# 与 _get_register_ratelimit 的 =="true" 判定一致；env 显式设置时仍接管））
REGISTRATION_CONFIG_SCHEMA: dict[str, tuple] = {
    "registration_mode": ("enum", ("open", "closed")),
    "registration_closed_message": ("str",),
    "register_ratelimit_enabled": ("bool",),
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

# 挂载直读出流（2026-10-30 立项）：总开关默认开、白名单与根映射默认空
# （空＝行为与现行完全一致）；ids/roots 的内容校验在
# mount_serving.write_config（schema 层只管类型），开关 word 风格
# ("bool",) 存 "true"/"false"（同 welfare 口径）。
MOUNT_SERVING_CONFIG_SCHEMA: dict[str, tuple] = {
    "mount_serving_enabled": ("bool",),
    "mount_serving_mount_ids": ("raw",),
    "mount_serving_local_roots": ("raw",),
}

# 播放交付编排器（换代线 step 5，2026-11-01）：(用户, 条目)
# 交付计划预计算开关 + 计划 TTL。读取唯一实现 delivery_plan
# .delivery_plan_enabled / plan_ttl_sec（热读、默认开/TTL 默认
# 120 秒，读取侧钳制与本 schema 同口径）；关 = 不建计划、热
# 路径不读计划，选路与现行逐字一致。纯 SystemConfig 键，不
# 进 env.example（#1527 先例）。
DELIVERY_PLAN_CONFIG_SCHEMA: dict[str, tuple] = {
    "delivery_plan_enabled": ("bool",),
    "delivery_plan_ttl_sec": ("int", 30, 3600),
}

# 列表去重缓存（2026-10-18）：Emby 列表接口 dropped 集合的服务端缓存。
# enabled 默认开（纯性能优化、零行为变化）；ttl=0 或关 = 旧路径。
ITEMS_CACHE_CONFIG_SCHEMA: dict[str, tuple] = {
    "items_dedup_cache_ttl_seconds": ("int", 10, 3600),
    "items_dedup_cache_max_entries": ("int", 100, 20000),
}

# 转存调度（2026-10-30）：跨 provider 总开关 + 跨单在途上限 +
# 跨单 aging 提权阈值 + 执行道并发 + 单用户在飞上限 + 115 秒传/
# 分片上传开关 + 换绑豁免限频两键。读取唯一实现 transfer_variant
# 的 cross_provider_enabled
# / cross_lane_concurrency / cross_aging_sec / lane_concurrency /
# per_owner_inflight /
# pan115_rapid_upload_enabled / pan115_multipart_upload_enabled /
# rebind_waiver_window_days / rebind_waiver_max_uses，
# 范围与读取侧钳制同口径。探针道五键（2026-11-01）读取实现
# 同在 transfer_variant（probe_enabled / probe_priority_enabled
# / probe_lane_concurrency / probe_recheck_sec /
# play_inline_probe_budget_sec）。
TRANSFER_CONFIG_SCHEMA: dict[str, tuple] = {
    "cross_provider_transfer_enabled": ("bool",),
    "transfer_cross_lane_concurrency": ("int", 1, 8),
    # 探针道（2026-11-01 转存等待压缩 期一＋期二）：探针道总
    # 开关 + 期一优先排序开关默认开（读取默认兜底在
    # transfer_variant.probe_enabled / probe_priority_enabled，
    # 无行=True）；探针单轮认领上限默认 4、钳 [1, 16]（管理端
    # 字段同名，见 Settings 转存卡片）；探针节流默认 300、钳
    # [30, 3600]；点播当场探针预算默认 15、钳 [1, 60]。范围与
    # 读取侧钳制同口径。纯 SystemConfig 键，不进 env.example。
    "transfer_probe_enabled": ("bool",),
    "transfer_probe_priority_enabled": ("bool",),
    "transfer_probe_lane_concurrency": ("int", 1, 16),
    "transfer_probe_recheck_sec": ("int", 30, 3600),
    "play_inline_probe_budget_sec": ("int", 1, 60),
    # 剧集预热（2026-11-01 转存等待压缩 期四）：总开关默认开
    # （站里没用户口径；读取默认兜底在 transfer_variant.
    # series_preheat_enabled，无行=True）；搬字节预热独立日帽
    # 默认 2、钳 [1, 16]（只数 trigger=series_preheat_byte 的行，
    # 与 prefetch 帽分开）；候选集数默认 3、钳 [1, 10]。范围与
    # 读取侧钳制同口径。纯 SystemConfig 键，不进 env.example。
    "transfer_series_preheat_enabled": ("bool",),
    "transfer_series_preheat_daily_max": ("int", 1, 16),
    "transfer_series_preheat_next_count": ("int", 1, 10),
    "transfer_cross_aging_sec": ("int", 60, 86400),
    "transfer_fast_lane_concurrency": ("int", 1, 32),
    "transfer_per_owner_inflight": ("int", 1, 16),
    "pan115_rapid_upload_enabled": ("bool",),
    "pan115_multipart_upload_enabled": ("bool",),
    "transfer_rebind_waiver_window_days": ("int", 1, 365),
    "transfer_rebind_waiver_max_uses": ("int", 1, 20),
    # 形态 B 云下载（2026-10-30 立项，用户亲批默认关）：开关默认
    # 关由 transfer_variant.cross_cloudpull_enabled 的读取默认
    # 兜底（无行=False）；TTL 默认 43200 覆盖 115 排队调度＋
    # GB 级慢拉＋其内置重试，钳 [1800, 86400]；轮询超时默认
    # 3600，钳 [60, 21600]。纯 SystemConfig 键，不进 env.example。
    "transfer_cross_cloudpull_enabled": ("bool",),
    "transfer_cross_cloudpull_ttl_sec": ("int", 1800, 86400),
    "transfer_cross_cloudpull_timeout_sec": ("int", 60, 21600),
    # 换代线地基记账开关（2026-11-01）：交付状态落盘＋块哈希
    # 清单记账，纯记账 fail-open；默认开由 transfer_variant
    # .manifest_accounting_enabled 的读取默认兜底（无行=True），
    # 关时执行面不接线、行为与上线前逐字一致。纯 SystemConfig
    # 键，不进 env.example（#1527 先例）。
    "transfer_manifest_accounting_enabled": ("bool",),
    # 换代线 step 4 执行面接管开关（2026-11-01）：开 = 115 续传
    # 恢复以落盘交付行为准、清单 complete 的整文件 sha1 直接
    # 采信、同内容在跑单调度合并；须与记账开关联动（记账关时
    # 本开关无数据可依、整步回落）。默认开由 transfer_variant
    # .manifest_takeover_enabled 的读取默认兜底。
    "transfer_manifest_takeover_enabled": ("bool",),
    # 夸克二期（转存 + 点播中继）开关（2026-11-01）：默认开由
    # transfer_variant.quark_play_enabled 的读取默认兜底（无行
    # =True）；关 = 只绑夸克的用户点播在门禁 failed 明示、不
    # 回落共享。纯 SystemConfig 键，不进 env.example。
    "quark_play_enabled": ("bool",),
    # 类别级熔断（2026-11-01 预见层 v2 第一层）：JSON 规则数组
    # [{provider, error_kind}] 的 canonical 文本。结构校验在
    # transfer_variant.coerce_paused_categories（保存侧 strict、
    # 非法 400），注册为 ("str",) 只管落库形态；空/缺行 = 不暂停。
    "transfer_paused_categories": ("str",),
    # 指纹沉淀（2026-11-01 转存等待压缩期三）：读取唯一实现在
    # transfer_sediment（sediment_enabled / tier_enabled /
    # daily_budget_bytes 等，热读 fail-safe），范围与读取侧钳制
    # 同口径。总开关默认开、需求/追剧档默认开、热门/新入库档
    # 默认关（本期只有开关位、不选片），默认均由读取侧兜底
    # （无行=默认）；fused 是转化率熔断的运行时状态键（触发时
    # 由 worker 落 true，管理员清回 false 恢复）。纯
    # SystemConfig 键，不进 env.example（#1527 先例）。
    "transfer_sediment_enabled": ("bool",),
    "transfer_sediment_tier_demand_enabled": ("bool",),
    "transfer_sediment_tier_watching_enabled": ("bool",),
    "transfer_sediment_tier_hot_enabled": ("bool",),
    "transfer_sediment_tier_new_enabled": ("bool",),
    "transfer_sediment_daily_budget_gib": ("int", 1, 10000),
    "transfer_sediment_interval_sec": ("int", 300, 86400),
    "transfer_sediment_max_items_per_round": ("int", 1, 200),
    "transfer_sediment_fuse_window_days": ("int", 7, 90),
    "transfer_sediment_fuse_unconsumed_ratio": ("float", 0.1, 1.0),
    "transfer_sediment_fused": ("bool",),
}

# Drive API 全局限速（2026-10-30）：扫描列举与 Changes 增量共用的
# 全局令牌桶 QPS 上限。读取唯一实现 drive_changes.configured_api_qps
# （热读 + 周期复检），范围与限速器自身钳制 [1, 20] 同口径；
# 无行时读取侧回退默认 8。
DRIVE_API_CONFIG_SCHEMA: dict[str, tuple] = {
    "drive_api_qps": ("float", 1, 20),
}

# 上游聚合拉取参数（2026-10-31 优化 O-05 起，后续 O-01/O-02/O-03
# 的任务参数同家族登记）：读取唯一实现在 upstream_aggregate_run /
# upstream_aggregate_jobs，范围与读取侧钳制同口径；无行时读取侧
# 回退代码默认（页大小 500、心跳过期 120 秒等）。纯 SystemConfig
# 键，不进 env.example（#1527 先例）。
UPSTREAM_AGGREGATE_CONFIG_SCHEMA: dict[str, tuple] = {
    "upstream_aggregate_default_page_size": ("int", 10, 500),
    # 拉取有界并发（2026-11-01 加速）：逐库拉取预取后续页的并发
    # 数，默认 4；1 = 串行旧行为。只改请求时序（页序/计数不变），
    # 增量轮恒串行不读它。读取唯一实现 upstream_aggregate_run
    # .configured_fetch_concurrency，钳制同此范围。
    "upstream_aggregate_fetch_concurrency": ("int", 1, 16),
    "upstream_aggregate_page_retries": ("int", 0, 5),
    "upstream_aggregate_page_retry_backoff_sec": ("int", 0, 30),
    "upstream_aggregate_heartbeat_stale_sec": ("int", 30, 3600),
    "upstream_aggregate_claim_poll_sec": ("int", 1, 60),
    "upstream_aggregate_progress_log_every": ("int", 1, 100),
    "upstream_aggregate_task_retention_days": ("int", 1, 365),
    # O-08 系列级配对（series+episode）置信度：默认 0.9（低于
    # imdb 0.95、高于 title+year 0.7），读取侧钳制同此范围。
    "upstream_aggregate_series_episode_confidence": ("float", 0.01, 1),
    # U-02 详情页上游线路：开时有映射的条目在详情/PlaybackInfo 来源
    # 列表多一条「上游 · <源名>」线路（点它走上游取流口现查映射起播）；
    # 关时列表不出现、取流口 404。无行＝开（读取方默认 True，
    # 见 upstream_playback.detail_line_enabled）。
    "upstream_aggregate_detail_line_enabled": ("bool",),
    # O-09 映射抽样巡检（第 1、2 层常驻 janitor）：默认开、默认
    # 每 7 天一轮、每 evidence 层默认抽 30 条；不一致率超默认
    # 5% 响亮报红。读取唯一实现在 upstream_mapping_patrol，范围
    # 与读取侧钳制同口径。纯 SystemConfig 键，不进 env.example。
    "upstream_aggregate_mapping_patrol_enabled": ("bool",),
    "upstream_aggregate_mapping_patrol_interval_days": ("int", 1, 30),
    "upstream_aggregate_mapping_patrol_sample_size": ("int", 1, 200),
    "upstream_aggregate_mapping_patrol_alert_pct": ("int", 0, 100),
}


def coerce_player_blacklist_rules(value: Any, *, strict: bool) -> list[dict]:
    """播放器黑名单规则结构归一：[{label, match[], exclude[]}]。

    模式串去首尾空白并转小写（匹配侧大小写不敏感，存小写即所见即所得）。
    strict=True（保存校验）：任一结构问题抛 ConfigValueError；
    strict=False（运行时读已存值）：跳过坏规则/坏项，尽量不因一条坏配置
    把匹配整体打挂。
    """
    if not isinstance(value, list):
        if strict:
            raise ConfigValueError("player_login_blacklist 必须是规则数组")
        return []
    rules: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            if strict:
                raise ConfigValueError("player_login_blacklist 每条规则必须是对象")
            continue

        def _patterns(field: str) -> list[str]:
            raw = item.get(field)
            if raw is None:
                return []
            if not isinstance(raw, list) or any(not isinstance(p, str) for p in raw):
                if strict:
                    raise ConfigValueError(
                        f"player_login_blacklist 规则的 {field} 必须是字符串数组")
                return []
            return [p.strip().lower() for p in raw if p.strip()]

        label = item.get("label")
        rules.append({
            "label": str(label) if label is not None else "",
            "match": _patterns("match"),
            "exclude": _patterns("exclude"),
        })
    return rules


def normalize_player_blacklist_rules(value: Any) -> str:
    """保存侧校验入口：JSON 文本或已解析 list → canonical JSON 文本。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ConfigValueError("player_login_blacklist 必须是合法的 JSON 数组")
    rules = coerce_player_blacklist_rules(value, strict=True)
    return json.dumps(rules, ensure_ascii=False)


# 播放器登录黑名单（2026-10-30）：规则数组整体走 json_rules 校验，
# 结构为 [{label, match[], exclude[]}]（backend/emby_server/player_blacklist.py）。
# 空串/None = 删行回代码级默认名单；空数组 [] 存行 = 管理员主动清空（功能停用）。
PLAYER_BLACKLIST_CONFIG_SCHEMA: dict[str, tuple] = {
    "player_login_blacklist": ("json_rules",),
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
    "media_seek_daily_vote_limit": ("int", 0, _BIG),
    # 2026-10-23 去重 #5：两个月限与日限同卡（运营参数）后需在此登记才可
    # 从该页保存；welfare_request_monthly 同时保留在 WELFARE_CONFIG_SCHEMA
    # （公益配置端点的历史口径），paid_request_monthly 此前无任何登记
    "welfare_request_monthly": ("int", 0, 10000),
    "paid_request_monthly": ("int", 0, 10000),
    # r40：卡码授予天数所用默认套餐（codes._resolve_plan 读取；无行=按服自动推导）
    "code_default_plan_id": ("int", 0, _BIG),
    "recharge_enabled": ("bool",), "subscription_purchase_enabled": ("bool",),
    "subscription_buy_self_earned_only": ("bool",),
    "subscription_points_purchase_enabled": ("bool",),
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
    "vitality_watch_tier1_minutes": ("int", 0, _BIG),
    "vitality_watch_tier1_reward": ("int", 0, _BIG),
    "vitality_watch_tier2_minutes": ("int", 0, _BIG),
    "vitality_watch_tier2_reward": ("int", 0, _BIG),
    "vitality_checkin_skip_deduct_enabled": ("bool",),
    # 签到活力奖励（economy._do_checkin_core 读取；默认 1，填 0 则签到不恢复活力）
    "vitality_checkin_gain": ("int", 0, _BIG),
    # 观影能量（货币再设计第一期；watch_points 读取：当日累计分钟分档发能量，
    # 首看/一档/二档奖励与分钟数＋每日封顶，0 奖励=该档关闭、封顶 0=不限）
    "watch_points_first": ("int", 0, _BIG),
    "watch_points_first_minutes": ("int", 0, _BIG),
    "watch_points_tier1": ("int", 0, _BIG),
    "watch_points_tier1_minutes": ("int", 0, _BIG),
    "watch_points_tier2": ("int", 0, _BIG),
    "watch_points_tier2_minutes": ("int", 0, _BIG),
    "watch_points_daily_cap": ("int", 0, _BIG),
    "series_merge_interval_sec": ("int", 3600, 604800),
    "series_merge_batch_limit": ("int", 10, 100000),
}

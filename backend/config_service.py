"""配置写入的统一编排层（配置瘦身第 4 批）：校验→落库→失效→（调用方）提交。

各管理端保存接口的 SystemConfig 写入最终都应收敛到这里。
落库与失效复用 ``integrations.store.write_values``（正统通道：批量落库 + 写时失效）；
本层只新增一层薄编排，不造新轮子：

1. 按家族 schema（``config_schema`` 登记）逐键校验——任一非法即整批拒绝
   （``ConfigValueError``，落库前抛，不写半截）；
2. ``store.write_values`` 批量落库（内部已按 store 纪律写时失效）；
3. 可选审计钩子（调用方负责脱敏；内置的最小实现只记键名、不记值）。

事务：本层不提交，提交由调用方按现有事务边界决定（与 store.write_values 一致）。
空值语义：沿用 config_schema 的约定——空串/None = 未设置，本层删行
（与 economy/welfare 的「空串删行回退默认」口径一致）。

明确不收敛的（见设计报告）：ms_config（密钥语义）、realm 映射层、种子/自愈、
运行态 KV。业务校验（如防盗链签名联动、路径安全）保留在各模块，本层只做
「类型+范围」这一层校验。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend import config_schema, models
from backend.integrations import store

logger = logging.getLogger(__name__)

#: 家族名 → 校验注册表（config_schema 登记）。各家族迁移到本层时在此挂名。
SCHEMAS: dict[str, dict[str, tuple]] = {
    "alerts": config_schema.ALERTS_CONFIG_SCHEMA,
    "reminders": config_schema.REMINDERS_CONFIG_SCHEMA,
    "coupons": config_schema.COUPONS_CONFIG_SCHEMA,
    "registration": config_schema.REGISTRATION_CONFIG_SCHEMA,
    "view_unlock": config_schema.VIEW_UNLOCK_CONFIG_SCHEMA,
    # r36：防盗链（play_sign）家族迁入（用户 2026-10-17 18:29 拍板恢复排期）
    "hotlink": config_schema.HOTLINK_CONFIG_SCHEMA,
    "mount_serving": config_schema.MOUNT_SERVING_CONFIG_SCHEMA,
    # 列表去重缓存（2026-10-18）：开关 + TTL + 条目上限
    "items_cache": config_schema.ITEMS_CACHE_CONFIG_SCHEMA,
    # 转存调度（2026-10-30）：跨 provider 开关 + 三个并发上限
    "transfer": config_schema.TRANSFER_CONFIG_SCHEMA,
    # Drive API 全局限速（2026-10-30）：全局令牌桶 QPS
    "drive_api": config_schema.DRIVE_API_CONFIG_SCHEMA,
    # 播放器登录黑名单（2026-10-30）：规则数组 JSON 校验
    "player_blacklist": config_schema.PLAYER_BLACKLIST_CONFIG_SCHEMA,
    "welfare": config_schema.WELFARE_CONFIG_SCHEMA,
    # 4-6：economy 家族（设计报告说的 44 键，实查 49 键；校验语义与迁移前
    # economy_spec 的历史行为逐字一致，str/secret 用 ("raw",) 原样存库；
    # 复合校验保留在端点层）
    "economy": config_schema.ECONOMY_CONFIG_SCHEMA,
}


def save_config(
    db: Session,
    values: Mapping[str, Any],
    *,
    schema: str,
    audit: Optional[Callable[[str, dict[str, Optional[str]]], None]] = None,
    descriptions: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """统一保存一批配置键。

    :param values: {键: 值}；值走各键的 spec 校验，空串/None = 删行。
    :param schema: SCHEMAS 里的家族名；未登记的键一律拒绝（防笔误造脏键）。
    :param audit: 可选审计钩子 ``(schema_name, normalized)``——normalized 是
        校验后的 {键: 落库字符串}；**调用方负责脱敏**（如 webhook secret 这类
        敏感值不要传进来，或只传脱敏后的审计字典）。
    :param descriptions: 新建行时的 description（仅新行用，存量行不改）。
    :return: 实际处理的键列表。
    :raises config_schema.ConfigValueError: 未知家族/未知键/值非法——整批不落库。
    """
    if not isinstance(values, dict):
        raise config_schema.ConfigValueError("配置必须是对象")
    specs = SCHEMAS.get(schema)
    if specs is None:
        raise config_schema.ConfigValueError(f"未知的配置家族：{schema}")

    # 1) 先整体校验再动库：任一非法直接抛，不会写入半截
    normalized: dict[str, Optional[str]] = {}
    for key, value in values.items():
        spec = specs.get(key)
        if spec is None:
            raise config_schema.ConfigValueError(f"{schema} 家族未知配置键：{key}")
        style = spec[1] if spec[0] == "bool" and len(spec) > 1 else "word"
        normalized[key] = config_schema.normalize(key, spec, value, bool_style=style)

    # 2) 落库：非空走正统批量通道（写时即失效）；空值删行 + 失效
    to_write = {k: v for k, v in normalized.items() if v is not None}
    to_delete = [k for k, v in normalized.items() if v is None]
    if to_write:
        store.write_values(db, to_write, descriptions)
    if to_delete:
        rows = (
            db.query(models.SystemConfig)
            .filter(models.SystemConfig.key.in_(to_delete))
            .all()
        )
        for row in rows:
            db.delete(row)
        store.invalidate(*to_delete)

    # 3) 审计钩子（最小实现：结构化日志只记键名、不记值，避免敏感值落日志）
    logger.info("config.save family=%s keys=%s", schema, sorted(normalized))
    if audit is not None:
        audit(schema, normalized)
    return list(normalized)

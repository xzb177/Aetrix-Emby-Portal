"""找源配置：全部走 env，改动即读（不在 import 时钉死）。

键（见 env.example，r44 防漂移测试要求每个键都有文档）：
- ``SOURCE_DISCOVERY_ENABLED``：总闸，默认关。关时本组接口惰性 404。
- ``SOURCE_DISCOVERY_ADAPTERS``：启用适配器清单，逗号分隔，
  默认 ``upstream_mapping``。清单外的名字一律忽略（不报错、不猜）。
- ``SOURCE_DISCOVERY_UPSTREAM_MAPPING_ENABLED``：分源开关，
  默认开（总闸开时该源才生效；管理员可单独关掉它）。
- ``SOURCE_DISCOVERY_AUTO_TRANSFER_ENABLED``：自动模式，默认关。
  关时确认必须指定候选三元组；开时确认可只给 tmdb_id，
  由服务端按置信度自动挑最高候选。两种模式下搜索本身绝不建任务，
  且绑定 / 转存总闸 / 24h 节流一律照常执行——自动模式不绕过审核。
"""

from __future__ import annotations

import os

MASTER_GATE_ENV = "SOURCE_DISCOVERY_ENABLED"
ADAPTERS_ENV = "SOURCE_DISCOVERY_ADAPTERS"
UPSTREAM_MAPPING_ENV = "SOURCE_DISCOVERY_UPSTREAM_MAPPING_ENABLED"
AUTO_TRANSFER_ENV = "SOURCE_DISCOVERY_AUTO_TRANSFER_ENABLED"

DEFAULT_ADAPTERS = ("upstream_mapping",)

# 分源开关：适配器名 -> env 键。后续适配器在这里登记一行即可。
ADAPTER_SWITCH_ENVS = {
    "upstream_mapping": UPSTREAM_MAPPING_ENV,
}


def _env_on(key: str, default: str) -> bool:
    raw = (os.getenv(key, default) or "").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def discovery_enabled() -> bool:
    """总闸，默认关。"""
    return _env_on(MASTER_GATE_ENV, "0")


def auto_transfer_enabled() -> bool:
    """自动模式，默认关。"""
    return _env_on(AUTO_TRANSFER_ENV, "0")


def adapters_list() -> list[str]:
    """管理员配置的适配器清单（去重、保序、去空）。"""
    raw = os.getenv(ADAPTERS_ENV, "")
    if raw is None or not raw.strip():
        return list(DEFAULT_ADAPTERS)
    seen: set[str] = set()
    out: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out or list(DEFAULT_ADAPTERS)


def adapter_enabled(name: str) -> bool:
    """某适配器的分源开关：未登记的适配器名一律视为关。"""
    env_key = ADAPTER_SWITCH_ENVS.get(name)
    if env_key is None:
        return False
    # upstream_mapping 分源开关默认开；其余未来适配器登记时再定默认值。
    return _env_on(env_key, "1")


def enabled_adapters() -> list[str]:
    """总闸开时真正生效的适配器：清单 ∩ 分源开关开。"""
    if not discovery_enabled():
        return []
    return [name for name in adapters_list() if adapter_enabled(name)]

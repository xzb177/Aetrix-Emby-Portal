"""环境变量读取 helper（StrmAssistant 打磨 R3：横切能力只许一套）。

全仓唯一的 env 数值解析入口（r42 统一迁入）：
- 非法值/空值记 warning 后回落 default，绝不在模块 import 时抛 ValueError
  （此前 64 处模块顶层裸 int()/float() 解析，配错一个值整服务起不来）；
- lo/hi 为 None 时不钳制该侧（保持各调用点原有的 max()/min() 语义）。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def env_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """读整数环境变量，非法值回落 default 并记 warning。

    fail-safe：模块顶层调用也不炸（完美审查 C-P2-6/7）。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        logger.warning("环境变量 %s=%r 非法，已回落默认值 %r", name, raw, default)
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def env_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    """读浮点环境变量，非法值回落 default 并记 warning。

    fail-safe：模块顶层调用也不炸（完美审查 C-P2-6/7）。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        logger.warning("环境变量 %s=%r 非法，已回落默认值 %r", name, raw, default)
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v

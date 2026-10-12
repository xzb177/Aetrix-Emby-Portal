"""环境变量读取 helper（StrmAssistant 打磨 R3：横切能力只许一套）。

四个 worker（probe/thumbnail/subtitle_scan/merge_versions）之前各复制了一份
_env_int/_env_float，现在统一到这里。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def env_int(name: str, default: int, lo: int, hi: int) -> int:
    """读整数环境变量，钳制到 [lo, hi]，非法值回落 default 并记 warning。

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
    return max(lo, min(hi, v))


def env_float(name: str, default: float, lo: float) -> float:
    """读浮点环境变量，下限 lo，非法值回落 default 并记 warning。

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
    return max(lo, v)

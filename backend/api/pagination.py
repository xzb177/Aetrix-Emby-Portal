"""分页参数统一钳制（P1 系统性修复 F-P1-5）。

全站列表接口曾经各写各的 ``min(limit, 200)``：只限上界不限下界，
``limit=-1`` 在 PG 下直接 500、在 SQLite 下被当成"无限制"并触发后继
N+1 全量构造。以后所有新接口统一走这里，老接口逐步迁移。
"""
from __future__ import annotations

from typing import Tuple

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def pagination_params(
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    *,
    default_limit: int = DEFAULT_LIMIT,
    max_limit: int = MAX_LIMIT,
) -> Tuple[int, int]:
    """把 (limit, offset) 钳制到合法范围。

    - ``limit``：非法输入回 ``default_limit``，最终钳制到 ``[1, max_limit]``
    - ``offset``：非法输入回 0，最终钳制到 ``>= 0``
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default_limit
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, max_limit)), max(0, offset)

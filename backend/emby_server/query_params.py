"""Emby 协议查询参数解析（P1 系统性修复 G1-P1-1/2）。

播放协议的 ``Limit``/``StartIndex`` 曾是裸 ``int(q.get(...) or N)``：
无上界（``Limit=1000000`` 一次拉全库进内存序列化），畸形输入直接 500。
以后协议端统一走这里。
"""
from __future__ import annotations

from typing import Mapping


def qint(
    q: Mapping,
    name: str,
    default: int,
    lo: int = 0,
    hi: int = 200,
) -> int:
    """取查询参数并钳制到 ``[lo, hi]``。

    - 缺失/空 → ``default``
    - 非法输入（``?Limit=abc``）→ ``default``（原直接抛 ValueError → 500）
    """
    raw = q.get(name)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
    return max(lo, min(value, hi))


def qlimit(q: Mapping, default: int = 100, hi: int = 200) -> int:
    """取 ``Limit`` 参数：下界 1，上界 ``hi``。"""
    return qint(q, "Limit", default, lo=1, hi=hi)


def qstart(q: Mapping, default: int = 0, hi: int = 100000) -> int:
    """取 ``StartIndex`` 参数：下界 0，加软上界防离谱偏移。"""
    return qint(q, "StartIndex", default, lo=0, hi=hi)

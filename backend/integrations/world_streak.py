"""能力：连签里程碑 · 临界提醒"""
from __future__ import annotations
from sqlalchemy.orm import Session
from backend.integrations import store

SPEC = {
    "title": "连签里程碑 · 临界提醒",
    "desc": "里程碑天数后台可配（逗号分隔），用户连签接近下一个里程碑时前端出临界提示。",
    "group": "探险世界",
    "docs_hint": "配置连签里程碑天数与临界提醒提前天数，用于前端连签临界提示。",
    "fields": [
        {
            "key": "streak_milestones",
            "label": "里程碑天数",
            "type": "str",
            "default": "7,30,100,200,365",
            "hint": "逗号分隔的正整数，例如 7,30,100,200,365",
        },
        {
            "key": "streak_warn_within",
            "label": "临界提醒提前天数",
            "type": "int",
            "default": 1,
            "hint": "距里程碑还差几天内开始提醒，默认 1（只在差 1 天时提醒）",
        },
    ],
}
_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}
_FIELD_DEFAULTS["streak_warn_within"] = 1


def read(db: Session) -> dict:
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def parse_milestones(raw: str) -> list[int]:
    result: set[int] = set()
    if not raw:
        return []
    for seg in str(raw).split(","):
        seg = seg.strip()
        if not seg:
            continue
        try:
            n = int(seg)
        except (TypeError, ValueError):
            continue
        if n > 0:
            result.add(n)
    return sorted(result)


def next_milestone(streak: int, milestones: list[int]) -> tuple[int, int] | None:
    if not milestones:
        return None
    try:
        s = int(streak)
    except (TypeError, ValueError):
        return None
    ordered = sorted({int(m) for m in milestones if int(m) > 0})
    if not ordered:
        return None
    for m in ordered:
        if m > s:
            return (m, m - s)
    return None

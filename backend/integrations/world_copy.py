"""能力：世界文案池 · 每日新鲜感"""
from __future__ import annotations
from sqlalchemy.orm import Session
from backend.integrations import store

SPEC = {
    "title": "世界文案池 · 每日新鲜感",
    "desc": "问候区「今日一句话」文案池，按时段配，后台可改；一行一条，格式「时段 | 文案」，时段取 dawn/day/dusk/night。",
    "group": "探险世界",
    "docs_hint": "配置问候区今日一句话文案池，一行一条，格式「时段 | 文案」，时段取 dawn/day/dusk/night。",
    "fields": [
        {
            "key": "daypart_lines",
            "label": "时段文案池",
            "type": "str",
            "default": (
                "dawn | 清晨的光刚爬上山坡，先挑一部短的醒醒神\n"
                "dawn | 天刚亮，世界还安静，适合慢慢挑片\n"
                "dawn | 晨雾未散，今天的第一部片想看点什么\n"
                "day | 阳光正好，适合追一部明亮的喜剧\n"
                "day | 午后容易困，来部节奏快的提提神\n"
                "day | 白天把长剧存好，晚上一次看个够\n"
                "dusk | 夕阳落在屋顶上，适合重温一部老片\n"
                "dusk | 天快黑了，晚饭配一部下饭剧刚刚好\n"
                "dusk | 黄昏是给经典留的，翻出一部压箱底的吧\n"
                "night | 今晚星星很亮，适合看一部长的\n"
                "night | 夜深了，选一部安静的慢慢看\n"
                "night | 月亮值班，悬疑片的气氛刚刚好"
            ),
            "hint": "一行一条，格式「时段 | 文案」，时段取 dawn/day/dusk/night",
        },
    ],
}
_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}

_VALID_DAYPARTS = ("dawn", "day", "dusk", "night")


def read(db: Session) -> dict:
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def parse_lines(raw: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {d: [] for d in _VALID_DAYPARTS}
    if not raw:
        return result
    for line in str(raw).splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" not in line:
            continue
        daypart, text = line.split("|", 1)
        daypart = daypart.strip()
        text = text.strip()
        if daypart not in _VALID_DAYPARTS:
            continue
        if not text:
            continue
        result[daypart].append(text)
    return result


def pick_line(raw: str, daypart: str, ordinal: int) -> str | None:
    lines_map = parse_lines(raw)
    lines = lines_map.get(daypart) or []
    if not lines:
        return None
    try:
        idx = int(ordinal) % len(lines)
    except (TypeError, ValueError):
        idx = 0
    return lines[idx]

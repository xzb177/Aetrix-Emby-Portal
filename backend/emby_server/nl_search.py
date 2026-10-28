"""自然语言搜片（第三批第二单，默认关）

只搜用户自己的盘和库：调用方（路由层）按
``backend.library_scope.effective_ids_safe`` 算出该用户可见库集合
（``None`` = 不过滤、空集 = 故障 fail-closed），传给
:func:`search_items`；本模块只在该集合内查活行，绝不跨可见范围。

不推片、不摆货架：结果按 id 稳定排序，不做推荐排序、不加主推位；
不调用外部搜索服务、不上传用户数据；解析失败回落普通关键词搜并
在返回值里明示 ``fallback=True``，不编造结果。

解析维度（均可配置，见下表；默认值只是通用词汇表，不是示例内容）：
- 年代：``90年代``/``1990年代``/``90s`` → 1990–1999；``1994年`` → 当年。
  两位数年代按 >=30 归 19xx、<30 归 20xx（``00年代`` → 2000s）。
- 地区：可配置别名表（默认含常见地区词），匹配 ``countries`` 列
  （“国产”按既有口径映射为“内地”，与“大陆”同义）；查询层中文名与
  TMDB origin_country ISO 代码双轨匹配（生产行 countries 存代码）。
- 类型：可配置别名表（默认含常见类型词），匹配 ``genres`` 列。
- 最近：无显式年代时，“最近”/“近期” → 今年及前两年（production_year 区间）。
- 其余文本：关键词，匹配 name / sort_name / original_title / aliases，
  以及 emby_people.name（演员等人名字段）；首尾虚词“的/之”剥离，
  不进 LIKE 查询。

维度表可经 env JSON 覆盖（``NL_SEARCH_REGION_MAP`` /
``NL_SEARCH_GENRE_MAP``，形如 ``{"港": "香港"}``）；JSON 非法时
忽略覆盖、用默认值（搜索面不因配置写错而整体不可用，但覆盖不生效）。

总开关 ``NL_SEARCH_ENABLED``（默认关），每次调用现读 env。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Optional, Sequence

from sqlalchemy import or_

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

ENABLED_ENV = "NL_SEARCH_ENABLED"
REGION_MAP_ENV = "NL_SEARCH_REGION_MAP"
GENRE_MAP_ENV = "NL_SEARCH_GENRE_MAP"

_TRUTHY = {"1", "true", "yes", "on"}

#: 默认地区别名表（键 = 用户可能输入的词，值 = 匹配 countries 列用的词）。
DEFAULT_REGION_MAP: dict[str, str] = {
    "港": "香港", "香港": "香港", "内地": "内地", "大陆": "内地",
    "国产": "内地",
    "台湾": "台湾", "台": "台湾", "日本": "日本", "日": "日本",
    "韩国": "韩国", "韩": "韩国", "美国": "美国", "美": "美国",
    "英国": "英国", "法国": "法国", "德国": "德国", "欧洲": "欧洲",
}
#: 地区中文名 → TMDB origin_country ISO 代码。countries 列的生产取值是
#: ISO 代码（tmdb.py apply_details 只写 origin_country，如 CN/HK/JP），
#: 不是中文名；只按中文名 LIKE 命中不了生产行，故查询层须中文名与代码
#: 双轨匹配（中文名保留，兼容旧夹具/手工写中文名的行；“欧洲”无单码，
#: 仍只按中文名匹配）。
REGION_CODE_MAP: dict[str, str] = {
    "香港": "HK", "内地": "CN", "台湾": "TW", "日本": "JP",
    "韩国": "KR", "美国": "US", "英国": "GB", "法国": "FR", "德国": "DE",
}


def _like_escape(text: str) -> str:
    """LIKE 模式转义：用户输入的 %/_ 须按字面匹配，不当通配符。"""
    return (text.replace("\\", "\\\\")
                .replace("%", "\\%").replace("_", "\\_"))


def _like_col(column, text: str):
    return column.like(f"%{_like_escape(text)}%", escape="\\")


#: 默认类型别名表（键 = 用户可能输入的词，值 = 匹配 genres 列用的词）。
DEFAULT_GENRE_MAP: dict[str, str] = {
    "喜剧": "喜剧", "动作": "动作", "爱情": "爱情", "科幻": "科幻",
    "恐怖": "恐怖", "剧情": "剧情", "动画": "动画", "纪录片": "纪录片",
    "悬疑": "悬疑", "惊悚": "惊悚", "犯罪": "犯罪", "战争": "战争",
    "comedy": "喜剧", "action": "动作", "drama": "剧情",
}

_DECADE_RE = re.compile(r"(\d{2,4})\s*(?:年代|s\b|’s|s’)", re.IGNORECASE)
_YEAR_RE = re.compile(r"((?:19|20)\d{2})\s*年")
_PUNCT_RE = re.compile(r"[，。、；;,.!?！？\s]+")


def enabled() -> bool:
    """总开关：现读 env，默认关。"""
    return (os.getenv(ENABLED_ENV, "0") or "0").strip().lower() in _TRUTHY


class NlSearchDisabled(Exception):
    """开关未开启。"""


def _env_map(env_name: str, default: dict[str, str]) -> dict[str, str]:
    raw = (os.getenv(env_name, "") or "").strip()
    if not raw:
        return dict(default)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("env %s JSON 非法，改用默认维度表", env_name)
        return dict(default)
    if not isinstance(data, dict):
        return dict(default)
    merged = dict(default)
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str) and k and v:
            merged[k] = v
    return merged


def _decade_range(num: int) -> tuple[int, int]:
    if num >= 1000:
        start = (num // 10) * 10
    elif num >= 100:
        start = (num // 10) * 10
    elif num >= 30:
        start = 1900 + (num // 10) * 10
    else:
        start = 2000 + (num // 10) * 10
    return (start, start + 9)


#: 解析后剥离的虚词（“周星驰的”里的“的”若进 LIKE 查询必空；
#: 只剥 token 首尾，不动词中——“月之恋”里的“之”不受影响）。
_PARTICLES = {"的", "之"}


def _strip_particles(token: str) -> str:
    token = token.strip()
    while token and token[0] in _PARTICLES:
        token = token[1:]
    while token and token[-1] in _PARTICLES:
        token = token[:-1]
    return token


#: 解析后丢弃的泛词（“港片”里的“片”、“香港电影”里的“电影”不是关键词，
#: 留下会把结果误收窄到片名含“片”的条目）。
_STOPWORDS = {"片", "电影", "影片", "剧集", "电视剧", "剧"}


def parse_query(text: str) -> dict:
    """自然语言查询 → 结构化条件。

    返回 ``{year_range, year, region, genre, keyword, fallback}``：
    - 任一维度命中 → fallback=False，keyword 为剩余文本（可空）。
    - 全未命中 → fallback=True，keyword 为原查询全文（普通关键词搜）。
    - 多条件组合：各维度同时保留，查询层按 AND 组合。
    """
    raw = (text or "").strip()
    if not raw:
        return {"year_range": None, "year": None, "region": None,
                "genre": None, "keyword": "", "fallback": True}
    rest = raw
    year_range = None
    year = None
    m = _DECADE_RE.search(rest)
    if m:
        year_range = _decade_range(int(m.group(1)))
        rest = rest[:m.start()] + " " + rest[m.end():]
    else:
        m = _YEAR_RE.search(rest)
        if m:
            year = int(m.group(1))
            rest = rest[:m.start()] + " " + rest[m.end():]
        elif "最近" in rest or "近期" in rest:
            # 无显式年代时，“最近/近期” → 今年及前两年（不编造新字段语义，
            # 走既有 production_year 区间；显式年代优先，不覆盖它）。
            y = date.today().year
            year_range = (y - 2, y)
    # 相对时间词在任何分支下都不进 keyword：显式年代已定（90年代最近…）
    # 或两词并存（最近近期…）时，残留的“最近/近期”若进 LIKE 必把结果
    # 误收窄到片名含该词的条目；此处全量清除（只清一次不够）。
    rest = rest.replace("最近", " ").replace("近期", " ")
    region_map = _env_map(REGION_MAP_ENV, DEFAULT_REGION_MAP)
    region = None
    for token in sorted(region_map, key=len, reverse=True):
        if token in rest:
            region = region_map[token]
            rest = rest.replace(token, " ", 1)
            break
    genre_map = _env_map(GENRE_MAP_ENV, DEFAULT_GENRE_MAP)
    genre = None
    for token in sorted(genre_map, key=len, reverse=True):
        if token in rest:
            genre = genre_map[token]
            rest = rest.replace(token, " ", 1)
            break
    keyword = _PUNCT_RE.sub(" ", rest).strip()
    keyword = " ".join(
        t for t in (_strip_particles(tok) for tok in keyword.split(" "))
        if t and t not in _STOPWORDS)
    parsed_any = (year_range is not None or year is not None
                  or region is not None or genre is not None)
    if not parsed_any:
        return {"year_range": None, "year": None, "region": None,
                "genre": None, "keyword": raw, "fallback": True}
    return {"year_range": year_range, "year": year, "region": region,
            "genre": genre, "keyword": keyword, "fallback": False}


def search_items(db, query_text: str,
                 allowed_library_ids: Optional[set[int]],
                 limit: int = 20) -> dict:
    """在可见库内执行结构化搜索。

    ``allowed_library_ids=None`` = 调用方已判定不过滤；空集 = 一律无结果
    （fail-closed，不查库）。只查活行（deleted_at / merged_into_id 为空），
    结果按 id 排序（不做推荐排序）。limit 钳制到 1~100。
    """
    require_enabled = enabled()
    if not require_enabled:
        raise NlSearchDisabled(
            "自然语言搜片未开启：NL_SEARCH_ENABLED=1 后重试（默认关）"
        )
    parsed = parse_query(query_text)
    if allowed_library_ids is not None and not allowed_library_ids:
        return {**parsed, "results": []}
    limit = max(1, min(int(limit or 20), 100))
    q = db.query(em.MediaItem).filter(
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None),
    )
    if allowed_library_ids is not None:
        q = q.filter(em.MediaItem.library_id.in_(sorted(allowed_library_ids)))
    if parsed["year_range"] is not None:
        lo, hi = parsed["year_range"]
        q = q.filter(em.MediaItem.production_year >= lo,
                     em.MediaItem.production_year <= hi)
    if parsed["year"] is not None:
        q = q.filter(em.MediaItem.production_year == parsed["year"])
    if parsed["region"] is not None:
        region_clauses = [_like_col(em.MediaItem.countries, parsed["region"])]
        code = REGION_CODE_MAP.get(parsed["region"])
        if code:
            region_clauses.append(_like_col(em.MediaItem.countries, code))
        q = q.filter(or_(*region_clauses))
    if parsed["genre"] is not None:
        q = q.filter(_like_col(em.MediaItem.genres, parsed["genre"]))
    if parsed["keyword"]:
        kw = parsed["keyword"]
        # 人物匹配走 emby_people.name（演员表；导演若落库同表亦命中）。
        person_hit = db.query(em.EmbyPerson.id).filter(
            em.EmbyPerson.item_id == em.MediaItem.id,
            _like_col(em.EmbyPerson.name, kw),
        ).exists()
        q = q.filter(or_(
            _like_col(em.MediaItem.name, kw),
            _like_col(em.MediaItem.sort_name, kw),
            _like_col(em.MediaItem.original_title, kw),
            _like_col(em.MediaItem.aliases, kw),
            person_hit,
        ))
    rows = q.order_by(em.MediaItem.id).limit(limit).all()
    return {**parsed, "results": [
        {"id": r.id, "name": r.name, "item_type": r.item_type,
         "production_year": r.production_year, "genres": r.genres or ""}
        for r in rows]}

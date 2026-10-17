"""媒体搜索：归一化、多别名与结果排序

对标成熟媒体站的做法，把「标题完全匹配 / 前缀匹配」排在「别名命中 / 模糊命中」之前，
并把中英文、繁简体、多别名统一成同一个可比较形式。

要点：

- **归一化**：NFKC（全角→半角）、大小写、标点与空白、繁→简。
  繁简转换依赖 `zhconv`（缺失时退化为不做转换，并只告警一次，不影响搜索可用性）。
- **多别名**：name / original_title / aliases / sort_name / tags / studios / genres。
- **排序**：完全匹配 > 前缀 > 词边界前缀 > 子串 > 别名 > 分类元数据 > 模糊相似。
- **演员**：当前数据模型还没有演职员表，因此不做演员加权（留了 `_cast_score` 挂点，
  等 cast 落库后接上即可，不会影响现有排序语义）。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable, Optional
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# ==================== 分值（越大越优先）====================

TIER_TITLE_EXACT = 100      # 标题归一化后完全相等
TIER_TITLE_PREFIX = 90      # 标题以查询开头
TIER_TITLE_WORD = 80        # 词边界前缀（"rick" 命中 "Rick and Morty"）
TIER_TITLE_CONTAINS = 70    # 标题包含查询
TIER_ALIAS_EXACT = 65       # 别名 / 原名完全相等
TIER_ALIAS_CONTAINS = 55    # 别名 / 原名包含
TIER_META = 40              # 类型 / 工作室 / 标签命中
TIER_FUZZY = 25             # difflib 相似度达到阈值
TIER_NONE = 0

FUZZY_THRESHOLD = 0.72
# 单次搜索最多参与排序的候选数，防止整库载入内存
CANDIDATE_LIMIT = 500

# 注意：标点与分隔符换成空格（而不是删掉），否则 "rick" 与 "rickard" 无法区分，
# _title_score 里的「词边界前缀」分支永远不会生效。
_PUNCT_RE = re.compile(
    r"[\-_\.·・、,，。:：;；!！?？'\"“”‘’()（）\[\]【】{}<>《》/\\|~+=*&#@$%^`]+"
)
_NOISE_RE = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|dolby|dv|hevc|h264|h265|x264|x265|avc|"
    r"aac|ac3|dts|flac|truehd|atmos|web-?dl|webrip|bluray|blu-ray|bdrip|remux|hdtv|"
    r"repack|proper|internal|complete|multi|chs|cht|eng|gb|big5)\b",
    re.IGNORECASE,
)

_zhconv = None
_zhconv_warned = False


def _get_zhconv():
    """惰性加载 zhconv（未安装时返回 None 并只告警一次）"""
    global _zhconv, _zhconv_warned
    if _zhconv is None and not _zhconv_warned:
        try:
            import zhconv  # type: ignore

            _zhconv = zhconv
        except Exception:  # noqa: BLE001 — 未安装时降级，不影响搜索
            _zhconv_warned = True
            logger.warning("未安装 zhconv，繁简体搜索将不做转换（pip install zhconv 可启用）")
            return None
    return _zhconv


def to_simplified(text: str) -> str:
    """繁体 → 简体（zhconv 缺失时原样返回）"""
    if not text:
        return ""
    conv = _get_zhconv()
    if conv is None:
        return text
    try:
        return conv.convert(text, "zh-cn")
    except Exception:  # noqa: BLE001
        return text


def to_traditional(text: str) -> str:
    """简体 → 繁体（用于把用户输入也扩成繁体，命中库里遗留的繁体标题）"""
    if not text:
        return ""
    conv = _get_zhconv()
    if conv is None:
        return text
    try:
        return conv.convert(text, "zh-hant")
    except Exception:  # noqa: BLE001
        return text


def strip_noise(text: str) -> str:
    """去掉发布标签（1080p / WEB-DL / x265 …），让「片子名」可比"""
    return _NOISE_RE.sub(" ", text or "")


def normalize(text: str) -> str:
    """归一化：全角→半角、去发布标签与标点、压缩空白、大小写、繁→简"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = strip_noise(s)
    s = _PUNCT_RE.sub(" ", s)
    s = to_simplified(s)
    return re.sub(r"\s+", " ", s).strip().lower()


def normalize_compact(text: str) -> str:
    """更激进的归一化：在 normalize 基础上去掉所有空白（中文标题里空格常不一致）"""
    return re.sub(r"\s+", "", normalize(text))


def split_fields(raw: Optional[str]) -> list[str]:
    """把逗号/竖线分隔的多值字段拆成列表"""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[,|]", str(raw)) if p.strip()]


# ==================== 候选生成（用于 SQL 预筛）====================

#: 拼音首字母搜索的输入形态：纯 ASCII 字母（可含空格，如手机键盘常输入的 "c j h"）
_PINYIN_INPUT_RE = re.compile(r"[A-Za-z ]+")


def search_variants(term: str) -> list[str]:
    """把查询词扩成多个候选形式，供 SQL LIKE 预筛使用

    覆盖：原样、繁→简、简→繁、去掉发布标签后的核心词、拼音首字母去空格小写。
    这样「输入简体命中繁体标题」「输入带标签的完整文件名」「输入拼音首字母
    （带空格也行）命中中文标题」都能捞到候选，再交给 rank_items 精排。
    """
    term = (term or "").strip()
    if not term:
        return []
    variants = [term, to_simplified(term), to_traditional(term)]
    core = strip_noise(term).strip()
    if core and core != term:
        variants.extend([core, to_simplified(core), to_traditional(core)])
    # 拼音首字母搜索（2026-10-17，对标 raisemby）：
    # sort_name 存的是无空格拼音首字母串（"长津湖"→"cjh"），纯 ASCII 字母
    # （可含空格）输入时追加去空格小写变体，让 sort_name.ilike("%cjh%")
    # 捞到候选；中文/混合输入维持原逻辑、零变化。单字母不追加（太泛）。
    if _PINYIN_INPUT_RE.fullmatch(term):
        compact = re.sub(r"\s+", "", term).lower()
        if len(compact) >= 2 and all(compact != v.lower() for v in variants):
            variants.append(compact)

    out: list[str] = []
    for v in variants:
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out[:6]


# ==================== 打分 ====================


def _title_score(term_n: str, term_c: str, value: Optional[str]) -> int:
    """对单个标题字段打分（term_n 带空格的归一化形式；term_c 去空格形式）"""
    if not value:
        return TIER_NONE
    value_n = normalize(value)
    if not value_n:
        return TIER_NONE

    if value_n == term_n:
        return TIER_TITLE_EXACT
    if value_n.startswith(term_n):
        return TIER_TITLE_PREFIX
    # 词边界前缀：英文标题按词切分
    if any(w.startswith(term_n) for w in value_n.split()):
        return TIER_TITLE_WORD
    if term_n in value_n:
        return TIER_TITLE_CONTAINS

    # 中文标题常无空格，补一次去空格比较
    value_c = normalize_compact(value)
    if term_c and term_c != term_n:
        if value_c == term_c:
            return TIER_TITLE_EXACT - 2
        if value_c.startswith(term_c):
            return TIER_TITLE_PREFIX - 2
        if term_c in value_c:
            return TIER_TITLE_CONTAINS - 2
    return TIER_NONE


def _cast_score(term: str, item) -> int:
    """演员匹配挂点

    当前 MediaItem 没有演职员字段，恒返回 0（不影响排序）。
    等 cast 落库后在这里返回 TIER_META + 一个偏移即可，排序语义无需改动。
    """
    return 0


def score_item(item, term: str) -> tuple[int, str]:
    """返回 (分值, 命中层级) —— 分值为 0 表示不匹配"""
    term = (term or "").strip()
    if not term:
        return TIER_NONE, "none"

    term_n = normalize(term)
    term_c = normalize_compact(term)
    if not term_n:
        return TIER_NONE, "none"

    # 1) 标题族：name > original_title > sort_name
    best = 0
    level = "none"
    for field, label in ((item.name, "title"), (item.original_title, "original_title"),
                         (getattr(item, "sort_name", None), "sort_name")):
        s = _title_score(term_n, term_c, field)
        if s > best:
            best, level = s, label

    # 2) 别名族：aliases（逗号分隔的多别名）> tags
    for field, label, exact, contains in (
        (getattr(item, "aliases", None), "alias", TIER_ALIAS_EXACT, TIER_ALIAS_CONTAINS),
        (getattr(item, "tags", None), "tag", TIER_ALIAS_CONTAINS, TIER_META),
    ):
        for value in split_fields(field):
            if not value:
                continue
            value_n = normalize(value)
            if not value_n:
                continue
            if value_n == term_n or normalize_compact(value) == term_c:
                s = exact
            elif term_n in value_n or (term_c and term_c in normalize_compact(value)):
                s = contains
            else:
                s = TIER_NONE
            if s > best:
                best, level = s, label

    # 3) 分类元数据：类型 / 工作室
    for field, label in ((item.genres, "genre"), (item.studios, "studio")):
        for value in split_fields(field):
            value_n = normalize(value)
            if value_n and (value_n == term_n or term_n in value_n):
                if TIER_META > best:
                    best, level = TIER_META, label
                break

    # 4) 演员（预留）
    if best < TIER_META:
        cast = _cast_score(term, item)
        if cast > best:
            best, level = cast, "cast"

    # 5) 模糊兜底
    if best == TIER_NONE:
        name_n = normalize(item.name)
        if name_n:
            ratio = SequenceMatcher(None, term_n, name_n).ratio()
            if ratio >= FUZZY_THRESHOLD:
                return TIER_FUZZY, "fuzzy"

    return best, level


def rank_items(items: Iterable, term: str, limit: Optional[int] = None) -> list:
    """按相关度排序；不匹配的条目被丢弃

    同分时按「集/季编号更小者优先，其次短标题优先」，保证结果稳定可复现。
    """
    scored = []
    for item in items:
        score, level = score_item(item, term)
        if score <= 0:
            continue
        scored.append((score, level, item))

    def _sort_key(entry):
        score, level, item = entry
        episode = getattr(item, "episode_number", None) or 0
        season = getattr(item, "season_number", None) or 0
        return (-score, season, episode, len(getattr(item, "name", "") or ""), str(getattr(item, "guid", "")))

    scored.sort(key=_sort_key)
    ranked = [entry[2] for entry in scored]
    return ranked[:limit] if limit else ranked

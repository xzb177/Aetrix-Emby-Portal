# -*- coding: utf-8 -*-
"""拼音首字母排序（对标 StrmAssistant "Pinyin Initials Sort Title"）。

本模块同时是拼音**搜索**派生键的生成口（2026-10-24，FMBY 学习批二
#16）：pinyin_full / make_pinyin_fields 由标题生成全拼与首字母紧凑键，
落库到 emby_items 的 pinyin_full / pinyin_initials 两列供搜索使用，
与下面的排序用途共用同一套生成口径（逐字取 pypinyin 默认读音）。

StrmAssistant 的描述："Auto generate pinyin initials as sort title"。
实现：中文标题自动生成拼音首字母作为 sort_name，用于排序，不改变显示。

例如：
- "长津湖" → "zjh"（或 "长津湖" 的首字母）
- "肖申克的救赎" → "xskdjsh"
- "The Shawshank Redemption" → "the shawshank redemption"（非中文原样小写）

与现有 sort_name 的关系：
- 扫描器原来把 sort_name 设为 name.lower()，中文标题按 Unicode 排序，
  中文全挤在一起，体验差。
- 本模块生成拼音首字母，中文标题可以按 A-Z 排序。
- 开关：PINYIN_SORT_ENABLED（默认开）。关闭则回退到 name.lower()。
"""

import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

PINYIN_SORT_ENABLED = (
    os.getenv("PINYIN_SORT_ENABLED", "1") or "1"
).strip().lower() not in ("0", "false", "no")

# 中文字符范围：CJK 统一表意文字
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


@lru_cache(maxsize=65536)
def pinyin_initials(name: str) -> str:
    """生成拼音首字母排序键。

    - 含中文：每个中文字符取拼音首字母，非中文字符保留原样（小写）。
    - 纯非中文：直接小写返回。
    - 空：返回空字符串。

    lru_cache：同一系列名在剧/季/集行里重复计算，去重后扫描更快。
    """
    if not name:
        return ""
    name = name.strip()
    if not name:
        return ""
    if not _has_cjk(name):
        return name.lower()
    try:
        from pypinyin import pinyin, Style
    except ImportError:
        logger.warning("pypinyin 未安装，拼音排序回退到小写")
        return name.lower()
    try:
        # 每个字符取首字母，非中文字符 pypinyin 会原样返回
        result = pinyin(name, style=Style.FIRST_LETTER)
        initials = "".join(item[0] for item in result if item and item[0])
        # 去掉非字母数字（标点/空格），保留紧凑的排序键
        initials = re.sub(r"[^a-z0-9]", "", initials.lower())
        return initials or name.lower()
    except Exception as e:
        logger.warning("拼音转换失败 %r: %s，回退到小写", name, e)
        return name.lower()


def make_sort_name(name: str) -> str:
    """生成 sort_name：拼音排序开时用拼音首字母，关时用小写。

    这是扫描器统一入口，替代原来的 `name.lower()`。
    """
    if not name:
        return ""
    if PINYIN_SORT_ENABLED and _has_cjk(name):
        return pinyin_initials(name)
    return name.lower()


def _compact_key(text: str) -> str:
    """搜索键紧凑化：只留 a-z0-9（拼音字段与查询紧凑形式同一口径）。"""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@lru_cache(maxsize=65536)
def pinyin_full(name: str) -> str:
    """生成拼音全拼搜索键（紧凑小写，如「流浪地球」→「liulangdiqiu」）。

    与 pinyin_initials 同口径：逐字取 pypinyin 默认读音，非中文字符原样
    保留，最后统一紧凑化。纯非中文直接小写紧凑返回。多音字只取默认
    读音一组（如「长津湖」的长读 zhang），不生成异读变体——与 sort_name
    首字母的既有口径一致，异读扩展留待后续。
    """
    if not name:
        return ""
    name = name.strip()
    if not name:
        return ""
    if not _has_cjk(name):
        return _compact_key(name)
    try:
        from pypinyin import pinyin, Style
    except ImportError:
        logger.warning("pypinyin 未安装，拼音全拼回退到小写紧凑")
        return _compact_key(name)
    try:
        result = pinyin(name, style=Style.NORMAL)
        full = "".join(item[0] for item in result if item and item[0])
        return _compact_key(full) or _compact_key(name)
    except Exception as e:  # noqa: BLE001 — 生成失败不许拖垮落库，回退紧凑原名
        logger.warning("拼音全拼转换失败 %r: %s，回退到小写紧凑", name, e)
        return _compact_key(name)


def make_pinyin_fields(name: str) -> tuple[str, str]:
    """由标题生成 (全拼, 首字母) 两个搜索派生键，一律紧凑小写。

    条目落库钩子（models.fill_item_pinyin_fields）与存量回填脚本
    （scripts/backfill_pinyin_search_fields.py）共用这一入口，保证同一
    标题在任何路径下生成的键逐字一致。与 make_sort_name 的差异：搜索键
    一律紧凑化（纯英文标题的首字母键也去空格），且不受
    PINYIN_SORT_ENABLED 影响——排序关掉，拼音照样要能搜。
    """
    if not name:
        return "", ""
    return pinyin_full(name), _compact_key(pinyin_initials(name))

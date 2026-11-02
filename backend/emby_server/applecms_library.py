# -*- coding: utf-8 -*-
"""苹果 CMS 媒体库类型的共享小工具（库行字段解析，零依赖）。

库类型本身长在 ``emby_libraries`` 行上（``kind`` /
``upstream_source_ids`` / ``cms_type_map`` 三列，见 models.Library），
消费方有三类：管理端建库/改库（portal）、扫描隔离（scan_queue /
scanner）、CMS 同步工人（applecms_library_sync）。解析口径必须只有
一份，所以收在这里；本模块只做纯文本解析，不碰 DB、不碰网络。
"""

from __future__ import annotations

import json
from typing import Iterable, List, Optional

#: 库类型取值：local = 现有全部库；applecms = 本案新类型。
KIND_LOCAL = "local"
KIND_APPLECMS = "applecms"
LIBRARY_KINDS = (KIND_LOCAL, KIND_APPLECMS)


def library_kind(library) -> str:
    """库类型：老行/老对象没有 kind 列时按 local（升级前行为）。"""
    kind = getattr(library, "kind", None)
    return kind if kind in LIBRARY_KINDS else KIND_LOCAL


def is_applecms_library(library) -> bool:
    return library_kind(library) == KIND_APPLECMS


def parse_upstream_source_ids(raw) -> List[int]:
    """逗号分隔的源 id 文本 → int 列表（去空、去重、保序）。

    入参可为库行对象（取其 upstream_source_ids 列）或原始文本。
    非法片段直接跳过：这一列只由管理端校验后写入，读侧宽容不炸。
    """
    if raw is not None and not isinstance(raw, str):
        raw = getattr(raw, "upstream_source_ids", "")
    text = (raw or "").strip()
    if not text:
        return []
    out: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in out:
            out.append(value)
    return out


def format_upstream_source_ids(ids: Iterable[int]) -> str:
    out: List[int] = []
    for value in ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in out:
            out.append(number)
    return ",".join(str(v) for v in out)


def parse_cms_type_map(raw) -> dict:
    """分类映射 JSON 文本 → {分类名: "in"/"out"}（非法一律回空 dict）。

    入参可为库行对象（取其 cms_type_map 列）、原始文本或已解析 dict。
    空 = 全收（由消费方按此口径处理）；值只认 in/out，其余条目丢弃。
    """
    if raw is not None and not isinstance(raw, (str, dict)):
        raw = getattr(raw, "cms_type_map", None)
    if isinstance(raw, dict):
        data = raw
    else:
        text = (raw or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        name = str(key).strip()
        verdict = str(value).strip().lower()
        if name and verdict in ("in", "out"):
            out[name] = verdict
    return out


def format_cms_type_map(mapping) -> Optional[str]:
    """分类映射 dict → 入库 JSON 文本；空映射存 None（= 全收）。"""
    parsed = parse_cms_type_map(mapping)
    if not parsed:
        return None
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)

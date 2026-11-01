"""系统性去重合并（参考 Emby 多版本逻辑）。

Emby 官方的多版本机制：
- 同一部电影/剧集（同 TMDB ID，或同名+年份）只显示一条
- 多个文件版本挂在同一条目下（MediaSources）
- 去重键：电影 = TMDB ID 或 标题+年份；剧集 = TMDB ID 或 标题+年份

本模块提供：
1. API 层去重：列表返回时按去重键分组，每组只返回主记录
2. 扫描器防重：建条目前查库里是否已存在同去重键的记录
3. 存量清理：一次性脚本合并已有重复
"""

import os
import re
import unicodedata
from typing import Any, Iterable, Optional


# 只对顶层条目去重：movie / series。season / episode 不去重
# （episode 的去重通过 series 合并自然解决）
DEDUP_TYPES = ("movie", "series")


#: 行尾残缺年份后缀：手工改名残留的「九尾狐传 (2023」（括号未闭合）。
#: 归一化时当作残缺片段剥掉，让它与正名「九尾狐传」归一一致，避免认亲失败
#: （缺集普查 2026-10-19 桶 C2）。只剥**行尾**、只认 ``(19|20)xx`` 四位年份；
#: 闭合的「(2023)」**故意不动**——年份仍是跨年同名剧的区分信号（dedup_key 的
#: year 分量与 find_duplicate_ids 的 ±1 过滤都依赖它）；剥掉会把
#: 「X (2023)」和「X (2024)」误判成同一部。非年份的未闭合括号
#: （如「xxx (导演剪辑版」）同样不动。
_TRAILING_BROKEN_YEAR_RE = re.compile(r"[\(（\[【［]\s*(?:19|20)\d{2}\s*$")


def normalize_name(name: Optional[str]) -> str:
    """归一化标题：NFKC、全小写、去所有空白与标点。

    目标：让「19号消防局」「19号消防局 」「19號消防局」算同一个。
    注意：不做繁简转换（避免误合并），只做空白/标点/大小写归一。
    另见 _TRAILING_BROKEN_YEAR_RE：行尾未闭合的年份后缀（如「(2023」）
    先当作残缺片段剥掉。
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", name)
    s = s.lower()
    s = _TRAILING_BROKEN_YEAR_RE.sub("", s)
    # 去掉所有空白和标点，只保留字母数字（中日韩字符属于字母，保留）
    s = "".join(ch for ch in s if ch.isalnum())
    return s


# 宽松归一化用的虚词字符集。
# 可用环境变量 SERIES_LOOSE_PARTICLES 追加虚词：
# extra = os.getenv("SERIES_LOOSE_PARTICLES", "")
SERIES_LOOSE_PARTICLES = "的之和与特·-＆&・･•"


def normalize_name_loose(name: Optional[str]) -> str:
    """宽松归一化：normalize_name() 基础上再剥虚词与尾部年份数字。

    只用于同剧识别（扫描防重/自动合并），不许替代 normalize_name() 现有调用点。
    """
    s = normalize_name(name)
    s = re.sub(r"(19|20)\d{2}$", "", s)
    # 环境变量追加的虚词同样转小写：normalize_name 已把输入转小写，
    # 大写追加进来永远匹配不上（r29 测试抓到）。
    extra = (os.getenv("SERIES_LOOSE_PARTICLES", "") or "").lower()
    particles = SERIES_LOOSE_PARTICLES + extra
    return "".join(ch for ch in s if ch not in particles)


def dedup_key(item: Any) -> Optional[tuple]:
    """返回条目的去重键，None 表示不参与去重。

    键格式：
    - 有 tmdb_id：(library_id, item_type, "tmdb", tmdb_id)
    - 无 tmdb_id：(library_id, item_type, "name", 归一化标题, production_year)
    """
    item_type = getattr(item, "item_type", None)
    if item_type not in DEDUP_TYPES:
        return None
    library_id = getattr(item, "library_id", None)
    tmdb_id = (getattr(item, "tmdb_id", None) or "").strip()
    if tmdb_id:
        return (library_id, item_type, "tmdb", tmdb_id)
    name = normalize_name(getattr(item, "name", None))
    if not name:
        return None
    year = getattr(item, "production_year", None) or 0
    return (library_id, item_type, "name", name, year)


def dedup_key_for(library_id: int, item_type: str,
                  tmdb_id: Optional[str] = None,
                  name: Optional[str] = None,
                  year: Optional[int] = None) -> Optional[tuple]:
    """不依赖 ORM 对象，直接用字段构造去重键（扫描器用）。"""
    if item_type not in DEDUP_TYPES:
        return None
    tmdb_id = (tmdb_id or "").strip()
    if tmdb_id:
        return (library_id, item_type, "tmdb", tmdb_id)
    norm = normalize_name(name)
    if not norm:
        return None
    return (library_id, item_type, "name", norm, year or 0)


def _primary_score(item: Any) -> tuple:
    """主记录评分：分数越小越优先。

    优先级：有本地文件 > 有海报 > 有 tmdb_id > 有简介 > id 小（最早入库）

    「有本地文件」居首（进库一期，2026-11-01）：上游托管行（无
    file_path、只有上游线路）只是展示/播放兜底，本地文件行才是正主；
    托管行当主记录会让本地播放、转存、扫描口径全部落空。托管行被
    合并后其映射迁到存活行（merge_versions_worker 调
    upstream_item_mappings.migrate_mappings），线路不丢。
    """
    has_poster = bool(getattr(item, "poster_path", None)
                      or getattr(item, "primary_image_url", None))
    has_tmdb = bool((getattr(item, "tmdb_id", None) or "").strip())
    has_overview = bool((getattr(item, "overview", None) or "").strip())
    item_id = getattr(item, "id", 0) or 0
    return (0 if (getattr(item, "file_path", None) or "") else 1,
            0 if has_poster else 1,
            0 if has_tmdb else 1,
            0 if has_overview else 1,
            item_id)


def pick_primary(group: list) -> Any:
    """从同组中选主记录。

    三期（开关 ``IDENTITY_SHELF_PHASE3_ENABLED``，须总开关同时开启）：
    组内有已绑定成员时，只在已绑定成员里选主——有海报的未绑定份不得
    压过已绑定的成员当代表（设计稿 §3.2 的坑）。开关关时与旧评分
    逐字一致；本函数只改组内代表人选，不改分组结果（分组键的三级
    扩展另立项，见 identity_gate 模块说明）。
    """
    if len(group) > 1:
        # 延迟导入：dedup 被扫描器等底层路径 import，identity_gate 只依赖
        # models/env_util，无循环引用；函数级 import 让测试 patch 常量即时生效。
        from backend.emby_server import identity_gate as _gate
        if _gate.phase3_enabled():
            bound = [g for g in group if _gate.is_bound_row(g)]
            if bound:
                group = bound
    return min(group, key=_primary_score)


def deduplicate_items(items: Iterable) -> list:
    """对条目列表去重：同去重键只保留一条主记录。

    被合并的记录 id/guid 会挂在主记录的 ``_merged_ids`` / ``_merged_guids``
    属性上（内存属性，不写库），供详情页合并集数/版本时使用。

    非 movie/series 类型原样返回，不参与去重。

    多版本合并（StrmAssistant #4）：``merged_into_id`` 非空的条目是已被
    物理合并的 alternate version，直接过滤掉（主记录会展示多版本）。
    """
    items = list(items)
    # 先过滤已被物理合并的（数据层合并优先于展示层去重）
    items = [i for i in items if getattr(i, "merged_into_id", None) is None]
    groups: dict = {}
    order: list = []  # 保持原有顺序：记录每个 key 首次出现的位置
    for item in items:
        key = dedup_key(item)
        if key is None:
            # 不参与去重：用 guid 占一个独立组，保证原样返回
            key = ("__raw__", getattr(item, "guid", id(item)))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)

    result = []
    for key in order:
        group = groups[key]
        if len(group) == 1:
            result.append(group[0])
            continue
        primary = pick_primary(group)
        # 挂合并信息（内存属性）
        primary._merged_ids = [g.id for g in group if g.id != primary.id]
        primary._merged_guids = [g.guid for g in group if g.guid != primary.guid]
        primary._merged_count = len(group) - 1
        result.append(primary)
    return result


def find_duplicate_ids(db, library_id: int, item_type: str,
                       tmdb_id: Optional[str] = None,
                       name: Optional[str] = None,
                       year: Optional[int] = None,
                       exclude_id: Optional[int] = None) -> list:
    """在库里找同去重键的重复记录 id 列表（不含 exclude_id）。

    扫描器防重用：建 series/movie 前先查，命中则复用已有记录。
    """
    # 延迟导入，避免循环引用
    from . import models as em

    if item_type not in DEDUP_TYPES:
        return []
    q = db.query(em.MediaItem.id).filter(
        em.MediaItem.library_id == library_id,
        em.MediaItem.item_type == item_type,
        em.MediaItem.deleted_at.is_(None),
    )
    if exclude_id:
        q = q.filter(em.MediaItem.id != exclude_id)
    tmdb_id = (tmdb_id or "").strip()
    if tmdb_id:
        q = q.filter(em.MediaItem.tmdb_id == tmdb_id)
        return [row[0] for row in q.all()]

    # 无 tmdb_id 时按归一化标题匹配：SQL 层面做近似（去空格小写），
    # Python 层面再用 normalize_name 精确过滤
    norm = normalize_name(name)
    if not norm:
        return []
    # 先按 name 粗筛（避免全表扫），再精确比对
    candidates = db.query(em.MediaItem.id, em.MediaItem.name,
                          em.MediaItem.production_year).filter(
        em.MediaItem.library_id == library_id,
        em.MediaItem.item_type == item_type,
        em.MediaItem.deleted_at.is_(None),
    )
    if year:
        # 年份允许 ±1 容差（刮削年份偶尔差一年）
        candidates = candidates.filter(
            em.MediaItem.production_year.between(year - 1, year + 1))
    if exclude_id:
        candidates = candidates.filter(em.MediaItem.id != exclude_id)
    out = []
    for row_id, row_name, row_year in candidates.all():
        if normalize_name(row_name) != norm:
            continue
        # 年份都存在时要求一致（已在 SQL 做了 ±1，这里收紧到精确或任一为空）
        if year and row_year and abs(year - row_year) > 1:
            continue
        out.append(row_id)
    return out


def group_duplicates(db, library_id: Optional[int] = None) -> dict:
    """全库扫描重复组：{(library_id, item_type, key): [ids]}，key 为 tmdb_id 或归一化名。

    存量清理脚本用。只返回成员数 > 1 的组。
    """
    from . import models as em

    q = db.query(em.MediaItem.id, em.MediaItem.library_id,
                 em.MediaItem.item_type, em.MediaItem.tmdb_id,
                 em.MediaItem.name, em.MediaItem.production_year).filter(
        em.MediaItem.item_type.in_(DEDUP_TYPES),
        em.MediaItem.deleted_at.is_(None),
    )
    if library_id:
        q = q.filter(em.MediaItem.library_id == library_id)

    groups: dict = {}
    for row_id, lib_id, itype, tmdb_id, name, year in q.all():
        tmdb_id = (tmdb_id or "").strip()
        if tmdb_id:
            key = (lib_id, itype, "tmdb", tmdb_id)
        else:
            norm = normalize_name(name)
            if not norm:
                continue
            key = (lib_id, itype, "name", norm, year or 0)
        groups.setdefault(key, []).append(row_id)

    return {k: v for k, v in groups.items() if len(v) > 1}

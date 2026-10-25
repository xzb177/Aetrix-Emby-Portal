"""苹果 CMS 同步工人（W3，FMBY 学习 #25 苹果 CMS 并列线）。

定位（用户已定，绝不越界）：
- 苹果 CMS 与 Emby/Jellyfin **并列**，绝不做兜底定位：本模块只做
  目录同步——把 CMS 目录转成上游聚合记录、按标题+年份配对后写
  映射；不建货架面、不写播放地址、不新增 API 路由。
- 只写两处：映射（经可注入的 ``record_map``）与健康进度（经可
  注入的 ``set_health``）。播放地址绝不落库：本模块**从不读取**
  CMS 条目的 ``vod_play_url``，``vod_douban_id`` 也绝不用于配对
  （CMS 无 tmdb/imdb，配对证据恒为 ``'title+year'``）。

依赖一律可注入（生产接线由协调员在组装时传入真实函数）：
- ``fetch_page(source, category, page, *, h=None)``：拉一页 CMS
  list，返回含 ``list`` 与 ``pagecount``（或 ``page_count``，或
  ``total``/``limit``）的字典。``category`` 即绑定分类的 ``t`` 参数，
  ``h`` 为增量参数，原样透传。
- ``fetch_detail(source, vod_id)``：可选；只在 list 条目缺
  ``vod_name``/``vod_year`` 时调用一次做补全，不用于取播放地址。
- ``list_bindings(source)``：列出绑定，每项为对象或字典，含
  ``sync_enabled`` 与分类（``t`` / ``category_id`` /
  ``upstream_category_id`` / ``category`` 任一）；缺 ``sync_enabled``
  的绑定按不同步处理（fail-closed）。
- ``list_locals(binding)``：列出该绑定对应的本地候选条目（对象或
  字典，含 ``id``、标题、年份；剧集成员可带 ``episode_number`` /
  ``item_type``）。不传时全部 CMS 条目走未匹配路径。
- ``record_map(*, local_item_id, source_id, upstream_item_id,
  confidence, evidence, episode_index)``：映射写入。注意：本仓库
  现有 ``upstream_item_mappings.record_mapping`` 尚无
  ``episode_index`` 参数（另一路在加），生产接线须由协调员的适配
  器透传该参数，本模块不改那个文件。
- ``set_health(source_id, health)``：健康写入，``health`` 取值见下。
- ``sleep(seconds)``：默认 ``time.sleep``，测试注入 fake 计次。

开关（代码里读 env，默认值照任务书；本批不改 ``env.example``）：
- ``APPLECMS_ENABLED=0`` 与 ``UPSTREAM_SYNC_ENABLED=0``：两闸必须
  同时为 ``"1"`` 才同步，否则无副作用返回 ``health="disabled"``。
- ``APPLECMS_UNMATCHED_MODE=parallel``：未匹配处理，取值
  ``parallel`` | ``list`` | ``hide``（``list`` 与 ``parallel`` 同义：
  计入结果的未匹配列表；``hide``：从结果列表排除并计入 hidden）。
- ``APPLECMS_SYNC_PAGE_DELAY_SEC=0.5``：页间等待秒数。
- 映射写路径仍受既有 ``UPSTREAM_ITEM_MAPPING_ENABLED`` 约束（关
  时不调 ``record_map``，照既有语义；配对只计 mapped=0，不报错）。

配对（走 ``upstream_aggregation`` 聚合配对）：
- CMS 条目转 ``UpstreamCatalogRecord``：``title=vod_name``、
  ``year=vod_year`` 转 int 或 None、``provider_ids`` 为空；经
  ``aggregate_sources`` 聚合后按聚合键与本地候选的标题+年份键比对。
- 配对成功：证据固定 ``'title+year'``、置信度封顶 0.7（CMS 无外部
  身份，0.7 即上限）；剧集按本地组成员顺序每集一行，
  ``episode_index`` 组内 1 起顺序编号（不从集标题文字解析）；
  电影单条 ``episode_index=None``。年份缺失的条目不配对（不只按
  标题猜），直接走未匹配路径。
- 本地组是否剧集：成员数 > 1，或任一成员 ``item_type`` 为
  Episode/Series，或任一成员带 ``episode_number``，即按剧集枚举；
  否则按电影单条。

健康（单轮裁决；只写 ``upstream_sources`` 允许的三值
``unknown`` / ``healthy`` / ``unhealthy``，不自造第四种状态）：
- 任一 list/detail 成功 → ``healthy``。
- 无成功的一轮 → ``unhealthy``（手动触发先行口径：单轮即裁决，
  下一轮成功即翻回 healthy；定时调度落地后再议跨轮防抖）。
- 两闸关 / 无启用绑定时不写健康（结果 health 为 disabled /
  unknown，仅内存态，不落库）。
- 无启用绑定的源：无取数、无失败，不写健康，``health="unknown"``。

单次运行有界（模块常量，默认不许无界）：
- ``MAX_PAGES_PER_CATEGORY = 200``：每分类最多拉页数。
- ``MAX_ENTRIES_PER_RUN = 5000``：单次运行最多扫描条目数。
- ``MAX_CATEGORIES_PER_RUN = 100``：单次运行最多同步分类数。
- ``MAX_429_RETRIES = 3``：429 首次失败后最多重试 3 次（共 4 次
  尝试）；耗尽后记 error 并停该源，绝不硬刷。
- ``MAX_ERRORS = 100``：结果 errors 列表上限，超限截断。

本模块不发 HTTP、不碰数据库、不读 ``worker.py``；手动触发函数
``trigger_applecms_sync`` 与工人门函数 ``start_applecms_sync_worker``
只做门与组装，实际调度循环归协调员的 worker 接线。
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from backend.emby_server import upstream_aggregation as agg

logger = logging.getLogger(__name__)

#: 两闸 env 键（必须同时为 "1"）。
APPLECMS_ENABLED_ENV = "APPLECMS_ENABLED"
UPSTREAM_SYNC_ENABLED_ENV = "UPSTREAM_SYNC_ENABLED"
#: 未匹配模式 env 键。
UNMATCHED_MODE_ENV = "APPLECMS_UNMATCHED_MODE"
#: 页间等待 env 键。
PAGE_DELAY_ENV = "APPLECMS_SYNC_PAGE_DELAY_SEC"
#: 映射写路径既有开关（照 upstream_item_mappings 的语义镜像判断，
#: 本模块不 import 那个重模块，保持本模块轻量可单测）。
MAPPING_ENABLED_ENV = "UPSTREAM_ITEM_MAPPING_ENABLED"

#: 配对证据固定值；置信度上限（CMS 无外部身份，0.7 即封顶）。
MATCH_EVIDENCE = "title+year"
MATCH_CONFIDENCE_CAP = 0.7

#: 单次运行上限（见模块头）。
MAX_PAGES_PER_CATEGORY = 200
MAX_ENTRIES_PER_RUN = 5000
MAX_CATEGORIES_PER_RUN = 100
MAX_ERRORS = 100

#: 429 重试：首次失败后最多重试次数；退避基数秒（指数 + 抖动）。
MAX_429_RETRIES = 3
RETRY_BASE_SEC = 1.0
RETRY_JITTER_SEC = 0.5

#: 未匹配模式允许值（list 与 parallel 同义）。
UNMATCHED_MODES = ("parallel", "list", "hide")

# ---------------------------------------------------------------------------
# env 读取
# ---------------------------------------------------------------------------


def _env_is_one(name: str) -> bool:
    """两闸口径：值剥离空白后严格等于 "1" 才算开。"""
    return (os.getenv(name, "0") or "").strip() == "1"


def gates_enabled() -> bool:
    """两闸是否同时满足（APPLECMS_ENABLED 与 UPSTREAM_SYNC_ENABLED）。"""
    return _env_is_one(APPLECMS_ENABLED_ENV) and _env_is_one(
        UPSTREAM_SYNC_ENABLED_ENV
    )


def _mapping_enabled() -> bool:
    """映射写路径开关：镜像 upstream_item_mappings.mapping_enabled 的
    语义（0/false/no/off/空 = 关，其余 = 开）。"""
    return (str(os.getenv(MAPPING_ENABLED_ENV, "0") or "0").strip().lower()
            not in {"0", "false", "no", "off", ""})


def _unmatched_mode(explicit: Optional[str] = None) -> str:
    mode = (explicit if explicit is not None
            else os.getenv(UNMATCHED_MODE_ENV, "parallel"))
    mode = (mode or "parallel").strip().lower()
    return mode if mode in UNMATCHED_MODES else "parallel"


def _page_delay_sec(explicit: Optional[float] = None) -> float:
    if explicit is not None:
        try:
            return max(0.0, float(explicit))
        except (TypeError, ValueError):
            return 0.5
    try:
        return max(0.0, float(os.getenv(PAGE_DELAY_ENV, "0.5") or "0.5"))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# 结果与鸭子类型取值
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """一次同步的结果（进度与健康载体，不含任何播放地址）。

    ``unmatched`` 是未匹配作品的聚合键列表（parallel/list 模式）；
    ``hidden`` 是 hide 模式下被排除的未匹配作品数。
    """

    source_id: str
    scanned: int = 0
    mapped: int = 0
    unmatched: List[str] = field(default_factory=list)
    hidden: int = 0
    errors: List[str] = field(default_factory=list)
    health: str = "unknown"


def _get(item: Any, *names: str) -> Any:
    """鸭子类型取值：先查字典键，再查对象属性，缺失返回 None。"""
    if isinstance(item, dict):
        for name in names:
            if item.get(name) is not None:
                return item.get(name)
    else:
        for name in names:
            value = getattr(item, name, None)
            if value is not None:
                return value
    return None


def _match_confidence() -> float:
    """标题+年份配对的置信度：唯一证据，原始分 0.7 即等于封顶值；
    min 保证未来证据加权改动时仍不超 ``MATCH_CONFIDENCE_CAP``。"""
    return min(0.7, MATCH_CONFIDENCE_CAP)


def _source_key(source: Any) -> str:
    return str(_get(source, "id", "source_id") or "")


def _binding_enabled(binding: Any) -> bool:
    value = _get(binding, "sync_enabled", "enabled")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _binding_category(binding: Any) -> Any:
    return _get(binding, "t", "category_id", "upstream_category_id", "category")


def _cms_year(value: Any) -> Optional[int]:
    """vod_year 转 int 或 None（字符串取首个四位年份，非法值不猜）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1800 <= value <= 2200 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = int(text)
            return number if 1800 <= number <= 2200 else None
        except ValueError:
            pass
        import re as _re
        match = _re.search(r"\d{4}", text)
        if match:
            number = int(match.group(0))
            return number if 1800 <= number <= 2200 else None
    return None


def cms_entry_to_record(
    entry: dict, *, source_id: str, category_id: str = ""
) -> agg.UpstreamCatalogRecord:
    """CMS 条目转聚合记录。

    只读 ``vod_id`` / ``vod_name`` / ``vod_year``。``vod_play_url``
   （播放地址）与 ``vod_douban_id``（绝不用于配对）刻意不读。
    """
    if not isinstance(entry, dict):
        raise ValueError("cms entry must be a dict")
    vod_id = str(entry.get("vod_id") or entry.get("id") or "").strip()
    if not vod_id:
        raise ValueError("cms entry missing vod_id")
    name = str(entry.get("vod_name") or entry.get("name") or "").strip()
    if not name:
        raise ValueError("cms entry missing vod_name")
    return agg.UpstreamCatalogRecord(
        source_id=source_id,
        upstream_item_id=vod_id,
        name=name,
        item_type="Movie",
        category_id=str(category_id or ""),
        production_year=_cms_year(entry.get("vod_year")),
        tmdb_id=None,
        imdb_id=None,
    )


def _local_work_key(local: Any) -> Optional[str]:
    """本地候选的标题+年份键（年份缺失返回 None：不只按标题配对）。

    本地成员的 item_type 强制按 Movie 换算，抹掉季集后缀，使同一
    剧集的各集共享同一作品键；组内顺序另由 episode_number 排。
    """
    title = _get(local, "name", "title", "series_name")
    year = _cms_year(_get(local, "production_year", "year", "vod_year"))
    if not title or year is None:
        return None
    probe = agg.UpstreamCatalogRecord(
        source_id="",
        upstream_item_id="",
        name=str(title),
        item_type="Movie",
        production_year=year,
    )
    return agg.aggregate_key_for(probe)


def _local_id(local: Any) -> Optional[Any]:
    return _get(local, "id", "local_item_id")


def _is_series_group(group: List[Any]) -> bool:
    if len(group) > 1:
        return True
    for member in group:
        item_type = str(_get(member, "item_type") or "")
        if item_type in ("Episode", "Series"):
            return True
        if _get(member, "episode_number", "episode_index") is not None:
            return True
    return False


def _sort_group(group: List[Any]) -> List[Any]:
    """组内排序：有集号的按集号升序，无集号的排后、再按 id 排。

    只决定枚举顺序，不从集标题文字解析任何编号。
    """

    def _sort_key(member: Any) -> Any:
        number = _get(member, "episode_number", "episode_index")
        try:
            number = int(number) if number is not None else None
        except (TypeError, ValueError):
            number = None
        member_id = _local_id(member)
        return (0, number, str(member_id)) if number is not None else (
            1, 0, str(member_id))

    return sorted(group, key=_sort_key)


# ---------------------------------------------------------------------------
# 429 退避
# ---------------------------------------------------------------------------


class RateLimitedError(Exception):
    """抓取层抛出的 429 信号；``retry_after`` 为上游给的等待秒数。"""

    def __init__(self, message: str = "rate limited", retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class _RateLimitExhausted(Exception):
    """429 重试耗尽：停该源的内部信号，不向外扩散。"""


def _is_rate_limited(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitedError):
        return True
    for attr in ("status", "status_code"):
        try:
            if getattr(exc, attr, None) == 429:
                return True
        except Exception:
            continue
    return False


def _retry_after(exc: BaseException) -> Optional[float]:
    value = getattr(exc, "retry_after", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _call_with_retry(
    fn: Callable[..., Any], sleep: Callable[[float], None], *args: Any,
    **kwargs: Any,
) -> Any:
    """调 fn；429 时指数 + 抖动退避重试，最多 ``MAX_429_RETRIES`` 次。

    耗尽后抛 ``_RateLimitExhausted``（调用方记 error 并停该源）；
    非 429 异常立即原样抛出。
    """
    retries = 0
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 需区分 429 与其它异常
            if not _is_rate_limited(exc):
                raise
            if retries >= MAX_429_RETRIES:
                raise _RateLimitExhausted(str(exc)) from exc
            delay = RETRY_BASE_SEC * (2 ** retries) + random.uniform(
                0, RETRY_JITTER_SEC)
            hinted = _retry_after(exc)
            if hinted is not None:
                delay = max(delay, hinted)
            sleep(delay)
            retries += 1


# ---------------------------------------------------------------------------
# 同步主流程
# ---------------------------------------------------------------------------


def _append_error(result: SyncResult, message: str) -> None:
    if len(result.errors) < MAX_ERRORS:
        result.errors.append(message)


def _fetch_page(
    fetch_page: Callable[..., Any], sleep: Callable[[float], None],
    source: Any, category: Any, page: int, h: Any,
) -> Any:
    """调注入的 fetch_page；对不接受 h 参数的老签名做一次兼容回落。"""
    try:
        return _call_with_retry(fetch_page, sleep, source, category, page, h=h)
    except TypeError as exc:
        if "h" in str(exc) or "unexpected keyword" in str(exc):
            return _call_with_retry(fetch_page, sleep, source, category, page)
        raise


def _page_entries(page_data: Any) -> List[dict]:
    if not isinstance(page_data, dict):
        return []
    entries = page_data.get("list")
    return [entry for entry in entries if isinstance(entry, dict)] \
        if isinstance(entries, list) else []


def _page_count(page_data: Any) -> Optional[int]:
    if not isinstance(page_data, dict):
        return None
    for key in ("pagecount", "page_count"):
        value = page_data.get(key)
        try:
            number = int(value) if value is not None else None
        except (TypeError, ValueError):
            number = None
        if number is not None and number > 0:
            return number
    try:
        total = int(page_data.get("total")) if page_data.get("total") is not None else None
        limit = int(page_data.get("limit")) if page_data.get("limit") is not None else None
    except (TypeError, ValueError):
        return None
    if total is not None and limit:
        return max(1, math.ceil(total / limit))
    return None


def sync_source(
    source: Any,
    *,
    fetch_page: Optional[Callable[..., Any]] = None,
    fetch_detail: Optional[Callable[..., Any]] = None,
    list_bindings: Optional[Callable[[Any], Iterable[Any]]] = None,
    list_locals: Optional[Callable[[Any], Iterable[Any]]] = None,
    record_map: Optional[Callable[..., Any]] = None,
    set_health: Optional[Callable[[Any, str], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    h: Any = None,
    unmatched_mode: Optional[str] = None,
    page_delay_sec: Optional[float] = None,
) -> SyncResult:
    """同步一个苹果 CMS 源（只写映射与健康进度）。

    两闸不满足时无副作用返回 ``health="disabled"``（此时连
    ``fetch_page`` 都不调）。两闸满足但 ``fetch_page`` /
    ``record_map`` 未注入时抛 ``ValueError``（编程错误响亮，不静默
    空跑）；``set_health`` 未注入时只跳过健康写入，结果 health 仍算。
    """
    source_id = _source_key(source)
    result = SyncResult(source_id=source_id)
    if not gates_enabled():
        result.health = "disabled"
        return result
    if fetch_page is None:
        raise ValueError("sync_source 需要注入 fetch_page")
    if record_map is None:
        raise ValueError("sync_source 需要注入 record_map")
    do_sleep = sleep if sleep is not None else time.sleep
    mode = _unmatched_mode(unmatched_mode)
    if unmatched_mode is None and (os.getenv(UNMATCHED_MODE_ENV, "parallel")
                                   or "").strip().lower() not in UNMATCHED_MODES:
        _append_error(result, "APPLECMS_UNMATCHED_MODE 非法，已回落 parallel")
    delay = _page_delay_sec(page_delay_sec)
    mapping_on = _mapping_enabled()

    bindings: List[Any] = []
    if list_bindings is not None:
        try:
            bindings = list(list_bindings(source) or [])
        except Exception as exc:  # noqa: BLE001 - 绑定列出失败记错不停机外
            _append_error(result, f"list_bindings 失败: {exc}")
            bindings = []
    enabled_bindings = [b for b in bindings if _binding_enabled(b)]
    enabled_bindings = enabled_bindings[:MAX_CATEGORIES_PER_RUN]
    if not enabled_bindings:
        result.health = "unknown"
        return result

    any_success = False
    stopped = False
    for binding in enabled_bindings:
        if stopped or result.scanned >= MAX_ENTRIES_PER_RUN:
            break
        category = _binding_category(binding)
        category_label = str(category or "")
        # 本地候选：按作品键分组（年份缺失的本地条目不进配对池）。
        local_by_key: Dict[str, List[Any]] = {}
        if list_locals is not None:
            try:
                for local in list_locals(binding) or []:
                    key = _local_work_key(local)
                    if key is None or _local_id(local) is None:
                        continue
                    local_by_key.setdefault(key, []).append(local)
            except Exception as exc:  # noqa: BLE001 - 本地列出失败则该类全走未匹配
                _append_error(result, f"分类 {category_label} list_locals 失败: {exc}")
                local_by_key = {}
        page = 1
        pages_fetched = 0
        while True:
            if stopped or result.scanned >= MAX_ENTRIES_PER_RUN:
                break
            if pages_fetched >= MAX_PAGES_PER_CATEGORY:
                _append_error(
                    result,
                    f"分类 {category_label} 达单类页上限 "
                    f"{MAX_PAGES_PER_CATEGORY}，剩余页未拉",
                )
                break
            try:
                page_data = _fetch_page(
                    fetch_page, do_sleep, source, category, page, h)
            except _RateLimitExhausted as exc:
                _append_error(
                    result, f"分类 {category_label} 第 {page} 页 429 重试耗尽，停该源: {exc}")
                stopped = True
                break
            except Exception as exc:  # noqa: BLE001 - 单类取页失败隔离到该类
                _append_error(result, f"分类 {category_label} 第 {page} 页失败: {exc}")
                break
            any_success = True
            pages_fetched += 1
            entries = _page_entries(page_data)
            total_pages = _page_count(page_data)
            records: List[agg.UpstreamCatalogRecord] = []
            for entry in entries:
                if result.scanned >= MAX_ENTRIES_PER_RUN:
                    break
                result.scanned += 1
                vod_id = str(entry.get("vod_id") or entry.get("id") or "").strip()
                if (not str(entry.get("vod_name") or "").strip()
                        or _cms_year(entry.get("vod_year")) is None) \
                        and fetch_detail is not None and vod_id:
                    # 仅缺名/缺年时补一次详情；详情失败不致命。
                    try:
                        detail = _call_with_retry(
                            fetch_detail, do_sleep, source, vod_id)
                        if isinstance(detail, dict):
                            detail_entry = detail.get("list") or detail
                            if isinstance(detail_entry, list) and detail_entry:
                                detail_entry = detail_entry[0]
                            if isinstance(detail_entry, dict):
                                entry = {**detail_entry, **{
                                    k: v for k, v in entry.items() if v not in (None, "")}}
                    except _RateLimitExhausted as exc:
                        _append_error(
                            result, f"vod {vod_id} 详情 429 重试耗尽，停该源: {exc}")
                        stopped = True
                        break
                    except Exception as exc:  # noqa: BLE001 - 详情失败走原条目
                        _append_error(result, f"vod {vod_id} 详情失败: {exc}")
                try:
                    records.append(cms_entry_to_record(
                        entry, source_id=source_id, category_id=category_label))
                except ValueError as exc:
                    _append_error(result, f"分类 {category_label} 跳过脏条目: {exc}")
            if stopped:
                break
            if records:
                aggregated = agg.aggregate_sources({source_id: records})
                items = aggregated[0].items if aggregated else []
                if aggregated and aggregated[0].error:
                    _append_error(
                        result,
                        f"分类 {category_label} 聚合失败: {aggregated[0].error}")
                for item in items:
                    work_key = item.aggregate_key
                    group = local_by_key.get(work_key)
                    if not group:
                        # 年份缺失的 CMS 聚合（键尾年份为空）同样落此分支：
                        # 不只按标题配对，直接走未匹配路径。
                        if mode == "hide":
                            result.hidden += 1
                        else:
                            result.unmatched.append(work_key)
                        continue
                    if not mapping_on:
                        continue
                    ordered = _sort_group(group)
                    series = _is_series_group(ordered)
                    for line in item.lines:
                        for index, local in enumerate(ordered):
                            record_map(
                                local_item_id=_local_id(local),
                                source_id=_get(source, "id", "source_id"),
                                upstream_item_id=line.upstream_item_id,
                                confidence=_match_confidence(),
                                evidence=MATCH_EVIDENCE,
                                episode_index=(index + 1) if series else None,
                            )
                            result.mapped += 1
            if not entries:
                break
            if total_pages is not None and page >= total_pages:
                break
            if result.scanned < MAX_ENTRIES_PER_RUN and not stopped:
                do_sleep(delay)
            page += 1

    # 单轮裁决：只写允许的三值之一（healthy/unhealthy）；两闸关与
    # 无启用绑定的 early return 在前，不走到这里写健康。
    result.health = "healthy" if any_success else "unhealthy"
    if set_health is not None:
        try:
            set_health(source_id, result.health)
        except Exception:  # noqa: BLE001 - 健康写入失败不毁同步结果
            logger.exception("applecms set_health 失败 source=%s", source_id)
    return result


# ---------------------------------------------------------------------------
# 手动触发与工人门（本批不新增路由、不碰 worker.py）
# ---------------------------------------------------------------------------


def trigger_applecms_sync(
    source_id: Any,
    *,
    db: Any = None,
    get_source: Optional[Callable[[Any], Any]] = None,
    **sync_kwargs: Any,
) -> SyncResult:
    """手动触发：从 DB 取源并调 :func:`sync_source`（供未来管理路由调用）。

    ``get_source(source_id)`` 未注入时，若给了 ``db`` 则惰性调
    ``upstream_sources.get_source(db, source_id)``；两者皆无时抛
    ``ValueError``。源不存在时返回带 error 的结果，不写健康。
    """
    if not gates_enabled():
        return SyncResult(source_id=str(source_id), health="disabled")
    if get_source is None:
        if db is None:
            raise ValueError("trigger_applecms_sync 需要 get_source 或 db")
        from backend.emby_server import upstream_sources as ups
        row = ups.get_source(db, source_id)
    else:
        row = get_source(source_id)
    if row is None:
        result = SyncResult(source_id=str(source_id), health="unknown")
        _append_error(result, f"源 {source_id} 不存在")
        return result
    return sync_source(row, **sync_kwargs)


def start_applecms_sync_worker(
    *,
    list_sources: Optional[Callable[[], Iterable[Any]]] = None,
    **sync_kwargs: Any,
) -> bool:
    """工人门：两闸不满足返回 False（worker.py 接线由协调员组装）。

    两闸满足时返回 True；若注入了 ``list_sources``，则对每个源各跑
    一次 ``sync_source``（单源异常隔离到该源，不中断整轮）。
    """
    if not gates_enabled():
        return False
    if list_sources is None:
        return True
    for source in list_sources() or []:
        try:
            sync_source(source, **sync_kwargs)
        except Exception:  # noqa: BLE001 - 单源异常不中断整轮
            logger.exception("applecms worker 单源同步失败")
    return True

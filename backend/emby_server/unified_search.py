"""跨服统一搜索：一个入口搜本站库 + 所有已启用上游源（第二批第 1 单）

定位（#25/#1352 聚合的延伸，只做搜索、不做聚合）：
- 复用现有口径，不新造索引：本站侧复用 ``search_api`` 的预筛
  （``search.search_variants``）与精排（``search.rank_items``）；
  上游侧复用 ``upstream_aggregation`` 的作品归一键
  （``aggregate_key_for``：tmdb > imdb > 标题+年份，单集追加季集号）
  与多源失败隔离形态（``aggregate_sources``：单源失败只隔离到该源）。
- 只读：本模块不触发聚合（绝不调 ``run_manual_aggregation``）、
  不写库（绝不调 ``record_mapping``、不写任何表）。上游现查是
  只读的 Emby ``/Items?SearchTerm=`` 查询，不是聚合跑批；查到
  的记录只进本次响应，不落库、不建映射。
- 开关：``UNIFIED_SEARCH_ENABLED``（默认关）。关时调用方接口
  返回 404，全站行为逐字不变；本模块的 ``unified_search_enabled``
  每次调用现读 env，供测试用 ``monkeypatch.setenv`` 覆盖。
- 搜不到不谎报：空词不返回全库（返回空结果）；某源不可用时
  该源在 ``sources`` 里明确标 ``unavailable`` 并附原因码，不静默
  丢弃该源、不把半份结果说成全量。
- 上游前置条件不变：对方未放行本方出口 IP 的源，现查会被对方
  前门拦（403 无 JSON 体 → ``upstream_frontend_blocked``），搜不
  到是预期，见调用方未知项，绝不编造连通性。
- 凭据纪律：上游播放地址、对方令牌、密封凭据一律不进结果与
  错误体；错误体只用固定原因码 + 固定文案，绝不拼接上游原始体
  或异常文本（异常文本可能含带 api_key 的 URL）。

排序与去重口径（写死在本模块，调用方不另定）：
- 本地在前：本站命中按 ``rank_items`` 的顺序全在前，上游绝不
  插队到本地前面（与 ``select_for_presentation`` 的本地优先同口径）。
- 上游在后：上游命中按 ``aggregate_key`` 排序（与 ``build_aggregates``
  的输出顺序同口径），与上游返回顺序抖动无关。
- 去重：同一 ``aggregate_key`` 只出一行；首现来源为主来源，
  其余同键来源记进 ``also_on_sources``（不静默丢源信息）。
  本地已有的作品，上游同键只追加到该本地行的 ``also_on_sources``。
- 按源过滤：``source_filter`` 为源 id 集合（接受 ``"1"`` 与
  ``"upstream:1"`` 两种写法）；过滤只影响输出行与 ``also_on_sources``，
  被滤掉的源在 ``sources`` 里标 ``not_requested``，不冒充已查。
- 上游标题匹配的已知局限：上游记录只有标题/剧名，没有别名与
  拼音派生字段，故上游匹配只做归一化后的标题/剧名子串
  （``search.normalize_compact`` + ``search_variants`` 的繁简变体），
  不做拼音与别名匹配；本站侧仍走完整精排口径。
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from backend.emby_server import upstream_aggregation as agg
from backend.emby_server.search import normalize_compact, search_variants

#: 统一搜索总开关 env 键（默认关）。
UNIFIED_SEARCH_ENV = "UNIFIED_SEARCH_ENABLED"

#: 上游现查单次返回上限（Emby Items Limit）：搜索不是拉全目录，
#: 只取首屏候选，超了不翻页、不谎报全量（TotalRecordCount 照实回）。
UPSTREAM_SEARCH_LIMIT = 100


def unified_search_enabled() -> bool:
    """总开关是否开启（每次调用现读 env，默认关）。"""
    return (str(os.getenv(UNIFIED_SEARCH_ENV, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def normalize_source_filter(raw: Optional[Iterable[str]]) -> Optional[Set[str]]:
    """把源过滤参数归一为源 id 字符串集合。

    接受 ``"1"`` 与 ``"upstream:1"`` 两种写法；空集合 / None = 不过滤。
    """
    if raw is None:
        return None
    cleaned = set()
    for token in raw:
        token = (str(token) or "").strip()
        if token.startswith("upstream:"):
            token = token[len("upstream:"):]
        if token:
            cleaned.add(token)
    return cleaned or None


def _source_sort_key(source_id: str) -> Tuple[int, object]:
    """源 id 确定性排序键：纯数字按数值排，其余按字符串排。"""
    if source_id.isdigit():
        return (0, int(source_id))
    return (1, source_id)


def local_aggregate_key(*, name: str, item_type: str,
                        production_year: Optional[int],
                        scan_year: Optional[int],
                        tmdb_id: Optional[str], imdb_id: Optional[str],
                        season_number: Optional[int],
                        episode_number: Optional[int]) -> str:
    """本站条目的归一键（调用方唯一入口，纯函数）。

    年份口径须与上游侧一致：上游记录的 production_year 来自对方
    ProductionYear，与本站 production_year 对得上；scan_year 只是扫描期
    文件名年份，仅在 production_year 为空时回落。用 scan_year 优先算键
    会让无外部身份的同一作品（标题+年份键）在两边算出不同的键，
    去重 miss、本地行与上游行重复出现。
    """
    probe = agg.UpstreamCatalogRecord(
        source_id="", upstream_item_id="",
        name=name, item_type=item_type,
        production_year=(production_year if production_year is not None
                         else scan_year),
        tmdb_id=tmdb_id, imdb_id=imdb_id, series_name="",
        season_number=season_number, episode_number=episode_number)
    return agg.aggregate_key_for(probe)


# ---------------------------------------------------------------------------
# 输入 / 输出模型（纯数据，不碰数据库）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalSearchEntry:
    """本站侧已精排的一行（调用方按 rank_items 顺序传入）。"""

    aggregate_key: str
    name: str
    item_type: str = ""
    production_year: Optional[int] = None


@dataclass(frozen=True)
class UnifiedSearchResult:
    """合并后的一行：主来源 + 同键的其余来源。"""

    aggregate_key: str
    name: str
    item_type: str
    production_year: Optional[int]
    origin: str  # "local" 或 "upstream:<source_id>"
    source: str  # "local" 或源 id
    also_on_sources: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceStatus:
    """单个源的本次搜索状态：ok / unavailable / not_requested。"""

    source_id: str
    status: str
    count: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class UnifiedSearchResponse:
    """合并结果（调用方直接序列化）。"""

    query: str
    results: Tuple[UnifiedSearchResult, ...]
    sources: Tuple[SourceStatus, ...]

    @property
    def total(self) -> int:
        return len(self.results)


# ---------------------------------------------------------------------------
# 上游标题匹配（复用搜索归一化，不新造索引）
# ---------------------------------------------------------------------------


def matches_upstream_term(record: agg.UpstreamCatalogRecord, term: str) -> bool:
    """上游记录是否命中查询词：归一化标题/剧名子串。

    用 ``search_variants`` 的繁简变体逐个比对紧凑串；空词恒为
    False（空词不 dump 全库）。拼音与别名因上游记录无此字段而
    不参与匹配（见模块头已知局限）。
    """
    term = (term or "").strip()
    if not term:
        return False
    haystacks = [
        normalize_compact(record.name),
        normalize_compact(record.series_name or ""),
    ]
    haystacks = [text for text in haystacks if text]
    if not haystacks:
        return False
    for variant in search_variants(term):
        needle = normalize_compact(variant)
        if not needle:
            continue
        if any(needle in hay for hay in haystacks):
            return True
    return False


# ---------------------------------------------------------------------------
# 合并：本地在前、上游在后、同键去重、单源失败隔离
# ---------------------------------------------------------------------------


def merge_search_results(
    local_entries: Iterable[LocalSearchEntry],
    per_source: Dict[str, object],
    term: str,
    *,
    source_filter: Optional[Set[str]] = None,
) -> UnifiedSearchResponse:
    """合并本站与多源上游搜索结果（纯函数，不写库、不发 HTTP）。

    ``per_source`` 的值约定（与 ``aggregate_sources`` 同款）：
    成功时为该源的目录记录列表（``UpstreamCatalogRecord``），
    失败时为 ``Exception`` 实例或错误字符串；未知形态按失败计，
    不抛异常。空词返回空结果（不 dump 全库）。
    """
    query = (term or "").strip()
    wanted = normalize_source_filter(source_filter)
    if not query:
        statuses = tuple(
            SourceStatus(
                source_id=source_id,
                status=("not_requested"
                        if wanted is not None and source_id not in wanted
                        else "unavailable"),
                error=("not_requested" if wanted is not None
                        and source_id not in wanted else "empty_query"),
            )
            for source_id in sorted(per_source, key=_source_sort_key)
        )
        return UnifiedSearchResponse(query=query, results=(), sources=statuses)

    def _requested(source_id: str) -> bool:
        return wanted is None or source_id in wanted

    results: List[UnifiedSearchResult] = []
    by_key: Dict[str, UnifiedSearchResult] = {}
    # 本地在前：按调用方传入的 rank_items 顺序，本地行不重排。
    for entry in local_entries:
        if entry.aggregate_key in by_key:
            continue
        row = UnifiedSearchResult(
            aggregate_key=entry.aggregate_key,
            name=entry.name,
            item_type=(entry.item_type or "").lower(),
            production_year=entry.production_year,
            origin="local",
            source="local",
            also_on_sources=(),
        )
        by_key[entry.aggregate_key] = row
        results.append(row)

    # 上游：先按源内去重、按查询词过滤，再跨源归一。
    upstream_by_key: Dict[str, dict] = {}
    statuses: List[SourceStatus] = []
    for source_id in sorted(per_source, key=_source_sort_key):
        value = per_source[source_id]
        if not _requested(source_id):
            statuses.append(SourceStatus(source_id=source_id,
                                         status="not_requested"))
            continue
        if isinstance(value, Exception):
            # 异常原文绝不进 error 体：它可能含带 api_key 的上游 URL
            # （模块头凭据纪律）；统一记固定原因码，不拼接异常文本。
            statuses.append(SourceStatus(source_id=source_id,
                                         status="unavailable",
                                         error="upstream_unavailable"))
            continue
        if isinstance(value, str):
            statuses.append(SourceStatus(source_id=source_id,
                                         status="unavailable",
                                         error=value))
            continue
        if not (isinstance(value, list) and all(
                isinstance(record, agg.UpstreamCatalogRecord)
                for record in value)):
            statuses.append(SourceStatus(
                source_id=source_id, status="unavailable",
                error="unexpected per-source value type"))
            continue
        seen_in_source: Set[str] = set()
        matched = 0
        for record in value:
            if not matches_upstream_term(record, query):
                continue
            key = agg.aggregate_key_for(record)
            if key in seen_in_source:
                continue
            seen_in_source.add(key)
            matched += 1
            slot = upstream_by_key.get(key)
            if slot is None:
                upstream_by_key[key] = {
                    "name": record.series_name or record.name,
                    "item_type": (record.item_type or "").lower(),
                    "production_year": record.production_year,
                    "sources": [source_id],
                }
            else:
                slot["sources"].append(source_id)
        statuses.append(SourceStatus(source_id=source_id, status="ok",
                                     count=matched))

    # 上游在后：按 aggregate_key 排序，与 build_aggregates 同口径。
    for key in sorted(upstream_by_key):
        slot = upstream_by_key[key]
        sources = slot["sources"]
        primary = sources[0]
        existing = by_key.get(key)
        if existing is not None:
            # 本地已有：上游来源只追加到 also_on_sources，不另起一行。
            merged_also = tuple(
                dict.fromkeys(list(existing.also_on_sources) + sources))
            merged_row = UnifiedSearchResult(
                aggregate_key=existing.aggregate_key,
                name=existing.name,
                item_type=existing.item_type,
                production_year=existing.production_year,
                origin=existing.origin,
                source=existing.source,
                also_on_sources=merged_also,
            )
            results[results.index(existing)] = merged_row
            by_key[key] = merged_row
            continue
        row = UnifiedSearchResult(
            aggregate_key=key,
            name=slot["name"],
            item_type=slot["item_type"],
            production_year=slot["production_year"],
            origin=f"upstream:{primary}",
            source=primary,
            also_on_sources=tuple(sources[1:]),
        )
        by_key[key] = row
        results.append(row)

    return UnifiedSearchResponse(query=query, results=tuple(results),
                                 sources=tuple(statuses))


def to_response_body(response: UnifiedSearchResponse,
                     *, limit: Optional[int] = None) -> dict:
    """把合并结果序列化为接口响应体（截断只截尾，不重排）。"""
    rows = list(response.results)
    if limit is not None:
        rows = rows[:max(0, limit)]
    return {
        "query": response.query,
        "results": [
            {
                "aggregate_key": row.aggregate_key,
                "name": row.name,
                "item_type": row.item_type,
                "production_year": row.production_year,
                "origin": row.origin,
                "source": row.source,
                "also_on_sources": list(row.also_on_sources),
            }
            for row in rows
        ],
        "sources": [
            {
                "source_id": status.source_id,
                "status": status.status,
                "count": status.count,
                "error": status.error,
            }
            for status in response.sources
        ],
        "total": len(rows),
    }


# ---------------------------------------------------------------------------
# 上游现查（只读 Emby /Items?SearchTerm=，不是聚合跑批）
# ---------------------------------------------------------------------------


def search_upstream_source(source, term: str, *,
                           http_get=None, http_post=None):
    """对单个已启用 Emby/Jellyfin 型源做只读现查。

    返回 ``(records, error)``：成功时 ``error`` 为 None；失败时
    ``records`` 为空、``error`` 为固定原因码 + 固定文案（绝不含
    上游原始体与异常文本）。applecms 型源调用方不许走到这里
    （调用方直接记 unavailable，见 unified_search_api）。

    传输与凭据复用 ``upstream_aggregate_run``（``_resolve_api_key`` /
    ``_default_http_get`` / ``_source_headers``）：凭据只在容器内
    解密使用，绝不进返回值。本函数不写库、不触发聚合。
    """
    from backend.emby_server import upstream_aggregate_run as run

    term = (term or "").strip()
    if not term:
        return [], "empty_query"
    get = run._default_http_get if http_get is None else http_get
    try:
        api_key = run._resolve_api_key(source, http_post=http_post)
    except run.UpstreamAggregateRunError as exc:
        return [], f"{exc.code}: {exc.message}"
    except Exception:  # noqa: BLE001 — 异常文本可能含带 key 的 URL，绝不外泄
        return [], "upstream_unavailable: 上游搜索请求失败"
    base = (source.base_url or "").rstrip("/")
    headers = run._source_headers(source)
    params = {
        "SearchTerm": term,
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Series,Episode",
        "Fields": "ProviderIds",
        "StartIndex": "0",
        "Limit": str(UPSTREAM_SEARCH_LIMIT),
    }
    if api_key:
        params["api_key"] = api_key
    url = f"{base}/Items?{urllib.parse.urlencode(params)}"
    try:
        status, body = get(url, headers, run.HTTP_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 — 同上，不外泄异常文本
        return [], "upstream_unavailable: 上游搜索请求失败"
    if status == 200 and isinstance(body, dict):
        records, _total = agg.parse_emby_items_page(
            body, source_id=str(source.id))
        matched = [record for record in records
                   if matches_upstream_term(record, term)]
        return matched, None
    if status == 401:
        return [], "upstream_credential_invalid: 上游凭据失效，请管理员重录该源凭据"
    if status == 403:
        if isinstance(body, dict):
            return [], ("upstream_credential_invalid: "
                        "上游凭据失效，请管理员重录该源凭据")
        return [], ("upstream_frontend_blocked: "
                    "上游前门（nginx）拦截，未到 Emby："
                    "请对方放行本方出口 IP 15.204.95.101，重录凭据无效")
    if status == 429:
        return [], "upstream_quota_exhausted: 上游额度已耗尽，请稍后再试或换个源"
    return [], "upstream_unavailable: 上游服务当前不可用，请稍后再试"

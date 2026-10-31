"""上游聚合：Emby 型目录聚合读模型（FMBY 学习 #25，25-2）

只学机制，不抄闭源代码。FMBY 的上游聚合是闭源 Rust 实现，我方
看不见源码；本模块的形态只按研究报告第六阶段的**实测证据**搭，
证据不足处一律按最接近实测的方案做，并在模块注释与交付报告里
显式标出“待复验”，不冒充实测、不按我方旧口径悄悄定死。

实测证据（``fmby-study-20261023.md`` §6.4 与上游自连实测段）：
- ``upstream_sources``（``source_type, base_url, auth_method,
  sealed_secret, extra_headers_json, user_agent, referer``）：凭据
  密封存储，自连实测建源后 ``has_secret=true``。
- 发现库：``discover-libraries`` 成功发现 3 个库，落
  ``upstream_categories``；分类经 ``upstream_category_bindings`` +
  ``mapping_presets`` 模板化映射到本地库。
- 目录层：拉回的目录落 ``upstream_catalog_records``；聚合层是
  ``upstream_aggregate_items`` / ``variants`` / ``external_ids``；
  播放线路是 ``upstream_playback_variants``（``line_index,
  line_key, episode_index, play_url, protocol, availability_status,
  last_checked_at``），定期探活。
- 同一条目可挂多个播放变体（§三“多版本”段）。

按实测形态，本模块只产**上游侧聚合记录**，不写本地条目表：
- 混排还是独立分区：实测形态既不是“并进本地条目表”，也不是
  “单独的上游库分区”，而是**独立聚合层 + 分类映射到本地库**。
  因此聚合结果每条都带分区标记（``partition``：上游侧为
  ``upstream:<source_id>``），展示层以后既可按映射混排、也可按
  分区过滤——混排/分区之争不在本模块定，留给后续裁定。
- 版本与线路（待复验，见 :func:`build_aggregates`）：实测的变体
  字段是线路字段（``line_key``）。本模块定：一个上游条目（Emby
  Item）就是一条线路；同一 Item 下的多个 MediaSource 暂按版本
  挂在线路下。FMBY 实测没有直接讲 MediaSource 与线路的对应
  关系，这一步是推断，不是实测。
- 本地优先、上游兜底（用户口径，非 FMBY 实测）：FMBY 实测里
  上游播放根本没跑完，排序规则实测中没有。本模块的展示排序
  （本地在前、上游只补本地没有的作品）是用户已定口径，实测
  数据既不证实也不冲突——见 :func:`select_for_presentation`。
- 对账宽限（最接近实测的推断，待复验）：清单可信度守卫 +
  缺失宽限是在 FMBY 扫描域实测的（删文件整轮中止、缺失计数
  宽限），上游同步域没有实测到；本模块把同一纪律借用到上游
  目录对账，见 :func:`reconcile`。

本模块是纯读模型，不发 HTTP、不碰数据库、不写本地条目：
- 拉目录的传输（分页请求、SSRF 校验、凭据密封）归 25-1 的源
  管理与未来的同步工人；本模块只接受“已授权的取页回调”与
  “已拉回的原始 JSON”，凭据（api_key / token / secret）绝不
  进入任何输出记录——解析时只取身份与结构字段，原始字典
  不原样透传。
- 播放地址（``play_url``）不在本模块产出：它是 25-4 播放接入
  的事，且上游地址常带对方令牌，绝不许经本模块落库或下发。
- 线路可用性（``availability_status``）是 25-3 探活工人的事：
  本模块新线路初始状态一律 ``unknown``，绝不虚报 available。
- 总开关：调用方传 ``aggregation_enabled=False`` 时聚合结果
  为空、展示只剩本地，全站行为逐字不变；本模块自身不读 env，
  开关归属调用方（与 play_variants import 时读 env 的口径不
  同——这里是纯模型，故意不绑全局开关，免得测试与工人各读
  一份开关打架）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

#: 新线路的初始可用性：25-3 探活之前，一律 unknown，绝不虚报。
AVAILABILITY_UNKNOWN = "unknown"

#: 对账缺失宽限轮数默认值。注意：该数值借自 FMBY 扫描域的
#: 灵魂验收建议（连续空达宽限轮数默认 3 才下架），上游同步域
#: 本身没有实测到——是“最接近实测的推断”，待复验，调用方可
#: 覆盖；绝不许把它说成 FMBY 上游同步的实测值。
DEFAULT_MISSING_GRACE_ROUNDS = 3

#: 凭据类键名：解析时凡遇此名单一律丢弃，绝不进聚合记录。
_CREDENTIAL_KEYS = frozenset(
    {
        "apikey",
        "api_key",
        "api-key",
        "x-emby-token",
        "token",
        "access_token",
        "secret",
        "password",
    }
)


# ---------------------------------------------------------------------------
# 源接口约定（25-1 的源表还没合并，本模块不重建源表）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamSource:
    """聚合调用方须提供的源描述（替身源同款形状）。

    25-1 落地后应由其源表适配而来：类型 Emby/Jellyfin、地址、
    启用开关。本模块绝不持有凭据——``base_url`` 只用于调试日志
    的源标识，不参与任何输出记录的身份计算；凭据密封与传输
    安全归 25-1，SSRF 校验归未来的同步工人。
    """

    source_id: str
    source_type: str  # "emby" 或 "jellyfin"，首批只做这两型
    base_url: str
    enabled: bool = True


# ---------------------------------------------------------------------------
# 目录层：拉回的原始条目（对应 upstream_catalog_records）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpstreamVersion:
    """一条线路下的一个版本：仅来自同一 Emby Item 的 MediaSource。

    只存 Id + Name + RunTimeTicks，不存 Path（对方服务器本地
    路径，存了就是泄露对方布局，且本模块用不上）。
    """

    media_source_id: str
    name: str = ""
    run_time_ticks: Optional[int] = None


@dataclass(frozen=True)
class UpstreamCatalogRecord:
    """拉回的一条目录记录（Emby Items 的一项）。

    ``category_id`` 是上游侧的库/分类标识，对应实测的
    ``upstream_categories``；它到本地库的映射（binding）由
    :func:`build_aggregates` 的调用方以 ``category_bindings``
    传入，本模块不猜映射关系。
    """

    source_id: str
    upstream_item_id: str
    name: str
    item_type: str  # Movie / Series / Episode / Season 等，照 Emby 原值
    category_id: str = ""
    production_year: Optional[int] = None
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    series_name: str = ""
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    versions: Tuple[UpstreamVersion, ...] = ()


def _provider_id(provider_ids: object, *names: str) -> Optional[str]:
    if not isinstance(provider_ids, dict):
        return None
    for name in names:
        value = provider_ids.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def _as_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def parse_emby_item(
    raw: dict, *, source_id: str, category_id: str = ""
) -> UpstreamCatalogRecord:
    """把一个 Emby/Jellyfin Items 项解析为目录记录（纯函数）。

    只取身份与结构字段；凭据类键、原始字典、对方服务器路径
    一律不进记录。``ProviderIds`` 的 Tmdb/Imdb 是身份证据，
    不是凭据，保留。
    """
    if not isinstance(raw, dict):
        raise ValueError("emby item must be a dict")
    item_id = raw.get("Id")
    if not item_id:
        raise ValueError("emby item missing Id")
    media_sources = raw.get("MediaSources")
    versions: List[UpstreamVersion] = []
    if isinstance(media_sources, list):
        for media_source in media_sources:
            if not isinstance(media_source, dict):
                continue
            media_source_id = media_source.get("Id")
            if not media_source_id:
                continue
            versions.append(
                UpstreamVersion(
                    media_source_id=str(media_source_id),
                    name=str(media_source.get("Name") or ""),
                    run_time_ticks=_as_int(media_source.get("RunTimeTicks")),
                )
            )
    return UpstreamCatalogRecord(
        source_id=source_id,
        upstream_item_id=str(item_id),
        name=str(raw.get("Name") or ""),
        item_type=str(raw.get("Type") or ""),
        category_id=category_id,
        production_year=_as_int(raw.get("ProductionYear")),
        tmdb_id=_provider_id(raw.get("ProviderIds"), "Tmdb", "tmdb"),
        imdb_id=_provider_id(raw.get("ProviderIds"), "Imdb", "imdb"),
        series_name=str(raw.get("SeriesName") or ""),
        season_number=_as_int(raw.get("ParentIndexNumber")),
        episode_number=_as_int(raw.get("IndexNumber")),
        versions=tuple(versions),
    )


def parse_emby_items_page(
    page: dict, *, source_id: str, category_id: str = ""
) -> Tuple[List[UpstreamCatalogRecord], int]:
    """解析一页 Emby Items 响应，返回（记录，TotalRecordCount）。

    Items 不是列表时按空页处理（上游返回形态异常时不炸调用方，
    对账层按“取页失败/空页”各自的纪律处理，见 :func:`reconcile`）。
    """
    if not isinstance(page, dict):
        return [], 0
    items = page.get("Items")
    records: List[UpstreamCatalogRecord] = []
    if isinstance(items, list):
        for raw in items:
            try:
                records.append(
                    parse_emby_item(raw, source_id=source_id, category_id=category_id)
                )
            except ValueError:
                # 无 Id 的脏项跳过，不让一条脏数据毁掉整页。
                continue
    return records, _as_int(page.get("TotalRecordCount")) or 0


def iter_emby_pages(
    fetch_page: Callable[[int, int], dict],
    *,
    source_id: str,
    category_id: str = "",
    page_size: int,
) -> Iterable[UpstreamCatalogRecord]:
    """按 Emby StartIndex/Limit 分页拉取（传输由调用方注入）。

    调用方须保证 ``fetch_page`` 已授权、已过 SSRF 校验；本模块
    不发 HTTP。某页返回 0 条即停（防上游 TotalRecordCount 说谎
    导致死循环）；TotalRecordCount 为 0 时只拉首轮即停。
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    start = 0
    while True:
        page = fetch_page(start, page_size)
        records, total = parse_emby_items_page(
            page, source_id=source_id, category_id=category_id
        )
        yield from records
        if not records:
            break
        start += len(records)
        if total and start >= total:
            break


# ---------------------------------------------------------------------------
# 聚合层：作品 + 线路 + 版本（对应 upstream_aggregate_items/variants）
# ---------------------------------------------------------------------------


def _normalize_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title or "").strip().lower()
    text = re.sub(r"[\s\-_.,:;!?()\[\]{}'\"]+", "", text)
    return text


def aggregate_key_for(record: UpstreamCatalogRecord) -> str:
    """作品归一键：外部身份优先，缺身份时退化为标题+年份。

    优先级 tmdb > imdb > 标题+年份——与 #13 远端身份“证据先行”
    同序：有外部身份绝不靠标题猜。剧集单集在系列键后追加
    季/集号，同一系列的不同集绝不归成同一作品。
    """
    if record.tmdb_id:
        base = f"tmdb:{record.tmdb_id}"
    elif record.imdb_id:
        base = f"imdb:{record.imdb_id}"
    else:
        title = _normalize_title(record.series_name or record.name)
        year = record.production_year or ""
        base = f"title:{title}:{year}"
    if record.item_type == "Episode" or record.episode_number is not None:
        # 注意：单集即使有 tmdb/imdb，季集号仍须参与键——同一
        # 剧集的不同集靠它区分，同一集重复出现时靠它归一。
        base = f"{base}:s{record.season_number}:e{record.episode_number}"
    return base


@dataclass
class UpstreamLine:
    """一条播放线路：对应实测 upstream_playback_variants 的一行。

    ``line_key`` 是线路的稳定身份（源 + 上游条目 id），与 25-3
    探活的 ``availability_status`` / ``last_checked_at`` 同键；
    本模块只写初始 ``unknown``，探活结果绝不在此伪造。
    ``play_url`` 刻意不在此：上游地址常带对方令牌，25-4 之前
    任何环节都不许把它落进聚合记录。
    """

    line_index: int
    line_key: str
    source_id: str
    upstream_item_id: str
    episode_index: Optional[int] = None
    protocol: str = "emby"
    availability_status: str = AVAILABILITY_UNKNOWN
    last_checked_at: Optional[str] = None
    versions: Tuple[UpstreamVersion, ...] = ()


@dataclass
class UpstreamAggregateItem:
    """一个聚合作品（对应 upstream_aggregate_items 的一行）。

    ``partition`` 是展示层分区标记：上游侧为
    ``upstream:<主线路源>``（排序后首条线路的源，与输入顺序
    无关）；混排/分区之争由展示层用它过滤，本模块不把上游
    作品并进本地条目，也不替展示层决定混排。注意 partition
    只标主线路源，全部线路的源信息在 ``lines[]`` 里。
    ``category_binding`` 是调用方传入的分类映射结果（上游分类
    → 本地库的不透明标识），本模块不猜映射、不建映射表。
    """

    aggregate_key: str
    name: str
    item_type: str
    production_year: Optional[int] = None
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    partition: str = ""
    category_binding: str = ""
    # 单集的季号与所属系列名（O-08 系列级配对用）：聚合键里已含
    # 季集号，但配对层不该去解析键串，这里从记录显式带出；非单集
    # 记录这两项为空/None，不参与任何既有口径。
    season_number: Optional[int] = None
    series_name: str = ""
    lines: List[UpstreamLine] = field(default_factory=list)


def build_aggregates(
    records: Iterable[UpstreamCatalogRecord],
    *,
    category_bindings: Optional[Dict[str, str]] = None,
) -> List[UpstreamAggregateItem]:
    """把目录记录归一为聚合作品（纯函数，不写库）。

    归一规则（待复验点已在模块头声明）：
    - 同一作品键的记录归一为一个聚合作品；
    - 每个（源，上游条目）是一条线路（``line_key``），同一源的
      两个条目即使同作品也是两条线路——因为 Emby 里每个 Item
      都可独立走 PlaybackInfo，合并成版本会丢掉独立寻址；
      同一 Item 的多个 MediaSource 才是版本，挂在线路下。
    - 线路按（源 id，条目 id）排序，保证同一输入同一输出，
      不依赖上游返回顺序抖动。
    """
    bindings = category_bindings or {}
    by_key: Dict[str, UpstreamAggregateItem] = {}
    for record in records:
        key = aggregate_key_for(record)
        item = by_key.get(key)
        if item is None:
            item = UpstreamAggregateItem(
                aggregate_key=key,
                name=record.series_name or record.name,
                item_type=record.item_type,
                production_year=record.production_year,
                tmdb_id=record.tmdb_id,
                imdb_id=record.imdb_id,
                partition=f"upstream:{record.source_id}",
                category_binding=bindings.get(record.category_id, ""),
                season_number=record.season_number,
                series_name=record.series_name,
            )
            by_key[key] = item
        else:
            # 身份证据只许补全、不许覆盖：先到的外部身份为准，
            # 后到的同键记录缺身份时不把已有身份冲掉。
            if not item.tmdb_id:
                item.tmdb_id = record.tmdb_id
            if not item.imdb_id:
                item.imdb_id = record.imdb_id
            if not item.production_year:
                item.production_year = record.production_year
            if item.season_number is None:
                item.season_number = record.season_number
            if not item.series_name:
                item.series_name = record.series_name
        line_key = f"upstream:{record.source_id}:{record.upstream_item_id}"
        if any(line.line_key == line_key for line in item.lines):
            continue
        item.lines.append(
            UpstreamLine(
                line_index=0,  # 排序后统一重编，见下
                line_key=line_key,
                source_id=record.source_id,
                upstream_item_id=record.upstream_item_id,
                episode_index=record.episode_number,
                protocol="emby",
                versions=record.versions,
            )
        )
    result = list(by_key.values())
    for item in result:
        item.lines.sort(key=lambda line: (line.source_id, line.upstream_item_id))
        for index, line in enumerate(item.lines):
            line.line_index = index
        # 分区标记取排序后首条线路的源（主线路），与输入顺序无关；
        # 全部线路的源信息在 lines[] 里，不以 partition 冒充全量。
        if item.lines:
            item.partition = f"upstream:{item.lines[0].source_id}"
    result.sort(key=lambda item: item.aggregate_key)
    return result


# ---------------------------------------------------------------------------
# 展示选择：本地优先、上游兜底（用户口径，非 FMBY 实测）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PresentationEntry:
    """展示层候选条目：只排序、不合并表。

    ``origin`` 为 ``local`` 或 ``upstream:<source_id>``；
    ``aggregate_key`` 是本地作品与上游作品比对同一性的唯一
    依据（本地侧调用方须用同一 :func:`aggregate_key_for` 口径
    换算，换算函数不在本模块——本地条目结构归本地模块管）。
    """

    aggregate_key: str
    origin: str
    name: str


def select_for_presentation(
    local_entries: Iterable[PresentationEntry],
    upstream_items: Iterable[UpstreamAggregateItem],
    *,
    aggregation_enabled: bool,
) -> List[PresentationEntry]:
    """展示排序：本地在前，上游只补本地没有的作品。

    用户已定口径，全站一个规矩，不按来源/按片子分细规则：
    关时只剩本地（逐字不变）；开时本地作品按原顺序全在前，
    上游作品只在本地没有同键作品时追加在后——上游绝不插队
    到本地前面，也绝不顶掉本地作品。这是用户口径，不是 FMBY
    实测（FMBY 上游播放未跑完，排序规则实测中没有），报告与
    模块头均已显式标明。
    """
    local = list(local_entries)
    if not aggregation_enabled:
        return local
    local_keys = {entry.aggregate_key for entry in local}
    result = list(local)
    seen = set(local_keys)
    for item in upstream_items:
        if item.aggregate_key in seen:
            continue
        seen.add(item.aggregate_key)
        result.append(
            PresentationEntry(
                aggregate_key=item.aggregate_key,
                origin=item.partition or "upstream",
                name=item.name,
            )
        )
    return result


# ---------------------------------------------------------------------------
# 对账：新增 / 变更 / 缺失宽限 / 下架（最接近实测的推断，待复验）
# ---------------------------------------------------------------------------


@dataclass
class TrackedUpstreamItem:
    """对账跟踪态：调用方（同步工人）持有，不落本模块的表。

    ``missing_rounds`` 是连续缺失轮数；``withdrawn`` 为真表示
    已达宽限应下架——下架动作本身归工人，本模块只算状态。
    ``external_ids`` 在下架与恢复时都保留，恢复后身份绑定不丢。
    """

    aggregate_key: str
    missing_rounds: int = 0
    withdrawn: bool = False
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None


@dataclass
class ReconcileResult:
    """一轮对账的结果（纯计算，不写库、不删条目）。"""

    added: List[str] = field(default_factory=list)
    changed: List[str] = field(default_factory=list)
    still_missing: List[str] = field(default_factory=list)
    withdrawn: List[str] = field(default_factory=list)
    round_trusted: bool = True
    tracked: Dict[str, TrackedUpstreamItem] = field(default_factory=dict)


def reconcile(
    tracked: Dict[str, TrackedUpstreamItem],
    new_items: Optional[Iterable[UpstreamAggregateItem]],
    *,
    fetch_failed: bool = False,
    missing_grace_rounds: int = DEFAULT_MISSING_GRACE_ROUNDS,
) -> ReconcileResult:
    """对账一轮目录（纯函数；写库/下架归调用方）。

    纪律（借自 FMBY 扫描域实测，待复验）：
    - 取页失败（``fetch_failed=True``）→ 整轮不可信，状态原样
      不动：单轮拉不到不删，绝不因一次失败误下架。
    - 取页成功但某作品缺席 → 该作品 ``missing_rounds + 1``；
      连续缺失达 ``missing_grace_rounds`` 才进 ``withdrawn``；
      未达宽限前只记 ``still_missing``，不下架。
    - 作品重现 → ``missing_rounds`` 清零、``withdrawn`` 解除，
      外部身份保留（身份绑定不丢）。
    - 新增作品进 ``added``；已跟踪且身份/名称变化的进
      ``changed``（名称变化只算展示变化，不算身份变化）。
    """
    if missing_grace_rounds < 1:
        raise ValueError("missing_grace_rounds must be >= 1")
    result = ReconcileResult(round_trusted=not fetch_failed)
    new_tracked: Dict[str, TrackedUpstreamItem] = {
        key: TrackedUpstreamItem(
            aggregate_key=state.aggregate_key,
            missing_rounds=state.missing_rounds,
            withdrawn=state.withdrawn,
            tmdb_id=state.tmdb_id,
            imdb_id=state.imdb_id,
        )
        for key, state in tracked.items()
    }
    if fetch_failed:
        result.tracked = new_tracked
        return result
    new_by_key: Dict[str, UpstreamAggregateItem] = {}
    for item in new_items or []:
        new_by_key[item.aggregate_key] = item
    for key, item in new_by_key.items():
        state = new_tracked.get(key)
        if state is None:
            new_tracked[key] = TrackedUpstreamItem(
                aggregate_key=key,
                tmdb_id=item.tmdb_id,
                imdb_id=item.imdb_id,
            )
            result.added.append(key)
        else:
            changed = (
                (state.tmdb_id or None) != (item.tmdb_id or None)
                or (state.imdb_id or None) != (item.imdb_id or None)
            )
            if changed:
                result.changed.append(key)
                state.tmdb_id = item.tmdb_id
                state.imdb_id = item.imdb_id
            state.missing_rounds = 0
            state.withdrawn = False
    for key, state in new_tracked.items():
        if key in new_by_key:
            continue
        state.missing_rounds += 1
        if state.missing_rounds >= missing_grace_rounds:
            if not state.withdrawn:
                result.withdrawn.append(key)
            state.withdrawn = True
        else:
            result.still_missing.append(key)
    result.tracked = new_tracked
    return result


# ---------------------------------------------------------------------------
# 多源聚合：失败隔离到该源（一个源挂了不拖垮整页）
# ---------------------------------------------------------------------------


@dataclass
class SourceAggregateResult:
    """单个源的聚合结果：成功给聚合项，失败给错误，互不干扰。"""

    source_id: str
    items: List[UpstreamAggregateItem] = field(default_factory=list)
    error: Optional[str] = None


def aggregate_sources(
    per_source: Dict[str, object],
    *,
    category_bindings: Optional[Dict[str, str]] = None,
) -> List[SourceAggregateResult]:
    """多源聚合入口：每源独立成败，失败只隔离到该源。

    ``per_source`` 的值约定：成功时为该源的目录记录列表
    （``UpstreamCatalogRecord``），失败时为 ``Exception`` 实例
    或错误字符串；未知形态按失败计，不抛异常——展示层拿到
    的永远是“健康的源照常出、挂的源只少它自己那份”，整页
    绝不因一个源 500 而空。
    """
    results: List[SourceAggregateResult] = []
    for source_id in sorted(per_source):
        value = per_source[source_id]
        if isinstance(value, Exception):
            results.append(
                SourceAggregateResult(source_id=source_id, error=str(value))
            )
            continue
        if isinstance(value, str):
            results.append(SourceAggregateResult(source_id=source_id, error=value))
            continue
        if isinstance(value, list) and all(
            isinstance(record, UpstreamCatalogRecord) for record in value
        ):
            results.append(
                SourceAggregateResult(
                    source_id=source_id,
                    items=build_aggregates(value, category_bindings=category_bindings),
                )
            )
            continue
        results.append(
            SourceAggregateResult(
                source_id=source_id,
                error=f"unexpected per-source value type: {type(value).__name__}",
            )
        )
    return results

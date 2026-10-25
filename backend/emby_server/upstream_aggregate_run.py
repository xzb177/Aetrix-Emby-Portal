"""上游聚合手动触发（FMBY 学习 #25，25-2 接线）。

25-2 的 ``upstream_aggregation`` 是纯模型：只比 ``aggregate_key``、
不发 HTTP、不碰数据库，生产代码一直没有调用方。本模块就是那
个调用方——但只做**管理端手动触发**，不做自动调度、不做定时
任务、不接任何自动链路：

- 选源 → 拉目录（Emby Items 分页）→ ``aggregate_sources`` →
  配对本地条目 → 开关开时 ``record_mapping`` 写映射 → 回配对结果。
- 只许读 ``enabled`` 的源（停用即断聚合，照 25-1 口径）；
  ``applecms`` 型源不走这里（它有自己的 ``applecms_sync``），
  误触发响亮 400，不静默按 Emby 协议去撞 CMS 接口。
- 绝不碰 ``video_stream``、绝不接自动兜底：映射落地不等于自动
  播放兜底上线，那是映射落地后的另一步，本模块不替它开路。
- 凭据纪律：只经 ``upstream_sources.load_credential`` 取凭据
  （密封通道，容器内解密使用）；凭据原文、密封值、对方令牌、
  上游播放地址一律不进返回值、不进日志。

配对规则（证据先行，身份件优先、标题只作最后手段）：
- ``tmdb_id`` 精确命中 → 置信度 1.0，证据 ``tmdb``；
- ``imdb_id`` 精确命中 → 置信度 0.95，证据 ``imdb``；
- 上游聚合本身无外部身份（键就是 ``title:`` 形）时，才按
  标题 + 年份配对 → 置信度 0.7（与 ``applecms_sync`` 的
  ``_match_confidence`` 同值），证据 ``title+year``；
  有外部身份的上游聚合绝不降级成标题配对——那是按标题猜，
  本模块不做。
- 本地标题取 ``scan_title``（扫描期身份快照）回落 ``name``，
  年份取 ``scan_year`` 回落 ``production_year``；年份缺失不只
  按标题配（照 CMS 同步同款纪律）。
- 单集只按单集自身的 ``tmdb_id`` / ``imdb_id`` 精确配对：
  剧名 + 季集号没有年份护栏，弱证据不配对，缺身份的单集
  只进未配对统计、不写映射（待后续项：系列身份 + 季集号
  的系列级配对）。
- 同一本地条目被多个上游条目配上时，每个上游条目各写一行
  映射（同一本地条目多线路是合法形态）；同一三元组重复写
  由 ``record_mapping`` 的幂等 upsert 兜底。
- 本地侧只认活行：``merged_into_id IS NULL``（``deleted_at``
  由 ORM 全局钩子自动拼）；同一身份多行时取 id 最小（确定性）。

写路径开关：``UPSTREAM_ITEM_MAPPING_ENABLED``（默认关）只管
写路径——关时 ``record_mapping`` 返回 None、不写任何东西，
本模块照实回 ``written=0`` 与 ``mapping_enabled=false``，
配对结果照常返回（管理员先看配对质量、再决定开不开写）。

有界性：单次手动聚合最多拉 20000 条目录记录，超了响亮报错
（不静默截断）；上游 TotalRecordCount 说谎导致死循环时，
同样由该上限截停。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as us

logger = logging.getLogger(__name__)

#: 单次手动聚合的目录记录上限（防上游 TotalRecordCount 说谎死循环）。
MAX_RECORDS_PER_RUN = 20000

#: 单页默认大小（Emby StartIndex/Limit 分页）。
DEFAULT_PAGE_SIZE = 100

#: 单次请求超时（秒）：目录页带数据，比探活的 10 秒放宽。
HTTP_TIMEOUT_SEC = 30.0

#: 配对置信度：外部身份精确命中高于标题 + 年份封顶值 0.7。
CONFIDENCE_TMDB = 1.0
CONFIDENCE_IMDB = 0.95
CONFIDENCE_TITLE_YEAR = 0.7

#: 上游侧可配对的条目类型：季（Season）不是播放寻址目标，不配对。
PAIRABLE_ITEM_TYPES = ("Movie", "Series", "Episode")


class UpstreamAggregateRunError(Exception):
    """手动聚合的运行错误：带 HTTP 状态码与短码，管理端直接映射。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _credential_error() -> UpstreamAggregateRunError:
    return UpstreamAggregateRunError(
        409, "upstream_credential_invalid",
        "上游凭据失效，请管理员重录该源凭据")


def _unavailable_error() -> UpstreamAggregateRunError:
    return UpstreamAggregateRunError(
        502, "upstream_unavailable", "上游服务当前不可用，请稍后再试")


def _quota_error() -> UpstreamAggregateRunError:
    return UpstreamAggregateRunError(
        429, "upstream_quota_exhausted", "上游额度已耗尽，请稍后再试或换个源")


# ---------------------------------------------------------------------------
# 传输层（urllib，与 upstream_playback 的 _default_http_post 同款形态）
# ---------------------------------------------------------------------------

def _default_http_post(url: str, payload: dict, headers: dict,
                       timeout: float):
    """认证 POST：返回 (status|None, body|None)，超时/断连给 (None, None)。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                body = None
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            body = None
        return exc.code, body
    except Exception:
        return None, None


def _default_http_get(url: str, headers: dict, timeout: float):
    """目录 GET：返回 (status|None, body|None)，超时/断连给 (None, None)。"""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                body = None
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            body = None
        return exc.code, body
    except Exception:
        return None, None


def _source_headers(source) -> dict:
    """发往上游的请求头：源上配的 UA / Referer / 附加头。

    与 upstream_playback._source_headers 同口径；凭据绝不走这里。
    """
    headers: dict = {"Accept": "application/json"}
    if getattr(source, "user_agent", None):
        headers["User-Agent"] = source.user_agent
    if getattr(source, "referer", None):
        headers["Referer"] = source.referer
    try:
        extra = json.loads(source.extra_headers_json or "{}")
    except (TypeError, ValueError):
        extra = {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and isinstance(value, str):
                headers[key] = value
    return headers


def _api_key_for_source(source, *, http_post: Optional[Callable] = None):
    """取上游 Items 请求用的 api_key（匿名返回 None）。

    - 未配凭据 → None（匿名访问）；
    - api_key 形态 → 密封值本身就是 key；
    - password 形态 → 向上游认证口换 AccessToken，换不到按凭据
      失效 / 上游不可用映射，绝不降级成匿名。
    密封解不开（SealedSecretError）→ 凭据失效，调用方须请管理员重录。
    """
    try:
        cred = us.load_credential(source)
    except us.SealedSecretError as exc:
        raise _credential_error() from exc
    if cred is None:
        return None
    if cred["auth_method"] == "api_key":
        return cred["secret"]
    if cred["auth_method"] == "password":
        post = _default_http_post if http_post is None else http_post
        base = (source.base_url or "").rstrip("/")
        headers = dict(_source_headers(source))
        headers["Content-Type"] = "application/json"
        headers["X-Emby-Authorization"] = (
            'MediaBrowser Client="Aetrix", Device="upstream-aggregate", '
            'DeviceId="upstream-aggregate", Version="1.0"'
        )
        status, body = post(
            f"{base}/Users/AuthenticateByName",
            {"Username": cred["username"], "Pw": cred["secret"]},
            headers, HTTP_TIMEOUT_SEC)
        if status == 200 and isinstance(body, dict) and body.get("AccessToken"):
            return str(body["AccessToken"])
        if status in (401, 403):
            raise _credential_error()
        raise _unavailable_error()
    raise _unavailable_error()


def fetch_upstream_records(source, *, http_get: Optional[Callable] = None,
                           http_post: Optional[Callable] = None,
                           page_size: int = DEFAULT_PAGE_SIZE
                           ) -> List[agg.UpstreamCatalogRecord]:
    """拉回某源的全部目录记录（Emby Items 分页，Recursive）。

    只拉 Movie / Series / Episode 三型；季不进聚合（不是配对目标）。
    取页失败（401/403/429/5xx/断连）按播放口同款短码映射，绝不
    把半页数据当成完整目录返回。
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    get = _default_http_get if http_get is None else http_get
    api_key = _api_key_for_source(source, http_post=http_post)
    base = (source.base_url or "").rstrip("/")
    headers = _source_headers(source)

    def fetch_page(start: int, limit: int) -> dict:
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Series,Episode",
            "Fields": "ProviderIds,MediaSources",
            "StartIndex": str(start),
            "Limit": str(limit),
        }
        if api_key:
            params["api_key"] = api_key
        url = f"{base}/Items?{urllib.parse.urlencode(params)}"
        status, body = get(url, headers, HTTP_TIMEOUT_SEC)
        if status == 200 and isinstance(body, dict):
            return body
        if status in (401, 403):
            raise _credential_error()
        if status == 429:
            raise _quota_error()
        raise _unavailable_error()

    records: List[agg.UpstreamCatalogRecord] = []
    for record in agg.iter_emby_pages(fetch_page, source_id=str(source.id),
                                      page_size=page_size):
        records.append(record)
        if len(records) > MAX_RECORDS_PER_RUN:
            raise UpstreamAggregateRunError(
                502, "upstream_catalog_too_large",
                f"上游目录超过单次手动聚合上限 "
                f"{MAX_RECORDS_PER_RUN} 条，未做截断，请分源/分库处理")
    return records


# ---------------------------------------------------------------------------
# 配对：聚合作品 → 本地条目（只算配对，不写库；写库在 run 里）
# ---------------------------------------------------------------------------

def _local_title_year(local) -> Tuple[Optional[str], Optional[int]]:
    """本地标题 + 年份：扫描期快照优先，缺失回落现行字段。"""
    title = (getattr(local, "scan_title", None)
             or getattr(local, "name", None))
    year = (getattr(local, "scan_year", None)
            if getattr(local, "scan_year", None) is not None
            else getattr(local, "production_year", None))
    title = (title or "").strip() or None
    return title, year


def _local_title_year_key(local) -> Optional[str]:
    """本地条目的标题 + 年份键（年份缺失返回 None：不只按标题配）。"""
    title, year = _local_title_year(local)
    if not title or year is None:
        return None
    probe = agg.UpstreamCatalogRecord(
        source_id="", upstream_item_id="", name=title,
        item_type="Movie", production_year=year)
    return agg.aggregate_key_for(probe)


def _alive_local_query(db: Session):
    """本地活行查询基底：merged_into_id 显式过滤（deleted_at 由 ORM
    全局钩子自动拼，见软删除纪律）。"""
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.merged_into_id.is_(None)))


def _type_matches(local, aggregate_item_type: str) -> bool:
    """本地行类型与上游聚合类型是否同类（movie↔Movie 等）。

    tmdb/imdb 精确命中也须同类：电影身份绝不配到单集行上。
    """
    local_type = (getattr(local, "item_type", "") or "").lower()
    return local_type == (aggregate_item_type or "").lower()


def pair_aggregates(db: Session, items: List[agg.UpstreamAggregateItem]):
    """给聚合作品配本地条目（纯配对，不写库）。

    返回 (pairs, unpaired_reasons)：pairs 每项含本地条目 id、
    上游条目 id（逐线路）、置信度、证据、集序号；unpaired_reasons
    按原因码计数（不逐条列未配对明细，响应体保持小）。

    查询策略（批量，不逐聚合查库）：
    - tmdb / imdb 各一次 IN 查询建映射；
    - 标题 + 年份按年份分组查候选，Python 里按聚合键比对。
    """
    pairs: List[dict] = []
    unpaired: Dict[str, int] = {}

    def bump(reason: str) -> None:
        unpaired[reason] = unpaired.get(reason, 0) + 1

    tmdb_ids = {item.tmdb_id for item in items if item.tmdb_id}
    imdb_ids = {item.imdb_id for item in items if item.imdb_id}
    by_tmdb: Dict[str, object] = {}
    by_imdb: Dict[str, object] = {}
    if tmdb_ids:
        for row in (_alive_local_query(db)
                    .filter(em.MediaItem.tmdb_id.in_(sorted(tmdb_ids)))
                    .order_by(em.MediaItem.id).all()):
            by_tmdb.setdefault(str(row.tmdb_id), row)
    if imdb_ids:
        for row in (_alive_local_query(db)
                    .filter(em.MediaItem.imdb_id.in_(sorted(imdb_ids)))
                    .order_by(em.MediaItem.id).all()):
            by_imdb.setdefault(str(row.imdb_id), row)

    title_year_items = [item for item in items
                        if not item.tmdb_id and not item.imdb_id
                        and item.item_type in ("Movie", "Series")]
    by_title_year_key: Dict[str, object] = {}
    if title_year_items:
        years = {item.production_year for item in title_year_items
                 if item.production_year is not None}
        for year in sorted(years):
            candidates = (_alive_local_query(db)
                          .filter(em.MediaItem.item_type.in_(
                              ["movie", "series"]))
                          .filter((em.MediaItem.scan_year == year)
                                  | (em.MediaItem.production_year == year))
                          .order_by(em.MediaItem.id).all())
            for row in candidates:
                key = _local_title_year_key(row)
                if key:
                    by_title_year_key.setdefault(key, row)

    for item in items:
        if item.item_type not in PAIRABLE_ITEM_TYPES:
            bump("type_not_pairable")
            continue
        local = None
        confidence = 0.0
        evidence = ""
        tmdb_row = by_tmdb.get(item.tmdb_id) if item.tmdb_id else None
        imdb_row = by_imdb.get(item.imdb_id) if item.imdb_id else None
        if tmdb_row is not None and _type_matches(tmdb_row, item.item_type):
            local = tmdb_row
            confidence, evidence = CONFIDENCE_TMDB, "tmdb"
        elif imdb_row is not None and _type_matches(imdb_row, item.item_type):
            local = imdb_row
            confidence, evidence = CONFIDENCE_IMDB, "imdb"
        elif item.item_type in ("Movie", "Series"):
            # 只有本身无外部身份的聚合才走标题 + 年份：有身份的绝不
            # 降级成标题猜（见模块头配对规则）。
            if item.aggregate_key.startswith("title:"):
                local = by_title_year_key.get(item.aggregate_key)
                if local is not None:
                    confidence, evidence = CONFIDENCE_TITLE_YEAR, "title+year"
        if local is None:
            if item.item_type == "Episode":
                bump("episode_no_identity")
            elif item.tmdb_id or item.imdb_id:
                bump("identity_no_local_match")
            else:
                bump("title_year_no_local_match")
            continue
        for line in item.lines:
            pairs.append({
                "aggregate_key": item.aggregate_key,
                "name": item.name,
                "item_type": item.item_type,
                "local_item_id": local.id,
                "upstream_item_id": line.upstream_item_id,
                "episode_index": line.episode_index,
                "confidence": confidence,
                "evidence": evidence,
            })
    return pairs, unpaired


# ---------------------------------------------------------------------------
# 手动触发总入口（只 flush 不 commit，调用方事务内提交）
# ---------------------------------------------------------------------------

def run_manual_aggregation(db: Session, source, *,
                           http_get: Optional[Callable] = None,
                           http_post: Optional[Callable] = None,
                           page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """手动聚合一轮：拉目录 → 聚合 → 配对 → 开关开时写映射。

    前置校验（响亮失败，不静默跳过）：
    - 源不存在由调用方先 404（本函数不查源行）；
    - 源未启用 → 400（停用即断聚合）；
    - applecms 型 → 400（走它自己的同步，不按 Emby 协议撞接口）。
    返回的 pairs 含 written 标记：开关关时一律 false、written=0。
    """
    if not getattr(source, "enabled", False):
        raise UpstreamAggregateRunError(
            400, "source_disabled", "上游源未启用，先启用再聚合")
    if getattr(source, "source_type", "") == "applecms":
        raise UpstreamAggregateRunError(
            400, "source_type_not_supported",
            "苹果 CMS 源走自己的同步，不走 Emby 目录聚合")
    records = fetch_upstream_records(
        source, http_get=http_get, http_post=http_post, page_size=page_size)
    aggregated = agg.aggregate_sources({str(source.id): records})
    if aggregated and aggregated[0].error:
        raise UpstreamAggregateRunError(
            502, "upstream_aggregate_failed",
            f"上游聚合失败：{aggregated[0].error}")
    items = aggregated[0].items if aggregated else []
    pairs, unpaired = pair_aggregates(db, items)
    mapping_on = uim.mapping_enabled()
    written = 0
    for pair in pairs:
        row = uim.record_mapping(
            db, local_item_id=pair["local_item_id"],
            source_id=source.id,
            upstream_item_id=pair["upstream_item_id"],
            confidence=pair["confidence"], evidence=pair["evidence"],
            episode_index=pair["episode_index"])
        pair["written"] = row is not None
        if row is not None:
            written += 1
    logger.info("上游手动聚合 source_id=%s fetched=%s aggregates=%s "
                "pairs=%s written=%s mapping_enabled=%s",
                source.id, len(records), len(items), len(pairs), written,
                mapping_on)
    return {
        "source_id": source.id,
        "fetched": len(records),
        "aggregates": len(items),
        "pairs": pairs,
        "paired": len(pairs),
        "written": written,
        "mapping_enabled": mapping_on,
        "unpaired_reasons": unpaired,
    }

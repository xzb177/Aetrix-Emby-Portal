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

拉取口径（2026-10-31 优化 O-05）：分页 Fields 只留 ProviderIds
（MediaSources 解析出的 versions 不落库、不参与配对，每页白背
是纯浪费）；单页默认 500（SystemConfig 可调，见
``configured_default_page_size``）；单页瞬时失败（断连/超时/
5xx）有限重试（默认 2 次、退避 2 秒，SystemConfig 可调），
401/403/429 等确定性错误不重试、映射逐字不变。

有界性：按库分窗聚合，不再整源一把梭，也不再设单库总量上限。
先经 ``/Library/MediaFolders`` 取上游库清单，再逐库按 ``ParentId``
拉取、每 ``WINDOW_RECORDS`` 条一窗聚合配对写映射；内存与单次
IN 查询规模恒以窗为界，不随库大小增长。旧单库 20000 上限已删：
2026-10-28 实证它把云海服 4 个真实大库（58100 / 32151 /
113070 / 142533 条）挡在聚合之外；它原先防的“TotalRecordCount
说谎死循环”已由 ``iter_emby_pages`` 的空页终止覆盖（某页 0 条
即停，总量说谎只会提前截断、不会死循环）。单库失败只记入该库
的失败项、继续下一库，已写窗的映射不受影响；重跑靠
``record_mapping`` 的幂等 upsert 续跑（已写窗不建新行），本模块
不新增持久检查点表——断点续跑 = 重跑本接口，失败库重试、成功
窗 upsert 不重复。全局性失败（源未启用、凭据失效、额度耗尽）
发生在首库之前时仍响亮报错，不写任何东西。2026-10-31 优化
O-01 起，任务层（upstream_aggregate_jobs + upstream_aggregate_
tasks 表）在此之上加了逐库提交与可恢复续跑：执行器传
commit_each_library=True 时逐库一事务、撞冲突只回滚单库；
续跑按任务行里已完成的库跳过（skip_library_ids），本模块
自身仍不建检查点表。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_http
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as us

logger = logging.getLogger(__name__)

#: 整源拉取助手的遗留上限（仅 ``fetch_upstream_records`` 整源路径用，
#: 生产逐库路径已改分窗、不再用它）：防上游 TotalRecordCount 说谎
#: 死循环。2026-10-28 实证：该上限曾把云海服 4 个真实大库
#: （58100 / 32151 / 113070 / 142533 条）挡在聚合之外；逐库路径
#: 的防循环已由 ``iter_emby_pages`` 的空页终止覆盖（见模块头）。
MAX_RECORDS_PER_RUN = 20000

#: 单库上限的显式别名，与 MAX_RECORDS_PER_RUN 同值（同上，仅遗留路径）。
MAX_RECORDS_PER_LIBRARY = MAX_RECORDS_PER_RUN

#: 逐库分窗大小（条）：单库按此分窗拉取 → 聚合 → 配对 → 写映射，
#: 内存与单次 IN 查询规模恒以窗为界，不随库大小增长；窗内失败
#: 只失败该库（已写窗的映射不受影响，重跑 upsert 不重复）。
#: 5000 条约数 MB 内存、50 次分页请求，6 小时调度轮次内绰绰有余。
WINDOW_RECORDS = 5000

#: 单页默认大小（Emby StartIndex/Limit 分页）。2026-10-31 优化
#: O-05：100 → 500（接口上限），同样数据量请求数降到 1/5；缺省值
#: 可经 SystemConfig ``upstream_aggregate_default_page_size`` 调
#: （钳 10–500，见 configured_default_page_size），显式传参仍优先。
DEFAULT_PAGE_SIZE = 500

#: 单页瞬时失败重试次数 SystemConfig 键（O-05）：默认 2 次；
#: 只重试断连/超时/5xx 这类瞬时形态，401/403/429 是确定性错误
#: 不重试。钳 [0, 5]，配 0 即关重试、回旧行为。纯 SystemConfig
#: 键，不进 env.example（#1527 先例）。
KEY_PAGE_RETRIES = "upstream_aggregate_page_retries"
PAGE_RETRIES_DEFAULT = 2

#: 单页重试退避（秒）SystemConfig 键（O-05）：默认 2 秒，钳 [0, 30]。
KEY_PAGE_RETRY_BACKOFF_SEC = "upstream_aggregate_page_retry_backoff_sec"
PAGE_RETRY_BACKOFF_DEFAULT = 2

#: 默认页大小 SystemConfig 键（O-05）：钳 [10, 500]，无行/非法
#: 回落 DEFAULT_PAGE_SIZE。
KEY_DEFAULT_PAGE_SIZE = "upstream_aggregate_default_page_size"

#: 测试注入：重试退避的睡眠实现（生产用 time.sleep）。
_sleep = time.sleep


def _system_config_int(db, key):
    """SystemConfig 整数热读：无行/非法/异常一律返回 None（回退）。

    与 transfer_variant._system_config_int 同口径（读失败不许把
    聚合打停，一律回落代码默认）。
    """
    if db is None:
        return None
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    # 与 transfer 版的唯一差别：本家族里 0 是合法值（重试配 0 = 关
    # 重试、退避配 0 = 不等待），只有负数按未设置处理。
    return value if value >= 0 else None


def configured_default_page_size(db=None) -> int:
    """聚合缺省页大小：SystemConfig > DEFAULT_PAGE_SIZE，钳 [10, 500]。"""
    configured = _system_config_int(db, KEY_DEFAULT_PAGE_SIZE)
    if configured is not None:
        return max(10, min(500, configured))
    return DEFAULT_PAGE_SIZE


def _page_retry_config(db):
    """单页重试 (次数, 退避秒)：SystemConfig > 代码默认，钳同注册表。"""
    retries = _system_config_int(db, KEY_PAGE_RETRIES)
    if retries is None:
        retries = PAGE_RETRIES_DEFAULT
    backoff = _system_config_int(db, KEY_PAGE_RETRY_BACKOFF_SEC)
    if backoff is None:
        backoff = PAGE_RETRY_BACKOFF_DEFAULT
    return max(0, min(5, retries)), max(0, min(30, backoff))

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


def _frontend_blocked_error() -> UpstreamAggregateRunError:
    return UpstreamAggregateRunError(
        502, "upstream_frontend_blocked",
        "上游前门（nginx）拦截，未到 Emby：请对方放行本方出口 IP "
        "15.204.95.101，重录凭据无效")


def _forbidden_error(body) -> UpstreamAggregateRunError:
    """403 分流：Emby JSON 报错体 → 凭据失效；无 JSON 体（nginx HTML
    被传输层吞成 None）→ 前门拦截 502。重录凭据修不好后者，绝不误判。"""
    if isinstance(body, dict):
        return _credential_error()
    return _frontend_blocked_error()


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
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "Aetrix/1.0")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
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
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "Aetrix/1.0")
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
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
    # 前门会拦 Python-urllib 默认 UA（实测 403，聚合误判为凭据失效）：
    # 源没配 UA 时默认用 Aetrix UA，不许裸用 urllib 默认 UA 出站。
    headers["User-Agent"] = getattr(source, "user_agent", None) or "Aetrix/1.0"
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
      失效 / 上游不可用映射，绝不降级成匿名；403 按体分流
      （JSON 体 → 凭据失效，无 JSON 体 → 前门拦截 502）。
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
        if status == 401:
            raise _credential_error()
        if status == 403:
            raise _forbidden_error(body)
        raise _unavailable_error()
    raise _unavailable_error()


def _resolve_api_key(source, *, http_post=None):
    """取 Items 请求用的 api_key；None = 匿名。只解析一次供逐库复用。"""
    return _api_key_for_source(source, http_post=http_post)


def _page_fetcher(source, api_key, get, *, parent_id=None,
                  retries: int = PAGE_RETRIES_DEFAULT,
                  backoff_sec: float = PAGE_RETRY_BACKOFF_DEFAULT):
    """造一个 Emby Items 取页回调：parent_id 给了就按库拉，不给走整源。

    错误映射与旧整源路径同款：401→凭据失效、403 分流（JSON 体→
    凭据失效、无 JSON 体→前门拦截 502）、429→额度、其余→上游
    不可用，绝不把半页数据当成完整目录返回。

    拉取瘦身（O-05）：Fields 只留 ProviderIds——MediaSources 解析出
    的 versions 从不落库、不参与配对（见上游聚合链优化设计稿
    P-06），每页白背全量版本信息是纯浪费。

    分页韧性（O-05）：断连/超时（status None）与 5xx 是瞬时形态，
    按 retries/backoff_sec 有限重试后才判上游不可用；401/403/429
    是确定性错误，重试只会放大上游压力且改变不了结论，不重试。
    """

    base = (source.base_url or "").rstrip("/")
    headers = _source_headers(source)

    def fetch_page(start: int, limit: int) -> dict:
        params = {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Series,Episode",
            "Fields": "ProviderIds",
            "StartIndex": str(start),
            "Limit": str(limit),
        }
        if parent_id:
            params["ParentId"] = str(parent_id)
        if api_key:
            params["api_key"] = api_key
        url = f"{base}/Items?{urllib.parse.urlencode(params)}"
        attempt = 0
        while True:
            status, body = get(url, headers, HTTP_TIMEOUT_SEC)
            if status == 200 and isinstance(body, dict):
                return body
            if status == 401:
                raise _credential_error()
            if status == 403:
                raise _forbidden_error(body)
            if status == 429:
                raise _quota_error()
            # 其余（None / 5xx / 200 非 dict）按瞬时失败处理。
            if attempt >= retries:
                raise _unavailable_error()
            attempt += 1
            logger.warning(
                "上游聚合取页瞬时失败重试 source_id=%s start=%s "
                "attempt=%s/%s status=%s",
                getattr(source, "id", None), start, attempt, retries,
                status)
            if backoff_sec > 0:
                _sleep(backoff_sec)

    return fetch_page


def fetch_upstream_libraries(source, *, http_get=None,
                             http_post=None, api_key=None) -> List[dict]:
    """取上游库清单（Emby /Library/MediaFolders）。

    返回 [{"id": ..., "name": ...}]（按上游顺序，Id 去重）。
    该端点是 Emby 官方库清单口（本仓库 compat_routes 亦实现同口），
    api_key 形态同样走 api_key 参数。空清单 = 上游无可见库，照实
    返回空列表，不回落整源拉取（回落会重回整源上限问题）。
    403 按体分流：Emby JSON 体 → 凭据失效，无 JSON 体（nginx HTML
    被传输层吞成 None）→ 前门拦截 502。
    """
    get = _default_http_get if http_get is None else http_get
    if api_key is None:
        api_key = _resolve_api_key(source, http_post=http_post)
    base = (source.base_url or "").rstrip("/")
    headers = _source_headers(source)
    params = {}
    if api_key:
        params["api_key"] = api_key
    url = f"{base}/Library/MediaFolders"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    status, body = get(url, headers, HTTP_TIMEOUT_SEC)
    if status == 401:
        raise _credential_error()
    if status == 403:
        raise _forbidden_error(body)
    if status == 429:
        raise _quota_error()
    if status != 200 or body is None:
        raise _unavailable_error()
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = body.get("Items") or []
    else:
        items = []
    libraries: List[dict] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        lib_id = item.get("Id")
        if not lib_id or str(lib_id) in seen:
            continue
        seen.add(str(lib_id))
        libraries.append({"id": str(lib_id),
                          "name": str(item.get("Name") or "")})
    return libraries


def fetch_upstream_records(source, *, http_get: Optional[Callable] = None,
                           http_post: Optional[Callable] = None,
                           page_size: int = DEFAULT_PAGE_SIZE,
                           retries: int = PAGE_RETRIES_DEFAULT,
                           backoff_sec: float = PAGE_RETRY_BACKOFF_DEFAULT
                           ) -> List[agg.UpstreamCatalogRecord]:
    """拉回某源的全部目录记录（Emby Items 分页，Recursive）。

    遗留整源路径：只拉 Movie / Series / Episode 三型；季不进聚合。
    新的 run_manual_aggregation 不再走这里（走逐库路径），本函数
    保留供显式整源调用与旧测试使用；单库上限语义见模块头。
    取页失败（401/403/429/5xx/断连）按播放口同款短码映射，绝不
    把半页数据当成完整目录返回；瞬时形态先按 retries 有限重试
    （O-05，本路径无 DB 会话，重试参数用代码默认，逐库路径才
    从 SystemConfig 解析）。
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    get = _default_http_get if http_get is None else http_get
    api_key = _resolve_api_key(source, http_post=http_post)
    fetch_page = _page_fetcher(source, api_key, get,
                               retries=retries, backoff_sec=backoff_sec)
    records: List[agg.UpstreamCatalogRecord] = []
    for record in agg.iter_emby_pages(fetch_page, source_id=str(source.id),
                                      page_size=page_size):
        records.append(record)
        if len(records) > MAX_RECORDS_PER_RUN:
            raise UpstreamAggregateRunError(
                502, "upstream_catalog_too_large",
                f"上游单库目录超过单库聚合上限 "
                f"{MAX_RECORDS_PER_RUN} 条，未做截断")
    return records


def iter_upstream_records_for_parent(
        source, parent_id, *, http_get: Optional[Callable] = None,
        http_post: Optional[Callable] = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        api_key: Optional[str] = None,
        retries: int = PAGE_RETRIES_DEFAULT,
        backoff_sec: float = PAGE_RETRY_BACKOFF_DEFAULT):
    """按 ParentId 流式拉回某上游库的目录记录（逐库分窗的核心）。

    只产出、不攒 list、不设单库总量上限：总量上限曾把真实大库
    挡在聚合之外（2026-10-28 云海服 4 库实证）；防循环由
    ``agg.iter_emby_pages`` 的空页终止覆盖（某页 0 条即停）。
    与整源路径同款分页与错误映射，唯一区别是多传 ParentId；
    category_id 记为该库 id，供聚合层按库分区。
    api_key 传了就复用（逐库不重复换 token），没传则现解析。
    """
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if not parent_id:
        raise ValueError("parent_id 不能为空")
    get = _default_http_get if http_get is None else http_get
    if api_key is None:
        api_key = _resolve_api_key(source, http_post=http_post)
    fetch_page = _page_fetcher(source, api_key, get, parent_id=parent_id,
                               retries=retries, backoff_sec=backoff_sec)
    yield from agg.iter_emby_pages(
        fetch_page, source_id=str(source.id),
        category_id=str(parent_id), page_size=page_size)


def iter_record_windows(records, window_size):
    """把记录流切成定长窗（末窗可短）：纯切分，不发请求。"""
    if window_size <= 0:
        raise ValueError("window_size must be positive")
    window = []
    for record in records:
        window.append(record)
        if len(window) >= window_size:
            yield window
            window = []
    if window:
        yield window


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
    """本地活行查询基底：merged_into_id 与 is_hidden 显式过滤
    （deleted_at 由 ORM 全局钩子自动拼，见软删除纪律；与本站
    浏览口径对齐）。"""
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.merged_into_id.is_(None))
            .filter(em.MediaItem.is_hidden == False))  # noqa: E712


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
# 手动触发总入口（默认只 flush 不 commit；O-01 起执行器可传
# commit_each_library=True 改走逐库一事务）
# ---------------------------------------------------------------------------


def ensure_aggregatable(source) -> None:
    """不发上游请求的前置校验：未启用、苹果 CMS 直接拒。

    凭据失效、额度、上游不可用要打到上游才知道，不在这里判断。
    管理端入队前先调本函数，这几类仍是立刻返回的 400；
    ``run_manual_aggregation`` 开头再查一次，入队后源被停用也不会静默跑。
    """
    if not getattr(source, "enabled", False):
        raise UpstreamAggregateRunError(
            400, "source_disabled", "上游源未启用，先启用再聚合")
    if getattr(source, "source_type", "") == "applecms":
        raise UpstreamAggregateRunError(
            400, "source_type_not_supported",
            "苹果 CMS 源走自己的同步，不走 Emby 目录聚合")


def _fire_event(on_event, event: dict) -> None:
    """把进度事件交给调用方（任务行更新等）；回调异常不许打断聚合。

    回调是观测面：它失败最多丢一条进度，绝不能让一轮聚合陪葬；
    异常记日志后继续。回调契约见 run_manual_aggregation docstring。
    """
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:
        logger.exception("上游聚合进度回调失败 type=%s",
                         event.get("type"))


def run_manual_aggregation(db: Session, source, *,
                           http_get: Optional[Callable] = None,
                           http_post: Optional[Callable] = None,
                           page_size: Optional[int] = None,
                           on_event: Optional[Callable] = None,
                           commit_each_library: bool = False,
                           skip_library_ids=None,
                           prior_summaries: Optional[Dict[str, dict]] = None
                           ) -> dict:
    """手动聚合一轮：按库分批拉目录 → 逐库聚合 → 逐库配对 → 开关开时写映射。

    前置校验（响亮失败，不静默跳过，发生在任何写库之前）：
    - 源不存在由调用方先 404（本函数不查源行）；
    - 源未启用 → 400（停用即断聚合）；
    - applecms 型 → 400（走它自己的同步，不按 Emby 协议撞接口）。
    逐库语义：
    - 先取库清单，再逐库分窗（WINDOW_RECORDS 条一窗）拉取/聚合/
      配对/写映射（flush，调用方事务内提交）；
    - 单库失败（拉取失败、聚合 error）只记入该库失败项，继续下一库，
      已写窗的映射不受影响；返回时照实带 failed_libraries；
    - 全局性失败（库清单取不到凭据/额度）仍响亮报错，此时尚未写库。
    返回的 pairs 含 written 标记：开关关时一律 false、written=0；
    每对另带 library_id / library_name 供追溯；written 计 upsert
    返回的行数（含重复三元组的更新行，与旧口径一致）。

    事务口径（2026-10-31 优化 O-01，``commit_each_library``）：
    - 默认 False = 旧口径：全程只 flush，提交归调用方；单库失败
      隔离只挡 UpstreamAggregateRunError，IntegrityError 等穿透
      到调用方（整轮回滚）；
    - True = 逐库一事务：一库跑完即 commit 该库映射；该库中途
      撞 IntegrityError（映射三元组冲突）只回滚该库、记
      failed_libraries（code=mapping_conflict）后继续下一库，
      前面库的成果不陪葬。该库 summary 的 paired/written 按
      落库实况归 0（fetched/aggregates 照实），其 pairs 不进
      返回的 pairs 列表。

    进度回调（``on_event``，O-01/O-02 的可观测面）：
    - ``libraries_discovered``：库清单拿到后发一次，带 libraries
      与 total；发在任何写库之前，回调可自行 commit；
    - ``library_done``：一库跑完、commit 之前发（commit 模式），
      回调对任务行的更新搭同一事务一起提交——回调**不许**自己
      commit；非 commit 模式下在一库跑完后发，回调可自行 commit；
    - ``library_failed``：该库已 rollback（commit 模式）或已记入
      失败项后发，回调可自行 commit。
    回调异常被吞并记日志（见 _fire_event），不影响聚合本身。

    断点续跑（O-01）：``skip_library_ids`` 里的库不再拉取，其
    ``prior_summaries``（上一轮该库的 summary）原样并入本轮
    libraries 列表与总量，summary 副本带 ``skipped_resume=True``
    标记；不给 prior 的跳过库按零计数空摘要并入。靠的是任务表
    （upstream_aggregate_tasks），本函数仍不自建检查点表。
    """
    ensure_aggregatable(source)
    if page_size is None:
        page_size = configured_default_page_size(db)
    page_retries, page_backoff = _page_retry_config(db)
    api_key = _resolve_api_key(source, http_post=http_post)
    libraries = fetch_upstream_libraries(
        source, http_get=http_get, http_post=http_post, api_key=api_key)
    _fire_event(on_event, {
        "type": "libraries_discovered",
        "libraries": libraries, "total": len(libraries)})
    mapping_on = uim.mapping_enabled()
    skip_ids = {str(x) for x in (skip_library_ids or [])}
    prior = prior_summaries or {}
    all_pairs: List[dict] = []
    unpaired_total: Dict[str, int] = {}
    library_summaries: List[dict] = []
    failed_libraries: List[dict] = []
    total_fetched = 0
    total_aggregates = 0
    total_paired = 0
    total_written = 0
    for library in libraries:
        lib_id = library["id"]
        lib_name = library["name"]
        if str(lib_id) in skip_ids:
            # 续跑跳过：上一轮已完成的库不再拉取，摘要续报。
            summary = dict(prior.get(str(lib_id)) or {
                "library_id": lib_id, "library_name": lib_name,
                "fetched": 0, "aggregates": 0, "paired": 0, "written": 0,
                "status": "ok",
            })
            summary["library_id"] = lib_id
            summary["library_name"] = lib_name
            summary["status"] = "ok"
            summary["skipped_resume"] = True
            library_summaries.append(summary)
            total_fetched += int(summary.get("fetched") or 0)
            total_aggregates += int(summary.get("aggregates") or 0)
            total_paired += int(summary.get("paired") or 0)
            total_written += int(summary.get("written") or 0)
            _fire_event(on_event,
                        {"type": "library_done", "summary": summary})
            if commit_each_library:
                db.commit()
            continue
        summary: Dict[str, object] = {
            "library_id": lib_id, "library_name": lib_name,
            "fetched": 0, "aggregates": 0, "paired": 0, "written": 0,
            "status": "ok",
        }
        lib_fetched = lib_aggregates = lib_paired = lib_written = 0
        lib_pairs: List[dict] = []
        failure: Optional[Tuple[str, str]] = None
        try:
            record_iter = iter_upstream_records_for_parent(
                source, lib_id, http_get=http_get, http_post=http_post,
                page_size=page_size, api_key=api_key,
                retries=page_retries, backoff_sec=page_backoff)
            for window in iter_record_windows(record_iter, WINDOW_RECORDS):
                aggregated = agg.aggregate_sources({str(source.id): window})
                if aggregated and aggregated[0].error:
                    raise UpstreamAggregateRunError(
                        502, "upstream_aggregate_failed",
                        f"上游库 {lib_name or lib_id} 聚合失败："
                        f"{aggregated[0].error}")
                items = aggregated[0].items if aggregated else []
                pairs, unpaired = pair_aggregates(db, items)
                written = 0
                for pair in pairs:
                    pair["library_id"] = lib_id
                    pair["library_name"] = lib_name
                    row = uim.record_mapping(
                        db, local_item_id=pair["local_item_id"],
                        source_id=source.id,
                        upstream_item_id=pair["upstream_item_id"],
                        confidence=pair["confidence"], evidence=pair["evidence"],
                        episode_index=pair["episode_index"])
                    pair["written"] = row is not None
                    if row is not None:
                        written += 1
                lib_fetched += len(window)
                lib_aggregates += len(items)
                lib_paired += len(pairs)
                lib_written += written
                if commit_each_library:
                    lib_pairs.extend(pairs)
                else:
                    all_pairs.extend(pairs)
                for reason, count in unpaired.items():
                    unpaired_total[reason] = unpaired_total.get(reason, 0) + count
                total_fetched += len(window)
                total_aggregates += len(items)
                total_paired += len(pairs)
                total_written += written
        except UpstreamAggregateRunError as exc:
            failure = (exc.code, exc.message)
        except IntegrityError:
            if not commit_each_library:
                raise
            failure = ("mapping_conflict", "映射写入冲突，请重试")
            logger.exception("上游按库聚合映射冲突回滚单库 source_id=%s "
                             "library=%s", source.id, lib_id)
        if failure is not None:
            code, message = failure
            if commit_each_library:
                # 逐库事务：回滚本库未提交的窗，前面库的成果不动；
                # 本库 paired/written 按落库实况归 0、其 pairs 不进
                # 返回列表，总量同步扣回。
                db.rollback()
                total_paired -= lib_paired
                total_written -= lib_written
                summary.update({
                    "fetched": lib_fetched, "aggregates": lib_aggregates,
                    "paired": 0, "written": 0,
                    "status": "failed", "error_code": code, "error": message})
            else:
                # 窗粒度断点（旧口径）：已写窗的进度照实留在 summary。
                summary.update({
                    "fetched": lib_fetched, "aggregates": lib_aggregates,
                    "paired": lib_paired, "written": lib_written,
                    "status": "failed", "error_code": code, "error": message})
            failed_libraries.append({
                "library_id": lib_id, "library_name": lib_name,
                "code": code, "message": message})
            logger.warning("上游按库聚合单库失败 source_id=%s library=%s "
                           "code=%s", source.id, lib_id, code)
            library_summaries.append(summary)
            _fire_event(on_event,
                        {"type": "library_failed", "summary": summary})
            continue
        summary.update({"fetched": lib_fetched, "aggregates": lib_aggregates,
                        "paired": lib_paired, "written": lib_written})
        library_summaries.append(summary)
        if commit_each_library:
            all_pairs.extend(lib_pairs)
            _fire_event(on_event,
                        {"type": "library_done", "summary": summary})
            try:
                db.commit()
            except IntegrityError:
                # 提交期才暴露的冲突（延后约束）：按单库失败处理，
                # library_done 的进度更新同事务回滚、随失败事件重记。
                db.rollback()
                total_paired -= lib_paired
                total_written -= lib_written
                summary.update({
                    "paired": 0, "written": 0, "status": "failed",
                    "error_code": "mapping_conflict",
                    "error": "映射写入冲突，请重试"})
                del all_pairs[len(all_pairs) - len(lib_pairs):]
                failed_libraries.append({
                    "library_id": lib_id, "library_name": lib_name,
                    "code": "mapping_conflict",
                    "message": "映射写入冲突，请重试"})
                logger.exception("上游按库聚合提交期映射冲突 source_id=%s "
                                 "library=%s", source.id, lib_id)
                _fire_event(on_event,
                            {"type": "library_failed", "summary": summary})
        else:
            _fire_event(on_event,
                        {"type": "library_done", "summary": summary})
    logger.info("上游按库手动聚合 source_id=%s libraries=%s fetched=%s "
                "aggregates=%s pairs=%s written=%s failed_libraries=%s "
                "mapping_enabled=%s",
                source.id, len(libraries), total_fetched, total_aggregates,
                total_paired, total_written, len(failed_libraries),
                mapping_on)
    return {
        "source_id": source.id,
        "fetched": total_fetched,
        "aggregates": total_aggregates,
        "pairs": all_pairs,
        "paired": total_paired,
        "written": total_written,
        "mapping_enabled": mapping_on,
        "unpaired_reasons": unpaired_total,
        "libraries": library_summaries,
        "libraries_discovered": len(libraries),
        "failed_libraries": failed_libraries,
    }

"""上游播放接入（FMBY 学习 #25 25-4：播放接入，播放阶段 7）。

对应魂 3（调度与数据面分离）与魂 6（状态集中维护）：上游播放的
选路、探活、报错全部收进本模块，video_stream 的本地选路一字不动；
上游源的读写仍只走 25-1 的 ``upstream_sources``（本模块只读）。

## 本阶段的范围（诚实边界，先说清楚什么没做）

- 做的：一条显式的上游播放兜底口——给定「已启用的上游源 +
  上游条目 id」，服务端凭源凭据向上游取流地址，先探后 302：
  探到可播才下发 302，探到问题直接回结构化警告，不把死链、
  401、429 原样丢给播放器去猜。
- 不做的：video_stream 本地失败后自动改走上游。自动兜底需要
  「本地条目 → 上游条目」的持久映射（25-2 的聚合目前只是内存
  展示模型，落库映射尚未立项）；没有映射就自动兜底等于按标题
  猜，是虚报。本地优先的口径仍由 25-2 的
  ``select_for_presentation`` 守着（本地在前、上游只补本地没有
  的作品），本口只是那个口径下的兜底目标：客户端在本地变体
  全部不可用时才调本口，服务端不替它悄悄改主意。
- 不做的：苹果 CMS 型（用户已定首批后置，25-1 的类型校验会拒）；
  局域网上游放行（25-1 的 SSRF 守卫仍在，待裁定项不擅自改）。

## 超限结构化警告（本阶段第二件）

上游额度/配额耗尽绝不静默失败、也不笼统 500：探活结果按上游
状态码一一映射成带 code 的结构化错误体（见
:class:`UpstreamPlaybackError` 的 code 表），客户端凭 code 就能
区分「换个源」「等一会儿再试」「找管理员重录凭据」「片子没了」：

- 上游 429 → 429 ``upstream_quota_exhausted``（带 Retry-After，
  上游没给就不带，不编数字）；
- 上游 403 → 403 ``upstream_forbidden``（只报“上游拒绝”，不谎称
  是额度问题——403 也可能是权限问题，原因上游没说清就不猜）；
- 上游 401 → 409 ``upstream_credential_invalid``（凭据失效，管理
  员重录；与 BYOS 绑定失效的 409 口径一致）；
- 上游 404 → 404 ``upstream_item_not_found``（上游已无此条目）；
- 上游连不上/超时/其它 5xx → 502 ``upstream_unavailable``。
- 开关关、源不存在、源未启用同样结构化（404/409），不走 500。

## 开关与回滚

- ``UPSTREAM_PLAYBACK_ENABLED``（默认关）：关时本口返回 404，
  行为上等于该口不存在，本地播放逐字不变，是本阶段唯一回滚手段。
- 凭据纪律：探活与 302 地址里的上游令牌只出现在发往上游的请求
  与播放器的 302 地址里（Emby 直播本身就是这个形态）；日志与
  错误体里只许出现 source_id 与 code，绝不出现令牌、密封值、URL。
- 超时是运维常量（``UPSTREAM_PROBE_TIMEOUT_SEC``），不是业务档位，
  不走 env；兜底口本身就是低频路径（本地优先），一次认证 + 一次
  Range 探活是可接受的成本。

## 苹果 CMS 分支（本批新增，与 Emby/Jellyfin 并列、绝不兜底）

- 分发点：``resolve_upstream_stream`` 在源存在且启用后，按
  ``source.source_type == "applecms"`` 分发；Emby/Jellyfin 分支
  逐字不动。本分支不做「本地失败自动改走苹果 CMS」：苹果 CMS
  与 Emby/Jellyfin 是并列的上游类型，选哪条由源与映射决定，
  不在本函数里互相兜底。
- 门禁（四道，默认全关）：总闸 ``UPSTREAM_PLAYBACK_ENABLED``
  → 苹果 CMS 开关 ``APPLECMS_ENABLED``（关时与总闸关闭行为一致，
  404 upstream_playback_disabled）→ 分级门控 ``APPLECMS_TIER_GATE``
  （关时不查；开且映射未配置时 403，复用既有 upstream_forbidden
  码，见 ``_resolve_applecms_stream``）→ 解析线路开关
  ``APPLECMS_PARSE_LINES_ENABLED``（只影响解析型组的 unplayable
  原因，不放行任何解析）。同步开关 ``UPSTREAM_SYNC_ENABLED``
  默认关且本批无工人消费；映射写开关
  ``UPSTREAM_ITEM_MAPPING_ENABLED`` 只管映射写路径，不 gate 播放读
  路径（它默认关，若 gate 播放则播放恒关，是错的）。
- 取数：``upstream_item_id`` 即 vod_id（映射行记的上游条目 id
  对苹果 CMS 型就是 vod_id）；``episode_index`` 从 1 起，调用方
  未传时默认 1（播第 1 集）。detail 现取现解析，播放地址绝不落库。
- 探活与错误码全复用既有：CMS 接口非 1 → 502
  upstream_unavailable；detail 空列表（vod 消失）→ 404
  upstream_item_not_found；429 → 429；401 → 409
  upstream_credential_invalid、403 → 403 upstream_forbidden；
  直链探活仍走 ``_classify_probe_status``。unplayable 原因映射：
  episode_out_of_range → 404 upstream_item_not_found，其余
  （parse_not_enabled / parse_rule_missing / no_direct_group）
  → 502 upstream_unavailable，message 区分原因，code 不新发明。
- 已知缺口（不在本批文件范围，报协调员）：源类型允许集
  （``upstream_sources.SOURCE_TYPES`` 与模型 CHECK 约束）目前
  只有 emby/jellyfin，苹果 CMS 源行尚建不出来；本分支代码与
  测试已就绪（测试用桩源绕过建源校验），等允许集放行即接通。
  播放口 HTTP 层（``api.py`` 的 upstream_stream 路由）目前未传
  episode 参数，接线时由路由把 ``?episode=`` 透传给本函数。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.emby_server import upstream_sources as us

logger = logging.getLogger(__name__)

#: 上游播放总开关 env 键（默认关）。
UPSTREAM_PLAYBACK_ENV = "UPSTREAM_PLAYBACK_ENABLED"

#: 苹果 CMS 播放开关 env 键（默认关，与总闸是两道门：总闸关则全关，
#: 苹果 CMS 分支还须本开关开才放行）。
APPLECMS_ENABLED_ENV = "APPLECMS_ENABLED"

#: 苹果 CMS 解析线路开关 env 键（默认关）：关时解析型组一律
#: unplayable（parse_not_enabled），不猜解析规则。
APPLECMS_PARSE_LINES_ENV = "APPLECMS_PARSE_LINES_ENABLED"

#: 苹果 CMS 分级门控开关 env 键（默认关）：关时不查分级；
#: 开且分级映射未配置时返回 403（复用既有 upstream_forbidden 码）。
APPLECMS_TIER_GATE_ENV = "APPLECMS_TIER_GATE"

#: 上游同步开关 env 键（默认关）：本批只有读路径（现取现解析），
#: 没有同步工人；本键是未来同步工人复用的门，本模块只定义读取
#: 口，不消费它写任何东西。
APPLECMS_SYNC_ENV = "UPSTREAM_SYNC_ENABLED"

#: 上游探活/认证单次超时（秒，运维常量，见模块 docstring）。
UPSTREAM_PROBE_TIMEOUT_SEC = 10.0


class UpstreamPlaybackError(Exception):
    """上游播放结构化错误：HTTP 状态 + 机器码 + 人话 + 源 id。

    code 表（见模块 docstring 的超限一节）；message 是固定文案，
    绝不拼接上游原始体（原始体可能含内部信息）与任何凭据。
    """

    def __init__(self, status: int, code: str, message: str,
                 source_id: Optional[int] = None,
                 retry_after: Optional[int] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.source_id = source_id
        self.retry_after = retry_after

    def to_body(self) -> dict:
        """错误体：只含 code / message / source_id，retry_after 有才带。"""
        body = {
            "code": self.code,
            "message": self.message,
            "source_id": self.source_id,
        }
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


def playback_enabled() -> bool:
    """上游播放总开关（env，默认关）。每次调用现读，供测试 monkeypatch。"""
    return (str(os.getenv(UPSTREAM_PLAYBACK_ENV, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def _env_on(env_key: str) -> bool:
    """env 开关通用读法（默认关；0/false/no/off/空 = 关）。"""
    return (str(os.getenv(env_key, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def applecms_enabled() -> bool:
    """苹果 CMS 播放开关（env，默认关）。"""
    return _env_on(APPLECMS_ENABLED_ENV)


def applecms_parse_lines_enabled() -> bool:
    """苹果 CMS 解析线路开关（env，默认关）。"""
    return _env_on(APPLECMS_PARSE_LINES_ENV)


def applecms_tier_gate_enabled() -> bool:
    """苹果 CMS 分级门控开关（env，默认关）。"""
    return _env_on(APPLECMS_TIER_GATE_ENV)


def applecms_sync_enabled() -> bool:
    """上游同步开关（env，默认关）。

    本批没有同步工人：本函数只是把门留给未来同步工作复用，
    播放分支绝不因它写库、写映射、起后台任务。
    """
    return _env_on(APPLECMS_SYNC_ENV)


def applecms_tier_configured() -> bool:
    """苹果 CMS 分级映射是否已配置。

    本批无映射（TODO：分级→可播范围的映射由后续批次定义并落
    配置后，本函数才返回 True）。现阶段恒为 False，因此分级门
    控一旦开启，苹果 CMS 播放一律 403——这是 fail-closed，不是 bug。
    """
    # TODO(苹果 CMS 分级映射)：映射定义落地后在此接配置读取。
    return False


def build_upstream_stream_url(base_url: str, upstream_item_id: str, *,
                              api_key: Optional[str] = None,
                              media_source_id: Optional[str] = None) -> str:
    """拼上游 Emby/Jellyfin 直播地址。

    形态与 Emby 直播一致：``{base}/Videos/{id}/stream?static=true``，
    mediaSourceId 原样透传（上游多版本时客户端指定用哪个），api_key
    有才带（匿名源不带）。base_url 在 25-1 建源时已归一化（无尾斜
    杠），这里仍 defensive 地去一次尾斜杠；条目 id 进 path 前 quote，
    media_source_id 与 api_key 走 urlencode，不许原文拼进 URL。
    """
    base = (base_url or "").rstrip("/")
    item = urllib.parse.quote((upstream_item_id or "").strip(), safe="")
    if not item:
        raise ValueError("上游条目 id 不能为空")
    params = [("static", "true")]
    if (media_source_id or "").strip():
        params.append(("mediaSourceId", media_source_id.strip()))
    if api_key:
        params.append(("api_key", api_key))
    return f"{base}/Videos/{item}/stream?{urllib.parse.urlencode(params)}"


def _source_headers(source) -> dict:
    """发往上游的请求头：源上配的 UA / Referer / 附加头（25-1 字段）。

    附加头在 25-1 已校验为「键值均为字符串」的对象；脏数据按空处理，
    不炸探活。凭据绝不走这里——凭据只走 api_key 参数或认证口。
    """
    headers: dict = {"Accept": "*/*"}
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


def _default_http_post(url: str, payload: dict, headers: dict,
                       timeout: float):
    """默认认证 POST 层（urllib，与 25-3 的 _default_http_get 同款）。

    返回 (status:int|None, body:dict|None)：HTTPError 带状态码，
    超时/断连返回 (None, None)。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST")
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


def _default_http_range_probe(url: str, headers: dict, timeout: float):
    """默认 Range 探活层（urllib）：只读 1 字节，不拉整条流。

    返回 (status:int|None, retry_after:int|None)：HTTPError 带状态码
    与 Retry-After（能转 int 才带，转不了按没有计，不编数字）；
    超时/断连返回 (None, None)。
    """
    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, headers=probe_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, None
    except urllib.error.HTTPError as exc:
        retry_after = None
        raw = exc.headers.get("Retry-After") if exc.headers else None
        if raw is not None:
            try:
                retry_after = int(str(raw).strip())
            except (TypeError, ValueError):
                retry_after = None
        return exc.code, retry_after
    except Exception:
        return None, None


def _auth_token_for_source(source, *,
                           http_post: Optional[Callable] = None) -> Optional[str]:
    """取上游播放凭据（25-1 凭据模型的唯一消费点之一）。

    - 未配凭据 → None（匿名访问，探活与 302 都不带 api_key）；
    - api_key 形态 → 密封值本身就是 key，直接返回；
    - password 形态 → 向上游认证口换 AccessToken（Emby/Jellyfin
      标准 ``/Users/AuthenticateByName``），换不到按凭据失效/上游
      不可用映射（见内联注释），绝不降级成匿名。
    密封解不开抛 SealedSecretError，调用方按凭据失效处理。
    """
    cred = us.load_credential(source)
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
            'MediaBrowser Client="Aetrix", Device="upstream-playback", '
            'DeviceId="upstream-playback", Version="1.0"'
        )
        status, body = post(
            f"{base}/Users/AuthenticateByName",
            {"Username": cred["username"], "Pw": cred["secret"]},
            headers, UPSTREAM_PROBE_TIMEOUT_SEC)
        if status == 200 and isinstance(body, dict) and body.get("AccessToken"):
            return str(body["AccessToken"])
        if status in (401, 403):
            raise UpstreamPlaybackError(
                409, "upstream_credential_invalid",
                "上游凭据失效，请管理员重录该源凭据",
                source_id=source.id)
        raise UpstreamPlaybackError(
            502, "upstream_unavailable",
            "上游服务当前不可用，请稍后再试",
            source_id=source.id)
    raise UpstreamPlaybackError(
        502, "upstream_unavailable",
        "上游服务当前不可用，请稍后再试",
        source_id=source.id)


def _classify_probe_status(status: Optional[int], source_id: int,
                           retry_after: Optional[int]) -> None:
    """把探活状态码映射成结构化错误；可播（200/206）返回 None。

    映射口径见模块 docstring 的超限一节；其它 5xx 与 None（连不上/
    超时）一律 upstream_unavailable，不把上游内部状态原样透传。
    """
    if status in (200, 206):
        return None
    if status == 429:
        raise UpstreamPlaybackError(
            429, "upstream_quota_exhausted",
            "上游额度已耗尽，请稍后再试或换个源",
            source_id=source_id, retry_after=retry_after)
    if status == 403:
        raise UpstreamPlaybackError(
            403, "upstream_forbidden",
            "上游拒绝了本次播放，请检查该源权限",
            source_id=source_id)
    if status == 401:
        raise UpstreamPlaybackError(
            409, "upstream_credential_invalid",
            "上游凭据失效，请管理员重录该源凭据",
            source_id=source_id)
    if status == 404:
        raise UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目",
            source_id=source_id)
    raise UpstreamPlaybackError(
        502, "upstream_unavailable",
        "上游服务当前不可用，请稍后再试",
        source_id=source_id)


def _map_applecms_error(exc, source_id: int) -> UpstreamPlaybackError:
    """把 ``AppleCMSError`` 按种类映射成既有结构化错误（不发明新码）。

    - rate_limited → 429 upstream_quota_exhausted（CMS 侧未透传
      Retry-After，本批不带该字段，不编数字）；
    - unauthorized：401 → 409 upstream_credential_invalid，
      403 → 403 upstream_forbidden（与既有探活同义映射一致）；
    - not_found → 404 upstream_item_not_found；
    - network / http_error / bad_response / bad_code → 502
      upstream_unavailable（CMS 接口非 1 即属此类）。
    """
    kind = getattr(exc, "kind", None)
    status = getattr(exc, "http_status", None)
    if kind == "rate_limited":
        return UpstreamPlaybackError(
            429, "upstream_quota_exhausted",
            "上游额度已耗尽，请稍后再试或换个源",
            source_id=source_id)
    if kind == "unauthorized":
        if status == 401:
            return UpstreamPlaybackError(
                409, "upstream_credential_invalid",
                "上游凭据失效，请管理员重录该源凭据",
                source_id=source_id)
        return UpstreamPlaybackError(
            403, "upstream_forbidden",
            "上游拒绝了本次播放，请检查该源权限",
            source_id=source_id)
    if kind == "not_found":
        return UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目",
            source_id=source_id)
    return UpstreamPlaybackError(
        502, "upstream_unavailable",
        "上游服务当前不可用，请稍后再试",
        source_id=source_id)


def _resolve_applecms_stream(source, vod_id: str, *,
                             episode_index: int = 1,
                             http_get: Optional[Callable] = None,
                             http_probe: Optional[Callable] = None) -> str:
    """苹果 CMS 播放分支：现取现解析、先探后 302，绝不落库。

    门禁顺序：APPLECMS_ENABLED 关 → 404 upstream_playback_disabled
    （与总闸关闭行为一致）；分级门控开且映射未配置 → 403
    upstream_forbidden（文件里唯一的 403 码，复用；其标准语义是
    「上游拒绝」，此处是本地分级未配置的拒绝，message 已区分，
    若协调员要为分级单独立码，换这里一处即可）。

    vod_id 即调用方传入的 ``upstream_item_id``（映射行里记的上游
    条目 id 对苹果 CMS 型就是 vod_id）；``episode_index`` 从 1 起，
    默认 1（调用方未传集序号时播第 1 集）。detail 现取现解析，
    播放地址只出现在返回的 302 地址与探活请求里，不写库、不进日志
    （日志只含 source_id）。CMS 密封凭据不走 Emby 认证口：
    本分支只用源上配的 UA / Referer / 附加头（``_source_headers``）
    发请求；CMS 若需密钥，走附加头配置，不走密封凭据。
    """
    if not applecms_enabled():
        raise UpstreamPlaybackError(
            404, "upstream_playback_disabled",
            "上游播放未开启", source_id=source.id)
    if applecms_tier_gate_enabled() and not applecms_tier_configured():
        raise UpstreamPlaybackError(
            403, "upstream_forbidden",
            "该源分级映射未配置，苹果 CMS 播放暂不可用",
            source_id=source.id)
    from backend.emby_server import applecms_fetcher as acf
    headers = _source_headers(source)
    try:
        detail = acf.fetch_detail(
            source.base_url, vod_id, http_get=http_get, headers=headers)
    except acf.AppleCMSError as exc:
        raise _map_applecms_error(exc, source.id) from exc
    if detail is None:
        raise UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目", source_id=source.id)
    try:
        episode = int(episode_index)
    except (TypeError, ValueError):
        episode = -1
    result = acf.resolve_play(
        detail, episode,
        parse_lines_enabled=applecms_parse_lines_enabled())
    if result[0] != "ok":
        reason = result[1]
        if reason == "episode_out_of_range":
            raise UpstreamPlaybackError(
                404, "upstream_item_not_found",
                "上游无此集", source_id=source.id)
        messages = {
            "parse_not_enabled": "解析型线路未开启，该条目暂无直链可播",
            "parse_rule_missing": "本站无解析规则，该条目暂无直链可播",
            "no_direct_group": "该条目暂无直链可播",
        }
        raise UpstreamPlaybackError(
            502, "upstream_unavailable",
            messages.get(reason, "该条目暂无直链可播"),
            source_id=source.id)
    _, _group_name, address = result
    probe = _default_http_range_probe if http_probe is None else http_probe
    probe_status, retry_after = probe(
        address, _source_headers(source), UPSTREAM_PROBE_TIMEOUT_SEC)
    _classify_probe_status(probe_status, source.id, retry_after)
    logger.info("上游播放解析成功 source_id=%s", source.id)
    return address


def resolve_upstream_stream(db: Session, source_id: int,
                            upstream_item_id: str, *,
                            media_source_id: Optional[str] = None,
                            http_probe: Optional[Callable] = None,
                            http_post: Optional[Callable] = None,
                            episode_index: int = 1,
                            http_get: Optional[Callable] = None) -> str:
    """上游播放解析：查源 → 取凭据 → 拼地址 → Range 探活 → 返回 302 地址。

    每一步失败都以 UpstreamPlaybackError 结构化返回，绝不返回死链、
    绝不静默回落其它源（换源是客户端拿着 code 自己决定的事，全站
    一个规矩：本地优先、上游兜底，不在本函数里替客户端挑第二个源）。
    关着的源、不存在的源、开关关，一律先失败，不发任何上游请求。
    """
    if not playback_enabled():
        raise UpstreamPlaybackError(
            404, "upstream_playback_disabled",
            "上游播放未开启", source_id=source_id)
    item_id = (upstream_item_id or "").strip()
    if not item_id:
        raise UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目", source_id=source_id)
    source = us.get_source(db, source_id)
    if source is None:
        raise UpstreamPlaybackError(
            404, "upstream_source_not_found",
            "上游源不存在", source_id=source_id)
    if not source.enabled:
        raise UpstreamPlaybackError(
            409, "upstream_source_disabled",
            "该上游源未启用", source_id=source_id)
    if getattr(source, "source_type", None) == "applecms":
        return _resolve_applecms_stream(
            source, item_id, episode_index=episode_index,
            http_get=http_get, http_probe=http_probe)
    try:
        token = _auth_token_for_source(source, http_post=http_post)
    except us.SealedSecretError as exc:
        raise UpstreamPlaybackError(
            409, "upstream_credential_invalid",
            "上游凭据失效，请管理员重录该源凭据",
            source_id=source_id) from exc
    url = build_upstream_stream_url(
        source.base_url, item_id, api_key=token,
        media_source_id=media_source_id)
    probe = _default_http_range_probe if http_probe is None else http_probe
    status, retry_after = probe(url, _source_headers(source),
                                UPSTREAM_PROBE_TIMEOUT_SEC)
    _classify_probe_status(status, source.id, retry_after)
    logger.info("上游播放解析成功 source_id=%s", source.id)
    return url

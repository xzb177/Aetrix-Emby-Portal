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


def resolve_upstream_stream(db: Session, source_id: int,
                            upstream_item_id: str, *,
                            media_source_id: Optional[str] = None,
                            http_probe: Optional[Callable] = None,
                            http_post: Optional[Callable] = None) -> str:
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

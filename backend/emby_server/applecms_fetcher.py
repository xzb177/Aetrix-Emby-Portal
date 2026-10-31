# -*- coding: utf-8 -*-
"""苹果 CMS provide 接口客户端（FMBY 学习 #25 苹果 CMS 型，并列口径）。

定位（先说清楚边界）：

- 本模块只做「取回 + 解析」，不做落库、不做同步、不写任何映射行：
  播放时现取现解析（见 ``upstream_playback`` 的苹果 CMS 分支），
  播放地址绝不落库；同步工人本批不存在（``UPSTREAM_SYNC_ENABLED``
  默认关，见 ``upstream_playback.applecms_sync_enabled``）。
- 本模块不内置、不预置任何资源站地址：``base_url`` 一律由调用方
  传入（生产上即管理员在源管理里配的上游源地址），这里只拼
  provide 协议路径 ``/api.php/provide/vod/``。
- 绝不真实联网（除非调用方显式不传 ``http_get`` 时用默认实现）：
  ``http_get`` 必须可注入，签名 ``http_get(url, headers, timeout)``
  返回 ``(status, payload)``——``status`` 为 ``None`` 表示连不上/
  超时，``payload`` 为解析后的 JSON 对象（解析失败为 ``None``）。
  模块与测试一律不许真实联网：测试只传桩。
- 日志与错误纪律：本模块不打任何含地址的日志；异常只带种类
  （``kind``）、HTTP 状态与 CMS 顶层 ``code``，绝不带播放地址、
  凭据、附加头原文。

AppleCMS provide 返回形态（调用方约定）：

- 顶层 ``{"code": 1, "msg": ..., "page": n, "pagecount": m,
  "limit": k, "total": t, "list": [vod, ...]}``；``code == 1`` 才算成功。
- vod 播放字段：``vod_play_from`` 以 ``$$$`` 分组，
  ``vod_play_url`` 以 ``$$$`` 分组、组内以 ``#`` 分集、
  每集 ``标题$地址``。

错误种类（``AppleCMSError.kind``，调用方按种类映射，见
``upstream_playback`` 的映射表）：

- ``"network"``：连不上 / 超时 / 桩抛异常；
- ``"rate_limited"``：HTTP 429；
- ``"unauthorized"``：HTTP 401 / 403（调用方再按 401/403 同义映射）；
- ``"not_found"``：HTTP 404；
- ``"http_error"``：其它非 200 HTTP 状态；
- ``"bad_response"``：体不是 JSON 对象；
- ``"bad_code"``：顶层 ``code != 1``（``cms_code`` 带原值）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from backend.emby_server import upstream_http

#: provide 协议路径（协议本身，不是资源站地址；站地址只来自 base_url）。
PROVIDE_PATH = "/api.php/provide/vod/"

#: 默认单次请求超时（秒，运维常量，不走 env）。
CMS_HTTP_TIMEOUT_SEC = 10.0

#: 分页安全阀：pagecount/limit 缺失时最多翻这么多页，防止桩/异常
#: 服务端无限吐页导致死循环。
DEFAULT_MAX_PAGES = 500

VALID_ACS = ("list", "detail", "search")


class AppleCMSError(Exception):
    """苹果 CMS 取回失败：种类 + HTTP 状态 + CMS code，不带任何地址。

    ``message`` 是固定文案，绝不拼接 URL、播放地址或上游原始体。
    """

    def __init__(self, kind: str, message: str,
                 http_status: Optional[int] = None,
                 cms_code=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status
        self.cms_code = cms_code


def _default_cms_http_get(url: str, headers: dict, timeout: float):
    """默认 HTTP GET 层（SSRF 守卫版）：只在调用方不注入时用。

    走 ``upstream_http.safe_urlopen``（审计 A-119，与 A-053 三路同款）：
    发请求前解析复验目标主机（私网/回环一律拦截）、重定向一律不跟随。
    守卫拦截（``OutboundBlockedError``）属 ``Exception``，走超时/断连
    同款 ``(None, None)`` 口径。

    行为变化（与 A-053 同款，须在 PR 说明里明示）：原来依赖 CMS
    302 跳转的源，今后不再跟随跳转，改为失败口径。

    返回 ``(status, payload)``：HTTPError 带状态码与体（体解析失败
    为 None）；超时/断连/守卫拦截返回 ``(None, None)``。注意：本函数
    不打日志，URL 绝不进任何日志或异常。
    """
    req = urllib.request.Request(url, headers=dict(headers or {}),
                                 method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            try:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            payload = None
        return exc.code, payload
    except Exception:
        return None, None


def _build_provide_url(base_url: str, params: dict) -> str:
    """拼 provide 请求地址：base_url 去尾斜杠 + 协议路径 + 参数。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("苹果 CMS base_url 不能为空")
    query = urllib.parse.urlencode(
        {key: value for key, value in params.items() if value is not None})
    return f"{base}{PROVIDE_PATH}?{query}"


def fetch_page(base_url: str, ac: str = "list", pg: int = 1,
               t=None, h=None, wd=None, ids=None,
               http_get: Optional[Callable] = None,
               headers: Optional[dict] = None) -> dict:
    """取 provide 一页，返回顶层 payload dict。

    ``ac`` 只许 ``list`` / ``detail`` / ``search``，其它值直接
    ``ValueError``（调用方传错参数，不是上游故障，不包进
    ``AppleCMSError``）。顶层 ``code == 1``（容忍字符串 ``"1"``）
    才算成功，否则按种类抛 ``AppleCMSError``。
    """
    if ac not in VALID_ACS:
        raise ValueError(f"不支持的 ac={ac!r}，只许 list/detail/search")
    get = _default_cms_http_get if http_get is None else http_get
    params = {"ac": ac, "pg": pg}
    if t is not None:
        params["t"] = t
    if h is not None:
        params["h"] = h
    if wd is not None:
        params["wd"] = wd
    if ids is not None:
        params["ids"] = ids
    url = _build_provide_url(base_url, params)
    try:
        status, payload = get(url, dict(headers or {}),
                              CMS_HTTP_TIMEOUT_SEC)
    except Exception as exc:
        raise AppleCMSError(
            "network", "苹果 CMS 接口连不上，请稍后再试") from exc
    if status is None:
        raise AppleCMSError(
            "network", "苹果 CMS 接口连不上，请稍后再试")
    if status == 429:
        raise AppleCMSError(
            "rate_limited", "苹果 CMS 接口限流，请稍后再试",
            http_status=429)
    if status in (401, 403):
        raise AppleCMSError(
            "unauthorized", "苹果 CMS 接口拒绝了本次请求",
            http_status=status)
    if status == 404:
        raise AppleCMSError(
            "not_found", "苹果 CMS 接口不存在该资源",
            http_status=404)
    if status != 200:
        raise AppleCMSError(
            "http_error", "苹果 CMS 接口当前不可用，请稍后再试",
            http_status=status)
    if not isinstance(payload, dict):
        raise AppleCMSError(
            "bad_response", "苹果 CMS 接口返回了无法解析的内容")
    code = payload.get("code")
    if code not in (1, "1"):
        raise AppleCMSError(
            "bad_code", "苹果 CMS 接口返回了异常状态",
            http_status=status, cms_code=code)
    return payload


def iter_pages(base_url: str, ac: str = "list",
               http_get: Optional[Callable] = None,
               headers: Optional[dict] = None,
               max_pages: int = DEFAULT_MAX_PAGES, **filters) -> object:
    """分页循环生成器：逐页取回，逐条产出 vod dict。

    停页条件（任一即停）：本页 ``list`` 为空；已知 ``pagecount``
    且已到最后一页；已知 ``limit`` 且本页条数不足一页；
    达到 ``max_pages`` 安全阀。``filters`` 原样透传给 ``fetch_page``
   （``t`` / ``h`` / ``wd`` / ``ids`` 等）。
    """
    pg = 1
    while pg <= max_pages:
        payload = fetch_page(base_url, ac=ac, pg=pg, http_get=http_get,
                             headers=headers, **filters)
        items = payload.get("list") or []
        if not isinstance(items, list):
            return
        for item in items:
            yield item
        if not items:
            return
        pagecount = payload.get("pagecount")
        try:
            pagecount = int(pagecount) if pagecount is not None else None
        except (TypeError, ValueError):
            pagecount = None
        if pagecount is not None and pg >= pagecount:
            return
        limit = payload.get("limit")
        try:
            limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit = None
        if limit is not None and len(items) < limit:
            return
        pg += 1


def fetch_detail(base_url: str, vod_id, *,
                 http_get: Optional[Callable] = None,
                 headers: Optional[dict] = None) -> Optional[dict]:
    """取单条 vod 详情：``ac=detail&ids=<vod_id>`` 的首条。

    返回 vod dict；``list`` 为空表示该 vod 已消失，返回 ``None``
   （调用方按 404 处理），不抛异常。
    """
    vod_id = ("" if vod_id is None else str(vod_id)).strip()
    if not vod_id:
        raise ValueError("vod_id 不能为空")
    payload = fetch_page(base_url, ac="detail", ids=vod_id,
                         http_get=http_get, headers=headers)
    items = payload.get("list") or []
    if not isinstance(items, list) or not items:
        return None
    return items[0]


def parse_play_groups(vod_play_from, vod_play_url) -> list:
    """解析播放分组：``$$$`` 分组、``#`` 分集、``$`` 标题$地址。

    返回 ``[{"name": 组名, "episodes": [{"title": 标题,
    "address": 地址}, ...]}, ...]``。``vod_play_from`` 为空时组名
    fallback 为 ``线路N``（N 从 1 起）；``from`` 组数少于 ``url``
    组数时多余组同样 fallback；空集串、空地址一律跳过，不产空集。
    """
    from_text = vod_play_from if isinstance(vod_play_from, str) else ""
    url_text = vod_play_url if isinstance(vod_play_url, str) else ""
    url_groups = url_text.split("$$$") if url_text.strip() else []
    from_groups = from_text.split("$$$") if from_text.strip() else []
    groups = []
    for index, url_group in enumerate(url_groups):
        name = (from_groups[index].strip() if index < len(from_groups)
                and from_groups[index].strip()
                else f"线路{index + 1}")
        episodes = []
        for raw_episode in url_group.split("#"):
            raw_episode = raw_episode.strip()
            if not raw_episode:
                continue
            if "$" in raw_episode:
                title, address = raw_episode.split("$", 1)
                title, address = title.strip(), address.strip()
            else:
                title, address = "", raw_episode
            if not address:
                continue
            episodes.append({"title": title, "address": address})
        if episodes:
            groups.append({"name": name, "episodes": episodes})
    return groups


def classify_url(url) -> str:
    """给单个播放地址定性：``direct`` / ``parse`` / ``unknown``。

    - ``direct``：http(s) 且路径后缀为 ``.m3u8`` / ``.mp4``
      （query/fragment 不影响后缀判断）；
    - ``unknown``：空地址或无法解析；
    - ``parse``：其余一切非空地址（直链后缀之外的 http(s) 地址、
      或非 http(s) 的解析型标识），即需要解析线路才能播的地址。
    """
    if not isinstance(url, str):
        return "unknown"
    text = url.strip()
    if not text:
        return "unknown"
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return "unknown"
    if parsed.scheme.lower() in ("http", "https"):
        path = (parsed.path or "").lower()
        if path.endswith(".m3u8") or path.endswith(".mp4"):
            return "direct"
        return "parse"
    return "parse" if parsed.path or parsed.scheme else "unknown"


def pick_direct_group(groups) -> Optional[dict]:
    """首个全部地址均为 direct 的组；没有则 ``None``。

    空组不算（组内至少 1 集且全部 direct 才算直链组）。
    """
    for group in groups or []:
        episodes = group.get("episodes") or []
        if episodes and all(
                classify_url(ep.get("address")) == "direct"
                for ep in episodes):
            return group
    return None


def episode_address(group, episode_index) -> Optional[str]:
    """取某组第 ``episode_index`` 集的地址，``episode_index`` 从 1 起。

    越界（<1 或超集数）返回 ``None``，不抛异常。
    """
    if not isinstance(group, dict):
        return None
    try:
        index = int(episode_index)
    except (TypeError, ValueError):
        return None
    episodes = group.get("episodes") or []
    if index < 1 or index > len(episodes):
        return None
    return episodes[index - 1].get("address")


def resolve_play(vod_detail, episode_index,
                 parse_lines_enabled: bool = False,
                 parse_resolver: Optional[Callable] = None):
    """从 vod 详情里选播放地址（只解析，不发任何请求、不落库）。

    返回 ``("ok", 组名, 地址)`` 或 ``("unplayable", 原因)``，原因取值：

    - ``"episode_out_of_range"``：直链组存在，但该集序号越界；
    - ``"parse_not_enabled"``：只有解析型组，解析线路开关关着；
    - ``"parse_rule_missing"``：解析线路开关开着，但本站无解析规则
      （未注入 ``parse_resolver``，或解析器对该集无可用结果）；
    - ``"no_direct_group"``：无直链组、也无解析型组（无播放信息、
      地址全 unknown/空）。

    逐站解析（后续四件之 ③）：开关关时行为与此前逐字一致
    （解析型组一律 ``parse_not_enabled``，绝不调解析器）；开关开
    时，``parse_resolver`` 是调用方按站/线路注入的解析函数，签名
    ``parse_resolver(address, group_name) -> Optional[str]``——
    ``address`` 是该集的解析型地址，``group_name`` 是 CMS 给的线路
    名（数据驱动，调用方可按它分站派发，本模块不 hardcode 任何
    站名与规则）。解析器只对请求的这一集调一次；返回的地址必须
    经 ``classify_url`` 判定为 direct 才算解析成功（返回解析型/
    空地址都按无规则处理），成功结果走既有 ``("ok", …)`` 口径，
    下游探活/302 口径与直链组完全一致，不另起播放路径。
    """
    if not isinstance(vod_detail, dict):
        return ("unplayable", "no_direct_group")
    groups = parse_play_groups(vod_detail.get("vod_play_from"),
                               vod_detail.get("vod_play_url"))
    direct = pick_direct_group(groups)
    if direct is not None:
        address = episode_address(direct, episode_index)
        if address is None:
            return ("unplayable", "episode_out_of_range")
        return ("ok", direct["name"], address)
    has_parse = any(
        classify_url(ep.get("address")) == "parse"
        for group in groups for ep in group.get("episodes") or [])
    if has_parse:
        if not parse_lines_enabled:
            return ("unplayable", "parse_not_enabled")
        if parse_resolver is not None:
            for group in groups:
                address = episode_address(group, episode_index)
                if address is None:
                    continue
                if classify_url(address) != "parse":
                    continue
                try:
                    resolved = parse_resolver(address, group["name"])
                except Exception:
                    continue
                if isinstance(resolved, str) and resolved.strip() \
                        and classify_url(resolved) == "direct":
                    return ("ok", group["name"], resolved.strip())
        return ("unplayable", "parse_rule_missing")
    return ("unplayable", "no_direct_group")

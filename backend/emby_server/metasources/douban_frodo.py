"""豆瓣 Frodo 桥：IMDb 编号对齐豆瓣条目 + Frodo 详情文案（FMBY 学习批二 #14）

## 学的是什么（魂 1：身份与证据先行）

FMBY 的豆瓣 provider 不走网页 ``subject_suggest``（我方实测：豆瓣 suggest
从机房 IP 对任何查询都返回空），走移动端 Frodo 接口
（``frodo.douban.com``，备用域名 ``m.douban.com/rexxar``），并且用
**IMDb 编号在 TMDB 与豆瓣之间搭桥**（二进制里有
``api.douban.com/v2/movie/imdb/`` 字样）。桥的本质是身份对齐：先用 IMDb
编号把两边的条目精确认亲，再谈搬文案——而不是拿片名模糊搜去撞运气。

本模块三件事：

1. **IMDb 桥（主路）**：``find_by_imdb`` 用条目的 ``imdb_id`` 经 v2 的
   imdb 端点精确换到豆瓣 subject id，再用 Frodo 详情补齐文案
   （简介 / 评分 / 海报 / 类型）。编号对不上（年份都已知且不一致）
   直接判没找到，绝不退化成片名乱认。
2. **Frodo 片名搜（兜底）**：``search_by_title`` 只在没有 IMDb 编号时用，
   候选必须过与多源补缺同一道置信门槛（标题归一化全等 / 互为前缀 +
   年份都已知时必须相等），过不了就当没搜到。
3. **熔断隔离**：全部请求在这一层喂统一熔断注册表（``breaker``）的
   ``douban_frodo`` 源名——403/429 立即开断、冷却后半开单探测，与
   suggest 腿（``douban`` 源名）分开记账：Frodo 腿被豆瓣拒只熔断它
   自己，TMDB 主链与 suggest 腿都不受牵连（魂 1 的另一面：失败要
   隔离在本腿内）。

## 前提（已知常态，不是 bug）

Frodo 与 v2 端点从机房 IP 大概率吃 403（FMBY 在机房实测同样 403）：
本模块交付的是「接口 + 桥 + 熔断隔离」的完整能力，豆瓣腿在当前出口
下尽力而为；403 会被熔断挡下、冷却后自动半开重试，不会逐条硬撞，
更不会拖慢 TMDB 主链（enrich 的多源采集对单源失败本来就隔离）。

## 与既有豆瓣腿的分工

- ``altmeta.search``（suggest）：只给标题 / 年份 / 海报，没有简介评分；
- ``douban.py``（subject 页 JSON-LD）：主客户端，自带封禁退避；
- 本模块：给多源引擎的豆瓣源做 Frodo 兜底（suggest 没命中时补上，
  且能带回简介 / 评分），并给引擎提供 IMDb → 豆瓣的桥（见
  ``engine._relay_douban``）。引擎里的自动采集仍受豆瓣总开关与全局
  封禁期两道既有闸门管辖（封禁期零豆瓣请求，本模块不另开口子）。

## 缓存（复用批一 #8 / #1203 的 provider 缓存口径）

详情与桥接的**成功**响应走 ``tmdb_cache`` 的磁盘信封缓存（与 TMDB
同一套实现：信封 ``{at, v, hits, raw}``——``v`` 是归一化载荷、
``raw`` 是上游原始响应、``hits`` 是命中计数），以独立 endpoint 名
（``douban_frodo_subject`` / ``douban_frodo_imdb``）分键，不与 TMDB
条目混杂；TTL 沿用 details 口径（30 天）。失败不落盘——缓存绝不能
把「暂时被拒」固化成「没有这部片」。片名搜不落盘（搜索语义，且
阴性结果在机房出口下大概率是 403 造成的假阴性）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from backend.emby_server.metasources import breaker as provider_breaker

logger = logging.getLogger(__name__)

#: 熔断注册表里的源名（与 suggest 腿的 ``douban`` 分开记账）
PROVIDER = "douban_frodo"

FRODO_BASE = "https://frodo.douban.com/api/v2"
#: Frodo 的备用同构域名（FMBY 二进制里的 ``douban-rexxar`` 字样）：
#: 主域名请求失败时按同一路径试一次，成败各自喂熔断。
REXXAR_BASE = "https://m.douban.com/rexxar/api/v2"
#: IMDb → 豆瓣的桥端点（FMBY 二进制原文）：v2 的 imdb 精确查询
V2_IMDB_URL = "https://api.douban.com/v2/movie/imdb/{imdb_id}"

#: Frodo 客户端的公开 app key（豆瓣 App 内置、开源客户端通用）；
#: 可用 env ``DOUBAN_FRODO_APIKEY`` 覆盖成自备 key。
_DEFAULT_APIKEY = "0dad551ec0f84ed0290468895f21142c"  # secret-scan: allow — 豆瓣官方 App 内置的公开 apikey（随 App 安装包分发、非我方密钥），可用 env DOUBAN_FRODO_APIKEY 覆盖
# Frodo 对 UA 敏感：用移动端 UA（可 env 覆盖）
_DEFAULT_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")

REQUEST_TIMEOUT = 10

# 豆瓣封禁页标记（200 也可能是封禁页，与 douban.py 同口径的三个标记）
_BAN_MARKERS = ("sec.douban.com", "检测到有异常请求", "<title>禁止访问</title>")

_CACHE_SUBJECT_ENDPOINT = "douban_frodo_subject"
_CACHE_IMDB_ENDPOINT = "douban_frodo_imdb"


def _cfg_apikey() -> str:
    return (os.getenv("DOUBAN_FRODO_APIKEY", "").strip() or _DEFAULT_APIKEY)


def _cfg_ua() -> str:
    return os.getenv("DOUBAN_FRODO_UA", "").strip() or _DEFAULT_UA


def _cfg_min_interval() -> float:
    """两次 Frodo 请求的最小间隔秒数（``DOUBAN_FRODO_MIN_INTERVAL``，默认 1.0）。"""
    try:
        return max(0.0, float(os.getenv("DOUBAN_FRODO_MIN_INTERVAL", "1.0")))
    except (TypeError, ValueError):
        return 1.0


class _RateLimiter:
    """最小间隔限速（与 altmeta 同语义：两次请求至少隔 N 秒）。"""

    def __init__(self) -> None:
        self._last: float = 0.0
        self._lock = threading.Lock()

    def acquire(self, min_interval: float) -> None:
        if min_interval <= 0:
            with self._lock:
                self._last = time.time()
            return
        while True:
            with self._lock:
                now = time.time()
                wait = self._last + min_interval - now if self._last else 0.0
                if wait <= 0:
                    self._last = now
                    return
            time.sleep(min(wait, 5.0))


_limiter = _RateLimiter()


def _status_of(exc: Exception) -> Optional[int]:
    """urllib 异常 → HTTP 状态码（熔断认 403/429 用）；非 HTTP 失败给 None。"""
    return exc.code if isinstance(exc, urllib.error.HTTPError) else None


def _request_json(url: str):
    """发一个豆瓣请求并解析 JSON；失败返回 None（异常不在这一层往上抛）。

    成败在本层喂 ``breaker`` 的 ``douban_frodo`` 源（与 altmeta 喂
    ``douban`` / ``bangumi`` 的口径一致：本模块的公开函数一律把失败
    吞成 None，上层分不清「没搜到」与「源坏了」，只能在这里记账）。
    熔断开着时直接返回 None、不发请求——豆瓣腿被拒后靠冷却 + 半开
    探测自愈，不逐条硬撞。
    """
    if not provider_breaker.allow(PROVIDER):
        logger.debug("[douban-frodo] 熔断中，跳过请求 %s", url)
        return None
    _limiter.acquire(_cfg_min_interval())
    req = urllib.request.Request(url, headers={
        "User-Agent": _cfg_ua(),
        "Referer": "https://m.douban.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 — 网络/反爬失败不应中断补全
        provider_breaker.record_failure(
            PROVIDER, status=_status_of(exc), error=str(exc))
        logger.debug("[douban-frodo] 请求失败 %s: %s", url, exc)
        return None
    if any(marker in body for marker in _BAN_MARKERS):
        # 200 + 封禁页：按 403 记账（立即熔断），别把封禁页当数据解析
        provider_breaker.record_failure(
            PROVIDER, status=403, error="豆瓣封禁页（Frodo）")
        logger.debug("[douban-frodo] 命中封禁页 %s", url)
        return None
    try:
        data = json.loads(body)
    except ValueError:
        provider_breaker.record_failure(PROVIDER, error="返回的不是 JSON")
        logger.debug("[douban-frodo] 返回不是 JSON %s", url)
        return None
    provider_breaker.record_success(PROVIDER)   # 拿到合法响应 = 源活着
    return data


def _frodo_url(base: str, path: str, params: Optional[dict] = None) -> str:
    query = {"apikey": _cfg_apikey()}
    query.update(params or {})
    return f"{base}{path}?{urllib.parse.urlencode(query)}"


def _get_with_fallback(path: str, params: Optional[dict] = None):
    """Frodo 主域名 → rexxar 备用域名，同一路径试一轮；都失败返回 None。

    主域名若是被 403 熔断的，备用域名的尝试会被 ``breaker.allow``
    直接挡下（同一个源名记账），不会真的再发一发去撞。
    """
    data = _request_json(_frodo_url(FRODO_BASE, path, params))
    if data is not None:
        return data
    return _request_json(_frodo_url(REXXAR_BASE, path, params))


# ---------------------------------------------------------------------------
# 归一化：Frodo / v2 的 subject 形状 → 与 altmeta 同形状的 dict
# ---------------------------------------------------------------------------

def _rating_of(raw) -> Optional[float]:
    from backend.emby_server.metasources.sources import _number

    if isinstance(raw, dict):
        return _number(raw.get("average") or raw.get("value"))
    return _number(raw)


def _normalize_subject(raw: dict, subtype: str = "") -> dict:
    """豆瓣 subject（Frodo 详情 / Frodo 搜索 target / v2 响应）→ 归一化 dict。

    字段口径与 ``altmeta.search`` 的返回对齐并扩充简介 / 评分 / 类型
    （suggest 给不了的那几项，正是 Frodo 补上的价值）：
    ``{id, title, original_title, overview, rating, year, image, genres,
    subtype}``。取不到就留空——宁可空着，也不拿不可靠的来源填。
    """
    from backend.emby_server.metasources.sources import _year_of

    pic = raw.get("pic") if isinstance(raw.get("pic"), dict) else {}
    images = raw.get("images") if isinstance(raw.get("images"), dict) else {}
    image = (pic.get("large") or pic.get("normal")
             or raw.get("cover_url")
             or images.get("large") or images.get("medium") or "")
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "original_title": str(raw.get("original_title") or ""),
        # Frodo 详情是 intro；v2 是 summary；搜索 target 偶有 abstract
        "overview": str(raw.get("intro") or raw.get("summary")
                        or raw.get("abstract") or ""),
        "rating": _rating_of(raw.get("rating")),
        "year": _year_of(raw.get("year")),
        "image": str(image or ""),
        "genres": [str(g) for g in (raw.get("genres") or []) if g],
        "subtype": str(subtype or raw.get("subtype") or ""),
    }


def _subtype_for_kind(kind: str) -> str:
    return "movie" if kind == "movie" else "tv"


# ---------------------------------------------------------------------------
# Frodo 详情（带磁盘信封缓存）
# ---------------------------------------------------------------------------

def subject_detail(subject_id: str, subtype: str = "") -> Optional[dict]:
    """按豆瓣 subject id 取 Frodo 详情（简介 / 评分 / 海报 / 类型）。

    ``subtype`` 为 ``movie`` / ``tv``（Frodo 详情按这两类分路径）；
    不给时按 movie → tv 顺序各试一次。成功响应走 ``tmdb_cache``
    信封缓存（批一 #8 口径，见模块头注），命中不发请求、不碰熔断。
    """
    subject_id = str(subject_id or "").strip()
    if not subject_id:
        return None
    from backend.emby_server import tmdb_cache

    hit, cached = tmdb_cache.load_details(_CACHE_SUBJECT_ENDPOINT, subject_id)
    if hit and isinstance(cached, dict) and cached.get("id"):
        return cached
    candidates = [subtype] if subtype in ("movie", "tv") else ["movie", "tv"]
    for sub in candidates:
        data = _get_with_fallback(f"/{sub}/{subject_id}")
        if not isinstance(data, dict) or not data.get("id"):
            continue
        normalized = _normalize_subject(data, sub)
        if not normalized["id"]:
            continue
        tmdb_cache.save_details(_CACHE_SUBJECT_ENDPOINT, subject_id,
                                normalized, raw=data)
        return normalized
    return None


# ---------------------------------------------------------------------------
# IMDb 桥（主路）：imdb_id → 豆瓣 subject
# ---------------------------------------------------------------------------

def find_by_imdb(imdb_id: str, year: Optional[int] = None,
                 kind: str = "") -> Optional[dict]:
    """用 IMDb 编号精确换豆瓣条目（桥的主路），返回归一化 dict 或 None。

    走 v2 的 imdb 端点（``api.douban.com/v2/movie/imdb/{id}``，FMBY
    证据原文）：编号本身就是精确身份，不需要标题校验；只留一道年份
     sanity 门——两边年份都已知且不一致时判没找到（防端点返回串号
    结果被当真）。v2 给的文案不全（summary 常为空）时，再用 Frodo
    详情按 id 补齐简介 / 评分（只补空缺，不覆盖 v2 已给的）。

    注意：v2 的 imdb 端点历史上只索引电影；剧集编号查不到是预期内
    的miss，**不在这里退化成片名搜**——片名兜底是搜索路径的事，
    桥只认编号（魂 1：身份先行，不许拿模糊搜撞运气当主路）。
    """
    imdb_id = str(imdb_id or "").strip()
    if not imdb_id:
        return None
    from backend.emby_server import tmdb_cache

    hit, cached = tmdb_cache.load_details(_CACHE_IMDB_ENDPOINT, imdb_id)
    if hit and isinstance(cached, dict) and cached.get("id"):
        return cached
    data = _request_json(V2_IMDB_URL.format(
        imdb_id=urllib.parse.quote(imdb_id, safe="")))
    if not isinstance(data, dict) or not data.get("id"):
        # v2 查无此编号时返回的是错误对象（{"msg": ..., "code": ...}）
        return None
    normalized = _normalize_subject(data)
    if year and normalized["year"] and int(year) != int(normalized["year"]):
        logger.debug("[douban-frodo] IMDb 桥年份不符，丢弃：%s -> 豆瓣 %s (%s vs %s)",
                     imdb_id, normalized["id"], normalized["year"], year)
        return None
    if not normalized["overview"] or normalized["rating"] is None:
        detail = subject_detail(
            normalized["id"],
            normalized["subtype"] or _subtype_for_kind(kind))
        if detail:
            for key, value in detail.items():
                if not normalized.get(key) and value:
                    normalized[key] = value
    tmdb_cache.save_details(_CACHE_IMDB_ENDPOINT, imdb_id,
                            normalized, raw=data)
    return normalized


# ---------------------------------------------------------------------------
# Frodo 片名搜（兜底）：没有 IMDb 编号时才用，过置信门槛
# ---------------------------------------------------------------------------

def search_by_title(title: str, year: Optional[int] = None,
                    kind: str = "") -> Optional[dict]:
    """Frodo 片名搜（兜底路），返回归一化 dict 或 None。

    置信门槛与多源补缺既有口径对齐（``sources._title_consistent`` +
    年份门，与 altmeta / OMDb 的「宁可漏也不错配」一致）：候选标题
    归一化后须与查询全等或互为前缀，年份都已知时必须相等，类型
    （movie / tv）须与 ``kind`` 对得上。命中后取 Frodo 详情补齐
    文案；详情取不到时退回搜索 target 自带的字段（标题 / 年份 /
    评分 / 海报），不整条丢弃。
    """
    q = (title or "").strip()
    if not q:
        return None
    from backend.emby_server.metasources.sources import _title_consistent, _year_of

    data = _get_with_fallback("/search", {"q": q, "start": 0, "count": 10})
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    want_subtype = _subtype_for_kind(kind) if kind else ""
    for entry in items:
        if not isinstance(entry, dict):
            continue
        target = entry.get("target")
        if not isinstance(target, dict):
            continue
        ttype = str(entry.get("target_type") or target.get("subtype") or "")
        if want_subtype and ttype and ttype != want_subtype:
            continue
        cand_title = str(target.get("title") or "")
        if not _title_consistent(q, cand_title):
            continue
        cand_year = _year_of(target.get("year"))
        if year and cand_year and int(year) != int(cand_year):
            continue
        normalized = _normalize_subject(target, ttype)
        if not normalized["id"]:
            continue
        detail = subject_detail(normalized["id"],
                                ttype or want_subtype)
        return detail or normalized
    return None


__all__ = [
    "PROVIDER", "find_by_imdb", "search_by_title", "subject_detail",
]

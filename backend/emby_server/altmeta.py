"""豆瓣兜底刮削：TMDB 搜不到时的第二数据源。

## 为什么需要

实测生产 `metadata_source='none'` 的条目（TMDB 搜不到），用豆瓣 `subject_suggest`
能找回多数：

    电锯人              -> 链锯人：刺客篇 (2026)
    落语朱音            -> 朱音落语 (2026) 12集
    B PROJECT           -> B-PROJECT (2016) 13集
    现在多闻君是哪一面  -> 现在的是哪一个多闻！？ (2026) 13集

TMDB 对中文剧集/综艺收录偏少，豆瓣互补很有效。

## 为什么不用 api.douban.com/v2

官方 v2 API 早已停止服务。这里走无需 key 的 ``/j/subject_suggest``
（实测 200、数据完整），无需配置任何密钥。

## 字段能力边界

``subject_suggest`` 只返回 标题/年份/海报/条目ID/副标题，**没有简介和评分**。
所以本模块只补这三项，简介与评分留空（宁可空着，也不要拿不可靠的来源填）。
需要简介评分只能抓 subject 页面，反爬脆弱、得不偿失。

## 匹配策略

**只兜底，不覆盖**：仅在条目当前没有 TMDB 命中时才写。已有 TMDB 数据的条目
一律不动——豆瓣是第二数据源，不能反过来盖掉更权威的 TMDB。

结果同样写入 ``metadata_source='douban'``，与 NFO/TMDB 区分开。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

from backend.emby_server.tmdb import _YEAR_TAIL_RE

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://movie.douban.com/j/subject_suggest"
BANGUMI_SEARCH_URL = "https://api.bgm.tv/search/subject"
CONFIG_ENABLED = "altmeta_enabled"
# 注意：这个 key 的语义一旦确定就不能再改。PR #178 曾把它的语义从
# "每分钟请求数"改成"两次请求最小间隔秒数"，沿用同名 key 会导致老配置
# 被误读（如配了 30 会从"30次/分钟"变成"30秒一次"，慢 60 倍）。
# 以后如果还要改语义，必须换新 key 名 + 写迁移，禁止复用。
CONFIG_RATE = "altmeta_douban_rate"
CONFIG_BANGUMI_RATE = "altmeta_bangumi_rate"

# 速率限制：**两次请求之间的最小间隔秒数**（``altmeta_douban_rate`` 的语义）。
#
# 取"秒/次"而不是"次/分钟"：配置里写 1.0 时，按次/分钟解读是「每分钟 1 次」，
# 153 条要跑两个多小时，追新时新条目得等两小时才补上——慢到没有实用价值。
# 按秒/次解读则是每秒 1 次（60/分），既够快又足够保守。rate=0 表示不限速。
DEFAULT_MIN_INTERVAL = 1.0
REQUEST_TIMEOUT = 15
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 电视剧/综艺的 type 值（subject_suggest 返回 movie/tv/book/music）
_SERIES_TYPES = {"tv", "show", "series"}
# Bangumi subject type：2=动画 4=音乐 6=电影（无 TV 类，番剧都归 2）
_BANGUMI_SERIES_TYPES = {"2"}

_lock = threading.Lock()


class _RateLimiter:
    """按最小间隔限速，避免把豆瓣打到封 IP。

    语义是「两次请求至少隔 N 秒」而不是「每分钟最多 N 次」：前者对调用方更直观，
    也不会出现「配 1.0 结果一分钟只发一次」这种反直觉行为。
    """

    def __init__(self) -> None:
        self._last: float = 0.0

    def acquire(self, min_interval: float) -> None:
        # 无论是否限速都要记时间戳：否则「先不限速调用一次、再限速调用」时
        # 第二次不会等待（_last 还是 0），限速形同虚设。
        if min_interval <= 0:
            with _lock:
                self._last = time.time()
            return
        while True:
            with _lock:
                now = time.time()
                wait = self._last + min_interval - now if self._last else 0.0
                if wait <= 0:
                    self._last = now
                    return
            time.sleep(min(wait, 5.0))


_limiter = _RateLimiter()


def _get_config(db, key: str, default: str = "") -> str:
    """统一热读（只许这一套）：短 TTL 缓存，保存时失效"""
    from backend.integrations import store
    try:
        return store.get_value(db, key, default)
    except Exception:  # noqa: BLE001 — 读配置失败不该影响刮削
        return default


def enabled(db) -> bool:
    return _get_config(db, CONFIG_ENABLED, "0") == "1"


def min_interval(db) -> float:
    """两次请求的最小间隔秒数（``altmeta_douban_rate``，0 = 不限速）"""
    try:
        return max(0.0, float(_get_config(db, CONFIG_RATE, str(DEFAULT_MIN_INTERVAL))))
    except (TypeError, ValueError):
        return DEFAULT_MIN_INTERVAL


def _clean(title: str) -> str:
    """归一化标题用于比对：去空白与常见标点（`B PROJECT` ≡ `B-PROJECT`）"""
    return re.sub(r"[\s:：·\-—_\.、,，!！?？~～'\"“”‘’()（）\[\]【】]+",
                  "", (title or "").lower())


def search(title: str, year: Optional[int] = None, kind: str = "series",
           min_interval: float = DEFAULT_MIN_INTERVAL) -> Optional[dict]:
    """搜一条，返回 ``{id, title, year, image, type, url}``；没有可信命中返回 None

    只取首条结果，且要求清洗后标题与查询高度一致——豆瓣的 suggest 结果里
    常常是同名的别的作品（「电锯人」搜到「链锯人」），宁可漏也不错配。
    """
    q = (title or "").strip()
    if not q:
        return None
    _limiter.acquire(min_interval)
    url = f"{SUGGEST_URL}?q={urllib.parse.quote(q)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:  # noqa: BLE001 — 网络/反爬失败不应中断补全
        logger.debug("[altmeta] 豆瓣搜索失败 %r: %s", q, exc)
        return None
    if not isinstance(data, list) or not data:
        return None
    hit = data[0]
    if not isinstance(hit, dict):
        return None
    got = _clean(str(hit.get("title") or ""))
    want = _clean(q)
    if not got or not want:
        return None
    # 标题必须完全一致，或一方是另一方的前缀（「落语朱音」/「朱音落语」不算，
    # 宁可漏掉这类也不要写错片名）
    if got != want and not (got.startswith(want) or want.startswith(got)):
        logger.debug("[altmeta] 豆瓣首条标题不匹配，丢弃：%r -> %r", q, got)
        return None
    if year and hit.get("year"):
        try:
            if int(hit["year"]) != int(year):
                logger.debug("[altmeta] 豆瓣年份不符，丢弃：%r %s vs %s",
                             q, hit.get("year"), year)
                return None
        except (TypeError, ValueError):
            pass
    if kind in ("series", "season", "episode") and hit.get("type") not in _SERIES_TYPES:
        # 类型对不上就不要（避免电影条目被当成剧集）
        if hit.get("type") != "tv":
            logger.debug("[altmeta] 豆瓣类型不符(%s)，丢弃：%r", hit.get("type"), q)
            return None
    return {
        "id": str(hit.get("id") or ""),
        "title": str(hit.get("title") or ""),
        "year": hit.get("year") or None,
        "image": str(hit.get("img") or ""),
        "type": str(hit.get("type") or ""),
        "url": str(hit.get("url") or ""),
    }


def apply(item: Any, hit: dict, source: str = "douban") -> None:
    """把豆瓣命中落到条目上

    只补 TMDB 没给的东西，且**绝不覆盖**已有值：豆瓣是兜底源，不是权威源。
    """
    from datetime import datetime
    if not item.tmdb_id:
        item.tmdb_id = None  # 保持为 None——豆瓣 id 放 douban_id，不混用 tmdb_id
    if hit.get("title"):
        # 名字只在"三无"时才用豆瓣标题补（无简介、无海报、无主图）：
        # 这种一般是文件名解析出的脏名，豆瓣标题（已过校验）比它靠谱；
        # 只要已有简介或海报，说明之前已刮削过，名字不动（可能是用户/NFO 整理过的）。
        if not (item.overview or "").strip() and not item.poster_path \
                and not item.primary_image_url:
            from backend.emby_server.tmdb import clean_title
            _n = clean_title(hit["title"])
            if _n:
                item.name = _n
    if hit.get("year") and not item.production_year:
        try:
            item.production_year = int(hit["year"])
        except (TypeError, ValueError):
            pass
    if hit.get("image") and not item.poster_path and not item.primary_image_url:
        from backend.emby_server.tmdb import _set_image
        _set_image(item, "Primary", hit["image"])
    if not item.last_scraped_at:
        item.last_scraped_at = datetime.now()
    item.metadata_source = source


# ==================== Bangumi（番组）兜底 ====================

def min_interval_bangumi(db) -> float:
    """Bangumi 的最小间隔秒数（``altmeta_bangumi_rate``，0 = 不限速）"""
    try:
        return max(0.0, float(_get_config(db, CONFIG_BANGUMI_RATE,
                                          str(DEFAULT_MIN_INTERVAL))))
    except (TypeError, ValueError):
        return DEFAULT_MIN_INTERVAL


_bgm_limiter = _RateLimiter()


def search_bangumi(title: str, year: Optional[int] = None, kind: str = "series",
                   min_interval: float = DEFAULT_MIN_INTERVAL) -> Optional[dict]:
    """搜 Bangumi。返回与 :func:`search` 同形状的 dict。

    走 ``/search/subject/`` 公开接口（实测 200）。它比豆瓣好在两点：
    1. 返回 ``name_cn``（中文名）——TMDB 对中文剧集收录差，这正是我们缺的；
    2. 返回 ``images``（封面），豆瓣 suggest 也能给但中文剧命中率低得多。

    同样从严：只认清洗后标题完全一致（或互为前缀）且 type 是动画的候选。
    实测「电锯人」会搜到「电锯人~温泉旅行篇」「电锯人 舞台剧」等同系列作品，
    这类必须拒绝——写错片名比不写更糟。
    """
    q = (title or "").strip()
    if not q:
        return None
    # 目录名几乎都带 `(YYYY)`，不剥掉就永远匹配不上（与 TMDB 搜索同一个坑）
    q = re.sub(r"\s+", " ", _YEAR_TAIL_RE.sub(" ", q)).strip(" -_.()（）")
    if not q:
        return None
    _bgm_limiter.acquire(min_interval)
    url = f"{BANGUMI_SEARCH_URL}/{urllib.parse.quote(q)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:  # noqa: BLE001 — 网络/限流失败不应中断补全
        logger.debug("[altmeta] Bangumi 搜索失败 %r: %s", q, exc)
        return None
    items = (data or {}).get("list") or []
    want = _clean(q)
    for it in items[:5]:
        if not isinstance(it, dict):
            continue
        if kind in ("series", "season", "episode") and str(it.get("type")) not in _BANGUMI_SERIES_TYPES:
            continue
        names = [it.get("name_cn"), it.get("name")]
        got = next((_clean(str(n)) for n in names if n), "")
        if not got:
            continue
        # 必须是**完全相等**。前缀匹配会放进「电锯人」→「电锯人~温泉旅行篇」
        # 「电锯人 舞台剧」这类同系列其它作品——写错片名比不写更糟。
        if got != want:
            continue
        img = it.get("images") or {}
        cover = img.get("large") or img.get("common") or img.get("medium") or ""
        air = str(it.get("air_date") or "")
        return {
            "id": str(it.get("id") or ""),
            "title": str(it.get("name_cn") or it.get("name") or ""),
            "year": air[:4] or None,
            "image": str(cover or ""),
            "type": "tv",
            "url": str(it.get("url") or ""),
        }
    return None

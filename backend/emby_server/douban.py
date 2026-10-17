"""豆瓣元数据客户端：中文内容刮削的主数据源。

TMDB 对中文剧集/综艺收录偏少（短中文剧名 Tier1 LCS>=6 永远达不到），
中文标题优先走豆瓣，搜中率高、速度快。

数据源：
- 搜索：https://movie.douban.com/j/subject_suggest?q=（无需 key，实测 200）
- 详情：https://movie.douban.com/subject/{id}/ 页面的 JSON-LD（schema.org）

反爬：Chrome UA + 线程安全限速器（默认 1 秒/次，可配置）。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://movie.douban.com/j/subject_suggest"
# 管理后台配置键
DOUBAN_ENABLED_KEY = "douban_enabled"
DOUBAN_RATE_KEY = "douban_min_interval"
# 默认 2.5s：豆瓣反爬激进，1.0s 持续请求会被封 IP（2026-10-13 生产事故）。
# 可在管理后台「刮削设置」调大，0 = 不限速（不推荐）。
DEFAULT_MIN_INTERVAL = 2.5
REQUEST_TIMEOUT = 15

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# CJK 字符范围：中日韩统一表意文字 + 假名 + 韩文
_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\u3040-\u309f\u30a0-\u30ff\U00020000-\U0002a6df"
    "\uac00-\ud7af]"
)

# JSON-LD 提取：豆瓣 subject 页面的结构化数据
_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# 电视剧/综艺的 type 值（subject_suggest 返回 movie/tv/book/music）
_SERIES_TYPES = {"tv", "show", "series"}

_lock = threading.Lock()

# 封禁检测：豆瓣封 IP 时返回 403/429，或 200 + 跳 sec.douban.com 的验证码/禁止访问页。
# 以前这些都被当成「没搜中」→ 中文条目落到 TMDB / metadata_source='none'。
# 现在抛 DoubanBannedError（瞬态），并进入全局冷却：冷却期内不再发任何豆瓣请求。
#
# 反风控设计（2026-10-13 生产封禁事故后）：
# - 冷却指数退避：连续被封次数越多，冷却越长（15min → 30min → 1h → 2h 封顶），
#   避免固定 900s 到点后惊群重试瞬间再封。
# - 冷却本身加随机抖动，不让重试节奏可预测。
BAN_BASE_COOLDOWN_SECONDS = 900
BAN_MAX_COOLDOWN_SECONDS = 7200
_BAN_HTTP_CODES = (403, 418, 429)
_BAN_MARKERS = ("sec.douban.com", "检测到有异常请求", "<title>禁止访问</title>")
_banned_until: float = 0.0
_ban_strikes: int = 0  # 连续被封次数；成功请求一次清零


class DoubanBannedError(RuntimeError):
    """豆瓣封禁/限流（瞬态）：调用方应留待重试，而不是当成没搜中。"""


def ban_remaining() -> float:
    """全局冷却剩余秒数（0 = 未封禁）。"""
    return max(0.0, _banned_until - time.time())


def reset_ban() -> None:
    global _banned_until, _ban_strikes
    with _lock:
        _banned_until = 0.0
        _ban_strikes = 0


def ban_strikes() -> int:
    """连续被封次数（测试/观测用）。"""
    with _lock:
        return _ban_strikes


def _trip_ban(reason: str) -> None:
    global _banned_until, _ban_strikes
    with _lock:
        fresh = _banned_until <= time.time()
        _ban_strikes += 1
        # 指数退避：900 * 2^(n-1)，封顶 7200s；再加 0~60s 随机抖动
        backoff = min(BAN_BASE_COOLDOWN_SECONDS * (2 ** (_ban_strikes - 1)),
                      BAN_MAX_COOLDOWN_SECONDS)
        cooldown = backoff + random.uniform(0, 60)
        _banned_until = time.time() + cooldown
    if fresh:
        logger.warning("[douban] 疑似被封禁（%s），第 %d 次，冷却 %ds",
                       reason, _ban_strikes, int(cooldown))


def _note_success() -> None:
    """一次成功请求：连续被封计数清零。"""
    global _ban_strikes
    with _lock:
        _ban_strikes = 0


class _RateLimiter:
    """线程安全限速器：两次请求之间的最小间隔秒数。

    反风控：固定间隔是典型的机器人签名。实际等待 = 间隔 * [1.0, 1.6) 随机，
    让请求节奏不可预测。interval=0 时不限速（测试用）。
    """

    def __init__(self) -> None:
        self._last: float = 0.0

    def acquire(self, min_interval: float) -> None:
        if min_interval <= 0:
            with _lock:
                self._last = time.time()
            return
        # 每次取号时现算抖动，避免所有线程同相位
        effective = min_interval * random.uniform(1.0, 1.6)
        while True:
            with _lock:
                now = time.time()
                wait = self._last + effective - now if self._last else 0.0
                if wait <= 0:
                    self._last = now
                    return
            time.sleep(min(wait, 5.0))


_limiter = _RateLimiter()


def has_cjk(text: str) -> bool:
    """检测文本是否包含中日韩字符。"""
    if not text:
        return False
    return _CJK_RE.search(text) is not None


def _clean(title: str) -> str:
    """归一化标题用于比对：去空白与常见标点。"""
    return re.sub(r"[\s:：·\-—_\.、,，!！?？~～'\"“”‘’()（）\[\]【】]+",
                  "", (title or "").lower())


def enabled(db: Any) -> bool:
    """豆瓣总开关（默认关；读不到配置时 fail-closed，不打豆瓣）。"""
    from backend.integrations import store
    try:
        v = store.get_value(db, DOUBAN_ENABLED_KEY, "0")
    except Exception:
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def min_interval(db: Any) -> float:
    """两次豆瓣请求的最小间隔秒数（默认 1.0，0 = 不限速）。"""
    from backend.integrations import store
    try:
        return max(0.0, float(store.get_value(db, DOUBAN_RATE_KEY,
                                              str(DEFAULT_MIN_INTERVAL))))
    except (TypeError, ValueError):
        return DEFAULT_MIN_INTERVAL


class DoubanClient:
    """豆瓣元数据客户端：搜索 + 详情，内置限速。"""

    def __init__(self, interval: float = DEFAULT_MIN_INTERVAL) -> None:
        self._interval = interval

    def _get(self, url: str) -> Optional[str]:
        """GET 一页。封禁/验证码页抛 DoubanBannedError；其它失败返回 None。"""
        left = ban_remaining()
        if left > 0:
            raise DoubanBannedError(f"豆瓣冷却中，剩余 {int(left)}s")
        _limiter.acquire(self._interval)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                final_url = ""
                try:
                    final_url = resp.geturl() or ""
                except Exception:  # noqa: BLE001
                    pass
                body = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            if exc.code in _BAN_HTTP_CODES:
                _trip_ban(f"HTTP {exc.code}")
                raise DoubanBannedError(f"HTTP {exc.code}") from exc
            logger.debug("[douban] 请求失败 %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.debug("[douban] 请求失败 %s: %s", url, exc)
            return None
        if "sec.douban.com" in final_url or any(m in body[:4096] for m in _BAN_MARKERS):
            _trip_ban("验证码/禁止访问页")
            raise DoubanBannedError("captcha page")
        _note_success()
        return body

    def search(self, title: str, year: Optional[int] = None,
               kind: str = "series") -> Optional[dict]:
        """搜一条，返回 {id, title, year, image, type, url}；无可信命中返回 None。

        只取首条结果，且要求清洗后标题与查询高度一致——豆瓣 suggest 结果里
        常常是同名的别的作品，宁可漏也不错配。
        """
        q = (title or "").strip()
        if not q:
            return None
        url = f"{SUGGEST_URL}?q={urllib.parse.quote(q)}"
        raw = self._get(url)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, list) or not data:
            return None
        hit = data[0]
        if not isinstance(hit, dict):
            return None
        # 标题必须完全一致，或一方是另一方的前缀
        got = _clean(str(hit.get("title") or ""))
        want = _clean(q)
        if not got or not want:
            return None
        if got != want and not (got.startswith(want) or want.startswith(got)):
            logger.debug("[douban] 首条标题不匹配，丢弃：%r -> %r", q, got)
            return None
        # 年份校验
        if year and hit.get("year"):
            try:
                if int(hit["year"]) != int(year):
                    logger.debug("[douban] 年份不符，丢弃：%r %s vs %s",
                                 q, hit.get("year"), year)
                    return None
            except (TypeError, ValueError):
                pass
        # 类型校验：剧集不能拿电影条目
        if kind in ("series", "season", "episode") \
                and hit.get("type") not in _SERIES_TYPES:
            logger.debug("[douban] 类型不符(%s)，丢弃：%r", hit.get("type"), q)
            return None
        return {
            "id": str(hit.get("id") or ""),
            "title": str(hit.get("title") or ""),
            "year": hit.get("year") or None,
            "image": str(hit.get("img") or ""),
            "type": str(hit.get("type") or ""),
            "url": str(hit.get("url") or ""),
        }

    def get_details(self, douban_id: str) -> Optional[dict]:
        """按豆瓣 ID 取详情：标题/简介/评分/年份/海报/类型。

        解析 subject 页面的 JSON-LD（schema.org 结构化数据）。
        失败返回 None（调用方用 search 结果兜底，不中断流程）。
        """
        if not douban_id:
            return None
        url = "https://movie.douban.com/subject/{}/".format(
            urllib.parse.quote(str(douban_id)))
        html = self._get(url)
        if not html:
            return None
        for m in _LD_JSON_RE.finditer(html):
            try:
                data = json.loads(m.group(1).strip())
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict) or not data.get("name"):
                continue
            rating = data.get("aggregateRating") or {}
            return {
                "title": str(data.get("name") or ""),
                "overview": str(data.get("description") or ""),
                "rating": rating.get("ratingValue"),
                "year": str(data.get("datePublished") or "")[:4] or None,
                "image": str(data.get("image") or ""),
                "genres": data.get("genre") or [],
                "runtime_ticks": parse_iso_duration(data.get("duration")),
            }
        logger.debug("[douban] subject %s 无 JSON-LD", douban_id)
        return None


    def get_celebrities(self, douban_id: str) -> list[dict]:
        """按豆瓣 ID 取演职员表：``[{name, image, role}]``（只取演员）。

        解析 ``/subject/{id}/celebrities`` 页面：头像在 ``background-image``，
        角色在 ``span.role`` 的「演员 Actor (饰 展望)」里。默认占位头像不算头像。
        失败返回 []（演员头像是增强信息，不能影响主流程）。
        """
        if not douban_id:
            return []
        url = "https://movie.douban.com/subject/{}/celebrities".format(
            urllib.parse.quote(str(douban_id)))
        html = self._get(url)
        if not html:
            return []
        return parse_celebrities(html)


_ISO_DUR_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", re.IGNORECASE)


def parse_iso_duration(value: Any) -> int:
    """JSON-LD 的 ISO 8601 时长（``PT1H58M``）→ 100ns ticks；解析不了返回 0。"""
    m = _ISO_DUR_RE.match(str(value or "").strip())
    if not m or not any(m.groups()):
        return 0
    h, mi, se = (int(g or 0) for g in m.groups())
    secs = h * 3600 + mi * 60 + se
    return secs * 10_000_000 if 0 < secs < 86400 else 0


_CELEB_BLOCK_RE = re.compile(r'<li[^>]*class="celebrity"[^>]*>(.*?)</li>',
                             re.DOTALL | re.IGNORECASE)
_CELEB_NAME_RE = re.compile(r'class="name"[^>]*>\s*(?:<a[^>]*>)?([^<]+)<', re.IGNORECASE)
_CELEB_TITLE_RE = re.compile(r'<a[^>]*title="([^"]+)"', re.IGNORECASE)
_CELEB_AVATAR_RE = re.compile(r'background-image:\s*url\(([^)]+)\)', re.IGNORECASE)
_CELEB_ROLE_RE = re.compile(r'class="role"[^>]*>([^<]*)<', re.IGNORECASE)
_PLAYS_RE = re.compile(r'饰\s*([^)）/]+)')


def parse_celebrities(html: str) -> list[dict]:
    """解析豆瓣演职员页（纯函数，便于测试）。只返回演员行。"""
    out: list[dict] = []
    for m in _CELEB_BLOCK_RE.finditer(html or ""):
        block = m.group(1)
        role_m = _CELEB_ROLE_RE.search(block)
        role_text = (role_m.group(1) if role_m else "").strip()
        if role_text and not (role_text.startswith("演员") or "Actor" in role_text
                              or "Actress" in role_text):
            continue  # 导演/编剧等
        nm = _CELEB_NAME_RE.search(block) or _CELEB_TITLE_RE.search(block)
        name = (nm.group(1) if nm else "").strip()
        if not name:
            continue
        av = _CELEB_AVATAR_RE.search(block)
        image = (av.group(1) if av else "").strip().strip("'\"")
        if not image.startswith(("http://", "https://")) or "default" in image:
            image = ""
        plays = _PLAYS_RE.search(role_text)
        out.append({"name": name, "image": image,
                    "role": plays.group(1).strip() if plays else ""})
    return out


def name_matches(local: str, douban_name: str) -> bool:
    """库里的演员名（如「嘉羿」）与豆瓣名（「嘉羿 Jia Yi」）是不是同一个人。"""
    a = (local or "").strip()
    b = (douban_name or "").strip()
    if not a or not b:
        return False
    if a == b or b.startswith(a + " "):
        return True
    # 库里是英文名、豆瓣是「中文名 英文名」
    return bool(not has_cjk(a)) and b.lower().endswith(" " + a.lower())


def display_name(douban_name: str) -> str:
    """豆瓣演员名「嘉羿 Jia Yi」→ 库里显示用的「嘉羿」（只去掉尾部的外文名）。

    纯外文名（无 CJK）原样返回。
    """
    name = (douban_name or "").strip()
    if not has_cjk(name):
        return name
    parts = name.split()
    keep: list[str] = []
    for tok in parts:
        if keep and not has_cjk(tok):
            break
        keep.append(tok)
    return " ".join(keep) or name


client = DoubanClient()

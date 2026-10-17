"""TMDB 刮削客户端（从 scanner.py 拆出）

扫描器有两件完全不同性质的事：**遍历文件系统**（IO 密集、状态多）与**跟 TMDB 说话**
（网络、配额轮询、短 TTL 缓存）。前者是 scanner.py 的主体，后者是这里的 `TmdbClient`：

- 密钥轮询（`TMDB_API_KEYS=key1,key2,key3`，单个 key 429/401 自动切换）；
- 短 TTL 进程内缓存：扫描时工作线程先「搜索 + 详情」预热，写库线程随后调同一请求直接命中，
  既并行又不重复消耗配额。

拆开之后扫描器只剩扫描逻辑，客户端也能单独测（不依赖扫描器的目录缓存、线程池等状态）。
`scanner.tmdb_client` 等既有引用由 scanner.py 重新导出，调用方无需改动。
"""
from __future__ import annotations

import logging
import contextlib
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.emby_server import image_store
from backend.emby_server import models as emby_models
from backend.emby_server import scan_progress as progress
from backend.emby_server import tmdb_cache

logger = logging.getLogger(__name__)


class TmdbTransientError(RuntimeError):
    """TMDB 请求的**瞬态**失败（网络重试耗尽 / 5xx / 响应解析失败 / 全部密钥被限流）。

    必须与「真搜不到」区分开：瞬态失败向上抛，补全 worker 走既有的
    ``ok=False → _mark_failed`` 指数退避重试（复现见 scripts/repro_scrape_stability.py）。
    旧实现把它吞成 ``None``，``_enrich_apply`` 便写成终态
    ``done + metadata_source='none'``——条目从此不再重试，这正是
    「刮削时好时坏」的根因（问题一）。

    非瞬态仍返回 ``None``，保持既有阴性语义：404（真没有）、401（密钥配置错）、
    未配置 key —— 这三种烧重试预算没有意义。
    """


TMDB_API_DEFAULT = "https://api.themoviedb.org/3"
TMDB_IMAGE_DEFAULT = "https://image.tmdb.org/t/p"
# 兼容旧引用（scanner.py 再导出这两个名字）：真正生效的地址走 ``api_base()`` /
# ``image_base()``，可以在管理后台配镜像（境内直连不通时用）。
TMDB_API = TMDB_API_DEFAULT
TMDB_IMAGE = TMDB_IMAGE_DEFAULT
# 遗留常量：历史代码从环境变量读语言。新口径统一走 preferred_language()
#（环境变量 TMDB_LANGUAGE > SystemConfig > zh-CN），这里仅保留兼容。
TMDB_LANG = os.getenv("TMDB_LANGUAGE", "zh-CN")

# TMDB API Key 在 SystemConfig 里的键：管理后台「元数据与刮削」填写，保存即热生效
TMDB_KEYS_CONFIG_KEY = "tmdb_api_keys"
# 镜像 / 反代地址（空 = 用官方地址）。API 与图片分开：跨境时常见的是图片 CDN 可走
# 镜像而 API 直连（或反过来），一条配置管两件事会互相绑死。
TMDB_API_BASE_CONFIG_KEY = "tmdb_api_base"
TMDB_IMAGE_BASE_CONFIG_KEY = "tmdb_image_base"
# 密钥冷却时长：单把密钥 429 / 401 之后跳过它多久（阈值走配置，不写死）
TMDB_KEY_COOLDOWN_CONFIG_KEY = "tmdb_key_cooldown_sec"
TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY = "tmdb_key_invalid_cooldown_sec"
DEFAULT_KEY_COOLDOWN_SEC = 900          # 15 分钟：配额窗口一般几分钟
DEFAULT_KEY_INVALID_COOLDOWN_SEC = 21600  # 6 小时：401 多半是 key 废了，等管理员换
# 冷却 / 镜像配置的进程内 TTL：跨进程（API / worker / EA）靠它兜底，同进程保存即失效
SETTINGS_TTL_SEC = 30.0
# TMDB 首选语言（SystemConfig 落库）：简介/标题/别名返回哪种语言。
# 管理后台「元数据与刮削」卡片的下拉框填写，保存即热生效。
TMDB_PREFERRED_LANGUAGE_CONFIG_KEY = "tmdb_preferred_language"
TMDB_PREFERRED_LANGUAGE_DEFAULT = "zh-CN"
TMDB_LANGUAGE_OPTIONS = ("zh-CN", "zh-TW", "en-US", "ja-JP")

# StrmAssistant 对标 #10：海报语言偏好
# - "system"：跟随系统语言（默认，现有行为）
# - "original"：优先原语言海报（如英文原版海报）
# - "zh-CN"：优先中文海报
TMDB_POSTER_LANGUAGE_CONFIG_KEY = "tmdb_poster_language"
TMDB_POSTER_LANGUAGE_DEFAULT = "system"
TMDB_POSTER_LANGUAGE_OPTIONS = ("system", "original", "zh-CN")

# 批2（刮削瘦身）：TMDB 文字字段只填空、不覆盖（标题/年份/简介/评分/类型/
# 国家/语言）。默认 "1"=开（新行为：已有非空文字 TMDB 永不覆盖，为将来 NFO
# 或人工编辑让路）；配 "0" 回退旧的覆盖行为。开关走 config_service 既有模式，
# 无后台 UI（内部收紧，非用户能力）。
TMDB_TEXT_FILL_ONLY_CONFIG_KEY = "enrich_tmdb_text_fill_only"
_TEXT_FILL_CACHE = {"at": 0.0, "value": True}
_TEXT_FILL_TTL_SEC = 30.0

# ---------------------------------------------------------------------------
# 请求级限流 / 超时 / 429 退避（v2.42.9）

# StrmAssistant 对标：MovieDbFallbackLanguages
# 首选语言无高置信命中时，按此链逐个重试（去重后）。
MOVIEDB_FALLBACK_LANGUAGES = ("zh-CN", "zh-HK", "zh-TW", "ja-JP", "en-US")
# fallback 最大深度（环境变量 TMDB_FALLBACK_MAX_LANGS）：首选语言 + 最多 N 种备选。
# 最坏请求数 = (1+N) × 候选数；默认 2（最坏 15 请求），设 0 则关闭 fallback。
try:
    _FALLBACK_MAX = int(os.getenv("TMDB_FALLBACK_MAX_LANGS", "2") or 2)
except ValueError:
    _FALLBACK_MAX = 2
TMDB_FALLBACK_MAX_LANGS = max(0, min(_FALLBACK_MAX, len(MOVIEDB_FALLBACK_LANGUAGES)))


def language_fallback_chain(db=None) -> list[str]:
    """TMDB 语言 fallback 链：首选语言打头，后跟备选（去重，深度受限）。

    对标 StrmAssistant ``LanguageUtility.MovieDbFallbackLanguages``。
    zh-CN 无结果时自动试 zh-TW/en-US 等，不直接放弃。
    """
    preferred = preferred_language(db)
    chain = [preferred]
    for lang in MOVIEDB_FALLBACK_LANGUAGES:
        if len(chain) > TMDB_FALLBACK_MAX_LANGS:
            break
        if lang not in chain:
            chain.append(lang)
    return chain
#
# 旧实现把令牌桶放在 enrich_worker 里，**按条目**扣一个 token，而一个条目后面可能是
# 0~6 个 HTTP（中文标题 4~5 个候选搜索，最贵）；于是“4/秒”实际上是 8~24 请求/秒，
# 而且扫描那条路（scanner._tmdb_work）**完全没有限流**。
# 现在桶搬到这里，一条请求一个 token，口径与 TMDB 配额一致，两条路共用同一个桶。
#
# 注：TMDB 2019-12-16 已取消旧的 40 次/10 秒硬限流，现在是非正式 ~50 请求/秒/IP，
# 所以**不要**把速率拉满；429 时下面的自适应会自己降下来。
#
# 环境变量（沿用旧名字，但语义已从「条目/秒」改为「请求/秒」）：
#   ENRICH_TMDB_PER_SEC      请求上限（默认 2）
#   ENRICH_TMDB_MIN_PER_SEC  429 自适应下降的下限（默认 1）
#   TMDB_TIMEOUT             单次请求超时秒数（默认 10；带 append_to_response 的详情请求
#                            在跨境链路上 8 秒偏紧）
#   TMDB_NET_RETRIES         读超时/连接错误的重试次数（默认 2）
TMDB_PER_SEC = max(1.0, float(os.getenv("ENRICH_TMDB_PER_SEC", "2") or 2))
TMDB_MIN_PER_SEC = max(1.0, float(os.getenv("ENRICH_TMDB_MIN_PER_SEC", "1") or 1))
TMDB_TIMEOUT = max(5.0, float(os.getenv("TMDB_TIMEOUT", "10") or 10))
TMDB_NET_RETRIES = max(0, min(4, int(os.getenv("TMDB_NET_RETRIES", "2") or 2)))
# Retry-After 上限：后台 worker 睡太久会白白占着线程， 超过就按上限退避
TMDB_RETRY_AFTER_CAP_SEC = max(1.0, float(os.getenv("TMDB_RETRY_AFTER_CAP_SEC", "20") or 20))
# 连续成功多少次之后才试着恢复速率（避免成功一次就立刻又撞 429）
TMDB_RECOVER_AFTER = max(5, int(os.getenv("TMDB_RECOVER_AFTER", "50") or 50))


class _RequestLimiter:
    """请求级令牌桶 + 429 自适应（进程内；两条 TMDB 调用路径共用）

    - `acquire()`：拿一个 token（不够就睡到够），与旧桶同形状；
    - `note_throttled()`：429 时速率乘性下调（×0.5，下限 min_rate），返回建议等待秒数；
    - `note_success()`：连续成功到 TMDB_RECOVER_AFTER 次后速率 +25%，上限是配置值。
    """

    def __init__(self, rate: float = TMDB_PER_SEC, min_rate: float = TMDB_MIN_PER_SEC):
        self._ceiling = max(1.0, float(rate))
        self._min_rate = max(1.0, min(float(min_rate), self._ceiling))
        self._rate = self._ceiling
        self._tokens = self._ceiling
        self._updated = time.monotonic()
        self._lock = threading.Lock()
        self._ok_streak = 0
        self._throttled = 0

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def ceiling(self) -> float:
        """配置上限（ENRICH_TMDB_PER_SEC）：自适应恢复最多回到这里"""
        return self._ceiling

    @property
    def throttled_count(self) -> int:
        return self._throttled

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._rate, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            # v2.56.0：等待加 0~20% 随机抖动——令牌桶本身是完全规律的，
            # 多 worker 齐步时会形成固定节拍。抖动打破规律性，防 TMDB 风控。
            # （反风控原则：轮询/多账号切换必须加随机抖动、避免完全规律的轮换模式）
            jittered = wait * (1.0 + random.random() * 0.2)
            time.sleep(min(jittered, 0.6))

    def note_throttled(self, retry_after: Optional[float] = None) -> float:
        """429：速率乘性下调并返回建议等待秒数（已按上限截断）"""
        with self._lock:
            self._rate = max(self._min_rate, self._rate * 0.5)
            self._tokens = 0.0          # 下调后桶也清空：立刻再打一次毫无意义
            self._updated = time.monotonic()
            self._ok_streak = 0
            self._throttled += 1
            rate = self._rate
        wait = TMDB_RETRY_AFTER_CAP_SEC if retry_after is None else max(0.0, float(retry_after))
        wait = min(wait, TMDB_RETRY_AFTER_CAP_SEC)
        if wait <= 0:
            # 没给 Retry-After（或给了 0）：按新速率等一个 token 的时间，别直接转圈
            wait = min(1.0 / rate, TMDB_RETRY_AFTER_CAP_SEC)
        return wait

    def note_success(self) -> None:
        """连续成功后缓慢恢复（每次 +25%，不超上限）"""
        with self._lock:
            if self._rate >= self._ceiling:
                self._ok_streak = 0
                return
            self._ok_streak += 1
            if self._ok_streak >= TMDB_RECOVER_AFTER:
                self._rate = min(self._ceiling, self._rate * 1.25)
                self._ok_streak = 0


class _KeyedLimiter(_RequestLimiter):
    """按 key 独立的令牌桶（v2.57.0 提速包 item3）。

    背景：4 把 key 共用 1 个 4 req/s 桶时，加 key 只分摊配额、不提速
    （生产实证：245 次调用四 key 均匀 61/61/61/62，吞吐 6→5 条/分未涨）。
    每把 key 一个独立桶后，有效上限 = key 数 × TMDB_PER_SEC
    （4 把 ≈ 16 req/s，远低于 TMDB 非正式上限 ~50 req/s/IP，安全区内，
    不要再往上加）。

    429 语义：哪把 key 被 429，就只压低那把 key 的桶（note_throttled 走
    limiter_for(key)），不连累其他 key——配额本来就是按 key 算的。

    测试兼容：测试会整体替换 client._limiter 为普通 _RequestLimiter；
    _keyed_limiter() 检测到没有 _per_key 属性时直接返回它，测试语义不变。
    """

    def __init__(self, rate: float = TMDB_PER_SEC, min_rate: float = TMDB_MIN_PER_SEC):
        super().__init__(rate=rate, min_rate=min_rate)
        self._per_key: dict = {}
        self._per_key_lock = threading.Lock()

    def sync_keys(self, keys) -> None:
        """key 池变化时同步：保留存活 key 的桶（含被 429 压低的速率状态），
        删掉离场 key 的桶（防无界增长）。"""
        alive = set(keys or ())
        with self._per_key_lock:
            for k in list(self._per_key):
                if k not in alive:
                    del self._per_key[k]
            for k in alive:
                if k not in self._per_key:
                    self._per_key[k] = _RequestLimiter()

    def limiter_for(self, key: str) -> "_RequestLimiter":
        """取某把 key 的独立桶（懒创建，线程安全）。key 为空时返回顶层桶。"""
        if not key:
            return self
        with self._per_key_lock:
            lim = self._per_key.get(key)
            if lim is None:
                lim = _RequestLimiter()
                self._per_key[key] = lim
            return lim

    @property
    def rate(self) -> float:
        # 观测口径：有 key 桶时报各桶速率之和（= 当前实际总上限）；没有时报顶层
        with self._per_key_lock:
            kids = list(self._per_key.values())
        if not kids:
            return super().rate
        return round(sum(k.rate for k in kids), 3)

    @property
    def ceiling(self) -> float:
        with self._per_key_lock:
            kids = list(self._per_key.values())
        if not kids:
            return super().ceiling
        return round(sum(k.ceiling for k in kids), 3)

    @property
    def throttled_count(self) -> int:
        with self._per_key_lock:
            kids = list(self._per_key.values())
        return super().throttled_count + sum(k.throttled_count for k in kids)


def _retry_after_seconds(resp) -> Optional[float]:
    """读 Retry-After（秒数或 HTTP 日期）——旧实现全文未读取该头"""
    try:
        raw = (resp.headers.get("retry-after") or "").strip()
    except Exception:  # noqa: BLE001 — 响应对象可能是测试替身
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:  # noqa: BLE001 — 格式不认识就当没给
        return None


# ---------------------------------------------------------------------------
# 查询清洗与置信度校验（2026-09-26）
#
# 通用 bug：文件名解析出的剧名常带发行标签（DUAL/DD5.1/H264/BdC/集数标记/中字等），
# 直接拿去调 TMDB search 命中率极低；而 TMDB 的 results[0] 对短查询经常张冠李戴
# （如"虎子"→"老虎和兔子"、"骑士与魔法"→"魔法使的新娘"）。
# search() 因此改为：清洗查询 → 多候选 → 对多个返回结果做置信度校验，
# 只返回高置信命中；否则返回 None（调用方记 last_scraped_at，名字保持原样，
# 绝不写错名字）。

# 发行/压制/碟片/字幕类标签：只出现在文件名里，不属于标题
_QUERY_JUNK_RE = re.compile(r"""(?ix)
      \b(dual|ddp?\s?5\s?1|dd\s?5\s?1|dts\s?hd|dtshd|atmos|truehd|dts(?:-hd)?|ac3|aac|flac|[234]audios?)\b
    | \b([hx]?\s?264|[hx]?\s?265|hevc|avc|bdc|bluray|blu-ray|web-?dl|webrip|hdtv|dvdrip|bdrip|bdmv|remux)\b
    | \b(1080[ip]|720p|480p|2160p|4k|8k|10bit)\b
    | \b(s\d{1,2}e\d{1,3}|e\d{1,3}|ep\d{1,3}|d[12]|disc\s?\d|cd[12])\b
    | (原盘|中字|简繁\w{0,3}|繁中|简中|粤语|国语|双语|高码率|重制版|蓝光|特效|花絮|熟肉|生肉)
""")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ffー]+")
# 归一化：小写、去全部空白与标点（保留中日韩文字与字母数字）
# 全角标点先折成半角再做归一化：TMDB 用半角冒号，而国内剧名常见全角
# （`90 Day： The Last Resort`、`GIGN：精英部队`），不折就必然零结果。
_PUNCT_FOLD = {
    "：": ":", "，": ",", "。": ".", "！": "!", "？": "?",
    "（": "(", "）": ")", "［": "[", "］": "]", "｛": "{", "｝": "}",
    "、": ",", "；": ";", "／": "/", "＼": "\\", "｜": "|", "％": "%",
    "＃": "#", "＠": "@", "＆": "&", "＊": "*", "＋": "+", "－": "-",
    "＝": "=", "～": "~", "＄": "$", "＾": "^", "｀": "`", "　": " ",
}
_PUNCT_FOLD_RE = re.compile("|".join(re.escape(k) for k in _PUNCT_FOLD))

_NORM_STRIP_RE = re.compile(
    r"[\s\u3000・·･—\-–_.,;:!?()（）【】《》\"'’‘“”/\\|&+*~^$#@%…‰「」『』〈〉＜＞★☆×″′©®™‖§]+"
)
_YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")
# 目录名尾部的年份：` (2009)` / `(2023-)` / ` - 2019` / ` 2019 年` 等
_YEAR_TAIL_RE = re.compile(
    r"[\(\（\[【]?\s*(?:19|20)\d{2}\s*(?:[-–—~]\s*\d{0,4})?\s*[\)）\]】]?\s*(?:年|Season\s*\d{1,2}?)?\s*$",
    re.IGNORECASE,
)

# 名字末尾的年份后缀：`xxx (2021)` / `xxx（2021）` -> `xxx`
# enrich/豆瓣兜底等所有「往 name 写标题」的地方共用这一份实现（横切只许一套），
# 年份只进 production_year，不进名字。
_YEAR_SUFFIX_RE = re.compile(r"\s*[\(\（]\s*(?:19|20)\d{2}\s*[\)\）]\s*$")


def strip_year_suffix(name):
    """去掉标题末尾的年份后缀（半角/全角括号都认）。"""
    if not name:
        return ""
    return _YEAR_SUFFIX_RE.sub("", str(name)).strip()


# ---- 标题清洗（问题二：横切纪律——写 item.name 的地方只许走这一套） ----
#
# ① 冒号切「主标题：副标题」：TMDB 中文译名常带「：第一季」这类后缀
#    （实例：黑鸟：第一季），冒号后的副标题不该进标题；全角/半角冒号都切。
# ② 控制字符（Tab/换行/响铃…）、零宽/双向控制符是脏数据，绝不能出现在标题里
#    （拷贝、排序、展示都会出怪招）；反斜杠是路径分隔符，`" < > |` 从不出现在
#    正规标题里、又是 XML/HTML/日志的危险字符，一并清掉。
# ③ **保留** `/ ? *`：它们是合法标题字符（Face/Off、What If?、M*A*S*H）——
#    标题只用于展示与匹配、不生成文件名，砍掉会把正确标题改错。
#    冒号已在 ① 处理（切完的主标题里不可能再有冒号）。
_TITLE_SPLIT_RE = re.compile(r"[:：]")
_TITLE_CTRL_RE = re.compile(r'[\x00-\x1f\x7f\\"<>|]')
_TITLE_INVISIBLE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def clean_title(name) -> str:
    """标题清洗：写 ``item.name`` 的唯一入口（TMDB / 豆瓣 / 多源共用，一套口径）。

    顺序：去年份后缀（``strip_year_suffix`` 既有口径）→ 切冒号副标题 →
    清控制/零宽/反斜杠 → 折叠空白。清完为空返回 ``""``，
    调用方**不写空名**（保持原样，等下一轮）。
    """
    s = strip_year_suffix(name)
    if not s:
        return ""
    if _TITLE_SPLIT_RE.search(s):
        head = _TITLE_SPLIT_RE.split(s, 1)[0]
        # 切掉副标题后，主标题尾部悬空的分隔符（"标题 ·：副标题"）一并修剪
        s = head.rstrip(" ·—–-").strip()
        if not s:
            # 「:xxx」这种开头就冒号的怪名：没有可留的主标题，
            # 退回全串并把冒号降级为空格（而不是返回空、让调用方写不进名字）
            s = _TITLE_SPLIT_RE.sub(" ", strip_year_suffix(name))
    s = _TITLE_CTRL_RE.sub(" ", s)
    s = _TITLE_INVISIBLE_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _year_from_tmdb_date(data):
    """从 TMDB 的 first_air_date / release_date 取年份（YYYY-MM-DD）。"""
    for key in ("first_air_date", "release_date"):
        v = (data or {}).get(key)
        if v and len(str(v)) >= 4 and str(v)[:4].isdigit():
            y = int(str(v)[:4])
            if 1900 <= y <= 2100:
                return y
    return None


def _fold_punct(s: str) -> str:
    """全角标点折半角（见 _PUNCT_FOLD）"""
    return _PUNCT_FOLD_RE.sub(lambda m: _PUNCT_FOLD[m.group(0)], s) if s else s


# TMDB 类型 id → 中文名：全项目唯一一份映射（横切纪律：不许有第二套）。
# 搜索接口只给 genre_ids，用它翻译；详情接口给 genres=[{id, name}]，
# name 已是本地化中文、优先用 name，缺 name 时才回退到这张表。
_GENRE_NAMES = {
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪",
    99: "纪录片", 18: "剧情", 10751: "家庭", 14: "奇幻", 36: "历史",
    27: "恐怖", 10402: "音乐", 9648: "悬疑", 10749: "爱情",
    878: "科幻", 10770: "电视电影", 53: "惊悚", 10752: "战争", 37: "西部",
}
_MAX_GENRES = 4


def _genres_from_details(data: dict) -> list:
    """从详情接口 payload 提取类型名（不发请求，纯解析）。

    payload 形态：``genres=[{"id": 28, "name": "动作"}, ...]``。
    name 为空或缺失时回退到 ``_GENRE_NAMES`` 按 id 翻译；都取不到就跳过
    （不写原文 id 数字——搜索路径 apply() 写数字是历史行为，保持不动）。
    """
    out = []
    for g in (data or {}).get("genres") or []:
        name = ""
        gid = None
        if isinstance(g, dict):
            name = (g.get("name") or "").strip()
            gid = g.get("id")
        elif isinstance(g, (int, str)):
            gid = g
        if not name and gid is not None:
            try:
                name = _GENRE_NAMES.get(int(gid), "")
            except (TypeError, ValueError):
                name = ""
        if name and name not in out:
            out.append(name)
        if len(out) >= _MAX_GENRES:
            break
    return out


def _norm_text(s):
    if not s:
        return ""
    return _NORM_STRIP_RE.sub("", _fold_punct(s).lower())


def _clean_query(name):
    """去发行标签与分隔符，返回可用于搜索的干净查询。"""
    # 先折全角标点，否则「剧名：副标题」里的全角冒号会让 TMDB 零结果
    s = _QUERY_JUNK_RE.sub(" ", _fold_punct(name or ""))
    s = re.sub(r"[._\-+]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _first_run(query):
    """首个"标题式"词段：连续的中日韩/拉丁/数字（如"机动战士高达0079"、"X战警"）。

    文件名里标题通常在最前面，后面跟的是集标题/发行信息；取首段能保住
    "0079"这类数字后缀（纯中日韩提取会把它弄丢）。只取含中日韩的首段：
    纯拉丁单词（如 Blossom / 2gether）精确撞上同名不相干条目的概率太高。
    """
    for m in re.finditer(r"[\u4e00-\u9fff\u3040-\u30ffーa-zA-Z0-9]+", query or ""):
        w = m.group(0)
        if len(w) >= 2 and _CJK_RE.search(w):
            return w
    return ""


def _search_candidates(name, year=None):
    """按优先级返回 (候选查询, 是否允许模糊接受)。

    顺序：清洗后全名 → 原名 → 首个标题词段 → 中日韩部分。
    只有"中日韩部分"是信息有损的（丢了拉丁关键词，如演唱会的艺人名），
    它只允许 Tier 2 精确接受，不做模糊——否则"容祖儿 演唱会"会配到
    "容祖儿1314演唱会"（原文件名是 My Secret Live）。

    year：已知年份时追加 "清洗名 YYYY" 候选。parse 已把年份从名字里剥掉，
    但年份可能是标题的一部分（如《Wonder Woman 1984》实为 2020 年电影），
    没有这个候选就永远精确命中不了它。
    """
    # 原始名也要折全角标点：它会直接进 _query_variants 去和 TMDB 标题比对
    raw = _fold_punct((name or "").strip())
    cleaned = _clean_query(name)
    cands = []
    for c, fuzzy in ((cleaned, True), (raw, True), (_first_run(cleaned), True)):
        if c and c not in [x[0] for x in cands]:
            cands.append((c, fuzzy))
    # 剥掉年份再搜一次：目录名几乎都带「(YYYY)」，而归一化后年份仍在字符串里
    # （`Schlag den Star (2009)` → `schlagdenstar2009`），永远匹配不上 TMDB 标题
    # `schlagdenstar`。西方剧几乎全带年份，不剥就是全军覆没（欧美剧 679 条
    # 曾有 129 条因此零命中）。年份已作为 _hit_score 的排序信号单独使用。
    no_year = _YEAR_TAIL_RE.sub(" ", cleaned).strip()
    no_year = re.sub(r"\s+", " ", no_year).strip(" -_.()（）")
    if no_year and no_year != cleaned and no_year not in [x[0] for x in cands]:
        cands.append((no_year, True))
    cjk = " ".join(_CJK_RE.findall(cleaned))
    if cjk and cjk not in [x[0] for x in cands]:
        cands.append((cjk, False))
    if year:
        # 年份可能是标题的一部分（如 Wonder Woman 1984，实为 2020 年电影）：
        # 补一个带年份的候选，否则永远精确命中不了它
        yq = f"{cleaned} {year}".strip()
        if yq and yq not in [x[0] for x in cands]:
            cands.append((yq, True))
    return cands


def _query_variants(query):
    """一个候选查询的归一化变体：全串、中日韩部分。"""
    out = []
    nq = _norm_text(query)
    if nq:
        out.append(nq)
    cjk = _norm_text(" ".join(_CJK_RE.findall(query)))
    if cjk and cjk not in out:
        out.append(cjk)
    return out


def _lcs_len(a, b):
    """最长公共子串长度（短串优化的 DP）。"""
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(a) + 1)
    best = 0
    for cb in b:
        cur = [0] * (len(a) + 1)
        for i, ca in enumerate(a, 1):
            if ca == cb:
                cur[i] = prev[i - 1] + 1
                if cur[i] > best:
                    best = cur[i]
        prev = cur
    return best


def _latin_words(query):
    """查询里的拉丁单词（长度≥4）：艺人/专辑等关键信息，不能丢。"""
    return [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", query or "")]


def _cjk_contain_rank(variants, names, hit):
    """Tier 1.5（C-14）：CJK 包含式匹配的 rank；不达标返回 None。

    短中文标题的 LCS 上限就是标题长度——四字名永远进不了 Tier 1
    （lcs≥6），这是 PR #119 那个坑的精确成因。查询 ≥3 字时改用「字符级
    包含」（子串，或字符全含且标题只多出 2 字以内），并用**语种/地区闸门**
    防错配：TMDB 条目必须是中日韩原生产物。≥3 字这一条先挡掉了
    「虎子」→「老虎和兔子」这类两字名错配；闸门再挡掉拉丁世界同名巧合。
    rank = 标题超长度的负值（越接近 0 = 标题越贴近查询）。
    """
    cjk_ok = (
        (hit.get("original_language") or "") in ("zh", "ja", "ko", "cn", "tw", "hk")
        or any(c in ("CN", "TW", "HK", "JP", "KR")
               for c in (hit.get("origin_country") or []))
        or bool(_CJK_RE.search(hit.get("original_name") or ""))
    )
    if not cjk_ok:
        return None
    for v in variants:
        if re.search(r"[a-z]", v) or len(v) < 3:
            continue
        for hn in names:
            if v in hn or (set(v) <= set(hn) and len(hn) <= len(v) + 2):
                return -(len(hn) - len(v))
    return None


def _confirm_year(score, year, hit):
    """年份二次确认：用解析出的年份（调用方传入），而不是从名字里正则挖。

    名字里的 4 位数字可能是标题的一部分（如《Blade Runner 2049》的 2049、
    《Wonder Woman 1984》的 1984），按它否决会把正确结果打下去。
    同名不同年（Dune 1984 vs 2021）：年份对不上的结果降一级，
    年份对上的胜出——不只看 TMDB 返回顺序。
    year 为 None（没解析出年份）时跳过：没有可靠信号，不做启发式猜测。
    score：(tier, rank) 元组或 Tier 1 的 int rank。
    """
    if not year:
        return score
    qy = str(year)
    hy = (hit.get("first_air_date") or hit.get("release_date") or "")[:4]
    if hy and hy != qy:
        if isinstance(score, tuple):
            return (score[0], score[1] - 1)
        return score - 1
    return score


def _hit_score(raw_name, query, hit, fuzzy_ok=True, year=None):
    """置信度打分：返回 (tier, rank)，tier 大者优先，同 tier 比 rank；None 表拒绝。

    Tier 2（精确）：任一归一化变体与任一标题字段（name/title/原名）精确相等。
    Tier 1（模糊）：纯中日韩变体与标题有足够长的公共子串（防"虎子"→"老虎和兔子"
    类错配）；此时查询里的拉丁关键词必须在标题里出现过（防"S.H.E 安可场"配到
    "蔡依林 安可场"），同 tier 内用"全查询与标题的最长公共子串"排名消歧
    （如"高达0079"应在 SEED 之前选中 0079）。
    Tier 1.5（包含式，C-14）：短中文标题（2~5 字）的 LCS 上限就是标题长度，
    在 Tier 1 的 lcs≥6 口径下结构性永远不达标（PR #119 的坑）。仅在 Tier 1
    未命中时启用：CJK 查询 ≥3 字用字符级包含，并以语种/地区闸门防同名错配。
    另：文件名里有年份而 TMDB 条目年份对不上，直接否决。
    """
    variants = _query_variants(query)
    names = [_norm_text(hit.get(k)) for k in ("name", "title", "original_name", "original_title")]
    names = [n for n in names if n]
    if not variants or not names:
        return None
    for v in variants:
        if v in names:
            return _confirm_year((2, len(v)), year, hit)
    if not fuzzy_ok:
        return None
    latin_ok = all(any(w in hn for hn in names) for w in _latin_words(query))
    if not latin_ok:
        return None
    raw_norm = _norm_text(raw_name)
    best = None
    for v in variants:
        if re.search(r"[a-z]", v):
            continue
        for hn in names:
            lcs = _lcs_len(v, hn)
            short = min(len(v), len(hn))
            if lcs >= 6 and short > 0 and lcs / short >= 0.5:
                rank = _lcs_len(raw_norm, hn)
                if best is None or rank > best:
                    best = rank
    if best is None:
        # Tier 1.5：短中文标题在 LCS≥6 口径下结构性永远不达标；包含式 + 闸门补上。
        # tier 用 1.5：仍低于 Tier 2、高于「无结果」，但排在该条目可用的 Tier 1 之后。
        t15 = _cjk_contain_rank(variants, names, hit)
        if t15 is not None:
            best = (1.5, t15)
    if best is None:
        return None
    best = _confirm_year(best, year, hit)
    return best if isinstance(best, tuple) else (1, best)



def _split_keys(raw: str) -> list[str]:
    """多 key 切分：逗号 / 换行 / 空白分隔，去重保序"""
    parts = re.split(r"[\s,;，；]+", str(raw or ""))
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


def _env_keys() -> list[str]:
    """环境变量里的 key（TMDB_API_KEYS 优先，兼容单键 TMDB_API_KEY）"""
    return _split_keys(os.getenv("TMDB_API_KEYS", "") or os.getenv("TMDB_API_KEY", ""))


def _read_config_value(db) -> str:
    """统一热读（只许这一套）：短 TTL 缓存，保存时失效"""
    from backend.integrations import store
    return store.get_value(db, TMDB_KEYS_CONFIG_KEY, "")


# ---------------------------------------------------------------------------
# 镜像地址与冷却时长：进程内短 TTL 缓存（读多写少，跨进程靠 TTL 兜底）
# ---------------------------------------------------------------------------

_SETTINGS_CACHE: dict = {"at": 0.0, "data": {}}
_settings_lock = threading.Lock()


def normalize_base(raw: str, default: str) -> str:
    """镜像地址归一化：补 https://、去尾部斜杠；空值回落默认"""
    value = str(raw or "").strip()
    if not value:
        return default
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/")


def validate_base(raw: str, field: str) -> str:
    """后台保存镜像地址时的校验：必须是 http(s) 绝对地址，错误要能直接给管理员看"""
    value = str(raw or "").strip()
    if not value:
        return ""  # 空 = 回到官方地址
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{field}不能包含空格")
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{field}必须以 http:// 或 https:// 开头")
    body = value.split("://", 1)[1]
    if not body or "/" == body[0] or "." not in body.split("/")[0]:
        raise ValueError(f"{field}看起来不像完整地址（例如 https://tmdb.example.com）")
    return normalize_base(value, "")


def _config_text(key: str, db=None, *, refresh: bool = False) -> str:
    """读一个 SystemConfig 字符串；读不到返回空（表没建好 / 未配置）

    refresh=True 时绕过 store 的 TTL 热缓存，直查 DB（管理后台 GET 接口
    在保存后立刻读时用，避免 60s TTL 窗口内读到旧值）。
    """
    from backend.integrations import store
    try:
        ttl = 0 if refresh else store.TTL_DEFAULT
        if db is None:
            from backend.database import SessionLocal
            session = SessionLocal()
            try:
                return store.get_value(session, key, "", ttl=ttl)
            finally:
                session.close()
        return store.get_value(db, key, "", ttl=ttl)
    except Exception:  # noqa: BLE001 — DB 没建好时当没配
        return ""


def settings(db=None, *, refresh: bool = False) -> dict:
    """镜像地址与冷却时长（环境变量 > SystemConfig > 默认）

    这是**唯一**读这四个配置的地方：热路径（每次拼图片 URL、每次发请求）都走它，
    内部 30 秒 TTL 缓存，不会每次都开一次数据库会话。
    """
    now = time.monotonic()
    with _settings_lock:
        if not refresh and (now - _SETTINGS_CACHE["at"]) < SETTINGS_TTL_SEC \
                and _SETTINGS_CACHE.get("data"):
            return dict(_SETTINGS_CACHE["data"])
    env_api = (os.getenv("TMDB_API_BASE") or "").strip()
    env_image = (os.getenv("TMDB_IMAGE_BASE") or "").strip()
    env_cooldown = (os.getenv("TMDB_KEY_COOLDOWN_SEC") or "").strip()
    env_invalid = (os.getenv("TMDB_KEY_INVALID_COOLDOWN_SEC") or "").strip()
    data = {
        "api_base": normalize_base(env_api or _config_text(TMDB_API_BASE_CONFIG_KEY, db),
                                   TMDB_API_DEFAULT),
        "image_base": normalize_base(env_image or _config_text(TMDB_IMAGE_BASE_CONFIG_KEY, db),
                                     TMDB_IMAGE_DEFAULT),
        # 配置瘦身第 3c 批：env > DB > 默认（此前只读了 env，DB 键 unreachable，
        # 与本函数 docstring 的口径不符；补上 DB 一层后冷却时长才真正可配）
        "cooldown_sec": _positive_float(
            env_cooldown or _config_text(TMDB_KEY_COOLDOWN_CONFIG_KEY, db, refresh=refresh),
            DEFAULT_KEY_COOLDOWN_SEC, 1.0),
        "invalid_cooldown_sec": _positive_float(
            env_invalid or _config_text(TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY, db, refresh=refresh),
            DEFAULT_KEY_INVALID_COOLDOWN_SEC, 1.0),
        "api_base_from_env": bool(env_api),
        "image_base_from_env": bool(env_image),
    }
    with _settings_lock:
        _SETTINGS_CACHE.update({"at": now, "data": dict(data)})
    return data


def _positive_float(raw: str, default: float, floor: float) -> float:
    try:
        return max(floor, float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def invalidate_settings() -> None:
    """保存镜像 / 冷却配置后立刻失效缓存（同进程即时生效，跨进程靠 TTL）"""
    with _settings_lock:
        _SETTINGS_CACHE.update({"at": 0.0, "data": {}})


_LANGUAGE_CACHE: dict = {"at": 0.0, "value": ""}


def preferred_language(db=None, *, refresh: bool = False) -> str:
    """TMDB 首选语言：环境变量 TMDB_LANGUAGE > SystemConfig > zh-CN。

    与 settings() 同一套口径：进程内短 TTL 缓存（读多写少），保存时
    invalidate_language() 立刻失效。非法值回落到默认（防手写 DB 搞坏）。

    refresh=True 时绕过 _LANGUAGE_CACHE 和 store TTL 缓存，直查 DB
    （管理后台 GET /scrape/tmdb-language 在保存后立刻读时用）。
    """
    now = time.monotonic()
    with _settings_lock:
        if not refresh and (now - _LANGUAGE_CACHE["at"]) < SETTINGS_TTL_SEC \
                and _LANGUAGE_CACHE["value"]:
            return _LANGUAGE_CACHE["value"]
    env_lang = (os.getenv("TMDB_LANGUAGE") or "").strip()
    cfg_lang = (_config_text(TMDB_PREFERRED_LANGUAGE_CONFIG_KEY, db,
                            refresh=refresh) or "").strip()
    value = env_lang or cfg_lang or TMDB_PREFERRED_LANGUAGE_DEFAULT
    if value not in TMDB_LANGUAGE_OPTIONS:
        value = TMDB_PREFERRED_LANGUAGE_DEFAULT
    with _settings_lock:
        _LANGUAGE_CACHE.update({"at": now, "value": value})
    return value


def invalidate_language() -> None:
    """保存首选语言后立刻失效缓存（同进程即时生效，跨进程靠 TTL）"""
    with _settings_lock:
        _LANGUAGE_CACHE.update({"at": 0.0, "value": ""})


def poster_language(db=None) -> str:
    """海报语言偏好：SystemConfig > system（跟随系统）。

    对标 StrmAssistant #10「获取原语言海报」。
    """
    try:
        value = (_config_text(TMDB_POSTER_LANGUAGE_CONFIG_KEY, db) or "").strip()
    except Exception:
        value = ""
    if value not in TMDB_POSTER_LANGUAGE_OPTIONS:
        value = TMDB_POSTER_LANGUAGE_DEFAULT
    return value


def tmdb_text_fill_only(db=None) -> bool:
    """批2：TMDB 文字字段是否只填空、不覆盖（默认 True=新行为）。

    口径：环境变量 ENRICH_TMDB_TEXT_FILL_ONLY > SystemConfig
    ``enrich_tmdb_text_fill_only`` > 默认开。配成 "0" 即回退旧的覆盖行为
    （回滚开关）。进程内 30 秒 TTL 缓存，与 settings() 同口径。
    """
    now = time.monotonic()
    with _settings_lock:
        if db is None and (now - _TEXT_FILL_CACHE["at"]) < _TEXT_FILL_TTL_SEC:
            return _TEXT_FILL_CACHE["value"]
    env = (os.getenv("ENRICH_TMDB_TEXT_FILL_ONLY") or "").strip()
    raw = env if env else (_config_text(TMDB_TEXT_FILL_ONLY_CONFIG_KEY, db) or "").strip()
    value = raw != "0"  # 缺省/空/"1" → 只填空；显式 "0" → 回退覆盖
    with _settings_lock:
        _TEXT_FILL_CACHE.update({"at": now, "value": value})
    return value


def api_base(db=None) -> str:
    """当前生效的 API 基础地址（镜像 / 反代）"""
    return settings(db)["api_base"]


def image_base(db=None) -> str:
    """当前生效的图片 CDN 基础地址（与 API 分开配）"""
    return settings(db)["image_base"]


def _db_keys(db=None) -> list[str]:
    """SystemConfig 里的 key。db 未给时自己开短会话；读不到返回空（不抛异常）"""
    try:
        if db is None:
            from backend.database import SessionLocal
            session = SessionLocal()
            try:
                raw = _read_config_value(session)
            finally:
                session.close()
        else:
            raw = _read_config_value(db)
    except Exception:  # noqa: BLE001 — DB 没建好 / 表不存在时当没配
        return []
    return _split_keys(raw)

# 缓存未命中的哨兵：TMDB 的“没搜到”也是合法结果，必须与“没查过”区分开
_MISS = object()


def image_specs(*payloads) -> list[tuple[str, str]]:
    """一次刮削要落的图片 ``(kind, url)`` 列表——IO 阶段按这个预热，写库阶段按这个落字段

    必须是单一口径：``_set_image`` 在写事务里**不再下载**，所以预热要看**同一份** URL，
    否则本地化会静默失效（w500 海报 / w1280 背景这两个尺寸不要在任何地方再拼一遍）。
    """
    out: list[tuple[str, str]] = []
    # 图片地址走**可配的 CDN 镜像**：境内直连 image.tmdb.org 不稳时，单独改这一项就行
    base = image_base()
    for data in payloads:
        if not data:
            continue
        poster = data.get("poster_path")
        backdrop = data.get("backdrop_path")
        if poster:
            out.append(("Primary", f"{base}/w500{poster}"))
        if backdrop:
            out.append(("Backdrop", f"{base}/w1280{backdrop}"))
    return out


def prewarm_images(*payloads, extra=()) -> int:
    """IO 阶段把这一批刮削要落的图先下到本地（写库阶段只落字段，v2.42.9）

    ``payloads`` 是 TMDB 的 hit / details（按 ``image_specs`` 拼 URL）；``extra`` 是不走
    TMDB 尺寸规则的图（豆瓣 / Bangumi 兜底源给的原图 URL）。

    写事务里的 ``_set_image`` 是 ``allow_download=False``：只认已经在本地的那份。所以只要
    要调 ``apply`` / ``apply_images``，就应在**写事务之前**调一次这里。预热失败或被并发旋钮
    挡住（``image_store._STATS["throttled"]``）只是「这轮不本地化」，取图时会按需补一份。
    """
    try:
        urls = [url for _kind, url in image_specs(*payloads)]
        urls += [str(u) for u in extra if u]
        return image_store.prewarm(urls) if urls else 0
    except Exception as exc:  # noqa: BLE001 — 预热失败不影响刮削
        logger.debug("图片预热失败: %s", exc)
        return 0


# 演员表一次取几个（TMDB cast 按戏份排序，取前 10 足够详情页展示）
TMDB_CAST_LIMIT = 10


def _has_credits(data) -> bool:
    """details 载荷里有没有 credits：v2.51.0 前缓存的老载荷没有这个键。

    ``details()`` 发现缓存里是老载荷时视为过期、重新拉一次——之后 30 天内
    都是带 credits 的新载荷，不会反复重拉。
    阴性缓存（请求失败记的 None）返回 True：保持「TTL 内不再重打」的原语义，
    重试由补全 worker 自己的退避调度负责。
    """
    if data is None:
        return True
    return isinstance(data, dict) and isinstance(data.get("credits"), dict)


_CJK_ORIGINAL_LANGS = {"zh", "cn"}
_HAN_RE = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ROMANIZED_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]*(?: [A-Za-z][A-Za-z'\-]*){0,3}$")


def is_romanized_role(name: str, role: str) -> bool:
    """演员名是汉字、角色名却是纯拉丁字母的短串（多为拼音）→ True。

    只在调用方已确认是华语作品时使用：英文片里中国演员演英文角色是正常的。
    """
    if not role or not name:
        return False
    return bool(_HAN_RE.search(name)) and not _HAN_RE.search(role) \
        and bool(_ROMANIZED_RE.match(role.strip()))


def runtime_ticks_from(details: Optional[dict]) -> int:
    """TMDB 载荷里的片长（分钟）→ 100ns ticks。

    电影/单集是 ``runtime``；剧集是 ``episode_run_time``（列表，取第一个正数）。
    没有返回 0。
    """
    if not isinstance(details, dict):
        return 0
    cands = [details.get("runtime")]
    ert = details.get("episode_run_time")
    if isinstance(ert, list):
        cands.extend(ert)
    for v in cands:
        try:
            minutes = int(v or 0)
        except (TypeError, ValueError):
            continue
        if 0 < minutes < 24 * 60:
            return minutes * 60 * 10_000_000
    return 0


def apply_runtime(item, details: Optional[dict]) -> bool:
    """元数据片长只补空（真实时长来自探测，存在 duration_ticks，两者互不覆盖）。"""
    ticks = runtime_ticks_from(details)
    if ticks and not (getattr(item, "metadata_runtime_ticks", 0) or 0):
        item.metadata_runtime_ticks = ticks
        return True
    return False


def cast_list(details: Optional[dict], limit: int = TMDB_CAST_LIMIT) -> list[dict]:
    """从 details 载荷（``append_to_response=credits``）里取前 N 个演员。

    返回 ``[{"name": 演员名, "role": 饰演角色, "image": 头像 URL,
    "sort_order": 原顺序, "person_id": TMDB person id}]``。头像尺寸用 w185
    （TMDB 标准头像尺寸）。

    **单一口径**：``_enrich_fetch`` 用它拼预热 URL，``_enrich_apply`` 用它写
    ``emby_people``——两边看到的演员表必须完全一致，否则预热和落库会对不上
    （预热了 A 的头像、落库了 B 的名字）。
    """
    if not details:
        return []
    cast = (details.get("credits") or {}).get("cast") or []
    base = image_base()
    cjk_title = str(details.get("original_language") or "").lower() in _CJK_ORIGINAL_LANGS
    out: list[dict] = []
    for c in cast[: max(0, int(limit))]:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        profile = c.get("profile_path")
        role = str(c.get("character") or "").strip()
        if cjk_title and is_romanized_role(name, role):
            # 华语片的角色名在 TMDB 上常被录成拼音（「沦陷」：嘉羿 → "Zhan Wang"=展望），
            # 客户端在演员名下面显示一串拼音像是把英文名当成了角色。不知道中文角色名
            # 时宁可留空；豆瓣有中文角色名时由 refresh_person_worker 补上。
            role = ""
        out.append({
            "name": name,
            "role": role,
            "image": f"{base}/w185{profile}" if profile else "",
            "sort_order": len(out),
            # StrmAssistant #9 对标：存 person tmdb_id，供后续刷新演员详情用
            "person_id": str(c.get("id") or ""),
        })
    return out


def _set_image(item: emby_models.MediaItem, kind: str, url: str) -> None:
    """落一个刮削到的图片地址；本地文件**已预热**时同时落本地路径

    远程地址**始终**保留（唯一事实来源）：本地那份只是缓存，被清掉/被删掉都能自愈——
    取图时若发现本地文件不在了，会按需再落一份（见 media_routes.item_image）。

    这个函数只在**写事务里**被调用，所以这里绝不发 HTTP（``allow_download=False``）：
    下载统一在 IO 阶段做（``image_specs`` + ``image_store.prewarm``）。在此处下载一张图
    超时 15s，就等于攥着数据库写锁 15s，而且与 scanner 里「写库不调网络」的声明相矛的
    （v2.42.9 之前的实情）。没预热上的图只是「这轮不本地化」，取图时会按需补一份。
    """
    if kind == "Backdrop":
        item.backdrop_image_url = url
    else:
        item.primary_image_url = url
    local = image_store.localize(url, allow_download=False)
    if not local:
        return
    if kind == "Backdrop":
        item.backdrop_path = local
    else:
        item.poster_path = local


def extract_air_dates(data: dict) -> tuple[Optional[int], Optional[datetime]]:
    """从 TMDB 负载（search hit / details）提取 (production_year, premiere_date)。

    纯函数：只读 ``data``、不碰 ``item``。search hit 与 details 同形状
    （``first_air_date`` / ``release_date``），供 ``apply()`` /
    ``apply_details()`` 与一次性回填脚本共用——年份口径全仓库只有这一套。
    """
    raw = str((data or {}).get("first_air_date")
              or (data or {}).get("release_date") or "").strip()
    m = re.match(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$", raw)
    if not m:
        return None, None
    try:
        year = int(m.group(1))
    except (TypeError, ValueError):  # pragma: no cover — 正则已保证 4 位数字
        return None, None
    premiere: Optional[datetime] = None
    if m.group(2) and m.group(3):
        try:
            premiere = datetime(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            # "2021-13-99" 这类脏日期：年份照取，完整日期不要
            premiere = None
    return year, premiere


def _fill_air_dates(item, data: dict) -> None:
    """把 TMDB 的首播/上映日期落到条目：**只补缺项**。

    ``apply()`` 与 ``apply_details()`` 共用——两条路口径必须一致，
    否则新刮的 series 还是会丢年份（P0-1 的根因）。
    已有值（NFO / 用户整理 / 豆瓣兜底先落的）一律不覆盖。
    """
    year, premiere = extract_air_dates(data)
    if year is not None and not getattr(item, "production_year", None):
        item.production_year = year
    if premiere is not None and not getattr(item, "premiere_date", None):
        item.premiere_date = premiere


def _is_placeholder_episode_title(name):
    """单集占位标题判定（严格：宁可漏判不许误伤真实标题）。

    占位名来源：扫描器命名（「剧名 第 N 集 S01E01」「第N集」等），
    与生产实测（2026-10-15：12.6 万集中约 11 万占位）一致。
    注意：NFO 从不给 episode 写标题（nfo.py apply_nfo 只处理 movie/series
    的 title），所以这里不会误判 NFO 标题为占位。
    """
    n = (name or "").strip()
    if not n:
        return True
    if re.match(r"^第\s*\d+\s*集$", n):
        return True
    # 「剧名 第 N 集 S01E01」类（含版本 tag 如 PTerWEB）
    if re.match(r"^.+ 第\s*\d+\s*集( .+)? S\d{1,2}E\d{1,3}$", n):
        return True
    return False


class TmdbClient:
    """轻量 TMDB 客户端（未配置 key 时静默跳过）

    支持多密钥轮询：`TMDB_API_KEYS=key1,key2,key3`（或 `TMDB_API_KEY` 单键）。
    密钥来源：环境变量优先，为空时回落 SystemConfig（管理后台「元数据与刮削」填写，
    保存后调 `refresh_keys()` 即时生效，无需重启）。
    单个密钥超出配额（429）或报 401 时自动轮到下一个，整库刮削不会因为一个 key 限额就停滞。

    带短 TTL 缓存：扫库时引擎会先在后台线程把「搜索 + 详情」预热，写库线程随后
    调用的同一请求直接命中缓存——既能并行，又不会重复消耗 TMDB 配额。
    """

    _CACHE_TTL = 300        # 秒；一次扫描的预热结果在这个窗口内有效
    _CACHE_MAX = 20000      # 缓存条数上限，满时按写入顺序淘汰最旧的一批（保留大部分预热结果）
    # 淘汰到只剩这个比例：一万部片子的库刚好是两万条，整表清空会顺手扔掉刚预热好的条目，
    # 写库线程接着取同一个片名就变成一次真的网络请求（见 _cache_put 的说明）
    _CACHE_KEEP_RATIO = 0.75
    _cache: dict = {}
    _cache_lock = threading.Lock()

    def __init__(self) -> None:
        self._keys_lock = threading.Lock()
        # 当前 key 来源：env（环境变量）/ db（后台填写）/ none（未配置），界面展示用
        self.key_source = "none"
        self.api_keys: list[str] = []
        self.api_key = ""
        self._key_index = 0
        # 密钥池健康：key -> {"until": 单调时钟截止, "reason", "hits"}
        # 冷却是**进程内**状态（同令牌桶的性质）：重启后全部重新试一遍，
        # 写进配置反而会变成“记得某个 key 坏过”这种陈旧结论。
        self._cooldown: dict = {}
        self.session = None
        self._session_proxy_sig: Optional[str] = None
        self._session_lock = threading.Lock()
        # 请求级限流器：所有 TMDB 调用（扫描 + 补全）共用（v2.42.9）；
        # v2.57.0 起按 key 独立成桶（_KeyedLimiter），有效上限 = key 数 × TMDB_PER_SEC
        self._limiter = _KeyedLimiter()
        # 运行观测：短路 / 重试 / 网络失败（429 次数在 _limiter 里，那边是加锁的）
        self._stats = {"short_circuit": 0, "retry": 0, "net_fail": 0}
        self.refresh_keys()  # 环境变量优先，为空则回落 SystemConfig
        # v2.56.0：启动时随机轮转起点——多 worker/多进程同时启动时不要齐步用
        # 同一把 key，避免规律性被 TMDB 风控（反风控原则：请求间隔随机化）。
        # 注意：随机只做这一次，之后的 _pick_key 保持确定性轮转（k1→k2→…），
        # 否则每次调用都随机既不可测试、也让 _key_index 语义失效。
        if len(self.api_keys) > 1:
            self._key_index = random.randrange(len(self.api_keys))
            self.api_key = self.api_keys[self._key_index]
        self._ensure_session()

    def _set_keys(self, keys: list[str], source: str) -> None:
        with self._keys_lock:
            self.api_keys = list(keys)
            self.api_key = self.api_keys[0] if self.api_keys else ""
            self._key_index = 0
            self.key_source = source if keys else "none"
            # 已被换掉的 key 不再留着冷却记录（它不在池子里了，记着也没用）
            alive = set(self.api_keys)
            self._cooldown = {k: v for k, v in self._cooldown_map().items() if k in alive}
            # v2.57.0 item3：key 池变化时同步各 key 的独立令牌桶
            # （getattr 防御：测试可能已把 _limiter 换成普通桶）
            sync = getattr(getattr(self, "_limiter", None), "sync_keys", None)
            if callable(sync):
                sync(self.api_keys)

    def refresh_keys(self, db=None) -> dict:
        """重算有效密钥：后台保存与进程启动都走这里，无需重启进程。

        优先级：环境变量 ``TMDB_API_KEYS``（非空即用）> SystemConfig ``tmdb_api_keys``。
        返回来源与数量（不含原文，可直接给界面）。
        """
        keys = _env_keys()
        source = "env"
        if not keys:
            keys = _db_keys(db)
            source = "db" if keys else "none"
        self._set_keys(keys, source)
        self._ensure_session()
        return {"source": self.key_source, "count": len(self.api_keys)}

    def _keyed_limiter(self, key: str) -> "_RequestLimiter":
        """取某次请求该走的令牌桶：按 key 独立（v2.57.0 item3）。

        测试会整体替换 client._limiter 为普通 _RequestLimiter——此时没有
        _per_key 属性，直接返回被替换的桶，测试语义不变。
        """
        lim = self._limiter
        if getattr(lim, "_per_key", None) is None:
            return lim
        return lim.limiter_for(key)

    def masked_keys(self) -> list[str]:
        """界面展示用：只露后 4 位"""
        return [f"****{k[-4:]}" if len(k) > 4 else "****" for k in self.api_keys]

    def _ensure_session(self) -> None:
        """按需创建 / 重建 HTTP 会话——让后台改完「网络代理」对刮削生效

        进程范围注意（r30 F4）：``proxy.apply()`` 只改**本进程**的环境变量，
        而保存动作发生在 API 进程，enrich_worker 跑在 worker 进程
        （main.py ``if not _is_api_role`` 分支）。worker 进程只在启动时
        ``apply_all()`` 一次，运行时改代理不会更新它的环境，所以指纹在
        worker 进程里永远比不出变化——「改完立刻生效」只对 API 进程内的
        刮削调用成立，worker/EA 进程需要重启才生效。

        httpx 只在**构造 client 时**读一遍代理环境变量（``trust_env``），建好之后改环境不再
        有效；而刮削客户端是进程级单例、活得比一次扫描久得多。不重建的话，管理员填完代理
        依然会看到「TMDB 连不上」，只能靠重启进程解决。

        这里不依赖「保存代理时通知谁」，而是每次请求前比一次代理指纹：进程内任何路径改了
        代理（``apply`` / ``apply_all``，将来别处也一样）都会被察觉，指纹没变时只是拼一次
        字符串，不会每次请求都换连接。
        """
        if not self.api_keys:
            return
        from backend.integrations import proxy

        sig = proxy.signature()
        with self._session_lock:
            if self.session is not None:
                if self._session_proxy_sig is None:
                    # 会话不是这里建的（密钥/会话后来被注入，测试也这么接）：只补记指纹，
                    # 不在别人刚换上的会话上动手。
                    self._session_proxy_sig = sig
                    return
                if sig == self._session_proxy_sig:
                    return
            import httpx

            # timeout 8 → 10：带 append_to_response 的详情请求在跨境链路上 8 秒偏紧。
            # 重试统一交给 _request()（连接错误与读超时一视同仁，见那里）——**不要**在这里
            # 传自定义 transport：一旦显式给 transport，httpx 就不再按环境变量挂代理了，
            # 后台配的代理会静默失效（scripts/smoke_test_capabilities.py 钉住了这一点）。
            stale, self.session = self.session, httpx.Client(timeout=TMDB_TIMEOUT)
            self._session_proxy_sig = sig
        if stale is not None:
            # 可能还有别的扫描线程正拿旧会话取数据：它会拿到一次异常，_get 按「一次失败的
            # 网络请求」处理（条目这轮不刮削，下轮再来）。重建只发生在管理员改代理时。
            try:
                stale.close()
            except Exception:  # noqa: BLE001
                pass
            logger.info("TMDB 客户端已按新的代理设置重建连接")

    def _rotate(self) -> bool:
        """轮到下一个密钥，全部用过则返回 False（保留给旧调用方）"""
        if len(self.api_keys) <= 1:
            return False
        self._key_index = (self._key_index + 1) % len(self.api_keys)
        self.api_key = self.api_keys[self._key_index]
        logger.warning("TMDB 密钥轮询到第 %s 个", self._key_index + 1)
        return True

    # ---------------- 密钥池：轮换 + 逐把冷却 ----------------

    def _key_lock(self):
        """密钥锁（防御式：测试里会用 ``__new__`` 绕开 ``__init__`` 造客户端）"""
        lock = getattr(self, "_keys_lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    def _cooldown_map(self) -> dict:
        """冷却表（防御式取：测试里会用 ``__new__`` 绕开 ``__init__`` 造客户端）"""
        data = getattr(self, "_cooldown", None)
        if data is None:
            data = {}
            self._cooldown = data
        return data

    def _pick_key(self, exclude=(), *, allow_cooled: bool = False) -> str:
        """从池子里挑一把能用的 key（轮转起点，避免总是第一把先死）

        ``exclude`` 是**这一次调用已经试过**的 key：冷却是给后续请求用的，
        不能让一把刚被判 429 的 key 在同一次调用里立刻又被选上（那会变成死循环）。
        ``allow_cooled=True`` 是兵底：全池都在冷却时也得把请求发出去，
        冷却是优化，不是锁死。
        """
        keys = self.api_keys
        if not keys:
            return ""
        excluded = set(exclude)
        now = time.monotonic()
        # 轮转起点：默认从第一把开始（裸客户端没有 _key_index 时也是从 0 起）。
        # v2.56.0 修：随机偏移只在进程启动时做一次（见 __init__），不要在每次
        # _pick_key 都加——那样轮转完全不可预测，既破坏确定性测试，也让
        # _key_index 的"下一把"语义失效（每次都被随机起点带偏）。
        start = int(getattr(self, "_key_index", 0) or 0) % max(1, len(keys))
        with self._key_lock():
            total = len(keys)
            for step in range(total):
                idx = (start + step) % total
                key = keys[idx]
                if key in excluded:
                    continue
                entry = self._cooldown_map().get(key)
                if entry and entry.get("until", 0) > now and not allow_cooled:
                    continue
                self._key_index = (idx + 1) % total
                self.api_key = key
                return key
        return ""

    def _cool_key(self, key: str, reason: str, seconds: float) -> None:
        """把一把 key 放进冷却，并记下原因（界面上要能写出“为什么这把在休息”）"""
        seconds = max(1.0, float(seconds or 0))
        with self._key_lock():
            entry = self._cooldown_map().setdefault(key, {"until": 0.0, "reason": "", "hits": 0})
            entry["until"] = max(float(entry.get("until") or 0), time.monotonic() + seconds)
            entry["reason"] = reason
            entry["hits"] = int(entry.get("hits") or 0) + 1
        logger.warning("TMDB 密钥 ****%s 冷却 %.0f 秒（%s）", key[-4:] if key else "", seconds, reason)

    def note_key_throttled(self, key: str, retry_after: Optional[float] = None) -> float:
        """429：按 Retry-After（读不到则用配置值）冷却这把 key，返回建议等待秒数"""
        wait = float(retry_after) if retry_after and retry_after > 0 else 0.0
        if wait <= 0:
            wait = float(settings()["cooldown_sec"])
        self._cool_key(key, "限流（HTTP 429）", min(wait, TMDB_RETRY_AFTER_CAP_SEC) or wait)
        return wait

    def note_key_invalid(self, key: str) -> None:
        """401：这把 key 基本废了，冷却更久（等管理员换掉它）"""
        self._cool_key(key, "无效（HTTP 401）", float(settings()["invalid_cooldown_sec"]))

    def clear_cooldowns(self) -> int:
        """手动清除全部冷却（管理员换完 key / 网络恢复后用）"""
        with self._key_lock():
            count = len(self._cooldown_map())
            self._cooldown_map().clear()
        return count

    def key_pool(self, db=None) -> list:
        """密钥池快照（只给掩码与状态，**不返回原文**）

        冷却剩余秒数是运行时事实，前端据此显示“这把在休息，还要 xx 秒”。
        """
        now = time.monotonic()
        rows: list[dict] = []
        for idx, key in enumerate(self.api_keys):
            with self._key_lock():
                entry = dict(self._cooldown_map().get(key) or {})
            remaining = max(0.0, float(entry.get("until") or 0) - now)
            rows.append({
                "index": idx + 1,
                "masked": f"****{key[-4:]}" if len(key) > 4 else "****",
                "current": key == self.api_key,
                "cooling": remaining > 0,
                "cooldown_remaining": int(round(remaining)),
                "reason": entry.get("reason") or "",
                "hits": int(entry.get("hits") or 0),
            })
        return rows

    def _throttle(self, key: str, retry_after: Optional[float] = None) -> float:
        """429：压低**这把 key**的桶速率（v2.57.0 item3：配额按 key 算，只罚肇事 key），
        返回建议退避秒数（睡不睡由调用方决定）。"""
        return self._keyed_limiter(key).note_throttled(retry_after)

    def _request(self, path: str, params: dict, key: str = ""):
        """单次 GET：先过**请求级**令牌桶，再按 TMDB_NET_RETRIES 退避重试网络异常

        连接错误与读超时都在这里重试（httpx 默认的连接级重试不覆盖读超时，而跨境
        链路上更常见的恰恰是读超时）；一次抖动不再让条目这轮刮不上、要等下一轮
        补全才回来。
        """
        if not self.session:
            return None
        # 首选语言在这里统一注入：所有 TMDB 请求都带上。调用方如果显式传了
        # language（极少数特殊接口），显式值仍然优先。
        payload = {"language": preferred_language(), **params, "api_key": key or self.api_key}
        base = api_base()
        delay = 0.5
        for attempt in range(TMDB_NET_RETRIES + 1):
            # 重试也要重新取 token：一次重试就是一次真的请求，配额照样要花。
            # v2.57.0 item3：按 key 独立取 token（_keyed_limiter 内部路由）
            self._keyed_limiter(key).acquire()
            try:
                # v2.42.9：每一次真实发出的 HTTP 都计进 tmdb_req（含重试与换 key 后的重试）
                with progress.stage_timer("tmdb_req"):
                    return self.session.get(f"{base}{path}", params=payload)
            except Exception as e:  # noqa: BLE001 — 网络异常不应中断整次扫描
                if attempt >= TMDB_NET_RETRIES:
                    self._stats["net_fail"] += 1
                    logger.warning("TMDB 请求失败 %s（重试 %s 次后放弃）: %s", path, attempt, e)
                    return None
                self._stats["retry"] += 1
                # 加抖动：多个 worker 同时撞上抖动时不要齐步重打
                time.sleep(delay + random.random() * 0.25)
                delay = min(delay * 2, 4.0)
        return None

    def _get(self, path: str, params: dict) -> Optional[dict]:
        """带密钥轮询的 GET：限流 → 网络重试 → 429 退避/换 key（v2.42.9 收口）

        旧实现的限速是「条目/秒」且只在补全那条路上：一个条目背后是 0~6 次 HTTP，
        于是「2/秒」实际打出去 4~12 请求/秒，而扫描那条路完全没限速。现在两条路都
        收敛到 `_request()` 的同一个桶上，口径与 TMDB 配额一致（``tmdb_req`` 阶段计数
        记的是**请求**，与这个桶同一个口径；旧的条目级桶记的是条目，两者对不上正是
        积压期最容易看错的地方）。

        429 也不再是「单 key 直接放弃」：真的读 `Retry-After` 并据此退避，同时把
        速率自适应减半——否则后续条目会在同一个窗口里继续把配额撞满。

        **瞬态失败上抛** ``TmdbTransientError``（网络重试耗尽、5xx、解析失败、
        全 key 429），让调用方把条目送回重试队列；404/401/未配置照旧返回
        ``None``（阴性语义，不烧重试预算）。
        """
        self._ensure_session()
        if not self.session:
            return None
        total = max(1, len(self.api_keys))
        tried: set[str] = set()
        last_wait = 0.0
        saw_429 = False
        while True:
            # 先跳在冷却里的 key；全都在冷却时宁可硬用一把（冷却是优化，不是锁死）
            key = self._pick_key(tried) or self._pick_key(tried, allow_cooled=True)
            if not key:
                break
            tried.add(key)
            r = self._request(path, params, key)
            if r is None:
                # 网络异常已在 _request 里按 TMDB_NET_RETRIES 退避重试过，到这
                # 说明抖动持续存在：抛瞬态异常，条目回重试队列（吞成 None 会被
                # 写成终态 none，且 L1 会把失败当空结果缓存 300 秒——时好时坏）
                raise TmdbTransientError(f"TMDB 网络请求失败（重试耗尽）: {path}")
            status = r.status_code
            if status in (401, 429):
                if status == 429:
                    wait = self._throttle(key, _retry_after_seconds(r))
                    self.note_key_throttled(key, wait)
                    saw_429 = True
                else:
                    wait = 0.0
                    self.note_key_invalid(key)
                last_wait = max(last_wait, wait)
                # 还有别的 key 就换一把（配额是按 key 算的，每把 key 只试一次）
                if len(tried) < total:
                    if wait > 0:
                        time.sleep(min(wait, 1.0))
                    continue
                break
            if status >= 500:
                # TMDB/网关侧的临时错误：瞬态，转重试队列（404/400 仍按阴性处理）
                logger.warning("TMDB 服务端错误 %s: HTTP %s（瞬态，转重试）", path, status)
                raise TmdbTransientError(f"TMDB 服务端错误 HTTP {status}: {path}")
            if status >= 400:
                logger.warning("TMDB 响应异常 %s: HTTP %s", path, status)
                return None
            try:
                data = r.json()
            except Exception as exc:  # noqa: BLE001 — 200 却解析不出（多半是网关错误页）
                logger.warning("TMDB 响应解析失败 %s: %s", path, exc)
                raise TmdbTransientError(f"TMDB 响应解析失败: {path}") from exc
            # 成功一次就清掉这把的失败计数（它可能只是碰到了临时配额窗口）
            with self._key_lock():
                entry = self._cooldown_map().pop(key, None)
            if entry:
                logger.info("TMDB 密钥 ****%s 恢复正常", key[-4:])
            self._keyed_limiter(key).note_success()
            return data
        if last_wait > 0:
            # 全部 key 都撞了限流：先退避一轮再放行后续请求（这段窗口内的条目
            # 落到重试队列，而不是继续把已经超限的配额撞满）
            time.sleep(last_wait)
        if saw_429:
            # 限流是配额窗口问题（瞬态）：抛出去让条目退避重试；
            # 只有 401/无 key 这种「配置坏了」才按阴性返回 None
            raise TmdbTransientError(f"TMDB 全部密钥限流（HTTP 429）: {path}")
        logger.warning("TMDB 全部密钥不可用或已用尽: %s", path)
        return None

    @property
    def configured(self) -> bool:
        return bool(self.api_keys)

    def stats(self) -> dict:
        """运行观测：实际限速、429 次数、网络重试/失败、Tier 2 短路次数（v2.42.9）

        限速是**自适应**的（撞 429 减半、连续成功再慢慢加回来），所以「配置里写的
        ENRICH_TMDB_PER_SEC」并不等于「实际在打的速率」——管理后台要看这里的实际值。
        """
        return {
            "rate": round(self._limiter.rate, 3),
            "ceiling": round(self._limiter.ceiling, 3),
            "throttled": self._limiter.throttled_count,
            "retries": self._stats["retry"],
            "net_fail": self._stats["net_fail"],
            "short_circuits": self._stats["short_circuit"],
            "keys_cooling": len([row for row in self.key_pool() if row["cooling"]]),
        }

    def _cache_get(self, key):
        """取缓存（未命中返回 _MISS；过期视为未命中，顺手删掉）"""
        with TmdbClient._cache_lock:
            hit = TmdbClient._cache.get(key)
            if hit is None:
                return _MISS
            ts, value = hit
            if (datetime.now() - ts).total_seconds() > self._CACHE_TTL:
                # 过期条目留着只会占名额：满载时它会把还在窗口内的预热结果一起挤出去
                TmdbClient._cache.pop(key, None)
                return _MISS
            return value

    def _cache_put(self, key, value) -> None:
        """写缓存：满了先清过期，再按写入顺序淘汰最旧的一批（不再是整表清空）

        整表清空看似简单，代价却是「把自己刚预热好的结果扔掉」：写库线程随后要取同一个
        片名，缓存已经空了，于是同一个请求又发一次。扫描规模越接近上限越容易撞上
        （一万部片子的库：搜索 + 详情刚好两万条），配额也就跟着白花。
        """
        now = datetime.now()
        with TmdbClient._cache_lock:
            cache = TmdbClient._cache
            if key not in cache and len(cache) >= self._CACHE_MAX:
                for stale in [k for k, (ts, _) in cache.items()
                              if (now - ts).total_seconds() > self._CACHE_TTL]:
                    cache.pop(stale, None)
                keep = max(1, int(self._CACHE_MAX * self._CACHE_KEEP_RATIO))
                # 多淘汰一条：给正在写的这个新条目腾位置（否则下一次写入又会触发一轮淘汰）
                for old in list(cache)[:max(0, len(cache) - keep + 1)]:
                    cache.pop(old, None)
            cache[key] = (now, value)

    def _search_raw(self, query: str, year: Optional[int], kind: str,
                    lang: Optional[str] = None) -> list:
        """原始搜索（带两级缓存），返回 results 列表。

        L1 进程内（300 秒，同一次扫描的预热/写库复用）→ L2 磁盘
        （``tmdb_cache``，跨进程/跨重启，search 命中 7 天、阴性 24 小时）
        → 单飞后真的发请求。缓存键用 ``_norm_text`` 归一化（年份独立维度）：
        同一部剧的两个文件名（``Some Show`` 与 ``Some Show (2024)``）不再各打
        一遍——跨条目去重从此可靠而非碰运气（§7.3③）。
        瞬态失败直接抛 ``TmdbTransientError``（L1/L2 都不写，网络恢复后自然
        重打）；``data=None`` 的非瞬态（401/404）不写 L2 磁盘。

        lang：指定语言（fallback 链用）；None 则用首选语言。
        """
        self._ensure_session()
        if not self.session:
            return []
        endpoint = "tv" if kind == "series" else "movie"
        norm = _norm_text(query)
        # 语言是缓存维度：TMDB 按 language 返回本地化标题/简介，不同语言互不命中
        lang = lang or preferred_language()
        key = ("search", endpoint, norm, year or 0, lang)
        cached = self._cache_get(key)
        if cached is not _MISS:
            return cached or []
        hit, results = tmdb_cache.load_search(endpoint, norm, year or 0, lang)
        if hit:
            progress.note_stage("tmdb_disk_hit")
            self._cache_put(key, results)   # 回填 L1：同批后续请求走内存
            return results or []
        with tmdb_cache.single_flight(key):
            # 拿到锁后二次检查：等锁期间别的线程可能已经填好 L1 或磁盘
            cached = self._cache_get(key)
            if cached is not _MISS:
                return cached or []
            hit, results = tmdb_cache.load_search(endpoint, norm, year or 0, lang)
            if hit:
                progress.note_stage("tmdb_disk_hit")
                self._cache_put(key, results)
                return results or []
            params: dict = {"language": lang, "query": query}
            if year:
                if endpoint == "tv":
                    params["first_air_date_year"] = year
                else:
                    params["year"] = year
            data = self._get(f"/search/{endpoint}", params)
            results = (data or {}).get("results") or []
            if data is not None:
                tmdb_cache.save_search(endpoint, norm, year or 0, results, lang)
            self._cache_put(key, results)
            return results

    def search(self, name: str, year: Optional[int], kind: str,
               loose_ok: bool = False) -> Optional[dict]:
        """智能搜索：清洗查询 → 多候选 → 置信度校验，只返回高置信命中。

        无高置信命中时返回 None（调用方按既有口径记 last_scraped_at，
        条目名字保持原样，绝不写错）。

        loose_ok=True（Q3）在候选末尾追加宽松归一化候选（去虚词/尾部年份），
        仅接受"宽松归一化后完全相等"的 Tier 2 命中，绝不接受模糊。
        默认 False，现有调用零影响。

        瞬态失败（网络/5xx/全 key 429）在**没有任何命中**时上抛
        ``TmdbTransientError``——那不是「搜过了没有」，是「没搜成」，
        条目应进重试队列；已有命中则照常返回（部分候选失败不挡命中）。

        语言 fallback（对标 StrmAssistant）：首选语言无高置信命中时，
        按 ``language_fallback_chain()`` 逐个语言重试。
        """
        from backend.emby_server import dedup as _dedup_lib

        loose_query = None
        if loose_ok:
            loose_query = _dedup_lib.normalize_name_loose(name) or None

        def _cands(nm, yr):
            cs = list(_search_candidates(nm, yr))
            if loose_query and all(q != loose_query for q, _ in cs):
                cs.append((loose_query, False))
            return cs
        # 外层：语言 fallback 链；内层：查询候选
        # 首选语言先走完所有候选，无命中才换语言（省请求）
        for lang in language_fallback_chain():
            best = None  # (tier, rank, hit)：跨候选、跨结果取全局最可信
            transient: Optional[Exception] = None
            saw_results = False
            for query, fuzzy_ok in _cands(name, year):
                # 短路（v2.42.9）：已有 Tier 2（归一化后**精确相等**）就收手。
                # Tier 2 永远压过 Tier 1（元组比较先看 tier），后续候选最多只能换来
                # 「更长的精确变体」这一个 rank 的差别，不值得再打 1~4 次 HTTP。
                # 中文短标题的候选数最多（4~5 个）而命中率最低，正是这一条最划算的地方。
                if best is not None and best[0] == 2:
                    self._stats["short_circuit"] += 1
                    progress.note_stage("tmdb_search_short")
                    break
                try:
                    results = self._search_raw(query, year, kind, lang=lang)
                except TmdbTransientError as exc:
                    # 瞬态失败只记不吞：一个候选都没拿到真实响应时整次上抛，
                    # 条目走补全退避重试（吞成 None 会被写成终态 none）
                    transient = transient or exc
                    continue
                except Exception:  # noqa: BLE001 — 缓存/磁盘等杂项异常换下一个候选
                    continue
                if results:
                    saw_results = True
                for hit in results[:10]:
                    sc = _hit_score(name, query, hit, fuzzy_ok, year=year)
                    if sc is None and loose_query and query == loose_query:
                        # Q3 宽松精确：仅当宽松归一化后与 hit 任一标题完全相等
                        # 才接受为 Tier 2 命中；绝不接受模糊（保持"绝不写错"）。
                        _hit_names = [
                            str(hit.get(k) or "") for k in
                            ("name", "title", "original_name", "original_title")
                        ]
                        if any(
                            _dedup_lib.normalize_name_loose(hn) == loose_query
                            for hn in _hit_names if hn
                        ):
                            sc = _confirm_year(
                                (2, len(loose_query)), year, hit)
                    if sc and (best is None or sc > best[:2]):
                        best = (sc[0], sc[1], hit)
            if best:
                return best[2]
            if year and not saw_results:
                # 年份回退：带年份过滤搜出来是空的（不是没命中，是 TMDB 直接
                # 没返回）→ 文件名里的年份可能是标题的一部分（如
                # 《Wonder Woman 1984》实为 2020 年电影），去年份再搜一轮。
                # 注意回退后不再做年份二次确认（年份已知不可靠），
                # 但候选仍保留"清洗名 YYYY"（年份可能是标题的一部分）。
                # 缓存键年份维度不同（year or 0），不会污染带年份的缓存。
                for query, fuzzy_ok in _cands(name, year):
                    try:
                        results = self._search_raw(query, None, kind, lang=lang)
                    except TmdbTransientError as exc:
                        transient = transient or exc
                        continue
                    except Exception:  # noqa: BLE001
                        continue
                    for hit in results[:10]:
                        sc = _hit_score(name, query, hit, fuzzy_ok, year=None)
                        if sc and (best is None or sc > best[:2]):
                            best = (sc[0], sc[1], hit)
                if best:
                    return best[2]
            if transient is not None:
                raise transient
            # 本语言无命中，继续下一种语言
        return None

    def search_candidates(self, name: str, kind: str, limit: int = 6) -> list[dict]:
        """原始候选搜索（求片中心用）：不做置信度裁剪，用户自己认片

        刮削要的是「唯一正确答案」（``search()`` 会按置信度裁剪、宁缺毋滥），
        而求片要的是「一组候选」：用户搜什么就列什么，选哪部由他决定。
        同样走两级缓存与请求限流，失败返回空表（不让一次网络抖动把用户的搜索框打成报错）。
        """
        query = _clean_query(name) or (name or "").strip()
        if not query:
            return []
        try:
            results = self._search_raw(query, None, kind)
        except TmdbTransientError as exc:
            # 求片中心的契约是「失败也不 500」：照样返回空表，
            # 但必须留下可见日志（网络恢复后用户再搜一次即可）
            logger.warning("TMDB 候选搜索暂时不可用 q=%r: %s", query, exc)
            return []
        except Exception:  # noqa: BLE001 — 与 search() 同口径：单个查询失败不当成致命错误
            return []
        return [hit for hit in list(results or [])[:limit] if isinstance(hit, dict)]

    def details(self, tmdb_id: str, kind: str) -> Optional[dict]:
        """详情（补 IMDb Id 与多别名）——只在条目缺这两项时调用。

        两级缓存同 ``_search_raw``（L1 300 秒 → L2 磁盘 30 天，按 id 键；
        语言同样是维度：首选语言变了就按新语言重新拉，不会串味）。

        v2.51.0：``append_to_response`` 带上 ``credits``——演员表和详情**一次请求**
        拿全，不新增请求、不新增限流器（走同一个令牌桶）。升级前缓存的老载荷没有
        credits 键，读到时视为过期重拉一次（见 ``_has_credits``）。
        """
        endpoint = "tv" if kind == "series" else "movie"
        lang = preferred_language()
        key = ("details", endpoint, str(tmdb_id), lang)
        cached = self._cache_get(key)
        if cached is not _MISS and _has_credits(cached):
            return cached
        hit, data = tmdb_cache.load_details(endpoint, str(tmdb_id), lang)
        if hit and _has_credits(data):
            progress.note_stage("tmdb_disk_hit")
            self._cache_put(key, data)
            return data
        with tmdb_cache.single_flight(key):
            cached = self._cache_get(key)
            if cached is not _MISS and _has_credits(cached):
                return cached
            hit, data = tmdb_cache.load_details(endpoint, str(tmdb_id), lang)
            if hit and _has_credits(data):
                progress.note_stage("tmdb_disk_hit")
                self._cache_put(key, data)
                return data
            data = self._get(f"/{endpoint}/{tmdb_id}",
                             {"language": lang,
                              "append_to_response": "alternative_titles,external_ids,credits"})
            if data is not None:
                tmdb_cache.save_details(endpoint, str(tmdb_id), data, lang)
            self._cache_put(key, data)
            return data

    def batch_search(self, queries, return_transient=False):
        """批量搜索。

        使用 8 个线程并发调用 self.search(name, year, kind)，
        单个任务异常时返回 None，最终返回 {index: hit_or_None}，包含所有下标。

        TMDB 没有原生批量接口，这里是"并发 + 共用令牌桶限速"的批量：
        限速走 _keyed_limiter（线程安全；v2.57.0 起按 key 独立成桶，有效上限 =
        key 数 × TMDB_PER_SEC）；缓存走
        search() 内部的两级缓存（L1 内存 + L2 磁盘），命中不发请求。

        :param queries: [(name, year, kind), ...] 搜索参数列表
        :param return_transient: 为 True 时返回 (results, transient_indices) 二元组，
            transient_indices 为触发 TmdbTransientError 或意外异常的下标 set（对应结果仍记 None）
        :return: dict，键为输入下标，值为搜索命中 dict 或 None；
            return_transient=True 时返回 (results, transient_indices) 二元组
        """
        import concurrent.futures

        results = {}
        transient_indices = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_index = {
                executor.submit(self.search, name, year, kind): index
                for index, (name, year, kind) in enumerate(queries)
            }
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except TmdbTransientError:
                    transient_indices.add(index)
                    results[index] = None
                except Exception as exc:  # noqa: BLE001
                    # 意外异常不是「搜不到」：当 None 会被终态化为 metadata_source='none'，
                    # 逐条路径此时异常冒泡会重试——批量路径按瞬态处理，保持一致
                    logger.warning("TMDB 批量任务意外异常（按瞬态重试）#%s: %s", index, exc)
                    transient_indices.add(index)
                    results[index] = None
        if return_transient:
            return results, transient_indices
        return results

    def batch_details(self, ids, return_transient=False):
        """批量取详情。

        使用 8 个线程并发调用 self.details(tid, kind)，
        单个任务异常时返回 None（异常隔离），最终返回
        {index: details_or_None}，包含所有下标。

        限速与缓存口径同 batch_search（共用令牌桶 + 两级缓存）。

        :param ids: [(tmdb_id, kind), ...] 详情参数列表
        :param return_transient: 为 True 时返回 (results, transient_indices) 二元组，
            transient_indices 为触发 TmdbTransientError 或意外异常的下标 set（对应结果仍记 None）
        :return: dict，键为输入下标，值为详情 dict 或 None；
            return_transient=True 时返回 (results, transient_indices) 二元组
        """
        import concurrent.futures

        results = {}
        transient_indices = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_index = {
                executor.submit(self.details, tid, kind): index
                for index, (tid, kind) in enumerate(ids)
            }
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except TmdbTransientError:
                    transient_indices.add(index)
                    results[index] = None
                except Exception as exc:  # noqa: BLE001
                    # 意外异常不是「搜不到」：当 None 会被终态化为 metadata_source='none'，
                    # 逐条路径此时异常冒泡会重试——批量路径按瞬态处理，保持一致
                    logger.warning("TMDB 批量任务意外异常（按瞬态重试）#%s: %s", index, exc)
                    transient_indices.add(index)
                    results[index] = None
        if return_transient:
            return results, transient_indices
        return results

    def credits(self, tmdb_id: str, kind: str) -> list[dict]:
        """前 10 演员（``cast_list`` 口径：name / role / image / sort_order）。

        复用 ``details()`` 的 ``append_to_response=credits``——**不新增请求**，
        也不新增限流器（``details`` 里那次 ``_get`` 已经过了令牌桶）。
        """
        return cast_list(self.details(str(tmdb_id), kind))

    def images_with_language(self, tmdb_id: str, kind: str,
                             image_lang: Optional[str] = None) -> Optional[dict]:
        """按语言偏好取图片（对标 StrmAssistant #10 原语言海报）。

        image_lang：None 则按 ``poster_language()`` 配置决定：
        - "system"：返回 None（用 details 里的默认图，不额外请求）
        - "original"：取 ``include_image_language=en,null``（原版+无语言）
        - "zh-CN"：取 ``include_image_language=zh-CN,null``

        磁盘缓存 30 天（同 details 口径）：同 tmdb_id 不重复打 TMDB。
        """
        if image_lang is None:
            pref = poster_language()
            if pref == "system":
                return None
            image_lang = "en,null" if pref == "original" else "zh-CN,null"
        endpoint = "tv" if kind == "series" else "movie"
        # 先读缓存（键含语言维度）
        try:
            hit, cached = tmdb_cache.load_details(
                f"{endpoint}_images", str(tmdb_id), image_lang)
            if hit and isinstance(cached, dict):
                return cached
        except Exception:
            pass
        try:
            data = self._get(f"/{endpoint}/{tmdb_id}/images",
                             {"include_image_language": image_lang})
        except Exception:
            return None
        if data:
            try:
                tmdb_cache.save_details(
                    f"{endpoint}_images", str(tmdb_id), data, image_lang)
            except Exception:
                pass
        return data

    def season_episodes(self, tmdb_id: str, season_number: int) -> Optional[list]:
        """获取某季所有集的信息（标题/简介/剧照）。

        走 `GET /tv/{tv_id}/season/{season_number}`，一次请求拿整季，
        同一季的多个集共享缓存，不重复打 TMDB。

        两级缓存同 ``details``（L1 300 秒 → L2 磁盘 30 天，按 tv_id+季号键）。

        返回 episode 字典列表（每项有 episode_number / name / overview /
        still_path），失败返回 None。
        """
        try:
            season_no = int(season_number)
        except (TypeError, ValueError):
            return None
        lang = preferred_language()
        key = ("season_episodes", str(tmdb_id), str(season_no), lang)
        cached = self._cache_get(key)
        if cached is not _MISS:
            return cached
        cache_key = f"{tmdb_id}_{season_no}"
        hit, data = tmdb_cache.load_details("season", cache_key, lang)
        if hit:
            progress.note_stage("tmdb_disk_hit")
            eps = (data or {}).get("episodes")
            self._cache_put(key, eps)
            return eps
        with tmdb_cache.single_flight(key):
            cached = self._cache_get(key)
            if cached is not _MISS:
                return cached
            hit, data = tmdb_cache.load_details("season", cache_key, lang)
            if hit:
                progress.note_stage("tmdb_disk_hit")
                eps = (data or {}).get("episodes")
                self._cache_put(key, eps)
                return eps
            data = self._get(f"/tv/{tmdb_id}/season/{season_no}",
                             {"language": lang})
            if data is not None:
                tmdb_cache.save_details("season", cache_key, data, lang)
            eps = (data or {}).get("episodes") if data else None
            self._cache_put(key, eps)
            return eps

    def find_episode(self, tmdb_id: str, season_number: int,
                     episode_number: int) -> Optional[dict]:
        """在某季里找指定集的数据（标题/简介/剧照）。

        找不到返回 None（TMDB 没这集，或季接口失败）。
        """
        try:
            ep_no = int(episode_number)
        except (TypeError, ValueError):
            return None
        episodes = self.season_episodes(str(tmdb_id), season_number)
        if not episodes:
            return None
        for ep in episodes:
            try:
                if int(ep.get("episode_number", -1)) == ep_no:
                    return ep
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def apply_episode(episode_item, episode_data: dict,
                      fields: Optional[dict] = None) -> dict:
        """把单集 TMDB 数据落到条目上（纯内存操作，不碰 DB/网络）。

        fields: {"title": bool, "overview": bool, "still": bool}，None 表示
        全部开（与分项开关未配置/默认一致，行为与旧调用完全相同）。
        批3（分项开关）：被关的项跳过写入。

        返回 {"updated": bool, "still_path": Optional[str]}：
        - updated=True 表示 name/overview 有变化，调用方需要写库
        - still_path 是 TMDB 的剧照路径（如 "/abc123.jpg"），调用方负责拼 URL
          并预热下载

        标题规则（用户要求）：
        - TMDB 有真实标题（非空，且不是 "Episode N" 这种占位）→ 用真实标题
        - 当前 name 是"第X集"格式 → 只有 TMDB 有真实标题时才覆盖，否则保留
        - 当前 name 不是"第X集"格式（已有真实标题）→ 不覆盖
        """
        result = {"updated": False, "still_path": None}
        if not episode_data:
            return result

        # 批3：分项开关门控（None = 全部开，与旧行为一致）
        enabled = {"title": True, "overview": True, "still": True}
        if fields:
            for _k in enabled:
                if _k in fields:
                    enabled[_k] = bool(fields[_k])

        tmdb_name = (episode_data.get("name") or "").strip()
        # TMDB 占位标题（"Episode 1" / 中文"第 1 集"）不算真实标题
        is_placeholder = (
            not tmdb_name
            or tmdb_name.lower().startswith("episode ")
            or bool(re.match(r"^第\s*\d+\s*集$", tmdb_name))
        )
        current = (getattr(episode_item, "name", "") or "").strip()
        is_generic = _is_placeholder_episode_title(current)

        if enabled["title"] and not is_placeholder and is_generic:
            # 当前是"第X集"占位，TMDB 有真实标题 → 覆盖。先过统一标题清洗
            # （冒号副标题/控制符不进标题）；清空了就不覆盖，保住现有真实名
            cleaned = clean_title(tmdb_name)
            if cleaned:
                episode_item.name = cleaned
                # 与 nfo.py:202 同口径：改名同步 sort_name
                from backend.emby_server.pinyin_sort import make_sort_name
                episode_item.sort_name = make_sort_name(cleaned)
                result["updated"] = True
        # 简介：当前为空时才补（不覆盖 NFO/已有的）；批3：简介分项关时跳过
        tmdb_overview = (episode_data.get("overview") or "").strip()
        if (enabled["overview"] and tmdb_overview
                and not (getattr(episode_item, "overview", "") or "").strip()):
            episode_item.overview = tmdb_overview
            result["updated"] = True
        # 单集片长（元数据口径，探测拿到真实时长前用）
        if apply_runtime(episode_item, episode_data):
            result["updated"] = True
        # 剧照路径（调用方拼 URL + 预热）；批3：剧照分项关时不返回路径
        still = episode_data.get("still_path")
        if enabled["still"] and still:
            result["still_path"] = still
        return result

    def find_by_imdb(self, imdb_id: str) -> Optional[dict]:
        """按 IMDb ID 反查 TMDB（GET /find/{external_id}?external_source=imdb_id）。

        手动识别时管理员手头常只有 IMDb 链接（tt 开头）；先换算成 TMDB ID 再走
        现有流程。走 ``_get``：密钥轮换 / 限流 / language 注入与其它读取同一套口径；
        L1 进程缓存按 imdb_id 键（IMDb→TMDB 映射是稳定的事实）。
        """
        iid = (imdb_id or "").strip()
        if not iid:
            return None
        key = ("find", "imdb", iid, preferred_language())
        cached = self._cache_get(key)
        if cached is not _MISS:
            return cached
        data = self._get(f"/find/{iid}", {"external_source": "imdb_id"})
        self._cache_put(key, data)
        return data

    def enrich(self, item: emby_models.MediaItem, kind: str) -> None:
        """补齐 imdb_id 与 aliases（中英文/繁简多别名搜索的基础）"""
        if not item.tmdb_id or (item.imdb_id and item.aliases):
            return
        data = self.details(str(item.tmdb_id), kind)
        if not data:
            return
        self.apply_details(item, data)

    def apply_details(self, item: emby_models.MediaItem, data: dict) -> None:
        """把详情接口的返回落到条目上（与 enrich 同口径，供批量扫描预先取回后套用）"""
        if not data:
            return
        # 详情只补缺项（简介/评分/别名/类型/年份/首播日期），不覆盖 NFO 提供的文字。
        # 用 getattr 兜底：调用方（含测试里的轻量替身）未必带这个字段。
        if not getattr(item, "metadata_source", None):
            item.metadata_source = "tmdb"
        # 片长（元数据口径）：探测前客户端「媒体信息」不至于显示 0 秒
        apply_runtime(item, data)
        # 类型：详情接口直接给 genres=[{id, name}]（language=zh-CN 下 name 已是中文）。
        # 这是「有 tmdb_id 却无类型」的主因：以前 apply_details 完全忽略它，
        # 凡走 tmdb_id → 详情分支的（NFO 自带 tmdb_id、别名匹配、已有 id 补缺）
        # 都永远拿不到类型，只有搜索命中走 apply() 的才有。
        if not (getattr(item, "genres", None) or "").strip():
            gnames = _genres_from_details(data)
            if gnames:
                item.genres = ",".join(gnames)
        imdb = (data.get("external_ids") or {}).get("imdb_id") or data.get("imdb_id")
        if imdb:
            item.imdb_id = imdb
        if not getattr(item, "production_year", None):
            y = _year_from_tmdb_date(data)
            if y:
                item.production_year = y
        # 简介与评分也从详情补：搜索结果的 overview/vote_average 经常是空或 0，
        # 只靠 apply() 会让「有 tmdb_id 却缺简介/评分」的一大批永远补不上。
        # 批2：只填空、不覆盖——已有简介/评分时 TMDB 详情不再覆盖。
        fill_only = tmdb_text_fill_only()
        overview = data.get("overview")
        if overview and (not fill_only or not (getattr(item, "overview", None) or "").strip()):
            item.overview = overview
        rating = data.get("vote_average")
        if rating is not None and rating != "" and \
                (not fill_only or getattr(item, "community_rating", None) is None):
            try:
                item.community_rating = round(float(rating), 1)
            except (TypeError, ValueError):
                pass
        alt = (data.get("alternative_titles") or {})
        titles = [t.get("title") for t in (alt.get("titles") or [])]
        titles += [t.get("title") for t in (alt.get("results") or [])]
        names = [n for n in ([data.get("name"), data.get("original_name"),
                              data.get("title"), data.get("original_title")] + titles) if n]
        if names:
            # 批2：别名在只填空口径下按「增量合并」处理——保留已有、追加新增
            # （与 apply() 同口径）；回退旧行为时保持原来的整体替换。
            new_names = [n for n in dict.fromkeys(
                str(n).strip() for n in names) if n]
            if fill_only:
                existing = [a for a in
                            (getattr(item, "aliases", None) or "").split(",")
                            if a.strip()]
                merged = existing + [n for n in new_names if n not in existing]
                if merged:
                    item.aliases = ",".join(merged[:12])
            elif new_names:
                item.aliases = ",".join(new_names[:12])
        # 首播/上映日期 → production_year + premiere_date（只补缺项；
        # 详情的 first_air_date 通常比搜索命中准，但已有值不覆盖）
        _fill_air_dates(item, data)
        # v2.51.0：国家/语言落库（EA 详情页 Countries / Languages 用）。
        # 只补缺项、不覆盖已有（与简介/评分同口径）；getattr 兜底轻量替身。
        countries = [str(c).strip() for c in (data.get("origin_country") or [])
                     if str(c).strip()]
        if countries and not getattr(item, "countries", None):
            item.countries = ",".join(countries)[:500]
        spoken = data.get("spoken_languages") or []
        lang_names = [str(l.get("english_name") or l.get("name") or "").strip()
                      for l in spoken if isinstance(l, dict)]
        lang_names = [n for n in lang_names if n]
        if not lang_names:
            orig = str(data.get("original_language") or "").strip()
            if orig:
                lang_names = [orig]
        if lang_names and not getattr(item, "languages", None):
            item.languages = ",".join(lang_names)[:500]

    def refresh_images(self, item: emby_models.MediaItem, kind: str) -> bool:
        """重新取图——数据库里有图片记录但本地文件已丢失时用

        （客户端取图 404 会把条目排进修复队列，下一轮扫描到这里把图换成 TMDB 远程图）
        """
        if not item.tmdb_id:
            return False
        data = self.details(str(item.tmdb_id), kind)
        # 修复路径同样是「先落盘再落字段」：写库阶段不发 HTTP（见 _set_image）
        image_store.prewarm(url for _kind, url in image_specs(data))
        return self.apply_images(item, data)

    def apply_images(self, item: emby_models.MediaItem, data: dict,
                     images_lang: Optional[dict] = None) -> bool:
        """把详情接口里的图片落到条目上（返回是否拿到图）

        images_lang：images_with_language() 预取的语言偏好图片（IO 阶段取，
        对标 StrmAssistant #10 原语言海报）。有原语言海报时优先用它做主海报。
        """
        if not data:
            return False
        specs = image_specs(data)
        # 原语言海报优先：images 的 posters[0] 替换默认 Primary
        if images_lang:
            posters = [p for p in (images_lang.get("posters") or [])
                       if isinstance(p, dict) and p.get("file_path")]
            if posters:
                base = image_base()
                lang_poster = f"{base}/w500{posters[0]['file_path']}"
                specs = [("Primary", lang_poster)] + [
                    s for s in specs if s[0] != "Primary"
                ]
        for img_kind, url in specs:
            _set_image(item, img_kind, url)
        if specs and getattr(item, "metadata_source", None) == "nfo":
            # NFO 管文字、TMDB 补图（B 方案）——这是最常见的组合，单独标记出来
            item.metadata_source = "tmdb_img"
        return bool(specs)

    def apply(self, item: emby_models.MediaItem, hit: dict, kind: str) -> None:
        item.tmdb_id = str(hit.get("id"))
        item.last_scraped_at = datetime.now()
        # 文字与图片都来自 TMDB 搜索结果
        item.metadata_source = "tmdb"
        item.overview = hit.get("overview") or item.overview
        rating = hit.get("vote_average")
        # 批2（刮削瘦身）：文字字段只填空、不覆盖。注意不能用 `if rating:`：
        # TMDB 对没有评分的条目返回 0.0，那是**合法数据**，判假值会让这类条目
        # 永远没有评分（生产实测 21天重养自己 就是 0.0）。已有评分时 TMDB 的
        # 0.0 不再覆盖（为将来 NFO/人工编辑让路）。
        fill_only = tmdb_text_fill_only()
        if rating is not None and rating != "" and \
                (not fill_only or item.community_rating is None):
            try:
                item.community_rating = round(float(rating), 1)
            except (TypeError, ValueError):
                pass
        for img_kind, url in image_specs(hit):
            _set_image(item, img_kind, url)
        if not fill_only or not (getattr(item, "name", None) or "").strip():
            if kind == "series" and hit.get("name"):
                _n = clean_title(hit.get("name"))
                if _n:
                    item.name = _n
            elif hit.get("title"):
                _n = clean_title(hit.get("title"))
                if _n:
                    item.name = _n
        if not getattr(item, "production_year", None):
            y = _year_from_tmdb_date(hit)
            if y:
                item.production_year = y
        # 搜索命中里就能拿到的多别名（中英文/原名）：先落库，详情接口再补全
        hit_aliases = [hit.get("name"), hit.get("title"),
                       hit.get("original_name"), hit.get("original_title")]
        existing = [a for a in (item.aliases or "").split(",") if a]
        merged = existing + [a.strip() for a in hit_aliases if a and a.strip() not in existing]
        if merged:
            item.aliases = ",".join(dict.fromkeys(merged))[:2000]
        genre_ids = hit.get("genre_ids") or []
        # 批2：类型只填空、不覆盖（TMDB genre_id 映射是粗分类，NFO/人工更准）
        if genre_ids and (not fill_only or not (getattr(item, "genres", None) or "").strip()):
            item.genres = ",".join(
                _GENRE_NAMES.get(g, str(g)) for g in genre_ids[:_MAX_GENRES]
            )
        # 搜索命中里就有 first_air_date / release_date：年份与首播日期只补缺项
        _fill_air_dates(item, hit)


tmdb_client = TmdbClient()  # 进程级单例：一次扫描里的预热与写库共用同一份缓存与连接池

"""自建 Emby 服务器：媒体库扫描 + 元数据刮削"""
from __future__ import annotations

import hashlib
import json  # 扫描结果落库（scan_stats）需要
import logging
import os
import posixpath
import re
import struct
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlparse

from backend.emby_server.cpu_budget import background_workers as _cpu_workers
from backend.emby_server.env_util import env_float, env_int
from backend.emby_server.pinyin_sort import make_sort_name

from sqlalchemy import func, inspect, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from backend.db_retry import commit_with_retry, deadlock_backoff, is_deadlock, retry_write
from backend.emby_server import disc_filter
from backend.emby_server import models as emby_models
from backend.emby_server import mounts as mount_lib
from backend.emby_server import mount_sources
from backend.emby_server import scan_snapshots
from backend.emby_server import nfo as nfo_lib
# 软删除（v2.48.0）：清理阶段默认只标记 deleted_at，物理删除留给过期回收
from backend.emby_server import soft_delete as _soft_delete
# 实时进度与远程 IO 计数（v2.27.0）：只依赖标准库，不会与 scanner / mounts 形成循环
from backend.emby_server import scan_progress as progress

# TMDB 刮削客户端已拆到 tmdb.py：扫描器只管遍历与写库，网络客户端（密钥轮询 + 短 TTL 缓存）
# 单独成模块。这里重新导出一次，`scanner.tmdb_client` / `scanner.TmdbClient` 等既有引用不变。
#
# 注意：`tmdb_client` 故意**不**在这里 import。`from x import tmdb_client` 会在 scanner 首次
# 导入时把对象快照进本模块命名空间，之后 `tmdb.tmdb_client` 被替换（例如测试里的
# mock.patch）时，scanner 仍持有旧对象；曾导致 patch 窗口内首次导入 scanner 的场景把 mock
# 泄漏到后续所有 `scanner._tmdb_work` 调用，`tests/test_enrich_worker.py` 全量跑时 flaky。
# 因此内部统一走下面的 `_tmdb.tmdb_client` 活引用；外部 `scanner.tmdb_client` 引用由
# PEP 562 的模块级 __getattr__ 兼容，见下方定义。
from backend.emby_server.tmdb import (  # noqa: F401
    TMDB_API,
    TMDB_IMAGE,
    TMDB_LANG,
    TmdbClient,
    prewarm_images,
)
# 模块别名（活引用）：内部统一用 `_tmdb.tmdb_client`，不要 `from tmdb import tmdb_client`
from backend.emby_server import tmdb as _tmdb


def __getattr__(name: str):
    """PEP 562 模块属性兜底（紧跟上面的 import 块）。

    只为兼容外部的 `scanner.tmdb_client` 引用（属性访问 / `from scanner import tmdb_client`）：
    每次访问都返回 `backend.emby_server.tmdb.tmdb_client` 的当前值。
    本模块内部统一走上面的 `_tmdb.tmdb_client`（模块 `__getattr__` 不参与模块内部的
    全局名查找，内部直接写裸名会 NameError）。
    其余名字保持标准的 AttributeError 行为。
    """
    if name == "tmdb_client":
        return _tmdb.tmdb_client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 外挂字幕的同名判定与语言识别已拆到 subtitle_match.py（本机与远程挂载共用同一份实现；
# 注意别与负责字幕**投递**的 subtitles.py 搞混）。同样重新导出，
# `scanner.match_subtitle_names` 等既有引用不需改动。
from backend.emby_server.subtitle_match import (  # noqa: F401
    SUBTITLE_EXTS,
    find_external_subtitles_in,
    find_external_subtitles_remote,
    match_subtitle_names,
    subtitle_language,
)

logger = logging.getLogger(__name__)

# ==================== 资源预算（这个后端要能在小机器上跑完整库）（这个后端要能在 1C1G 的小机器上跑完整库）====================
# 老实现每处理一个文件就：2~5 次数据库查询 + 1 次 ffprobe + 1~2 次目录列举 + 1 次 commit，
# 而且清理阶段会把整库 MediaItem 一次性载入内存。大库（十万级）下既慢又吃内存。
# 现在按「批次」处理：
#   SCAN_BATCH  一批多少个文件（批量查库 / 批量提交 / 批后清空会话）
#   SCAN_WORKERS 并行 IO 线程数（ffprobe、目录列举、TMDB 搜索都是“等网络/等磁盘”，不是吃 CPU）
SCAN_BATCH = env_int("SCAN_BATCH", 400, 20, 100000)
# 并发按 CPU 自适应（cpu_budget）：未配置时按核数一半，永远给前台留一核
SCAN_WORKERS = _cpu_workers("SCAN_WORKERS")
# 两阶段扫描（v2.39.0）：
#   inline（默认）：与旧行为完全一致，扫描时直接 ffprobe；
#   background：Phase 1 只入库结构（路径解析/NFO/TMDB 图），不做 ffprobe，
#     需要探测的条目标 probe_status='pending'，由 probe_worker 后台按优先级探测。
# 回滚：改回 inline 并重启，行为即回到从前。
SCAN_PROBE_MODE = (os.getenv("SCAN_PROBE_MODE", "inline") or "inline").strip().lower()
PROBE_BACKGROUND = SCAN_PROBE_MODE == "background"
# SQLite 的绑定变量上限是 999，IN 查询按这个分片（片内元素个数）
SQL_IN_CHUNK = 200
# 清理阶段每批读多少条：这一步不碰 IO（只读 4 个列），批越大越省往返。
# 实测十万条目：每批 400 条 2.1s → 每批 5000 条 0.6s（稳定库还有更快的快速路径，见下）。
CLEANUP_BATCH = env_int("SCAN_CLEANUP_BATCH", 5000, 20, 100000)

# ==================== 删除保护（v2.46.0）====================
# 事故形状：「挂载断开 → 底层空目录还在 → 本轮扫描看到 0 个文件 → 清理阶段把整库删了」。
# ``failed_roots`` 挡不住这种（目录还在，只是空的），所以再加两道：
# 1) **零结果**：一个文件都没看到，但库里有条目 —— 直接不删。一次判断就能兜住事故主体。
# 2) **数量阈值**：本轮删掉的量超过库内条目数的这个比例（或绝对数），就认为不对劲。
REMOVAL_MIN_SEEN = 1
#: 比例阈值：删掉超过库内文件类条目的这个比例就停手（0.5 = 一半以上全没了）
REMOVAL_MAX_RATIO = env_float("SCAN_REMOVAL_MAX_RATIO", 0.5, 0.0)
#: 绝对阈值：小库也要有下限保护（库里总共就 10 条，删 6 条就已经是“全没了”）
REMOVAL_MAX_ABSOLUTE = env_int("SCAN_REMOVAL_MAX_ABSOLUTE", 20, 1, 1000000)

# ==================== 缺失宽限（2026-10-24，FMBY 学习批一 #6）====================
# 魂「对外部变化克制」：单次观测不作数。上面的删除保护管的是「整库级」
# 事故（来源整体不可用时一条都不删）；这里管的是「单条级」抖动：
# 单个文件这一轮没看到（挂载抖动、目录半读、网盘短暂抽风），不许当轮就
# 下架——先记 missing_count 累计，连续缺失达到这个轮数才真正软删除。
# 文件重新出现即清零。设 1 = 回到旧行为（确认缺失即下架）。
MISSING_GRACE_SCANS = env_int("SCAN_MISSING_GRACE_SCANS", 3, 1, 1000)


def _removal_budget(db: Session, library, seen_guids: set) -> tuple[bool, str, int]:
    """能不能开始清理 → ``(能不能, 不能的原因, 库内文件类条目数)``

    只做计数（走索引），O(1) 次查询；不做任何删除。

    计数**只算可见行**（v2.48.0 软删除）：已经下架的行不需要再删，也不能拿它们当
    分母——否则一个删过 90 部的库会把「本轮看到 10 部」误判成“来源整体不可用”，
    把后面真正需要清理的场景也一起拦住。
    """
    total = int(db.execute(
        text(
            "SELECT COUNT(*) FROM emby_items "
            "WHERE library_id = :lib AND item_type IN ('movie', 'episode') "
            "AND deleted_at IS NULL"
        ),
        {"lib": library.id},
    ).scalar() or 0)
    if total <= 0:
        return True, "", 0                      # 本来就没有文件类条目，清不清都一样
    seen = len(seen_guids)
    if seen < REMOVAL_MIN_SEEN:
        return False, (
            f"本轮一个文件都没看到，但库里有 {total} 条文件类条目——"
            f"这正是「挂载断了底层空目录还在」的事故形状，本轮禁止清理"
        ), total
    # 剩下要删的至多 total 条；超过阈值就说明来源多半整体读不到了
    limit = max(REMOVAL_MAX_ABSOLUTE, int(total * REMOVAL_MAX_RATIO))
    if total - seen >= limit:
        return False, (
            f"本轮只看到 {seen} 个文件，库里有 {total} 条，预计要删 {total - seen} 条"
            f"（阈值 {limit}）——超过阈值，判定来源整体不可用，本轮禁止清理"
        ), total
    return True, "", total

# 上一次清理阶段的决策（健康检查与测试用：有没有走快速路径、走过多少条、耗时）
_CLEANUP_LAST: dict = {"fast_path": False, "walked": 0, "removed": 0, "graced": 0, "elapsed_ms": 0.0,
                       # skipped = 删除保护拦下了本轮清理的原因（空串 = 没拦）
                       # 调用点用 .get("skipped") 读，所以这个键**必须一开始就存在**，
                       # 否则读出来恒为空、“被拦了”这件事就永远传不到接口/前端。
                       "skipped": ""}

# ==================== 扫描限速（播放优先）====================
# 扫描跑在 API 进程的后台线程里，而 POSIX 的 nice 是**进程级**的：直接给本进程降优先级，
# 会连同一进程里正在播放的请求一起降——那是错的。所以这里只降**子进程**：ffprobe 是扫描里
# 唯一的外部进程，既是最吃 CPU 的一步，也最容易和播放抢磁盘 IO。
#   SCAN_IO_NICE  探测子进程的 niceness（0 = 关闭，默认 10；越大越让路）
# 同时若有 ionice 就把 IO 优先级降到 best-effort 最低档（-c2 -n7；只降不升，普通用户即可）。
SCAN_IO_NICE = env_int("SCAN_IO_NICE", 10, 0, 19)


def _tool_usable(argv: list) -> bool:
    """先拿一个空命令试跑一次：工具存在但内核/容器不允许时，绝不能拖累真正的探测"""
    try:
        proc = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def _io_nice_prefix() -> tuple:
    """给探测子进程加「低优先级」前缀；平台或工具不具备时返回空，不影响功能

    只在**启动时探一次**（结果缓存）：宁可退化成普通优先级，也不能让 wrapper 把 ffprobe 挡掉。
    """
    if SCAN_IO_NICE <= 0:
        return ()
    prefix: list = []
    if os.name == "posix":
        ionice = shutil_which("ionice")
        if ionice and _tool_usable([ionice, "-c", "2", "-n", "7", "sleep", "0"]):
            prefix += [ionice, "-c", "2", "-n", "7"]
    nice = shutil_which("nice")
    if nice:
        prefix += [nice, "-n", str(SCAN_IO_NICE)]
    if prefix:
        logger.info("扫描限速：ffprobe 子进程降优先级 %s（SCAN_IO_NICE=%d）", " ".join(prefix), SCAN_IO_NICE)
    return tuple(prefix)


def _io_nice_command(argv: list) -> list:
    """把命令包成低优先级版本（前缀为空时原样返回）"""
    return list(_io_nice_prefix()) + list(argv)


def _chunks(seq, size: int):
    """把序列切成固定大小的块（避免一次性构造巨大的 IN 查询 / 巨大的列表）"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

# `.strm` 不是视频文件，而是「内容为播放直链的文本文件」；是否把 strm 当媒体由调用方决定
# `.iso` 是光盘镜像文件：部分客户端（Infuse / VLC 等）可直接播放整盘 ISO
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m2ts", ".ts", ".m4v", ".strm", ".iso"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# 集号解析: S01E02 / 1x02 / 第1集 / EP03 / E03
# 注意：调用方用 `parsed["season"] is not None` 判定条目是单集还是电影/剧，
# 所以**新增的集号模式一律只在剧集库启用**（EP_PATTERNS_TV），否则
# "Movie 2" 这类电影会被误判成 episode。
EP_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})\s?[\s._-]*[Ee](\d{1,4})"),
    re.compile(r"(\d{1,2})x(\d{1,4})"),
    re.compile(r"第\s*([一二三四五六七八九十百千万两\d]{1,6})\s*[集话話]"),
    re.compile(r"\b[Ee][Pp]\.?\s?(\d{1,4})(?!\d)"),
]
# 仅剧集库：独立 E05（不带 P，动漫常见）/ 4 位 EP
# `\b[Ee]` 在 "S01E05" 里（"1E" 间无词边界）不会误命中，SxxEyy 仍走上面。
EP_PATTERNS_TV = [
    re.compile(r"\b[Ee][Pp]?\.?\s?(\d{1,4})(?!\d)"),
]
# 合集区间：S06E27-E28 / S01E26 - E27 / S01E27E28（同一文件含多集）。
# **必须在 EP_PATTERNS_TV 的独立 E 模式之前命中**：否则 "-E28" 的尾巴
# 会被独立 E 模式先咬住（生产实证：海绵宝宝 S06E27-E28 被标成 S1E28）。
# 命中后 episode 取区间起点，episode_end 记终点；下游按起点入库。
EP_RANGE_RES = [
    re.compile(r"[Ss](\d{1,2})\s?[\s._-]*[Ee](\d{1,4})\s*-\s*[Ee]?(\d{1,3})"),
    # 连写：S01E27E28。排在分隔符形态之后；单集 S01E27 不会被它咬住
    # （需要第二个 E + 数字）。
    re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,4})[Ee](\d{1,3})\b"),
]
# 仅剧集库：绝对集数（动漫常见 `Frieren - 28`）。
# 要求分隔符 + 锚定末尾；年份段位 (1900-2099) 让位给年份解析；不与已匹配段重叠。
_ABS_EP_RE = re.compile(r"[\s\-_.](\d{1,4})\s*$")

# 只有季号（季包 / 中文命名）: S09 / Season 9 / 第九季 / 第9季
# 注意：裸 S09 只在剧集库里启用，否则 "S1m0ne" 这类片名会被误判成第 1 季。
SEASON_PATTERNS_TV = [
    re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,2})(?![\dEe])"),
]
SEASON_PATTERNS_ANY = [
    re.compile(r"[Ss]eason\s*\.?\s*(\d{1,2})", re.IGNORECASE),
    re.compile(r"第\s*(\d{1,2})\s*季"),
    re.compile(r"第\s*([一二三四五六七八九十百]{1,3})\s*季"),
]
# 年份候选：4 位年份段数字，前后不能紧贴数字（避免 1080p 的 1080 误命中，
# 不过 1080 < 1900 本来也会被范围检查滤掉）。
_YEAR_CAND_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
# 旧写法 `[.\_-_\[\]【】]+'` 在字符类外多了一个引号，导致这个正则几乎永不命中，
# 于是 `Rick.and.Morty` 这类点分隔片名会原样入库（显示成 "Rick.and.Morty"）。
CLEAN_RE = re.compile(r"[\.\_\-\[\]【】]+")

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(text: str) -> Optional[int]:
    """中文数字 → 整数（支持到千位：十二/二十三/一百二十三/两百/一千零一）。

    集号解析需要百位（第十二集、第两百集），季号调用方仍限制 <=99。
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total, num = 0, 0
    for ch in text:
        if ch in _CN_UNITS:
            # 十/百/千是单位（"十" 不能走 _CN_DIGITS 的数字分支）
            unit = _CN_UNITS[ch]
            if num == 0:
                num = 1  # "十" = 10，"百" = 100
            total += num * unit
            num = 0
        elif ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        else:
            return None
    return total + num


def _overlaps(a0: int, a1: int, span) -> bool:
    """[a0, a1) 是否与 span (s0, s1) 重叠；span 为 None 返回 False"""
    if not span:
        return False
    s0, s1 = span
    return a0 < s1 and s0 < a1


def _extract_year(stem: str, folder: str, exclude_spans=()) -> Optional[int]:
    """从文件名 stem、目录名 folder 提取年份。

    旧 YEAR_RE 的两个 bug：
    1. 要求年份后必须跟 `)`/`.`/空白 → `Inception.2010`、`Wonder Woman 1984`、
       `Dune.2021`（位于 stem 末尾）解析不到；
    2. 只取第一个匹配 → `Blade Runner 2049 (2017)` 先撞上 " 2049 "，
       范围检查拒绝后直接返回 None，不再尝试 (2017)。

    新规则：扫描全部候选，括号里的年份最优先，否则取最晚出现的有效候选
    （年份通常缀在标题末尾；`2012.2009` 里 2012 是片名、2009 才是上映年）。
    exclude_spans：(start, end) 列表（基于 stem 坐标），与之重叠的候选跳过，
    避免 `S01E2019` 的 2019 被当成年份。
    """
    now_max = datetime.now().year + 2

    def _cands(text: str, exclude=()):
        out = []
        for m in _YEAR_CAND_RE.finditer(text or ""):
            y = int(m.group(1))
            if not (1900 <= y <= now_max):
                continue
            if any(_overlaps(m.start(), m.end(), sp) for sp in exclude):
                continue
            # 括号里的年份最可信：[(（[【]YYYY[)）\]】]，括号内允许空格
            i, j = m.start() - 1, m.end()
            while i >= 0 and text[i] in " \t":
                i -= 1
            while j < len(text) and text[j] in " \t":
                j += 1
            paren = (i >= 0 and text[i] in "(（[【"
                     and j < len(text) and text[j] in ")）]】")
            out.append((paren, m.start(), y))
        return out

    cands = _cands(stem, exclude_spans)
    if not cands:
        cands = _cands(folder)
    if not cands:
        return None
    # 括号优先；同优先级取最晚出现
    cands.sort(key=lambda t: (t[0], t[1]))
    return cands[-1][2]

# ==================== 发行平台识别（虚拟媒体库的数据来源）====================
# 片名里的发行组标签是最稳定的平台信号：NF / DSNP / ATVP / AMZN / MAX …
# 这里把常见标签映射成“平台 id（展示名）”，供按平台自动生成虚拟媒体库。
PLATFORM_TAGS: dict[str, tuple[str, ...]] = {
    "netflix": ("nf", "netflix", "nfhd", "nfweb"),
    "disney": ("dsnp", "dsny", "disney", "disneyplus"),
    "appletv": ("atvp", "atv", "appletv", "itunes", "atv4k"),
    "prime": ("amzn", "prime", "amazon", "primevideo"),
    "max": ("max", "hmax", "hbo", "hbomax"),
    "hulu": ("hulu",),
    "paramount": ("pmtp", "pmp", "paramount", "paramountplus"),
    "peacock": ("pcok", "peacock"),
    "crunchyroll": ("cr", "crunchyroll", "cr-web", "crweb"),
}
# 展示名（虚拟媒体库名称）
PLATFORM_LABELS: dict[str, str] = {
    "netflix": "Netflix",
    "disney": "Disney+",
    "appletv": "Apple TV+",
    "prime": "Prime Video",
    "max": "Max",
    "hulu": "Hulu",
    "paramount": "Paramount+",
    "peacock": "Peacock",
    "crunchyroll": "Crunchyroll",
}
# 只认“独立成词”的标签，避免 nf / cr / max 这种短标签误命中片名
_PLATFORM_RE = {
    platform: re.compile(
        r"(?<![A-Za-z0-9])(" + "|".join(re.escape(t) for t in sorted(tags, key=len, reverse=True)) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for platform, tags in PLATFORM_TAGS.items()
}


# 发布标识（分辨率/来源/编码）：短平台标签（nf / cr / max …）只有出现在真正的发布名里才认，
# 否则一部就叫《Max》或《C.R.》的电影会被打成 HBO Max 库。
_RELEASE_HINT_RE = re.compile(
    r"(?<![A-Za-z0-9])(2160p|1080p|720p|480p|4k|uhd|web-?dl|webrip|bluray|blu-ray|"
    r"bdrip|remux|hdtv|hdrip|dvdrip|x264|x265|h264|h265|hevc|avc)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
SHORT_PLATFORM_TAG_LEN = 4


def detect_platforms(path: str) -> list[str]:
    """从文件名/目录识别发行平台（返回平台 id 列表，稳定有序）

    片名与最近几层父目录一起作为依据（整季常把发行平台写在目录名上）。
    """
    text = os.path.basename(path or "")
    parents = (path or "").replace("\\", "/").split("/")[-4:-1]
    haystack = " ".join([text, *parents])
    if not haystack.strip():
        return []
    release_like = bool(_RELEASE_HINT_RE.search(haystack))
    found = [
        platform
        for platform, pat in _PLATFORM_RE.items()
        if pat.search(haystack)
        and (release_like or all(len(t) > SHORT_PLATFORM_TAG_LEN for t in PLATFORM_TAGS[platform]))
    ]
    return sorted(found)


# ==================== 刮削策略 ====================
# 后台可选的媒体库刮削策略：天数为“到期重刮”窗口，None = 永远只补缺，0 = 每次全量
SCAN_POLICIES: dict[str, Optional[int]] = {
    "missing_only": None,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    "all": 0,
}
DEFAULT_SCRAPE_POLICY = "missing_only"

def normalize_scrape_policy(value: Optional[str]) -> str:
    """归一化刮削策略（未知值回退为 missing_only，不会因为脏数据全量重刮）"""
    return value if value in SCAN_POLICIES else DEFAULT_SCRAPE_POLICY


def should_scrape(item, policy: str, now: Optional[datetime] = None) -> bool:
    """按策略判断某个条目是否需要（重新）刮削

    - `missing_only`：只补缺（没 TMDB 命中就刮，已有就不再建请求——省配额）
    - `3m` / `6m` / `1y`：到期重刮；**缺元数据的总是补**，不受窗口限制
    - `all`：每次全量重刮（仅在管理员显式选择时生效）
    """
    policy = normalize_scrape_policy(policy)
    if not item.tmdb_id:  # 缺元数据：任何策略下都要刮
        return True
    if policy == "missing_only":
        return False
    if policy == "all":
        return True
    window = SCAN_POLICIES.get(policy)
    if not window:
        return False
    last = item.last_scraped_at or item.date_added
    if not last:
        return True
    return (now or datetime.now()) - last >= timedelta(days=window)


_INT64_MAX = 2 ** 63 - 1


def safe_probe_int(value, default: int = 0) -> int:
    """把探测结果里的数值字段收敛成能落库的整数。

    ffprobe/MediaInfo 在远程流上偶发给出 None、负数或超大码率。任一情况直接
    落库都会抛 ``NumericValueOutOfRange``，把这一条 commit 失败、连带整轮扫描
    或探测白跑，所以写库前一律走这里。
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        # 少数探测源给的是 "1234.9" 这类字符串小数，退一步按浮点解析再截断
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            return default
    if parsed < 0 or parsed > _INT64_MAX:
        return default
    return parsed


def needs_probe(item, path: str, size: Optional[int] = None) -> bool:
    """是否需要重新 ffprobe

    文件信息已经获取过就不再每次扫描重复探测——ffprobe 是扫描里最贵的一步，
    整库重扫时它会吃掉绝大部分时间。仅当文件大小变了（换源/重压）才重探。

    远程挂载（115 / WebDAV / AList）没有本机文件，大小由调用方从目录列表传入。
    """
    # 远程探测失败后的 degraded、以及 ffprobe 跑完但拿不到时长的 probed_no_duration，
    # 都是**明确的终态结果**：同一文件不要每轮扫描无限重试（否则 2.6 万条会把 worker
    # 名额占满，真正能探到的文件排不进来）。用户按需点详情/播放时再 boost_probe，
    # 或文件大小变化时重新排队。
    if (getattr(item, "probe_status", None) in ("degraded", "probed_no_duration")
            and item.last_probed_at is not None):
        if size is None:
            return False
        return bool(item.size) and size != item.size
    if not item.duration_ticks or item.last_probed_at is None:
        return True
    if size is None:
        try:
            size = os.path.getsize(path)
        except OSError:
            # 远程源取不到大小时不重复探测（ffprobe 走网络更贵），保留已有元数据
            return False
    if not size:
        return False
    return bool(item.size) and size != item.size


def item_guid(path: str) -> str:
    """由稳定路径生成 32 位 guid"""
    return hashlib.md5(("rb:item:" + path.lower()).encode("utf-8")).hexdigest()


def _multi_episode_guid(stored_path: str, episode: int) -> str:
    """多集文件第 N 集行的 guid：与主行（path 派生）同族、必不冲突。

    主行 guid = item_guid(path)；第二行 = item_guid(f"{path}#E{episode:03d}")。
    md5 十六进制恒 32 位，符合 emby_items.guid 长度不变式；重命名/移动时
    两行按各自 guid 独立走改名识别（见 _match_renames_by_*），互不干扰。
    """
    return item_guid(f"{stored_path}#E{episode:03d}")


def _find_duplicate_series(db, library_id: int, tmdb_id=None,
                           name=None, year=None) -> list:
    """系统性去重：建 series 前查库里是否已有同部剧。

    去重键（参考 Emby 多版本逻辑）：
    - 有 tmdb_id：同库同 tmdb_id
    - 无 tmdb_id：同库同归一化标题（年份 ±1 容差）

    返回已存在的 series id 列表（按 id 升序，最早的在前）。
    """
    MI = emby_models.MediaItem
    q = db.query(MI.id).filter(
        MI.library_id == library_id,
        MI.item_type == "series",
        MI.deleted_at.is_(None),
    )
    tmdb_id = (str(tmdb_id or "")).strip()
    if tmdb_id:
        q = q.filter(MI.tmdb_id == tmdb_id)
        return [row[0] for row in q.order_by(MI.id.asc()).all()]

    # 无 tmdb_id：按归一化标题匹配
    try:
        from backend.emby_server import dedup as dedup_lib
        norm = dedup_lib.normalize_name(name)
    except ImportError:
        norm = (name or "").strip().lower()
    if not norm:
        return []
    candidates = q
    if year:
        candidates = candidates.filter(
            MI.production_year.between(year - 1, year + 1))
    # 需要 name 做 Python 层精确比对
    rows = db.query(MI.id, MI.name, MI.production_year).filter(
        MI.library_id == library_id,
        MI.item_type == "series",
        MI.deleted_at.is_(None),
    )
    if year:
        rows = rows.filter(MI.production_year.between(year - 1, year + 1))
    try:
        from backend.emby_server import dedup as dedup_lib
        norm_fn = dedup_lib.normalize_name
    except ImportError:
        norm_fn = lambda n: (n or "").strip().lower()  # noqa: E731
    out = []
    for row_id, row_name, row_year in rows.order_by(MI.id.asc()).all():
        if norm_fn(row_name) != norm:
            continue
        out.append(row_id)
    return out


def _file_fingerprint(scan_file: "ScanFile") -> str:
    """文件指纹：秒跳未变更文件的唯一依据。

    本机文件用 path|size|mtime_ns（一次 os.stat，多一个系统调用）；
    远程挂载拿不到 mtime（多一次 API 太贵），用 path|size。
    大小变了一定命中不了；同名替换但大小时间都一样是玄学，接受。
    """
    if scan_file.mtime_ns is not None:
        raw = "|".join((scan_file.stored_path, str(scan_file.size),
                        str(scan_file.mtime_ns)))
    else:
        # P2 修复（完美审查 C-P2-3）：远端指纹混入网盘 ModTime（rclone list_dir
        # 本来就有，见 MountEntry.mod_ts）——同尺寸内容替换时 path|size 不变，
        # 之前 L1 秒跳与目录指纹双双放行，元数据永久陈旧。拿不到 ModTime 的
        # 提供方保持老口径，避免全量指纹抖动。
        rmt = float(getattr(scan_file, "remote_mod_ts", 0.0) or 0.0)
        if rmt > 0:
            raw = "|".join((scan_file.stored_path, str(scan_file.size),
                            f"{rmt:.0f}"))
        else:
            raw = "|".join((scan_file.stored_path, str(scan_file.size)))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _external_subtitles_changed(ctx: "_ScanContext", item, scan_file: "ScanFile") -> bool:
    """视频指纹未变时，检查外挂字幕有无增减。

    当前字幕用扫描器已缓存的目录列表（零额外 IO）；已登记字幕用本批预加载的
    ctx.ext_subtitles（零额外 SQL）。返回 True 表示字幕变了 → 不能秒跳，
    要走正常流程重做 side（probe 会因视频文件未变而自动跳过，只重登记字幕）。
    """
    try:
        if scan_file.local_dir:
            names = _local_names(ctx, scan_file.local_dir)
            current = find_external_subtitles_in(names, scan_file.stored_path)
            current_paths = {p for _, p in current}
        elif scan_file.mount_id is not None:
            entries = _mount_names(ctx, scan_file)
            current = find_external_subtitles_remote(
                scan_file.name, entries, scan_file.mount_id, scan_file.dir_rel)
            current_paths = {p for _, p in current}
        else:
            return False
        db_paths = ctx.ext_subtitles.get(getattr(item, "id", None), set())
        return current_paths != db_paths
    except Exception:
        # 查不到就保守一点：不秒跳，走正常流程（安全优先）
        return True


def _tidy_name(raw: str) -> str:
    """清洗片名；结果只剩分隔符时返回空（供回退到目录名用）"""
    out = clean_name(raw or "")
    return "" if not out.strip(" .-_[]()") else out


# 发行平台标签（NF / DSNP / ATVP …）也属于发布标签，片名里要一起去掉
_PLATFORM_TOKEN_RE = re.compile(
    r"(" + "|".join(
        sorted((t for tags in PLATFORM_TAGS.values() for t in tags), key=len, reverse=True)
    ) + r")",
    re.IGNORECASE,
)


def _strip_platform_tags(name: str) -> str:
    """去掉片名里的发行平台标签

    只删非首个词：一部片名就叫 `Max` / `Hulu` 的电影不会被吃空。
    否则 `Alpha.Target.2024.1080p.NF.WEB-DL` 会入库成 “Alpha Target  NF”。
    """
    parts = [p for p in (name or "").split() if p]
    return " ".join(p for idx, p in enumerate(parts) if idx == 0 or not _PLATFORM_TOKEN_RE.fullmatch(p))


# 名字尾巴上带括号的年份：「泪之女王 (2024)」「好想告诉你（2023）」。
# 只认结尾、带括号的 4 位年份（1900–2099）：片名本体的数字没有括号
# （如电影《2012》）、不会命中；「(真人版)」这类非年份括号也不命中。
_TRAILING_YEAR_RE = re.compile(
    r"\s*[\(\[【（]\s*((?:19|20)\d{2})\s*[\)\]】）]\s*$")


def _split_trailing_year(name: str):
    """把片名尾巴带括号的年份剥出来，返回 (剥净的名字, 年份或 None)。"""
    m = _TRAILING_YEAR_RE.search(name or "")
    if not m:
        return name, None
    return (name[: m.start()].strip(), int(m.group(1)))


def _strip_year(name: str, year) -> str:
    """从片名里去掉年份（含括号），避免留下空括号 `()`。

    直接 `name.replace(str(year), "")` 会把 `母亲往事 (2025)` 变成 `母亲往事 ()`，
    而 `.strip(" .-_()")` 只剥字符串两端、剥不掉中间的 `()`——这就是生产库
    115 条 `XXX () 3` 乱码的根因（见 fix/scrape-title-garbled-parens）。
    """
    if not year:
        # 年份没解析出来时，尾巴上带括号的年份也要剥（见 _TRAILING_YEAR_RE）：
        # 否则「泪之女王 (2024)」整段当片名入库、年份栏反而留空，客户端
        # 会把年份显示两遍（2026-10-20 生产 1,662 条实证，用户亲令
        # 「名字后面不要带年份」）。剥出的年份由调用方按需回填年份栏。
        return _split_trailing_year(name)[0]
    out = re.sub(r"[\(\[【（]\s*" + str(year) + r"\s*[\)\]】）]", " ", name)
    if str(year) in out:
        out = out.replace(str(year), "")
    return re.sub(r"\s+", " ", out).strip(" .-_()")


# 标题里紧贴文字的版本号 token：`梅根2.0` 的 `2.0`、`哥斯拉-1.0` 的 `-1.0`。
# 其中的 `.`/`-` 是片名的一部分、不是分隔符；CLEAN_RE 会把它们拆成空格
# （生产实证：「梅根2 0」「哥斯拉 1 0」）。清洗前先把 token 内的 `.`/`-`
# 换成占位符，清洗完再还原。约束（防误伤点分风格的发布名）：
# - 只认紧贴非空白字符的 token（`(?<=\S)`）：独立的空格分隔版本号
#   （如 `Movie 2.0`）不在此列，保持既有行为；
# - 整数/小数各 1–2 位且两侧不挨数字与点：`2024.1080p`（年份.分辨率）
#   这类链不能被当成版本号，否则会洗出「Alpha Target 2024.」的尾巴点。
_VERSION_TOKEN_RE = re.compile(r"(?<=\S)(?<![\d.])-?\d{1,2}\.\d{1,2}(?![\d.])")
_VERSION_DOT_PLACEHOLDER = "\ue000"
_VERSION_DASH_PLACEHOLDER = "\ue001"


_DUP_DASH_BEFORE_YEAR_RE = re.compile(
    r"-\s*(\d{1,2})(?=\s*[\(\[【]|\.\s*(?:19|20)\d{2}\b)"
)


def _strip_redundant_dash_number(name: str) -> str:
    """去掉标题后紧贴的冗余 `-N` 重复标记（其后是年份/质量标签时）。

    只剔"冗余"的：N 必须已作为数字出现在它前面的标题里。
    - `月球叛军2：烙印之人-2 (2024)`：标题里已有 `2`，`-2` 是下载器的重复
      记号 → 剔除（生产实证）。
    - `Delta Movie 02-00 (2020)`：标题里的数字是 `02`，`-00` 不是冗余，
      是真标题的一部分 → 保留。
    - `Avatar-2 (2022)`：标题里没有数字 → 保留（可能是真续集号）。
    数字比较按整数值（`02` 与 `2` 视为同一数字）。
    """
    def _repl(m: "re.Match") -> str:
        n = str(int(m.group(1)))
        title_before = name[: m.start()]
        # 纯数字标题守卫：标题里没有任何非数字的实义字符时（如真实电影
        # `11-11-11`、`24-24`），每个 `-N` 按上面的口径都会判"冗余"，
        # 启发式失效——这类标题的 `-N` 一律保留、保守不洗（r66 实证：
        # `11-11-11` 曾被洗成 `11 11`）。
        if not re.sub(r"[\d\s\.\-_\(\)\[\]:：]", "", title_before):
            return m.group(0)
        seen = {str(int(x)) for x in re.findall(r"\d+", title_before)}
        return "" if n in seen else m.group(0)

    return _DUP_DASH_BEFORE_YEAR_RE.sub(_repl, name)


def clean_name(raw: str) -> str:
    name = raw or ""
    # 版本号 token 先行保护（见 _VERSION_TOKEN_RE 注释）：`梅根2.0`→不拆，
    # `哥斯拉-1.0`→不拆。必须在下面的 `-N` 剔除之前，占位的 `-` 不再被误杀。
    name = _VERSION_TOKEN_RE.sub(
        lambda m: m.group(0)
        .replace("-", _VERSION_DASH_PLACEHOLDER)
        .replace(".", _VERSION_DOT_PLACEHOLDER),
        name,
    )
    # 去掉文件名主干末尾的 `[N]` 重复下载标记（如 `... - 1080p [2]`）：下载器
    # 给同名第二个文件打的记号，不是片名。必须在 CLEAN_RE 剥掉 `[]` 之前
    # 整段删除，否则只剩孤立的 `N` 留在片名里（生产实证：「第四公民 2」）。
    name = re.sub(r"\s*\[\s*\d{1,2}\s*\]\s*$", "", name)
    # 去掉 `-N` 重复标记（多版本/重复下载，非片名），两种位置：
    # ① 字符串末尾（`WEB-DL-3`，既有行为）；② 紧跟年份段之前
    # （`月球叛军2：烙印之人-2 (2024)` 的 `-2`，生产实证曾入库成
    # 「月球叛军2：烙印之人 2」）。
    # 必须在 CLEAN_RE 把 `-` 换成空格之前处理，否则会变成孤立数字留下来。
    # 版本号 token 已在上面占位保护，`-1.0` 不会被这里误删；
    # ② 必须认"冗余"才剔（见 _strip_redundant_dash_number）：`Delta Movie 02-00
    # (2020)` 的 `-00` 是真标题的一部分——剁掉会让同目录 25 个文件解析成同一
    # 个标题，进而撞上扫描器的同名电影去重（_claim_batch_guids），把别人的行
    # 复用掉（CI 冒烟 side=202≠201 的实证）。
    name = re.sub(r"-\s*\d{1,2}\s*$", "", name)
    name = _strip_redundant_dash_number(name)
    # 兜底串：重复标记已剥净的版本。纯标记输入（如 ` [2]`）最后不把标记
    # 当片名吐回去（之前会因为 `or raw` 变成「2」/「[2]」）。
    fallback = name
    name = CLEAN_RE.sub(" ", name)
    # 去掉质量标签。注意 CLEAN_RE 已经把 `.` `_` `-` 换成了空格，所以这里的分隔符
    # 必须容得下空格：否则 "WEB-DL" 变成 "WEB DL" 后不再被识别。
    name = re.sub(
        r"\b(2160p|1440p|1080p|1080i|720p|480p|\d{3,4}[pi]|4k|8k|uhd|hdr10\+?|hdr|dolby|dv|hevc|h\.?264|x264|h\.?265|x265|"
        r"aac|ac3|eac3|dts|flac|truehd|atmos|web[\s\-]?dl|webrip|web|remux|blu[\s\-]?ray|bluray|"
        r"bdrip|brrip|hdtv|dvdrip|diy|repack|proper|multi|internal|chs&eng|chs|cht|eng|gb|big5)\b",
        " ",
        name,
        flags=re.IGNORECASE,
    )
    name = _strip_platform_tags(name)
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    # 还原版本号 token 的占位符
    name = name.replace(_VERSION_DOT_PLACEHOLDER, ".").replace(
        _VERSION_DASH_PLACEHOLDER, "-")
    fallback = fallback.replace(_VERSION_DOT_PLACEHOLDER, ".").replace(
        _VERSION_DASH_PLACEHOLDER, "-")
    if name:
        return name
    if not fallback:
        # 输入只剩重复标记（` [2]` / `-3` 之类）：标记已剥净，返回空，
        # 让上层回退到目录名，而不是把标记原样当片名。
        return ""
    return fallback or raw


def _season_from_dirname(folder: str) -> Optional[int]:
    """A3（缺集普查 2026-10-19）：父目录名的确定性季号兜底。

    只认与文件名解析同一套的季号形态（Season 1 / S01 / 第1季…）；
    目录名给出 0 个或多个**不同**值时返回 None——含糊的一律不动（宁漏勿错标）。
    """
    if not folder:
        return None
    found = set()
    for pat in SEASON_PATTERNS_ANY + SEASON_PATTERNS_TV:
        m = pat.search(folder)
        if not m:
            continue
        raw = m.group(1)
        v = cn_to_int(raw) if raw and not raw.isdigit() else int(raw)
        if v is not None and 0 <= v <= 99:
            found.add(v)
    return found.pop() if len(found) == 1 else None


def parse_media_filename(path: str, library_type: str) -> dict:
    """从文件路径解析 基础名称/年份/季/集

    季的识别除 S01E02 / 1x02 / 第1集 外，还覆盖**只有季号**的常见命名：
    `S09`、`Season 9`、`第九季`、`第9季` —— 季包与中文命名场景下，
    旧实现认不出季，整季包会被当成电影。
    """
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    folder = os.path.basename(os.path.dirname(path))
    is_tv = library_type == "tvshows"

    season = episode = None
    episode_end = None
    # A3：季号是否来自文件名的显式标记（Sxx / Season N / 第N季…）。
    # 裸 E05 / 绝对集数给的 season=1 只是默认值，不算显式——父目录名有确定
    # 季号时允许兜底覆盖；显式标记永远优先，目录名不得覆盖它。
    season_explicit = False
    matched_in_folder = False
    # A3：只有 1a/1b 的集号匹配来自目录时才置 True；第 2 步的季号目录匹配
    # 不算——否则 `Season 3/Frieren - 28` 这类绝对集数会被连带跳过，
    # 集号恒为 None（普查 A3 的 episode_number 为空 104 行即此类）。
    ep_matched_in_folder = False
    match_span: Optional[tuple[int, int]] = None
    abs_ep_span: Optional[tuple[int, int]] = None

    def _to_num(g: str) -> Optional[int]:
        g = (g or "").strip()
        if not g:
            return None
        if g.isdigit():
            return int(g)
        return cn_to_int(g)

    # 1) 合集区间先行：S06E27-E28（见 EP_RANGE_RES 注释）
    for pat in EP_RANGE_RES:
        m = pat.search(stem)
        if not m and is_tv:
            m = pat.search(folder)
            matched_in_folder = m is not None
            ep_matched_in_folder = m is not None
        if not m:
            continue
        season, episode = int(m.group(1)), int(m.group(2))
        end = int(m.group(3))
        episode_end = end if end > episode else None
        season_explicit = True
        match_span = (m.start(), m.end())
        break

    # 1b) 带集号：文件名优先，剧集库再退回目录名
    #    剧集库先试 EP_PATTERNS_TV（独立 E05 / 4 位 EP），再试通用模式
    ep_patterns = (EP_PATTERNS_TV if is_tv else []) + EP_PATTERNS
    for pat in ep_patterns:
        if episode is not None:
            break
        m = pat.search(stem)
        if not m and is_tv:
            m = pat.search(folder)
            matched_in_folder = m is not None
            ep_matched_in_folder = m is not None
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 2:
            season, episode = int(groups[0]), _to_num(groups[1])
            season_explicit = True
        else:
            season, episode = 1, _to_num(groups[0])
        if episode is None:
            # 中文数字解析失败（如非法字符），继续试下一个模式
            season = None
            season_explicit = False
            continue
        match_span = (m.start(), m.end())
        break

    # 2) 只有季号：中文季与 "Season N" 任意库都认；裸 S09 仅剧集库认
    if season is None:
        season_patterns = SEASON_PATTERNS_ANY + (SEASON_PATTERNS_TV if is_tv else [])
        for pat in season_patterns:
            m = pat.search(stem)
            if not m and is_tv:
                m = pat.search(folder)
                matched_in_folder = m is not None
            if not m:
                continue
            raw = m.group(1)
            season = cn_to_int(raw) if raw and not raw.isdigit() else int(raw)
            if season is None or season < 0 or season > 99:
                season = None
                continue
            season_explicit = True
            match_span = (m.start(), m.end())
            break

    # 2b) 绝对集数（仅剧集库）：动漫 `Frieren - 28` 这类无 E 标记的写法。
    # 必须在季号匹配之后：`The 100 Season 1` 的 "1" 是季号，不能被当成集号
    #（用 _overlaps 排除已匹配段）。
    # 年份段位 (1900-2099) 让位给年份解析。
    if episode is None and is_tv and not ep_matched_in_folder:
        m = _ABS_EP_RE.search(stem)
        if m and not _overlaps(m.start(), m.end(), match_span):
            n = int(m.group(1))
            if not (1900 <= n <= 2099):
                if season is None:
                    season = 1
                episode = n
                abs_ep_span = (m.start(), m.end())

    # A3（缺集普查 2026-10-19）：季号兜底。文件名没给显式季号（裸 E05 /
    # 绝对集数只给了默认的 season=1）时，看父目录名有没有确定的季号。
    # 只认既有可解析形态（Season 1 / S01 / 第1季…），目录名含糊（无值或
    # 多个不同值）时不动——宁漏勿错标。显式季号永远优先，不会被覆盖。
    if not season_explicit and is_tv:
        _dir_season = _season_from_dirname(folder)
        if _dir_season is not None:
            season = _dir_season

    # 片名：把匹配到的季/集标记从名称里**挖掉**其余保留。
    # 只看前缀在 "Rick.and.Morty.Season 9" 这类命名上会把季号留在片名里，
    # 也会在标记在最前面时丢掉片名。
    # 从后往前挖，避免位置偏移；只有基于 target 文本的段才参与挖掘
    #（match_span 可能基于 folder，abs_ep_span 只在 stem 上）。
    # r60：挖词目标按集号段的来源定——A3 新增组合（季号在目录、集号用绝对
    # 集数在文件名）时 matched_in_folder 为 True，但 abs_ep_span 是 stem
    # 坐标；此时必须从 stem 挖，否则会把 stem 坐标的 span 切在 folder 文本
    # 上（实证："Season 3/Frieren - 28" 的名被挖成 "Season 3" 而不是
    # "Frieren"，单集展示名变成 "Season 3 S03E28"）。
    target = folder if (matched_in_folder and abs_ep_span is None) else stem
    dig_spans = []
    if match_span and not matched_in_folder:
        dig_spans.append(match_span)
    if abs_ep_span:
        dig_spans.append(abs_ep_span)
    raw_name = target
    for s0, s1 in sorted(dig_spans, reverse=True):
        raw_name = f"{raw_name[:s0]} {raw_name[s1:]}".strip()
    name_part = _tidy_name(raw_name) or _tidy_name(folder) or _tidy_name(stem)

    # 年份：排除集号匹配段，避免 S01E2019 的 2019 被当成年份
    #（_extract_year 内部已处理 stem→folder 回退；exclude 只作用于 stem 坐标）
    year_spans = [s for s in (match_span if not matched_in_folder else None,
                             abs_ep_span) if s]
    year = _extract_year(stem, folder, exclude_spans=year_spans)
    if year:
        name_part = _strip_year(name_part, year)
    elif name_part:
        # 年份没从文件名/目录解析出来时，片名尾巴带括号的年份就是唯一
        # 线索：剥出来填进年份栏、名字里去掉（与 _series_name_of 同口径，
        # 见 _TRAILING_YEAR_RE 的实证注释）。
        name_part, trailing_year = _split_trailing_year(name_part)
        if trailing_year is not None:
            year = trailing_year

    return {"name": name_part or stem, "year": year, "season": season, "episode": episode,
            "episode_end": episode_end}


def _episode_display_name(name: str, season_no: Optional[int], ep_no: Optional[int]) -> str:
    """一集的标准展示名：``片名 S01E02``

    **只解析出季号、没有集号**时（``片名 Season 2.mkv``、季包整季文件）不能硬套
    ``E{ep_no:02d}``：``None`` 做 ``:02d`` 会抛
    ``TypeError: unsupported format string passed to NoneType.__format__``，
    整个媒体库的扫描会在这一行崩掉、前面已扫的批次全部白跑。

    这类文件按 ``片名 S02`` 命名，集号留空（后续人工补），不猜、不填 0。
    """
    base = (name or "").strip()
    if ep_no is None:
        return f"{base} S{season_no:02d}" if season_no is not None else base
    if season_no is None:
        return f"{base} E{ep_no:02d}"
    return f"{base} S{season_no:02d}E{ep_no:02d}"


# 远程 ffprobe 的有限 Range 上限：rclone rc-serve 不接受默认的开放式
# ``Range: bytes=0-``，但媒体头（尤其 Matroska 的 EBML/Info）通常在前段即可拿到。
#
# 窗口不能开太大：ffprobe 会把 206 响应当成**整个文件**，窗口一大就等于喂了半个文件，
# 它读到截断处判定 "File ended prematurely" 反而失败。实测 1 MiB 稳定拿到 mkv 时长，
# 8 MiB 就会触发该错误，所以默认按 1 MiB 走（256 KiB～4 MiB 均可，按需用环境变量调）。
PROBE_REMOTE_RANGE_BYTES = env_int("PROBE_REMOTE_RANGE_BYTES", 1 << 20, 1 << 18)

# 第三跳（去掉 Range 让 ffprobe 自己 seek）的流量闸：moov 在文件尾部时
# 这一跳等于把整个文件下载下来，一个大文件就能烧掉一份单文件下载配额。
# 已知大小超过此阈值的远程文件跳过第三跳、记 degraded（信息不完整但
# 可播放），不拿配额换探测完整度。<=0 表示不设闸（回到旧行为）。
PROBE_WHOLE_FILE_MAX_BYTES = env_int("PROBE_WHOLE_FILE_MAX_BYTES", 1 << 30)


# 第二跳（双 Range）的**尾部**窗口。moov 原子在文件尾的 MP4/MOV，头部窗口读不到时长，
# 这时才并行再取一段尾巴——只对下面这些容器开，其它封装（MKV 的时长在 EBML 头部）
# 第一跳就拿到了，别为它们多花流量。
PROBE_REMOTE_TAIL_BYTES = env_int("PROBE_REMOTE_TAIL_BYTES", 2 << 20, 1 << 18)

# 自适应尾窗上限：moov 比固定尾窗大时（实测 2.2~4.6 MiB），按 mdat 声明长度算出的
# moov 跨度在此上限内才补一次精确 Range。超过上限的按现行失败语义走（日志计数），
# 不无封顶地烧探测流量。
PROBE_MOOV_MAX_BYTES = env_int("PROBE_MOOV_MAX_BYTES", 8 << 20, 1 << 20)


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in ("0", "false", "no", "off")


# 头尾探测（探测正案第 1 步）：不再让 ffprobe 直接在 FUSE 上乱 seek——两次读
# （头部 + 尾部），写进同尺寸的稀疏临时文件（truncate(size) 后只填头尾），再对
# 本地文件跑 ffprobe -probesize 5M -analyzeduration 0。mkv 的 Info/Tracks 在头部、
# mp4 的 moov 在头或尾，都覆盖得到；50GB 的原盘和 500MB 的剧集一样只读头尾两段，
# 耗时与大小无关。失败时返回 None，调用方回退现行流程。
PROBE_HEADTAIL_ENABLED = _env_flag("PROBE_HEADTAIL_ENABLED")
PROBE_HEADTAIL_HEAD_BYTES = env_int("PROBE_HEADTAIL_HEAD_BYTES", 4 << 20, 1 << 20)
PROBE_HEADTAIL_TAIL_BYTES = env_int("PROBE_HEADTAIL_TAIL_BYTES", 16 << 20, 1 << 20)
PROBE_HEADTAIL_PROBESIZE = env_int("PROBE_HEADTAIL_PROBESIZE", 5 << 20, 1024)
PROBE_HEADTAIL_ANALYZEDURATION = env_int("PROBE_HEADTAIL_ANALYZEDURATION", 0, 0)

#: 需要「尾部再读一次」的容器（moov 可能落在文件尾）。
#: 只收 MP4/MOV 系：Matroska / WebM 的时长写在文件头的 Segment Info 里，
#: 第一跳 1 MiB 稳定命中，给它们加尾部请求纯属白烧网盘流量与带宽。
_TAIL_MOOV_CONTAINERS = frozenset({
    "mp4", "m4v", "m4a", "mov", "qt", "3gp", "3g2", "f4v",
})

#: 双 Range 抓字节的超时。rclone rc-serve / 云盘直链首字节慢是常态，
#: 但也别放到和 ffprobe 一样的 90 秒——那是兜底 seek 路径的量级。
PROBE_HTTP_TIMEOUT = 45.0

#: 探测**本机文件**（含 FUSE / rclone 挂载）的超时秒数。
#:
#: 原先本机与远端共用 90 秒，而 ffprobe 失败后还会再跑一次同样 90 秒的 MediaInfo
#: 备用探测——单个文件最坏 180 秒。生产实测（2026-10-05）：4 核机器、``/mnt/mp``
#: 是 fuse.rclone，mediainfo 常驻 D 状态（不可中断 IO），探测吞吐掉到约 1.3 个
#: 文件/分钟，一千多个待探测条目堆着不动，同时把补全 worker 的 IO 一起堵住。
#:
#: 读文件头不需要 90 秒：正常文件秒级返回，读不出来的是挂载本身没响应，等满 90 秒
#: 也等不到——只会白占一个 worker 槽位。
PROBE_FILE_TIMEOUT = env_float("PROBE_FILE_TIMEOUT_SEC", 30.0, 5.0)
#: 远端 URL 的探测上限不变：网络慢是常态，砍时间会误伤真正在下载的大文件。
PROBE_REMOTE_TIMEOUT = env_float("PROBE_REMOTE_TIMEOUT_SEC", 90.0, 10.0)
#: ffprobe 分析上限（Hark 6链路第4项）：显式限制，避免在残缺/远程流上无限制读取。
#: -probesize 字节数（默认 10 MiB），-analyzeduration 微秒数（默认 10s）。
FFPROBE_PROBESIZE = env_int("FFPROBE_PROBESIZE", 10485760, 1024, 1 << 34)
FFPROBE_ANALYZEDURATION = env_int("FFPROBE_ANALYZEDURATION", 10000000, 1000, 1 << 40)

#: 最近一次 ffprobe 是否以超时告终。ffprobe 跑在多个 worker 线程里，所以用线程局部
#: 存；判定只发生在紧接其后的调用点，不跨条目复用。
_ffprobe_timeout_flag = threading.local()


def _mark_ffprobe_timed_out() -> None:
    _ffprobe_timeout_flag.timed_out = True


def _last_ffprobe_timed_out() -> bool:
    """读取「本次 ffprobe 是否超时」并**清零**（供 probe_metadata 决定要不要跑备用探测）

    读完就清，避免上一条标记让后面的判定走偏；``_ffprobe`` 入口也会再清一次——
    两处都清是刻意的：这里防「上一条」，入口防「上一跳」。
    """
    was = bool(getattr(_ffprobe_timeout_flag, "timed_out", False))
    _ffprobe_timeout_flag.timed_out = False
    return was


# ffprobe 错误码翻译：把含糊的底层错误转成可行动的信息
_FFPROBE_HTTP_ERRORS = {
    403: "远端配额/权限受限 (HTTP 403)——可能是 Google Drive 每日下载配额耗尽，24 小时后自动恢复；或检查 rclone 账号权限",
    401: "远端认证失败 (HTTP 401)——检查 rclone 账号/Token 是否过期",
    404: "远端文件不存在 (HTTP 404)——文件可能已被删除或路径变更",
    429: "远端限流 (HTTP 429)——请求过快，稍后重试",
}


def _parse_ffprobe_http_error(stderr: str) -> Optional[int]:
    """从 ffprobe stderr 解析 HTTP 状态码。

    ffprobe 的 http 层对非 200 一律报 404（误导人），实际需从
    "Server returned 403 Forbidden" 这类信息里提取真实状态码。
    """
    import re
    # 匹配 "Server returned 403 Forbidden" 或 "HTTP error 403" 等
    for pat in (r"Server returned (\d{3})", r"HTTP error (\d{3})", r"HTTP (\d{3})"):
        m = re.search(pat, stderr)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


def _ffprobe(path: str, headers: Optional[dict] = None, size: int = 0,
             ranged: bool = True, local_path: str = "",
             probesize: Optional[int] = None, analyzeduration: Optional[int] = None) -> Optional[dict]:
    """ffprobe 提取媒体信息（无 ffprobe 时优雅降级）。

    远程直链补一个有限 Range，兼容 rclone rc-serve；本机文件不改变行为。
    ``ranged=False`` 时不注入 Range，让 ffprobe 自己按需 seek（慢速回退路径，
    见 ``probe_metadata``：有限窗口读不到 moov 的容器需要它）。
    ``local_path`` 非空时改为探测**这个本地文件**（双 Range 拼出来的「头部 + moov」临时文件），
    完全不碰 Range / ``-headers``——这两项只对 http 协议有效，喂本地文件反而会让
    ffprobe 报 ``Option headers not found`` 直接失败。
    ``probesize`` / ``analyzeduration`` 非空时覆盖模块默认的 FFPROBE_PROBESIZE /
    FFPROBE_ANALYZEDURATION（头尾探测按正案要求用 -probesize 5M -analyzeduration 0）。

    返回的 dict 可能含 `_error` 字段：
    - `_error="quota"`：HTTP 403，远端配额/权限受限
    - `_error="not_found"`：HTTP 404，文件不存在
    - `_error="auth"`：HTTP 401，认证失败
    """
    if not shutil_which("ffprobe"):
        return None
    # -v error：只输出错误（不用 quiet，否则 403/404 的真实原因被吞掉）
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    # Hark 6链路第4项：显式限制分析量，避免在残缺/远程流上无限制读取导致探测慢
    cmd += ["-probesize", str(FFPROBE_PROBESIZE if probesize is None else probesize),
            "-analyzeduration", str(FFPROBE_ANALYZEDURATION if analyzeduration is None else analyzeduration)]
    target = local_path or path
    if not local_path:
        probe_headers = dict(headers or {})
        if ranged and path.startswith(("http://", "https://")) and not any(
            str(k).lower() == "range" for k in probe_headers
        ):
            total = int(size or 0)
            end = min(total - 1, PROBE_REMOTE_RANGE_BYTES - 1) if total > 0 else PROBE_REMOTE_RANGE_BYTES - 1
            probe_headers["Range"] = f"bytes=0-{max(0, end)}"
        if probe_headers:
            joined = "".join(f"{k}: {v}\r\n" for k, v in probe_headers.items())
            cmd += ["-headers", joined]
    cmd.append(target)
    # 扫描探测让路给播放（见文件头「扫描限速」）：只降这一条子命令，不碰本进程
    cmd = _io_nice_command(cmd)
    # 本机文件（含 FUSE）与远端 URL 用不同上限：FUSE 读不出就是读不出，
    # 干等 90 秒只是白占 worker 槽位（见 PROBE_FILE_TIMEOUT 的说明）。
    _is_remote = (not local_path) and path.startswith(("http://", "https://"))
    _timeout = PROBE_REMOTE_TIMEOUT if _is_remote else PROBE_FILE_TIMEOUT
    # 每次调用先清零：上一次 ffprobe 超时不应让这一次的判定跟着走偏。
    _ffprobe_timeout_flag.timed_out = False
    try:
        out = _run_probe_cmd(cmd, timeout=_timeout)
        import json

        data = json.loads(out.stdout or "{}")
        # 检查 stderr 里的 HTTP 错误（ffprobe 把 403 也报成 404，需提取真实码）
        if not data.get("format"):
            http_code = _parse_ffprobe_http_error(out.stderr or "")
            if http_code:
                data["_http_code"] = http_code
                detail = _FFPROBE_HTTP_ERRORS.get(http_code, f"远端 HTTP 错误 ({http_code})")
                data["_error_detail"] = detail
                if http_code == 403:
                    data["_error"] = "quota"
                    logger.warning("ffprobe 探测 %s: %s", path, detail)
                elif http_code == 404:
                    data["_error"] = "not_found"
                elif http_code == 401:
                    data["_error"] = "auth"
                elif http_code == 429:
                    data["_error"] = "ratelimit"
                elif http_code in (500, 502, 503, 504):
                    data["_error"] = "server_error"
                else:
                    data["_error"] = f"http_{http_code}"
        return data
    except subprocess.TimeoutExpired:
        # 超时 ≠ 数据不对，而是「挂载/端点没响应」。标记给 probe_metadata，
        # 让它跳过同样会超时的 MediaInfo 备用探测。
        # 必须排在下面的宽 except **之前**：TimeoutExpired 继承自 SubprocessError，
        # 被宽分支先吃掉的话这个专用处理永远轮不到（测试就是这么抓出来的）。
        _mark_ffprobe_timed_out()
        logger.warning("ffprobe 超时（%ss）%s: 挂载或端点无响应", _timeout, path)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("ffprobe 失败 %s: %s", path, e)
        return None


def _run_probe_cmd(cmd, timeout: float):
    """跑 ffprobe / mediainfo：有上限、整组可杀（见 ``proc_util.bounded_run``）。

    原先是 ``subprocess.run(timeout=…)``：超时后只杀直接子进程再无限 ``wait()``，
    FUSE 挂死（D 状态）时 wait 永不返回，探测 worker 的线程被永久吃掉。
    这里同时尊重 ``proc_util.deadline`` 设下的「单条目总预算」。
    """
    from backend.emby_server import proc_util
    return proc_util.bounded_run(cmd, timeout=timeout, text=True)


def shutil_which(cmd: str) -> Optional[str]:
    from shutil import which

    return which(cmd)


def _mediainfo(path: str, headers: Optional[dict] = None, size: int = 0) -> Optional[dict]:
    """ffprobe 失败后的 MediaInfo 备用探测。

    MediaInfo 对部分 MP4/MKV 的容错比 ffprobe 好，但它同样可能被远程 Range 限制，
    所以只是 fallback，不把它当万能替代。返回与 _ffprobe 兼容的简化结构。
    """
    if not shutil_which("mediainfo"):
        return None
    cmd = ["mediainfo", "--Output=JSON"]
    # MediaInfo 没有 ffprobe 那样稳定的 header 注入口；本机文件直接用，
    # 远程 URL 失败就交给 degraded，不额外制造一次可能更慢的网络请求。
    if path.startswith(("http://", "https://")):
        return None
    cmd.append(path)
    # 与 ffprobe 用同一个上限：FUSE 上超时意味着挂载没响应，
    # 这时再等一次同样读不出东西。
    try:
        out = _run_probe_cmd(cmd, timeout=PROBE_FILE_TIMEOUT)
        import json
        raw = json.loads(out.stdout or "{}")
        tracks = raw.get("media", {}).get("track", [])
        if not tracks:
            return None
        info = {"format": {}, "streams": []}
        for t in tracks:
            kind = str(t.get("@type") or "").lower()
            if kind == "general":
                info["format"] = {
                    "duration": float(t.get("Duration") or 0) / 1000.0,
                    "size": int(float(t.get("FileSize") or size or 0)),
                    "bit_rate": int(float(t.get("OverallBitRate") or 0)),
                }
            elif kind in ("video", "audio", "text"):
                stype = {"video": "video", "audio": "audio", "text": "subtitle"}[kind]
                info["streams"].append({
                    "codec_type": stype,
                    "codec_name": t.get("CodecID") or t.get("Format"),
                    "width": int(float(t.get("Width") or 0)),
                    "height": int(float(t.get("Height") or 0)),
                    "bit_rate": int(float(t.get("BitRate") or 0)),
                    "channels": int(float(t.get("Channel_s_") or t.get("Channels") or 0)),
                    "tags": {"language": t.get("Language") or ""},
                })
        return info if info.get("format", {}).get("duration") else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("MediaInfo 备用探测失败 %s: %s", path, exc)
        return None


# ==================== 第二跳：远程 moov 在尾的双 Range 读取 ====================
#
# 背景：第一跳只注入 ``Range: bytes=0-1MiB``，ffprobe 把 206 当成整个文件。mkv 的时长在
# EBML 头部（第一跳就拿到），但**moov 原子在文件尾**的 mp4/mov 前 1 MiB 里根本没时长，
# 于是 format 为空。旧做法是「去掉 Range 再探一次」让 ffprobe 自己 seek——它会把整个文件
# 从头拉到尾，云盘直链上这是几 GB 的流量，2.6 万条文件撞上它就是场灾难。
#
# 现在改成：**并行**取两段（前 1 MiB + 后 N MiB），从尾段里把 moov 盒子**摘出来**，
# 拼成「头部 + moov」的小文件再喂 ffprobe，并把 mdat 的大小改写成恰好在 moov 前结束。
# 这样 demuxer 读得懂、时长与各轨参数都来自 moov（正确），而落盘只有 1 MiB 出头。
#
# 为什么不是「头尾直接首尾相接」：实测（ffmpeg 7.0.2）那样拼出来会报
# ``moov atom not found`` —— 头部里的 mdat 声明了整个 mdat 的长度，demuxer 跳完它就
# EOF 了，根本走不到后面接的 moov。改写 mdat 长度是让它「指到 moov」的唯一办法。
#
# 为什么不用「按真实偏移回填 + 稀疏文件」：那样 moov 的绝对位置天然正确，但文件逻辑
# 大小等于原文件。ext4 与 Docker 的 overlayfs 在实测里**不会真的挖洞**（8 GiB 的空洞
# 实打实占了 8 GiB），一次探测就能把磁盘吃穿。


def _mp4_boxes(buf: bytes, base: int = 0, allow_overrun: bool = False):
    """按 MP4 box 结构逐层遍历顶层盒子，产出 (类型, 绝对起点, 绝对终点, 头长度, 声明大小)。

    ``allow_overrun=True`` 时不因「盒子声明的长度超出缓冲区」而停下。定位 mdat 头必须用
    这个模式：它的声明长度就是**整个媒体文件的大小**，远大于我们手上的 1 MiB 窗口，
    严格模式下永远走不到它。
    """
    off = 0
    while off + 8 <= len(buf):
        size = int.from_bytes(buf[off:off + 4], "big")
        typ = bytes(buf[off + 4:off + 8])
        hdr = 8
        if size == 1:                      # 64 位长度：大文件常见
            if off + 16 > len(buf):
                return
            size = int.from_bytes(buf[off + 8:off + 16], "big")
            hdr = 16
        if size == 0:                      # 「一直到文件末尾」
            size = len(buf) - off
        if size < hdr:
            return                          # 非法长度，停止遍历
        if off + size > len(buf):
            if not allow_overrun:
                return                      # 被截断了，停止遍历
            # 越界盒（就是 mdat）：位置与头长度仍然可信，交给调用方去改写它
            yield (typ, base + off, base + off + size, hdr, size)
            return
        yield (typ, base + off, base + off + size, hdr, size)
        off += size


def _find_moov(buf: bytes, base: int = 0):
    """在字节里定位 moov 盒子，返回 (绝对起点, 绝对终点)；找不到返回 None。

    不能从 buf[0] 按盒结构往下解析：尾段的起点在 mdat 中间，第一个「盒子」就是乱码，
    解析会立刻停住。改成搜 ``moov`` 四字节码，再校验它前面那个长度字段站得住脚。
    """
    pos = 0
    while True:
        hit = buf.find(b"moov", pos)
        if hit < 0:
            return None
        start = hit - 4
        # 2026-10-19 自适应尾窗：moov 可能恰好顶在缓冲区开头（start=0），
        # 此时 buf[0:4] 就是合法的长度字段。旧的 >=4 会把这种顶格 moov 漏掉。
        if start >= 0:
            size = int.from_bytes(buf[start:start + 4], "big")
            hdr = 8
            if size == 1 and start + 16 <= len(buf):
                size = int.from_bytes(buf[start + 8:start + 16], "big")
                hdr = 16
            if size >= hdr and start + size <= len(buf):
                return (base + start, base + start + size)
        pos = hit + 4


def _mp4_probe_bytes(head: bytes, tail: bytes) -> Optional[bytes]:
    """「头部 + moov」拼出 ffprobe 能读的小文件；拼不出来返回 None（交给上层回退）。"""
    span = _find_moov(tail)
    if not span:
        return None
    moov = tail[span[0]:span[1]]
    body = bytearray(head + moov)
    moov_at = len(head)
    for typ, start, _end, hdr, _size in _mp4_boxes(bytes(body), allow_overrun=True):
        if typ != b"mdat":
            continue
        # 把 mdat 的声明长度改成「恰好停在 moov 前」，demuxer 跳过它就能读到 moov
        new_size = moov_at - start
        if new_size < 8:
            return None
        if hdr == 16 or new_size > 0xFFFFFFFF:
            struct.pack_into(">I", body, start, 1)
            struct.pack_into(">Q", body, start + 8, new_size)
        else:
            struct.pack_into(">I", body, start, new_size)
        return bytes(body)
    return None  # 没有顶层 mdat（分片 mp4 / 非标准封装），别硬拼


def _tail_moov_container(path: str, container: str = "") -> bool:
    """这个文件要不要走尾部再读一次（moov 可能落在文件尾的 MP4/MOV 系）。

    优先用调用方从库里带来的 ``container``（``MediaItem.container``，扫描时写死的），
    没给就从 URL 的后缀推——远程直链常带 ``?token=`` 之类的查询串，要先剥掉。
    """
    name = (container or "").strip().lower().lstrip(".")
    if not name:
        bare = path.split("?", 1)[0].split("#", 1)[0]
        name = posixpath.splitext(bare)[1].lstrip(".").lower()
    return name in _TAIL_MOOV_CONTAINERS


def _http_fetch_range(url: str, headers: Optional[dict], start: int, end: int) -> Optional[bytes]:
    """按 ``Range: bytes=start-end`` 取一段字节；失败返回 None（不抛）。

    云盘直链有几种不讲理的行为，这里都当正常情况处理：
    - 206（正常）与 200（**无视 Range 直接回整个文件**）都接受，后者按偏移自己切；
    - 4xx/5xx 抛出去（403 配额、404 文件没了要在上层翻译成 _error）。
    """
    import httpx

    req = {str(k): str(v) for k, v in (headers or {}).items()}
    req["Range"] = f"bytes={start}-{end}"
    with httpx.Client(timeout=PROBE_HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers=req)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}")
    body = resp.content or b""
    if resp.status_code == 206:
        return body
    # 200：服务端没认 Range，按我们请求的偏移自己切一刀（长度不够说明拿不全，弃用）
    return body[start:end + 1] if len(body) >= end + 1 else None


def _local_fetch_range(path: str, headers: Optional[dict], start: int, end: int) -> Optional[bytes]:
    """读本机文件的一段字节（FUSE 挂载也只做两次定长读，不让 ffprobe 乱 seek）。

    签名与 ``_http_fetch_range`` 对齐（headers 忽略），失败返回 None（不抛）。
    """
    try:
        want = int(end) - int(start) + 1
        if want <= 0:
            return None
        with open(path, "rb") as fh:
            fh.seek(int(start))
            data = fh.read(want)
        return data if len(data) == want else None
    except OSError:
        return None


def _headtail_sparse_temp(path: str, headers: Optional[dict], size: int) -> Optional[str]:
    """头尾探测：并行取「头 PROBE_HEADTAIL_HEAD_BYTES + 尾 PROBE_HEADTAIL_TAIL_BYTES」，
    写进同尺寸的稀疏临时文件（ftruncate(total) 后只在头尾两处落盘，中间全是空洞），
    返回临时文件路径（调用方负责删）。

    远端直链走 HTTP Range（比走挂载稳）；本机/FUSE 路径走两次定长本地读。
    两个请求是并行的：先全部 submit 再取结果。
    任何一步没成就返回 None，调用方回退到现行流程，行为不倒退。
    """
    total = int(size or 0)
    head_n = int(PROBE_HEADTAIL_HEAD_BYTES)
    tail_n = int(PROBE_HEADTAIL_TAIL_BYTES)
    if total <= 0 or head_n <= 0 or tail_n <= 0:
        return None
    if total <= head_n + tail_n:
        return None  # 小文件：两段窗口就盖住全文件，走现行流程更直接
    if any(str(k).lower() == "range" for k in (headers or {})):
        return None  # 调用方自己带了 Range（说明另有安排），别插手
    remote = path.startswith(("http://", "https://"))
    fetch = _http_fetch_range if remote else _local_fetch_range
    head_end = head_n - 1
    tail_start = total - tail_n
    with ThreadPoolExecutor(max_workers=2) as pool:
        head_future = pool.submit(fetch, path, headers, 0, head_end)
        tail_future = pool.submit(fetch, path, headers, tail_start, total - 1)
        try:
            head = head_future.result(timeout=PROBE_HTTP_TIMEOUT)
            tail = tail_future.result(timeout=PROBE_HTTP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — 抓字节失败只是「这次头尾没成」
            logger.debug("头尾探测抓取失败 %s: %s", path, exc)
            return None
    if not head or len(head) != head_n or not tail or len(tail) != tail_n:
        logger.debug("头尾探测缺段 %s：head=%s tail=%s",
                     path, len(head or b""), len(tail or b""))
        return None
    suffix = posixpath.splitext(path.split("?", 1)[0])[1][:8] or ".bin"
    fd, tmp = tempfile.mkstemp(prefix="probe-headtail-", suffix=suffix)
    try:
        os.ftruncate(fd, total)  # 同尺寸稀疏文件：中间全是空洞，不占空间
        os.pwrite(fd, head, 0)
        os.pwrite(fd, tail, tail_start)
    except OSError as exc:
        logger.debug("头尾探测写临时文件失败 %s: %s", path, exc)
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    try:
        os.close(fd)
    except OSError:
        pass
    return tmp


def _remote_returns_login_page(url: str, headers: Optional[dict]) -> bool:
    """远端 URL 预检：响应的 Content-Type 是不是 text/html。

    2026-10-19 探测塌方教训：``drive.usercontent.google.com/download`` 对部分
    文件即使带 SA Bearer 也返回 HTTP 200 的 Google 登录页 HTML——状态码检查
    全部通过，ffprobe 读 HTML 拿不到 format，白白烧掉 3 次尝试后判死。
    只读响应头（stream 方式，不下载 body）；预检失败（超时/异常）一律返回
    False（放行），不阻断正常探测。非 http(s) URL 直接返回 False。
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    try:
        import httpx
        req = {str(k): str(v) for k, v in (headers or {}).items()}
        req["Range"] = "bytes=0-0"
        with httpx.Client(timeout=PROBE_HTTP_TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url, headers=req) as resp:
                if not (200 <= resp.status_code < 300):
                    return False
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                return ctype == "text/html"
    except Exception:  # noqa: BLE001 — 预检失败放行
        return False


def _mdat_end_from_head(head: bytes) -> Optional[int]:
    """从头部字节里算第一个顶层 mdat 的绝对终点。

    allow_overrun 模式下 mdat 的声明长度（整个媒体数据大小）是可信的——
    这正是「moov 落在文件尾」布局里定位 moov 起点的锚。找不到返回 None。
    """
    for typ, _start, end, _hdr, _size in _mp4_boxes(head, base=0, allow_overrun=True):
        if typ == b"mdat":
            return end
    return None


def _adaptive_moov_tail(path: str, headers: Optional[dict], head: bytes,
                        total: int, tail_start: int) -> Optional[bytes]:
    """自适应尾窗：moov 比固定尾窗大时的补救（最多多 1 次 Range 请求）。

    背景（2026-10-19 大 moov 教训）：moov > PROBE_REMOTE_TAIL_BYTES 时，
    它的四字节码根本不在尾窗口里，``_find_moov`` 注定找不到，文件会被
    ``moov atom not found`` 判死。此时用头部 mdat 的声明长度算出 moov
    起点（= 第一个 mdat 的终点），按精确范围补一次。

    触发条件（缺一不可）：
    - 头部里能解析出 mdat，且它的终点在尾窗口起点之前（moov 可证明地
      完全落在固定窗口之外——正常文件走不到这里）；
    - moov 跨度（total - mdat_end）在 PROBE_MOOV_MAX_BYTES 上限内。
    否则返回 None，保持现行失败语义。
    """
    mdat_end = _mdat_end_from_head(head)
    if mdat_end is None:
        return None
    if mdat_end >= tail_start:
        # moov 起点在尾窗口内：_find_moov 已经找过，没什么可补的
        return None
    span = int(total or 0) - mdat_end
    if span <= 0:
        return None
    if span > PROBE_MOOV_MAX_BYTES:
        # 2026-10-19 r56：日志只打去查询串的 path——115 直链自带签名查询参数，
        # log_redact 的 URL 脱敏词表不覆盖其参数名，完整 URL 进日志会泄签名。
        # （与 _tail_moov_container 剥查询串的手法一致；实际请求仍用完整 path。）
        safe_path = path.split("?", 1)[0].split("#", 1)[0]
        logger.info("双 Range 自适应尾窗跳过 %s：moov 跨度 %d 超过上限 %d",
                    safe_path, span, PROBE_MOOV_MAX_BYTES)
        return None
    try:
        extra = _http_fetch_range(path, headers, mdat_end, int(total or 0) - 1)
    except Exception as exc:  # noqa: BLE001 — 补取失败只是「这次没成」
        logger.debug("双 Range 自适应补取失败 %s: %s",
                     path.split("?", 1)[0].split("#", 1)[0], exc)
        return None
    if not extra:
        return None
    logger.debug("双 Range 自适应补取 %s：mdat_end=%d span=%d",
                 path.split("?", 1)[0].split("#", 1)[0], mdat_end, span)
    return extra


def _dual_range_temp_file(path: str, headers: Optional[dict], size: int) -> Optional[str]:
    """并行取「前 1 MiB + 后 PROBE_REMOTE_TAIL_BYTES」，拼成「头部 + moov」的临时文件。

    返回临时文件路径（调用方负责删），任何一步没成就返回 None。
    **两个请求是并行的**：先全部 submit 到 2 线程的池子，再逐个取结果——
    顺序等待等于串行，等于白优化。
    """
    total = int(size or 0)
    if total <= PROBE_REMOTE_RANGE_BYTES:
        return None  # 文件整个都在头部窗口里，第二跳没意义
    head_end = min(total - 1, PROBE_REMOTE_RANGE_BYTES - 1)
    tail_start = max(0, total - PROBE_REMOTE_TAIL_BYTES)
    if tail_start <= head_end + 1:
        return None  # 两段已经贴上（乃至重叠），拼出来就是整个文件，不如直接慢速回退
    with ThreadPoolExecutor(max_workers=2) as pool:
        # 两个请求都 submit 完再取结果：此刻它们已经在路上，并行。
        # 谁先返回谁先完成无所谓——顺序等待就是串行，等于白优化。
        head_future = pool.submit(_http_fetch_range, path, headers, 0, head_end)
        tail_future = pool.submit(_http_fetch_range, path, headers, tail_start, total - 1)
        try:
            head = head_future.result()
            tail = tail_future.result()
        except Exception as exc:  # noqa: BLE001 — 抓字节失败只是「这次双 Range 没成」
            logger.debug("双 Range 抓取失败 %s: %s", path, exc)
            return None
    if not head or not tail:
        logger.debug("双 Range 缺段 %s：head=%s tail=%s",
                     path, len(head or b""), len(tail or b""))
        return None
    payload = _mp4_probe_bytes(head, tail)
    if not payload and tail:
        # 尾窗口里没有完整 moov：可能是 moov 比窗口大（自适应补一次），
        # 也可能这文件本来就没有可拼的 moov（补取函数会自行放弃）。
        tail2 = _adaptive_moov_tail(path, headers, head, total, tail_start)
        if tail2:
            payload = _mp4_probe_bytes(head, tail2)
    if not payload:
        logger.debug("双 Range 拼不出可探的 mp4 %s（尾窗口里没有完整的 moov？）", path)
        return None
    suffix = posixpath.splitext(path.split("?", 1)[0])[1][:8] or ".bin"
    fd, tmp = tempfile.mkstemp(prefix="probe-tail-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
    except OSError as exc:
        logger.debug("双 Range 写临时文件失败 %s: %s", path, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return tmp


def _rescale_synthetic_format(data: dict, real_size: int, synth_size: int) -> None:
    """把 ffprobe 从「合成文件」算出来的容器级数值换算回原文件（就地改）。

    时长与各轨参数（分辨率 / 编码 / 帧率 / 声道）都写在 moov 里，不受拼装影响；
    但 ``format.bit_rate`` 与 ``format.size`` 是 demuxer 用「文件大小 ÷ 时长」现算的，
    而我们喂进去的只有 1 MiB 出头。实测差到千分之几（224 MiB 的文件被算成 19 kb/s）。
    漏掉这一步会把一堆离谱的码率写进库、显示在客户端的「媒体信息」页上。
    """
    fmt = data.get("format") or {}
    if not isinstance(fmt, dict):
        return
    if real_size > 0:
        fmt["size"] = str(real_size)
    if real_size <= 0 or synth_size <= 0 or synth_size >= real_size:
        return
    try:
        bit_rate = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        return
    if bit_rate > 0:
        fmt["bit_rate"] = int(bit_rate * real_size / synth_size)


def _dual_range_probe(path: str, headers: Optional[dict], size: int,
                      container: str = "") -> Optional[dict]:
    """第二跳：头尾并行抓 → 拼出「头部 + moov」→ ffprobe。

    拿不到 format 就返回 None，让上层回退到旧的自由 seek 路径（行为不倒退）。
    """
    if not _tail_moov_container(path, container):
        return None
    # 调用方自己带了 Range（说明它对这条直链另有安排），别插手
    if any(str(k).lower() == "range" for k in (headers or {})):
        return None
    tmp = _dual_range_temp_file(path, headers, size)
    if not tmp:
        return None
    try:
        data = _ffprobe(path, headers, size=size, local_path=tmp)
        if data and data.get("format"):
            try:
                _rescale_synthetic_format(data, int(size or 0), os.path.getsize(tmp))
            except OSError:
                pass
        return data
    finally:
        try:
            os.unlink(tmp)
        except OSError:  # noqa: BLE001 — 临时文件删不掉也不该让探测崩掉
            logger.debug("双 Range 临时文件删除失败 %s", tmp)


# 逐流字段白名单（与 probe_worker._STREAM_COLS 同口径）
_PROBE_STREAM_COLS = frozenset({
    "stream_index", "stream_type", "codec", "language", "display_title", "title",
    "channels", "bit_rate", "frame_rate", "video_range", "profile", "level",
    "pixel_format", "aspect_ratio", "bit_depth", "sample_rate",
    "channel_layout", "sample_format",
})


def _detect_video_range(stream: dict, tags: dict) -> str:
    """从 ffprobe 字段判断动态范围（客户端「动态范围」那一行）"""
    dovi = (tags.get("DOVI") or tags.get("dovi") or "").lower()
    if "dv" in dovi or "dolby vision" in dovi:
        return "DolbyVision"
    transfer = (stream.get("color_transfer") or "").lower()
    primaries = (stream.get("color_primaries") or "").lower()
    space = (stream.get("color_space") or "").lower()
    if "smpte2084" in transfer or "smpte2084" in primaries or "smpte2084" in space:
        return "HDR10"
    if "arib-std-b67" in transfer:
        return "HLG"
    return "SDR"


def _bit_depth(stream: dict, tags: dict) -> int:
    """位深：优先 raw_sample，其次 bits_per_sample"""
    for value in (tags.get("bits_per_raw_sample"), stream.get("bits_per_raw_sample"),
                  tags.get("bits_per_sample"), stream.get("bits_per_sample")):
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 0


def probe_metadata(path: str, headers: Optional[dict] = None, size: int = 0,
                   container: str = "") -> dict:
    """返回 duration_ticks/bitrate/尺寸/编码/轨道信息

    ``size`` 是已知的文件大小（远程挂载从目录列表传入），ffprobe 读不到本地文件大小时用它兜底。
    ``container`` 是调用方从库里带来的容器后缀（``MediaItem.container``）；不给就从 URL 后缀推。

    ``_error`` 可能含 ``"login_page"``（2026-10-19）：远端返回 text/html 登录页，
    授权类临时故障——调用方（media_probe.probe_one / probe_worker.process_item）
    一律走 24h 长退避，永不判死、不消耗普通重试次数。
    """
    info: dict = {
        "duration_ticks": 0, "bitrate": 0, "width": 0, "height": 0,
        "video_codec": None, "audio_codec": None,
        "audio_languages": "", "subtitle_languages": "", "streams": [],
        "moov_position": None,
    }
    remote = path.startswith(("http://", "https://"))
    if remote and _remote_returns_login_page(path, headers):
        return {"_error": "login_page", "_http_code": 200,
                "_error_detail": "远端返回登录页（Content-Type=text/html，需要有效授权）"}
    # 第零跳（头尾探测，探测正案第 1 步）：先于下面的第一跳。两次读（头 + 尾）写进
    # 同尺寸稀疏临时文件，对本地文件跑 ffprobe -probesize 5M -analyzeduration 0。
    # 拿到 format 就直接用，后面两级回退不再跑；拿不到则走下面的现行流程。
    data = None
    _via_headtail = False
    if PROBE_HEADTAIL_ENABLED:
        _ht_tmp = _headtail_sparse_temp(path, headers, size)
        if _ht_tmp:
            try:
                _ht_data = _ffprobe(path, headers, size=size, local_path=_ht_tmp,
                                    probesize=PROBE_HEADTAIL_PROBESIZE,
                                    analyzeduration=PROBE_HEADTAIL_ANALYZEDURATION)
                if _ht_data and _ht_data.get("format"):
                    try:
                        # 合成大小取 ffprobe **自己报的**文件大小：稀疏临时文件与原文件
                        # 同尺寸时报的就是原大小（码率本就按真实大小算过，不该再放大）；
                        # 真是小尺寸的合成文件时才按 真实大小/合成大小 还原码率。
                        _fmt = _ht_data.get("format") or {}
                        try:
                            _synth_size = int(_fmt.get("size") or 0)
                        except (TypeError, ValueError):
                            _synth_size = 0
                        if _synth_size <= 0:
                            _synth_size = os.path.getsize(_ht_tmp)
                        _rescale_synthetic_format(_ht_data, int(size or 0), _synth_size)
                    except OSError:
                        pass
                    _via_headtail = True
                    data = _ht_data
            finally:
                try:
                    os.unlink(_ht_tmp)
                except OSError:  # noqa: BLE001 — 临时文件删不掉也不该让探测崩掉
                    logger.debug("头尾探测临时文件删除失败 %s", _ht_tmp)
    if data is None:
        data = _ffprobe(path, headers, size=size)
    # 有限 Range 只是「少读点」的快速路径（decd396）：它只保证**头部带时长**的封装
    # （Matroska 实测有效）。moov 在文件尾的 MP4/MOV 在前 1 MiB 里根本没有时长，
    # 而我们注入的 Range 又让 ffprobe 把 206 当成整个文件，读到窗口末尾就报
    # "File ended prematurely" → format 为空 → 白白记 degraded（生产 4.3 万条）。
    #
    # 拿不到 format、且不是明确的 HTTP 错误时，按「速度优先」分两级回退：
    #   第二跳（mp4/mov 专有）：并行取「头 1 MiB + 尾 2 MiB」，从尾段摘出 moov 拼成
    #     「头部 + moov」的小文件，时长与各轨参数都来自 moov（正确）。流量固定在
    #     两个窗口，与文件多大无关（8 GB 的片子也只读 3 MiB）。
    #   第三跳（兜底）：去掉 Range 让 ffprobe 自己按需 seek。这条会把整个文件拉下来，
    #     所以放在最后，只在双 Range 也失手时才走。
    # 只对「窗口确实截断了文件」的条目做，避免给本就完整的文件白跑一遍。
    _moov_via_dual_range = False
    if (remote and (size <= 0 or size > PROBE_REMOTE_RANGE_BYTES)
            and (not data or not data.get("format"))
            and not (data or {}).get("_http_code")):
        tail_first = _dual_range_probe(path, headers, size, container)
        if tail_first and tail_first.get("format"):
            _moov_via_dual_range = True
            data = tail_first
        elif size > 0 and 0 < PROBE_WHOLE_FILE_MAX_BYTES < size:
            # 第三跳会把整个文件拉下来：已知大文件直接跳过（见常量注释），
            # data 保持无 format，下面统一记 degraded。
            logger.info("远程探测跳过整文件兜底：size=%d 超过上限 %d",
                        size, PROBE_WHOLE_FILE_MAX_BYTES)
        else:
            seekable = _ffprobe(path, headers, size=size, ranged=False)
            if seekable and seekable.get("format"):
                data = seekable
    used_mediainfo = False
    if not data or not data.get("format"):
        # ffprobe 是**超时**还是**读完但没数据**，决定还要不要跑 MediaInfo：
        # 超时说明挂载本身没响应（生产上 /mnt/mp 是 fuse.rclone，mediainfo 常驻
        # D 状态），再花一个 PROBE_FILE_TIMEOUT 读同一份字节只是白等——
        # 单文件最坏耗时直接减半。读完没数据才值得换工具再试。
        ffprobe_timed_out = _last_ffprobe_timed_out()
        fallback = None
        if not ffprobe_timed_out:
            # 本机文件再尝试 MediaInfo；远程 URL 不重复发起一次随机读取，
            # 直接由调用方记 degraded（客户端仍可播放，信息不完整）。
            fallback = _mediainfo(path, headers, size=size)
        if fallback:
            data = fallback
            used_mediainfo = True
        else:
            try:
                info["size"] = os.path.getsize(path)
            except OSError:
                info["size"] = size
            if remote:
                # 明确解析出的远端 HTTP 错误照样随结果透出（403 → 熔断器；
                # 404/401 → 重试→failed），别让早返回把它吞成一句「信息不完整」。
                if (data or {}).get("_error"):
                    info["_error"] = data["_error"]
                    info["_http_code"] = data.get("_http_code")
                    info["_error_detail"] = data.get("_error_detail", "")
                info["_degraded"] = True
                if not info.get("_error_detail"):
                    info["_error_detail"] = "远程媒体可访问，但探测未取得完整时长（ffprobe/MediaInfo）"
            return info
    if used_mediainfo:
        info["_probe_backend"] = "mediainfo"
    elif _via_headtail:
        info["_probe_backend"] = "headtail"
    else:
        _container_lc = (container or "").lower()
        if _container_lc in ("mp4", "mov", "m4v"):
            info["moov_position"] = "back" if _moov_via_dual_range else "front"
    # 透出 ffprobe 的 HTTP 错误（供熔断器和日志使用）
    if data.get("_error"):
        info["_error"] = data["_error"]
        info["_http_code"] = data.get("_http_code")
        info["_error_detail"] = data.get("_error_detail", "")

    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or 0)
    info["duration_ticks"] = int(duration * 10_000_000)
    info["bitrate"] = int(fmt.get("bit_rate") or 0)
    try:
        info["size"] = os.path.getsize(path)
    except OSError:
        info["size"] = int(fmt.get("size") or 0) or size

    audio_langs, sub_langs, streams = [], [], []
    for idx, s in enumerate(data.get("streams", [])):
        stype = s.get("codec_type")
        tags = s.get("tags") or {}
        lang = tags.get("language", "")
        entry = {
            "stream_index": idx, "stream_type": stype, "codec": s.get("codec_name"),
            "language": lang, "channels": s.get("channels"),
            "bit_rate": int(s.get("bit_rate") or 0),
            "display_title": tags.get("title"),
            "title": tags.get("title"),
        }
        if stype == "video":
            entry["stream_type"] = "Video"
            info["width"] = s.get("width", 0)
            info["height"] = s.get("height", 0)
            info["video_codec"] = (s.get("codec_name") or "").upper()
            # 客户端「媒体信息」页逐行显示这些，缺了详情页只剩编码/码率两行
            fps = s.get("avg_frame_rate") or s.get("r_frame_rate") or ""
            if fps and fps not in ("0/0", "N/A"):
                entry["frame_rate"] = fps
            entry["video_range"] = _detect_video_range(s, tags)
            if s.get("profile"):
                entry["profile"] = str(s.get("profile"))
            if s.get("level") not in (None, -99, ""):
                entry["level"] = str(s.get("level"))
            if s.get("pix_fmt"):
                entry["pixel_format"] = s.get("pix_fmt")
            sar = s.get("sample_aspect_ratio") or ""
            if sar not in ("", "N/A", "0:1", "1:1"):
                entry["aspect_ratio"] = sar
            depth = _bit_depth(s, tags)
            if depth:
                entry["bit_depth"] = depth
        elif stype == "audio":
            entry["stream_type"] = "Audio"
            info["audio_codec"] = (s.get("codec_name") or "").upper()
            if s.get("sample_rate"):
                try:
                    entry["sample_rate"] = int(s.get("sample_rate"))
                except (TypeError, ValueError):
                    pass
            if s.get("sample_fmt"):
                entry["sample_format"] = s.get("sample_fmt")
            layout = s.get("channel_layout")
            if layout:
                entry["channel_layout"] = layout
            else:
                ch = int(s.get("channels") or 0)
                entry["channel_layout"] = {
                    1: "mono", 2: "stereo", 6: "5.1", 8: "7.1",
                }.get(ch, "")
            depth = _bit_depth(s, tags)
            if depth:
                entry["bit_depth"] = depth
            if lang:
                audio_langs.append(lang)
        elif stype == "subtitle":
            entry["stream_type"] = "Subtitle"
            if lang:
                sub_langs.append(lang)
        streams.append(entry)
    info["streams"] = streams
    info["audio_languages"] = ",".join(dict.fromkeys(audio_langs))
    info["subtitle_languages"] = ",".join(dict.fromkeys(sub_langs))
    return info


# ==================== 本机目录列表缓存 ====================
# 扫描一个目录下十几个文件时，旧实现每个文件都会 os.listdir 两次（找图 + 找字幕），
# 大目录（整季集数）下这就是几千次多余的目录读。缓存在每次扫描开始时清空。
_DIR_LIST_CACHE: dict = {}
_DIR_LIST_MAX = 20000
_DIR_LIST_LOCK = threading.Lock()


def clear_dir_cache() -> None:
    """开始一次扫描前调用：保证不拿上一次扫描的目录列表

    同时清掉**挂载层的共用缓存**（见 mounts.cached_listing）：那份缓存是扫描与播放
    共用的，扫描必须看到当下的目录——否则刚上传的文件会被播放刚踩过的缓存挡住，
    要等下一轮扫描才入库。
    """
    with _DIR_LIST_LOCK:
        _DIR_LIST_CACHE.clear()
    mount_lib.invalidate_list_cache()


def _list_dir_cached(dir_path: str) -> list:
    """本机目录列表（同一次扫描里同一目录只读一次；缓存有上限，不会无限长大）

    「查缓存 → 列目录 → 写缓存」必须整体在锁里完成：同一个目录下的多个文件会被分发到
    不同工作线程，分成两段的实现在线程同时要同一目录时会各自真列一次（CI 上表现为
    3 个目录列了 6 次）。列目录本身很快，串行化它换来的是「目录读次数只跟目录数有关」。
    """
    with _DIR_LIST_LOCK:
        hit = _DIR_LIST_CACHE.get(dir_path)
        if hit is not None:
            return hit
        try:
            entries = os.listdir(dir_path)
        except OSError:
            entries = []
        if len(_DIR_LIST_CACHE) >= _DIR_LIST_MAX:
            _DIR_LIST_CACHE.clear()
        _DIR_LIST_CACHE[dir_path] = entries
        return entries


def find_local_images_in(entries, dir_path: str, base_name: str) -> tuple[Optional[str], Optional[str]]:
    """在已经拿到的目录列表里查 poster/fanart

    扫描时同一目录下的文件很多，目录只需列一次（老实现每个文件列两次），因此把
    “取列表”和“在列表里找”拆开：找的部分是纯计算，可以复用同一份列表。
    """
    poster = fanart = None
    for f in entries:
        low = f.lower()
        stem, ext = os.path.splitext(low)
        if ext not in IMAGE_EXTS:
            continue
        if stem in {"poster", "cover", "folder"} or stem == base_name.lower():
            poster = poster or os.path.join(dir_path, f)
        if stem in {"fanart", "backdrop", "background"}:
            fanart = fanart or os.path.join(dir_path, f)
    return poster, fanart


def find_external_subtitles(file_path: str) -> list[tuple[str, str]]:
    """本机文件的外挂字幕：返回 [(lang, 绝对路径)]（目录列表走本次扫描的缓存）

    判定本身在 subtitles.py；这里保留包装是因为它要用**本次扫描的目录缓存**，
    而缓存属于扫描器——反过来让 subtitles 依赖 scanner 会形成循环导入。
    """
    d = os.path.dirname(file_path)
    return find_external_subtitles_in(_list_dir_cached(d), file_path)


@dataclass(frozen=True)
class LibrarySnapshot:
    """一次扫描固定使用的媒体库配置

    扫描可能跑很久，期间管理员可能改路径/策略。任务只认自己这份快照：

    - 运行中的任务不会被“改到一半”，不会出现“旧根目录扫一半、新根目录扫一半”；
    - 保存新配置后重新触发，用的就是新路径，**旧路径不会被继续扫描**。
    """

    library_id: int
    name: str
    collection_type: str
    paths: tuple[str, ...]
    scrape_policy: str
    # 绑定的存储挂载（storage_mounts.id）；扫描时与 paths 一起遍历
    mount_ids: tuple[int, ...] = ()
    # 本轮是否走增量秒跳：全局开关 SCAN_INCREMENTAL 与本库的开关都要开（v2.44.0）
    incremental_scan: bool = True
    # 这一轮强制全量（管理员手动点「全量扫描」）：无视所有指纹，处理一遍全部文件。
    # 快照字段而不是全局开关 —— 别的库、别的轮次都不能被这次手动操作带跑。
    force_full: bool = False
    # v2.52.0 定向扫描（Drive Changes API）：只扫描这些路径前缀下的文件，
    # 不做删除清理（看不到的目录不能当成"文件已删除"）。
    # 为空 = 正常全库扫描。
    limit_prefixes: tuple = ()
    targeted: bool = False

    @classmethod
    def of(cls, library) -> "LibrarySnapshot":
        paths = tuple(p.strip() for p in (library.paths or "").split(",") if p.strip())
        # 挂载互斥（scan_queue._MOUNT_OWNER）要看「实际用了哪些挂载」：
        # 除了 mount_ids 字段，paths 里的 mount://<id>/... 也算。
        # 否则 mount_ids 为空的库会绕过串行化，与同挂载的库并发扫描——
        # 2026-09-29 生产事故：动漫/国产剧/欧美剧同用 mount 3 并发扫描，
        # 触发 SQLAlchemy Session 并发崩溃（provisioning a new connection）。
        mount_ids = set(mount_lib.parse_mount_ids(library))
        for p in paths:
            parsed = mount_lib.parse_mount_path(p)
            if parsed is not None:
                mount_ids.add(parsed[0])
        return cls(
            library_id=library.id,
            name=library.name,
            collection_type=library.collection_type or "movies",
            paths=paths,
            scrape_policy=normalize_scrape_policy(getattr(library, "scrape_policy", None)),
            mount_ids=tuple(sorted(mount_ids)),
            # 老库没有这两列时 getattr 默认 True = 保持升级前的行为（本来就有增量）
            incremental_scan=getattr(library, "incremental_scan", True) is not False,
        )


@dataclass
class ScanFile:
    """扫描器看到的一个待处理媒体文件

    ``stored_path`` 是入库的 ``file_path``：本机可读的来源（媒体库路径 / local 挂载 /
    strm 挂载）存真实文件路径，远程挂载（115 / WebDAV / AList）存 ``mount://<id>/<rel>``。
    播放/探测输入懒解析——没装 ffprobe 或不需要重探时不会白白发 API 请求。
    """

    stored_path: str
    name: str
    local_dir: Optional[str]      # 本机目录（找本地图片 / 外挂字幕）；远程挂载为 None
    dir_rel: str = "/"            # 远程挂载里的目录（相对挂载根）
    size: int = 0
    container: str = ""           # 已知容器（strm 取直链里的真实容器）
    mount_id: Optional[int] = None
    rel: str = ""
    # Drive file_id（改名/移动不变）：扫描器用它识别改名
    file_id: str = ""
    #: 新表 library_sources 行 id（清单第 8 项身份锚：入库写 item.source_id 用）。
    #: 旧列回落扫描没有行 id，恒为 None；写盘点只在有行 id 时写锚，绝不清除已有锚。
    source_row_id: Optional[int] = None
    mtime_ns: Optional[int] = None  # 本机文件=st_mtime_ns；远程挂载无mtime，记None
    #: 远端挂载的最后修改时间（Unix 秒；拿不到记 0.0）——rclone ModTime 解析值，
    #: 混入文件指纹/目录指纹用（完美审查 C-P2-3：同尺寸内容替换的检出依据）。
    remote_mod_ts: float = 0.0
    provider: Any = None
    local_target: Any = None      # 本机可读来源的播放目标（strm 的直链在这里）
    _target: Any = None

    def play_target(self):
        if self._target is None:
            # 远程挂载走 resolve_final：.strm 条目先取文件内容再当直链播
            # （否则客户端会收到一个文本文件）
            self._target = (self.local_target if self.local_target is not None
                            else self.provider.resolve_final(self.rel))
        return self._target

    def probe_input(self) -> tuple[str, dict]:
        target = self.play_target()
        return target.value, target.headers


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _drive_file_id_from_url(url: str) -> str:
    """从 .strm 直链解析 Google Drive 文件 ID；拿不到/非法返回 ""（不抛异常）。

    v2.55.x A'：只认 Google Drive 域名（netloc 以 drive.google.com /
    docs.google.com 结尾），支持 /uc?id=<ID> 查询参数式和 /file/d/<ID>/
    路径式；ID 必须匹配 [A-Za-z0-9_-]{10,64}。非 Drive 直链/异常一律返回 ""，
    保持现状行为（写库循环的 ``if _fid:`` 会跳过，不清已有值）。
    """
    try:
        if not url:
            return ""
        parsed = urlparse(url)
        netloc = (parsed.netloc or "").lower()
        if not (netloc.endswith("drive.google.com")
                or netloc.endswith("docs.google.com")):
            return ""
        candidate = ""
        query = parse_qs(parsed.query)
        ids = query.get("id")
        if ids:
            candidate = ids[0]
        else:
            m = re.search(r"/file/d/([^/]+)", parsed.path or "")
            if m:
                candidate = m.group(1)
        if candidate and re.match(r"^[A-Za-z0-9_-]{10,64}$", candidate):
            return candidate
        return ""
    except Exception:  # noqa: BLE001 - 解析失败 = 当没有 ID，不阻断扫描
        return ""


def _local_dir_files(root: str, failed_roots: list,
                     source_row_id: Optional[int] = None) -> Iterator[ScanFile]:
    """本机目录：与历史行为一致，直接 os.walk 真实文件"""
    for dirpath, _dirnames, filenames in os.walk(
        root,
        # 失败要能归到具体的子目录（e.filename）：_source_entry 靠 "{label}: " 前缀
        # 把失败原因归到来源上，所以条目必须以来源标签开头，子目录写在后面
        onerror=lambda e: failed_roots.append(
            f"{root}: 子目录 {getattr(e, 'filename', None) or '?'} 列举失败: "
            f"{getattr(e, 'strerror', '') or e}"
        ),
    ):
        # 原盘结构目录（BDMV/STREAM、CERTIFICATE…）整棵剪掉：里面的 .m2ts 是码流片段，
        # 当成电影会产出一堆名为「00000」这样、既刮不出元数据也没有封面的条目
        _dirnames[:] = [d for d in _dirnames if not disc_filter.is_disc_subtree_dir(d)]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                target = mount_lib.local_play_target(full_path)
            except mount_lib.MountError as exc:
                logger.warning("跳过无法解析的媒体：%s（%s）", full_path, exc)
                continue
            container = ext.lstrip(".")
            if container == "strm":
                container = mount_lib.strm_container(target.value)
            try:
                _mtime_ns = os.stat(full_path).st_mtime_ns
            except OSError:
                _mtime_ns = None
            yield ScanFile(
                stored_path=full_path, name=fname, local_dir=dirpath,
                size=_safe_size(full_path), container=container, local_target=target,
                mtime_ns=_mtime_ns,
                # v2.55.x A'：纸条直链里的 Drive ID 原地落库（target 已在内存，无新增 IO）
                file_id=_drive_file_id_from_url(target.value),
                source_row_id=source_row_id,
            )


def _remote_strm_container(provider, rel: str) -> str:
    """远程挂载里的 .strm：读文件内容取直链，从直链推断真实容器

    推断不出来（或直链临时失效）不阻断入库，先按 strm 记下——播放时会重新解析。
    """
    try:
        target = provider.resolve_final(rel)
    except mount_lib.MountError as exc:
        logger.warning("STRM 直链解析失败：%s（%s）", rel, exc)
        return "strm"
    return mount_lib.strm_container(target.value)


def _mount_files(src, provider, failed_roots: list) -> Iterator[ScanFile]:
    """一个挂载里的媒体文件

    本机可读的挂载（local / strm）入库真实路径；远程挂载（115 / WebDAV / AList）
    入库 ``mount://<id>/<rel>``，播放时再解析成直链。
    """
    root = provider.local_root
    mount_id = src.mount.id
    # src.subpath：库配置为 mount://<id>/<子目录> 时只扫描该子目录（默认为 "/" 即整个挂载）
    subpath = getattr(src, "subpath", "/") or "/"
    for mount_file in provider.walk_media(root=subpath):
        rel = mount_file.rel
        dir_rel = os.path.dirname(rel) or "/"
        if root is not None:
            full_path = os.path.join(root, rel.lstrip("/"))
            try:
                target = mount_lib.local_play_target(full_path)
            except mount_lib.MountError as exc:
                logger.warning("跳过无法解析的媒体：%s（%s）", full_path, exc)
                continue
            container = os.path.splitext(mount_file.name)[1].lstrip(".").lower()
            if container == "strm":
                container = mount_lib.strm_container(target.value)
            yield ScanFile(
                stored_path=full_path, name=mount_file.name,
                local_dir=os.path.dirname(full_path), dir_rel=dir_rel,
                size=mount_file.size, container=container,
                mount_id=mount_id, rel=rel, provider=provider, local_target=target,
                # v2.55.x A'：本机可读挂载的 .strm，挂载层没给 ID 时从直链解析兜底
                file_id=getattr(mount_file, "file_id", "") or _drive_file_id_from_url(target.value),
                source_row_id=getattr(src, "source_row_id", None),
            )
            continue
        container = os.path.splitext(mount_file.name)[1].lstrip(".").lower()
        if container == "strm":
            container = _remote_strm_container(provider, rel)
        yield ScanFile(
            stored_path=mount_lib.mount_path(mount_id, rel), name=mount_file.name,
            local_dir=None, dir_rel=dir_rel, size=mount_file.size,
            container=container, mount_id=mount_id, rel=rel, provider=provider,
            file_id=getattr(mount_file, "file_id", "") or "",
            source_row_id=getattr(src, "source_row_id", None),
            # P2（完美审查 C-P2-3）：远端 ModTime 进指纹，同尺寸替换可检出。
            remote_mod_ts=getattr(mount_file, "mod_ts", 0.0) or 0.0,
        )



def _prefix_filter(files, prefixes: tuple) -> "Iterator":
    """v2.52.0: 定向扫描——只产出路径前缀命中的文件。"""
    if not prefixes:
        yield from files
        return
    for sf in files:
        sp = getattr(sf, "stored_path", "") or ""
        for px in prefixes:
            if sp == px or sp.startswith(px.rstrip("/") + "/"):
                yield sf
                break


def _source_path_prefix(src) -> str:
    """该来源的入库路径前缀（用于判断库里是否已有该来源的条目）。

    - 本机来源：绝对目录，如 ``/mnt/mp/剧集/``；
    - 本地挂载：挂载根 + 子目录，如 ``/mnt/paul/剧集/``；
    - 远程挂载：``mount://<id>/<子目录>/``。
    库里条目的 ``file_path`` 都以此前缀开头。
    """
    if src.kind == "local":
        return (src.path or "").rstrip("/") + "/"
    mount_id = getattr(src.mount, "id", None)
    subpath = (getattr(src, "subpath", "/") or "/").strip("/")
    provider = getattr(src, "provider", None)
    local_root = getattr(provider, "local_root", None) if provider else None
    if local_root:
        base = os.path.join(local_root, subpath) if subpath else local_root
        return base.rstrip("/") + "/"
    if mount_id is None:
        return ""
    tail = (subpath + "/") if subpath else ""
    return f"{mount_lib.MOUNT_PATH_PREFIX}{mount_id}/{tail}"


def _count_visible_under(db: Session, library_id: int, prefix: str) -> int:
    """库里该路径前缀下还有多少可见条目（LIKE 转义，避免路径里的 % _ 误匹配）"""
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return int(db.execute(
        text(
            "SELECT COUNT(*) FROM emby_items "
            "WHERE library_id = :lib AND deleted_at IS NULL "
            "AND file_path LIKE :pfx ESCAPE '\\'"
        ),
        {"lib": library_id, "pfx": esc + "%"},
    ).scalar() or 0)


def _watch_empty_source(label: str, src, files: Iterator,
                        db: Session, library_id: int,
                        failed_roots: list, check_empty: bool = True) -> Iterator:
    """包一层来源文件流：来源"零文件产出"时判定是不可用还是真空。

    挂载抖动/目录被临时收回时，一个来源可能一反常态地列出 0 个文件——这时不能
    当成"该来源文件全删了"，否则清理阶段会把该来源的条目全下架。
    判定口径：本轮 0 产出 **且** 库里该来源路径下还有可见条目 → 视为不可用，
    记进 ``failed_roots``（扫描器据此跳过清理阶段，与来源报错同一条保护链）。
    真空的来源（库里该来源下本来就没条目）不受影响，照常走清理。

    注意：这个包装器的尾部判定只在**正常遍历完**时执行。内层抛错（走
    ``_guarded`` 记进 failed_roots）或调用方提前放弃时，判定都不会跑——
    前者已有错误记录，后者说明扫描本身没跑完，清理本来也不会正常执行。

    ``check_empty=False`` 时只透传不判定：定向扫描（limit_prefixes）下来源
    0 产出是正常的，不能误判为不可用。
    """
    seen = 0
    for f in files:
        seen += 1
        yield f
    if seen > 0 or not check_empty:
        return
    prefix = _source_path_prefix(src)
    if not prefix:
        return
    try:
        n = _count_visible_under(db, library_id, prefix)
    except Exception as exc:  # noqa: BLE001 — 计数失败不该影响扫描本身
        logger.warning("来源 %s 空产出判定计数失败: %s", label, exc)
        return
    if n > 0:
        msg = (f"{label}: 本轮列出 0 个文件，但库里该来源下还有 {n} 条记录——"
               f"视为来源不可用，本轮跳过清理")
        logger.warning("媒体库来源异常：%s", msg)
        failed_roots.append(msg)


def iter_scan_sources(snap: "LibrarySnapshot", library, db: Session,
                      failed_roots: list,
                      report: Optional[list] = None) -> Iterator[tuple]:
    """遍历一个媒体库的全部扫描来源（本机路径 + 挂载），产出 ``(来源标签, 文件迭代器)``

    来源不可用（目录不存在 / 挂载停用 / 账号失效 / 网络不通）时**不会**抛出去中断整个扫描，
    而是记进 ``failed_roots``：扫描器据此跳过清理阶段，避免把「读不到」当成「文件已删除」。
    传了 ``report`` 时，不可用的来源也记一条 ``kind="unavailable"`` 的明细进去——
    「这轮一共几个来源、哪个一条都没扫到」在后台要能一眼看见（见 scan_result_payload）。
    明细在生成器收尾时补写：调用方中途退出也拿得到。
    """
    # 清单第 2 项：扫描来源解析改读新表（无行时回落旧列并记警告，见 mount_sources）。
    sources, failed = mount_sources.read_library_sources(db, library)
    unavailable = []
    for item in failed:
        logger.warning("媒体库「%s」来源不可用：%s（%s）", snap.name, item["label"], item["reason"])
        failed_roots.append(f"{item['label']}: {item['reason']}")
        unavailable.append({
            "label": item["label"], "kind": "unavailable", "error": item["reason"],
            # 形状与可用来源保持一致（计数器都在，只是全 0）：消费方不用为「读不到的来源」分支
            **{key: 0 for key in SCAN_SOURCE_COUNTERS},
        })

    try:
        prefixes = getattr(snap, "limit_prefixes", ()) or ()
        for src in sources:
            # 定向扫描只扫部分目录：来源 0 产出是正常的，不做空来源判定
            check_empty = not prefixes
            if src.kind == "local":
                yield src.label, _watch_empty_source(
                    src.label, src,
                    _prefix_filter(_local_dir_files(
                        src.path, failed_roots,
                        getattr(src, "source_row_id", None)), prefixes),
                    db, library.id, failed_roots, check_empty)
                continue

            def _guarded(src=src, check_empty=check_empty):
                try:
                    yield from _watch_empty_source(
                        src.label, src,
                        _prefix_filter(_mount_files(src, src.provider, failed_roots),
                                           prefixes),
                        db, library.id, failed_roots, check_empty)
                except mount_lib.MountError as exc:
                    logger.warning("媒体库「%s」的挂载「%s」不可用：%s", snap.name, src.label, exc)
                    failed_roots.append(f"{src.label}: {exc}")
                except Exception as exc:  # noqa: BLE001 — 一个挂载坏掉不该拖垮整次扫描
                    logger.warning("媒体库「%s」的挂载「%s」异常：%s", snap.name, src.label, exc)
                    failed_roots.append(f"{src.label}: {exc}")

            yield src.label, _guarded()
    finally:
        if report is not None:
            report.extend(unavailable)


# 同一媒体库同时只允许一个扫描任务（进程内互斥；EM/EA 都是单进程部署）
_ACTIVE_SCANS: dict[int, datetime] = {}  # 库 id → 开始时间（本进程内）
_ACTIVE_SCANS_LOCK = threading.Lock()


class ScanInProgress(RuntimeError):
    """同一媒体库已有扫描任务在运行"""


def is_scan_active(library_id: int) -> bool:  # noqa: D401
    """是否有扫描任务正在跑（比数据库里的 is_scanning 标志可靠：进程崩溃不会卡死）"""
    with _ACTIVE_SCANS_LOCK:
        return library_id in _ACTIVE_SCANS


def _acquire_scan(library_id: int) -> None:
    with _ACTIVE_SCANS_LOCK:
        if library_id in _ACTIVE_SCANS:
            raise ScanInProgress(f"媒体库 {library_id} 正在扫描中")
        _ACTIVE_SCANS[library_id] = datetime.now()


def _release_scan(library_id: int) -> None:
    with _ACTIVE_SCANS_LOCK:
        _ACTIVE_SCANS.pop(library_id, None)


def count_virtual_items(db: Session, library) -> int:
    """虚拟媒体库的条目数

    虚拟媒体库没有自己的文件与库归属，它是跨库的“发行平台视图”（Netflix / Disney+ …），
    因此计数按 `MediaItem.platforms` 里的平台标签聚合。
    """
    platform = (getattr(library, "platform", "") or "").strip()
    if not platform:
        return 0
    return (
        db.query(emby_models.MediaItem)
        .filter(
            emby_models.MediaItem.is_hidden == False,  # noqa: E712
            emby_models.MediaItem.item_type.in_(["movie", "series"]),
            emby_models.MediaItem.platforms.ilike(f"%{platform}%"),
        )
        .count()
    )


@dataclass
class _Pending:
    """一批里待处理的一个文件：解析结果 + 预取到的 IO 结果"""

    scan_file: Any
    guid: str
    parsed: dict
    item_type: str
    series_guid: Optional[str] = None
    season_guid: Optional[str] = None
    # 从剧集目录推导的稳定剧名；不能拿 episode 文件名当 series 名，
    # 否则首页会出现「某剧 第 1 集」这种伪剧集卡片。
    series_name: Optional[str] = None
    item: Any = None
    # 改名/移动标记（v2.52.0）：guid 没命中但 drive_file_id 命中的 = 同一文件换了路径。
    # 写库循环里更新 file_path + guid（保持 guid=path 派生的不变式），不删记录、
    # 不丢元数据（TMDB 绑定、刮削结果、播放进度全保留）。
    renamed: bool = False
    claimed_id: Optional[int] = None  # 批次开头占位成功拿到的 id（None=没抢到）
    claimed_item: Any = None  # 占位成功的行（强引用，防 identity map 弱引用被 GC）
    # 多集文件（如 S06E27-E28）：本 pending 是该文件的第二集行。
    # master_pending 指向同一文件的第一集 pending：探测/海报/字幕/NFO 的
    # IO Future 复用主行的，同一文件只跑一次 IO（证据见 tests/test_multi_episode_file.py）。
    multi_episode: bool = False
    master_pending: Any = None
    series: Any = None
    season: Any = None
    probe: Any = None   # Future[dict] | None
    probe_deferred: bool = False  # 两阶段 background 模式：本次不探测，写库时标 pending
    probe_needed: bool = False  # 本次是否需要探测（提交点按 needs_probe 语义算好）
    side: Any = None    # Future[(poster, fanart, subtitles)] | None
    tmdb: Any = None    # Future[(hit, details)] | None
    nfo: Any = None     # Future[(nfo_data, series_nfo_data, season_nfo_data)] | None
    series_tmdb: Any = None  # Future[(hit, details)] | None（剧集级 NFO 只补图用）
    # 上面三个是「排队中的 IO」，下面四个是**已经取回**的纯值。
    # 取回动作统一发生在批次开头（那时还没有任何写事务），写库循环里只读纯值——
    # 一旦在写事务里等网络，SQLite 的写锁就被按在网络 RTT 后面（见 docs/performance.md）。
    probe_data: Optional[dict] = None
    side_data: Any = None           # (poster, fanart, subtitles) | None
    tmdb_hit: Any = None
    tmdb_details: Any = None
    nfo_data: Any = None            # 本文件/电影的 NFO 解析结果
    series_nfo_data: Any = None     # tvshow.nfo 解析结果（episode 用）
    season_nfo_data: Any = None     # season.nfo 解析结果（episode 用）
    skipped: bool = False  # 增量扫描：目录没变且库里已是最新 → 本次不做任何写库工作
    fast_skipped: bool = False  # 分层 L1 秒跳：文件指纹命中且已补全 → 连目录指纹都不算
    file_fingerprint: Optional[str] = None  # 本批算出的文件指纹，写库时落库
    layered: bool = False  # 分层 L1：本文件只做极简入库，慢 IO 留给后台
    layered_local_fast: bool = False  # 分层 L1 本机文件：side/NFO 照常做（本地 IO 快）


@dataclass
class _ScanContext:
    """一次扫描里跨批次复用的状态（内存里只留 guid 集合与目录列表缓存）"""

    snap: LibrarySnapshot
    lib_id: int
    stats: dict
    seen_guids: set = field(default_factory=set)
    dir_cache: dict = field(default_factory=dict)          # 本机目录列表
    mount_dir_cache: dict = field(default_factory=dict)    # 远程挂载目录列表
    mount_lock: threading.Lock = field(default_factory=threading.Lock)
    mount_dir_locks: dict = field(default_factory=dict)     # 每个远程目录一把单飞锁
    # 剧集层级：guid → 条目对象（不是 id），一次扫描内复用
    # （同一部剧的多集只查一次、只建一次；存对象是因为写库循环后段还要用它们做
    #  「集 → 季 → 剧」的海报回退；同一 Session 的身份映射保证对象不会有两个实例）
    series_items: dict = field(default_factory=dict)
    season_items: dict = field(default_factory=dict)
    # B2（缺集普查 2026-10-19）：(series.id, season_no) → 季行。季的身份跟
    # 最终落定的 series 走（目录推导 guid 在重扫/改名后会变），建集时同事务
    # 内确保季行存在就靠这把键，幂等且不受目录 guid 漂移影响。
    season_items_by_series: dict = field(default_factory=dict)
    # B2 季行随批预取（2026-10-20）：本轮扫描中新建的 series id。
    # _ensure_season_row 的 (series.id, season_no) 回退 SELECT 只对"扫描前已
    # 存在"的 series 有意义——新建 series 的季行不可能已在库里，跳过回退直接
    # 插入，守住"按批加载"预算护栏（逐键 SELECT 曾把预算从 12 推到 14）。
    new_series_ids: set = field(default_factory=set)
    # 增量扫描：目录 → 本次指纹（None = 拿不到，这个目录不走捷径）
    dir_fingerprints: dict = field(default_factory=dict)
    # 本批处理过的目录（目录 → 当前指纹），批次提交时写回
    dirty_dirs: dict = field(default_factory=dict)
    # 到期调度（批二 #12）：目录 → (next_due_at, 本轮判定)，随批次提交写回；
    # 与 dirty_dirs 分开是因为「到期重验过但指纹没变」的目录不在 dirty 里，
    # 却同样要推进 next_due_at
    dirty_dir_schedule: dict = field(default_factory=dict)
    # 到期调度：目录 → 本轮判定（first/changed/due/not_due/None），一轮只判一次
    # （同一目录的文件可能跨批次，判定与统计计数都以第一次为准）
    dir_decisions: dict = field(default_factory=dict)
    # 按源快照层（任务清单第 11 项）：目录 → 所属源行 id（解析不到的不在内，
    # 走旧 ScanDirState 路径）；目录 → (指纹, next_due_at, 项数)；目录 → 本轮
    # 清单项数；目录 → (源 id, 指纹, 项数, 判定, 到期时刻)，随批次提交写回
    snapshot_resolved: dict = field(default_factory=dict)
    snapshot_states: dict = field(default_factory=dict)
    snapshot_counts: dict = field(default_factory=dict)
    dirty_snapshots: dict = field(default_factory=dict)
    # 本轮扫描已取过的源行 [(id, mount_id, sub_path)]，None = 还没取过
    snapshot_source_rows: Optional[list] = None
    # 列举失败的远程目录（读不到就别拿空列表当“目录没变”）
    dir_list_failed: set = field(default_factory=set)
    # NFO：已解析的 NFO 缓存（含阴性缓存），key 见 _read_nfo_cached
    nfo_cache: dict = field(default_factory=dict)
    # 剧集级 NFO 补图：series_guid → TMDB 详情（本轮已取过的不重复取）
    nfo_series_details: dict = field(default_factory=dict)
    # 本轮已处理过补图的 series_guid（去重，避免同剧多集/多批次重复调详情接口）
    nfo_series_imaged: set = field(default_factory=set)
    # episode 隐式 series 的 TMDB 搜索结果：series_guid → (hit, details)
    series_tmdb_results: dict = field(default_factory=dict)
    # 本轮已为隐式 series 提交过 TMDB 搜索的 series_guid（去重，避免同剧多集/多批次重复搜）
    series_tmdb_searched: set = field(default_factory=set)
    ext_subtitles: dict = field(default_factory=dict)
    # 本批条目的外挂字幕：item_id → {external_path}（秒跳时比对用，一批只查一次 DB，
    # 不能每个文件查一次，否则 202 个文件就是 202 条 SQL，直接撑爆 SQL 预算）
    # enrich done→pending 翻转明细（v2.55.x 翻转监控）：每项
    # {item_id, item_type, reason, old_size, new_size}。本轮结束时
    # finish_scan 汇总，超阈值则告警 + 写 PG 事件（容器重建也丢不掉）。
    enrich_flips: list = field(default_factory=list)



# ==================== 增量扫描：目录指纹 ====================
# 重扫一个库时，绝大多数目录里的文件一个都没动过，但旧实现照样把每个文件走一遍完整流程
# （建/查条目、找本地图片、找外挂字幕、重建外挂字幕轨、每文件一次提交）。实测 2000 个
# 文件的重扫 ≈ 冷扫的 95%，约 4 条 SQL + 1 次 commit / 文件。
#
# 现在按**目录**记指纹：
#   指纹没变 → 该目录的文件不再逐条处理，只做两件必须做的事：
#              ① guid 记进 seen_guids（否则清理阶段会把这些文件当成已删除）；
#              ② 确认库里存在这一行且不需要重探 / 重刮 / 补详情。
#              任一条不满足（新文件、大小变了、库里没这一行、要补图）→ 照常完整处理。
#   指纹变了 → 整个目录照常完整处理，处理完把新指纹写回。
#
# 安全性：跳过**永远**以「库里那一行存在且不过期」为前提，指纹只决定「图片 / 字幕 /
# 轨道这些逐文件的重活要不要重做」。因此指纹写早了、随后进程崩了也不会漏文件——
# 下次扫描会因为「查不到那一行」而重新处理。指纹含目录项数，增删改名都会动它；
# 替换同名文件不一定动 mtime，所以跳过的前提里还要求「文件大小与库里的记录一致」。
#
# 远程挂载同样参与：目录列举本来就要做（图片/字幕判定用同一份列表），指纹只是复用
# 那份列表，不额外增加任何网络往返。SCAN_INCREMENTAL=0 可整体关掉（回到每次全量处理）。
#
# v2.44.0 多了**每库**开关（``Library.incremental_scan``，默认开）：扫描循环里一律问
# ``_incremental_on(ctx)``，而不是直接读这个模块级常量——否则全局开着时无法只给某一个
# 关掉。手动「全量扫描」通过快照上的 ``force_full`` 只让**那一轮**变成全量。
SCAN_INCREMENTAL = (os.getenv("SCAN_INCREMENTAL", "1") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}


def _incremental_on(ctx: "_ScanContext") -> bool:
    """这一轮要不要走增量秒跳：全局开关 ∧ 本库开关 ∧ 没被强制全量

    全量时返回 False，于是「读目录指纹 / 文件指纹秒跳」那一整套都退化为不启用——
    目录照常完整处理一遍，指纹这一轮也不写回（写回也没错，但会白丢一批下次能秒跳的
    机会，所以 force_full 时不写）。
    """
    snap = getattr(ctx, "snap", None)
    if snap is None:
        return SCAN_INCREMENTAL
    if getattr(snap, "force_full", False):
        return False
    return SCAN_INCREMENTAL and getattr(snap, "incremental_scan", True) is not False


# ==================== 到期调度：清单指纹 v2 + next_due_at（FMBY 学习批二 #12）====================
# 学的是 FMBY 的扫描调度：每个目录存一份**清单指纹**，扫描由「到期」驱动——
# 不是每轮都把每个目录按同一深度重验一遍。
#
# 在上面这套增量（L1 文件指纹秒跳 + 目录指纹跳过）之上再分一层（魂 3：调度与
# 数据面分离——调度只决定「核对深度」，枚举/入库/清理这些数据面一律不感知
# 到期）：
#
#   清单指纹（v2）：指纹从旧的弱启发式（本机 mtime:项数 / 挂载 项数:总字节:摘要）
#   升级为对**清单内容**的哈希：本机 = 目录 mtime + 全部条目名，挂载 = 每条目的
#   名/大小/远端 ModTime。输入全部来自列举时本来就拿到的元数据，不为算指纹
#   逐个打开文件。格式带 ``v2:`` 前缀：升级后存量指纹必然对不上，首轮按
#   「指纹变了」完整处理一次（L1 文件级秒跳仍在，成本有界），之后恢复增量。
#
#   到期调度：每个目录在 ScanDirState 里多一份排期（next_due_at + last_decision）：
#     first    —— 第一次见到：完整处理，处理完排期（now + 周期）；
#     changed  —— 清单指纹变了：完整处理，处理完重新排期；
#     due      —— 指纹没变但已到期：走完整核对（含到期重刮判定），核对完推进排期；
#     not_due  —— 指纹没变且未到期：整段跳过，只跑不带 IO 的安全检查（条目在、
#                 没下架、没报修、大小没变），到期重刮这类「到期才做」的判定
#                 推迟到 next_due_at；安全检查不过的文件照常完整处理。
#   周期由 SCAN_DIR_RECHECK_SEC 定（默认 24 小时）；SCAN_DIR_DUE_SCHEDULING=0
#   整体关掉到期层（指纹 v2 仍生效，核对深度与旧行为逐字一致）。
#
# 与其它增量机制的关系（别造两套增量互相打架）：
#   - L1 文件指纹秒跳在更前面：enrich 已补全的文件先被它拦下，根本走不到
#     目录判定——到期层管的是「还没被 L1 拦下」的那些文件所在目录的深度。
#   - Drive Changes 增量（定向扫描，snap.targeted）：事件驱动的条目级更新，
#     只看部分目录——到期层**不参与**（定向扫描不判到期、不写排期），它写回的
#     指纹仍是「扫描当时清单」的事实，下一次整库扫描按清单现状判定即可。
#   - 清理阶段（缺失宽限 #1201）：到期层只动「处理深度」，枚举一根汗毛不碰，
#     seen_guids 永远完整；清理内部顺序也原样——清单可信度守卫
#     （_removal_budget）先判、missing 计数判缺在后（魂 5：对外部变化克制）。
#     尤其不许因为目录「未到期」就剪枝枚举：看不到的文件会被清理当成已删除。
#
# 可观测：每轮扫描统计带 dirs_new / dirs_changed / dirs_due / dirs_not_due
# 四个计数（经 _scan_metrics 对外），加上 ScanDirState 里的 next_due_at /
# last_decision 两列，「哪个目录何时到期、为何触发」在管理面与库里都可查。


def _dir_due_enabled() -> bool:
    """到期调度总开关（批二 #12）：1=开（默认）。运行时读环境变量，
    供测试中途开关（与 _fast_skip_enabled 同口径）。"""
    return (os.getenv("SCAN_DIR_DUE_SCHEDULING", "1") or "1").strip().lower() \
        not in {"0", "false", "no", "off"}


def _dir_recheck_after() -> timedelta:
    """到期周期：目录完整核对后，隔多久再到期重验一次。

    默认 24 小时；钳制 5 分钟～30 天（env_int 统一口径，非法值回落默认）。
    """
    return timedelta(seconds=env_int("SCAN_DIR_RECHECK_SEC", 86400, 300, 2592000))


def _dir_decision(ctx: "_ScanContext", dir_key: str,
                  fingerprint: Optional[str],
                  stored: Optional[tuple],
                  entry_count: Optional[int] = None) -> Optional[str]:
    """目录本轮的到期判定（批二 #12；任务清单第 11 项加按源快照层）。

    返回 None = 到期层不参与本目录，走旧的完整核对深度（增量关 /
    指纹缺失 / 定向扫描 / 到期层关）。

    每个目录每轮只判一次，结果缓存在 ctx.dir_decisions，统计也只计一次。
    ``stored`` 是 (指纹, next_due_at)，按源快照层传 (指纹, next_due_at,
    项数) 时多一条：项数对不上也算 changed（清单项数变了，指纹再像
    也不能跳过——项数是零成本的前置校验）。
    """
    if dir_key in ctx.dir_decisions:
        return ctx.dir_decisions[dir_key]
    decision: Optional[str] = None
    snap = getattr(ctx, "snap", None)
    if (_incremental_on(ctx) and _dir_due_enabled()
            and not getattr(snap, "targeted", False) and fingerprint):
        if stored is None:
            decision = "first"
        elif stored[0] != fingerprint:
            decision = "changed"
        elif (entry_count is not None and len(stored) > 2
                and stored[2] is not None and stored[2] != entry_count):
            decision = "changed"
        elif stored[1] is None or stored[1] <= datetime.now():
            decision = "due"
        else:
            decision = "not_due"
    ctx.dir_decisions[dir_key] = decision
    if decision == "first":
        ctx.stats["dirs_new"] = ctx.stats.get("dirs_new", 0) + 1
    elif decision == "changed":
        ctx.stats["dirs_changed"] = ctx.stats.get("dirs_changed", 0) + 1
    elif decision == "due":
        ctx.stats["dirs_due"] = ctx.stats.get("dirs_due", 0) + 1
    elif decision == "not_due":
        ctx.stats["dirs_not_due"] = ctx.stats.get("dirs_not_due", 0) + 1
    return decision


def _schedule_dir(ctx: "_ScanContext", dir_key: str, decision: str,
                  due_at: Optional[datetime] = None) -> None:
    """把目录本轮的排期决定记进 ctx，批次收尾时随 _store_dir_states 写回。

    due_at 缺省 = now + 周期（刚完整核对/处理过）；not_due 的跳过要保留原
    到期时刻（只记判定供查，不许把排期越推越远），由调用方把原值传进来。
    """
    ctx.dirty_dir_schedule[dir_key] = (
        due_at if due_at is not None else datetime.now() + _dir_recheck_after(),
        decision,
    )


# 分层扫描 L1/L2 总开关（v2.40.0）：1=开（默认）。
# 开：Phase 1 只做文件发现+指纹+极简入库（秒级可见），side/NFO/TMDB 全部
#     交给后台 enrich_worker 补全；关：回到旧行为（Phase 1 内联做完所有事）。
# 探测：background 模式走 probe_worker（不变）；inline 模式在分层下也推迟到
#     enrich_worker 排队（避免远程直链拖慢秒级入库），关分层则恢复当场探测。
SCAN_LAYERED = (os.getenv("SCAN_LAYERED", "1") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}

# 强制远程模式（2026-10-05）：FUSE 挂载（如 /mnt/mp）有本地路径但 IO 并不快，
# 按"本地"处理会走 side/NFO 的逐文件慢操作。设为 1 后，所有文件都按远程处理，
# 跳过逐文件的 FUSE 慢操作，只做极简入库。简洁/高效/稳定：默认关闭，按需开启。
SCAN_FORCE_REMOTE = (os.getenv("SCAN_FORCE_REMOTE", "0") or "0").strip().lower() \
    not in {"0", "false", "no", "off"}


def _fast_skip_enabled() -> bool:
    """文件指纹秒跳总开关（v2.40.0）：1=开（默认）。运行时读环境变量，
    供 smoke_test_scan_incremental.py 这类测试中途开关。"""
    return (os.getenv("SCAN_FAST_SKIP", "1") or "1").strip().lower() \
        not in {"0", "false", "no", "off"}


def _dir_key_of(scan_file: "ScanFile") -> str:
    """目录的稳定标识：本机用绝对路径，挂载用 ``mount://<id>/<rel>``"""
    if scan_file.local_dir:
        return scan_file.local_dir
    return mount_lib.mount_path(scan_file.mount_id, scan_file.dir_rel)


def _dir_fingerprint(ctx: "_ScanContext", scan_file: "ScanFile") -> Optional[str]:
    """目录清单指纹 v2（拿不到返回 None：这个目录这一次不走增量捷径）

    v2 起指纹是**清单内容**的哈希（格式 ``v2:<md5>``），不再是旧的弱启发式
    （本机 mtime:项数 / 挂载 项数:总字节:摘要）：同项数同总大小的改名、等尺寸
    替换在旧口径下都可能漏检，v2 对清单逐条序列化后哈希，清单变指纹必变。
    输入全部来自列举元数据（本机 os.listdir 的名字 + 目录 mtime；挂载条目的
    名/大小/远端 ModTime），不为算指纹逐个打开文件。
    """
    key = _dir_key_of(scan_file)
    if key in ctx.dir_fingerprints:
        return ctx.dir_fingerprints[key]
    value: Optional[str] = None
    if scan_file.local_dir:
        try:
            listing = _list_dir_cached(scan_file.local_dir)
            payload = (f"{os.stat(scan_file.local_dir).st_mtime_ns}\n"
                       + "\n".join(sorted(listing)))
            value = "v2:" + hashlib.md5(payload.encode("utf-8")).hexdigest()
        except OSError:
            value = None
    elif key not in ctx.dir_list_failed:
        entries = _mount_names(ctx, scan_file)
        if key not in ctx.dir_list_failed:
            # 挂载清单序列化（名/大小/远端 ModTime 逐条）：P2 修复（完美审查
            # C-P2-3）的口径整体保留——同尺寸替换只改单个文件时，ModTime 变
            # 指纹必变；无 ModTime 的提供方 mod_ts 全 0，退化为"名字+尺寸"，
            # 仍是确定性的。v2 与旧格式的差别只在不再截断摘要、不再混入
            # 项数/总字节这类可碰撞的聚合量。
            payload = "\n".join(
                f"{e.name}:{int(e.size or 0)}:"
                f"{float(getattr(e, 'mod_ts', 0.0) or 0.0):.0f}"
                for e in sorted(entries, key=lambda x: x.name)
            )
            value = "v2:" + hashlib.md5(payload.encode("utf-8")).hexdigest()
    ctx.dir_fingerprints[key] = value
    return value


def _load_dir_states(db: Session, lib_id: int, dir_keys: list) -> dict:
    """一批目录的已存状态（分片查）：dir_key → (指纹, next_due_at)

    批二 #12 起连排期一起取回：到期判定要在 prepare 里当场做，不能逐目录
    再查一次库。
    """
    found: dict = {}
    unique = list(dict.fromkeys(k for k in dir_keys if k))
    for chunk in _chunks(unique, SQL_IN_CHUNK):
        for row in db.query(emby_models.ScanDirState.dir_key,
                            emby_models.ScanDirState.fingerprint,
                            emby_models.ScanDirState.next_due_at).filter(
            emby_models.ScanDirState.library_id == lib_id,
            emby_models.ScanDirState.dir_key.in_(chunk),
        ):
            found[row.dir_key] = (row.fingerprint, row.next_due_at)
    return found


def _store_dir_states(db: Session, ctx: "_ScanContext") -> None:
    """把本批处理过的目录指纹与排期写回（与条目在同一个事务里提交）

    指纹（ctx.dirty_dirs）与排期（ctx.dirty_dir_schedule）是两个独立的脏集：
    到期重验过但指纹没变的目录只在排期集里，只推进 next_due_at / last_decision，
    不动指纹。
    """
    dirty, ctx.dirty_dirs = ctx.dirty_dirs, {}
    schedule, ctx.dirty_dir_schedule = ctx.dirty_dir_schedule, {}
    if not dirty and not schedule:
        return
    keys = list(dict.fromkeys([*dirty.keys(), *schedule.keys()]))
    existing: dict = {}
    for chunk in _chunks(keys, SQL_IN_CHUNK):
        for row in db.query(emby_models.ScanDirState).filter(
            emby_models.ScanDirState.library_id == ctx.lib_id,
            emby_models.ScanDirState.dir_key.in_(chunk),
        ):
            existing[row.dir_key] = row
    now = datetime.now()
    for dir_key in keys:
        row = existing.get(dir_key)
        if row is None:
            row = emby_models.ScanDirState(library_id=ctx.lib_id, dir_key=dir_key,
                                           fingerprint=dirty.get(dir_key),
                                           updated_at=now)
            db.add(row)
        if dir_key in dirty and row.fingerprint != dirty[dir_key]:
            row.fingerprint = dirty[dir_key]
            row.updated_at = now
        if dir_key in schedule:
            due_at, decision = schedule[dir_key]
            row.next_due_at = due_at
            row.last_decision = decision
            row.updated_at = now


def _load_snapshot_states(db: Session, ctx: "_ScanContext", dir_keys: list) -> None:
    """本批目录的按源快照状态（任务清单第 11 项）。

    只解析到源行的目录进快照层：ctx.snapshot_resolved 记 dir_key → 源行 id，
    ctx.snapshot_states 记 dir_key → (指纹, next_due_at, 项数)（无快照行
    的不记，判定走 first）。解析不到、读表失败、开关关闭时两字典都空，
    调用方一律走旧 ScanDirState 路径（fail-closed）。
    """
    ctx.snapshot_resolved = {}
    ctx.snapshot_states = {}
    if ctx.snapshot_source_rows is None:
        ctx.snapshot_source_rows = scan_snapshots.load_source_rows(db, ctx.lib_id)
    rows = ctx.snapshot_source_rows
    if not rows:
        return
    resolved: dict = {}
    for key in dict.fromkeys(k for k in dir_keys if k):
        source_id = scan_snapshots.resolve_source_id(rows, key)
        if source_id is not None:
            resolved[key] = source_id
    if not resolved:
        return
    ctx.snapshot_resolved = resolved
    found = scan_snapshots.load_snapshots(db, ctx.lib_id, list(resolved.keys()))
    for key in resolved:
        if key in found:
            fp, count, due = found[key]
            ctx.snapshot_states[key] = (fp, due, count)


def _dir_listing_count(ctx: "_ScanContext", scan_file: "ScanFile") -> Optional[int]:
    """目录本轮清单项数（零额外 IO：只读已缓存的列举结果）。

    本机读模块级目录缓存；挂载读 _mount_names 的预热缓存。列举失败
    回 None（调用方不拿项数做判定，退回指纹单一路）。
    """
    key = _dir_key_of(scan_file)
    if scan_file.local_dir:
        try:
            return len(_list_dir_cached(scan_file.local_dir))
        except OSError:
            return None
    if key in ctx.dir_list_failed:
        return None
    entries = _mount_names(ctx, scan_file)
    if key in ctx.dir_list_failed:
        return None
    return len(entries)


def _record_snapshot(ctx: "_ScanContext", dir_key: str, decision: str,
                     due_at: Optional[datetime] = None) -> None:
    """把目录本轮的快照写回决定记进 ctx，批次收尾时随 _store_snapshots 写回。

    只对解析到源行的目录记；due_at 缺省 = now + 周期（与 _schedule_dir
    同口径）；not_due 的调用方须把原到期时刻传进来（不许越推越远）。
    """
    if decision is None:
        return
    source_id = ctx.snapshot_resolved.get(dir_key)
    if source_id is None:
        return
    fingerprint = ctx.dir_fingerprints.get(dir_key)
    if fingerprint is None:
        return
    if due_at is None:
        due_at = datetime.now() + _dir_recheck_after()
    ctx.dirty_snapshots[dir_key] = (
        source_id, fingerprint, ctx.snapshot_counts.get(dir_key),
        decision, due_at)


def _store_snapshots(db: Session, ctx: "_ScanContext") -> None:
    """把本批快照写回（与条目在同一个事务里提交；旧表双写不受影响）。

    directory_path 与 dir_key 同值（本机绝对路径 / mount://<id>/<rel>）。
    假 ctx（部分测试用 SimpleNamespace/_Ctx 替身，只带旧属性）没有
    dirty_snapshots 时直接返回——快照层缺席不许拖垮提交路径。
    """
    dirty = getattr(ctx, "dirty_snapshots", None)
    if not dirty:
        return
    ctx.dirty_snapshots = {}
    scan_snapshots.store_snapshots(db, ctx.lib_id, dirty, datetime.now())


def _can_skip_file(ctx: "_ScanContext", item, pending: "_Pending", fingerprint: Optional[str],
                   stored_fingerprint: Optional[str], defer_scrape: bool = False) -> bool:
    """这个文件能不能不做任何写库工作（只记一笔）

    任何一条不满足都退回完整处理——宁可多做，不能漏文件或漏元数据。

    defer_scrape（批二 #12 到期调度）：目录未到期时传 True——只推迟下面两处
    「到期才做」的刮削判定（到期重刮 / 详情补齐 / 父剧未刮），其余安全检查
    （条目存在、未下架、指纹一致、探针、报修、层级）一条不少。推迟有界：
    目录 next_due_at 一到，重验走完整判定，这些活一件不会少。
    """
    if not _incremental_on(ctx) or item is None or not fingerprint:
        return False
    if getattr(item, "deleted_at", None):
        return False                     # 已下架的条目又出现了：必须走完整路径把它放回来
    if fingerprint != stored_fingerprint:
        return False                     # 目录变过：图片/字幕/轨道都要重新看一遍
    scan_file = pending.scan_file
    if needs_probe(item, scan_file.stored_path, scan_file.size):
        return False                     # 没探过 / 文件大小变了
    if getattr(item, "repair_requested_at", None):
        return False
    # 刮削相关的条件只在「真的可能刮到东西」时才拦：没配 TMDB 的部署重试一万次也不会有结果，
    # 没道理因此让整库每轮都重做一遍（配好密钥的下一次扫描自然会重新处理这些条目）。
    # 另外只有**电影 / 剧集**参与刮削（写库循环里是 item_type in ("series", "movie")）：
    # 集与季根本没有 tmdb_id，`should_scrape` 对它们恒等于「缺元数据 → 要刮」，落到这里就是
    # 「配了 TMDB 的剧集库每轮重扫都得完整处理每一集」——增量扫描对这个最常见的库型等于关掉。
    if not defer_scrape and _tmdb.tmdb_client.configured and pending.item_type in ("series", "movie"):
        if should_scrape(item, ctx.snap.scrape_policy):
            return False                 # 到期重刮 / all 策略 / 缺元数据
        if item.tmdb_id and not (item.imdb_id and item.aliases):
            return False                 # 与循环里的 need_details 一致：详情还没补齐
    if (not defer_scrape and pending.item_type == "episode" and _tmdb.tmdb_client.configured
            and pending.series_guid):
        # B 方案补全：父 series 从没走过 TMDB（无 tmdb_id 且无尝试记录）时不能跳过——
        # episode 隐式创建的 series 平时走不到 TMDB 搜索分支，跳过了就永远刮不到。
        # 干净落地后（无论命中与否）会记 last_scraped_at，之后不再拦：
        # 否则配了 TMDB 的剧集库每轮重扫都得完整处理每一集（v2.31.0 钉住的行为）。
        s = ctx.series_items.get(pending.series_guid)
        if s is not None and not s.tmdb_id and not s.last_scraped_at:
            return False
    if pending.item_type == "episode" and not (item.series_id and item.parent_id):
        return False                     # 剧集/季层级没挂全，走完整处理补齐
    return True


def _prefetch_remote_listings(ctx: "_ScanContext", prepared: list, pool) -> None:
    """并发预热本批远程目录的列举（指纹与图片/字幕判定共用同一份）

    指纹要先列一次目录才拿得到，而那份列表本来就要为图片 / 外挂字幕判定而列——
    这里只是把它从「写库循环里等着」提前到批次开头，和其它 IO 一起并行跑，
    不会因为增量扫描而多出网络往返。
    """
    seen: set = set()
    futures: list = []
    for pending in prepared:
        scan_file = pending.scan_file
        if scan_file.local_dir or scan_file.mount_id is None:
            continue
        key = (scan_file.mount_id, scan_file.dir_rel)
        if key in seen:
            continue
        seen.add(key)
        with ctx.mount_lock:
            if key in ctx.mount_dir_cache:
                continue
        futures.append(_submit(pool, _mount_names, ctx, scan_file))
    for fut in futures:
        _result(fut)                     # 读不到时 _mount_names 自己兜住（记进 dir_list_failed）


# 季目录名（远端挂载的剧集分组用）：Season 01 / S01 / 第1季 / 第一季
_SEASON_DIR_RE = re.compile(
    r"^(?:season\s*\.?\s*\d{1,2}|s\d{1,2}|第\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*季)$",
    re.IGNORECASE,
)


def _series_name_of(scan_file: "ScanFile", parsed: dict) -> str:
    """从剧集目录推导 series 名，绝不从 episode 文件名猜剧名。

    远端路径通常是 ``剧名/Season 1/file S01E01.mkv``；本机路径同理。
    没有 Season 目录时取文件所在目录。目录名里的年份、发布标签由同一套
    ``_tidy_name`` 清洗；若目录名异常为空，才退回解析出的文件名。
    """
    if scan_file.local_dir:
        directory = scan_file.local_dir.rstrip("/\\")
        parent = os.path.basename(os.path.dirname(directory))
        leaf = os.path.basename(directory)
        directory_name = parent if _SEASON_DIR_RE.match(leaf) else leaf
    else:
        directory = (scan_file.dir_rel or "/").rstrip("/") or "/"
        leaf = posixpath.basename(directory)
        parent = posixpath.basename(posixpath.dirname(directory))
        directory_name = parent if _SEASON_DIR_RE.match(leaf) else leaf

    name = _tidy_name(directory_name)
    year = parsed.get("year")
    if year and str(year) in name:
        name = _strip_year(name, year)
    elif not year:
        # 目录名是剧名的权威来源，年份常常只写在目录名尾巴上
        # （「泪之女王 (2024)/Season 1/…」）：集文件名里解析不到年份时，
        # 从这里剥出来回填 parsed["year"]（建 series 行时写进年份栏），
        # 名字里不再带年份（2026-10-20 用户亲令：名字后面不要带年份）。
        name, trailing_year = _split_trailing_year(name)
        if trailing_year is not None:
            parsed["year"] = trailing_year
    return name or _tidy_name(parsed.get("name") or "") or (parsed.get("name") or "未知剧集")


def _series_guid_of(scan_file: "ScanFile") -> str:
    """剧集条目的 guid（由剧集目录推导）

    - 本机：文件目录的父目录（Season 子目录结构；与旧实现一致）
    - 远端：dir_rel 本身是季目录（Season 01 / S01 / 第1季）时取其父目录，
      否则取 dir_rel 本身——与本机分支同口径。旧实现对远端直接取文件目录，
      会把同一部剧的各季拆成多个剧集条目。
    """
    dirpath = scan_file.local_dir
    if dirpath:
        series_dir = os.path.dirname(dirpath.rstrip("/")) if dirpath else ""
        return item_guid(series_dir or (os.path.dirname(scan_file.stored_path) or scan_file.stored_path))
    d = (scan_file.dir_rel or "/").rstrip("/") or "/"
    if _SEASON_DIR_RE.match(os.path.basename(d)):
        d = posixpath.dirname(d) or "/"
    return item_guid(mount_lib.mount_path(scan_file.mount_id, d))


def _library_exists(db: Session, lib_id: int) -> bool:
    """媒体库是否还在（扫描可能跑很久，期间库可能被删）"""
    return db.query(emby_models.Library.id).filter(
        emby_models.Library.id == lib_id
    ).first() is not None


def _load_items(db: Session, guids: list) -> dict:
    """按 guid 批量取条目（分片，避免超出数据库的绑定变量上限）

    老实现每个文件查 1~3 次（条目、剧集、季），十万个文件就是几十万次往返；
    现在一批只查（guids / 200）次。

    **这里必须看得见已软删的条目**（``include_deleted``）：扫描遇到一个 guid 时，
    如果那一行存在只是被下架了，正确做法是把它放回来（连同播放进度与收藏），
    而不是报 guid 唯一键冲突。这也是软删除能“可恢复”的关键一步。
    """
    found: dict = {}
    unique = list(dict.fromkeys(g for g in guids if g))
    with _soft_delete.include_deleted():
        for chunk in _chunks(unique, SQL_IN_CHUNK):
            for row in db.query(emby_models.MediaItem).filter(
                    emby_models.MediaItem.guid.in_(chunk)):
                found[row.guid] = row
    return found


def _is_unique_violation(exc: IntegrityError) -> bool:
    """这个 IntegrityError 是唯一约束冲突（而不是 NOT NULL / 外键 / 类型错）

    只认「唯一约束」这一种，因为只有它值得降级重试：NOT NULL 或外键违规说明
    扫描器自己算错了数据，那属于真 bug，应该原样抛出去让整库失败、留下证据。
    """
    text_ = str(getattr(exc, "orig", exc) or "")
    return ("unique" in text_.lower() or "duplicate key" in text_.lower()
            or "UNIQUE constraint" in text_)


def _claim_guid(db: Session, guid: str, library_id: int, defaults: dict) -> Optional[int]:
    """原子地「占位」一个 guid，返回 ID = 这次 INSERT 真的插进去了（None = 没抢到）

    占位行就是最终那一行。调用方（``_claim_batch_guids``）随后用 IN 查询把本批
    抢到的行取出来直接当 ORM 对象用，**不要**再 ``db.add`` 一个同 guid 的新对象
    （那等于自己撞自己）。

    为什么需要它：``_load_items`` 是「先查有没有，再决定插不插」，而扫描期间
    同一个 guid 可能被**另一个 Session** 先插进去——跨进程扫描（另一台面板 /
    EA）、同一库的重叠任务、或者容器在扫描中途重启后重扫同一个文件。
    查和插之间那个窗口一旦被抢到，两个插入都认为「不存在」，
    第二个 INSERT 撞 ``ix_emby_items_guid`` → **整批回滚、整库扫描 abort**。

    2026-10-04 生产事故：库 17（国产剧）扫到 ``兰香如故 S01E42``，
    该 guid 已由 8 秒前那次（被容器重启打断的）扫描提交入库，本轮再插一次就炸，
    一个文件拖垮整库。

    用数据库自己的 upsert 把「查+插」压成一条原子语句，窗口就没了：
    - PostgreSQL：``INSERT ... ON CONFLICT (guid) DO NOTHING``，
      冲突时靠 RETURNING 是否有行判断是谁赢的；
    - SQLite：``INSERT ... ON CONFLICT (guid) DO NOTHING``；
    - MySQL：``INSERT IGNORE``。

    ``DO NOTHING`` 而不是 ``DO UPDATE``：占位行随后会被扫描器正常补全字段，
    真正要更新已有数据的话走 ORM 的 UPDATE，不需要在这里覆盖别人的行。
    """
    MI = emby_models.MediaItem
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert
        stmt = _pg_insert(MI).values(guid=guid, library_id=library_id, **defaults)
        stmt = stmt.on_conflict_do_nothing(index_elements=["guid"])
        # RETURNING 只在真的插进去时才有行 —— 返回 ID（None = 没抢到）
        # v2.48.2: 返回 ID 而不是 True/False，避免调用方再查一次
        row = db.execute(stmt.returning(MI.id)).first()
        return row[0] if row else None

    # SQLite / MySQL：INSERT OR IGNORE / INSERT IGNORE，rowcount 为 0 即冲突
    if db.bind is not None and db.bind.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert as _my_insert
        stmt = _my_insert(MI).values(guid=guid, library_id=library_id, **defaults)
        stmt = stmt.prefix_with("IGNORE")
    else:
        from sqlalchemy.dialects.sqlite import insert as _lite_insert
        stmt = _lite_insert(MI).values(guid=guid, library_id=library_id, **defaults)
        stmt = stmt.on_conflict_do_nothing(index_elements=["guid"])
    res = db.execute(stmt)
    if (res.rowcount or 0) > 0:
        # 插进去了，取 ID（SQLite/MySQL 没有 RETURNING，用 lastrowid）
        return res.lastrowid
    return None


def _claim_batch_guids(db: Session, prepared: list, ctx: "_ScanContext") -> None:
    """批量占位本批新文件的 guid，并把抢到的行取出来挂在 _pending 上。

    ``_claim_guid`` 的 Core upsert 本身不产生 SELECT，但它只返回 id——如果随后
    每个文件 ``db.get()`` 一次，identity map 里没有这行，162 个文件就是 162 次
    SELECT，直接超冒烟预算。

    这里把占位并成批：先把本批所有新 guid 一次占位完，再用
    ``WHERE id IN (...)`` 把抢到的行一次性取出来（1~2 条语句），以**强引用**
    挂在各 ``_pending.claimed_item`` 上。注意必须强引用：identity map 是弱引用，
    不拿住的话对象会被 GC，后面就取不到了。

    没抢到的（冲突）``claimed_item`` 为 None，调用方走 ``_resolve_conflict``
    单查——冲突是小概率事件，不影响预算。
    """
    MI = emby_models.MediaItem
    to_claim = []
    for p in prepared:
        p.claimed_id = None
        p.claimed_item = None
        if p.item is None:
            to_claim.append(p)

    # 系统性去重（电影）：批量查库里是否已有同部电影，避免 N+1。
    # 参考 Emby 多版本逻辑：同一部电影（同 tmdb_id，或同归一化标题+年份）只保留一条。
    movie_dup_map: dict[int, int] = {}  # pending index -> existing movie id
    movie_pendings = [(idx, p) for idx, p in enumerate(to_claim)
                      if p.item_type == "movie"]
    if movie_pendings:
        try:
            from backend.emby_server import dedup as dedup_lib
            # 一次查出本库所有电影的 (id, tmdb_id, name, year)，Python 层建去重映射
            existing = db.query(MI.id, MI.tmdb_id, MI.name,
                                MI.production_year).filter(
                MI.library_id == ctx.lib_id,
                MI.item_type == "movie",
                MI.deleted_at.is_(None),
            ).all()
            key_to_id: dict = {}
            for eid, etmdb, ename, eyear in existing:
                ekey = dedup_lib.dedup_key_for(ctx.lib_id, "movie", etmdb,
                                               ename, eyear)
                if ekey and ekey not in key_to_id:
                    key_to_id[ekey] = eid
            for idx, p in movie_pendings:
                pkey = dedup_lib.dedup_key_for(
                    ctx.lib_id, "movie", None,
                    p.parsed.get("name"), p.parsed.get("year"))
                if pkey and pkey in key_to_id:
                    movie_dup_map[idx] = key_to_id[pkey]
        except ImportError:
            pass

    # 系统性去重（剧集 Q3）：照抄 movie 模式。严格 key 先行
    # （同 tmdb_id，或同归一化标题+年份）；宽松 key（去虚词归一+年份）
    # 只收录"无歧义"的——同一宽松 key 对应 2 个不同非空 tmdb_id
    # （如星球大战三元组）时整 key 剔除不用。
    series_dup_map: dict[int, int] = {}  # pending index -> existing series id
    series_pendings = [(idx, p) for idx, p in enumerate(to_claim)
                       if p.item_type == "series"]
    if series_pendings:
        try:
            from backend.emby_server import dedup as dedup_lib
            from backend.emby_server import merge_versions_worker as _mvw
            merge_cfg = _mvw.series_merge_config(db)
            # 一次查出本库所有剧集的 (id, tmdb_id, name, year)，Python 层建去重映射
            existing = db.query(MI.id, MI.tmdb_id, MI.name,
                                MI.production_year).filter(
                MI.library_id == ctx.lib_id,
                MI.item_type == "series",
                MI.deleted_at.is_(None),
            ).all()
            key_to_id: dict = {}
            loose_to_tmdb: dict = {}  # 宽松 key -> 见过的非空 tmdb_id 集合
            loose_to_id: dict = {}    # 宽松 key -> (eid, eyear)，歧义的不进
            for eid, etmdb, ename, eyear in existing:
                ekey = dedup_lib.dedup_key_for(ctx.lib_id, "series", etmdb,
                                               ename, eyear)
                if ekey and ekey not in key_to_id:
                    key_to_id[ekey] = eid
                lkey = ("loose", ctx.lib_id,
                        dedup_lib.normalize_name_loose(ename), eyear or 0)
                tmdb_norm = (etmdb or "").strip() or None
                seen = loose_to_tmdb.setdefault(lkey, set())
                if tmdb_norm:
                    seen.add(tmdb_norm)
                if len(seen) > 1:
                    # 生死守卫：2 个不同非空 tmdb_id，整 key 剔除
                    loose_to_id.pop(lkey, None)
                elif lkey not in loose_to_id:
                    loose_to_id[lkey] = (eid, eyear)
            for idx, p in series_pendings:
                pkey = dedup_lib.dedup_key_for(
                    ctx.lib_id, "series", None,
                    p.parsed.get("name"), p.parsed.get("year"))
                if pkey and pkey in key_to_id:
                    series_dup_map[idx] = key_to_id[pkey]
                    continue
                # 总开关=0 时宽松复用全停（严格逻辑不受影响）
                if not merge_cfg["enabled"] or not merge_cfg["loose_enabled"]:
                    continue
                lkey = ("loose", ctx.lib_id,
                        dedup_lib.normalize_name_loose(p.parsed.get("name")),
                        p.parsed.get("year") or 0)
                hit = loose_to_id.get(lkey)
                if hit is None:
                    continue
                eid, eyear = hit
                # 年份守卫：双方年份都存在且相差 > 1 时不复用；任一为空允许复用
                pyear = p.parsed.get("year")
                if pyear and eyear and abs(pyear - eyear) > 1:
                    continue
                series_dup_map[idx] = eid
        except ImportError:
            pass

    for idx, p in enumerate(to_claim):
        if idx in movie_dup_map:
            # 命中已有的同部电影：复用，不建新记录
            p.claimed_id = movie_dup_map[idx]
        elif idx in series_dup_map:
            # 命中已有的同部剧集：复用，不建新记录
            p.claimed_id = series_dup_map[idx]
        else:
            p.claimed_id = _claim_guid(db, p.guid, ctx.lib_id, {
                "item_type": p.item_type,
                "name": p.parsed["name"],
                "file_path": p.scan_file.stored_path,
            })
    won_ids = [p.claimed_id for p in to_claim if p.claimed_id is not None]
    by_id = {}
    # 分片 IN 查询：SQL_IN_CHUNK=200，一批最多 400 个文件，至多 2 条语句
    for chunk in _chunks(won_ids, SQL_IN_CHUNK):
        for it in db.query(MI).filter(MI.id.in_(chunk)).all():
            by_id[it.id] = it
    for p in to_claim:
        # 同一事务内刚占位的行，IN 查询一定能查到；万一没查到就让调用方走
        # _resolve_conflict 兜底，不会丢条目
        p.claimed_item = by_id.get(p.claimed_id)


def _guid_held_by_other(db: Session, guid: Optional[str], row_id: int) -> Optional[int]:
    """检查 guid 是否已被另一行占用（P0 改名冲突保护）。

    改名识别（file_id / 指纹）在 ``_load_items`` 之后运行：并发扫描、批次重试
    可能让当时不存在的行现在已存在。复用旧行前必须确认目标 guid 空闲，
    否则写库时的 ``item.guid = new_guid`` 会撞 ``ix_emby_items_guid`` 唯一键、
    整批回滚（2026-10-11 生产事故：同集多版本互认成改名，两次撞库）。

    软删除的行也算占用：唯一约束不看 ``deleted_at``。
    guid 为空（mock/异常路径）时直接返回 None：无法检查就不拦。
    返回占用者的 id，无占用返回 None。
    """
    if not guid:
        return None
    MI = emby_models.MediaItem
    with _soft_delete.include_deleted():
        hit = (
            db.query(MI.id)
            .filter(MI.guid == guid, MI.id != row_id)
            .first()
        )
    return hit[0] if hit else None


def _resolve_conflict(db: Session, guid: str) -> Any:
    """upsert 没抢到（别人先插了）时，把那行取出来给扫描器当已存在条目用

    查得到就返回 ORM 对象：调用方随后走 update 分支，字段会被正常补全，
    于是「被抢到的这一集」最终状态与「自己插的」完全一样，只是不再重复插入。
    """
    return db.query(emby_models.MediaItem).filter(
        emby_models.MediaItem.guid == guid
    ).first()


def _season_row_visible(row) -> bool:
    """季行是否对播放器可见：软删行与合并走的行不算（季列表只认可见行）。"""
    return (row is not None
            and getattr(row, "deleted_at", None) is None
            and getattr(row, "merged_into_id", None) is None)


def _prefetch_batch_season_rows(db: Session, ctx: "_ScanContext",
                                prepared: list) -> None:
    """B2 季行随批预取（2026-10-20）：本批剧集用到的季行按 (series_id, season_no)
    一次查回，写库循环里 ``_ensure_season_row`` 不再逐键回退 SELECT。

    只能覆盖预取时已解析出的 series（``ctx.series_items`` 里已有行，即扫描前
    已存在的剧）；本批新建的 series 在预取时还没有 id——它们的季行不可能已在
    库里，``_ensure_season_row`` 里凭 ``ctx.new_series_ids`` 跳过回退 SELECT
    直接插入。目录 guid 漂移（``_find_duplicate_series`` 复用老 series）是例外：
    老 series 不在 new_series_ids 里，回退 SELECT 保留。
    """
    MI = emby_models.MediaItem
    pairs = set()
    for pending in prepared:
        if getattr(pending, "item_type", None) != "episode":
            continue
        season_no = (getattr(pending, "parsed", None) or {}).get("season")
        if season_no is None:
            continue
        srow = ctx.series_items.get(getattr(pending, "series_guid", None))
        sid = getattr(srow, "id", None)
        if sid is not None:
            pairs.add((sid, season_no))
    if not pairs:
        return
    for chunk in _chunks(sorted(pairs), SQL_IN_CHUNK):
        rows = (
            db.query(MI)
            .filter(MI.library_id == ctx.lib_id,
                    MI.item_type == "season",
                    tuple_(MI.series_id, MI.season_number).in_(chunk),
                    MI.deleted_at.is_(None),
                    MI.merged_into_id.is_(None))
            .all()
        )
        for row in rows:
            ctx.season_items_by_series[(row.series_id, row.season_number)] = row


def _ensure_season_row(db: Session, ctx: "_ScanContext", library,
                       series, season_no: int, season_guid_hint: str):
    """B2（缺集普查 2026-10-19）：建集行时同事务内保证季行存在。

    幂等键是 ``(series.id, season_no)``——季的身份跟最终落定的 series 走，
    而不是目录推导的 guid：重扫/改名/目录结构变化后目录 guid 会漂移，旧实现
    只按目录 guid 查就会漏建（集行建了季行没建）或建重复。
    并发安全沿用现有模式：SAVEPOINT 内插入，撞唯一键（另一扫描并发先建了）
    时降级为按 guid 重查复用，不让整批回滚。
    特殊集（season 0）沿用现有惯例（同样建「第 0 季」行），不另立口径。
    """
    key = (series.id, season_no)
    season_item = ctx.season_items_by_series.get(key)
    if _season_row_visible(season_item):
        return season_item
    # 批次预取的 guid 缓存：命中也要验可见性（_load_items 带 include_deleted，
    # 软删/合并走的季行会被查回来，但季列表认不出它们）。
    season_item = ctx.season_items.get(season_guid_hint)
    if _season_row_visible(season_item):
        # 同一 guid 的季行若归属别的 series，不能复用（防目录 guid 漂移串季）
        if (season_item.series_id == series.id
                and season_item.season_number == season_no):
            ctx.season_items_by_series[key] = season_item
            return season_item
        season_item = None
    if season_item is None and series.id not in ctx.new_series_ids:
        # 回退 SELECT 只对扫描前已存在的 series 有意义：目录 guid 漂移时季行
        # 挂在老 guid 下，(series.id, season_no) 能把它找回来（B2 的主场景）。
        # 本批新建的 series（ctx.new_series_ids）季行不可能已存在，跳过回退
        # 直接插入——守住"按批加载"预算护栏（逐键 SELECT 曾把预算 12 推到 14）。
        # 本批可解析 series 的季行已在 _prepare_and_prefetch 里随批预取。
        MI = emby_models.MediaItem
        season_item = (
            db.query(MI)
            .filter(MI.library_id == library.id,
                    MI.item_type == "season",
                    MI.series_id == series.id,
                    MI.season_number == season_no,
                    MI.deleted_at.is_(None),
                    MI.merged_into_id.is_(None))
            .order_by(MI.id)
            .first()
        )
    if season_item is None:
        season_item = emby_models.MediaItem(
            guid=season_guid_hint, library_id=library.id,
            item_type="season", name=f"第 {season_no} 季",
            parent_id=series.id, series_id=series.id,
            season_number=season_no, date_added=datetime.now(),
            probe_status="done",  # 剧集/季没有文件可探测
        )
        try:
            with db.begin_nested():
                db.add(season_item)
                db.flush()
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            # 并发扫描抢先建了同 guid 季行：降级复用，不让整批回滚。
            # 注意：SAVEPOINT 回滚后失败对象已不在 session.new 里（跨版本行为有
            # 差异），重查前只在仍在 new 里时才 expunge，避免 autoflush 重插。
            if season_item in db.new:
                db.expunge(season_item)
            logger.info("季行并发建重复，降级复用 guid=%s", season_guid_hint)
            # 复用行可能已被软删/合并（guid 唯一键仍占着，建不出第二行）：
            # 用 include_deleted 把它找回来（B1 死壳范畴，读侧虚拟季兜底），
            # 而不是让唯一键异常漏出去把整批掀翻。
            with _soft_delete.include_deleted():
                season_item = (
                    db.query(emby_models.MediaItem)
                    .filter(emby_models.MediaItem.guid == season_guid_hint)
                    .first()
                )
            if season_item is None:
                raise
    if not _season_row_visible(season_item):
        # 极少数情况：同 guid 的旧季行已被软删/合并（B1 死壳范畴）。
        # guid 唯一键占着建不出新行，只能复用旧行；先把属性加载完再脱离
        # 会话并跳过缓存——否则后续属性刷新会被软删钩子过滤掉而抛
        # ObjectDeletedError。读侧虚拟季（#1092）会为这类季合成条目，季列表不断。
        if not inspect(season_item).persistent:
            # 已不在本会话里的实例：只能是本分支在同一 (剧, 季) 的前一集
            # 处理时 refresh+expunge 留下的（ctx.season_items 的 guid 键
            # 没有摘，后续集数还会命中它）。脱离前属性已全量加载，直接
            # 复用；再对脱离实例 db.refresh 会抛 InvalidRequestError
            # （2026-10-20 动漫库连续三轮整库失败的生产事故）。
            return season_item
        with _soft_delete.include_deleted():
            db.refresh(season_item)
        db.expunge(season_item)
        return season_item
    ctx.season_items_by_series[key] = season_item
    ctx.season_items[season_guid_hint] = season_item
    return season_item


def _load_external_subtitles(db, item_ids: list) -> dict:
    """批量取本批条目的外挂字幕路径：item_id → {external_path}。

    秒跳时要比对字幕有无变化，一批只查一次（分片），不能每个文件查一次。
    """
    result: dict = {}
    unique = list(dict.fromkeys(i for i in item_ids if i))
    for chunk in _chunks(unique, SQL_IN_CHUNK):
        for item_id, ext_path in db.query(
                emby_models.MediaStream.item_id,
                emby_models.MediaStream.external_path).filter(
                emby_models.MediaStream.item_id.in_(chunk),
                emby_models.MediaStream.is_external.is_(True)):
            if ext_path:
                result.setdefault(item_id, set()).add(ext_path)
    return result


def _ext_subtitles_need_refresh(external, db_paths) -> bool:
    """本文件是否需要走外挂字幕删除+重建。

    external: 本轮发现的字幕 list[(lang, sub_path)] 或 None
    db_paths: DB 已登记的字幕路径 set 或 None（来自 ctx.ext_subtitles，零额外 SQL）
    返回 False 时调用方跳过 db.flush() 和 DELETE（零 DB 写）。
    """
    if external:
        return True
    return bool(db_paths)


def _local_names(ctx: "_ScanContext", dirpath: str) -> list:
    """本机目录列表（与写库线程共用同一份缓存，预热之后不会重复读目录）"""
    return _list_dir_cached(dirpath)


def _mount_dir_entries(ctx: "_ScanContext", scan_file: "ScanFile", dir_rel: str) -> list:
    """远程挂载目录列表（同一个目录只请求一次；大目录下这一项能省掉九成网络往返）

    同一个目录的文件会被分发到不同工作线程，所以这里按目录单飞：线程之间只等「别人正在
    列的这个目录」，不同目录仍然并行。分成两段的实现在并发时会各自去请求一次网盘。
    比旧 _mount_names 多一个 dir_rel 参数：NFO 发现要查父目录（tvshow.nfo），
    复用同一份缓存与单飞锁，不会多一次网络往返。
    """
    key = (scan_file.mount_id, dir_rel)
    with ctx.mount_lock:
        if key in ctx.mount_dir_cache:
            return ctx.mount_dir_cache[key]
        lock = ctx.mount_dir_locks.get(key)
        if lock is None:
            lock = ctx.mount_dir_locks[key] = threading.Lock()
    with lock:
        with ctx.mount_lock:               # 别人可能已经列完了
            if key in ctx.mount_dir_cache:
                return ctx.mount_dir_cache[key]
        try:
            entries = scan_file.provider.list_dir(dir_rel)
        except mount_lib.MountError as exc:
            logger.warning("读取挂载目录失败，跳过：%s（%s）", dir_rel, exc)
            entries = []
            ctx.dir_list_failed.add(_dir_key_of(scan_file))
        except Exception as exc:  # noqa: BLE001 — 目录读不到不该中断扫描
            logger.warning("读取挂载目录异常，跳过：%s（%s）", dir_rel, exc)
            entries = []
            ctx.dir_list_failed.add(_dir_key_of(scan_file))
        with ctx.mount_lock:
            ctx.mount_dir_cache[key] = entries
            ctx.mount_dir_locks.pop(key, None)  # 列完就不必再留着这把锁
        return entries


def _mount_names(ctx: "_ScanContext", scan_file: "ScanFile") -> list:
    """文件所在目录的远程列举（_mount_dir_entries 的便捷包装）"""
    return _mount_dir_entries(ctx, scan_file, scan_file.dir_rel)


def _side_info(ctx: "_ScanContext", scan_file: "ScanFile") -> tuple:
    """本地图片与外挂字幕（IO 密集，放在线程池里跑）"""
    if scan_file.local_dir:
        names = _local_names(ctx, scan_file.local_dir)
        poster, fanart = find_local_images_in(
            names, scan_file.local_dir, os.path.splitext(scan_file.name)[0],
        )
        return poster, fanart, find_external_subtitles_in(names, scan_file.stored_path)
    if scan_file.mount_id is not None:
        entries = _mount_names(ctx, scan_file)
        return None, None, find_external_subtitles_remote(
            scan_file.name, entries, scan_file.mount_id, scan_file.dir_rel,
        )
    return None, None, []


def _nfo_candidates(kind: str, name: str) -> list:
    """按 Kodi 约定给出 NFO 候选文件名（按优先级）"""
    stem = os.path.splitext(name)[0]
    if kind == "movie":
        return [f"{stem}.nfo", f"{name}.nfo", "movie.nfo"]
    if kind == "series":
        return ["tvshow.nfo"]
    # episode：实测远端形如 xxx.1080p.mp4.nfo（全名 + .nfo），也兼容去扩展名写法
    return [f"{name}.nfo", f"{stem}.nfo"]


def _read_nfo_cached(ctx: "_ScanContext", scan_file: "ScanFile",
                     local_path: Optional[str] = None,
                     mount_rel: Optional[str] = None):
    """读 + 解析一个 NFO（ctx 级缓存，含阴性缓存；异常当没有 NFO）"""
    key = ("local", local_path) if local_path else ("mount", scan_file.mount_id, mount_rel)
    if key not in ctx.nfo_cache:
        try:
            # v2.42.9：真的读一次才计数（耗时含 NFO 的读 + 解析）——
            # 「nfo_read 次数」与「nfo_hit 次数」的比就是 ctx 缓存的实际复用率
            with progress.stage_timer("nfo_read"):
                if local_path:
                    with open(local_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                        text = f.read()
                else:
                    # 阶段 2 拆分：NFO 读走旁挂文本过渡口（MountProvider
                    # 子类走新口，duck-type 旧提供者回落 read_text，见
                    # mounts.read_sidecar_text_from 的过渡口径）。
                    text = mount_lib.read_sidecar_text_from(
                        scan_file.provider, mount_rel)
                ctx.nfo_cache[key] = nfo_lib.parse_nfo(text)
        except Exception:  # noqa: BLE001 — 单个 NFO 读不到就当没有
            ctx.nfo_cache[key] = None
    else:
        # ctx 缓存命中（含阴性缓存）：不读盘、不联网，只记一次命中
        progress.note_stage("nfo_hit")
    return ctx.nfo_cache[key]


def _locate_series_dir(ctx: "_ScanContext", scan_file: "ScanFile", dirpath: str) -> Optional[str]:
    """episode 的剧集目录：本目录或父目录中含 tvshow.nfo 的那个（都没有返回 None）"""
    if scan_file.local_dir is not None:
        if "tvshow.nfo" in _list_dir_cached(dirpath):
            return dirpath
        parent = os.path.dirname(dirpath.rstrip("/"))
        if parent and parent != dirpath and "tvshow.nfo" in _list_dir_cached(parent):
            return parent
        return None
    if "tvshow.nfo" in {e.name for e in _mount_dir_entries(ctx, scan_file, dirpath)}:
        return dirpath
    parent = posixpath.dirname(dirpath.rstrip("/")) or "/"
    if parent != dirpath and "tvshow.nfo" in {
        e.name for e in _mount_dir_entries(ctx, scan_file, parent)
    }:
        return parent
    return None


def _find_nfos(ctx: "_ScanContext", scan_file: "ScanFile", kind: str) -> tuple:
    """发现并解析 NFO → (nfo_data, series_nfo_data, season_nfo_data)

    - movie：同目录 `<片名>.nfo` / `movie.nfo`
    - series（剧集库单文件）：同目录 `tvshow.nfo`
    - episode：同目录 `<集文件名>.nfo`；同目录 `season.nfo`；剧集目录 `tvshow.nfo`
    """
    is_local = scan_file.local_dir is not None
    if not is_local and not (scan_file.mount_id is not None and scan_file.provider is not None):
        return None, None, None
    dirpath = scan_file.local_dir if is_local else (scan_file.dir_rel or "/")

    def read_nfo(d: str, fname: str):
        if is_local:
            return _read_nfo_cached(ctx, scan_file, local_path=os.path.join(d, fname))
        return _read_nfo_cached(ctx, scan_file, mount_rel=posixpath.join(d, fname))

    if is_local:
        names = set(_list_dir_cached(dirpath))
    else:
        names = {e.name for e in _mount_dir_entries(ctx, scan_file, dirpath)}

    nfo_data = series_nfo_data = season_nfo_data = None
    if kind in ("movie", "episode"):
        for cand in _nfo_candidates(kind, scan_file.name):
            if cand in names:
                nfo_data = read_nfo(dirpath, cand)
                if nfo_data:
                    break
    elif kind == "series" and "tvshow.nfo" in names:
        nfo_data = read_nfo(dirpath, "tvshow.nfo")
    if kind == "episode":
        if "season.nfo" in names:
            season_nfo_data = read_nfo(dirpath, "season.nfo")
        series_dir = _locate_series_dir(ctx, scan_file, dirpath)
        if series_dir:
            series_nfo_data = read_nfo(series_dir, "tvshow.nfo")
    return nfo_data, series_nfo_data, season_nfo_data


def _nfo_work(ctx: "_ScanContext", scan_file: "ScanFile", kind: str) -> tuple:
    """NFO 发现与解析（IO 线程池里跑；单个失败不影响整批）"""
    try:
        return _find_nfos(ctx, scan_file, kind)
    except Exception as exc:  # noqa: BLE001
        logger.warning("NFO 发现失败 %s：%s", scan_file.stored_path, exc)
        return None, None, None


def _tmdb_work(need_search: bool, name: str, year, kind: str,
               existing_id, want_details: bool) -> tuple:
    """在**同一个工作线程**里跑完这个条目需要的 TMDB 调用（搜索 → 详情）

    搜索与详情有先后依赖，所以不能拆到两个批次里；但条目之间可以并行——
    老实现是逐条串行等网络，追新时最卡的就是这里。多个条目共用同一个 httpx 客户端，
    它的连接池是线程安全的；密钥轮询只在配额报错时发生。

    ``want_details`` 由调用方按「写库那一步会不会用详情」算好（新条目要补 IMDb Id 与别名、
    带补图标记的条目要重取图）；这里不再对每个命中都无条件拉一次详情。
    """
    hit = _tmdb.tmdb_client.search(name, year, kind) if need_search else None
    details = None
    if hit:
        if want_details:
            details = _tmdb.tmdb_client.details(str(hit.get("id")), kind)
    elif existing_id and want_details:
        details = _tmdb.tmdb_client.details(str(existing_id), kind)
    return hit, details


def _result(handle, default=None):
    """取出预取结果（None 表示没提交任务；任务异常不影响整次扫描）"""
    if handle is None:
        return default
    if isinstance(handle, Future):
        try:
            return handle.result()
        except Exception as exc:  # noqa: BLE001 — 单个文件的探测/刮削失败不该拖垮整库
            logger.warning("扫描预取任务失败：%s", exc)
            return default
    return handle


# 扫描用的进程级线程池：同一进程里可能连续扫多个媒体库（多来源、多库批量扫描），
# 反复建池/销池反而更贵。池子只在第一次真正提交任务时才会创建线程，空闲时几乎不吃资源，
# 进程退出时由解释器统一回收。
_SCAN_POOL: Optional[ThreadPoolExecutor] = None
_SCAN_POOL_LOCK = threading.Lock()


def _scan_pool() -> ThreadPoolExecutor:
    global _SCAN_POOL
    with _SCAN_POOL_LOCK:
        if _SCAN_POOL is None or getattr(_SCAN_POOL, "_shutdown", False):
            _SCAN_POOL = ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="scan-io")
        return _SCAN_POOL


def _submit(pool: ThreadPoolExecutor, fn, *args, **kwargs) -> "Future":
    """提交任务；池已关闭就换一个再提交。

    ## 为什么需要

    旧实现把池直接交给调用方 ``pool.submit(...)``。而 ``_scan_pool()`` 只在
    **取池那一刻**检查 ``_shutdown``——从取到提交之间存在窗口：部署重启
    （容器收到 SIGTERM）会关掉进程级线程池，此时正在跑的扫描继续往旧池提交，
    就会抛：

        RuntimeError: cannot schedule new futures after shutdown

    整库扫描因此被标记 failed。生产实测：部署期间「动漫」库整轮扫描失败。

    这里捕获该异常、换一个新池重试一次。重启属正常运维，换池继续是对的；
    真正提不上去的函数异常会正常抛出，不被这里吞掉。
    """
    try:
        return pool.submit(fn, *args, **kwargs)
    except RuntimeError as exc:
        if "after shutdown" not in str(exc):
            raise
        logger.info("扫描线程池已关闭，换新池重试提交：%s", fn.__name__ if hasattr(fn, "__name__") else fn)
        return _scan_pool().submit(fn, *args, **kwargs)



def _match_renames_by_file_id(db: Session, prepared: list, known: dict,
                              ctx: "_ScanContext",
                              claimed_row_ids: Optional[set] = None) -> None:
    """v2.52.0: 用 drive_file_id 识别改名/移动。

    guid（path 派生）没命中的文件，如果它的 drive_file_id 在库里能找到，
    说明是同一文件改了名/换了目录：把已有的那行直接挂到 pending 上，
    写库循环走 update 分支（更新 file_path + guid），不删记录、不丢元数据。

    批量查一次，不逐文件点查。没有 file_id 的文件（本地/115）跳过。

    claimed_row_ids：调用方先跑的 ``_attach_known_items`` 已认领的行 id
    集合——这些行不得再被认领（r60：多集迁移下主/次 pending 双认领同一旧行）。
    """
    MI = emby_models.MediaItem
    # 收集需要按 file_id 查的 pending
    need: list = []
    fids: list[str] = []
    for p in prepared:
        if p.item is not None:
            continue  # guid 已命中，不是改名
        fid = getattr(p.scan_file, "file_id", "") or ""
        if not fid:
            continue
        need.append(p)
        fids.append(fid)
    if not need:
        return
    # 批量查（分片），要看得见软删除的行（改名后又删了又改回来也能认出来）。
    # 同一 file_id 可能对应多行（多集文件 S06E27-E28 建两行）：按行收，
    # 后面按集号精确配对，不许张冠李戴。
    found: dict[str, list] = {}
    unique_fids = list(dict.fromkeys(fids))
    with _soft_delete.include_deleted():
        for chunk in _chunks(unique_fids, SQL_IN_CHUNK):
            for row in db.query(MI).filter(
                    MI.drive_file_id.in_(chunk),
                    MI.library_id == ctx.lib_id).all():
                if row.drive_file_id:
                    found.setdefault(row.drive_file_id, []).append(row)
    matched = 0
    # r60：先跑的 _attach_known_items 已认领的行不得再认领（多集迁移场景）。
    claimed_row_ids = set(claimed_row_ids) if claimed_row_ids else set()
    for p, fid in zip(need, fids):
        rows = [r for r in found.get(fid, []) if r.id not in claimed_row_ids]
        if not rows:
            continue
        # 多集文件同一 file_id 对应多行：按集号精确配对（E27 的 pending
        # 只认 episode_number=27 的行）。电影行没有集号时沿用旧行为。
        if p.item_type == "episode":
            _ep = (p.parsed or {}).get("episode")
            rows = [r for r in rows
                    if getattr(r, "episode_number", None) == _ep]
            if len(rows) != 1:
                # 集号对不上 / 歧义：不碰，走新文件流程（宁可删旧建新）
                continue
        row = rows[0]
        claimed_row_ids.add(row.id)
        # 同一文件但路径没变：说明 guid 查漏了（不应该发生），防御性处理
        if row.file_path == p.scan_file.stored_path:
            p.item = row
            continue
        # 改名/移动：复用这一行
        # P0 改名冲突保护（与指纹路径同理）：目标 guid 被占用则放弃改名，
        # 让 pending 走正常新文件占位流程，避免撞唯一键整批回滚。
        _holder = _guid_held_by_other(db, getattr(p, "guid", None), row.id)
        if _holder is not None:
            logger.warning(
                "扫描改名识别放弃（file_id）：%s 的目标 guid 已被条目 %s 占用，"
                "按新文件处理",
                p.scan_file.stored_path, _holder)
            ctx.stats["rename_collision_skipped"] = (
                ctx.stats.get("rename_collision_skipped", 0) + 1)
            continue
        p.item = row
        p.renamed = True
        matched += 1
        logger.info("扫描识别改名/移动: %s -> %s（保留元数据）",
                    row.file_path, p.scan_file.stored_path)
    if matched:
        ctx.stats["renamed"] = ctx.stats.get("renamed", 0) + matched


def _attach_known_items(prepared: list, known: dict) -> set:
    """把 guid 命中的已有行挂到 pending 上。

    guid（path 派生）是最强的身份，调用方必须**先**调本函数、再调改名匹配
    （``_match_renames_by_file_id`` / ``_match_renames_by_fingerprint``），
    并把返回的已认领行 id 集合传给它们——改名识别不得再认领这些行。

    r60 实证：多集迁移场景（旧单行 guid 与主行一致）下，若改名识别先跑，
    第二行会把旧行抢走，随后本函数又把同一旧行挂给主行——两个 pending
    共用一行，第二行永远建不出来，且静默无报错。
    """
    claimed: set = set()
    for pending in prepared:
        if pending.item is None:
            row = known.get(pending.guid)
            if row is not None:
                pending.item = row
                claimed.add(row.id)
    return claimed


def _match_renames_by_fingerprint(db: Session, prepared: list, known: dict,
                                  ctx: "_ScanContext",
                                  claimed_row_ids: Optional[set] = None) -> None:
    """用 (文件大小, 解析身份) 指纹识别改名/移动（无 file_id 的来源）。

    ``_match_renames_by_file_id`` 只覆盖有稳定 file_id 的来源（Drive/rclone）；
    本地目录和 115 没有 file_id，改名/移动会走"删旧建新"，播放进度、收藏、
    刮削元数据全丢在这里。

    指纹 = (size, 解析身份)：size 必须 > 0 且相等（大小变了说明内容变了，
    不算改名）；解析身份 movie 用归一化标题 + 年份，episode 用归一化剧名 +
    季 + 集，候选行用 ``parse_media_filename`` 同口径解析，保证两边一致。

    保守策略（宁可删旧建新，不张冠李戴）：
    - 同一指纹下必须**恰好 1 个待匹配文件对恰好 1 个候选行**才认领；
    - 候选行必须本轮没再见到（guid 在 seen_guids 里的是重复文件，不是改名）；
    - 有 file_id 的文件跳过——file_id 是权威身份，没匹配上就是真新文件。
    - claimed_row_ids：``_attach_known_items`` 已认领的行不得再认领（r60）。
    """
    from types import SimpleNamespace
    # 版本签名模块：函数顶部导入一次，_ver_sig 在热路径（逐文件×逐候选行）
    # 被调用，import 语句虽走 sys.modules 缓存仍是无谓开销。
    # （episode_versions 顶层只依赖 logging/os/typing，与 scanner 无循环导入）
    from backend.emby_server import episode_versions as _ev

    def _norm(name: str) -> str:
        return "".join(
            ch for ch in (name or "").lower()
            if not ch.isspace() and ch not in "._-"
        )

    def _ver_sig(path: str) -> tuple:
        # 版本签名（P1）：同集多版本（2160p / 1080p / H265…）不再互认为改名。
        # 纯字符串解析，零 IO。任一版本缺签名则退化为旧口径（相等），不误伤正常改名。
        try:
            return _ev.version_signature(path)
        except Exception:  # noqa: BLE001
            return (None, None, None)

    def _pending_fp(p) -> Optional[tuple]:
        size = p.scan_file.size or 0
        if size <= 0:
            return None
        parsed = p.parsed or {}
        if p.item_type == "movie":
            if not parsed.get("name"):
                return None
            return ("movie", size, _norm(parsed["name"]), parsed.get("year"))
        if p.item_type == "episode":
            if (not p.series_name or parsed.get("season") is None
                    or parsed.get("episode") is None):
                return None
            return ("episode", size, _norm(p.series_name),
                    parsed["season"], parsed["episode"],
                    _ver_sig(p.scan_file.stored_path))
        return None

    def _row_fp(row) -> Optional[tuple]:
        if not row.file_path or not row.size:
            return None
        lib_type = "movies" if row.item_type == "movie" else "tvshows"
        try:
            parsed = parse_media_filename(row.file_path, lib_type)
        except Exception:  # noqa: BLE001 — 解析失败的行不参与指纹匹配
            return None
        if row.item_type == "movie":
            if not parsed.get("name"):
                return None
            return ("movie", row.size, _norm(parsed["name"]), parsed.get("year"))
        # episode：用 _series_name_of 从旧路径推导剧名（与 pending 侧同口径）
        if mount_lib.is_mount_path(row.file_path):
            _mid, rel = mount_lib.parse_mount_path(row.file_path)
            sf = SimpleNamespace(local_dir=None,
                                 dir_rel=posixpath.dirname(rel) or "/")
        else:
            sf = SimpleNamespace(local_dir=os.path.dirname(row.file_path),
                                 dir_rel=None)
        try:
            series_name = _series_name_of(sf, parsed)
        except Exception:  # noqa: BLE001
            return None
        # episode 的季/集身份用行自身的字段（入库时写死），而不是从旧路径
        # 重新解析——多集文件的第二行（episode_number=episode_end）解析旧路径
        # 只会得到区间起点，永远匹配不上自己的 pending。单集行两者一致，
        # 行为不变（存量手工订正过的行按订正后的身份参与匹配，更准确）。
        _r_season = (row.season_number if row.season_number is not None
                     else parsed.get("season"))
        _r_ep = (row.episode_number if row.episode_number is not None
                 else parsed.get("episode"))
        if not series_name or _r_season is None or _r_ep is None:
            return None
        return ("episode", row.size, _norm(series_name),
                _r_season, _r_ep,
                _ver_sig(row.file_path or ""))

    need: list = []
    for p in prepared:
        if p.item is not None or p.renamed:
            continue
        if p.item_type not in ("movie", "episode"):
            continue
        if getattr(p.scan_file, "file_id", "") or "":
            continue  # 有 file_id 的走 file_id 版，不掺和
        fp = _pending_fp(p)
        if fp is None:
            continue
        need.append((p, fp))
    if not need:
        return

    sizes = sorted({fp[1] for _, fp in need})
    candidates: list = []
    with _soft_delete.include_deleted():
        for chunk in _chunks(sizes, SQL_IN_CHUNK):
            candidates.extend(
                db.query(emby_models.MediaItem).filter(
                    emby_models.MediaItem.library_id == ctx.lib_id,
                    emby_models.MediaItem.item_type.in_(("movie", "episode")),
                    emby_models.MediaItem.size.in_(chunk),
                ).all()
            )

    by_fp: dict = {}
    # 版本签名回退索引：任一侧签名缺失（全 None）时退化为旧口径（相等），
    # 不误伤正常改名。key 为去掉版本签名的基础指纹，value 为 (row, ver_sig) 列表。
    by_fp_base: dict = {}
    _EMPTY_SIG = (None, None, None)
    for row in candidates:
        if row.guid in ctx.seen_guids:
            continue  # 本轮还见到的行是重复文件，不是改名
        fp = _row_fp(row)
        if fp is None:
            continue
        by_fp.setdefault(fp, []).append(row)
        by_fp_base.setdefault(fp[:-1], []).append((row, fp[-1]))

    # 1:1 才认领：同一指纹多个待匹配文件，或多个候选行，都算歧义，放弃
    want_count: dict = {}
    for _, fp in need:
        want_count[fp] = want_count.get(fp, 0) + 1
    claimed_rows: set = set(claimed_row_ids) if claimed_row_ids else set()
    matched = 0
    for p, fp in need:
        if want_count[fp] != 1:
            continue
        rows = [r for r in by_fp.get(fp, []) if r.id not in claimed_rows]
        if not rows:
            # 版本签名回退：精确指纹无命中时，若任一侧版本签名缺失（全 None），
            # 退化为旧口径（去掉版本签名比较）。两边都有明确签名且不一致时
            # （如 1080p vs 2160p）不回退——那是多版本，不是改名。
            _p_ver = fp[-1]
            for _r, _r_ver in by_fp_base.get(fp[:-1], []):
                if _r.id in claimed_rows:
                    continue
                if _p_ver == _EMPTY_SIG or _r_ver == _EMPTY_SIG:
                    rows.append(_r)
            # 去重（同一行可能被多次加入）
            _seen = set()
            _uniq = []
            for _r in rows:
                if _r.id not in _seen:
                    _seen.add(_r.id)
                    _uniq.append(_r)
            rows = _uniq
        if len(rows) != 1:
            continue
        row = rows[0]
        # P0 改名冲突保护：p.guid 可能已被另一行占用（并发扫描/批次重试
        # 导致 _load_items 时不存在的行现在已存在）。直接复用会撞唯一键，
        # 整批回滚。放弃改名，让 pending 走正常新文件占位流程
        # （_claim_guid  upsert + _resolve_conflict 兜底）。
        _holder = _guid_held_by_other(db, getattr(p, "guid", None), row.id)
        if _holder is not None:
            logger.warning(
                "扫描改名识别放弃：%s 的目标 guid 已被条目 %s 占用，"
                "按新文件处理（疑似同集多版本或并发写入）",
                p.scan_file.stored_path, _holder)
            ctx.stats["rename_collision_skipped"] = (
                ctx.stats.get("rename_collision_skipped", 0) + 1)
            continue
        claimed_rows.add(row.id)
        p.item = row
        p.renamed = True
        matched += 1
        logger.info("扫描指纹识别改名/移动: %s -> %s（保留元数据）",
                    row.file_path, p.scan_file.stored_path)
    if matched:
        ctx.stats["renamed"] = ctx.stats.get("renamed", 0) + matched


def _reuse_master_io_futures(pending) -> None:
    """多集文件第二行复用主行的 IO Future（探测/海报/字幕/NFO 只跑一次）。

    主行本轮没提交对应 IO（秒跳/不需要探测/分层跳过）时主行对应属性为
    None，第二行按自己的条件正常提交，保证正确不丢活。
    """
    _mp = getattr(pending, "master_pending", None)
    if _mp is None:
        return
    for attr in ("probe", "side", "nfo"):
        if getattr(_mp, attr, None) is not None and getattr(pending, attr, None) is None:
            setattr(pending, attr, getattr(_mp, attr))


def _maybe_make_multi_episode_pending(ctx, prepared: list, guids: list,
                                      scan_file, pending, parsed: dict,
                                      item_type: str) -> None:
    """多集文件为第二集追加一个 pending（合集文件一文件顶两集）。

    触发条件：解析出 episode_end 且 episode_end > episode（S06E27-E28 /
    S06E27E28 等形态；_1092 的解析保证）。
    第二行：guid = _multi_episode_guid(path, episode_end)（与主行同族不冲突）；
    parsed 拷贝一份只改 episode；series/season 身份与主行一致；
    master_pending 指回主行 pending 供 IO Future 复用。
    """
    ep_end = parsed.get("episode_end")
    if not (item_type == "episode" and ep_end
            and parsed.get("episode") is not None
            and ep_end > parsed["episode"]):
        return
    parsed2 = dict(parsed)
    parsed2["episode"] = ep_end
    guid2 = _multi_episode_guid(scan_file.stored_path, ep_end)
    if guid2 in ctx.seen_guids:
        # 同一文件在本轮已被处理过（遍历器重复产出）：沿用主行的
        # 防重复语义，第二次不再建任务
        ctx.stats["duplicate_files"] = ctx.stats.get("duplicate_files", 0) + 1
        return
    ctx.seen_guids.add(guid2)
    pending2 = _Pending(scan_file=scan_file, guid=guid2,
                       parsed=parsed2, item_type="episode")
    pending2.series_guid = pending.series_guid
    pending2.series_name = pending.series_name
    pending2.season_guid = pending.season_guid
    pending2.multi_episode = True
    pending2.master_pending = pending
    prepared.append(pending2)
    guids.append(guid2)

def _prepare_and_prefetch(db: Session, batch: list, ctx: "_ScanContext", pool) -> list:
    """把一批文件变成「可直接写库」的任务：一次查库 + 并行预取"""
    prepared: list = []
    guids: list = []
    for scan_file in batch:
        guid = item_guid(scan_file.stored_path)
        if guid in ctx.seen_guids:
            # 同一文件在本轮扫描里已经处理过：遍历器偶发产出重复
            # （rclone lsjson 网盘侧重复条目 / 分页异常，见 _CloudMount.walk_media
            # 的注释），第二次不再建任务。否则两个 _Pending 的 item 都为 None，
            # 写库循环里两次 db.add 同 guid，flush 时撞 emby_items.guid 唯一键、
            # 整批回滚、整库扫描 abort（2026-09-25 生产事故）。
            # seen_guids 里已有它，清理阶段不受影响；计数进统计方便事后排查。
            ctx.stats["duplicate_files"] = ctx.stats.get("duplicate_files", 0) + 1
            logger.debug("扫描跳过重复文件：%s", scan_file.stored_path)
            continue
        ctx.seen_guids.add(guid)
        parsed = parse_media_filename(scan_file.stored_path, ctx.snap.collection_type)
        item_type = (
            "episode" if parsed["season"] is not None
            else ("series" if ctx.snap.collection_type == "tvshows" else "movie")
        )
        pending = _Pending(scan_file=scan_file, guid=guid, parsed=parsed, item_type=item_type)
        if item_type == "episode":
            pending.series_guid = _series_guid_of(scan_file)
            pending.series_name = _series_name_of(scan_file, parsed)
            pending.season_guid = item_guid(f"{pending.series_guid}:S{parsed['season']:02d}")
        prepared.append(pending)
        guids.append(guid)

        # 多集文件（如 S06E27-E28）：为第二集单独建一个 pending/行，
        # 两集在播放器里都可见、都能播。第二行 guid 与主行同族但不冲突
        # （见 _multi_episode_guid）；IO（探测/海报/字幕/NFO）在提交阶段
        # 复用主行的 Future，同一文件只跑一次。
        _maybe_make_multi_episode_pending(ctx, prepared, guids, scan_file,
                                          pending, parsed, item_type)

    known = _load_items(db, guids)

    # r60：guid（path 派生）是最强的身份，先挂行；改名识别随后，且不得抢走
    # 已被 guid 认领的行——否则多集迁移场景（旧单行 guid 与主行一致）下，
    # 主行与第二行会双认领同一旧行，第二行永远建不出来（静默失败）。
    claimed_row_ids = _attach_known_items(prepared, known)

    # v2.52.0: guid 没命中的，用 drive_file_id 识别改名/移动（复用已有行）
    _match_renames_by_file_id(db, prepared, known, ctx, claimed_row_ids)

    # v2.53.x: 没有 file_id 的来源（本地目录/115），用 (大小, 解析身份) 指纹
    # 识别改名/移动，保住播放进度与元数据
    _match_renames_by_fingerprint(db, prepared, known, ctx, claimed_row_ids)

    # 外挂字幕一批查齐：秒跳时每个文件都要比对字幕有无变化，不能逐文件查 DB
    ctx.ext_subtitles = _load_external_subtitles(
        db, [it.id for it in known.values() if getattr(it, "id", None)])

    # 剧集层级一次查齐：老实现在写库循环里对**每一集**点查剧集、再点查季（两集一次往返
    # 各一次），十万集就是二十万次查询。这里在批次开头把这一批用到的剧集与季一次取回，
    # 结果按 guid 缓存到本次扫描结束——同一部剧的后续批次连这次查询都省了。
    parent_guids: list = []
    for pending in prepared:
        if not pending.series_guid:
            continue
        if pending.series_guid not in ctx.series_items:
            parent_guids.append(pending.series_guid)
        if pending.season_guid not in ctx.season_items:
            parent_guids.append(pending.season_guid)
    if parent_guids:
        parents = _load_items(db, parent_guids)
        for pending in prepared:
            if not pending.series_guid:
                continue
            row = parents.get(pending.series_guid)
            if row is not None:
                ctx.series_items[pending.series_guid] = row
            row = parents.get(pending.season_guid)
            if row is not None:
                ctx.season_items[pending.season_guid] = row

    # B2 季行随批预取（2026-10-20）：本批剧集用到的 (series_id, season_no)
    # 季行一次查回——写库循环里 _ensure_season_row 不再逐键回退 SELECT，
    # 守住"按批加载"预算护栏。只能覆盖预取时已解析出的 series。
    _prefetch_batch_season_rows(db, ctx, prepared)

    # 增量扫描：先把本批远程目录的列举并发预热（指纹要用，写库循环里的图片/字幕判定
    # 也要用同一份，不会多一次网络往返），再一次取出这些目录的已存指纹
    if _incremental_on(ctx):
        _prefetch_remote_listings(ctx, prepared, pool)
    stored_states = _load_dir_states(
        db, ctx.lib_id, [_dir_key_of(p.scan_file) for p in prepared]
    ) if _incremental_on(ctx) else {}
    # 按源快照层（任务清单第 11 项）：解析到源行的目录以快照为准，
    # 解析不到/开关关闭时两字典为空，循环里一律走旧路径。
    if _incremental_on(ctx) and scan_snapshots.snapshots_enabled():
        _load_snapshot_states(db, ctx, [_dir_key_of(p.scan_file) for p in prepared])
    else:
        ctx.snapshot_resolved = {}
        ctx.snapshot_states = {}

    policy = ctx.snap.scrape_policy
    # r60：_attach_known_items 已在批次开头（改名识别之前）执行，
    # 这里不再重复挂（幂等，但避免歧义）。
    for pending in prepared:
        item = pending.item
        scan_file = pending.scan_file
        is_new = item is None
        # 分层扫描 L1 秒跳：文件指纹是「文件本身变没变」的唯一依据，一次哈希比较。
        # 命中（且 L2/L3 已补全、没被标记重修）→ 本文件本轮零 IO：不算目录指纹、
        # 不提交 probe/side/NFO/TMDB，写库循环里只记 unchanged。
        # 注意顺序：这个检查必须在目录指纹之前——目录指纹要列目录（远程=网络 IO），
        # 文件指纹是纯内存计算。旧 _can_skip_file 保留做兜底（指纹缺失的老数据）。
        file_fp = _file_fingerprint(scan_file)
        pending.file_fingerprint = file_fp
        if (_incremental_on(ctx)
                and _fast_skip_enabled()
                and not is_new
                and not getattr(item, "deleted_at", None)   # 下架过的条目不能秒跳
                and (getattr(item, "file_fingerprint", None) or None) == file_fp
                and (getattr(item, "enrich_status", None) or "pending") == "done"
                and not getattr(item, "repair_requested_at", None)):
            # 外挂字幕是独立于视频的 sidecar：视频没变但字幕增减时不能秒跳，
            # 否则新字幕永远登记不上。用缓存目录列表检查，零额外 IO。
            if _external_subtitles_changed(ctx, item, scan_file):
                # 字幕变了 → 走正常流程；probe 会因视频未变自动跳过，只重做 side
                pass
            else:
                pending.fast_skipped = True
                ctx.stats["unchanged"] = ctx.stats.get("unchanged", 0) + 1
                continue
        # 目录没变、库里这一行也是最新的 → 不做任何逐文件工作（guid 已在上面记进 seen_guids）
        dir_key = _dir_key_of(scan_file)
        fingerprint = _dir_fingerprint(ctx, scan_file) if _incremental_on(ctx) else None
        stored = stored_states.get(dir_key)
        stored_fp = stored[0] if stored else None
        # 按源快照层：解析到源行的目录，快照（指纹 + 项数 + 到期）为准；
        # 无快照行时 stored=None → 判定 first，完整处理一次并写快照。
        listing_count = None
        if dir_key in ctx.snapshot_resolved:
            snap_stored = ctx.snapshot_states.get(dir_key)
            stored = snap_stored
            stored_fp = stored[0] if stored else None
            listing_count = _dir_listing_count(ctx, scan_file) if fingerprint else None
            ctx.snapshot_counts[dir_key] = listing_count
        # 到期调度（批二 #12）：先判这个目录本轮的核对深度。未到期且指纹没变
        # → 只跑安全检查、推迟到期类判定（defer_scrape）；判定为 None（到期层
        # 不参与）或 first/changed/due → 走下面原有的完整判定，一字不差。
        decision = _dir_decision(ctx, dir_key, fingerprint, stored,
                                 entry_count=listing_count) if fingerprint else None
        if decision == "not_due" and _can_skip_file(
                ctx, item, pending, fingerprint, stored_fp, defer_scrape=True):
            pending.skipped = True
            # 计数口径与下面的完整核对跳过不同：那里是「全深度核对过、确认
            # 没变」，照旧 updated + unchanged 各计一次；这里是「未到期、
            # 只跑了安全检查」——没有真核对过内容，只计 unchanged，
            # 保住「updated + unchanged 不重复计同一个文件」的统计口径。
            ctx.stats["unchanged"] = ctx.stats.get("unchanged", 0) + 1
            # 只记判定供查：保留原到期时刻，不许把排期越推越远
            _schedule_dir(ctx, dir_key, "not_due", due_at=stored[1])
            _record_snapshot(ctx, dir_key, "not_due", due_at=stored[1])
            continue
        if _can_skip_file(ctx, item, pending, fingerprint, stored_fp):
            pending.skipped = True
            # updated 照旧计一次（对用户来说这个条目本轮确实重新核对过），
            # 另外单独记 unchanged，方便看“增量到底省了多少”
            ctx.stats["updated"] = ctx.stats.get("updated", 0) + 1
            ctx.stats["unchanged"] = ctx.stats.get("unchanged", 0) + 1
            if decision is not None:
                # 到期重验过但指纹没变：指纹不在 dirty 里，靠排期集推进到期时刻
                _schedule_dir(ctx, dir_key, decision)
                _record_snapshot(ctx, dir_key, decision)
            continue
        if fingerprint:
            ctx.dirty_dirs[dir_key] = fingerprint   # 这个目录这批真处理了 → 提交时写回指纹
        if decision is not None:
            _schedule_dir(ctx, dir_key, decision)   # 完整处理过 → 重新排期
            _record_snapshot(ctx, dir_key, decision)
        pending.probe_needed = bool(is_new or needs_probe(item, scan_file.stored_path,
                                                             scan_file.size))
        if SCAN_LAYERED and not pending.fast_skipped:
            # 分层 L1：本机文件只跳过 TMDB（side/NFO 是本地磁盘 IO，很快，照常做，
            # 保证本地海报扫描完立即可见）；远程文件跳过全部 IO（网络慢，后台补）。
            # 写库时 enrich_status='pending'，enrich_worker 随后补完剩下的。
            pending.layered = True
            # SCAN_FORCE_REMOTE=1 时强制按远程处理（FUSE 有本地路径但 IO 慢）
            pending.layered_local_fast = bool(scan_file.local_dir) and not SCAN_FORCE_REMOTE
        # 多集第二行：复用主行的 IO Future（探测/海报/字幕/NFO 只跑一次）。
        # 主行本轮没提交对应 IO（秒跳/不需要探测/分层跳过）时对应属性为 None，
        # 下面的提交分支按第二行自己的条件正常提交，保证正确不丢活。
        _reuse_master_io_futures(pending)
        if pending.probe_needed and pending.probe is None:
            if PROBE_BACKGROUND:
                # Phase 1：不提交 ffprobe（连 probe_input 的直链解析都省了），
                # 写库时把条目标 probe_status='pending' 交给后台 worker。
                pending.probe_deferred = True
            elif pool is not None and (not pending.layered or getattr(pending, "layered_local_fast", False)):
                # inline 模式当场探测：分层下本机文件也探（本地 ffprobe 很快），
                # 只有远程文件才推迟（直链解析要走网络）。
                # 多集第二行（master_pending 非空）且主行已提交探测时，
                # 上面已复用主行的 Future，这里不再重复提交。
                pending.probe = _submit(pool, probe_metadata, *scan_file.probe_input(), size=scan_file.size)
            # 分层 + inline + 远程：L1 不探，enrich_worker 补全时把 probe_status
            # 置 pending，probe_worker 接手
        if (pending.side is None and pool is not None
                and (not pending.layered or getattr(pending, "layered_local_fast", False))):
            pending.side = _submit(pool, _side_info, ctx, scan_file)
        # NFO 发现与解析（与 probe / side / TMDB 并行；结果在下面先取回，
        # TMDB 预取口径按 NFO 有无决定：有 tmdb_id 就不调搜索）
        if (pending.nfo is None and pool is not None
                and pending.item_type in ("series", "movie", "episode")
                and (not pending.layered or getattr(pending, "layered_local_fast", False))):
            pending.nfo = _submit(pool, _nfo_work, ctx, scan_file, pending.item_type)

    # NFO 先取回（小文件 IO），再决定 TMDB 预取口径——搜索是扫描里最贵的网络调用，
    # 远端已有 NFO 刮削的条目直接跳过它（B 方案：TMDB 只在缺图/缺详情时调详情接口）。
    for pending in prepared:
        if pending.skipped or pending.fast_skipped or pending.layered:
            continue
        nfo_data, series_nfo_data, season_nfo_data = (
            _result(pending.nfo, default=(None, None, None)) or (None, None, None)
        )
        pending.nfo_data = nfo_data
        pending.series_nfo_data = series_nfo_data
        pending.season_nfo_data = season_nfo_data
        if pending.item_type not in ("series", "movie", "episode"):
            continue
        item = pending.item
        is_new = item is None
        needs_repair = bool(getattr(item, "repair_requested_at", None))
        if pending.item_type in ("series", "movie"):
            kind = "series" if pending.item_type == "series" else "movie"
            nfo_id = (nfo_data or {}).get("tmdb_id")
            if nfo_id:
                # NFO 命中且有 tmdb_id：不调 TMDB 搜索；只在缺图/缺 imdb/别名/
                # 带补图标记时取详情（补图用）
                need_search = False
                existing_id = str(nfo_id)
                want_details = bool(
                    needs_repair
                    or not ((item.poster_path if item else None)
                            or (item.primary_image_url if item else None))
                    or not ((item.imdb_id if item else None)
                            and (item.aliases if item else None))
                )
            else:
                need_search = is_new or needs_repair or should_scrape(item, policy)
                existing_id = getattr(item, "tmdb_id", None)
                need_details = bool(existing_id) and bool(
                    needs_repair or not (item.imdb_id and item.aliases)
                )
                # 详情预取的口径必须**覆盖**写库那一步可能用到的所有情况：新条目（补 IMDb /
                # 别名）、带补图标记的条目、已有 tmdb_id 但缺 IMDb Id / 多别名的条目，
                # 以及「这一轮会重刮、而库里还缺这两项」的条目——后者 apply() 之后
                # tmdb_id 才被写上，写库那一步就会去要详情。
                # 少预取一次，写库那一步就会在事务里发一次网络请求（见 _tmdb_work）。
                want_details = bool(
                    is_new or needs_repair or need_details
                    or (should_scrape(item, policy) and not (item.imdb_id and item.aliases))
                )
            if pool is not None and (need_search or want_details):
                pending.tmdb = _submit(pool, 
                    _tmdb_work, need_search, pending.parsed["name"], pending.parsed["year"],
                    kind, existing_id, want_details,
                )
        else:  # episode：剧集级 NFO 的 TMDB 详情只补图，同一部剧一批只取一次
            series_id = (series_nfo_data or {}).get("tmdb_id")
            if series_id and pending.series_guid not in ctx.nfo_series_imaged:
                ctx.nfo_series_imaged.add(pending.series_guid)  # 占位去重：跨批次不重复取
                s_item = ctx.series_items.get(pending.series_guid)
                if pool is not None and (s_item is None or not (s_item.poster_path or s_item.primary_image_url)):
                    pending.series_tmdb = _submit(pool, 
                        _tmdb_work, False, "", None, "series", str(series_id), True,
                    )
            elif (not series_id and _tmdb.tmdb_client.configured
                    and pending.series_guid not in ctx.series_tmdb_searched):
                # B 方案补全：episode 隐式创建的 series 平时走不到 TMDB 搜索，
                # NFO 里又没有 tmdb_id 时，按剧名搜一次（TMDB 只补图与缺的字段，
                # NFO 文本随后在写库循环里覆盖，口径与顶层 series 分支一致）
                s_item = ctx.series_items.get(pending.series_guid)
                # 只给“从没走过 TMDB”的剧搜一次：干净落地后记 last_scraped_at，
                # 命中与否都不再重复——否则配了 TMDB 就每轮重做全集（v2.31.0）。
                # 注意这里故意不用 should_scrape：它对无 tmdb_id 的条目恒为 True。
                if s_item is None or (not s_item.tmdb_id and not s_item.last_scraped_at):
                    ctx.series_tmdb_searched.add(pending.series_guid)  # 占位去重：跨批次不重复搜
                    pending.series_tmdb = _submit(pool, 
                        _tmdb_work, True, pending.parsed["name"],
                        pending.parsed["year"], "series", None, True,
                    )

    # 把这一批的 IO 结果**全部取回**，然后才把写库交给调用方。
    # 这是「事务只包纯 DB 写」的关键一步：ffprobe / rclone 列目录 / TMDB 搜索都可能在
    # 这里等上几秒（远程挂载与刮削都是网络），而调用方拿到任务后立刻开始写库——
    # 如果等到写库循环里再取结果，SQLite 的写锁就要陪着一起等（超过 busy_timeout=30s
    # 就是那句 database is locked，前端表现是 30 秒超时）。
    for pending in prepared:
        if pending.skipped or pending.fast_skipped:
            continue
        pending.probe_data = _result(pending.probe)
        pending.side_data = _result(pending.side)
        if pending.layered:
            # 分层 L1：side/probe 结果已取回（本机文件的 side 在上面提交了），
            # TMDB 等慢活跳过，等 enrich_worker。
            continue
        hit, details = _result(pending.tmdb, default=(None, None)) or (None, None)
        pending.tmdb_hit, pending.tmdb_details = hit, details
        series_hit, series_details = _result(pending.series_tmdb, default=(None, None)) or (None, None)
        if series_details is not None:
            ctx.nfo_series_details[pending.series_guid] = series_details
        if series_hit is not None:
            # episode 分支为隐式 series 做的 TMDB 搜索命中，留给写库循环落到 series 上
            ctx.series_tmdb_results[pending.series_guid] = (series_hit, series_details)
        # 图片也在这里落盘（写事务之外）：写库循环里的 apply_images / apply 只落字段
        # （见 tmdb._set_image 的 allow_download=False）。剧集详情同样要预热。
        prewarm_images(hit, details, series_hit, series_details)
        if pending.series_tmdb is not None and isinstance(pending.series_tmdb, Future):
            # 搜索任务本身抛异常了：别记“已搜过”，下次扫描重来
            # （_result 上面已经等过 future，这里 exception() 不会阻塞）
            if pending.series_tmdb.exception() is not None:
                ctx.series_tmdb_searched.discard(pending.series_guid)
    return prepared


def _maybe_yield_for_foreground() -> None:
    """扫描批次边界的前台让路（2026-10-16 网页加速批）。

    用户在刷网页/后台时，aetrix-api 的限流中间件会往 Redis 打点；
    这里读到近期有前台活跃就小睡几轮，把 CPU 让出来。只延迟、不丢活；
    Redis 不可用或开关关闭时行为与旧版完全一致。永不抛异常。
    """
    try:
        from backend.emby_server import foreground_activity as _fg

        _fg.maybe_yield_for_foreground(tag="scan")
    except Exception:
        pass


def _yield_safely() -> None:
    """批次边界让路的安全包装：钩子任何情况下都不许打断扫描主循环。"""
    try:
        _maybe_yield_for_foreground()
    except Exception:
        pass


def _iter_prepared(ctx: "_ScanContext", files, pool, db: Session, process_one,
                   label: str, counted: "_FileCounter", stats: dict, failed_roots: list) -> None:
    """按批次把文件流变成待写库的任务，并直接驱动写库回调。

    - 每批 SCAN_BATCH 个文件只查一次库，不再“每个文件查一两次”；
    - 探测 / 目录列举 / TMDB 在同一批内并行预热，写库线程随后取用不再等网络；
    - 每批结束只提交一次：几百个文件一次 fsync，会话也不会随库变大而堆积对象；
    - 增量扫描命中的文件（``pending.skipped``）不交给写库回调（本来就没活要干），
      并在这批提交之后把处理过的目录指纹写回（与条目同一个事务）。
    - 撞 PG 死锁时整批回滚重试（抖动退避，最多 3 次）：重试边界必须包住写库
      回调——死锁发生在回调里的 flush（db.commit 在本函数内被替换成 flush），
      生成器 yield 抓不住消费方帧抛出的异常，所以这里用回调驱动而不是 yield。
      重试会把本批重新走一遍写回调，stats 可能轻微多计（小概率事件，可接受）。
    """
    pool = _scan_pool()  # 用进程级线程池（参数里的 pool 只作兼容，不再单独建池）
    batch: list = []
    real_commit = db.commit
    db.commit = db.flush  # 只改本 Session 实例：写回调里的“每文件一次提交”退化成 flush

    def commit_batch() -> None:
        """批次提交：撞上别人占着写锁（另一台 EA / 备份 / checkpoint）时退避重试三次

        主体里之所以敢把「每文件一次提交」退化成 flush，是因为这里一批只提交一次；
        代价是这一批的写事务期间不能有任何 IO（见 _prepare_and_prefetch 的说明）。

        唯一键冲突（IntegrityError）单独处理：唯一约束撞了说明这一批里某个 guid
        被**另一个 Session** 抢先插入了——批次开头的 _load_items 与此刻之间存在窗口。
        这种情况**不能让整批、整库失败**：2026-10-04 生产事故里，一个文件让国产剧
        整库反复扫描失败。所以这里只回滚到 SAVEPOINT、保住这一批其余的写入，
        把冲突的那一批降级为「重查 + 改写」。
        """
        try:
            retry_write(real_commit, label="扫描批次提交")
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            logger.warning(
                "扫描批次提交撞唯一键，回滚本批写入后改为逐条重写：%s",
                str(exc).splitlines()[0][:200])
            db.rollback()
            return "conflict"

    def emit_batch(current: list) -> None:
        """准备并提交一批；撞唯一键时整批重走一次（第二次会走 update 分支）。

        重试必须把本批的 guid 从 ``ctx.seen_guids`` 里摘掉，否则 _prepare_and_prefetch
        会把它们全当「遍历器重复产出」跳过，重试等于什么都没做——那样静默丢条目，
        比报错更难查。所以这里按本批实际入队的 guid 精确回退。

        唯一键冲突只重试一次：第二次还撞说明不是竞态而是真有重复数据，让它抛出去、
        整库失败留下证据，比无限重试诚实。PG 死锁单独给 3 次重试预算。
        """
        before = set(ctx.seen_guids)
        conflict_retries = 0
        deadlock_retries = 0
        while True:
            prepared = [p for p in _prepare_and_prefetch(db, current, ctx, pool)
                        if not p.skipped and not p.fast_skipped]
            # 批量占位 + 一次取回：写回调里每个新文件直接用预热好的 ORM 对象，
            # 不再产生逐文件 SELECT（资源预算）
            _claim_batch_guids(db, prepared, ctx)
            try:
                for p in prepared:
                    process_one(p.scan_file, p)
                _store_dir_states(db, ctx)   # 增量扫描：这批真处理过的目录指纹随本次提交写回
                _store_snapshots(db, ctx)      # 按源快照：与指纹同事务写回
                outcome = commit_batch()
            except OperationalError as exc:
                # PG 死锁（40P01）：enrich worker 的批量认领
                # ``UPDATE emby_items ... WHERE id IN (...)`` 与扫描的逐行
                # ``UPDATE emby_items ... WHERE id = ...`` 并发时，行锁获取顺序
                # 相反 → 循环等待，PG 会 abort 其中一个事务（生产 run 1981/2077）。
                # PG 已经回滚了本事务：抖动退避后整批重做即可（幂等：guid 占位是
                # upsert，重查会命中已提交的行；回滚过的行占位会重新拿到）。
                if not is_deadlock(exc) or deadlock_retries >= 3:
                    raise
                db.rollback()
                deadlock_retries += 1
                deadlock_backoff(deadlock_retries)
                logger.warning(
                    "扫描批次撞 PG 死锁，已回滚整批，抖动退避后第 %d/3 次重试",
                    deadlock_retries)
                # 回滚本批占过的 guid，让重试能重新准备这些文件
                ctx.seen_guids.difference_update(ctx.seen_guids - before)
                continue
            if outcome != "conflict":
                return
            # 回滚本批占过的 guid，让重试能重新准备这些文件
            ctx.seen_guids.difference_update(ctx.seen_guids - before)
            conflict_retries += 1
            if conflict_retries >= 2:
                # 第二次还撞唯一键：不是竞态而是真有重复数据，抛出去留证据
                raise AssertionError("unreachable — conflict 重试一次后仍冲突")

    # _attribute_source 的按来源记账逻辑内联于此（原先包在生成器外层）
    sink = stats.setdefault("sources", [])
    before_stats = {key: stats.get(key) or 0 for key in SCAN_SOURCE_COUNTERS}
    try:
        for scan_file in files:
            batch.append(scan_file)
            if len(batch) < SCAN_BATCH:
                continue
            # 批次边界检查库还在不在：被删了就停手，不再往已删库插条目
            if not _library_exists(db, ctx.lib_id):
                logger.warning("媒体库 %s 在扫描期间被删除，扫描提前结束", ctx.lib_id)
                return
            # 前台让路（2026-10-16 网页加速批）：批次边界事务是干净的
            # （上一批已提交，本批文件还只是内存对象），此时睡眠不持有 DB 锁。
            _yield_safely()
            emit_batch(batch)
            batch = []
        if batch:
            _yield_safely()
            emit_batch(batch)
    finally:
        # 正常结束、中途报错都要恢复真实提交
        db.commit = real_commit
        sink.append(_source_entry(label, before_stats, stats, counted.total, failed_roots))
        # 线程池是进程级的（_scan_pool）：扫描结束不销毁，也不影响后面的扫描



def _purge_items(db: Session, item_ids: list) -> None:
    """批量删条目与从属数据（用批量语句：不逐条加载 ORM 对象，也不触发级联查询）

    `MediaStream` 在 ORM 上有级联会被一起带走，但 `UserMediaData`
    （播放进度 / 收藏）**没有级联**——只 `db.delete(item)` 会留下永远指向不存在条目的
    孤儿行；这类行只增不减，追新久了就是几十万条垃圾，也是「跑久了变慢」的来源之一。
    """
    ids = list(item_ids)
    # 媒体信息 JSON 清理（对标 DeleteMediaInfoJson）：先取 guid 再删库，
    # 否则删库后算不出 JSON 路径，/data/mediainfo 只增不减
    try:
        from types import SimpleNamespace
        from backend.emby_server import mediainfo_persist as _mip
        _guids = [
            r[0] for r in
            db.query(emby_models.MediaItem.guid)
            .filter(emby_models.MediaItem.id.in_(ids))
            .all()
        ]
        for _g in _guids:
            try:
                _mip.delete_json(SimpleNamespace(guid=_g))
            except Exception:
                pass
    except Exception:
        pass
    db.query(emby_models.UserMediaData).filter(
        emby_models.UserMediaData.item_id.in_(ids)
    ).delete(synchronize_session=False)
    db.query(emby_models.MediaStream).filter(
        emby_models.MediaStream.item_id.in_(ids)
    ).delete(synchronize_session=False)
    db.query(emby_models.ItemFacet).filter(
        emby_models.ItemFacet.item_id.in_(ids)
    ).delete(synchronize_session=False)
    # v2.51.0 演员表：emby_people.item_id 外键指向 emby_items，不清掉这里
    # PG 会报 FK 违反（与上面三个从属表同口径：批量删，不逐条）。
    db.query(emby_models.EmbyPerson).filter(
        emby_models.EmbyPerson.item_id.in_(ids)
    ).delete(synchronize_session=False)
    db.query(emby_models.MediaItem).filter(
        emby_models.MediaItem.id.in_(ids)
    ).delete(synchronize_session=False)


def _no_removals_possible(db: Session, library, seen_guids: set) -> bool:
    """快速路径：一眼判断「这一轮不可能有要清理的条目」

    三个条件都成立才返回 True，都只是计数查询（走索引），比遍历整库便宜两个数量级：

    1. **文件类条目数 == 本次扫描见到的文件数**：说明每个文件都有行，库里也没有多余的
       （来源里已经没有的）文件类条目；
    2. **剧/季行数 == 被引用的剧数 + 被引用的季数**：说明没有「没有子条目的剧或季」残留
       （剧被 season/episode 的 ``series_id`` 引用，季被 episode 的 ``parent_id`` 引用）；
    3. **没有挂着缺失计数的条目**（批一 #6 缺失宽限）：有的话遍历要负责清零/累计，
       不能跳过。

    任何一个不成立就退回遍历——**宁可多花时间，也不能漏掉该删的条目**。
    实测十万条目（剧集库，稳定无删除）：这里约 40 ms，遍历整库约 2.2 s。
    """
    # 一条查询同时拿文件类条目数与「挂着缺失计数的条目数」（批一 #6）：
    # 快速路径的立身之本是便宜，不许为宽限多加一次往返（资源预算冒烟
    # 对条目表 SELECT 次数有硬上限）。missing_count 只会落在 movie/episode
    # 上（宽限只管文件类条目），折进这条计数不丢信息。
    _fb_row = db.execute(
        text(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN missing_count > 0 THEN 1 ELSE 0 END), 0) "
            "FROM emby_items "
            "WHERE library_id = :lib AND item_type IN ('movie', 'episode') "
            "AND deleted_at IS NULL"
        ),
        {"lib": library.id},
    ).first()
    file_backed = int(_fb_row[0] or 0)
    pending_grace = int(_fb_row[1] or 0)
    if file_backed != len(seen_guids):
        return False                      # 条数与文件数对不上：可能有已消失的条目
    # 缺失宽限（批一 #6）：有条目正挂着缺失计数时不能走快速路径——
    # 遍历要负责给「文件回来了」的条目清零、给「还缺着」的累计计数；
    # 跳过遍历会让计数永远停在旧值，下一次单轮缺失就直接越线下架。
    # 必须放在下面两个提前 return 之前查，否则电影库/空库永远轮不到。
    if pending_grace:
        return False
    if not file_backed:
        return True                       # 空库：本来就没有可清理的
    parents = int(db.execute(
        text(
            "SELECT COUNT(*) FROM emby_items "
            "WHERE library_id = :lib AND item_type IN ('series', 'season') "
            "AND deleted_at IS NULL"
        ),
        {"lib": library.id},
    ).scalar() or 0)
    if not parents:
        return True                       # 电影库：没有剧/季要收拾
    refs_series = int(db.execute(
        text(
            "SELECT COUNT(DISTINCT series_id) FROM emby_items "
            "WHERE library_id = :lib AND item_type IN ('episode', 'season') "
            "AND series_id IS NOT NULL AND deleted_at IS NULL"
        ),
        {"lib": library.id},
    ).scalar() or 0)
    refs_season = int(db.execute(
        text(
            "SELECT COUNT(DISTINCT parent_id) FROM emby_items "
            "WHERE library_id = :lib AND item_type = 'episode' "
            "AND parent_id IS NOT NULL AND deleted_at IS NULL"
        ),
        {"lib": library.id},
    ).scalar() or 0)
    return parents == refs_series + refs_season


def _evict_gone_drive_files(db, library, gone: list) -> None:
    """来源里已经没有的 Drive 文件：摘掉播放侧存在性正缓存。

    播放在 EA 容器，扫描在别的容器，失效走 Redis 墓碑（见 api.invalidate）。
    任何失败都不许打断清理。
    """
    if not gone:
        return
    try:
        from types import SimpleNamespace

        from backend.emby_server.api import invalidate_drive_exists_for_item
    except Exception:  # noqa: BLE001
        logger.debug("加载 Drive 存在性缓存失效入口失败", exc_info=True)
        return
    for file_path, file_id in gone:
        try:
            invalidate_drive_exists_for_item(
                db, SimpleNamespace(
                    file_path=file_path or "",
                    drive_file_id=file_id,
                    library=library,
                ))
        except Exception:  # noqa: BLE001
            logger.debug("扫描失效存在性缓存失败 file=%s", file_id, exc_info=True)
        # 批一 #1：同点位连带清解析结果缓存——文件已确认不在，解析快照
        # 不许再留（否则 TTL 内播放还会拿到这条已消失文件的直链）。
        try:
            from backend.emby_server.mounts import (
                invalidate_resolve_cache_for_path)
            invalidate_resolve_cache_for_path(file_path)
        except Exception:  # noqa: BLE001
            logger.debug("扫描失效解析缓存失败 file=%s", file_id, exc_info=True)


def _apply_missing_resets(db: Session, item_ids: list[int]) -> None:
    """缺失计数清零（批一 #6）：文件重新出现的条目，旧账一笔勾销。"""
    for i in range(0, len(item_ids), SQL_IN_CHUNK):
        chunk = item_ids[i:i + SQL_IN_CHUNK]
        db.execute(
            update(emby_models.MediaItem)
            .where(emby_models.MediaItem.id.in_(chunk))
            .values(missing_count=0, missing_since=None)
        )


def _apply_missing_grace(db: Session, item_ids: list[int]) -> None:
    """缺失计数 +1（批一 #6）：首次缺失记下时刻，后续只累计次数。

    一条 SQL 统一自增：每个 id 本轮至多出现一次（遍历按 id 单趟），
    不存在同批重复累计。
    """
    now = datetime.now()
    for i in range(0, len(item_ids), SQL_IN_CHUNK):
        chunk = item_ids[i:i + SQL_IN_CHUNK]
        db.execute(
            update(emby_models.MediaItem)
            .where(emby_models.MediaItem.id.in_(chunk))
            .values(
                missing_count=emby_models.MediaItem.missing_count + 1,
                missing_since=func.coalesce(emby_models.MediaItem.missing_since, now),
            )
        )


def _remove_missing_items(db: Session, library, seen_guids: set) -> int:
    """清理「来源里已经没有」的条目，返回删除条数

    三个刻意的选择：

    - **快速路径**：稳定库（每个文件都有行、没有多余行、没有无子条目的剧/季）根本不必
      遍历整库，两次计数就能证明「没有可清理的条目」；任何一条对不上就退回遍历。
    - **游标分批**：老实现 `db.query(...).all()` 把整库条目一次性读进内存——十万级库
      就是几十万个 ORM 对象，正是「扫描把机器拖垮」的主因。现在按 id 递增读取，
      且只取 4 个列（Core 语句，不构造 ORM 实体），删除也不会让游标卡住（每批只往后走）。
    - **软删除优先**（v2.48.0）：默认只写 `deleted_at`（见 `soft_delete`），从属数据
      全部留着——误删一部片还能连播放进度与收藏一起回来。物理删除仍会做，但只针对
      「已经隐藏很久」的条目（`MEDIA_SOFT_DELETE_PURGE_DAYS`，默认 30 天），
      否则表会无限涨。把 `MEDIA_SOFT_DELETE=0` 就完全回到硬删。
    - **缺失宽限**（2026-10-24 批一 #6）：确认缺失先累计 `missing_count`，
      连续缺失 `SCAN_MISSING_GRACE_SCANS` 轮（默认 3）才下架；文件回来即清零。
      与删除保护分工：保护管整库级事故，宽限管单条级抖动。
    """
    global _CLEANUP_LAST
    started = time.perf_counter()
    _CLEANUP_LAST = {"fast_path": False, "walked": 0, "removed": 0, "graced": 0,
                     "elapsed_ms": 0.0, "skipped": ""}
    if _no_removals_possible(db, library, seen_guids):
        elapsed = (time.perf_counter() - started) * 1000
        _CLEANUP_LAST.update(fast_path=True, elapsed_ms=elapsed)
        logger.info(
            "清理阶段：%d 个文件与库内条目一一对应，无需遍历整库（%.0f ms）",
            len(seen_guids), elapsed,
        )
        return 0
    # 删除保护：先过预算，再开始删（预算不够则一条都不删）
    allowed, why, _total = _removal_budget(db, library, seen_guids)
    if not allowed:
        _CLEANUP_LAST.update(skipped=why, walked=0, removed=0,
                             elapsed_ms=(time.perf_counter() - started) * 1000)
        logger.error("清理阶段已中止（删除保护）：%s", why)
        return 0
    removed = 0
    graced = 0
    walked = 0
    last_id = 0
    while True:
        rows = db.execute(
            select(
                emby_models.MediaItem.id,
                emby_models.MediaItem.guid,
                emby_models.MediaItem.item_type,
                emby_models.MediaItem.file_path,
                emby_models.MediaItem.drive_file_id,
                emby_models.MediaItem.missing_count,
            )
            .where(
                emby_models.MediaItem.library_id == library.id,
                emby_models.MediaItem.id > last_id,
                # 已经下架的行不用再看：全局可见性过滤只管 ORM 查询，Core 语句要自己带
                emby_models.MediaItem.deleted_at.is_(None),
            )
            .order_by(emby_models.MediaItem.id)
            .limit(CLEANUP_BATCH)
        ).all()
        if not rows:
            break
        last_id = rows[-1][0]
        walked += len(rows)
        doomed: list[int] = []
        gone_drive: list[tuple] = []
        grace_ids: list[int] = []
        reset_ids: list[int] = []
        for item_id, guid, item_type, file_path, drive_file_id, missing_count in rows:
            missing_count = missing_count or 0
            if guid in seen_guids:
                # 文件回来了：缺失计数清零（批一 #6），别让旧账害它下次单轮缺失就越线
                if missing_count > 0:
                    reset_ids.append(item_id)
                continue
            if item_type in ("movie", "episode"):
                # 本机路径与远程挂载都查：命中的说明是别的来源的文件，保留；
                # 查不到（挂载已删/已停用）才当删除处理。
                if file_path and mount_lib.media_exists(file_path, db, library):
                    if missing_count > 0:
                        reset_ids.append(item_id)  # 文件在别的来源活着，同样清零
                    continue  # 其他来源（路径 / 挂载）的文件
                # 确认缺失：先过宽限（批一 #6），连续缺失达到阈值才下架；
                # 宽限期内条目保持可见——单轮抖动不作数（魂：对外部变化克制）。
                if missing_count + 1 >= MISSING_GRACE_SCANS:
                    doomed.append(item_id)
                    fid = (drive_file_id or "").strip()
                    if fid:
                        # 真正判缺了才摘存在性正缓存：宽限期内不摘，
                        # 文件可能只是暂时读不到，摘了会害播放白查一趟来源
                        gone_drive.append((file_path, fid))
                else:
                    grace_ids.append(item_id)
            elif item_type == "season":
                has_children = db.query(emby_models.MediaItem.id).filter(
                    emby_models.MediaItem.parent_id == item_id
                ).first()
                if not has_children:
                    doomed.append(item_id)
            elif item_type == "series":
                has_children = db.query(emby_models.MediaItem.id).filter(
                    emby_models.MediaItem.series_id == item_id
                ).first()
                if not has_children:
                    doomed.append(item_id)
        if reset_ids:
            _apply_missing_resets(db, reset_ids)
        if grace_ids:
            _apply_missing_grace(db, grace_ids)
            graced += len(grace_ids)
        if gone_drive:
            _evict_gone_drive_files(db, library, gone_drive)
        if doomed:
            if _soft_delete.soft_delete_enabled():
                # 只标记：从属数据（播放进度 / 收藏 / 分面）留着，文件回来就能恢复
                # r55：守卫 fail-closed 跳过的行不计入 removed（之前用 len(doomed) 虚高）
                removed += _soft_delete.mark_soft_deleted(db, doomed)
            else:
                _purge_items(db, doomed)
                removed += len(doomed)
        if doomed or grace_ids or reset_ids:
            db.commit()  # 一批一次提交：与扫描主体同一套资源口径
    if _soft_delete.soft_delete_enabled():
        try:
            _soft_delete.purge_expired(db, library_id=library.id)
        except Exception as exc:  # noqa: BLE001 — 回收失败不影响本轮清理结果
            db.rollback()
            logger.warning("软删除回收失败 library_id=%s: %s", library.id, exc)
    _CLEANUP_LAST.update(walked=walked, removed=removed, graced=graced,
                         elapsed_ms=(time.perf_counter() - started) * 1000)
    return removed


# ==================== 扫描结果与流水（落库 + 对外可查）====================
# 扫描统计以前只在返回值与日志里：管理端刷新一下就没了，「上一轮到底扫到什么」只能去
# 服务器日志翻。这里做两层：
#
# - **最近一次**写回 Library（scan_status / scan_stats / scan_error），由
#   /api/admin/emby/libraries 一并带回（见 scan_result_payload）——刷新页面就能看；
# - **最近若干轮**记进 ScanRun 流水（状态 / 触发方 / 耗时 / 同一份统计 / 失败原因），
#   由 /api/admin/emby/libraries/{id}/scans 提供（见 scan_runs_payload）——
#   「这个库每轮都失败」和「只是最近一轮失败」是两件事，只看最后一次分不出来。
SCAN_STATUS_RUNNING = "running"
SCAN_STATUS_SUCCESS = "success"
SCAN_STATUS_PARTIAL = "partial"   # 有来源读不到：本轮已跳过清理
SCAN_STATUS_FAILED = "failed"
SCAN_STATUS_INTERRUPTED = "interrupted"   # 被进程重启打断（不是失败）：启动维护会收尾成这个状态并自动续扫

# 谁触发的扫描（写进流水：回答「半夜三点是谁在扫」）：
# manual = 面板按钮/一键扫描 · client = Emby 客户端刷新 · node = 归属节点转发 ·
# repair = 面板修复端点 · auto = 定时扫描守护 · fs-event = 文件系统监听 ·
# drive-changes = Drive 变更守护 · chase-new = 追新守护 · strm-gen = 生成器收尾触发 ·
# rescrape = 面板重刮任务（api/admin_scrape.py）·
# resume = 启动维护自动续扫被进程重启打断的那一轮（maintenance.resume_interrupted_scans）。
# 注意：新增触发值必须同步加到这个元组里——2026-10-14 的 enrich 大翻转事故里，
# chase-new/drive-changes/strm-gen 等 6 个值因为不在白名单里被 normalize 成 manual，
# 14 天 222 个 run 全标 manual，复盘时误读为"有人在点扫描"。
SCAN_TRIGGERS = (
    "manual", "client", "node", "repair",
    "auto", "fs-event", "drive-changes", "chase-new", "strm-gen", "rescrape",
    "resume",
)

# 每个媒体库保留多少轮扫描流水（0 = 不记流水）。一轮一行，超过就按库回收最旧的行——
# 量很小，但没人清就会随「建库→删库」和长期运行一直涨。
SCAN_RUN_KEEP = env_int("EMBY_SCAN_HISTORY", 20, 1, 10000)

# 需要长期保留的统计键：其余键本来只是内部状态，不进库
SCAN_STATS_KEYS = ("added", "updated", "removed", "probed", "probe_queued", "scraped",
                   "repaired", "unchanged", "resurrected", "removal_skipped",
                   # 到期调度（批二 #12）：目录判定计数，不进白名单落不了库
                   "dirs_new", "dirs_changed", "dirs_due", "dirs_not_due",
                   "failed_roots", "duplicate_files", "duration_ms", "sources",
                   # 删除保护拦截原因：finish_scan 的 error 兜底链 + _scan_metrics
                   # 对外口径都依赖它，不进白名单就落不了库（v2.55.x 修复）
                   "removal_guard",
                   # 定向扫描标记：清理跳过是预期行为，finish_scan 据此不判 partial
                   #（v2.56.x 修复"没有可用来源"误报）
                   "targeted_scan",
                   # 定向扫描的范围（limit_prefixes）：begin_scan 开跑时记下，
                   # 被重启打断后自动续扫据它定向续、不放大成整库遍历；
                   # 只在流水 stats 里流转，_scan_metrics 不提取、不对外
                   "resume_scope")


def normalize_scan_trigger(trigger: Optional[str]) -> str:
    """把触发方收敛到枚举内。

    已知触发值（SCAN_TRIGGERS）原样保留落库；未知值当作 manual——
    调用方写错触发名也不能因此丢一条扫描流水（原设计意图，保留）。
    """
    value = (trigger or "").strip().lower()
    return value if value in SCAN_TRIGGERS else "manual"


# 需要跟着一轮扫描一起落库的来源计数器
SCAN_SOURCE_COUNTERS = ("files", "added", "updated", "probed", "scraped", "repaired", "unchanged")
SCAN_SOURCES_MAX = 50              # 一个库的来源（路径 + 挂载）远小于这个量级；超了只留前 N 条
SCAN_SOURCE_LABEL_MAX = 200
SCAN_SOURCE_TEXT_MAX = 300


class _FileCounter:
    """数一条来源里**发现**了多少媒体文件

    必须放在来源流的最外层：``_iter_prepared`` 会跳过增量扫描命中的文件（未变化，不交给
    写库循环），按「写库条数」计的话，一个全都没变的来源会显示成 0 个文件——那是假象。
    """

    def __init__(self, files):
        self.total = 0
        self._files = files

    def __iter__(self):
        for item in self._files:
            self.total += 1
            yield item


def _attribute_source(label: str, counted: "_FileCounter", prepared,
                      stats: dict, failed_roots: list) -> Iterator[tuple]:
    """把一条来源的发现数与它在处理期间对总计的改动记到它名下

    两件事各归各位：

    - **发现数**来自 ``counted``（来源流最外层），见 _FileCounter；
    - **各计数器增量**在这一层收尾时算差值——不在扫描循环里逐条累加，循环体一个字都不用改
      （那是最容易被改坏的地方），而且中途抛错时记下的也是真实的**部分**结果。
      放在里层（来源流上）会提前收尾，最后一批的改动会被算到下一条来源头上。
    """
    sink = stats.setdefault("sources", [])
    before = {key: stats.get(key) or 0 for key in SCAN_SOURCE_COUNTERS}
    try:
        yield from prepared
    finally:
        sink.append(_source_entry(label, before, stats, counted.total, failed_roots))


def _source_entry(label: str, before: dict, stats: dict, seen: int,
                  failed_roots: list) -> dict:
    """一条来源的明细：文件数 + 各计数器增量 + 该来源自己的失败原因（若有）"""
    entry: dict = {"label": label, "files": seen}
    for key in SCAN_SOURCE_COUNTERS:
        if key == "files":
            continue
        current = stats.get(key)
        entry[key] = max(0, int(current or 0) - int(before.get(key) or 0)) if current is not None else 0
    prefix = f"{label}: "
    errors = [str(r)[len(prefix):] for r in failed_roots if str(r).startswith(prefix)]
    if errors:
        entry["error"] = "；".join(dict.fromkeys(errors))
    return entry


def _encode_scan_sources(entries) -> list:
    """来源明细 → 可落库结构（条数、标签与原因都封顶：库里的 JSON 不该无限长）

    单条坏数据只丢它自己，不影响整轮统计（统计里带着扫描结论，不能因为一条来源烂掉全没）。
    """
    out = []
    for entry in list(entries or [])[:SCAN_SOURCES_MAX]:
        if not isinstance(entry, dict):
            continue
        try:
            label = str(entry.get("label") or "").strip()[:SCAN_SOURCE_LABEL_MAX]
            if not label:
                continue  # 连来源都说不清的明细没有意义，别让它在面板上占一行
            item = {"label": label}
            for key in ("kind", "error"):
                if entry.get(key):
                    item[key] = str(entry[key])[:SCAN_SOURCE_TEXT_MAX]
            for key in SCAN_SOURCE_COUNTERS:
                item[key] = max(0, int(entry.get(key) or 0))
        except Exception:  # noqa: BLE001 — 一条坏来源不该带走整轮统计
            continue
        out.append(item)
    return out


def encode_scan_stats(stats: Optional[dict]) -> Optional[str]:
    """统计字典 → JSON 文本（序列化失败一律当作没有，不影响扫描本身）"""
    if not stats:
        return None
    try:
        out = {k: stats.get(k, 0) for k in SCAN_STATS_KEYS if k in stats}
        if "failed_roots" in out:
            out["failed_roots"] = [str(r)[:SCAN_SOURCE_TEXT_MAX]
                                   for r in (out.get("failed_roots") or [])][:20]
        if "removal_guard" in out:
            # 与 scan_error 列同口径截断：超长拦截原因不撑爆统计 JSON
            out["removal_guard"] = str(out.get("removal_guard") or "")[:500]
        if "sources" in out:
            out["sources"] = _encode_scan_sources(out.get("sources"))
        if "resume_scope" in out:
            # 只留定向范围本身：前缀逐个截断、条数封顶，坏形状直接丢弃
            scope = out.get("resume_scope")
            if isinstance(scope, dict) and scope.get("targeted"):
                out["resume_scope"] = {
                    "targeted": True,
                    "limit_prefixes": [
                        str(p)[:1000] for p in (scope.get("limit_prefixes") or [])
                    ][:200],
                }
            else:
                out.pop("resume_scope", None)
        return json.dumps(out, ensure_ascii=False, sort_keys=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("序列化扫描统计失败: %s", exc)
        return None


def decode_scan_stats(raw: Optional[str]) -> dict:
    """JSON 文本 → 统计字典（坏数据回空字典，不向外抛）"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    return value if isinstance(value, dict) else {}


def _scan_metrics(stats: dict) -> dict:
    """统计字典 → 对外字段（「最近一次」与「扫描流水」共用同一份口径）"""
    return {
        "added": int(stats.get("added") or 0),
        "updated": int(stats.get("updated") or 0),
        "removed": int(stats.get("removed") or 0),
        "probed": int(stats.get("probed") or 0),
        "scraped": int(stats.get("scraped") or 0),
        "repaired": int(stats.get("repaired") or 0),
        "unchanged": int(stats.get("unchanged") or 0),
        # 软删除（v2.48.0）：文件重新出现、被放回可见的原条目数
        "resurrected": int(stats.get("resurrected") or 0),
        "removal_skipped": bool(stats.get("removal_skipped")),
        "failed_roots": list(stats.get("failed_roots") or []),
        # v2.56.x：定向扫描标记（预期跳过清理，前端可据此不显示警告）
        "targeted_scan": bool(stats.get("targeted_scan")),
        # 删除保护拦截原因（v2.55.x）：本轮没删任何东西是因为被拦了，而不是
        # "库里本来就空"。finish_scan() 已把该原因写进 error 兜底链，这里单独
        # 暴露给排障/接口调用方，避免再出现"有写入、零读取"的死字段。
        "removal_guard": stats.get("removal_guard") or "",
        # 按来源拆分的明细：只记失败来源的话，「挂载在、但一条文件都没有」这种
        # （挂载点被清空 / 账号范围变了 / 路径写错了但目录存在）根本看不出来
        "sources": [s for s in (stats.get("sources") or []) if isinstance(s, dict)],
    }


def scan_result_payload(library) -> Optional[dict]:
    """媒体库最近一次扫描结果的对外结构（管理端列表与客户端接口共用一份口径）

    从没扫过（既没有状态也没有时间）时返回 None，前端据此显示「尚未扫描」——
    「没扫过」和「扫了但都是 0」是两件事，不能混成一个空结构。
    """
    status = getattr(library, "scan_status", None)
    if not status and not getattr(library, "last_scan_at", None):
        return None
    stats = decode_scan_stats(getattr(library, "scan_stats", None))
    return {
        "status": status or SCAN_STATUS_SUCCESS,
        "finished_at": library.last_scan_at.isoformat() if library.last_scan_at else None,
        "duration_ms": stats.get("duration_ms"),
        **_scan_metrics(stats),
        "error": getattr(library, "scan_error", None),
    }


def _scan_run_payload(run) -> dict:
    """一条扫描流水的对外结构（与 scan_result_payload 同一套字段口径）"""
    stats = decode_scan_stats(getattr(run, "stats", None))
    duration = run.duration_ms if run.duration_ms is not None else stats.get("duration_ms")
    return {
        "id": run.id,
        "status": run.status,
        "trigger": run.trigger,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_ms": duration,
        **_scan_metrics(stats),
        "error": run.error,
    }


def scan_runs_payload(db: Session, library_id: int, limit: int = 20) -> list:
    """某个媒体库最近的扫描流水（新的在前；上限钳到 200，避免有人拉全表）"""
    hits = max(0, min(int(limit or 0), 200))
    if not hits:
        return []
    rows = (
        db.query(emby_models.ScanRun)
        .filter(emby_models.ScanRun.library_id == library_id)
        .order_by(emby_models.ScanRun.id.desc())
        .limit(hits)
        .all()
    )
    return [_scan_run_payload(run) for run in rows]


def recent_scan_runs(db: Session, library_ids, limit: int = 40) -> list:
    """跨库取最近若干轮扫描流水（服务器维度视图用；新的在前）

    「整服视角的扫描历史」问的是「这台节点负责的那批库，最近扫得怎么样」——
    按库各查一遍再拼接既慢又排不出时间顺序，所以这里一次查完再排。
    每条带上 ``library_id``，面板才能把流水写回库名。
    """
    ids = [int(i) for i in (library_ids or [])]
    if not ids:
        return []
    hits = max(0, min(int(limit or 0), 200))
    if not hits:
        return []
    rows = (
        db.query(emby_models.ScanRun)
        .filter(emby_models.ScanRun.library_id.in_(ids))
        .order_by(emby_models.ScanRun.id.desc())
        .limit(hits)
        .all()
    )
    return [{**_scan_run_payload(run), "library_id": run.library_id} for run in rows]


def prune_library_scan_runs(db: Session, library_id: int,
                            keep: Optional[int] = None) -> int:
    """只留某个媒体库最近 ``keep`` 轮流水（不提交：由调用方与本次写入一起提交）"""
    limit = SCAN_RUN_KEEP if keep is None else max(0, int(keep))
    if limit <= 0:
        return 0
    stale_ids = [
        row[0] for row in db.query(emby_models.ScanRun.id)
        .filter(emby_models.ScanRun.library_id == library_id)
        .order_by(emby_models.ScanRun.id.desc())
        .offset(limit)
        .all()
    ]
    if not stale_ids:
        return 0
    deleted = db.query(emby_models.ScanRun).filter(
        emby_models.ScanRun.id.in_(stale_ids)
    ).delete(synchronize_session=False)
    return int(deleted or 0)


def _finish_scan_run(db: Session, run_id: Optional[int], status: str,
                     stats: Optional[dict], error: Optional[str]) -> None:
    """收尾这一轮的扫描流水，并就地回收过旧的行（写失败只记日志，不影响扫描本身）"""
    if not run_id:
        return
    try:
        run = db.query(emby_models.ScanRun).filter(emby_models.ScanRun.id == run_id).first()
        if run is None:
            return
        run.status = status
        run.finished_at = datetime.now()
        duration = (stats or {}).get("duration_ms")
        if duration is None and run.started_at is not None:
            # 调用方没给耗时（直接调 begin/finish 的路径）也能量出来：
            # 收尾过的流水必须有耗时，否则「扫了多久」这条信息就丢了
            duration = int((run.finished_at - run.started_at).total_seconds() * 1000)
        run.duration_ms = duration
        run.stats = encode_scan_stats(stats)
        run.error = (error or "")[:500] or None
        prune_library_scan_runs(db, run.library_id)
        commit_with_retry(db, label="扫描流水收尾")
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入扫描流水失败: %s", exc, exc_info=True)
        db.rollback()


def _write_scan_state(db: Session, library: emby_models.Library, status: str,
                      stats: Optional[dict], error: Optional[str]) -> None:
    """状态 / 统计 / 原因写回媒体库并提交（落盘失败只记日志，不覆盖真实异常）"""
    library.is_scanning = False
    library.last_scan_at = datetime.now()
    library.scan_started_at = None   # 本轮已收尾，不再参与复位判定
    library.scan_status = status
    library.scan_stats = encode_scan_stats(stats)
    library.scan_error = (error or "")[:500] or None
    # 列表去重缓存：本轮扫描可能增删改了条目，bump 该库数据版本号
    #（与状态写回同一事务提交；bump 自带异常兜底，不影响扫描本身）。
    try:
        from backend.emby_server import items_dedup_cache as _idc
        _idc.bump_library_item_versions(db, [library.id])
    except Exception as exc:  # noqa: BLE001
        logger.warning("去重缓存版本号 bump 失败（扫描收尾）: %s", exc)
    try:
        commit_with_retry(db, label="扫描结果写回")
    except Exception as exc:  # noqa: BLE001
        logger.warning("写入扫描结果失败: %s", exc, exc_info=True)
        db.rollback()


def _resume_scope_of(snapshot) -> Optional[dict]:
    """从快照里提取「被打断后该按什么范围续扫」，整库扫描返回 None。

    定向扫描（Drive Changes 触发，只扫几个变化目录）被重启打断时，续扫必须
    还是定向——历史上续扫不分青红皂白整库重走，一次几分钟的增量能被续成
    几天走不完的全量遍历（2026-10-22 国产剧实证）。范围只在定向时才记：
    整库扫描本来就该整库续，记了反而多一层出错面。
    """
    try:
        if snapshot is None or not getattr(snapshot, "targeted", False):
            return None
        prefixes = [str(p) for p in (getattr(snapshot, "limit_prefixes", ()) or ()) if p]
        if not prefixes:
            return None
        return {"targeted": True, "limit_prefixes": prefixes}
    except Exception:  # noqa: BLE001 — 取范围失败绝不拖累扫描本身
        return None


def interrupted_resume_prefixes(db: Session, library_id: int) -> Optional[list]:
    """该库最近一条「已中断」流水里记下的定向续扫范围；没有就返回 None。

    续扫方（maintenance.resume_interrupted_scans）据此决定定向续还是整库续：
    返回 None = 整库续（整库扫描被打断、或老流水没记范围时的既有口径）。
    读不出来（没流水 / stats 损坏）一律 None 回退整库，不许抛。

    范围落点两代兼容（批一 #7）：新流水先读检查点列 ``state_json``
    （begin_scan 专属落点、收尾永不覆写）；它为空/损坏/没记范围时回退
    ``stats`` 里的 resume_scope（老流水的落点，升级前只有这一处）。
    """
    try:
        run = (
            db.query(emby_models.ScanRun)
            .filter(emby_models.ScanRun.library_id == library_id,
                    emby_models.ScanRun.status == SCAN_STATUS_INTERRUPTED)
            .order_by(emby_models.ScanRun.id.desc())
            .first()
        )
        if run is None:
            return None
        scope = decode_scan_stats(getattr(run, "state_json", None)).get("resume_scope") or {}
        if not scope.get("targeted"):
            scope = decode_scan_stats(getattr(run, "stats", None)).get("resume_scope") or {}
        if not scope.get("targeted"):
            return None
        prefixes = [str(p) for p in (scope.get("limit_prefixes") or []) if p]
        return prefixes or None
    except Exception:  # noqa: BLE001 — 回退整库续是安全侧
        return None


#: 跨进程防重的部分唯一索引名（models.ScanRun.__table_args__ 与
#: database._migrate_scan_run_single_running 同名，三处必须一致）
SCAN_RUN_SINGLE_INDEX = "uq_scan_runs_one_running"


def _is_single_running_violation(exc: IntegrityError) -> bool:
    """这次 IntegrityError 是不是撞了「同库只许一条 running 流水」的索引。

    PG 文案带约束名：``duplicate key value violates unique constraint
    "uq_scan_runs_one_running"``——认名最准。SQLite 不报索引名、只报
    ``UNIQUE constraint failed: emby_scan_runs.library_id``（实测 3.45）：
    begin_scan 这次提交里 emby_scan_runs 上的唯一约束只有这一条索引，
    「表名 + 唯一冲突」即是它。两条都不命中（别的表冲突 / NOT NULL /
    外键）说明别处数据算错了，仍走「吞掉只记 warning」的老口径，
    不能误升级成 ScanInProgress 把真问题藏起来。

    只看驱动原始报错（``exc.orig``），不看 SQLAlchemy 拼的全文——全文里
    带的 SQL 语句本身就含表名，会把别的约束的锅也算到这张表头上。
    """
    haystack = str(getattr(exc, "orig", "") or "")
    if SCAN_RUN_SINGLE_INDEX in haystack:
        return True
    return "emby_scan_runs" in haystack and _is_unique_violation(exc)


def begin_scan(db: Session, library: emby_models.Library,
               trigger: str = "manual",
               snapshot=None) -> Optional[int]:
    """标记扫描开始，并开一条扫描流水，返回它 id

    独立提交一次：扫描过程中进程被杀，库里也能看出「上一轮在跑」以及是谁触发的。
    状态写不进去不影响扫描本身（只是这次没有流水）。

    ``snapshot`` 给了且是定向扫描时，把范围记进流水的 stats（resume_scope，
    老落点）与 state_json（检查点专属落点，批一 #7）：流水收尾只动
    status/error、不动 stats，被打断后范围还在，续扫先读 state_json、
    再回退 stats。对外流水结构（_scan_metrics）是白名单提取，
    这两个键都不会漏到管理端。

    **跨进程防重（批一 #7）**：插入的 running 流水撞上「同库只许一条
    running」的部分唯一索引时，说明另一个进程（worker / EA）已在扫这个
    库——抛 ``ScanInProgress``，与进程内 ``_acquire_scan`` 同一个异常，
    队列层按「重复任务拒绝」的现成口径处理。其余写库失败（抖动、锁、
    别的约束）维持老口径：吞掉只记 warning，本轮照常扫、只是没流水。
    ``SCAN_RUN_KEEP=0``（不开流水）时没有行可撞，防重只剩进程内一层。
    """
    library.is_scanning = True
    library.scan_status = SCAN_STATUS_RUNNING
    library.scan_error = None
    # 单独记本轮起始时刻：复位判定只能看它（见 models.Library.scan_started_at）
    library.scan_started_at = datetime.now()
    run = None
    if SCAN_RUN_KEEP > 0:
        run = emby_models.ScanRun(library_id=library.id, status=SCAN_STATUS_RUNNING,
                                  trigger=normalize_scan_trigger(trigger),
                                  started_at=datetime.now())
        scope = _resume_scope_of(snapshot)
        if scope is not None:
            encoded_scope = encode_scan_stats({"resume_scope": scope})
            run.stats = encoded_scope
            run.state_json = encoded_scope
        db.add(run)
    try:
        commit_with_retry(db, label="扫描开始状态")
    except IntegrityError as exc:
        db.rollback()
        if _is_single_running_violation(exc):
            raise ScanInProgress(
                f"媒体库 {library.id} 正在扫描中（其他进程已在扫描）") from exc
        logger.warning("写入扫描开始状态失败: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — 状态写不进去也要照常扫描
        logger.warning("写入扫描开始状态失败: %s", exc)
        db.rollback()
        return None
    return int(run.id) if run is not None and run.id is not None else None


def _cover_autogen_after_scan(db: Session, library, stats: dict) -> None:
    """新片入库后自动重生成封面（``Library.cover_auto_regen`` 开了才做）

    三个提前返回都是刻意的：

    - 没开开关 → 什么都不做（这是绝大多数库）；
    - 没选样式 → 没有可复用的配置，重生成必然失败，不如什么都不做；
    - **本轮没新增** → 封面就是最新的，重画一遍只会白白花渲染时间 + 让缓存失效。
      “自动更新”该跟“新片”绑定，而不是跟“又扫了一次”绑定。

    延迟导入：封面模块住在 ``backend.api`` 且反过来依赖本包，直接 import 会成环
    （与 ``probe_worker`` 里的延迟导入同理）。**只入队**、不渲染：渲染交给后台
    线程按库合并执行（见 ``library_cover.enqueue_cover_regeneration``），封面慢、
    失败或根本没有海报，都不会把一轮扫描的结果拖下水。
    """
    if not getattr(library, "cover_auto_regen", False):
        return
    if not getattr(library, "cover_template", ""):
        return
    if not int((stats or {}).get("added") or 0):
        return
    library_id = getattr(library, "id", None)
    try:
        # 只入队，不渲染（v2.48.0）：渲染慢会把整轮扫描拖住，而且转场一批新片时
        # 同一轮里会反复触发。队列按库合并，扫描与封面完全解耦。
        from backend.api.library_cover import enqueue_cover_regeneration

        created = enqueue_cover_regeneration(library_id)
        logger.info("封面已排队（%s）library_id=%s 新增=%s",
                    "新建任务" if created else "已有任务在跑，合并", library_id,
                    (stats or {}).get("added"))
    except Exception:  # noqa: BLE001 — 封面失败绝不能影响扫描结果落库
        logger.warning("封面任务入队失败 library_id=%s", library_id, exc_info=True)


def _maybe_alert_enrich_flip(db: Session, library: emby_models.Library,
                             stats: dict, run_id: Optional[int]) -> None:
    """v2.55.x 翻转监控：本轮 done→pending 翻转超阈值则告警。

    2026-10-11 生产事故：5723 条翻转触发源未知、日志丢失。以后这类事件：
    1. warning 日志（docker logs 即时可见）；
    2. PG 写 EnrichFlipEvent（持久现场，容器重建不丢）；
    3. /api/health 暴露 enrich_flip_last_hour（见 health_report）。
    监控失败只记日志，不影响扫描结果落库。
    """
    flip_count = int((stats or {}).get("enrich_flips") or 0)
    if not flip_count:
        return
    try:
        from backend.emby_server import enrich_flip_monitor as _fm
        if not _fm.should_alert(flip_count):
            return
        # 触发源：从扫描流水取（见 SCAN_TRIGGERS），取不到就记 scan
        trigger = "scan"
        if run_id:
            try:
                run = db.query(emby_models.ScanRun).filter(
                    emby_models.ScanRun.id == run_id).first()
                if run is not None and getattr(run, "trigger", None):
                    trigger = f"scan:{run.trigger}"
            except Exception:  # noqa: BLE001
                pass
        reasons = (stats or {}).get("enrich_flip_reasons") or {}
        sample_ids = (stats or {}).get("enrich_flip_sample_ids") or []
        item_types = (stats or {}).get("enrich_flip_item_types") or {}
        # 复用 alert_flip 共享能力（内部自带 should_alert + warning 日志 + 写库），
        # 不再手写一遍"warning + record_flip_event"。
        _fm.alert_flip(
            db, flip_count=flip_count, trigger=trigger,
            library_id=getattr(library, "id", None), scan_run_id=run_id,
            summary={"reasons": reasons, "item_types": item_types,
                     "sample_ids": sample_ids},
        )
    except Exception as exc:  # noqa: BLE001 — 监控不能影响扫描
        logger.warning("enrich 翻转告警检查失败: %s", exc)


def _note_scan_feed(db: Session, library: emby_models.Library, stats: dict,
                    status: str) -> None:
    """扫描完成 → 发现页实时动态流埋点（真实触发：本轮扫描确实跑完）。

    - 有新增/更新 → 一条 scan 事件「扫描完成 · 《库名》新增 X · 更新 Y」
    - 清掉失效条目 → 一条 system 事件「死链清理 · 《库名》清理了 N 条失效条目」
    - 本轮零变化且成功 → 不写（避免定时空扫刷屏）
    """
    try:
        from backend.panel_feed import note_feed
        added = int(stats.get("added") or 0)
        updated = int(stats.get("updated") or 0)
        removed = int(stats.get("removed") or 0)
        lib_id = getattr(library, "id", None)
        if added or updated:
            note_feed(db, f"扫描完成 · 新增 {added} · 更新 {updated}",
                      event_type="scan", library_id=lib_id)
        elif status != SCAN_STATUS_SUCCESS:
            note_feed(db, f"扫描完成（{status}）", event_type="scan",
                      library_id=lib_id)
        if removed:
            note_feed(db, f"死链清理 · 清理了 {removed} 条失效条目",
                      event_type="system", library_id=lib_id)
    except Exception:  # noqa: BLE001 — 埋点失败绝不能影响扫描结果落库
        logger.warning("扫描 feed 埋点失败 library_id=%s",
                       getattr(library, "id", "?"), exc_info=True)


def finish_scan(db: Session, library: emby_models.Library, stats: Optional[dict],
                run_id: Optional[int] = None) -> str:
    """写入一轮扫描的结果，返回归类后的状态

    只有「来源全部可用且正常跑完」才算 success；任何来源读不到就记 partial——
    那种情况下清理阶段被跳过，结果本身并不完整，不能对外报「一切正常」。

    v2.56.x：定向扫描（targeted_scan）的清理跳过是预期行为——它只看了部分
    目录，本来就不能做删除清理。只要没有来源真的失败，就按 success 处理，
    不再误报"媒体库没有可用来源"。
    """
    stats = stats or {}
    removal_skipped = bool(stats.get("removal_skipped"))
    # 定向扫描 + 无真实失败 = 预期内的跳过，不算问题
    _is_expected_skip = bool(stats.get("targeted_scan")) and not (
        stats.get("failed_roots") or stats.get("removal_guard")
    )
    status = SCAN_STATUS_PARTIAL if (removal_skipped and not _is_expected_skip) \
        else SCAN_STATUS_SUCCESS
    error = None
    if status == SCAN_STATUS_PARTIAL:
        # 删除保护拦截时优先展示真实拦截原因（removal_guard），而不是误导性的
        # 兜底文案——failed_roots 为空不代表"没有可用来源"，可能只是"一个文件
        # 都没看到但库里有旧条目"，这时 guard 里有真正的事故形状描述。
        error = "；".join(str(r) for r in (stats.get("failed_roots") or []))[:500] \
            or (stats.get("removal_guard") or "")[:500] \
            or "媒体库没有可用来源，本轮已跳过清理"
    _write_scan_state(db, library, status, stats, error)
    # 与媒体库状态分开两次提交：流水写不进去不该影响「上一轮扫得怎么样」这条主状态
    _finish_scan_run(db, run_id, status, stats, error)
    # v2.55.x 翻转监控：本轮 done→pending 超阈值则告警 + 写 PG 事件（持久现场）
    _maybe_alert_enrich_flip(db, library, stats, run_id)
    # 结果已经落库了，封面重生成即使失败也不影响上面那两条状态
    _cover_autogen_after_scan(db, library, stats)
    # 发现页实时动态流埋点（只加写入调用，不改扫描行为）
    _note_scan_feed(db, library, stats, status)
    return status


def fail_scan(db: Session, library: emby_models.Library, exc: Exception,
              run_id: Optional[int] = None,
              duration_ms: Optional[int] = None) -> None:
    """扫描抛异常时把原因落库（管理端要能看到「为什么没扫成」）"""
    # 诊断日志：定位 Session 跨线程等问题时直接抓现行（只记日志，不改落库逻辑）。
    # 调用方保证在 except 块内调用，因此 traceback.format_exc() 一定有内容。
    logger.error(
        "扫描失败诊断 library_id=%s trigger=fail_scan thread=%s session_id=%s\n%s",
        getattr(library, "id", "?"),
        threading.current_thread().name,
        id(db),
        traceback.format_exc(),
    )
    error = f"{type(exc).__name__}: {exc}"
    _write_scan_state(db, library, SCAN_STATUS_FAILED, None, error)
    # 失败也要记耗时：扫了四十分钟才炸和刚开就炸不是一回事
    stats = {"duration_ms": duration_ms} if duration_ms is not None else None
    _finish_scan_run(db, run_id, SCAN_STATUS_FAILED, stats, error)


def scan_library_sync(db: Session, library: emby_models.Library,
                      snapshot: Optional[LibrarySnapshot] = None,
                      trigger: str = "manual") -> dict:
    """扫描单个媒体库（同步实现，可在后台线程运行）

    约定：

    - **配置快照**：整个任务只认 `snapshot`（默认在入口处拍一张），见 LibrarySnapshot。
    - **重复任务拒绝**：同一媒体库同时只允许一个任务，重复触发抛 `ScanInProgress`。
    - **目录不完整禁止清理**：任何根目录不可读或遍历报错时跳过清理阶段，
      避免“读取失败 → 当成文件已删除 → 误删整库记录”。
    - **探测/刮削按需**：已探测过的文件不再重复 ffprobe（除非文件大小变了），
      刮削按媒体库策略（missing_only / 3m / 6m / 1y / all）。
    - **外挂字幕与视频探测解耦**：换字幕文件不需要重探视频。
    - **结果可查**：成功 / 部分失败 / 异常三条路都会把这一轮的统计与原因写回媒体库
      （scan_status / scan_stats / scan_error），见 ``scan_result_payload``；
      同时记一条扫描流水（含 ``trigger``），见 ``scan_runs_payload``。
    """
    snap = snapshot or LibrarySnapshot.of(library)
    # 先在**进程内**占位（重复任务会在这一步被拒绝，不会写库、也不会启动扫描），再写库状态
    _acquire_scan(snap.library_id)
    # 让路配额（2026-10-23）：本轮开扫重置配额时钟；定向扫描（新片入库）
    # 全程豁免让路——只扫几个变化目录、用户正等着上架，进来就跑完。
    try:
        from backend.emby_server import foreground_activity as _fg

        _fg.reset_yield_clock()
        _fg.set_yield_exempt(bool(getattr(snap, "targeted", False)))
    except Exception:  # noqa: BLE001 — 让路记账失败绝不拖累扫描本身
        pass
    try:
        run_id = begin_scan(db, library, trigger, snapshot=snap)
        started = time.perf_counter()
        try:
            stats = _scan_library_body(db, library, snap)
        except Exception as exc:  # noqa: BLE001 — 异常要落到库里，再原样抛给调用方
            if isinstance(exc, RuntimeError) and "interpreter shutdown" in str(exc):
                # 容器重启/部署时扫描线程被中断：不是扫描逻辑故障，给用户一句人话，
                # 而不是 "cannot schedule new futures after interpreter shutdown"。
                logger.warning("媒体库「%s」扫描被容器重启/部署中断，重扫即可", snap.name)
                exc = RuntimeError("扫描被容器重启中断（部署/重建），重扫即可")
                exc.__cause__ = None
            else:
                logger.exception("媒体库「%s」扫描失败", snap.name)
            fail_scan(db, library, exc, run_id,
                      duration_ms=int((time.perf_counter() - started) * 1000))
            raise
        stats["duration_ms"] = int((time.perf_counter() - started) * 1000)
        finish_scan(db, library, stats, run_id)
        return stats
    finally:
        # 无论成功、失败还是生成器被提前关闭，都释放进程内占位，否则这个库再也扫不动
        _release_scan(snap.library_id)
        try:
            from backend.emby_server import foreground_activity as _fg

            _fg.set_yield_exempt(False)
        except Exception:  # noqa: BLE001
            pass


# ---- enrich_status 统一写入点（P2-1/P2-3/P3-1，2026-10-11 round3-p2p3）----
# 原两套写入语义（分层状态机 vs 非分层简单标记）已统一到 _apply_enrich_status。
# 所有 enrich_status 写入必须走 _write_enrich_status，reason 必填，保证日志可审计。
_ENRICH_PENDING = "pending"
_ENRICH_DONE = "done"
_ENRICH_FAILED = "failed"
_ENRICH_RETRY_DELAY = timedelta(hours=1)
# 参与后台补全流水线的类型（保留原非分层路径行为）。
_ENRICH_SUPPORTED_TYPES = ("movie", "episode")


def _write_enrich_status(
    item,
    status,
    *,
    reason,
    old_status=None,
    old_size=None,
    new_size=None,
    reset_failed_attempts=False,
):
    """Single writer for item.enrich_status.

    Contract:
      * layered=True  -> PENDING means "enrich_worker will fill the rest"
      * layered=False -> DONE means "this scan finalizes the item"
        （第 0 批起：只有真拿到元数据的行才 done；无 NFO、无 TMDB 命中、
        无有效文本的三无行同样走 PENDING，见 _apply_enrich_status）
    `reason` is mandatory so every transition is auditable in logs.

    Returns True when this write was a done→pending flip (v2.55.x 翻转监控：
    调用方把明细记到 ctx.enrich_flips，本轮结束超阈值则告警）。
    """
    was_done = (old_status or _ENRICH_PENDING) == _ENRICH_DONE
    item.enrich_status = status

    attempts_reset = False
    if (
        status == _ENRICH_PENDING
        and reset_failed_attempts
        and old_status == _ENRICH_FAILED
    ):
        item.enrich_attempts = 0
        item.enrich_next_retry_at = datetime.now() + _ENRICH_RETRY_DELAY
        attempts_reset = True

    # PENDING 低频且运维关键 -> INFO；DONE 高频幂等 -> DEBUG 防日志风暴。
    log = logger.info if status == _ENRICH_PENDING else logger.debug
    log(
        "enrich_status write: id=%s status=%s reason=%s old_status=%s "
        "old_size=%s new_size=%s attempts_reset=%s",
        getattr(item, "id", None),
        status,
        reason,
        old_status,
        old_size,
        new_size,
        attempts_reset,
    )
    return was_done and status == _ENRICH_PENDING


_STRM_STUB_MAX = 64 * 1024


def _compute_size_change(item, scan_file):
    """Return (old_size, new_size, size_changed), keep item.size in sync.

    修复隐藏 bug：原代码 size 变化时未回写 item.size，导致下次扫描
    看到旧 size 又判定 size_changed=True，无限重排。

    strm 双口径规则：对 .strm 条目，本机扫描的文本文件大小（纸条口径）
    与网盘扫描的目标视频大小（视频口径）不互相判为变化，item.size 优先
    保持/同步为视频口径。
    """
    old_size = getattr(item, "size", 0) or 0
    new_size = getattr(scan_file, "size", 0) or 0

    if not old_size and new_size:
        # 首次得知大小：记下，本轮不视为"变化"。
        item.size = new_size
        return old_size, new_size, False

    file_path = str(getattr(item, "file_path", "") or "")
    stored_path = str(getattr(scan_file, "stored_path", "") or "")
    is_strm = file_path.lower().endswith(".strm") or stored_path.lower().endswith(".strm")

    if is_strm:
        old_is_stub = old_size <= _STRM_STUB_MAX
        new_is_stub = new_size <= _STRM_STUB_MAX

        if old_is_stub != new_is_stub:
            # 一边纸条、一边视频：不视为变化，item.size 同步为视频口径。
            item.size = max(old_size, new_size)
            return old_size, new_size, False

        if old_is_stub and new_is_stub:
            # 两边都是纸条：不相等也不视为变化，item.size 保持 old。
            return old_size, new_size, False

    size_changed = bool(old_size and new_size and old_size != new_size)
    if size_changed:
        # 必须回写，否则下次扫描看到旧 size 又判为变化，无限重排。
        item.size = new_size

    return old_size, new_size, size_changed


# 已落定为权威元数据的来源标记：行上带这些标记说明身份/文本已齐，
# 非分层扫描可直接终结（done），不需要 enrich_worker 再跑一轮。
# 注意 'none' 不在里面：它表示"搜过、没有"，需要重新排队再试。
_ENRICH_AUTHORITATIVE_SOURCES = frozenset({
    "nfo", "tmdb", "tmdb_img", "douban", "bangumi", "multisource", "inherit",
})


def _nfo_has_content(nfo_data) -> bool:
    """NFO 是否带了实质内容（B 方案：NFO 文本权威，TMDB 只补图）。"""
    if not isinstance(nfo_data, dict):
        return False
    return bool(
        nfo_data.get("tmdb_id")
        or (nfo_data.get("title") or "").strip()
        or (nfo_data.get("overview") or "").strip()
    )


def _scan_has_metadata(item, pending) -> bool:
    """本轮（非分层）扫描是否已拿到足以终结的元数据。

    供 ``_apply_enrich_status`` 的 flat 分支判定 done/pending（第 0 批修复，
    2026-10-17）：旧实现无条件 done，无 NFO、无 TMDB 命中的新行也被标 done，
    永不进补全队列（假完成）。现在只有真拿到元数据的行才终结：
    - 本轮 TMDB 搜索命中（pending.tmdb_hit，写库后段会 apply，
      metadata_source='tmdb' + last_scraped_at）；
    - 本轮 NFO 有实质内容（title/overview/tmdb_id）：NFO 权威，B 方案本意；
    - 行上已有权威元数据（真实 metadata_source / 有效简介）：
      更新扫描/重刮，worker 不需要再跑一轮。
    三者皆无（无 NFO 且无 TMDB 命中且无有效文本）→ False，行走 pending，
    与分层远程文件用同一个待刮状态值，等 enrich_worker 后台补。

    注意（2026-10-20 假完成修复）：tmdb_id 不再单独算数——它只是身份
    标识、不是刮削证据。只带 tmdb_id、metadata_source 与简介皆空的行
    实际从未刮过，在此算「已齐」标 done 即假完成（与 enrich_worker
    _suppress_flood 同口径修复）。
    """
    if getattr(pending, "tmdb_hit", None):
        return True
    if _nfo_has_content(getattr(pending, "nfo_data", None)):
        return True
    if getattr(item, "metadata_source", None) in _ENRICH_AUTHORITATIVE_SOURCES:
        return True
    return bool((getattr(item, "overview", None) or "").strip())


def _apply_enrich_status(item, scan_file, *, is_new, layered, item_type,
                         flip_tracker=None, has_metadata=True):
    """Decide and write enrich_status. Max nesting depth: 2.

    flip_tracker（v2.55.x 翻转监控）：可选的 list，done→pending 翻转时
    追加 {"item_id", "item_type", "reason", "old_size", "new_size"}。
    调用方（扫描写库循环）传 ctx.enrich_flips。

    has_metadata（第 0 批，2026-10-17）：仅 flat（非分层）分支使用。
    调用方用 _scan_has_metadata(item, pending) 计算：本轮真拿到元数据的行
    才终结为 done；三无行走 pending（与分层远程文件相同的待刮状态值）。
    默认 True 保持旧调用方行为不变。
    """
    old_status = getattr(item, "enrich_status", None)
    old_size, new_size, size_changed = _compute_size_change(item, scan_file)

    def _record(reason):
        if flip_tracker is not None:
            flip_tracker.append({
                "item_id": getattr(item, "id", None),
                "item_type": item_type,
                "reason": reason,
                "old_size": old_size,
                "new_size": new_size,
            })

    if not layered:
        # 非分层：本轮已做完补全——但只在真拿到元数据时才终结（第 0 批修复，
        # 2026-10-17）：无 NFO、无 TMDB 命中、无有效文本的三无行走 pending，
        # 用与分层远程文件相同的待刮状态值，等 enrich_worker 后台补；
        # NFO 真填满的行标 done 是 B 方案本意，保留。
        # 注意：series/season 骨架行由 episode 分支创建，走 L2 补全，这里不抢标。
        if (
            item_type in _ENRICH_SUPPORTED_TYPES
            and (old_status or _ENRICH_PENDING) == _ENRICH_PENDING
        ):
            if has_metadata:
                _write_enrich_status(
                    item,
                    _ENRICH_DONE,
                    reason="flat_scan_finalized",
                    old_status=old_status,
                    old_size=old_size,
                    new_size=new_size,
                )
            else:
                _write_enrich_status(
                    item,
                    _ENRICH_PENDING,
                    reason="flat_scan_no_metadata",
                    old_status=old_status,
                    old_size=old_size,
                    new_size=new_size,
                )
        return

    # 分层扫描 enrich_status 状态机
    # （2026-10-11 生产事故修复 #593，round3 完善）：
    # mtime 不可靠，内容真实变更只看文件大小变化。
    if is_new or old_status is None:
        flipped = _write_enrich_status(
            item,
            _ENRICH_PENDING,
            reason="new_item" if is_new else "no_prior_status",
            old_status=old_status,
            old_size=old_size,
            new_size=new_size,
        )
        if flipped:
            _record("new_item" if is_new else "no_prior_status")
        return

    if size_changed:
        flipped = _write_enrich_status(
            item,
            _ENRICH_PENDING,
            reason="size_changed",
            old_status=old_status,
            old_size=old_size,
            new_size=new_size,
            reset_failed_attempts=True,
        )
        if flipped:
            _record("size_changed")
        return

    # 仅 mtime 抖动 / 无实质变化：保持原状态，防止队列风暴。
    logger.debug(
        "enrich_status kept: id=%s status=%s old_size=%s new_size=%s",
        getattr(item, "id", None),
        old_status,
        old_size,
        new_size,
    )



def _scan_library_body(db: Session, library: emby_models.Library,
                       snap: LibrarySnapshot) -> dict:
    """一轮扫描的主体（不含进程内互斥与结果落库，见 scan_library_sync）"""
    stats: dict = {
        "added": 0, "updated": 0, "removed": 0, "probed": 0, "scraped": 0,
        "repaired": 0, "removal_skipped": False, "failed_roots": [],
        "sources": [],  # 按来源记账（虚拟库没有来源，保持空列表）
        "targeted_scan": False,  # v2.56.x：定向扫描标记（预期跳过清理，不误报）
    }
    clear_dir_cache()  # 新的一轮扫描不复用上一轮的目录列表
    ctx = _ScanContext(snap=snap, lib_id=library.id, stats=stats)
    seen_guids = ctx.seen_guids  # 清理阶段用它判断“文件还在不在”（与旧变量同名）
    failed_roots: list[str] = []
    pool: Optional[ThreadPoolExecutor] = None
    try:
        if getattr(library, "is_virtual", False):
            # 虚拟媒体库没有自己的文件：只按发行平台回算计数
            library.item_count = count_virtual_items(db, library)
            db.commit()
            return stats

        # 并行 IO 线程池：ffprobe / 目录列举 / TMDB 搜索都在这里跑，写库仍在当前线程按顺序进行
        pool = ThreadPoolExecutor(max_workers=SCAN_WORKERS, thread_name_prefix="scan-io")
        for label, source_files in iter_scan_sources(snap, library, db, failed_roots,
                                                     report=stats["sources"]):
            def _process_one(scan_file, _pending):
                """处理单个文件的写库（由 _iter_prepared 按批次驱动）。

                从扫描主循环体抽取：原循环体的 ``continue``（跳到下一个文件）
                在这里语义为 ``return``。
                """
                full_path = scan_file.stored_path
                fname = scan_file.name
                guid = _pending.guid
                parsed = _pending.parsed

                # 条目（以及所属剧集/季）在批次开头已经一次查齐，这里直接用
                item = _pending.item
                is_new = item is None
                if is_new:
                    # 原子占位在批次开头已由 _claim_batch_guids 批量完成
                    # （Core upsert，无 SELECT；抢到的行已取回并以强引用挂在
                    # _pending.claimed_item 上）。这里直接用，不产生任何 SQL。
                    # 跨 Session 竞态（另一进程/重叠任务/重启重扫）下没抢到的，
                    # 走 _resolve_conflict 取回别人那行——两条路径最后都拿到
                    # 一个「已在库里」的 ORM 对象，下面那套字段赋值共用。
                    # 注意：占位成功后**不能再 db.add 一个同 guid 的新对象**，
                    # 那才是自己撞自己。
                    item = _pending.claimed_item
                    won = item is not None
                    if not won:
                        item = _resolve_conflict(db, guid)
                    if item is None:
                        # 占位行在别的未提交事务里，本事务看不到它。
                        # 只能退回等价的纯内存处理：本批跳过这个文件，
                        # 交给下一轮扫描处理（下一轮 _load_items 就能查到它了）。
                        stats["conflict_skipped"] = stats.get("conflict_skipped", 0) + 1
                        logger.warning(
                            "扫描跳过条目 %s：guid %s 正被其他事务持有，"
                            "本轮不插入，留给下一轮扫描",
                            full_path, guid)
                        return
                    _pending.item = item
                    if won:
                        stats["added"] += 1
                    else:
                        # 别人先插了：走 update 分支补全字段
                        stats["conflict_taken_over"] = stats.get("conflict_taken_over", 0) + 1
                else:
                    stats["updated"] += 1
                    if getattr(item, "deleted_at", None):
                        # 文件又出现了：撤销上一轮的软删除。播放进度、收藏、
                        # 已刮好的元数据都还在行上，跟着一起回来了。
                        item.deleted_at = None
                        stats["resurrected"] = stats.get("resurrected", 0) + 1
                        logger.info("条目已重新出现，恢复可见: %s", guid)

                # 探测 / 目录列举 / TMDB 的结果都在批次开头取回了（见 _prepare_and_prefetch）：
                # 这里只读纯值，本循环里**不许再出现任何网络或磁盘 IO**——
                # 从这一行往下到 db.commit() 之间，事务里只包纯 DB 写。
                probe = _pending.probe_data
                if probe is not None:
                    stats["probed"] += 1
                side_poster, side_fanart, external = _pending.side_data or (None, None, [])

                item_type = _pending.item_type  # 解析与查库在批次开头完成
                item.item_type = item_type
                item.name = parsed["name"]
                item.original_title = parsed["name"]
                item.sort_name = make_sort_name(parsed["name"])
                item.production_year = parsed["year"]
                item.file_path = full_path
                # v2.52.0: 改名/移动的行，guid 跟着新路径走（保持 guid=path 派生不变式）；
                # 元数据（TMDB/刮削/播放进度）都在同一行上，全保留。
                if getattr(_pending, "renamed", False):
                    _new_guid = _pending.guid
                    if item.guid != _new_guid:
                        # P0 最后一道防线：匹配阶段与写库之间可能有并发提交。
                        # 目标 guid 被占用则放弃改名，直接更新归属行
                        # （等价于 _resolve_conflict 的处理），绝不撞唯一键。
                        _holder_id = _guid_held_by_other(db, _new_guid, item.id)
                        if _holder_id is not None:
                            _holder_item = db.get(emby_models.MediaItem, _holder_id)
                            if _holder_item is not None:
                                logger.warning(
                                    "扫描改名 guid 冲突保护：%s 的目标 guid 已被条目 %s 持有，"
                                    "放弃改名复用，改走更新分支",
                                    full_path, _holder_id)
                                item = _holder_item
                                _pending.item = _holder_item
                                stats["rename_collision_rerouted"] = (
                                    stats.get("rename_collision_rerouted", 0) + 1)
                            else:
                                # 归属行在别的未提交事务里：本轮跳过，下一轮处理
                                stats["conflict_skipped"] = (
                                    stats.get("conflict_skipped", 0) + 1)
                                logger.warning(
                                    "扫描跳过条目 %s：目标 guid 正被其他事务持有，"
                                    "本轮不处理，留给下一轮扫描", full_path)
                                return
                        else:
                            item.guid = _new_guid
                            ctx.stats["renamed_updated"] = (
                                ctx.stats.get("renamed_updated", 0) + 1)
                    # else: guid 本来就一致，无需更新
                # v2.52.0: 落 drive_file_id（改名识别 + Changes API 都靠它）
                _fid = getattr(scan_file, "file_id", "") or ""
                if _fid:
                    item.drive_file_id = _fid
                    # 身份锚（清单第 8 项）：Drive/rclone 的 file_id 双写 provider_file_id；
                    # 115/本机没有稳定 file_id，provider_file_id 留空，绝不猜。
                    item.provider_file_id = _fid
                # 身份锚 source_id：只在本次扫描带新表行 id 时写（来源换了行 id 就跟到新行）；
                # 旧列回落扫描没有行 id，不清除已有锚（锚可能仍有效，回落路径会验证）。
                _src_row_id = getattr(scan_file, "source_row_id", None)
                if _src_row_id is not None:
                    item.source_id = _src_row_id
                item.container = scan_file.container or os.path.splitext(fname)[1].lstrip(".")
                # 分层扫描：文件指纹落库（下次秒跳的依据）；补全状态标记。
                # layered=本轮只做 L1 极简入库 → pending，等后台 enrich_worker；
                # 非 layered=本轮已做完所有补全 → 有元数据才 done，三无行同样
                # pending（第 0 批，_scan_has_metadata 判定）。
                if _pending.file_fingerprint:
                    item.file_fingerprint = _pending.file_fingerprint
                # enrich_status 统一走 _apply_enrich_status（P2-1/P2-3/P3-1 重构，
                # 2026-10-11 round3-p2p3）：单一写入点 + reason 必填日志 + 早返回结构。
                # v2.55.x 翻转监控：done→pending 明细记到 ctx.enrich_flips，
                # 本轮结束超阈值则告警 + 写 PG 事件。
                _apply_enrich_status(
                    item,
                    scan_file,
                    is_new=is_new,
                    layered=_pending.layered,
                    item_type=_pending.item_type,
                    flip_tracker=ctx.enrich_flips,
                    has_metadata=_scan_has_metadata(item, _pending),
                )
                platforms = detect_platforms(full_path)  # 发行平台标签（虚拟媒体库用）
                if platforms:
                    item.platforms = ",".join(platforms)
                if probe is not None:
                    item.size = probe.get("size", 0) or item.size or scan_file.size
                    item.duration_ticks = probe["duration_ticks"]
                    item.bitrate = safe_probe_int(probe.get("bitrate"))
                    item.width = safe_probe_int(probe.get("width"))
                    item.height = safe_probe_int(probe.get("height"))
                    item.video_codec = probe["video_codec"]
                    item.audio_codec = probe["audio_codec"]
                    item.audio_languages = probe["audio_languages"]
                    item.subtitle_languages = probe["subtitle_languages"]
                    item.last_probed_at = datetime.now()
                    item.probe_status = "degraded" if probe.get("_degraded") else "done"
                    item.probe_attempts = 0
                    item.probe_next_retry_at = None
                elif _pending.probe_deferred:
                    # 两阶段 background 模式：Phase 1 不探测，交给后台 worker。
                    # 新文件（或换源：大小变了）优先探测；其余按默认优先级排队。
                    size_changed = (not is_new and bool(item.size)
                                    and bool(scan_file.size)
                                    and scan_file.size != item.size)
                    item.probe_status = "pending"
                    item.probe_priority = 100 if (is_new or size_changed) else 0
                    item.probe_attempts = 0
                    item.probe_next_retry_at = None
                    stats["probe_queued"] = stats.get("probe_queued", 0) + 1
                elif (not _pending.probe_needed
                        and getattr(item, "probe_status", None) != "done"):
                    # 提交点判定为不需要探测 → 数据已有效，明确标 done。
                    # 作用：迁移来的 'pending' 默认值在这里被纠正，worker 不会空跑；
                    # 从 background 回滚到 inline 时，残留的 pending 也会被顺手纠正。
                    item.probe_status = "done"

                # 本地图片 / 外挂字幕都在批次开头算好了（_side_info 在 IO 线程池里跑）：
                # 远程挂载的目录列举因此只发生一次，且不在写事务里
                item.poster_path = side_poster or item.poster_path
                item.backdrop_path = side_fanart or item.backdrop_path

                # 剧集层级：episode -> season -> series
                # 剧集与季在**批次开头**已一次查齐（见 _prepare_and_prefetch），结果缓存
                # 在 ctx.series_items / ctx.season_items，这里只在确实没有时才新建。
                # 旧实现在这个循环里对每一集点查两次（先剧集、再季）：十万集就是二十万次
                # 往返，即使每条都走 guid 唯一索引，也要多花几秒、把写库线程压在查询上。
                # 两者在下面按需赋值；非剧集条目保持 None（后段的海报回退要求它们已绑定）
                series = season_item = None
                if item_type == "episode":
                    season_no, ep_no = parsed["season"], parsed["episode"]
                    series_guid = _pending.series_guid
                    series = ctx.series_items.get(series_guid)
                    if series is None:
                        # 系统性去重：建 series 前先查库里是否已有同部剧
                        # （同 tmdb_id，或同归一化标题+年份），有则复用，不建新记录。
                        # 参考 Emby 多版本逻辑：同一部剧只保留一条。
                        _series_name = _pending.series_name or parsed["name"]
                        _series_year = parsed["year"]
                        _nfo_tmdb = ((_pending.series_nfo_data or {}).get("tmdb_id")
                                     if _pending.series_nfo_data else None)
                        _dup_ids = _find_duplicate_series(
                            db, library.id, tmdb_id=_nfo_tmdb,
                            name=_series_name, year=_series_year)
                        if _dup_ids:
                            # 复用已有记录：把新 guid 也指向它，本批后续集数都挂过去
                            series = db.get(emby_models.MediaItem, _dup_ids[0])
                            ctx.series_items[series_guid] = series
                        else:
                            series = emby_models.MediaItem(
                                guid=series_guid, library_id=library.id,
                                item_type="series", name=_series_name,
                                sort_name=make_sort_name(_series_name),
                                production_year=_series_year,
                                platforms=item.platforms or "",
                                date_added=datetime.now(),
                                # 剧集/季没有文件可探测：直接 done，不进探测队列
                                probe_status="done",
                            )
                            db.add(series)
                            db.flush()
                            ctx.series_items[series_guid] = series
                            # B2 季行随批预取（2026-10-20）：本批新建的 series，
                            # _ensure_season_row 对它跳过回退 SELECT（季行不可能已存在）。
                            ctx.new_series_ids.add(series.id)
                    # 现有隐式 series 可能是旧版本用 episode 文件名建出来的污染名称；
                    # 只要还没有 TMDB/NFO 的权威名称，就用目录名纠正。
                    if _pending.series_name and not series.tmdb_id:
                        series.name = _pending.series_name
                        series.sort_name = make_sort_name(_pending.series_name)
                    item.series_id = series.id
                    # B 方案补全：本批为隐式 series 预取的 TMDB 搜索命中先落库
                    # （补 tmdb_id/简介/海报/评分/别名；NFO 文本随后覆盖，保证 NFO 优先）
                    series_tmdb_applied = False
                    tmdb_res = ctx.series_tmdb_results.get(series_guid)
                    if tmdb_res and not series.tmdb_id:
                        series_hit, series_details = tmdb_res
                        if series_hit:
                            _tmdb.tmdb_client.apply(series, series_hit, "series")
                            stats["scraped"] += 1
                            series_tmdb_applied = True
                        if series_details:
                            _tmdb.tmdb_client.apply_details(series, series_details)
                    if (series_guid in ctx.series_tmdb_searched
                            and not series.tmdb_id and not series.last_scraped_at):
                        # 本轮干净地搜过但没命中：记一笔尝试，免得以后每轮重扫
                        # 都把这一集完整处理一遍（v2.31.0）
                        series.last_scraped_at = datetime.now()
                    series_nfo = _pending.series_nfo_data
                    if series_nfo and (not series.tmdb_id or not series.overview
                                       or series_tmdb_applied):
                        # 剧集级 NFO（tvshow.nfo）：这类 series 平时不走 TMDB，
                        # 远端有刮削就直接用；B 方案下 TMDB 只补图
                        if series_nfo.get("tmdb_id") and not (
                            series.poster_path or series.primary_image_url
                        ):
                            details = ctx.nfo_series_details.get(series_guid)
                            if details is not None:
                                _tmdb.tmdb_client.apply_images(series, details)
                        nfo_lib.apply_nfo(series, series_nfo, "series")
                        stats["nfo"] = stats.get("nfo", 0) + 1

                    season_guid = _pending.season_guid
                    season_item = _ensure_season_row(
                        db, ctx, library, series, season_no, season_guid)
                    if _pending.season_nfo_data and not season_item.overview:
                        # 季 NFO（season.nfo）：只补简介与评分
                        nfo_lib.apply_nfo(season_item, _pending.season_nfo_data, "season")
                    item.parent_id = season_item.id
                    item.season_number = season_no
                    item.episode_number = ep_no
                    ep_nfo = _pending.nfo_data
                    ep_title = (ep_nfo or {}).get("title")
                    item.name = _episode_display_name(ep_title or parsed["name"], season_no, ep_no)
                    if ep_nfo:
                        # 单集 NFO：简介与评分（标题已用于展示名）
                        nfo_lib.apply_nfo(item, ep_nfo, "episode")
                        stats["nfo"] = stats.get("nfo", 0) + 1

                # TMDB 刮削：只刮剧集/电影这类顶层条目。
                # 旧实现对**每一集**也发搜索请求，不但浪费配额，还会把电影元数据
                # 写进 episode（污染 tmdb_id / 简介 / 图片），并让“只补缺”策略失效。
                if item_type in ("series", "movie"):
                    kind = "series" if item_type == "series" else "movie"
                    # 图片丢失的条目（repair_requested_at）无论策略如何都要重取图
                    needs_repair = bool(item.repair_requested_at)
                    nfo_data = _pending.nfo_data
                    if nfo_data and nfo_data.get("tmdb_id"):
                        # —— NFO 优先（B 方案）：远端已有刮削，不调 TMDB 搜索 ——
                        # TMDB 只补图；NFO 里没给 imdb_id 时才用详情补 imdb/别名缺项，
                        # 最后 NFO 文本覆盖（用户整理的标题/简介/评分优先于 TMDB）。
                        if not nfo_data.get("imdb_id") and (
                            needs_repair or not (item.imdb_id and item.aliases)
                        ):
                            _tmdb.tmdb_client.apply_details(item, _pending.tmdb_details)
                        if needs_repair or not (item.poster_path or item.primary_image_url):
                            _tmdb.tmdb_client.apply_images(item, _pending.tmdb_details)
                        nfo_lib.apply_nfo(item, nfo_data, kind)
                        stats["nfo"] = stats.get("nfo", 0) + 1
                        if needs_repair:
                            item.repair_requested_at = None
                            stats["repaired"] = stats.get("repaired", 0) + 1
                    else:
                        if needs_repair or should_scrape(item, snap.scrape_policy):
                            # 搜索结果在批次开头就取回了（写事务里不发网络请求）
                            hit = _pending.tmdb_hit
                            if hit:
                                _tmdb.tmdb_client.apply(item, hit, kind)
                                stats["scraped"] += 1
                            if nfo_data:
                                # 有 NFO 但无 tmdb_id：TMDB 打底，NFO 文本优先
                                nfo_lib.apply_nfo(item, nfo_data, kind)
                                stats["nfo"] = stats.get("nfo", 0) + 1
                        elif nfo_data and not item.overview:
                            # 策略不要求重刮、但条目缺简介且远端有 NFO：补上（幂等，不调 TMDB）
                            nfo_lib.apply_nfo(item, nfo_data, kind)
                            stats["nfo"] = stats.get("nfo", 0) + 1
                        # 已有 TMDB 命中但缺 IMDb Id / 多别名 → 用详情接口补齐
                        # （中英文、繁简、多别名搜索依赖 aliases）
                        # 详情同样是预取的：apply_details / apply_images 只把已取回的
                        # 数据落到条目上，不会在这里发请求（与 enrich / refresh_images 同口径）。
                        # v2.42.9 起图片也一样：下载在 _prepare_and_prefetch 的
                        # prewarm_images 里完成，这里的 _set_image 只查本地文件
                        if item.tmdb_id and (needs_repair or not (item.imdb_id and item.aliases)):
                            if not needs_repair or _tmdb.tmdb_client.apply_images(item, _pending.tmdb_details):
                                _tmdb.tmdb_client.apply_details(item, _pending.tmdb_details)
                            if needs_repair:
                                item.repair_requested_at = None
                                stats["repaired"] = stats.get("repaired", 0) + 1

                # 剧集海报回退：集 → 季 → 剧集（避免整库集图空白）
                if item_type == "episode" and series is not None:
                    sources = [x for x in (season_item, series) if x is not None]
                    if not (item.poster_path or item.primary_image_url):
                        for src in sources:
                            if src.poster_path or src.primary_image_url:
                                item.poster_path = src.poster_path
                                item.primary_image_url = src.primary_image_url
                                break
                    if not (item.backdrop_path or item.backdrop_image_url):
                        for src in sources:
                            if src.backdrop_path or src.backdrop_image_url:
                                item.backdrop_path = src.backdrop_path
                                item.backdrop_image_url = src.backdrop_image_url
                                break

                # 内封轨道：仅在真正探测过时重建（未探测则保留旧轨道）
                if probe is not None:
                    # 新建条目此时还没落库，item.id 是 None：直接拿它写轨道的
                    # item_id 会在 flush 时撞 NOT NULL 约束，把**整库**扫描带崩
                    # （任何一批里的任何一个新条目都能触发，旧条目有 id 所以不炸）。
                    # 这里先把 item 落库拿到主键，再建轨道。
                    if item.id is None:
                        db.flush()
                    db.query(emby_models.MediaStream).filter(
                        emby_models.MediaStream.item_id == item.id,
                        emby_models.MediaStream.is_external.isnot(True),
                    ).delete(synchronize_session=False)
                    for s in probe["streams"]:
                        stream = emby_models.MediaStream(item_id=item.id, **{
                            k: v for k, v in s.items()
                            if k in _PROBE_STREAM_COLS
                        })
                        # 超长 title/display_title 截断（ffprobe 描述句不受控）
                        emby_models.sanitize_stream_strings(stream)
                        db.add(stream)

                # 外挂字幕在批次开头就找好了（本地目录 / 挂载目录都在 IO 线程池里列过），
                # 这里只剩纯 DB 写：绝大多数条目根本没有外挂字幕，也就不必为「给新字幕轨
                # 分配 stream_index」再查一次 max()——十万条目的库就是十万次多余的 SELECT。
                # 外挂字幕总是刷新（与视频探测无关），
                # 并且需要合成 stream_index：客户端靠它拼
                # /Videos/{id}/{mid}/Subtitles/{Index}/Stream.{Format}，
                # 旧实现不写 stream_index（None），字幕地址会变成 Subtitles/None 无法拉取。
                # v2.54.0: 无字幕且 DB 无记录时跳过 flush + DELETE（十万文件省十万次无用写）。
                _sub_db_paths = ctx.ext_subtitles.get(item.id) or set()
                if _ext_subtitles_need_refresh(external, _sub_db_paths):
                    if item.id is None:
                        db.flush()
                    db.query(emby_models.MediaStream).filter(
                        emby_models.MediaStream.item_id == item.id,
                        emby_models.MediaStream.is_external.is_(True),
                    ).delete(synchronize_session=False)
                next_index = 0
                if external and _ext_subtitles_need_refresh(external, _sub_db_paths):
                    if item.id is None:
                        db.flush()
                    next_index = db.query(func.max(emby_models.MediaStream.stream_index)).filter(
                        emby_models.MediaStream.item_id == item.id
                    ).scalar() or 0
                for offset, (lang, sub_path) in enumerate(external, start=1):
                    stream = emby_models.MediaStream(
                        item_id=item.id, stream_index=next_index + offset,
                        stream_type="Subtitle",
                        codec=os.path.splitext(sub_path)[1].lstrip("."),
                        language=lang, display_title=os.path.basename(sub_path),
                        is_default=(offset == 1),
                        is_external=True, external_path=sub_path,
                    )
                    # 字幕文件名长度不可控（用户自己起的），写库前截断
                    emby_models.sanitize_stream_strings(stream)
                    db.add(stream)
                db.commit()
                # Drive 链接分享（2026-10-19 播放事故治本）：新入库条目异步补开
                # anyone reader 权限，否则播放 302 到 usercontent 匿名直链会被
                # 谷歌登录页拦截。纯内存入队、无 IO，提交后执行不阻塞扫描；
                # 补开失败绝不影响入库，由 enrich janitor 巡检兜底。
                if is_new and _fid:
                    try:
                        from backend.emby_server import drive_share as _dsh
                        _dsh.note_new_file(item.id, _fid, True)
                    except Exception:
                        logger.warning("drive-share 入队失败（巡检会兜底）",
                                       exc_info=True)
            counted = _FileCounter(source_files)
            _iter_prepared(ctx, counted, pool, db, _process_one,
                            label, counted, stats, failed_roots)
    finally:
        # 扫描标志与结果由 scan_library_sync 统一写回（成功 / 部分失败 / 异常三条路都覆盖）
        try:
            commit_with_retry(db, label="扫描中间状态")
        except Exception as e:  # noqa: BLE001
            logger.warning("回写扫描中间状态失败: %s", e)
            db.rollback()

    stats["failed_roots"] = failed_roots
    if getattr(snap, "targeted", False):
        # v2.52.0 定向扫描（Drive Changes）：只看了部分目录，看不到的不能当成
        # "文件已删除"，跳过清理阶段。删除检测留给整库扫描做。
        # v2.56.x：标记为预期的跳过——finish_scan 据此不判 partial、不报
        # "没有可用来源"误报（定向扫描是正常行为，不是来源故障）。
        stats["removal_skipped"] = True
        stats["targeted_scan"] = True
        logger.info("媒体库「%s」定向扫描，跳过清理阶段", snap.name)
    elif failed_roots or not (snap.paths or snap.mount_ids):
        # 来源列表不完整：禁止清理，宁肯多留几条记录，也不能误删整库
        stats["removal_skipped"] = True
        logger.warning(
            "媒体库「%s」来源列表不完整（%s），跳过清理阶段",
            snap.name, "; ".join(str(r) for r in failed_roots) or "无来源",
        )
    else:
        # 移除已不存在的文件条目（电影/集）；series/season 无实体文件，仅在没有子条目时清理
        stats["removed"] += _remove_missing_items(db, library, seen_guids)
        # 删除保护触发过就把原因写进本轮 stats：接口/前端能看出“本轮没删任何东西是因为被拦了”，
        # 而不是以为“库里本来就空”（日志有了，但排障时看不到的是这个）
        if _CLEANUP_LAST.get("skipped"):
            stats["removal_guard"] = _CLEANUP_LAST["skipped"]
            stats["removal_skipped"] = True

    # WS1：显式可见口径 —— merged_into_id 无全局过滤，必须显式剔除合并副本
    #（与 portal.admin_overview 同类；索引齐全，仍是单条 COUNT 查询）
    library.item_count = _soft_delete.count_visible(
        db,
        emby_models.MediaItem.library_id == library.id,
        emby_models.MediaItem.item_type.in_(["movie", "series"]),
        emby_models.MediaItem.deleted_at.is_(None),
        emby_models.MediaItem.merged_into_id.is_(None),
    )
    # 单集多版本：扫描结束后唤醒合并 worker（best-effort，零查询）。
    # 合并本身在 merge_versions_worker 后台做，不占用扫描的资源预算；
    # 同一进程（worker）内 nudge 即时生效，跨进程时由 worker 周期兜底。
    try:
        from backend.emby_server import merge_versions_worker as _mvw
        _mvw.nudge()
    except Exception:  # noqa: BLE001 — 唤醒失败不影响扫描结果
        pass
    # v2.55.x 翻转监控：本轮 done→pending 翻转数进 stats，finish_scan 汇总告警。
    # 明细在 ctx.enrich_flips（内存），告警事件写 PG（持久，容器重建不丢）。
    stats["enrich_flips"] = len(ctx.enrich_flips)
    if ctx.enrich_flips:
        _flip_reasons: dict = {}
        _flip_sample_ids: list = []
        _flip_item_types: dict = {}
        for _f in ctx.enrich_flips:
            _r = str(_f.get("reason", "?"))
            _flip_reasons[_r] = _flip_reasons.get(_r, 0) + 1
            _it = str(_f.get("item_type", "?"))
            _flip_item_types[_it] = _flip_item_types.get(_it, 0) + 1
            if len(_flip_sample_ids) < 10:
                _flip_sample_ids.append(_f.get("item_id"))
        stats["enrich_flip_reasons"] = _flip_reasons
        stats["enrich_flip_sample_ids"] = _flip_sample_ids
        stats["enrich_flip_item_types"] = _flip_item_types
    db.commit()
    return stats

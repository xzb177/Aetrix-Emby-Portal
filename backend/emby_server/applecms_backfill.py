# -*- coding: utf-8 -*-
"""苹果 CMS 智能补源：给存量剧行按多信号打分补其它源的映射。

来历（站主 2026-11-02 17:17 亲令「补别的源。补源这个功能能不能
优化，让他有头脑」）：五库建库后剩 42 部剧只有最大（8，已停用）
映射、不可播；旧的一次性补映射脚本只搜非凡/暴风两家、只按片名
归一化相等命中、年份两边都有才校验——CMS 行普遍缺年份，实际命
中几乎全是仅标题命中，同名不同作品错挂风险未测，译名对不齐的
直接未命中（42 部的成因）。本模块把补源升级为可复跑的正式能力：

- **多钥匙搜索**：一部剧生成多把钥匙逐个搜——中文名、TMDB 身
  份带来的原名/英文名/别名（行字段与 TMDB 详情有什么用什么）、
  去季号、去标点空格、繁简变体（复用 ``search`` 的 zhconv 口径，
  不另造转换）。钥匙来源与命中钥匙全程留痕。
- **多信号打分**：候选逐项打分并留分项——标题归一化（相等/包
  含/相似度）、年份、地区、类型分类、总集数接近度、主演/导演
  交集、简介相似度。总分三档：高置信自动补／中置信须多信号齐
  全才补／低置信拒绝并记录原因。硬否决（veto）：年份两边都有
  且差 ≥2、季号明确不同、国语/粤语变体对不上、总集数差到离谱
  （行 ≥6 集时候选不足一半或超过两倍）。宁可补不上也不许为了
  凑数放松：剩几部补不上是可接受结果，错挂一部是事故。
- **补前验活**：候选 detail 必须有全直链组（沿用旧口径）；首
  选线路首集地址做一次轻量存活探测（复用取流侧探活口径），
  探不通的候选拒绝；仅 Range 探活被 403 时回落一次普通 GET
  复验、拉到清单才算活（ppqrrs 类 CDN 单一 Range 口径误杀，
  2026-11-02 生产实证，见 ``probe_first_episode``）。停用源与
  未启用源不搜（行自带映射的源只
  许作只读参照资料，不作候选）。
- **质量门同口径**：候选过既有质量门——短剧/微短剧/竖屏/竖版
  分类跳过；解说/预告/花絮/资讯分类不收；片名或备注带 CAM/TC/
  TS/枪版/抢先标记、明确标 SD/标清/低清的跳过。判据与同步/扩
  抓/旧补映射脚本同一套（本模块是这套判据在仓库内的落点，旧
  脚本在 ~/workspace/tmp，不在仓库内）。
- **集数形态校验**沿用旧口径：单集组只在行本身单集时接受；
  补映射走 ``ensure_series_tree(existing_series=行)`` 补到已有
  集行，剧行零新增；逐（行×源）提交、单条失败回滚不污染整库。
- **审计留痕**：每次运行产出结构化结果（条目级 audit 列表，
  例程落 JSON 文件）：每行补了哪个源、总分与分项、用哪把钥匙
  命中；被拒候选及拒绝原因；未命中行的原因分类。这是错挂事
  后能定位、能按行回滚映射的依据。

取流认不认库外源映射（本单亲读定论）：``upstream_playback``
的展示与取流口（``get_displayable_mappings`` /
``resolve_mapped_upstream_stream``）只按映射行本身＋源存在且启
用来裁决，**从不查库的 ``upstream_source_ids``**——该列只有同
步灌入面与管理端在消费。所以映射补到任何已启用源即可播，不
需要动库配置。

本模块不读 env 阈值：打分阈值是代码常量（见下），改阈值走
PR，不留半死开关。
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import applecms_sync as acs
from backend.emby_server import search as search_norm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 打分阈值（可配常量；依据见模块头与各项注释，改动走 PR）
# ---------------------------------------------------------------------------

#: 高置信：总分 ≥70 且标题分 ≥30（标题相等/强包含级别）。
TIER_HIGH_MIN = 70
TIER_HIGH_TITLE_MIN = 30
#: 中置信：总分 ≥52、标题分 ≥24，且标题之外至少 2 个信号族有
#: 非零分（年份/集数/地区/类型/主演/导演/简介）——单靠标题凑
#: 分不许补，这正是旧脚本「仅标题命中」的教训。
TIER_MEDIUM_MIN = 52
TIER_MEDIUM_TITLE_MIN = 24
TIER_MEDIUM_MIN_FAMILIES = 2

#: 年份硬否决线：两边都有年份且差 ≥2 视为不同作品。
YEAR_VETO_DIFF = 2
#: 集数硬否决：行集数 ≥6 时，候选集数不足行的一半或超过两倍。
EPISODE_VETO_MIN_ROW_EPS = 6
EPISODE_VETO_LOW_RATIO = 0.5
EPISODE_VETO_HIGH_RATIO = 2.0

#: 每行每源最多试的搜索钥匙数、最多拉 detail 打分的候选数。
KEYS_PER_SOURCE = 4
CANDIDATES_PER_SOURCE = 3
#: 每行默认最多新增几个源：补上一个可播源即达标即停，避免为
#: 了凑线路数把请求量打满；例程可用参数放宽。
DEFAULT_MAX_NEW_SOURCES_PER_ROW = 1

# ---------------------------------------------------------------------------
# 质量门（与同步/扩抓同一套判据；仓库内落点）
# ---------------------------------------------------------------------------

#: 竖版短剧：分类/类型名带这些词直接跳过（2026-11-02 16:49 口径）。
SHORT_DRAMA_TOKENS = ("短剧", "微短剧", "竖屏", "竖版")
#: 非内容分类：解说/预告/花絮/资讯不收（2026-11-02 15:05 质量门）。
NON_CONTENT_TOKENS = ("解说", "预告片", "预告", "花絮", "资讯")
#: 枪版类标记（片名/备注）：中文词 + 英文词边界（CAM/TC/TS 单
#: 独出现才算，嵌在英文单词里不算）。与扩抓 lane
#: （cms_full_run4.qfail）同口径：裸「抢先」已含「抢先版/
#: 抢先看」，「影院版」亦属枪版信号。
BAD_VERSION_CN_TOKENS = ("枪版", "抢先", "影院版")
# 英文标记的边界不能用 \b：汉字在正则里算词字符，「CAM版」这类
# 中英相邻写法 \b 不成立。改用「两侧不是 ASCII 字母」的环视。
# 词表与扩抓 lane 一致（含 HDTS/TELESYNC/TELECINE）。
_BAD_VERSION_EN_RE = re.compile(
    r"(?<![A-Za-z])(CAM|TC|TS|HDTS|HDTC|TELESYNC|TELECINE)(?![A-Za-z])",
    re.IGNORECASE)
#: 明确低清标记：只认明确写出的，无信号不猜杀。
LOW_QUALITY_CN_TOKENS = ("标清", "低清")
_LOW_QUALITY_EN_RE = re.compile(
    r"(?<![A-Za-z])SD(?![A-Za-z])", re.IGNORECASE)


def class_short_drama_ids(payload: dict) -> set:
    """provide ``class`` 分类树 → 短剧类 type_id 集合。

    与同步侧同门判定同一算法（PA-079 不另造第二套）：自身或任
    一祖先分类名命中 SHORT_DRAMA_TOKENS 即是；源不给
    ``type_pid`` 时只认自身名；树缺失/成环返回已确认部分，不
    猜。缺树时返回空集，调用方只认自身名降级，绝不反向放行。
    """
    nodes = {}
    for node in (payload or {}).get("class") or []:
        if not isinstance(node, dict):
            continue
        try:
            tid = int(node.get("type_id"))
        except (TypeError, ValueError):
            continue
        try:
            pid = int(node.get("type_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        nodes[tid] = (pid, str(node.get("type_name") or "").strip())
    short = set()
    for tid in nodes:
        cur, seen = tid, set()
        while cur in nodes and cur not in seen:
            seen.add(cur)
            parent, cur_name = nodes[cur]
            if any(m in cur_name for m in SHORT_DRAMA_TOKENS):
                short.add(tid)
                break
            if parent == 0 or parent not in nodes:
                break
            cur = parent
    return short


def _is_short_drama_entry(entry: dict, short_ids) -> bool:
    """条目短剧判定（纯函数，与同步侧同门判定同口径）：分类 id
    命中短剧类集合，或条目自身 type_name / vod_class / vod_remarks
    任一带短剧标记。片名不作证据。"""
    try:
        tid = int(entry.get("type_id"))
    except (TypeError, ValueError):
        tid = None
    if tid is not None and tid in (short_ids or set()):
        return True
    for field in ("type_name", "vod_class", "vod_remarks"):
        text = str(entry.get(field) or "")
        if any(m in text for m in SHORT_DRAMA_TOKENS):
            return True
    return False


def version_quality_block(name: str = "", remarks: str = "") -> Optional[str]:
    """片名/备注级枪版/低清判定（与 quality_gate_reject 同判据）。

    只查枪版/低清两层，不含分类/短剧层，供同步侧
    （applecms_library_sync._process_entry）复用——仓库内唯一
    落点，不另造第二套。无信号不杀：名与备注皆空时返回 None。
    返回 ``bad_version`` / ``low_quality``，未命中返回 None。
    """
    text = f"{name or ''} {remarks or ''}"
    for token in BAD_VERSION_CN_TOKENS:
        if token in text:
            return "bad_version"
    if _BAD_VERSION_EN_RE.search(text):
        return "bad_version"
    for token in LOW_QUALITY_CN_TOKENS:
        if token in text:
            return "low_quality"
    if _LOW_QUALITY_EN_RE.search(text):
        return "low_quality"
    return None


def quality_gate_reject(type_name: str = "", name: str = "",
                        remarks: str = "", *, vod_class: str = "",
                        short_ids=None, type_id=None) -> Optional[str]:
    """质量门：命中返回拒绝原因，未命中返回 None。

    三层入参对应 CMS 条目的分类/类型名、片名、备注——list 层与
    detail 层都用同一函数查（detail 层字段更全，调用方两层都
    查）。短剧分支的分类证据（PA-079）：分类 id 命中调用方传
    入的短剧类集合（search payload 的 class 树算出），或
    type_name / vod_class / remarks 任一带短剧标记；片名不作短
    剧证据（与同步侧同口径）。缺 class 树时 short_ids 为空，只
    认自身名降级，不反向放行。返回原因取值：``short_drama`` /
    ``non_content`` / ``bad_version`` / ``low_quality``。
    """
    if type_id is not None:
        try:
            if int(type_id) in (short_ids or set()):
                return "short_drama"
        except (TypeError, ValueError):
            pass
    for text in (type_name or "", vod_class or "", remarks or ""):
        for token in SHORT_DRAMA_TOKENS:
            if token in text:
                return "short_drama"
    for token in NON_CONTENT_TOKENS:
        if token in (type_name or ""):
            return "non_content"
    blocked = version_quality_block(name, remarks)
    if blocked is not None:
        return blocked
    return None


# ---------------------------------------------------------------------------
# 标题小工具：去季号、季号解析、语言变体、地区桶
# ---------------------------------------------------------------------------

_SEASON_CN_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百]+)\s*[季部]")
_SEASON_EN_RE = re.compile(r"[Ss]eason\s*([0-9]+)")
_SEASON_SXX_RE = re.compile(r"\bS([0-9]{1,2})\b")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100}


def _cn_number(text: str) -> Optional[int]:
    """中文数字（≤99）转 int；解析不了返回 None（不猜）。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    return None


def season_number_of(name: str) -> Optional[int]:
    """从片名解析季号（第X季 / Season N / Sxx）；没有返回 None。"""
    text = name or ""
    match = _SEASON_CN_RE.search(text)
    if match:
        return _cn_number(match.group(1))
    match = _SEASON_EN_RE.search(text) or _SEASON_SXX_RE.search(text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def strip_season(name: str) -> str:
    """去掉季号标记（第X季/Season N/Sxx）与尾部语言变体括号。

    只用于生成搜索钥匙与打分时的季号无关比对；原名保留在别处，
    季号是否一致另有显式校验（见 score 的 season 否决）。
    """
    text = name or ""
    text = _SEASON_CN_RE.sub("", text)
    text = _SEASON_EN_RE.sub("", text)
    text = _SEASON_SXX_RE.sub("", text)
    return text.strip(" ·-—_（(）)")


_SEASON_CN_SUFFIX_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百]+)\s*[季部]$")
_SEASON_EN_SUFFIX_RE = re.compile(r"[Ss]eason\s*([0-9]+)$")
_SEASON_SXX_SUFFIX_RE = re.compile(r"(?<![A-Za-z0-9])S([0-9]{1,2})$")
_TRAILING_NUMBER_RE = re.compile(r"([0-9]{1,2})$")
_ANIM_SUFFIXES = ("动画版", "动画")


def season_suffix_parts(name: str):
    """片名尾部季号后缀解析（按季折叠通道专用，与 season_number_of 同源）。

    全仓标题季号解析只许这一套实现：本函数复用本模块的季号正则与
    中文数字口径，专供「同剧按季拆行」治理使用。

    返回 (底名, 季号|None, 标记类)：
    - 标记类 "explicit"：第X季/第X部、Season N、Sxx 明确标记；
    - "digit"：纯尾数（如「不良人7」）——只报数、不许自动折，且底名
      保留尾数（自成一组，不混进无尾数的底名组）；
    - None：无季号后缀。
    底名 = 去全部空白后反复剥尾部季号后缀与「动画版/动画」后缀的
    剩余原文（不做归一化，归一在调用方）。只认尾部，季号字样在
    名中部的不动；整名就是后缀时返回原名、无季号。
    """
    text = re.sub(r"\s+", "", name or "")
    if not text:
        return "", None, None
    season = None
    marker = None
    while True:
        m = _SEASON_CN_SUFFIX_RE.search(text)
        if m:
            num = _cn_number(m.group(1))
            if num is None:
                break
            if season is None:
                season, marker = num, "explicit"
            text = text[:m.start()]
            continue
        m = (_SEASON_EN_SUFFIX_RE.search(text)
             or _SEASON_SXX_SUFFIX_RE.search(text))
        if m:
            if season is None:
                season, marker = int(m.group(1)), "explicit"
            text = text[:m.start()]
            continue
        for suf in _ANIM_SUFFIXES:
            if text.endswith(suf) and len(text) > len(suf):
                text = text[:-len(suf)]
                break
        else:
            break
    if not text:
        return re.sub(r"\s+", "", name or ""), None, None
    if season is None:
        m = _TRAILING_NUMBER_RE.search(text)
        if m and len(text) > len(m.group(1)):
            return text, int(m.group(1)), "digit"
        return text, None, None
    return text, season, marker


def lang_variant_of(name: str) -> Optional[str]:
    """片名语言变体标记：国语→guo、粤语→yue；没有返回 None。

    五库里存在同剧国语/粤语双行（各是一行），把粤语候选挂到国
    语行上等于配错音轨，所以变体对不上是硬否决信号。
    """
    text = name or ""
    if "粤语" in text or "粵語" in text:
        return "yue"
    if "国语" in text or "國語" in text:
        return "guo"
    return None


#: 地区桶：与地区分流的桶口径一致（港台归 cn、日韩合桶）。
#: 顺序有意义：先判 jpkr（「日韩」含韩字），再 cn，再 west。
AREA_BUCKET_KEYWORDS = (
    ("jpkr", ("日本", "韩国", "韓國", "日韩", "韩日")),
    ("cn", ("中国", "中國", "大陆", "大陸", "内地", "香港", "台湾",
            "臺灣", "华语", "華語", "国产", "國產", "港台")),
    ("west", ("美国", "美國", "英国", "英國", "法国", "法國",
              "德国", "德國", "加拿大", "澳大利亚", "澳大利亞",
              "意大利", "西班牙", "欧美", "歐美", "北美", "欧洲",
              "歐洲", "俄罗斯", "俄羅斯")),
)


def area_bucket_of(area_text: str) -> Optional[str]:
    """vod_area 文本 → 地区桶（cn/west/jpkr）；判不出返回 None。"""
    text = area_text or ""
    if not text:
        return None
    for bucket, keywords in AREA_BUCKET_KEYWORDS:
        for keyword in keywords:
            if keyword in text:
                return bucket
    return None


def _split_people(raw) -> List[str]:
    """主演/导演字段拆名：逗号/顿号/斜杠/空白分隔，去占位词。"""
    if not raw:
        return []
    parts = re.split(r"[,，、/\s]+", str(raw))
    out = []
    for part in parts:
        name = part.strip()
        if name and name not in ("内详", "未知", "暂无", "佚名"):
            out.append(name)
    return out


def _split_genres(raw) -> List[str]:
    """类型字段拆分（行 genres / 候选 vod_class 共用）。"""
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[,，、/|]", str(raw)) if p.strip()]


# ---------------------------------------------------------------------------
# 画像：行侧与候选侧
# ---------------------------------------------------------------------------


@dataclass
class RowProfile:
    """待补行画像：打分与搜索钥匙的全部行侧输入。"""

    item_id: int
    library_id: int
    name: str
    year: Optional[int] = None
    #: (标题文本, 来源种类)：name/original/alias/identity/tmdb。
    titles: List = field(default_factory=list)
    episode_count: int = 0
    expected_area_bucket: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    actors: List[str] = field(default_factory=list)
    directors: List[str] = field(default_factory=list)
    overview: str = ""
    lang_variant: Optional[str] = None


@dataclass
class CandidateProfile:
    """候选画像：从候选 detail（缺时回落 list 条目）抽取。"""

    vod_id: str
    name: str
    year: Optional[int] = None
    area: str = ""
    type_name: str = ""
    genres: List[str] = field(default_factory=list)
    actors: List[str] = field(default_factory=list)
    directors: List[str] = field(default_factory=list)
    overview: str = ""
    remarks: str = ""
    episode_count: int = 0
    lang_variant: Optional[str] = None


def build_search_keys(profile: RowProfile) -> List:
    """生成搜索钥匙 [(文本, 种类)]：多而有序、去重、保序。

    顺序即优先级：中文名 → TMDB 原名/别名 → 行别名 → 去季号 →
    繁简变体。归一化后为空或过短（<2 字）的钥匙丢弃；同归一形
    只留第一把（留痕记第一把的种类）。
    """
    keys: List = []
    seen = set()

    def _add(text, kind):
        text = (text or "").strip()
        if not text:
            return
        compact = search_norm.normalize_compact(text)
        if len(compact) < 2 or compact in seen:
            return
        seen.add(compact)
        keys.append((text, kind))

    _add(profile.name, "name")
    for text, kind in profile.titles:
        if kind in ("tmdb_original", "tmdb_alias"):
            _add(text, kind)
    for text, kind in profile.titles:
        if kind in ("original", "alias", "identity"):
            _add(text, kind)
    stripped = strip_season(profile.name)
    if stripped and stripped != profile.name:
        _add(stripped, "season_stripped")
    for text, _kind in list(keys):
        traditional = search_norm.to_traditional(text)
        if traditional and traditional != text:
            _add(traditional, "traditional")
        simplified = search_norm.to_simplified(text)
        if simplified and simplified != text:
            _add(simplified, "simplified")
    return keys


# ---------------------------------------------------------------------------
# 多信号打分
# ---------------------------------------------------------------------------


@dataclass
class ScoreResult:
    total: int
    parts: Dict[str, int]
    vetoes: List[str]
    tier: str  # "high" / "medium" / "reject"
    title_via: str = ""
    families: int = 0


def _title_score(profile: RowProfile, cand: CandidateProfile):
    """标题分（0–40）与命中标题来源。

    逐把行侧标题与候选名比：先比季号无关形（去季号后归一），
    相等 40；包含 30；再用 difflib 相似度折算。季号两边都有且
    不同时不在这里扣（由 score 的 season 否决统一处理）。
    """
    cand_compact = search_norm.normalize_compact(strip_season(cand.name))
    if not cand_compact:
        return 0, ""
    best = 0
    best_via = ""
    for text, kind in [(profile.name, "name")] + list(profile.titles):
        compact = search_norm.normalize_compact(strip_season(text))
        if not compact:
            continue
        if compact == cand_compact:
            score = 40
        elif len(compact) >= 3 and len(cand_compact) >= 3 and (
                compact in cand_compact or cand_compact in compact):
            score = 30
        else:
            ratio = difflib.SequenceMatcher(
                None, compact, cand_compact).ratio()
            if ratio >= 0.85:
                score = 32
            elif ratio >= 0.7:
                score = 24
            elif ratio >= 0.55:
                score = 14
            else:
                score = int(round(10 * ratio))
        if score > best:
            best = score
            best_via = kind
    return best, best_via


def _overview_score(row_overview: str, cand_overview: str) -> int:
    """简介相似度（0–10）：双字组 Jaccard，两边都有且够长才算。"""
    a = search_norm.normalize_compact(row_overview or "")
    b = search_norm.normalize_compact(cand_overview or "")
    if len(a) < 20 or len(b) < 20:
        return 0

    def _bigrams(text):
        return {text[i:i + 2] for i in range(len(text) - 1)}

    set_a, set_b = _bigrams(a), _bigrams(b)
    if not set_a or not set_b:
        return 0
    jaccard = len(set_a & set_b) / len(set_a | set_b)
    if jaccard >= 0.25:
        return 10
    if jaccard >= 0.12:
        return 6
    if jaccard >= 0.05:
        return 3
    return 0


def score_candidate(profile: RowProfile, cand: CandidateProfile,
                     *, audit: bool = False) -> ScoreResult:
    """多信号打分：分项 + 硬否决 + 三档裁决。

    ``audit=True`` 是存量抽查口径：集数否决降为重罚＋标记（存
    量行集数是多源并集，单源候选天然可能少于行集数，不能按补
    源否决口径误报），其余否决照常。
    """
    parts: Dict[str, int] = {}
    vetoes: List[str] = []

    title, title_via = _title_score(profile, cand)
    parts["title"] = title

    # 季号：两边都明确且不同 → 不同季，不同作品形态。
    row_season = season_number_of(profile.name)
    cand_season = season_number_of(cand.name)
    if (row_season is not None and cand_season is not None
            and row_season != cand_season):
        vetoes.append("season_mismatch")

    # 语言变体：国语行配粤语候选（或反之）→ 配错音轨，硬否决。
    cand_lang = cand.lang_variant or lang_variant_of(cand.name)
    if (profile.lang_variant and cand_lang
            and profile.lang_variant != cand_lang):
        vetoes.append("lang_variant_mismatch")

    # 年份：都有才咬合（CMS 行普遍缺年份，缺了不奖不罚）。
    if profile.year and cand.year:
        diff = abs(int(profile.year) - int(cand.year))
        if diff >= YEAR_VETO_DIFF:
            vetoes.append("year_mismatch")
            parts["year"] = 0
        elif diff == 0:
            parts["year"] = 15
        else:
            parts["year"] = 6
    else:
        parts["year"] = 0

    # 总集数：候选 detail 直链组集数 vs 行内已有集数。
    row_eps = int(profile.episode_count or 0)
    cand_eps = int(cand.episode_count or 0)
    if row_eps > 0 and cand_eps > 0:
        if cand_eps == row_eps:
            parts["episodes"] = 15
        else:
            ratio = cand_eps / row_eps
            far = (row_eps >= EPISODE_VETO_MIN_ROW_EPS and (
                ratio < EPISODE_VETO_LOW_RATIO
                or ratio > EPISODE_VETO_HIGH_RATIO))
            if far and not audit:
                vetoes.append("episode_count_mismatch")
                parts["episodes"] = 0
            elif far:
                parts["episodes"] = -20
            elif abs(cand_eps - row_eps) / row_eps <= 0.1:
                parts["episodes"] = 13
            elif abs(cand_eps - row_eps) / row_eps <= 0.3:
                parts["episodes"] = 8
            else:
                parts["episodes"] = 2
    else:
        parts["episodes"] = 0

    # 地区：候选 vod_area 桶 vs 行的期望桶（库地区口径）。
    cand_bucket = area_bucket_of(cand.area)
    if cand_bucket and profile.expected_area_bucket:
        parts["area"] = 10 if cand_bucket == profile.expected_area_bucket \
            else -15
    else:
        parts["area"] = 0

    # 类型：候选分类词（vod_class + type_name）与行 genres 交集。
    cand_genres = set(cand.genres)
    if cand.type_name:
        cand_genres.add(cand.type_name)
    if profile.genres and cand_genres:
        overlap = len(set(profile.genres) & cand_genres)
        parts["genre"] = min(10, 5 * overlap)
    else:
        parts["genre"] = 0

    # 主演/导演交集。
    if profile.actors and cand.actors:
        common = len(set(profile.actors) & set(cand.actors))
        parts["cast"] = min(18, 6 * common)
        if common == 0 and title < 40:
            parts["cast"] = -8
    else:
        parts["cast"] = 0
    if profile.directors and cand.directors:
        parts["director"] = 6 if set(profile.directors) & set(
            cand.directors) else 0
    else:
        parts["director"] = 0

    parts["overview"] = _overview_score(profile.overview, cand.overview)

    total = sum(parts.values())
    families = sum(1 for key in ("year", "episodes", "area", "genre",
                                  "cast", "director", "overview")
                   if parts.get(key, 0) != 0)
    if vetoes:
        tier = "reject"
    elif total >= TIER_HIGH_MIN and title >= TIER_HIGH_TITLE_MIN:
        tier = "high"
    elif (total >= TIER_MEDIUM_MIN and title >= TIER_MEDIUM_TITLE_MIN
            and families >= TIER_MEDIUM_MIN_FAMILIES):
        tier = "medium"
    else:
        tier = "reject"
    return ScoreResult(total=total, parts=parts, vetoes=vetoes,
                       tier=tier, title_via=title_via, families=families)


# ---------------------------------------------------------------------------
# 画像构建（DB / TMDB 详情 / 候选 detail）
# ---------------------------------------------------------------------------


def _titles_from_identity(raw) -> List:
    """identity_json 里能用的标题（有什么用什么，坏 JSON 不炸）。"""
    out: List = []
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return out
    if not isinstance(data, dict):
        return out
    for key in ("original_title", "original_name", "title", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out.append((value.strip(), "identity"))
    aliases = data.get("aliases") or data.get("alternative_titles")
    if isinstance(aliases, list):
        for value in aliases:
            if isinstance(value, str) and value.strip():
                out.append((value.strip(), "identity"))
            elif isinstance(value, dict):
                title = value.get("title") or value.get("name")
                if isinstance(title, str) and title.strip():
                    out.append((title.strip(), "identity"))
    return out


def _titles_from_tmdb_details(details) -> List:
    """TMDB 详情里的原名/别名（alternative_titles 两种形态都收）。"""
    out: List = []
    if not isinstance(details, dict):
        return out
    original = details.get("original_name") or details.get("original_title")
    if isinstance(original, str) and original.strip():
        out.append((original.strip(), "tmdb_original"))
    name = details.get("name") or details.get("title")
    if isinstance(name, str) and name.strip():
        out.append((name.strip(), "tmdb_alias"))
    alt = details.get("alternative_titles") or {}
    entries = []
    if isinstance(alt, dict):
        entries = alt.get("titles") or alt.get("results") or []
    elif isinstance(alt, list):
        entries = alt
    for entry in entries:
        if isinstance(entry, dict):
            title = entry.get("title") or entry.get("name")
            if isinstance(title, str) and title.strip():
                out.append((title.strip(), "tmdb_alias"))
    return out


def _people_from_tmdb_details(details):
    """TMDB 详情 credits → (演员名表, 导演名表)；created_by 算导演侧。"""
    actors: List[str] = []
    directors: List[str] = []
    if not isinstance(details, dict):
        return actors, directors
    credits = details.get("credits") or {}
    for person in credits.get("cast") or []:
        if isinstance(person, dict) and person.get("name"):
            actors.append(str(person["name"]).strip())
    for person in credits.get("crew") or []:
        if not isinstance(person, dict) or not person.get("name"):
            continue
        if person.get("job") == "Director" or \
                person.get("department") == "Directing":
            directors.append(str(person["name"]).strip())
    for person in details.get("created_by") or []:
        if isinstance(person, dict) and person.get("name"):
            directors.append(str(person["name"]).strip())
    return actors, directors


def build_row_profile(row, *, episode_count: int,
                      expected_area_bucket: Optional[str] = None,
                      tmdb_details=None, ref_detail=None) -> RowProfile:
    """组装行画像：行字段优先，TMDB 详情与参照 detail 只填空缺。

    ``ref_detail`` 是行自带映射源（多为已停用的最大）的 detail，
    只读参照：补演员/导演/地区/简介这类行本身没有的信号，绝不
    作候选。``tmdb_details`` 是 enrich 同口径的 TMDB 详情 dict。
    """
    titles: List = []
    original = getattr(row, "original_title", None)
    if original and str(original).strip():
        titles.append((str(original).strip(), "original"))
    for alias in search_norm.split_fields(getattr(row, "aliases", "")):
        titles.append((alias, "alias"))
    titles.extend(_titles_from_identity(getattr(row, "identity_json", None)))
    titles.extend(_titles_from_tmdb_details(tmdb_details))

    year = getattr(row, "production_year", None)
    if not year and isinstance(tmdb_details, dict):
        date_text = str(tmdb_details.get("first_air_date")
                        or tmdb_details.get("release_date") or "")
        year = acs._cms_year(date_text[:4]) if date_text else None
    if not year and isinstance(ref_detail, dict):
        year = acs._cms_year(ref_detail.get("vod_year"))

    genres = _split_genres(getattr(row, "genres", ""))
    if not genres and isinstance(tmdb_details, dict):
        for genre in tmdb_details.get("genres") or []:
            if isinstance(genre, dict) and genre.get("name"):
                genres.append(str(genre["name"]).strip())

    actors, directors = _people_from_tmdb_details(tmdb_details)
    overview = str(getattr(row, "overview", "") or "")
    if not overview and isinstance(tmdb_details, dict):
        overview = str(tmdb_details.get("overview") or "")
    if isinstance(ref_detail, dict):
        if not actors:
            actors = _split_people(ref_detail.get("vod_actor"))
        if not directors:
            directors = _split_people(ref_detail.get("vod_director"))
        if not overview:
            overview = str(ref_detail.get("vod_content")
                           or ref_detail.get("vod_blurb") or "")
    return RowProfile(
        item_id=int(row.id), library_id=int(row.library_id),
        name=str(row.name or ""), year=year, titles=titles,
        episode_count=int(episode_count or 0),
        expected_area_bucket=expected_area_bucket, genres=genres,
        actors=actors, directors=directors, overview=overview,
        lang_variant=lang_variant_of(str(row.name or "")))


def candidate_from_detail(detail: dict, entry: Optional[dict] = None,
                          episode_count: int = 0) -> CandidateProfile:
    """候选画像：detail 为主，缺字段回落 list 条目同名字段。"""
    src = detail if isinstance(detail, dict) else {}
    fallback = entry if isinstance(entry, dict) else {}

    def _pick(key):
        value = src.get(key)
        if value in (None, ""):
            value = fallback.get(key)
        return value

    return CandidateProfile(
        vod_id=str(_pick("vod_id") or ""),
        name=str(_pick("vod_name") or ""),
        year=acs._cms_year(_pick("vod_year")),
        area=str(_pick("vod_area") or ""),
        type_name=str(_pick("type_name") or ""),
        genres=_split_genres(_pick("vod_class")),
        actors=_split_people(_pick("vod_actor")),
        directors=_split_people(_pick("vod_director")),
        overview=str(_pick("vod_content") or _pick("vod_blurb") or ""),
        remarks=str(_pick("vod_remarks") or ""),
        episode_count=int(episode_count or 0),
        lang_variant=lang_variant_of(str(_pick("vod_name") or "")))


# ---------------------------------------------------------------------------
# DB 读口（映射来源集合 / 集数 / 目标行）
# ---------------------------------------------------------------------------


def series_mapping_sources(db, series_id: int) -> set:
    """一部剧的集行映射来源集合（与分布快照同一口径）。"""
    from backend.emby_server import models as em
    ep_ids = [r[0] for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.series_id == series_id,
        em.MediaItem.item_type == "episode").all()]
    if not ep_ids:
        return set()
    return {r[0] for r in db.query(em.UpstreamItemMapping.source_id).filter(
        em.UpstreamItemMapping.local_item_id.in_(ep_ids),
        em.UpstreamItemMapping.episode_index.isnot(None)).distinct().all()}


def series_episode_count(db, series_id: int) -> int:
    from backend.emby_server import models as em
    return (db.query(em.MediaItem).filter(
        em.MediaItem.series_id == series_id,
        em.MediaItem.item_type == "episode",
        em.MediaItem.deleted_at.is_(None)).count())


def series_mapping_vod_id(db, series_id: int, source_id: int) -> Optional[str]:
    """取某剧在某源的一条映射 vod_id（参照 detail / 抽查用）。"""
    from backend.emby_server import models as em
    row = (db.query(em.UpstreamItemMapping.upstream_item_id)
           .join(em.MediaItem,
                 em.MediaItem.id == em.UpstreamItemMapping.local_item_id)
           .filter(em.MediaItem.series_id == series_id,
                   em.MediaItem.item_type == "episode",
                   em.UpstreamItemMapping.source_id == source_id,
                   em.UpstreamItemMapping.episode_index.isnot(None))
           .first())
    return str(row[0]) if row and row[0] else None


def find_unplayable_series(db, library_ids, enabled_source_ids) -> List:
    """目标行：集行映射来源与已启用源零交集的剧行（即不可播）。"""
    from backend.emby_server import models as em
    enabled = set(enabled_source_ids or [])
    rows = (db.query(em.MediaItem).filter(
        em.MediaItem.library_id.in_(list(library_ids)),
        em.MediaItem.item_type == "series",
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None))
        .order_by(em.MediaItem.library_id, em.MediaItem.id).all())
    out = []
    for row in rows:
        if not (series_mapping_sources(db, row.id) & enabled):
            out.append(row)
    return out


def enabled_applecms_sources(db, library=None) -> List:
    """已启用苹果 CMS 源：库自配源优先，其余按 id 升序。

    停用源（含最大 8）从不出现在结果里——不搜、不作候选。
    """
    from backend.emby_server import models as em
    sources = (db.query(em.UpstreamSource).filter(
        em.UpstreamSource.source_type == "applecms",
        em.UpstreamSource.enabled.is_(True))
        .order_by(em.UpstreamSource.id).all())
    if library is not None:
        from backend.emby_server import applecms_library as acl
        preferred = acl.parse_upstream_source_ids(
            getattr(library, "upstream_source_ids", ""))
        rank = {sid: idx for idx, sid in enumerate(preferred)}
        sources.sort(key=lambda s: (rank.get(s.id, len(rank)), s.id))
    return sources


# ---------------------------------------------------------------------------
# 补前验活（复用取流侧探活口径）
# ---------------------------------------------------------------------------


#: 补前探活 403 回落复验（普通 GET）的超时（秒）：只在 Range 探
#: 活被 403 拒绝时走、一次为限；补源是离线批处理，慢死不许久等。
_PROBE_GET_FALLBACK_TIMEOUT_SEC = 5.0


def probe_first_episode(detail: dict, headers: dict, *,
                        http_probe: Optional[Callable] = None,
                        http_get_text: Optional[Callable] = None) -> bool:
    """首选直链组首集地址存活探测：取流侧探活口径，200/206 算活。

    探测层默认复用 ``upstream_playback._default_http_range_probe``
    （Range 读 1 字节、有界跟随重定向并逐跳复验出站地址——上游
    以 302 发流是常见形态，不跟随会把可播内容误判死，2026-10-31
    生产实证）。探不通返回 False，调用方拒绝该候选。

    403 回落复验（2026-11-02 生产实证）：ppqrrs 类 CDN 对 Range
    请求回 403，普通 GET 却能拉到真清单（《爆笑虫子在中国》5 个
    高置信候选全被单一 Range 口径误杀），而播放器拉清单本就不用
    Range。故仅当 Range 探活最终状态为 403 时，回落一次无 Range
    的普通 GET（默认复用取流侧 ``_default_http_get_text_followed``：
    有界跟随、逐跳复验出站地址、只读一小段），200 且内容是清单
    （#EXTM3U 开头）才算活；GET 也失败或非清单仍判死。其余失败
    形态（404／超时／断连／5xx）不回落、维持原判死口径；403 本
    身更不许一律放行——服务器侧对这类 CDN 偶发 GET 403，只能
    给一次复验机会，不能假设 GET 必成功。
    """
    groups = acf.parse_play_groups(
        (detail or {}).get("vod_play_from"), (detail or {}).get("vod_play_url"))
    direct = acf.pick_direct_group(groups)
    if not direct:
        return False
    address = acf.episode_address(direct, 1)
    if not address:
        return False
    if http_probe is None:
        from backend.emby_server.upstream_playback import (
            _default_http_range_probe)
        http_probe = _default_http_range_probe
    try:
        status, _retry_after = http_probe(
            address, dict(headers or {}), 10.0)
    except Exception:  # noqa: BLE001 - 探测失败即不活，不外溢
        return False
    if status in (200, 206):
        return True
    if status != 403:
        return False
    from backend.emby_server.upstream_playback import _is_playlist_text
    if http_get_text is None:
        from backend.emby_server.upstream_playback import (
            _default_http_get_text_followed)
        http_get_text = _default_http_get_text_followed
    try:
        get_status, text = http_get_text(
            address, dict(headers or {}),
            _PROBE_GET_FALLBACK_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 - 复验失败即不活，不外溢
        return False
    return get_status == 200 and _is_playlist_text(text)


# ---------------------------------------------------------------------------
# 补源主流程
# ---------------------------------------------------------------------------

_TIER_RANK = {"reject": 0, "medium": 1, "high": 2}

_VETO_REASONS = ("year_mismatch", "season_mismatch",
                 "lang_variant_mismatch", "episode_count_mismatch")


def _pick_unresolved_reason(reasons: List[str], saw_shape_reject: bool) -> str:
    """未命中原因分类：按信息量取，不按时间顺序取。

    硬否决（年份/季号/语言变体/集数）＞低置信＞验活失败＞写失败
    ＞形态拒绝（无直链组等）＞无候选＞源错误。否则最后一个源的
    网络错误会盖掉前面源给出的实质拒绝原因，审计就失真了。
    """
    for reason in reasons:
        if reason in _VETO_REASONS:
            return reason
    for reason in ("low_confidence", "probe_failed", "write_failed"):
        if reason in reasons:
            return reason
    if saw_shape_reject:
        return "no_direct_group"
    if "no_candidate" in reasons:
        return "no_candidate"
    return reasons[-1] if reasons else "no_source_available"


def _fetch_ref_detail(db, row_id: int, have: set, http_get) -> Optional[dict]:
    """行自带映射的参照 detail（只读；源停用也只影响候选资格，
    参照资料照常可读——参照的是这部剧在旧源上的条目本身）。"""
    from backend.emby_server import upstream_sources as us
    from backend.emby_server.applecms_library_sync import _source_headers
    for source_id in sorted(have):
        vod_id = series_mapping_vod_id(db, row_id, source_id)
        if not vod_id:
            continue
        source = us.get_source(db, source_id)
        if source is None:
            continue
        try:
            return acf.fetch_detail(
                source.base_url, vod_id, http_get=http_get,
                headers=_source_headers(source))
        except Exception:  # noqa: BLE001 - 参照拿不到就降级，不炸主流程
            continue
    return None


def run_backfill(db, library, rows, *, expected_area_bucket=None,
                 apply: bool = False, http_get=None, http_probe=None,
                 http_get_text=None,
                 tmdb_details: Optional[Callable] = None,
                 max_new_sources_per_row: int =
                 DEFAULT_MAX_NEW_SOURCES_PER_ROW,
                 keys_per_source: int = KEYS_PER_SOURCE,
                 candidates_per_source: int = CANDIDATES_PER_SOURCE,
                 delay_sec: float = 0.0, sleep_fn=None) -> dict:
    """对一批剧行智能补源。返回 {"stats": ..., "audit": [...]}。

    ``apply=False`` 为干跑：搜索、拉 detail、打分、验活全做，
    只不写映射。``apply=True`` 时逐（行×源）调
    ``ensure_series_tree(existing_series=行)`` 并单独提交，单条
    失败回滚不污染其它行。映射开关关着时 apply 直接整批拒绝。
    """
    from backend.emby_server import applecms_series as acser
    from backend.emby_server import upstream_item_mappings as uim
    from backend.emby_server.applecms_library_sync import _source_headers
    sleep = sleep_fn if sleep_fn is not None else time.sleep
    stats = {"rows": 0, "filled": 0, "unresolved": 0, "mappings_added": 0,
             "searches": 0, "details": 0, "quality_skipped": 0,
             "probe_failed": 0, "errors": 0,
             "unresolved_reasons": {}}
    audit: List[dict] = []
    if apply and not uim.mapping_enabled():
        return {"stats": stats, "audit": audit,
                "blocked": "mapping_off"}
    sources = enabled_applecms_sources(db, library)

    for row in rows:
        stats["rows"] += 1
        have = series_mapping_sources(db, row.id)
        ep_count = series_episode_count(db, row.id)
        details_data = None
        if tmdb_details is not None and getattr(row, "tmdb_id", None):
            try:
                details_data = tmdb_details(str(row.tmdb_id))
            except Exception:  # noqa: BLE001 - 身份补强失败降级
                details_data = None
        ref_detail = _fetch_ref_detail(db, row.id, have, http_get)
        profile = build_row_profile(
            row, episode_count=ep_count,
            expected_area_bucket=expected_area_bucket,
            tmdb_details=details_data, ref_detail=ref_detail)
        keys = build_search_keys(profile)
        row_audit = {"item_id": int(row.id), "name": row.name,
                     "library_id": int(row.library_id),
                     "have_before": sorted(have), "gained": [],
                     "attempts": [], "unresolved_reason": None}
        gained = 0
        reasons: List[str] = []
        saw_shape_reject = False
        for source in sources:
            if gained >= max_new_sources_per_row:
                break
            if source.id in have:
                continue
            headers = _source_headers(source)
            attempt = {"source_id": int(source.id), "keys_tried": [],
                       "candidates": [], "error": None}
            row_audit["attempts"].append(attempt)
            best = None  # (rank, total, cand, score, detail, key_kind)
            for key_text, key_kind in keys[:keys_per_source]:
                attempt["keys_tried"].append(
                    {"key": key_text, "kind": key_kind})
                stats["searches"] += 1
                try:
                    payload = acf.fetch_page(
                        source.base_url, ac="search", wd=key_text,
                        http_get=http_get, headers=headers)
                except acf.AppleCMSError as exc:
                    attempt["error"] = exc.kind
                    break
                # PA-079：本页 class 树算短剧类集合，供 list/detail
                # 两层门禁共用（同源同分类体系）；缺树为空集降级。
                short_ids = class_short_drama_ids(payload)
                scored = 0
                for entry in (payload.get("list") or []):
                    if scored >= candidates_per_source:
                        break
                    if not isinstance(entry, dict):
                        continue
                    gate = quality_gate_reject(
                        str(entry.get("type_name") or ""),
                        str(entry.get("vod_name") or ""),
                        str(entry.get("vod_remarks") or ""),
                        vod_class=str(entry.get("vod_class") or ""),
                        short_ids=short_ids,
                        type_id=entry.get("type_id"))
                    if gate:
                        stats["quality_skipped"] += 1
                        attempt["candidates"].append({
                            "vod_id": str(entry.get("vod_id") or ""),
                            "rejected": f"quality:{gate}"})
                        continue
                    vod_id = str(entry.get("vod_id") or "")
                    stats["details"] += 1
                    try:
                        detail = acf.fetch_detail(
                            source.base_url, vod_id, http_get=http_get,
                            headers=headers)
                    except acf.AppleCMSError:
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id, "rejected": "detail_error"})
                        continue
                    if detail is None:
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id,
                             "rejected": "detail_missing"})
                        continue
                    gate = quality_gate_reject(
                        str(detail.get("type_name") or ""),
                        str(detail.get("vod_name") or ""),
                        str(detail.get("vod_remarks") or ""),
                        vod_class=str(detail.get("vod_class") or ""),
                        short_ids=short_ids,
                        type_id=detail.get("type_id"))
                    if gate:
                        stats["quality_skipped"] += 1
                        attempt["candidates"].append(
                            {"vod_id": vod_id,
                             "rejected": f"quality:{gate}"})
                        continue
                    planned = acser.plan_episodes(detail)
                    if not planned:
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id,
                             "rejected": "no_direct_group"})
                        continue
                    if len(planned) == 1 and ep_count != 1:
                        saw_shape_reject = True
                        attempt["candidates"].append(
                            {"vod_id": vod_id,
                             "rejected": "single_episode_shape"})
                        continue
                    cand = candidate_from_detail(
                        detail, entry, episode_count=len(planned))
                    score = score_candidate(profile, cand)
                    scored += 1
                    attempt["candidates"].append({
                        "vod_id": vod_id, "name": cand.name,
                        "key_kind": key_kind, "total": score.total,
                        "parts": score.parts, "tier": score.tier,
                        "vetoes": score.vetoes,
                        "title_via": score.title_via})
                    rank = _TIER_RANK[score.tier]
                    if best is None or (rank, score.total) > (best[0], best[1]):
                        best = (rank, score.total, cand, score, detail,
                                key_kind)
                if best is not None and best[3].tier in ("high", "medium"):
                    break
            if best is None:
                reasons.append(
                    "source_error" if attempt["error"] else "no_candidate")
                continue
            _rank, _total, cand, score, detail, key_kind = best
            if score.tier not in ("high", "medium"):
                reasons.append(score.vetoes[0] if score.vetoes
                               else "low_confidence")
                continue
            if not probe_first_episode(detail, headers,
                                       http_probe=http_probe,
                                       http_get_text=http_get_text):
                stats["probe_failed"] += 1
                reasons.append("probe_failed")
                for cand_audit in attempt["candidates"]:
                    if cand_audit.get("vod_id") == cand.vod_id:
                        cand_audit["rejected"] = "probe_failed"
                continue
            if apply:
                try:
                    record = acs.cms_entry_to_record(
                        detail, source_id=str(source.id))
                    tree = acser.ensure_series_tree(
                        db, library, source, record, detail,
                        existing_series=row)
                    db.commit()
                except Exception:  # noqa: BLE001 - 单条失败不污染整库
                    db.rollback()
                    stats["errors"] += 1
                    attempt["error"] = "write_failed"
                    reasons.append("write_failed")
                    continue
                stats["mappings_added"] += int(tree.get("mapped", 0))
            row_audit["gained"].append({
                "source_id": int(source.id), "vod_id": cand.vod_id,
                "tier": score.tier, "total": score.total,
                "parts": score.parts, "key_kind": key_kind,
                "title_via": score.title_via, "applied": bool(apply)})
            gained += 1
            have.add(source.id)
            if delay_sec > 0:
                sleep(delay_sec)
        if gained:
            stats["filled"] += 1
        else:
            stats["unresolved"] += 1
            reason = _pick_unresolved_reason(reasons, saw_shape_reject)
            row_audit["unresolved_reason"] = reason
            stats["unresolved_reasons"][reason] = \
                stats["unresolved_reasons"].get(reason, 0) + 1
        audit.append(row_audit)
    return {"stats": stats, "audit": audit}


# ---------------------------------------------------------------------------
# 存量抽查：旧「仅标题命中」补映射用新打分器只读复核（只报不删）
# ---------------------------------------------------------------------------


def audit_existing_mappings(db, library_ids, source_ids=(7, 9), *,
                            expected_bucket_for: Optional[Callable] = None,
                            http_get=None, tmdb_details=None,
                            limit: Optional[int] = None,
                            delay_sec: float = 0.0, sleep_fn=None) -> dict:
    """对存量补映射逐（行×源）拉 detail 用新打分器复核。

    只读：不写任何映射、不删任何行。疑似错挂判据＝新打分器给
    reject 档、有硬否决、或总分低于中置信线。集数按 ``audit``
    口径（并集行不误报，见 score_candidate）。返回计数、原因分
    布与样例（样例上限 25 条）。
    """
    from backend.emby_server import applecms_series as acser
    from backend.emby_server import models as em
    from backend.emby_server import upstream_sources as us
    from backend.emby_server.applecms_library_sync import _source_headers
    sleep = sleep_fn if sleep_fn is not None else time.sleep
    result = {"checked_pairs": 0, "suspicious_pairs": 0,
              "detail_missing": 0, "errors": 0,
              "by_reason": {}, "examples": []}
    rows = (db.query(em.MediaItem).filter(
        em.MediaItem.library_id.in_(list(library_ids)),
        em.MediaItem.item_type == "series",
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None))
        .order_by(em.MediaItem.library_id, em.MediaItem.id).all())
    for row in rows:
        have = series_mapping_sources(db, row.id) & set(source_ids)
        if not have:
            continue
        ep_count = series_episode_count(db, row.id)
        details_data = None
        if tmdb_details is not None and getattr(row, "tmdb_id", None):
            try:
                details_data = tmdb_details(str(row.tmdb_id))
            except Exception:  # noqa: BLE001
                details_data = None
        bucket = expected_bucket_for(row.library_id) \
            if expected_bucket_for else None
        profile = build_row_profile(
            row, episode_count=ep_count, expected_area_bucket=bucket,
            tmdb_details=details_data)
        for source_id in sorted(have):
            if limit is not None and result["checked_pairs"] >= limit:
                return result
            source = us.get_source(db, source_id)
            vod_id = series_mapping_vod_id(db, row.id, source_id)
            if source is None or not vod_id:
                continue
            result["checked_pairs"] += 1
            try:
                detail = acf.fetch_detail(
                    source.base_url, vod_id, http_get=http_get,
                    headers=_source_headers(source))
            except acf.AppleCMSError:
                result["errors"] += 1
                continue
            if detail is None:
                result["detail_missing"] += 1
                continue
            planned = acser.plan_episodes(detail)
            cand = candidate_from_detail(
                detail, episode_count=len(planned))
            score = score_candidate(profile, cand, audit=True)
            reasons = list(score.vetoes)
            if score.tier == "reject" and not reasons:
                reasons = ["low_confidence"]
            if score.tier == "reject" or score.total < TIER_MEDIUM_MIN:
                result["suspicious_pairs"] += 1
                for reason in reasons:
                    result["by_reason"][reason] = \
                        result["by_reason"].get(reason, 0) + 1
                if len(result["examples"]) < 25:
                    result["examples"].append({
                        "item_id": int(row.id), "name": row.name,
                        "library_id": int(row.library_id),
                        "source_id": int(source_id), "vod_id": vod_id,
                        "cand_name": cand.name, "total": score.total,
                        "parts": score.parts, "reasons": reasons})
            if delay_sec > 0:
                sleep(delay_sec)
    return result

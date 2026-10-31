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
- 单集先按单集自身的 ``tmdb_id`` / ``imdb_id`` 精确配对；
  自身**无**身份的单集再走系列级配对（2026-10-31 优化
  O-08）：先定系列——同批系列已按上面三档口径配上本地系列
  的用那份结果，同批同名系列没配上的不许靠标题回落配进
  疑似同名异版，同批没有该系列时本地同名系列唯一才用——
  再按季集号精确对齐本地单集，证据 ``series+episode``、
  置信度默认 0.9（低于 imdb、高于 title+year，SystemConfig
  可调）。防错红线（配错集比配错电影严重）：季集号缺失或
  对不齐不配（仍记 ``episode_no_identity``）；系列多候选记
  ``episode_series_ambiguous``、同季同集多行记
  ``episode_multi_candidate``，一律不配。带自身身份的单集
  不走此路（没命中仍记 ``episode_no_identity``，口径不变）。
- 同一本地条目被多个上游条目配上时，每个上游条目各写一行
  映射（同一本地条目多线路是合法形态）；同一三元组重复写
  由 ``record_mapping`` 的幂等 upsert 兜底。
- 本地侧只认活行：``merged_into_id IS NULL``（``deleted_at``
  由 ORM 全局钩子自动拼）；同一身份多行时取 id 最小（确定性）。

备注-分类对位（2026-10-31 用户亲定，2026-11-01 口径修订）：
分类只用来收窄，不写就不收窄。源备注（``upstream_sources.note``）
按「备注-分类」约定写分类时（如「乐观-国产剧」），聚合先从
备注解析出分类，再把分类对位到一个本地真实库（分类→库名
可经 SystemConfig ``upstream_aggregate_category_library_map``
改配，没配到的分类默认同名对位；分类名单与库名一律来自
服主数据，代码不写死），本源全部配对只在该库内进行；写了
分类却对不上任何本地库时整源跳过：不解析凭据、不拉上游，
结果带 ``skipped`` 与 ``skip_reason``（短码 + 文案 + 分类），
日志同步记一条——写错分类名不许静默全量乱配。跳过不是
失败，任务照常以 succeeded 落终态、结果里看得到跳过原因。

全量模式（2026-11-01 简单方案，用户亲定，取代按分类拆源）：
备注没写分类（空/解析不出）默认就是全量：整源聚合、配对
不收窄，按条目自身所属逐库对位——分类本来就是机器按片子
对位，没必要让人先分。分类段恰写「全部」是同一模式的显式
写法，与没写分类等效。服主全程不用为聚合碰备注，除非他
想把某个源收窄到一个库。

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


def _system_config_text(db, key):
    """SystemConfig 文本热读：与 _system_config_int 同口径
    （无行/异常一律 None 回退，读失败不许把聚合打停）。"""
    if db is None:
        return None
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


#: 分类→本地库名改配 SystemConfig 键（备注-分类对位）：JSON 对象
#: {"分类名": "本地库名"}，只列要改配的分类；没列的分类默认同名
#: 对位。保存侧在 config_schema 严格校验（json_str_map），运行时
#: 读取宽容（见 category_library_map）。纯 SystemConfig 键，不进
#: env.example（#1527 先例）。
KEY_CATEGORY_LIBRARY_MAP = "upstream_aggregate_category_library_map"

#: 整源跳过短码：只剩「分类对不上任何本地库」一种——备注没写
#: 分类不再跳过（2026-11-01 口径修订：默认全量，见 CATEGORY_ALL
#: 与 resolve_target_library），旧短码 category_remark_missing
#: 已随口径取消，不要再产生。
SKIP_LIBRARY_UNMATCHED = "category_library_unmatched"

#: 全量模式保留分类（2026-11-01 简单方案，用户亲定）：备注分类
#: 段恰为此值时整源全量聚合、配对不收窄到单一库（按条目自身
#: 逐库对位）。它是模式开关、不是分类名也不是库名：对位解析
#: 先于改配表命中它就短路，改配表里给它配了值也不生效——映射
#: 表只管分类→改配库。同日口径修订后它只是全量的显式写法：
#: 备注没写分类默认就是全量（见 resolve_target_library），写
#: 不写「全部」等效；服主不必按分类拆源（拆源方案已被取代）。
CATEGORY_ALL = "全部"

#: 备注里「备注」与「分类」的分隔符：半角与全角横线都认。
_REMARK_SEPARATORS = ("-", "－")


def parse_remark_category(note) -> Optional[str]:
    """从源备注解析分类：「备注-分类」取最后一段为分类。

    - 备注为空/全空白 → None；
    - 含分隔符 → 分隔符后的最后一段（去空白，空段按无分类）；
    - 不含分隔符 → 整段备注即分类名（服主只写分类也成立）。
    分类名本身是服主数据，这里只做形态切分、不校验名单。
    """
    text = (note or "").strip()
    if not text:
        return None
    cut = -1
    for sep in _REMARK_SEPARATORS:
        idx = text.rfind(sep)
        if idx > cut:
            cut = idx
    if cut < 0:
        return text
    category = text[cut + 1:].strip()
    return category or None


def category_library_map(db) -> Dict[str, str]:
    """读分类→库名改配表（运行时宽容口径）。

    存量值解析不出来（人工直改库写坏等）一律当没配——回落同名
    对位，不许把一轮聚合打停；坏条目（非字符串键值、空键空值）
    逐条跳过。保存侧的严格校验在 config_schema，运行时不复检。
    """
    raw = _system_config_text(db, KEY_CATEGORY_LIBRARY_MAP)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("上游聚合分类改配表解析失败，按未配置处理")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("上游聚合分类改配表不是 JSON 对象，按未配置处理")
        return {}
    out: Dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str):
            key = key.strip()
            value = value.strip()
            if key and value:
                out[key] = value
    return out


def resolve_target_library(db: Session, source) -> dict:
    """把源备注的分类对位到本地真实库（备注-分类对位的唯一实现）。

    返回 {"library": Library|None, "category": str|None,
    "all_libraries": bool,
    "skip": None|{"code", "message", "category"}}：
    - 备注为空/解析不出分类 → 默认全量模式（2026-11-01 口径
      修订）：library=None、category=None、all_libraries=True、
      不跳过——分类只用来收窄，没写就不收窄；
    - 分类恰为「全部」（CATEGORY_ALL）→ 同样是全量模式（显式
      写法，与没写分类等效）：先于改配表短路，映射表只管
      分类→改配库，劫持不了模式开关；
    - 其余分类：目标库名 = 改配表命中值，未命中默认分类同名；
      按名找非虚拟库（虚拟库是平台生成的展示库、没有自己的
      条目，不是对位目标），同名多行取 id 最小（确定性）；
      找不到 → skip=category_library_unmatched（写了分类就要
      对得上，对不上响亮跳过并报明，不许静默全量）。
    """
    category = parse_remark_category(getattr(source, "note", None))
    if category is None:
        return {"library": None, "category": None, "skip": None,
                "all_libraries": True}
    if category == CATEGORY_ALL:
        return {"library": None, "category": category, "skip": None,
                "all_libraries": True}
    target_name = category_library_map(db).get(category, category)
    # 同名多行取 id 最小（确定性）；虚拟判定走全仓同款 Python 侧
    # bool() 口径（is_virtual 为 NULL 的老行算非虚拟，SQL == False
    # 会把它们漏掉）。库是「台」数量级，全取回内存筛不放大查询。
    library = None
    for row in (db.query(em.Library)
                .filter(em.Library.name == target_name)
                .order_by(em.Library.id).all()):
        if not bool(getattr(row, "is_virtual", False)):
            library = row
            break
    if library is None:
        return {
            "library": None, "category": category, "all_libraries": False,
            "skip": {
                "code": SKIP_LIBRARY_UNMATCHED,
                "message": f"源备注分类「{category}」对位本地库"
                           f"「{target_name}」不存在，已跳过该源聚合",
                "category": category,
            },
        }
    return {"library": library, "category": category, "skip": None,
            "all_libraries": False}


def _system_config_float(db, key):
    """SystemConfig 浮点热读：与 _system_config_int 同口径（无行/
    非法/异常一律 None 回退）；非正按未设置处理（置信度域 (0, 1]）。"""
    if db is None:
        return None
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _series_episode_confidence(db) -> float:
    """O-08 系列级配对置信度：SystemConfig > 默认 0.9，钳 [0.01, 1]。"""
    configured = _system_config_float(db, KEY_SERIES_EPISODE_CONFIDENCE)
    if configured is not None:
        return max(0.01, min(1.0, configured))
    return CONFIDENCE_SERIES_EPISODE

#: 单次请求超时（秒）：目录页带数据，比探活的 10 秒放宽。
HTTP_TIMEOUT_SEC = 30.0

#: 配对置信度：外部身份精确命中高于标题 + 年份封顶值 0.7。
CONFIDENCE_TMDB = 1.0
CONFIDENCE_IMDB = 0.95
CONFIDENCE_TITLE_YEAR = 0.7

#: O-08 系列级配对（证据 ``series+episode``）默认置信度：低于
#: imdb 的 0.95、高于 title+year 的 0.7——系列身份本身可靠，
#: 不确定性只在季集对齐这一跳；可经 SystemConfig 调（见
#: KEY_SERIES_EPISODE_CONFIDENCE），读取钳 [0.01, 1] 与注册表
#: 同口径。纯 SystemConfig 键，不进 env.example（#1527 先例）。
CONFIDENCE_SERIES_EPISODE = 0.9
KEY_SERIES_EPISODE_CONFIDENCE = \
    "upstream_aggregate_series_episode_confidence"

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


def _alive_local_query(db: Session, library_id=None):
    """本地活行查询基底：merged_into_id 与 is_hidden 显式过滤
    （deleted_at 由 ORM 全局钩子自动拼，见软删除纪律；与本站
    浏览口径对齐）。``library_id`` 给了就再按库收窄——备注-分类
    对位后配对只认对位库内的行，聚合的全部本地查询都从这里出，
    一处收窄即全链路不跨库。"""
    query = (db.query(em.MediaItem)
             .filter(em.MediaItem.merged_into_id.is_(None))
             .filter(em.MediaItem.is_hidden == False))  # noqa: E712
    if library_id is not None:
        query = query.filter(em.MediaItem.library_id == library_id)
    return query


def _type_matches(local, aggregate_item_type: str) -> bool:
    """本地行类型与上游聚合类型是否同类（movie↔Movie 等）。

    tmdb/imdb 精确命中也须同类：电影身份绝不配到单集行上。
    """
    local_type = (getattr(local, "item_type", "") or "").lower()
    return local_type == (aggregate_item_type or "").lower()


def pair_aggregates(db: Session, items: List[agg.UpstreamAggregateItem],
                    *, library_id=None):
    """给聚合作品配本地条目（纯配对，不写库）。

    返回 (pairs, unpaired_reasons)：pairs 每项含本地条目 id、
    上游条目 id（逐线路）、置信度、证据、集序号；unpaired_reasons
    按原因码计数（不逐条列未配对明细，响应体保持小）。
    ``library_id`` 给了时只在该本地库内配对（备注-分类对位，
    见 resolve_target_library）；不给 = 全库配对——run 的全量
    模式（默认/分类「全部」）经对位解析传 None 走这条，另有
    存量直接调用方与旧测试。

    查询策略（批量，不逐聚合查库）：
    - tmdb / imdb 各一次 IN 查询建映射；
    - 标题 + 年份按年份分组查候选，Python 里按聚合键比对；
    - 自身无身份的单集延后到第二遍走 O-08 系列级配对
      （见 _pair_deferred_episodes），其余口径一字不变。
    """
    pairs: List[dict] = []
    unpaired: Dict[str, int] = {}
    # O-08 第二遍的入料：自身无身份、第一遍没配上的单集先攒着，
    # 等同批系列的配对结果齐了再定系列（系列可能排在单集之后）。
    deferred_episodes: List[agg.UpstreamAggregateItem] = []
    resolved_series_by_title: Dict[str, set] = {}
    unresolved_series_titles: set = set()

    def bump(reason: str) -> None:
        unpaired[reason] = unpaired.get(reason, 0) + 1

    tmdb_ids = {item.tmdb_id for item in items if item.tmdb_id}
    imdb_ids = {item.imdb_id for item in items if item.imdb_id}
    by_tmdb: Dict[str, object] = {}
    by_imdb: Dict[str, object] = {}
    if tmdb_ids:
        for row in (_alive_local_query(db, library_id)
                    .filter(em.MediaItem.tmdb_id.in_(sorted(tmdb_ids)))
                    .order_by(em.MediaItem.id).all()):
            by_tmdb.setdefault(str(row.tmdb_id), row)
    if imdb_ids:
        for row in (_alive_local_query(db, library_id)
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
            candidates = (_alive_local_query(db, library_id)
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
                if item.tmdb_id or item.imdb_id:
                    # 带自身身份仍没命中：旧口径不变，且不走系列级
                    # 路径（O-08 范围只限自身无身份的单集）。
                    bump("episode_no_identity")
                else:
                    deferred_episodes.append(item)
            elif item.tmdb_id or item.imdb_id:
                bump("identity_no_local_match")
            else:
                bump("title_year_no_local_match")
            if item.item_type == "Series":
                title_key = agg._normalize_title(item.name or "")
                if title_key:
                    unresolved_series_titles.add(title_key)
            continue
        if item.item_type == "Series":
            title_key = agg._normalize_title(item.name or "")
            if title_key:
                resolved_series_by_title.setdefault(
                    title_key, set()).add(local.id)
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
    if deferred_episodes:
        _pair_deferred_episodes(
            db, deferred_episodes, pairs, bump,
            resolved_series_by_title, unresolved_series_titles,
            library_id=library_id)
    return pairs, unpaired


def _local_series_title_index(db: Session, library_id=None):
    """本地活系列按归一标题索引：{标题键: {id: 行}}（O-08 回落用）。

    标题取 _local_title_year 口径（scan_title 回落 name），归一用
    聚合模块同款 _normalize_title；同名多行全保留——多候选由
    调用方按防错红线拒配，这里不替它做主。``library_id`` 给了
    时索引只含对位库内的系列（备注-分类对位，不跨库）。
    """
    index: Dict[str, Dict[int, object]] = {}
    for row in (_alive_local_query(db, library_id)
                .filter(em.MediaItem.item_type == "series").all()):
        title, _year = _local_title_year(row)
        if not title:
            continue
        key = agg._normalize_title(title)
        if key:
            index.setdefault(key, {})[row.id] = row
    return index


def _pair_deferred_episodes(db: Session, episodes, pairs, bump,
                            resolved_series_by_title,
                            unresolved_series_titles,
                            *, library_id=None) -> None:
    """O-08 系列级配对（第二遍）：自身无身份的单集先定系列再对齐。

    定系列（按可靠度排序）：
    1. 同批系列已按现行口径（tmdb / imdb / title+year）配上本地
       系列时，直接用那份结果——系列身份走的是原配对链；
    2. 同批有同名系列但它没配上（身份对不上本地）时，不许靠
       标题回落把单集配进疑似同名异版（翻拍）系列——拒配；
    3. 同批没有该系列时，本地同名系列唯一才用它；同名多部
       （翻拍等）拒配，记 episode_series_ambiguous。

    对齐：本地系列下按 (season_number, episode_number) 精确找
    单集——缺号/对不齐拒配（仍记 episode_no_identity）；同季
    同集多行拒配，记 episode_multi_candidate（配错集比不配
    更糟）。配上的一律证据 series+episode、置信度走
    _series_episode_confidence（默认 0.9）。
    """
    confidence = _series_episode_confidence(db)
    local_series_by_title = None
    episodes_by_series: Dict[int, list] = {}

    for item in episodes:
        title_key = agg._normalize_title(item.series_name or "")
        episode_numbers = {line.episode_index for line in item.lines}
        if (not title_key or item.season_number is None
                or len(episode_numbers) != 1
                or None in episode_numbers):
            bump("episode_no_identity")
            continue
        episode_number = next(iter(episode_numbers))
        if title_key in resolved_series_by_title:
            series_ids = resolved_series_by_title[title_key]
            if len(series_ids) > 1:
                bump("episode_series_ambiguous")
                continue
            local_series_id = next(iter(series_ids))
        elif title_key in unresolved_series_titles:
            bump("episode_no_identity")
            continue
        else:
            if local_series_by_title is None:
                local_series_by_title = _local_series_title_index(
                    db, library_id)
            rows = local_series_by_title.get(title_key) or {}
            if not rows:
                bump("episode_no_identity")
                continue
            if len(rows) > 1:
                bump("episode_series_ambiguous")
                continue
            local_series_id = next(iter(rows))
        if local_series_id not in episodes_by_series:
            episodes_by_series[local_series_id] = (
                _alive_local_query(db, library_id)
                .filter(em.MediaItem.item_type == "episode")
                .filter(em.MediaItem.series_id == local_series_id)
                .all())
        candidates = [
            row for row in episodes_by_series[local_series_id]
            if row.season_number == item.season_number
            and row.episode_number == episode_number]
        if not candidates:
            bump("episode_no_identity")
            continue
        if len(candidates) > 1:
            bump("episode_multi_candidate")
            continue
        local = candidates[0]
        for line in item.lines:
            pairs.append({
                "aggregate_key": item.aggregate_key,
                "name": item.name,
                "item_type": item.item_type,
                "local_item_id": local.id,
                "upstream_item_id": line.upstream_item_id,
                "episode_index": line.episode_index,
                "confidence": confidence,
                "evidence": "series+episode",
            })


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
                           on_heartbeat: Optional[Callable] = None,
                           commit_each_library: bool = False,
                           skip_library_ids=None,
                           prior_summaries: Optional[Dict[str, dict]] = None
                           ) -> dict:
    """手动聚合一轮：按库分批拉目录 → 逐库聚合 → 逐库配对 → 开关开时写映射。

    前置校验（响亮失败，不静默跳过，发生在任何写库之前）：
    - 源不存在由调用方先 404（本函数不查源行）；
    - 源未启用 → 400（停用即断聚合）；
    - applecms 型 → 400（走它自己的同步，不按 Emby 协议撞接口）。

    备注-分类对位（2026-10-31，2026-11-01 口径修订）：前置
    校验后先按源备注解析对位（见 resolve_target_library）。
    写了分类却对不上本地库时整源跳过——不算失败：不解析
    凭据、不拉上游，直接返回带 ``skipped=True`` 与
    ``skip_reason`` 的零计数结果，并记一条 warning 日志；
    对位命中后全部配对只在对位库内进行，结果带 ``category`` /
    ``target_library_id`` / ``target_library_name`` 供追溯。
    没写分类（默认）或分类恰为「全部」时走全量模式：不收窄、
    按条目自身逐库对位，结果带显式模式标记
    ``all_libraries=True``（单库对位与跳过时为 False）、
    ``target_library_id`` / ``target_library_name`` 为 None、
    ``category`` 为解析出的分类（「全部」或 None），另记一行
    全量模式日志；逐库推进/逐库提交的既有机制不因此改变。
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

    窗级心跳（``on_heartbeat``，A-118）：与进度回调分离的轻量
    回调，每窗（``WINDOW_RECORDS`` 条）触发一次，只刷新任务行
    heartbeat、不碰执行事务。进度事件只在库边界，单库 142533
    条≈286 页、均页 0.42 秒即超默认 120 秒心跳阈值——没有窗级
    心跳，长库进行中再点聚合/续跑会被误判死亡、先标 interrupted
    再放行第二轮。心跳回调异常同样被 _fire_event 吞并记日志。

    断点续跑（O-01）：``skip_library_ids`` 里的库不再拉取，其
    ``prior_summaries``（上一轮该库的 summary）原样并入本轮
    libraries 列表与总量，summary 副本带 ``skipped_resume=True``
    标记；不给 prior 的跳过库按零计数空摘要并入。靠的是任务表
    （upstream_aggregate_tasks），本函数仍不自建检查点表。
    """
    ensure_aggregatable(source)
    routing = resolve_target_library(db, source)
    if routing["skip"] is not None:
        skip = routing["skip"]
        logger.warning("上游聚合按备注分类跳过整源 source_id=%s "
                       "code=%s category=%s", source.id, skip["code"],
                       skip["category"])
        return {
            "source_id": source.id,
            "fetched": 0,
            "aggregates": 0,
            "pairs": [],
            "paired": 0,
            "written": 0,
            "mapping_enabled": uim.mapping_enabled(),
            "unpaired_reasons": {},
            "libraries": [],
            "libraries_discovered": 0,
            "failed_libraries": [],
            "skipped": True,
            "skip_reason": skip,
            "category": routing["category"],
            "all_libraries": bool(routing.get("all_libraries")),
            "target_library_id": None,
            "target_library_name": None,
        }
    target_library = routing["library"]
    # 全量模式（没写分类或分类=「全部」）没有单一对位库：target
    # 置 None，pair_aggregates 收到 library_id=None 即走不收窄
    # 的逐库对位。
    target_library_id = (target_library.id
                         if target_library is not None else None)
    if routing.get("all_libraries"):
        if routing["category"]:
            logger.info("上游聚合全量模式 source_id=%s category=%s"
                        "（备注分类「全部」：配对不收窄，按条目自身"
                        "逐库对位）", source.id, routing["category"])
        else:
            logger.info("上游聚合全量模式 source_id=%s category=None"
                        "（备注未写分类，默认全量：配对不收窄，"
                        "按条目自身逐库对位）", source.id)
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
                pairs, unpaired = pair_aggregates(
                    db, items, library_id=target_library_id)
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
                # 窗级心跳（A-118）：长库内每窗刷一次存活信号，不
                # 等库边界的进度事件；只刷 heartbeat、不碰执行事务。
                _fire_event(on_heartbeat, {
                    "type": "heartbeat", "library_id": lib_id,
                    "library_name": lib_name,
                    "window_records": len(window)})
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
                "mapping_enabled=%s category=%s target_library_id=%s",
                source.id, len(libraries), total_fetched, total_aggregates,
                total_paired, total_written, len(failed_libraries),
                mapping_on, routing["category"], target_library_id)
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
        "skipped": False,
        "skip_reason": None,
        "category": routing["category"],
        "all_libraries": bool(routing.get("all_libraries")),
        "target_library_id": target_library_id,
        "target_library_name": (target_library.name
                                if target_library is not None else None),
    }

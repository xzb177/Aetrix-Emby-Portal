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
  原因；开时调用方可经 ``parse_resolver`` 参数注入逐站解析函数，
  未注入时仍记 parse_rule_missing，不内置任何站规则、不猜解析）。
  同步开关 ``UPSTREAM_SYNC_ENABLED``
  默认关且本批无工人消费；映射写开关
  ``UPSTREAM_ITEM_MAPPING_ENABLED`` 只管映射写路径，不 gate 播放读
  路径（它默认关，若 gate 播放则播放恒关，是错的）。
- 取数：``upstream_item_id`` 即 vod_id（映射行记的上游条目 id
  对苹果 CMS 型就是 vod_id）；``episode_index`` 从 1 起，调用方
  未传时默认 1（播第 1 集）。detail 解析结果走 TTL 缓存（见下
  条），播放地址绝不落库。
- detail 解析缓存（播放优化 A2）：``fetch_detail`` 的网络请求
  结果按（源, vod）走既有 CacheManager（与画质缓存同通道、
  不落库），TTL 按源分层（有采样证据的源长、无证据的短），
  解析出的播放主机属地域 403 族时封顶短 TTL；命中缓存后探活
  403/404 或解析失败，先淘汰该条再回源重解析一次——重解析走
  现取的全部既有语义（含探活 403 软放行），仍失败按原错误
  报错。缓存总开关 ``APPLECMS_DETAIL_CACHE_ENABLED`` 默认开，
  关时不读不写、行为与无缓存时逐字一致。
- 探活与错误码全复用既有：CMS 接口非 1 → 502
  upstream_unavailable；detail 空列表（vod 消失）→ 404
  upstream_item_not_found；429 → 429；401 → 409
  upstream_credential_invalid；CMS 接口本身的 403（取 detail 时）
  → 403 upstream_forbidden。直链探活走 ``_classify_probe_status``，
  唯一例外是探活 403 软放行（2026-11-02 定案，只限本分支）：
  播放 CDN 对数据中心 IP 的 403 不判死，记 cms_probe_soft_pass
  日志后照常下发 302——detail 现取成功已证明源与条目可用，
  403 只证明服务器 IP 被播放 CDN 拒，用户家里未必被拒；
  404/401/429/5xx 仍按统一分类判死。unplayable 原因映射：
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.emby_server import upstream_sources as us
from backend.emby_server import upstream_health as uh
from backend.emby_server import upstream_http
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server.env_util import env_float, env_int

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

#: 苹果 CMS 坏源自动熔断阈值 env 键：真实点播路径上某 CMS 源连续
#: 失败达到该值时，进入整源停用判定（只对 applecms 型源生效）。
#: 默认值与健康状态机翻 Unavailable 的阈值
#: （UPSTREAM_HEALTH_FAILURE_THRESHOLD，默认 3）对齐——同一份
#: 连续失败计数（SourceHealthRecord）喂两处消费：先翻 Unavailable
#: 沉底排序，再达熔断阈值进入停用判定；两个阈值默认同值、各自
#: 可调。注意：达阈值只是停用判定的门票，真正停用还须过 PA-078
#: 双重保险（时间跨度门 + 停用前复核门，见
#: APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_SEC 与 _maybe_disable_cms_source）。
APPLECMS_CIRCUIT_BREAKER_THRESHOLD_ENV = "APPLECMS_CIRCUIT_BREAKER_THRESHOLD"


def cms_circuit_breaker_threshold() -> int:
    """CMS 坏源自动熔断的连续失败阈值（默认与健康状态机阈值同值）。"""
    return env_int(APPLECMS_CIRCUIT_BREAKER_THRESHOLD_ENV,
                   uh.failure_threshold(), 1, 100)

#: 苹果 CMS 熔断保险一（PA-078）：连续失败串的最小时间跨度 env 键。
#: 计数达阈值后，失败串从第一笔（SourceHealthRecord.first_failure_at）
#: 到当前不足该秒数，一律不许整源停用。依据：四度误伤的共同形态是
#: 源站出现「分钟级坏窗口」，坏窗口内点播密集、 counted 失败几分钟
#: 就攒满阈值（源 11 实证：计数被复位后约 5.5 分钟再攒满 3 次即被
#: 停，连带 5,174 部已验活单源片断播）。默认 600 秒要求失败横跨
#: 至少 10 分钟的真实点播：单个分钟级坏窗口熬不满这条线，真持续
#: 死源的失败则会自然横跨，至多延迟 10 分钟停用——延迟期间源已被
#: 健康分翻 Unavailable 沉底，择优不耽误。配 0 即退回计数单门
#: （运维显式选择，非默认）。
APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_ENV = "APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_SEC"


def cms_circuit_breaker_min_span_sec() -> float:
    """整源停用要求的连续失败最小时间跨度（秒，默认 600）。"""
    return env_float(APPLECMS_CIRCUIT_BREAKER_MIN_SPAN_ENV,
                     600.0, 0.0, 86400.0)

#: 上游探活/认证单次超时（秒，运维常量，见模块 docstring）。
UPSTREAM_PROBE_TIMEOUT_SEC = 10.0

#: CMS 播放 302 关键路径探活单跳超时（秒，运维常量）：302 下发前
#: 的探活必须快，单跳 2.5 秒（最多 4 跳跟随，最坏 10 秒，原 40 秒）；
#: 其余调用点（Emby 认证、Emby/Jellyfin 分支探活、媒体参数拉取）
#: 仍用 UPSTREAM_PROBE_TIMEOUT_SEC，一字不动。
CMS_PLAY_PROBE_TIMEOUT_SEC = 2.5

#: CMS 同 vod 多候选逐个探活的上限（运维常量）：候选是同一条映射
#: 同一个 vod 的平行播放组（同一内容的等价线路）。2026-11-03 生产
#: 实证：某 vod 9 个全直链组里前 6 组探活 404、第 7 组才 200，旧
#: 实现只探首组就把整条线路判死（用户端 404 无下文）。逐候选试到
#: 首个可播即下发；上限按生产实见组数（9 组）留余量取 10——判死
#: 地址通常亚秒级返回，真正吃时间的只有挂死主机（单候选探活最坏
#: 4 跳 × 2.5 秒）；全灭时按首候选的错误口径收口。
CMS_CANDIDATE_PROBE_LIMIT = 10

#: 播放探活重定向跟随上限（跳，运维常量）：上游以 302 发流是常态
#: （Emby stream → 网盘直链 → 对象存储签名链，生产实测两跳），
#: 但跟随必须有界且逐跳复验，见 ``_default_http_range_probe``。
UPSTREAM_PROBE_MAX_REDIRECTS = 3

#: 探活层视为重定向、需要跟随的状态码集合。
_PROBE_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))

#: CMS 线路画质缓存 TTL（秒，运维常量）：画质是 vod 的固有属性、
#: 极少变化，7 天足够覆盖两次点播之间的展示需求；过期后下一次
#: 点播取流时自然回填，不设主动刷新。
CMS_QUALITY_CACHE_TTL_SEC = 7 * 24 * 3600

#: m3u8 主清单抓取超时（秒，运维常量）：只为画质缓存服务，必须
#: 短——超时/失败直接放弃缓存，绝不拖住 302 下发。
M3U8_FETCH_TIMEOUT_SEC = 3.0

#: m3u8 主清单读取上限（字节）：主清单是小文本，256KB 绰绰有余，
#: 防异常大响应吃内存。
_M3U8_MAX_BYTES = 256 * 1024


def cms_quality_cache_key(mapping_id) -> str:
    """CMS 线路画质缓存键：按映射 id 一条线路一份。"""
    return f"cmsq:{int(mapping_id)}"


def read_cms_quality(mapping_id) -> Optional[dict]:
    """读 CMS 线路画质缓存（取流路径回填，展示路径消费）。

    内容形态：``{"width": int, "height": int, "bandwidth": int,
    "video_range": str|None, "label": str|None,
    "duration_sec": float}``（字段可缺；duration_sec 是 media
    playlist 分段时长求和，只有分段清单的源才有）。
    无缓存/缓存坏了返回 None——展示侧回落默认形态，不报错。
    """
    if mapping_id is None:
        return None
    try:
        from backend.database import CacheManager
        raw = CacheManager.get(cms_quality_cache_key(mapping_id))
    except Exception:  # noqa: BLE001 — 缓存只是补强，故障不外溢
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def store_cms_quality(mapping_id, quality: dict) -> None:
    """写 CMS 线路画质缓存（与已有内容合并，新非空值优先）。

    全程 fail-safe：缓存写失败不许影响取流主路径（调用方在 302
    下发前调用，异常一律吞掉）。
    """
    if mapping_id is None or not quality:
        return
    try:
        from backend.database import CacheManager
        merged = dict(read_cms_quality(mapping_id) or {})
        for key, value in quality.items():
            if value not in (None, "", 0):
                merged[key] = value
        if not merged:
            return
        CacheManager.set(cms_quality_cache_key(mapping_id),
                         json.dumps(merged, ensure_ascii=False),
                         ttl=CMS_QUALITY_CACHE_TTL_SEC)
    except Exception:  # noqa: BLE001 — 同 read_cms_quality 口径
        pass


#: 苹果 CMS detail 解析缓存开关 env 键（默认开）：关时不读不写
#: 缓存，取流行为与无缓存时逐字一致，是本节的回滚口。
APPLECMS_DETAIL_CACHE_ENV = "APPLECMS_DETAIL_CACHE_ENABLED"

#: detail 缓存默认 TTL env 键（秒）：没有分层证据的源只给短 TTL。
APPLECMS_DETAIL_CACHE_TTL_DEFAULT_ENV = "APPLECMS_DETAIL_CACHE_TTL_DEFAULT_SEC"

#: detail 缓存分源 TTL env 键：``源id:秒,源id:秒`` 形态，覆盖默认层。
APPLECMS_DETAIL_CACHE_TTL_BY_SOURCE_ENV = "APPLECMS_DETAIL_CACHE_TTL_BY_SOURCE_SEC"

#: 地域 403 族播放主机 TTL 封顶 env 键（秒）。
APPLECMS_DETAIL_CACHE_TTL_BLOCKED_CAP_ENV = "APPLECMS_DETAIL_CACHE_TTL_BLOCKED_CAP_SEC"

#: detail 缓存默认 TTL（秒）：采样证据只覆盖少数源，其余源的
#: detail 内容无时效证据，只给 1 小时短 TTL。
CMS_DETAIL_CACHE_TTL_DEFAULT_SEC = 3600

#: detail 缓存分源 TTL 默认表（秒，2026-11-02 播放地址采样定案）：
#: 非凡（7）播放地址无签名、其 CDN 自报 30 天缓存，给 24 小时
#: （只有其自报时效的 1/30，留足余量）；暴风（9）/ 如意（13）
#: 无签名、三轮重抓逐字节不变但零缓存头，保守 12 小时。
CMS_DETAIL_CACHE_TTL_BY_SOURCE_DEFAULT = "7:86400,9:43200,13:43200"

#: 地域 403 族 TTL 封顶（秒）：见 _CMS_REGION_BLOCKED_HOST_PREFIXES。
CMS_DETAIL_CACHE_TTL_BLOCKED_CAP_SEC = 3600

#: 地域 403 族播放主机前缀（2026-11-02 采样定案）：非凡
#: ``vip.ffzy-play*`` 与如意 ``svip.ryplay*`` 两族对数据中心 IP
#: 一律 403（The region has been denied），其地址时效无正面
#: 证据，解析出这类主机时该条 TTL 一律封顶到短值。
_CMS_REGION_BLOCKED_HOST_PREFIXES = ("vip.ffzy-play", "svip.ryplay")


def cms_detail_cache_enabled() -> bool:
    """detail 解析缓存总开关（env，默认开）。每次调用现读。"""
    return (str(os.getenv(APPLECMS_DETAIL_CACHE_ENV, "1") or "1")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def cms_detail_cache_key(source_id, vod_id) -> str:
    """CMS detail 解析缓存键：按（源, vod）一条一份。"""
    return f"cmsd:{int(source_id)}:{str(vod_id).strip()}"


def _cms_detail_ttl_env_int(env_key: str, fallback: int) -> int:
    """TTL 类 env 读法：正整数才认，脏值/非正数回落默认，不炸路径。"""
    try:
        value = int(str(os.getenv(env_key, "") or "").strip())
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _parse_cms_detail_ttl_table(raw) -> dict:
    """解析 ``源id:秒,源id:秒`` 分源 TTL 表；脏条目逐条跳过。"""
    table: dict = {}
    for part in str(raw or "").split(","):
        sid, sep, ttl = part.partition(":")
        if not sep:
            continue
        try:
            sid_int, ttl_int = int(sid.strip()), int(ttl.strip())
        except (TypeError, ValueError):
            continue
        if ttl_int > 0:
            table[sid_int] = ttl_int
    return table


def cms_detail_cache_ttl(source_id, address: Optional[str] = None) -> int:
    """detail 缓存 TTL 分层（秒）：分源层 → 默认层，403 族封顶。

    分层依据见 ``CMS_DETAIL_CACHE_TTL_BY_SOURCE_DEFAULT`` 注释
    （env 分源表只覆盖它显式列出的源，未列出的仍走内置默认表）；
    解析出的播放地址主机属地域 403 族时，无论哪一层都封顶到
    ``CMS_DETAIL_CACHE_TTL_BLOCKED_CAP_SEC``（env 可覆盖）。
    """
    ttl = _parse_cms_detail_ttl_table(
        os.getenv(APPLECMS_DETAIL_CACHE_TTL_BY_SOURCE_ENV, "")).get(
            int(source_id))
    if ttl is None:
        ttl = _parse_cms_detail_ttl_table(
            CMS_DETAIL_CACHE_TTL_BY_SOURCE_DEFAULT).get(int(source_id))
    if ttl is None:
        ttl = _cms_detail_ttl_env_int(
            APPLECMS_DETAIL_CACHE_TTL_DEFAULT_ENV,
            CMS_DETAIL_CACHE_TTL_DEFAULT_SEC)
    host = urllib.parse.urlparse(address or "").hostname or ""
    if host.startswith(_CMS_REGION_BLOCKED_HOST_PREFIXES):
        cap = _cms_detail_ttl_env_int(
            APPLECMS_DETAIL_CACHE_TTL_BLOCKED_CAP_ENV,
            CMS_DETAIL_CACHE_TTL_BLOCKED_CAP_SEC)
        ttl = min(ttl, cap)
    return ttl


def read_cms_detail(source_id, vod_id) -> Optional[dict]:
    """读 CMS detail 解析缓存（取流路径写、取流路径消费）。

    内容形态就是 ``applecms_fetcher.fetch_detail`` 返回的 vod
    dict。无缓存/缓存坏了返回 None——调用方回落现取，不报错。
    """
    try:
        from backend.database import CacheManager
        raw = CacheManager.get(cms_detail_cache_key(source_id, vod_id))
    except Exception:  # noqa: BLE001 — 缓存只是补强，故障不外溢
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def store_cms_detail(source_id, vod_id, detail: dict, ttl: int) -> None:
    """写 CMS detail 解析缓存。全程 fail-safe，同 store_cms_quality。"""
    if not isinstance(detail, dict) or not detail:
        return
    try:
        from backend.database import CacheManager
        CacheManager.set(cms_detail_cache_key(source_id, vod_id),
                         json.dumps(detail, ensure_ascii=False),
                         ttl=ttl)
    except Exception:  # noqa: BLE001 — 同 read_cms_detail 口径
        pass


def evict_cms_detail(source_id, vod_id) -> None:
    """淘汰一条 CMS detail 解析缓存。全程 fail-safe。"""
    try:
        from backend.database import CacheManager
        CacheManager.delete(cms_detail_cache_key(source_id, vod_id))
    except Exception:  # noqa: BLE001 — 淘汰失败不外溢，最坏下次再淘
        pass


# ---------------------------------------------------------------- 已验活
# 地址快路（起播解析慢治理，2026-11-05 生产实证）：成功起播
# resolve_ms p50 2864ms / p95 10657ms，大头不是解析、是每播必做
# 的同步探活——探活单跳直达 CDN，但 CDN 对服务器机房位置的首字
# 节耗时实测 1.9~9.7 秒；detail 缓存只省取 detail，省不掉探活。
# 而 46% 的播放是同一条目 30 秒内的复播，照样全额再探一遍。
# 快路口径：同一条映射线路刚下发过的地址，在短窗口内复播、且该
# 地址仍在本份 detail 的候选内时，免探活直接下发同一个地址——
# 读的正是探活刚写下的现成结论，不关探活、不关择优/熔断（线路
# 排序与健康记录照常喂数）；窗口外、地址已不在候选、无映射 id
# （显式上游口）一律逐字回现行逐候选探活。窗口只需覆盖复播
# 间隔：地址本身的时效另有 detail TTL 与探活淘汰守着，窗口内
# 地址恰好死亡的最坏后果与今日「探活刚过地址就死」同形态（播
# 放器报错、用户换线），窗口一过自动恢复逐发探活。
#: 已验活地址快路窗口 env 键（秒）：0 = 关闭快路（回今日行为）。
APPLECMS_VERIFIED_PLAY_TTL_ENV = "APPLECMS_VERIFIED_PLAY_TTL_SEC"

#: 已验活地址快路默认窗口（秒）。
CMS_VERIFIED_PLAY_TTL_DEFAULT_SEC = 300


def cms_verified_play_ttl() -> int:
    """已验活地址快路窗口（秒）：0/负数 = 关闭；脏值回默认。"""
    raw = str(os.getenv(APPLECMS_VERIFIED_PLAY_TTL_ENV, "") or "").strip()
    if not raw:
        return CMS_VERIFIED_PLAY_TTL_DEFAULT_SEC
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return CMS_VERIFIED_PLAY_TTL_DEFAULT_SEC
    return max(0, value)


def cms_verified_play_key(mapping_id) -> str:
    """已验活地址缓存键：按映射 id 一条一份（映射 = 一条下发线路）。"""
    return f"cmsvp:{int(mapping_id)}"


def read_verified_play_address(mapping_id) -> Optional[str]:
    """读已验活地址：无映射/快路关/无记录/记录坏了都返回 None。

    调用方拿到 None 即回现行探活路径，不报错（与 detail 缓存
    同口径：缓存只是补强，故障不外溢）。
    """
    if mapping_id is None or cms_verified_play_ttl() <= 0:
        return None
    try:
        from backend.database import CacheManager
        raw = CacheManager.get(cms_verified_play_key(mapping_id))
    except Exception:  # noqa: BLE001 — 同 read_cms_detail 口径
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        address = data.get("address")
        if isinstance(address, str) and address:
            return address
    return None


def store_verified_play_address(mapping_id, address) -> None:
    """记一条已验活地址（下发成功时写）。全程 fail-safe。"""
    ttl = cms_verified_play_ttl()
    if mapping_id is None or not address or ttl <= 0:
        return
    try:
        from backend.database import CacheManager
        CacheManager.set(
            cms_verified_play_key(mapping_id),
            json.dumps({"address": address}, ensure_ascii=False), ttl=ttl)
    except Exception:  # noqa: BLE001 — 同 store_cms_detail 口径
        pass


# ---------------------------------------------------------------- 探活分级
# （件一）：同一主机连续 403 满 3 次即跳过探活——恒 403 主机（如区
# 域拒绝型 CDN）每次点播省掉 1.5–3.8 秒探活等待；跳过时返回的
# (403, None) 与实测 403 走同一条 resolve 软放行分支，播放决策
# 不变，只省等待。计数键走 CacheManager、TTL 6 小时，到期后下
# 一次恢复实测自愈；非 403 的确定性状态清键，超时／断连（None）
# 不改计数。计数按探活入口 URL 的主机记（跟随链终跳主机不同时
# 仍以入口主机为准——403 本就是软放行 verdict，跳过不改播放决
# 策，只省等待）。
_CMS_PROBE_403_KEY_PREFIX = "cmsprobe403:"
_CMS_PROBE_403_THRESHOLD = 3
_CMS_PROBE_403_TTL_SEC = 6 * 3600


def _probe_403_host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url or "").hostname or ""
    except Exception:
        return ""


def _probe_403_count(host: str) -> int:
    try:
        # CacheManager 必须函数内导入（与本模块其余用法同口径）：
        # 本模块没有顶层导入，裸名会 NameError——且曾被下面的
        # except 静默吞掉，计数恒 0、跳过永不触发（CI 定因实证）。
        from backend.database import CacheManager

        raw = CacheManager.get(_CMS_PROBE_403_KEY_PREFIX + host.lower())
        return int(raw) if raw is not None else 0
    except Exception:  # noqa: BLE001 — 读不到按 0（照常实测），但留日志不许静默
        logger.warning("cms probe 403 count read failed host=%s",
                       host, exc_info=True)
        return 0


def _record_probe_403(host: str, is_403: bool) -> None:
    try:
        from backend.database import CacheManager

        key = _CMS_PROBE_403_KEY_PREFIX + host.lower()
        if is_403:
            CacheManager.set(
                key, str(_probe_403_count(host) + 1),
                ttl=_CMS_PROBE_403_TTL_SEC)
        else:
            CacheManager.delete(key)
    except Exception:  # noqa: BLE001 — 写失败不许影响探活主路径，但留日志不许静默
        logger.warning("cms probe 403 count write failed host=%s",
                       host, exc_info=True)


def _get_text_single_hop(url: str, headers: dict, timeout: float):
    """文本 GET 单跳：返回 (status, text|None, location|None)。

    与既有文本 GET 同口径：safe_urlopen 不跟随重定向、只读
    ``_M3U8_MAX_BYTES``；多带 Location 供跟随层逐跳决策（3xx 在
    无重定向处理器的 opener 下以 HTTPError 回到这里）。
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            body = resp.read(_M3U8_MAX_BYTES)
            location = resp.headers.get("Location") if resp.headers else None
            return resp.status, body.decode("utf-8", "replace"), location
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location") if exc.headers else None
        return exc.code, None, location
    except Exception:
        return None, None, None


def _default_http_get_text(url: str, headers: dict, timeout: float):
    """默认文本 GET 层：返回 (status, text|None)，只读一小段。"""
    status, text, _location = _get_text_single_hop(url, headers, timeout)
    return status, text


def _default_http_get_text_followed(url: str, headers: dict,
                                    timeout: float):
    """文本 GET（有界跟随重定向版）：跳转型播放地址抓清单用。

    与探活层 ``_default_http_range_probe`` 同款手工逐跳：最多
    ``UPSTREAM_PROBE_MAX_REDIRECTS`` 跳，每一跳的新 URL 先过
    ``upstream_http.verify_outbound_url`` 复验才发请求
    （safe_urlopen 发请求前还会再验一次，opener 本身仍不跟随）；
    验不过、没有 Location 或跳数超限，一律 (None, None) 按抓不到
    处理。返回最终跳的 (status, text)。

    总预算（PA-080）：``timeout`` 是整条跟随链的总预算、不是每跳
    预算——入口算 deadline，每跳取 min(timeout, 剩余)，预算耗尽
    即停；旧口径每跳各传 3.0 秒、至多 4 跳最坏 12 秒。
    """
    deadline = time.monotonic() + timeout
    current_url = url
    for hop in range(UPSTREAM_PROBE_MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, None
        status, text, location = _get_text_single_hop(
            current_url, headers, min(timeout, remaining))
        if status not in _PROBE_REDIRECT_STATUSES:
            return status, text
        if hop >= UPSTREAM_PROBE_MAX_REDIRECTS or not location:
            return None, None
        next_url = urllib.parse.urljoin(current_url, location)
        try:
            upstream_http.verify_outbound_url(next_url)
        except Exception:
            return None, None
        current_url = next_url
    return None, None


def _is_playlist_text(text) -> bool:
    """响应内容是不是 m3u8 清单：以 #EXTM3U 开头（容 BOM/空白）。"""
    if not text:
        return False
    return str(text).lstrip("﻿ \t\r\n").startswith("#EXTM3U")


def _capture_cms_quality(source, mapping_id, detail, address, *,
                         http_get_text=None) -> None:
    """取流成功顺手回填 CMS 线路画质（备注标记 + m3u8 主清单）。

    两路信号：detail 的 vod_remarks 里带明确画质标记（4K/1080P
    等）时拼标签存 ``label``；播放地址现抓清单解析分辨率/带宽/
    范围信号（真实数值，优先于备注标签）——.m3u8 地址直抓，
    跳转型地址（share 页等无 .m3u8 后缀的地址）走有界跟随
    （逐跳复验、只认 #EXTM3U 开头的内容，其余一律留空）；
    抓到的若是只有分段的 media playlist（无主清单信号），只
    回填分段时长求和 ``duration_sec``。全程 fail-safe：任何
    失败只留空，绝不阻塞、绝不改写 302 结果。

    新鲜度门禁（PA-073）：缓存里已有 label 且宽、高非空即视为
    完整，直接返回、不再发任何 HTTP——画质是 vod 的固有属性，
    7 天 TTL 内重复点播不该每次都在 302 下发前同步抓主清单；
    仅缺失/不完整（缺 label 或宽高任一为空）时才抓取回填，
    与 CMS_QUALITY_CACHE_TTL_SEC 注释的「过期后下一次点播取流
    时自然回填」口径一致。

    门禁放宽（PA-080）：media playlist 线只回填 duration_sec、403/
    HTML 线 payload 为空，旧门禁（仅 label＋宽高）对这两类永不
    闭合、每播重复抓取；现放宽为 label＋宽高齐全、或已有
    duration_sec、或已有 no-master 标记（非清单/失败落点已判死）
    任一即闭合；no-master 标记走 store_cms_quality 的 7 天 TTL，
    到期后下一次点播自然重探。
    """
    if mapping_id is None:
        return
    try:
        existing = read_cms_quality(mapping_id) or {}
        if ((existing.get("label") and existing.get("width")
                and existing.get("height"))
                or existing.get("duration_sec")
                or existing.get("no_master")):
            return
        from backend.emby_server import quality_label as ql
        payload: dict = {}
        if isinstance(detail, dict):
            label = ql.label_from_text(detail.get("vod_remarks"))
            if label:
                payload["label"] = label
        path = (address or "").split("?", 1)[0].lower()
        is_m3u8 = path.endswith(".m3u8")
        if http_get_text is not None:
            get_text = http_get_text
        elif is_m3u8:
            get_text = _default_http_get_text
        else:
            # 跳转型地址：只做一次有界跟随尝试（逐跳复验），
            # 落点内容不是清单就当没抓到，绝不放大抓取次数。
            get_text = _default_http_get_text_followed
        try:
            status, text = get_text(
                address, _source_headers(source), M3U8_FETCH_TIMEOUT_SEC)
        except Exception:  # noqa: BLE001 — 抓清单失败留空
            status, text = None, None
        if status == 200 and text and (is_m3u8 or _is_playlist_text(text)):
            master = ql.parse_m3u8_master(text)
            if master:
                payload.update({
                    "width": master.get("width") or 0,
                    "height": master.get("height") or 0,
                    "bandwidth": master.get("bandwidth") or 0,
                    "video_range": master.get("video_range"),
                })
                master_label = ql.label_for_master(master)
                if master_label:
                    payload["label"] = master_label
            elif _is_playlist_text(text):
                # media playlist（只有分段、无主清单信号）：宽高/
                # 带宽判不出，只回填分段时长求和这一项真信号。
                duration = ql.media_playlist_duration_sec(text)
                if duration:
                    payload["duration_sec"] = duration
        if payload:
            store_cms_quality(mapping_id, payload)
        else:
            # PA-080：本次确实发起了抓取却无可用信号（非清单/失败
            # 落点），写 no-master 标记使门禁闭合，TTL 内不再每播
            # 重探；TTL 到期后自然重探。展示侧只读宽高／标签／
            # 时长字段，标记键被忽略。
            store_cms_quality(mapping_id, {"no_master": True})
    except Exception:  # noqa: BLE001 — 画质回填绝不影响取流
        pass


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


class _CmsEvictableFailure(UpstreamPlaybackError):
    """缓存条目专属失败标记：status / code / message 与既有错误逐字一致。

    只在「本次 detail 来自缓存」且失败属可淘汰类（探活 403/404、
    解析失败）时抛出；``_resolve_applecms_stream`` 捕获后淘汰缓存
    并回源重解析一次。不命中的调用方按普通 UpstreamPlaybackError
    处理，对外行为无异。
    """


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


def get_source_row(db, source_id: int):
    """播放侧读源行唯一入口：api.py 等播放接线经本模块读源行，

    不直连 upstream_sources（25-1 接线守卫：api.py 等四文件不 import
    该模块；上游源读写仍只走 25-1，本模块只读）。
    """
    return us.get_source(db, source_id)


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


def _probe_single_hop(url: str, headers: dict, timeout: float):
    """探活单跳：返回 (status, retry_after, location)。

    单跳口径与旧探活逐字一致：成功回状态码；HTTPError 带状态码与
    Retry-After（能转 int 才带，转不了按没有计，不编数字）；
    超时/断连/出站校验拦截回 (None, None, None)。location 只取
    Location 头，任何一跳都不读响应体（探活不是拉流）。
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with upstream_http.safe_urlopen(req, timeout=timeout) as resp:
            location = resp.headers.get("Location") if resp.headers else None
            return resp.status, None, location
    except urllib.error.HTTPError as exc:
        retry_after = None
        raw = exc.headers.get("Retry-After") if exc.headers else None
        if raw is not None:
            try:
                retry_after = int(str(raw).strip())
            except (TypeError, ValueError):
                retry_after = None
        location = exc.headers.get("Location") if exc.headers else None
        return exc.code, retry_after, location
    except Exception:
        return None, None, None


def _default_http_range_probe(url: str, headers: dict, timeout: float):
    """默认 Range 探活层（urllib）：只读 1 字节，不拉整条流。

    有界跟随重定向（最多 ``UPSTREAM_PROBE_MAX_REDIRECTS`` 跳）：
    上游以 302 发流是 Emby 常见形态（stream → 网盘直链 → 签名链），
    而 safe_urlopen 按 SSRF 纪律刻意不跟随；若探活也不跟，302 会
    落进 ``_classify_probe_status`` 的兜底 502，把完全可播的内容
    误判不可用（2026-10-31 生产实证：两跳 302 后 206 可播）。
    跟随只在本层手工逐跳做：每一跳的新 URL 先过
    ``upstream_http.verify_outbound_url`` 复验才发请求（safe_urlopen
    发请求前还会再验一次，opener 本身仍不装重定向处理器）；
    验不过、没有 Location、或跳数超限，一律按连不上口径处理。

    返回 (status:int|None, retry_after:int|None)：均为**最终跳**
    的状态与 Retry-After，调用方的分类表只看最终跳，与直达同
    口径；超时/断连返回 (None, None)。
    """
    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-0"
    entry_host = _probe_403_host(url)
    if entry_host and _probe_403_count(entry_host) >= _CMS_PROBE_403_THRESHOLD:
        # 件一：恒 403 主机跳过探活（见 helpers 注释），返回 verdict
        # 与实测 403 一致，resolve 侧走既有软放行分支。
        return 403, None
    current_url = url
    for hop in range(UPSTREAM_PROBE_MAX_REDIRECTS + 1):
        status, retry_after, location = _probe_single_hop(
            current_url, probe_headers, timeout)
        if status not in _PROBE_REDIRECT_STATUSES:
            if entry_host and status is not None:
                # 超时/断连（None）不改计数：只有确定性状态才记
                # 403 或清键，None 落进 record 的 False 分支会把
                # 已攒计数误删（与本节头口径相悖，CI 定因实证）。
                _record_probe_403(entry_host, status == 403)
            return status, retry_after
        if hop >= UPSTREAM_PROBE_MAX_REDIRECTS or not location:
            return None, None
        next_url = urllib.parse.urljoin(current_url, location)
        try:
            upstream_http.verify_outbound_url(next_url)
        except Exception:
            return None, None
        current_url = next_url
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


def _fetch_cms_detail_network(source, vod_id: str, *,
                              http_get: Optional[Callable] = None,
                              headers: Optional[dict] = None) -> dict:
    """现取 CMS detail（网络）：错误映射与空 detail 口径原样。

    ``AppleCMSError`` → ``_map_applecms_error`` 映射；detail 为
    None（vod 消失）→ 404 upstream_item_not_found。成功返回 vod dict。
    """
    from backend.emby_server import applecms_fetcher as acf
    try:
        detail = acf.fetch_detail(
            source.base_url, vod_id, http_get=http_get,
            headers=_source_headers(source) if headers is None else headers)
    except acf.AppleCMSError as exc:
        raise _map_applecms_error(exc, source.id) from exc
    if detail is None:
        raise UpstreamPlaybackError(
            404, "upstream_item_not_found",
            "上游已无此条目", source_id=source.id)
    return detail


def _capture_cms_quality_async(source, mapping_id, detail, address, *,
                               http_get_text=None) -> None:
    """画质回填放后台线程：302 关键路径不等它。

    回填本体已 fail-safe（全程吞异常、缓存门禁先行），线程 daemon
    化、随进程退出丢弃——漏补的下次点播门禁未满时再补，不阻塞、
    不改写 302 结果。测试可 monkeypatch 本函数改同步。
    """
    if mapping_id is None:
        return
    thread = threading.Thread(
        target=_capture_cms_quality,
        args=(source, mapping_id, detail, address),
        kwargs={"http_get_text": http_get_text},
        name="cms-quality-capture", daemon=True)
    try:
        thread.start()
    except Exception:  # noqa: BLE001 — 后台回填失败不影响取流
        pass


def _play_from_cms_detail(source, detail, episode_index, *,
                          http_probe: Optional[Callable] = None,
                          http_get_text: Optional[Callable] = None,
                          quality_mapping_id=None,
                          parse_resolver: Optional[Callable] = None,
                          soft_pass_403: bool = True) -> str:
    """由一份 detail 解析播放地址并探活下发（CMS 分支后半段）。

    解析失败（``resolve_play_candidates`` 非 ok）抛
    ``_CmsEvictableFailure``（status/code 与既有口径逐字一致）；
    解析成功后在同一 vod 的候选地址间逐个探活（上限
    ``CMS_CANDIDATE_PROBE_LIMIT`` 个）：首个探活通过（或 403 软
    放行，见下）的候选即下发并回填画质。候选全在客户端所选映射的
    同一个 vod 内（CMS detail 的平行播放组是同一内容的等价线路），
    不跨映射、不跨条目、不换 MediaSource——选线口径不变，只是同一
    条线内首组被 CDN 判死时不再整线判死。探活 403：``soft_pass_403``
    为 True 时按既有软放行下发，为 False（detail 来自缓存）时抛
    ``_CmsEvictableFailure`` 让调用方淘汰重解析（保持缓存口径：
    首个候选 403 即淘汰，不逐个试缓存里的旧地址）。候选全灭时抛
    **首候选**的错误（404 仍包装为 ``_CmsEvictableFailure``，使
    缓存淘汰＋重取重试的既有机制仍只触发一轮；其余状态走
    ``_classify_probe_status`` 原样分类）。

    探活前先查已验活地址快路（``read_verified_play_address``）：
    同一映射刚下发过的地址仍在候选内时免探活直发，下发时（探活
    通过与 403 软放行两条返回路）顺手记账；快路不改变任何一次
    下发的地址选择，只省复播时对同一地址的重复验活等待。
    """
    from backend.emby_server import applecms_fetcher as acf
    try:
        episode = int(episode_index)
    except (TypeError, ValueError):
        episode = -1
    result = acf.resolve_play_candidates(
        detail, episode,
        parse_lines_enabled=applecms_parse_lines_enabled(),
        parse_resolver=parse_resolver)
    if result[0] != "ok":
        reason = result[1]
        if reason == "episode_out_of_range":
            raise _CmsEvictableFailure(
                404, "upstream_item_not_found",
                "上游无此集", source_id=source.id)
        messages = {
            "parse_not_enabled": "解析型线路未开启，该条目暂无直链可播",
            "parse_rule_missing": "本站无解析规则，该条目暂无直链可播",
            "no_direct_group": "该条目暂无直链可播",
        }
        raise _CmsEvictableFailure(
            502, "upstream_unavailable",
            messages.get(reason, "该条目暂无直链可播"),
            source_id=source.id)
    candidates = result[1]
    # 已验活地址快路（见 read_verified_play_address 一节）：同一条
    # 线路刚下发过的地址仍在本份 detail 候选内时免探活直发，省的
    # 是复播时对同一地址再验一遍的 CDN 首字节等待；候选 membership
    # 现算，detail 一变（换组/换地址）立刻回现行探活，不发旧地址。
    verified = read_verified_play_address(quality_mapping_id)
    if verified is not None and any(
            address == verified for _group_name, address
            in candidates[:CMS_CANDIDATE_PROBE_LIMIT]):
        _capture_cms_quality_async(source, quality_mapping_id, detail,
                                   verified, http_get_text=http_get_text)
        return verified
    probe = _default_http_range_probe if http_probe is None else http_probe
    first_error: Optional[UpstreamPlaybackError] = None
    for _group_name, address in candidates[:CMS_CANDIDATE_PROBE_LIMIT]:
        probe_status, retry_after = probe(
            address, _source_headers(source), CMS_PLAY_PROBE_TIMEOUT_SEC)
        if probe_status == 403:
            if not soft_pass_403:
                raise _CmsEvictableFailure(
                    403, "upstream_forbidden",
                    "上游拒绝了本次播放，请检查该源权限",
                    source_id=source.id)
            # 探活软放行（CMS 分支专属，2026-11-02 定案）：多家 CMS 播放
            # CDN 对数据中心 IP 一律 403——403 只证明「服务器这个 IP 被
            # 拒」，不证明链死；detail 刚刚现取成功（源活着、条目在、
            # 直链组在、集序号不越界）才是可用性信号。照常下发 302，
            # 播放器在用户家里自拉。日志只记 id 不记地址。其余状态仍走
            # 统一分类（404 判死 / 401 凭据 / 429 额度 / 5xx 不可用）；
            # Emby/Jellyfin 分支不经这里，403 仍判 upstream_forbidden。
            logger.info("cms_probe_soft_pass source_id=%s", source.id)
            store_verified_play_address(quality_mapping_id, address)
            _capture_cms_quality_async(source, quality_mapping_id, detail,
                                       address, http_get_text=http_get_text)
            return address
        try:
            _classify_probe_status(probe_status, source.id, retry_after)
        except UpstreamPlaybackError as exc:
            if probe_status == 404:
                exc = _CmsEvictableFailure(
                    exc.status, exc.code, exc.message,
                    source_id=exc.source_id,
                    retry_after=exc.retry_after)
            if first_error is None:
                first_error = exc
            continue
        logger.info("上游播放解析成功 source_id=%s", source.id)
        store_verified_play_address(quality_mapping_id, address)
        _capture_cms_quality_async(source, quality_mapping_id, detail,
                                   address, http_get_text=http_get_text)
        return address
    # resolve 口径保证 candidates 非空，全灭必有首候选错误。
    raise first_error


def _resolve_applecms_stream(source, vod_id: str, *,
                             episode_index: int = 1,
                             http_get: Optional[Callable] = None,
                             http_probe: Optional[Callable] = None,
                             http_get_text: Optional[Callable] = None,
                             quality_mapping_id=None,
                             parse_resolver: Optional[Callable] = None) -> str:
    """苹果 CMS 播放分支：现取现解析、先探后 302，绝不落库。

    门禁顺序：APPLECMS_ENABLED 关 → 404 upstream_playback_disabled
    （与总闸关闭行为一致）；分级门控开且映射未配置 → 403
    upstream_forbidden（文件里唯一的 403 码，复用；其标准语义是
    「上游拒绝」，此处是本地分级未配置的拒绝，message 已区分，
    若协调员要为分级单独立码，换这里一处即可）。

    vod_id 即调用方传入的 ``upstream_item_id``（映射行里记的上游
    条目 id 对苹果 CMS 型就是 vod_id）；``episode_index`` 从 1 起，
    默认 1（调用方未传集序号时播第 1 集）。detail 先查解析缓存
    （``read_cms_detail``）、未命中才现取，命中后失败淘汰重解析
    一次（见 ``cms_detail_cache_ttl`` 一节）；播放地址只出现在
    返回的 302 地址与探活请求里，不写库、不进日志
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
    headers = _source_headers(source)
    cache_on = cms_detail_cache_enabled()
    if cache_on:
        cached_detail = read_cms_detail(source.id, vod_id)
        if cached_detail is not None:
            try:
                return _play_from_cms_detail(
                    source, cached_detail, episode_index,
                    http_probe=http_probe, http_get_text=http_get_text,
                    quality_mapping_id=quality_mapping_id,
                    parse_resolver=parse_resolver, soft_pass_403=False)
            except _CmsEvictableFailure:
                # 命中缓存后失败（探活 403/404 或解析失败）：淘汰
                # 该条并回源重解析一次，不许把死缓存一直发给用户；
                # 落到下方现取路径，只重解析这一次。
                evict_cms_detail(source.id, vod_id)
                logger.info(
                    "cms_detail_cache_evicted source_id=%s", source.id)
    detail = _fetch_cms_detail_network(
        source, vod_id, http_get=http_get, headers=headers)
    address = _play_from_cms_detail(
        source, detail, episode_index,
        http_probe=http_probe, http_get_text=http_get_text,
        quality_mapping_id=quality_mapping_id,
        parse_resolver=parse_resolver, soft_pass_403=True)
    if cache_on:
        store_cms_detail(source.id, vod_id, detail,
                         cms_detail_cache_ttl(source.id, address))
    return address


# ---------------------------------------------------------------------------
# CMS 取流留痕、线路择优与坏源熔断（播放优化 B 件，2026-11-02）
#
# 数据来源钉死在真实点播路径上：生产探活工人三守护默认关，健康分
# 若只等探活喂数就是空中楼阁。取流收口（resolve_upstream_stream 的
# CMS 分支）逐次把成败/延迟写进既有健康记录（源级 SourceHealthRecord
# + 线路级 LineHealthRecord，同一份 Redis 存储、同一套状态机），
# PlaybackInfo 组装（get_displayable_mappings）按这份记录择优排序，
# 连续失败达阈值时进入 PA-078 双重保险判定，过门才经唯一启停
# 写口 disable_source 自动停用坏源（见 _maybe_disable_cms_source）。
# ---------------------------------------------------------------------------

def _get_health_store():
    """健康记录存储口（生产 Redis 实现；测试替换本函数注入内存实现）。"""
    return uh.RedisHealthStore()


def _cms_line_key(source_id, quality_mapping_id) -> str:
    """线路健康键：有映射按映射 id（一条映射 = 一条下发线路），
    显式源口无映射时按源 id 兜底。"""
    if quality_mapping_id is not None:
        return f"mapping:{quality_mapping_id}"
    return f"source:{source_id}"


#: 取流失败的结构化错误码 → 健康状态机的 failure_kind。未列出的码
#: 不写健康结论——那是本地状态，不是源的可用性证据：
#: upstream_playback_disabled 是本地总闸；upstream_forbidden 在 CMS
#: 分支有两个来源（本地分级门控未配置、源站接口 403），按码无法
#: 区分，且 403 与探活软放行同哲学（只证明服务器 IP 被拒或本地
#: 未配，不证明源坏了），一律不记失败，免得把好源误熔断。
#: upstream_quota_exhausted（429）同理刻意排除（PA-078）：429
#: 只证明源站对本站共享 IP 短暂限流，是瞬态、不是源坏了的证据；
#: 计入连续失败会在限流窗口内把健康源自动停用，且探活工人默认关、
#: 无自动复活。注意：排除的只是健康/熔断喂数，结构化错误本身
#: 仍按原样向调用方报 429 upstream_quota_exhausted。
_CMS_FAILURE_KINDS = {
    "upstream_credential_invalid": "unauthorized",
    "upstream_unavailable": "server_error",
}


def _cms_breaker_recheck_confirms_dead(source, http_get=None) -> bool:
    """整源停用前复核（PA-078 保险二）：换一种方式问源站还活着没。

    点播路径的 counted 失败来自 detail 抓取与播放地址探活；复核
    改走与轻探活（applecms_probe）同形态的只读一问——provide
    接口首页（ac=list&pg=1），不写任何健康状态、不受探活总开关
    影响（保险不能被另一个默认关的开关架空）：
    - 接口正常应答（fetch_page 成功返回）→ 源还活着，返回 False；
    - 限流（429）→ 源站明明应答了、只是限流，与 #1770 同口径，
      不是「已死」证据，返回 False；
    - 其余明确失败（AppleCMSError：连不上、401/403、非 1 code
      等）→ 拿到「已死」的第二种证据，返回 True；
    - 复核本身无法定性（非抓取层异常等）→ 返回 False：无法定性
      绝不当作确认已死，保险口径宁可晚停、不可误停。
    """
    from backend.emby_server import applecms_fetcher as acf
    try:
        acf.fetch_page(source.base_url, ac="list", pg=1,
                       http_get=http_get,
                       headers=_source_headers(source))
    except acf.AppleCMSError as exc:
        return exc.kind != "rate_limited"
    except Exception:  # noqa: BLE001 — 复核无法定性按未确认计，见 docstring
        logger.warning("CMS 熔断停用前复核无法定性，按未确认已死计 "
                       "source_id=%s", getattr(source, "id", None),
                       exc_info=True)
        return False
    return False


def _maybe_disable_cms_source(db, source, record, now) -> None:
    """整源停用的保险判定（PA-078）：两道门都过才真停。

    调用点已保证：本轮是 counted 失败、源仍启用、连续失败计数
    已达 cms_circuit_breaker_threshold()。
    - 门一（时间跨度）：失败串起点 first_failure_at 到 now 不足
      cms_circuit_breaker_min_span_sec() → 暂缓，只记 warning
      （cms_circuit_breaker_deferred reason=span）；计数保留，
      下一次 counted 失败再判。无起点（异常路径）按跨度 0 计。
    - 门二（停用前复核）：_cms_breaker_recheck_confirms_dead 换
      轻探活口径问源站 API，未确认已死 → 暂缓（reason=
      recheck_not_dead）。
    两门皆过才经唯一启停写口 disable_source 停用并落审计 error
    日志（cms_circuit_breaker_open，老 grep 口径不变）。本函数在
    _record_cms_stream_outcome 的 fail-open try 内被调用，异常
    由调用方吞掉，绝不打断取流。
    """
    span = (now - record.first_failure_at
            if record.first_failure_at is not None else 0.0)
    if span < cms_circuit_breaker_min_span_sec():
        logger.warning(
            "cms_circuit_breaker_deferred source_id=%s reason=span "
            "failures=%s span_sec=%.0f min_span_sec=%.0f",
            source.id, record.consecutive_unavailable_failures, span,
            cms_circuit_breaker_min_span_sec())
        return
    if not _cms_breaker_recheck_confirms_dead(source):
        logger.warning(
            "cms_circuit_breaker_deferred source_id=%s "
            "reason=recheck_not_dead failures=%s",
            source.id, record.consecutive_unavailable_failures)
        return
    us.disable_source(db, source)
    db.commit()
    logger.error(
        "cms_circuit_breaker_open source_id=%s failures=%s "
        "threshold=%s trigger=resolve_upstream_stream",
        source.id, record.consecutive_unavailable_failures,
        cms_circuit_breaker_threshold())


def _record_cms_stream_outcome(db, source, *, error_code=None,
                               latency_ms=None,
                               quality_mapping_id=None) -> None:
    """把一次 CMS 取流结论写进既有健康记录（源级 + 线路级）。

    - 源级：复用 upstream_health.apply_source_result 与
      SourceHealthRecord（键 = str(source.id)，与 CMS 轻探活同
      口径）——点播结论与探活结论共用同一份连续失败计数，健康分
      不另起第二套统计。
    - 线路级：复用 LineHealthRecord / apply_line_result（状态翻转
      仍守「源级已确认才摘线」口径），并补记线路连续失败数、失败
      原因与最近一次成功延迟，供择优排序消费。
    - 熔断：失败累计达到 cms_circuit_breaker_threshold() 且源仍
      启用时，进入 _maybe_disable_cms_source 的 PA-078 双重保险
      判定（时间跨度门 + 停用前复核门），两门皆过才经唯一启停
      写口 us.disable_source 停用并落一条审计日志；只对 applecms
      型源生效。恢复只走手动 enable_source（管理端现成启停，
      「最大」源人工停用即先例；enable 时同步清零熔断计数，
      见 upstream_sources.enable_source）——生产探活工人默认关，
      不存在可靠的自动复活信号，不许假装能自动恢复。
    - 条目不存在（upstream_item_not_found）按 unknown 记：只推进
      last_checked_at。源站明明应答了、是条目没了，不许拿它给源
      记失败、更不许触发熔断。
    全程 fail-open：留痕/熔断的任何异常只记 warning，绝不打断
    取流主路径（成功照常下发、失败照常抛原结构化错误）。
    """
    try:
        if getattr(source, "source_type", None) != "applecms":
            return
        if error_code is None:
            outcome = uh.ProbeOutcome(uh.OUTCOME_SUCCESS)
        elif error_code == "upstream_item_not_found":
            outcome = uh.ProbeOutcome(uh.OUTCOME_UNKNOWN)
        elif error_code in _CMS_FAILURE_KINDS:
            outcome = uh.ProbeOutcome(
                uh.OUTCOME_FAILURE, _CMS_FAILURE_KINDS[error_code])
        else:
            return
        store = _get_health_store()
        now = time.time()
        source_key = str(source.id)
        record = store.get_source(source_key) or uh.SourceHealthRecord(
            source_key=source_key)
        uh.apply_source_result(record, outcome,
                               threshold=uh.failure_threshold(), now=now)
        store.set_source(record)
        line_key = _cms_line_key(source.id, quality_mapping_id)
        line = store.get_line(line_key) or uh.LineHealthRecord(
            line_key=line_key)
        uh.apply_line_result(
            line, outcome,
            confirmed=record.availability_state == uh.STATE_UNAVAILABLE,
            now=now)
        if outcome.kind == uh.OUTCOME_SUCCESS:
            line.consecutive_failures = 0
            line.last_success_at = now
            if latency_ms is not None:
                line.last_latency_ms = float(latency_ms)
        elif outcome.kind == uh.OUTCOME_FAILURE:
            line.consecutive_failures += 1
            line.last_failure_kind = outcome.failure_kind
            line.last_failure_at = now
        store.set_line(line)
        if (outcome.kind == uh.OUTCOME_FAILURE and db is not None
                and getattr(source, "enabled", False)
                and record.consecutive_unavailable_failures
                >= cms_circuit_breaker_threshold()):
            _maybe_disable_cms_source(db, source, record, now)
    except Exception:  # noqa: BLE001 — 留痕/熔断不许打断取流，见 docstring
        logger.warning("CMS 取流留痕/熔断异常，已忽略 source_id=%s",
                       getattr(source, "id", None), exc_info=True)


#: CMS 线路档位降权名单 SystemConfig 键（upstream_aggregate 家族，
#: config_schema 登记为 int_list）：逗号分隔的源 id，名单内的源在
#: 线路排序里沉到同健康层最后、只作兜底（2026-11-03 印制广告摸底
#: 处置，用户亲点定版）。读取走 integrations.store 热读（60 秒 TTL
#: ＋写时失效），与本模块 _system_config_bool 同口径，不另开配置通道。
KEY_DEMOTED_SOURCE_IDS = "upstream_aggregate_demoted_source_ids"

#: 降权名单的代码默认（无配置行时生效）＝2026-11-03 摸底定版的
#: 毒档：红牛 14 / 光速 15 / IKUN 16 / 虎牙 18 / 鲸鱼 20 / 爱蛋 21 /
#: 茅台 22 / 速博 24——片头字幕式赌博赞助带抽样 93%~100%。干净档
#: （非凡 7 / 暴风 9 / 极速 10 / 如意 13）与未测未抽的源（无尽 11 /
#: 电影天堂 12 / 优酷 17 / 乐子 19 / 豆瓣 23 / 飘零 25）不进名单
#: ＝中性：不降权、也不提权，保持既有顺序。
DEFAULT_DEMOTED_SOURCE_IDS = frozenset({14, 15, 16, 18, 20, 21, 22, 24})

#: 「无配置行」哨兵：store.get_value 行不存在时原样回它，与「有行
#: 但为空」（管理员显式清空＝降权关）区分开，见 demoted_source_ids。
_DEMOTED_IDS_UNSET = "\x00upstream-demoted-source-ids-unset\x00"


def demoted_source_ids(db) -> frozenset:
    """读档位降权名单（源 id 集合），口径：

    - 无配置行 → 代码默认（摸底毒档名单）；
    - 有行 → 按行内逗号列表解析（全角逗号兼容、坏项跳过），空行
      ＝显式清空（降权关），不回退默认；
    - db=None 或读异常 → 代码默认：读失败不许把线路展示打挂，
      更不许静默关掉已定版的降权。
    """
    if db is None:
        return DEFAULT_DEMOTED_SOURCE_IDS
    try:
        from backend.integrations import store
        raw = store.get_value(db, KEY_DEMOTED_SOURCE_IDS,
                              _DEMOTED_IDS_UNSET)
    except Exception:  # noqa: BLE001 — 配置读失败回默认，见 docstring
        return DEFAULT_DEMOTED_SOURCE_IDS
    if raw is None or raw == _DEMOTED_IDS_UNSET:
        return DEFAULT_DEMOTED_SOURCE_IDS
    return _parse_source_id_list(raw)


def _parse_source_id_list(raw) -> frozenset:
    """源 id 名单的唯一解析口径（降权名单与专属名单共用）：

    逗号分隔（全角逗号兼容）、坏项跳过、只收 >= 1 的整数。
    """
    out = set()
    for part in str(raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            continue
        if n >= 1:
            out.add(n)
    return frozenset(out)


# ---------------------------------------------------------------------------
# 白名单专属线路（2026-11-04 用户亲令）：飞牛家宽回源线只给白名单
#
# 哪些源算「专属」不写死在代码里：名单是 SystemConfig
# ``upstream_aggregate_exclusive_source_ids``（upstream_aggregate 家族，
# int_list），管理后台「上游源」页可增删，服务端选线热读执法（store
# 60 秒 TTL＋写时失效，与降权名单同口径）。粒度落在「源」：选线模型
# 里用户看到的线路是逐条目的映射（十万级、随条目生灭，无法当管理
# 单位），而飞牛可达性本来就是按源/分片域名验的（试点按源取样），
# 源粒度与证据同构；同一源的全部映射统一待遇，也与降权名单先例一致。
#
# 与降权名单的关键差异：专属名单**没有代码默认**——无配置行/空行/
# 读失败一律空集＝功能惰性（普通选线零影响）。这是白名单权益面，
# 读不到名单时宁可不发专属线（fail-closed），不许凭代码默认把线
# 发出去。飞牛试点线（暴风 9 / 如意 13，试点已验）作为第一条种子
# 数据经配置落入名单，不经代码常量。
# ---------------------------------------------------------------------------

#: 白名单专属源名单 SystemConfig 键（upstream_aggregate 家族）。
KEY_EXCLUSIVE_SOURCE_IDS = "upstream_aggregate_exclusive_source_ids"

#: 飞牛试点基址 SystemConfig 键：形如
#: ``https://pilot.135505.autos/p/<TOKEN>``——路径 token 只许经配置
#: 注入（从飞牛本地 token.txt 读出后写入），不许进代码/仓库/报告。
KEY_FEINIU_PILOT_BASE_URL = "feiniu_pilot_base_url"

# ---------------------------------------------------------------------------
# pilot 独立入口（2026-11-05 用户亲定，专属线路形态改定）：pilot 域名
# 本身成为独立反代入口——飞牛 nginx 把 /p/ 之外路径反代到本站并注入
# 标记头 X-Aetrix-Pilot-Entry，后端据此认定 pilot 入口。标记头的值
# 必须与 pilot_entry_secret 完全一致且 pilot_entry_enabled 为开：
# 外部用户可直连 emby 域名并自带任意同名头，值不对/未配密钥/开关关
# 一律按普通入口处理，绝不凭一个头名就冒充 pilot 入口。两键均为纯
# SystemConfig 键（upstream_aggregate 家族），密钥原文只许经管理后台
# 写入，不许进代码/仓库/日志。
# ---------------------------------------------------------------------------

#: pilot 入口总开关 SystemConfig 键（upstream_aggregate 家族）。
KEY_PILOT_ENTRY_ENABLED = "pilot_entry_enabled"

#: pilot 入口标记密钥 SystemConfig 键：飞牛 nginx 注入的
#: X-Aetrix-Pilot-Entry 头值必须与它逐字一致。
KEY_PILOT_ENTRY_SECRET = "pilot_entry_secret"

#: pilot 入口标记头名（飞牛 nginx 反代时注入）。
PILOT_ENTRY_HEADER = "X-Aetrix-Pilot-Entry"


def exclusive_source_ids(db) -> frozenset:
    """读白名单专属源名单（源 id 集合），口径：

    - 无配置行 / 空行 → 空集（管理员未配或显式清空＝专属线关）；
    - db=None 或读异常 → 空集：这是权益面，fail-closed，不发线；
    - 解析与降权名单共用 _parse_source_id_list 唯一口径。
    """
    if db is None:
        return frozenset()
    try:
        from backend.integrations import store
        raw = store.get_value(db, KEY_EXCLUSIVE_SOURCE_IDS, "")
    except Exception:  # noqa: BLE001 — 读失败回空集，见上方口径
        return frozenset()
    return _parse_source_id_list(raw)


def feiniu_pilot_base_url(db) -> str:
    """读飞牛试点基址（尾部斜杠剥净）；未配/读失败回空串。"""
    if db is None:
        return ""
    try:
        from backend.integrations import store
        raw = store.get_value(db, KEY_FEINIU_PILOT_BASE_URL, "")
    except Exception:  # noqa: BLE001 — 读失败当未配，专属线不发
        return ""
    return str(raw or "").strip().rstrip("/")


def exclusive_line_ready(db) -> bool:
    """专属线路整体可用性：名单非空且试点基址已配，缺一即惰性。"""
    return bool(feiniu_pilot_base_url(db)) and bool(exclusive_source_ids(db))


def pilot_entry_enabled(db) -> bool:
    """pilot 独立入口总开关：默认关（无行＝关，入口形态未启用前
    任何标记头都不生效）。读取失败回关（fail-closed）。"""
    return _system_config_bool(db, KEY_PILOT_ENTRY_ENABLED, False)


def pilot_entry_secret(db) -> str:
    """读 pilot 入口标记密钥；未配/读失败回空串（＝入口永不认定）。"""
    if db is None:
        return ""
    try:
        from backend.integrations import store
        raw = store.get_value(db, KEY_PILOT_ENTRY_SECRET, "")
    except Exception:  # noqa: BLE001 — 读失败当未配，入口不认定
        return ""
    return str(raw or "").strip()


def pilot_entry_origin(db) -> str:
    """飞牛 pilot 域名 origin（scheme://host[:port]）：从飞牛基址派生。

    基址形如 https://pilot.135505.autos/p/<TOKEN>，origin 即
    https://pilot.135505.autos——pilot 入口下发的取流地址只借这个
    域名，基址的 /p/<TOKEN> 路径段是飞牛口令门、绝不进播放地址。
    未配/形态不对回空串。
    """
    base = feiniu_pilot_base_url(db)
    if not base:
        return ""
    try:
        parts = urllib.parse.urlsplit(base)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _pilot_header_value(request):
    """从请求取 pilot 标记头值：Starlette 头大小写不敏感；鸭子类型
    （测试桩的普通 dict）按大小写不敏感扫描兜底。取不到回空串。"""
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get(PILOT_ENTRY_HEADER)
    except Exception:  # noqa: BLE001
        value = None
    if value:
        return str(value)
    try:
        items = headers.items()
    except Exception:  # noqa: BLE001
        return ""
    for key, val in items:
        if str(key).lower() == PILOT_ENTRY_HEADER.lower() and val:
            return str(val)
    return ""


def is_pilot_entry(db, request) -> bool:
    """本次请求是否经 pilot 独立入口进入（唯一认定口径）。

    三项全满足才算：开关开、密钥已配、标记头值与密钥逐字一致
    （compare_digest 定长比较）。任一不满足按普通入口处理——
    外部伪造同名头直连 emby 域名时，值不对就只是普通请求，
    不报错、也不给 pilot 待遇。
    """
    if db is None or request is None:
        return False
    if not pilot_entry_enabled(db):
        return False
    secret = pilot_entry_secret(db)
    if not secret:
        return False
    value = _pilot_header_value(request)
    if not value:
        return False
    import secrets as _secrets
    return _secrets.compare_digest(value, secret)


def feiniu_pilot_rewrite(base_url: str, upstream_url: str):
    """把上游播放地址改写成飞牛试点通用代理地址；形态不对回 None。

    ``{base}/x/{host}{path}[?{query}]``——飞牛侧通用 location 按
    host 把请求原样代理到该播放主机（清单与分段同路，分段走缓存）。
    只收 http/https 且有主机名的地址；上游路径/查询串原样保留
    （分段相对名由播放器以本地址为基准拼接，自然回到飞牛路径）。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return None
    try:
        parts = urllib.parse.urlsplit(str(upstream_url or ""))
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    out = f"{base}/x/{parts.hostname}{parts.path or '/'}"
    if parts.query:
        out += f"?{parts.query}"
    return out


def _mapping_health_sort_key(store, mapping, source,
                             demoted_ids=frozenset()):
    """线路择优排序键（越小越靠前；同键靠稳定排序保持映射偏好序）。

    合成顺序（分层，靠前者优先）：
    1. 健康层（可用性优先，既有口径一字不动）：源健康已判不可用
       （含过期等效不可用）沉底；源有连续失败或本线路已翻
       unavailable 的次之；健康线路在前。无记录 = 没有坏过的
       证据，按健康计（与 is_source_available 的无记录当 Active
       同口径）。
    2. 档位降权（demoted_ids 名单内的源沉底）：只决定「同健康层
       里谁排前」，排在健康层之后——死线/熔断线永远不会因为
       档位干净被顶到活线前面；名单内的线不删、仍可播。
    3. 既有择优：先按连续失败数升序、再按最近成功延迟升序。
    """
    record = store.get_source(str(source.id))
    line = store.get_line(_cms_line_key(source.id, mapping.id))
    if record is not None and not uh.is_source_available(record):
        tier = 2
    elif ((record is not None
           and record.consecutive_unavailable_failures > 0)
          or (line is not None and not uh.is_line_available(line))):
        tier = 1
    else:
        tier = 0
    demoted = 1 if source.id in demoted_ids else 0
    failures = record.consecutive_unavailable_failures if record else 0
    if line is not None and line.consecutive_failures > failures:
        failures = line.consecutive_failures
    latency = line.last_latency_ms if line is not None else None
    return (tier, demoted, failures,
            latency if latency is not None else float("inf"))


def resolve_upstream_stream(db: Session, source_id: int,
                            upstream_item_id: str, *,
                            media_source_id: Optional[str] = None,
                            http_probe: Optional[Callable] = None,
                            http_post: Optional[Callable] = None,
                            episode_index: int = 1,
                            http_get: Optional[Callable] = None,
                            http_get_text: Optional[Callable] = None,
                            quality_mapping_id=None,
                            parse_resolver: Optional[Callable] = None) -> str:
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
        # CMS 取流留痕/熔断接线（播放优化 B 件）：逐次结论写既有
        # 健康记录，失败达阈值自动停用坏源；留痕全程 fail-open，
        # 原结构化错误与成功地址逐字不变（见 _record_cms_stream_outcome）。
        _cms_t0 = time.perf_counter()
        try:
            _cms_url = _resolve_applecms_stream(
                source, item_id, episode_index=episode_index,
                http_get=http_get, http_probe=http_probe,
                http_get_text=http_get_text,
                quality_mapping_id=quality_mapping_id,
                parse_resolver=parse_resolver)
        except UpstreamPlaybackError as exc:
            _record_cms_stream_outcome(
                db, source, error_code=exc.code,
                latency_ms=int((time.perf_counter() - _cms_t0) * 1000),
                quality_mapping_id=quality_mapping_id)
            raise
        _record_cms_stream_outcome(
            db, source,
            latency_ms=int((time.perf_counter() - _cms_t0) * 1000),
            quality_mapping_id=quality_mapping_id)
        return _cms_url
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


# ---------------------------------------------------------------------------
# U-02 详情页上游线路（客户端入口）
#
# 详情/PlaybackInfo 的来源列表在本地条目有上游映射时多一条「上游」线路，
# 点它走本站取流口：口子按本地条目 id 现查映射（get_best_mapping）、
# 复验后才调 resolve_upstream_stream——(source_id, upstream_item_id)
# 永不下发给前端长期持有。线路开关是 SystemConfig（upstream_aggregate
# 家族），默认开；关时列表不出现、取流口 404 明示。
# ---------------------------------------------------------------------------

#: 详情页上游线路开关 SystemConfig 键（upstream_aggregate 家族）。
KEY_DETAIL_LINE_ENABLED = "upstream_aggregate_detail_line_enabled"


def _system_config_bool(db, key, default):
    """SystemConfig 布尔热读：无行/非法/异常一律回默认。

    与 transfer_variant._system_config_bool 同口径（读失败不许把
    展示链路打挂，一律回落代码默认）。
    """
    if db is None:
        return default
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return default
    text = str(raw or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def detail_line_enabled(db) -> bool:
    """详情页上游线路开关：默认开（已部署功能开关默认开）。"""
    return _system_config_bool(db, KEY_DETAIL_LINE_ENABLED, True)


def get_displayable_mapping(db, local_item_id):
    """列表展示口径的映射读取：返回 ``(mapping, source)`` 或 None。

    身份组（``get_grouped_mappings``，自身 + 同 tmdb 身份重复行）
    偏好序首位映射且源存在、已启用才算可展示；映射指向的源
    已删/已停用时线路不出现（取流口复验同口径，见
    resolve_mapped_upstream_stream）。无重复行时组内首位与
    get_best_mapping 逐字等价。
    """
    grouped = uim.get_grouped_mappings(db, local_item_id)
    mapping = grouped[0] if grouped else None
    if mapping is None:
        return None
    source = us.get_source(db, mapping.source_id)
    if source is None or not source.enabled:
        return None
    return mapping, source


def get_displayable_mappings(db, local_item_id):
    """列表展示口径的全部映射读取：返回 ``[(mapping, source), ...]``。

    播放源按映射逐条下发（站主 2026-11-02 亲定：每条上游线路一个
    MediaSource、点选哪条定向解析哪条）需要的全量读口。有效性口径
    与 get_displayable_mapping 逐字一致（映射层 O-10 校验先行，
    再逐条验源存在且启用），偏好序即列表展示序（最佳在前）。

    映射集合走映射层的身份组读口（``get_grouped_mappings``）：同
    一部片的未合并重复行（同库同 tmdb 身份）线路一并下发——只读
    命中行自身映射时，代表行线路全死、同片兄弟行的活线路永远
    到不了客户端（2026-11-03《丛林猎凶》实证）。MediaSource 的
    Id 仍挂命中条目的 guid + 映射 id，定向解析同口径认组内映射。

    择优排序（播放优化 B 件）：返回前按既有健康记录稳定排序——
    不可用/连续失败的线路沉底、延迟低且成功率高的排前面、最优线
    成为默认首位，其余线保留可手选；同分保持映射偏好序。健康
    记录读不到（Redis 故障等）时 fail-open 回偏好序原样，绝不
    因为择优把线路弄丢或把 PlaybackInfo 组装打挂。

    档位降权（2026-11-03）：同一份排序键里健康层之后合成档位
    （见 _mapping_health_sort_key 与 demoted_source_ids）——带
    赌博赞助带的毒档源在同健康层内沉底、只作兜底，线不删、
    仍可播；只有毒档线的条目照常全量下发。
    """
    out = []
    for mapping in uim.get_grouped_mappings(db, local_item_id):
        source = us.get_source(db, mapping.source_id)
        if source is None or not source.enabled:
            continue
        out.append((mapping, source))
    if len(out) > 1:
        try:
            store = _get_health_store()
            demoted_ids = demoted_source_ids(db)
            out.sort(key=lambda pair: _mapping_health_sort_key(
                store, pair[0], pair[1], demoted_ids))
        except Exception:  # noqa: BLE001 — 择优是补强，故障回偏好序
            logger.warning("上游线路择优排序异常，按映射偏好序下发",
                           exc_info=True)
    return out


def _default_http_get_json(url: str, headers: dict, timeout: float):
    """默认 GET 层（与 _default_http_post 同款）：返回 (status, body)。"""
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


#: 现查媒体参数时轨道只许带这些字段：上游 MediaStream 里的
#: DeliveryUrl / Path 等可能嵌着对方地址与凭据，一律不许过界。
_MEDIA_PARAM_STREAM_KEYS = (
    "Index", "Type", "Codec", "Language", "DisplayTitle", "IsDefault",
    "IsForced", "IsExternal", "Width", "Height", "Channels", "BitRate",
    "AspectRatio", "VideoRange", "SampleRate",
)


def fetch_upstream_media_params(source, upstream_item_id: str, *,
                                http_get: Optional[Callable] = None,
                                http_post: Optional[Callable] = None):
    """现查上游条目的媒体参数（无本地源时的合成源取数，进库一期）。

    GET ``{base}/Items/{id}`` 取首个 MediaSource 的 Size /
    RunTimeTicks / Container / Bitrate 与轨道摘要（白名单字段，见
    ``_MEDIA_PARAM_STREAM_KEYS``——绝不带上游 URL、路径与凭据）。
    凭据走本模块唯一消费点 ``_auth_token_for_source``。任何失败
    （凭据失效、上游不可用、响应异常）一律返回 None：合成源回落
    本地行数据，现查只是参数补强，绝不许把 PlaybackInfo 打挂。
    """
    try:
        token = _auth_token_for_source(source, http_post=http_post)
    except Exception:
        return None
    base = (source.base_url or "").rstrip("/")
    item_part = urllib.parse.quote((upstream_item_id or "").strip(), safe="")
    if not base or not item_part:
        return None
    url = f"{base}/Items/{item_part}"
    if token:
        url += "?" + urllib.parse.urlencode({"api_key": token})
    get = _default_http_get_json if http_get is None else http_get
    try:
        status, body = get(url, _source_headers(source),
                           UPSTREAM_PROBE_TIMEOUT_SEC)
    except Exception:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    media_sources = body.get("MediaSources")
    if not isinstance(media_sources, list) or not media_sources:
        return None
    first = media_sources[0]
    if not isinstance(first, dict):
        return None
    streams = []
    raw_streams = first.get("MediaStreams")
    if isinstance(raw_streams, list):
        for raw in raw_streams:
            if isinstance(raw, dict):
                streams.append({k: raw[k] for k in _MEDIA_PARAM_STREAM_KEYS
                                if k in raw})
    return {
        "size": first.get("Size"),
        "run_time_ticks": first.get("RunTimeTicks"),
        "container": first.get("Container"),
        "bitrate": first.get("Bitrate"),
        "media_streams": streams,
    }


def resolve_mapped_upstream_stream(db: Session, local_item_id: int, *,
                                   mapping_id=None,
                                   http_probe=None, http_post=None,
                                   http_get=None, http_get_text=None,
                                   parse_resolver=None) -> str:
    """按本地条目 id 解析上游取流地址（详情页上游线路取流口专用）。

    现查映射 + 复验：开关关 → 404 upstream_line_disabled；无映射 →
    404 upstream_mapping_not_found；源停用按 O-10 读契约
    （get_best_mapping 默认 validate=True）在现查时即不可见、同
    无映射口径 404（查与解析之间源被停用的竞态仍由
    resolve_upstream_stream 抛 409 upstream_source_disabled）；
    凭据失效等其余结构化错误原样上抛——明示失败，
    绝不静默回落到任何其它线路。

    ``mapping_id`` 定向口径（站主 2026-11-02 亲定，FMBY 形态）：
    播放器点选了具体某条线路（MediaSourceId 带映射 id）时，只解
    析那一条映射——映射不存在、不属于本条目的身份组、或按读契约
    已不可见（源停用/条目已死），一律 404
    upstream_mapping_not_found 硬报错，**绝不偷偷换到最佳映射**。
    不带 mapping_id 的旧形态（MediaSourceId 只到 ``:upstream``）
    保持最佳映射口径。

    身份组口径（2026-11-03）：映射集合与展示侧
    :func:`get_displayable_mappings` 同源——走映射层
    ``get_grouped_mappings``（自身 + 同库同 tmdb 身份的未合并
    重复行）。展示下发了哪条线路，定向解析就必须认哪条；最佳
    口径同样取组内偏好序首位（无重复行时与 get_best_mapping
    逐字等价，自身无有效映射时才落到兄弟行）。
    """
    if not detail_line_enabled(db):
        raise UpstreamPlaybackError(
            404, "upstream_line_disabled", "上游线路未开启")
    if mapping_id is not None:
        mapping = None
        for row in uim.get_grouped_mappings(db, local_item_id):
            if row.id == mapping_id:
                mapping = row
                break
        if mapping is None:
            raise UpstreamPlaybackError(
                404, "upstream_mapping_not_found", "所选线路已不可用")
    else:
        grouped = uim.get_grouped_mappings(db, local_item_id)
        mapping = grouped[0] if grouped else None
        if mapping is None:
            raise UpstreamPlaybackError(
                404, "upstream_mapping_not_found", "该条目暂无上游线路")
    return resolve_upstream_stream(
        db, mapping.source_id, mapping.upstream_item_id,
        episode_index=mapping.episode_index or 1,
        http_probe=http_probe, http_post=http_post, http_get=http_get,
        http_get_text=http_get_text, quality_mapping_id=mapping.id,
        parse_resolver=parse_resolver)

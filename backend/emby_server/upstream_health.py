# -*- coding: utf-8 -*-
"""上游源探活与 availability_status（FMBY 学习 #25 25-3，源级）。

## 本模块是什么

FMBY 把“某条上游线路还活不活”做成**落库的状态**，而不是进程里的
dict：线路状态在 ``upstream_playback_variants.availability_status``，
源状态在 ``library_source_health``（``availability_state``、
``consecutive_unavailable_failures``、``last_failure_kind``、``hidden_at``）。
本模块先落地其中**源级**的一半——源健康状态机 + 源握手探活工人。
线路级的状态机（``apply_line_result``）同文件给出，但线路的实际抓取
（对方 PlaybackInfo + 小 Range 试读）属于 25-4，那时才有 25-2 落地的
线路数据；本阶段不给线路级状态机接任何生产调用方。

## 按 FMBY 实测口径做的部分（深挖报告第 4 节 + 研究第六阶段）

以下每条都有实测证据（闭源二进制串 / 前人报告交叉），不是自创口径：

- **状态值**：源状态只存 ``"Active"`` / ``"Unavailable"`` 两种；
  查询语义是 ``COALESCE(availability_state, 'Active') = 'Active'``，
  即**没有健康记录的源默认当可用**，不是默认当不可用。
- **Unknown 是“探活结论”，不是“存的状态”**：新状态为 Unknown 时
  保留旧状态（写进 SQL 的克制）。本模块里 Unknown 结论只推进
  ``last_checked_at``，状态、失败计数、隐藏时间、失败原因一律不动。
- **成功复位**：一次成功就把状态复位为 Active、连续失败计数清零、
  ``hidden_at`` 清空、``last_success_at`` 更新。
- **失败计数与隐藏**：失败累计 ``consecutive_unavailable_failures``；
  达到阈值才把状态翻为 Unavailable 并记 ``hidden_at``（首次翻转时间），
  之后继续失败只累计计数、不刷新隐藏时间（隐藏起点不被反复改写）。
- **摘线后调度过滤**：状态非 Active 的源在调度时不应再被下发。
  本模块只提供 ``is_source_available`` 这一判断口；调度侧接线是 25-4。
- **熔断复用，不另起一套**：探活的请求级开合复用批一 #8 的统一熔断
  注册表（``metasources.breaker``），探活前先 ``allow``，探活后按结论
  喂 ``record_success`` / ``record_failure``。注意熔断是**进程内**的，
  与本模块持久化的 availability 状态是两层东西，不可混为一谈：
  熔断管“这次请求让不让打”，状态管“这个源对外还算不算可用”。

## 证据不足、待复验 / 待裁定的部分（显式标出，不许当成定版读）

- **探活周期与失败阈值的默认值**：FMBY 探活的“几次摘、几轮接回”
  在深挖中**没有实测到阈值常量**（只看到计数与复位机制）。B3 报告
  建议的“60 秒周期 × 3 次失败”是**建议值，不是 FMBY 实测值**；
  豆瓣搜索路径“单次 403 即冷却约 60 秒”是另一条路径的现象，
  深挖已注明不许把它统一成一个探活阈值。本模块的默认值
  （周期 60 秒、阈值 3 次）只是**我方起步值**，主代理裁定前不得
  视为 FMBY 口径；两个值都做成 env，改数不用改代码。
- **线路级 availability_status 的库内枚举**：深挖只实测到字段名与
  前端映射（healthy/active/available/ready/ok → healthy；
  critical/error/failed/offline/unavailable → critical；默认
  attention），库内枚举本身未直接观测。本模块线路状态存
  ``"available"`` / ``"unavailable"``，这是按前端映射与 B3 验收措辞
  （“翻为 unavailable / 回 available”）拼的最接近形态，待复验。
- **线路翻转是按源确认还是按线路计数**：B3 验收只写到“源打挂后其
  全部变体翻为 unavailable”。FMBY 是源状态驱动线路翻转、还是每条
  线路各自计数，深挖未直接观测。本模块的 ``apply_line_result``
  因此要求调用方显式传 ``confirmed``（源级已确认才翻线路），
  不把“线路失败一次就摘”写死进状态机。
- **持久化落点**：FMBY 的状态在 PG 表里。本阶段 25-1 的源表还没
  合并，新建一张竞争关系的 PG 表会与 25-1 的设计撞车；按 B3
  “状态必须落 DB/Redis”的口径，本阶段先落 Redis（进程重启不丢
  的前提是 Redis 自身持久化，见报告），25-1 落地后迁移到 PG 表
  ``upstream_source_health`` 是明确的后续项，不是“已等价于 FMBY”。
- **探活深度**：本阶段工人只做源握手（见 ``probe_source``），
  不做 PlaybackInfo + 小 Range 试读——那是线路级真探活，属于
  25-4 且需 25-2 的线路数据。源活着但播放接口坏时（System/Info
  200、PlaybackInfo 500），本阶段的源握手**判不出来**，这是本阶段
  已知的能力边界，不是“源握手 = FMBY 真探活”。

## 红线

- 默认关：``UPSTREAM_HEALTH_PROBE_ENABLED`` 默认 0，关时
  ``probe_sources_once`` 不发任何请求、不写任何状态，全站行为逐字不变。
- 不自动启动：本模块 import 时不启动任何线程 / 定时任务；
  ``start_probe_daemon`` 必须被显式调用才跑。
- 不重试风暴：每源每轮最多一次请求；失败不立即重打，退避只发生在
  已翻为 Unavailable 之后（见 ``next_probe_due``），计数阶段仍按周期探。
- 凭据纪律：上游 api_key 只放请求头（``X-Emby-Authorization``），
  绝不进 URL、绝不进日志、绝不进持久化记录；base_url 带 userinfo
  或非 http(s) 协议时直接判 Unknown、不发请求（SSRF 面）。
- 不改默认主线、不改 play_sign、不改 EA 申报、不改 #1205 缓存键、
  不改 #1207 租约、不改 #1208 打点；不写死任何业务数值（阈值、
  周期、超时、退避上限全是 env）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Protocol
from urllib.parse import urlparse

from backend.emby_server.env_util import env_float, env_int
from backend.emby_server.metasources import breaker as provider_breaker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状态值（FMBY 实测口径）
# ---------------------------------------------------------------------------

#: 源健康状态：FMBY 库内实测值。没有记录时按 Active 计（见 is_source_available）。
STATE_ACTIVE = "Active"
STATE_UNAVAILABLE = "Unavailable"

#: 线路可用性状态：最接近 FMBY 前端映射的形态，库内枚举待复验（见模块 docstring）。
LINE_AVAILABLE = "available"
LINE_UNAVAILABLE = "unavailable"

#: 探活结论：Unknown 是结论、不是状态——它永远不写进状态字段。
OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_UNKNOWN = "unknown"

#: Redis 持久化键前缀。记录是 JSON，不含任何凭据。
REDIS_KEY_PREFIX = "upstream:health:"


# ---------------------------------------------------------------------------
# 配置（全部 env；默认值是“我方起步值”，待裁定——见模块 docstring）
# ---------------------------------------------------------------------------

def probe_enabled() -> bool:
    """探活总开关，默认关。关时工人不发请求、不写状态。"""
    return (str(os.getenv("UPSTREAM_HEALTH_PROBE_ENABLED", "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off"})


def probe_interval_sec() -> float:
    """探活周期（秒）。默认值 60 是 B3 建议值，非 FMBY 实测值，待裁定。"""
    return env_float("UPSTREAM_HEALTH_PROBE_INTERVAL_SEC", 60.0, 1.0, 86400.0)


def failure_threshold() -> int:
    """连续失败几次翻为 Unavailable。默认值 3 是建议值拼凑，非实测值，待裁定。"""
    return env_int("UPSTREAM_HEALTH_FAILURE_THRESHOLD", 3, 1, 100)


def probe_timeout_sec() -> float:
    """单次握手超时（秒）。默认值 5 与 node_health 同口径，我方起步值，待实测调。"""
    return env_float("UPSTREAM_HEALTH_PROBE_TIMEOUT_SEC", 5.0, 0.5, 120.0)


def probe_backoff_max_sec() -> float:
    """已翻 Unavailable 后的退避上限（秒）。我方起步值，待裁定。"""
    return env_float("UPSTREAM_HEALTH_BACKOFF_MAX_SEC", 600.0, 60.0, 86400.0)


# ---------------------------------------------------------------------------
# 源与探活结论
# ---------------------------------------------------------------------------

@dataclass
class UpstreamSource:
    """25-1 落地前的接口约定（替身上游源按此形状构造即可）。

    25-1 的真实源表字段（研究第六阶段实测 FMBY 形态）含 source_type、
    base_url、auth_method、sealed_secret 等；本模块只依赖下面这组
    最小约定，不碰 25-1 的文件、不等 25-1 合并。
    """

    key: str  # 源唯一键（25-1 落地后对齐为源 id 的字符串形态）
    kind: str  # "emby" | "jellyfin"（首批只做这两型，CMS 型后置）
    base_url: str
    enabled: bool = True
    api_key: Optional[str] = None  # 只进请求头，绝不进日志与持久化


@dataclass
class ProbeOutcome:
    """一次探活的结论。failure_kind 仅用于观测与熔断喂数，不进状态枚举。"""

    kind: str  # success | failure | unknown
    failure_kind: Optional[str] = None  # unreachable|unauthorized|rate_limited|server_error
    http_status: Optional[int] = None


@dataclass
class SourceHealthRecord:
    """源健康记录（字段名对齐 FMBY library_source_health 实测字段）。"""

    source_key: str
    availability_state: str = STATE_ACTIVE
    consecutive_unavailable_failures: int = 0
    last_failure_kind: Optional[str] = None
    last_failure_at: Optional[float] = None
    last_success_at: Optional[float] = None
    last_checked_at: Optional[float] = None
    hidden_at: Optional[float] = None  # 首次翻为 Unavailable 的时刻；成功清零
    next_probe_at: float = 0.0  # <= now 即到期；0 表示从未探过、立即到期


@dataclass
class LineHealthRecord:
    """线路可用性记录（字段名对齐 upstream_playback_variants 实测字段）。"""

    line_key: str
    availability_status: str = LINE_AVAILABLE
    last_checked_at: Optional[float] = None


def is_source_available(record: Optional[SourceHealthRecord]) -> bool:
    """调度过滤口：无记录当 Active；只有明确 Unavailable 才算不可用。"""
    if record is None:
        return True
    return record.availability_state == STATE_ACTIVE


def is_line_available(record: Optional[LineHealthRecord]) -> bool:
    """线路调度过滤口：无记录当 available；Unknown 从不落为状态，故无此分支。"""
    if record is None:
        return True
    return record.availability_status == LINE_AVAILABLE


# ---------------------------------------------------------------------------
# 纯状态机（无 I/O，可单测钉死）
# ---------------------------------------------------------------------------

def apply_source_result(
    record: SourceHealthRecord,
    outcome: ProbeOutcome,
    *,
    threshold: int,
    now: float,
) -> SourceHealthRecord:
    """把一次探活结论写进源健康记录（FMBY 实测规则）。

    - success：状态复位 Active、计数清零、hidden_at 清空、记 last_success_at。
    - failure：计数 +1、记失败原因与时刻；计数达到阈值才翻 Unavailable
      并记 hidden_at；已是 Unavailable 时只累计、不刷新 hidden_at。
    - unknown：只推进 last_checked_at，其余字段一律不动。
    """
    record.last_checked_at = now
    if outcome.kind == OUTCOME_SUCCESS:
        record.availability_state = STATE_ACTIVE
        record.consecutive_unavailable_failures = 0
        record.last_success_at = now
        record.hidden_at = None
    elif outcome.kind == OUTCOME_FAILURE:
        record.consecutive_unavailable_failures += 1
        record.last_failure_kind = outcome.failure_kind
        record.last_failure_at = now
        if (record.consecutive_unavailable_failures >= threshold
                and record.availability_state != STATE_UNAVAILABLE):
            record.availability_state = STATE_UNAVAILABLE
            record.hidden_at = now
    elif outcome.kind == OUTCOME_UNKNOWN:
        pass  # 克制写进状态机：未知不覆盖已知
    else:  # pragma: no cover - 防御：未知结论名按 unknown 计
        logger.warning("未知探活结论 %r，按 unknown 处理", outcome.kind)
    return record


def apply_line_result(
    record: LineHealthRecord,
    outcome: ProbeOutcome,
    *,
    confirmed: bool,
    now: float,
) -> LineHealthRecord:
    """把一次线路探活结论写进线路记录。

    - success：翻为 available。
    - failure 且 confirmed（源级已确认，见模块 docstring 待复验项）：
      翻为 unavailable；failure 但未确认时只推进 last_checked_at，
      不摘线（防单次抖动闪断）。
    - unknown：只推进 last_checked_at，状态不动。
    """
    record.last_checked_at = now
    if outcome.kind == OUTCOME_SUCCESS:
        record.availability_status = LINE_AVAILABLE
    elif outcome.kind == OUTCOME_FAILURE and confirmed:
        record.availability_status = LINE_UNAVAILABLE
    return record


def next_probe_due(
    record: SourceHealthRecord,
    *,
    interval_sec: float,
    backoff_max_sec: float,
    threshold: int,
    now: float,
) -> float:
    """算下次探活时刻并写回记录。

    - Active（含计数中）：按周期探，不退避——“多次确认才摘”要求
      计数阶段的探活节奏不被拉长，否则 B3 验收的翻转时限无从谈起。
    - Unavailable：退避，间隔 = interval * 2**(翻转后又失败的次数)，
      上限 backoff_max_sec；恢复只靠一次成功，不靠“多轮好转”。
    """
    if record.availability_state == STATE_UNAVAILABLE:
        extra = max(0, record.consecutive_unavailable_failures - threshold)
        # 指数先封顶再乘：extra 无上界时 interval * 2**extra 会在
        # extra 约 1020 处 OverflowError（长死源约 7 天即到），异常
        # 会在 set_source 之前炸掉 probe_source、连带 abort 整轮
        # probe_sources_once（后排源当轮全被跳过）。2**60 已远超
        # 任何 backoff_max_sec 上限，封顶不改变任何实际退避值。
        delay = min(interval_sec * (2 ** min(extra, 60)), backoff_max_sec)
    else:
        delay = interval_sec
    record.next_probe_at = now + delay
    return record.next_probe_at


# ---------------------------------------------------------------------------
# 探活结论分类（纯函数）
# ---------------------------------------------------------------------------

def classify_handshake(status: Optional[int], body: object) -> ProbeOutcome:
    """给源握手响应定性。

    - 200 且 body 是含 Id/ServerName 的 dict：源活着 → success。
    - 200 但 body 形态不对：无法判断源是死了还是只回了怪东西 → unknown
      （不覆盖已知状态；这是 Unknown 结论最常见的合法来源）。
    - 401/403 → failure/unauthorized；429 → failure/rate_limited；
      5xx → failure/server_error；其余状态 → failure/server_error。
    - status 为 None（调用方没拿到响应，如超时/断连）→ failure/unreachable。
    """
    if status is None:
        return ProbeOutcome(OUTCOME_FAILURE, "unreachable", None)
    if status == 200:
        if isinstance(body, dict) and ("Id" in body or "ServerName" in body):
            return ProbeOutcome(OUTCOME_SUCCESS, None, 200)
        return ProbeOutcome(OUTCOME_UNKNOWN, None, 200)
    if status in (401, 403):
        return ProbeOutcome(OUTCOME_FAILURE, "unauthorized", status)
    if status == 429:
        return ProbeOutcome(OUTCOME_FAILURE, "rate_limited", status)
    return ProbeOutcome(OUTCOME_FAILURE, "server_error", status)


def classify_line_probe(
    system_ok: bool,
    playback_status: Optional[int],
    range_status: Optional[int],
) -> ProbeOutcome:
    """给线路级真探活定性（分类逻辑先行，抓取属于 25-4）。

    覆盖 B3 验收第 4 条的关键情形：源进程活着（System/Info 200）
    但播放接口坏（PlaybackInfo 500）时，线路必须被判失败——
    只 ping 通不算活。playback_status/range_status 为 None
    （调用方没拿到可定性的响应）时判 unknown，不覆盖已知。
    """
    if not system_ok:
        return ProbeOutcome(OUTCOME_FAILURE, "unreachable", None)
    if playback_status is not None and playback_status != 200:
        # 播放接口已明确坏（哪怕 Range 还没试）：失败是确定的，不判 unknown。
        return ProbeOutcome(OUTCOME_FAILURE, "playback_unavailable", playback_status)
    if playback_status is None or range_status is None:
        return ProbeOutcome(OUTCOME_UNKNOWN, None, playback_status)
    if range_status == 206:
        return ProbeOutcome(OUTCOME_SUCCESS, None, 200)
    return ProbeOutcome(OUTCOME_FAILURE, "range_unavailable", range_status)


# ---------------------------------------------------------------------------
# 持久化：HealthStore 协议 + 内存实现 + Redis 实现
# ---------------------------------------------------------------------------

class HealthStore(Protocol):
    def get_source(self, key: str) -> Optional[SourceHealthRecord]: ...
    def set_source(self, record: SourceHealthRecord) -> None: ...
    def get_line(self, key: str) -> Optional[LineHealthRecord]: ...
    def set_line(self, record: LineHealthRecord) -> None: ...


class InMemoryHealthStore:
    """测试与无 Redis 环境用的内存实现（进程重启丢状态，仅作替身）。"""

    def __init__(self) -> None:
        self._sources: dict[str, SourceHealthRecord] = {}
        self._lines: dict[str, LineHealthRecord] = {}

    def get_source(self, key: str) -> Optional[SourceHealthRecord]:
        return self._sources.get(key)

    def set_source(self, record: SourceHealthRecord) -> None:
        self._sources[record.source_key] = record

    def get_line(self, key: str) -> Optional[LineHealthRecord]:
        return self._lines.get(key)

    def set_line(self, record: LineHealthRecord) -> None:
        self._lines[record.line_key] = record


def _record_to_json(record: SourceHealthRecord) -> str:
    return json.dumps({
        "availability_state": record.availability_state,
        "consecutive_unavailable_failures": record.consecutive_unavailable_failures,
        "last_failure_kind": record.last_failure_kind,
        "last_failure_at": record.last_failure_at,
        "last_success_at": record.last_success_at,
        "last_checked_at": record.last_checked_at,
        "hidden_at": record.hidden_at,
        "next_probe_at": record.next_probe_at,
    })


def _record_from_json(key: str, raw: str) -> Optional[SourceHealthRecord]:
    """脏 Redis 值按无记录计（fail-open，与 get_line 的坏 JSON 同口径）。

    只拦 json 解析与非 dict 是不够的：数值字段被写坏（如
    ``consecutive_unavailable_failures`` 存成 ``"abc"``）时 int/float
    转换照样抛 ValueError，且 RedisHealthStore.get_source 只包了
    Redis 读本身——异常会一路穿出 probe_source、abort 整轮探活。
    数值字段一律在 try 内转换，任一字段坏掉即整条按无记录计。
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        failures = int(data.get("consecutive_unavailable_failures") or 0)
        next_probe_at = float(data.get("next_probe_at") or 0.0)
    except (ValueError, TypeError):
        return None
    return SourceHealthRecord(
        source_key=key,
        availability_state=data.get("availability_state", STATE_ACTIVE),
        consecutive_unavailable_failures=failures,
        last_failure_kind=data.get("last_failure_kind"),
        last_failure_at=data.get("last_failure_at"),
        last_success_at=data.get("last_success_at"),
        last_checked_at=data.get("last_checked_at"),
        hidden_at=data.get("hidden_at"),
        next_probe_at=next_probe_at,
    )


class RedisHealthStore:
    """Redis 实现：键 ``upstream:health:<source_key>``，值是 JSON（不含凭据）。

    Redis 不可用时只记 warning 并跳过持久化，不抛异常、不打断工人
    （fail-open：探活结论丢一轮，下轮重新探；绝不因为状态存不下
    就把源判死或把工人打停）。
    """

    def __init__(self, client=None) -> None:
        self._client = client  # 测试可注入替身；生产走懒加载的 get_redis

    def _redis(self):
        if self._client is None:
            from backend.database import get_redis  # 懒加载：import 时不碰 Redis
            self._client = get_redis()
        return self._client

    def get_source(self, key: str) -> Optional[SourceHealthRecord]:
        try:
            raw = self._redis().get(REDIS_KEY_PREFIX + key)
        except Exception:
            logger.warning("上游健康状态读 Redis 失败，视为无记录", exc_info=True)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        return _record_from_json(key, raw)

    def set_source(self, record: SourceHealthRecord) -> None:
        try:
            self._redis().set(REDIS_KEY_PREFIX + record.source_key,
                              _record_to_json(record))
        except Exception:
            logger.warning("上游健康状态写 Redis 失败，本轮结论未持久化", exc_info=True)

    def get_line(self, key: str) -> Optional[LineHealthRecord]:
        # 线路记录本阶段无生产调用方；接口先行，持久化形态与源记录同口径。
        try:
            raw = self._redis().get(REDIS_KEY_PREFIX + "line:" + key)
        except Exception:
            logger.warning("线路健康状态读 Redis 失败，视为无记录", exc_info=True)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return LineHealthRecord(
            line_key=key,
            availability_status=data.get("availability_status", LINE_AVAILABLE),
            last_checked_at=data.get("last_checked_at"),
        )

    def set_line(self, record: LineHealthRecord) -> None:
        try:
            self._redis().set(
                REDIS_KEY_PREFIX + "line:" + record.line_key,
                json.dumps({
                    "availability_status": record.availability_status,
                    "last_checked_at": record.last_checked_at,
                }))
        except Exception:
            logger.warning("线路健康状态写 Redis 失败，本轮结论未持久化", exc_info=True)


# ---------------------------------------------------------------------------
# 源握手探活
# ---------------------------------------------------------------------------

def validate_base_url(base_url: str) -> str:
    """校验并归一化 base_url。非法时抛 ValueError（调用方按 unknown 计）。

    只许 http/https；拒绝带 userinfo 的 URL（凭据绝不进 URL）；
    拒绝带 query/fragment 的 URL；host 为空拒绝。
    """
    parsed = urlparse((base_url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"上游地址协议非法: {parsed.scheme!r}")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ValueError("上游地址不得带 userinfo（凭据只走请求头）")
    if not parsed.hostname:
        raise ValueError("上游地址缺少 host")
    if parsed.query or parsed.fragment:
        raise ValueError("上游地址不得带 query/fragment")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _default_http_get(url: str, headers: dict, timeout: float):
    """默认 HTTP 层（urllib，与 node_health 同款无依赖做法）。

    返回 (status, json_body_or_None)。超时/断连返回 (None, None)，
    由 classify_handshake 定为 unreachable；HTTPError 带状态码。
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "Aetrix/1.0")
    req = urllib.request.Request(url, headers=headers, method="GET")
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


def _auth_headers(source: UpstreamSource) -> dict:
    headers = {"Accept": "application/json"}
    if source.api_key:
        # Emby/Jellyfin 系标准头；Token 只出现在这里，绝不进 URL 与日志。
        headers["X-Emby-Authorization"] = (
            'MediaBrowser Client="Aetrix", Device="upstream-probe", '
            'DeviceId="upstream-probe", Version="1.0", '
            f'Token="{source.api_key}"'
        )
    return headers


def probe_source(
    source: UpstreamSource,
    store: HealthStore,
    *,
    http_get: Optional[Callable] = None,
    now: Optional[float] = None,
    threshold: Optional[int] = None,
    interval_sec: Optional[float] = None,
    backoff_max_sec: Optional[float] = None,
    timeout_sec: Optional[float] = None,
) -> dict:
    """探一个源：握手 → 定性 → 熔断喂数 → 状态机 → 持久化。

    返回 dict（key/outcome/state_before/state_after/skipped），不抛异常：
    任何内部异常都按 unknown 计（不覆盖已知状态），并记 warning。
    skipped 的几种情形一律不发请求、不写状态：源被禁用、熔断未放行、
    未到探活时刻、总开关关。
    """
    now = time.time() if now is None else now
    threshold = failure_threshold() if threshold is None else threshold
    interval_sec = probe_interval_sec() if interval_sec is None else interval_sec
    backoff_max_sec = probe_backoff_max_sec() if backoff_max_sec is None else backoff_max_sec
    timeout_sec = probe_timeout_sec() if timeout_sec is None else timeout_sec
    http_get = _default_http_get if http_get is None else http_get

    def _skipped(reason: str, record) -> dict:
        return {
            "key": source.key, "outcome": "skipped", "skipped_reason": reason,
            "state_before": record.availability_state if record else None,
            "state_after": record.availability_state if record else None,
        }

    if not probe_enabled():
        return _skipped("disabled", None)
    if not source.enabled:
        return _skipped("source_disabled", store.get_source(source.key))
    record = store.get_source(source.key) or SourceHealthRecord(source_key=source.key)
    state_before = record.availability_state
    if record.next_probe_at and record.next_probe_at > now:
        return _skipped("not_due", record)
    breaker_key = f"upstream:{source.key}"
    try:
        if not provider_breaker.allow(breaker_key):
            return _skipped("breaker_open", record)
    except Exception:
        logger.warning("上游熔断 allow() 异常，按放行计", exc_info=True)

    try:
        base = validate_base_url(source.base_url)
    except ValueError:
        # 配置错误是“无法判断”，不是“源死了”：按 unknown，不覆盖已知。
        apply_source_result(record, ProbeOutcome(OUTCOME_UNKNOWN), threshold=threshold, now=now)
        next_probe_due(record, interval_sec=interval_sec, backoff_max_sec=backoff_max_sec,
                       threshold=threshold, now=now)
        store.set_source(record)
        return {"key": source.key, "outcome": OUTCOME_UNKNOWN,
                "skipped_reason": "invalid_base_url",
                "state_before": state_before, "state_after": record.availability_state}

    try:
        status, body = http_get(f"{base}/System/Info/Public", {}, timeout_sec)
        outcome = classify_handshake(status, body)
        if outcome.kind == OUTCOME_SUCCESS and source.api_key:
            # 公开口只证明“服务器活着”；有凭据时再验一次需鉴权的口，
            # 否则“凭据已失效的源”会被误判为活着。
            status2, body2 = http_get(f"{base}/System/Info",
                                      _auth_headers(source), timeout_sec)
            if status2 == 200 and isinstance(body2, dict):
                outcome = ProbeOutcome(OUTCOME_SUCCESS, None, 200)
            elif status2 is None:
                outcome = ProbeOutcome(OUTCOME_FAILURE, "unreachable", None)
            elif status2 in (401, 403):
                outcome = ProbeOutcome(OUTCOME_FAILURE, "unauthorized", status2)
            elif status2 == 429:
                outcome = ProbeOutcome(OUTCOME_FAILURE, "rate_limited", status2)
            else:
                outcome = ProbeOutcome(OUTCOME_FAILURE, "server_error", status2)
    except Exception:
        logger.warning("上游 %s 握手异常，按 unknown 计", source.key, exc_info=True)
        outcome = ProbeOutcome(OUTCOME_UNKNOWN)

    try:
        if outcome.kind == OUTCOME_SUCCESS:
            provider_breaker.record_success(breaker_key)
        elif outcome.kind == OUTCOME_FAILURE:
            provider_breaker.record_failure(breaker_key, status=outcome.http_status,
                                            error=outcome.failure_kind or "")
        # unknown 不喂熔断：没定性的结论不该污染熔断计数。
    except Exception:
        logger.warning("上游熔断喂数异常，已忽略", exc_info=True)

    apply_source_result(record, outcome, threshold=threshold, now=now)
    next_probe_due(record, interval_sec=interval_sec, backoff_max_sec=backoff_max_sec,
                   threshold=threshold, now=now)
    store.set_source(record)
    return {"key": source.key, "outcome": outcome.kind,
            "failure_kind": outcome.failure_kind,
            "state_before": state_before, "state_after": record.availability_state}


def probe_sources_once(
    sources: list,
    store: HealthStore,
    *,
    http_get: Optional[Callable] = None,
    now: Optional[float] = None,
    **kwargs,
) -> list:
    """工人单步：逐源探一次（每源最多一次请求，无内层重试）。

    总开关关时直接返回空列表，不碰任何源、不写任何状态。
    """
    if not probe_enabled():
        return []
    now = time.time() if now is None else now
    return [probe_source(src, store, http_get=http_get, now=now, **kwargs)
            for src in sources]


def start_probe_daemon(sources_fn: Callable[[], list], store: HealthStore,
                       **kwargs) -> threading.Thread:
    """启动探活守护线程（必须显式调用；import 时绝不自动启动）。

    sources_fn 每轮重新取源列表（25-1 落地后由此接线）；总开关关时
    线程空转等待，不发请求。返回线程对象，调用方持有它以便观测。
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                if probe_enabled():
                    probe_sources_once(sources_fn(), store, **kwargs)
            except Exception:
                logger.warning("上游探活轮次异常，已跳过本轮", exc_info=True)
            stop.wait(probe_interval_sec())

    thread = threading.Thread(target=_loop, name="upstream-health-probe", daemon=True)
    thread.start()
    return thread


__all__ = [
    "STATE_ACTIVE", "STATE_UNAVAILABLE", "LINE_AVAILABLE", "LINE_UNAVAILABLE",
    "OUTCOME_SUCCESS", "OUTCOME_FAILURE", "OUTCOME_UNKNOWN",
    "UpstreamSource", "ProbeOutcome", "SourceHealthRecord", "LineHealthRecord",
    "HealthStore", "InMemoryHealthStore", "RedisHealthStore",
    "is_source_available", "is_line_available",
    "apply_source_result", "apply_line_result", "next_probe_due",
    "classify_handshake", "classify_line_probe",
    "validate_base_url", "probe_enabled", "probe_interval_sec",
    "failure_threshold", "probe_timeout_sec", "probe_backoff_max_sec",
    "probe_source", "probe_sources_once", "start_probe_daemon",
]

"""多源熔断注册表：按 provider 独立的断路器（进程内运行时状态）

## 为什么需要（2026-10-24，FMBY 学习批一 #8）

此前每个源的失败处理各管各的：TMDB 有逐 key 冷却 + 自适应限速、豆瓣有
自己的封禁退避、其余源（AniList / TVmaze / OMDb / TheTVDB / Bangumi）
**没有任何连续失败保护**——源站抽风时，每条待补条目都会实打实地撞一次，
既烧请求又拖慢整轮采集。FMBY 的口径是「源可用性熔断」：连续失败到阈值
就整源停打一阵，时间到后放一个探测请求试水，成功自动恢复。

这里做一套全源共用的（横切只许一套）：

- **连续失败计数**：达到 ``PROVIDER_BREAKER_THRESHOLD``（默认 3）开断；
- **4xx 风控类立即熔断**：HTTP 403 / 429 不等凑满次数，一次就开断
  （FMBY 实测豆瓣搜索路径单次 403 即冷却约 60 秒半开，同此口径）；
- **半开探测**：冷却到期后只放行一个请求试水，成功 → 关断恢复，
  失败 → 重新开断，不放整批流量去撞；名额被占超过 ``_PROBE_STALE_SEC``
  还没记账视为泄漏（调用方异常没人接），下一个调用方可回收重发，
  不让一次泄漏把源永久堵死；
- **成功清零**：任何一次成功（含「源活着、只是没搜到」）都清零连续失败。

## 与既有机制的分工（不替代、不重复造）

- TMDB 逐 key 冷却 / 豆瓣 ``douban.py`` 封禁退避**保留**：它们管的是
  「哪把 key / 哪个 IP 被针对」，粒度比 provider 更细；本注册表管的是
  「这个源整体还活不活」，是更粗一层的总闸。豆瓣的封禁会同步记进这里，
  让观测面（管理端多源视图）看到的是同一份结论。
- 状态只存进程内（与 KeyPool 冷却同一口径）：写进库会留下「重启后还记
  着源坏过」的陈旧结论；新进程重新试一遍才是对的。

## 喂数口径（谁在请求层谁喂）

计数必须落在真正发 HTTP 的那一层，按**请求**记而不是按条目记：

- TMDB：``TmdbClient._get`` 喂（一次搜索背后是多次请求，按请求记才准）；
- 豆瓣 / Bangumi：``altmeta`` 的 HTTP 层喂（它的异常被吞成 None，
  引擎层分不清「没搜到」和「源坏了」）；
- 其余源：引擎 ``_fetch_one`` 按 ``spec.search`` 的成败喂；
- 豆瓣主客户端（``douban.py``）在触发 / 解除封禁时同步喂一笔。

配置（全部 env，动态读，测试可逐例改）：

===============================  =====================================
``PROVIDER_BREAKER_ENABLED``      ``0`` 关闭整套熔断（默认开启）
``PROVIDER_BREAKER_THRESHOLD``    连续失败几次开断，默认 3
``PROVIDER_BREAKER_COOLDOWN_SEC`` 开断后冷却秒数，默认 60
``PROVIDER_BREAKER_FAST_SEC``     403/429 立即熔断的冷却秒数，默认 60
===============================  =====================================
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

#: 命中这些 HTTP 状态 = 源在明确拒绝（封禁 / 限流）：不等凑满阈值，立即熔断
FAST_TRIP_STATUSES = (403, 429)


def _cfg_enabled() -> bool:
    return (os.getenv("PROVIDER_BREAKER_ENABLED", "1") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _cfg_threshold() -> int:
    return env_int("PROVIDER_BREAKER_THRESHOLD", 3, 1, 100)


def _cfg_cooldown() -> float:
    # 下限 0：冷却 0 = 开断后立即半开探测（合法配置，测试也靠它免真等）
    return env_float("PROVIDER_BREAKER_COOLDOWN_SEC", 60.0, 0.0, 86400.0)


def _cfg_fast_cooldown() -> float:
    return env_float("PROVIDER_BREAKER_FAST_SEC", 60.0, 0.0, 86400.0)


#: 半开探测名额的最长占用：探测就是一发 HTTP（超时至多几十秒），超过
#: 这个时长还没记账，多半是调用方在领名额后抛了没人接的异常——名额
#: 泄漏。到期后允许下一个调用方把名额「偷」走自愈，免得泄漏一次就把
#: 这个源永久堵死（协调员审查补，2026-10-24）。
_PROBE_STALE_SEC = 120.0


@dataclass
class _ProviderState:
    failures: int = 0                    # 连续失败次数（成功清零）
    open_until: float = 0.0              # monotonic 时刻：开断到何时（0 = 未开断）
    probe_in_flight: bool = False        # 半开期已有探测在飞（只放一个）
    probe_since: float = 0.0             # 探测名额领走的 monotonic 时刻（泄漏自愈用）
    trips: int = 0                       # 累计开断次数（观测用）
    last_error: str = ""


_states: dict[str, _ProviderState] = {}
_lock = threading.Lock()


def _state_for(provider: str) -> _ProviderState:
    state = _states.get(provider)
    if state is None:
        state = _ProviderState()
        _states[provider] = state
    return state


def allow(provider: str) -> bool:
    """这个源现在还能不能打。

    关断（closed）→ True；开断未到期 → False；开断到期 → 半开，
    第一个调用方拿到探测名额（True），探测出结果前其余一律 False。
    """
    if not _cfg_enabled():
        return True
    now = time.monotonic()
    with _lock:
        state = _states.get(provider)
        if state is None or state.open_until <= 0:
            return True
        if state.open_until > now:
            return False
        # 冷却到期：半开，只放一个探测
        if state.probe_in_flight:
            if now - state.probe_since <= _PROBE_STALE_SEC:
                return False
            # 名额被占超时 = 上一个探测多半死在半路（异常没人记账）：
            # 偷走名额自愈，别让一次泄漏把这个源永久堵死
            logger.warning(
                "[provider-breaker] %s 的半开探测名额超时未归还，已回收重发",
                provider)
            state.probe_since = now
            return True
        state.probe_in_flight = True
        state.probe_since = now
        return True


def release_probe(provider: str) -> None:
    """归还半开探针名额：领了名额但请求最终没发出去（预检失败等）。

    不归还的后果是名额被永久占住、这个源再也等不到下一次探测——
    所有「allow 通过但没真正发请求」的出口都必须走这里。
    """
    with _lock:
        state = _states.get(provider)
        if state is not None:
            state.probe_in_flight = False
            state.probe_since = 0.0


def record_success(provider: str) -> None:
    """一次成功（源活着——含「搜到了」与「合法地没搜到」）：清零并关断。"""
    with _lock:
        state = _states.get(provider)
        if state is None:
            return
        was_open = state.open_until > 0 or state.probe_in_flight
        state.failures = 0
        state.open_until = 0.0
        state.probe_in_flight = False
        state.probe_since = 0.0
        state.last_error = ""
    if was_open:
        logger.info("[provider-breaker] %s 已恢复（探测成功，熔断解除）", provider)


def record_failure(provider: str, *, status: int | None = None, error: str = "") -> None:
    """一次失败：403/429 立即开断；其余累计到阈值开断；半开探测失败直接再开断。

    总开关关着时只记账不开断（与 env.example 的承诺一致：关熔断是紧急
    止血手段，观测面仍要能看见谁在坏）。
    """
    enabled = _cfg_enabled()
    fast = status in FAST_TRIP_STATUSES
    reason = (error or (f"HTTP {status}" if status else "") or "请求失败")[:200]
    trip_seconds: float | None = None
    with _lock:
        state = _state_for(provider)
        state.failures += 1
        state.last_error = reason
        if not enabled:
            return
        if state.probe_in_flight:
            # 半开探测失败：不用再凑次数，直接重新开断
            state.probe_in_flight = False
            state.probe_since = 0.0
            trip_seconds = _cfg_fast_cooldown() if fast else _cfg_cooldown()
        elif fast:
            trip_seconds = _cfg_fast_cooldown()
        elif state.failures >= _cfg_threshold():
            trip_seconds = _cfg_cooldown()
        if trip_seconds is not None:
            state.open_until = time.monotonic() + trip_seconds
            state.trips += 1
    if trip_seconds is not None:
        logger.warning(
            "[provider-breaker] %s 熔断：冷却 %.0f 秒后半开探测"
            "（连续失败 %d 次，最近错误：%s）",
            provider, trip_seconds, state.failures, reason)


def status() -> dict[str, dict]:
    """逐源熔断状态（管理端多源视图 / 排查用）。没喂过数的源不出现。"""
    now = time.monotonic()
    with _lock:
        rows = {}
        for provider, state in _states.items():
            if state.open_until > now:
                phase = "open"
                remaining = int(round(state.open_until - now))
            elif state.open_until > 0:
                phase = "half_open"
                remaining = 0
            else:
                phase = "closed"
                remaining = 0
            rows[provider] = {
                "state": phase,
                "failures": state.failures,
                "open_remaining_sec": remaining,
                "trips": state.trips,
                "last_error": state.last_error,
            }
        return rows


def reset_all() -> None:
    """测试用：清掉全部熔断状态。"""
    with _lock:
        _states.clear()


__all__ = [
    "FAST_TRIP_STATUSES", "allow", "record_failure", "record_success",
    "release_probe", "reset_all", "status",
]

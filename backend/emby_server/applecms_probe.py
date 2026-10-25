# -*- coding: utf-8 -*-
"""苹果 CMS 轻探活（FMBY 学习 #25 下一批唯一施工项）。

定位（先说清楚边界）：

- 本模块只做「轻探活 + 写现有健康存储」，不做同步、不做聚合、
  不做播放、不写任何映射行、不内置任何站规则。
- 探活形态是 CMS 匿名 provide 接口，不是 Emby/Jellyfin 的登录握手：
  ``upstream_health.probe_source`` 的握手（``/System/Info/Public``）
  不适用 CMS，故独立成模块；状态机与存储口径则与 Emby 侧共用
  （同一份 ``HealthStore``、同一个 ``apply_source_result``、源键
  同为 ``str(source.id)``），健康落库同步守护因此自动把 CMS 结论
  翻译进 PG（该同步另有自己的开关）。
- 只取首页：``ac=list&pg=1`` 一次请求，不翻页、不拉全量；
  抓取走 #1263 的 ``applecms_fetcher``（不改它），首而即止。
- 失败定性只写两类：``probe_failed``（连不上 / 超时 / 非 1 code /
  其它 HTTP 失败）与 ``auth_error``（401 / 403）。成功复位、
  Unknown 只推 ``last_checked`` 不覆盖已知状态（沿用 #1271 健康表口径）。
- 总开关 ``APPLECMS_PROBE_ENABLED`` 默认关：关时不发任何请求、
  不读写任何状态，全站行为逐字不变；import 时绝不自动启动线程，
  ``start_applecms_probe_daemon`` 必须被显式调用才跑。
- 日志与凭据纪律：日志只含源键与结论种类，绝不含地址、凭据、
  附加头原文；CMS 若需密钥走源上配的附加头，不走密封凭据。
- 验证口径（本批）：全部 mock（注入 ``http_get`` 桩替身），
  真实闭环待用户给 1 个试点 CMS 端点后才做——本模块不编造端点。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import upstream_health as uh

logger = logging.getLogger(__name__)

#: CMS 探活失败定性（仅两类，不另起枚举）。
FAILURE_PROBE_FAILED = "probe_failed"
FAILURE_AUTH_ERROR = "auth_error"


def probe_enabled() -> bool:
    """苹果 CMS 探活总开关，默认关。关时工人不发请求、不写状态。"""
    return (str(os.getenv("APPLECMS_PROBE_ENABLED", "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off"})


@dataclass
class AppleCMSSource:
    """探活源最小约定（生产由 worker 适配 PG 行，测试按此形状构造）。

    ``key`` 与 Emby 侧同口径：PG 源 id 的字符串形态。
    ``headers`` 是源上配的 UA / Referer / 附加头（见 ``source_headers``），
    凭据绝不走这里。
    """

    key: str
    base_url: str
    enabled: bool = True
    headers: dict = field(default_factory=dict)


def source_headers(source) -> dict:
    """发往 CMS 的请求头：源上配的 UA / Referer / 附加头（与播放分支同口径）。

    附加头须是「键值均为字符串」的对象，脏数据按空处理，不炸探活；
    凭据绝不走这里——凭据只走密封通道或源配置的附加头本身。
    """
    headers: dict = {"Accept": "*/*"}
    if getattr(source, "user_agent", None):
        headers["User-Agent"] = source.user_agent
    if getattr(source, "referer", None):
        headers["Referer"] = source.referer
    try:
        extra = json.loads(getattr(source, "extra_headers_json", None) or "{}")
    except (TypeError, ValueError):
        extra = {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and isinstance(value, str):
                headers[key] = value
    return headers


def _classify_fetch_error(exc: acf.AppleCMSError) -> uh.ProbeOutcome:
    """把抓取失败映射为探活结论（失败定性只写两类）。"""
    if exc.kind == "unauthorized":
        return uh.ProbeOutcome(uh.OUTCOME_FAILURE, FAILURE_AUTH_ERROR,
                               exc.http_status)
    return uh.ProbeOutcome(uh.OUTCOME_FAILURE, FAILURE_PROBE_FAILED,
                           exc.http_status)


def probe_applecms_source(
    source: AppleCMSSource,
    store: uh.HealthStore,
    *,
    http_get: Optional[Callable] = None,
    now: Optional[float] = None,
    threshold: Optional[int] = None,
    interval_sec: Optional[float] = None,
    backoff_max_sec: Optional[float] = None,
) -> dict:
    """探一个苹果 CMS 源：首页轻探 → 定性 → 状态机 → 持久化。

    返回 dict（key/outcome/failure_kind/state_before/state_after/
    skipped_reason），不抛异常：任何内部异常都按 unknown 计
    （不覆盖已知状态），并记 warning。skipped 的情形一律不发请求、
    不写状态：总开关关、源被禁用、未到探活时刻。
    周期 / 阈值 / 退避复用 Emby 探活的起步值（60 秒 / 3 次 /
    上限 600 秒），是“我方起步值”，改数不用改代码。
    """
    now = time.time() if now is None else now
    threshold = uh.failure_threshold() if threshold is None else threshold
    interval_sec = uh.probe_interval_sec() if interval_sec is None else interval_sec
    backoff_max_sec = (uh.probe_backoff_max_sec()
                       if backoff_max_sec is None else backoff_max_sec)

    def _skipped(reason: str, record) -> dict:
        return {
            "key": source.key, "outcome": "skipped",
            "skipped_reason": reason,
            "state_before": record.availability_state if record else None,
            "state_after": record.availability_state if record else None,
        }

    if not probe_enabled():
        return _skipped("disabled", None)
    if not source.enabled:
        return _skipped("source_disabled", store.get_source(source.key))
    record = (store.get_source(source.key)
              or uh.SourceHealthRecord(source_key=source.key))
    state_before = record.availability_state
    if record.next_probe_at and record.next_probe_at > now:
        return _skipped("not_due", record)

    try:
        base = uh.validate_base_url(source.base_url)
    except ValueError:
        # 配置错误是“无法判断”，不是“源死了”：按 unknown，不覆盖已知。
        uh.apply_source_result(record, uh.ProbeOutcome(uh.OUTCOME_UNKNOWN),
                               threshold=threshold, now=now)
        uh.next_probe_due(record, interval_sec=interval_sec,
                          backoff_max_sec=backoff_max_sec,
                          threshold=threshold, now=now)
        store.set_source(record)
        return {"key": source.key, "outcome": uh.OUTCOME_UNKNOWN,
                "skipped_reason": "invalid_base_url",
                "state_before": state_before,
                "state_after": record.availability_state}

    try:
        acf.fetch_page(base, ac="list", pg=1,
                       http_get=http_get, headers=dict(source.headers or {}))
        outcome = uh.ProbeOutcome(uh.OUTCOME_SUCCESS, None, 200)
    except acf.AppleCMSError as exc:
        outcome = _classify_fetch_error(exc)
    except ValueError:
        # 调用方传参错误（本模块 ac/pg 写死，正常走不到）：按 unknown。
        logger.warning("苹果 CMS 探活源 %s 参数异常，按 unknown 计",
                       source.key, exc_info=True)
        outcome = uh.ProbeOutcome(uh.OUTCOME_UNKNOWN)
    except Exception:
        logger.warning("苹果 CMS 探活源 %s 异常，按 unknown 计",
                       source.key, exc_info=True)
        outcome = uh.ProbeOutcome(uh.OUTCOME_UNKNOWN)

    uh.apply_source_result(record, outcome, threshold=threshold, now=now)
    uh.next_probe_due(record, interval_sec=interval_sec,
                      backoff_max_sec=backoff_max_sec,
                      threshold=threshold, now=now)
    store.set_source(record)
    return {"key": source.key, "outcome": outcome.kind,
            "failure_kind": outcome.failure_kind,
            "state_before": state_before, "state_after": record.availability_state}


def probe_applecms_sources_once(
    sources: list,
    store: uh.HealthStore,
    *,
    http_get: Optional[Callable] = None,
    now: Optional[float] = None,
    **kwargs,
) -> list:
    """工人单步：逐源轻探一次（每源最多一次请求，无内层重试）。

    总开关关时直接返回空列表，不碰任何源、不写任何状态。
    """
    if not probe_enabled():
        return []
    now = time.time() if now is None else now
    return [probe_applecms_source(src, store, http_get=http_get, now=now,
                                  **kwargs)
            for src in sources]


def start_applecms_probe_daemon(sources_fn: Callable[[], list],
                                store: uh.HealthStore,
                                **kwargs) -> threading.Thread:
    """启动苹果 CMS 轻探活守护线程（必须显式调用；import 时绝不自动启动）。

    sources_fn 每轮重新取源列表；总开关关时线程空转等待，不发请求。
    返回线程对象，调用方持有它以便观测。
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                if probe_enabled():
                    probe_applecms_sources_once(sources_fn(), store, **kwargs)
            except Exception:
                logger.warning("苹果 CMS 探活轮次异常，已跳过本轮", exc_info=True)
            stop.wait(uh.probe_interval_sec())

    thread = threading.Thread(target=_loop, name="applecms-probe", daemon=True)
    thread.start()
    return thread


__all__ = [
    "FAILURE_PROBE_FAILED", "FAILURE_AUTH_ERROR",
    "probe_enabled", "AppleCMSSource", "source_headers",
    "probe_applecms_source", "probe_applecms_sources_once",
    "start_applecms_probe_daemon",
]

# -*- coding: utf-8 -*-
"""外部目标连续失败计数与熔断暂停（PanSave 借鉴落地 B 线）。

来源：PanSave 深挖报告第五节「失败按来源分表留痕」（其迁移 108/117）：
它用 (来源, 归一化 URL) 主键的计数表管熔断——同一外部目标连续失败
达上限（其业务常量为 5 次）即暂停重试，成功一次即清零；设表目的是
防「永远拿不到的清单每轮反复触发长超时、长期占用探测预算」。

我方落地口径（定版，不扩范围）：
- 通用表 ``external_failure_counts``：(scope, normalized_key) 唯一，
  fail_count / last_fail_at / paused_until 三列；时间为 epoch 秒
  浮点，与 upstream_health / metasources.breaker 的时间口径一致。
- scope 按消费路径分账（与 PanSave 按机制拆表同构）：
  ``upstream_probe``（Emby 系源健康探测）、``upstream_fetch``
  （上游聚合抓取）、``cms_fetch``（苹果 CMS 抓取，含骑在抓取上
  的 CMS 轻探活）。本单只接这一片，其余路径（播放、转存等）不接。
- 归一化键口径（先定死、测试锁定）：scheme + host（小写）+ 非默认
  端口 + path；query 与 fragment 整体丢弃——带签名的临时参数全在
  query 里，去参后同一目标恒同键，不会各算各的导致熔断永不触发；
  userinfo 丢弃，凭据绝不进键；空 path 记 "/"，非根 path 去尾斜杠。
- 行为：连续失败达阈值（env ``EXTERNAL_FAILURE_PAUSE_THRESHOLD``，
  默认 5）→ ``paused_until = 最近失败时刻 + 暂停时长``（env
  ``EXTERNAL_FAILURE_PAUSE_SECONDS``，默认 1800 秒）。暂停期内
  消费方一律跳过（不发请求、不再喂数）；到期后下一个动作即半开
  试探：成功清零，失败因计数已在阈值之上立即重新暂停。
- 非阻断铁律：本模块全部公开函数吞掉自身异常（只记 warning），
  计数读写失败绝不阻断抓取/探测主流程；读失败一律按「未暂停」计。
- session 注入仅供测试：注入时本模块会在该 session 上 commit，
  生产调用方一律不注入（各自短开短关），勿在事务中途注入。
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import Column, Float, Integer, String, UniqueConstraint

from backend.database import Base, SessionLocal
from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

#: scope：健康探测（upstream_health.probe_source）。
SCOPE_UPSTREAM_PROBE = "upstream_probe"
#: scope：上游聚合抓取（upstream_aggregate_run 的三个取数入口）。
SCOPE_UPSTREAM_FETCH = "upstream_fetch"
#: scope：苹果 CMS 抓取（applecms_fetcher.fetch_page，含 CMS 探活）。
SCOPE_CMS_FETCH = "cms_fetch"

ALL_SCOPES = (SCOPE_UPSTREAM_PROBE, SCOPE_UPSTREAM_FETCH, SCOPE_CMS_FETCH)


class ExternalFailureCount(Base):
    """(scope, 归一化目标键) → 连续失败计数与暂停截止时刻。"""

    __tablename__ = "external_failure_counts"
    __table_args__ = (
        UniqueConstraint("scope", "normalized_key",
                         name="uq_external_failure_counts_scope_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(64), nullable=False, index=True)
    normalized_key = Column(String(512), nullable=False, index=True)
    fail_count = Column(Integer, nullable=False, default=0)
    #: 最近一次计入失败的 epoch 秒；成功不清此列（留作排障痕迹）。
    last_fail_at = Column(Float, nullable=True)
    #: 暂停截止 epoch 秒；NULL 或已过期 = 未暂停（到期即半开）。
    paused_until = Column(Float, nullable=True)


# ---------------------------------------------------------------------------
# 配置（全部 env，与 upstream_health 同口径）
# ---------------------------------------------------------------------------

def pause_threshold() -> int:
    """连续失败几次触发暂停。默认 5（PanSave 业务常量同值）。"""
    return env_int("EXTERNAL_FAILURE_PAUSE_THRESHOLD", 5, 1, 1000)


def pause_seconds() -> float:
    """触发暂停后停探多久（秒）。默认 1800（30 分钟）。"""
    return env_float("EXTERNAL_FAILURE_PAUSE_SECONDS", 1800.0, 1.0,
                     7 * 24 * 3600)


# ---------------------------------------------------------------------------
# 归一化键（口径定死，测试锁定；细节见模块头）
# ---------------------------------------------------------------------------

def normalize_target_key(url: str) -> str:
    """外部目标 URL → 归一化计数键。见模块头口径，永不抛异常。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        scheme = (parts.scheme or "").lower()
        try:
            port = parts.port
        except ValueError:
            port = None
        path = parts.path or ""
    except ValueError:
        host = ""
        scheme = ""
        port = None
        path = ""
    if not host:
        # 解析不出主机时退化为「去 query/fragment 的原文」，至少保证
        # 同一坏地址恒同键，不让它绕过计数。
        return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if (scheme == "http" and port == 80) or \
            (scheme == "https" and port == 443):
        port = None
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    if not path:
        path = "/"
    elif path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return f"{scheme}://{authority}{path}" if scheme else \
        f"{authority}{path}"


# ---------------------------------------------------------------------------
# 计数读写（全部吞异常；见模块头非阻断铁律）
# ---------------------------------------------------------------------------

def _epoch(now: Optional[float]) -> float:
    return time.time() if now is None else float(now)


def _get_row(db, scope: str, key: str) -> Optional[ExternalFailureCount]:
    return (db.query(ExternalFailureCount)
            .filter(ExternalFailureCount.scope == scope,
                    ExternalFailureCount.normalized_key == key)
            .first())


def _with_session(session, op, default, *, retries: int = 0):
    """执行计数操作并 commit；任何异常吞掉、回滚、返回 default。

    自开 session 用完即关；注入 session 不关（归调用方），但会在
    其上 commit/rollback（见模块头注入约定）。retries 用于并发插入
    撞唯一约束时重试一次（输掉竞态的一方改走更新路径）。
    """
    own = session is None
    db = session if not own else SessionLocal()
    attempt = 0
    try:
        while True:
            try:
                result = op(db)
                db.commit()
                return result
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                if attempt < retries:
                    attempt += 1
                    continue
                logger.warning(
                    "外部失败计数操作失败，已忽略（不阻断主流程）",
                    exc_info=True)
                return default
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass


def is_paused(scope: str, key: str, *, now: Optional[float] = None,
              session=None) -> bool:
    """该 (scope, 目标) 是否处于暂停期。读失败按未暂停计。"""
    if not scope or not key:
        return False
    ts = _epoch(now)

    def _op(db):
        row = _get_row(db, scope, key)
        return bool(row is not None and row.paused_until is not None
                    and row.paused_until > ts)

    return bool(_with_session(session, _op, False))


def record_failure(scope: str, key: str, *, now: Optional[float] = None,
                   session=None) -> None:
    """记一次连续失败；达阈值置 paused_until（半开再失败亦走此路）。"""
    if not scope or not key:
        return
    ts = _epoch(now)
    threshold = pause_threshold()
    span = pause_seconds()

    def _op(db):
        row = _get_row(db, scope, key)
        if row is None:
            row = ExternalFailureCount(scope=scope, normalized_key=key,
                                       fail_count=0)
            db.add(row)
            db.flush()
        row.fail_count = int(row.fail_count or 0) + 1
        row.last_fail_at = ts
        if row.fail_count >= threshold:
            row.paused_until = ts + span

    _with_session(session, _op, None, retries=1)


def record_success(scope: str, key: str, *, now: Optional[float] = None,
                   session=None) -> None:
    """记一次成功：连续失败清零并解除暂停（已干净时不写库）。

    ``now`` 仅为与 record_failure 调用口径对称而接受，清零与
    时间无关、不使用它。
    """
    if not scope or not key:
        return

    def _op(db):
        row = _get_row(db, scope, key)
        if row is not None and (row.fail_count or row.paused_until):
            row.fail_count = 0
            row.paused_until = None

    _with_session(session, _op, None)


def pause_states_for_key(key: str, *, now: Optional[float] = None,
                         session=None) -> dict:
    """某目标在各 scope 的计数/暂停态（管理端源列表并入展示用）。

    返回 {"paused": bool, "paused_until": float|None,
    "scopes": {scope: {"fail_count": int, "paused": bool,
    "paused_until": float|None, "last_fail_at": float|None}}}。
    读失败返回未暂停空态，绝不抛异常。
    """
    empty = {"paused": False, "paused_until": None, "scopes": {}}
    if not key:
        return empty
    ts = _epoch(now)

    def _op(db):
        rows = (db.query(ExternalFailureCount)
                .filter(ExternalFailureCount.normalized_key == key)
                .all())
        scopes = {}
        paused_until = None
        for row in rows:
            row_paused = (row.paused_until is not None
                          and row.paused_until > ts)
            scopes[row.scope] = {
                "fail_count": int(row.fail_count or 0),
                "paused": bool(row_paused),
                "paused_until": row.paused_until,
                "last_fail_at": row.last_fail_at,
            }
            if row_paused:
                paused_until = max(paused_until or 0.0, row.paused_until)
        return {"paused": paused_until is not None,
                "paused_until": paused_until, "scopes": scopes}

    return _with_session(session, _op, empty) or empty


__all__ = [
    "ExternalFailureCount",
    "SCOPE_UPSTREAM_PROBE", "SCOPE_UPSTREAM_FETCH", "SCOPE_CMS_FETCH",
    "ALL_SCOPES",
    "pause_threshold", "pause_seconds", "normalize_target_key",
    "is_paused", "record_failure", "record_success",
    "pause_states_for_key",
]

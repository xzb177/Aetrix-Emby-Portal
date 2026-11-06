# -*- coding: utf-8 -*-
"""转存/聚合自动决策留痕（PanSave 借鉴落地 A 线 PR-1）。

学的是 PanSave 的 monitor_rounds 轮次快照：一轮自动决策开轮时
把「策略指纹 + 候选快照 + 裁决与理由 + 执行回链」落一行，事后
能回答「这一轮凭什么这么判」。我方此前只有任务行状态：门禁
verdict 算完即弃、聚合进度就地覆写，决策依据不落库，排障只能
翻日志考古。本模块给决策侧补上与播放侧 PlayStartEvent 同等待
遇的落库复盘。

写入点只有两处（不许第三处另建统计，照 play_events 的单落点
纪律）：
- transfer_variant.ensure_user_copy_gate：门禁 verdict 算完后
  落一行（scope=transfer_gate，resource_key=item:<id>，
  execution_ref=转存任务行 id）；
- upstream_aggregate_jobs._enqueue_locked：真开新轮
  （created=True）时落一行（scope=upstream_aggregate，
  resource_key=source:<id>，execution_ref=聚合 task_id）；
  复用进行中任务不是新轮次，不落。

**铁律：留痕不许拖垮主流程。** note_decision 用独立会话写，
任何失败（DB 挂、序列化炸、行超长）一律静默丢弃并计数返回
False，绝不向上抛——照 play_start_events「写失败静默丢弃」
的先例。门禁与聚合的返回值、事务、状态推进一概不依赖留痕
成败。

policy_hash：参与决策的配置键值规范化 JSON（键排序、紧凑
分隔符）的 sha256。策略内容变了指纹就变，用来判断一行决策
是在哪一版配置下做的；不追求与 PanSave 同算法（其算法在
混淆二进制内不可见，见深挖报告第一节未知项）。

保留期：行随轮次增长，maintenance.janitor_tick 周期调
purge_expired_rounds 清理过期行。保留天数 SystemConfig
``transfer_decision_rounds_retention_days`` 可配，默认 30，
钳 [1, 365]。

管理端只读面：list_recent_rounds 供
GET /api/admin/tasks/transfer-decision-rounds（admin_tasks）
与任务总览页「转存决策留痕」区。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from backend.database import SessionLocal
from backend.emby_server import models as em

logger = logging.getLogger(__name__)

#: scope 取值：转存门禁 / 上游聚合开轮
SCOPE_TRANSFER_GATE = "transfer_gate"
SCOPE_UPSTREAM_AGGREGATE = "upstream_aggregate"

#: 聚合开轮的裁决常量（门禁侧直接用 verdict 原文）
DECISION_ROUND_OPENED = "round_opened"

#: 保留期配置键与默认值（天）
RETENTION_KEY = "transfer_decision_rounds_retention_days"
RETENTION_DEFAULT_DAYS = 30

_STATS = {"noted": 0, "written": 0, "write_errors": 0}
_STATS_LOCK = threading.Lock()


def decision_stats() -> dict:
    """留痕流水计数（测试/观测用）：noted 尝试数、written
    落库数、write_errors 静默丢弃数。"""
    with _STATS_LOCK:
        return dict(_STATS)


def _bump(name: str) -> None:
    with _STATS_LOCK:
        _STATS[name] += 1


def policy_hash_for(policy) -> str:
    """参与决策的配置键值 → 策略指纹。

    规范化口径钉死：键排序、紧凑分隔符、不可序列化值 str 化，
    再取 sha256 十六进制。同值同哈希（与键序无关）、值变哈希变。
    """
    normalized = json.dumps(
        dict(policy or {}), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _trim(value, limit: int):
    if value is None:
        return None
    text = str(value)
    return text[:limit] if len(text) > limit else text


def note_decision(*, scope: str, resource_key: str, decision: str,
                  reason: Optional[str] = None, policy=None,
                  candidates=None,
                  execution_ref: Optional[str] = None) -> bool:
    """落一行决策留痕。成功 True；任何失败静默丢弃返回 False，
    绝不抛（见模块 docstring 铁律）。

    独立会话：不借调用方的事务——调用方回滚不连带留痕，
    留痕失败也不污染调用方事务。
    """
    _bump("noted")
    try:
        candidates_json = None
        if candidates is not None:
            candidates_json = json.dumps(
                candidates, ensure_ascii=False, default=str)
        row = em.TransferDecisionRound(
            scope=_trim(scope, 40),
            resource_key=_trim(resource_key, 191),
            policy_hash=policy_hash_for(policy),
            candidates_json=candidates_json,
            decision=_trim(decision, 60),
            reason=_trim(reason, 120),
            execution_ref=_trim(execution_ref, 64),
        )
    except Exception:  # noqa: BLE001 — 组装失败同样静默丢弃
        _bump("write_errors")
        logger.warning("决策留痕组装失败 scope=%s", scope,
                       exc_info=True)
        return False
    db = None
    try:
        db = SessionLocal()
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001 — 写失败静默丢弃，不挡主流程
        _bump("write_errors")
        logger.warning("决策留痕写入失败 scope=%s resource=%s",
                       scope, resource_key, exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    _bump("written")
    return True



# ---------------------------------------------------------------------------
# 保留期与只读面
# ---------------------------------------------------------------------------

def _config_int(db, key: str) -> Optional[int]:
    """SystemConfig 整数热读：无行/非法/异常一律 None（回退默认）。

    与 transfer_variant._system_config_int 同口径；本模块不反向
    import transfer_variant（它要 import 本模块写留痕，防循环）。
    """
    if db is None:
        return None
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:  # noqa: BLE001
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def retention_days(db) -> int:
    """留痕保留天数：SystemConfig > 默认 30，钳 [1, 365]。"""
    configured = _config_int(db, RETENTION_KEY)
    if configured is not None:
        return max(1, min(365, configured))
    return RETENTION_DEFAULT_DAYS


def purge_expired_rounds(db, *, now: Optional[datetime] = None) -> int:
    """删超保留期的留痕行（本函数提交），返回删除行数。

    由 maintenance.janitor_tick 周期调用；失败由调用方兜底，
    本函数不吞异常（与 janitor 各分项同口径：分项抛、tick 记
    warning 后继续下一项）。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days(db))
    deleted = (db.query(em.TransferDecisionRound)
               .filter(em.TransferDecisionRound.created_at < cutoff)
               .delete(synchronize_session=False))
    db.commit()
    return int(deleted or 0)


def _row_view(row) -> dict:
    candidates = None
    if row.candidates_json:
        try:
            candidates = json.loads(row.candidates_json)
        except (TypeError, ValueError):
            candidates = None
    return {
        "id": row.id,
        "scope": row.scope,
        "resource_key": row.resource_key,
        "policy_hash": row.policy_hash,
        "candidates": candidates,
        "decision": row.decision,
        "reason": row.reason,
        "execution_ref": row.execution_ref,
        "created_at": (row.created_at.isoformat()
                       if row.created_at else None),
    }


def list_recent_rounds(db, *, limit: int = 50,
                       scope: Optional[str] = None) -> list:
    """最近决策轮次（管理端只读面）：id 倒序，limit 钳 [1, 200]，
    scope 给了就只看那一类。"""
    limit = max(1, min(200, int(limit or 50)))
    query = db.query(em.TransferDecisionRound)
    if scope:
        query = query.filter(em.TransferDecisionRound.scope == scope)
    rows = (query.order_by(em.TransferDecisionRound.id.desc())
            .limit(limit).all())
    return [_row_view(row) for row in rows]

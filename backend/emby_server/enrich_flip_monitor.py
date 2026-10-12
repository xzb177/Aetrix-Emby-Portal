"""enrich done→pending 批量翻转监控（v2.55.x）

背景（2026-10-11 生产事故）：17:52 发生 5723 条 done→pending 翻转，
触发源未知；旧容器在 17:54 部署时被重建，docker 日志丢失，无法追查。

本模块提供翻转告警的共享能力，被两处调用：
- ``scanner._maybe_alert_enrich_flip``：扫描结束时检查本轮翻转数；
- ``enrich_worker.retry_unmatched``：管理端手动「重试未匹配项」批量捞回时。

设计要点：
- 阈值可配置：``ENRICH_FLIP_ALERT_THRESHOLD``（默认 100），走 env_util 单一口径；
- 现场持久化：告警事件写 PG（``EnrichFlipEvent``），容器重建/日志轮转丢不掉；
- 失败隔离：监控本身绝不能把业务拖下水，写库失败只记日志。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

# 单次批量翻转超过该条数就告警（可配置）。100 的依据：正常扫描里
# size_changed 是零星的（个位数）；三位数以上一定是异常（mtime 风暴/
# 批量误判/未知触发源），值得留现场。
FLIP_ALERT_THRESHOLD = env_int("ENRICH_FLIP_ALERT_THRESHOLD", 100, 1, 1_000_000)


def should_alert(flip_count: int, threshold: Optional[int] = None) -> bool:
    """是否达到告警线。纯函数，方便测试。"""
    th = FLIP_ALERT_THRESHOLD if threshold is None else threshold
    return flip_count >= th


def summarize_flips(flips: list) -> dict:
    """把翻转明细聚合成可读摘要。flips 每项至少含 reason / item_type。"""
    reasons: dict[str, int] = {}
    item_types: dict[str, int] = {}
    sample_ids: list = []
    for f in flips:
        reasons[str(f.get("reason", "?"))] = reasons.get(str(f.get("reason", "?")), 0) + 1
        it = str(f.get("item_type", "?"))
        item_types[it] = item_types.get(it, 0) + 1
        if len(sample_ids) < 10:
            sample_ids.append(f.get("item_id"))
    return {"reasons": reasons, "item_types": item_types, "sample_ids": sample_ids}


def record_flip_event(
    db,
    *,
    flip_count: int,
    trigger: str,
    library_id: Optional[int] = None,
    scan_run_id: Optional[int] = None,
    details: Optional[dict] = None,
    threshold: Optional[int] = None,
) -> bool:
    """写一条翻转告警事件到 PG（持久化现场）。成功返回 True。

    写失败只记日志返回 False —— 监控不能影响业务。

    F-P1-2：用独立 Session 写告警，不碰调用方的 Session。
    原逻辑在调用方的 Session 上 commit/rollback：调用方 Session 里若有未提交的
    业务修改，告警成功会提前提交它们，告警失败会撤销它们。
    """
    th = FLIP_ALERT_THRESHOLD if threshold is None else threshold
    # F-P1-2：独立会话，调用方的 db 参数仅保留兼容性，不再使用
    from backend.database import SessionLocal
    _db = SessionLocal()
    try:
        _db.add(em.EnrichFlipEvent(
            library_id=library_id,
            scan_run_id=scan_run_id,
            trigger=trigger,
            flip_count=int(flip_count),
            threshold=int(th),
            details=json.dumps(details or {}, ensure_ascii=False)[:4000],
        ))
        _db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — 监控写库失败不能拖垮业务
        logger.warning("写入 enrich 翻转告警事件失败: %s", exc)
        try:
            _db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
    finally:
        try:
            _db.close()
        except Exception:  # noqa: BLE001
            pass


def alert_flip(
    db,
    *,
    flip_count: int,
    trigger: str,
    library_id: Optional[int] = None,
    scan_run_id: Optional[int] = None,
    flips: Optional[list] = None,
    summary: Optional[dict] = None,
    threshold: Optional[int] = None,
) -> bool:
    """翻转告警主入口：打 warning 日志 + 写 PG 事件。返回是否触发了告警。

    summary：调用方已聚合好的摘要（{"reasons", "item_types", "sample_ids"}），
    传入则跳过 summarize_flips。scanner 侧在 _scan_library_body 已聚合好，
    直接传进来，避免"共享能力"名实不符（两处各写一遍 warning+写库）。
    """
    th = FLIP_ALERT_THRESHOLD if threshold is None else threshold
    if not should_alert(flip_count, th):
        return False
    summary = summary if summary is not None else summarize_flips(flips or [])
    logger.warning(
        "enrich 批量翻转告警：%d 条 done→pending（阈值 %d）trigger=%s "
        "library_id=%s scan_run_id=%s reasons=%s item_types=%s sample_ids=%s",
        flip_count, th, trigger, library_id, scan_run_id,
        summary["reasons"], summary["item_types"], summary["sample_ids"],
    )
    record_flip_event(
        db, flip_count=flip_count, trigger=trigger,
        library_id=library_id, scan_run_id=scan_run_id,
        details=summary, threshold=th,
    )
    return True


def flip_count_last_hours(db, hours: int = 1,
                          exclude_triggers: tuple = ()) -> int:
    """最近 N 小时的翻转总数（/api/health 指标用）。异常返回 0。

    exclude_triggers：不计入的触发源。管理端手动「重试未匹配项」是已知操作，
    事件仍在 PG 留痕审计，但不应把 health 打成 warn，调用方按需排除。
    """
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        q = (
            db.query(em.EnrichFlipEvent.flip_count)
            .filter(em.EnrichFlipEvent.created_at >= cutoff)
        )
        if exclude_triggers:
            q = q.filter(~em.EnrichFlipEvent.trigger.in_(exclude_triggers))
        rows = q.all()
        return sum(int(r[0] or 0) for r in rows)
    except Exception as exc:  # noqa: BLE001
        logger.debug("统计 enrich 翻转数失败: %s", exc)
        return 0


def recent_flip_events(db, limit: int = 20) -> list[dict[str, Any]]:
    """最近的翻转告警事件（管理端/排查询用）。"""
    try:
        rows = (
            db.query(em.EnrichFlipEvent)
            .order_by(em.EnrichFlipEvent.id.desc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        out = []
        for r in rows:
            try:
                details = json.loads(r.details or "{}")
            except Exception:  # noqa: BLE001
                details = {}
            out.append({
                "id": r.id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "library_id": r.library_id,
                "scan_run_id": r.scan_run_id,
                "trigger": r.trigger,
                "flip_count": r.flip_count,
                "threshold": r.threshold,
                "details": details,
            })
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("查询 enrich 翻转事件失败: %s", exc)
        return []

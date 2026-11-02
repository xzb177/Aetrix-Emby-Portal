# -*- coding: utf-8 -*-
"""全员赠送能量：批次发放执行器（worker 侧）。

定位：把「给全体用户送能量」做成正式能力（2026-10-31 用户亲点）。管理端
端点（backend/api/admin_economy.py 的 /economy/gift-all/*）只负责校验参数、
落批次快照与审计，**绝不在请求里同步遍历全站发分**；真正的发放在这里按
批次分块执行：

- 人群在**发放时刻**按批次 scope 实时判定（建批后注销的账号、过期的公益
  资格都不会误发）；批次行的 target_count 只是建批时的预计人数快照，
  granted_count 才是实际发放数。
- 每人一笔 PointsLog（type='gift_all'，非自挣，见 backend/points.py 的
  来源分类表），ref_id='gift_all:<批次号>'，description=批次备注。
- 幂等锚点是 points_gift_grants 的 (batch_id, user_id) 唯一约束：明细行、
  余额自增、台账行在同一事务里提交；撞约束即已发过、跳过。批次中途失败
  标 failed 并留 last_error，worker 下一轮自动重新认领续跑，已发的不重发。
- 认领用条件 UPDATE（pending/failed，或 running 但 updated_at 超过
  _STALE_RUNNING_MINUTES 未更新＝上一执行者已死），双实例不会同跑一批。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.api.economy import _add_points

logger = logging.getLogger(__name__)

GIFT_ALL_TYPE = "gift_all"
GIFT_ALL_SCOPES = ("all", "welfare")
DEFAULT_DESCRIPTION = "全员赠送能量"

_CHUNK_SIZE = 200
_POLL_SECONDS = 15
_STALE_RUNNING_MINUTES = 10


# ==================== 人群口径（端点预览与执行器共用，全仓一套） ====================

def eligible_ids_query(db: Session, scope: str, now: datetime | None = None):
    """某 scope 下的目标用户 id 查询（未排序、未分页）。

    - all：现存全部 web_users 行。WebUser 无软删列，注销即行不存在，
      天然不在结果里；is_active 是禁用态、与注销不同口径，不排除。
    - welfare：is_welfare 且资格未过期（welfare_expires_at 为空＝永久，
      或晚于当前时间），与 welfare_expiry 的现行口径一致。
    """
    q = db.query(models.WebUser.id)
    if scope == "welfare":
        now = now or datetime.now()
        q = q.filter(
            models.WebUser.is_welfare == True,  # noqa: E712
            or_(
                models.WebUser.welfare_expires_at.is_(None),
                models.WebUser.welfare_expires_at > now,
            ),
        )
    return q


def count_eligible_users(db: Session, scope: str) -> int:
    return eligible_ids_query(db, scope).count()


# ==================== 批次执行 ====================

def _granted_count(db: Session, batch_id: int) -> int:
    return db.query(models.PointsGiftGrant).filter(
        models.PointsGiftGrant.batch_id == batch_id).count()


def _claim_batch(db: Session, batch_id: int, now: datetime) -> bool:
    """条件认领：只有 pending/failed 或已僵死的 running 批次能被认领。"""
    stale_before = now - timedelta(minutes=_STALE_RUNNING_MINUTES)
    updated = (
        db.query(models.PointsGiftBatch)
        .filter(
            models.PointsGiftBatch.id == batch_id,
            or_(
                models.PointsGiftBatch.status.in_(("pending", "failed")),
                and_(
                    models.PointsGiftBatch.status == "running",
                    models.PointsGiftBatch.updated_at < stale_before,
                ),
            ),
        )
        .update(
            {models.PointsGiftBatch.status: "running",
             models.PointsGiftBatch.updated_at: now},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1


def process_batch(db: Session, batch_id: int, chunk_size: int = _CHUNK_SIZE) -> dict:
    """执行一个批次（分块、逐人一事务）。返回执行摘要 dict。

    单个用户发放失败（非幂等冲突）即停批：批次标 failed、留 last_error，
    已落明细的人不回滚——续跑时靠明细跳过他们。
    """
    batch = db.query(models.PointsGiftBatch).filter(
        models.PointsGiftBatch.id == batch_id).first()
    if batch is None:
        return {"batch_id": batch_id, "claimed": False, "status": "missing"}
    if batch.status == "done":
        return {"batch_id": batch_id, "claimed": False, "status": "done",
                "granted_count": batch.granted_count}
    if not _claim_batch(db, batch_id, datetime.now()):
        return {"batch_id": batch_id, "claimed": False, "status": batch.status}

    batch = db.query(models.PointsGiftBatch).filter(
        models.PointsGiftBatch.id == batch_id).first()
    description = (batch.reason or "").strip() or DEFAULT_DESCRIPTION
    ref_id = f"gift_all:{batch.id}"
    last_id = 0
    while True:
        rows = (
            eligible_ids_query(db, batch.scope)
            .filter(models.WebUser.id > last_id)
            .order_by(models.WebUser.id)
            .limit(chunk_size)
            .all()
        )
        if not rows:
            break
        for (user_id,) in rows:
            last_id = user_id
            already = db.query(models.PointsGiftGrant).filter(
                models.PointsGiftGrant.batch_id == batch.id,
                models.PointsGiftGrant.user_id == user_id,
            ).first()
            if already is not None:
                continue
            user = db.query(models.WebUser).filter(
                models.WebUser.id == user_id).first()
            if user is None:
                continue  # 判定后又被注销：跳过，不算失败
            try:
                grant = models.PointsGiftGrant(batch_id=batch.id, user_id=user_id)
                db.add(grant)
                db.flush()
                _add_points(db, user, int(batch.amount), GIFT_ALL_TYPE,
                            description, ref_id)
                log = (
                    db.query(models.PointsLog)
                    .filter(models.PointsLog.user_id == user_id,
                            models.PointsLog.ref_id == ref_id)
                    .order_by(models.PointsLog.id.desc())
                    .first()
                )
                grant.points_log_id = log.id if log else None
                db.commit()
            except IntegrityError:
                # 另一执行者刚给同一人发过：唯一约束挡住双发，跳过即可
                db.rollback()
                continue
            except Exception as e:  # noqa: BLE001
                db.rollback()
                batch.status = "failed"
                batch.last_error = str(e)[:255]
                batch.granted_count = _granted_count(db, batch.id)
                batch.updated_at = datetime.now()
                db.commit()
                logger.error("全员赠送批次 %s 发放中断（已发 %s 人）: %s",
                             batch.id, batch.granted_count, e, exc_info=True)
                return {"batch_id": batch.id, "claimed": True,
                        "status": "failed", "granted_count": batch.granted_count}
        batch.granted_count = _granted_count(db, batch.id)
        batch.updated_at = datetime.now()
        db.commit()

    batch.status = "done"
    batch.finished_at = datetime.now()
    batch.granted_count = _granted_count(db, batch.id)
    batch.updated_at = datetime.now()
    db.commit()
    logger.info("全员赠送批次 %s 完成：每人 %s，共 %s 人（scope=%s）",
                batch.id, batch.amount, batch.granted_count, batch.scope)
    return {"batch_id": batch.id, "claimed": True, "status": "done",
            "granted_count": batch.granted_count}


def process_pending_batches(db: Session, chunk_size: int = _CHUNK_SIZE) -> list[dict]:
    """认领并执行全部待处理批次（pending/failed/僵死 running），按 id 序。"""
    stale_before = datetime.now() - timedelta(minutes=_STALE_RUNNING_MINUTES)
    batches = (
        db.query(models.PointsGiftBatch)
        .filter(or_(
            models.PointsGiftBatch.status.in_(("pending", "failed")),
            and_(
                models.PointsGiftBatch.status == "running",
                models.PointsGiftBatch.updated_at < stale_before,
            ),
        ))
        .order_by(models.PointsGiftBatch.id)
        .all()
    )
    return [process_batch(db, b.id, chunk_size) for b in batches]


# ==================== worker 轮询线程 ====================

_STARTED = False
_START_LOCK = threading.Lock()


def _poll_loop() -> None:
    while True:
        try:
            from backend.database import SessionLocal
            db = SessionLocal()
            try:
                process_pending_batches(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("全员赠送批次轮询失败（下一轮重试）")
        time.sleep(_POLL_SECONDS)


def start_gift_all_worker() -> bool:
    """启动全员赠送发放轮询（daemon 线程，同一进程只启动一次）。"""
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True
    t = threading.Thread(target=_poll_loop, daemon=True, name="gift-all-worker")
    t.start()
    return True

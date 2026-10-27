# -*- coding: utf-8 -*-
"""全自动追剧（opt-in，2026-10-27 首批第二单，#1395 暖缓存 +
#1359 下一集预测的延伸）。

用户在账号页主动订阅一部剧（默认不跟）；新集到货后自动为该
用户建转存任务，转到用户自己绑定的盘（aetrix-transfer 目录、
真 copy、按用户记副本——复用 transfer_variant.create_user_job，
不另起任务表与状态机）。

硬规矩（与播放门禁、转存失败总原则同一口径）：
- 绝不静默回落共享 SA 中继：无绑定时扫集直接跳过
  （reason=no_binding，不建注定失败的 pending 单、不污染
  24h 节流窗；绑定出现后下一轮扫集（默认 <=600 秒）即建单；
  状态口对无任务的新集同样标 no_binding 原因）；盘满时工人
  按 _SpaceInsufficient 把已建任务置 failed（转存失败总原则）；
  本模块不在任何失败路径上改走共享链。
- 提醒只用站内可见状态：本模块只提供订阅列表 + 每集转存
  状态（新接口/账号页状态），绝不对外发消息、不写站内消息。
- 115 与跨 provider 转存不在本单范围：115 源的集直接跳过
  （reason=unsupported_provider_115，不建任务、不猜盘）；只绑
  了另一 provider 的用户不走跨单（跨单是另一单的范围）。
- 开关 SERIES_FOLLOW_ENABLED 默认关：关时订阅接口 404、扫
  描函数直接返回 swept=0（不建任务）；转存总闸
  TRANSFER_VARIANT_ENABLED 关时 create_user_job 本身报
  master_off，同样不建任务、不回落。
- “新集”只认订阅之后到货：单集 date_added >= 订阅行
  created_at；date_added 为空的集不猜、跳过并计数。已 ready
  的集、已有任务行（24h 节流内）的集、非 Drive 源的集一律
  不重复建。扫描函数本身不抛错进调用方（工人循环内异常只
  记 warning，见 transfer_variant 守护循环的调用点）。
"""

import logging
import os
from datetime import datetime

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

TRIGGER = "series_follow"


def series_follow_enabled() -> bool:
    """追剧总开关，默认关。"""
    return os.getenv("SERIES_FOLLOW_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def follow_sweep_sec() -> int:
    """工人循环里追剧扫的间隔（秒），默认 600；非法值回落默认。"""
    try:
        val = int(os.getenv("SERIES_FOLLOW_SWEEP_SEC", "600"))
    except (TypeError, ValueError):
        return 600
    return max(60, val)


def subscribe(db, user_id: int, series_item_id: int) -> dict:
    """显式订阅：只认活着的 series 条目；已订阅/已取消都幂等处理。

    返回 {"subscribed": bool, "reason": str, "follow_id": int|None}：
    series 不存在/不是 series 类型 → subscribed=False（不建行）；
    已 active → 幂等成功（不重复建）；已 cancelled → 原地恢复。
    """
    series = (db.query(em.MediaItem)
              .filter(em.MediaItem.id == series_item_id)
              .first())
    if series is None:
        return {"subscribed": False, "reason": "series_not_found",
                "follow_id": None}
    if (getattr(series, "item_type", "") or "") != "series":
        return {"subscribed": False, "reason": "not_a_series",
                "follow_id": None}
    if getattr(series, "merged_into_id", None) is not None:
        return {"subscribed": False, "reason": "series_merged",
                "follow_id": None}
    existing = (db.query(em.SeriesFollow)
                .filter(em.SeriesFollow.user_id == user_id,
                        em.SeriesFollow.series_item_id == series_item_id)
                .first())
    if existing is not None:
        if existing.status == "active":
            return {"subscribed": True, "reason": "already_active",
                    "follow_id": existing.id}
        existing.status = "active"
        db.commit()
        return {"subscribed": True, "reason": "resubscribed",
                "follow_id": existing.id}
    row = em.SeriesFollow(user_id=user_id, series_item_id=series_item_id,
                          status="active")
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"subscribed": True, "reason": "subscribed", "follow_id": row.id}


def unsubscribe(db, user_id: int, series_item_id: int) -> dict:
    """取消订阅：只置 cancelled，不删行；未订阅过也幂等成功。"""
    existing = (db.query(em.SeriesFollow)
                .filter(em.SeriesFollow.user_id == user_id,
                        em.SeriesFollow.series_item_id == series_item_id)
                .first())
    if existing is None:
        return {"subscribed": False, "reason": "not_subscribed",
                "follow_id": None}
    if existing.status != "active":
        return {"subscribed": False, "reason": "already_cancelled",
                "follow_id": existing.id}
    existing.status = "cancelled"
    db.commit()
    return {"subscribed": False, "reason": "cancelled",
            "follow_id": existing.id}


def _episodes_after_follow(db, follow):
    """该剧订阅之后到货的单集（活行，按季/集/id 排序）。

    date_added 为空的集单独返回 unknown_date 计数，不猜它是
    新集还是旧集（旧集回填会违背“只转新集”，新集漏转也好过
    错转一整季旧集进用户盘）。
    """
    from sqlalchemy import nullslast
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.item_type == "episode",
                    em.MediaItem.series_id == follow.series_item_id,
                    em.MediaItem.merged_into_id.is_(None))
            .order_by(nullslast(em.MediaItem.season_number.asc()),
                      nullslast(em.MediaItem.episode_number.asc()),
                      em.MediaItem.id.asc())
            .all())
    new_eps, unknown = [], 0
    for ep in rows:
        added = getattr(ep, "date_added", None)
        if added is None:
            unknown += 1
            continue
        if follow.created_at is not None and added < follow.created_at:
            continue
        new_eps.append(ep)
    return new_eps, unknown


def _latest_user_job(db, episode_id, user_id):
    return (db.query(em.TransferJob)
            .filter(em.TransferJob.item_id == episode_id,
                    em.TransferJob.owner_scope == "user",
                    em.TransferJob.owner_ref == str(user_id))
            .order_by(em.TransferJob.id.desc())
            .first())


def _ready_variant(db, episode_id, user_id):
    return (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == episode_id,
                    em.TransferVariant.owner_scope == "user",
                    em.TransferVariant.owner_ref == str(user_id),
                    em.TransferVariant.status == "ready")
            .order_by(em.TransferVariant.id.desc())
            .first())


def process_series_follows(db) -> dict:
    """扫全部 active 订阅，为新到货的 Drive 单集建转存任务。

    返回 {"swept_follows", "jobs_created", "job_ids",
    "skipped": {reason: count}}。每集只走一条分支，原因明示：
    already_ready / throttled / master_off / no_drive /
    no_source_fid / unsupported_provider_115 / unknown_provider /
    job_created。异常按集隔离：单集抛错记 skipped[internal_error]，
    不中断整轮（工人循环外层另有兜底 warning）。
    开关关时直接返回零结果，不查订阅、不建任务。
    """
    result = {"swept_follows": 0, "jobs_created": 0, "job_ids": [],
              "skipped": {}}
    if not series_follow_enabled():
        return result
    from backend.emby_server import transfer_variant as tv
    follows = (db.query(em.SeriesFollow)
               .filter(em.SeriesFollow.status == "active")
               .order_by(em.SeriesFollow.id.asc())
               .all())
    for follow in follows:
        result["swept_follows"] += 1
        try:
            episodes, unknown = _episodes_after_follow(db, follow)
        except Exception as exc:  # noqa: BLE001 — 单剧隔离
            logger.warning("追剧扫取集失败 follow=%s：%s", follow.id, exc)
            result["skipped"]["internal_error"] = (
                result["skipped"].get("internal_error", 0) + 1)
            continue
        if unknown:
            result["skipped"]["unknown_date"] = (
                result["skipped"].get("unknown_date", 0) + unknown)
        for ep in episodes:
            try:
                if _ready_variant(db, ep.id, follow.user_id) is not None:
                    reason = "already_ready"
                else:
                    provider = tv._item_provider(db, ep)
                    if provider is None:
                        reason = "unknown_provider"
                    elif provider == "pan115":
                        # 115 与跨 provider 不在本单范围：不建任务。
                        reason = "unsupported_provider_115"
                    elif not tv.has_active_drive_binding(db,
                                                         follow.user_id):
                        # 无绑定不建注定失败的单（见模块头硬规矩）。
                        reason = "no_binding"
                    else:
                        try:
                            job = tv.create_user_job(
                                db, ep, follow.user_id, provider,
                                trigger=TRIGGER)
                        except tv.TransferError as exc:
                            reason = exc.kind
                        else:
                            result["jobs_created"] += 1
                            result["job_ids"].append(job.id)
                            reason = None
                if reason is not None:
                    result["skipped"][reason] = (
                        result["skipped"].get(reason, 0) + 1)
            except Exception as exc:  # noqa: BLE001 — 单集隔离
                logger.warning("追剧扫建任务失败 episode=%s：%s",
                               ep.id, exc)
                result["skipped"]["internal_error"] = (
                    result["skipped"].get("internal_error", 0) + 1)
    return result


def follow_status(db, user_id: int) -> dict:
    """该用户订阅与每集转存状态（只读，不建任务、不触发转存）。

    每集状态取最新用户任务行（pending/running/done/failed +
    error_kind/error_detail 截断 200 字）或 ready 副本（done 且
    有 ready 行时状态为 ready）；无任务无副本的新集标
    not_queued（原因见 provider/绑定判定，不猜测）。
    只列订阅之后到货的集（与扫集同一口径），旧集不列。
    """
    out = {"follows": []}
    follows = (db.query(em.SeriesFollow)
               .filter(em.SeriesFollow.user_id == user_id)
               .order_by(em.SeriesFollow.id.asc())
               .all())
    for follow in follows:
        series = (db.query(em.MediaItem)
                  .filter(em.MediaItem.id == follow.series_item_id)
                  .first())
        episodes, _unknown = _episodes_after_follow(db, follow)
        ep_rows = []
        for ep in episodes:
            ready = _ready_variant(db, ep.id, user_id)
            job = _latest_user_job(db, ep.id, user_id)
            if ready is not None:
                state, kind, detail = "ready", None, None
                queue_reason = None
            elif job is not None:
                state = job.status
                kind = job.error_kind
                detail = (job.error_detail or "")[:200]
                queue_reason = None
            else:
                # 无任务的新集：原因现算（不建任务、不触发转存）。
                from backend.emby_server import transfer_variant as tv
                provider = tv._item_provider(db, ep)
                if provider == "pan115":
                    queue_reason = "unsupported_provider_115"
                elif provider is None:
                    queue_reason = "unknown_provider"
                elif not tv.has_active_drive_binding(db, user_id):
                    queue_reason = "no_binding"
                else:
                    queue_reason = "waiting_next_sweep"
                state, kind, detail = "not_queued", None, None
            ep_rows.append({
                "episode_id": ep.id,
                "season_number": ep.season_number,
                "episode_number": ep.episode_number,
                "name": ep.name,
                "state": state,
                "error_kind": kind,
                "error_detail": detail,
                "queue_reason": queue_reason,
                "job_id": job.id if job is not None else None,
            })
        out["follows"].append({
            "follow_id": follow.id,
            "series_item_id": follow.series_item_id,
            "series_name": series.name if series is not None else None,
            "status": follow.status,
            "episodes": ep_rows,
        })
    return out

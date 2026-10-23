"""首页聚合接口：一次请求返回 HomeView 首屏的 9 项数据

背景：HomeView 原来用 Promise.all 并发 9 个请求。浏览器对同源 HTTP/1.1
只有 6 条并发上限，9 个请求要分两波排队；加上路由守卫的 auth/me，骨架屏
要熬 3 个串行往返。合并成 1 个请求后骨架期只取决于最慢的子查询，
首屏从「两波 RTT」缩到「一波」。

口径约定：**逐字段对齐原 9 个接口**，前端拿到后按原类型拆开展示，
不改任何展示逻辑：
- points          ← GET /api/user/economy/points/log?limit=1
- checkin         ← GET /api/user/economy/checkin/status
- invite          ← GET /api/user/invite/my-code（没有则自动生成，同口径）
- subscriptions   ← GET /api/user/subscriptions（end_date 现算 active）
- announcements   ← GET /api/user/announcements
- stats           ← GET /api/user/emby/stats
- media_seek      ← GET /api/user/media-seek
- tickets         ← GET /api/user/tickets
- sessions        ← GET /api/user/emby/sessions（只列未结束的）

容错：每个子查询独立 try/except，单项失败返回 None（前端各归各的降级），
不因为一个冷门子查询炸了把整页拖垮——与原前端逐项 catch 语义一致。
invite 的自动生成含 commit，其余子查询只读；单项失败不污染其它项。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import models
from backend.database import get_db
from backend.api.user import get_current_user
from backend.media_seek import WITHDRAWN_STATUS, season_label
from backend.emby_server import models as em
from backend.emby_server.api import TICKS

logger = logging.getLogger("aetrix.home")

router = APIRouter(prefix="/api/user/home", tags=["用户端-首页聚合"])


def _safe(name: str, fn, db=None):
    """单项失败不连带：记日志、返回 None，前端按 null 降级"""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        logger.exception("首页聚合子查询失败: %s", name)
        # F-P1-18：回滚会话，防污染后续查询。
        # SQLAlchemy 会话在异常后进入待回滚状态，不回滚则后续查询抛 PendingRollbackError，
        # 一个子项失败会拖垮整页。
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        return None


def _points(db: Session, user: models.WebUser) -> dict:
    """≈ GET /economy/points/log?limit=1（首页只读 balance，不取流水）"""
    return {"balance": user.points or 0}


def _checkin(db: Session, user: models.WebUser) -> dict:
    """≈ GET /economy/checkin/status（与 economy.checkin_status 同口径）"""
    from backend.api.economy import _today_start, _checkin_rules

    today = _today_start()
    checked = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == user.id,
        models.CheckinRecord.checkin_date >= today,
    ).first() is not None

    anchor = today if checked else today - timedelta(days=1)
    last = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == user.id,
        models.CheckinRecord.checkin_date <= anchor,
    ).order_by(models.CheckinRecord.checkin_date.desc()).first()

    rules = _checkin_rules(db)
    return {
        "enabled": rules["enabled"],
        "checked_today": checked,
        "streak": last.streak if last else 0,
        "base_points": rules["base_points"],
        "streak_bonus": rules["streak_bonus"],
        "streak_max_bonus": rules["streak_max_bonus"],
        "points": user.points or 0,
    }


def _invite(db: Session, user: models.WebUser) -> dict:
    """≈ GET /invite/my-code（含没有则自动生成，与原 handler 同口径）"""
    from backend.api.invitation import _generate_invite_code, get_invite_config

    code = db.query(models.InvitationCode).filter(
        models.InvitationCode.user_id == user.id,
        models.InvitationCode.is_active == True,  # noqa: E712
    ).first()
    if not code:
        # reward_points=0 = 跟随全局（推广配置）：个人码不做数值快照，
        # 否则全局改价对已生成的个人码不生效（2026-10-23 去重时清的账：
        # 生产 299 张个人码都带着生成当时的快照值，与渠道码专属奖励混淆）
        code = models.InvitationCode(
            code=_generate_invite_code(db),
            user_id=user.id,
            reward_points=0,
        )
        db.add(code)
        db.commit()
        db.refresh(code)

    invited_count = db.query(models.InvitationRecord).filter(
        models.InvitationRecord.inviter_id == user.id
    ).count()
    return {
        "code": code.code,
        "use_count": code.use_count or 0,
        "max_uses": code.max_uses,
        "invited_count": invited_count,
        "config": get_invite_config(db),
    }


def _subscriptions(db: Session, user: models.WebUser) -> list:
    """≈ GET /user/subscriptions（end_date 现算 active，与原 handler 同口径）"""
    from backend import realms

    subs = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user.id
    ).order_by(models.UserSubscription.created_at.desc()).all()

    now = datetime.now()
    plans = {
        p.id: p for p in db.query(models.SubscriptionPlan).filter(
            models.SubscriptionPlan.id.in_([s.plan_id for s in subs if s.plan_id])
        ).all()
    } if subs else {}
    realm_names = {r.id: r.name for r in realms.list_realms(db)}

    result = []
    for sub in subs:
        plan = plans.get(sub.plan_id)
        is_current = bool(
            sub.status == "active" and sub.end_date and sub.end_date > now
        )
        result.append({
            "id": sub.id,
            "plan_name": plan.name if plan else "未知套餐",
            "realm_id": sub.realm_id,
            "realm_name": realm_names.get(sub.realm_id, "") if sub.realm_id else "",
            "start_date": sub.start_date.isoformat() if sub.start_date else None,
            "end_date": sub.end_date.isoformat() if sub.end_date else None,
            "status": "active" if is_current else "expired",
            "is_current": is_current,
            "auto_renew": sub.auto_renew,
            "days_left": max(0, (sub.end_date - now).days) if sub.end_date else 0,
        })
    return result


def _announcements(db: Session) -> list:
    """≈ GET /user/announcements（前端只用 is_pinned 置顶位）"""
    rows = db.query(models.Announcement).filter(
        models.Announcement.is_active == True,  # noqa: E712
    ).order_by(
        models.Announcement.is_pinned.desc(),
        models.Announcement.created_at.desc(),
    ).limit(20).all()
    return [
        {
            "id": a.id, "title": a.title, "content": a.content,
            "type": a.type, "is_pinned": bool(a.is_pinned),
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


def _stats(db: Session, user: models.WebUser) -> dict:
    """≈ GET /emby/stats（总时长只统计最近 20 条会话，与原 handler 同口径）"""
    total_rows = db.query(
        func.count(em.UserMediaData.id),
        func.coalesce(func.sum(em.UserMediaData.play_count), 0),
    ).filter(em.UserMediaData.user_id == user.id).first()

    rows = (
        db.query(em.PlaybackSession, em.MediaItem)
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(em.PlaybackSession.user_id == user.id)
        .order_by(em.PlaybackSession.last_update_at.desc())
        .limit(20)
        .all()
    )
    total_seconds = 0
    for session, item in rows:
        runtime = item.duration_ticks or 0
        pos = session.position_ticks or 0
        total_seconds += min(pos, runtime) // TICKS if runtime else pos // TICKS

    recent = [
        {
            "item": item.name, "type": item.item_type,
            "device": session.device_name, "client": session.client_name,
            "position_ticks": session.position_ticks,
            "duration_ticks": item.duration_ticks,
            "at": session.last_update_at.isoformat() if session.last_update_at else None,
        }
        for session, item in rows[:10]
    ]
    return {
        "total_plays": int(total_rows[1] or 0),
        "watched_items": int(total_rows[0] or 0),
        "total_seconds": total_seconds,
        "recent": recent,
    }


def _media_seek(db: Session, user: models.WebUser) -> dict:
    """≈ GET /user/media-seek（列表 + 今日额度，与原 handler 同口径）"""
    from backend import realms

    query = db.query(models.MovieRequest).filter(
        models.MovieRequest.user_id == user.id,
        models.MovieRequest.status != WITHDRAWN_STATUS,
    )
    requests = query.order_by(models.MovieRequest.created_at.desc()).limit(200).all()
    realm_names = {r.id: r.name for r in realms.list_realms(db)}
    from backend import media_seek as _ms
    limit = _ms.daily_limit(db)
    used = _ms.used_today(db, user.id)

    return {
        "requests": [
            {
                "id": r.id, "movie_name": r.movie_name, "year": r.year,
                "type": r.type, "note": r.note, "status": r.status,
                "admin_note": r.admin_note,
                # 首页只需要列表与额度；字段口径与 GET /user/media-seek 一致
                "season": r.season,
                "season_label": season_label(r.season),
                "emby_item_id": r.emby_item_id,
                "realm_id": r.realm_id,
                "realm_name": realm_names.get(r.realm_id, "") if r.realm_id else "",
                "created_at": r.created_at.isoformat(),
            }
            for r in requests
        ],
        "quota": {"used_today": used, "daily_limit": limit, "remaining": max(0, limit - used)},
    }


def _tickets(db: Session, user: models.WebUser) -> list:
    """≈ GET /user/tickets"""
    rows = db.query(models.Ticket).filter(
        models.Ticket.user_id == user.id
    ).order_by(models.Ticket.updated_at.desc()).all()
    return [
        {
            "id": t.id, "title": t.title, "category": t.category,
            "status": t.status, "priority": t.priority,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in rows
    ]


def _sessions(db: Session, user: models.WebUser) -> dict:
    """≈ GET /emby/sessions（只列未结束会话，与原 handler 同口径）"""
    rows = (
        db.query(em.PlaybackSession, em.MediaItem)
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(
            em.PlaybackSession.user_id == user.id,
            em.PlaybackSession.ended_at.is_(None),
        )
        .order_by(em.PlaybackSession.last_update_at.desc())
        .all()
    )
    return {
        "sessions": [
            {
                "session_key": s.session_key,
                "item_id": i.guid,
                "item": i.name,
                "item_type": i.item_type,
                "device": s.device_name,
                "client": s.client_name,
                "remote_addr": s.remote_addr,
                "play_method": s.play_method,
                "is_paused": bool(s.is_paused),
                "position_ticks": s.position_ticks,
                "duration_ticks": i.duration_ticks,
                "progress": round((s.position_ticks or 0) / (i.duration_ticks or 1) * 100, 1),
                "started_at": s.start_time.isoformat() if s.start_time else None,
                "updated_at": s.last_update_at.isoformat() if s.last_update_at else None,
            }
            for s, i in rows
        ]
    }


@router.get("/summary")
def home_summary(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """首页首屏聚合：9 项数据一次返回，单项失败各自为 None"""
    user = current_user
    return {
        "points": _safe("points", lambda: _points(db, user), db),
        "checkin": _safe("checkin", lambda: _checkin(db, user), db),
        "invite": _safe("invite", lambda: _invite(db, user), db),
        "subscriptions": _safe("subscriptions", lambda: _subscriptions(db, user), db),
        "announcements": _safe("announcements", lambda: _announcements(db), db),
        "stats": _safe("stats", lambda: _stats(db, user), db),
        "media_seek": _safe("media_seek", lambda: _media_seek(db, user), db),
        "tickets": _safe("tickets", lambda: _tickets(db, user), db),
        "sessions": _safe("sessions", lambda: _sessions(db, user), db),
    }

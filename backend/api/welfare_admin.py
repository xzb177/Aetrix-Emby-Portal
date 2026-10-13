# -*- coding: utf-8 -*-
"""公益服管理后台 API

- GET  /welfare/users              公益用户列表
- POST /welfare/grant              开通/续期公益
- POST /welfare/revoke             取消公益资格
- POST /welfare/bulk-extend        批量延期
- GET/POST /welfare/lottery/prizes 奖品列表/新增
- PUT/DELETE /welfare/lottery/prizes/{id} 编辑/删除奖品
- GET  /welfare/lottery/logs       抽奖记录
- GET/PUT /welfare/config          公益配置读写
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend import models
from backend.api.admin_core import admin_router, get_current_admin, _audit
from backend.database import get_db


# ==================== 公益用户管理 ====================

@admin_router.get("/welfare/users")
def welfare_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str = Query(""),
    only_welfare: bool = Query(True),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """公益用户列表"""
    q = db.query(models.WebUser)
    if only_welfare:
        q = q.filter(models.WebUser.is_welfare == True)  # noqa: E712
    if keyword:
        q = q.filter(models.WebUser.username.like(f"%{keyword}%"))
    total = q.count()
    users = q.order_by(models.WebUser.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    from backend.emby_server import portal
    items = []
    for u in users:
        st = portal.get_welfare_status(db, u)
        items.append({
            "id": u.id,
            "username": u.username,
            "is_welfare": bool(getattr(u, "is_welfare", False)),
            "welfare_expires_at": u.welfare_expires_at.isoformat() if getattr(u, "welfare_expires_at", None) else None,
            "days_left": st["days_left"],
            "welfare_grant_channel": getattr(u, "welfare_grant_channel", None),
        })
    return {"total": total, "items": items}


class GrantRequest(BaseModel):
    user_id: int
    days: int = Field(30, description="天数，0=永不过期")
    channel: str = "admin"


@admin_router.post("/welfare/grant")
def welfare_grant(
    req: GrantRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """开通/续期公益资格"""
    user = db.query(models.WebUser).filter(models.WebUser.id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    from backend.emby_server import portal
    expires = portal.grant_welfare(db, user, req.channel, req.days, granted_by=current_admin.id)
    _audit(db, current_admin.id, "welfare_grant", "user", user.id, {"days": req.days, "channel": req.channel})
    db.commit()
    return {"success": True, "expires_at": expires.isoformat() if expires else None}


class RevokeRequest(BaseModel):
    user_id: int


@admin_router.post("/welfare/revoke")
def welfare_revoke(
    req: RevokeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """取消公益资格"""
    user = db.query(models.WebUser).filter(models.WebUser.id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.is_welfare = False
    db.add(models.WelfareGrantLog(user_id=user.id, channel="admin_revoke", days=0, granted_by=current_admin.id))
    _audit(db, current_admin.id, "welfare_revoke", "user", user.id, {})
    db.commit()
    return {"success": True}


class BulkExtendRequest(BaseModel):
    min_expired_days: int = Field(0, description="过期天数下限")
    max_expired_days: int = Field(30, description="过期天数上限")
    add_days: int = Field(30, description="增加天数")


@admin_router.post("/welfare/bulk-extend")
def welfare_bulk_extend(
    req: BulkExtendRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """按过期天数范围批量延期"""
    now = datetime.now()
    users = db.query(models.WebUser).filter(models.WebUser.is_welfare == True).all()  # noqa: E712
    from backend.emby_server import portal
    affected = 0
    for u in users:
        exp = getattr(u, "welfare_expires_at", None)
        if exp is None:
            continue  # 永不过期跳过
        expired_days = (now - exp).days
        if expired_days < 0:
            continue  # 未过期跳过
        if req.min_expired_days <= expired_days <= req.max_expired_days:
            portal.grant_welfare(db, u, "bulk_extend", req.add_days, granted_by=current_admin.id)
            affected += 1
    _audit(db, current_admin.id, "welfare_bulk_extend", "system", None, {"affected": affected, "add_days": req.add_days})
    db.commit()
    return {"success": True, "affected": affected}


# ==================== 抽奖奖品管理 ====================

def _prize_to_dict(p):
    return {
        "id": p.id,
        "name": p.name,
        "type": p.type,
        "value": p.value,
        "probability": p.probability,
        "enabled": bool(p.enabled),
    }


@admin_router.get("/welfare/lottery/prizes")
def lottery_prizes(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """奖品列表"""
    prizes = db.query(models.LotteryPrize).order_by(models.LotteryPrize.id).all()
    return [_prize_to_dict(p) for p in prizes]


class PrizeRequest(BaseModel):
    name: str
    type: str = "days"
    value: int = 0
    probability: float = 1.0
    enabled: bool = True


@admin_router.post("/welfare/lottery/prizes")
def lottery_prize_create(
    req: PrizeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """新增奖品"""
    p = models.LotteryPrize(
        name=req.name, type=req.type, value=req.value,
        probability=req.probability, enabled=req.enabled,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    _audit(db, current_admin.id, "lottery_prize_create", "lottery_prize", p.id, {"name": req.name})
    db.commit()
    return _prize_to_dict(p)


class PrizeUpdateRequest(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    value: Optional[int] = None
    probability: Optional[float] = None
    enabled: Optional[bool] = None


@admin_router.put("/welfare/lottery/prizes/{prize_id}")
def lottery_prize_update(
    prize_id: int,
    req: PrizeUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """编辑奖品"""
    p = db.query(models.LotteryPrize).filter(models.LotteryPrize.id == prize_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="奖品不存在")
    for field in ("name", "type", "value", "probability", "enabled"):
        v = getattr(req, field)
        if v is not None:
            setattr(p, field, v)
    _audit(db, current_admin.id, "lottery_prize_update", "lottery_prize", p.id, {})
    db.commit()
    return _prize_to_dict(p)


@admin_router.delete("/welfare/lottery/prizes/{prize_id}")
def lottery_prize_delete(
    prize_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """删除奖品"""
    p = db.query(models.LotteryPrize).filter(models.LotteryPrize.id == prize_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="奖品不存在")
    _audit(db, current_admin.id, "lottery_prize_delete", "lottery_prize", prize_id, {"name": p.name})
    db.delete(p)
    db.commit()
    return {"success": True}


@admin_router.get("/welfare/lottery/logs")
def lottery_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """抽奖记录"""
    q = db.query(models.LotteryLog).order_by(models.LotteryLog.id.desc())
    total = q.count()
    logs = q.offset((page - 1) * page_size).limit(page_size).all()
    items = []
    for log in logs:
        items.append({
            "id": log.id,
            "username": log.user.username if log.user else "",
            "prize_name": log.prize.name if log.prize else "",
            "created_at": log.created_at.isoformat() if log.created_at else None,
        })
    return {"total": total, "items": items}


# ==================== 求片审核 ====================

def _request_to_dict(r):
    return {
        "id": r.id,
        "title": r.title,
        "media_type": r.media_type,
        "tmdb_id": r.tmdb_id,
        "username": r.user.username if r.user else "",
        "status": r.status,
        "admin_note": r.admin_note or "",
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@admin_router.get("/welfare/requests")
def welfare_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """求片审核列表"""
    q = db.query(models.MediaRequest).order_by(models.MediaRequest.id.desc())
    if status:
        q = q.filter(models.MediaRequest.status == status)
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [_request_to_dict(r) for r in items]}


class RequestNote(BaseModel):
    admin_note: str = ""


@admin_router.post("/welfare/requests/{request_id}/approve")
def welfare_request_approve(
    request_id: int,
    req: RequestNote,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """通过求片"""
    r = db.query(models.MediaRequest).filter(models.MediaRequest.id == request_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="求片记录不存在")
    r.status = "approved"
    r.admin_note = req.admin_note
    _audit(db, current_admin.id, "welfare_request_approve", "media_request", r.id, {"title": r.title})
    db.commit()
    return {"success": True}


@admin_router.post("/welfare/requests/{request_id}/reject")
def welfare_request_reject(
    request_id: int,
    req: RequestNote,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """拒绝求片"""
    r = db.query(models.MediaRequest).filter(models.MediaRequest.id == request_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="求片记录不存在")
    r.status = "rejected"
    r.admin_note = req.admin_note
    _audit(db, current_admin.id, "welfare_request_reject", "media_request", r.id, {"title": r.title})
    db.commit()
    return {"success": True}


@admin_router.post("/welfare/requests/{request_id}/done")
def welfare_request_done(
    request_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """标记求片已入库"""
    r = db.query(models.MediaRequest).filter(models.MediaRequest.id == request_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="求片记录不存在")
    r.status = "done"
    _audit(db, current_admin.id, "welfare_request_done", "media_request", r.id, {"title": r.title})
    db.commit()
    return {"success": True}


# ==================== 公益配置 ====================

WELFARE_CONFIG_KEYS = {
    "points_signin_min": "1",
    "points_signin_max": "3",
    "points_signin_streak_bonus": "2",
    "points_chat_daily_cap": "20",
    "points_redeem_7d": "100",
    "points_redeem_30d": "300",
    "welfare_grace_days": "7",
    "welfare_inactive_days": "30",
    "welfare_request_monthly": "3",
    "lottery_cost": "10",
    # P0 统一货币体系：充值比例与红包规则
    "recharge_ratio": "1.2",
    # C4 商店改造：快捷金额
    "recharge_quick_amounts": "10,30,50,100,200",
    # B4 TG Bot 总控：总开关、群白名单、限流、各命令开关、红包开关
    "bot_enabled": "true",
    "bot_group_ids": "",
    "bot_rate_limit_seconds": "3",
    "bot_group_rate_limit": "20",
    "bot_cmd_checkin": "true",
    "bot_cmd_points": "true",
    "bot_cmd_redeem": "true",
    "bot_cmd_bind": "true",
    "bot_redpacket_enabled": "true",
    "redpacket_fee_pct": "5",
    "redpacket_send_limit_7d": "20",
    "redpacket_recv_limit_7d": "10",
    # 红包过期自动退款：总开关 + 扫描间隔（秒，最小 60）
    "redpacket_refund_enabled": "true",
    "redpacket_refund_interval_sec": "300",
    # TG 门禁：公益服能力（签到/积分/红包/抽奖）是否要求绑定 Telegram
    "welfare_require_tg_bind": "1",
    # 群抽奖：总开关默认开（关闭后不可新建活动），允许群逗号分隔、空=不限制
    "lottery_enabled": "1",
    "lottery_group_ids": "",
    # 群抽奖自动开奖：总开关默认开，扫描间隔默认 60 秒（最小 30），开奖通知默认开
    "lottery_auto_draw_enabled": "1",
    "lottery_draw_interval_sec": "60",
    "lottery_notify_winners": "1",
    # 注册后 TG 绑定引导页总开关
    "tg_bind_guide_enabled": "1",
    # 求片 v2：总开关 / 附议 / 附议者入库通知（默认全开）
    "media_seek_enabled": "1",
    "media_seek_vote_enabled": "1",
    "media_seek_notify_voters": "1",
# M1 群发言积分：总开关默认关闭，服主手动开启
    "chat_points_enabled": "false",
    # C2 积分转账：总开关默认开启，手续费/限额可配（0=不收/不限）
    "points_transfer_enabled": "1",
    "points_transfer_fee_pct": "5",
    "points_transfer_min": "1",
    "points_transfer_max": "0",
    "points_transfer_daily_cap": "0",
    "chat_points_group_ids": "",
    "chat_points_per_message": "1",
    "chat_points_min_len": "2",
    "chat_points_minute_window": "60",
    "chat_points_daily_cap": "20",
    "chat_points_points_per_day": "100",
}


@admin_router.get("/welfare/config")
def welfare_config_get(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """读取公益配置"""
    result = {}
    for key, default in WELFARE_CONFIG_KEYS.items():
        cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        result[key] = cfg.value if cfg else default
    return result


@admin_router.put("/welfare/config")
def welfare_config_set(
    data: dict,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存公益配置"""
    updated = []
    for key, value in data.items():
        if key not in WELFARE_CONFIG_KEYS:
            continue
        cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if cfg:
            cfg.value = str(value)
        else:
            db.add(models.SystemConfig(key=key, value=str(value)))
        updated.append(key)
    _audit(db, current_admin.id, "welfare_config_update", "system", None, {"updated": updated})
    db.commit()
    return {"success": True, "updated": updated}


# ==================== 群抽奖活动管理（G2） ====================

def _get_lottery():
    """获取群抽奖核心模块（G1），未部署时抛出 501。"""
    try:
        import backend.lottery as lottery_module
        return lottery_module
    except ImportError:
        raise HTTPException(status_code=501, detail="群抽奖核心模块（G1）尚未部署")


def _row_to_dict(row):
    """把 SQL 查询行转为字典，datetime 统一转 isoformat，None 保持 None。"""
    data = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    out = {}
    for key, value in data.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


class PrizeItem(BaseModel):
    """抽奖奖品项。"""
    name: str
    type: str
    value: int = Field(ge=0)
    quantity: int = Field(ge=1)


class RoundCreateRequest(BaseModel):
    """创建群抽奖活动请求体。"""
    title: str
    chat_id: int  # Telegram 群组 chat_id（整数，可为负数）
    prizes: List[PrizeItem]
    draw_at: Optional[str] = None
    max_participants: Optional[int] = None


@admin_router.get("/welfare/lottery/rounds")
def list_lottery_rounds(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """分页查询群抽奖活动列表。"""
    _get_lottery()
    offset = (page - 1) * page_size
    total = db.execute(
        text("SELECT COUNT(*) FROM lottery_rounds WHERE (:status IS NULL OR status = :status)"),
        {"status": status},
    ).scalar()
    rows = db.execute(
        text(
            """
            SELECT r.id, r.title, r.chat_id, r.status, r.draw_at, r.created_at,
                   (SELECT COUNT(*) FROM lottery_round_entries e WHERE e.round_id = r.id) AS participant_count,
                   (SELECT COUNT(*) FROM lottery_round_prizes p WHERE p.round_id = r.id) AS prize_count
            FROM lottery_rounds r
            WHERE (:status IS NULL OR r.status = :status)
            ORDER BY r.id DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        {"status": status, "limit": page_size, "offset": offset},
    ).all()
    items = []
    for row in rows:
        d = _row_to_dict(row)
        items.append(
            {
                "id": d.get("id"),
                "title": d.get("title"),
                "chat_id": d.get("chat_id"),
                "status": d.get("status"),
                "participant_count": d.get("participant_count"),
                "prize_count": d.get("prize_count"),
                "draw_at": d.get("draw_at"),
                "created_at": d.get("created_at"),
            }
        )
    return {"total": total, "items": items}


@admin_router.post("/welfare/lottery/rounds")
def create_lottery_round(
    req: RoundCreateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """创建群抽奖活动。"""
    lottery_module = _get_lottery()
    if not req.title or not req.title.strip():
        raise HTTPException(status_code=400, detail="活动标题不能为空")
    if not req.chat_id:
        raise HTTPException(status_code=400, detail="群 ID 不能为空")
    if not req.prizes:
        raise HTTPException(status_code=400, detail="至少需要配置 1 个奖品")
    for item in req.prizes:
        if item.type not in ("days", "points", "whitelist"):
            raise HTTPException(status_code=400, detail="奖品类型只能是 days/points/whitelist")
        if item.value < 0:
            raise HTTPException(status_code=400, detail="奖品价值不能为负数")
        if item.quantity < 1:
            raise HTTPException(status_code=400, detail="奖品数量至少为 1")
    dt = None
    if req.draw_at:
        try:
            dt = datetime.fromisoformat(req.draw_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="draw_at 时间格式无效")
    if req.max_participants is not None and req.max_participants < 1:
        raise HTTPException(status_code=400, detail="最大参与人数至少为 1")
    # 总开关：读 SystemConfig，查不到视为开启
    enabled_cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == "lottery_enabled").first()
    enabled_raw = enabled_cfg.value if enabled_cfg else "1"
    if str(enabled_raw).strip().lower() in ("0", "false"):
        raise HTTPException(status_code=403, detail="群抽奖功能未开启")
    # 群白名单：查不到视为不限制
    group_cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == "lottery_group_ids").first()
    group_raw = group_cfg.value if group_cfg else ""
    if group_raw and str(group_raw).strip():
        allowed_ids = set()
        for x in str(group_raw).split(","):
            x = x.strip()
            if x:
                try:
                    allowed_ids.add(int(x))
                except ValueError:
                    pass
        if allowed_ids and req.chat_id not in allowed_ids:
            raise HTTPException(status_code=403, detail="该群未被允许")
    # 同群已有进行中的活动则拒绝
    active = lottery_module.get_active_round(db, req.chat_id)
    if active:
        raise HTTPException(status_code=400, detail="该群已有进行中的抽奖活动")
    result = lottery_module.create_round(
        db=db,
        title=req.title,
        chat_id=req.chat_id,
        prizes=[item.model_dump() for item in req.prizes],
        draw_at=dt,
        max_participants=req.max_participants,
        created_by=current_admin.id,
    )
    _audit(db, current_admin.id, "lottery_round_create", "lottery_round", result.id, {"title": req.title, "chat_id": req.chat_id})
    db.commit()
    return {
        "id": result.id,
        "title": result.title,
        "chat_id": result.chat_id,
        "status": result.status,
        "seed_hash": result.seed_hash,
        "draw_at": result.draw_at.isoformat() if result.draw_at else None,
        "created_at": result.created_at.isoformat() if result.created_at else None,
    }


@admin_router.get("/welfare/lottery/rounds/{round_id}")
def get_lottery_round(
    round_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """查询群抽奖活动详情。"""
    lottery_module = _get_lottery()
    row = db.execute(text("SELECT * FROM lottery_rounds WHERE id = :id"), {"id": round_id}).first()
    if row is None:
        raise HTTPException(status_code=404, detail="抽奖活动不存在")
    round_dict = _row_to_dict(row)
    prizes = [
        _row_to_dict(r)
        for r in db.execute(
            text("SELECT id, round_id, name, type, value, quantity, sort FROM lottery_round_prizes WHERE round_id = :id ORDER BY sort ASC"),
            {"id": round_id},
        ).all()
    ]
    entries = [
        _row_to_dict(r)
        for r in db.execute(
            text("SELECT id, user_id, telegram_id, joined_at FROM lottery_round_entries WHERE round_id = :id ORDER BY joined_at ASC LIMIT 100"),
            {"id": round_id},
        ).all()
    ]
    winners = [
        _row_to_dict(r)
        for r in db.execute(
            text(
                """
                SELECT w.id, w.prize_id, p.name AS prize_name, e.user_id, e.telegram_id,
                       w.distributed, w.distributed_at
                FROM lottery_round_winners w
                LEFT JOIN lottery_round_prizes p ON p.id = w.prize_id
                LEFT JOIN lottery_round_entries e ON e.id = w.entry_id
                WHERE w.round_id = :id
                ORDER BY w.id ASC
                """
            ),
            {"id": round_id},
        ).all()
    ]
    try:
        verify = lottery_module.verify_round(db, round_id)
    except Exception:
        verify = None
    return {"round": round_dict, "prizes": prizes, "entries": entries, "winners": winners, "verify": verify}


@admin_router.post("/welfare/lottery/rounds/{round_id}/draw")
def draw_lottery_round(
    round_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动执行群抽奖开奖与发放。"""
    lottery_module = _get_lottery()
    row = db.execute(text("SELECT id, status FROM lottery_rounds WHERE id = :id"), {"id": round_id}).first()
    if row is None:
        raise HTTPException(status_code=404, detail="抽奖活动不存在")
    if dict(row._mapping).get("status") != "open":
        raise HTTPException(status_code=400, detail="只能对进行中的活动开奖")
    winners = lottery_module.draw_round(db, round_id)
    distribute_result = lottery_module.distribute_round(db, round_id)
    _audit(db, current_admin.id, "lottery_round_draw", "lottery_round", round_id, {"winner_count": len(winners) if isinstance(winners, list) else 0})
    db.commit()
    return {
        "success": True,
        "winners": [
            {"id": w.id, "entry_id": w.entry_id, "prize_id": w.prize_id, "distributed": bool(w.distributed)}
            for w in (winners if isinstance(winners, list) else [])
        ],
        "distribute": distribute_result,
    }


@admin_router.post("/welfare/lottery/rounds/{round_id}/cancel")
def cancel_lottery_round(
    round_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """取消进行中的群抽奖活动。"""
    _get_lottery()
    row = db.execute(text("SELECT id, status FROM lottery_rounds WHERE id = :id"), {"id": round_id}).first()
    if row is None:
        raise HTTPException(status_code=404, detail="抽奖活动不存在")
    if dict(row._mapping).get("status") != "open":
        raise HTTPException(status_code=400, detail="只能取消进行中的活动")
    db.execute(text("UPDATE lottery_rounds SET status = 'cancelled' WHERE id = :id"), {"id": round_id})
    _audit(db, current_admin.id, "lottery_round_cancel", "lottery_round", round_id, {})
    db.commit()
    return {"success": True}

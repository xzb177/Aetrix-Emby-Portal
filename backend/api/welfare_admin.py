# -*- coding: utf-8 -*-
"""公益服管理后台 API

- GET  /welfare/users              公益用户列表
- POST /welfare/grant              开通/续期公益
- POST /welfare/revoke             取消公益资格
- POST /welfare/bulk-extend        批量延期
- GET/PUT /welfare/config          公益配置读写
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, List

from fastapi import Depends, HTTPException, Query

logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from backend import config_service, lottery_governance, models
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
    # P2 修复（完美审查 B-P2-1）：负天数会穿透成"永不过期"，必须 ge=0。
    days: int = Field(30, ge=0, description="天数，0=永不过期")
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
    # P2 修复（完美审查 B-P2-1）：负天数会穿透成"永不过期"，必须 ge=0。
    add_days: int = Field(30, ge=0, description="增加天数")


@admin_router.post("/welfare/bulk-extend")
def welfare_bulk_extend(
    req: BulkExtendRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """按过期天数范围批量延期"""
    now = datetime.now()
    from backend.emby_server import portal
    affected = 0
    failed: list[dict] = []
    # P2 修复（完美审查 B-P2-3）：键集分页（id > last_id，每批 500），
    # 与 welfare_expiry.check_welfare_expiry 同一口径——之前全表 .all()
    # 无界加载进内存。
    last_id = 0
    while True:
        users = (
            db.query(models.WebUser)
            .filter(models.WebUser.is_welfare == True, models.WebUser.id > last_id)  # noqa: E712
            .order_by(models.WebUser.id)
            .limit(500)
            .all()
        )
        if not users:
            break
        for u in users:
            last_id = u.id
            exp = getattr(u, "welfare_expires_at", None)
            if exp is None:
                continue  # 永不过期跳过
            expired_days = (now - exp).days
            if expired_days < 0:
                continue  # 未过期跳过
            if req.min_expired_days <= expired_days <= req.max_expired_days:
                try:
                    portal.grant_welfare(db, u, "bulk_extend", req.add_days, granted_by=current_admin.id)
                    affected += 1
                except Exception as e:
                    # 批量里单个失败不许掀翻整批：grant_welfare 每用户独立提交，
                    # 已延期的不回滚；失败记清单并 rollback 清会话，最后汇总审计
                    # 如实返回，前端能看到谁没延上（此前中途抛错=前面白做、
                    # 汇总审计也没落盘、前端只拿到 500 猜谜）。
                    db.rollback()
                    failed.append({"user_id": u.id, "username": u.username,
                                   "error": str(e)[:200]})
                    logger.warning("公益批量延期用户 %s 失败：%s", u.id, e)
    _audit(db, current_admin.id, "welfare_bulk_extend", "system", None,
           {"affected": affected, "failed": len(failed), "add_days": req.add_days})
    db.commit()
    return {"success": True, "affected": affected, "failed": failed}


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
    q = (
        db.query(models.MediaRequest)
        .options(joinedload(models.MediaRequest.user))  # r49: 防 N+1（_request_to_dict 逐行读 r.user.username）
        .order_by(models.MediaRequest.id.desc())
    )
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
    "points_chat_per_msg": "1",
    "points_chat_daily_cap": "20",
    "points_redeem_7d": "100",
    "points_redeem_30d": "300",
    # G1 资格码月配额：每位管理员每月可生成的资格码（reward_type=welfare）
    # 张数上限，默认 30（宽松值）；0=不限（读侧 codes.welfare_code_monthly_quota）
    "welfare_code_monthly_quota": "30",
    # 兑换资格门槛（2026-10-30 定版，读侧 points.redeem_welfare）：来源限定
    # 总开关（只认自挣能量）＋号龄/活跃双钥匙；管理员发放不受此门约束
    "welfare_redeem_self_earned_only": "true",
    "welfare_redeem_min_account_age_days": "3",
    "welfare_redeem_min_active_days": "2",
    # 新注册冷启动赠送天数（emby_portal._grant_register_gifts 读取；0=永不过期仅福利天数适用）
    "register_gift_welfare_days": "30",
    "register_gift_view_days": "30",
    # 新注册送公益资格总开关（2026-10-29 默认关：开放注册但不送资格；
    # 服主在管理端改成 true 即可重开，天数仍走上面两个键）
    "register_gift_welfare_enabled": "false",
    # 新注册送查看凭证总开关（2026-10-29 默认关：新注册不送凭证、可以送资格；
    # 服主在管理端改成 true 即可重开，天数仍走 register_gift_view_days）
    "register_gift_view_enabled": "false",
    "welfare_grace_days": "7",
    "welfare_streak_renew_min": "30",
    # 连签保资格（streak_renew）总开关（默认开＝现行行为）＋续期天数（默认 30）
    "welfare_streak_renew_enabled": "true",
    "welfare_streak_renew_days": "30",
    "welfare_inactive_days": "30",
    "welfare_request_monthly": "3",
    # P0 统一货币体系：充值比例与红包规则
    "recharge_ratio": "1.2",
    # C4 商店改造：快捷金额
    "recharge_quick_amounts": "10,30,50,100,200",
    # B4 TG Bot 总控：总开关、群白名单、限流、各命令开关、红包开关
    "bot_group_ids": "",
    "bot_rate_limit_seconds": "3",
    "bot_group_rate_limit": "20",
    # 观影榜海报日推：总开关默认开 / 发分开关默认关 / 名次发分表（逗号分隔）
    "bot_rankpush_enabled": "true",
    "bot_rankpush_reward_enabled": "false",
    "bot_rankpush_reward_table": "10,8,6,5,4,3,2,1,1,1",
    # 频道马甲治理＋退群处置（2026-10-30 借鉴 embyboss 第 1 项；读侧
    # tg_bot.policy，缺省与 policy.DEFAULTS 逐字对齐）
    "bot_channel_guard_enabled": "true",
    "bot_channel_whitelist": "",
    "bot_leave_action": "cancel_welfare",
    "redpacket_fee_pct": "5",
    "redpacket_send_limit_7d": "20",
    "redpacket_recv_limit_7d": "10",
    # 红包过期自动退款：总开关 + 扫描间隔（秒，最小 60）
    "redpacket_refund_interval_sec": "300",
    # TG 门禁：公益服能力（签到/积分/红包/抽奖）是否要求绑定 Telegram
    "welfare_require_tg_bind": "1",
    # 群抽奖：总开关默认开（关闭后不可新建活动），允许群逗号分隔、空=不限制
    "lottery_group_ids": "",
    # 群抽奖自动开奖：总开关默认开，扫描间隔默认 60 秒（最小 30），开奖通知默认开
    "lottery_draw_interval_sec": "60",
    # 三重门：身份门（参赛者必须在 TG 群里，默认开）/ 资格门（新号限制天数、每日参加上限，默认 0=不限制）
    "lottery_require_group_member": "1",
    "lottery_min_account_age_days": "0",
    "lottery_max_joins_per_day": "0",
    # 能量老虎机（2026-10-30 用户亲批）：总开关默认开；注额三档；赔付倍数；
    # 头奖池抽水 5%＋种子 100（池余额存 slots_pool 表，不在此处）；
    # 每日下注封顶 200 / 每日赢分封顶 1000（基础赔付计，池奖不计）
    "slots_enabled": "true",
    # 群里可玩（2026-10-30 用户亲令）：名单群的老虎机入口与注额按钮
    # 在群内直接可用、扣点击者本人；关掉则群里回落「请私聊」提示。
    "slots_group_enabled": "true",
    "slots_bet_tiers": "5,10,20",
    "slots_pay_777": "20",
    "slots_pay_bar": "10",
    "slots_pay_fruit": "5",
    "slots_pay_two7": "1",
    "slots_pool_rake_pct": "5",
    "slots_pool_seed": "100",
    "slots_daily_bet_cap": "200",
    "slots_daily_win_cap": "1000",
    # 注册后 TG 绑定引导页总开关
    # 求片 v2：总开关 / 附议 / 附议者入库通知（默认全开）
    # C2 积分转账：总开关默认开启，手续费/限额可配（0=不收/不限）
    "points_transfer_fee_pct": "5",
    "points_transfer_min": "1",
    "points_transfer_max": "0",
    "points_transfer_daily_cap": "0",
    # M1 群发言积分（chat_points_*）已于 2026-10-23 去重退役：与
    # points.award_chat_points（points_chat_*）是两套重复系统，退役
    # 机器人这套（生产一直关着），发言积分只保留 points.py 一套。
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
    """保存公益配置（数值/开关键按 config_schema 校验，非法 400；空字符串=未设置，回退默认值）"""
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="配置必须是对象")
    # 配置瘦身第 4 批 4-4：白名单过滤保留在端点层（未知键静默忽略，行为不变），
    # 校验→落库→写时失效收敛到 config_service。bool 风格（digit/word）已登记
    # 进 WELFARE_CONFIG_SCHEMA 的 spec，normalize 口径与今天逐字一致。
    values = {key: value for key, value in data.items() if key in WELFARE_CONFIG_KEYS}
    if values:
        # 校验失败抛 ConfigValueError，由 main.py 的统一 handler 转 400
        config_service.save_config(db, values, schema="welfare")
    updated = list(values)
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
    chat_id: Optional[int] = None  # 可选；不传则从 lottery_group_ids 自动解析
    prizes: List[PrizeItem]
    draw_at: Optional[str] = None
    max_participants: Optional[int] = None
    lottery_type: str = "button"  # button=按钮抽奖 / password=口令抽奖
    password_keyword: Optional[str] = None  # 口令抽奖时的关键词


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
            SELECT r.id, r.title, r.chat_id, r.status, r.lottery_type, r.draw_at, r.created_at,
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
                "lottery_type": d.get("lottery_type") or "button",
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
    effective_chat_id = req.chat_id
    if effective_chat_id is None:
        # 未显式传群 ID：从允许群名单自动解析。名单恰好一个群才敢替用户做主，
        # 否则必须先到功能开关里把允许群 ID 设成一个群。
        allowed_from_cfg = lottery_module.allowed_group_ids(db)
        if len(allowed_from_cfg) == 1:
            effective_chat_id = next(iter(allowed_from_cfg))
        else:
            raise HTTPException(status_code=400, detail="请先在功能开关里把允许群ID设置为一个群，再来新建活动")
    if not effective_chat_id:
        raise HTTPException(status_code=400, detail="群 ID 不能为空")
    if not req.prizes:
        raise HTTPException(status_code=400, detail="至少需要配置 1 个奖品")
    for item in req.prizes:
        if item.type not in ("days", "points", "whitelist"):
            raise HTTPException(status_code=400, detail="奖品类型只能是 days/points/whitelist")
        if item.type in ("points", "days"):
            # 积分/天数奖品 value=0 发奖时必失败（distribute 记 errors），创建时直接拦截
            if item.value <= 0:
                label = "积分" if item.type == "points" else "天数"
                raise HTTPException(status_code=400, detail=f"{label}奖品的价值必须大于 0")
        elif item.value < 0:
            raise HTTPException(status_code=400, detail="奖品价值不能为负数")
        if item.quantity < 1:
            raise HTTPException(status_code=400, detail="奖品数量至少为 1")
    dt = None
    if req.draw_at:
        try:
            dt = datetime.fromisoformat(req.draw_at)
            # 用户侧时间口径：按 Asia/Shanghai naive 入库（前端 toISOString 发的是 UTC，
            # aware 转上海去 tzinfo；naive 按 UTC 理解再转上海，见 lottery.to_shanghai_naive）
            dt = lottery_module.to_shanghai_naive(dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="draw_at 时间格式无效")
    if req.max_participants is not None and req.max_participants < 1:
        raise HTTPException(status_code=400, detail="最大参与人数至少为 1")
    if req.lottery_type not in ("button", "password"):
        raise HTTPException(status_code=400, detail="抽奖类型只能是 button 或 password")
    if req.lottery_type == "password" and not (req.password_keyword or "").strip():
        raise HTTPException(status_code=400, detail="口令抽奖必须设置口令关键词")
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
        if allowed_ids and effective_chat_id not in allowed_ids:
            raise HTTPException(status_code=403, detail="该群未被允许")
    # 同群已有进行中的活动则拒绝
    active = lottery_module.get_active_round(db, effective_chat_id)
    if active:
        raise HTTPException(status_code=400, detail="该群已有进行中的抽奖活动")
    try:
        result = lottery_module.create_round(
            db=db,
            title=req.title,
            chat_id=effective_chat_id,
            prizes=[item.model_dump() for item in req.prizes],
            draw_at=dt,
            max_participants=req.max_participants,
            created_by=current_admin.id,
            lottery_type=req.lottery_type,
            password_keyword=req.password_keyword,
        )
    except IntegrityError:
        # R49：上面的读-查-写拦不住并发双建；uq_lottery_rounds_open_chat
        # （chat_id WHERE status='open'）在 DB 层拦下第二个，PG 抛唯一冲突
        db.rollback()
        raise HTTPException(status_code=400, detail="该群已有进行中的抽奖活动")
    _audit(db, current_admin.id, "lottery_round_create", "lottery_round", result.id, {"title": req.title, "chat_id": effective_chat_id, "lottery_type": req.lottery_type})
    db.commit()
    # 新活动创建后通知到群（之前漏掉了，群里没人知道有新抽奖）。
    # 通知失败不影响创建结果，只记日志。
    try:
        notify_result = lottery_module.notify_new_round(db, result.id)
    except Exception:
        notify_result = None
        logger.exception("群抽奖新活动通知失败 round_id=%s（不影响创建结果）", result.id)
    return {
        "id": result.id,
        "title": result.title,
        "chat_id": effective_chat_id,
        "status": result.status,
        "lottery_type": result.lottery_type,
        "password_keyword": result.password_keyword,
        "seed_hash": result.seed_hash,
        "draw_at": result.draw_at.isoformat() if result.draw_at else None,
        "created_at": result.created_at.isoformat() if result.created_at else None,
        "notify": notify_result,
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
    status = dict(row._mapping).get("status")
    if status == "done":
        # 已开奖但有中奖者未发放（上次发奖中途失败）：只重跑发放。
        # distribute_round 逐条原子认领，已发放的跳过，不会重复发奖。
        if round_id not in lottery_module.pending_distribution_round_ids(db):
            raise HTTPException(status_code=400, detail="该活动已开奖且奖品已全部发放")
        distribute_result = lottery_module.distribute_round(db, round_id)
        _audit(db, current_admin.id, "lottery_round_redistribute", "lottery_round", round_id, {"distributed": distribute_result.get("distributed", 0)})
        db.commit()
        winners = lottery_module.draw_round(db, round_id)
        return {
            "success": True,
            "redistributed": True,
            "winners": [
                {"id": w.id, "entry_id": w.entry_id, "prize_id": w.prize_id, "distributed": bool(w.distributed)}
                for w in (winners if isinstance(winners, list) else [])
            ],
            "distribute": distribute_result,
        }
    if status != "open":
        raise HTTPException(status_code=400, detail="只能对进行中的活动开奖")
    # 原子认领（与自动开奖调度 run_due_draws 同一口径）：只看 status 再开奖是读-判-写，
    # 管理员手动开奖与调度/重复点击并发时会两边都开奖、两边都发奖
    claimed = db.execute(
        text("UPDATE lottery_rounds SET status='drawing', drawing_at=:now WHERE id=:id AND status='open'"),
        {"id": round_id, "now": datetime.now()},
    ).rowcount
    db.commit()
    if not claimed:
        raise HTTPException(status_code=409, detail="该活动正在开奖或已开奖，请刷新后查看")
    try:
        winners = lottery_module.draw_round(db, round_id)
        distribute_result = lottery_module.distribute_round(db, round_id)
    except Exception as exc:
        db.rollback()
        # 与自动开奖 run_due_draws 同一口径：认领后开奖失败要把状态打回 open，
        # 否则轮次永久卡死在 drawing（调度只扫 open、管理后台拒绝非 open 重试）。
        # draw 已提交（status=done）但 distribute 失败时 WHERE drawing 匹配不到行，
        # 不会误删开奖结果；未发放的中奖者走上面的 done+pending 路径补发。
        # rowcount 区分三种结局，保证返回给管理员的文案与实际状态一致
        # （之前三种结局都写"已恢复为可重试状态"，回滚失败/已 done 时与事实不符）。
        reset_done = False
        reset_failed = False
        try:
            reset_done = (
                db.execute(
                    text("UPDATE lottery_rounds SET status='open', drawing_at=NULL WHERE id=:id AND status='drawing'"),
                    {"id": round_id},
                ).rowcount
                > 0
            )
            db.commit()
        except Exception:
            db.rollback()
            reset_failed = True
            logger.exception("群抽奖手动开奖状态回滚失败 round_id=%s", round_id)
        if reset_failed:
            detail = f"开奖失败：{exc}，且状态回滚失败（轮次可能卡在 drawing），请人工检查"
        elif reset_done:
            detail = f"开奖失败：{exc}，已恢复为可重试状态"
        else:
            detail = f"开奖失败：{exc}，开奖结果已保留，未发放的奖品可在轮次页重试补发"
        raise HTTPException(status_code=500, detail=detail)
    # 手动开奖也要通知到群（与自动开奖 run_due_draws 同一口径）。
    # 之前漏掉了 notify 调用，导致管理后台点"开奖"后群里收不到开奖公告。
    # 通知失败不影响开奖结果，只记日志。
    try:
        notify_result = lottery_module.notify_draw_results(
            db, round_id, winners, distribute_errors=distribute_result.get("errors")
        )
    except Exception:
        notify_result = None
        logger.exception("群抽奖手动开奖通知失败 round_id=%s（不影响开奖结果）", round_id)
    _audit(db, current_admin.id, "lottery_round_draw", "lottery_round", round_id, {"winner_count": len(winners) if isinstance(winners, list) else 0})
    db.commit()
    return {
        "success": True,
        "winners": [
            {"id": w.id, "entry_id": w.entry_id, "prize_id": w.prize_id, "distributed": bool(w.distributed)}
            for w in (winners if isinstance(winners, list) else [])
        ],
        "distribute": distribute_result,
        "notify": notify_result,
    }


@admin_router.post("/welfare/lottery/rounds/{round_id}/cancel")
def cancel_lottery_round(
    round_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """取消进行中的群抽奖活动。"""
    _get_lottery()
    # P2 修复（完美审查 B-P2-2）：条件 UPDATE 原子取消，与调度器 run_due_draws
    # 的 UPDATE ... WHERE status='open' 原子认领同一口径——否则管理员取消与
    # 自动开奖竞态时，"奖已发出但轮次显示已取消"。
    result = db.execute(
        text("UPDATE lottery_rounds SET status = 'cancelled' "
             "WHERE id = :id AND status = 'open'"),
        {"id": round_id},
    )
    if result.rowcount == 1:
        _audit(db, current_admin.id, "lottery_round_cancel", "lottery_round", round_id, {})
        db.commit()
        return {"success": True}
    db.rollback()
    row = db.execute(
        text("SELECT id FROM lottery_rounds WHERE id = :id"), {"id": round_id}
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="抽奖活动不存在")
    raise HTTPException(status_code=409, detail="该活动正在开奖或已开奖，无法取消")


# ==================== 等级治理 Step 3：活动提案审批 ====================
# 用户端提交的提案（POST /api/lottery/proposals）由管理员在这里审批：
# 批准 -> 建轮次（created_by=提案人）并走正常开奖流程；拒绝 -> 不产生任何东西。
# 批准/拒绝另写 AdminLog（lottery_proposal_approve/reject）；提案行本身也是审计记录。


class ProposalReviewRequest(BaseModel):
    note: Optional[str] = None


@admin_router.get("/welfare/lottery/proposals")
def list_lottery_proposals(
    status: Optional[str] = Query(None, description="pending/approved/rejected"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """分页列出活动提案（默认倒序，可按状态过滤）。"""
    total, items = lottery_governance.list_proposals(
        db, user_id=None, status=status, page=page, page_size=page_size
    )
    return {"total": total, "items": items}


@admin_router.post("/welfare/lottery/proposals/{proposal_id}/approve")
def approve_lottery_proposal(
    proposal_id: int,
    req: ProposalReviewRequest = ProposalReviewRequest(),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """批准活动提案：建轮次并关联提案，轮次走正常开奖流程。"""
    admin_id = current_admin.id
    try:
        proposal, round_obj = lottery_governance.approve_proposal(
            db, admin_id, proposal_id, note=req.note
        )
    except lottery_governance.LotteryGovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    _audit(db, admin_id, "lottery_proposal_approve", "lottery_proposal", proposal.id,
           {"round_id": round_obj.id, "proposer_id": proposal.user_id})
    db.commit()
    try:
        notify = _get_lottery().notify_new_round(db, round_obj.id)
    except Exception:  # noqa: BLE001
        notify = {"ok": False}
    return {
        "proposal": lottery_governance.proposal_to_dict(proposal),
        "round": {
            "id": round_obj.id,
            "title": round_obj.title,
            "chat_id": round_obj.chat_id,
            "status": round_obj.status,
            "lottery_type": round_obj.lottery_type,
            "seed_hash": round_obj.seed_hash,
        },
        "notify": notify,
    }


@admin_router.post("/welfare/lottery/proposals/{proposal_id}/reject")
def reject_lottery_proposal(
    proposal_id: int,
    req: ProposalReviewRequest = ProposalReviewRequest(),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """拒绝活动提案：只改提案行，不产生轮次与奖品。"""
    admin_id = current_admin.id
    try:
        proposal = lottery_governance.reject_proposal(
            db, admin_id, proposal_id, note=req.note
        )
    except lottery_governance.LotteryGovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    _audit(db, admin_id, "lottery_proposal_reject", "lottery_proposal", proposal.id,
           {"proposer_id": proposal.user_id})
    db.commit()
    return lottery_governance.proposal_to_dict(proposal)


# ==================== 三重门·资格门：黑名单管理 ====================

class LotteryBlacklistAdd(BaseModel):
    user_id: int = Field(..., gt=0)
    reason: str = Field("", max_length=200)


@admin_router.get("/welfare/lottery/blacklist")
def list_lottery_blacklist(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """抽奖黑名单列表（三重门·资格门）。"""
    total = db.query(models.LotteryBlacklist).count()
    rows = (
        db.query(models.LotteryBlacklist)
        .order_by(models.LotteryBlacklist.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = []
    # r49: 一次 IN 查完用户名（防 N+1：此前循环内每行一次 query）
    user_ids = [r.user_id for r in rows]
    name_by_id = {
        u.id: u.username
        for u in db.query(models.WebUser.id, models.WebUser.username)
        .filter(models.WebUser.id.in_(user_ids))
        .all()
    } if user_ids else {}
    for r in rows:
        items.append({
            "id": r.id,
            "user_id": r.user_id,
            "username": name_by_id.get(r.user_id),
            "reason": r.reason,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@admin_router.post("/welfare/lottery/blacklist")
def add_lottery_blacklist(
    body: LotteryBlacklistAdd,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """把用户加入抽奖黑名单（三重门·资格门）。"""
    u = db.query(models.WebUser).filter(models.WebUser.id == body.user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    exists = (
        db.query(models.LotteryBlacklist)
        .filter(models.LotteryBlacklist.user_id == body.user_id)
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="该用户已在黑名单中")
    rec = models.LotteryBlacklist(
        user_id=body.user_id,
        reason=body.reason or None,
        created_by=current_admin.id,
    )
    db.add(rec)
    _audit(db, current_admin.id, "lottery_blacklist_add", "lottery_blacklist", body.user_id,
           {"reason": body.reason})
    db.commit()
    return {"success": True, "id": rec.id}


@admin_router.delete("/welfare/lottery/blacklist/{user_id}")
def remove_lottery_blacklist(
    user_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """把用户移出抽奖黑名单（三重门·资格门）。"""
    rec = (
        db.query(models.LotteryBlacklist)
        .filter(models.LotteryBlacklist.user_id == user_id)
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="该用户不在黑名单中")
    db.delete(rec)
    _audit(db, current_admin.id, "lottery_blacklist_remove", "lottery_blacklist", user_id, {})
    db.commit()
    return {"success": True}


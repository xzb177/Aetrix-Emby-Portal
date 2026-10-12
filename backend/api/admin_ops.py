"""
管理后台运营 API（卡码体系 / 设备风控 / 登录与安全日志）

借鉴 twilight-kotomi 的运营面板核心运营能力，与 `backend/api/admin.py`
（用户、套餐、经济、审计等既有管理面）分文件维护，避免单文件继续膨胀。

- 卡码：类型化生成（注册码 / 续期码 / 白名单码 / 诱饵码 / 指名码）、总览统计、停用与回收
- 设备：跨用户设备审查、封禁（同时吊销该设备令牌）、移除（踢下线）
- 日志：登录成功/失败、设备超限、诱饵码触发等风控事件筛选与清理

鉴权与管理端完全一致：复用 `get_current_admin`（is_staff 的 WebUser）+ JWT。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from backend import (authlog, codes, devices, library_scope, models, promotion,
                     realms, share_guard)
from backend import access_guard  # noqa: E402  - 与其余模块同一行导入
from backend.api.invitation import _generate_invite_code
from backend.api.admin_core import _audit, get_current_admin
from backend.api.pagination import pagination_params
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_ops_router = APIRouter(prefix="/api/admin", tags=["管理后台·运营"])


# ==================== 卡码体系 ====================


def _code_state(code: models.RegistrationCode, now: datetime) -> str:
    if not code.is_active:
        return "disabled"
    if code.expires_at and code.expires_at < now:
        return "expired"
    if code.max_uses and (code.use_count or 0) >= code.max_uses:
        return "used_up"
    return "active"


def _code_state_sql(now: datetime):
    """F-P1-7：_code_state 的 SQL 等价表达式（CASE），供状态过滤/统计下推用。

    口径与 Python 版严格一致：
    disabled = is_active 假；expired = expires_at 非空且已过；
    used_up = max_uses>0 且 use_count>=max_uses；其余 active。
    NULL 与 False 都判停用（isnot(True)），与 Python `not is_active` 口径一致（F-P1-7 NULL 对齐修复）。
    """
    return case(
        (models.RegistrationCode.is_active.isnot(True), "disabled"),
        (
            and_(
                models.RegistrationCode.expires_at.isnot(None),
                models.RegistrationCode.expires_at < now,
            ),
            "expired",
        ),
        (
            and_(
                models.RegistrationCode.max_uses.isnot(None),
                models.RegistrationCode.max_uses > 0,
                func.coalesce(models.RegistrationCode.use_count, 0)
                >= models.RegistrationCode.max_uses,
            ),
            "used_up",
        ),
        else_="active",
    )


def _code_usernames(db: Session, code: models.RegistrationCode) -> list:
    ids = [int(i) for i in str(code.used_by or "").split(",") if str(i).strip().isdigit()]
    if not ids:
        return []
    rows = db.query(models.WebUser.id, models.WebUser.username).filter(
        models.WebUser.id.in_(ids)
    ).all()
    return [{"id": r[0], "username": r[1]} for r in rows]


def code_dto(db: Session, code: models.RegistrationCode, now: Optional[datetime] = None,
             realm_names: Optional[dict] = None,
             used_by_map: Optional[dict] = None) -> dict:
    """used_by_map：F-P1-7 批量预取的 {code_id: used_by 用户列表}，传了就不再逐码查库。"""
    now = now or datetime.now()
    code_type = int(code.code_type or codes.CODE_TYPE_REGISTER)
    days = code.days if code.days is not None else codes.DEFAULT_DAYS
    if realm_names is None:
        realm_names = {r.id: r.name for r in realms.list_realms(db)}
    return {
        "id": code.id,
        "code": code.code,
        # 卡码是一个服一个的：它开的是哪个服的会员，列表里得看得出来
        "realm_id": code.realm_id,
        "realm_name": realm_names.get(code.realm_id, "") if code.realm_id else "",
        "code_type": code_type,
        "code_type_name": codes.CODE_TYPE_NAMES.get(code_type, "卡码"),
        "days": days,
        "days_text": codes.format_days(days),
        "is_permanent": days < 0,
        "is_decoy": codes.is_honeypot(code),
        "target_username": code.target_username or "",
        "source": code.source or "admin",
        "max_uses": code.max_uses,
        "use_count": code.use_count or 0,
        "is_active": bool(code.is_active),
        "state": _code_state(code, now),
        "note": code.note,
        "expires_at": code.expires_at.isoformat() if code.expires_at else None,
        "used_by": used_by_map.get(code.id) if used_by_map is not None else _code_usernames(db, code),
        "created_at": code.created_at.isoformat() if code.created_at else None,
    }


@admin_ops_router.get("/registration-codes/list")
def list_codes(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    code_type: Optional[int] = None,
    state: str = "",
    keyword: str = "",
    realm_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
):
    """卡码列表（类型 / 状态 / 关键字 / 归属服筛选 + 分页）

    卡码是一个服一个的：``realm_id=0`` 看全部服，不传看当前服。
    """
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    query = realms.scope(db.query(models.RegistrationCode),
                         models.RegistrationCode.realm_id, scope_id)
    if code_type:
        query = query.filter(models.RegistrationCode.code_type == code_type)
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        query = query.filter(or_(
            models.RegistrationCode.code.ilike(like),
            models.RegistrationCode.note.ilike(like),
            models.RegistrationCode.target_username.ilike(like),
        ))

    now = datetime.now()
    # F-P1-7：状态过滤下推 SQL（CASE 与 _code_state 同口径），再走 SQL 分页。
    # 原实现全量 .all() 后内存过滤+切片：limit=1 也要把全部匹配行传进内存。
    if state:
        if state not in ("active", "disabled", "expired", "used_up"):
            raise HTTPException(status_code=400, detail=f"未知状态：{state}")
        query = query.filter(_code_state_sql(now) == state)
    # 本接口历史上限 300（卡码批量管理场景），保持不变；只修"读全量"问题
    limit, offset = pagination_params(limit, offset, default_limit=100, max_limit=300)
    total = query.count()
    page = (
        query.order_by(models.RegistrationCode.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    realm_names = {r.id: r.name for r in realms.list_realms(db)}
    # code_dto 内逐码查 used_by 用户名也是 N+1：本页一次查完
    _used_ids: list[int] = []
    for c in page:
        _used_ids.extend(
            int(i) for i in str(c.used_by or "").split(",") if str(i).strip().isdigit()
        )
    _uname = (
        {
            r[0]: r[1]
            for r in db.query(models.WebUser.id, models.WebUser.username)
            .filter(models.WebUser.id.in_(_used_ids or [-1]))
            .all()
        }
        if _used_ids
        else {}
    )
    used_by_map = {
        c.id: [
            {"id": i, "username": _uname[i]}
            for i in (
                int(x) for x in str(c.used_by or "").split(",") if str(x).strip().isdigit()
            )
            if i in _uname
        ]
        for c in page
    }
    return {
        "total": total,
        "realm_id": scope_id,
        "realm_name": realms.get_realm(db, scope_id).name if scope_id else "全部服",
        "realms": [{"id": r.id, "name": r.name} for r in realms.list_realms(db)],
        "codes": [code_dto(db, c, now, realm_names, used_by_map) for c in page],
    }


@admin_ops_router.get("/registration-codes/stats")
def code_stats(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    realm_id: Optional[int] = None,
):
    """卡码总览：按类型统计 + 诱饵命中 + 累计授予天数（按服；``realm_id=0`` 为全部服）"""
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    base = realms.scope(db.query(models.RegistrationCode),
                        models.RegistrationCode.realm_id, scope_id)
    now = datetime.now()
    # F-P1-7：统计下推 SQL（原全量 .all() 后 Python 循环）。
    # 诱饵码判定：is_honeypot 看 code.upper().startswith("HONEY-")，SQL 等价写法；
    # code_type 口径与 Python ``c.code_type or CODE_TYPE_REGISTER`` 一致（0/NULL→1）。
    state_expr = _code_state_sql(now)
    is_decoy = func.upper(models.RegistrationCode.code).like("HONEY-%")
    days_col = func.coalesce(models.RegistrationCode.days, codes.DEFAULT_DAYS)
    used_col = func.coalesce(models.RegistrationCode.use_count, 0)
    type_col = func.coalesce(
        func.nullif(models.RegistrationCode.code_type, 0), codes.CODE_TYPE_REGISTER
    )

    agg = (
        base.with_entities(
            func.count().label("total"),
            func.sum(case((state_expr == "active", 1), else_=0)).label("active"),
            func.sum(case((state_expr == "disabled", 1), else_=0)).label("disabled"),
            func.sum(case((state_expr == "expired", 1), else_=0)).label("expired"),
            func.sum(case((state_expr == "used_up", 1), else_=0)).label("used_up"),
            func.sum(case((is_decoy, 1), else_=0)).label("decoy_total"),
            func.sum(case((and_(is_decoy, used_col > 0), 1), else_=0)).label("decoy_triggered"),
            func.sum(
                case((and_(~is_decoy, days_col > 0), days_col * used_col), else_=0)
            ).label("days_granted"),
        ).first()
    )
    type_rows = (
        base.with_entities(
            type_col.label("code_type"),
            func.count().label("total"),
            func.sum(used_col).label("used"),
            func.sum(case((state_expr == "active", 1), else_=0)).label("available"),
        )
        .group_by(type_col)
        .all()
    )
    by_type = [
        {
            "code_type": int(r.code_type),
            "code_type_name": codes.CODE_TYPE_NAMES.get(int(r.code_type), "卡码"),
            "total": int(r.total or 0),
            "used": int(r.used or 0),
            "available": int(r.available or 0),
        }
        for r in type_rows
    ]

    return {
        "total": int(agg.total or 0),
        "active": int(agg.active or 0),
        "disabled": int(agg.disabled or 0),
        "expired": int(agg.expired or 0),
        "used_up": int(agg.used_up or 0),
        "decoy": {"total": int(agg.decoy_total or 0),
                  "triggered": int(agg.decoy_triggered or 0)},
        "days_granted": int(agg.days_granted or 0),
        "realm_id": scope_id,
        "realm_name": realms.get_realm(db, scope_id).name if scope_id else "全部服",
        "by_type": sorted(by_type, key=lambda x: x["code_type"]),
    }


class CodeGenerateRequest(BaseModel):
    """类型化卡码生成"""
    code_type: int = Field(default=codes.CODE_TYPE_REGISTER, ge=1, le=3)
    count: int = Field(default=1, ge=1, le=200)
    days: Optional[int] = Field(default=None, ge=-1, le=36500)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_days: int = Field(default=30, ge=1, le=3650)
    algorithm: str = codes.DEFAULT_ALGORITHM
    is_decoy: bool = False
    target_username: str = ""
    note: str = ""
    # 这张卡码开通哪个服的会员（留空 = 当前服）
    realm_id: Optional[int] = None
    # M1：奖励类型
    reward_type: str = Field(default="subscription")
    points_value: int = Field(default=0, ge=0)
    discount_pct: int = Field(default=0, ge=0, le=100)


@admin_ops_router.post("/registration-codes/generate")
def generate_codes(
    request: CodeGenerateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """批量生成类型化卡码"""
    if request.algorithm not in codes.RANDOM_ALGORITHMS:
        raise HTTPException(status_code=400, detail="随机算法不支持")

    days = request.days if request.days is not None else codes.DEFAULT_DAYS
    if request.code_type == codes.CODE_TYPE_WHITELIST:
        days = -1  # 白名单码语义即长期有效（与 codes.grant_days_for 口径一致）

    target_username = (request.target_username or "").strip()
    if target_username:
        exists = db.query(models.WebUser).filter(
            func.lower(models.WebUser.username) == target_username.lower()
        ).first()
        if not exists:
            raise HTTPException(status_code=400, detail=f"指名账号不存在：{target_username}")

    target_realm = realms.claim(db, request.realm_id)
    if not realms.get_realm(db, target_realm):
        raise HTTPException(status_code=400, detail=f"服不存在: #{target_realm}")

    expires_at = datetime.now() + timedelta(days=request.expires_days)
    created = []
    for index in range(1, request.count + 1):
        raw = ""
        for _attempt in range(20):
            raw = codes.render_code(
                code_type=request.code_type, days=days, index=index,
                algorithm=request.algorithm, decoy=bool(request.is_decoy),
            )
            if not db.query(models.RegistrationCode).filter(
                models.RegistrationCode.code == raw
            ).first():
                break
        else:
            raise HTTPException(status_code=500, detail="卡码生成冲突，请重试")

        # M1：校验奖励类型
        if request.reward_type not in ("subscription", "points", "discount"):
            raise HTTPException(status_code=400, detail="reward_type 非法")
        # 交叉校验「奖励类型 × 面额」：坏卡一旦入库，用户核销时才会失败
        # （见 codes.redeem_code 里的 points<=0 / pct<=0 or pct>=100 判断），
        # 不如在生成阶段就拦截；口径与 admin.economy_create_exchange_codes 一致。
        if request.reward_type == "points" and request.points_value <= 0:
            raise HTTPException(status_code=400, detail="积分型卡码必须设置积分面额（points_value > 0）")
        if request.reward_type == "discount" and not (1 <= request.discount_pct <= 99):
            raise HTTPException(status_code=400, detail="折扣型卡码的 discount_pct 必须在 1-99 之间（85=八五折）")
        code = models.RegistrationCode(
            code=raw,
            max_uses=request.max_uses,
            use_count=0,
            is_active=True,
            note=request.note or None,
            expires_at=expires_at,
            created_by=current_admin.id,
            code_type=request.code_type,
            days=days,
            target_username=target_username or None,
            source="admin",
            realm_id=target_realm,
            reward_type=request.reward_type,
            points_value=request.points_value,
            discount_pct=request.discount_pct,
        )
        db.add(code)
        created.append(code)

    db.commit()
    for c in created:
        db.refresh(c)

    type_name = codes.CODE_TYPE_NAMES.get(request.code_type, "卡码")
    realm_name = codes.realm_label(db, target_realm)
    _audit(db, current_admin, "generate_registration_codes", "registration_code",
           created[0].id if created else None,
           {"count": request.count, "code_type": request.code_type, "days": days,
            "is_decoy": request.is_decoy, "target_username": target_username or None,
            "realm_id": target_realm})
    db.commit()

    return {
        "success": True,
        "message": f"已生成 {len(created)} 个{type_name}（「{realm_name}」的会员）",
        "realm_id": target_realm,
        "realm_name": realm_name,
        "codes": [
            {"id": c.id, "code": c.code, "days_text": codes.format_days(days),
             "expires_at": c.expires_at.isoformat()}
            for c in created
        ],
    }


class CodeUpdateRequest(BaseModel):
    is_active: Optional[bool] = None
    note: Optional[str] = None
    # 改归属服：改之前先确认（这张码还没被用过才安全）
    realm_id: Optional[int] = None


@admin_ops_router.patch("/registration-codes/{code_id}")
def update_code(
    code_id: int,
    request: CodeUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """修改卡码：启用 / 停用 / 备注"""
    code = db.query(models.RegistrationCode).filter(
        models.RegistrationCode.id == code_id
    ).first()
    if not code:
        raise HTTPException(status_code=404, detail="卡码不存在")

    if request.is_active is not None:
        code.is_active = request.is_active
    if request.note is not None:
        code.note = request.note or None
    if request.realm_id is not None and int(request.realm_id) != int(code.realm_id or 0):
        if not realms.get_realm(db, request.realm_id):
            raise HTTPException(status_code=400, detail=f"服不存在: #{request.realm_id}")
        if code.use_count:
            # 已核销的卡码换服会让「开通的是哪个服的会员」对不上账
            raise HTTPException(status_code=400, detail="该卡码已被使用，不能改归属服")
        code.realm_id = int(request.realm_id)
    db.commit()

    _audit(db, current_admin, "update_registration_code", "registration_code", code.id,
           {"is_active": code.is_active, "note": code.note, "realm_id": code.realm_id})
    db.commit()
    return {"success": True, "code": code_dto(db, code)}


@admin_ops_router.delete("/registration-codes/{code_id}")
def delete_code(
    code_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """删除卡码：已核销的保留审计只允许停用

    P0-2 修复：检查与删除必须是原子的（同 economy_delete_exchange_code）。
    用带 COALESCE(use_count,0)=0 条件的原子 DELETE，防并发核销穿透删除。
    """
    code_row = db.query(models.RegistrationCode.code).filter(
        models.RegistrationCode.id == code_id
    ).first()
    if not code_row:
        raise HTTPException(status_code=404, detail="卡码不存在")

    deleted = (
        db.query(models.RegistrationCode)
        .filter(
            models.RegistrationCode.id == code_id,
            func.coalesce(models.RegistrationCode.use_count, 0) == 0,
        )
        .delete(synchronize_session=False)
    )
    if deleted == 0:
        # 行存在但 use_count > 0：并发核销在我们前面检查后使用了它
        db.rollback()
        raise HTTPException(status_code=400, detail="该卡码已被使用，只能停用不能删除")

    _audit(db, current_admin, "delete_registration_code", "registration_code", code_id,
           {"code": code_row[0]})
    db.commit()
    return {"success": True, "message": "卡码已删除"}


# ==================== 设备风控 ====================


@admin_ops_router.get("/devices/stats")
def device_stats(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    cutoff = datetime.now() - timedelta(days=devices.ACTIVE_DEVICE_DAYS)
    total = db.query(func.count(models.UserDevice.id)).scalar() or 0
    active = db.query(func.count(models.UserDevice.id)).filter(
        models.UserDevice.is_blocked.is_(False),
        models.UserDevice.last_seen_at >= cutoff,
    ).scalar() or 0
    blocked = db.query(func.count(models.UserDevice.id)).filter(
        models.UserDevice.is_blocked.is_(True)
    ).scalar() or 0
    users = db.query(func.count(func.distinct(models.UserDevice.user_id))).scalar() or 0
    return {
        "total": int(total),
        "active_30d": int(active),
        "blocked": int(blocked),
        "users": int(users),
        "limit_per_user": devices.device_limit(db),
        "auto_evict": devices.device_auto_evict(db),
        "active_days": devices.ACTIVE_DEVICE_DAYS,
    }


@admin_ops_router.get("/devices")
def list_all_devices(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    user_id: Optional[int] = None,
    keyword: str = "",
    only_blocked: bool = False,
    limit: int = 100,
    offset: int = 0,
):
    """跨用户设备审查"""
    query = db.query(models.UserDevice, models.WebUser).outerjoin(
        models.WebUser, models.WebUser.id == models.UserDevice.user_id
    )
    if user_id:
        query = query.filter(models.UserDevice.user_id == user_id)
    if only_blocked:
        query = query.filter(models.UserDevice.is_blocked.is_(True))

    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        query = query.filter(or_(
            models.UserDevice.device_id.ilike(like),
            models.UserDevice.name.ilike(like),
            models.UserDevice.client.ilike(like),
            models.UserDevice.ip.ilike(like),
            models.WebUser.username.ilike(like),
        ))

    total = query.count()
    rows = query.order_by(models.UserDevice.last_seen_at.desc()).offset(
        max(offset, 0)
    ).limit(min(max(limit, 1), 300)).all()

    cutoff = datetime.now() - timedelta(days=devices.ACTIVE_DEVICE_DAYS)
    items = []
    for device, user in rows:
        dto = devices.device_dto(device)
        dto["user_id"] = device.user_id
        dto["username"] = user.username if user else f"用户 #{device.user_id}"
        dto["is_user_active"] = bool(user.is_active) if user else False
        dto["is_online_recent"] = bool(
            device.last_seen_at is None or device.last_seen_at >= cutoff
        )
        items.append(dto)
    return {"total": total, "devices": items}


class DeviceBlockRequest(BaseModel):
    is_blocked: bool


@admin_ops_router.put("/devices/{device_id}")
def update_device(
    device_id: str,
    request: DeviceBlockRequest,
    user_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """封禁 / 解封设备（封禁同时吊销该设备令牌）"""
    device = db.query(models.UserDevice).filter(
        models.UserDevice.user_id == user_id,
        models.UserDevice.device_id == device_id,
    ).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    # F-P1-8 修复：吊销令牌与封禁状态变更同事务。原先先 commit 封禁、再调
    # revoke_device_tokens（内部又 commit 一次）：吊销抛错时会留下"封禁已生效
    # 但令牌未吊销、审计缺失"的部分成功状态。这里内联 UPDATE，只在末尾提交一次。
    device.is_blocked = request.is_blocked
    if request.is_blocked:
        from backend.emby_server import models as em
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == user_id,
            em.EmbyApiToken.device_id == device_id,
        ).update({"is_revoked": True}, synchronize_session=False)

    _audit(db, current_admin, "update_device", "user_device", device.id,
           {"device_id": device_id, "user_id": user_id, "is_blocked": request.is_blocked})
    db.commit()
    return {
        "success": True,
        "message": "设备已封禁，该设备需重新登录且会被拒绝" if request.is_blocked else "设备已解封",
    }


@admin_ops_router.delete("/devices/{device_id}")
def delete_device(
    device_id: str,
    user_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """移除设备并吊销令牌（踢下线）"""
    ok = devices.remove_device(db, user_id, device_id, revoke_tokens=True)
    if not ok:
        raise HTTPException(status_code=404, detail="设备不存在")
    _audit(db, current_admin, "remove_device", "user_device", None,
           {"device_id": device_id, "user_id": user_id})
    db.commit()
    return {"success": True, "message": "设备已移除，对应客户端需重新登录"}


# ==================== 登录 / 安全日志 ====================


@admin_ops_router.get("/login-logs")
def list_login_logs(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    username: str = "",
    ip: str = "",
    reason: str = "",
    success: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
):
    """登录与风控事件日志"""
    query = db.query(models.LoginLog)
    kw = (username or "").strip()
    if kw:
        query = query.filter(models.LoginLog.username.ilike(f"%{kw}%"))
    ip_kw = (ip or "").strip()
    if ip_kw:
        query = query.filter(models.LoginLog.ip.ilike(f"%{ip_kw}%"))
    if reason:
        query = query.filter(models.LoginLog.reason == reason)
    if success is not None:
        query = query.filter(models.LoginLog.success.is_(success))

    total = query.count()
    rows = query.order_by(models.LoginLog.created_at.desc()).offset(
        max(offset, 0)
    ).limit(min(max(limit, 1), 300)).all()

    day_ago = datetime.now() - timedelta(hours=24)
    failed_24h = db.query(func.count(models.LoginLog.id)).filter(
        models.LoginLog.success.is_(False),
        models.LoginLog.created_at >= day_ago,
    ).scalar() or 0
    risk_24h = db.query(func.count(models.LoginLog.id)).filter(
        models.LoginLog.reason.in_(["device_limit", "decoy_code"]),
        models.LoginLog.created_at >= day_ago,
    ).scalar() or 0

    return {
        "total": total,
        "summary": {"failed_24h": int(failed_24h), "risk_24h": int(risk_24h)},
        "reasons": [{"value": k, "label": v} for k, v in authlog.REASONS.items()],
        "logs": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "ip": r.ip,
                "region": r.region or "",
                "user_agent": r.user_agent,
                "success": bool(r.success),
                "reason": r.reason,
                "reason_label": authlog.REASONS.get(r.reason or "", r.reason or "-"),
                "detail": r.detail,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


class LogPurgeRequest(BaseModel):
    days: Optional[int] = Field(default=None, ge=0, le=3650)


@admin_ops_router.post("/login-logs/purge")
def purge_login_logs(
    request: LogPurgeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """按保留天数清理日志（days=0 清空）"""
    if request.days == 0:
        deleted = int(db.query(models.LoginLog).delete(synchronize_session=False) or 0)
        db.commit()
    else:
        deleted = authlog.purge_old(db, request.days)

    _audit(db, current_admin, "purge_login_logs", "login_log", None,
           {"days": request.days, "deleted": deleted})
    db.commit()
    return {"success": True, "message": f"已清理 {deleted} 条日志", "deleted": deleted}


# ==================== 防共享：跨城市轨迹 + 同播检测（v2.43.0） ====================
#
# 判定与处置逻辑都在 ``backend/share_guard.py``；这里只做三件事：读策略与事件、
# 写策略、清理事件。两处写端点都写审计——「谁把自动停用打开了」必须是可追溯的，
# 否则一个被误伤的用户没地方说理。


@admin_ops_router.get("/share-guard")
def get_share_guard(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    kind: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """防共享策略 + 事件流水 + 近 24h 概览（只读，运营与只读角色都能看）

    只列**判定**：基线行（``is_baseline``，只是「这个账号现在在 X 城」）是判定算法
    自己的记忆，不是异常。它留在库里（判定要靠它记住上一次的城市），但不进这一页——
    否则列表里绝大多数是「用户还在原地」，概览数字也会被它冲高到失去意义。
    """
    query = db.query(models.ShareGuardEvent).filter(
        models.ShareGuardEvent.is_baseline.is_(False)
    )
    if kind in (share_guard.KIND_TRAVEL, share_guard.KIND_CONCURRENT):
        query = query.filter(models.ShareGuardEvent.kind == kind)

    total = query.count()
    rows = (
        query.order_by(models.ShareGuardEvent.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 300))
        .all()
    )
    day_ago = datetime.now() - timedelta(hours=24)
    _recent = (
        models.ShareGuardEvent.created_at >= day_ago,
        models.ShareGuardEvent.is_baseline.is_(False),
    )
    travel_24h = db.query(func.count(models.ShareGuardEvent.id)).filter(
        *_recent, models.ShareGuardEvent.kind == share_guard.KIND_TRAVEL,
    ).scalar() or 0
    concurrent_24h = db.query(func.count(models.ShareGuardEvent.id)).filter(
        *_recent, models.ShareGuardEvent.kind == share_guard.KIND_CONCURRENT,
    ).scalar() or 0
    enforced_24h = db.query(func.count(models.ShareGuardEvent.id)).filter(
        *_recent, models.ShareGuardEvent.action == "enforce",
    ).scalar() or 0

    return {
        "success": True,
        "policy": share_guard.policy_payload(db),
        "summary": {
            "total": int(total),
            "travel_24h": int(travel_24h),
            "concurrent_24h": int(concurrent_24h),
            "enforced_24h": int(enforced_24h),
        },
        "kinds": [
            {"value": "", "label": "全部"},
            {"value": share_guard.KIND_TRAVEL, "label": share_guard.KIND_LABELS[share_guard.KIND_TRAVEL]},
            {"value": share_guard.KIND_CONCURRENT, "label": share_guard.KIND_LABELS[share_guard.KIND_CONCURRENT]},
        ],
        "events": [share_guard.event_dto(row) for row in rows],
    }


class ShareGuardPolicyRequest(BaseModel):
    policy: dict


@admin_ops_router.put("/share-guard/policy")
def update_share_guard_policy(
    payload: ShareGuardPolicyRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """写回防共享策略（只认白名单里的键，非法值保持原值不动）

    不静默降级：写入后返回**真实生效值**，前端照它回填，不自己猜。
    """
    applied = share_guard.write_policy(db, payload.policy)
    _audit(db, current_admin, "share_guard_policy_update", "system", None, applied)
    db.commit()
    return {
        "success": True,
        "applied": applied,
        "policy": share_guard.policy_payload(db),
    }


class ShareGuardPurgeRequest(BaseModel):
    days: Optional[int] = Field(default=None, ge=0, le=3650)


@admin_ops_router.post("/share-guard/purge")
def purge_share_guard_events(
    payload: ShareGuardPurgeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """清理防共享事件（days=0 = 清空全部）"""
    if payload.days == 0:
        deleted = int(db.query(models.ShareGuardEvent).delete(synchronize_session=False) or 0)
        db.commit()
    else:
        deleted = share_guard.purge_old(db, payload.days)

    _audit(db, current_admin, "purge_share_guard_events", "share_guard_event", None,
           {"days": payload.days, "deleted": deleted})
    db.commit()
    return {"success": True, "message": f"已清理 {deleted} 条防共享事件", "deleted": deleted}


# ==================== 访问拦截：UA 关键词 + IP 归属地 ====================
#
# 判定逻辑在 ``backend/access_guard.py``，这里只做存取。写端点写审计——
# 「谁把屏蔽打开了」必须可追溯：这一页的两个开关一旦打开就会**直接影响所有人
# 的访问**，而误配的表现是「用户打不开站」，事后很难自己想起来。


@admin_ops_router.get("/access-guard")
def get_access_guard(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """当前访问拦截策略（只读，运营与只读角色都能看）"""
    return {"success": True, "policy": access_guard.policy_payload(db)}


class AccessGuardPolicyRequest(BaseModel):
    policy: dict


@admin_ops_router.put("/access-guard/policy")
def update_access_guard_policy(
    payload: AccessGuardPolicyRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """写回访问拦截策略（只认白名单里的键，非法值保持原值不动）

    不静默降级：返回**真实生效值**，前端照它回填。
    """
    applied = access_guard.write_policy(db, payload.policy)
    _audit(db, current_admin, "access_guard_policy_update", "system", None, applied)
    db.commit()
    return {
        "success": True,
        "applied": applied,
        "policy": access_guard.policy_payload(db),
    }


class AccessGuardPreviewRequest(BaseModel):
    ip: str = ""
    user_agent: str = ""


@admin_ops_router.post("/access-guard/preview")
def preview_access_guard(
    payload: AccessGuardPreviewRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """用当前规则试跑一个样本 IP / UA，**不拦截任何东西**。

    开「只允许国内」这类规则之前先拿自己的 IP 跑一遍，比开完发现进不来
    再去改配置要安全得多。
    """
    _audit(db, current_admin, "access_guard_preview", "system", None,
           {"ip": payload.ip, "user_agent": payload.user_agent[:200]})
    db.commit()
    return {
        "success": True,
        "result": access_guard.preview(
            db, ip=payload.ip, user_agent=payload.user_agent
        ),
    }


# ==================== 媒体库可见范围（v2.43.0） ====================
#
# 两层规则（服务器默认范围 + 指定用户覆盖）与判定逻辑都在
# ``backend/library_scope.py``；这里只做读写与审计。
#
# 写端点**不静默失败**：启用时一个库都没选属于配置错误，
# ``library_scope`` 会抛 ``ValueError``，这里转 400 报给前端——
# 悄悄把配置存成「谁也看不见」比报错危险得多。


class LibraryScopeDefaultRequest(BaseModel):
    enabled: bool = False
    library_ids: list[int] = Field(default_factory=list)


@admin_ops_router.get("/library-scope")
def get_library_scope(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """媒体库可见范围：默认范围 + 逐用户覆盖 + 可选库与用户（只读）"""
    payload = library_scope.policy_payload(db)
    return {"success": True, **payload}


@admin_ops_router.put("/library-scope")
def update_library_scope_default(
    request: LibraryScopeDefaultRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存服务器默认范围"""
    try:
        applied = library_scope.write_default(db, request.enabled, request.library_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(db, current_admin, "update_library_scope_default", "system", None, applied)
    db.commit()
    return {"success": True, "applied": applied,
            "policy": library_scope.policy_payload(db)}


class LibraryScopeUserRequest(BaseModel):
    enabled: bool = False
    library_ids: list[int] = Field(default_factory=list)


@admin_ops_router.put("/library-scope/users/{user_id}")
def update_library_scope_user(
    user_id: int,
    request: LibraryScopeUserRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """保存某个用户的单独覆盖（``enabled=false`` = 恢复跟随服务器默认）"""
    target = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    try:
        applied = library_scope.write_user_override(
            db, user_id, request.enabled, request.library_ids
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _audit(db, current_admin, "update_library_scope_user", "user", user_id,
           {**applied, "username": target.username})
    db.commit()
    return {"success": True, "applied": applied,
            "policy": library_scope.policy_payload(db)}


@admin_ops_router.delete("/library-scope/users/{user_id}")
def delete_library_scope_user(
    user_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """删掉某个用户的覆盖 = 彻底回到跟随默认"""
    applied = library_scope.remove_user_override(db, user_id)
    _audit(db, current_admin, "remove_library_scope_user", "user", user_id, applied)
    db.commit()
    return {"success": True, "applied": applied,
            "policy": library_scope.policy_payload(db)}


# ==================== 邀请码管理（v2.44.0 第一阶段） ====================
#
# 用户自己的那张码（``GET /api/user/invite/my-code``）照旧自动建，不受影响；
# 这里是**管理员维度**的批量生成 / 改配 / 作废，回答的是
# 「我发出去的那一堆渠道码现在用到哪一步了」。
#
# 次数、有效天数、白名单都是每码一份，不写死：0 次数 = 不限，0 天 = 永不过期，
# 白名单空 = 不限（与升级前行为一致）。判定逻辑在 ``apply_invitation``，
# 这里只管生与改，两处口径必须一致。


def _inv_code_state(code: models.InvitationCode, now: datetime) -> str:
    if not code.is_active:
        return "revoked"
    if code.expires_at and code.expires_at < now:
        return "expired"
    if code.max_uses and (code.use_count or 0) >= code.max_uses:
        return "used_up"
    return "active"


INV_CODE_STATE_LABELS = {
    "active": "可用",
    "used_up": "已用完",
    "expired": "已过期",
    "revoked": "已作废",
}


def _inv_code_dto(code: models.InvitationCode, owner: str, now: datetime) -> dict:
    whitelist = [w.strip() for w in (code.whitelist or "").split(",") if w.strip()]
    state = _inv_code_state(code, now)
    days_left = None
    if code.expires_at:
        days_left = max(0, int((code.expires_at - now).total_seconds() // 86400))
    return {
        "id": code.id,
        "code": code.code,
        "owner_user_id": code.user_id,
        "owner_username": owner,
        "max_uses": int(code.max_uses or 0),
        "use_count": int(code.use_count or 0),
        # None = 不限次数（前端显示「不限」，不要显示 0）
        "remaining": (None if not code.max_uses
                      else max(0, int(code.max_uses) - int(code.use_count or 0))),
        "whitelist": whitelist,
        "is_active": bool(code.is_active),
        "state": state,
        "state_label": INV_CODE_STATE_LABELS.get(state, state),
        "expires_at": code.expires_at.isoformat() if code.expires_at else None,
        "expires_days_left": days_left,
        "reward_points": int(code.reward_points or 0),
        "created_at": code.created_at.isoformat() if code.created_at else None,
    }


def _normalize_whitelist(raw: str) -> str:
    """逗号/空格/换行分隔 → 去重小写逗号串（存储口径，与 apply_invitation 一致）"""
    seen: list[str] = []
    for chunk in (raw or "").replace("，", ",").replace("\n", ",").split(","):
        name = chunk.strip().lower()
        if name and name not in seen:
            seen.append(name)
    return ",".join(seen)[:2000]


class InvitationCodeGenerateRequest(BaseModel):
    owner_user_id: int = Field(..., ge=1)
    count: int = Field(default=1, ge=1, le=200)
    max_uses: int = Field(default=100, ge=0, le=100000)
    expires_days: int = Field(default=0, ge=0, le=3650)
    whitelist: str = ""


@admin_ops_router.post("/invitation-codes")
def generate_invitation_codes(
    request: InvitationCodeGenerateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """批量生成：一次给某个用户发 N 张同规格的渠道码"""
    owner = db.query(models.WebUser).filter(
        models.WebUser.id == request.owner_user_id
    ).first()
    if not owner:
        raise HTTPException(status_code=404, detail="归属用户不存在")

    expires_at = (datetime.now() + timedelta(days=request.expires_days)
                  if request.expires_days > 0 else None)
    whitelist = _normalize_whitelist(request.whitelist)

    created = []
    for _ in range(request.count):
        # _generate_invite_code 自己会循环到库/会话里都不撞号为止（本批 pending
        # 行会被 autoflush 出来，所以批内也不会重号），这里不必再兜一层
        created.append(models.InvitationCode(
            code=_generate_invite_code(db),
            user_id=owner.id,
            max_uses=request.max_uses,
            use_count=0,
            reward_points=0,
            expires_at=expires_at,
            is_active=True,
            whitelist=whitelist,
        ))
    db.add_all(created)
    db.commit()
    for row in created:
        db.refresh(row)

    _audit(db, current_admin, "generate_invitation_codes", "invitation_code",
           created[0].id if created else None,
           {"count": len(created), "owner_user_id": owner.id,
            "max_uses": request.max_uses, "expires_days": request.expires_days,
            "whitelist": whitelist or None})
    db.commit()

    now = datetime.now()
    return {
        "success": True,
        "message": f"已为「{owner.username}」生成 {len(created)} 个邀请码",
        "codes": [_inv_code_dto(c, owner.username, now) for c in created],
    }


class InvitationCodeUpdateRequest(BaseModel):
    max_uses: Optional[int] = Field(default=None, ge=0, le=100000)
    # None = 不改；0 = 改成永不过期；>0 = 从现在起 N 天
    expires_days: Optional[int] = Field(default=None, ge=0, le=3650)
    whitelist: Optional[str] = None
    is_active: Optional[bool] = None


@admin_ops_router.put("/invitation-codes/{code_id}")
def update_invitation_code(
    code_id: int,
    request: InvitationCodeUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """改邀请码：次数 / 有效天数 / 白名单 / 启停"""
    code = db.query(models.InvitationCode).filter(
        models.InvitationCode.id == code_id
    ).first()
    if not code:
        raise HTTPException(status_code=404, detail="邀请码不存在")

    before = {"max_uses": int(code.max_uses or 0), "is_active": bool(code.is_active),
              "expires_at": code.expires_at.isoformat() if code.expires_at else None,
              "whitelist": code.whitelist or ""}

    if request.max_uses is not None:
        code.max_uses = request.max_uses
    if request.expires_days is not None:
        code.expires_at = (datetime.now() + timedelta(days=request.expires_days)
                           if request.expires_days > 0 else None)
    if request.whitelist is not None:
        code.whitelist = _normalize_whitelist(request.whitelist)
    if request.is_active is not None:
        code.is_active = request.is_active
    db.commit()

    _audit(db, current_admin, "update_invitation_code", "invitation_code", code.id,
           {"before": before,
            "after": {"max_uses": int(code.max_uses or 0),
                      "is_active": bool(code.is_active),
                      "expires_at": code.expires_at.isoformat() if code.expires_at else None,
                      "whitelist": code.whitelist or ""}})
    db.commit()
    owner = db.query(models.WebUser).filter(models.WebUser.id == code.user_id).first()
    return {"success": True, "code": _inv_code_dto(code, owner.username if owner else "", datetime.now())}


class InvitationCodeRevokeRequest(BaseModel):
    ids: list[int] = Field(default_factory=list, min_length=1, max_length=500)


@admin_ops_router.post("/invitation-codes/revoke")
def revoke_invitation_codes(
    request: InvitationCodeRevokeRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """批量作废：只翻 is_active，不删行——已经产生的邀请关系还指着这些码"""
    rows = db.query(models.InvitationCode).filter(
        models.InvitationCode.id.in_(request.ids)
    ).all()
    for row in rows:
        row.is_active = False
    db.commit()

    _audit(db, current_admin, "revoke_invitation_codes", "invitation_code", None,
           {"ids": [r.id for r in rows], "count": len(rows)})
    db.commit()
    return {"success": True, "message": f"已作废 {len(rows)} 个邀请码", "count": len(rows)}


@admin_ops_router.get("/invitation-codes")
def list_invitation_codes(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    keyword: str = "",
    owner_user_id: Optional[int] = None,
    state: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """邀请码列表（关键字 / 归属人 / 状态筛选 + 使用情况汇总）"""
    query = db.query(models.InvitationCode)
    if owner_user_id:
        query = query.filter(models.InvitationCode.user_id == owner_user_id)
    kw = (keyword or "").strip().lower()
    if kw:
        query = query.filter(models.InvitationCode.code.ilike(f"%{kw}%"))

    now = datetime.now()
    # F-P1-7：状态过滤与统计下推 SQL（原全量 .all() 后内存过滤/统计/切片）。
    # 口径与 _inv_code_state 一致：revoked=停用；expired=过期；used_up=用完；其余 active。
    inv_state = case(
        (models.InvitationCode.is_active.isnot(True), "revoked"),
        (
            and_(
                models.InvitationCode.expires_at.isnot(None),
                models.InvitationCode.expires_at < now,
            ),
            "expired",
        ),
        (
            and_(
                models.InvitationCode.max_uses.isnot(None),
                models.InvitationCode.max_uses > 0,
                func.coalesce(models.InvitationCode.use_count, 0)
                >= models.InvitationCode.max_uses,
            ),
            "used_up",
        ),
        else_="active",
    )
    if state:
        if state not in INV_CODE_STATE_LABELS:
            raise HTTPException(status_code=400, detail=f"未知状态：{state}")
        query = query.filter(inv_state == state)
    # 本接口历史上限 300，保持不变；只修"读全量"问题
    limit, offset = pagination_params(limit, offset, default_limit=100, max_limit=300)

    # 汇总在过滤后的集合上统计（GROUP BY 状态），与原语义一致
    summary = {key: 0 for key in INV_CODE_STATE_LABELS}
    summary["total"] = 0
    summary["uses"] = 0
    for r in (
        query.with_entities(
            inv_state.label("st"),
            func.count().label("n"),
            func.sum(func.coalesce(models.InvitationCode.use_count, 0)).label("uses"),
        )
        .group_by(inv_state)
        .all()
    ):
        if r.st in summary:
            summary[r.st] = int(r.n or 0)
        summary["total"] += int(r.n or 0)
        summary["uses"] += int(r.uses or 0)

    page_rows = (
        query.order_by(models.InvitationCode.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    owner_ids = {r.user_id for r in page_rows}
    owners = (
        {
            u.id: u.username
            for u in db.query(models.WebUser)
            .filter(models.WebUser.id.in_(list(owner_ids) or [-1]))
            .all()
        }
        if owner_ids
        else {}
    )
    page = [
        _inv_code_dto(r, owners.get(r.user_id, f"用户 #{r.user_id}"), now)
        for r in page_rows
    ]
    return {
        "success": True,
        "total": summary["total"],
        "summary": summary,
        "states": [{"value": "", "label": "全部"}] + [
            {"value": k, "label": v} for k, v in INV_CODE_STATE_LABELS.items()
        ],
        "codes": page,
    }


# ==================== 推广奖励（v2.44.0 第一阶段） ====================
#
# 开关与阈值全在 SystemConfig（``backend/promotion.py``），出厂全关/全 0：
# 管理员不手动打开，邀请成功就不会多发任何东西。


class PromotionPolicyRequest(BaseModel):
    policy: dict


@admin_ops_router.get("/promotion")
def get_promotion(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    limit: int = 100,
    offset: int = 0,
):
    """推广奖励：策略 + 汇总 + 发放流水（只读）"""
    total = db.query(func.count(models.PromotionReward.id)).scalar() or 0
    day_ago = datetime.now() - timedelta(hours=24)
    reward_24h = db.query(func.count(models.PromotionReward.id)).filter(
        models.PromotionReward.created_at >= day_ago
    ).scalar() or 0

    rows = (
        db.query(models.PromotionReward)
        .order_by(models.PromotionReward.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 300))
        .all()
    )
    user_ids = {r.inviter_id for r in rows} | {r.invitee_id for r in rows}
    names = {u.id: u.username for u in db.query(models.WebUser).filter(
        models.WebUser.id.in_(user_ids)).all()} if user_ids else {}

    return {
        "success": True,
        "policy": promotion.policy_payload(db),
        "summary": {"total": int(total), "reward_24h": int(reward_24h)},
        "rewards": [
            {**promotion.dto(r, names.get(r.invitee_id, "已注销")),
             "inviter_username": names.get(r.inviter_id, "已注销")}
            for r in rows
        ],
    }


@admin_ops_router.put("/promotion/policy")
def update_promotion_policy(
    payload: PromotionPolicyRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """写回推广奖励策略（只认白名单键，非法值保持原值不动）"""
    applied = promotion.write_policy(db, payload.policy)
    _audit(db, current_admin, "promotion_policy_update", "system", None, applied)
    db.commit()
    return {"success": True, "applied": applied,
            "policy": promotion.policy_payload(db)}


__all__ = ["admin_ops_router"]

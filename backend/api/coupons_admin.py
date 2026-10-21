"""
管理后台 · 优惠券

优惠券的核心逻辑在 `backend/coupons.py`（预订制额度、Decimal 计价、下单/履约/关单三段状态），
这里只做运营面：建券、改券、停用、看核销记录与开关。

为什么单独成文件：`backend/api/admin.py` 已经很大；订单关单/退款也是同样的做法
（`backend/api/orders_admin.py`）。鉴权口径完全一致（`get_current_admin` + JWT + `_audit`）。

几个刻意的口径：

- **改券不改历史订单**：优惠金额在核销记录（`coupon_usages`）与订单上都有快照，
  改折扣、改门槛只影响之后的订单；已经付过钱的订单金额不会跟着变。
- **总次数不能改到已用量以下**：否则「已占用 5 次、上限改成 3」会让券立即超发，
  这里直接拒绝，让管理员先停用或新建一张。
- **删券只在从没用过时允许**：有核销记录就停用（`is_active=false`），
  删掉会让历史订单的优惠来源凭空消失，对账时说不清。
- **有效期两种给法**：直接给 `valid_until`，或给 `valid_days`（从今天算 N 天），
  二选一，都给了以 `valid_until` 为准——生成页面上「30 天有效」更好点，
  但精确到时刻的场景必须能直接给时间。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend import config_service, coupons, models
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_coupons_router = APIRouter(prefix="/api/admin/economy/coupons",
                                 tags=["管理后台·优惠券"])

# 去掉了容易看错的 0/O/1/I：运营是要靠人念码、复制码的
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 10


def _gen_code(db: Session, attempts: int = 12) -> str:
    """生成一个没被用过的优惠码（重试有限次，避免死循环）"""
    for _ in range(attempts):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        exists = db.query(models.CouponCode.id).filter(
            models.CouponCode.code == code).first()
        if not exists:
            return code
    raise HTTPException(status_code=500, detail="优惠码生成失败，请重试")


def _money_str(value) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """接受 '2026-10-01' 与 '2026-10-01 12:30:00' / ISO 两种写法"""
    text = (value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "")
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail=f"时间格式无法识别：{value}")


def _usage_stats(db: Session, coupon_ids: list[int]) -> dict[int, dict[str, int]]:
    """一次查出所有券的核销分布（避免每张券三个 count 的 N+1）"""
    stats: dict[int, dict[str, int]] = {}
    if not coupon_ids:
        return stats
    rows = db.query(
        models.CouponUsage.coupon_id,
        models.CouponUsage.status,
        func.count(models.CouponUsage.id),
    ).filter(models.CouponUsage.coupon_id.in_(coupon_ids)) \
        .group_by(models.CouponUsage.coupon_id, models.CouponUsage.status).all()
    for coupon_id, status, count in rows:
        stats.setdefault(coupon_id, {})[status or coupons.RESERVED] = int(count)
    return stats


def serialize(coupon: models.CouponCode, stats: dict[str, int] | None = None) -> dict:
    stats = stats or {}
    reserved = int(stats.get(coupons.RESERVED, 0))
    consumed = int(stats.get(coupons.CONSUMED, 0))
    released = int(stats.get(coupons.RELEASED, 0))
    return {
        "id": coupon.id,
        "code": coupon.code,
        "kind": coupon.kind or "all",
        "discount_type": coupon.discount_type,
        "value": int(coupon.value or 0),
        "min_amount": _money_str(coupon.min_amount),
        "max_discount": _money_str(coupon.max_discount),
        "realm_id": coupon.realm_id,
        "realm_name": (coupon.realm.name if coupon.realm else ""),
        "max_uses": int(coupon.max_uses or 0),
        "use_count": int(coupon.use_count or 0),
        "per_user_limit": int(coupon.per_user_limit or 0),
        "valid_from": _iso(coupon.valid_from),
        "valid_until": _iso(coupon.valid_until),
        "is_active": bool(coupon.is_active),
        "note": coupon.note or "",
        "created_at": _iso(coupon.created_at),
        # 额度分布：reserved 是「占着但还没付」的，运营需要能看见这部分
        "stats": {"reserved": reserved, "consumed": consumed, "released": released},
        "usable": bool(coupon.is_active) and (
            coupon.max_uses == 0 or int(coupon.use_count or 0) < int(coupon.max_uses)),
    }


class CouponCreateRequest(BaseModel):
    """建券：`code` 留空则按 `count` 批量生成随机码"""

    count: int = Field(default=1, ge=1, le=200)
    code: str = Field(default="", description="指定优惠码（留空 = 随机生成）")
    kind: str = Field(default="all", description="all / subscription / recharge")
    discount_type: str = Field(default="percent", description="percent=打折 / fixed=减钱")
    value: int = Field(default=90, description="percent: 实付百分比（90 = 九折）；fixed: 减免金额（元）")
    min_amount: float = Field(default=0, ge=0)
    max_discount: float = Field(default=0, ge=0, description="百分比券的封顶减免，0 = 不封顶")
    realm_id: Optional[int] = None
    max_uses: int = Field(default=0, ge=0, description="总次数上限，0 = 不限")
    per_user_limit: int = Field(default=1, ge=0, description="每人限用次数，0 = 不限")
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    valid_days: int = Field(default=0, ge=0, description="从今天起 N 天有效（与 valid_until 二选一）")
    note: str = ""


class CouponUpdateRequest(BaseModel):
    kind: Optional[str] = None
    discount_type: Optional[str] = None
    value: Optional[int] = None
    min_amount: Optional[float] = None
    max_discount: Optional[float] = None
    realm_id: Optional[int] = None
    max_uses: Optional[int] = None
    per_user_limit: Optional[int] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    is_active: Optional[bool] = None
    note: Optional[str] = None


class CouponSettingsRequest(BaseModel):
    # 2026-10-21 摘面板后前端只发 reserve_hours；enabled 仅兼容旧调用方，可省略
    enabled: Optional[bool] = None
    # 超时未支付的预订多久自动关单并还额度（小时，0 = 不自动清理）
    reserve_hours: Optional[int] = Field(default=None, ge=0, le=720)


def _validate_kind(kind: str) -> str:
    if kind not in ("all", "subscription", "recharge"):
        raise HTTPException(status_code=400, detail="适用范围必须是 all / subscription / recharge")
    return kind


def _validate_discount(discount_type: str, value: int) -> None:
    if discount_type not in (coupons.PERCENT, coupons.FIXED):
        raise HTTPException(status_code=400, detail="折扣类型必须是 percent 或 fixed")
    if discount_type == coupons.PERCENT and not (1 <= int(value) <= 100):
        raise HTTPException(status_code=400, detail="百分比折扣的 value 必须在 1~100 之间（90 = 九折）")
    if discount_type == coupons.FIXED and int(value) <= 0:
        raise HTTPException(status_code=400, detail="固定减免的 value 必须大于 0（单位：元）")


# ==================== 开关 ====================
# 注意：这两条必须声明在 /{coupon_id} 之前，否则 "settings" 会先去尝试解析成 int

@admin_coupons_router.get("/settings")
def get_coupon_settings(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return {
        "enabled": coupons.enabled(db),
        "reserve_hours": coupons.reserve_hours(db),
        "active_usage": db.query(models.CouponUsage).filter(
            models.CouponUsage.status == coupons.RESERVED).count(),
    }


@admin_coupons_router.put("/settings")
def update_coupon_settings(
    request: CouponSettingsRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """预订超时清理时限（总开关 2026-10-21 摘面板：request.enabled 仅为兼容
    旧前端保留、不再落库——开关改走 env COUPON_ENABLED，缺省=开）"""
    values = {}
    if request.reserve_hours is not None:
        values[coupons.CONFIG_RESERVE_HOURS] = str(int(request.reserve_hours))
    # 配置瘦身第 4 批 4-4：走统一写入通道（校验→落库→写时失效）；
    # store.write_values 内部已按 store 纪律失效，4-1 的失效覆盖由正统通道承接。
    config_service.save_config(db, values, schema="coupons")
    db.commit()

    _audit(db, current_admin, "economy_coupon_settings", "system", None,
           {"enabled": request.enabled, "reserve_hours": request.reserve_hours})
    db.commit()
    return {
        "success": True,
        "enabled": coupons.enabled(db),
        "reserve_hours": request.reserve_hours if request.reserve_hours is not None
        else coupons.reserve_hours(db),
    }


# ==================== 核销记录 ====================

def coupon_usages_payload(db: Session, limit: int = 30,
                          coupon_id: Optional[int] = None) -> dict:
    """最近的核销记录（跨券）：预订 / 已消费 / 已释放都列出来

    用户端只会看到「优惠了多少钱」，运营要能回答「这张券到底被谁用了、用在哪张订单、
    后来关单退款没有」——所以三种状态都要在。
    """
    limit = max(1, min(int(limit or 30), 200))
    query = db.query(models.CouponUsage)
    if coupon_id:
        query = query.filter(models.CouponUsage.coupon_id == coupon_id)
    usages = query.order_by(models.CouponUsage.id.desc()).limit(limit).all()

    user_ids = {u.user_id for u in usages}
    users = {u.id: u.username for u in db.query(models.WebUser).filter(
        models.WebUser.id.in_(user_ids)).all()} if user_ids else {}
    coupon_ids = {u.coupon_id for u in usages}
    code_map = {c.id: c.code for c in db.query(models.CouponCode).filter(
        models.CouponCode.id.in_(coupon_ids)).all()} if coupon_ids else {}

    return {"records": [
        {
            "id": u.id,
            "coupon_id": u.coupon_id,
            "code": code_map.get(u.coupon_id, ""),
            "user_id": u.user_id,
            "username": users.get(u.user_id, f"用户 {u.user_id}"),
            "order_id": u.order_id,
            "kind": u.kind,
            "status": u.status,
            "list_price": _money_str(u.list_price),
            "discount_amount": _money_str(u.discount_amount),
            "paid_amount": _money_str(u.paid_amount),
            "created_at": _iso(u.created_at),
            "closed_at": _iso(u.closed_at),
        }
        for u in usages
    ]}


@admin_coupons_router.get("/usages")
def list_coupon_usages(
    limit: int = 30,
    coupon_id: Optional[int] = None,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """最近的核销记录（跨券）—— 同步 `def` 路由由 FastAPI 丢线程池执行"""
    return coupon_usages_payload(db, limit=limit, coupon_id=coupon_id)


@admin_coupons_router.get("/{coupon_id}/usages")
def list_coupon_usages_by_code(
    coupon_id: int,
    limit: int = 100,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """单张券的核销记录（与跨券口径共用同一段查询）"""
    return coupon_usages_payload(db, limit=limit, coupon_id=coupon_id)


# ==================== 列表 ====================

@admin_coupons_router.get("")
def list_coupons(
    kind: Optional[str] = None,
    active: Optional[str] = None,
    search: str = "",
    limit: int = 200,
    offset: int = 0,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """优惠券列表（带额度分布与归属服）

    分页：此前无 offset 参数、total 返回的是本页条数而非真实总数，
    券超过 500 张时旧券永远翻不到、前端分页也被 total 误导。
    offset 默认为 0，老调用方行为不变。
    """
    limit = max(1, min(int(limit or 200), 500))
    offset = max(0, int(offset or 0))
    query = db.query(models.CouponCode)
    if kind in ("all", "subscription", "recharge"):
        query = query.filter(models.CouponCode.kind == kind)
    if active in ("true", "false"):
        query = query.filter(models.CouponCode.is_active == (active == "true"))
    text = (search or "").strip().upper()
    if text:
        # ilike：在 PG 下是 ILIKE（不区分大小写），在 SQLite 下退化为 LIKE
        # （SQLite 的 LIKE 本来就不区分 ASCII 大小写），两边行为一致。
        query = query.filter(models.CouponCode.code.ilike(f"%{text}%"))

    rows = query.order_by(models.CouponCode.id.desc()).offset(offset).limit(limit).all()
    stats = _usage_stats(db, [c.id for c in rows])
    return {
        "enabled": coupons.enabled(db),
        "total": query.count(),
        "offset": offset,
        "limit": limit,
        "coupons": [serialize(c, stats.get(c.id)) for c in rows],
    }


# ==================== 建券 ====================

@admin_coupons_router.post("")
def create_coupons(
    request: CouponCreateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """建券（可批量）：指定码只能建一张，批量时一律随机码"""
    _validate_kind(request.kind)
    _validate_discount(request.discount_type, request.value)

    if request.realm_id:
        realm = db.query(models.ServerRealm).filter(
            models.ServerRealm.id == request.realm_id).first()
        if realm is None:
            raise HTTPException(status_code=404, detail="指定的服不存在")

    valid_from = _parse_dt(request.valid_from)
    valid_until = _parse_dt(request.valid_until)
    if valid_until is None and request.valid_days:
        valid_until = datetime.now() + timedelta(days=int(request.valid_days))
    if valid_from and valid_until and valid_until < valid_from:
        raise HTTPException(status_code=400, detail="失效时间不能早于生效时间")

    explicit = (request.code or "").strip().upper()
    if explicit and request.count > 1:
        raise HTTPException(status_code=400, detail="指定优惠码时只能建一张，批量请用随机码")
    if explicit and len(explicit) > 32:
        raise HTTPException(status_code=400, detail="优惠码最长 32 个字符")

    codes = [explicit] if explicit else []
    for _ in range(int(request.count) - len(codes)):
        codes.append(_gen_code(db))

    created: list[models.CouponCode] = []
    # r49: 一次 IN 查完已存在的码（防 N+1：此前循环内每码一次 query；_gen_code 生成时已查重，
    # 此处只防手动指定码与并发插入，语义与逐码查一致）
    existing = {
        row[0]
        for row in db.query(models.CouponCode.code)
        .filter(models.CouponCode.code.in_(codes))
        .all()
    }
    for code in codes:
        if code in existing:
            raise HTTPException(status_code=400, detail=f"优惠码 {code} 已存在")
        coupon = models.CouponCode(
            code=code,
            kind=request.kind,
            discount_type=request.discount_type,
            value=int(request.value),
            min_amount=Decimal(str(request.min_amount or 0)),
            max_discount=Decimal(str(request.max_discount or 0)),
            realm_id=request.realm_id,
            max_uses=int(request.max_uses or 0),
            use_count=0,
            per_user_limit=int(request.per_user_limit or 0),
            valid_from=valid_from,
            valid_until=valid_until,
            is_active=True,
            note=(request.note or "").strip()[:255],
            created_by=current_admin.id,
        )
        db.add(coupon)
        created.append(coupon)
    db.commit()

    _audit(db, current_admin, "economy_create_coupons", "coupon", None, {
        "codes": [c.code for c in created],
        "kind": request.kind, "discount_type": request.discount_type,
        "value": request.value, "max_uses": request.max_uses,
        "per_user_limit": request.per_user_limit,
        "realm_id": request.realm_id,
    })
    db.commit()
    logger.info("创建优惠券 %s 张（管理员 %s）", len(created), current_admin.id)
    return {"success": True, "count": len(created),
            "coupons": [serialize(c) for c in created]}


# ==================== 改券 ====================

@admin_coupons_router.put("/{coupon_id}")
def update_coupon(
    coupon_id: int,
    request: CouponUpdateRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    coupon = db.query(models.CouponCode).filter(models.CouponCode.id == coupon_id).first()
    if coupon is None:
        raise HTTPException(status_code=404, detail="优惠券不存在")

    changed: dict = {}

    if request.kind is not None:
        coupon.kind = _validate_kind(request.kind)
        changed["kind"] = coupon.kind

    discount_type = request.discount_type if request.discount_type is not None else coupon.discount_type
    value = int(request.value) if request.value is not None else int(coupon.value or 0)
    if request.discount_type is not None or request.value is not None:
        _validate_discount(discount_type, value)
        coupon.discount_type = discount_type
        coupon.value = value
        changed["discount_type"] = discount_type
        changed["value"] = value

    if request.min_amount is not None:
        coupon.min_amount = Decimal(str(max(0, request.min_amount)))
        changed["min_amount"] = _money_str(coupon.min_amount)
    if request.max_discount is not None:
        coupon.max_discount = Decimal(str(max(0, request.max_discount)))
        changed["max_discount"] = _money_str(coupon.max_discount)

    if request.realm_id is not None:
        if request.realm_id:
            realm = db.query(models.ServerRealm).filter(
                models.ServerRealm.id == request.realm_id).first()
            if realm is None:
                raise HTTPException(status_code=404, detail="指定的服不存在")
        coupon.realm_id = request.realm_id or None
        changed["realm_id"] = coupon.realm_id

    if request.max_uses is not None:
        # 不能改到已占用之下：否则券立即变成超发状态，先停用或另建一张
        used = coupons.active_use_count(db, coupon)
        if request.max_uses and int(request.max_uses) < used:
            raise HTTPException(
                status_code=400,
                detail=f"该券已有 {used} 次占用（含未支付订单），总次数不能低于这个数",
            )
        coupon.max_uses = int(request.max_uses)
        changed["max_uses"] = coupon.max_uses

    if request.per_user_limit is not None:
        coupon.per_user_limit = int(request.per_user_limit)
        changed["per_user_limit"] = coupon.per_user_limit

    if request.valid_from is not None:
        coupon.valid_from = _parse_dt(request.valid_from)
        changed["valid_from"] = _iso(coupon.valid_from)
    if request.valid_until is not None:
        coupon.valid_until = _parse_dt(request.valid_until)
        changed["valid_until"] = _iso(coupon.valid_until)
    if coupon.valid_from and coupon.valid_until and coupon.valid_until < coupon.valid_from:
        raise HTTPException(status_code=400, detail="失效时间不能早于生效时间")

    if request.is_active is not None:
        coupon.is_active = bool(request.is_active)
        changed["is_active"] = coupon.is_active
    if request.note is not None:
        coupon.note = (request.note or "").strip()[:255]
        changed["note"] = coupon.note

    db.commit()
    _audit(db, current_admin, "economy_update_coupon", "coupon", coupon.id, changed)
    db.commit()
    return {"success": True, "coupon": serialize(coupon)}


@admin_coupons_router.delete("/{coupon_id}")
def delete_coupon(
    coupon_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """删券：只允许从没用过的券；有核销记录请改为停用（历史订单要能解释优惠来源）

    原子 DELETE：读-查-删之间若有用户并发预订占上额度（coupon_usages 无
    ondelete 的 FK），无条件 db.delete 会撞 FK 报 500。这里用 NOT EXISTS
    条件一次判定——与 registration_codes 的 delete_code（P0-2）同一写法。
    """
    code_row = db.query(models.CouponCode.code).filter(
        models.CouponCode.id == coupon_id
    ).first()
    if code_row is None:
        raise HTTPException(status_code=404, detail="优惠券不存在")

    deleted = (
        db.query(models.CouponCode)
        .filter(
            models.CouponCode.id == coupon_id,
            ~db.query(models.CouponUsage.id)
            .filter(models.CouponUsage.coupon_id == coupon_id)
            .exists(),
        )
        .delete(synchronize_session=False)
    )
    if deleted == 0:
        # 行存在但已有核销/预订记录：并发预订在我们检查后占上了额度
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="该优惠券已有核销记录，不能删除；请改为停用（历史订单需要保留优惠来源）",
        )

    code = code_row[0]
    _audit(db, current_admin, "economy_delete_coupon", "coupon", coupon_id, {"code": code})
    db.commit()
    return {"success": True, "message": f"优惠券 {code} 已删除"}


__all__ = ["admin_coupons_router"]

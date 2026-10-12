"""服管理 API：一个面板运营多个服

- ``GET    /api/admin/realms``                服清单（含每服运营数据）+ 当前服
- ``GET    /api/admin/realms/overview``       跨服汇总（数据概览页用）
- ``POST   /api/admin/realms``                新建服
- ``PUT    /api/admin/realms/{id}``           改名 / 地址 / 描述 / 启停
- ``POST   /api/admin/realms/{id}/activate``  切换面板当前操作的服
- ``POST   /api/admin/realms/{id}/sync``      重新体检该服的所有播放节点并带回节点自称的库
- ``DELETE /api/admin/realms/{id}``           删除（有数据时必须 ``move_to`` 指定移交目标）

「一个服一个」的东西（套餐、订阅、媒体库、挂载、播放节点、卡码、求片）都由
``backend/realms.py`` 统一收口；这里只负责服的增删改查与运营数据聚合。
所有写操作都落管理审计。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models, realms
from backend import servers as registry
from backend.api.admin_core import _audit, get_current_admin
from backend.api.pagination import pagination_params
from backend.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/realms", tags=["服管理（多服运营）"])
# 服的增删改查 + 运营数据聚合；「一个服一个」的数据由 backend/realms.py 收口


class RealmPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    slug: str = Field(default="", max_length=40)
    url: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=300)
    is_active: bool = True
    # 接入方式：paid（付费服，需要订阅）/ free（公益服，免费开放）
    access_mode: str = Field(default=realms.ACCESS_PAID, max_length=10)
    # 公益服规则文案（用户端展示）
    access_note: str = Field(default="", max_length=500)
    # 下载策略：不传 = 跟随全局（公益服默认禁止下载）
    allow_download: Optional[bool] = None


class RealmUpdatePayload(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    url: Optional[str] = Field(default=None, max_length=500)
    description: Optional[str] = Field(default=None, max_length=300)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None
    access_mode: Optional[str] = Field(default=None, max_length=10)
    access_note: Optional[str] = Field(default=None, max_length=500)
    # 三态下载策略：follow（跟随全局）/ allow / deny；不传 = 不改
    download_policy: Optional[str] = Field(default=None, max_length=10)


def _summary(
    db: Session,
    rows: Optional[list] = None,
    all_stats: Optional[dict[int, dict]] = None,
) -> dict:
    """跨服汇总：面板顶部与「数据概览」的服信息展示用。

    ``rows`` / ``all_stats`` 由列表端点复用，避免同一个请求里把每服统计
    做两遍（旧实现 /realms 一次序列化算一遍，顶部 summary 又算一遍）。
    """
    rows = realms.list_realms(db) if rows is None else rows
    all_stats = (
        {r.id: realms.stats(db, r.id) for r in rows}
        if all_stats is None else all_stats
    )
    total_active_subs = sum(s["active_subscriptions"] for s in all_stats.values())
    free_realms = [r for r in rows if realms.normalize_access_mode(r.access_mode) == realms.ACCESS_FREE]
    return {
        "total_realms": len(rows),
        "enabled_realms": len([r for r in rows if r.is_active]),
        # 公益服（免费开放）的服数：面板顶部与概览页用
        "free_realms": len(free_realms),
        "paid_realms": len(rows) - len(free_realms),
        "libraries": sum(s["libraries"] for s in all_stats.values()),
        "items": sum(s["items"] for s in all_stats.values()),
        "plans": sum(s["plans"] for s in all_stats.values()),
        "active_subscriptions": total_active_subs,
        "subscribers": sum(s["subscribers"] for s in all_stats.values()),
        "nodes": sum(s["nodes"] for s in all_stats.values()),
        "nodes_online": sum(s["nodes_online"] for s in all_stats.values()),
        "pending_requests": sum(s["pending_requests"] for s in all_stats.values()),
    }


@router.get("")
def list_realms(_: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    rows = realms.list_realms(db)
    active = realms.active_realm(db)
    # 一次请求内复用同一份 stats / legacy_id：
    # 旧实现先在每个 serialize() 里统计一次，随后 _summary() 再对所有服统计一次。
    # 服管理页打开一次就会把 COUNT / 订阅物化查询做两遍，服数越多越明显。
    legacy_id = realms.legacy_realm_id(db)
    all_stats = {r.id: realms.stats(db, r.id) for r in rows}
    return {
        "realms": [
            realms.serialize(db, r, stats_data=all_stats[r.id], legacy_id=legacy_id)
            for r in rows
        ],
        "active_realm_id": active.id,
        "active_realm_name": active.name,
        "summary": _summary(db, rows=rows, all_stats=all_stats),
    }


@router.get("/overview")
def realms_overview(_: models.WebUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    """只有各服的运营数据：给「数据概览」的服卡片用（不重复下发节点明细）"""
    rows = realms.list_realms(db)
    return {
        "realms": [
            {
                "id": r.id, "name": r.name, "slug": r.slug, "url": r.url or "",
                "is_active": bool(r.is_active),
                "is_default": r.id == realms.legacy_realm_id(db),
                "access_mode": realms.normalize_access_mode(r.access_mode),
                "is_free": realms.is_free_realm(db, r.id),
                "public_url": realms.realm_public_url(db, r.id),
                "stats": realms.stats(db, r.id),
            }
            for r in rows
        ],
        "active_realm_id": realms.active_realm_id(db),
        "summary": _summary(db),
    }


@router.post("")
def create_realm(
    payload: RealmPayload,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    try:
        realm = realms.create_realm(
            db, name=payload.name, slug=payload.slug, url=payload.url,
            description=payload.description, is_active=payload.is_active,
            access_mode=payload.access_mode, access_note=payload.access_note,
            allow_download=payload.allow_download,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit(db, admin, "create_realm", "realm", realm.id,
           {"name": realm.name, "slug": realm.slug,
            "access_mode": realms.normalize_access_mode(realm.access_mode)})
    db.commit()
    return {"success": True, "realm": realms.serialize(db, realm), "summary": _summary(db)}


@router.get("/{realm_id}/subscriptions")
def realm_subscriptions(
    realm_id: int,
    status_filter: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    _: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """某个服的订阅清单（``realm_id=0`` = 全部服）

    与 ``/api/admin/economy/subscriptions`` 同形，差别是**按服过滤**：订阅是一个服一个的，
    后台默认就该只看当前服，跨服汇总需要显式传 0。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import or_

    now = datetime.now()
    week_later = now + timedelta(days=7)
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))

    query = realms.scope(db.query(models.UserSubscription),
                         models.UserSubscription.realm_id, scope_id)

    # 用户名 / 邮箱搜索（订阅页的搜索框；按 user_id 过滤，避免再去 join）
    keyword = (search or "").strip()
    if keyword:
        matched = db.query(models.WebUser.id).filter(or_(
            models.WebUser.username.ilike(f"%{keyword}%"),
            models.WebUser.email.ilike(f"%{keyword}%"),
        )).all()
        query = query.filter(models.UserSubscription.user_id.in_([row[0] for row in matched] or [-1]))
    if status_filter == "active":
        query = query.filter(models.UserSubscription.status == "active",
                             models.UserSubscription.end_date > now)
    elif status_filter == "expiring":
        query = query.filter(models.UserSubscription.status == "active",
                             models.UserSubscription.end_date > now,
                             models.UserSubscription.end_date <= week_later)
    elif status_filter == "expired":
        query = query.filter(or_(models.UserSubscription.status == "expired",
                                 models.UserSubscription.end_date <= now))

    # F-P1-5：分页参数统一钳制（limit∈[1,200]，offset≥0；原 min() 只限上界，limit=-1 可绕过）
    limit, offset = pagination_params(limit, offset)
    total = query.count()
    rows = (query.order_by(models.UserSubscription.end_date.asc())
            .offset(offset).limit(limit).all())

    user_ids = {r.user_id for r in rows}
    users = {
        u.id: u.username
        for u in db.query(models.WebUser).filter(models.WebUser.id.in_(user_ids)).all()
    } if user_ids else {}

    def _scoped():
        return realms.scope(db.query(models.UserSubscription),
                            models.UserSubscription.realm_id, scope_id)

    realm = realms.get_realm(db, scope_id) if scope_id else None
    return {
        "total": total,
        "summary": {
            "active": _scoped().filter(models.UserSubscription.status == "active",
                                       models.UserSubscription.end_date > now).count(),
            "expiring_7d": _scoped().filter(models.UserSubscription.status == "active",
                                            models.UserSubscription.end_date > now,
                                            models.UserSubscription.end_date <= week_later).count(),
            "expired": _scoped().filter(or_(models.UserSubscription.status == "expired",
                                            models.UserSubscription.end_date <= now)).count(),
        },
        "realm_id": scope_id,
        "realm_name": realm.name if realm else "全部服",
        "active_realm_id": realms.active_realm_id(db),
        "subscriptions": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": users.get(r.user_id, "未知"),
                "plan_name": r.plan.name if r.plan else f"套餐 #{r.plan_id}",
                "realm_id": r.realm_id,
                "realm_name": (r.realm.name if r.realm else ""),
                "start_date": r.start_date.isoformat() if r.start_date else None,
                "end_date": r.end_date.isoformat() if r.end_date else None,
                "days_left": max(0, (r.end_date - now).days) if r.end_date else 0,
                "status": "active" if (r.status == "active" and r.end_date and r.end_date > now) else "expired",
            }
            for r in rows
        ],
    }


@router.put("/{realm_id}")
def update_realm(
    realm_id: int,
    payload: RealmUpdatePayload,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    realm = realms.get_realm(db, realm_id)
    if not realm:
        raise HTTPException(404, "服不存在")
    try:
        realm = realms.update_realm(
            db, realm, name=payload.name, url=payload.url,
            description=payload.description, is_active=payload.is_active,
            sort_order=payload.sort_order,
            access_mode=payload.access_mode, access_note=payload.access_note,
            download_policy=payload.download_policy,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit(db, admin, "update_realm", "realm", realm.id,
           {"name": realm.name, "is_active": bool(realm.is_active),
            "access_mode": realms.normalize_access_mode(realm.access_mode)})
    db.commit()
    return {"success": True, "realm": realms.serialize(db, realm), "summary": _summary(db)}


@router.post("/{realm_id}/activate")
def activate_realm(
    realm_id: int,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """切换面板当前操作的服（后台各页的作用域跟着切）"""
    realm = realms.get_realm(db, realm_id)
    if not realm:
        raise HTTPException(404, "服不存在")
    if not realm.is_active:
        raise HTTPException(400, "这个服已停用，先启用再切换过去")
    realms.set_active_realm(db, realm.id)
    _audit(db, admin, "activate_realm", "realm", realm.id, {"name": realm.name})
    db.commit()
    return {"success": True, "active_realm_id": realm.id,
            "realm": realms.serialize(db, realm), "summary": _summary(db)}


@router.post("/{realm_id}/sync")
async def sync_realm_nodes(
    realm_id: int,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """重新体检该服的所有播放节点

    顺便把每台节点**自称**的服与负责的库带回来：配错 ``REALM`` / ``NODE_KEY``
    会表现成「客户端看不到任何库」，在这里当场就能看出来。
    """
    admin_id = admin.id          # 纯值：下面有提交，之后再读 ORM 属性会在事件循环上回查

    def load() -> tuple[Optional[models.ServerRealm], list, str]:
        """取服与本服下的节点（同步；下放线程池）"""
        realm = realms.get_realm(db, realm_id)
        if realm is None:
            return None, [], ""
        return realm, realms.nodes_of_realm(db, realm.id), realm.slug

    realm, nodes, realm_slug = await run_in_threadpool(load)
    if realm is None:
        raise HTTPException(404, "服不存在")

    def node_facts(node) -> dict:
        """体检之后要展示的纯值（同步；下放线程池——提交会让 ORM 属性过期）"""
        return {"id": node.id, "name": node.name, "url": node.url,
                "message": node.last_check_message or "",
                "node_key": node.node_key or ""}

    results = []
    for node in nodes:
        probe = await registry.probe_and_store(db, node)
        identity = (probe.get("node") or {}).get("data") or {}
        facts = await run_in_threadpool(node_facts, node)
        results.append({
            "id": facts["id"],
            "name": facts["name"],
            "url": facts["url"],
            "ok": bool(probe.get("ok")),
            "message": facts["message"],
            "node_key_claimed": identity.get("node_key") or facts["node_key"],
            "realm_slug_reported": identity.get("realm_slug") or "",
            "libraries": len(identity.get("libraries") or []),
            # 节点自称的服与面板记录不一致时给出明确提示（配置错了）
            "realm_mismatch": bool(identity.get("realm_slug")
                                   and identity.get("realm_slug") != realm_slug),
        })
    _audit(db, admin_id, "sync_realm_nodes", "realm", realm_id,
           {"nodes": len(results), "online": len([r for r in results if r["ok"]])})
    await run_in_threadpool(db.commit)
    return {"success": True, "nodes": results,
            "realm": await run_in_threadpool(realms.serialize, db, realm)}


@router.delete("/{realm_id}")
def delete_realm(
    realm_id: int,
    move_to: Optional[int] = None,
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    realm = realms.get_realm(db, realm_id)
    if not realm:
        raise HTTPException(404, "服不存在")
    name = realm.name
    try:
        result = realms.delete_realm(db, realm, move_to=move_to)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit(db, admin, "delete_realm", "realm", realm_id, {"name": name, **result})
    db.commit()
    return {"success": True, **result, "summary": _summary(db)}


__all__ = ["router"]

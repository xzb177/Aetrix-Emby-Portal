"""合集域 L3 接口（后端 only）：管理端 + 用户端读口。

路径走 Aetrix 现有前缀（``/api/admin`` / ``/api/user``），不抄
FMBY 的接口形态。总闸关时全部接口惰性 404（现有接口不动、
不改一行现有路由）；总闸开时才提供服务。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.admin_core import get_current_admin
from backend.api.pagination import pagination_params
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import media_sets as ms

admin_media_sets_router = APIRouter(prefix="/api/admin",
                                    tags=["管理后台·合集"])
user_media_sets_router = APIRouter(prefix="/api/user",
                                   tags=["用户端·合集"])


def _require_enabled():
    if not ms.collections_enabled():
        raise HTTPException(status_code=404, detail="合集功能未开启")


def _get_set(db: Session, set_id: int) -> em.EmbyMediaSet:
    media_set = db.query(em.EmbyMediaSet).filter(
        em.EmbyMediaSet.id == set_id).first()
    if media_set is None:
        raise HTTPException(status_code=404, detail="合集不存在")
    return media_set


def _item_dict(item, order_index: int | None = None) -> dict:
    out = {"id": item.id, "name": item.name, "item_type": item.item_type,
           "production_year": item.production_year,
           "premiere_date": item.premiere_date.isoformat()
           if item.premiere_date else None}
    if order_index is not None:
        out["order_index"] = order_index
    return out


# ---------------------------------------------------------------- 管理端模型


class MediaSetCreate(BaseModel):
    name: str
    source_type: str = "manual"
    visibility_status: str = "visible"
    sort_order: int = 0
    min_effective_members: int = 2
    artwork_mode: str = "auto_collage"
    path_prefix: str | None = None
    bound_person_id: int | None = None


class MediaSetPatch(BaseModel):
    name: str | None = None
    source_type: str | None = None
    visibility_status: str | None = None
    sort_order: int | None = None
    min_effective_members: int | None = None
    artwork_mode: str | None = None
    path_prefix: str | None = None
    bound_person_id: int | None = None


class MemberAdd(BaseModel):
    item_id: int
    order_index: int = 0


class OrderEntry(BaseModel):
    id: int
    sort_order: int


class SetOrderBody(BaseModel):
    orders: list[OrderEntry]


class MemberOrderEntry(BaseModel):
    item_id: int
    order_index: int


class MemberOrderBody(BaseModel):
    orders: list[MemberOrderEntry]


class RuleCreate(BaseModel):
    rule_kind: str
    is_exclusion: bool = False
    value_text: str | None = None
    value_int: int | None = None
    value_float: float | None = None


class TemplateCreate(BaseModel):
    name: str
    rules: list[dict] = []


class TemplateInstantiate(BaseModel):
    name: str | None = None
    min_effective_members: int | None = None


class HotBoardInstantiate(BaseModel):
    """从当前热播榜生成一份手工合集。period 默认日榜，name 可覆盖默认名。"""
    period: str = "day"
    name: str | None = None


_HOT_PERIOD_LABEL = {"day": "日榜", "week": "周榜"}


def _check_set_fields(db: Session, source_type: str, visibility_status: str,
                      artwork_mode: str, path_prefix, bound_person_id):
    if source_type not in ms.SOURCE_TYPES:
        raise HTTPException(status_code=400,
                            detail=f"未知的合集来源: {source_type}")
    if visibility_status not in ms.VISIBILITY_STATUSES:
        raise HTTPException(status_code=400, detail="visibility_status 非法")
    if artwork_mode not in ms.ARTWORK_MODES:
        raise HTTPException(status_code=400, detail="artwork_mode 非法")
    if source_type == "path" and not (path_prefix or "").strip():
        raise HTTPException(status_code=400, detail="path 合集必须给 path_prefix")
    if source_type != "path" and path_prefix:
        raise HTTPException(status_code=400, detail="path_prefix 只对 path 合集有效")
    if bound_person_id is not None:
        if source_type not in ("rule", "path", "preset"):
            raise HTTPException(status_code=400,
                                detail="人物绑定只对 rule/path/preset 合集有效")
        if db.query(em.EmbyPerson).filter(
                em.EmbyPerson.id == bound_person_id).first() is None:
            raise HTTPException(status_code=400, detail="绑定的人物行不存在")


@admin_media_sets_router.post("/media-sets")
def create_media_set(body: MediaSetCreate,
                     current_admin=Depends(get_current_admin),
                     db: Session = Depends(get_db)):
    _require_enabled()
    _check_set_fields(db, body.source_type, body.visibility_status,
                      body.artwork_mode, body.path_prefix, body.bound_person_id)
    media_set = em.EmbyMediaSet(
        name=body.name.strip(), source_type=body.source_type,
        visibility_status=body.visibility_status, sort_order=body.sort_order,
        min_effective_members=body.min_effective_members,
        artwork_mode=body.artwork_mode,
        path_prefix=(body.path_prefix.strip() if body.path_prefix else None),
        bound_person_id=body.bound_person_id)
    if not media_set.name:
        raise HTTPException(status_code=400, detail="合集名不能为空")
    db.add(media_set)
    db.commit()
    db.refresh(media_set)
    return ms.set_to_dict(media_set, db)


@admin_media_sets_router.post("/media-sets/from-hot-leaderboard")
def instantiate_from_hot_leaderboard(
        body: HotBoardInstantiate | None = None,
        current_admin=Depends(get_current_admin),
        db: Session = Depends(get_db)):
    """把当前热播榜快照成一份手工合集。

    排名直接用用户热播榜的 ``hot_leaderboard_items``（同一窗口、同一
    Top20、剧集按整剧聚合），不另写算法。每次调用都新建一份合集：
    没有稳定键、不更新已有合集——榜是时点快照，重复点击得到另一份，
    避免静默覆盖管理员改过的成员。
    榜为空，或排名条目都已缺失时不建行，返回 ``created=false``。
    成员 ``order_index`` 从 1 起按榜名次排，观影轨即榜序。
    默认名由榜种 + 周期 + 窗口日期组成（如「热播日榜 2026-10-27」），
    body.name 可覆盖。
    """
    _require_enabled()
    body = body or HotBoardInstantiate()
    if body.period not in _HOT_PERIOD_LABEL:
        raise HTTPException(status_code=400, detail="period 只能是 day 或 week")
    # 函数内导入：portal 很重，且只在这个动作里需要排名函数。
    from backend.emby_server.portal import (
        hot_leaderboard_items, leaderboard_window)

    window_naive, window_utc = leaderboard_window(body.period)
    ranked, _user_totals = hot_leaderboard_items(db, window_utc)
    ranked_ids = [row["item_id"] for row in ranked]
    if ranked_ids:
        # 排名查询与插入之间条目可能被删/被合并：缺的跳过，不让外键把请求打成 500。
        alive = {
            mid for (mid,) in db.query(em.MediaItem.id).filter(
                em.MediaItem.id.in_(ranked_ids),
                em.MediaItem.merged_into_id.is_(None),
            ).all()
        }
        ranked_ids = [iid for iid in ranked_ids if iid in alive]
    window_start = window_naive.isoformat()
    if not ranked_ids:
        return {
            "created": False,
            "reason": "leaderboard_empty",
            "board": "hot",
            "period": body.period,
            "window_start": window_start,
            "members": [],
        }
    if body.name is None:
        name = (f"热播{_HOT_PERIOD_LABEL[body.period]} "
                f"{window_naive.strftime('%Y-%m-%d')}")
    else:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="合集名不能为空")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="合集名过长")
    media_set = em.EmbyMediaSet(name=name, source_type="manual")
    db.add(media_set)
    db.flush()
    members = []
    for rank, item_id in enumerate(ranked_ids, start=1):
        member = em.EmbyMediaSetMember(
            set_id=media_set.id, item_id=item_id,
            order_index=rank, member_source="manual")
        db.add(member)
        members.append(member)
    ms.bump_requested(media_set)
    db.commit()
    db.refresh(media_set)
    for member in members:
        db.refresh(member)
    out = ms.set_to_dict(media_set, db)
    out["created"] = True
    out["board"] = "hot"
    out["period"] = body.period
    out["window_start"] = window_start
    out["members"] = [ms.member_to_dict(m) for m in members]
    return out


@admin_media_sets_router.get("/media-sets")
def list_media_sets(current_admin=Depends(get_current_admin),
                    db: Session = Depends(get_db)):
    _require_enabled()
    sets = db.query(em.EmbyMediaSet).order_by(
        em.EmbyMediaSet.sort_order, em.EmbyMediaSet.id).all()
    return [ms.set_to_dict(s, db) for s in sets]


@admin_media_sets_router.get("/media-sets/{set_id}")
def get_media_set(set_id: int, current_admin=Depends(get_current_admin),
                  db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    out = ms.set_to_dict(media_set, db)
    out["rules"] = [ms.rule_to_dict(r) for r in db.query(
        em.EmbyMediaSetRule).filter(
        em.EmbyMediaSetRule.set_id == set_id).all()]
    out["members"] = [ms.member_to_dict(m) for m in db.query(
        em.EmbyMediaSetMember).filter(
        em.EmbyMediaSetMember.set_id == set_id).order_by(
        em.EmbyMediaSetMember.order_index,
        em.EmbyMediaSetMember.id).all()]
    return out


@admin_media_sets_router.patch("/media-sets/{set_id}")
def patch_media_set(set_id: int, body: MediaSetPatch,
                    current_admin=Depends(get_current_admin),
                    db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    data = body.model_dump(exclude_unset=True)
    new_source = data.get("source_type", media_set.source_type)
    new_path = data.get("path_prefix", media_set.path_prefix)
    new_bound = data.get("bound_person_id", media_set.bound_person_id)
    _check_set_fields(db, new_source,
                      data.get("visibility_status", media_set.visibility_status),
                      data.get("artwork_mode", media_set.artwork_mode),
                      new_path, new_bound)
    membership_changed = (
        new_source != media_set.source_type
        or (new_path or None) != (media_set.path_prefix or None)
        or new_bound != media_set.bound_person_id)
    for key, value in data.items():
        if key == "name":
            value = (value or "").strip()
            if not value:
                raise HTTPException(status_code=400, detail="合集名不能为空")
        setattr(media_set, key, value)
    if membership_changed:
        ms.bump_requested(media_set)
    db.commit()
    db.refresh(media_set)
    return ms.set_to_dict(media_set, db)


@admin_media_sets_router.delete("/media-sets/{set_id}")
def delete_media_set(set_id: int, current_admin=Depends(get_current_admin),
                     db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    db.query(em.EmbyMediaSetMember).filter(
        em.EmbyMediaSetMember.set_id == set_id).delete()
    db.query(em.EmbyMediaSetRule).filter(
        em.EmbyMediaSetRule.set_id == set_id).delete()
    db.delete(media_set)
    db.commit()
    return {"deleted": set_id}


@admin_media_sets_router.post("/media-sets/order")
def order_media_sets(body: SetOrderBody,
                     current_admin=Depends(get_current_admin),
                     db: Session = Depends(get_db)):
    """合集设序专用接口：批量改合集的 sort_order（架序）。"""
    _require_enabled()
    for entry in body.orders:
        media_set = _get_set(db, entry.id)
        media_set.sort_order = entry.sort_order
    db.commit()
    return {"updated": len(body.orders)}


@admin_media_sets_router.post("/media-sets/{set_id}/members")
def add_member(set_id: int, body: MemberAdd,
               current_admin=Depends(get_current_admin),
               db: Session = Depends(get_db)):
    """手工挑成员：已存在则幂等更新 order_index（不重复建行）。"""
    _require_enabled()
    media_set = _get_set(db, set_id)
    item = db.query(em.MediaItem).filter(
        em.MediaItem.id == body.item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    member = db.query(em.EmbyMediaSetMember).filter(
        em.EmbyMediaSetMember.set_id == set_id,
        em.EmbyMediaSetMember.item_id == body.item_id).first()
    if member is None:
        member = em.EmbyMediaSetMember(set_id=set_id, item_id=body.item_id,
                                       order_index=body.order_index,
                                       member_source="manual")
        db.add(member)
        ms.bump_requested(media_set)
    else:
        member.order_index = body.order_index
    db.commit()
    db.refresh(member)
    return ms.member_to_dict(member)


@admin_media_sets_router.delete("/media-sets/{set_id}/members/{item_id}")
def remove_member(set_id: int, item_id: int,
                  current_admin=Depends(get_current_admin),
                  db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    member = db.query(em.EmbyMediaSetMember).filter(
        em.EmbyMediaSetMember.set_id == set_id,
        em.EmbyMediaSetMember.item_id == item_id).first()
    if member is None:
        raise HTTPException(status_code=404, detail="成员不存在")
    db.delete(member)
    ms.bump_requested(media_set)
    db.commit()
    return {"deleted": item_id}


@admin_media_sets_router.post("/media-sets/{set_id}/members/order")
def order_members(set_id: int, body: MemberOrderBody,
                  current_admin=Depends(get_current_admin),
                  db: Session = Depends(get_db)):
    """成员设序专用接口：批量改成员的 order_index（观影轨排序键）。"""
    _require_enabled()
    _get_set(db, set_id)
    for entry in body.orders:
        member = db.query(em.EmbyMediaSetMember).filter(
            em.EmbyMediaSetMember.set_id == set_id,
            em.EmbyMediaSetMember.item_id == entry.item_id).first()
        if member is None:
            raise HTTPException(status_code=400,
                                detail=f"条目 {entry.item_id} 不是本合集成员")
        member.order_index = entry.order_index
    db.commit()
    return {"updated": len(body.orders)}


@admin_media_sets_router.post("/media-sets/{set_id}/rules")
def add_rule(set_id: int, body: RuleCreate,
             current_admin=Depends(get_current_admin),
             db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    if media_set.source_type not in ("rule", "preset"):
        raise HTTPException(status_code=400, detail="规则只对 rule/preset 合集有效")
    try:
        checked = ms.validate_rule(body.rule_kind, body.is_exclusion,
                                   body.value_text, body.value_int,
                                   body.value_float)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if checked["rule_kind"] == "library" and db.query(em.Library).filter(
            em.Library.id == checked["value_int"]).first() is None:
        raise HTTPException(status_code=400, detail="规则引用的库不存在")
    rule = em.EmbyMediaSetRule(set_id=set_id, **checked)
    db.add(rule)
    ms.bump_requested(media_set)
    db.commit()
    db.refresh(rule)
    return ms.rule_to_dict(rule)


@admin_media_sets_router.delete("/media-sets/{set_id}/rules/{rule_id}")
def delete_rule(set_id: int, rule_id: int,
                current_admin=Depends(get_current_admin),
                db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    rule = db.query(em.EmbyMediaSetRule).filter(
        em.EmbyMediaSetRule.id == rule_id,
        em.EmbyMediaSetRule.set_id == set_id).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    db.delete(rule)
    ms.bump_requested(media_set)
    db.commit()
    return {"deleted": rule_id}


@admin_media_sets_router.post("/media-sets/{set_id}/refresh")
def refresh_set(set_id: int, current_admin=Depends(get_current_admin),
                db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    result = ms.refresh_media_set(db, media_set)
    result.update({"id": set_id,
                   "requested_revision": media_set.requested_revision,
                   "completed_revision": media_set.completed_revision})
    return result


@admin_media_sets_router.post("/media-set-templates")
def create_template(body: TemplateCreate,
                    current_admin=Depends(get_current_admin),
                    db: Session = Depends(get_db)):
    _require_enabled()
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名不能为空")
    try:
        rules = ms.validate_template_rules(body.rules)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if db.query(em.EmbyMediaSetTemplate).filter(
            em.EmbyMediaSetTemplate.name == name).first() is not None:
        raise HTTPException(status_code=400, detail="模板名已存在")
    template = em.EmbyMediaSetTemplate(name=name,
                                       rules_json=json.dumps(rules))
    db.add(template)
    db.commit()
    db.refresh(template)
    return ms.template_to_dict(template)


@admin_media_sets_router.get("/media-set-templates")
def list_templates(current_admin=Depends(get_current_admin),
                   db: Session = Depends(get_db)):
    _require_enabled()
    return [ms.template_to_dict(t) for t in db.query(
        em.EmbyMediaSetTemplate).order_by(em.EmbyMediaSetTemplate.id).all()]


@admin_media_sets_router.post("/media-set-templates/{template_id}/instantiate")
def instantiate_template(template_id: int, body: TemplateInstantiate,
                         current_admin=Depends(get_current_admin),
                         db: Session = Depends(get_db)):
    """实例化模板生成合集：规则集原样拷成规则行，合集 source=preset。

    实例化是管理端显式动作，顺手做一次刷新（不是定时任务），
    返回合集 + 本次刷新结果；模板本身不存条目清单。
    """
    _require_enabled()
    template = db.query(em.EmbyMediaSetTemplate).filter(
        em.EmbyMediaSetTemplate.id == template_id).first()
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    try:
        rules = ms.validate_template_rules(template.rules_json)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    name = (body.name or "").strip() or template.name
    media_set = em.EmbyMediaSet(name=name, source_type="preset")
    if body.min_effective_members is not None:
        media_set.min_effective_members = body.min_effective_members
    db.add(media_set)
    db.flush()
    for checked in rules:
        db.add(em.EmbyMediaSetRule(set_id=media_set.id, **checked))
    ms.bump_requested(media_set)
    db.commit()
    db.refresh(media_set)
    result = ms.refresh_media_set(db, media_set)
    out = ms.set_to_dict(media_set, db)
    out["refresh"] = result
    return out


# ---------------------------------------------------------------- 用户端读口


@user_media_sets_router.get("/collections/shelves")
def get_shelves(current_user=Depends(get_current_user),
                db: Session = Depends(get_db)):
    """架列表：真实库 + 合集区（合集按 sort_order 出架）。

    总闸关时本口惰性 404；现有 Emby 兼容架口（UserViews 等）
    本单一行未改，关闸时响应逐字不变。
    """
    _require_enabled()
    return ms.list_shelf(db)


@user_media_sets_router.get("/collections/{set_id}")
def get_collection(set_id: int, current_user=Depends(get_current_user),
                   db: Session = Depends(get_db)):
    _require_enabled()
    media_set = _get_set(db, set_id)
    if media_set.visibility_status != "visible":
        raise HTTPException(status_code=404, detail="合集不存在")
    out = ms.set_to_dict(media_set, db)
    out.pop("requested_revision", None)
    out.pop("completed_revision", None)
    out.pop("refresh_status", None)
    return out


@user_media_sets_router.get("/collections/{set_id}/items")
def get_collection_items(set_id: int, track: str = "release",
                         limit: int = 50, offset: int = 0,
                         current_user=Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """合集成员列表（双轨）：track=release 上映序 / watch 观影序。

    露/藏与 min 门都在这里实时算：hidden 合集按不存在处理；
    有效成员未达 min 门时返回空 items + shelf_eligible=false，
    不谎称有内容。封面只出 artwork_mode，不出图 URL。
    """
    _require_enabled()
    if track not in ("release", "watch"):
        raise HTTPException(status_code=400, detail="track 须为 release/watch")
    limit, offset = pagination_params(limit, offset)
    media_set = _get_set(db, set_id)
    if media_set.visibility_status != "visible":
        raise HTTPException(status_code=404, detail="合集不存在")
    eligible = ms.shelf_eligible(db, media_set)
    effective = ms.effective_count(db, media_set)
    if not eligible:
        return {"id": set_id, "name": media_set.name, "track": track,
                "artwork_mode": media_set.artwork_mode,
                "shelf_eligible": False, "effective_count": effective,
                "min_effective_members": media_set.min_effective_members,
                "items": [], "total": 0}
    members = db.query(em.EmbyMediaSetMember).filter(
        em.EmbyMediaSetMember.set_id == set_id).all()
    member_by_item = {m.item_id: m for m in members}
    items = ms.visible_items_query(db, set_id).all()
    pairs = [(member_by_item[i.id], i) for i in items if i.id in member_by_item]
    ordered = ms.order_members(pairs, track)
    page = ordered[offset:offset + limit]
    return {"id": set_id, "name": media_set.name, "track": track,
            "artwork_mode": media_set.artwork_mode,
            "shelf_eligible": True, "effective_count": effective,
            "min_effective_members": media_set.min_effective_members,
            "items": [_item_dict(i, m.order_index) for m, i in page],
            "total": len(ordered)}

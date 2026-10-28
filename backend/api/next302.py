"""302 Next 用户端路由（MVP 非商店部分，默认关）。

挂点：用户端搜索与详情（/api/user/next302/*），只挂独立搜索页，
不碰首页/发现页现有区域，不碰商店文件（store.py / store_contract.py
是另一单的，B1 入口与想看按钮不在这里）。

总闸 ``NEXT302_ENABLED`` 默认关：关时本组接口惰性 404（路由级依赖
先于鉴权，未登录也不会先露 401），旧链一字不动。搜索口另有子开关
``NEXT302_SEARCH_ENABLED``，只在总闸开时才生效。

口径（与设计文档 §四一致）：
- 搜索只读本地数据集 + 库内混排，不在请求时现调 TMDB；库可见范围
  读失败时 fail-closed（数据集行照常回，但 ``library_known=False``，
  前端不许把任何一行标成「库外」）。
- 详情分两种：库内（有 library_item_id，可走既有播放链）与库外
  （只展示，不给播放入口；想看入口是商店那单的 B1，不在这里造
  一个假按钮）。
- 播放状态口（E4）只读：不建任务、不改行、不触发转存。它解释
  「这部片现在能不能播、为什么」，建任务的动作在商店
  （#1412 已合署，POST /want），本单只给文字指引，不造假按钮。
- 额度爆状态不在本口推导：没有可读的 quota 信号表，硬编一个
  「额度爆」等于说谎；额度命中时既有播放链的 409 文案本身就是
  额度说明，本口不重复造一个。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend import library_scope
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import next302_gate as gate
from backend.emby_server import next302_obs as obs
from backend.emby_server import playback_gate
from backend.emby_server import tmdb_dataset as dataset
from backend.emby_server import transfer_variant as tv
from backend.emby_server.next302_play_route import decide_route


def require_next302_enabled() -> None:
    """总闸。关时 404，避免未开启的路径先被鉴权依赖回答成 401。"""
    if not gate.next302_enabled():
        raise HTTPException(status_code=404, detail="302 Next 未开启")


def require_search_enabled() -> None:
    """搜索子开关。只在总闸开时才生效，关时同样 404。"""
    if not gate.search_enabled():
        raise HTTPException(status_code=404, detail="302 Next 搜索未开启")


router = APIRouter(
    prefix="/api/user/next302",
    tags=["用户端·302 Next"],
    # 插在端点依赖之前：总闸关时先 404，鉴权依赖不会先回答 401。
    dependencies=[Depends(require_next302_enabled)],
)


def _scope(db: Session, user) -> tuple[set[int] | None, bool]:
    """库可见范围（按 ``effective_ids`` 原口径，绝不猜）。

    返回 (allowed, known)：
    - known=False：范围读失败，调用方按「没算出来」处理（搜索只回
      数据集行且不标库内/库外；单条状态口直接 403）。
    - known=True 且 allowed=None：工作人员豁免 / 不过滤。
    - known=True 且 allowed 为集合：只认该集合内的库。
    """
    try:
        allowed = library_scope.effective_ids(db, user)
    except Exception:  # noqa: BLE001 — 范围读失败 = 没算出来，不是空范围
        return None, False
    return (set(allowed) if allowed is not None else None), True


@router.get("/search")
def search(q: str = Query(..., min_length=1, max_length=200),
           type: str = Query("all"),
           limit: int = Query(20, ge=1, le=100),
           user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_search_enabled()
    media_type = type if type in ("movie", "tv") else None
    allowed, known = _scope(db, user)
    result = dataset.search_local(db, q, media_type=media_type,
                                  limit=limit,
                                  allowed_library_ids=allowed,
                                  library_known=known)
    obs.log_event("next302_search", media_type=type,
                  result_total=result["total"],
                  library_known=known)
    return result


@router.get("/details/{media_type}/{tmdb_id}")
def details(media_type: str, tmdb_id: str,
            user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_search_enabled()
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=404, detail="未找到该条目")
    row = (
        db.query(em.TmdbDatasetItem)
        .filter(em.TmdbDatasetItem.tmdb_id == tmdb_id.strip(),
                em.TmdbDatasetItem.media_type == media_type)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="数据集里还没有这部片子")
    allowed, known = _scope(db, user)
    lib = None
    if known:
        lib_query = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.tmdb_id == tmdb_id.strip(),
                    em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None))
        )
        if allowed is not None:
            lib_query = lib_query.filter(
                em.MediaItem.library_id.in_(allowed))
        lib = lib_query.order_by(em.MediaItem.id).first()
    out = dataset._dataset_dict(
        row, in_library=lib is not None,
        library_item_id=lib.id if lib is not None else None,
        library_known=known)
    # 库外详情绝不给播放入口：本响应里没有播放地址字段，前端也不渲染。
    obs.log_event("next302_details", media_type=media_type,
                  in_library=out["in_library"])
    return out


@router.get("/play-status")
def play_status(item_id: int = Query(..., ge=1),
                user=Depends(get_current_user),
                db: Session = Depends(get_db)):
    """E4 播放状态口（只读）：这部库内片现在能不能播、为什么。

    状态与动作（action）只给文字指引，不建任务：
    - play：有本人 ready 副本，走既有播放链。
    - wait：转存进行中，转完才可播。
    - failed：转存失败，不播共享原件。
    - stale：用户副本已失效，重新转存后再播。
    - want：还没有自己的副本，去想看建转存任务（商店 POST /want）。
    - bind：未绑定，去绑定自己的盘后才能点播。
    """
    require_search_enabled()
    allowed, known = _scope(db, user)
    if not known:
        raise HTTPException(status_code=403, detail="库可见范围读失败，暂不提供状态")
    item_query = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.id == item_id,
                em.MediaItem.deleted_at.is_(None),
                em.MediaItem.merged_into_id.is_(None))
    )
    if allowed is not None:
        item_query = item_query.filter(
            em.MediaItem.library_id.in_(allowed))
    item = item_query.first()
    if item is None:
        raise HTTPException(status_code=404, detail="未找到该条目")
    user_id = getattr(user, "id", None)
    bound = playback_gate.has_active_binding(db, user_id) \
        if user_id is not None else False
    user_variants = (
        db.query(em.TransferVariant)
        .filter(em.TransferVariant.item_id == item.id,
                em.TransferVariant.owner_scope == "user",
                em.TransferVariant.owner_ref == str(user_id))
        .all()
    ) if user_id is not None else []
    ready = [v for v in user_variants
             if v.status == "ready" and (v.stale_count or 0) < 2]
    stale = [v for v in user_variants if v not in ready]
    job = tv._latest_job(db, item.id, "user", str(user_id)) \
        if user_id is not None else None
    job_status = getattr(job, "status", None) if job is not None else None
    verdict = decide_route(
        bound=bound,
        has_user_ready_copy=bool(ready),
        user_copy_stale=bool(stale) and not bool(ready),
        user_job_status=job_status)
    route = verdict["route"]
    action = {
        "user_copy": "play",
        "preparing": "wait",
        "failed": "failed",
        "stale_copy": "want",
        "quota_guide": "want",
        "no_copy_guide": "want",
        "bind_guide": "bind",
    }[route]
    obs.log_event("next302_play_status", route=route, item_id=item.id)
    return {
        "item_id": item.id,
        "route": route,
        "detail": verdict["detail"],
        "action": action,
        "has_binding": bound,
        "has_user_ready_copy": bool(ready),
        "job_status": job_status,
    }

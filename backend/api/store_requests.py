"""想看商店求片 R1：登记 + 榜单 + 入库联动（路由层）。

总闸与会员闸复用商店骨架（``backend.api.store``）：
- 总闸 ``STORE_ENABLED`` 默认关，关时本组接口惰性 404（路由级依赖
  先于鉴权，未登录也不会先露 401），与商店同款。
- 登记与我的列表限会员（``require_store_member``，与商店 B2 同款）。

入库联动是“读时同步”，不是后台任务：
- 读我的列表 / 榜单 / 管理端榜单时，对处理中（pending/approved）
  且 tmdb 对得上库内的行，当场标为 completed（只翻状态，不建
  转存任务、不发通知）。入库后用户下一次打开列表/榜单即看到
  已入库，随后可走正常想看流程（POST /want 的 R0 路）。
- emby_item_id 的 guid 记录仍归既有管理端“标记已入库”入口，
  本模块不同步写它，免得两处口径打架。

不做的事（写明，免得后人以为漏了）：
- 不自动下片、不自动寻源、不接站外下载（那是 R2，另批才议）。
- 不套 MediaSeek 的每日/月度额度与 seek_enabled/vote_enabled 开关。
- 管理端改状态（批准/拒绝/标记已入库）走既有 MediaSeek 入口，
  本模块的管理端榜单只读。
- 前端入口未接：路由与 StoreView 在 #1412 文件清单内，本单不碰；
  待 302 搜索页或后续单接。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import media_seek, models
from backend.api.admin_core import get_current_admin
from backend.api.store import (
    find_library_item,
    require_store_enabled,
    require_store_member,
)
from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server.store_requests_contract import (
    STATUS_TO_R1_STATE,
    REQUEST_ALREADY_REGISTERED_MESSAGE,
    REQUEST_IN_LIBRARY_MESSAGE,
    REQUEST_INVALID_TMDB_MESSAGE,
    REQUEST_REGISTERED_MESSAGE,
    REQUEST_TITLE_TOO_LONG_MESSAGE,
    REQUEST_VOTED_MESSAGE,
    RequestIn,
    RequestListResponse,
    RequestRecord,
)

#: 榜单与列表的单页上限（防全表扫）。
BOARD_LIMIT = 50
MY_LIST_LIMIT = 100
ADMIN_LIST_LIMIT = 100
#: 同步时多读的余量：部分行同步后掉出榜单，余量补位。
SYNC_READ_EXTRA = 150

router = APIRouter(
    prefix="/api/user/store",
    tags=["用户端·想看商店求片"],
    # 总闸关时先 404，鉴权依赖不会先回答 401（与商店同款）。
    dependencies=[Depends(require_store_enabled)],
)

admin_router = APIRouter(
    prefix="/api/admin/store",
    tags=["管理端·想看商店求片"],
    dependencies=[Depends(require_store_enabled)],
)


def _r1_state(status: str) -> str:
    """MediaSeek 原始 status → R1 展示态；未知 status 原样回。"""
    return STATUS_TO_R1_STATE.get(status or "", status or "")


def _recount_votes(db: Session, request_id: int) -> int:
    """附议数 = 附议行实数（插行后重算，防并发丢数）。"""
    return (
        db.query(func.count(models.MovieRequestVote.id))
        .filter(models.MovieRequestVote.request_id == request_id)
        .scalar()
        or 0
    )


def _sync_row(db: Session, row: models.MovieRequest) -> bool:
    """读时入库联动：处理中的行若 tmdb 已对上库内，当场标 completed。

    只翻状态：不建转存任务、不发通知、不写 emby_item_id（guid 记录
    归既有管理端“标记已入库”入口）。返回是否翻了状态。
    """
    if row.status not in media_seek.ACTIVE_STATUSES:
        return False
    if not row.tmdb_id:
        return False
    if find_library_item(db, row.tmdb_id) is None:
        return False
    row.status = "completed"
    return True


def _library_hit(db: Session, tmdb_id: str | None):
    """tmdb → (是否在库, 库内条目 id)。"""
    if not tmdb_id:
        return False, None
    row = find_library_item(db, tmdb_id)
    if row is None:
        return False, None
    return True, row.id


def _record(row: models.MovieRequest, *, in_library: bool,
            library_item_id: int | None, voted: bool,
            message: str) -> RequestRecord:
    return RequestRecord(
        request_id=row.id,
        tmdb_id=row.tmdb_id or "",
        movie_name=row.movie_name,
        vote_count=row.vote_count or 0,
        status=row.status,
        state=_r1_state(row.status),
        in_library=in_library,
        library_item_id=library_item_id,
        voted=voted,
        message=message,
    )


def _voted_ids(db: Session, user_id: int, request_ids: list[int]) -> set[int]:
    """当前用户在给定求片 id 里登记/附议过哪些（一次查询打标）。"""
    if not request_ids:
        return set()
    rows = (
        db.query(models.MovieRequestVote.request_id)
        .filter(
            models.MovieRequestVote.user_id == user_id,
            models.MovieRequestVote.request_id.in_(request_ids),
        )
        .all()
    )
    return {row[0] for row in rows}


@router.post("/requests", response_model=RequestRecord)
def post_request(
    body: RequestIn,
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestRecord:
    """会员求片登记：库外 tmdb 登记或附议；库内不登记、指去想看。

    - tmdb 已在库 → 不建行，回 in_library 态（request_id 为 None），
      客户端应引导去想看。
    - 同一 tmdb 已有处理中行 → 附议：没投过则 +1，已投过原样回
      （人数不重复算）；行若已入库（读时同步翻出）则按在库处理。
    - 否则新建 pending 行，创建人计 1 票（附议行与 vote_count 同落）。
    """
    require_store_member(db, user)
    key = media_seek.normalize_tmdb_id(body.tmdb_id)
    if not key:
        raise HTTPException(status_code=400, detail=REQUEST_INVALID_TMDB_MESSAGE)
    title = (body.title or "").strip()
    if len(title) > 255:
        raise HTTPException(status_code=400, detail=REQUEST_TITLE_TOO_LONG_MESSAGE)

    in_library, library_item_id = _library_hit(db, key)
    if in_library:
        return RequestRecord(
            request_id=None,
            tmdb_id=key,
            movie_name=title or None,
            vote_count=0,
            status="",
            state="in_library",
            in_library=True,
            library_item_id=library_item_id,
            voted=False,
            message=REQUEST_IN_LIBRARY_MESSAGE,
        )

    existing = (
        db.query(models.MovieRequest)
        .filter(
            models.MovieRequest.tmdb_id == key,
            models.MovieRequest.status.in_(media_seek.ACTIVE_STATUSES),
        )
        .order_by(models.MovieRequest.id)
        .first()
    )
    if existing is not None:
        # 同步检查：两次打开之间入库了，附议不再落到旧行上。
        if _sync_row(db, existing):
            db.commit()
            db.refresh(existing)
            in_library, library_item_id = _library_hit(db, key)
            return RequestRecord(
                request_id=None,
                tmdb_id=key,
                movie_name=existing.movie_name,
                vote_count=existing.vote_count or 0,
                status="completed",
                state="stored",
                in_library=in_library,
                library_item_id=library_item_id,
                voted=False,
                message=REQUEST_IN_LIBRARY_MESSAGE,
            )
        voted = (
            db.query(models.MovieRequestVote)
            .filter(
                models.MovieRequestVote.request_id == existing.id,
                models.MovieRequestVote.user_id == user.id,
            )
            .first()
            is not None
        )
        if voted:
            return _record(
                existing, in_library=False, library_item_id=None,
                voted=True, message=REQUEST_ALREADY_REGISTERED_MESSAGE,
            )
        db.add(models.MovieRequestVote(request_id=existing.id, user_id=user.id))
        db.flush()
        existing.vote_count = _recount_votes(db, existing.id)
        db.commit()
        db.refresh(existing)
        return _record(
            existing, in_library=False, library_item_id=None,
            voted=True, message=REQUEST_VOTED_MESSAGE,
        )

    row = models.MovieRequest(
        user_id=user.id,
        movie_name=title or key,
        tmdb_id=key,
        status="pending",
        vote_count=1,
    )
    db.add(row)
    db.flush()
    db.add(models.MovieRequestVote(request_id=row.id, user_id=user.id))
    db.commit()
    db.refresh(row)
    return _record(
        row, in_library=False, library_item_id=None,
        voted=True, message=REQUEST_REGISTERED_MESSAGE,
    )


@router.get("/requests", response_model=RequestListResponse)
def get_my_requests(
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestListResponse:
    """会员自己的求片：创建的 + 附议过的（tmdb 绑定行），含已入库态。

    读时同步：处理中的行若已入库当场翻 completed，列表即看到已入库。
    """
    require_store_member(db, user)
    voted_stmt = (
        select(models.MovieRequestVote.request_id)
        .where(models.MovieRequestVote.user_id == user.id)
    )
    rows = (
        db.query(models.MovieRequest)
        .filter(
            models.MovieRequest.tmdb_id.isnot(None),
            models.MovieRequest.tmdb_id != "",
            (
                (models.MovieRequest.user_id == user.id)
                | (models.MovieRequest.id.in_(voted_stmt))
            ),
        )
        .order_by(models.MovieRequest.created_at.desc())
        .limit(MY_LIST_LIMIT)
        .all()
    )
    changed = False
    for row in rows:
        changed = _sync_row(db, row) or changed
    if changed:
        db.commit()
    voted_set = _voted_ids(db, user.id, [row.id for row in rows])
    items = []
    for row in rows:
        in_library, library_item_id = _library_hit(db, row.tmdb_id)
        items.append(_record(
            row, in_library=in_library, library_item_id=library_item_id,
            voted=(row.user_id == user.id or row.id in voted_set),
            message="",
        ))
    return RequestListResponse(items=items)


@router.get("/requests/board", response_model=RequestListResponse)
def get_board(
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestListResponse:
    """会员可见求片榜：处理中的行按附议数排榜（人数 = 热度）。

    读时同步会把已入库的行翻出榜单（它们不再是“求片”，应去想看）。
    """
    require_store_member(db, user)
    rows = (
        db.query(models.MovieRequest)
        .filter(
            models.MovieRequest.tmdb_id.isnot(None),
            models.MovieRequest.tmdb_id != "",
            models.MovieRequest.status.in_(media_seek.ACTIVE_STATUSES),
        )
        .order_by(
            models.MovieRequest.vote_count.desc(),
            models.MovieRequest.created_at.desc(),
        )
        .limit(BOARD_LIMIT + SYNC_READ_EXTRA)
        .all()
    )
    changed = False
    for row in rows:
        changed = _sync_row(db, row) or changed
    if changed:
        db.commit()
    active = [row for row in rows if row.status in media_seek.ACTIVE_STATUSES]
    active = active[:BOARD_LIMIT]
    voted_set = _voted_ids(db, user.id, [row.id for row in active])
    items = []
    for row in active:
        in_library, library_item_id = _library_hit(db, row.tmdb_id)
        items.append(_record(
            row, in_library=in_library, library_item_id=library_item_id,
            voted=(row.user_id == user.id or row.id in voted_set),
            message="",
        ))
    return RequestListResponse(items=items)


@admin_router.get("/requests", response_model=RequestListResponse)
def get_admin_requests(
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> RequestListResponse:
    """管理端可见全部登记：含已入库 / 已拒绝 / 已撤回，按附议数排。

    只读：改状态（批准/拒绝/标记已入库）走既有 MediaSeek 入口，
    本模块不开写入口。
    """
    rows = (
        db.query(models.MovieRequest)
        .filter(
            models.MovieRequest.tmdb_id.isnot(None),
            models.MovieRequest.tmdb_id != "",
        )
        .order_by(
            models.MovieRequest.vote_count.desc(),
            models.MovieRequest.created_at.desc(),
        )
        .limit(ADMIN_LIST_LIMIT + SYNC_READ_EXTRA)
        .all()
    )
    changed = False
    for row in rows:
        changed = _sync_row(db, row) or changed
    if changed:
        db.commit()
    items = []
    for row in rows[:ADMIN_LIST_LIMIT]:
        in_library, library_item_id = _library_hit(db, row.tmdb_id)
        items.append(_record(
            row, in_library=in_library, library_item_id=library_item_id,
            voted=False, message="",
        ))
    return RequestListResponse(items=items)

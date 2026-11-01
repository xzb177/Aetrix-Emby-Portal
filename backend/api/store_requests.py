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

import hashlib
import threading
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
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


def _tmdb_lock_key(tmdb_id: str) -> int:
    """登记串行锁的 64 位 key：按 tmdb 派生，与订阅发放锁的 key 域隔离。"""
    return int.from_bytes(
        hashlib.sha256(f"aetrix-store-request:{tmdb_id}".encode()).digest()[:8],
        "big", signed=True,
    )


_tmdb_key_locks: dict[str, threading.Lock] = {}
_tmdb_key_locks_guard = threading.Lock()


def _tmdb_key_lock(tmdb_id: str) -> threading.Lock:
    with _tmdb_key_locks_guard:
        lock = _tmdb_key_locks.get(tmdb_id)
        if lock is None:
            lock = threading.Lock()
            _tmdb_key_locks[tmdb_id] = lock
        return lock


@contextmanager
def tmdb_request_lock(db: Session, tmdb_id: str):
    """同一 tmdb 登记的「先查处理中行再新建」串行锁（PA-026）。

    行锁锁不住「不存在的行」：两并发请求同时查不到处理中行，会各自
    新建 pending 行、附议被拆散。照订阅发放锁（economy.py 的
    ``subscription_grant_lock``）同款：PG 用事务级 advisory lock（随事
    务提交/回滚自动释放，第二个事务的检查一定能看到第一个已提交的行）；
    SQLite/测试用按 tmdb 分键的进程内锁。用户行锁粒度不够——两用户
    同 tmdb 也会撞，所以不用 ``lock_user_row``。
    """
    try:
        is_pg = db.bind.dialect.name == "postgresql"
    except Exception:  # noqa: BLE001
        is_pg = False
    if is_pg:
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                   {"k": _tmdb_lock_key(tmdb_id)})
        yield
    else:
        with _tmdb_key_lock(tmdb_id):
            yield


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
            message: str, weighted_vote_count: int) -> RequestRecord:
    return RequestRecord(
        request_id=row.id,
        tmdb_id=row.tmdb_id or "",
        movie_name=row.movie_name,
        vote_count=row.vote_count or 0,
        # Step 1 加权附议：原始人数仍见 vote_count；榜单排序与加权数走这里
        weighted_vote_count=weighted_vote_count,
        status=row.status,
        state=_r1_state(row.status),
        in_library=in_library,
        library_item_id=library_item_id,
        voted=voted,
        message=message,
    )


def _append_vote(db: Session, row: models.MovieRequest,
                 user_id: int) -> tuple[str, models.MovieRequest | None]:
    """给处理中行附议一票（并发安全），返回 (结果, 行)。

    结果四档：
    - ``"voted"``：本次落票成功；
    - ``"already"``：并发请求已先落下同一人的票，本次不重复计；
    - ``"gone"``：行并发消失，调用方重走新建支；
    - ``"limited"``：今日附议已达上限（Step 1，默认 20 次，按当日现存
      附议行计，取消删行即释放名额），本次不落票。
    先查后插在并发下会撞唯一约束 ``uq_vote_request_user``（两个请求
    同时看到“没投过”又同时插行）：撞上不 500，回滚后重查——对方已
    落票则按已登记回；重查没票（极少数假阳性）则重试一次，第二次
    再撞约束仍按已登记回（约束本身就是“已有人投过”的证据）。
    """
    # Step 1：每日附议上限（toggle_vote 同口径；创建人首票走新建支，不走这里）
    if media_seek.daily_votes_used(db, user_id) >= media_seek.daily_vote_limit(db):
        return "limited", row
    voter = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    weight = media_seek.vote_weight_for_user(db, voter, row) if voter is not None else 1
    for _attempt in range(2):
        try:
            db.add(models.MovieRequestVote(
                request_id=row.id, user_id=user_id, weight_snapshot=weight,
            ))
            db.flush()
            row.vote_count = _recount_votes(db, row.id)
            db.commit()
            return "voted", row
        except IntegrityError:
            db.rollback()
            fresh = (
                db.query(models.MovieRequest)
                .filter(models.MovieRequest.id == row.id)
                .first()
            )
            if fresh is None:
                return "gone", None
            row = fresh
            voted = (
                db.query(models.MovieRequestVote)
                .filter(
                    models.MovieRequestVote.request_id == row.id,
                    models.MovieRequestVote.user_id == user_id,
                )
                .first()
                is not None
            )
            if voted:
                return "already", row
    return "already", row


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

    with tmdb_request_lock(db, key):
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
                    weighted_vote_count=media_seek.weighted_votes_for(
                        db, [existing.id]).get(existing.id, 0),
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
                    weighted_vote_count=media_seek.weighted_votes_for(
                        db, [existing.id]).get(existing.id, 0),
                )
            outcome, existing = _append_vote(db, existing, user.id)
            if outcome == "gone":
                # 行并发消失：落到下面的新建支，重新登记一行。
                pass
            elif outcome == "limited":
                # Step 1：今日附议已达上限（与 toggle_vote 同文案，走 400）
                raise HTTPException(status_code=400, detail="今日附议已达上限")
            elif outcome == "already":
                return _record(
                    existing, in_library=False, library_item_id=None,
                    voted=True, message=REQUEST_ALREADY_REGISTERED_MESSAGE,
                    weighted_vote_count=media_seek.weighted_votes_for(
                        db, [existing.id]).get(existing.id, 0),
                )
            else:
                return _record(
                    existing, in_library=False, library_item_id=None,
                    voted=True, message=REQUEST_VOTED_MESSAGE,
                    weighted_vote_count=media_seek.weighted_votes_for(
                        db, [existing.id]).get(existing.id, 0),
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
        # Step 1：创建人首票同样按其等级权重快照计（与附议同口径）
        db.add(models.MovieRequestVote(
            request_id=row.id, user_id=user.id,
            weight_snapshot=media_seek.vote_weight_for_user(db, user, row),
        ))
        db.commit()
        db.refresh(row)
        return _record(
            row, in_library=False, library_item_id=None,
            voted=True, message=REQUEST_REGISTERED_MESSAGE,
            weighted_vote_count=media_seek.weighted_votes_for(
                db, [row.id]).get(row.id, 0),
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
    weighted_map = media_seek.weighted_votes_for(db, [row.id for row in rows])
    items = []
    for row in rows:
        in_library, library_item_id = _library_hit(db, row.tmdb_id)
        items.append(_record(
            row, in_library=in_library, library_item_id=library_item_id,
            voted=(row.user_id == user.id or row.id in voted_set),
            message="",
            weighted_vote_count=weighted_map.get(row.id, 0),
        ))
    return RequestListResponse(items=items)


@router.get("/requests/board", response_model=RequestListResponse)
def get_board(
    user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RequestListResponse:
    """会员可见求片榜：处理中的行按加权附议和排榜（Step 1；原始人数另列）。

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
            media_seek.weighted_vote_ordering().desc(),
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
    weighted_map = media_seek.weighted_votes_for(db, [row.id for row in active])
    items = []
    for row in active:
        in_library, library_item_id = _library_hit(db, row.tmdb_id)
        items.append(_record(
            row, in_library=in_library, library_item_id=library_item_id,
            voted=(row.user_id == user.id or row.id in voted_set),
            message="",
            weighted_vote_count=weighted_map.get(row.id, 0),
        ))
    return RequestListResponse(items=items)


@admin_router.get("/requests", response_model=RequestListResponse)
def get_admin_requests(
    admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> RequestListResponse:
    """管理端可见全部登记：含已入库 / 已拒绝 / 已撤回，按加权附议和排。

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
            media_seek.weighted_vote_ordering().desc(),
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
    page = rows[:ADMIN_LIST_LIMIT]
    weighted_map = media_seek.weighted_votes_for(db, [row.id for row in page])
    items = []
    for row in page:
        in_library, library_item_id = _library_hit(db, row.tmdb_id)
        items.append(_record(
            row, in_library=in_library, library_item_id=library_item_id,
            voted=False, message="",
            weighted_vote_count=weighted_map.get(row.id, 0),
        ))
    return RequestListResponse(items=items)

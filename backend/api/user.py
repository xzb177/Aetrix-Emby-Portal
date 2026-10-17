"""
用户端 API 路由
与管理后台联动，用户可接收管理员操作的通知
"""
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional
from datetime import datetime, timedelta
import logging

from sqlalchemy import func

from backend.database import get_db
from backend import codes, devices, media_seek, models, realms
from backend.emby_server.api import season_poster_url
from backend.db_retry import commit_with_retry
from backend.notifications import get_notification_service, AdminEvent
from backend.ratelimit import check_rate_limit
from backend.security import resolve_jwt_user_id

logger = logging.getLogger(__name__)

# 创建路由
user_router = APIRouter(prefix="/api/user", tags=["用户端"])

# 认证方案
security = HTTPBearer(auto_error=False)


# ==================== 依赖注入 ====================

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> models.WebUser:
    """获取当前登录用户（JWT access token 鉴权）"""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭证"
        )

    user_id = resolve_jwt_user_id(credentials.credentials)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或已过期的凭证"
        )

    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在"
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用"
        )

    # P2/P3（审查第八批）：与门户鉴权同一套新鲜度检查；
    # 改密/登出后旧 token 在这里同样作废，不许出现"部分接口仍认旧 token"。
    from backend.security import decode_token, enforce_token_freshness
    payload = decode_token(credentials.credentials, expected_type="access")
    enforce_token_freshness(db, payload, user)

    return user


# ==================== 请求/响应模型 ====================

class MessageResponse(BaseModel):
    """站内消息响应"""
    id: int
    title: str
    content: str
    message_type: str
    related_id: Optional[int]
    is_read: bool
    created_at: str
    from_user: Optional[str] = None

    class Config:
        from_attributes = True


class UnreadCountResponse(BaseModel):
    """未读消息数响应"""
    unread_count: int


class TicketCreateRequest(BaseModel):
    """工单创建请求"""
    title: str
    category: str = "other"
    message: str


class TicketReplyRequest(BaseModel):
    """工单回复请求（回复无需标题，此前误用创建模型，前端不得不塞占位 title）"""
    message: str


class TicketCreateResponse(BaseModel):
    """工单创建响应"""
    success: bool
    ticket_id: int
    message: str


class TicketResponse(BaseModel):
    """工单响应"""
    id: int
    title: str
    category: str
    status: str
    priority: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class TicketMessageResponse(BaseModel):
    """工单消息响应"""
    id: int
    message: str
    is_admin: bool
    created_at: str
    admin_name: Optional[str] = None

    class Config:
        from_attributes = True


class AnnouncementResponse(BaseModel):
    """公告响应"""
    id: int
    title: str
    content: str
    type: str
    is_pinned: bool
    created_at: str

    class Config:
        from_attributes = True


# ==================== 认证/用户信息 API ====================

# /auth/me 已迁移至 backend/api/emby_portal.py（JWT 鉴权版，见 auth_router）


# ==================== 站内消息 API ====================

@user_router.get("/messages", response_model=List[MessageResponse])
async def get_messages(
    unread_only: bool = False,
    limit: int = 50,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取站内消息列表

    ``limit`` 收口到 1..200：这是用户可控的查询参数，不夹一下就能一次把
    整个收件箱拉走（消息中心自己没有翻页，所以给了上限也不会有人取不到旧消息）。
    """
    notification_service = get_notification_service()

    messages = await notification_service.get_messages(
        user_id=current_user.id,
        limit=max(1, min(limit, 200)),
        unread_only=unread_only,
        db=db
    )

    result = []
    for msg in messages:
        from_name = None
        if msg.from_user:
            from_name = msg.from_user.username

        result.append(MessageResponse(
            id=msg.id,
            title=msg.title,
            content=msg.content,
            message_type=msg.message_type,
            related_id=msg.related_id,
            is_read=msg.is_read,
            created_at=msg.created_at.isoformat(),
            from_user=from_name
        ))

    return result


@user_router.get("/messages/unread-count", response_model=UnreadCountResponse)
async def get_unread_count(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取未读消息数量"""
    notification_service = get_notification_service()

    count = await notification_service.get_unread_count(
        user_id=current_user.id,
        db=db
    )

    return UnreadCountResponse(unread_count=count)


@user_router.post("/messages/{message_id}/read")
async def mark_message_read(
    message_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """标记消息为已读"""
    notification_service = get_notification_service()

    success = await notification_service.mark_as_read(
        message_id=message_id,
        user_id=current_user.id,
        db=db
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="消息不存在"
        )

    return {"success": True, "message": "消息已标记为已读"}


@user_router.post("/messages/read-all")
async def mark_all_read(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """标记所有消息为已读（批量 UPDATE 下放线程池，实时推送仍 await）"""
    user_id = current_user.id

    def _mark_all() -> None:
        # 批量更新未读消息
        db.query(models.StationMessage).filter(
            models.StationMessage.to_user_id == user_id,
            models.StationMessage.is_read == False
        ).update({
            "is_read": True,
            "read_at": datetime.now()
        })
        db.commit()

    await run_in_threadpool(_mark_all)

    # 通知 WebSocket
    from backend.websocket import send_notification
    await send_notification(
        notification_type="station.unread_count",
        user_id=user_id,
        title="",
        message="",
        data={"unread_count": 0}
    )

    return {"success": True, "message": "所有消息已标记为已读"}


# ==================== 兑换码 API ====================

# ==================== 工单 API ====================

@user_router.get("/tickets", response_model=List[TicketResponse])
def get_my_tickets(
    status_filter: Optional[str] = None,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取我的工单列表"""
    query = db.query(models.Ticket).filter(
        models.Ticket.user_id == current_user.id
    )

    if status_filter:
        query = query.filter(models.Ticket.status == status_filter)

    tickets = query.order_by(models.Ticket.updated_at.desc()).all()

    return [
        TicketResponse(
            id=t.id,
            title=t.title,
            category=t.category,
            status=t.status,
            priority=t.priority,
            created_at=t.created_at.isoformat(),
            updated_at=t.updated_at.isoformat()
        )
        for t in tickets
    ]


@user_router.post("/tickets", response_model=TicketCreateResponse)
async def create_ticket(
    request: TicketCreateRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """创建新工单 - 与后台工单系统联动（落库下放线程池，通知仍 await）"""
    user_id = current_user.id
    username = current_user.username   # 提交后回读属性=隐式回查，先在循环上取成纯值

    def _create() -> int:
        # F-P1-26 修复：工单与首条消息同一事务提交。原先两次 commit，
        # 第二次提交前异常会留下无首条消息的空工单，用户端展示异常。
        ticket = models.Ticket(
            user_id=user_id,
            title=request.title,
            category=request.category,
            status="open",
            priority="medium"
        )
        db.add(ticket)
        db.flush()  # 拿到 id 但不提交，消息与工单同事务
        ticket_id = ticket.id

        # 创建工单消息
        message = models.TicketMessage(
            ticket_id=ticket_id,
            user_id=user_id,
            message=request.message,
            is_admin=False
        )
        db.add(message)
        db.commit()
        return ticket_id

    ticket_id = await run_in_threadpool(_create)

    # 通知管理员有新工单（站内消息 + 实时推送）
    try:
        from backend.notifications import notify_staff_users

        await notify_staff_users(
            db,
            title="🎫 新工单待处理",
            content=f"{username} 提交了工单《{request.title}》\n{request.message[:120]}",
            message_type="ticket",
            related_id=ticket_id,
        )
    except Exception as exc:  # 通知失败不应影响工单创建
        logger.warning("工单管理员通知失败: %s", exc)

    return TicketCreateResponse(
        success=True,
        ticket_id=ticket_id,
        message="工单创建成功"
    )


@user_router.get("/tickets/{ticket_id}", response_model=TicketResponse)
def get_ticket_detail(
    ticket_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取工单详情"""
    ticket = db.query(models.Ticket).filter(
        models.Ticket.id == ticket_id,
        models.Ticket.user_id == current_user.id
    ).first()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在"
        )

    return TicketResponse(
        id=ticket.id,
        title=ticket.title,
        category=ticket.category,
        status=ticket.status,
        priority=ticket.priority,
        created_at=ticket.created_at.isoformat(),
        updated_at=ticket.updated_at.isoformat()
    )


@user_router.get("/tickets/{ticket_id}/messages", response_model=List[TicketMessageResponse])
def get_ticket_messages(
    ticket_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取工单消息列表"""
    ticket = db.query(models.Ticket).filter(
        models.Ticket.id == ticket_id,
        models.Ticket.user_id == current_user.id
    ).first()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在"
        )

    # R32-F3：admin_name 逐条懒加载是 N+1（一条工单 N 条消息 → N+1 次查询），
    # 一次 join 预加载（joinedload 只在 is_admin 行上真正用，行为不变）。
    messages = db.query(models.TicketMessage).options(
        joinedload(models.TicketMessage.admin)
    ).filter(
        models.TicketMessage.ticket_id == ticket_id
    ).order_by(models.TicketMessage.created_at.asc()).all()

    result = []
    for msg in messages:
        admin_name = None
        if msg.is_admin and msg.admin:
            admin_name = msg.admin.username

        result.append(TicketMessageResponse(
            id=msg.id,
            message=msg.message,
            is_admin=msg.is_admin,
            created_at=msg.created_at.isoformat(),
            admin_name=admin_name
        ))

    return result


@user_router.post("/tickets/{ticket_id}/messages")
async def reply_ticket(
    ticket_id: int,
    request: TicketReplyRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """回复工单（落库下放线程池；通知与刷新时间戳仍走原来的顺序）"""
    user_id = current_user.id
    username = current_user.username
    content = (request.message or "").strip()

    def _reply() -> dict:
        ticket = db.query(models.Ticket).filter(
            models.Ticket.id == ticket_id,
            models.Ticket.user_id == user_id
        ).first()

        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="工单不存在"
            )

        if not content:
            raise HTTPException(status_code=400, detail="回复内容不能为空")
        if len(content) > 2000:
            raise HTTPException(status_code=400, detail="回复内容过长（最多 2000 字）")

        # F-P1-27 修复：条件 UPDATE 原子 reopen。原先"检查 closed→写入 open"非原子，
        # 管理员在检查后关闭工单，用户回复仍会把工单翻回 open。
        # WHERE status != 'closed' 抢不到行即 400；消息与状态同事务，失败一起回滚。
        message = models.TicketMessage(
            ticket_id=ticket.id,
            user_id=user_id,
            message=content,
            is_admin=False
        )
        db.add(message)
        reopened = (
            db.query(models.Ticket)
            .filter(
                models.Ticket.id == ticket_id,
                models.Ticket.user_id == user_id,
                models.Ticket.status != "closed",
            )
            .update(
                {"status": "open", "updated_at": datetime.now()},
                synchronize_session=False,
            )
        )
        if not reopened:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="工单已关闭，无法回复"
            )

        db.commit()
        return {"ticket_id": ticket.id, "title": ticket.title}

    info = await run_in_threadpool(_reply)

    def _touch() -> None:
        """通知之后刷新一次时间戳（原来是拿同一个会话直接写，现在也在工作线程）"""
        row = db.query(models.Ticket).filter(models.Ticket.id == info["ticket_id"]).first()
        if row is not None:
            row.updated_at = datetime.now()
            db.commit()

    # 通知管理员（站内消息 + WebSocket 实时推送），与新建工单走同一链路
    try:
        from backend.notifications import notify_staff_users

        await notify_staff_users(
            db,
            title="💬 工单有新回复",
            content=f"{username} 在「{info['title']}」中回复：{content[:60]}",
            message_type="ticket",
            related_id=info["ticket_id"],
        )
        await run_in_threadpool(_touch)
    except Exception as exc:  # 通知失败不应影响回复本身
        logger.warning("工单回复通知发送失败: %s", exc)

    return {"success": True, "message": "回复成功"}


@user_router.post("/tickets/{ticket_id}/close")
def close_ticket(
    ticket_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """关闭工单"""
    ticket = db.query(models.Ticket).filter(
        models.Ticket.id == ticket_id,
        models.Ticket.user_id == current_user.id
    ).first()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="工单不存在"
        )

    ticket.status = "closed"
    ticket.updated_at = datetime.now()
    db.commit()

    return {"success": True, "message": "工单已关闭"}


# ==================== 公告 API ====================

@user_router.get("/announcements", response_model=List[AnnouncementResponse])
def get_announcements(
    db: Session = Depends(get_db)
):
    """获取系统公告 - 与后台公告管理联动

    P0-6 修复：公开接口恒只返回已启用的公告。此前 active_only 参数可被
    未鉴权客户端传 false 绕过，读出已停用/撤回公告的完整 content。
    需要看停用公告请走管理端 /api/admin/announcements（需管理员鉴权）。
    """
    announcements = (
        db.query(models.Announcement)
        .filter(models.Announcement.is_active == True)  # noqa: E712 — 公开接口恒过滤
        .order_by(
            models.Announcement.is_pinned.desc(),
            models.Announcement.created_at.desc()
        ).limit(20).all()
    )

    return [
        AnnouncementResponse(
            id=a.id,
            title=a.title,
            content=a.content,
            type=a.type,
            is_pinned=a.is_pinned,
            created_at=a.created_at.isoformat()
        )
        for a in announcements
    ]


# ==================== 求片 API ====================
# 规则与配置都在 backend/media_seek.py：额度键（SystemConfig）、「剧集按整季申请」的季校验、
# TMDB 候选搜索、库内匹配。这里只做鉴权 / 参数 / 通知。

# 撤回状态在列表 / 撤回端点里判断「在处理中」，值与 media_seek 保持一份
WITHDRAWN_STATUS = media_seek.WITHDRAWN_STATUS


class MediaSeekRequest(BaseModel):
    """求片请求"""
    movie_name: str
    year: Optional[str] = None
    type: Optional[str] = None
    note: Optional[str] = None
    # 这部片要进哪个服的库（用户端会让他选/自动带出）；留空时后端自己推导
    realm_id: Optional[int] = None
    # 来自 TMDB 候选：id 用于管理端「标记已入库」精确匹配
    tmdb_id: Optional[str] = None
    # 剧集按整季申请："1,2" 或 "all"（全季）；电影/其它类型忽略
    season: Optional[str] = None


def _resolve_seek_realm(db: Session, user: models.WebUser,
                        requested: Optional[int] = None) -> Optional[int]:
    """求片登记给哪个服

    - 用户明确选了服：校验存在后用它；
    - 没选：只看得到一个服的会员就默认这个服（大多数用户不用做选择）；
    - 持有多个服或多个都看不清：留空（``NULL``）——「未标注」是诚实的结果，
      不替用户猜一个服，后台也看得出来这条没定。
    """
    if requested is not None:
        if not realms.get_realm(db, requested):
            raise HTTPException(status_code=400, detail=f"服不存在: #{requested}")
        return int(requested)

    now = datetime.now()
    rows = (db.query(models.UserSubscription.realm_id)
            .filter(
                models.UserSubscription.user_id == user.id,
                models.UserSubscription.status == "active",
                models.UserSubscription.end_date > now,
                models.UserSubscription.realm_id.isnot(None),
            )
            .all())
    realms_held = {int(r[0]) for r in rows if r[0]}
    if len(realms_held) == 1:
        return realms_held.pop()
    return None


@user_router.get("/media-seek/search")
def search_media_seek_candidates(
    query: str,
    type: Optional[str] = None,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """TMDB 候选搜索（求片表单的「搜你想看的片」）

    返回 TMDB 条目并标出哪些已经在库：在库的引导去播放（不占额度），
    没在库的可以直接发起求片。TMDB 未配置时 ``configured=False``，
    前端回退到按片名查本地库（``/media-seek/lookup``），不会出现永远搜不出东西的框。
    """
    return media_seek.search_candidates(db, query, type)


@user_router.get("/media-seek/seasons")
def media_seek_seasons(
    tmdb_id: str,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """剧集的季列表：用户端「按整季申请」的季选择器用它（查不到就手动填季）"""
    return {"seasons": media_seek.seasons_for(tmdb_id)}


@user_router.get("/media-seek/lookup")
def lookup_media(
    name: str,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """求片前库存检查：片名是否已在自建媒体库中

    返回命中的库内条目（含海报与详页 id），用于：
    - 已在库 → 直接引导播放，不占用求片额度
    - 未在库 → 前端提示可提交求片
    """
    from backend.emby_server import models as em

    keyword = (name or "").strip()
    if len(keyword) < 1:
        return {"in_library": False, "items": []}

    items = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.name.ilike(f"%{keyword}%"))
        .order_by(em.MediaItem.date_added.desc())
        .limit(6)
        .all()
    )

    return {
        "in_library": len(items) > 0,
        "items": [
            {
                "id": i.guid,
                "name": i.name,
                "type": i.item_type,
                "year": i.production_year,
                # 季无图时回退父剧集海报（season_poster_url），其余类型行为不变
                "poster_url": season_poster_url(i, db),
            }
            for i in items
        ],
    }


def _create_media_seek_sync(
    db: Session,
    user: models.WebUser,
    name: str,
    request: "MediaSeekRequest",
) -> int:
    """同步落库：去重 / 每日额度 / 写入求片（整段放在线程池里跑，见 create_media_seek）

    校验失败照原样抛 HTTPException——线程里抛出的异常会由 await 处原样抛给客户端，
    状态码与文案和以前一致（400 / 409 / 429 的语义没有变）。
    """
    # 剧集按整季申请：先把季归一化（无 / 脏输入 → 400；不填 = 全季）。
    # 电影/纪录片等非剧集类型忽略该字段，存空串，不让脏值进库。
    try:
        season = media_seek.normalize_season(
            request.season, series=request.type in media_seek.SERIES_TYPES
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 每日额度：统计**今天提交过多少条**（含后来撤回的，口径见 media_seek.used_today）。
    # P2 修复（审查）："先 count 后 insert"并发可绕过上限。先锁用户行再统计。
    from backend.api.economy import lock_user_row
    lock_user_row(db, user.id)

    # 去重：同名**且同季**且仍在处理中的请求不再重复提交（已撤回的不算「在处理中」，可以重新求）。
    # 季参与去重：同一部剧的 S1 在看、S2 还想求，是两条独立请求，不该被名字相同挡回去。
    # 去重也必须待在用户行锁内，和额度检查同一个道理——锁外「先查后写」并发时，
    # 两个请求都查不到对方，拿锁后各插一条，409 的承诺被绕过（两条都进审核、都占额度、都发通知）。
    # 锁内这条 SELECT 是拿到锁之后才发出的新语句：PG READ COMMITTED 能看到先提交的行，
    # SQLite 写锁同样串行，后到的那个必然撞上先到的行。去重本就按 user_id 过滤，用户行锁粒度正好。
    exists = db.query(models.MovieRequest).filter(
        models.MovieRequest.user_id == user.id,
        func.lower(models.MovieRequest.movie_name) == name.lower(),
        func.coalesce(models.MovieRequest.season, "") == season,
        models.MovieRequest.status.in_(media_seek.ACTIVE_STATUSES),
    ).first()
    if exists:
        # 无季的（电影 / 老数据）保持原来的文案；剧集把季说清楚，用户知道是哪一条在排队
        suffix = f"（{media_seek.season_label(season)}）" if season and season != media_seek.SEASON_ALL else ""
        raise HTTPException(status_code=409, detail=f"《{name}》{suffix}已在处理中，请耐心等待（可在列表中看到进度）")

    limit = media_seek.daily_limit(db)
    if media_seek.used_today(db, user.id) >= limit:
        raise HTTPException(status_code=429, detail=f"今日求片已达上限（{limit} 条），请明天再提交")

    # 月度额度：公益服 / 付费区分（与旧版公益服求片中心合并，见 media_seek.monthly_quota）。
    # 每日额度与月度额度同时校验，两个都通过才放行。
    mquota = media_seek.monthly_quota(db, user)
    if mquota["monthly_remaining"] <= 0:
        raise HTTPException(
            status_code=429,
            detail=f"本月求片额度已用完（{mquota['monthly_used']}/{mquota['monthly_limit']}），请下月再提交",
        )

    media_request = models.MovieRequest(
        user_id=user.id,
        movie_name=name,
        year=request.year,
        type=request.type,
        # TMDB 候选带过来的 id 与季：管理端匹配「真的入库了吗」靠它们
        tmdb_id=media_seek.normalize_tmdb_id(request.tmdb_id) or None,
        season=season or None,
        note=request.note,
        status="pending",
        # 求片是「给哪个服求」的：用户选/单服自动带出，读不出就未标注
        realm_id=_resolve_seek_realm(db, user, request.realm_id),
    )
    db.add(media_request)
    db.commit()
    db.refresh(media_request)
    return media_request.id


@user_router.post("/media-seek")
async def create_media_seek(
    request: MediaSeekRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """创建求片请求 - 与后台求片管理联动

    校验：片名非空 / 同名未完成请求去重 / 每日额度上限。
    """
    name = (request.movie_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写片名")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="片名过长")

    # v2 求片总开关：关闭后用户不能提交求片
    if not media_seek.seek_enabled(db):
        raise HTTPException(status_code=403, detail="求片功能已关闭")

    # 去重 / 额度 / 落库整段下放线程池：同步 SQLAlchemy 跑在事件循环上时，
    # 卡住的是**所有人**的请求（求片页是用户会连着点的地方）。
    request_id = await run_in_threadpool(
        _create_media_seek_sync, db, current_user, name, request
    )

    # 通知管理员有新求片请求（落站内消息 + WebSocket 推送）
    try:
        from backend.notifications import notify_staff_users

        await notify_staff_users(
            db,
            title="📥 新的求片请求",
            content=f"{current_user.username} 请求《{name}》",
            message_type="media_seek",
            related_id=request_id,
        )
    except Exception as exc:  # 通知失败不应影响求片提交
        logger.warning("求片通知发送失败: %s", exc)

    return {"success": True, "request_id": request_id, "message": "求片请求已提交"}


@user_router.delete("/media-seek/{request_id}")
def withdraw_media_seek(
    request_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """撤回自己的求片（仅限尚未被处理的请求）

    撤回是**改状态**（``withdrawn``）而不是删行：用户列表里不再出现它，
    但今天的提交数依然算得进去（见 create_media_seek 里的额度说明）。
    删行会让额度被无限刷新，也会让后台的审计记录凭空少一条。
    """
    # F-P1-28 修复：条件 UPDATE 原子撤回。原先"检查 pending→写入 withdrawn"非原子，
    # 管理员在检查后审核通过会被撤回覆盖。用 WHERE status='pending' 限定，
    # rowcount=0 时再区分"不存在"与"已被处理"，保持原有 404/400 口径。
    withdrawn = (
        db.query(models.MovieRequest)
        .filter(
            models.MovieRequest.id == request_id,
            models.MovieRequest.user_id == current_user.id,
            models.MovieRequest.status == "pending",
        )
        .update(
            {"status": WITHDRAWN_STATUS, "updated_at": datetime.now()},
            synchronize_session=False,
        )
    )
    if not withdrawn:
        db.rollback()
        exists = db.query(models.MovieRequest).filter(
            models.MovieRequest.id == request_id,
            models.MovieRequest.user_id == current_user.id,
        ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="求片记录不存在")
        raise HTTPException(status_code=400, detail="该请求已被处理，无法撤回")
    db.commit()
    return {"success": True, "message": "已撤回"}


@user_router.get("/media-seek/hot")
def hot_media_seeks(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """v2 热门求片：所有人 pending 的求片（匿名展示用户名），按附议数排序

    附议开关关闭时返回空列表（前端隐藏该区块）。
    """
    if not media_seek.vote_enabled(db):
        return {"requests": []}
    rows = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.status == "pending")
        .order_by(models.MovieRequest.vote_count.desc(),
                  models.MovieRequest.created_at.desc())
        .limit(50)
        .all()
    )
    voted = media_seek.voted_ids(db, current_user.id, [r.id for r in rows])
    return {
        "requests": [
            {
                "id": r.id,
                "movie_name": r.movie_name,
                "year": r.year,
                "type": r.type,
                "season_label": media_seek.season_label(r.season),
                "vote_count": r.vote_count or 0,
                "voted": r.id in voted,
                "mine": r.user_id == current_user.id,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@user_router.post("/media-seek/{request_id}/vote")
def vote_media_seek(
    request_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """v2 附议 / 取消附议一条求片（每人每条只能附议一次，不能给自己附议）"""
    try:
        result = media_seek.toggle_vote(db, current_user, request_id)
    except ValueError as e:
        msg = str(e)
        code = 403 if "已关闭" in msg else 400
        raise HTTPException(status_code=code, detail=msg)
    return {"success": True, **result}


@user_router.get("/media-seek")
def get_my_media_seeks(
    status_filter: Optional[str] = None,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取我的求片列表（支持状态筛选），并附带今日剩余额度

    主动撤回的条目不再列出来（用户看不到自己已经撤销的东西才符合直觉），
    但额度仍按今天的提交总数算。
    """
    query = db.query(models.MovieRequest).filter(
        models.MovieRequest.user_id == current_user.id,
        models.MovieRequest.status != WITHDRAWN_STATUS,
    )
    if status_filter:
        query = query.filter(models.MovieRequest.status == status_filter)

    requests = query.order_by(models.MovieRequest.created_at.desc()).limit(200).all()
    realm_names = {r.id: r.name for r in realms.list_realms(db)}
    # v2：一次查询拿出当前用户附议过的求片 id（列表打标用）
    voted = media_seek.voted_ids(db, current_user.id, [r.id for r in requests])

    return {
        "requests": [
            {
                "id": r.id,
                "movie_name": r.movie_name,
                "year": r.year,
                "type": r.type,
                "note": r.note,
                "status": r.status,
                "admin_note": r.admin_note,
                # v2 附议数 + 我是否已附议
                "vote_count": r.vote_count or 0,
                "voted": r.id in voted,
                # 剧集按整季申请：界面文案由 season_label 统一（全季 / 第 1、2 季）
                "season": r.season,
                "season_label": media_seek.season_label(r.season),
                "tmdb_id": r.tmdb_id,
                # 已入库的条目 guid：用户端可以一键跳到详情页（只读进度的一部分）
                "emby_item_id": r.emby_item_id,
                # 这部片求给哪个服（用户自己就能看到，不用问管理员）
                "realm_id": r.realm_id,
                "realm_name": realm_names.get(r.realm_id, "") if r.realm_id else "",
                "created_at": r.created_at.isoformat()
            }
            for r in requests
        ],
        # 「申请时显示剩余额度」：统一走 media_seek.quota（与提交时的校验同一份口径）；
        # 月度额度（公益/付费区分）一并返回，前端可展示
        "quota": {**media_seek.quota(db, current_user.id), **media_seek.monthly_quota(db, current_user)},
    }


# ==================== 订阅 API ====================

@user_router.get("/subscriptions")
def get_my_subscriptions(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取我的订阅列表 - 与后台订阅管理联动

    生效状态按 end_date 现算（status 字段不会随时间自动翻转），
    与后台订阅总览 / 用户 VIP 派生使用同一口径。
    """
    subscriptions = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == current_user.id
    ).order_by(models.UserSubscription.created_at.desc()).all()

    now = datetime.now()
    plans = {
        p.id: p for p in db.query(models.SubscriptionPlan).filter(
            models.SubscriptionPlan.id.in_([s.plan_id for s in subscriptions if s.plan_id])
        ).all()
    } if subscriptions else {}
    realm_names = {r.id: r.name for r in realms.list_realms(db)}

    result = []
    for sub in subscriptions:
        plan = plans.get(sub.plan_id)
        is_current = bool(
            sub.status == "active" and sub.end_date and sub.end_date > now
        )
        result.append({
            "id": sub.id,
            "plan_name": plan.name if plan else "未知套餐",
            # 会员属于哪个服：多服运营下用户会同时持有多个服的会员，必须分开看
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


@user_router.post("/subscriptions/{subscription_id}/cancel")
def cancel_my_subscription(
    subscription_id: int,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """取消/终止我的订阅（立即失效）"""
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.id == subscription_id,
        models.UserSubscription.user_id == current_user.id,
    ).first()
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    if sub.status == "cancelled":
        return {"success": True, "message": "订阅已取消"}
    sub.status = "cancelled"
    sub.auto_renew = False
    db.commit()
    return {"success": True, "message": "订阅已终止"}


@user_router.get("/subscription-plans")
def get_subscription_plans(
    realm_id: int | None = None,
    db: Session = Depends(get_db)
):
    """获取可用订阅套餐 - 与后台套餐管理联动

    套餐是一个服一个的：默认只列当前服（``realm_id=0`` 列全部服），
    带着所属服的名称，用户端才能说清“这是哪个服的会员”。
    """
    scope_id = None if realm_id == 0 else (realm_id or realms.active_realm_id(db))
    query = realms.scope(db.query(models.SubscriptionPlan),
                         models.SubscriptionPlan.realm_id, scope_id)
    plans = query.filter(
        models.SubscriptionPlan.is_active == True
    ).order_by(models.SubscriptionPlan.sort_order).all()
    # R32-F3：p.realm 逐套餐懒加载是 N+1。跟 get_my_subscriptions 同一口径：
    # 一次拿全服名映射（realm_id 无对应服时 ""，与 p.realm 为空的行为一致）。
    realm_names = {r.id: r.name for r in realms.list_realms(db)}

    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "price": float(p.price),
            "duration_days": p.duration_days,
            "features": p.features,
            "is_popular": p.is_popular,
            "realm_id": p.realm_id,
            "realm_name": realm_names.get(p.realm_id, ""),
        }
        for p in plans
    ]


# ==================== 会员卡码核销 ====================


class RedeemCodeRequest(BaseModel):
    code: str


@user_router.post("/membership/redeem/preview")
async def preview_membership_code(
    req: RedeemCodeRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """预检卡码：不核销，仅告知类型与天数

    用 POST 而非 GET，避免卡码出现在访问日志与浏览器历史中；
    限速与核销同口径，防止靠枚举探测卡码。
    """
    allowed, _ = check_rate_limit(f"code_preview:{current_user.id}", 20, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    return codes.preview_code(db, req.code, current_user.username)


@user_router.post("/membership/redeem")
def redeem_membership_code(
    req: RedeemCodeRequest,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """核销会员卡码：注册码 / 续期码 / 白名单码

    同步 `def`：整条链路（限速 / 查码 / 原子占位 / 发天数 / 提交）都是同步 SQLAlchemy，
    没有一个 await。留成 `async def` 就是让占位与发天数的那段写库压在事件循环上
    （见 docs/performance.md 的「事务范围」与 check_blocking_routes 的说明）。
    """
    allowed, _ = check_rate_limit(f"code_redeem:{current_user.id}", 10, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")

    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    result = codes.redeem_code(db, current_user, req.code)
    if not result.get("success"):
        # 失败路径要把事务收干净：redeem_code 里可能已经动过会话（占位 / 封禁），
        # 不回滚就交给 get_db 的 close() 会让错误更难查
        db.rollback()
        raise HTTPException(status_code=400, detail=result.get("message") or "卡码无效")
    # 卡码消耗与会员天数由这里统一提交（codes 里不再自己 commit）：
    # 两阶段提交不一致会导致「码烧了、会员没到账」或反过来（见 backend/codes.py 的说明）
    commit_with_retry(db, label="卡码核销")
    return result


# ==================== 我的设备（播放器） ====================


@user_router.get("/emby/devices")
async def get_my_devices(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """我的播放设备：用于查看已在哪些客户端登录，并清理不再使用的设备"""
    now = datetime.now()
    cutoff = now - timedelta(days=devices.ACTIVE_DEVICE_DAYS)
    rows = devices.list_devices(db, current_user.id)

    items = []
    for device in rows:
        dto = devices.device_dto(device)
        dto["is_online_recent"] = bool(
            not device.is_blocked
            and (device.last_seen_at is None or device.last_seen_at >= cutoff)
        )
        items.append(dto)

    limit = devices.device_limit(db)
    active_count = sum(1 for d in items if d["is_online_recent"])
    return {
        "limit": limit,
        "count": len(items),
        "active_count": active_count,
        "remaining": max(0, limit - active_count) if limit else None,
        "auto_evict": devices.device_auto_evict(db),
        "active_days": devices.ACTIVE_DEVICE_DAYS,
        "devices": items,
    }


@user_router.delete("/emby/devices/{device_id}")
async def remove_my_device(
    device_id: str,
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """移除设备并吊销其令牌（对应客户端需重新登录）"""
    ok = devices.remove_device(db, current_user.id, device_id, revoke_tokens=True)
    if not ok:
        raise HTTPException(status_code=404, detail="设备不存在")
    return {"success": True, "message": "设备已移除，对应客户端需重新登录"}


# ==================== 已移除的遗留端点 ====================
#
# 以下端点已于 v2.5.0 删除，它们读取的是「外部 Emby 服务器」时代的遗留模型
# （models.EmbyServer / models.UserEmbyAccount）：统一后端已完全自建（/api/user/emby/*），
# 这两张表不再有任何写入方，接口只会返回空数据，容易误导调用方。
#   - GET /api/user/emby-servers
#   - GET /api/user/emby-account


# ==================== 会员等级（P1 统一货币体系） ====================

@user_router.get("/member")
async def my_member_info(
    current_user: models.WebUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户的会员等级 / 经验 / 进度 / 全等级列表（含公开权益）"""
    from backend import member_level as _ml

    def _info() -> dict:
        return _ml.get_member_info(db, current_user)

    return await run_in_threadpool(_info)


# ==================== 导出 ====================

__all__ = ["user_router"]

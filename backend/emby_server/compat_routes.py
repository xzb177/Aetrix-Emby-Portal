"""会话上报与协议补齐路由

由 ``backend/emby_server/api.py`` 拆分而来（v2.13.0），播放会话上报、在线会话列表的旧实现（已被 session_routes 替换），
以及其他 Emby 4.7 协议面补齐端点（筛选值、收藏、相似、演职员、任务、活动日志等）。

路由仍注册在同一个 ``emby_router`` 上（从 ``api`` 导入同一实例），注册顺序与拆分前一致，
因此 ``mount_routes`` / ``image_routes`` / ``session_routes`` 的「按路径 + 端点名替换」逻辑不受影响。
模块由 ``backend/main.py`` 与 ``emby_api/main.py`` 在 ``include_router`` 之前导入。
无阻塞的协议路由一律写成同步 ``def``，由 Starlette 线程池执行，避免同步查询/文件 IO 卡住事件循环。
"""
from __future__ import annotations

import logging

from backend import admin_roles, models, share_guard
from backend.authlog import client_ip as real_client_ip
from backend.database import SessionLocal, get_db
from backend.emby_server import models as em
from backend.emby_server.auth import get_emby_user, parse_emby_authorization, resolve_token
from backend.emby_server import facets
from backend.emby_server import playback_bus
from backend.emby_server.facets import count_virtual_items  # 索引版（虚拟库条目数）
from backend.emby_server.query_params import qint, qlimit, qstart
from backend.emby_server.scanner import (
    ScanInProgress,
    item_guid,
    parse_media_filename,
    scan_library_sync,
)
from backend.emby_server.env_util import env_float, env_int
from backend.emby_server.streaming import (
    serve_file,
    serve_image,
)
from datetime import datetime, timedelta
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from backend.emby_server.api import (
    emby_router,
    SERVER_ID,
    SERVER_VERSION,
    _base_url,
    _empty_items,
    _filters_payload,
    _guid_of,
    _now_playing_dto,
    item_guid_for,
    _policy_dto,
    _query_result,
    _require_visible_item,
    _run_time_ticks,
    _stream_client_identity,
    _user_data_dto,
    user_views,
)

logger = logging.getLogger(__name__)

# ==================== 进度上报的写库节流（v2.14.0）====================
# 客户端每 10s 上报一次进度（暂停、卡顿的客户端也会照报），多路并发播放时每次都 commit
# 会让 SQLite 写锁排队。策略：拖动（位置跳变）与暂停切换必须立刻落库，其余上报在
# PROGRESS_WRITE_SECONDS 内只写一次——上限远小于 maintenance 的会话回收阈值
# （``SESSION_STALE_MINUTES``，默认 10 分钟），心跳不会掉队。
# 设 ``EMBY_PROGRESS_WRITE_SECONDS=0`` 回到旧行为（每报必写）。
PROGRESS_WRITE_SECONDS = env_float("EMBY_PROGRESS_WRITE_SECONDS", 15.0)
PROGRESS_SEEK_TICKS = env_int("EMBY_PROGRESS_SEEK_TICKS", 300000000)  # 30s


# ==================== 会话上报 ====================

def _note_play_feed(db: Session, item: em.MediaItem, started: bool,
                    finished: bool) -> None:
    """播放事件 → 发现页实时动态流埋点（**匿名**：文案一律「一位影迷」，绝不出现
    用户昵称/ID）。

    - 新会话（非 ended）→「一位影迷开始看《X》第 N 集」/「一位影迷开始看《X》」
    - ended 上报 →「一位影迷看完《X》」
    """
    if not started and not finished:
        return
    try:
        from backend.panel_feed import note_feed

        name = (getattr(item, "name", "") or "").strip() or "未知影片"
        ep = getattr(item, "episode_number", None)
        series_id = getattr(item, "series_id", None)
        if getattr(item, "item_type", "") == "episode" and series_id:
            try:
                sname = db.query(em.MediaItem.name).filter(
                    em.MediaItem.id == series_id).scalar()
                if sname:
                    name = sname
            except Exception:  # noqa: BLE001 — 剧名查不到就用条目名
                pass
        if ep:
            title = f"《{name}》第 {ep} 集"
        else:
            title = f"《{name}》"
        if started:
            text = f"一位影迷开始看{title}"
        else:
            text = f"一位影迷看完{title}"
        note_feed(db, text, event_type="play",
                  library_id=getattr(item, "library_id", None))
    except Exception:  # noqa: BLE001 — 埋点失败绝不能影响播放上报
        logger.warning("播放 feed 埋点失败 item_id=%s",
                       getattr(item, "id", "?"), exc_info=True)


def _upsert_session(db: Session, request: Request, user: models.WebUser,
                    item: em.MediaItem, body: dict,
                    ended: bool = False) -> tuple[em.PlaybackSession, bool]:
    """写入/更新播放会话（带写库节流，见文件头的 PROGRESS_WRITE_* 说明）

    返回 ``(会话, 本次是否真的写了库)``；没有新信息时只返回会话、不产生写事务，
    调用方据此决定「同一次上报的其它写入（如观看进度）要不要一起跳过」。
    """
    session_key = body.get("PlaySessionId") or f"{user.id}-{item.guid[:16]}"
    session = db.query(em.PlaybackSession).filter(
        em.PlaybackSession.session_key == session_key
    ).first()
    auth = parse_emby_authorization(request.headers.get("X-Emby-Authorization"))
    # 阶段 3 一等会话：PlaybackInfo 预建的行（awaiting_first_report）在首次
    # 上报时仍须走一次 share_guard 判定——行已存在绝不等于判定已通过，
    # 否则预建会话会悄悄绕过同播/跨城市拦截。旧行本列恒 False，行为不变。
    precreated = bool(
        session is not None
        and getattr(session, "awaiting_first_report", False)
        and session.ended_at is None
    )
    new_session = session is None or precreated
    if session is not None and session.ended_at is not None and not ended:
        # 同一会话键的新一轮播放（客户端复用了旧键 / 确定性键命中了已结束行）：
        # 复活该行并重新走一次判定。否则这次播放既不会被计入并发、也不会被
        # 同播/跨城市检查看到，还会被「已结束会话裁剪」在播放中途删掉。
        session.ended_at = None
        # 复活即新一轮播放：start_time 必须重置为现在，否则时长榜口径
        # （ended_at − start_time）会把两次播放之间的间隔也虚增进去。
        session.start_time = datetime.now()
        new_session = True
    if new_session:
        # 防共享两件套（默认全部 off，不判定也不写库）：
        # 1) 同播检测——并发超上限时 enforce 档直接拦下这条会话（返回 None）；
        # 2) 跨城市轨迹——播放也算一次活动，与登录走同一份判定。
        # 两件事都放在**建会话之前**：拦下时库里不会多一条会话记录，
        # 「谁在什么时候多路播放」看的就是这张表。
        # ``ended`` 不参与判定：客户端的「停止」上报也会命中新会话分支，
        # 而那根本不是在开始播放（拿它去数并发会把每一次正常结束都报成同播）。
        if not ended:
            # 判定用**可信代理口径**的 IP（与登录落点同一份）：直接取
            # request.client.host 在反代后面拿到的是代理自己的地址，归属地会
            # 全部落到机房所在城市，跨城市检测就废了。下面 remote_addr
            # 保持原有口径，不在这里跟着改。
            ip = real_client_ip(request)
            # commit=False：判定落库（事件/站内信）与下面的建会话行由本函数末尾
            # 统一提交。note_session 内取的用户行锁于是覆盖「判定 → 建行 →
            # 提交」整个区间，并发两路新建会话在此串行化；拦下时先提交已暂存
            # 的事件再返回（审计轨迹不丢，只是没有会话行）。
            verdict = share_guard.note_session(db, user, session_key, ip, commit=False)
            if verdict and verdict.get("blocked"):
                logger.info("同播拦截：user=%s session=%s", user.id, session_key)
                if precreated:
                    # 预建行被拦下：置 ended，不留幻影活会话（旧路径无此行）。
                    session.ended_at = datetime.now()
                    session.awaiting_first_report = False
                db.commit()
                return None, False
            verdict = share_guard.note_activity(db, user, ip, commit=False)
            if verdict and verdict.get("blocked"):
                logger.info("跨城市停用后拒绝建会话：user=%s", user.id)
                if precreated:
                    session.ended_at = datetime.now()
                    session.awaiting_first_report = False
                db.commit()
                return None, False
        if precreated:
            # 判定已通过：清零待首次上报标志（末尾统一提交时落库；本分支
            # new_session=True 故 material 恒真，不会走节流提前返回）。
            session.awaiting_first_report = False
        if session is None:
            # 真·新会话：建行；上面复活分支走进来时复用旧行，不另建
            session = em.PlaybackSession(
                session_key=session_key, user_id=user.id, item_id=item.id,
            )
            # 存点（savepoint）保护：防共享判定用 commit=False，事件行暂存
            # 在本事务里；begin_nested 会先把它们 flush 进事务再建存点，
            # 撞键回滚时只丢弃本次的 insert 本体，不丢判定事件（实测：
            # 回滚后撞键对象已自动 expunge，无需手动清理）。
            _sp = db.begin_nested()
            db.add(session)
            try:
                # 确定性会话键（HMAC 派生）下，Playing 与紧随的 Progress 可能
                # 并发走到这里：先 flush 抢唯一键，撞键则回滚并复用 winner 的行，
                # 避免第二个请求直接 IntegrityError 500。
                # （此前随机键永不碰撞，不需要这道防线。）
                db.flush()
            except IntegrityError:
                # 回滚到存点：只丢弃撞键的 insert 本体，存点前已 flush 的
                # 防共享事件行保留，之后随本函数末尾统一提交。
                _sp.rollback()
                session = (
                    db.query(em.PlaybackSession)
                    .filter(em.PlaybackSession.session_key == session_key)
                    .first()
                )
                if session is None:
                    raise
                logger.info("播放会话并发建行合并：user=%s session=%s 复用已存在行",
                            user.id, session_key)

    position = int(body.get("PositionTicks") or 0)
    paused = bool(body.get("IsPaused", False))
    # 新会话 / 播放结束 / 暂停切换 / 拖动进度：立刻落库（列表与进度条要不卡顿地跟着动）
    material = (
        new_session
        or ended
        or paused != bool(session.is_paused)
        or abs(position - int(session.position_ticks or 0)) >= PROGRESS_SEEK_TICKS
    )
    if not material and PROGRESS_WRITE_SECONDS:
        last = session.last_update_at
        if last is not None and (datetime.now() - last).total_seconds() < PROGRESS_WRITE_SECONDS:
            return session, False

    session.device_name = auth.get("Device") or session.device_name
    if not getattr(session, "device_id", None):
        # 阶段 3：上报时补设备稳定标识（预建行只在 PlaybackInfo 能拿到时才填）。
        session.device_id = (auth.get("DeviceId") or "")[:100] or None
    session.client_name = auth.get("Client") or session.client_name
    session.client_version = auth.get("Version") or session.client_version
    session.remote_addr = request.client.host if request.client else session.remote_addr
    session.position_ticks = position
    session.is_paused = paused
    session.play_method = body.get("PlayMethod") or session.play_method
    if ended:
        # r43：重复 Stopped 上报的幂等——第一次看完已写过 feed 埋点时不再重复写。
        # 复活分支已把 ended_at 置 None，不受影响；只改埋点条件，不改会话写入行为。
        was_ended = session.ended_at is not None
        session.ended_at = datetime.now()
    else:
        was_ended = False
    db.commit()
    # 发现页实时动态流埋点：新会话=开始看，ended=看完（只加写入调用，不改上报行为）
    _note_play_feed(db, item, started=(new_session and not ended),
                    finished=bool(ended and not was_ended))
    # 播放实时总线接线（FMBY #24，唯一发布点 = 写库后发）：
    # 新会话且非结束 → started；结束上报 → ended；其余写库 → progress。
    # 节流未写库（本函数提前返回处）不发；防共享拦下（session 为 None）
    # 在前面已返回，不发；起播打点处（api.py note_play_start）不发，
    # 同一事件绝不两处重复发。发布失败永不抛、不阻断上报
    # （playback_bus.publish_event 内部保证）。
    if new_session and not ended:
        _bus_event = playback_bus.EVENT_STARTED
    elif ended:
        _bus_event = playback_bus.EVENT_ENDED
    else:
        _bus_event = playback_bus.EVENT_PROGRESS
    playback_bus.publish_event(playback_bus.build_event(
        _bus_event,
        session_id=session.server_session_id or session.session_key,
        user_id=session.user_id, item_id=session.item_id,
        variant=session.variant, device_id=session.device_id,
        position_ticks=session.position_ticks, is_paused=session.is_paused))
    return session, True


# 三个会话上报端点的同步实现（路由只负责解析 JSON + 丢线程池，见下面各路由的说明）。
# 会话上报是**全站最热**的写路径：每个正在播放的客户端每 10 秒上报一次进度，
# 一次上报要读会话 / 写位置 / 写观看进度（2~3 条 SQL + 1 次提交）。这些 SQL 以前
# 跑在事件循环上：多路并发播放时，所有人的其它请求都得排在这几次写事务后面。


def _get_or_create_umd(db: Session, user: models.WebUser, item: em.MediaItem) -> em.UserMediaData:
    """取/建观看进度行（_record_progress 与 _record_stopped 共用，r50 抽取）。"""
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id
    ).first()
    if not umd:
        # 第一次上报这个条目：即使会话被节流也必须落库（否则「继续观看」没有起点）
        umd = em.UserMediaData(user_id=user.id, item_id=item.id)
        db.add(umd)
    return umd


def effective_max_position_ticks(umd: em.UserMediaData) -> int:
    """这行「历史最远看到哪」的有效值（进度双记，FMBY 学习批一 #4）。

    ``max_position_ticks`` 为 NULL = 这行还没被双记写过（升级前老数据），
    回退 ``playback_position_ticks``——老数据在第一次新上报前，最远点就等于
    当时的断点。不做批量回填：靠下一次真实进度上报在 _apply_play_progress
    里以旧 position 为基线自然长出来，心跳不写库的口径不因此被打破。
    """
    if umd.max_position_ticks is not None:
        return int(umd.max_position_ticks)
    return int(umd.playback_position_ticks or 0)


def _apply_play_progress(umd: em.UserMediaData, pos: int, finished: bool) -> None:
    """把一次进度落到 UserMediaData 行上（Progress 与 Stopped 共用；调用方负责 commit）。

    进度双记：position 记当前位置（可变小），max_position 记本轮最远点、
    只增不减（基线是 effective_max_position_ticks，老数据回退旧 position）。
    finished（90% 口径）时标已看并清零断点——max 与 position 一起清零，
    与现行清零口径对齐（同一轮观看结束，下一轮从零重新累计）；
    played 已标时 play_count 不重复加。
    """
    base_max = effective_max_position_ticks(umd)
    umd.playback_position_ticks = pos
    umd.max_position_ticks = max(base_max, pos)
    umd.last_played_at = datetime.now()
    if finished:
        if not umd.played:
            umd.play_count = (umd.play_count or 0) + 1
        umd.played = True
        umd.playback_position_ticks = 0
        umd.max_position_ticks = 0


def _mark_variant_played_best_effort(db, user, item) -> None:
    """转存副本播放回写（2026-10-27 清理规则第 4 条，尽力而为）。

    会话上报是播放的通用信号（直链 alt=media 不走 /stream，play_start
    未必有），所以回写钉在这里而不是 stream 路径。失败绝不影响上报
    本身；1 小时节流在 mark_variant_played 内部，不破坏上报节流。
    """
    try:
        from backend.emby_server import transfer_cleanup
        transfer_cleanup.mark_variant_played(db, user.id, item.id)
    except Exception:  # noqa: BLE001 — 追踪失败不挡播放上报
        logger.debug("转存副本播放回写失败", exc_info=True)


def _record_playing(request: Request, user: models.WebUser, db: Session, body: dict) -> None:
    item = _require_visible_item(db, user, body.get("ItemId") or "")
    session, _wrote = _upsert_session(db, request, user, item, body)
    if session is None:
        # 被防共享拦下（session 返回 None = 不建会话）
        raise HTTPException(
            status_code=403,
            detail="同时播放的会话数已达上限，本次播放请求已被拦截",
        )
    _mark_variant_played_best_effort(db, user, item)


def _record_progress(request: Request, user: models.WebUser, db: Session, body: dict) -> None:
    item = _require_visible_item(db, user, body.get("ItemId") or "")
    _session, wrote = _upsert_session(db, request, user, item, body)
    if _session is None:
        # 被拦下的会话：库里没有它，也没有观看进度可写，直接告诉客户端
        raise HTTPException(
            status_code=403,
            detail="同时播放的会话数已达上限，本次播放请求已被拦截",
        )

    pos = int(body.get("PositionTicks") or 0)
    runtime = item.duration_ticks or 0
    finished = bool(runtime and pos >= runtime * 0.9)

    umd = _get_or_create_umd(db, user, item)
    if (
        umd.id is not None  # 新建行（id 为 None）必须落库，不能被节流跳过
        and not wrote
        and (umd.playback_position_ticks or 0) == pos
        and not (finished and not umd.played)
    ):
        # 会话和观看进度都没有新信息：同一次上报不必再来一次写事务（见文件头的节流说明）
        return

    _mark_variant_played_best_effort(db, user, item)
    _apply_play_progress(umd, pos, finished)
    db.commit()


def _record_stopped(request: Request, user: models.WebUser, db: Session, body: dict) -> None:
    item = _require_visible_item(db, user, body.get("ItemId") or "")
    _upsert_session(db, request, user, item, body, ended=True)
    # r50：Stopped 携带的最终进度必须落库（此前只更新会话行，最终断点与已看状态
    # 全丢——客户端最后一次 Progress 可能因网络抖动没送达，或客户端只发 Stopped）。
    # 与 _record_progress 同一落库口径（90% 自动已看）；幂等：重复 Stopped 只是把
    # 同一位置再写一遍，played 已标时 play_count 不重复加。
    pos = int(body.get("PositionTicks") or 0)
    runtime = item.duration_ticks or 0
    finished = bool(runtime and pos >= runtime * 0.9)
    umd = _get_or_create_umd(db, user, item)
    # 观影时长分档奖励：本轮最远点必须在 _apply_play_progress 之前取——
    # 90% 看完（finished）时该函数会把 max_position 清零，取晚了就没了。
    # 注意最远点是位置不是实看时长（快进会虚增），每日上限 2 兜底超发。
    watched_ticks = max(effective_max_position_ticks(umd), pos)
    _mark_variant_played_best_effort(db, user, item)
    _apply_play_progress(umd, pos, finished)
    db.commit()
    # 观影时长分档奖励（尽力而为）：发放走自己的锁 + 提交，失败只记日志，
    # 绝不影响 Stopped 主流程与返回。
    try:
        from backend import vitality as _vitality
        _vitality.award_watch_minutes_reward(db, user.id, watched_ticks / 600_000_000)
    except Exception:  # noqa: BLE001 — 奖励失败不挡停播上报
        logger.debug("观影时长奖励发放失败", exc_info=True)
    # 批一 #3：尽力提前释放「挂载+路径前缀」播放路数租约（stream_limits）。
    # 只是提前量：客户端不发 Stopped 也没关系，租约靠 TTL 到期收敛；
    # release 本身永不抛，这里再套一层保险，不许影响 Stopped 主流程。
    try:
        from backend.emby_server import stream_limits

        stream_limits.release(
            db, _stream_client_identity(request, user),
            str(body.get("ItemId") or ""),
            getattr(item, "file_path", "") or "")
    except Exception:  # noqa: BLE001
        pass


@emby_router.post("/emby/Sessions/Playing")
@emby_router.post("/Sessions/Playing")
async def session_playing(request: Request, user: models.WebUser = Depends(get_emby_user),
                          db: Session = Depends(get_db)):
    body = await request.json()
    # 同步数据库段落一律下放到线程池：它跑在事件循环上时，卡住的是**所有人的请求**
    await run_in_threadpool(_record_playing, request, user, db, body)
    return {"success": True}


@emby_router.post("/emby/Sessions/Playing/Progress")
@emby_router.post("/Sessions/Playing/Progress")
async def session_progress(request: Request, user: models.WebUser = Depends(get_emby_user),
                           db: Session = Depends(get_db)):
    body = await request.json()
    await run_in_threadpool(_record_progress, request, user, db, body)
    return {"success": True}


@emby_router.post("/emby/Sessions/Playing/Stopped")
@emby_router.post("/Sessions/Playing/Stopped")
async def session_stopped(request: Request, user: models.WebUser = Depends(get_emby_user),
                          db: Session = Depends(get_db)):
    body = await request.json()
    await run_in_threadpool(_record_stopped, request, user, db, body)
    return {"success": True}


@emby_router.post("/emby/Sessions/Capabilities/Full")
@emby_router.post("/Sessions/Capabilities/Full")
@emby_router.post("/emby/Sessions/Capabilities")
@emby_router.post("/Sessions/Capabilities")
def session_capabilities(request: Request, user: models.WebUser = Depends(get_emby_user)):
    return {"success": True}


@emby_router.get("/emby/Sessions")
@emby_router.get("/Sessions")
def get_sessions(request: Request, db: Session = Depends(get_db),
                 user: models.WebUser = Depends(get_emby_user)):
    # P0 修复（审查）：此前无任何鉴权，未登录可窥探全站"谁在看什么"。
    # Emby 官方口径要求管理员；这里放宽为"本人看本人、管理看全站"，不破坏客户端兼容。
    q = db.query(em.PlaybackSession).filter(em.PlaybackSession.ended_at.is_(None))
    if not getattr(user, "is_staff", False):
        q = q.filter(em.PlaybackSession.user_id == user.id)
    sessions = q.order_by(em.PlaybackSession.last_update_at.desc()).all()
    result = []
    # 客户端会周期性轮询会话列表：逐条查 item/user 是 2N 次的 N+1，改为两个批量 IN
    # 查询（与 session_routes 同一口径）。空集合不给 IN 列表，直接用空映射跳过查询。
    item_ids = {s.item_id for s in sessions}
    user_ids = {s.user_id for s in sessions}
    items = {}
    owners = {}
    if item_ids:
        items = {
            item.id: item
            for item in db.query(em.MediaItem).filter(em.MediaItem.id.in_(item_ids)).all()
        }
    if user_ids:
        owners = {
            owner.id: owner
            for owner in db.query(models.WebUser).filter(models.WebUser.id.in_(user_ids)).all()
        }
    for s in sessions:
        item = items.get(s.item_id)
        owner = owners.get(s.user_id)
        if item and owner:  # 缺条目或缺用户的会话照旧跳过，不入结果
            result.append(_now_playing_dto(s, item, owner))
    return result


@emby_router.delete("/emby/Sessions/{session_key}")
@emby_router.delete("/Sessions/{session_key}")
def stop_session(session_key: str, db: Session = Depends(get_db),
                 user: models.WebUser = Depends(get_emby_user)):
    # P0 修复（审查）：此前无任何鉴权，未登录可结束任意用户会话并杀其转码进程。
    session = db.query(em.PlaybackSession).filter(
        em.PlaybackSession.session_key == session_key
    ).first()
    if session:
        if session.user_id != user.id and not getattr(user, "is_staff", False):
            raise HTTPException(status_code=403, detail="无权操作他人的播放会话")
        # 转码删除后结束会话不再需要停转码进程（已无转码会话存在）。
        session.ended_at = datetime.now()
        db.commit()
    return {"success": True}


# ==================== 其他兼容端点 ====================

@emby_router.get("/emby/LiveTV/Channels")
@emby_router.get("/LiveTV/Channels")
def livetv_channels():
    return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


@emby_router.get("/emby/DisplayPreferences/users")
@emby_router.get("/DisplayPreferences/users")
def display_prefs(request: Request, user: models.WebUser = Depends(get_emby_user)):
    return {"Id": "users", "CustomPrefs": {}}


@emby_router.post("/emby/DisplayPreferences/users")
@emby_router.post("/DisplayPreferences/users")
def save_display_prefs(request: Request, user: models.WebUser = Depends(get_emby_user)):
    return {"success": True}


# 每个媒体库（CollectionFolder）的显示偏好：官方 DisplayPreferences/{id} 路径，
# 客户端打开库时会按 Views 给的 DisplayPreferencesId 来读/存排序与视图偏好。
# 只实现 /users 时这个请求是 404，严格客户端会把库页当错误。本服务不持久化
# 按库偏好，读写都回标准空结构（与 /users 同口径）。
@emby_router.get("/emby/DisplayPreferences/{pref_id}")
@emby_router.get("/DisplayPreferences/{pref_id}")
def display_prefs_folder(pref_id: str, user: models.WebUser = Depends(get_emby_user)):
    return {"Id": pref_id, "CustomPrefs": {}}


@emby_router.post("/emby/DisplayPreferences/{pref_id}")
@emby_router.post("/DisplayPreferences/{pref_id}")
def save_display_prefs_folder(pref_id: str, user: models.WebUser = Depends(get_emby_user)):
    return {"Id": pref_id, "CustomPrefs": {}}


@emby_router.get("/emby/Localization/Culture")
@emby_router.get("/Localization/Culture")
def localization_cultures():
    return [
        {"Name": "Chinese (Simplified)", "TwoLetterISOLanguageName": "zh", "ThreeLetterISOLanguageName": "chi"},
        {"Name": "English", "TwoLetterISOLanguageName": "en", "ThreeLetterISOLanguageName": "eng"},
    ]


@emby_router.get("/emby/Localization/countries")
@emby_router.get("/Localization/countries")
def localization_countries():
    return [{"Name": "China", "TwoLetterISOLanguageName": "cn", "ThreeLetterISOLanguageName": "CHN"}]


@emby_router.get("/emby/Web/DefaultRoutingMap")
@emby_router.get("/Web/DefaultRoutingMap")
def default_routing_map():
    return {"Routes": []}


@emby_router.get("/emby/MediaBackup/Status")
@emby_router.get("/MediaBackup/Status")
def media_backup_status():
    return {"Status": "Disabled"}


@emby_router.get("/emby/swagger.json")
@emby_router.get("/swagger.json")
def swagger_json():
    return {"swagger": "2.0", "info": {"title": "Emby", "version": SERVER_VERSION}, "paths": {}}


@emby_router.get("/emby/quickconnect/info")
@emby_router.get("/quickconnect/info")
def quickconnect_info():
    return {"Available": False, "State": "Unavailable"}


# ==================== 客户端兼容补齐（对照 Emby 4.7 官方 API 面）====================
#
# 本节补齐真实客户端（Infuse / Forward / Hills / SenPlayer / 官方 App）会调用、
# 但旧实现未覆盖的端点。原则：
#   1) 有真实数据的给真实数据（搜索、相似、祖先、字幕、分类元数据）
#   2) 本服务没有对应概念的（预告片/主题曲/片头/插件）返回空结果集而不是 404，
#      客户端不会把 404 当错误反复重试
#   3) 涉及隐私的（/Users/Public 用户列表）按 Jellyfin 默认策略返回空


# ---- 用户策略 ----

@emby_router.get("/emby/Users/{user_id}/Policy")
@emby_router.get("/Users/{user_id}/Policy")
def get_user_policy(user_id: str, user: models.WebUser = Depends(get_emby_user)):
    return _policy_dto(user)


@emby_router.post("/emby/Users/{user_id}/Policy")
@emby_router.post("/Users/{user_id}/Policy")
def set_user_policy(user_id: str, user: models.WebUser = Depends(get_emby_user)):
    # 用户策略由门户统一管理：忽略客户端写入，返回当前真实策略，避免客户端状态错乱
    return _policy_dto(user)


# ---- 搜索 ----
# 搜索接口已迁到 backend/emby_server/search_api.py（相关度排序版），
# 并在 main.py / emby_api/main.py 中**先于本路由**注册。



# ---- 收藏的规范路由（客户端除 Rating 外还会直接调 FavoriteItems）----

def _set_favorite(db: Session, user: models.WebUser, item_id: str, value: bool) -> dict:
    item = _require_visible_item(db, user, item_id)
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id
    ).first()
    if not umd:
        umd = em.UserMediaData(user_id=user.id, item_id=item.id)
        db.add(umd)
    umd.is_favorite = value
    db.commit()
    # r50：补 item.guid（与 mark_played/mark_unplayed/rate 同口径；
    # _user_data_dto 无 item_id 时不下发 ItemId/Key，严格解码器会失败）
    return _user_data_dto(umd, item.guid, _run_time_ticks(item))


@emby_router.post("/emby/Users/{user_id}/FavoriteItems/{item_id}")
@emby_router.post("/Users/{user_id}/FavoriteItems/{item_id}")
def add_favorite(item_id: str, user_id: str,
                 user: models.WebUser = Depends(get_emby_user),
                 db: Session = Depends(get_db)):
    return _set_favorite(db, user, item_id, True)


@emby_router.delete("/emby/Users/{user_id}/FavoriteItems/{item_id}")
@emby_router.delete("/Users/{user_id}/FavoriteItems/{item_id}")
def remove_favorite(item_id: str, user_id: str,
                    user: models.WebUser = Depends(get_emby_user),
                    db: Session = Depends(get_db)):
    return _set_favorite(db, user, item_id, False)


# ---- 从继续观看隐藏（HideFromResume）----

def _set_hide_from_resume(db: Session, user: models.WebUser, item_id: str) -> dict:
    """落「从继续观看隐藏」标记。

    进度只写在单集行上、Resume 又把单集折叠成剧集展示（见 api.get_resume），
    所以客户端对剧集卡片点隐藏时传来的是剧集 guid：只标记剧集行自己的
    UserMediaData 没有任何行可排除，必须把该剧全部单集的进度行一并标记。
    标记不动 playback_position_ticks —— 取消隐藏没有对应端点，进度数据保留，
    与官方「隐藏只是不展示」的语义一致。
    """
    item = _require_visible_item(db, user, item_id)
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id
    ).first()
    if not umd:
        umd = em.UserMediaData(user_id=user.id, item_id=item.id)
        db.add(umd)
    umd.hide_from_resume = True
    if item.item_type == "series":
        ep_ids = [r[0] for r in db.query(em.MediaItem.id).filter(
            em.MediaItem.series_id == item.id,
            em.MediaItem.item_type == "episode",
        ).all()]
        if ep_ids:
            db.query(em.UserMediaData).filter(
                em.UserMediaData.user_id == user.id,
                em.UserMediaData.item_id.in_(ep_ids),
            ).update({em.UserMediaData.hide_from_resume: True},
                     synchronize_session=False)
    db.commit()
    return _user_data_dto(umd, item.guid, _run_time_ticks(item))


@emby_router.post("/emby/Users/{user_id}/Items/{item_id}/HideFromResume")
@emby_router.post("/Users/{user_id}/Items/{item_id}/HideFromResume")
def hide_from_resume(item_id: str, user_id: str,
                     user: models.WebUser = Depends(get_emby_user),
                     db: Session = Depends(get_db)):
    return _set_hide_from_resume(db, user, item_id)


# ---- 相似推荐 ----

@emby_router.get("/emby/Items/{item_id}/Similar")
@emby_router.get("/Items/{item_id}/Similar")
@emby_router.get("/emby/Movies/{item_id}/Similar")
@emby_router.get("/Movies/{item_id}/Similar")
@emby_router.get("/emby/Shows/{item_id}/Similar")
@emby_router.get("/Shows/{item_id}/Similar")
@emby_router.get("/emby/Trailers/{item_id}/Similar")
@emby_router.get("/Trailers/{item_id}/Similar")
def similar_items(item_id: str, request: Request,
                  user: models.WebUser = Depends(get_emby_user),
                  db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    genres = [g for g in (item.genres or "").split(",") if g]
    if not genres:
        return _empty_items()
    # G1-P1-2：Limit 钳制 200（原无上界）
    limit = qlimit(request.query_params, default=12)
    # 流派匹配走关联表索引（与筛选路径同一个口径）：旧写法是 genres ILIKE '%…%'，
    # 前置通配符用不上索引，等于每打开一个详情页就扫一遍全库文本列。
    if facets.ensure_ready(db):
        genre_cond = facets.candidate_condition(facets.KIND_GENRE, genres, db)
    else:  # 老库还没回填完：退回旧口径，结果一致
        genre_cond = or_(*[em.MediaItem.genres.ilike(f"%{g}%") for g in genres])
    rows = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.item_type == item.item_type,
            em.MediaItem.id != item.id,
            em.MediaItem.is_hidden == False,  # noqa: E712
            genre_cond,
        )
        .order_by(func.coalesce(em.MediaItem.community_rating, 0).desc())
        .limit(limit)
        .all()
    )
    return _query_result(rows, user, db, _base_url(request))


# ---- 祖先链路（客户端靠它做面包屑与“剧 → 季 → 集”导航）----

@emby_router.get("/emby/Items/{item_id}/Ancestors")
@emby_router.get("/Items/{item_id}/Ancestors")
def item_ancestors(item_id: str, request: Request,
                   user: models.WebUser = Depends(get_emby_user),
                   db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    chain: list = []
    seen: set = set()
    if item.item_type == "episode" and item.parent is not None:
        chain.append(item.parent)
        seen.add(item.parent.id)
    if item.item_type in ("episode", "season") and item.series is not None and item.series.id not in seen:
        chain.append(item.series)
    return _query_result(chain, user, db, _base_url(request))


# ---- 本服务没有的媒体附件：返回空结果集而非 404 ----

@emby_router.get("/emby/Items/{item_id}/LocalTrailers")
@emby_router.get("/Items/{item_id}/LocalTrailers")
def local_trailers(item_id: str, user: models.WebUser = Depends(get_emby_user)):
    return _empty_items()


@emby_router.get("/emby/Items/{item_id}/SpecialFeatures")
@emby_router.get("/Items/{item_id}/SpecialFeatures")
@emby_router.get("/emby/Users/{user_id}/Items/{item_id}/SpecialFeatures")
@emby_router.get("/Users/{user_id}/Items/{item_id}/SpecialFeatures")
def special_features(item_id: str, user_id: str = "",
                     user: models.WebUser = Depends(get_emby_user)):
    # 部分客户端只走带用户前缀的形态（与 Intros / PlaybackInfo 的双形态同理）：
    # 此前只有无前缀形态，带前缀请求直接 404（2026-10-22 晚实测 7 次）。
    return _empty_items()


@emby_router.get("/emby/Items/{item_id}/ThemeVideos")
@emby_router.get("/Items/{item_id}/ThemeVideos")
@emby_router.get("/emby/Items/{item_id}/ThemeSongs")
@emby_router.get("/Items/{item_id}/ThemeSongs")
def theme_items(item_id: str, user: models.WebUser = Depends(get_emby_user)):
    return _empty_items()


@emby_router.get("/emby/Items/{item_id}/ThemeMedia")
@emby_router.get("/Items/{item_id}/ThemeMedia")
def theme_media(item_id: str, user: models.WebUser = Depends(get_emby_user)):
    return {
        "ThemeVideosResult": _empty_items(),
        "ThemeSongsResult": _empty_items(),
        "SoundtrackSongsResult": _empty_items(),
    }


@emby_router.get("/emby/Items/{item_id}/Intros")
@emby_router.get("/Items/{item_id}/Intros")
@emby_router.get("/emby/Users/{user_id}/Items/{item_id}/Intros")
@emby_router.get("/Users/{user_id}/Items/{item_id}/Intros")
def item_intros(item_id: str, user_id: str = "",
                user: models.WebUser = Depends(get_emby_user)):
    return _empty_items()


@emby_router.get("/emby/Items/{item_id}/CriticReviews")
@emby_router.get("/Items/{item_id}/CriticReviews")
def critic_reviews(item_id: str, user: models.WebUser = Depends(get_emby_user)):
    return []


# ---- 分类元数据 ----

def _named_items(kind: str, names: list) -> dict:
    # 字段对照官方 BaseItemDto（Genre/Studio 等虚拟条目同样由 DtoService 生成：
    # ServerId 无条件下发、MediaType 恒发、UserData 带 ItemId/Key、ImageBlurHashes
    # 恒为空字典）。少发 ServerId 会让严格的三方客户端把整个分类列表丢掉——
    # 与 Views 缺 ServerId 是同一类 P0。
    items = []
    for n in names:
        item_id = _guid_of(kind, n)
        items.append({
            "Name": n,
            "ServerId": SERVER_ID,
            "Id": item_id,
            "Type": kind,
            "MediaType": "Unknown",
            "ImageTags": {},
            "BackdropImageTags": [],
            "ImageBlurHashes": {},
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False,
                         "IsFavorite": False, "PlayedPercentage": 0.0,
                         "ItemId": item_id, "Key": item_id},
        })
    return {"Items": items, "TotalRecordCount": len(items), "StartIndex": 0}


def _menu_names(db: Session, kind: str, column) -> list[str]:
    """分类菜单取值（/Genres、/Studios）：优先关联表，老库没回填完才读整列

    旧实现每个请求都把整库的 genres/studios 列读回来再切分（十万级库约 190ms 的
    全表扫描，而且随库线性增长）——客户端每打开一次就付一次。关联表路径走
    ``(kind, value, item_id)`` 覆盖索引上的 DISTINCT，并且带进程内取值缓存
    （扫描 / 删条目后由 facets 主动失效），量到的对比见
    scripts/benchmark_item_facets.py 第 3 节。
    """
    if facets.ensure_ready(db):
        return sorted(facets.kind_values(db, kind))
    return sorted({
        value
        for (raw,) in db.query(column).all()
        for value in (raw or "").split(",")
        if value
    })


@emby_router.get("/emby/Genres")
@emby_router.get("/Genres")
def genres_list(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    return _named_items("Genre", _menu_names(db, facets.KIND_GENRE, em.MediaItem.genres))


@emby_router.get("/emby/Genres/{name}")
@emby_router.get("/Genres/{name}")
def genre_by_name(name: str, user: models.WebUser = Depends(get_emby_user)):
    item_id = _guid_of("Genre", name)
    return {"Name": name, "ServerId": SERVER_ID, "Id": item_id, "Type": "Genre",
            "MediaType": "Unknown",
            "ImageTags": {}, "BackdropImageTags": [], "ImageBlurHashes": {},
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False,
                         "IsFavorite": False, "PlayedPercentage": 0.0,
                         "ItemId": item_id, "Key": item_id}}


@emby_router.get("/emby/Studios")
@emby_router.get("/Studios")
def studios_list(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    return _named_items("Studio", _menu_names(db, facets.KIND_STUDIO, em.MediaItem.studios))


# ---- 筛选分面列表（年份 / 标签 / 分级）----
#
# Items 列表的 Years=/Tags=/OfficialRatings= 过滤参数早已实现，缺的只是给
# 筛选面板供数的三个列表端点 —— 带分面筛选 UI 的客户端（Hills 等）打开库内
# 筛选时先拉这三个端点，此前全部 404（2026-10-22 晚各实测 7 次），筛选面板
# 直接残缺。取值直接复用 Items/Filters 的同一份聚合（_filters_payload，
# 带缓存与 facets 关联表口径），保证「面板里选得到的」与「Filters 报的」一致。

@emby_router.get("/emby/Years")
@emby_router.get("/Years")
def years_list(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    return _named_items("Year", [str(y) for y in _filters_payload(db)["Years"]])


@emby_router.get("/emby/Tags")
@emby_router.get("/Tags")
def tags_list(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    return _named_items("Tag", _filters_payload(db)["Tags"])


@emby_router.get("/emby/OfficialRatings")
@emby_router.get("/OfficialRatings")
def official_ratings_list(user: models.WebUser = Depends(get_emby_user),
                          db: Session = Depends(get_db)):
    return _named_items("OfficialRating", _filters_payload(db)["OfficialRatings"])


@emby_router.get("/emby/Persons")
@emby_router.get("/Persons")
def persons_list(request: Request, user: models.WebUser = Depends(get_emby_user),
                 db: Session = Depends(get_db),
                 Limit: str | None = None, StartIndex: str | None = None):
    """演员列表（v2.51.0）：``emby_people`` 落库后返回真实数据。

    以前刮削没落演员表，这里只能返回空（``_empty_items``）；现在 TMDB credits
    已入库，同名合并为一条：``Id`` 取最早入库行的 id（稳定）；同名里只要有一条
    有头像就给 ``PrimaryImageTag``（取图按名字找第一条有头像的行，
    见 ``media_routes.person_image``）。
    """
    # r50：畸形 Limit/StartIndex 回 default（此前 FastAPI 原生 int 强转 → 422；
    # 全站其余协议端点走 qint 畸形回 default，口径统一）。声明为 str 由 FastAPI 原样
    # 绑定、再经 qint/qstart 宽容解析；保留 Limit/StartIndex kwargs 兼容旧的直接调用
    #（tests/test_metadata_cast.py），任何行为都不放宽。
    start = qstart({"StartIndex": StartIndex})
    limit = qint({"Limit": Limit}, "Limit", 5000, lo=0, hi=5000)
    base = (
        db.query(
            em.EmbyPerson.name,
            func.min(em.EmbyPerson.id).label("pid"),
            func.max(em.EmbyPerson.image).label("img"),
        )
        .group_by(em.EmbyPerson.name)
        .order_by(em.EmbyPerson.name)
    )
    total = base.count()
    items = []
    # func.max(image)：只要同名里有一条有头像，max 出来就是非空 URL；
    # 全空/全 NULL 时 max 是 '' 或 None，都判假。
    for name, pid, img in base.offset(start).limit(limit).all():
        entry = {"Name": name, "Id": str(pid), "Type": "Person",
                 "ImageTags": {}, "BackdropImageTags": []}
        if img:
            entry["ImageTags"] = {"Primary": str(pid)}
        items.append(entry)
    return {"Items": items, "TotalRecordCount": total, "StartIndex": start}


# ---- 媒体库视图别名 ----

# user_views 自己已经是同步 `def`（体内没有 await），这里跟着改成同步路由：
# 异步路由里 await 一个同步函数，同步查询就还是跑在事件循环上，改了等于没改。


@emby_router.get("/emby/Library/MediaFolders")
@emby_router.get("/Library/MediaFolders")
def library_media_folders(user: models.WebUser = Depends(get_emby_user),
                          db: Session = Depends(get_db)):
    return user_views("me", user, db)


@emby_router.get("/emby/UserViews")
@emby_router.get("/UserViews")
def user_views_alias(user: models.WebUser = Depends(get_emby_user),
                     db: Session = Depends(get_db)):
    return user_views("me", user, db)


# ---- 系统 / 诊断 ----

@emby_router.get("/emby/System/Endpoint")
@emby_router.get("/System/Endpoint")
def system_endpoint(user: models.WebUser = Depends(get_emby_user)):
    return {"IsLocal": True, "IsInNetwork": True}


@emby_router.post("/emby/Sessions/Logout")
@emby_router.post("/Sessions/Logout")
def sessions_logout(request: Request, user: models.WebUser = Depends(get_emby_user),
                    db: Session = Depends(get_db)):
    resolved = resolve_token(db, request)
    if resolved:
        resolved[1].is_revoked = True
        db.commit()
    return {"success": True}


@emby_router.get("/emby/Localization/ParentalRatings")
@emby_router.get("/Localization/ParentalRatings")
def parental_ratings(user: models.WebUser = Depends(get_emby_user)):
    return []


@emby_router.get("/emby/Plugins")
@emby_router.get("/Plugins")
def plugins(user: models.WebUser = Depends(get_emby_user)):
    return []


@emby_router.get("/emby/ScheduledTasks")
@emby_router.get("/ScheduledTasks")
def scheduled_tasks(user: models.WebUser = Depends(get_emby_user)):
    return []


@emby_router.get("/emby/Activity/Log/Entries")
@emby_router.get("/Activity/Log/Entries")
def activity_log(user: models.WebUser = Depends(get_emby_user)):
    return _empty_items()


# ---- 转码释放（已删除，保留协议空壳） / 库刷新 ----
# 转码删除（2026-10-24）：服务端不再有转码会话。Emby 客户端在停止播放
# 时仍会按协议调用 ActiveEncodings 删除端点；保留 204 空壳只是为了
# 不让客户端把“停播”误判为失败，这里不再释放任何东西。

@emby_router.delete("/emby/Videos/ActiveEncodings")
@emby_router.delete("/Videos/ActiveEncodings")
@emby_router.post("/emby/Videos/ActiveEncodings/Delete")
@emby_router.post("/Videos/ActiveEncodings/Delete")
def delete_active_encodings(request: Request,
                            user: models.WebUser = Depends(get_emby_user)):
    logger.info("释放转码会话（空壳，转码已删除） user=%s", user.id)
    return Response(status_code=204)


@emby_router.post("/emby/Library/Refresh")
@emby_router.post("/Library/Refresh")
def library_refresh(request: Request,
                    user: models.WebUser = Depends(get_emby_user),
                    db: Session = Depends(get_db)):
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    # 触发全库重扫是写操作：只读角色不能通过 Emby 接口绕道（v2.26.0）
    admin_roles.ensure_admin_allowed(request, user)
    # 入队而不是直接起线程（v2.27.0）：客户端点「刷新媒体库」往往一次触发好几个库，
    # 而那些库很可能指向同一个远程挂载——由队列按挂载串行化，已在队列里的会自动合并。
    # （Emby 客户端触发的：流水里与面板按钮、节点转发区分开）
    from backend.emby_server import scan_queue

    queued: list[int] = []
    already: list[int] = []
    for lib in db.query(em.Library).filter(em.Library.is_enabled == True).all():  # noqa: E712
        result = scan_queue.enqueue(lib, trigger="client")
        (queued if result["created"] else already).append(lib.id)
    return {"success": True, "libraries": queued, "already": already}




# ==================== 弹幕插件空兼容 / 品牌 Logo（2026-10-22 兼容补齐）====================
#
# G7：国区客户端（Hills / Streama 系）的弹幕加载走 Danmu 插件约定路径
# ``/api/danmu/{item_id}``（含 ``/raw`` 原始 XML 形态），挂在 /emby 前缀下。
# 本服务没有弹幕功能，此前每次播放先撞一次 404（2026-10-22 晚实测 9 次）。
# 这里只做「合法空弹幕」承接：返回可解析的空弹幕文档（bilibili 风格 XML，
# 根元素 <i> 下零条 <d>），客户端拿到就是「这部片没弹幕」，不再报错重试。
# 不实现弹幕的抓取 / 存储 / 下发，那是独立产品功能，不在本兼容范围内。

_EMPTY_DANMU_XML = '<?xml version="1.0" encoding="utf-8"?><i></i>'


@emby_router.get("/emby/api/danmu/{item_id}")
@emby_router.get("/api/danmu/{item_id}")
@emby_router.get("/emby/api/danmu/{item_id}/raw")
@emby_router.get("/api/danmu/{item_id}/raw")
def danmu_empty(item_id: str, user: models.WebUser = Depends(get_emby_user)):
    return Response(content=_EMPTY_DANMU_XML,
                    media_type="text/xml; charset=utf-8")


# G10：GET /Branding/Logo —— 个别客户端拿它当服务器图标，此前 404（实测 2 次）。
# 本服务没有单独的台标图资源：仓库里有现成站点图标（user_frontend 的
# favicon.ico，全量部署形态下存在）就发它；后端独立镜像（Dockerfile.backend
# 白名单 COPY 不含 user_frontend）没有该文件时，降级为 1×1 透明 PNG 的
# 200 响应 —— 对客户端而言是「有图但为空」，不再把服务器图标位打成错误。

_LOGO_FALLBACK_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@emby_router.get("/emby/Branding/Logo")
@emby_router.get("/Branding/Logo")
def branding_logo(user: models.WebUser = Depends(get_emby_user)):
    import base64
    from pathlib import Path

    favicon = (Path(__file__).resolve().parents[2]
               / "user_frontend" / "public" / "favicon.ico")
    if favicon.is_file():
        # 不走 streaming.serve_file：它按媒体文件猜类型，会把 .ico 报成
        # video/mp4（实测），客户端同样会当坏图。这里显式给 image/x-icon。
        return Response(content=favicon.read_bytes(),
                        media_type="image/x-icon")
    return Response(content=base64.b64decode(_LOGO_FALLBACK_PNG_B64),
                    media_type="image/png")

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
import os

from backend import admin_roles, models, share_guard
from backend.authlog import client_ip as real_client_ip
from backend.database import SessionLocal, get_db
from backend.emby_server import models as em
from backend.emby_server.auth import get_emby_user, parse_emby_authorization, resolve_token
from backend.emby_server import facets
from backend.emby_server.facets import count_virtual_items  # 索引版（虚拟库条目数）
from backend.emby_server.query_params import qlimit
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
    _guid_of,
    _now_playing_dto,
    _policy_dto,
    _query_result,
    _require_visible_item,
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
    new_session = session is None
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
                db.commit()
                return None, False
            verdict = share_guard.note_activity(db, user, ip, commit=False)
            if verdict and verdict.get("blocked"):
                logger.info("跨城市停用后拒绝建会话：user=%s", user.id)
                db.commit()
                return None, False
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
    return session, True


# 三个会话上报端点的同步实现（路由只负责解析 JSON + 丢线程池，见下面各路由的说明）。
# 会话上报是**全站最热**的写路径：每个正在播放的客户端每 10 秒上报一次进度，
# 一次上报要读会话 / 写位置 / 写观看进度（2~3 条 SQL + 1 次提交）。这些 SQL 以前
# 跑在事件循环上：多路并发播放时，所有人的其它请求都得排在这几次写事务后面。


def _record_playing(request: Request, user: models.WebUser, db: Session, body: dict) -> None:
    item = _require_visible_item(db, user, body.get("ItemId") or "")
    session, _wrote = _upsert_session(db, request, user, item, body)
    if session is None:
        # 被防共享拦下（session 返回 None = 不建会话）
        raise HTTPException(
            status_code=403,
            detail="同时播放的会话数已达上限，本次播放请求已被拦截",
        )


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

    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id
    ).first()
    if not umd:
        # 第一次上报这个条目：即使会话被节流也必须落库（否则「继续观看」没有起点）
        umd = em.UserMediaData(user_id=user.id, item_id=item.id)
        db.add(umd)
    elif (
        not wrote
        and (umd.playback_position_ticks or 0) == pos
        and not (finished and not umd.played)
    ):
        # 会话和观看进度都没有新信息：同一次上报不必再来一次写事务（见文件头的节流说明）
        return

    umd.playback_position_ticks = pos
    umd.last_played_at = datetime.now()
    if finished:
        if not umd.played:
            umd.play_count = (umd.play_count or 0) + 1
        umd.played = True
        umd.playback_position_ticks = 0
    db.commit()


def _record_stopped(request: Request, user: models.WebUser, db: Session, body: dict) -> None:
    item = _require_visible_item(db, user, body.get("ItemId") or "")
    _upsert_session(db, request, user, item, body, ended=True)


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
    for s in sessions:
        item = db.query(em.MediaItem).filter(em.MediaItem.id == s.item_id).first()
        user = db.query(models.WebUser).filter(models.WebUser.id == s.user_id).first()
        if item and user:
            result.append(_now_playing_dto(s, item, user))
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
        # 转码已删除（2026-10-17）：这里只标记播放会话结束，不再有转码进程可杀。
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
    return _user_data_dto(umd)


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
def special_features(item_id: str, user: models.WebUser = Depends(get_emby_user)):
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


@emby_router.get("/emby/Persons")
@emby_router.get("/Persons")
def persons_list(user: models.WebUser = Depends(get_emby_user),
                 db: Session = Depends(get_db),
                 Limit: int | None = None,
                 StartIndex: int = 0):
    """演员列表（v2.51.0）：``emby_people`` 落库后返回真实数据。

    以前刮削没落演员表，这里只能返回空（``_empty_items``）；现在 TMDB credits
    已入库，同名合并为一条：``Id`` 取最早入库行的 id（稳定）；同名里只要有一条
    有头像就给 ``PrimaryImageTag``（取图按名字找第一条有头像的行，
    见 ``media_routes.person_image``）。
    """
    start = max(0, int(StartIndex or 0))
    limit = 5000 if Limit is None else max(0, min(int(Limit), 5000))
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


# ---- 转码释放 / 库刷新 ----

@emby_router.delete("/emby/Videos/ActiveEncodings")
@emby_router.delete("/Videos/ActiveEncodings")
@emby_router.post("/emby/Videos/ActiveEncodings/Delete")
@emby_router.post("/Videos/ActiveEncodings/Delete")
def delete_active_encodings(request: Request,
                            user: models.WebUser = Depends(get_emby_user)):
    # 转码已删除（2026-10-17）：不再有转码会话可释放。端点保留 204，
    # 兼容会在停止播放时调用它的客户端（删端点会让它们吃 404）。
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



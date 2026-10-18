"""播放流与字幕投递路由

由 ``backend/emby_server/api.py`` 拆分而来（v2.13.0），容器后缀变体流、原始文件端点（挂载来源由 mount_routes 接管）、
字幕投递。转码已删除（2026-10-17），不再有 HLS 通配路由。

路由仍注册在同一个 ``emby_router`` 上（从 ``api`` 导入同一实例），注册顺序与拆分前一致，
因此 ``mount_routes`` / ``image_routes`` / ``session_routes`` 的「按路径 + 端点名替换」逻辑不受影响。
模块由 ``backend/main.py`` 与 ``emby_api/main.py`` 在 ``include_router`` 之前导入。
无阻塞的协议路由一律写成同步 ``def``，由 Starlette 线程池执行，避免同步查询/文件 IO 卡住事件循环。
"""
from __future__ import annotations

import logging

from backend import models
from backend.emby_server.async_db import release_db_before_response
from backend.database import SessionLocal, get_db
from backend.emby_server import models as em
from backend.emby_server import subtitles as subs
from backend.emby_server.playback_security import validate_local_file_path
from backend.emby_server.auth import get_emby_user, get_play_user, parse_emby_authorization, resolve_token
from backend.subscriptions import ensure_download_allowed, ensure_playback_allowed
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy.orm import Session, joinedload
import os

from backend.emby_server.api import (
    emby_router,
    _require_visible_item,
    video_stream,
)

logger = logging.getLogger(__name__)


# ---- 播放流（容器后缀变体 / 原始文件）----

@emby_router.get("/emby/Videos/{item_id}/stream.{container}")
@emby_router.get("/Videos/{item_id}/stream.{container}")
async def video_stream_container(item_id: str, container: str, request: Request,
                                 user: models.WebUser = Depends(get_play_user),
                                 db: Session = Depends(get_db)):
    return await video_stream(item_id, request, user, db)


@emby_router.get("/emby/Items/{item_id}/File")
@emby_router.get("/Items/{item_id}/File")
@release_db_before_response
def item_file(item_id: str, user: models.WebUser = Depends(get_emby_user),
              db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    ensure_playback_allowed(db, user)
    if not item.file_path:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        file_path = validate_local_file_path(item.file_path)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, filename=os.path.basename(file_path))


# ---- 字幕投递 ----

def _find_subtitle_stream(item: em.MediaItem, sub_index: str):
    try:
        index = int(sub_index)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Subtitle stream not found")
    for s in item.streams:
        if (s.stream_type or "").lower() == "subtitle" and s.stream_index == index:
            return s
    raise HTTPException(status_code=404, detail="Subtitle stream not found")


def _subtitle_ordinal(item: em.MediaItem, stream) -> int:
    """该字幕轨在条目内“第几条字幕”（ffmpeg -map 0:s:N 需要序号而非绝对流索引）"""
    ordered = sorted(
        [s for s in item.streams if (s.stream_type or "").lower() == "subtitle"],
        key=lambda s: s.stream_index or 0,
    )
    for i, s in enumerate(ordered):
        if s.id == stream.id:
            return i
    return 0


def _serve_subtitle(item_id: str, sub_index: str, fmt: str, user, db) -> Response:
    item = _require_visible_item(db, user, item_id)
    # 字幕与播放同一门槛，避免非会员绕过付费墙拿到内容文本
    ensure_playback_allowed(db, user)
    stream = _find_subtitle_stream(item, sub_index)
    return subs.render_subtitle(
        item_guid=item.guid,
        media_path=item.file_path,
        stream=stream,
        fmt=fmt,
        subtitle_ordinal=_subtitle_ordinal(item, stream),
    )


@emby_router.get("/emby/Videos/{item_id}/{media_source_id}/Subtitles/{sub_index}/Stream.{fmt}")
@emby_router.get("/Videos/{item_id}/{media_source_id}/Subtitles/{sub_index}/Stream.{fmt}")
@emby_router.get("/emby/Videos/{item_id}/subtitles/{sub_index}/Stream.{fmt}")
@emby_router.get("/Videos/{item_id}/subtitles/{sub_index}/Stream.{fmt}")
def video_subtitle(item_id: str, sub_index: str, fmt: str, request: Request,
                   media_source_id: str = "",
                   user: models.WebUser = Depends(get_play_user),
                   db: Session = Depends(get_db)):
    return _serve_subtitle(item_id, sub_index, fmt, user, db)


@emby_router.get("/emby/Videos/{item_id}/{media_source_id}/Subtitles/{sub_index}/{start_ticks}/Stream.{fmt}")
@emby_router.get("/Videos/{item_id}/{media_source_id}/Subtitles/{sub_index}/{start_ticks}/Stream.{fmt}")
@emby_router.get("/emby/Videos/{item_id}/subtitles/{sub_index}/{start_ticks}/Stream.{fmt}")
@emby_router.get("/Videos/{item_id}/subtitles/{sub_index}/{start_ticks}/Stream.{fmt}")
def video_subtitle_offset(item_id: str, sub_index: str, start_ticks: str, fmt: str,
                          request: Request, media_source_id: str = "",
                          user: models.WebUser = Depends(get_play_user),
                          db: Session = Depends(get_db)):
    # 非直播场景忽略时间偏移，直接投递完整字幕
    return _serve_subtitle(item_id, sub_index, fmt, user, db)

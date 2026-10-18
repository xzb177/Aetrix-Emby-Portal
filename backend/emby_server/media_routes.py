"""图片与下载路由

由 ``backend/emby_server/api.py`` 拆分而来（v2.13.0），图片投递（含带序号的 Backdrop/Thumb 等）、原始文件下载。
转码已删除（2026-10-17），不再有 HLS 入口。

路由仍注册在同一个 ``emby_router`` 上（从 ``api`` 导入同一实例），注册顺序与拆分前一致，
因此 ``mount_routes`` / ``image_routes`` / ``session_routes`` 的「按路径 + 端点名替换」逻辑不受影响。
模块由 ``backend/main.py`` 与 ``emby_api/main.py`` 在 ``include_router`` 之前导入。
无阻塞的协议路由一律写成同步 ``def``，由 Starlette 线程池执行，避免同步查询/文件 IO 卡住事件循环。
"""
from __future__ import annotations

import logging

from backend import admin_roles, models
from backend.emby_server.async_db import release_db_before_response
from backend.database import SessionLocal, get_db
from backend.emby_server import image_store
from backend.emby_server import models as em
from backend.emby_server.auth import get_emby_user, parse_emby_authorization, resolve_token
from backend.emby_server.streaming import (
    serve_file,
    serve_image,
)
from backend.subscriptions import ensure_download_allowed, ensure_playback_allowed
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session, joinedload
import os

from backend.emby_server.api import (
    emby_router,
    _first_image,
    _item_visible,
    _play_target,
    _queue_image_repair,
    _require_visible_item,
)

logger = logging.getLogger(__name__)


def _remember_local_image(db: Session, item: em.MediaItem, kind: str, path: str) -> None:
    """把本地化后的图片路径记到条目上（下一次取图直接命中本地）

    写库失败只记日志：图片本身已经在磁盘上，这个函数只是让后续请求少走一步。
    """
    try:
        if kind == "Primary":
            item.poster_path = path
        else:
            item.backdrop_path = path
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("记录本地化图片路径失败 item=%s: %s", getattr(item, "guid", "?"), exc)
        db.rollback()


@emby_router.get("/emby/Items/{item_id}/Download")
@emby_router.get("/Items/{item_id}/Download")
@release_db_before_response
def download_item(item_id: str, request: Request,
                  user: models.WebUser = Depends(get_emby_user),
                  db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    # 付费墙：下载与在线播放同一门槛，避免绕过
    ensure_playback_allowed(db, user)
    # 站点级下载开关：第三方播放器触发的下载同样拦下。
    # request 带上网关（DownloadGuardMiddleware）已判过的结论，避免同一请求查两遍。
    ensure_download_allowed(db, user, request=request)
    target = _play_target(db, item)
    if target.kind == "url":
        # 最简播放（SEAM-3，中继已删）：Drive 条目 302 到谷歌 usercontent
        # 直链下载；rebase 到世界窗口定稿后按其处置更新。
        _fid = (getattr(item, "drive_file_id", "") or "").strip()
        if _fid:
            from backend.emby_server.drive_auth import drive_usercontent_download_url
            return RedirectResponse(drive_usercontent_download_url(_fid), status_code=302)
        raise HTTPException(status_code=410, detail="relay removed")
    return FileResponse(target.value, filename=os.path.basename(target.value))


def _sized_image(src: str, max_width=None, max_height=None) -> str:
    """按客户端要的尺寸发图：有现成/能生成的缩略图就发小图，否则发原图

    maxWidth/maxHeight 是标准 Emby 查询参数（前端海报墙发 maxWidth=320，
    详情页背景发 maxWidth=1280）；w/h 是同义别名（?w=300），标准参数优先。
    只缩小不放大，失败静默回原图。
    """
    if (max_width or max_height) and src and os.path.isfile(src):
        thumb = image_store.resized_variant(src, max_width=max_width,
                                            max_height=max_height)
        if thumb:
            return thumb
    return src


def serve_thumbnail(path: str) -> Response:
    """缩略图响应：先查内存 LRU（默认 32MB 总量，key=内容寻址文件名）

    内存命中直接发字节、不碰磁盘；未命中读盘一次并回填。ETag 口径与
    streaming.serve_image 一致（mtime-size），image_routes 的 304 逻辑照样生效。
    """
    from email.utils import formatdate

    name = os.path.basename(path)
    data = image_store.thumb_mem_get(name)
    try:
        st = os.stat(path)
    except OSError:
        raise HTTPException(status_code=404, detail="Image not found")
    if data is None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            raise HTTPException(status_code=404, detail="Image not found")
        image_store.thumb_mem_put(name, data)
    return Response(
        content=data,
        media_type="image/jpeg",  # 缩略图统一转 JPEG（见 resized_variant）
        headers={
            "ETag": f'"{int(st.st_mtime)}-{st.st_size}"',
            "Last-Modified": formatdate(st.st_mtime, usegmt=True),
            "Cache-Control": "public, max-age=86400",
        },
    )


def _serve_sized(src: str, max_width=None, max_height=None):
    """尺寸感知的图片下发：缩略图走内存缓存，原图走 FileResponse"""
    sized = _sized_image(src, max_width, max_height)
    if sized and image_store.is_thumb_variant(sized):
        return serve_thumbnail(sized)
    return serve_image(src)


@emby_router.get("/emby/Items/{item_id}/Images/{image_type}")
@emby_router.get("/Items/{item_id}/Images/{image_type}")
def item_image(item_id: str, image_type: str, request: Request,
               db: Session = Depends(get_db),
               maxWidth: str | None = None, maxHeight: str | None = None,
               w: str | None = None, h: str | None = None):
    """条目图片

    三处修正：

    1. **季/集回退**：季与集经常没有自己的图片，按「条目 → 季 → 剧集海报」回退；
    2. **数据库有记录但文件丢失**：不再把 FileNotFoundError 抛成 5xx（客户端会当成
       鉴权/服务器故障反复重试），而是干净地 404，同时把条目排进修复队列，
       下一轮扫描换成 TMDB 远程图；
    3. **远程图取不到**（404/超时）同样 404 并排队修复，而不是 500。

    尺寸参数：标准 Emby maxWidth/maxHeight 优先，w/h 是同义别名（见
    image_store.pick_dim）。非法值不 422，直接回原图。
    """
    # 按场景精确尺寸：海报墙 320/160、详情页 480、背景 1280，各调各的，不一刀切
    ew, eh = image_store.pick_dim(maxWidth, w), image_store.pick_dim(maxHeight, h)
    # 媒体库封面：第三方客户端用库的 guid 拉 /Images/Primary
    if image_type == "Primary":
        lib = db.query(em.Library).filter(em.Library.guid == item_id).first()
        cover_rel = getattr(lib, "cover_path", None) if lib is not None else None
        if cover_rel:
            import os as _os
            abs_path = cover_rel if _os.path.isabs(cover_rel) else _os.path.abspath(
                _os.path.join(image_store.image_dir(), cover_rel))
            if abs_path and _os.path.isfile(abs_path):
                return _serve_sized(abs_path, ew, eh)
            raise HTTPException(status_code=404, detail="Image not found")
    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_id).first()
    if not item:
        if image_type == "Primary" and item_id.isdigit():
            # 演员头像：People[].Id 是 emby_people 行 id（数字串，与 32 位 guid 不撞），
            # 客户端按 /Items/{Person.Id}/Images/Primary 取图
            person = db.query(em.EmbyPerson).filter(
                em.EmbyPerson.id == int(item_id)).first()
            if person is not None:
                return _serve_person_image(person.name, db, ew, eh)
        raise HTTPException(status_code=404, detail="Item not found")
    if image_type not in ("Primary", "Backdrop", "Art", "Thumb", "Logo"):
        raise HTTPException(status_code=404, detail="Image not found")
    kind = "Primary" if image_type == "Primary" else "Backdrop"

    src = _first_image(item, kind, db)
    if not src:
        raise HTTPException(status_code=404, detail="Image not found")
    if src.startswith("http://") or src.startswith("https://"):
        # 刮削图片本地化：远程图先落一份到本机（缓存）就直接用本地发，
        # 不必每个客户端、每次缓存穿透都去第三方拉一趟。落下就记住路径，
        # 下次请求直接命中本地；落不下来（源站挂了/被限速）就走下面的代理，行为不变。
        cached = image_store.localize(src)
        if cached:
            _remember_local_image(db, item, kind, cached)
            return _serve_sized(cached, ew, eh)
        # 仅允许代理 http(s) 远程图片（防 SSRF）
        # 共享客户端复用连接：海报墙穿透时不用每次建连
        from backend.emby_server import mounts as _mounts

        try:
            r = _mounts._shared_http_client("image_proxy", timeout=10).get(src)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 — 远程图失效不能让图片接口 5xx
            logger.warning("远程图片获取失败 %s: %s", src, e)
            _queue_image_repair(db, item, src)
            raise HTTPException(status_code=404, detail="Image not found") from e
        return Response(
            content=r.content,
            media_type=r.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if not os.path.isfile(src):
        # 数据库有图、本地文件已丢（换盘/迁移/挂载掉线）
        logger.warning("本地图片文件缺失，已排队修复：%s", src)
        _queue_image_repair(db, item, src)
        raise HTTPException(status_code=404, detail="Image not found")
    return _serve_sized(src, ew, eh)


@emby_router.get("/emby/Items/{item_id}/Images/{image_type}/{index}")
@emby_router.get("/Items/{item_id}/Images/{image_type}/{index}")
def item_image_index(item_id: str, image_type: str, index: str, request: Request,
                      db: Session = Depends(get_db),
                      maxWidth: str | None = None, maxHeight: str | None = None,
                      w: str | None = None, h: str | None = None):
    # 客户端普遍请求 /Images/Backdrop/0、/Images/Primary/0 这类带序号的地址。
    # 旧实现只注册了 Primary，其它类型（Backdrop/Thumb 等）会直接 404。
    return item_image(item_id, image_type, request, db,
                      maxWidth=maxWidth, maxHeight=maxHeight, w=w, h=h)


@emby_router.get("/emby/Persons/{name}/Images/Primary")
@emby_router.get("/Persons/{name}/Images/Primary")
def person_image(name: str, request: Request,
                 db: Session = Depends(get_db),
                 maxWidth: str | None = None, maxHeight: str | None = None,
                 w: str | None = None, h: str | None = None):
    """演员头像（v2.51.0）：``_item_dto`` 里 People 条目的 ``PrimaryImageTag``
    指向这里，对标 Emby 官方的 ``/emby/Persons/{Name}/Images/Primary``。

    同名演员取第一条有头像的行（``ORDER BY id``，行为稳定）。图片来源是
    ``emby_people.image`` 存的 TMDB 远程 URL——与 ``item_image`` 同口径：
    先 ``image_store.localize`` 落本地（刮削时已在 IO 阶段预热，通常是一次
    ``isfile``），落不下来就 404，不代理、不抛 5xx。只允许 http(s)（防 SSRF）。
    """
    ew, eh = image_store.pick_dim(maxWidth, w), image_store.pick_dim(maxHeight, h)
    return _serve_person_image(name, db, ew, eh)


def _serve_person_image(name: str, db: Session, ew=None, eh=None):
    """按演员名取第一条有头像的行并下发（本地化失败 404，不代理、不 5xx）。"""
    person = (
        db.query(em.EmbyPerson)
        .filter(em.EmbyPerson.name == name, em.EmbyPerson.image != "",
                em.EmbyPerson.image.isnot(None))
        .order_by(em.EmbyPerson.id)
        .first()
    )
    if not person or not person.image:
        raise HTTPException(status_code=404, detail="Image not found")
    src = person.image
    if not (src.startswith("http://") or src.startswith("https://")):
        raise HTTPException(status_code=404, detail="Image not found")
    cached = image_store.localize(src)
    if cached and os.path.isfile(cached):
        return _serve_sized(cached, ew, eh)
    raise HTTPException(status_code=404, detail="Image not found")


# ---------------------------------------------------------------------------
# 视频缩略图（对标 StrmAssistant #2 章节缩略图）
# ---------------------------------------------------------------------------

@emby_router.get("/emby/Items/{item_id}/Thumbnails/{index}")
@emby_router.get("/Items/{item_id}/Thumbnails/{index}")
@release_db_before_response
def item_thumbnail(item_id: str, index: int, request: Request,
                   user: models.WebUser = Depends(get_emby_user),
                   db: Session = Depends(get_db)):
    """视频预览缩略图：thumbnail_worker 按时长均分抽帧生成的 JPG。

    index 从 0 开始。没有缩略图时 404（worker 后台生成中）。
    """
    import os as _os

    from backend.emby_server import thumbnail_worker as _tw

    item = _require_visible_item(db, user, item_id)
    thumbs = _tw.list_thumbnails(item.guid)
    if not thumbs or index < 0 or index >= len(thumbs):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    path = thumbs[index]
    if not _os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return serve_file(path, request)


@emby_router.get("/emby/Items/{item_id}/Thumbnails")
@emby_router.get("/Items/{item_id}/Thumbnails")
def item_thumbnails(item_id: str, request: Request,
                    user: models.WebUser = Depends(get_emby_user),
                    db: Session = Depends(get_db)):
    """返回某条目的缩略图数量（供客户端判断是否有预览图）。"""
    from backend.emby_server import thumbnail_worker as _tw

    item = _require_visible_item(db, user, item_id)
    thumbs = _tw.list_thumbnails(item.guid)
    return {"TotalRecordCount": len(thumbs)}


# ---------------------------------------------------------------------------
# 多版本（对标 StrmAssistant #4 MergeMultiVersionTask）
# ---------------------------------------------------------------------------

def _version_dto(s) -> dict:
    """单个版本的信息，供版本切换器展示。"""
    from backend.emby_server.api import _version_label

    h = s.height or 0
    return {
        "Id": s.guid,
        "Name": _version_label(s),
        "Height": h,
        "Width": s.width or 0,
        "Size": s.size or 0,
        "Container": s.container or "",
        "VideoCodec": s.video_codec or "",
        "AudioCodec": s.audio_codec or "",
        "Bitrate": s.bitrate or 0,
        "VideoResolution": s.video_resolution or "",
        "MediaSource": s.media_source or "",
    }


@emby_router.get("/emby/Items/{item_id}/AlternateVersions")
@emby_router.get("/Items/{item_id}/AlternateVersions")
def item_alternate_versions(item_id: str, request: Request,
                            user: models.WebUser = Depends(get_emby_user),
                            db: Session = Depends(get_db)):
    """返回某条目的所有多版本（含主记录自己）。

    数据来源：merge_versions_worker 的物理合并（merged_into_id）。
    供详情页版本切换器、第三方客户端使用。
    """
    from backend.emby_server import merge_versions_worker as _mvw

    item = _require_visible_item(db, user, item_id)
    # 如果查的是被合并的版本，定位到主记录
    primary_id = item.merged_into_id or item.id
    versions = _mvw.get_alternate_versions(db, primary_id)
    # H1：只列用户可见媒体库里的版本（合并可能跨库）
    versions = sorted((v for v in versions if _item_visible(db, user, v)), key=lambda s: s.id)
    items = [_version_dto(s) for s in versions]
    # 标记主记录
    for v, s in zip(items, versions):
        v["IsPrimary"] = (s.id == primary_id)
    return {"Items": items, "TotalRecordCount": len(items)}


def _require_writer(request: Request, user) -> None:
    """全站共享数据的写操作：仅管理员，且角色可写（与 /api/admin/* 同一口径）"""
    if not getattr(user, "is_staff", False):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    admin_roles.ensure_admin_allowed(request, user)


# ==================== 片头片尾标记（StrmAssistant #3）====================

@emby_router.get("/emby/Items/{item_id}/IntroMarkers")
@emby_router.get("/Items/{item_id}/IntroMarkers")
def item_intro_markers(item_id: str,
                       user: models.WebUser = Depends(get_emby_user),
                       db: Session = Depends(get_db)):
    """返回某条目的片头片尾标记（含 Emby Chapter 格式，供播放器显示跳过按钮）。"""
    from backend.emby_server import intro_marker as _im

    item = _require_visible_item(db, user, item_id)
    markers = _im.get_markers(db, item.id)
    return {"Markers": markers, "Chapters": _im.to_chapters(markers)}


@emby_router.post("/emby/Items/{item_id}/IntroMarkers")
@emby_router.post("/Items/{item_id}/IntroMarkers")
def item_intro_marker_set(
    item_id: str,
    request: Request,
    marker_type: str = Body(...),
    start_ms: int = Body(...),
    end_ms: int = Body(...),
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
):
    """设置/更新片头片尾标记（同类型只保留一条，幂等）。"""
    from backend.emby_server import intro_marker as _im

    # 标记 / 剧集组选择是全站共享数据：只有可写角色的管理员能改（只读审计角色同样拦）
    _require_writer(request, user)
    item = _require_visible_item(db, user, item_id)
    try:
        return _im.set_marker(db, item.id, marker_type, start_ms, end_ms)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@emby_router.delete("/emby/Items/{item_id}/IntroMarkers/{marker_type}")
@emby_router.delete("/Items/{item_id}/IntroMarkers/{marker_type}")
def item_intro_marker_delete(item_id: str, marker_type: str, request: Request,
                             user: models.WebUser = Depends(get_emby_user),
                             db: Session = Depends(get_db)):
    """删除片头片尾标记。"""
    from backend.emby_server import intro_marker as _im

    # 标记 / 剧集组选择是全站共享数据：只有可写角色的管理员能改（只读审计角色同样拦）
    _require_writer(request, user)
    item = _require_visible_item(db, user, item_id)
    deleted = _im.delete_marker(db, item.id, marker_type)
    return {"deleted": deleted}


# ==================== TMDB 剧集组（StrmAssistant #15）====================

@emby_router.get("/emby/Items/{item_id}/EpisodeGroups")
@emby_router.get("/Items/{item_id}/EpisodeGroups")
def item_episode_groups(item_id: str,
                        user: models.WebUser = Depends(get_emby_user),
                        db: Session = Depends(get_db)):
    """返回某剧集的 TMDB 剧集组列表（含用户当前选择）。"""
    from backend.emby_server import episode_groups as _eg

    item = _require_visible_item(db, user, item_id)
    tmdb_id = (getattr(item, "tmdb_id", None) or "").strip()
    if not tmdb_id:
        return {"Groups": [], "Selected": ""}
    groups = _eg.get_episode_groups(tmdb_id)
    selected = _eg.get_selected_group(db, tmdb_id)
    return {"Groups": groups, "Selected": selected}


@emby_router.get("/emby/EpisodeGroups/{group_id}")
@emby_router.get("/EpisodeGroups/{group_id}")
def episode_group_detail(group_id: str,
                         user: models.WebUser = Depends(get_emby_user)):
    """返回某剧集组的详细信息（含每集的顺序映射）。"""
    from backend.emby_server import episode_groups as _eg

    detail = _eg.get_episode_group_detail(group_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Episode group not found")
    return detail


@emby_router.put("/emby/Items/{item_id}/EpisodeGroupSelection")
@emby_router.put("/Items/{item_id}/EpisodeGroupSelection")
def item_episode_group_select(
    item_id: str,
    request: Request,
    group_id: str = Body("", embed=True),
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
):
    """设置用户为某剧选择的剧集组（空串 = 恢复默认播出顺序）。"""
    from backend.emby_server import episode_groups as _eg

    # 标记 / 剧集组选择是全站共享数据：只有可写角色的管理员能改（只读审计角色同样拦）
    _require_writer(request, user)
    item = _require_visible_item(db, user, item_id)
    tmdb_id = (getattr(item, "tmdb_id", None) or "").strip()
    if not tmdb_id:
        raise HTTPException(status_code=400, detail="Item has no tmdb_id")
    _eg.set_selected_group(db, tmdb_id, group_id or "")
    return {"success": True, "selected": group_id or ""}

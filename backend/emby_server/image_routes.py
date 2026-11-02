"""图片端点：把客户端缓存校验接进来，命中缓存直接 304

`api.py` 里的图片实现负责真正的业务（季/集图片回退、远程图代理与 SSRF 防护、缺图排队修复、
本地文件丢失时干净 404），这里**不重复实现**任何一条，只在 `include_router` 之前把那几条
路由换成「包一层」的版本：

- 响应本身已经是带 `ETag` / `Last-Modified` / `Cache-Control` 的 `FileResponse`；
- 包一层之后就能处理 `If-None-Match` / `If-Modified-Since`，客户端缓存仍有效时直接返回
  **304（空响应体）**，而不是把同一张海报再传一遍。

媒体库页面上图片是请求量最大的资源（滚动、返回、切页都会重复请求同一张海报），
所以这一层省下的是最可观的那部分带宽。

与 `mount_routes.py` / `session_routes.py` 同一做法：按「路径 + 端点名」精确摘掉旧路由，
以后新增同名路径的实现不会被误伤。
"""
from __future__ import annotations

import logging
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from fastapi import Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend import models
from backend.database import get_db
from backend.emby_server import media_routes
from backend.emby_server.auth import get_emby_user

logger = logging.getLogger(__name__)

# 需要接管的图片路由（含 /emby/* 与裸根两套）
IMAGE_PATHS = (
    "/emby/Items/{item_id}/Images/{image_type}",
    "/Items/{item_id}/Images/{image_type}",
    "/emby/Items/{item_id}/Images/{image_type}/{index}",
    "/Items/{item_id}/Images/{image_type}/{index}",
)
# api.py 里的旧端点名（用于精确识别，避免误伤将来新增的同路径实现）
SUPERSEDED_ENDPOINTS = ("item_image", "item_image_index")

# 需要原样透传给 304 的响应头
PASSTHROUGH_HEADERS = ("etag", "last-modified", "cache-control", "accept-ranges", "content-type")


def _passthrough(response) -> dict:
    return {
        name: response.headers[name]
        for name in PASSTHROUGH_HEADERS
        if response.headers.get(name)
    }


def _not_modified(request: Request, headers: dict) -> bool:
    """客户端缓存是否仍然有效（If-None-Match 优先于 If-Modified-Since）"""
    etag = headers.get("etag")
    if etag:
        inm = request.headers.get("if-none-match")
        if inm:
            candidates = {token.strip() for token in inm.split(",")}
            if "*" in candidates or etag in candidates or f"W/{etag}" in candidates:
                return True
    ims = request.headers.get("if-modified-since")
    last_modified = headers.get("last-modified")
    if ims and last_modified:
        try:
            since = parsedate_to_datetime(ims)
            served = parsedate_to_datetime(last_modified)
        except (TypeError, ValueError):
            return False
        if since is None or served is None:
            return False
        if since.tzinfo is None:  # 少数客户端发不带时区的旧格式
            since = since.replace(tzinfo=timezone.utc)
        return served <= since
    return False


def with_conditional_get(response, request: Request):
    """把「缓存仍有效」的图片请求变成 304；其余响应原样返回

    带 ETag 校验器的响应才做这件事：本机文件（FileResponse）与内存缓存的
    缩略图（普通 Response，ETag 口径一致）都能 304；远程代理图（没有 ETag）
    与错误响应（404 等）保持原样，客户端行为与以前完全一致。
    """
    if request.method not in ("GET", "HEAD"):
        return response
    if not isinstance(response, Response) or response.status_code != 200:
        return response
    headers = _passthrough(response)
    if not headers.get("etag") or not _not_modified(request, headers):
        return response
    return Response(status_code=304, headers=headers)


def item_image_cached(
    item_id: str,
    image_type: str,
    request: Request,
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_emby_user),
    maxWidth: str | None = None,
    maxHeight: str | None = None,
    w: str | None = None,
    h: str | None = None,
):
    """条目图片：实现见 media_routes（v2.13.0 拆分），这里只补条件请求

    同步实现：图片是媒体库滚动时最高频的请求，读盘 / 代理远程图不能占着事件循环。
    maxWidth/maxHeight 透传给实现做缩略图（标准 Emby 参数，前端海报墙已在用）。
    PA-036：鉴权依赖必须在本层声明——本函数是直接调用实现函数（不是走
    FastAPI 注入），实现的 Depends 不会被执行，漏了这里就等于没鉴权。
    """
    return with_conditional_get(
        media_routes.item_image(item_id, image_type, request, db, user,
                                maxWidth=maxWidth, maxHeight=maxHeight,
                                w=w, h=h),
        request,
    )


def item_image_index_cached(
    item_id: str,
    image_type: str,
    index: str,
    request: Request,
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_emby_user),
    maxWidth: str | None = None,
    maxHeight: str | None = None,
    w: str | None = None,
    h: str | None = None,
):
    """带序号的图片地址（/Images/Backdrop/0 等）：同样交给 media_routes 的实现（同步）"""
    return with_conditional_get(
        media_routes.item_image_index(item_id, image_type, index, request, db, user,
                                      maxWidth=maxWidth, maxHeight=maxHeight,
                                      w=w, h=h),
        request,
    )


def install_image_routes(router) -> int:
    """替换图片端点（必须在 ``app.include_router(emby_router)`` **之前**调用）

    返回被替换的路由条数，供启动日志与冒烟测试断言。
    """
    kept = []
    superseded = 0
    for route in router.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(getattr(route, "endpoint", None), "__name__", "")
        if path in IMAGE_PATHS and endpoint in SUPERSEDED_ENDPOINTS:
            superseded += 1
            continue
        kept.append(route)
    if superseded:
        router.routes = kept

    for path in IMAGE_PATHS[:2]:
        router.get(path)(item_image_cached)
    for path in IMAGE_PATHS[2:]:
        router.get(path)(item_image_index_cached)

    logger.info("图片端点：已替换 %d 条旧路由（接入条件请求 304）", superseded)
    return superseded


__all__ = [
    "install_image_routes",
    "with_conditional_get",
    "item_image_cached",
    "item_image_index_cached",
    "IMAGE_PATHS",
    "SUPERSEDED_ENDPOINTS",
]

"""挂载来源的 ``/Items/{id}/File`` 端点

`api.py` 里的 ``/Items/{id}/File`` 是把 ``file_path`` 当本机文件的老实现：挂载来源的条目
（路径形如 ``mount://<挂载 id>/<相对路径>``）在它那里只能得到 404。

本模块在**注册协议面之前**把同名旧路由换成挂载感知实现（``install_mount_routes``），
运行期不保留重复端点：

- 本机来源：行为不变（``FileResponse``）；
- 挂载来源：由本服务按 Range 代理转发，115 Cookie / WebDAV Basic / AList 令牌等凭据不下发。

其它播放端点不需要在这里处理：``/Videos/{id}/stream``、``/Videos/{id}/Download`` 与 HLS
转码已在 ``api.py`` 内支持挂载，字幕端点通过 ``subtitles.register_mount_resolver``
自己认识 ``mount://`` 路径。
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from backend import models
from backend.emby_server.async_db import release_db_before_response
from backend.database import get_db
from backend.emby_server import mounts as mount_lib
from backend.emby_server.api import _play_target, _require_visible_item
from backend.emby_server.auth import get_play_user
from backend.subscriptions import ensure_playback_allowed

logger = logging.getLogger(__name__)

# 被替换的旧路由（``api.py`` 中 item_file 注册的两个路径）
SUPERSEDED_FILE_PATHS = ("/emby/Items/{item_id}/File", "/Items/{item_id}/File")


@release_db_before_response
def mounted_item_file(
    item_id: str,
    request: Request,
    user: models.WebUser = Depends(get_play_user),
    db: Session = Depends(get_db),
):
    """条目原始文件（Emby 客户端的「下载 / 拉文件」路径之一）

    r62：与其它播放投递端点统一走 get_play_user（短期签名优先，回退 Emby
    token/JWT）；此前 get_emby_user 不认 uid/exp/sign，签名链接会被 401。
    """
    item = _require_visible_item(db, user, item_id)
    # 与在线播放、下载同一门槛：付费墙对拉文件同样生效
    ensure_playback_allowed(db, user)
    # 复用 api 的目标解析与错误映射，保证两条端点对同一份数据行为一致
    target = _play_target(db, item)
    if target.kind == "url":
        # 最简播放（SEAM-3，中继已删）：Drive 条目 302 到谷歌 usercontent
        # 直链；rebase 到世界窗口定稿后按其处置更新。
        _fid = (getattr(item, "drive_file_id", "") or "").strip()
        if _fid:
            from backend.emby_server.drive_auth import drive_usercontent_download_url
            return RedirectResponse(drive_usercontent_download_url(_fid), status_code=302)
        raise HTTPException(status_code=410, detail="relay removed")
    if not target.value or not os.path.isfile(target.value):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target.value, filename=os.path.basename(target.value))


def install_mount_routes(router) -> int:
    """把只认本机文件的 ``/Items/{id}/File`` 换成挂载感知实现

    必须在 ``app.include_router(emby_router)`` **之前**调用：``include_router`` 会复制一份
    路由表，之后再改 router 不会生效。返回被替换的旧路由数量（供启动日志与测试断言）。
    """
    kept = []
    superseded = 0
    for route in router.routes:
        endpoint = getattr(route, "endpoint", None)
        is_old_file_route = (
            getattr(route, "path", "") in SUPERSEDED_FILE_PATHS
            and getattr(endpoint, "__name__", "") == "item_file"
        )
        if is_old_file_route:
            superseded += 1
            continue
        kept.append(route)
    if superseded:
        router.routes = kept
        logger.info("挂载端点：已替换 %d 条只认本机文件的 /Items/{id}/File 路由", superseded)
    router.get("/emby/Items/{item_id}/File")(mounted_item_file)
    router.get("/Items/{item_id}/File")(mounted_item_file)
    return superseded


__all__ = ["install_mount_routes", "mounted_item_file", "SUPERSEDED_FILE_PATHS"]

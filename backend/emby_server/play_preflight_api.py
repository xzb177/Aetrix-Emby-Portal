"""播放前体检接口（第二批第 2 单，只读诊断口）

``GET /emby/PlayPreflight?ItemId=<guid>[&variant=<name>]``（另有
``/PlayPreflight`` 裸路径别名，与 ``search_api`` 的双路径口径一致）。

只读：本口不下发播放链接、不签名、不改变体清单、不写库、不建
播放会话。可用变体名单复用 ``play_variants.list_variants`` 唯一
判定口（与 PlaybackInfo 同款输入：Drive 挂载可定位、挂载换链能力、
本用户 115 直链信号、本用户 Drive BYOS 信号），但不要求
``PLAY_VARIANTS_ENABLED`` 开——预检是自己带闸（``PLAY_PREFLIGHT_ENABLED``）
的诊断口，结论里的 ``variants_switch_on`` 会如实告知调用方
PlaybackInfo 当下是否在下发变体清单；开关关时预检结论仅供参考，
不代表 PlaybackInfo 会下发同一清单。

安全：条目必须经 ``_require_visible_item``（不存在 404、不可见
403）；绑定信号只算本用户自己的（``_user115_direct_available`` /
``_user_drive_byos_available`` 均以本用户为准），不碰他人盘。
预检 ``ready`` 不等于 PlaybackInfo 一定发链接：付费墙、播放门禁
（绑定门禁）仍在 PlaybackInfo 层执行，本口不替它们放行。

前端注意：本口挂在播放流程相关页面（点播前体检），绝不挂发现页
（首页冻结令）；返回体里永远没有播放链接。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend import models
from backend.database import get_db
from backend.emby_server import play_preflight as preflight
from backend.emby_server import play_variants
from backend.emby_server.api import (
    _DIRECT_STREAM_EXTS,
    _container_of,
    _drive_mount_id_for_item,
    _mount_can_generate_play_target,
    _require_visible_item,
    _user115_direct_available,
    _user_drive_byos_available,
)
from backend.emby_server.auth import get_emby_user

preflight_router = APIRouter(tags=["播放前体检"])


@preflight_router.get("/emby/PlayPreflight")
@preflight_router.get("/PlayPreflight")
def play_preflight(request: Request,
                   user: models.WebUser = Depends(get_emby_user),
                   db: Session = Depends(get_db)):
    """播放前体检：对本条目的候选变体做只读预检并选最匹配的直链。"""
    if not preflight.preflight_enabled():
        raise HTTPException(status_code=404, detail="播放前体检未启用")
    q = request.query_params
    item_id = (q.get("ItemId") or q.get("id") or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="ItemId 不能为空")
    requested = (q.get("variant") or "").strip() or None
    item = _require_visible_item(db, user, item_id)

    drive_mount_ok = False
    if (getattr(item, "drive_file_id", "") or "").strip():
        drive_mount_ok = _drive_mount_id_for_item(db, item) is not None
    variants = play_variants.list_variants(
        db, item,
        drive_mount_locatable=drive_mount_ok,
        mount_can_generate_play_target=_mount_can_generate_play_target(db, item),
        user115_direct_available=_user115_direct_available(db, item, user),
        user_drive_byos_available=_user_drive_byos_available(db, item, user))
    available_names = [v.name for v in variants]

    streams = [{
        "stream_type": s.stream_type, "codec": s.codec,
        "video_range": s.video_range, "is_default": s.is_default,
    } for s in (getattr(item, "streams", None) or [])]
    declared = {
        "container": _container_of(item),
        "video_codec": getattr(item, "video_codec", None),
        "audio_codec": getattr(item, "audio_codec", None),
        "streams": streams,
        "streamable_containers": _DIRECT_STREAM_EXTS,
    }
    if requested is not None:
        results = [preflight.preflight_variant(
            requested, available_names, **declared)]
        selected = None
        if (results[0].variant in preflight.DIRECT_VARIANTS
                and results[0].verdict in ("ready", "degraded")):
            selected = results[0]
    else:
        results = preflight.preflight_all(available_names, **declared)
        selected = preflight.select_best_direct(results)

    return {
        "item_id": item.guid,
        "requested_variant": requested,
        "available_variants": available_names,
        "variants_switch_on": play_variants.variants_enabled(),
        "results": [preflight.result_to_dict(r) for r in results],
        "selected_direct": (
            preflight.result_to_dict(selected) if selected else None),
        # 预检绝不提兜底：没有直链通过时调用方须明文报 blocked 原因，
        # 不许静默回落共享 SA 中继（drive_relay）或任何其他变体。
        "fallback_variant": None,
    }

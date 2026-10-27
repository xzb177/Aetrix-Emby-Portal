# -*- coding: utf-8 -*-
"""起播事件聚合读口的管理端接线（FMBY 学习批一 #2 闭环）。

现状（2026-10-27 亲验）：``play_events.aggregate_play_start_events``
只有定义与单元测试，生产代码零调用方——管理端页面挂靠一直是
“留后续”。本模块就是那个后续的调用方：管理端只读口直接调它，
聚合结果原样返回，不另发明字段、不改分组口径。

端点：
- GET /api/admin/playback/play-start-stats?hours=24：近 ``hours``
  小时起播事件按源（provider × mount_id）与错误类型汇总，
  回答“起播慢是慢在哪家源、失败是哪类错误”。

鉴权：与 playback_bus_admin 同口径——get_current_admin
（Bearer token + is_staff）。只读口，不写库、不进播放热路径：
低频管理面调用，不在 video_stream / PlaybackInfo 路径上。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.emby_server import play_events

# 管理端只读口的窗口上限：30 天。超过按 720 算、不报错、不拒绝，
# 避免一次超大窗口把聚合读口拖成全表扫描；下限钳制仍由聚合函数
# 内部负责（<1 按 1 算），此处不重复改下限、不改聚合函数语义。
MAX_WINDOW_HOURS = 720

router = APIRouter(prefix="/api/admin/playback",
                   tags=["管理后台·播放起播统计"])


@router.get("/play-start-stats")
def get_play_start_stats(hours: int = 24,
                         current_admin=Depends(get_current_admin),
                         db: Session = Depends(get_db)) -> dict:
    """起播事件聚合（FMBY #2）：直接调 play_events 的聚合读口。

    ``hours`` 上限在本只读口钳制：>720 按 720 算，不报错；
    下限仍由聚合函数内部钳制（<1 按 1 算）。聚合函数本身语义
    不动——其他直接调它的调用方不受本口上限影响。
    """
    return play_events.aggregate_play_start_events(
        db, hours=min(hours, MAX_WINDOW_HOURS))

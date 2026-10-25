# -*- coding: utf-8 -*-
"""播放实时总线管理端入口（FMBY #24 前半；阶段 8 PR-1 扩 pause / resume / seek 记账路径）。

注意：本模块的路由由 backend/main.py 管理路由区挂载（#24 接线 PR）。
总线仍默认关（PLAYBACK_BUS_ENABLED=0），关时发命令回 503、不执行、
不写命令行。

端点：
- POST /api/admin/playback-bus/commands：发一条命令（本阶段支持 stop /
  pause / resume / seek，其他命令回 400 并明示允许集，不静默忽略；
  总线关时回 503）。pause / resume / seek 只记账 + 发事件，终态
  outcome 为 recorded（播放器暂无接收通道，SendCommand 通道是后续
  PR 的活），绝不许读成播放器已照做。
- GET /api/admin/playback-bus/commands/{command_id}：查一条命令。
- GET /api/admin/playback-bus/commands：最近命令审计列表。
- GET /api/admin/playback-bus/snapshot：读最新快照（无快照回 404）。

鉴权：与 backend/api/playback_admin.py 同口径——get_current_admin
（Bearer JWT + is_staff）；写接口额外调 admin_roles.ensure_admin_allowed，
只读角色写命令得 403 且不写命令行（B3 验收 5 的“无行”选项）。
回执诚实口径：outcome=best_effort 时附 note 明说“会话已标记结束，
但已发出的直链地址在签名过期前仍可用”，不许显示“已停止”冒充已停；
outcome=recorded 时附 note 明说“只记账 + 发事件，播放器未接命令”。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import admin_roles
from backend.api.admin_core import get_current_admin
from backend.database import get_db
from backend.emby_server import playback_bus as bus

router = APIRouter(prefix="/api/admin/playback-bus",
                   tags=["管理后台·播放实时总线"])


class CommandIn(BaseModel):
    session_key: str
    command: str
    device_id: str | None = None
    payload: dict | None = None


def _command_response(command, note: str | None = None) -> dict:
    data = bus.command_to_dict(command)
    if note:
        data["note"] = note
    return data


@router.post("/commands")
def send_command(body: CommandIn, request: Request,
                 current_admin=Depends(get_current_admin),
                 db: Session = Depends(get_db)):
    """发一条播放控制命令。stop 同步执行到底；pause / resume / seek
    只记账 + 发事件（outcome=recorded），返回终态行，不留“发出去
    没回音”的悬空命令。"""
    admin_roles.ensure_admin_allowed(request, current_admin)
    if not bus.bus_enabled():
        raise HTTPException(
            status_code=503,
            detail="播放实时总线未开启（PLAYBACK_BUS_ENABLED=0），命令不执行",
        )
    if body.command not in bus.ALLOWED_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"本阶段支持的命令：{', '.join(bus.ALLOWED_COMMANDS)}；"
                   f"不支持：{body.command}",
        )
    try:
        command = bus.create_command(
            db, session_key=body.session_key, command=body.command,
            requested_by=current_admin.id, device_id=body.device_id,
            payload=body.payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    bus.mark_sent(db, command)
    bus.mark_delivered(db, command)
    if body.command == "stop":
        command = bus.execute_stop(db, command)
    else:
        command = bus.execute_state_command(db, command)
    note = None
    if command.outcome == bus.OUTCOME_BEST_EFFORT:
        note = ("会话已标记结束，但已发出的直链地址在签名过期前仍可用，"
                "本次停止为 best-effort，不是已确认停播。")
    elif command.outcome == bus.OUTCOME_ALREADY_ENDED:
        note = "目标会话执行前已结束，命令无事可做。"
    elif command.outcome == bus.OUTCOME_RECORDED:
        note = ("命令已记账并发布事件，但本次管理端命令未向播放器投递，"
                "未直接作用于播放器，不是已确认执行。")
    return _command_response(command, note)


@router.get("/commands/{command_id}")
def get_command(command_id: str,
                current_admin=Depends(get_current_admin),
                db: Session = Depends(get_db)):
    """查一条命令（审计读数）。"""
    command = bus.get_command(db, command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="命令不存在")
    return _command_response(command)


@router.get("/commands")
def list_commands(limit: int = 50,
                  current_admin=Depends(get_current_admin),
                  db: Session = Depends(get_db)):
    """最近命令审计列表（倒序，默认 50 条）。"""
    return [_command_response(c) for c in bus.list_commands(db, limit=limit)]


@router.get("/snapshot")
def get_snapshot(current_admin=Depends(get_current_admin)):
    """读最新快照（断线重连先拉快照再增量）。无快照回 404，不读脏数据。"""
    snapshot = bus.read_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="暂无播放快照")
    return snapshot

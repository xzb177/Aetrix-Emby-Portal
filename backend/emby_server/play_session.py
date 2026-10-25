"""一等播放会话（FMBY 播放全换阶段 3）。

现状问题（见迁移方案阶段 3 与深挖报告第 3 节）：
- PlaybackInfo 每次现生成随机 PlaySessionId，与上报侧 session_key 不是
  同一身份——会话在“已起播未上报”窗口内无目标可打，管理员无从踢人；
- 播放状态散在 5 处（DB 会话表、进程内流量计数、转码注册表、Redis
  租约、WebSocket 空壳），唯一跨进程的一份 DB 会话表又没有变体/线路/
  设备 id，生命周期只靠心跳过期推断。

本模块做的事（只加法，不改旧路径语义）：
- PlaybackInfo 时由服务端签发一等会话 id（随机、不可预测），同时写进
  session_key 与 server_session_id：客户端上报、停播、租约身份都按这
  个键走，旧路径不用改键；变体（PlaybackInfo 时的第一条 = 现行主线）、
  租约身份（ses:<server_id>）挂在会话行上。
- 旧会话（server_session_id 为空、客户端自带键）只读兼容：解析时先按
  server_session_id 查，查不到再按 session_key 查；停播两处都接受两种键。
- 生命周期仍以 ended_at 为唯一真相（不另立 status 列，避免双份状态）：
  创建（ended_at 为空）→ 活跃（上报刷新 last_update_at）→ 结束
  （ended_at 置值，停播路径 + janitor 回收不变）。
- 开关：PLAY_SESSION_FIRST_CLASS_ENABLED，默认关。关时 PlaybackInfo
  仍返回随机 token_hex(8)，与旧行为逐字一致；开时才预建会话行。
  开关在进程启动时读一次（与 PLAY_VARIANTS_ENABLED 同口径），改动需重启。

不做的事（后续阶段）：
- 不碰 play_sign 15 分钟口径、EA 申报口径、#1205 缓存键、#1207 租约、
  #1208 打点逻辑；能力判定仍只读阶段 2 的五能力位，不在这里再造一套。
- 不做 pause/seek/message 遥控（阶段 8），不做总线/快照（阶段 5）；
  302 变体停播仍是 best-effort，不装成功。
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from backend.emby_server import models as em

_ENABLED = os.getenv("PLAY_SESSION_FIRST_CLASS_ENABLED", "0") not in (
    "", "0", "false", "no", "off",
)


def first_class_enabled() -> bool:
    """一等会话总开关（env PLAY_SESSION_FIRST_CLASS_ENABLED，默认关）。"""
    return _ENABLED


def create_session(db: Session, user, item, *, variant=None,
                   device_id=None, remote_addr=None):
    """PlaybackInfo 时预建一等会话行。

    预建行 awaiting_first_report=True：首次上报仍须过 share_guard 判定
    （见 compat_routes._upsert_session），不得因“行已存在”跳过判定；
    判定通过后由上报路径清零。被判定拦下时上报路径把行置 ended，
    不留幻影活会话。
    """
    server_id = secrets.token_hex(16)
    row = em.PlaybackSession(
        session_key=server_id,
        server_session_id=server_id,
        user_id=user.id,
        item_id=item.id,
        variant=variant,
        device_id=(device_id or "")[:100] or None,
        lease_identity=f"ses:{server_id}",
        awaiting_first_report=True,
        remote_addr=remote_addr,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def resolve_session(db: Session, key: str):
    """按一等会话 id 查，查不到再按旧 session_key 查；两者都查不到回 None。

    停播路径（EA session_routes、管理端、用户端）共用本函数，两种键都接受。
    """
    if not key:
        return None
    row = (
        db.query(em.PlaybackSession)
        .filter(em.PlaybackSession.server_session_id == key)
        .first()
    )
    if row is not None:
        return row
    return (
        db.query(em.PlaybackSession)
        .filter(em.PlaybackSession.session_key == key)
        .first()
    )


def end_session(db: Session, session) -> bool:
    """结束会话：已结束返回 False，未结束置 ended_at 并提交返回 True。"""
    if session is None or session.ended_at is not None:
        return False
    session.ended_at = datetime.now()
    db.commit()
    return True

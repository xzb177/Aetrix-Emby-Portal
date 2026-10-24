# -*- coding: utf-8 -*-
"""播放实时总线（FMBY 播放全换阶段 5，#24 前半；阶段 8 PR-1 扩 pause / resume / seek）。

FMBY 形态（深挖报告第 4 节、B3 报告 #24 节）：
- 事实源是“快照 + 事件”两件套，不是 DB 会话表本身：快照给断线重连
  对齐用，事件给实时增量用。只做 pub/sub 不做快照不算达标——断线
  期间的消息丢了就靠快照补齐。
- 命令有生命周期：pending → sent → acked / failed / timeout，超时判
  失败不判成功；本阶段只实装 stop，收口现有三处 stop。

本模块的诚实边界（推断与实测分清）：
- 频道名用我方自定版本化名 ``aetrix:playback:realtime:v1``，不是
  FMBY 实测值。深挖报告第 4 节明写：本轮二进制串里没有直接抽到
  ``fmby:playback:realtime:v1`` 这个完整频道名字符串（只抽到 realtime
  相关模块与 /v1/realtime 路由），频道名只是引用前人报告、不作为
  本轮新证据；迁移方案第 7 节称昨日日志里有独立佐证，那仍不是
  二进制实测。因此不冒充人家的频道名，用自己的、写清楚。
- delivered 在本阶段的语义是“服务端执行器已受理”，不是“播放器已
  回执”：EA 兼容层仍诚实申报 SupportsRemoteControl=False，播放器
  没有接收命令的通道；把 delivered 写成播放器 ack 就是假回执。
- 302 / 直链变体的 stop 回执必须是 best_effort：会话行可以标记
  结束，但已经发出去的签名直链在过期前无法召回，不许显示“已停止”
  冒充已停。
- 阶段 8 PR-1（pause / resume / seek，记账路径）：中继 Range 流路径
  经核查不校验会话状态（streaming.py 的中继取流只认 drive_id /
  file_id + 盘凭据，无 session_key、无 ended_at 门控），因此新命令
  不许照抄 stop 的 enforced 语义——三者 outcome 一律 recorded：
  会话行已写 + progress 事件已发，播放器未接命令（SendCommand 通道
  是后续 PR 的活，本 PR 不做通道）。recorded 不是 best_effort：
  best_effort 是“直链已发出无法召回”，recorded 是“播放器没通道，
  根本没发过去”，两者绝不许混用。

开关：PLAYBACK_BUS_ENABLED，默认关（与 PLAY_VARIANTS_ENABLED、
PLAY_SESSION_FIRST_CLASS_ENABLED 同口径，进程启动时读一次，改动
需重启）。关时发布、快照、命令入口全部不执行，全站行为逐字不变。
接线（#24 接线 PR 已落地）：唯一发布点是 compat_routes._upsert_session
写库后发（started / progress / ended），起播打点处不发、不重复发；
管理路由挂载见 backend/main.py；快照工人由 backend/worker.py 在
开关开时启动。开关关时发布、快照、命令入口全部不执行。

发布失败永不拖播放：publish_event / run_snapshot_once 内部只记
warning、不抛异常（与 play_events 的永不抛、upstream_health 的
Redis 失败跳过同口径）。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from backend.database import Base
from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 开关与命名

_ENABLED = os.getenv("PLAYBACK_BUS_ENABLED", "0") not in (
    "", "0", "false", "no", "off",
)

# 我方自定频道名（见模块 docstring 的诚实边界），非 FMBY 实测值。
BUS_CHANNEL = "aetrix:playback:realtime:v1"
SNAPSHOT_KEY = "aetrix:playback:snapshot:v1"
SNAPSHOT_VERSION = 1

SNAPSHOT_INTERVAL_SEC = env_int("PLAYBACK_BUS_SNAPSHOT_INTERVAL_SEC", 15, lo=5, hi=300)
COMMAND_TIMEOUT_SEC = env_int("PLAYBACK_BUS_COMMAND_TIMEOUT_SEC", 10, lo=5, hi=120)
# 快照 TTL：写间隔的 4 倍（默认 60 秒），工人连续 4 拍没写上才过期，
# 避免单次工人抖动就让读端丢快照；过期快照按“无快照”处理，不读脏数据。
SNAPSHOT_TTL_SEC = max(60, SNAPSHOT_INTERVAL_SEC * 4)

EVENT_STARTED = "started"
EVENT_PROGRESS = "progress"
EVENT_ENDED = "ended"
EVENT_TYPES = (EVENT_STARTED, EVENT_PROGRESS, EVENT_ENDED)


def bus_enabled() -> bool:
    """总线总开关（env PLAYBACK_BUS_ENABLED，默认关）。"""
    return _ENABLED


# ---------------------------------------------------------------- 事件发布 / 订阅

def build_event(event_type: str, *, session_id: str, user_id=None,
                item_id=None, variant=None, device_id=None,
                position_ticks=None, is_paused=None) -> dict:
    """组装一条版本化总线事件（不含任何凭据与令牌原文）。"""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"未知的总线事件类型：{event_type}")
    event = {
        "v": 1,
        "type": event_type,
        "session_id": session_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if user_id is not None:
        event["user_id"] = user_id
    if item_id is not None:
        event["item_id"] = item_id
    if variant is not None:
        event["variant"] = variant
    if device_id is not None:
        event["device_id"] = device_id
    if position_ticks is not None:
        event["position_ticks"] = position_ticks
    if is_paused is not None:
        event["is_paused"] = is_paused
    return event


def _redis_client(client=None):
    """测试可注入替身；生产走懒加载的 get_redis（与 upstream_health 同口径）。"""
    if client is not None:
        return client
    try:
        from backend.database import get_redis
    except Exception:  # noqa: BLE001 — 无 Redis 环境也不许拖调用方
        return None
    try:
        return get_redis()
    except Exception:  # noqa: BLE001
        return None


def publish_event(event: dict, client=None) -> bool:
    """向总线频道发布一条事件。关开关 / 无 Redis 时静默不发回 False；
    发布失败只记 warning、不抛异常，绝不拖播放热路径。"""
    if not bus_enabled():
        return False
    redis = _redis_client(client)
    if redis is None:
        return False
    try:
        redis.publish(BUS_CHANNEL, json.dumps(event, ensure_ascii=False))
        return True
    except Exception:  # noqa: BLE001
        logger.warning("播放总线发布失败（事件丢弃，不影响播放）", exc_info=True)
        return False


def subscribe(client=None):
    """返回已订阅总线频道的 pubsub 对象（供门户 WebSocket 转发放）；
    无 Redis 时回 None。调用方负责按需取消息，本函数不阻塞。"""
    redis = _redis_client(client)
    if redis is None:
        return None
    try:
        pubsub = redis.pubsub()
        pubsub.subscribe(BUS_CHANNEL)
        return pubsub
    except Exception:  # noqa: BLE001
        logger.warning("播放总线订阅失败", exc_info=True)
        return None


# ---------------------------------------------------------------- 快照

def build_snapshot(db: Session) -> dict:
    """从 DB 会话表（唯一跨进程事实源）构建全量快照。

    只含进行中会话（ended_at 为空）；已结束会话不进快照，重启后
    不会出现“幽灵在播”。每行附带该会话最新命令的状态（没有则为
    None），供管理端一眼看到“这路有没有正在走的命令”。
    """
    rows = (
        db.query(em.PlaybackSession)
        .filter(em.PlaybackSession.ended_at.is_(None))
        .order_by(em.PlaybackSession.id)
        .all()
    )
    sessions = []
    for row in rows:
        last_cmd = (
            db.query(PlaybackControlCommand)
            .filter(
                (PlaybackControlCommand.session_key == row.session_key)
                | (PlaybackControlCommand.session_key == row.server_session_id)
            )
            .order_by(PlaybackControlCommand.id.desc())
            .first()
        )
        sessions.append({
            "session_id": row.server_session_id or row.session_key,
            "session_key": row.session_key,
            "user_id": row.user_id,
            "item_id": row.item_id,
            "variant": row.variant,
            "device_id": row.device_id,
            "position_ticks": row.position_ticks or 0,
            "is_paused": bool(row.is_paused),
            "last_update_at": row.last_update_at.isoformat() if row.last_update_at else None,
            "last_command_status": last_cmd.status if last_cmd is not None else None,
        })
    return {
        "v": SNAPSHOT_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
    }


def write_snapshot(client, snapshot: dict) -> bool:
    """把快照写进 Redis（带 TTL，过期按无快照处理）。失败回 False 不抛异常。"""
    redis = _redis_client(client)
    if redis is None:
        return False
    try:
        redis.set(SNAPSHOT_KEY, json.dumps(snapshot, ensure_ascii=False),
                  ex=SNAPSHOT_TTL_SEC)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("播放快照写入失败", exc_info=True)
        return False


def read_snapshot(client=None):
    """读快照：无记录 / JSON 损坏 / 版本不对一律回 None，不读脏数据。"""
    redis = _redis_client(client)
    if redis is None:
        return None
    try:
        raw = redis.get(SNAPSHOT_KEY)
    except Exception:  # noqa: BLE001
        logger.warning("播放快照读取失败，视为无快照", exc_info=True)
        return None
    if not raw:
        return None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        snapshot = json.loads(raw)
    except Exception:  # noqa: BLE001
        logger.warning("播放快照 JSON 损坏，视为无快照")
        return None
    if not isinstance(snapshot, dict) or snapshot.get("v") != SNAPSHOT_VERSION:
        return None
    return snapshot


def run_snapshot_once(db: Session, client=None) -> int:
    """构建并写入一次快照，返回会话数；失败回 -1 不抛异常。"""
    try:
        snapshot = build_snapshot(db)
    except Exception:  # noqa: BLE001
        logger.warning("播放快照构建失败", exc_info=True)
        return -1
    if not write_snapshot(client, snapshot):
        return -1
    return len(snapshot["sessions"])


def run_snapshot_loop(db_factory, interval_sec: int | None = None,
                      stop_event=None) -> None:
    """快照工人主循环（供 worker 接线，见报告）：每 interval 秒写一次
    全量快照。stop_event 置位即退出；单轮失败不杀工人。"""
    import time

    interval = interval_sec or SNAPSHOT_INTERVAL_SEC
    while stop_event is None or not stop_event.is_set():
        db = db_factory()
        try:
            run_snapshot_once(db)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)


# ---------------------------------------------------------------- 命令表与生命周期

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_REJECTED = "rejected"

# 合法流转：pending → sent → delivered → completed；
# pending / sent / delivered → failed / timeout。终态（completed /
# failed / timeout / rejected）不许再动，超时判失败不判成功。
_ALLOWED_TRANSITIONS = {
    STATUS_PENDING: {STATUS_SENT, STATUS_FAILED, STATUS_TIMEOUT},
    STATUS_SENT: {STATUS_DELIVERED, STATUS_FAILED, STATUS_TIMEOUT},
    STATUS_DELIVERED: {STATUS_COMPLETED, STATUS_FAILED, STATUS_TIMEOUT},
    STATUS_COMPLETED: set(),
    STATUS_FAILED: set(),
    STATUS_TIMEOUT: set(),
    STATUS_REJECTED: set(),
}

# 阶段 8 PR-1 起支持的命令：stop（原有）+ pause / resume / seek
# （记账路径）。volume / mute / message / next / previous 仍明确拒绝，
# 不静默忽略、不假装执行——无播放器通道时发出去没回音就是假遥控。
ALLOWED_COMMANDS = ("stop", "pause", "resume", "seek")

# stop 回执的诚实 outcome：
# - enforced：服务端经手字节（中继 / 挂载中继 / 本地文件）；
#   转码变体已随 #1228 删除，不再是 enforced 口径（未知变体恒 best_effort）；
#   会话结束即停播，可断言；
# - best_effort：302 / 直链变体，会话行已标记结束，但已发出的签名
#   直链在过期前无法召回，不许显示“已停止”冒充已停；
# - already_ended：目标会话执行前已结束，命令无事可做；
# - recorded（阶段 8 PR-1 新值）：pause / resume / seek 的唯一终态
#   outcome——会话行已写 + progress 事件已发，但播放器没有接收通道，
#   命令根本没发到播放器。绝不许复用 best_effort（其定义是“直链已
#   发出无法召回”，与“没通道没发过去”是两回事），绝不许写 completed
#   冒充播放器已照做。
OUTCOME_ENFORCED = "enforced"
OUTCOME_BEST_EFFORT = "best_effort"
OUTCOME_ALREADY_ENDED = "already_ended"
OUTCOME_RECORDED = "recorded"

# 服务端经手字节的变体（stop 可断言）；其余一律 best_effort，不多 claim。
_ENFORCED_VARIANTS = {"drive_relay", "mount_relay", "local_file"}


class UnsupportedCommandError(ValueError):
    """不支持的命令：调用方必须明确拒绝（HTTP 400），不许静默忽略。"""


class PlaybackControlCommand(Base):
    """播放控制命令（FMBY playback_control_commands 的我方形态）。

    建表口径：与 backend.database.Base 同一个元数据，init_db 的
    create_all 在应用构造（路由挂载）之后、lifespan 建表之前已把
    本模块导入，因此新表随既有表一起建出，无需手写 DDL；老库
    升级同样走 create_all 建新表（只建新表不改旧表，与既有新表
    同口径）。测试里用 Base.metadata.create_all 建表。
    """

    __tablename__ = "playback_control_commands"

    id = Column(Integer, primary_key=True, autoincrement=True)
    command_id = Column(String(64), unique=True, index=True)
    session_key = Column(String(64), index=True)
    device_id = Column(String(100))
    command = Column(String(30))
    # 阶段 8 PR-1：命令参数（JSON 文本）。seek 的目标 ticks、后续 volume
    # 的音量值、message 的文本都放这里；无参数的命令存 NULL。老库升级
    # 走 database._auto_migrate 补列（create_all 不改已建的表）。
    payload = Column(Text)
    status = Column(String(20), default=STATUS_PENDING)
    outcome = Column(String(20))
    requested_by = Column(Integer)
    requested_at = Column(DateTime, default=datetime.now)
    sent_at = Column(DateTime)
    delivered_at = Column(DateTime)
    completed_at = Column(DateTime)
    error = Column(Text)


def _validate_payload(command: str, payload) -> dict | None:
    """按命令名校验参数结构，非法回 ValueError（调用方转 400）。

    - stop / pause / resume：不接受参数，非空即非法；
    - seek：必须带 position_ticks（整数，bool 不算）；负数只 clamp 到 0，
      不编造上限（服务端时长来源未核，超片长由播放器侧 clamp）。
    返回归一化后的参数 dict，无参数回 None（存 NULL）。
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("命令参数必须是对象")
    if command in ("stop", "pause", "resume"):
        if payload:
            raise ValueError(f"{command} 不接受参数")
        return None
    if command == "seek":
        if "position_ticks" not in payload:
            raise ValueError("seek 必须带 position_ticks")
        ticks = payload["position_ticks"]
        if isinstance(ticks, bool) or not isinstance(ticks, int):
            raise ValueError("seek 的 position_ticks 必须是整数")
        return {"position_ticks": max(0, ticks)}
    raise ValueError(f"未知的命令参数校验：{command}")


def create_command(db: Session, *, session_key: str, command: str,
                   requested_by=None, device_id=None,
                   payload=None) -> PlaybackControlCommand:
    """建一条命令（pending）。不支持的命令抛 UnsupportedCommandError
    且不写行——拒绝必须明确，不能留一条假装执行过的记录。非法参数
    抛 ValueError 且不写行（调用方转 400）。"""
    if command not in ALLOWED_COMMANDS:
        raise UnsupportedCommandError(
            f"仅支持的命令：{', '.join(ALLOWED_COMMANDS)}；不支持：{command}")
    normalized = _validate_payload(command, payload)
    row = PlaybackControlCommand(
        command_id=secrets.token_hex(16),
        session_key=session_key,
        device_id=(device_id or "")[:100] or None,
        command=command,
        payload=(json.dumps(normalized, ensure_ascii=False)
                 if normalized is not None else None),
        status=STATUS_PENDING,
        requested_by=requested_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def transition(db: Session, command: PlaybackControlCommand,
               to_status: str) -> PlaybackControlCommand:
    """按合法流转推进命令状态，非法流转抛 ValueError。时间戳随状态走：
    sent→sent_at、delivered→delivered_at、completed→completed_at。"""
    allowed = _ALLOWED_TRANSITIONS.get(command.status, set())
    if to_status not in allowed:
        raise ValueError(
            f"命令状态非法流转：{command.status} → {to_status}")
    command.status = to_status
    now = datetime.now()
    if to_status == STATUS_SENT:
        command.sent_at = now
    elif to_status == STATUS_DELIVERED:
        command.delivered_at = now
    elif to_status == STATUS_COMPLETED:
        command.completed_at = now
    db.commit()
    db.refresh(command)
    return command


def mark_sent(db: Session, command: PlaybackControlCommand):
    """发出：命令已离开管理端（本阶段为同步执行，发出即受理）。"""
    return transition(db, command, STATUS_SENT)


def mark_delivered(db: Session, command: PlaybackControlCommand):
    """送达：服务端执行器已受理。注意：本阶段 delivered ≠ 播放器回执
    （播放器没有接收通道），见模块 docstring 的诚实边界。"""
    return transition(db, command, STATUS_DELIVERED)


def mark_completed(db: Session, command: PlaybackControlCommand,
                   outcome: str):
    """完成：执行结束，outcome 必须诚实（enforced / best_effort /
    already_ended / recorded），不许用 completed 冒充 best_effort 已停，
    更不许用 completed 冒充播放器已照做。"""
    command.outcome = outcome
    return transition(db, command, STATUS_COMPLETED)


def mark_failed(db: Session, command: PlaybackControlCommand, error: str):
    """失败：写明原因，不留无名失败。"""
    command.error = (error or "")[:500]
    return transition(db, command, STATUS_FAILED)


def timeout_stale_commands(db: Session, timeout_sec: int | None = None,
                           now: datetime | None = None) -> int:
    """把超时的未终态命令批量判 timeout，返回处理条数。"""
    timeout = timeout_sec if timeout_sec is not None else COMMAND_TIMEOUT_SEC
    cutoff = (now or datetime.now()) - timedelta(seconds=timeout)
    stale = (
        db.query(PlaybackControlCommand)
        .filter(
            PlaybackControlCommand.status.in_(
                (STATUS_PENDING, STATUS_SENT, STATUS_DELIVERED)),
            PlaybackControlCommand.requested_at < cutoff,
        )
        .all()
    )
    for row in stale:
        row.status = STATUS_TIMEOUT
        row.error = "命令执行超时"
    if stale:
        db.commit()
    return len(stale)


def get_command(db: Session, command_id: str):
    """按 command_id 查命令，查不到回 None。"""
    if not command_id:
        return None
    return (
        db.query(PlaybackControlCommand)
        .filter(PlaybackControlCommand.command_id == command_id)
        .first()
    )


def list_commands(db: Session, limit: int = 50):
    """最近的命令（倒序，默认 50 条，供管理端审计读）。"""
    return (
        db.query(PlaybackControlCommand)
        .order_by(PlaybackControlCommand.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )


def execute_stop(db: Session, command: PlaybackControlCommand,
                 publish_client=None) -> PlaybackControlCommand:
    """执行一条 stop 命令（调用前须已是 sent 或 delivered 状态）。

    服务端 stop 语义（收口现有三处 stop 的共识，不新发明）：
    会话行置 ended_at；已结束的回 already_ended；查不到会话的
    判 failed（session_not_found）；执行后尽力发布 ended 事件，
    发布失败不影响命令完成（事件丢了靠快照补齐，这是两件套的
    存在意义）。302 / 直链变体 outcome 恒为 best_effort。
    """
    if command.status not in (STATUS_SENT, STATUS_DELIVERED):
        raise ValueError(
            f"stop 只能在 sent / delivered 状态执行，当前：{command.status}")
    if command.command != "stop":
        raise UnsupportedCommandError(f"本阶段只执行 stop，不执行：{command.command}")
    from backend.emby_server import play_session as _ps

    session = _ps.resolve_session(db, command.session_key)
    if session is None:
        return mark_failed(db, command, "session_not_found")
    if session.ended_at is not None:
        return mark_completed(db, command, OUTCOME_ALREADY_ENDED)
    _ps.end_session(db, session)
    outcome = (OUTCOME_ENFORCED if (session.variant in _ENFORCED_VARIANTS)
               else OUTCOME_BEST_EFFORT)
    try:
        publish_event(build_event(
            EVENT_ENDED,
            session_id=session.server_session_id or session.session_key,
            user_id=session.user_id, item_id=session.item_id,
            variant=session.variant, device_id=session.device_id,
        ), client=publish_client)
    except Exception:  # noqa: BLE001 — 发布失败不影响命令完成
        logger.warning("stop 的 ended 事件发布失败（靠快照补齐）", exc_info=True)
    return mark_completed(db, command, outcome)


def execute_state_command(db: Session, command: PlaybackControlCommand,
                          publish_client=None) -> PlaybackControlCommand:
    """执行一条 pause / resume / seek 命令（调用前须已是 sent 或 delivered 状态）。

    与 stop 共用同一会话解析（play_session.resolve_session，先一等
    会话 id 后旧 session_key），不自创新定位语义。查不到会话判 failed
    （session_not_found）；已结束回 already_ended；否则只写会话行
    （pause 写 is_paused=True、resume 写 False、seek 写 position_ticks）
    + 尽力发 progress 事件，终态 outcome 一律 recorded——中继流路径
    不校验会话状态（见模块 docstring），新命令没有 enforced 的份，
    播放器通道（SendCommand）是后续 PR 的活。

    注意（PR-2 的活，本 PR 不修）：执行时不校验命令带的 device_id
    与会话 device_id 是否一致；无 PlaySessionId 时的回落键不含设备
    维度，两台设备播同一条目可能撞键打错设备——风险登记在 PR 描述里。
    """
    if command.status not in (STATUS_SENT, STATUS_DELIVERED):
        raise ValueError(
            f"状态命令只能在 sent / delivered 状态执行，当前：{command.status}")
    if command.command not in ("pause", "resume", "seek"):
        raise UnsupportedCommandError(
            f"本函数只执行 pause / resume / seek，不执行：{command.command}")
    from backend.emby_server import play_session as _ps

    session = _ps.resolve_session(db, command.session_key)
    if session is None:
        return mark_failed(db, command, "session_not_found")
    if session.ended_at is not None:
        return mark_completed(db, command, OUTCOME_ALREADY_ENDED)
    if command.command == "pause":
        session.is_paused = True
    elif command.command == "resume":
        session.is_paused = False
    else:  # seek：参数已在建命令时校验并 clamp，执行时只读存量值
        try:
            target = (json.loads(command.payload or "{}")
                      .get("position_ticks", 0))
        except Exception:  # noqa: BLE001 — 存量损坏按 0 计，不炸命令
            target = 0
        session.position_ticks = max(0, int(target))
    db.commit()
    try:
        publish_event(build_event(
            EVENT_PROGRESS,
            session_id=session.server_session_id or session.session_key,
            user_id=session.user_id, item_id=session.item_id,
            variant=session.variant, device_id=session.device_id,
            position_ticks=session.position_ticks,
            is_paused=session.is_paused,
        ), client=publish_client)
    except Exception:  # noqa: BLE001 — 发布失败不影响命令完成
        logger.warning("状态命令的 progress 事件发布失败（靠快照补齐）",
                       exc_info=True)
    return mark_completed(db, command, OUTCOME_RECORDED)


def command_to_dict(command: PlaybackControlCommand) -> dict:
    """命令行序列化（管理端读数用；不含任何凭据）。"""
    try:
        payload = json.loads(command.payload) if command.payload else None
    except Exception:  # noqa: BLE001 — 存量损坏按无参数读，不炸读数
        payload = None
    return {
        "command_id": command.command_id,
        "session_key": command.session_key,
        "device_id": command.device_id,
        "command": command.command,
        "payload": payload,
        "status": command.status,
        "outcome": command.outcome,
        "requested_by": command.requested_by,
        "requested_at": command.requested_at.isoformat() if command.requested_at else None,
        "sent_at": command.sent_at.isoformat() if command.sent_at else None,
        "delivered_at": command.delivered_at.isoformat() if command.delivered_at else None,
        "completed_at": command.completed_at.isoformat() if command.completed_at else None,
        "error": command.error,
    }

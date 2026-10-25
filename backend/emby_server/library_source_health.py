# -*- coding: utf-8 -*-
"""库源健康落库（FMBY 挂载学习第 5 步）。

FMBY 把“某条库源还活不活”做成落库的状态，而不是进程里的 dict：
``library_source_health``（``availability_state``、
``consecutive_unavailable_failures``、``last_failure_kind``、
``hidden_at``）。本模块落地其中库源级的一半——源健康状态机 +
挂载探针结论扇出 + 扫描过滤读口。

按 FMBY 实测口径做的部分（深挖报告第 4 节 + 研究第六阶段，
与上游源 upstream_health 同款状态机，落点是 PG/SQLite 表）：
- **状态值**：只存 ``"Active"`` / ``"Unavailable"`` 两种；查询语义是
  无记录默认当可用（查不到行即 Active），不是默认当不可用。
- **Unknown 是“探针结论”，不是“存的状态”**：新结论为 Unknown 时
  只推 ``last_checked_at``，状态、失败计数、隐藏时间、失败原因
  一律不动。
- **成功复位**：一次成功就把状态复位为 Active、连续失败计数清零、
  ``hidden_at`` 清空、``last_success_at`` 更新。
- **失败计数与隐藏**：失败累计 ``consecutive_unavailable_failures``；
  达到阈值才把状态翻为 Unavailable 并记 ``hidden_at``（首次翻转
  时间），之后继续失败只累计计数、不刷新隐藏时间。
- **阈值与开关**：全部走 env，不写死代码。``LIBRARY_SOURCE_HEALTH_ENABLED``
  默认 0（关时行为与今天逐字一致：不写表、不滤源）；
  ``LIBRARY_SOURCE_HEALTH_HIDE_AFTER`` 默认 3（挂载-8 暂定值，
  不是 FMBY 常量，用户一句话可推翻）。
- **复探**：不另设复探定时器——挂载体检每次运行都会把结论扇出到
  源行，成功即复位；复探节奏 = 现有挂载体检的运行节奏。

不做的部分（另行立项，不塞进本 PR）：
- 播放按健康表过滤：播放入口另行接线，本模块只提供
  ``is_source_available`` 读口，不碰播放链路。
- 探针失败定性：只写 ``probe_failed`` / ``auth_error`` 两个值，
  不编新档位（探针 dict 只有 ``ok`` / ``auth_error`` 两个信号）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

HEALTH_ENABLED_ENV = "LIBRARY_SOURCE_HEALTH_ENABLED"
HIDE_AFTER_ENV = "LIBRARY_SOURCE_HEALTH_HIDE_AFTER"

STATE_ACTIVE = "Active"
STATE_UNAVAILABLE = "Unavailable"

#: 探针失败定性：只写这两个值，不编新档位。
FAILURE_KIND_PROBE = "probe_failed"
FAILURE_KIND_AUTH = "auth_error"


def health_enabled() -> bool:
    """库源健康落库总闸（默认关）。"""
    return env_int(HEALTH_ENABLED_ENV, 0, lo=0, hi=1) == 1


def hide_after() -> int:
    """连续失败几次隐藏源（默认 3，挂载-8 暂定值）。"""
    return env_int(HIDE_AFTER_ENV, 3, lo=1)


def record_source_result(db: Session, library_source_id: int,
                         ok: Optional[bool],
                         failure_kind: Optional[str] = None, *,
                         now: Optional[datetime] = None,
                         threshold: Optional[int] = None):
    """记一条库源探针结论（调用方不必另行 commit）。

    - ok=True：成功复位（状态回 Active、计数清零、hidden_at 清空、
      last_success_at 更新；last_failure_kind 留作历史，不清除）。
    - ok=False：计数 +1，记 last_failure_kind；达阈值且当前非
      Unavailable 时翻为 Unavailable 并记 hidden_at（首次翻转时间）。
    - ok=None：Unknown 结论，只推 last_checked_at。
    """
    now = now or datetime.now()
    threshold = hide_after() if threshold is None else threshold
    row = db.query(em.LibrarySourceHealth).filter(
        em.LibrarySourceHealth.library_source_id == library_source_id).first()
    if row is None:
        row = em.LibrarySourceHealth(library_source_id=library_source_id)
        db.add(row)
    row.last_checked_at = now
    if ok is True:
        row.availability_state = STATE_ACTIVE
        row.consecutive_unavailable_failures = 0
        row.hidden_at = None
        row.last_success_at = now
    elif ok is False:
        row.consecutive_unavailable_failures = (row.consecutive_unavailable_failures or 0) + 1
        row.last_failure_kind = failure_kind or FAILURE_KIND_PROBE
        if (row.consecutive_unavailable_failures >= threshold
                and row.availability_state != STATE_UNAVAILABLE):
            row.availability_state = STATE_UNAVAILABLE
            row.hidden_at = now
    db.commit()
    return row


def is_source_available(db: Session, library_source_id: int) -> bool:
    """源是否可用：无记录默认可用；读表失败（老库无表）同样回 True
    + 警告，不挡扫描（显式回落，不是静默失败）。"""
    try:
        row = db.query(em.LibrarySourceHealth).filter(
            em.LibrarySourceHealth.library_source_id == library_source_id).first()
    except Exception as exc:  # noqa: BLE001 — 表缺失即默认可用
        logger.warning("库源健康表读取失败，默认当可用（library_source_id=%s）：%s",
                       library_source_id, exc)
        return True
    if row is None:
        return True
    return row.availability_state == STATE_ACTIVE


def sync_mount_probe_to_source_health(db: Session, mount_id: int,
                                      ok: Optional[bool],
                                      failure_kind: Optional[str] = None) -> int:
    """挂载探针结论扇出到该挂载的全部库源行。返回更新的源行数。

    只由 mounts_health 在开关开时调用；ok=None（未体检）不写。
    健康是源的事实：停用源（is_enabled=False）的行同样写，扫描侧
    本来就跳过停用源，写健康行不改变任何行为。
    """
    if ok is None:
        return 0
    rows = db.query(em.LibrarySource).filter(
        em.LibrarySource.mount_id == mount_id).all()
    for row in rows:
        record_source_result(db, row.id, ok, failure_kind)
    return len(rows)

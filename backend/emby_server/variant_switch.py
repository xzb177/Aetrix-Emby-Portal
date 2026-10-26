"""播放阶段 6：按可用性自动换线（FMBY 播放全换阶段 6，清单第 6 项）。

对应魂 6（状态集中维护）：阶段 4 把变体可用性落进了
``variant_availability`` 表，本阶段让默认起播选路读它——不是凭
感觉换线，也不是中继函数自己无限重试。

## 换线规则（只针对无变体请求的 Drive 默认路线）

- 首选：按可用性排序候选变体（无记录当可用），unavailable 的变体
  不做首选；同变体多挂载（``mount_relay:<id>``）按挂载键独立排序。
- 失败重试点：中继上游 502（远端不可用）时改走下一条可用变体
  （直链），上限一次；中继函数内部的 403 换 SA 重试仍在先，换线
  不替它做第二遍。
- 额度语义：凭据错（401/403/429/503，含取不到凭据）不记变体的账、
  不触发换线——换 SA 是另一机制，不在本阶段；目标没了（404）
  也不换线（同 file_id 的直链一样没）。
- 全部不可用：明确失败（409 + 变体名与原因），不静默播死线。
- 显式变体请求：不受换线影响（客户端选了就是选了，#11 PR-2 的
  404/409 纪律不变）；挂载/本机路线没有第二条默认变体，不在本
  阶段换线（byos115_direct 自动回落须另立项）。

## 开关与回滚

- 门控已删（2026-10-25，/watch 下掉、切新方案收尾）：换线恒生效，
  不再读 ``PLAYBACK_VARIANT_SWITCH_ENABLED``；回滚 = revert 本提交。
- 回退上限写死为 1（与迁移方案「回退上限一次」一致，不走 env）。
- 本模块不碰 PlaybackInfo 清单顺序：第一条恒为主线是阶段 1 的
  底线，清单重排会动老客户端行为；换线只发生在 video_stream
  的默认路线与失败重试点，不中途切已有流。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.emby_server import play_variants
from backend.emby_server import variant_availability as va

logger = logging.getLogger(__name__)

#: 回退上限：一次（迁移方案定死，不走 env，与现行中继重试一次同口径）。
MAX_FALLBACKS = 1

#: 唯一可换线的中继失败状态：上游 5xx 经中继口径转成的 502。
#: 403（轮换后仍失败）/ 503（取不到凭据）是凭据类，404 是目标没了，
#: 三者都不换线——见模块 docstring 的额度语义。
SWITCHABLE_RELAY_STATUS = 502


def is_switch_eligible_relay_status(status: int) -> bool:
    """中继失败状态是否可换线：只有 502（上游 5xx）可以。

    403 是轮换后仍失败的凭据错、503 是取不到凭据、404 是目标没了、
    429 是配额节流——四者都不换线（凭据/配额是另一机制的账）。
    """
    return status == SWITCHABLE_RELAY_STATUS


def split_available(db: Session, candidates) -> tuple[list, list]:
    """按可用性拆分候选变体（组内保持原偏好顺序）。

    ``candidates`` 是 ``[(变体名, 挂载 id 或 None)]``；挂载中继按
    ``mount_relay:<id>`` 分行查表，其余变体全局一行（与阶段 4 的
    ``variant_key_for`` 同口径）。无记录当可用——没探过的变体不
    能因为“没探过”就被摘掉。返回 ``(可用组, 不可用组)``。
    """
    available: list = []
    unavailable: list = []
    for name, mount_id in candidates:
        key = va.variant_key_for(name, mount_id)
        if va.is_variant_available(db, key):
            available.append((name, mount_id))
        else:
            unavailable.append((name, mount_id))
    return available, unavailable


def order_candidates(db: Session, candidates) -> list:
    """候选变体按可用性排序：可用在前、不可用在后，组内保序。

    换线决策只许经本函数读表，不许调用方各自再拼一遍可用性判断。
    """
    available, unavailable = split_available(db, candidates)
    return available + unavailable


def drive_route_choice(db: Session) -> tuple:
    """Drive 默认路线的换线决策（供 video_stream 的中继分支调用）。

    候选偏好顺序恒为 [drive_relay, drive_direct]（与今天默认路线
    的尝试顺序一致）。返回 ``(首选元组或 None, 可用组, 不可用组)``：
    首选 None = 两条都不可用，调用方须明确失败、不许试死线。
    """
    available, unavailable = split_available(db, [
        (play_variants.DRIVE_RELAY, None),
        (play_variants.DRIVE_DIRECT, None),
    ])
    first = available[0] if available else None
    return first, available, unavailable

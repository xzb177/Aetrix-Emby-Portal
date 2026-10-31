"""本地条目→上游条目持久映射层（#25 自动兜底的前置项）。

阶段 7（#1248）给了显式的上游播放兜底口，但故意没接 video_stream
自动兜底——因为没有本地条目到上游条目的持久映射：25-2 聚合
（``upstream_aggregation``）是纯模型，只比 ``aggregate_key``、不落库，
``select_for_presentation`` 的本地候选连本地条目 id 都不带，自动兜底
无从寻址。本层就是那个映射：未来同步工人在 25-2 聚合产出本地条目
与上游线路的配对时调 :func:`record_mapping` 落库；未来自动兜底口
凭本地条目 id 调 :func:`get_best_mapping` 取上游目标。

本层只做映射的读写，不做下列事（范围收住，不许扩）：
- 不动 video_stream 的选路、不接自动兜底——那是映射落地后的另一步；
- 不改 ``upstream_aggregation``（它是纯模型，不碰数据库）；
- 不联动清理死映射：源删了、本地条目删了，映射行留着供追溯，
  本层只读不删（删源是否级联由调用方设计定，见 O-04）；
- 不加外键（照 UpstreamSource 先例）。

读契约（2026-10-31 优化 O-10 收紧）：``get_best_mapping`` 默认过两道
校验——源必须存在且 enabled、本地条目必须是活行（未软删、未合并、
未隐藏，与找源适配器 / orchestrator.resolve_candidate 的既有口径
对齐）；不过的映射不算数，自动回落下一条有效映射，全无效返回
None。「读映射必须过这两道」是映射层默认行为，消费方不用再各自
防；只有明确要看原始行（如管理端追溯）的调用方才许显式
``validate=False`` 自担后果。

开关与回滚：
- ``UPSTREAM_ITEM_MAPPING_ENABLED``（默认关）只管写路径：关时
  :func:`record_mapping` 不写任何东西、返回 None，调用方行为逐字不变；
- 读路径不受开关影响，空映射返回 None、无害；
- 新表由 ``create_all`` 自动建，老库无需 ALTER（见 EnrichFlipEvent 先例）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

#: 映射写路径总开关 env 键（默认关）。
MAPPING_ENABLED_ENV = "UPSTREAM_ITEM_MAPPING_ENABLED"


def mapping_enabled() -> bool:
    """写路径总开关（env，默认关）。每次调用现读，供测试 monkeypatch。"""
    return (str(os.getenv(MAPPING_ENABLED_ENV, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def _clamp_confidence(value: float) -> float:
    """置信度钳制到 [0, 1]：非法输入按 0 计，绝不让脏值进库。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _as_pos_int(value: object, name: str) -> int:
    """正整数校验：非法或非正一律 ValueError，不静默截断。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须为正整数") from None
    if number <= 0:
        raise ValueError(f"{name} 必须为正整数")
    return number


def _as_nonneg_int(value: object, name: str) -> int:
    """非负整数校验（集号口径）：0 合法，负数 / 非法一律 ValueError。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须为非负整数") from None
    if number < 0:
        raise ValueError(f"{name} 必须为非负整数")
    return number


def record_mapping(db: Session, *, local_item_id: int, source_id: int,
                   upstream_item_id: str, confidence: float,
                   evidence: str = "",
                   episode_index: Optional[int] = None):
    """写一条映射（幂等 upsert）。

    - 开关关时不写任何东西，返回 None；
    - 同一（本地条目，源，上游条目）三元组重复写只更新置信度 / 证据 /
      更新时间，不建新行（唯一约束兜底）；
    - episode_index：集号，电影为 None；插入时写入，upsert 更新
      时仅当传入非 None 才更新它（None 不覆盖已有值）。口径按
      写入方分：Emby 聚合写入的是上游集号（IndexNumber），0 =
      第 0 集 / 特别篇、合法（与本地 MediaItem.episode_number
      同口径：无正数约束、扫描 S00E00 原样落 0）；苹果 CMS 同步
      写入的是组内 1 起序号（按构造恒 ≥ 1）。两口径共同的底线
      只有「不许负数」：非 None 且为负一律 ValueError，不静默
      入库。此前按一律正整数拒 0，上游一个 IndexNumber=0 的
      单集就把整轮聚合打死（2026-11-01 生产实证），已改非负；
    - 本函数只 flush 不 commit，调用方事务内执行（照
      ``upstream_sources.create_source`` 口径）；
    - 证据是短码（如 tmdb / imdb / title+year），只记“凭什么配上”，
      不记标题原文与任何凭据；超长截断到列宽。
    """
    if not mapping_enabled():
        return None
    item_id = (upstream_item_id or "").strip()
    if not item_id:
        raise ValueError("upstream_item_id 不能为空")
    local_id = _as_pos_int(local_item_id, "local_item_id")
    src_id = _as_pos_int(source_id, "source_id")
    if episode_index is not None:
        # 集号口径见 docstring：0 合法（第 0 集 / 特别篇），只拒
        # 负数。消费方（resolve_mapped_upstream_stream 等）对
        # Emby 型源不使用本值（上游条目 id 直寻址），对苹果 CMS
        # 型源映射由同步工人按 1 起写入、不会出现 0；其
        # ``episode_index or 1`` 兜底下 0 与 None 行为一致。
        episode_index = _as_nonneg_int(episode_index, "episode_index")
    row = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.local_item_id == local_id,
        em.UpstreamItemMapping.source_id == src_id,
        em.UpstreamItemMapping.upstream_item_id == item_id,
    ).first()
    now = datetime.now()
    if row is None:
        row = em.UpstreamItemMapping(
            local_item_id=local_id,
            source_id=src_id,
            upstream_item_id=item_id,
            episode_index=episode_index,
            confidence=_clamp_confidence(confidence),
            evidence=(evidence or "").strip()[:200],
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.confidence = _clamp_confidence(confidence)
        row.evidence = (evidence or "").strip()[:200]
        if episode_index is not None:
            row.episode_index = episode_index
        row.updated_at = now
    db.flush()
    return row


def _local_item_live(db: Session, local_id: int) -> bool:
    """本地条目是否活行：行在、未软删、未合并、未隐藏。

    与找源适配器 ``_resolve_items`` / ``orchestrator.resolve_candidate``
    的可见活行口径逐字对齐——同一条目在映射层与消费层不能有两种生死。
    """
    return (db.query(em.MediaItem.id)
            .filter(em.MediaItem.id == local_id,
                    em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None),
                    em.MediaItem.is_hidden == False)  # noqa: E712
            .first()) is not None


def get_best_mapping(db: Session, local_item_id: int, *,
                     validate: bool = True):
    """读本地条目对应的最佳上游映射。

    多条时取置信度最高；置信度相同时取最近更新（再相同时取 id 最大，
    保证同一输入同一输出）。空映射返回 None——读路径无害，不抛异常，
    也不受写路径开关影响。

    ``validate=True``（默认，O-10 读契约）：先验本地条目是活行
    （见 :func:`_local_item_live`），已死直接 None；再按偏好序逐条
    验源——源行不存在或未 enabled 的映射不算数，回落到下一条有效
    映射，全无效返回 None。``validate=False`` 跳过两道、返回原始
    最佳行，只许追溯类调用方显式使用。
    """
    local_id = _as_pos_int(local_item_id, "local_item_id")
    rows = (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.local_item_id == local_id)
            .order_by(em.UpstreamItemMapping.confidence.desc(),
                      em.UpstreamItemMapping.updated_at.desc(),
                      em.UpstreamItemMapping.id.desc())
            .all())
    if not rows:
        return None
    if not validate:
        return rows[0]
    if not _local_item_live(db, local_id):
        return None
    source_ids = {row.source_id for row in rows}
    enabled_ids = {
        source_id for (source_id,) in (
            db.query(em.UpstreamSource.id)
            .filter(em.UpstreamSource.id.in_(source_ids),
                    em.UpstreamSource.enabled.is_(True))
            .all())
    }
    for row in rows:
        if row.source_id in enabled_ids:
            return row
    return None

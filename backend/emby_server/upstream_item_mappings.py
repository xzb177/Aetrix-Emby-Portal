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
- 不校验源存在性：调用方（未来同步工人）持有合法源；读到死映射
  时未来兜底口按源不存在 / 条目不存在结构化处理，不在本层联动清理；
- 不加外键（照 UpstreamSource 先例）：源删了、本地条目删了，映射行
  留着供追溯，删源 / 删条目时是否联动清理由调用方设计定。

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


def record_mapping(db: Session, *, local_item_id: int, source_id: int,
                   upstream_item_id: str, confidence: float,
                   evidence: str = ""):
    """写一条映射（幂等 upsert）。

    - 开关关时不写任何东西，返回 None；
    - 同一（本地条目，源，上游条目）三元组重复写只更新置信度 / 证据 /
      更新时间，不建新行（唯一约束兜底）；
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
            confidence=_clamp_confidence(confidence),
            evidence=(evidence or "").strip()[:200],
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    else:
        row.confidence = _clamp_confidence(confidence)
        row.evidence = (evidence or "").strip()[:200]
        row.updated_at = now
    db.flush()
    return row


def get_best_mapping(db: Session, local_item_id: int):
    """读本地条目对应的最佳上游映射。

    多条时取置信度最高；置信度相同时取最近更新（再相同时取 id 最大，
    保证同一输入同一输出）。空映射返回 None——读路径无害，不抛异常，
    也不受写路径开关影响。
    """
    return (db.query(em.UpstreamItemMapping)
            .filter(em.UpstreamItemMapping.local_item_id
                    == _as_pos_int(local_item_id, "local_item_id"))
            .order_by(em.UpstreamItemMapping.confidence.desc(),
                      em.UpstreamItemMapping.updated_at.desc(),
                      em.UpstreamItemMapping.id.desc())
            .first())

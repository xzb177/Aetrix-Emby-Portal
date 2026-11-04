# -*- coding: utf-8 -*-
"""苹果 CMS 存量地区迁移（抓详情分流配套，2026-11-02）。

把总库（kind=applecms、没配地区桶的电影库）里存量托管行，按其
（源, vod）映射的 detail ``vod_area`` 判桶，迁进配了对应桶的地
区库：

- 判定走 ``applecms_library.area_bucket``——与同步工人同一处
  归一化，不许另写一份；detail 走
  ``applecms_library_sync.get_or_fetch_detail_harvest``——与工人
  同一缓存：干跑已经把 detail 打过一遍的，真跑时直接命中缓
  存，不重复打源站。
- 干跑（默认）只输出逐桶计数，一行都不动（缓存写入不算动行：
  那是抓取备忘，与工人共享）。
- 真跑只改 ``library_id`` 一列：guid、映射、托管生命周期全不
  动（托管行身份不挂在库上，见 upstream_provider 口径）；只迁
  判出地区桶（huayu/oumei/rihan）且已给出目标库的行，其他
  （other/unknown）与 detail 取不到（unresolved）的行留在总库，
  下次可再跑续迁。
- 上线时序（与工人同批）：先部署本批工人 → 再建地区库配桶 →
  最后跑本例程。反了会出事：工人没上线就迁行，下一轮总库同步
  会按 guid 锚定把行当「别库的」跳过、地区库工人又还没学会
  判桶，行就陈旧化了。

CLI 薄封装在 ``scripts/applecms_area_migration.py``。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from sqlalchemy.orm import Session

from backend.emby_server import applecms_library as acl
from backend.emby_server import applecms_library_sync as als
from backend.emby_server import models as em
from backend.emby_server import upstream_sources as us

logger = logging.getLogger(__name__)

_COMMIT_EVERY = 200

#: 存量扫描分页大小：按主键分块读，内存只驻留一页，规模再涨
#: 也不会像全量 ``.all()`` 一样把整库行一次载入被 OOM 杀掉。
_PAGE_SIZE = 500


def _validate(db: Session, from_library, targets: Dict[str, int]) -> None:
    if from_library is None:
        raise ValueError("来源库不存在")
    if not acl.is_applecms_library(from_library):
        raise ValueError("来源库不是苹果 CMS 库")
    if (getattr(from_library, "collection_type", "") or "") != "movies":
        raise ValueError("来源库不是电影库")
    if acl.parse_cms_area_bucket(from_library):
        raise ValueError("来源库已配地区桶：迁移只许从总库（无桶）往外迁")
    if not targets:
        raise ValueError("至少要给一个目标桶 → 目标库")
    for bucket, target_id in targets.items():
        if bucket not in acl.AREA_BUCKETS:
            raise ValueError(f"未知地区桶: {bucket!r}")
        target = db.get(em.Library, target_id)
        if target is None:
            raise ValueError(f"目标库不存在: {target_id}")
        if not acl.is_applecms_library(target):
            raise ValueError(f"目标库 {target_id} 不是苹果 CMS 库")
        if acl.parse_cms_area_bucket(target) != bucket:
            raise ValueError(
                f"目标库 {target_id} 的地区桶与 {bucket!r} 对不上")


def run_area_migration(db: Session, *, from_library_id: int,
                       targets: Dict[str, int], apply: bool = False,
                       http_get=None) -> dict:
    """存量迁移一轮。返回计数 dict（buckets/other/unknown/unresolved…）。

    ``targets`` 是桶键 → 目标库 id。``apply=False`` 为干跑：只
    计数（含把 detail 打进缓存），不改任何行。

    存量行按主键分块逐页读（``_PAGE_SIZE``），不全量载入：
    现规模（十万级）下整表 ``.all()`` 会被 OOM 杀掉，分块后
    内存只驻留一页，计数与迁移口径与全量版逐字一致。
    """
    from_library = db.get(em.Library, from_library_id)
    _validate(db, from_library, targets)
    result = {
        "from_library_id": from_library_id, "apply": bool(apply),
        "scanned": 0, "moved": 0,
        "buckets": {}, "other": 0, "unknown": 0, "unresolved": 0,
        "no_mapping": 0, "no_source": 0,
    }
    sources: Dict[int, object] = {}
    pending = 0
    # 按主键分块（keyset）逐页扫：分页键是 id 不是 library_id，
    # 真跑把行迁出源库也不会让后续页错位或重复扫到已处理行。
    last_id = 0
    while True:
        page = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == from_library_id,
                    em.MediaItem.item_type == "movie",
                    em.MediaItem.id > last_id)
            .order_by(em.MediaItem.id)
            .limit(_PAGE_SIZE)
            .all())
        if not page:
            break
        last_id = page[-1].id
        for row in page:
            result["scanned"] += 1
            mapping = (
                db.query(em.UpstreamItemMapping)
                .filter(em.UpstreamItemMapping.local_item_id == row.id,
                        em.UpstreamItemMapping.episode_index.is_(None))
                .order_by(em.UpstreamItemMapping.id)
                .first())
            if mapping is None:
                result["no_mapping"] += 1
                continue
            source_id = int(mapping.source_id)
            if source_id not in sources:
                sources[source_id] = us.get_source(db, source_id)
            source = sources[source_id]
            if source is None:
                result["no_source"] += 1
                continue
            harvest, _kind, _fetched = als.get_or_fetch_detail_harvest(
                db, source, str(mapping.upstream_item_id), http_get=http_get)
            if harvest is None:
                result["unresolved"] += 1
                continue
            bucket = acl.area_bucket(harvest.get("area"))
            if bucket in acl.AREA_BUCKETS:
                result["buckets"][bucket] = \
                    result["buckets"].get(bucket, 0) + 1
                if bucket not in targets:
                    # 判出了桶但没给目标库：如实计数，不许乱塞。
                    result.setdefault("no_target", {})
                    result["no_target"][bucket] = \
                        result["no_target"].get(bucket, 0) + 1
                    continue
                if apply:
                    row.library_id = targets[bucket]
                    result["moved"] += 1
                    pending += 1
                    if pending >= _COMMIT_EVERY:
                        db.commit()
                        pending = 0
            elif bucket == acl.AREA_OTHER:
                result["other"] += 1
            else:
                result["unknown"] += 1
        if len(page) < _PAGE_SIZE:
            break
    # 干跑同样提交：提交的只有 detail 缓存（抓取备忘，与工人
    # 共享），行上一列没动；真跑则把 library_id 变更一并落库。
    db.commit()
    logger.info("applecms 地区迁移%s: %s",
                "（真跑）" if apply else "（干跑）", result)
    return result

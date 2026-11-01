# -*- coding: utf-8 -*-
"""额度驾驶舱（只读管理端观测口，2026-10-27 首批第一单）。

只做三路只读聚合，不写库、不触发转存、不碰非转存目录、不列
用户盘里的任何文件：

1. SA 池：复用 ``drive_auth.sa_count()`` 的现有口径（observability
   的 ``_probe_sa_quota`` 同款：只数 SA 文件清单缓存，绝不为查
   配额去调 Drive API——探针高频调配额查询本身就是限流源）。
   因此本口只报 SA 数量，不报“每 SA 剩余配额”：Drive 没有不耗
   配额的逐 SA 配额查询口，硬查等于自造限流，该数不在本单范围。
   sa_count 抛错时整路降级为 unknown，不让驾驶舱崩掉。
2. 用户盘剩余空间：复用 #1368 的 ``about_storage`` 路径（Drive
   about.get，fields=storageQuota）。只查 active 的 drive 绑定行，
   每行一个 about 调用；任一行失败只降级该行（status=unknown，
   limit/usage/free 全 None），不影响其它行与整口。115 绑定无
   about 同款口，本单不报 115 剩余（写进未知项，不编数字）。
   响应里只出绑定 id / 用户 id / 展示名 / 配额三数，绝不出
   credential_id、凭据原文、密封值与文件列表。
3. 转存队列：只读 ``emby_transfer_jobs`` 的 status 计数
   （pending/running/done/failed）与最近 5 条 failed 行的原因
   （error_kind / error_detail 截断 200 字）。计数口径与工人
   状态机同一张表、同一批 status 字符串。

开关：``QUOTA_DASHBOARD_ENABLED`` 默认 0（关）。关时接口返回
404（不是空数据，避免“开关关着却看着像一切正常”的误读），
前端收到 404 即隐藏本区块。开关只在接口层门控，聚合函数本身
不读开关（测试可直接测聚合口径）。
"""

import logging
import os

from sqlalchemy import func, or_

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

MAX_USER_DISKS = 50
MAX_RECENT_FAILED = 5
_ERROR_DETAIL_LIMIT = 200


def quota_dashboard_enabled() -> bool:
    """开关默认关：只认 1/true/yes/on（大小写不敏感）。"""
    return os.getenv("QUOTA_DASHBOARD_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _sa_section() -> dict:
    try:
        from backend.emby_server import drive_auth
        count = drive_auth.sa_count()
        return {"count": int(count), "status": "ok",
                "note": "仅 SA 数量；逐 SA 剩余配额无不耗配额的查询口，本单不报"}
    except Exception as exc:  # noqa: BLE001 — 降级显示，不崩
        logger.warning("额度驾驶舱 SA 计数失败：%s", exc)
        return {"count": None, "status": "unknown",
                "note": "SA 数量取不到（降级显示）"}


def _queue_section(db) -> dict:
    counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    recent_failed = []
    # 统计口径（2026-11-01）：ops_cancelled（运维处置型取消）一律
    # 剔除——哪怕以 failed 形态出现也不计入 failed 计数与最近失败
    # 列表，防 2026-10-29 批量处置污染基线重演（类别唯一定义在
    # transfer_variant.OPS_CANCELLED_KIND）。
    from backend.emby_server import transfer_variant as _tv
    try:
        rows = (db.query(em.TransferJob.status, em.TransferJob.error_kind,
                         func.count())
                .group_by(em.TransferJob.status, em.TransferJob.error_kind)
                .all())
        for status, kind, n in rows:
            if status == "failed" and (kind or "") == _tv.OPS_CANCELLED_KIND:
                continue
            if status in counts:
                counts[status] += int(n)
    except Exception as exc:  # noqa: BLE001 — 降级显示，不崩
        logger.warning("额度驾驶舱转存队列计数失败：%s", exc)
        return {"counts": counts, "status": "unknown",
                "recent_failed": []}
    try:
        failed_rows = (db.query(em.TransferJob)
                       .filter(em.TransferJob.status == "failed",
                               or_(em.TransferJob.error_kind.is_(None),
                                   em.TransferJob.error_kind
                                   != _tv.OPS_CANCELLED_KIND))
                       .order_by(em.TransferJob.id.desc())
                       .limit(MAX_RECENT_FAILED).all())
        for job in failed_rows:
            detail = getattr(job, "error_detail", None) or ""
            recent_failed.append({
                "job_id": job.id,
                "item_id": job.item_id,
                "trigger": job.trigger,
                "error_kind": job.error_kind,
                "error_detail": detail[:_ERROR_DETAIL_LIMIT],
            })
    except Exception as exc:  # noqa: BLE001 — 失败原因拿不到不掩盖计数
        logger.warning("额度驾驶舱最近失败原因读取失败：%s", exc)
    return {"counts": counts, "status": "ok",
            "recent_failed": recent_failed}


def _default_drive_quota(db, binding):
    """默认用户盘配额探针：复用 #1368 的 about_storage 路径。

    构造与转存工人 Path A 同款：UserDriveApiClient(db, binding) +
    标记 ``myDrive:<user_id>``。只调 about_storage（只读配额），
    不列目录、不读文件、不建目录。
    """
    from backend.emby_server import transfer_variant as tv
    client = tv.UserDriveApiClient(db, binding)
    return client.about_storage(f"myDrive:{binding.user_id}")


def _user_disks_section(db, drive_quota_fn=None) -> dict:
    fn = drive_quota_fn or _default_drive_quota
    disks = []
    truncated = False
    try:
        bindings = (db.query(em.UserStorageBinding)
                    .filter(em.UserStorageBinding.provider == "drive",
                            em.UserStorageBinding.status == "active")
                    .order_by(em.UserStorageBinding.id.asc())
                    .limit(MAX_USER_DISKS + 1).all())
    except Exception as exc:  # noqa: BLE001 — 降级显示，不崩
        logger.warning("额度驾驶舱绑定列表读取失败：%s", exc)
        return {"items": [], "status": "unknown", "truncated": False}
    if len(bindings) > MAX_USER_DISKS:
        truncated = True
        bindings = bindings[:MAX_USER_DISKS]
    for binding in bindings:
        row = {"binding_id": binding.id, "user_id": binding.user_id,
               "account_label": binding.account_label,
               "limit": None, "usage": None, "free": None,
               "status": "unknown"}
        try:
            quota = fn(db, binding) or {}
            limit = quota.get("limit")
            usage = quota.get("usage")
            free = quota.get("free")
            row["limit"] = int(limit) if limit is not None else None
            row["usage"] = int(usage) if usage is not None else None
            row["free"] = int(free) if free is not None else None
            row["status"] = "ok"
        except Exception as exc:  # noqa: BLE001 — 单行降级，不影响其它行
            logger.warning("额度驾驶舱用户盘配额失败 binding=%s：%s",
                           binding.id, exc)
            row["status"] = "unknown"
        disks.append(row)
    return {"items": disks, "status": "ok", "truncated": truncated}


def build_quota_dashboard(db, drive_quota_fn=None) -> dict:
    """只读聚合：调用方（管理端接口）负责开关门控与鉴权。"""
    return {
        "sa_pool": _sa_section(),
        "user_disks": _user_disks_section(db, drive_quota_fn),
        "transfer_queue": _queue_section(db),
    }

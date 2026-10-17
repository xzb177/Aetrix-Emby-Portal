# -*- coding: utf-8 -*-
"""发现页「实时动态流」feed 的写/读共享能力。

事件落库到 ``panel_activity``（见 backend/models.py::PanelActivity），字段：
- ``event_type``：scan / enrich / play / system
- ``text``：展示文案（**绝不包含用户昵称/ID**，播放事件统一用「一位影迷」）
- ``library``：库名标签（系统事件可为 NULL）
- ``realm_id``：归属服（NULL = 全局事件，所有服可见）

保留策略（写时裁剪，无需定时任务）：
- 每个服分区（含 NULL 全局分区）最多保留最近 500 条，超出时删最旧的。

调用点（全部真实触发，无假数据）：
- 扫描完成 / 死链清理：``backend/emby_server/scanner.py::finish_scan``
- 刮削批次完成：``backend/emby_server/enrich_worker.py::_worker_loop``
- 播放开始/结束：``backend/emby_server/compat_routes.py::_upsert_session``
- 读：``backend/api/panel.py::panel_feed``（GET /api/panel/feed）
"""
import logging

logger = logging.getLogger(__name__)

# feed 事件类型（与 GET /api/panel/feed 返回的 type 字段取值一致）
FEED_TYPE_SCAN = "scan"
FEED_TYPE_ENRICH = "enrich"
FEED_TYPE_PLAY = "play"
FEED_TYPE_SYSTEM = "system"
FEED_EVENT_TYPES = (FEED_TYPE_SCAN, FEED_TYPE_ENRICH, FEED_TYPE_PLAY, FEED_TYPE_SYSTEM)

# 每服分区最多保留条数（NULL 服 = 全局事件，独立分区）
FEED_KEEP_PER_REALM = 500


def note_feed(db, text: str, event_type: str = FEED_TYPE_SYSTEM,
              library_id=None, realm_id=None, library_name: str | None = None) -> bool:
    """写入一条 feed 事件；失败只记日志、不影响主链路。

    library_id 传入时自动解析库名与归属服（library_name/realm_id 显式传入时优先）。
    返回 True=已写入。
    """
    try:
        from backend import models as _models
        from backend.emby_server import models as _em

        if event_type not in FEED_EVENT_TYPES:
            event_type = FEED_TYPE_SYSTEM
        if library_id is not None and (library_name is None or realm_id is None):
            try:
                lib = db.query(_em.Library).filter(
                    _em.Library.id == library_id).first()
                if lib is not None:
                    if library_name is None:
                        library_name = getattr(lib, "name", None)
                    if realm_id is None:
                        realm_id = getattr(lib, "realm_id", None)
            except Exception:  # noqa: BLE001 — 库解析失败不影响写入
                logger.warning("feed 库解析失败 library_id=%s", library_id,
                               exc_info=True)
        db.add(_models.PanelActivity(
            text=(text or "")[:300],
            event_type=event_type,
            library=(library_name or "")[:200] or None,
            realm_id=realm_id,
        ))
        db.commit()
        _trim_realm(db, realm_id)
        return True
    except Exception:  # noqa: BLE001 — 埋点绝不能炸主链路
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.exception("feed 事件写入失败")
        return False


def _trim_realm(db, realm_id) -> None:
    """按服分区裁剪：只留该分区最新 FEED_KEEP_PER_REALM 条。"""
    from backend import models as _models

    cond = (_models.PanelActivity.realm_id.is_(None)
            if realm_id is None
            else _models.PanelActivity.realm_id == realm_id)
    cutoff_id = (db.query(_models.PanelActivity.id)
                 .filter(cond)
                 .order_by(_models.PanelActivity.id.desc())
                 .offset(FEED_KEEP_PER_REALM).limit(1).scalar())
    if cutoff_id:
        db.query(_models.PanelActivity).filter(
            cond, _models.PanelActivity.id <= cutoff_id).delete(
            synchronize_session=False)
        db.commit()


def read_feed(db, realm_id, limit: int = 12) -> list:
    """按时间倒序读事件；realm 匹配或全局（NULL）事件可见。"""
    from backend import models as _models
    from sqlalchemy import or_ as _or

    limit = max(1, min(int(limit or 12), 50))
    rows = (db.query(_models.PanelActivity)
            .filter(_or(_models.PanelActivity.realm_id == realm_id,
                        _models.PanelActivity.realm_id.is_(None)))
            .order_by(_models.PanelActivity.id.desc())
            .limit(limit).all())
    items = []
    for r in rows:
        at = getattr(r, "created_at", None)
        items.append({
            "at": at.isoformat() if at else None,
            "type": (getattr(r, "event_type", None) or FEED_TYPE_SYSTEM),
            "text": r.text,
            "library": getattr(r, "library", None),
        })
    return items

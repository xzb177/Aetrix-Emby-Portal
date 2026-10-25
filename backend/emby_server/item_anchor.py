"""条目身份锚双读（清单第 8 项，挂载模型第 3 步）。

锚 = (``source_id``, ``provider_file_id``)：``source_id`` 指向
``library_sources`` 行（→ ``mount_id``），``provider_file_id`` 是供应商侧文件 id。
播放解析先按锚定位挂载，锚缺失 / 行悬空 / 挂载缺失 / 停用 / 与路径挂载
不一致时回落现行 ``mount://`` 解析——回落是双读的第二路，不是降级：
锚只确认挂载身份、跳过一次 ``load_mount`` 查询，绝不把播放重定向到路径
之外的挂载；``rel`` 仍取自 ``file_path``，纯锚解析（不靠路径）是后续批次的事。

回落率口径（断旧路的前置条件就看它）：
- 只统计 ``mount://`` 条目的播放解析，本机文件不在分母里；
- ``anchor`` = 锚定位成功且被采用；``fallback`` = 锚失败走路径解析。
- 计数在 Redis 哈希 ``aetrix:item_anchor:v1``（best-effort：Redis 不可用时
  不计数、绝不打断播放；Redis 清空则计数归零，属口径说明不是 bug）。
  查询：``redis-cli HGETALL aetrix:item_anchor:v1``，或调
  :func:`fallback_stats`（回落率 = fallback / (anchor + fallback)，分母为 0 时为 None）。
"""

import logging

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib

logger = logging.getLogger(__name__)

#: 回落率计数哈希（Redis）。字段：anchor / fallback / fallback:<原因>。
STATS_KEY = "aetrix:item_anchor:v1"

#: 锚失败原因（回落细分，绝不把异常当回落计——异常由路径解析原样处理）。
REASON_NO_ANCHOR = "no_anchor"            # source_id 缺失或 provider_file_id 为空
REASON_NO_SOURCE_ROW = "no_source_row"    # 源行不存在，或不属于该条目的库
REASON_NO_MOUNT = "no_mount"              # 源行没有挂载（本机行）
REASON_MOUNT_MISSING = "mount_missing"    # 挂载行不存在
REASON_MOUNT_DISABLED = "mount_disabled"  # 挂载已停用
REASON_MOUNT_MISMATCH = "mount_mismatch"  # 锚挂载与路径挂载不一致


def _redis_client():
    """best-effort 取 Redis 客户端：不可用返回 None，调用方不计数不报错。"""
    try:
        from backend.database import get_redis
        return get_redis()
    except Exception:  # noqa: BLE001 — 计数绝不打断播放
        return None


def _incr(field: str) -> None:
    client = _redis_client()
    if client is None:
        return
    try:
        client.hincrby(STATS_KEY, field, 1)
    except Exception:  # noqa: BLE001 — 计数失败静默，播放不受影响
        logger.debug("身份锚计数写入失败", exc_info=True)


def note_anchor_used() -> None:
    """锚定位成功且被采用，记一次。"""
    _incr("anchor")


def note_fallback(reason: str) -> None:
    """锚失败走路径解析，记一次回落（含原因细分）。"""
    _incr("fallback")
    _incr(f"fallback:{reason}")


def fallback_stats() -> dict:
    """读回落率口径：计数 + 回落率 + 原因细分。Redis 不可用时全 0、回落率为 None。"""
    out = {"anchor": 0, "fallback": 0, "fallback_rate": None, "by_reason": {}}
    client = _redis_client()
    if client is None:
        return out
    try:
        raw = client.hgetall(STATS_KEY) or {}
    except Exception:  # noqa: BLE001
        return out
    for key, value in raw.items():
        if isinstance(key, bytes):
            key = key.decode("utf-8", errors="replace")
        try:
            num = int(value)
        except (TypeError, ValueError):
            continue
        if key == "anchor":
            out["anchor"] = num
        elif key == "fallback":
            out["fallback"] = num
        elif key.startswith("fallback:"):
            out["by_reason"][key[len("fallback:"):]] = num
    total = out["anchor"] + out["fallback"]
    out["fallback_rate"] = (out["fallback"] / total) if total else None
    return out


def resolve_anchor(db: Session, item) -> tuple:
    """按身份锚定位挂载。

    返回 ``(mount_or_None, reason)``：成功时 reason 为 ``"ok"``；
    失败时 mount 为 None、reason 为上述 REASON_* 之一，调用方回落路径解析。
    本函数不做任何网络 IO：只查源行与挂载行（主键查询），不验证文件存在性。
    """
    source_id = getattr(item, "source_id", None)
    fid = (getattr(item, "provider_file_id", "") or "").strip()
    if source_id is None or not fid:
        return None, REASON_NO_ANCHOR
    row = db.query(em.LibrarySource).filter(
        em.LibrarySource.id == source_id).first()
    if row is None:
        return None, REASON_NO_SOURCE_ROW
    item_library_id = getattr(item, "library_id", None)
    if (item_library_id is not None and getattr(row, "library_id", None) is not None
            and row.library_id != item_library_id):
        return None, REASON_NO_SOURCE_ROW
    if row.mount_id is None:
        return None, REASON_NO_MOUNT
    mount = db.query(em.StorageMount).filter(
        em.StorageMount.id == row.mount_id).first()
    if mount is None:
        return None, REASON_MOUNT_MISSING
    if not getattr(mount, "is_enabled", False):
        return None, REASON_MOUNT_DISABLED
    parsed = mount_lib.parse_mount_path(getattr(item, "file_path", "") or "")
    if parsed is None or parsed[0] != mount.id:
        return None, REASON_MOUNT_MISMATCH
    return mount, "ok"

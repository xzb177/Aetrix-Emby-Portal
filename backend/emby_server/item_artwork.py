"""条目图片人工覆盖（媒体库学习 L2）。

优先级：覆盖 > 旁挂（本地图）> 刮削（远程图）。覆盖只在
ITEM_ARTWORK_OVERRIDE_ENABLED=1 时生效；关时 _image_chain 与
season_poster_url 的行为逐字不变（覆盖行即使存在也不读）。

本批覆盖只存 http(s) URL：图片路由拿到远程 URL 后走与刮削图同一
条代理/本地化路径，不新增本地文件写路径；本地直传图不在本批。
"""

from __future__ import annotations

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

OVERRIDE_ENABLED = env_int("ITEM_ARTWORK_OVERRIDE_ENABLED", 0, 0, 1)

KINDS = ("Primary", "Backdrop")


def override_enabled() -> bool:
    """条目图人工覆盖是否开启（测试 patch 本模块常量）。"""
    return OVERRIDE_ENABLED > 0


def _valid_url(url: str) -> bool:
    """本批只接受 http(s) URL（防 SSRF 式本地路径与 javascript: 注入）。"""
    url = (url or "").strip()
    return url.startswith("http://") or url.startswith("https://")


def get_override(db, item_id: int, kind: str):
    """取覆盖行；开关关或 kind 非法时一律 None（读侧不区分原因）。"""
    if not override_enabled() or kind not in KINDS:
        return None
    return (
        db.query(em.MediaItemArtworkOverride)
        .filter(
            em.MediaItemArtworkOverride.item_id == item_id,
            em.MediaItemArtworkOverride.kind == kind,
        )
        .first()
    )


def override_url(db, item_id: int, kind: str) -> str | None:
    """覆盖 URL（无覆盖 / 开关关 / kind 非法 → None）。"""
    row = get_override(db, item_id, kind)
    return row.image_url if row is not None else None


def set_override(db, item_id: int, kind: str, image_url: str, created_by=None):
    """新增或更新覆盖（幂等 upsert）。调用方须先确认条目存在。

    写侧不受开关影响（见 cancel_override 的说明）。

    Raises:
        ValueError: kind 非法或 URL 不是 http(s)。
    """
    if kind not in KINDS:
        raise ValueError(f"未知图类：{kind!r}（只接受 Primary / Backdrop）")
    if not _valid_url(image_url):
        raise ValueError("覆盖图只接受 http(s) URL")
    row = (
        db.query(em.MediaItemArtworkOverride)
        .filter(
            em.MediaItemArtworkOverride.item_id == item_id,
            em.MediaItemArtworkOverride.kind == kind,
        )
        .first()
    )
    if row is None:
        row = em.MediaItemArtworkOverride(
            item_id=item_id, kind=kind, image_url=image_url.strip(),
            created_by=created_by,
        )
        db.add(row)
    else:
        row.image_url = image_url.strip()
        row.created_by = created_by
    db.commit()
    return row


def cancel_override(db, item_id: int, kind: str) -> bool:
    """取消覆盖（行不存在也返回 False，不报错）。

    写侧（set / cancel）不受开关影响：开关只管读侧是否生效，
    管理员可以先配好覆盖再开开关。
    """
    row = (
        db.query(em.MediaItemArtworkOverride)
        .filter(
            em.MediaItemArtworkOverride.item_id == item_id,
            em.MediaItemArtworkOverride.kind == kind,
        )
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True

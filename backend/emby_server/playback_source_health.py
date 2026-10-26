# -*- coding: utf-8 -*-
"""播放按库源健康表过滤（FMBY 挂载学习第 5 步，播放侧）。

#1283 只落了健康表 + 扫描过滤，本模块补播放侧（扫描过滤若已
存在只复用、不重建；扫描侧本身不在本模块改动范围内）：

- **无记录默认可用**：条目映射不到健康行（无源行、无健康行、
  读表失败）一律当可用，不挡播放——健康过滤只挡“已确认连续
  失败达阈值”的源，不挡未知。
- **连续失败达阈值才隐藏**：判定直接复用
  ``library_source_health.is_source_available``（阈值走
  ``LIBRARY_SOURCE_HEALTH_HIDE_AFTER``，默认 3），本模块不另设阈值。
- **成功复位**：复位由探针扇出（``record_source_result``）做，
  本模块只读不写健康表。
- **试点范围**：复用 ① 的试点机制——设了试点库清单时，只对
  清单里的库做播放过滤；清单为空 = 对全部库过滤（与扫描侧一致）。
- **过滤位置**：候选/变体层——调用方（api.py）在 PlaybackInfo
  下发变体清单与 video_stream 解析变体时问本模块；video_stream
  另在取流前加一道 503 门（无变体老客户端也挡得住，见调用方注释）。

条目 → 源的映射（只认挂载路径条目）：
- ``file_path`` 为 ``mount://<id>/<相对路径>`` 时，按（库 id，
  挂载 id）找源行，再按子目录前缀取最长匹配行（子目录行优先
  于根行）；找不到前缀匹配行时默认可用，不猜。
- 非挂载路径条目（本机文件、Drive 直链条目等）目前映射不到
  源行，一律默认可用——这是已知缺口（见模块末尾），不是静默
  改走别源：Drive 条目走身份锚（源 id + 文件 id）映射是第 3 步
  的后续工作，本模块不为它编一套临时映射。

本模块不碰路径授权：``path_auth.py``、``playback_admin.py`` 及
api.py 里已有的路径授权接线行一律不动，健康过滤只在旁边新增
调用；不碰 #1290 草稿的 JWT 腿与 switch 门控行，不重构
video_stream。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.emby_server import library_source_health as lsh
from backend.emby_server import library_source_health_pilot as lsh_pilot
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib


def _matching_source_row(db: Session, item):
    """条目对应的库源行（挂载路径条目才有；否则回 None = 默认可用）。"""
    parsed = mount_lib.parse_mount_path(getattr(item, "file_path", "") or "")
    if parsed is None:
        return None
    mount_id, rel = parsed
    library_id = getattr(item, "library_id", None)
    if library_id is None:
        return None
    try:
        rows = db.query(em.LibrarySource).filter(
            em.LibrarySource.library_id == library_id,
            em.LibrarySource.mount_id == mount_id).all()
    except Exception:  # noqa: BLE001 — 表缺失即默认可用，不挡播放
        return None
    if not rows:
        return None
    # 最长子目录前缀匹配：子目录行优先于根行；无前缀匹配不猜。
    best = None
    best_len = -1
    for row in rows:
        sub = (row.sub_path or "/").rstrip("/") or "/"
        if sub == "/":
            matched, length = True, 0
        elif rel == sub or rel.startswith(sub + "/"):
            matched, length = True, len(sub)
        else:
            matched, length = False, -1
        if matched and length > best_len:
            best, best_len = row, length
    return best


def item_source_available(db: Session, item) -> bool:
    """该条目的源是否可播：健康总闸关 / 试点范围外 / 映射不到源行 /
    无健康记录时一律回 True；只在映射到的源行已隐藏时回 False。"""
    if not lsh.health_enabled():
        return True
    if not lsh_pilot.in_pilot_scope(getattr(item, "library_id", None)):
        return True
    row = _matching_source_row(db, item)
    if row is None:
        return True
    return lsh.is_source_available(db, row.id)


def filter_variants(variants, available: bool):
    """变体层过滤：源可用时原样返回，源已隐藏时返回空清单。

    调用方约定：PlaybackInfo 下发时，空清单 = 该条目无可播变体
    （不静默回落单源）；video_stream 解析时，空清单使已选变体
    按 409 处理（``resolve_requested_variant`` 现成语义）。
    """
    if available:
        return list(variants)
    return []

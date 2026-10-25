"""库级扫描预设策略（FMBY 媒体库学习 L10：只学机制，不抄预设名）。

魂：库的结构解析口径由预设 + 过滤声明，不是由扫描代码分支硬编码。

preset 取值集合的派生（PR 描述里写明，绝不照搬外部预设名、绝不
hardcode 业务片单）：直接取 Aetrix 现有 ``Library.collection_type``
的三个取值——movies（电影库：文件当电影解析，集号模式关闭）、
tvshows（剧集库：集号模式开启，文件先按季/集解析）、mixed（混合库：
现行启发式——解析出季/集则当集，否则当电影）。preset 行只是在某个库
上**覆盖**该库 collection_type 的解析口径；不改库本身的
collection_type，不改任何业务片单（本模块没有任何片名/剧名常量）。

过滤语义（只收窄、不放宽）：
- extensions：逗号分隔的扩展名白名单；空 = 不限。落到快照时与
  扫描器的 VIDEO_EXTS 取交集——策略永远不能把 .txt 之类非视频
  文件放进扫描。
- path_filters：JSON 数组（或逗号分隔）的子串包含过滤；空 = 不限。
  非空时只有路径命中任一子串的文件才纳入。

总开关：env ``LIBRARY_SCAN_PRESETS_ENABLED``（默认 0=关），模块加载
时读一次。开关关、或该库无策略行、或策略 preset 非法时，一律走
现行口径（快照原样返回），零行为变化。策略读失败（老库无表等）
同样原样返回、只记 warning，绝不阻断扫描。管理后台不另立开关。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 库级扫描预设总开关（默认关）。启动时读一次，热路径不碰 env。
LIBRARY_SCAN_PRESETS_ENABLED = env_int("LIBRARY_SCAN_PRESETS_ENABLED", 0, 0, 1)

#: preset 取值 = Library.collection_type 取值（见模块头派生说明）。
PRESETS = tuple(em.LIBRARY_SCAN_PRESETS)


def presets_enabled() -> bool:
    """扫描预设是否启用（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return LIBRARY_SCAN_PRESETS_ENABLED > 0


def parse_extensions(raw) -> tuple:
    """逗号分隔的扩展名 → 归一化元组（小写、补前导点、去空去重保序）。"""
    out = []
    for part in (raw or "").split(","):
        ext = part.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if ext not in out:
            out.append(ext)
    return tuple(out)


def parse_path_filters(raw) -> tuple:
    """path_filters 文本 → 子串元组：优先按 JSON 数组解析，失败则按逗号切分。"""
    if not raw:
        return ()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return tuple(str(x).strip() for x in data if str(x).strip())
    except (TypeError, ValueError):
        pass
    return tuple(p.strip() for p in str(raw).split(",") if p.strip())


def policy_for_library(db, library_id: int):
    """取某库的策略行：开关关 / 无行 / preset 非法时返回 None（走现行口径）。

    preset 非法时记 warning 并返回 None——脏数据绝不让扫描改口径。
    """
    if not presets_enabled():
        return None
    try:
        row = (db.query(em.LibraryScanPreset)
               .filter(em.LibraryScanPreset.library_id == library_id).first())
    except Exception:  # noqa: BLE001 — 老库无表等读失败 = 无策略，走现行口径
        logger.warning("扫描预设读取失败（库 %s），走现行口径", library_id,
                       exc_info=True)
        return None
    if row is None or row.preset not in PRESETS:
        if row is not None:
            logger.warning("库 %s 的扫描预设 %r 非法，走现行口径",
                           library_id, row.preset)
        return None
    return row


def attach_policy_to_snapshot(db, snap, allowed_extensions=None):
    """把策略挂到扫描快照上（scan_library_sync 入口调用）：

    - 无策略 → 原样返回快照（零行为变化）；
    - 有策略 → dataclasses.replace 出新快照：policy_parse_type = preset，
      policy_extensions = 策略白名单 ∩ allowed_extensions（None 时
      不限交集），policy_path_filters = 子串元组。
    任何异常都原样返回快照、只记 warning（策略绝不阻断扫描）。
    """
    try:
        row = policy_for_library(db, snap.library_id)
        if row is None:
            return snap
        exts = parse_extensions(row.extensions)
        if allowed_extensions is not None and exts:
            allowed = {e.lower() for e in allowed_extensions}
            exts = tuple(e for e in exts if e in allowed)
        return dataclasses.replace(
            snap, policy_parse_type=row.preset,
            policy_extensions=exts or None,
            policy_path_filters=parse_path_filters(row.path_filters))
    except Exception:  # noqa: BLE001 — 策略挂载失败 = 走现行口径
        logger.warning("扫描预设挂载失败（库 %s），走现行口径",
                       getattr(snap, "library_id", None), exc_info=True)
        return snap


def snapshot_policy_allows(snap, stored_path: str) -> bool:
    """快照策略下的文件准入判定（_prepare_and_prefetch 逐文件调用）：

    - 无策略字段（开关关 / 未配策略）→ True；
    - extensions 非空 → 文件扩展名必须在白名单内；
    - path_filters 非空 → 路径必须命中任一子串。
    """
    exts = getattr(snap, "policy_extensions", None)
    if exts:
        if os.path.splitext(stored_path or "")[1].lower() not in exts:
            return False
    filters = getattr(snap, "policy_path_filters", ()) or ()
    if filters:
        path = stored_path or ""
        if not any(f in path for f in filters):
            return False
    return True


def effective_parse_type(snap, collection_type: str) -> str:
    """本轮解析口径：策略 preset 覆盖库 collection_type，无策略时原样。"""
    return getattr(snap, "policy_parse_type", None) or collection_type


def upsert_policy(db, library_id: int, preset: str, extensions: str = "",
                  path_filters: str = ""):
    """管理端策略配置（复用现有管理鉴权，见 admin 端口）：一库一行，upsert。

    preset 非法抛 ValueError；extensions/path_filters 按解析函数归一化
    后落库（extensions 存逗号串、path_filters 存 JSON 数组串）。
    """
    if preset not in PRESETS:
        raise ValueError(f"未知扫描预设：{preset!r}（只许 {list(PRESETS)}）")
    row = (db.query(em.LibraryScanPreset)
           .filter(em.LibraryScanPreset.library_id == library_id).first())
    ext_text = ",".join(parse_extensions(extensions))
    filter_text = json.dumps(list(parse_path_filters(path_filters)),
                             ensure_ascii=False)
    if row is None:
        row = em.LibraryScanPreset(library_id=library_id, preset=preset,
                                   extensions=ext_text, path_filters=filter_text)
        db.add(row)
    else:
        row.preset = preset
        row.extensions = ext_text
        row.path_filters = filter_text
    db.flush()
    return row

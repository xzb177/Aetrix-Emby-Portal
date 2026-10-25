"""媒体库封面链（媒体库学习 L1）。

库封面解析顺序做成可配策略（LIBRARY_COVER_STRATEGY_ORDER，默认
"borrow,render,svg"，每次调用现读 env，D2 三选一定了只调顺序不返工）：

- borrow：借库内已绑定代表条目的海报。代表 = 案 A 判定（tmdb_id /
  imdb_id 任一非空，复用 identity_gate._own_bound 的事实判定，**不查
  IDENTITY_SHELF_GATE 开关**——借图不是货架准入）下，库内同身份键
  （tmdb:<id> 优先，次 imdb:<id>）的最低 id 行；多代表时取 id 最大
  （最新入库近似，id 自增）且有海报的那一个。代表海报 URL 口径与
  条目图链一致：本地缓存图走 /emby/Items/{guid}/Images/Primary，
  远程图直接给 URL。
- render：库已有的自研渲染封面（cover_template 已设置且 cover_path
  已生成时，复用现有封面口径：客户端经 /emby/Items/{lib.guid}/
  Images/Primary 拉图）。
- uploaded：管理员直传的 cover_path（cover_template 为空时；与 render
  同一出图口，策略上单列一步是为了 D2 调序时能把「直传」与「自研
  渲染」分开排）。
- svg：SVG 占位（静态字节，不含库名，出图路由见 media_library）。
- none：策略跑空（占位恒有，实际到不了；保留作策略穷尽）。

开关 LIBRARY_COVER_BORROW_ENABLED（默认 0）：关时五件套只回旧行为
（cover_path 有值 → render/uploaded，否则 null），不借代表、不出
占位策略——旧前端与旧客户端看到的与今天逐字一致。

策略顺序的来源（管理端「图片与顺序」页可改，不新增 env 键）：
env LIBRARY_COVER_STRATEGY_ORDER（文件配置，优先）→ 管理端保存的
system_configs[library_cover_strategy_order] → 默认 borrow,render,svg。
语义与默认值逐字不变：未知步骤丢弃、重复去重、跑空回默认。
"""

from __future__ import annotations

import json
import logging
import os

from backend.emby_server import image_store
from backend.emby_server import models as em
from backend.emby_server.env_util import env_int
from backend.emby_server.identity_gate import _own_bound
from backend.models import SystemConfig

logger = logging.getLogger(__name__)

BORROW_ENABLED = env_int("LIBRARY_COVER_BORROW_ENABLED", 0, 0, 1)

_DEFAULT_STRATEGY = "borrow,render,svg"
_KNOWN_STEPS = ("borrow", "render", "uploaded", "svg")
COVER_STRATEGY_KEY = "library_cover_strategy_order"


def borrow_enabled() -> bool:
    """库封面借代表是否开启（测试 patch 本模块常量）。"""
    return BORROW_ENABLED > 0


def _parse_strategy(raw: str | None) -> list[str]:
    """策略文本 → 步骤表：未知步骤丢弃、重复去重、跑空回默认。"""
    steps: list[str] = []
    for token in (raw or _DEFAULT_STRATEGY).split(","):
        step = token.strip()
        if step in _KNOWN_STEPS and step not in steps:
            steps.append(step)
    return steps or list(_DEFAULT_STRATEGY.split(","))


def strategy_order() -> list[str]:
    """解析策略顺序：env 现读，未知步骤丢弃、重复去重、跑空回默认。"""
    raw = (os.environ.get("LIBRARY_COVER_STRATEGY_ORDER") or "").strip()
    return _parse_strategy(raw if raw else None)


def env_strategy_order() -> list[str] | None:
    """env 里显式设置的顺序；未设置 → None（不代表默认）。"""
    raw = (os.environ.get("LIBRARY_COVER_STRATEGY_ORDER") or "").strip()
    return _parse_strategy(raw) if raw else None


def stored_strategy_order(db) -> list[str] | None:
    """管理端保存的顺序（system_configs）；未存/非法/跑空 → None。"""
    if db is None:
        return None
    row = (
        db.query(SystemConfig)
        .filter(SystemConfig.key == COVER_STRATEGY_KEY)
        .first()
    )
    if row is None or not (row.value or "").strip():
        return None
    try:
        parsed = json.loads(row.value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    steps: list[str] = []
    for entry in parsed:
        step = str(entry).strip()
        if step in _KNOWN_STEPS and step not in steps:
            steps.append(step)
    return steps or None


def effective_strategy_order(db=None) -> list[str]:
    """生效顺序：env（文件配置，优先）→ 管理端保存 → 默认。"""
    env_order = env_strategy_order()
    if env_order is not None:
        return env_order
    stored = stored_strategy_order(db)
    if stored is not None:
        return stored
    return list(_DEFAULT_STRATEGY.split(","))


def _identity_key(item) -> str | None:
    """代表归一键：tmdb 优先，次 imdb；两空 = 无身份，不作代表。"""
    tmdb = (getattr(item, "tmdb_id", None) or "").strip()
    if tmdb:
        return f"tmdb:{tmdb}"
    imdb = (getattr(item, "imdb_id", None) or "").strip()
    if imdb:
        return f"imdb:{imdb}"
    return None


def resolve_library_representative(db, library_id: int):
    """库内已绑定代表条目：同身份键只留最低 id 行；多代表取 id 最大
    且有海报（poster_path 或 primary_image_url 非空）的一个；无则 None。"""
    rows = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.library_id == library_id,
            em.MediaItem.item_type.in_(("movie", "series")),
            _own_bound(em.MediaItem),
        )
        .order_by(em.MediaItem.id.asc())
        .all()
    )
    reps: dict[str, object] = {}
    for item in rows:
        key = _identity_key(item)
        if key and key not in reps:
            reps[key] = item
    for item in sorted(reps.values(), key=lambda x: x.id, reverse=True):
        if (getattr(item, "poster_path", None) or "").strip() or (
            getattr(item, "primary_image_url", None) or ""
        ).strip():
            return item
    return None


def _item_poster_url(item) -> str | None:
    """代表条目的海报 URL：本地缓存图（文件在）→ Images 路由；
    否则远程图直接给 URL；本地路径写了但文件丢了 → 退远程图；
    两者皆无 → None。本地判定与 api._image_chain 逐字同口径
    （is_cached_path + 文件在磁盘）。"""
    poster_path = (getattr(item, "poster_path", None) or "").strip()
    remote = (getattr(item, "primary_image_url", None) or "").strip()
    if (
        poster_path
        and image_store.is_cached_path(poster_path)
        and os.path.isfile(poster_path)
    ):
        return f"/emby/Items/{item.guid}/Images/Primary"
    return remote or None


def _library_images_url(lib) -> str:
    """现有库封面口径：客户端经库 guid 的 Images/Primary 拉图
    （media_routes.item_image 的库分支，按 cover_path 出图）。"""
    return f"/emby/Items/{lib.guid}/Images/Primary"


def _svg_url(lib) -> str:
    """SVG 占位路由（media_library 的用户端路由，静态字节）。"""
    return f"/api/user/media-library/libraries/{lib.id}/cover-placeholder.svg"


def _legacy_cover(lib) -> dict:
    """开关关时的旧行为：只有直传/渲染封面，没有借代表与占位策略。"""
    if getattr(lib, "cover_path", None):
        source = "render" if getattr(lib, "cover_template", None) else "uploaded"
        return {"url": _library_images_url(lib), "source": source}
    return {"url": None, "source": "none"}


def resolve_library_cover(db, lib, kind: str) -> dict:
    """库封面五件套之一的解析结果：{"url": str|None, "source": str}。

    kind 仅影响出图口选择：poster / banner / backdrop / thumb 四种
    共用同一解析结果（本批库封面只有一张图）；logo 恒 null（库无
    logo 概念，FMBY 五件套里 logo 位留空）。source ∈
    {borrow, render, uploaded, svg, none}。
    """
    if kind == "logo":
        return {"url": None, "source": "none"}
    if not borrow_enabled():
        return _legacy_cover(lib)
    for step in effective_strategy_order(db):
        if step == "borrow":
            rep = resolve_library_representative(db, lib.id)
            url = _item_poster_url(rep) if rep is not None else None
            if url:
                return {"url": url, "source": "borrow"}
        elif step == "render":
            if getattr(lib, "cover_template", None) and getattr(lib, "cover_path", None):
                return {"url": _library_images_url(lib), "source": "render"}
        elif step == "uploaded":
            if getattr(lib, "cover_path", None) and not getattr(lib, "cover_template", None):
                return {"url": _library_images_url(lib), "source": "uploaded"}
        elif step == "svg":
            return {"url": _svg_url(lib), "source": "svg"}
    return {"url": None, "source": "none"}

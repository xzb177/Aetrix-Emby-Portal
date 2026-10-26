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

import hashlib
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


# ==================== 虚拟库派生封面（Emby 协议口径） ====================
# 背景：虚拟库（按平台聚合）没有自己的目录与封面文件，cover_path 恒空；
# Emby 协议 Views 里虚拟库因此拿不到 Primary 标记，第三方客户端无图。
# 本节只给「无 cover_path 的虚拟库」派生封面，真实库与已有显式封面的
# 虚拟库一律不走这里（调用方先分流）。派生不写库、不单独存封面文件，
# 封面从内容派生（L1 的魂），与上面五件套的 borrow 开关无关——虚拟库
# 过去本来就没有封面可回退，不存在旧行为可破坏；五件套路径本身不动。

# SVG 占位字节：与 backend/api/media_library.py 的占位路由同一份字节
# （800x450 景观占位，文案“暂无封面”）；占位路由改为从这里 import，
# 不再两处各存一份。
SVG_PLACEHOLDER_BYTES = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"'
    ' viewBox="0 0 800 450"><rect width="800" height="450" fill="#e8e4dc"/>'
    '<g fill="none" stroke="#9a948a" stroke-width="6">'
    '<rect x="310" y="150" width="180" height="150" rx="12"/>'
    '<circle cx="400" cy="225" r="42"/></g>'
    '<text x="400" y="350" font-family="sans-serif" font-size="28"'
    ' fill="#8a847a" text-anchor="middle">暂无封面</text></svg>'
).encode("utf-8")

# 列查询返回的列顺序（调用方按下标取，禁止用 SELECT * 式整行）：
# 0=id, 1=guid, 2=tmdb_id, 3=imdb_id, 4=poster_path, 5=primary_image_url
_VIRTUAL_COLS = (
    em.MediaItem.id,
    em.MediaItem.guid,
    em.MediaItem.tmdb_id,
    em.MediaItem.imdb_id,
    em.MediaItem.poster_path,
    em.MediaItem.primary_image_url,
)


def _virtual_member_rows(db, lib):
    """虚拟库成员列（与 facets.count_virtual_items 同口径）：

    平台聚合：ItemFacet 就绪时走关联表（kind=platform），否则回退
    platforms ILIKE；只取 movie/series、排除 is_hidden；按 id 升序
    （fallback 的确定性顺序与代表分组的最低 id 口径都依赖它）。
    平台为空 → []（无成员，调用方出占位）。
    """
    from backend.emby_server import facets as _facets

    platform = (getattr(lib, "platform", "") or "").strip()
    if not platform:
        return []
    base = (
        db.query(*_VIRTUAL_COLS)
        .filter(
            em.MediaItem.is_hidden == False,  # noqa: E712
            em.MediaItem.item_type.in_(("movie", "series")),
        )
    )
    if _facets.ready(db):
        condition = _facets.candidate_condition(_facets.KIND_PLATFORM, [platform], db)
        if condition is None:
            return []
        return base.filter(condition).order_by(em.MediaItem.id.asc()).all()
    return (
        base.filter(em.MediaItem.platforms.ilike(f"%{platform}%"))
        .order_by(em.MediaItem.id.asc())
        .all()
    )


def _pick_virtual_representative(rows):
    """案 A 代表规则的列元组版（与 resolve_library_representative 同规则，
    实体版保持不动，避免改动真实库路径）：

    rows 按 id 升序；同身份键（tmdb 优先、次 imdb）只留最低 id 行；
    再在代表中取 id 最大且有海报（poster_path 或 primary_image_url
    非空）的一个；无则 None。代表须已绑定（tmdb/imdb 任一 trim 后
    非空，与 _own_bound 的 SQL 语义逐字对应）。
    """
    reps = {}
    for row in rows:
        if not ((row[2] or "").strip() or (row[3] or "").strip()):
            continue
        key = _identity_key_of(row[2], row[3])
        if key and key not in reps:
            reps[key] = row
    for row in sorted(reps.values(), key=lambda r: r[0], reverse=True):
        if (row[4] or "").strip() or (row[5] or "").strip():
            return row
    return None


def _identity_key_of(tmdb, imdb):
    """代表归一键：tmdb 优先，次 imdb；两空 = 无身份。"""
    tmdb = (tmdb or "").strip()
    if tmdb:
        return f"tmdb:{tmdb}"
    imdb = (imdb or "").strip()
    if imdb:
        return f"imdb:{imdb}"
    return None


def _poster_key(row):
    """海报来源键（供 tag 用，来源变了 tag 就变）：本地缓存图取文件
    mtime（与 _item_poster_url 的本地判定同口径），否则取远程 URL。"""
    poster_path = (row[4] or "").strip()
    remote = (row[5] or "").strip()
    if (
        poster_path
        and image_store.is_cached_path(poster_path)
        and os.path.isfile(poster_path)
    ):
        try:
            return f"file:{int(os.path.getmtime(poster_path))}"
        except OSError:
            pass
    return f"url:{remote}"


def _virtual_cover_tag(source, row=None):
    """派生封面的 ImageTag（客户端缓存失效键，tag 本身不被服务端校验）：

    borrow/fallback：vb-/vf- + 代表条目 guid 与海报来源键的短哈希；
    svg：vs- + 占位字节短哈希（占位字节变了 tag 跟着变）。
    """
    if source == "svg":
        return "vs-" + hashlib.md5(SVG_PLACEHOLDER_BYTES).hexdigest()[:8]
    prefix = "vb-" if source == "borrow" else "vf-"
    raw = f"{row[1]}|{_poster_key(row)}".encode("utf-8")
    return prefix + hashlib.md5(raw).hexdigest()[:12]


def resolve_virtual_library_cover(db, lib):
    """虚拟库派生封面（Views ImageTags 与 Primary 出图口径共用）：

    返回 {"source", "tag", "item_id", "item_guid"}，source ∈：
    - borrow：案 A 代表条目的海报（已绑定代表中 id 最大且有海报者）；
    - fallback：无代表时，按 id 升序取首个有海报的成员（确定性顺序，
      不限已绑定——任务书定死的回退口径）；
    - svg：没有任何成员有海报（或库无平台/无成员）时占位；
    - none：仅理论（svg 恒有）；调用方遇 none 按无封面处理。

    代表判定不查 IDENTITY_SHELF_GATE 开关（与既有 borrow 一致：
    借图不是货架准入），但代表本身须已绑定；成员口径与计数一致
    （平台聚合、movie/series、排除 is_hidden）。本函数不写库。
    """
    rows = _virtual_member_rows(db, lib)
    rep = _pick_virtual_representative(rows)
    if rep is not None:
        return {
            "source": "borrow",
            "tag": _virtual_cover_tag("borrow", rep),
            "item_id": rep[0],
            "item_guid": rep[1],
        }
    for row in rows:  # rows 已按 id 升序：首个有海报者即确定性回退
        if (row[4] or "").strip() or (row[5] or "").strip():
            return {
                "source": "fallback",
                "tag": _virtual_cover_tag("fallback", row),
                "item_id": row[0],
                "item_guid": row[1],
            }
    return {
        "source": "svg",
        "tag": _virtual_cover_tag("svg"),
        "item_id": None,
        "item_guid": None,
    }

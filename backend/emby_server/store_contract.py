"""想看商店契约（骨架）。

仓库没有独立 OpenAPI 文件，接口形状跟现有路由一样用 Pydantic。
FastAPI 挂上 ``response_model`` 后，运行时的 ``/api/openapi.json`` 会带出同一套字段。

本模块只钉形状和文案，不访问数据库、不入队转存。

§7 已锁、本骨架遵守的边界：

- 资源路径 **R0**：想看只在 ``tmdb_id`` 对得上现有 ``emby_items`` 时才有资格转存；
  对不上则 ``missing_source``，文案写明不能直接开播。
- 想看要会员订阅；``STORE_ENABLED`` 打开时，访客可以只读浏览目录。
- 不做播放中继 / 边缘节点、不多账号池、不预热、不做 R1 自动求片、不做 R2 索引。
- 大实现另排期：这里不调用 ``transfer_variant.create_user_job``。
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

#: 想看状态。只有这六个值；骨架目前只会返回 missing_source，其余留给后续实现。
WantState = Literal[
    "wanted",
    "resolving",
    "transferring",
    "ready",
    "failed",
    "missing_source",
]

#: 总闸关闭时的 404 文案（惰性 404，与合集域「功能未开启」同一做法）。
STORE_DISABLED_DETAIL = "想看商店未开启"

#: 非会员打想看写口 / 想看清单时的 403 文案。响应体是 ``{"detail": <本字符串>}``。
STORE_MEMBERSHIP_REQUIRED_DETAIL = "想看需要有效的会员订阅"

#: R0：tmdb 对不上库内条目。明确「不能直接开播」，不是即时可看。
MISSING_SOURCE_MESSAGE = "片库里还没有这部片子，不能转入想看，也不能直接开播。"

#: 库内已有条目，但转存工人未接线。501 的 detail，不假装已经入队。
TRANSFER_NOT_WIRED_DETAIL = "库内已有对应条目，想看转存尚未接线，本次不会入队。"


def store_enabled() -> bool:
    """想看商店总闸，默认关。

    读法对齐 ``transfer_variant.master_enabled``：每次调用现读
    ``STORE_ENABLED``，不在 import 时钉死。合法开值：``1`` / ``true`` /
    ``yes`` / ``on``（大小写不敏感）。缺省、空串、``0`` / ``false`` /
    ``no`` / ``off`` 都是关。
    """
    raw = os.getenv("STORE_ENABLED", "0") or "0"
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


class StoreCard(BaseModel):
    """目录 / 详情卡片。骨架不填 TMDB 文案，只保证 ``in_library`` 有地方放。"""

    tmdb_id: str
    title: str | None = None
    year: int | None = None
    poster_url: str | None = None
    #: movie / tv。骨架留空，不猜。
    media_type: str | None = None
    in_library: bool = False
    library_item_id: int | None = None


class CatalogResponse(BaseModel):
    """GET /api/user/store/catalog。骨架固定空列表。"""

    items: list[StoreCard] = Field(default_factory=list)
    total: int = 0


class ItemResponse(StoreCard):
    """GET /api/user/store/items/{tmdb_id}。与卡片同形。"""


class WantIn(BaseModel):
    """POST /api/user/store/want 的请求体。"""

    tmdb_id: str = Field(min_length=1, max_length=32)


class WantRecord(BaseModel):
    """一条想看结果。``state=missing_source`` 时 ``in_library`` 为 false。"""

    tmdb_id: str
    state: WantState
    in_library: bool
    library_item_id: int | None = None
    message: str = ""


class WantListResponse(BaseModel):
    """GET /api/user/store/wants。骨架不落库，会员拿到空列表。"""

    items: list[WantRecord] = Field(default_factory=list)

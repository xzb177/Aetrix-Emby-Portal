"""302 Next 总闸与子开关（F1，默认全关）。

读法对齐 ``store_contract.store_enabled``：每次调用现读 env，不在
import 时钉死。合法开值：``1`` / ``true`` / ``yes`` / ``on``（大小写
不敏感）；缺省、空串、``0`` / ``false`` / ``no`` / ``off`` 都是关。

开关映射（新增 env 键必须同步进 env.example，否则 CI 的 env 文档
防漂移测试会红，见 2026-10-21 #1157 教训）：
- ``NEXT302_ENABLED``：总闸，默认关。关时本组接口惰性 404（路由级
  依赖先于鉴权，未登录也不会先露 401），旧播放链一字不动。
- ``NEXT302_DATASET_ENABLED``：A1 数据集同步任务开关，默认关。
  只在总闸开时才生效。
- ``NEXT302_SEARCH_ENABLED``：A2 搜索与详情、E4 播放状态口开关，
  默认关。只在总闸开时才生效。
- ``NEXT302_DATASET_SYNC_INTERVAL_SEC``：数据集同步间隔秒数，
  默认 86400，下限钳到 600（避免高频刷 TMDB 配额）。

E3（BYOS 选路）没有独立开关：它是门禁选路的纯函数表达
（见 ``next302_play_route``），不改线上播放行为；它的结果只通过
已设开关的 E4 状态口对外展示，所以不需要、也不应该再给它一个
能改选路行为的开关。
"""
from __future__ import annotations

import os

from backend.emby_server.env_util import env_int

MASTER_ENV = "NEXT302_ENABLED"
DATASET_ENV = "NEXT302_DATASET_ENABLED"
SEARCH_ENV = "NEXT302_SEARCH_ENABLED"
DATASET_INTERVAL_ENV = "NEXT302_DATASET_SYNC_INTERVAL_SEC"

DATASET_INTERVAL_DEFAULT = 86400
DATASET_INTERVAL_MIN = 600

_OPEN_VALUES = {"1", "true", "yes", "on"}


def _is_on(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _OPEN_VALUES


def next302_enabled() -> bool:
    """总闸。默认关。"""
    return _is_on(os.getenv(MASTER_ENV))


def dataset_enabled() -> bool:
    """A1 数据集同步开关：总闸与子开关同时开才生效。"""
    return next302_enabled() and _is_on(os.getenv(DATASET_ENV))


def search_enabled() -> bool:
    """A2 搜索与详情、E4 播放状态口开关：总闸与子开关同时开才生效。"""
    return next302_enabled() and _is_on(os.getenv(SEARCH_ENV))


def dataset_sync_interval_sec() -> int:
    """数据集同步间隔秒数，默认 86400，下限钳到 600。"""
    return env_int(DATASET_INTERVAL_ENV, DATASET_INTERVAL_DEFAULT,
                   lo=DATASET_INTERVAL_MIN)

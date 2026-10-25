"""刮削快照的语言 / 地区版本缓存口径（FMBY 高级刮削候选 5，第一批）。

FMBY 的刮削快照按 language / region 分版本、过期才重拉；本模块只定
口径，不替任何调用方决定“该缓存什么”：

- 版本键：``<前缀>:<身份>|<语言>|<地区>``，身份是条目已绑定的外部身份
  （调用方给 tmdb_id 或 imdb_id，见 zh_aggregate）；语言归一化成小写
  （``zh_TW`` 与 ``zh-tw`` 同键），地区归一化成大写；空语言 / 空地区
  就是“未指定”版本，不偷偷填任何具体语言或地区默认值。
- 过期语义：TTL 内命中为 fresh，直接用；过期后 ``get`` 视同未命中
  （调用方重拉），但 ``lookup`` 仍把过期快照以 ``stale`` 交出来——
  调用方自行决定要不要在重拉失败时拿旧数兜底，本模块不替它兜。
- 可配项（env，见 env.example）：总开关、TTL 秒数、键前缀；三者全是
  部署者配置，不 hardcode 具体语言、地区或业务片单。
- 存储只是进程内字典：本模块是口径层，不是持久化层；进程重启即空，
  重拉一轮即回满，不靠它保证跨进程命中。需要落盘时由调用方另接
  tmdb_cache 那套磁盘信封（它已有 language 维度的键）。
"""

from __future__ import annotations

import logging
import os
import time

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 版本缓存总开关（默认关）。关时调用方不查也不写本缓存。
VERSION_CACHE_ENABLED = env_int("SCRAPE_VERSION_CACHE_ENABLED", 0, 0, 1)
#: 版本快照 TTL（秒），默认 30 天（与 tmdb_cache 的 details 口径对齐）。
VERSION_CACHE_TTL_SEC = env_int("SCRAPE_VERSION_CACHE_TTL_SEC", 30 * 86400, 60, None)
#: 版本键前缀（默认 scrape:v1；改前缀即让全部旧版本键自然失效）。
_DEFAULT_KEY_PREFIX = "scrape:v1"


def version_cache_enabled() -> bool:
    """版本缓存是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return VERSION_CACHE_ENABLED > 0


def _cfg_prefix() -> str:
    raw = os.getenv("SCRAPE_VERSION_CACHE_KEY_PREFIX", "")
    return raw.strip() or _DEFAULT_KEY_PREFIX


def normalize_language(language) -> str:
    """语言归一化：去空白、小写、``_`` 统一成 ``-``；空 = 未指定。"""
    return str(language or "").strip().lower().replace("_", "-")


def normalize_region(region) -> str:
    """地区归一化：去空白、大写；空 = 未指定。"""
    return str(region or "").strip().upper()


def version_key(identity, language="", region="", prefix=None) -> str:
    """算版本键。身份为空时抛 ValueError——没身份的快照不许落缓存。

    键形：``<前缀>:<身份>|<语言>|<地区>``。语言 / 地区只做大小写与
    分隔符归一化，不填默认值：调用方没给就是空段，空段版本与有值
    版本互不污染。
    """
    ident = str(identity or "").strip()
    if not ident:
        raise ValueError("version_key 需要非空身份（tmdb_id / imdb_id）")
    if prefix is None:
        prefix = _cfg_prefix()
    return (
        f"{prefix}:{ident}|{normalize_language(language)}"
        f"|{normalize_region(region)}"
    )


def _identity_of_key(key: str) -> str:
    """从版本键反解身份段（invalidate 按身份清全部版本用）。

    键形 ``<前缀>:<身份>|<语言>|<地区>``，前缀本身可含冒号（如默认
    的 ``scrape:v1``），故先按 ``|`` 切掉语言段、再取最后一段冒号
    之后的内容当身份（身份本身不含冒号与竖线）。
    """
    head = key.split("|", 1)[0] if key else ""
    return head.rsplit(":", 1)[-1] if head else ""

class VersionCache:
    """进程内版本快照存储（口径见模块头注）。

    ``ttl_sec`` 不传时取模块常量（部署者 env）；测试可直接传小 TTL。
    时间一律经 ``now`` 参数注入（默认 time.time），过期语义单测可复现。
    """

    def __init__(self, ttl_sec=None):
        self.ttl_sec = ttl_sec if ttl_sec is not None else VERSION_CACHE_TTL_SEC
        self._store: dict = {}

    def __len__(self) -> int:
        return len(self._store)

    def put(self, identity, language, region, payload, now=None) -> str:
        """写一个版本快照（覆盖同键旧快照并刷新过期时间）。返回版本键。"""
        key = version_key(identity, language, region)
        at = time.time() if now is None else float(now)
        self._store[key] = (at + float(self.ttl_sec), payload)
        return key

    def lookup(self, identity, language, region, now=None):
        """查一个版本快照，返回 ``(状态, 载荷)``。

        状态：``hit``（TTL 内）、``stale``（已过期，载荷仍交出来供调用方
        自行决定）、``miss``（从来没有）。过期条目不自动删除——删由
        ``purge_expired`` 显式做，免得一次过期查询顺手清掉可兜底的旧数。
        """
        key = version_key(identity, language, region)
        entry = self._store.get(key)
        if entry is None:
            return "miss", None
        expires_at, payload = entry
        at = time.time() if now is None else float(now)
        if at <= expires_at:
            return "hit", payload
        return "stale", payload

    def get(self, identity, language, region, now=None):
        """只取新鲜快照：过期或没有都返回 None（调用方据此重拉）。"""
        status, payload = self.lookup(identity, language, region, now=now)
        return payload if status == "hit" else None

    def invalidate(self, identity) -> int:
        """按身份清掉该身份的全部语言 / 地区版本。返回清掉的条数。"""
        ident = str(identity or "").strip()
        if not ident:
            return 0
        doomed = [k for k in self._store if _identity_of_key(k) == ident]
        for key in doomed:
            del self._store[key]
        return len(doomed)

    def purge_expired(self, now=None) -> int:
        """清掉全部已过期条目。返回清掉的条数。"""
        at = time.time() if now is None else float(now)
        doomed = [k for k, (exp, _v) in self._store.items() if at > exp]
        for key in doomed:
            del self._store[key]
        return len(doomed)

    def clear(self) -> None:
        """清空全部快照（测试与进程内重置用）。"""
        self._store.clear()


#: 模块级默认缓存实例（聚合通道用它；测试可整体替换）。
DEFAULT_CACHE = VersionCache()


__all__ = [
    "VERSION_CACHE_ENABLED",
    "VERSION_CACHE_TTL_SEC",
    "VersionCache",
    "DEFAULT_CACHE",
    "normalize_language",
    "normalize_region",
    "version_cache_enabled",
    "version_key",
]

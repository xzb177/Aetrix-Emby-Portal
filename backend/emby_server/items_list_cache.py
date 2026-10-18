"""Emby 列表接口 ID 清单缓存（排好序的 kept id 清单 + total 总数）。

叠加在 :mod:`items_dedup_cache` 之上。dropped 集合缓存把「去重计算」
（E 分支 16 万行窗口排序，约 0.6s/请求）摊薄后，每个请求剩下的大头是
「全量 kept 排序 + 分页 + 计数」（约 0.5s/请求，PG CPU 瓶颈的直接来源）。
本层缓存「排好序的 kept id 清单（前 max_ids 个）+ total」，命中时请求只剩
「按主键取本页 20 行 + 用户态预取 + DTO 构建」。

正确性红线（比命中率重要）：

1. **DTO 永不进缓存**：最终条目（含单用户的播放状态/收藏）每次都从 DB
   现行重建；缓存里只有 id 和总数。
2. **用户状态相关查询一律绕行**：``Filters``/``IsFavorite``/``IsPlayed``/
   ``IsUnplayed``/``IsResumable``（读 ``UserMediaData``）与
   ``SortBy=DatePlayed/PlayCount``（按用户数据排序）命中任一 → 完全不读
   不写本缓存。随机排序分支在调用方独立处理，同样不进本缓存。
3. **键**：规范化查询参数（排除 ``api_key``/``StartIndex``/``Limit``/
   ``EnableTotalRecordCount``；StartIndex/Limit 是分页切片维度，不能进键，
   否则每页一个键）+ **allowed 可见库集合的哈希**（不用用户 id：相同可见
   范围的用户共享清单；用户状态在 DTO 层现查）+ 范围内各库数据版本号
   （与 :mod:`items_dedup_cache` 同一套 ``items_dedup_version_lib_<库id>``
   版本行、同样的直查 DB 读法；版本号变 → 键变 → 自然失效）。
   未来新增的筛选参数自动参与键，不会悄悄复用旧缓存。
4. **失效**：完全复用去重缓存的版本行与 bump 点（不新造第二套 bump；
   所有改变 kept 清单内容/顺序的写路径——扫描收尾/合并解合并/bind-tmdb/
   刮削回填/软删清理/后台解绑——都已在去重缓存的 bump 覆盖内，
   见 :func:`items_dedup_cache.bump_library_item_versions` 调用方）。
   残余缺口（例如运维手工 ORM 改数据不走应用写路径）由条目 TTL 兜底
   （默认 60s），与去重缓存同一残余口径。
5. **有界**：只存前 ``max_ids`` 个 id（默认 5000）+ total + 截断标记；
   请求的页窗口超出已存前缀 → 绕行现状路径（深翻页走 DB，不错不错位）。
   Redis string 紧凑二进制（SETEX），条目大小恒有界。
6. **fail-open**：开关关 / TTL<=0 / Redis 异常 / 值损坏 → 与现状逐行一致
   的旧路径，请求不受影响。

可配置（项目铁律）：``items_list_cache_enabled``（默认开）、
``items_list_cache_ttl_seconds``（默认 60，范围 10~3600）、
``items_list_cache_max_ids``（默认 5000，范围 100~50000）。
读法与 items_dedup_cache 同风格（store 热读，读失败回默认）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import struct
import time
from typing import Iterable, NamedTuple, Optional
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from backend.emby_server import items_dedup_cache as _dedup
from backend.emby_server import models as em
from backend.integrations import store

logger = logging.getLogger(__name__)

# ---------------- 配置键 ----------------
CFG_ENABLED = "items_list_cache_enabled"
CFG_TTL_SECONDS = "items_list_cache_ttl_seconds"
CFG_MAX_IDS = "items_list_cache_max_ids"

ENABLED_DEFAULT = True
TTL_DEFAULT = 60
TTL_MIN, TTL_MAX = 10, 3600
MAX_IDS_DEFAULT = 5000
MAX_IDS_MIN, MAX_IDS_MAX = 100, 50000

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}

# ---------------- Redis 键 ----------------
_KEY_PREFIX = "items:list:v1"
_INDEX_KEY = "items:list:v1:index"  # ZSET：成员=条目键，分值=最后访问时间戳

# 条目数硬上限（常量；热点列表页的键本来就少，与 max_ids 的字节上限是两回事）
_MAX_ENTRIES = 1000

# 不进缓存键的参数：api_key（鉴权轮换不应打掉缓存）、StartIndex/Limit
#（分页切片维度）、EnableTotalRecordCount（只影响返回形态不影响清单内容）
_KEY_EXCLUDE = {"api_key", "startindex", "limit", "enabletotalrecordcount"}

# 用户状态相关筛选：命中任一 → 完全绕行本缓存（见模块 docstring 红线 2）
USER_STATE_FILTERS = frozenset({"isfavorite", "isplayed", "isunplayed", "isresumable"})
# 用户状态相关排序
USER_SORT_KEYS = frozenset({"dateplayed", "playcount"})

# 值格式：b"IL1" + u64 total + u8 truncated + u64 ids...
_VALUE_MAGIC = b"IL1"


class CachedPage(NamedTuple):
    ids: list  # 已存前缀（有序 kept id）
    total: int  # kept 总数（与缓存时的 count 口径一致）
    truncated: bool  # kept 全量是否超过 max_ids（前缀是否被截断）


# ---------------- 配置读取（与 items_dedup_cache 同风格） ----------------

def cache_enabled(db: Session | None = None) -> bool:
    if db is None:
        return ENABLED_DEFAULT
    try:
        raw = store.get_value(db, CFG_ENABLED)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ID 清单缓存开关读取失败，默认启用: %s", exc)
        return ENABLED_DEFAULT
    if raw is None:
        return ENABLED_DEFAULT
    v = str(raw).strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return ENABLED_DEFAULT


def _clamp_int(raw, lo: int, hi: int, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def cache_ttl_seconds(db: Session | None = None) -> int:
    if db is None:
        return TTL_DEFAULT
    try:
        raw = store.get_value(db, CFG_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ID 清单缓存 TTL 读取失败，用默认值: %s", exc)
        return TTL_DEFAULT
    return _clamp_int(raw, TTL_MIN, TTL_MAX, TTL_DEFAULT)


def cache_max_ids(db: Session | None = None) -> int:
    if db is None:
        return MAX_IDS_DEFAULT
    try:
        raw = store.get_value(db, CFG_MAX_IDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ID 清单缓存 max_ids 读取失败，用默认值: %s", exc)
        return MAX_IDS_DEFAULT
    return _clamp_int(raw, MAX_IDS_MIN, MAX_IDS_MAX, MAX_IDS_DEFAULT)


def config_payload(db: Session | None = None) -> dict:
    """读当前值 + 默认值（供将来管理端点；与 dedup 的 config_payload 同构）。"""
    return {
        "enabled": cache_enabled(db),
        "ttl_seconds": cache_ttl_seconds(db),
        "max_ids": cache_max_ids(db),
        "defaults": {
            "enabled": ENABLED_DEFAULT,
            "ttl_seconds": TTL_DEFAULT,
            "max_ids": MAX_IDS_DEFAULT,
        },
    }


# ---------------- 可缓存性判定 ----------------

def is_cacheable(*, filters: set, sort_by: list, has_ids: bool) -> bool:
    """该请求是否允许读/写 ID 清单缓存。

    - 用户状态筛选（filters 含收藏/已看/未看/继续看）→ 绕行；
    - 按用户数据排序（DatePlayed/PlayCount）→ 绕行；
    - ``Ids`` 定向取条目（详情/版本切换类小查询）→ 绕行（缓存价值极低，
      且键里塞 guid 列表会制造键爆炸）。
    宁可少缓存，不可错缓存。
    """
    if has_ids:
        return False
    if set(filters or ()) & set(USER_STATE_FILTERS):
        return False
    for c in sort_by or ():
        if str(c).strip().lower() in USER_SORT_KEYS:
            return False
    return True


# ---------------- 键 ----------------

def scope_lib_ids(db: Session, allowed: Optional[set]) -> list[int]:
    """版本号作用域：allowed 非 None（含空集合）用它本身；None = 不过滤 → 全部启用库。

    ⚠️ 空集合是 _library_scope 的 fail-closed 语义（全部不可见），绝不能
    落到下面的「全部启用库」分支：那会让不可见用户与不限制用户（None）
    共用同一个缓存键，把全量清单喂给本该看不到任何条目的人。空集合
    返回空作用域（键独立、其清单本就为空，语义正确）。
    """
    if allowed is not None:
        return sorted({int(i) for i in allowed})
    try:
        return sorted(
            r[0] for r in
            db.query(em.Library.id).filter(em.Library.is_enabled == True).all()  # noqa: E712
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("ID 清单缓存读媒体库列表失败: %s", exc)
        return []


def _canonical_list_params(query_params) -> str:
    """查询参数的规范形：排序后 urlencode；排除鉴权/分页/返回形态参数。

    - 参数顺序不同但语义相同 → 同一键；
    - StartIndex/Limit 不参与键（它们是切片维度，同一份清单服务所有页）；
    - 未来新增的筛选参数自动参与键，不会悄悄复用旧缓存。
    """
    try:
        items = query_params.multi_items()  # Starlette QueryParams
    except AttributeError:
        try:
            items = list(query_params.items())  # 普通 dict（测试）
        except AttributeError:
            items = []
    pairs = sorted(
        (str(k), str(v)) for k, v in items
        if str(k).lower() not in _KEY_EXCLUDE
    )
    return urlencode(pairs)


def _cache_key(lib_ids: list[int], params_hash: str, versions: dict[int, str]) -> str:
    lib_ids = sorted({int(i) for i in (lib_ids or [])})
    scope_hash = hashlib.sha1(",".join(map(str, lib_ids)).encode()).hexdigest()[:12]
    ver = "|".join(f"{lid}={versions.get(lid, '0')}" for lid in lib_ids)
    ver_hash = hashlib.sha1(ver.encode()).hexdigest()[:16]
    return f"{_KEY_PREFIX}:a{scope_hash}:p{params_hash}:v{ver_hash}"


def _key_for(db: Session, query_params, lib_ids: list[int]) -> str:
    versions = _dedup.read_data_versions(db, lib_ids)
    params_hash = hashlib.sha1(_canonical_list_params(query_params).encode()).hexdigest()[:16]
    return _cache_key(lib_ids, params_hash, versions)


# ---------------- 序列化 ----------------

def _serialize(ids: Iterable[int], total: int, truncated: bool) -> str:
    ids = [int(i) for i in ids]
    body = (
        _VALUE_MAGIC
        + struct.pack(">Q", int(total))
        + struct.pack(">B", 1 if truncated else 0)
        + struct.pack(f">{len(ids)}Q", *ids)
    )
    return base64.urlsafe_b64encode(body).decode("ascii")


def _deserialize(raw) -> Optional[CachedPage]:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("ascii")
        body = base64.urlsafe_b64decode(raw.encode("ascii"))
    except Exception:  # noqa: BLE001
        return None
    head = len(_VALUE_MAGIC) + 8 + 1
    if len(body) < head or not body.startswith(_VALUE_MAGIC):
        return None
    if (len(body) - head) % 8 != 0:
        return None
    total = struct.unpack(">Q", body[len(_VALUE_MAGIC):len(_VALUE_MAGIC) + 8])[0]
    truncated = bool(body[len(_VALUE_MAGIC) + 8])
    n = (len(body) - head) // 8
    ids = list(struct.unpack(f">{n}Q", body[head:])) if n else []
    return CachedPage(ids=ids, total=total, truncated=truncated)


# ---------------- 读写 ----------------

def get_cached_page(db: Session, query_params, lib_ids: list[int]) -> Optional[CachedPage]:
    """读缓存：命中返回 CachedPage；miss/禁用/异常/损坏返回 None。"""
    if not cache_enabled(db):
        return None
    if cache_ttl_seconds(db) <= 0:
        return None
    key = _key_for(db, query_params, lib_ids)

    def _op(c):
        raw = c.get(key)
        if raw is not None:
            c.zadd(_INDEX_KEY, {key: time.time()})
        return raw

    raw = _dedup._do(_op, None)  # 与去重缓存共用 Redis 后端（含测试注入点）
    if raw is None:
        return None
    page = _deserialize(raw)
    if page is None:
        logger.warning("ID 清单缓存值无法解析，已按 miss 处理 key=%s", key)
    return page


def put_cached_page(db: Session, query_params, lib_ids: list[int],
                    ids: Iterable[int], total: int, truncated: bool) -> None:
    """写缓存：SETEX + 索引记时 + 超上限淘汰最旧。失败静默（不影响请求）。"""
    if not cache_enabled(db):
        return
    ttl = cache_ttl_seconds(db)
    if ttl <= 0:
        return
    key = _key_for(db, query_params, lib_ids)
    val = _serialize(ids, total, truncated)

    def _op(c):
        c.set(key, val, ex=ttl)
        c.zadd(_INDEX_KEY, {key: time.time()})
        over = c.zcard(_INDEX_KEY) - _MAX_ENTRIES
        if over > 0:
            oldest = c.zrange(_INDEX_KEY, 0, over - 1)
            if oldest:
                c.delete(*oldest)
            c.zremrangebyrank(_INDEX_KEY, 0, over - 1)
        return True

    _dedup._do(_op, False)

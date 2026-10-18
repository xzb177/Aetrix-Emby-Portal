"""Emby 列表接口去重结果缓存（``dropped`` 集合）。

背景（2026-10-18）：列表接口经四轮纯查询优化到单用户约 1.6s 后到头——
E 分支 16 万行窗口排序 405ms 是硬地板（证据见
``~/workspace/reports/items-perf-r4-report-20261018.md``），复测瓶颈在 PG CPU。
本模块缓存 :func:`_dedup_dropped_ids` 算出的「被去掉的 id 集合」
（全库约 1.6 万 id，单次请求约 0.6s），预期把单用户全库列表压进 1.0s 量级。

设计要点：

1. **缓存对象**：只缓存 ``dropped`` id 集合，不缓存最终渲染结果。
   最终 DTO（含用户维度的播放状态/收藏）每次都从 DB 现行重建，
   因此命中与未命中的返回结果天然一致——去重语义本身一字不动。

2. **键**：``用户 id + 规范化查询参数 + 范围内各媒体库的数据版本号``。
   查询参数取全部排序后的 ``(名, 值)`` 对（排除 ``api_key`` 这类鉴权材料），
   新参数自动参与键——后续加筛选条件不会悄悄复用旧缓存。

3. **失效（正确性优先于命中率）**：双保险。
   - 数据版本号：``system_configs`` 行 ``items_dedup_version_lib_<库id>``，
     去重相关写路径（扫描收尾、合并/解合并、bind-tmdb、刮削回填、
     软删清理）提交时 bump。版本号读**直查 DB**（不走 store 的 60s 热缓存），
     跨进程最多按条目 TTL 陈旧。
   - 条目 TTL（默认 60s，可配置）：版本号漏挂的路径最多旧 60s。
   最坏陈旧 ≈ 防抖窗口（30s）+ 条目 TTL。

4. **内存有界**：Redis 存（与 ``scan_queue_redis`` 同一个 ``redis_call``
   共享客户端，不自起第二套）；每条目 SETEX；另有一个 ZSET 索引记
   最后访问时间，写入时按配置上限淘汰最旧条目。Redis 不可用/异常时
   一律降级走旧路径（fail-open），不影响正常请求。

5. **可配置**（铁律）：总开关 ``items_dedup_cache_enabled``（默认开）、
   ``items_dedup_cache_ttl_seconds``（默认 60，范围 10~3600）、
   ``items_dedup_cache_max_entries``（默认 2000，范围 100~20000）。
   开关关或 TTL<=0 → 与没这个模块时逐行一致的旧路径。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Callable, Iterable, Optional
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from backend import models
from backend.emby_server import models as em
from backend.integrations import store

logger = logging.getLogger(__name__)

# ---------------- 配置键 ----------------
CFG_ENABLED = "items_dedup_cache_enabled"
CFG_TTL_SECONDS = "items_dedup_cache_ttl_seconds"
CFG_MAX_ENTRIES = "items_dedup_cache_max_entries"

ENABLED_DEFAULT = True
TTL_DEFAULT = 60
TTL_MIN, TTL_MAX = 10, 3600
MAX_ENTRIES_DEFAULT = 2000
MAX_ENTRIES_MIN, MAX_ENTRIES_MAX = 100, 20000

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}

# ---------------- Redis 键 ----------------
_KEY_PREFIX = "items:dedup:v1"
_INDEX_KEY = "items:dedup:v1:index"  # ZSET：成员=条目键，分值=最后访问时间戳

# 版本号行：items_dedup_version_lib_<库id>，值= bump 时的 epoch 秒
_VERSION_KEY_PREFIX = "items_dedup_version_lib_"

# bump 进程内防抖：同一库 30s 内最多实际写库一次（刮削是逐条短事务，
# 不防抖会把 system_configs 写成热点）。最坏陈旧 +30s，见模块 docstring。
_BUMP_DEBOUNCE_SEC = 30.0
_bump_lock = threading.Lock()
_last_bump_at: dict[int, float] = {}


def _version_key(library_id: int) -> str:
    return f"{_VERSION_KEY_PREFIX}{int(library_id)}"


# ---------------- 配置读取（热读 system_configs，走 store 60s 缓存；读失败回默认） ----------------

def cache_enabled(db: Session | None = None) -> bool:
    if db is None:
        return ENABLED_DEFAULT
    try:
        raw = store.get_value(db, CFG_ENABLED)
    except Exception as exc:  # noqa: BLE001
        logger.warning("去重缓存开关读取失败，默认启用: %s", exc)
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
        logger.warning("去重缓存 TTL 读取失败，用默认值: %s", exc)
        return TTL_DEFAULT
    return _clamp_int(raw, TTL_MIN, TTL_MAX, TTL_DEFAULT)


def cache_max_entries(db: Session | None = None) -> int:
    if db is None:
        return MAX_ENTRIES_DEFAULT
    try:
        raw = store.get_value(db, CFG_MAX_ENTRIES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("去重缓存条目上限读取失败，用默认值: %s", exc)
        return MAX_ENTRIES_DEFAULT
    return _clamp_int(raw, MAX_ENTRIES_MIN, MAX_ENTRIES_MAX, MAX_ENTRIES_DEFAULT)


def config_payload(db: Session | None = None) -> dict:
    """管理后台读取：当前值 + 默认值（前端卡片用；play_sign.config_payload 同构）。"""
    return {
        "enabled": cache_enabled(db),
        "ttl_seconds": cache_ttl_seconds(db),
        "max_entries": cache_max_entries(db),
        "defaults": {
            "enabled": ENABLED_DEFAULT,
            "ttl_seconds": TTL_DEFAULT,
            "max_entries": MAX_ENTRIES_DEFAULT,
        },
    }


def write_config(db: Session, *, enabled: bool, ttl_seconds: int,
                 max_entries: int) -> dict:
    """管理后台写入：范围非法抛 ValueError（端点转 400）；不提交（调用方提交）。"""
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError(f"缓存有效期需在 {TTL_MIN}~{TTL_MAX} 秒之间")
    if not (TTL_MIN <= ttl <= TTL_MAX):
        raise ValueError(f"缓存有效期需在 {TTL_MIN}~{TTL_MAX} 秒之间")
    try:
        mx = int(max_entries)
    except (TypeError, ValueError):
        raise ValueError(f"缓存条目上限需在 {MAX_ENTRIES_MIN}~{MAX_ENTRIES_MAX} 之间")
    if not (MAX_ENTRIES_MIN <= mx <= MAX_ENTRIES_MAX):
        raise ValueError(f"缓存条目上限需在 {MAX_ENTRIES_MIN}~{MAX_ENTRIES_MAX} 之间")
    from backend import config_service
    config_service.save_config(
        db,
        {
            CFG_ENABLED: bool(enabled),
            CFG_TTL_SECONDS: ttl,
            CFG_MAX_ENTRIES: mx,
        },
        schema="items_cache",
    )
    return config_payload(db)


# ---------------- 数据版本号 ----------------

def read_data_versions(db: Session, library_ids: Iterable[int]) -> dict[int, str]:
    """直查 DB 读各库数据版本号（**不走** store 热缓存：跨进程 bump 后最多
    按条目 TTL 陈旧，而不是 60s+TTL）。缺行 = "0"。"""
    lib_ids = sorted({int(i) for i in (library_ids or []) if i})
    if not lib_ids:
        return {}
    keys = [_version_key(i) for i in lib_ids]
    try:
        rows = (
            db.query(models.SystemConfig.key, models.SystemConfig.value)
            .filter(models.SystemConfig.key.in_(keys))
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("去重数据版本号读取失败，按无版本处理: %s", exc)
        return {i: "0" for i in lib_ids}
    found = {k: v for k, v in rows}
    return {i: found.get(_version_key(i), "0") for i in lib_ids}


def bump_library_item_versions(db: Session, library_ids: Iterable[int],
                               *, force: bool = False) -> int:
    """去重相关写路径的数据版本号 bump。

    只在调用方的事务里**暂存** upsert（不提交）：调用方必须在数据提交**之前**
    调用本函数，让版本号与数据变更原子提交；读路径直查 DB，提交后即刻可见。

    进程内 30s 防抖（刮削逐条提交时不把 system_configs 写成热点）。
    返回实际写入的库数（0 = 被防抖/无库）。
    """
    libs = sorted({int(i) for i in (library_ids or []) if i})
    if not libs:
        return 0
    if not force:
        now = time.monotonic()
        with _bump_lock:
            libs = [lid for lid in libs
                    if now - _last_bump_at.get(lid, 0.0) >= _BUMP_DEBOUNCE_SEC]
            for lid in libs:
                _last_bump_at[lid] = now
    if not libs:
        return 0
    # 毫秒时间戳：跨进程（api/worker/EA）的 force bump 可能落在同一秒，
    # 秒级版本号会写出相同值导致第二次 bump 语义上丢失；毫秒级基本杜绝碰撞。
    # （同进程内另有 30s 防抖；残余的极端碰撞仍被条目 TTL 兜底。）
    ts = str(int(time.time() * 1000))
    desc = "列表去重缓存数据版本号（写路径自动 bump，勿手改）"
    store.write_values(
        db,
        {_version_key(lid): ts for lid in libs},
        {_version_key(lid): desc for lid in libs},
    )
    return len(libs)


def _reset_bump_state() -> None:
    """测试用：清防抖状态。"""
    with _bump_lock:
        _last_bump_at.clear()


# ---------------- 缓存后端（Redis 共享客户端；测试可注入假后端） ----------------

_backend_override = None


def _set_backend_override(backend) -> None:
    """测试用：注入假后端（实现 get/set/zadd/zcard/zrange/zrem/delete）。"""
    global _backend_override
    _backend_override = backend


def _do(fn: Callable, default=None):
    """走共享 Redis（熔断器 + fail-open）；测试注入了假后端则直调。"""
    backend = _backend_override
    if backend is not None:
        try:
            return fn(backend)
        except Exception:  # noqa: BLE001
            return default
    try:
        from backend.database import redis_call
    except Exception:  # noqa: BLE001
        return default
    return redis_call(fn, default)


def _canonical_params(query_params) -> str:
    """查询参数的规范形：全部 (名, 值) 对排序后 urlencode。

    - 参数顺序不同但语义相同 → 同一键；
    - ``api_key`` 这类鉴权材料排除在外（轮换 token 不应打掉缓存）；
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
        if str(k).lower() != "api_key"
    )
    return urlencode(pairs)


def _cache_key(user_id: int, params_hash: str, versions: dict[int, str]) -> str:
    ver = "|".join(f"{lid}={versions[lid]}" for lid in sorted(versions))
    ver_hash = hashlib.sha1(ver.encode()).hexdigest()[:16]
    return f"{_KEY_PREFIX}:u{int(user_id)}:p{params_hash}:v{ver_hash}"


def _serialize(dropped: set[int]) -> str:
    return ",".join(str(i) for i in sorted(dropped))


def _deserialize(raw: str) -> set[int]:
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x}


def get_cached_dropped(db: Session, user_id: int, query_params,
                       scope_lib_ids: Iterable[int],
                       versions: dict[int, str] | None = None) -> Optional[set]:
    """读缓存：命中返回 dropped 集合；miss/禁用/异常返回 None。

    versions 预传时跳过读数据版本号（调用方已在 miss+put 两处复用一次读取）。
    """
    if not cache_enabled(db):
        return None
    if cache_ttl_seconds(db) <= 0:
        return None
    if versions is None:
        versions = read_data_versions(db, scope_lib_ids)
    params_hash = hashlib.sha1(_canonical_params(query_params).encode()).hexdigest()[:16]
    key = _cache_key(user_id, params_hash, versions)

    def _op(c):
        raw = c.get(key)
        if raw is not None:
            c.zadd(_INDEX_KEY, {key: time.time()})  # 刷新 LRU 时间戳
        return raw

    raw = _do(_op, None)
    if raw is None:
        return None
    try:
        return _deserialize(raw if isinstance(raw, str) else raw.decode())
    except (ValueError, AttributeError):
        # 脏数据：当 miss 处理（不抛，请求不能因为缓存挂掉）
        logger.warning("去重缓存值无法解析，已按 miss 处理 key=%s", key)
        return None


def put_cached_dropped(db: Session, user_id: int, query_params,
                       scope_lib_ids: Iterable[int], dropped: set,
                       versions: dict[int, str] | None = None) -> None:
    """写缓存：SETEX + 索引记时 + 超上限淘汰最旧。失败静默（不影响请求）。

    versions 预传时跳过读数据版本号（调用方已在 miss+put 两处复用一次读取）。
    """
    if not cache_enabled(db):
        return
    ttl = cache_ttl_seconds(db)
    if ttl <= 0:
        return
    max_entries = cache_max_entries(db)
    if versions is None:
        versions = read_data_versions(db, scope_lib_ids)
    params_hash = hashlib.sha1(_canonical_params(query_params).encode()).hexdigest()[:16]
    key = _cache_key(user_id, params_hash, versions)
    val = _serialize(dropped)

    def _op(c):
        c.set(key, val, ex=ttl)
        c.zadd(_INDEX_KEY, {key: time.time()})
        over = c.zcard(_INDEX_KEY) - max_entries
        if over > 0:
            oldest = c.zrange(_INDEX_KEY, 0, over - 1)
            if oldest:
                c.delete(*oldest)
                # 按成员名删（与 delete 配对）：并发交错时 zremrangebyrank 可能
                # 删掉「别的请求刚写的新条目」，成员名删保证一致。
                c.zrem(_INDEX_KEY, *oldest)
        return True

    _do(_op, False)


def dedup_dropped_ids_cached(request, query, db: Session, user,
                             allowed: Optional[set],
                             compute: Callable[[], Optional[set]],
                             *, lib_ids: Optional[list] = None,
                             versions: Optional[dict] = None) -> Optional[set]:
    """``_query_items`` 的唯一调用点：缓存包装的 ``_dedup_dropped_ids``。

    - 开关关 / Redis 不可用 → ``compute()``（旧路径，逐行一致）；
      （TTL 被钳制在 10~3600 秒，<=0 不可达，故不在此列）
    - 命中 → 直接返回集合；
    - 未命中 → ``compute()``，结果非 None 才写入缓存（``None`` 是回退旧实现
      的信号，不缓存）；
    - 范围 ``allowed`` 为 None（不过滤）时，版本号取全部启用库。
    - ``lib_ids``/``versions`` 预传时跳过内部的媒体库查询与版本号直查
      （r41：调用方 _query_items 已为 ID 清单缓存算过一次，同请求内恒定；
      不传时行为与旧实现逐行一致，含读失败 fail-closed 回 compute()）。
    """
    if not cache_enabled(db) or cache_ttl_seconds(db) <= 0:
        return compute()
    if lib_ids is None:
        if allowed is not None:
            # 空集合与 None 必须走不同的键：allowed=set() 时 lib_ids=[]，
            # read_data_versions(db, []) 返回 {}（空输入直接 return），版本号
            # 串为空 → 生成独立的降级缓存键，与 None（取全部启用库做版本号
            # 的全量键）天然不同；空范围查询的结果不再毒化全量缓存键。
            lib_ids = list(allowed)
        else:
            try:
                lib_ids = [r[0] for r in
                           db.query(em.Library.id).filter(em.Library.is_enabled == True).all()]  # noqa: E712
            except Exception as exc:  # noqa: BLE001
                logger.warning("读媒体库列表失败，走旧路径: %s", exc)
                return compute()
    q = request.query_params
    # miss+put 共用一次版本号读取：同请求内版本号恒定，省一次 DB 查询，
    # 且键与命中/写入的版本输入完全一致（与分两次读行为等价）。
    if versions is None:
        versions = read_data_versions(db, lib_ids)
    hit = get_cached_dropped(db, user.id, q, lib_ids, versions=versions)
    if hit is not None:
        return hit
    dropped = compute()
    if dropped is not None:
        put_cached_dropped(db, user.id, q, lib_ids, dropped, versions=versions)
    return dropped

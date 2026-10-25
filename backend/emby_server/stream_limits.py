"""按「挂载 + 路径前缀」限制同时播放路数（FMBY 批一 #3）。

学的是 FMBY 的 ``source_path_policies.max_concurrent_streams``：并发上限不挂在
「用户」上（我方已另有一套按用户的防共享口径，见 compat_routes 的会话拦截），
而是挂在「某个挂载的某个目录前缀」上——防的是一个热目录被很多路播放同时回源，
把网盘的下载配额 / API 限速打爆（魂 5：对外部变化克制；兼护底线：不触发网盘
风控）。

计数模型（租约式，跨进程一致）：
- 生产 api 与 ea 是两个进程，进程内计数各算各的等于上限翻倍，所以计数只放
  Redis：每个策略作用域一个 ZSET，成员是租约 id，score 是租约到期时刻。
- 「一路播放」= 一个租约：租约 id 由（流身份，条目 guid）决定，身份口径复用
  api._stream_client_identity（设备优先）。同一次播放的每个 Range 分片请求
  都刷新同一租约，不重复占位；不同设备/条目是不同租约。
- 没有可靠的「播放结束」信号（客户端 Stopped 上报可有可无、且晚于出流），
  所以灭点靠租约 TTL 到期收敛；Stopped 上报时尽力提前释放（见 release）。
- 获取走一段 Lua 原子完成「清过期 → 已有租约刷新放行 → 未满占位 → 满则拒」，
  避免两进程并发时一起读到未满、一起放行。

策略来源（清单第 15 项：表接管 + 配置兼容期）：
- 首选 ``source_path_policies`` 表（FMBY 字段：mount_id / path_prefix /
  priority / max_concurrent_streams）；表里有行就用表行，表空才回落
  旧 system_configs 键 ``stream_path_limits``（JSON 数组，每项
  ``{"mount_id": 3, "prefix": "/电影", "max_streams": 2}``；prefix 与
  parse_mount_path 的 rel 同口径（带前导 /），"/" 表示整个挂载）。
- 表读走进程内 60 秒 TTL 缓存（与 store.get_value 的 60 秒口径对齐；
  高频流请求每 Range 都调 acquire，不缓存就是每请求一次 DB 查询）。
  坏 JSON / 坏行一律按无策略放行并告警——配置写错不许打挂播放。
  旧配置仍可读（兼容期）；首批原样导入走 :func:`import_config_policies`
  （幂等，只补缺失行、不删行），导入触发点是管理端接线时的后续项。
- 租约键口径不变（``mount_id:prefix`` 摘要），只换策略来源；执法
  语义不变：命中策略全部生效（priority 只定返回顺序）。
- 租约 TTL 走 env ``STREAM_LIMIT_LEASE_TTL_SEC``（默认 600，钳 30–3600）。

fail-open：Redis 不可用或脚本异常时告警后放行。限流是保护网盘的护栏，不是
播放的前置依赖；Redis 抖一下就全站播不了是本末倒置。

已知取舍（租约模型的固有近似，写明不假装精确）：
- 302 直链发出后播放器直连网盘、不再回源，租约只能靠 TTL 到期或 Stopped
  释放，路数在 TTL 内偏保守（多算）；
- 同一设备同条目多开一个标签页仍是同一租约（少算）；
- 暂停超过 TTL 后恢复按新一路重新占位，作用域已满时恢复会被拒。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: system_configs 里的策略键（JSON 数组，格式见模块 docstring）。
POLICIES_KEY = "stream_path_limits"

#: 租约存活秒数：播放中的流请求会不断刷新；停播后最多多占这么久。
LEASE_TTL_SEC = env_int("STREAM_LIMIT_LEASE_TTL_SEC", 600, 30, 3600)

_KEY_PREFIX = "aetrix:streamlim:"

# 原子获取租约：清过期 → 已有租约刷新放行 → 未满占位放行 → 满返回 0。
# KEYS[1]=作用域 ZSET；ARGV=now, ttl, lease_id, max_streams。
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZSCORE', KEYS[1], ARGV[3]) then
  redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[2]), ARGV[3])
  return 1
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then
  return 0
end
redis.call('ZADD', KEYS[1], tonumber(ARGV[1]) + tonumber(ARGV[2]), ARGV[3])
return 1
"""


@dataclass(frozen=True)
class StreamPathPolicy:
    mount_id: int
    prefix: str          # 规范化后：带前导 /、无尾随 /（根为 "/"）
    max_streams: int


def _normalize_prefix(raw: object) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    prefix = raw.strip()
    if not prefix:
        return None
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if len(prefix) > 1:
        prefix = prefix.rstrip("/")
    return prefix or "/"


def parse_policies(raw: object) -> list[StreamPathPolicy]:
    """解析策略 JSON；坏数据一律降级（坏行跳过、整体坏按无策略），永不抛。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — 配置坏了按无策略，不许打挂播放
            logger.warning("stream_path_limits 不是合法 JSON，按无策略处理")
            return []
    if not isinstance(data, list):
        logger.warning("stream_path_limits 应为数组，按无策略处理")
        return []
    policies: list[StreamPathPolicy] = []
    for row in data:
        try:
            if not isinstance(row, dict):
                continue
            mount_id = int(row.get("mount_id"))
            max_streams = int(row.get("max_streams"))
            prefix = _normalize_prefix(row.get("prefix"))
            if prefix is None or max_streams < 1:
                continue
            policies.append(StreamPathPolicy(mount_id, prefix, max_streams))
        except (TypeError, ValueError):
            continue
    return policies


#: 表策略进程内缓存 TTL（秒）：与 store.get_value 的 60 秒口径对齐。
_TABLE_CACHE_TTL_SEC = 60.0
_table_cache: dict = {"at": 0.0, "rows": []}


def clear_table_policy_cache() -> None:
    """清表策略缓存（测试 / 管理端改表后立即生效用）。"""
    _table_cache["at"] = 0.0
    _table_cache["rows"] = []


def _load_table_policies(db) -> list[StreamPathPolicy]:
    """读 source_path_policies 表行（60 秒缓存）；表空返回 []。

    缓存只按时间失效，不按 db 会话区分——生产单库单真相，与 store
    的进程内缓存同一口径；测试里改表后须调 clear_table_policy_cache。
    """
    from backend.emby_server import models as em

    now = time.monotonic()
    if now - _table_cache["at"] < _TABLE_CACHE_TTL_SEC:
        return list(_table_cache["rows"])
    rows = (db.query(em.SourcePathPolicy)
            .order_by(em.SourcePathPolicy.priority.desc(),
                      em.SourcePathPolicy.path_prefix)
            .all())
    policies = []
    for row in rows:
        prefix = _normalize_prefix(row.path_prefix)
        try:
            max_streams = int(row.max_concurrent_streams)
        except (TypeError, ValueError):
            continue
        if prefix is None or max_streams < 1:
            continue
        policies.append(StreamPathPolicy(int(row.mount_id), prefix,
                                         max_streams))
    _table_cache["at"] = now
    _table_cache["rows"] = policies
    return list(policies)


def import_config_policies(db) -> dict:
    """把旧 system_configs 策略原样导入成表行（幂等）。

    只补（mount_id, prefix）缺失的行，不删、不改已有行；返回
    ``{"imported": n, "skipped": m}``。坏配置行按 parse_policies
    口径跳过。导入本身不切换策略来源——表里有行后下一次
    load_policies 自然用表行（见 load_policies）。
    """
    from backend.emby_server import models as em
    from backend.integrations import store

    imported = 0
    skipped = 0
    for policy in parse_policies(store.get_value(db, POLICIES_KEY, "")):
        existing = (db.query(em.SourcePathPolicy)
                    .filter(em.SourcePathPolicy.mount_id == policy.mount_id,
                            em.SourcePathPolicy.path_prefix == policy.prefix)
                    .first())
        if existing is not None:
            skipped += 1
            continue
        db.add(em.SourcePathPolicy(
            mount_id=policy.mount_id, path_prefix=policy.prefix,
            priority=0, max_concurrent_streams=policy.max_streams))
        imported += 1
    if imported:
        db.commit()
        clear_table_policy_cache()
    return {"imported": imported, "skipped": skipped}


def load_policies(db) -> list[StreamPathPolicy]:
    """策略来源：表优先、配置回落（清单第 15 项兼容期口径）。

    表里有行 → 用表行（不再读配置）；表空 → 经 store.get_value
    热读旧配置；表读异常 → 告警后回落旧配置（fail-open，与本模块
    一贯取舍一致：限流是护栏不是前置依赖）。
    """
    from backend.integrations import store

    try:
        table_policies = _load_table_policies(db)
    except Exception:  # noqa: BLE001 — 表读失败回落配置，不许打挂播放
        logger.warning("source_path_policies 表读失败，回落旧配置",
                       exc_info=True)
        table_policies = []
    if table_policies:
        return table_policies
    return parse_policies(store.get_value(db, POLICIES_KEY, ""))


def _prefix_matches(prefix: str, rel: str) -> bool:
    if prefix == "/":
        return True
    return rel == prefix or rel.startswith(prefix + "/")


def match_policies(policies: list[StreamPathPolicy], mount_id: int,
                   rel: str) -> list[StreamPathPolicy]:
    return [p for p in policies
            if p.mount_id == mount_id and _prefix_matches(p.prefix, rel)]


def _scope_key(policy: StreamPathPolicy) -> str:
    digest = hashlib.sha1(policy.prefix.encode("utf-8")).hexdigest()[:12]
    return f"{_KEY_PREFIX}{policy.mount_id}:{digest}"


def _lease_id(identity: str, item_key: str) -> str:
    raw = f"{identity}\x1f{item_key}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:32]


def _redis():
    from backend.database import get_redis

    return get_redis()


def _acquire_one(client, policy: StreamPathPolicy, lease_id: str) -> bool:
    result = client.eval(
        _ACQUIRE_LUA, 1, _scope_key(policy),
        time.time(), LEASE_TTL_SEC, lease_id, policy.max_streams)
    return bool(int(result))


def _release_from(client, policy: StreamPathPolicy, lease_id: str) -> None:
    client.zrem(_scope_key(policy), lease_id)


def acquire(db, identity: str, item_key: str,
            file_path: Optional[str]) -> Optional[str]:
    """为一次流请求占/刷新租约。放行返回 None；超限返回人话拒绝原因。

    Redis 异常一律 fail-open（告警后放行，见模块 docstring）。
    非挂载路径 / 无命中策略时不碰 Redis、直接放行（默认零行为变化）。
    """
    from backend.emby_server import mounts as mount_lib

    try:
        parsed = mount_lib.parse_mount_path(file_path)
        if not parsed:
            return None
        mount_id, rel = parsed
        # 配置读取也在 fail-open 内：读策略失败（库异常等）不许打挂播放。
        matched = match_policies(load_policies(db), mount_id, rel)
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("stream-limit 读策略失败，本次放行", exc_info=True)
        return None
    if not matched:
        return None
    try:
        client = _redis()
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("stream-limit 取 Redis 失败，本次放行", exc_info=True)
        return None
    if client is None:
        logger.warning("stream-limit Redis 不可用，本次放行")
        return None
    lease_id = _lease_id(identity, item_key)
    acquired: list[StreamPathPolicy] = []
    try:
        for policy in matched:
            if _acquire_one(client, policy, lease_id):
                acquired.append(policy)
                continue
            # 被这一条拒：回滚本次已在其他作用域占的位，别留半截租约。
            for done in acquired:
                try:
                    _release_from(client, done, lease_id)
                except Exception:  # noqa: BLE001 — 回滚尽力，TTL 兜底
                    pass
            return (f"目录「{policy.prefix}」的同时播放已达上限"
                    f"（{policy.max_streams} 路），请稍后再试")
    except Exception:  # noqa: BLE001 — fail-open
        logger.warning("stream-limit 计数异常，本次放行", exc_info=True)
        return None
    return None


def release(db, identity: str, item_key: str,
            file_path: Optional[str]) -> None:
    """尽力提前释放租约（客户端 Stopped 上报时调）。永不抛。"""
    try:
        from backend.emby_server import mounts as mount_lib

        parsed = mount_lib.parse_mount_path(file_path)
        if not parsed:
            return
        mount_id, rel = parsed
        matched = match_policies(load_policies(db), mount_id, rel)
        if not matched:
            return
        client = _redis()
        if client is None:
            return
        lease_id = _lease_id(identity, item_key)
        for policy in matched:
            try:
                _release_from(client, policy, lease_id)
            except Exception:  # noqa: BLE001 — 释放尽力，TTL 兜底
                pass
    except Exception:  # noqa: BLE001 — 释放永不打断 Stopped 主流程
        logger.debug("stream-limit 释放租约失败", exc_info=True)

"""配额方案 1：共享 Drive 起播限速 + 错峰。

只做这一件事：总闸默认关，生产行为与未接本模块时一致。开闸后，
还没有用户自己副本的共享 Drive 起播受三道软限制：

- 全局同时起播数（租约，刷新不算新起播）；
- 同一用户两次新起播之间的冷却（错峰）；
- 可选的单个共享文件同时起播数（0 = 这一道关着）。

已知的 Quota 额度页 / Drive 429 上，若用户还没有自己的副本，
返回 409，请客户端先转存再播，不再把同一条共享链 302 出去。

不做的事（明确拒绝，不要在本模块里补）：多服务账号轮换、
Cookie 池、预热秒传池、播放中层缓存。转存仍只调用既有门禁，
本模块不创建任务、不改 transfer_variant 的拷贝逻辑。

配置口径与 integrations.store 的热读一致：环境变量（非空）
优先于 system_configs，再缺省到下面的保守默认值。读失败
一律当总闸关，不挡播放。

计数：进程内 ``rate_limit_hit`` / ``quota_forced_transfer``，
另打一条 info 日志。Redis 可用时租约跨进程；不可用或计数
失败时回落本进程内存，再失败则本次放行（限流是护栏，不是
播放的前置依赖）。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException

logger = logging.getLogger(__name__)

#: system_configs 键。环境变量是同名大写。总闸默认关。
KEY_ENABLED = "shared_quota_pace_enabled"
KEY_GLOBAL = "shared_quota_pace_global_concurrency"
KEY_COOLDOWN = "shared_quota_pace_user_cooldown_sec"
KEY_PER_FILE = "shared_quota_pace_per_file_concurrency"
KEY_LEASE = "shared_quota_pace_lease_sec"

#: 变量名以 _ENV 结尾，env.example 对齐测试才能静态扫到这些键。
ENABLED_ENV = "SHARED_QUOTA_PACE_ENABLED"
GLOBAL_CAP_ENV = "SHARED_QUOTA_PACE_GLOBAL_CONCURRENCY"
USER_COOLDOWN_ENV = "SHARED_QUOTA_PACE_USER_COOLDOWN_SEC"
PER_FILE_ENV = "SHARED_QUOTA_PACE_PER_FILE_CONCURRENCY"
LEASE_ENV = "SHARED_QUOTA_PACE_LEASE_SEC"

_ENV_BY_KEY = {
    KEY_ENABLED: ENABLED_ENV,
    KEY_GLOBAL: GLOBAL_CAP_ENV,
    KEY_COOLDOWN: USER_COOLDOWN_ENV,
    KEY_PER_FILE: PER_FILE_ENV,
    KEY_LEASE: LEASE_ENV,
}

#: 总闸关时这些数字不生效。开闸后的缺省偏保守，运维可调高或填 0 关掉某一道。
_DEFAULTS = {
    KEY_ENABLED: "0",
    KEY_GLOBAL: "8",
    KEY_COOLDOWN: "3",
    KEY_PER_FILE: "2",
    KEY_LEASE: "120",
}

#: 稳定错误码（detail 前缀与 X-Error-Code 头，两处相同）。
CODE_GLOBAL = "shared_quota_pace_global"
CODE_USER_COOLDOWN = "shared_quota_pace_user_cooldown"
CODE_PER_FILE = "shared_quota_pace_per_file"
CODE_TRANSFER_REQUIRED = "shared_quota_transfer_required"

_MESSAGES = {
    CODE_GLOBAL: "共享播放同时起播过多，请稍后再试",
    CODE_USER_COOLDOWN: "共享源起播过于频繁，请稍后再试",
    CODE_PER_FILE: "该共享文件同时播放已达上限，请稍后再试",
    CODE_TRANSFER_REQUIRED: "共享源额度已耗尽，请先转存到自己的盘后再播",
}
_STATUS = {
    CODE_GLOBAL: 429,
    CODE_USER_COOLDOWN: 429,
    CODE_PER_FILE: 429,
    CODE_TRANSFER_REQUIRED: 409,
}

#: files.get 的 503 文案里嵌着「返回 429」。只认独立的 429，不认 1429。
_RE_429 = re.compile(r"(?<!\d)429(?!\d)")

_COUNTER_NAMES = ("rate_limit_hit", "quota_forced_transfer")
_counters = {name: 0 for name in _COUNTER_NAMES}
_counter_lock = threading.Lock()

_GLOBAL_REDIS_KEY = "aetrix:sqpace:g"

# 原子起播：清过期 → 已有租约刷新放行 → 冷却 → 全局 → 单文件 → 占位。
# KEYS: 全局 ZSET、该文件 ZSET、该用户冷却键。
# ARGV: now, lease_ttl, cooldown, global_cap, file_cap, member。
# 返回 {1|0, code_or_refresh, retry_after_sec}。
_ADMIT_LUA = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local cooldown = tonumber(ARGV[3])
local global_cap = tonumber(ARGV[4])
local file_cap = tonumber(ARGV[5])
local member = ARGV[6]

local function wait_from(key)
  local head = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local wait = ttl
  if head[2] ~= nil then
    wait = tonumber(head[2]) - now
  end
  if wait < 1 then
    wait = 1
  end
  return math.ceil(wait)
end

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZSCORE', KEYS[1], member) then
  local exp = now + ttl
  redis.call('ZADD', KEYS[1], exp, member)
  redis.call('ZADD', KEYS[2], exp, member)
  redis.call('EXPIRE', KEYS[1], math.ceil(ttl) + 30)
  redis.call('EXPIRE', KEYS[2], math.ceil(ttl) + 30)
  return {1, 'refresh', 0}
end
if cooldown > 0 then
  local left = redis.call('TTL', KEYS[3])
  if left and left > 0 then
    return {0, 'shared_quota_pace_user_cooldown', left}
  end
end
if global_cap > 0 and redis.call('ZCARD', KEYS[1]) >= global_cap then
  return {0, 'shared_quota_pace_global', wait_from(KEYS[1])}
end
if file_cap > 0 and redis.call('ZCARD', KEYS[2]) >= file_cap then
  return {0, 'shared_quota_pace_per_file', wait_from(KEYS[2])}
end
local exp = now + ttl
redis.call('ZADD', KEYS[1], exp, member)
redis.call('ZADD', KEYS[2], exp, member)
redis.call('EXPIRE', KEYS[1], math.ceil(ttl) + 30)
redis.call('EXPIRE', KEYS[2], math.ceil(ttl) + 30)
if cooldown > 0 then
  redis.call('SET', KEYS[3], '1', 'EX', math.ceil(cooldown))
end
return {1, 'ok', 0}
"""


@dataclass(frozen=True)
class PaceConfig:
    enabled: bool
    global_cap: int
    user_cooldown_sec: float
    per_file_cap: int
    lease_sec: float


def _now() -> float:
    return time.time()


def _bump(name: str) -> None:
    if name not in _counters:
        return
    with _counter_lock:
        _counters[name] += 1


def counters() -> dict:
    """进程内计数快照（重启清零）。"""
    with _counter_lock:
        return dict(_counters)


def reset_for_tests() -> None:
    """清计数与本进程租约（单测隔离）。不碰 Redis。"""
    with _counter_lock:
        for name in _counters:
            _counters[name] = 0
    _memory.reset()


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("shared_quota_pace 配置 %r 不是整数，回落 %s", raw, default)
        return default
    return max(lo, min(hi, value))


def _pick_str(db, key: str) -> str:
    """环境变量非空优先，否则 system_configs 热读，再否则代码缺省。"""
    env_name = _ENV_BY_KEY[key]
    raw = os.getenv(env_name)
    if raw is not None and raw.strip() != "":
        return raw.strip()
    if db is not None:
        try:
            from backend.integrations import store

            val = store.get_value(db, key, "")
        except Exception:  # noqa: BLE001 — 读失败回缺省，总闸因此保持关
            logger.warning("shared_quota_pace 读配置失败 key=%s", key, exc_info=True)
            val = ""
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return _DEFAULTS[key]


def load_config(db) -> PaceConfig:
    """有效配置。任何异常都回「总闸关」，不挡播放。

    总闸关时不再读其余键：播放热路径在默认关闭时只多一次热读。
    """
    try:
        enabled = _as_bool(_pick_str(db, KEY_ENABLED))
        if not enabled:
            return PaceConfig(False, 8, 3.0, 2, 120.0)
        global_cap = _as_int(_pick_str(db, KEY_GLOBAL), 8, 0, 100_000)
        cooldown = _as_int(_pick_str(db, KEY_COOLDOWN), 3, 0, 3600)
        per_file = _as_int(_pick_str(db, KEY_PER_FILE), 2, 0, 100_000)
        lease = _as_int(_pick_str(db, KEY_LEASE), 120, 30, 3600)
    except Exception:  # noqa: BLE001
        logger.warning("shared_quota_pace 配置解析失败，总闸按关处理", exc_info=True)
        return PaceConfig(False, 8, 3, 2, 120)
    return PaceConfig(enabled, global_cap, float(cooldown), per_file, float(lease))


def is_personal_copy(play_variant) -> bool:
    """用户自己的转存副本（owner_scope=user）。服主副本仍算共享源。"""
    if play_variant is None:
        return False
    scope = (getattr(play_variant, "owner_scope", "") or "").strip()
    return scope == "user"


def is_shared_drive_start(item, requested_variant, play_variant) -> bool:
    """这次取流会打到共享 Drive，而不是用户自己的盘或自己的副本。"""
    from backend.emby_server import play_variants

    fid = (getattr(item, "drive_file_id", "") or "").strip()
    if not fid:
        return False
    path = (getattr(item, "file_path", "") or "").strip()
    if path.startswith("byos://"):
        return False
    if requested_variant in (
            play_variants.BYOS_DRIVE_RELAY, play_variants.BYOS115_DIRECT):
        return False
    if is_personal_copy(play_variant):
        return False
    return True


def _http(code: str, retry_after: int | None = None) -> HTTPException:
    headers = {"X-Error-Code": code}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    return HTTPException(
        status_code=_STATUS[code],
        detail=f"{code}: {_MESSAGES[code]}",
        headers=headers,
    )


def is_known_drive_quota_http(exc) -> bool:
    """既有检测路径上的 Quota/429，不新造判断。

    - 中继把上游 429 原样抛出（detail 形如「源站返回 429」）；
    - files.get 的 429 被收成 503，detail 含「返回 429」。
    普通 403 / 502 / 不含 429 的 503 不认。
    """
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", "")
    if isinstance(detail, dict):
        return False
    text = "" if detail is None else str(detail)
    if status == 429:
        return True
    # files.get 把 429 收成 503，detail 形如「媒体来源暂不可用：files.get 返回 429」。
    if status == 503 and _RE_429.search(text):
        return True
    return False


def transfer_required_http(db, *, personal_copy: bool = False):
    """总闸开且没有用户副本时，返回「先转存再播」的 409。

    总闸关、或已经有用户副本：返回 None，调用方保持原行为
    （额度页回落 302 / 429 原样抛）。
    """
    if personal_copy:
        return None
    cfg = load_config(db)
    if not cfg.enabled:
        return None
    _bump("quota_forced_transfer")
    logger.info("shared_quota_pace quota_forced_transfer code=%s",
                CODE_TRANSFER_REQUIRED)
    return _http(CODE_TRANSFER_REQUIRED)


def _user_key(user) -> str:
    uid = getattr(user, "id", None)
    if uid is None:
        return "anon"
    return str(uid)


def _digest(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:n]


def _member(user_key: str, file_id: str) -> str:
    return _digest(f"{user_key}\x1f{file_id}", 32)


class _MemoryStore:
    """单进程租约。与 Redis Lua 同一顺序：刷新 → 冷却 → 全局 → 单文件。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, float] = {}
        self._file_of: dict[str, str] = {}
        self._files: dict[str, set[str]] = {}
        self._user_until: dict[str, float] = {}

    def reset(self) -> None:
        with self._lock:
            self._leases.clear()
            self._file_of.clear()
            self._files.clear()
            self._user_until.clear()

    def _expire(self, now: float) -> None:
        dead = [m for m, exp in self._leases.items() if exp <= now]
        for member in dead:
            self._leases.pop(member, None)
            file_key = self._file_of.pop(member, None)
            if file_key is None:
                continue
            bucket = self._files.get(file_key)
            if not bucket:
                continue
            bucket.discard(member)
            if not bucket:
                self._files.pop(file_key, None)

    def _soonest(self, now: float, members: set[str] | None, lease_sec: float) -> int:
        times = []
        if members is None:
            times = list(self._leases.values())
        else:
            for member in members:
                exp = self._leases.get(member)
                if exp is not None:
                    times.append(exp)
        if not times:
            wait = lease_sec
        else:
            wait = min(times) - now
        if wait < 1:
            wait = 1
        return int(math.ceil(wait))

    def admit(self, cfg: PaceConfig, user_key: str, file_id: str, now: float):
        member = _member(user_key, file_id)
        with self._lock:
            self._expire(now)
            if member in self._leases:
                exp = now + cfg.lease_sec
                self._leases[member] = exp
                return None
            if cfg.user_cooldown_sec > 0:
                until = self._user_until.get(user_key)
                if until is not None and until > now:
                    retry = max(1, int(math.ceil(until - now)))
                    return _http(CODE_USER_COOLDOWN, retry)
            if cfg.global_cap > 0 and len(self._leases) >= cfg.global_cap:
                return _http(CODE_GLOBAL, self._soonest(now, None, cfg.lease_sec))
            bucket = self._files.get(file_id, set())
            if cfg.per_file_cap > 0 and len(bucket) >= cfg.per_file_cap:
                return _http(
                    CODE_PER_FILE, self._soonest(now, bucket, cfg.lease_sec))
            exp = now + cfg.lease_sec
            self._leases[member] = exp
            self._file_of[member] = file_id
            self._files.setdefault(file_id, set()).add(member)
            if cfg.user_cooldown_sec > 0:
                self._user_until[user_key] = now + cfg.user_cooldown_sec
            return None


_memory = _MemoryStore()


def _redis_client():
    try:
        from backend.database import get_redis
        return get_redis()
    except Exception:  # noqa: BLE001
        return None


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _admit_redis(client, cfg: PaceConfig, user_key: str, file_id: str, now: float):
    member = _member(user_key, file_id)
    file_key = f"aetrix:sqpace:f:{_digest(file_id)}"
    user_redis_key = f"aetrix:sqpace:u:{_digest(user_key)}"
    result = client.eval(
        _ADMIT_LUA, 3,
        _GLOBAL_REDIS_KEY, file_key, user_redis_key,
        f"{now:.6f}", f"{cfg.lease_sec:.3f}", f"{cfg.user_cooldown_sec:.3f}",
        str(int(cfg.global_cap)), str(int(cfg.per_file_cap)), member,
    )
    ok = int(result[0])
    if ok:
        return None
    code = str(_decode(result[1]))
    retry = int(result[2] or 1)
    if code not in _STATUS or code == CODE_TRANSFER_REQUIRED:
        raise RuntimeError(f"shared_quota_pace unexpected code {code}")
    return _http(code, max(1, retry))


def admit_shared_start(db, user, file_id: str):
    """共享 Drive 起播占一个租约。放行返回 None；超限返回 429 HTTPException。

    总闸关或 file_id 为空：不计数，返回 None。同一用户同一文件在
    租约内再次取流（Range）只刷新，不占新名额、不触发冷却。
    """
    cfg = load_config(db)
    if not cfg.enabled:
        return None
    file_id = (file_id or "").strip()
    if not file_id:
        return None
    user_key = _user_key(user)
    now = _now()
    denied = None
    client = _redis_client()
    if client is not None:
        try:
            denied = _admit_redis(client, cfg, user_key, file_id, now)
        except Exception:  # noqa: BLE001 — Redis 抖一下回落本进程，再失败则放行
            logger.warning("shared_quota_pace Redis 计数失败，回落本进程",
                           exc_info=True)
            client = None
    if client is None:
        try:
            denied = _memory.admit(cfg, user_key, file_id, now)
        except Exception:  # noqa: BLE001
            logger.warning("shared_quota_pace 本进程计数失败，本次放行",
                           exc_info=True)
            return None
    if denied is None:
        return None
    _bump("rate_limit_hit")
    code = ""
    headers = getattr(denied, "headers", None) or {}
    code = headers.get("X-Error-Code", "")
    logger.info(
        "shared_quota_pace rate_limit_hit code=%s user=%s file=%s",
        code, user_key, file_id[:16])
    return denied

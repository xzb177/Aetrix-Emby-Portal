"""多源密钥池：与 TMDB 同一套语义，按源各管各的

## 与 TMDB 那套的关系

TMDB 在 v6a 之后已经有一份等价实现（``tmdb.TmdbClient`` 内置）。这里是**通用版**，
给其余 6 个源用：它们各自的密钥池、冷却状态互不相干（一家限流不该影响另一家）。

## 三条规则（和 TMDB 一致，不做两套口径）

1. **轮转起点**：不会总从第一把开始，避免每次都是它先死；
2. **冷却不是锁死**：全池都在冷却时仍然发请求 —— 宁可撞限流也不能不发；
3. **冷却是进程内运行时状态**：写进库会变成「重启后还记着某把 key 坏过」这种
   陈旧结论，新进程应该重新试一遍。
"""
from __future__ import annotations

import threading
import time
from typing import Optional

DEFAULT_COOLDOWN_SEC = 900.0            # 15 分钟：配额窗口一般几分钟
DEFAULT_INVALID_COOLDOWN_SEC = 21600.0  # 6 小时：401 多半是 key 废了


class KeyPool:
    """一个源的密钥池：轮换 + 逐把冷却（线程安全）"""

    def __init__(self, source_id: str, keys: list[str], *,
                 cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
                 invalid_cooldown_sec: float = DEFAULT_INVALID_COOLDOWN_SEC):
        self.source_id = source_id
        self._lock = threading.RLock()
        self._keys = list(keys or [])
        self._index = 0
        self._cooldown: dict[str, dict] = {}
        self._cooldown_sec = max(1.0, float(cooldown_sec))
        self._invalid_cooldown_sec = max(1.0, float(invalid_cooldown_sec))

    # ---------------- 基本状态 ----------------

    @property
    def keys(self) -> list[str]:
        with self._lock:
            return list(self._keys)

    @property
    def usable(self) -> bool:
        with self._lock:
            return bool(self._keys)

    def resize(self, keys: list[str]) -> None:
        """换掉整池（后台增删后调用），并丢掉已不在池子里的冷却记录"""
        with self._lock:
            alive = set(keys or [])
            self._keys = list(alive and keys or [])
            self._index = 0
            self._cooldown = {k: v for k, v in self._cooldown.items() if k in alive}

    def _map(self) -> dict:
        data = getattr(self, "_cooldown", None)
        if data is None:
            data = {}
            self._cooldown = data
        return data

    # ---------------- 取用 ----------------

    def pick(self, exclude=(), *, allow_cooled: bool = False) -> str:
        """挑一把能用的 key（``exclude`` 是本次调用已经试过的）"""
        with self._lock:
            if not self._keys:
                return ""
            excluded = set(exclude)
            now = time.monotonic()
            total = len(self._keys)
            for step in range(total):
                idx = (self._index + step) % total
                key = self._keys[idx]
                if key in excluded:
                    continue
                entry = self._map().get(key)
                if entry and entry.get("until", 0) > now and not allow_cooled:
                    continue
                self._index = (idx + 1) % total
                return key
            return ""

    def pick_any(self, exclude=()) -> str:
        """先跳冷却的，全冷却时退而用一把（冷却是优化，不是锁死）"""
        return self.pick(exclude) or self.pick(exclude, allow_cooled=True)

    def note_throttled(self, key: str, retry_after: Optional[float] = None,
                       cap: float = 30.0) -> float:
        """429：按 Retry-After 冷却这把，返回实际冷却秒数"""
        wait = float(retry_after) if retry_after and retry_after > 0 else 0.0
        if wait <= 0:
            wait = self._cooldown_sec
        wait = min(wait, max(1.0, cap)) if cap else wait
        self._cool(key, "限流（HTTP 429）", wait)
        return wait

    def note_invalid(self, key: str) -> None:
        """401：这把 key 基本废了，冷却更久"""
        self._cool(key, "无效（HTTP 401）", self._invalid_cooldown_sec)


    def _cool(self, key: str, reason: str, seconds: float) -> None:
        with self._lock:
            entry = self._map().setdefault(key, {"until": 0.0, "reason": "", "hits": 0})
            entry["until"] = max(float(entry.get("until") or 0), time.monotonic() + seconds)
            entry["reason"] = reason
            entry["hits"] = int(entry.get("hits") or 0) + 1

    def clear(self) -> int:
        with self._lock:
            count = len(self._map())
            self._map().clear()
            return count

    def status(self) -> list[dict]:
        """逐把状态（**只给掩码**，密钥原文不出这个函数）"""
        now = time.monotonic()
        with self._lock:
            rows = []
            for idx, key in enumerate(self._keys):
                entry = dict(self._map().get(key) or {})
                remaining = max(0.0, float(entry.get("until") or 0) - now)
                rows.append({
                    "index": idx + 1,
                    "masked": mask(key),
                    "cooling": remaining > 0,
                    "cooldown_remaining": int(round(remaining)),
                    "reason": entry.get("reason") or "",
                    "hits": int(entry.get("hits") or 0),
                })
        return rows

    def cooling_count(self) -> int:
        now = time.monotonic()
        with self._lock:
            return len([e for e in self._map().values() if e.get("until", 0) > now])


def mask(key: str) -> str:
    """掩码：只露后 4 位（与 TMDB 那套同一口径）"""
    return f"****{key[-4:]}" if len(key or "") > 4 else "****"


_POOLS: dict[str, KeyPool] = {}
_POOL_LOCK = threading.Lock()


def pool_for(source_id: str, keys: list[str], *, cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
             invalid_cooldown_sec: float = DEFAULT_INVALID_COOLDOWN_SEC) -> KeyPool:
    """按源取密钥池；**首次**创建，之后复用（冷却状态要跨调用活着）"""
    with _POOL_LOCK:
        pool = _POOLS.get(source_id)
        if pool is None:
            pool = KeyPool(source_id, keys, cooldown_sec=cooldown_sec,
                           invalid_cooldown_sec=invalid_cooldown_sec)
            _POOLS[source_id] = pool
        elif keys != pool.keys:
            pool.resize(keys)
        return pool


def reset_all() -> None:
    """测试用：丢掉全部池（冷却状态也一起清）"""
    with _POOL_LOCK:
        _POOLS.clear()


class RateGate:
    """每源一个最小请求间隔（秒）。0 = 不限速。

    与 TMDB 的令牌桶不同：这些站没有配额文档，只能**按站点礼仪**保守限速；
    Bangumi / TVmaze / AniList 都明确要求不要打太快。
    """

    def __init__(self, source_id: str, min_interval: float):
        self.source_id = source_id
        self._min = max(0.0, float(min_interval or 0))
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self, sleep=time.sleep) -> None:
        if self._min <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._min - (now - self._last)
            if wait > 0:
                sleep(wait)
                now = time.monotonic()
            self._last = now


__all__ = [
    "DEFAULT_COOLDOWN_SEC", "DEFAULT_INVALID_COOLDOWN_SEC",
    "KeyPool", "RateGate", "mask", "pool_for", "reset_all",
]

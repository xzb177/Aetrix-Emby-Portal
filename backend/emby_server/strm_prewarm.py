"""strm 播放链路预热（PlaybackInfo 阶段提前暖 Drive 连接）。

**场景**：用户点播放后，客户端先调 PlaybackInfo 拿播放地址，0.3~2 秒后
才请求 /stream。/stream 的最大耗时是"现去 Drive 建连 + 等首字节"
（悉尼→Google 跨洋 300~700ms）。本模块在 PlaybackInfo 阶段就提前发一个
``Range: bytes=0-1`` 请求把链路暖好（TLS/TCP、307 跳转、Drive 热路径），
收到响应头就关，不读 body。

**为什么不用 playback_prewarm**：那个预热的是本地文件头（读 10MB），
.strm 文件本身只有 ~100 字节，瓶颈在 Drive 网络不在本地，所以对 .strm
链路完全无效。本模块暖的是"到 Drive 的网络链路"。

**不抢资源 / 不触发风控的设计**：

- **有界并发**：``asyncio.Semaphore`` 上限默认 4，拿不到信号量直接跳过不排队。
- **去重**：同一文件 300 秒内只预热一次。
- **抖动**：每次预热前随机延迟 0.05~0.15 秒，避免规律化请求。
- **失败静默**：任何异常只记 debug 日志，绝不影响 PlaybackInfo 主流程。
- **配额影响**：每次预热 = 1 次 Drive API 调用（去重 300s），可忽略。

所有阈值可配置（SystemConfig，修改后 60 秒内生效）：

- ``strm_prewarm_enabled``（bool，默认 true）：总开关，关掉即回滚。
- ``strm_prewarm_dedup_seconds``（int，默认 300）：同文件去重窗口。
- ``strm_prewarm_max_concurrent``（int，默认 4，范围 1~16）：并发上限。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

KEY_ENABLED = "strm_prewarm_enabled"
KEY_DEDUP_SECONDS = "strm_prewarm_dedup_seconds"
KEY_MAX_CONCURRENT = "strm_prewarm_max_concurrent"

CONFIG_KEYS = (KEY_ENABLED, KEY_DEDUP_SECONDS, KEY_MAX_CONCURRENT)

DEFAULTS: dict[str, str] = {
    KEY_ENABLED: "true",
    KEY_DEDUP_SECONDS: "300",
    KEY_MAX_CONCURRENT: "4",
}

_BOUNDS: dict[str, tuple[int, int]] = {
    KEY_DEDUP_SECONDS: (0, 86400),
    KEY_MAX_CONCURRENT: (1, 16),
}


def _parse_bool(raw: str, default: bool) -> bool:
    text = (raw or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _parse_int(raw: str, key: str) -> int:
    default = int(DEFAULTS[key])
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    lo, hi = _BOUNDS[key]
    return max(lo, min(hi, value))


def read_config(db) -> dict[str, Any]:
    """读一份类型化配置（热缓存读；非法值钳回合法区间）。"""
    from backend.integrations import store

    raw = store.get_values(db, CONFIG_KEYS, DEFAULTS)
    return {
        "enabled": _parse_bool(raw[KEY_ENABLED], True),
        "dedup_seconds": _parse_int(raw[KEY_DEDUP_SECONDS], KEY_DEDUP_SECONDS),
        "max_concurrent": _parse_int(raw[KEY_MAX_CONCURRENT], KEY_MAX_CONCURRENT),
    }


# ---------------------------------------------------------------------------
# 预热器
# ---------------------------------------------------------------------------

#: 模块级状态（asyncio 单事件循环，无需 threading.Lock）
_sem: Optional[asyncio.Semaphore] = None
_sem_size: int = 0
_last_warm: dict[str, float] = {}          # path -> 上次预热的 monotonic
_counters: dict[str, int] = {
    "triggered": 0,
    "warmed": 0,
    "skipped_dedup": 0,
    "skipped_busy": 0,
    "skipped_disabled": 0,
    "failed": 0,
}


def _get_sem(n: int) -> asyncio.Semaphore:
    """按需取（或重建）指定大小的信号量。"""
    global _sem, _sem_size
    if _sem is None or _sem_size != n:
        _sem = asyncio.Semaphore(n)
        _sem_size = n
    return _sem


def _bump(key: str) -> None:
    _counters[key] = _counters.get(key, 0) + 1


async def _do_warm_request(url: str, headers: dict) -> bool:
    """发 Range: bytes=0-1 预热请求，收到响应头就关。返回是否成功。

    用 streaming 的共享 httpx 客户端（连接复用，暖的就是它的池子）。
    任何异常返回 False，不抛。
    """
    try:
        from backend.emby_server.streaming import get_relay_client

        client = get_relay_client()
        # get_relay_client 可能返回协程（懒建）：await 开
        if asyncio.iscoroutine(client):
            client = await client

        h = dict(headers or {})
        h["Range"] = "bytes=0-1"

        req = client.build_request("GET", url, headers=h)
        resp = await asyncio.wait_for(client.send(req, stream=True), timeout=10)
        try:
            return True  # 收到响应头即算暖好，不读 body
        finally:
            await resp.aclose()
    except Exception:
        return False


async def warm(db, item) -> None:
    """预热单个条目的 .strm 播放链路。永不抛异常。"""
    # 防风控抖动：避免规律化请求
    await asyncio.sleep(random.uniform(0.05, 0.15))

    try:
        from backend.emby_server.async_db import run_db

        file_path = getattr(item, "file_path", None)
        if not file_path or not str(file_path).lower().endswith(".strm"):
            return

        def _resolve(db, item):
            from backend.emby_server import mounts as mount_lib
            return mount_lib.resolve_play_target(item.file_path, db, item.library)

        target = await run_db(_resolve, db, item)

        if target is None or getattr(target, "kind", None) != "url":
            return

        ok = await _do_warm_request(target.value, getattr(target, "headers", {}))
        _bump("warmed" if ok else "failed")
    except Exception:
        logger.debug(
            "strm prewarm failed: guid=%s path=%s",
            getattr(item, "guid", None),
            getattr(item, "file_path", None),
            exc_info=True,
        )
        _bump("failed")


async def maybe_warm(db, item) -> None:
    """生产入口：读配置 → 去重/并发判断 → 起预热协程。永不抛异常。

    由 api.py 的 playback_info 在后台调用：
    ``asyncio.create_task(strm_prewarm.maybe_warm(db, item))``。
    注意：db session 只在 maybe_warm 内同步读配置时用，
    warm 内部用 run_db 新建 session，不跨协程复用。
    """
    try:
        _bump("triggered")
        cfg = read_config(db)
        if not cfg["enabled"]:
            _bump("skipped_disabled")
            return

        path = str(getattr(item, "file_path", "") or "")
        if not path:
            return

        # 去重：窗口内同一文件只预热一次
        now = time.monotonic()
        window = cfg["dedup_seconds"]
        if window > 0:
            last = _last_warm.get(path)
            if last is not None and now - last < window:
                _bump("skipped_dedup")
                return
            _last_warm[path] = now
            # 有界：防止长期运行泄漏
            if len(_last_warm) > 4096:
                cutoff = now - window
                for p in [p for p, ts in _last_warm.items() if ts < cutoff]:
                    _last_warm.pop(p, None)

        # 有界并发：拿不到信号量直接跳过，不排队
        sem = _get_sem(cfg["max_concurrent"])
        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            _bump("skipped_busy")
            return

        try:
            await warm(db, item)
        finally:
            sem.release()
    except Exception:
        logger.debug("strm prewarm maybe_warm 跳过", exc_info=True)


def metrics_snapshot() -> dict[str, Any]:
    """指标快照（给 /api/health 用；永不抛异常）。"""
    try:
        return {"counters": dict(_counters)}
    except Exception:
        return {"counters": {}}


def reset_state() -> None:
    """清空去重表与指标（单元测试隔离用）。"""
    global _sem, _sem_size
    _last_warm.clear()
    for key in _counters:
        _counters[key] = 0
    _sem = None
    _sem_size = 0

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

- ``strm_prewarm_enabled``（bool，默认 false）：总开关。默认关——预热会
  真实消耗 Drive 请求与单文件配额（2026-10-16 口径：预热全改可配开关、
  默认关），服主在管理后台开启后才生效。
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
    KEY_ENABLED: "false",
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
        "enabled": _parse_bool(raw[KEY_ENABLED], False),
        "dedup_seconds": _parse_int(raw[KEY_DEDUP_SECONDS], KEY_DEDUP_SECONDS),
        "max_concurrent": _parse_int(raw[KEY_MAX_CONCURRENT], KEY_MAX_CONCURRENT),
    }


def _quota_blocked(item) -> bool:
    """条目文件是否处于 Drive 单文件配额负缓存期（只读 #940 的护盾信号）。

    预热请求同样打 Drive：文件已确认配额超限时再预热纯属撞墙白烧请求量，
    一律跳过。信号不可用（非 Drive 条目/异常）时按未拦截处理，永不抛错。
    """
    fid = getattr(item, "drive_file_id", None)
    if not fid:
        return False
    try:
        from backend.emby_server.streaming import drive_quota_blocked_retry_after

        return drive_quota_blocked_retry_after(str(fid)) > 0
    except Exception:
        return False


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
    "skipped_non_strm": 0,
    "skipped_quota": 0,
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

        if _quota_blocked(item):
            _bump("skipped_quota")
            return

        def _resolve(db, item):
            from backend.emby_server import mounts as mount_lib
            return mount_lib.resolve_play_target(item.file_path, db, item.library)

        target = await run_db(_resolve, db, item)

        if target is None or getattr(target, "kind", None) != "url":
            return

        # 走完整跳转链：原始 URL → Drive API 地址 → 307 跳转到最终地址，
        # 和 serve_remote_async 完全相同的路径，并写 P0-2 跳转缓存。
        # 之前只请求原始 URL 不跟跳转，导致第一次播放现做跳转还是慢。
        from backend.emby_server.streaming import resolve_drive_final_url
        final_url = await resolve_drive_final_url(
            target.value, getattr(target, "headers", {}))
        if not final_url:
            _bump("failed")
            return

        ok = await _do_warm_request(final_url, getattr(target, "headers", {}))
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
    注意：传入的是给后台任务新建的独立 session（不是请求的 db）。
    maybe_warm 内同步读配置；warm 经 run_db 在线程池顺序复用该 session
    （await 期间不另碰，符合 async_db 纪律）。
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

        # P3-2：非 .strm 直接返回——不写去重表、不占信号量。
        # warm() 里也有后缀检查（双保险），但那时已在信号量内白等 50~150ms 抖动；
        # pr686 把触发点移出 IsPlayback 门控后，非 .strm 的 PlaybackInfo 很多，
        # 放行到信号量会挤掉真正的 .strm 预热（skipped_busy）。
        if not path.lower().endswith(".strm"):
            _bump("skipped_non_strm")
            return

        # 配额护盾：负缓存期内的文件跳过预热（不占去重窗口，配额恢复后可正常预热）
        if _quota_blocked(item):
            _bump("skipped_quota")
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

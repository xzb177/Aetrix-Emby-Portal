# -*- coding: utf-8 -*-
"""按需转码（弱网降码率）：码率档位 + 持久化缓存 + 并发硬限制

Jellyfin 式按需转码的最小可用实现：

- 触发：客户端请求转码（``video_hls`` 路由），不预转码。
- 档位：480p / 720p / 1080p 三档，按客户端请求的码率就近归档，
  只转需要的档，不转任意码率。
- 缓存：整片转完（ffmpeg 正常退出、且从 0 开始转）的输出落盘持久化
  （``EMBY_TRANSCODE_CACHE_DIR``，默认 ``/data/transcode_cache``），
  同一片同档位第二次直接复用，不再起 ffmpeg。
- 限流：同时最多 ``TRANSCODE_MAX_CONCURRENT`` 路（默认 2，VPS 性能有限），
  超了 503 让客户端降级直连。

转码命令的构造仍在 ``streaming.build_hls_command``（全项目只一套），
本模块只做档位 / 缓存 / 限流编排，不重复造 ffmpeg 命令。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime
from typing import Optional

from backend.emby_server.cpu_budget import background_workers as _cpu_workers
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# P1 修复（D-P1-1）：转码入场检查与启动之间的竞态。
# ensure_slot_or_503() 只做"检查时判定"，N 个并发请求可同时通过检查并全部 fork ffmpeg。
# 加模块级锁，把"检查→登记"做成原子"取号"，调用方必须用 transcode_slot() 上下文管理器。
import threading as _threading
_transcode_slot_lock = _threading.Lock()

# 2026-10 简化：删除服务端三档转码（480p/720p/1080p）。
# 转码参数由客户端在 PlaybackInfo/播放请求里指定，服务端只管"转不转、转几路"，
# 不再把客户端要的码率归档到固定档位（学 Linger 的"薄服务器"思路）。


def _streaming():
    """延迟导入 streaming，避免循环导入（streaming 的回收钩子反过来调本模块）。"""
    from backend.emby_server import streaming
    return streaming


def cache_dir() -> str:
    return os.getenv("EMBY_TRANSCODE_CACHE_DIR", "/data/transcode_cache")


def max_concurrent() -> int:
    """同时允许的转码路数：按 CPU 自适应（cpu_budget），未配置时按核数一半。

    转码是吃 CPU 大户，手动配置的值会被截断到核数-1，永远给 API/直传留一核。
    """
    return _cpu_workers("TRANSCODE_MAX_CONCURRENT")


def clamp_to_source(height: Optional[int], src_height: Optional[int]) -> Optional[int]:
    """源片分辨率低于请求时不做无意义的上采样。"""
    if height and src_height and height > src_height:
        return src_height
    return height


def cache_key(item_guid: str, video_bitrate: int, height: Optional[int],
              fingerprint: Optional[str] = None) -> str:
    """缓存键：同一片 + 同码率 + 同分辨率 + 同源文件指纹 → 同一份转码缓存。

    源文件被替换（指纹变化）时缓存自动失效，不会播出旧内容。
    """
    raw = f"{item_guid}|{video_bitrate}|{height or 0}|{fingerprint or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _cache_valid(path: str) -> bool:
    """缓存目录有效：有播放列表 + 至少一个分片。"""
    if not os.path.isdir(path):
        return False
    try:
        playlist = os.path.join(path, "master.m3u8")
        if not (os.path.isfile(playlist) and os.path.getsize(playlist) > 0):
            return False
        return any(name.endswith(".ts") for name in os.listdir(path))
    except OSError:
        # 判断与读取之间目录被删 / 挂载掉线：视为无效，不抛异常
        return False


def find_cache(item_guid: str, video_bitrate: int, height: Optional[int],
               fingerprint: Optional[str] = None) -> Optional[str]:
    """找可复用的转码缓存，命中返回缓存目录，否则 None。"""
    path = os.path.join(cache_dir(), cache_key(item_guid, video_bitrate, height, fingerprint))
    if _cache_valid(path):
        logger.info("转码缓存命中 %s %dbps %s", item_guid, video_bitrate, height)
        return path
    return None


def register_cache_session(cache_path: str, user_id: int, item_guid: str,
                           video_bitrate: int, height: Optional[int]) -> str:
    """把缓存目录注册成一个「无进程」的转码会话。

    复用 ``video_hls`` 已有的会话服务逻辑（播放列表重写 / 切片投递）；
    ``cached=True`` 让回收时跳过删目录（见 ``streaming._terminate_and_cleanup``），
    ``proc=None`` 表示没有 ffmpeg 进程在跑。
    """
    session_id = uuid.uuid4().hex[:16]
    _streaming()._TRANSCODE_PROCS[session_id] = {
        "proc": None,
        "dir": cache_path,
        "started": datetime.now(),
        "user_id": user_id,
        "item_guid": item_guid,
        "video_bitrate": video_bitrate,
        "height": height,
        "cached": True,
        "last_access": time.monotonic(),  # S6：闲置按客户端最后访问算
    }
    return session_id


def live_transcode_count() -> int:
    """当前正在跑（有 ffmpeg 进程）的转码路数。缓存复用不占路数。"""
    streaming = _streaming()
    count = 0
    for sid in streaming.active_transcode_ids():
        info = streaming.get_transcode(sid)
        if not info or info.get("cached"):
            continue
        proc = info.get("proc")
        if proc is not None and proc.poll() is None:
            count += 1
    return count


def ensure_slot_or_503() -> None:
    """转码入场检查：达到并发上限就 503，让客户端降级走直连。

    注意：本函数只做检查，不持有锁。调用方必须使用 transcode_slot()
    上下文管理器来保证"检查→启动"的原子性，否则仍有竞态。
    """
    limit = max_concurrent()
    running = live_transcode_count()
    if running >= limit:
        # S6：先把客户端已闲置（EMBY_TRANSCODE_IDLE 内没拉过播放列表/切片）的会话让出来，
        # 再判定。旧实现只在**起新转码之后**才回收，而这里先 503 了，回收永远轮不到。
        # 同步调用（terminate + wait），调用方须在线程里（见 api.video_hls 的准备阶段）。
        streaming = _streaming()
        if streaming.reap_idle_transcodes(streaming.transcode_idle_seconds()):
            running = live_transcode_count()
    if running >= limit:
        raise HTTPException(
            status_code=503,
            detail=f"转码忙（{running}/{limit} 路），请使用直连播放",
        )


import contextlib as _contextlib

@_contextlib.contextmanager
def transcode_slot():
    """转码槽位上下文管理器（P1 修复 D-P1-1）。

    用法：
        with transcode_mod.transcode_slot():
            session_id = start_transcode(...)

    在锁内做入场检查，保证"检查→启动"原子，突发请求不会绕过并发上限打满 CPU。
    """
    with _transcode_slot_lock:
        ensure_slot_or_503()
        yield


def _dir_size(path: str) -> int:
    """目录总字节数；异常返回 0（不影响主流程）。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _cache_mtime(entry_path: str) -> float:
    """缓存目录的 LRU 时间：优先 cache.json 的 completed_at，否则目录 mtime。"""
    try:
        with open(os.path.join(entry_path, "cache.json"), encoding="utf-8") as fh:
            completed = json.load(fh).get("completed_at")
        if completed:
            return datetime.fromisoformat(completed).timestamp()
    except (OSError, ValueError, KeyError):
        pass
    try:
        return os.path.getmtime(entry_path)
    except OSError:
        return 0.0


def _active_cache_dirs() -> set[str] | None:
    """正被已注册转码会话服务中的缓存目录（abspath 集合）。

    LRU 淘汰必须跳过它们：``register_cache_session`` 登记后，客户端后续所有
    切片都从 ``info["dir"]`` 直读；删掉该目录会让播放中途 404/503。
    会话被回收（reap_idle/stop 摘除登记）后，目录才可淘汰。
    任何异常内部吞掉返回 None（活跃会话未知）；调用方须按 fail-closed
    跳过本轮淘汰——删目录不可逆，未知时宁可不删。
    """
    try:
        streaming = _streaming()
        procs = getattr(streaming, "_TRANSCODE_PROCS", None)
        if not procs:
            return set()
        dirs: set[str] = set()
        for _sid, info in list(procs.items()):
            d = info.get("dir") if isinstance(info, dict) else None
            if d:
                dirs.add(os.path.abspath(d))
        return dirs
    except Exception:
        return None


def _enforce_cache_capacity(new_dir: str) -> None:
    """P1 修复（审查）：转码缓存 LRU 淘汰，防止无上限写满磁盘。

    上限由 EMBY_TRANSCODE_CACHE_MAX_GB 控制（默认 10G）；超限时按最旧优先
    删除，直到回到上限 90%。跳过刚落盘的 new_dir，以及正被活跃会话服务中
    的目录（删掉会导致播放中途 404/503）。活跃集合未知时整轮跳过淘汰
    （fail-closed），删除前再逐个复查。全部异常内部吞掉。
    """
    try:
        max_gb = float(os.getenv("EMBY_TRANSCODE_CACHE_MAX_GB", "10") or 10)
    except ValueError:
        max_gb = 10.0
    if max_gb <= 0:
        return
    base = cache_dir()
    try:
        entries = [
            os.path.join(base, name) for name in os.listdir(base)
            if os.path.isdir(os.path.join(base, name))
        ]
    except OSError:
        return
    sizes = {p: _dir_size(p) for p in entries}
    total = sum(sizes.values())
    limit = max_gb * 1024 ** 3
    if total <= limit:
        return
    # 最旧优先；跳过刚落盘的目录，以及正被活跃会话服务中的目录
    # （删掉会导致该会话后续切片 404/503、播放中途断掉）。
    # fail-closed：活跃集合未知（None）时整轮跳过，宁可不删。
    active = _active_cache_dirs()
    if active is None:
        return
    victims = sorted(
        (
            p
            for p in entries
            if os.path.abspath(p) != os.path.abspath(new_dir)
            and os.path.abspath(p) not in active
        ),
        key=_cache_mtime,
    )
    removed, freed = 0, 0
    for path in victims:
        if total <= limit * 0.9:
            break
        # 删除前复查：快照与删除之间的窗口里可能有新会话登记复用了该目录
        # （TOCTOU）；集合未知（None）或该目录已活跃时跳过，不删。
        current_active = _active_cache_dirs()
        if current_active is None or os.path.abspath(path) in current_active:
            continue
        try:
            shutil.rmtree(path, ignore_errors=False)
        except OSError as exc:
            logger.warning("转码缓存淘汰删除失败 %s: %s", path, exc)
            continue
        removed += 1
        freed += sizes.get(path, 0)
        total -= sizes.get(path, 0)
    logger.info("转码缓存 LRU 淘汰：删除 %d 个目录，释放 %.1fMB", removed, freed / 1024 ** 2)


def maybe_promote_to_cache(session_id: str, info: dict) -> bool:
    """转码完成 → 落盘进持久缓存。返回是否已提升。

    只提升「整片转完」的会话：ffmpeg 正常退出（returncode 0）、从 0 开始转、
    有档位信息。seek 中途开始的、转码失败的都不进缓存。
    由 ``streaming.reap_stale_transcodes`` 在回收前调用。
    """
    if info.get("cached"):
        return False
    proc = info.get("proc")
    if proc is None or proc.poll() != 0:
        return False
    if (info.get("start_seconds") or 0) > 1:
        return False
    key = info.get("cache_key")
    src_dir = info.get("dir")
    if not key or not src_dir:
        return False
    if not _cache_valid(src_dir):
        return False
    dst = os.path.join(cache_dir(), key)
    try:
        os.makedirs(cache_dir(), exist_ok=True)
    except OSError:
        return False
    if os.path.isdir(dst):
        # 并发转完：先到的已落盘，后到的直接丢弃（调用方正常回收删目录）
        return False
    # 两步落盘，堵住上面 isdir 检查与真正落盘之间的窗口：
    # 先把 src_dir 搬到缓存目录下的唯一临时名（与 dst 同目录，保证 rename 不跨设备），
    # 再用 os.rename 原子认领 dst。若直接 shutil.move(src_dir, dst) 而此刻 dst 已被
    # 并发方建好，move 不会报错，而是把 src_dir 整个塞进 dst 里（dst/<src_basename>），
    # 造成缓存目录嵌套与磁盘双倍占用。rename 到已存在的非空目录则必然抛 OSError，
    # 后到者据此判定输掉竞态，丢弃自己的临时拷贝，绝不污染先到者的 dst。
    tmp = dst + ".promote-" + uuid.uuid4().hex[:12]
    try:
        shutil.move(src_dir, tmp)
    except OSError as exc:
        logger.warning("转码缓存落盘失败 %s: %s", session_id, exc)
        return False
    try:
        os.rename(tmp, dst)
    except OSError as exc:
        # 竞态失败方（dst 已存在）或落盘失败：清掉自己的临时目录后返回。
        shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.isdir(dst):
            logger.warning("转码缓存落盘失败 %s: %s", session_id, exc)
        return False
    try:
        meta = {
            "item_guid": info.get("item_guid"),
            "video_bitrate": info.get("video_bitrate"),
            "height": info.get("height"),
            "fingerprint": info.get("fingerprint"),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(os.path.join(dst, "cache.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False)
    except OSError:
        pass
    # 会话继续登记，指向缓存目录：正在拉切片的客户端不受影响；
    # 回收时 cached=True 跳过删目录，缓存留给下次复用。
    info["dir"] = dst
    info["cached"] = True
    logger.info("转码缓存落盘 %s %sbps", info.get("item_guid"), info.get("video_bitrate"))
    # 落盘后检查容量：超限则 LRU 淘汰最旧的缓存（防无上限写满磁盘）
    try:
        _enforce_cache_capacity(dst)
    except Exception:  # noqa: BLE001 — 淘汰失败绝不影响已落盘的缓存
        logger.warning("转码缓存容量检查失败", exc_info=True)
    return True

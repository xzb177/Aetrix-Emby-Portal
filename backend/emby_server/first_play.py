# -*- coding: utf-8 -*-
"""首次播放加速：预检 + SA 轮换 + 详情页预热（与播放器无关的共性层）。

问题（2026-10-27 视频 + 服务端会话实证）：
- 首次播放常在 0 KB/s → 几 KB/s → MB/s 之间反复探，20–180 秒才出首帧，
  第二遍却很快；个别首播在约 60 秒后才露出 HTTP 403。
- 根因不在 PlaybackInfo 的 DB 查询（本地实测 token 现取 100–316ms、
  服务端到谷歌 alt=media 首字节约 1.0–1.2s、尾部 1MB 约 1.3s），而在
  下发给播放器的那条 alt=media 凭据本身没有被验证过：
  旧代码在 PlaybackInfo 里无亲和地取一个随机 SA 令牌就下发，播放器
  第一次拿到的令牌若对应 SA 无权读该文件，就会 403 / 反复重试，
  第二次 PlaybackInfo 换了一个 SA 才通——“第一次慢、第二次快”
  正是这个随机 SA 抽奖的表象。

本模块只做三件共性事，不按播放器名分流（不读任何客户端名）：
1. 预检（probe）：下发前用 Range 0-0 真打一次 alt=media，206 才算可用。
2. 轮换（rotate）：403/401 时换一个 SA 再预检，最多 5 次；全败才放弃，
   且放弃时记一条 play_start error 事件（error_kind 带轮换次数），
   播放器永远不会在第一次就拿到一个未经验证的坏令牌。
3. 预热（warm）：详情页、单条 Ids 直达、选集列表、以及首页
   Resume / Latest / NextUp 各暖用户最可能点的 1 条，在后台线程里把
   1+2 做完并写进 playback 域共享缓存；电影/单集在返回前就占上在途标记，
   PlaybackInfo 若缓存未热会等这一次预检结束再复用，不再并行再打一遍。
   命中新鲜窗口（默认 180 秒，FIRST_PLAY_WARM_FRESH_SEC）内的预热直接
   复用（alt_warm_hit），超过则先单次复检再用；预热缓存 TTL 默认 300 秒
   （FIRST_PLAY_WARM_TTL_SEC），远小于 SA 令牌 1 小时寿命。预热 206 且
   有 fid 时顺手写入 exists 正缓存，随后 /stream 少打一次 files.get。

注意：预热只能热“服务端 → 谷歌”这一段（令牌、预检、尾部可达），
播放器 → 谷歌那段的网络（用户侧 VPN 节点、播放器首次探格式）
服务端无法直接加速；本模块把服务端能确定的部分全部提前做完，
让播放器点播放时不再为坏令牌重试几十秒。

尾部索引诚实结论（第二轮，2026-10-27 #1358 上线后实测）：
尾部 1KB 探测只能证明“该令牌能读尾部”，不能让播放器少一次尾部
seek——DirectPlayUrl 是谷歌 alt=media 直链，播放器解析 mp4 moov /
mkv cues 时必须自己向谷歌拉尾部，服务端没有给播放器递索引字节
的通道（递了就是中继，违背直链主线）。#1358 已有尾部探测、但
PlaybackInfo→会话仍隔 11–14 秒，正是这一点的实证；所以本轮不
再假装“预取尾部能加速首帧”，尾部探测只保留作可读性验证。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 预检单次超时（秒）：服务端到谷歌首字节实测约 1.1s，10s 已留足余量，
#: 超时按失败处理，不阻塞播放（调用方回落旧地址）。
PRECHECK_TIMEOUT_SEC = 10.0
#: 403/401 时最多轮换次数（含首次尝试共 5 个 SA）。
MAX_ROTATIONS = 5
#: 预热缓存 TTL（秒），远小于 SA 令牌 1 小时寿命。
#: FIRST_PLAY_WARM_TTL_SEC，默认 300。空/非整数回落默认；负数钳成 0。
_WARM_TTL_DEFAULT = 300
#: 预热新鲜窗口（秒）：窗口内可直接复用；超过则单次复检后再用。
#: FIRST_PLAY_WARM_FRESH_SEC，默认 180（原先写死 120，刻意略加深）。
_WARM_FRESH_DEFAULT = 180


def warm_fresh_sec() -> int:
    """新鲜窗口秒数。空/非整数回落 180；负数钳成 0。"""
    return env_int("FIRST_PLAY_WARM_FRESH_SEC", _WARM_FRESH_DEFAULT, 0, 86400)


def warm_ttl_sec() -> int:
    """预热缓存 TTL 秒数。空/非整数回落 300；负数钳成 0。"""
    return env_int("FIRST_PLAY_WARM_TTL_SEC", _WARM_TTL_DEFAULT, 0, 86400)


#: 尾部探测字节数：只验尾部可达，不下载整段。
TAIL_PROBE_BYTES = 1024
#: 跨进程 SA 亲和记录 TTL（秒）：文件→SA 的可读关系本身是稳定的
#: （SA 在分享名单里才会 206），6 小时内先用上次成功的 SA；若该 SA
#: 某次 403（配额/权限变化），轮换成功后立即用新 SA 覆盖本记录，
#: 自愈，不靠 TTL 过期才恢复。
SA_AFFINITY_TTL_SEC = 6 * 3600
#: PlaybackInfo 等待在途预热的上限：与单次预检超时同量级。健康预检约 1s，
#: 等得到就复用；等不到再走自己的冷预检（不把一次挂死放大成两次叠加之前，
#: 上限停在这一次预检的预算上）。
INFLIGHT_WAIT_SEC = PRECHECK_TIMEOUT_SEC


def _log_warm(event: str, **fields) -> None:
    """打一条预热日志。走 play-timing 的 logger：EA 默认没有 root handler，
    普通 logger.info 会被吞，而 [play-timing] 那条在生产上能 grep 到。
    日志里只有条目/用户/来源，没有令牌。
    """
    try:
        from backend.emby_server.play_timing import logger as _pt

        extras = " ".join(f"{k}={v}" for k, v in fields.items())
        _pt.info("[first-play] %s %s", event, extras)
    except Exception:  # noqa: BLE001 — 日志失败不影响预热
        logger.info("[first-play] %s", event)


def alt_media_url(file_id: str) -> str:
    """文件 id → Drive alt=media 地址（无令牌在 URL 里）。"""
    from backend.emby_server import drive_auth as _da

    return _da.drive_api_media_url(_da.drive_usercontent_download_url(file_id))


def probe_alt_media(url: str, token: str, *, range_header: str = "bytes=0-0",
                    timeout: float = PRECHECK_TIMEOUT_SEC,
                    _http: Optional[Callable] = None) -> dict:
    """对 alt=media 做一次 Range 预检，返回状态与耗时（永不抛）。

    _http 注入点（测试用）：callable(url, headers, timeout) -> 具有
    status / headers 的对象；默认走 urllib。
    """
    t0 = time.monotonic()
    try:
        if _http is not None:
            resp = _http(url, {"Authorization": f"Bearer {token}",
                               "Range": range_header}, timeout)
            status = int(getattr(resp, "status", 0) or 0)
            headers = getattr(resp, "headers", {}) or {}
            ct = headers.get("Content-Type") if hasattr(headers, "get") else None
            cr = headers.get("Content-Range") if hasattr(headers, "get") else None
        else:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}",
                              "Range": range_header})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                # 只读 1 字节验证可读性，不下载整段。
                resp.read(1)
                status = int(resp.status)
                ct = resp.headers.get("Content-Type")
                cr = resp.headers.get("Content-Range")
        return {"ok": status == 206, "status": status,
                "content_type": ct, "content_range": cr,
                "ttfb_ms": int((time.monotonic() - t0) * 1000)}
    except urllib.error.HTTPError as exc:
        # urlopen 对 4xx/5xx 不返回响应、而是抛 HTTPError：状态码必须
        # 从 exc.code 取回。旧代码把它吞进通用 except、报 status=0，
        # 轮换循环见不到 403/401、首轮即 break——“403 换 SA 最多 5 次”
        # 在真实路径上从未生效（注入探针的测试盖不住这一层）。
        # 429/5xx 同样如实上报状态，轮换循环仍按“非 403/401 不换 SA”
        # 直接放弃，只是不再把状态谎报成 0。
        try:
            exc.close()
        except Exception:  # noqa: BLE001 — 关错误体失败不影响状态上报
            pass
        headers = getattr(exc, "headers", None)
        get = getattr(headers, "get", None) if headers is not None else None
        return {"ok": False, "status": int(exc.code),
                "content_type": get("Content-Type") if get else None,
                "content_range": get("Content-Range") if get else None,
                "ttfb_ms": int((time.monotonic() - t0) * 1000),
                "error": "HTTPError"}
    except Exception as exc:  # noqa: BLE001 — 预检失败即不可用，不向上抛
        return {"ok": False, "status": 0, "content_type": None,
                "content_range": None, "ttfb_ms": int((time.monotonic() - t0) * 1000),
                "error": type(exc).__name__}


def resolve_verified_alt_media(
    file_id: str, token_fn: Callable[[int], Optional[str]], *,
    max_rotations: int = MAX_ROTATIONS,
    probe: Optional[Callable] = None,
) -> dict:
    """取一个经预检验证的 alt=media 凭据（轮换 SA，直到 206 或耗尽）。

    token_fn(attempt) -> token|None：attempt 从 0 起，生产侧用
    drive_auth.get_drive_bearer_token(f"{file_id}:{attempt}")，不同
    attempt 落到不同亲和键，从而换 SA；测试侧注入坏→好令牌序列。
    probe(url, token) -> probe_alt_media 的结果 dict；默认走真实预检。
    返回：ok 时含 url/token/rotations/precheck_ms/content_type；
    失败时含 ok=False、last_status、rotations（已尝试次数）。
    """
    url = alt_media_url(file_id) if file_id else ""
    if not url:
        return {"ok": False, "url": "", "token": None, "rotations": 0,
                "last_status": 0, "precheck_ms": 0}
    probe_fn = probe or (lambda u, t: probe_alt_media(u, t))
    last_status = 0
    total_ms = 0
    attempts = 0
    for attempt in range(max(1, max_rotations)):
        attempts += 1
        try:
            token = token_fn(attempt)
        except Exception:  # noqa: BLE001 — 令牌失败按本轮不可用计
            token = None
        if not token:
            continue
        result = probe_fn(url, token)
        total_ms += int(result.get("ttfb_ms") or 0)
        last_status = int(result.get("status") or 0)
        if result.get("ok") and last_status == 206:
            return {"ok": True, "url": url, "token": token,
                    "rotations": attempt, "precheck_ms": total_ms,
                    "content_type": result.get("content_type"),
                    "content_range": result.get("content_range")}
        # 只有 403/401 值得换 SA 再试；其它状态（429/5xx/网络错）换 SA
        # 也无帮助，直接放弃，调用方回落旧地址。
        if last_status not in (403, 401):
            break
    return {"ok": False, "url": url, "token": None, "rotations": attempts,
            "last_status": last_status, "precheck_ms": total_ms}


def warm_key(guid: str, user_id) -> str:
    """预热缓存键：按条目 + 用户隔离，令牌绝不跨用户复用。"""
    return f"fp:{guid}:{user_id}"


def warm_usable(entry: dict | None, now: float | None = None,
                fresh_sec: int | None = None) -> bool:
    """预热条目是否可直接复用（年龄 <= fresh_sec 且有令牌与地址）。"""
    if fresh_sec is None:
        fresh_sec = warm_fresh_sec()
    if not isinstance(entry, dict):
        return False
    if not entry.get("url") or not entry.get("token"):
        return False
    warmed_at = entry.get("warmed_at")
    if warmed_at is None:
        return False
    now = time.time() if now is None else now
    return 0 <= (now - float(warmed_at)) <= fresh_sec


def store_warm(cache_set: Callable, guid: str, user_id, result: dict,
               ttl: int | None = None) -> bool:
    """把验证过的直链写进共享缓存（只存本次验证结果，不存用户令牌）。"""
    if ttl is None:
        ttl = warm_ttl_sec()
    try:
        payload = {"url": result.get("url"), "token": result.get("token"),
                   "fid": result.get("fid"),
                   "rotations": result.get("rotations", 0),
                   "content_type": result.get("content_type"),
                   "tail_ok": bool(result.get("tail_ok")),
                   "warmed_at": time.time()}
        cache_set(warm_key(guid, user_id), json.dumps(payload), ttl)
        return True
    except Exception:  # noqa: BLE001 — 缓存失败不影响播放
        return False


def note_drive_exists_from_warm(fid: str, drive_id: str | None = None) -> None:
    """预热已验证 206 且有 fid：写入 exists 正缓存。永不抛。

    drive_id 已知时写本进程字典（``api._drive_exists_cache_put``）。
    未知时（预热路径的常态，令牌预检拿不到挂载盘 id）走跨进程正缓存
    ``transfer_variant.publish_drive_exists_ok``，键
    ``aetrix:drive_exists:ok:{fid}``，与工人登记 ready 同一约定。
    懒导入，避免 first_play ↔ api 在模块加载时环回。
    """
    fid = (fid or "").strip()
    if not fid:
        return
    drive_id = (drive_id or "").strip()
    if drive_id:
        try:
            from backend.emby_server import api as _api

            _api._drive_exists_cache_put(drive_id, fid)
            return
        except Exception:  # noqa: BLE001 — 本进程写失败再试跨进程键
            logger.debug("first_play exists local put failed", exc_info=True)
    try:
        from backend.emby_server import transfer_variant as _tv

        _tv.publish_drive_exists_ok(fid)
    except Exception:  # noqa: BLE001 — 正缓存失败不影响预热
        logger.debug("first_play exists publish failed", exc_info=True)


def load_warm(cache_get: Callable, guid: str, user_id) -> dict | None:
    """读预热缓存，解析失败 / 缺字段时返回 None（调用方走正常验证）。"""
    try:
        raw = cache_get(warm_key(guid, user_id))
        if not raw:
            return None
        entry = json.loads(raw)
        return entry if isinstance(entry, dict) else None
    except Exception:  # noqa: BLE001
        return None


def sa_affinity_key(fid: str) -> str:
    """跨进程 SA 亲和缓存键：按文件隔离，不按用户隔离（SA 可读性是文件属性）。"""
    return f"fp:sa:{fid}"


def load_sa_affinity(cache_get, fid: str) -> str | None:
    """读上次预检成功的 SA 邮箱；过期 / 解析失败 / 缺字段时返回 None。"""
    if not fid:
        return None
    try:
        raw = cache_get(sa_affinity_key(fid))
        if not raw:
            return None
        entry = json.loads(raw)
        if not isinstance(entry, dict):
            return None
        email = entry.get("email")
        updated_at = entry.get("updated_at")
        if not email or updated_at is None:
            return None
        if 0 <= (time.time() - float(updated_at)) <= SA_AFFINITY_TTL_SEC:
            return str(email)
    except Exception:  # noqa: BLE001 — 亲和记录失败只回落轮换
        return None
    return None


def store_sa_affinity(cache_set, fid: str, email: str) -> bool:
    """把预检成功的 SA 邮箱写进共享缓存（只存邮箱，不存令牌）。"""
    if not fid or not email:
        return False
    try:
        cache_set(sa_affinity_key(fid),
                  json.dumps({"email": email, "updated_at": time.time()}),
                  SA_AFFINITY_TTL_SEC)
        return True
    except Exception:  # noqa: BLE001 — 缓存失败不影响播放
        return False


def _resolve_verified(fid: str, token_fn, *, cache_get=None, cache_set=None,
                      probe=None) -> dict:
    """带持久亲和的验证解析：先用上次成功的 SA，失败再走轮换。

    顺序（每一步失败都只回落，不抛）：
    1. 共享缓存里有该文件的成功 SA 邮箱 → 按邮箱取令牌并预检，
       206 直接返回（rotations=0，不消耗轮换次数）。
    2. 亲和 SA 预检 403/401 或取不到令牌 → 走原轮换循环（最多 5 个 SA）。
    3. 轮换成功时，把成功令牌对应的 SA 邮箱写回共享缓存（本进程
       缓存里查不到该令牌时跳过记录，下次仍走轮换，不出错）。
    亲和 SA 若因配额 403 而失败，本次多付一次预检（约 1 秒）后轮换；
    轮换成功即覆盖记录，所以亲和记录不会把播放钉死在一个坏 SA 上。
    """
    from backend.emby_server import drive_auth as _da

    url = alt_media_url(fid) if fid else ""
    if not url:
        return {"ok": False, "url": "", "token": None, "rotations": 0,
                "last_status": 0, "precheck_ms": 0}
    probe_fn = probe or (lambda u, t: probe_alt_media(u, t))
    if cache_get is not None:
        pref_email = load_sa_affinity(cache_get, fid)
        if pref_email:
            try:
                pref_token = _da.get_drive_bearer_token_for_email(pref_email)
            except Exception:  # noqa: BLE001
                pref_token = None
            if pref_token:
                pref_result = probe_fn(url, pref_token)
                if pref_result.get("ok") and int(pref_result.get("status") or 0) == 206:
                    return {"ok": True, "url": url, "token": pref_token,
                            "rotations": 0, "precheck_ms": int(pref_result.get("ttfb_ms") or 0),
                            "content_type": pref_result.get("content_type"),
                            "content_range": pref_result.get("content_range"),
                            "preferred": True}
    result = resolve_verified_alt_media(fid, token_fn, probe=probe_fn)
    if result.get("ok") and cache_set is not None:
        try:
            email = _da.sa_email_for_token(result.get("token"))
        except Exception:  # noqa: BLE001
            email = None
        if email:
            store_sa_affinity(cache_set, fid, email)
    return result


def _resolve_for_playback(db, item, user, fid: str) -> dict:
    """PlaybackInfo 同步入口：验证直链并写预热缓存（在 run_db 线程里跑）。

    与 warm_first_play_sync 的区别：本函数不读详情缓存、不探尾部，
    只做“令牌 + 预检 + 轮换 + 写缓存”这一段，供 PlaybackInfo 冷启动用。
    """
    try:
        from backend.emby_server import drive_auth as _da
        from backend.database import CacheManager

        _log_warm(
            "warm trigger", source="playbackinfo_cold",
            item_id=getattr(item, "id", None),
            user_id=getattr(user, "id", None))

        def _token_fn(attempt: int):
            return _da.get_drive_bearer_token(f"{fid}:{attempt}")

        result = _resolve_verified(
            fid, _token_fn,
            cache_get=lambda k: CacheManager.get(k, domain="playback"),
            cache_set=lambda k, v, ttl: CacheManager.set(k, v, ttl, domain="playback"))
        if result.get("ok"):
            result["fid"] = fid
            store_warm(
                lambda k, v, ttl: CacheManager.set(k, v, ttl, domain="playback"),
                getattr(item, "guid", ""), getattr(user, "id", None), result)
            note_drive_exists_from_warm(fid)
        return result
    except Exception as exc:  # noqa: BLE001 — 失败只回落旧地址
        logger.warning("first_play resolve failed: %s", type(exc).__name__)
        return {"ok": False, "last_status": 0, "rotations": 0, "precheck_ms": 0}


def warm_first_play_sync(db, item, user) -> dict:
    """详情页后台线程入口：解析播放 fid → 验证直链 → 探尾部 → 写缓存。

    同步函数（在后台线程里跑，不碰请求 DB 会话）。任何异常只返回
    ok=False，绝不抛到详情页。返回含 rotations/precheck_ms/tail_ok。
    """
    outcome = {"ok": False, "reason": "exception"}
    try:
        from backend.emby_server import playback_strategy as _ps
        from backend.emby_server import transfer_variant as _tv
        from backend.emby_server import drive_auth as _da

        fid, _ = _tv.resolve_play_file_id(db, item, user)
        fid = (fid or "").strip()
        if not fid or not _ps.drive_direct_play_enabled(db):
            outcome = {"ok": False, "reason": "no_drive_direct"}
            return outcome
        from backend.database import CacheManager

        cached = load_warm(
            lambda k: CacheManager.get(k, domain="playback"),
            getattr(item, "guid", ""), getattr(user, "id", None))
        if warm_usable(cached):
            outcome = {"ok": True, "reason": "warm_hit",
                       "rotations": cached.get("rotations", 0), "precheck_ms": 0,
                       "tail_ok": bool(cached.get("tail_ok"))}
            return outcome

        def _token_fn(attempt: int):
            return _da.get_drive_bearer_token(f"{fid}:{attempt}")

        result = _resolve_verified(
            fid, _token_fn,
            cache_get=lambda k: CacheManager.get(k, domain="playback"),
            cache_set=lambda k, v, ttl: CacheManager.set(k, v, ttl, domain="playback"))
        if not result.get("ok"):
            outcome = {"ok": False, "reason": "precheck_failed",
                       "rotations": result.get("rotations", 0),
                       "last_status": result.get("last_status"),
                       "precheck_ms": result.get("precheck_ms", 0)}
            return outcome
        # 尾部可达性：只探最后 1KB，验该令牌能否读尾部索引段。
        tail_ok = False
        try:
            size = None
            cr = result.get("content_range") or ""
            if "/" in cr:
                size = int(cr.rsplit("/", 1)[-1])
            if size and size > TAIL_PROBE_BYTES:
                tail = probe_alt_media(
                    result["url"], result["token"],
                    range_header=f"bytes={size - TAIL_PROBE_BYTES}-{size - 1}")
                tail_ok = bool(tail.get("ok"))
        except Exception:  # noqa: BLE001 — 尾部探失败不否定头部验证
            tail_ok = False
        result["tail_ok"] = tail_ok
        result["fid"] = fid
        store_warm(
            lambda k, v, ttl: CacheManager.set(k, v, ttl, domain="playback"),
            getattr(item, "guid", ""), getattr(user, "id", None), result)
        note_drive_exists_from_warm(fid)
        outcome = {"ok": True, "reason": "warmed", "rotations": result["rotations"],
                   "precheck_ms": result["precheck_ms"], "tail_ok": tail_ok}
        return outcome
    except Exception as exc:  # noqa: BLE001 — 预热永不抛
        logger.warning("first_play warm failed: %s", type(exc).__name__)
        outcome = {"ok": False, "reason": "exception"}
        return outcome
    finally:
        _log_warm(
            "warm done",
            item_id=getattr(item, "id", None),
            guid=getattr(item, "guid", ""),
            user_id=getattr(user, "id", None),
            reason=outcome.get("reason"),
            precheck_ms=outcome.get("precheck_ms", 0))



# ---------------------------------------------------------------------------
# 第二轮：预热范围扩展（详情 → 剧集页/列表页/下一集）与目标选择
#
# 界线（先定死，避免预热失控）：
# - 每次触发最多暖 1 条：详情页暖本条或本剧下一集、列表页暖父级
#   的第一集未看、PlaybackInfo 暖下一集、Resume/Latest/NextUp 各暖
#   用户最可能点的那 1 条；绝不整列表全暖（一个列表 20 条冷预热就是
#   20 次令牌 + 20 次谷歌预检，配额与耗时都失控）。
# - 预热本身已做 warm_usable 早退：已暖过的条目在线程里只做 DB
#   解析、不再打谷歌，所以重复触发不会重复烧预检。
# - “下一集”口径与站内 NextUp 一致：同剧第一集未看（按
#   season_number / episode_number / id 排序），不是按入库顺序。
# ---------------------------------------------------------------------------

def _is_watched(db, user_id: int, item_id: int) -> bool:
    """该用户是否看过该条目（played / 有进度 / 有播放次数，NextUp 口径）。"""
    from backend.emby_server import models as _em

    row = db.query(_em.UserMediaData).filter(
        _em.UserMediaData.user_id == user_id,
        _em.UserMediaData.item_id == item_id).first()
    if row is None:
        return False
    return bool(getattr(row, "played", False)
                or (getattr(row, "playback_position_ticks", 0) or 0) > 0
                or (getattr(row, "play_count", 0) or 0) > 0)


def _episodes_of_series(db, series_id: int):
    """同剧全部单集（活行，按季/集/id 排序；NULL 季集号排最后）。"""
    from backend.emby_server import models as _em
    from sqlalchemy import nullslast

    return (db.query(_em.MediaItem)
            .filter(_em.MediaItem.item_type == "episode",
                    _em.MediaItem.series_id == series_id,
                    _em.MediaItem.merged_into_id.is_(None),
                    _em.MediaItem.is_hidden == False)  # noqa: E712
            .order_by(nullslast(_em.MediaItem.season_number.asc()),
                      nullslast(_em.MediaItem.episode_number.asc()),
                      _em.MediaItem.id.asc())
            .all())


def first_unwatched_episode(db, user_id: int, series_id: int, season_number=None):
    """同剧（可选限定季）第一集未看；全看过时返回 None（不猜、不回落已看）。"""
    if not series_id:
        return None
    for ep in _episodes_of_series(db, series_id):
        if season_number is not None and getattr(ep, "season_number", None) != season_number:
            continue
        if not _is_watched(db, user_id, ep.id):
            return ep
    return None


def next_episode_after(db, item):
    """本集的下一集：同剧按季/集/id 排序，取本集之后的第一个；无则 None。

    纯排序比较（不做 SQL 元组比较，避免 NULL 季集号语义坑）；
    单集数通常 < 数百行，放在预热后台线程里，不进请求路径。
    """
    if getattr(item, "item_type", None) != "episode":
        return None
    series_id = getattr(item, "series_id", None)
    if not series_id:
        return None
    cur = (getattr(item, "season_number", None) or 0,
           getattr(item, "episode_number", None) or 0)
    for ep in _episodes_of_series(db, series_id):
        nxt = (getattr(ep, "season_number", None) or 0,
               getattr(ep, "episode_number", None) or 0)
        if (nxt, ep.id) > (cur, item.id):
            return ep
    return None


def warm_target_for_detail(db, user_id: int, item):
    """详情页预热目标（最多 1 条）：可播条目暖自身；剧/季暖第一集未看。

    剧/季本身无 drive_file_id 时旧代码在线程里直接返回，等于没暖；
    本函数把目标换成用户最可能点的下一集，详情页才真正起到预热作用。
    """
    item_type = getattr(item, "item_type", None)
    if item_type in ("episode", "movie"):
        return item
    if item_type == "series":
        return first_unwatched_episode(db, user_id, item.id)
    if item_type == "season":
        return first_unwatched_episode(
            db, user_id, getattr(item, "series_id", None),
            season_number=getattr(item, "season_number", None))
    return None


# ---------------------------------------------------------------------------
# 在途预热：同一用户同一条目只打一次谷歌预检。
# 电影/单集在 HTTP 返回前就 claim，PlaybackInfo 若缓存还没写上就等这一次，
# 不再并行再开一轮冷预检。剧/季的目标是另一集，等线程选中目标后再 claim。
# ---------------------------------------------------------------------------

_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


def inflight_key(user_id, item_id) -> str:
    return f"{user_id}:{item_id}"


def claim_inflight(user_id, item_id):
    """占上在途标记。返回 (event, owner)。owner=False 表示已有一次在跑。"""
    key = inflight_key(user_id, item_id)
    with _inflight_lock:
        current = _inflight.get(key)
        if current is not None and not current.is_set():
            return current, False
        ev = threading.Event()
        _inflight[key] = ev
        return ev, True


def finish_inflight(user_id, item_id, ev: threading.Event | None) -> None:
    """结束在途标记（幂等）。等的人被放开；下一次点播走缓存而不是再等。"""
    if ev is None:
        return
    ev.set()
    key = inflight_key(user_id, item_id)
    with _inflight_lock:
        if _inflight.get(key) is ev:
            _inflight.pop(key, None)


def wait_inflight(user_id, item_id, timeout: float) -> bool:
    """有在途预热就等它结束。没有在途返回 False（调用方走自己的冷预检）。"""
    key = inflight_key(user_id, item_id)
    with _inflight_lock:
        ev = _inflight.get(key)
    if ev is None:
        return False
    return bool(ev.wait(timeout))


def consume_inflight_warm(cache_get, user_id, item_id, guid, fid,
                          timeout: float = INFLIGHT_WAIT_SEC) -> dict | None:
    """等在途预热写完缓存。等不到、或写上的不是这个文件，返回 None。"""
    if not wait_inflight(user_id, item_id, timeout):
        return None
    entry = load_warm(cache_get, guid, user_id)
    if warm_usable(entry) and entry.get("fid") == fid:
        return entry
    return None


def direct_item_guid(ids) -> str | None:
    """恰好一条 Ids 才是点名直达。批量刷新不预热，避免一次列表打多次谷歌。"""
    if not ids or len(ids) != 1:
        return None
    guid = str(ids[0] or "").strip()
    return guid or None


def iter_query_ids(values) -> list[str]:
    """把查询串里的 Ids（可重复、可逗号拼接）收成 guid 列表。"""
    out: list[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _warm_thread_body(item_id: int, user_id: int, mode: str,
                      preclaimed_event: threading.Event | None = None):
    """预热后台线程体：自开 DB 会话，不碰请求会话；任何异常只记日志。

    ``preclaimed_event`` 是调用方在返回 HTTP 之前占上的在途标记（电影/单集
    暖自身）。目标若换成别的条目（剧详情 → 第一集未看），先放开原标记再占
    新目标，避免 PlaybackInfo 空等。无论成功失败，finally 都会放开标记。
    """
    session = None
    finish_item = item_id
    finish_ev = preclaimed_event
    try:
        from backend.database import SessionLocal as _SL
        from backend.emby_server import models as _em

        session = _SL()
        item = session.query(_em.MediaItem).filter(
            _em.MediaItem.id == item_id).first()
        user = session.query(_em.WebUser).filter(
            _em.WebUser.id == user_id).first()
        if item is None or user is None:
            return
        if mode == "next":
            target = next_episode_after(session, item)
        else:  # "detail"
            target = warm_target_for_detail(session, user_id, item)
        if target is None:
            return
        if finish_ev is not None and target.id != item_id:
            finish_inflight(user_id, item_id, finish_ev)
            finish_ev = None
        if finish_ev is None:
            ev, owner = claim_inflight(user_id, target.id)
            if not owner:
                return
            finish_ev = ev
            finish_item = target.id
        warm_first_play_sync(session, target, user)
    except Exception:  # noqa: BLE001 — 预热永不影响请求路径
        pass
    finally:
        finish_inflight(user_id, finish_item, finish_ev)
        try:
            if session is not None:
                session.close()
        except Exception:  # noqa: BLE001
            pass


def spawn_first_play_warm(item_id: int, user_id: int, mode: str = "detail", *,
                          source: str = "", self_target: bool = False) -> bool:
    """起一个 daemon 预热线程（不阻塞、不抛）；启动失败返回 False。

    ``self_target=True``（电影/单集暖自身）：在起线程之前就占上在途标记，
    这样紧跟着的 PlaybackInfo 能等这一次预检，而不是再打一遍。已有在途则
    直接返回（幂等）。``source`` 只写进日志，不参与任何播放器分支。
    """
    if source != "parent_list":
        _log_warm(
            "warm trigger", item_id=item_id, user_id=user_id,
            mode=mode, source=source or "-")
    preclaimed = None
    if self_target and mode != "next":
        ev, owner = claim_inflight(user_id, item_id)
        if not owner:
            _log_warm(
                "warm already inflight", item_id=item_id, user_id=user_id,
                source=source or "-")
            return True
        preclaimed = ev
    try:
        threading.Thread(
            target=_warm_thread_body,
            args=(item_id, user_id, mode, preclaimed),
            daemon=True, name="first-play-warm").start()
        return True
    except Exception:  # noqa: BLE001
        finish_inflight(user_id, item_id, preclaimed)
        return False


def spawn_first_play_warm_by_guid(guid: str, user_id: int, *,
                                  source: str = "guid") -> bool:
    """按 guid 起详情预热（列表页 ParentId / 单条 Ids 用）。

    guid 查不到条目时线程内空转返回。列表页 ParentId 可能是库 guid，
    那种空转不打 warm trigger 日志，避免每次刷库都刷一屏。
    """
    guid = (guid or "").strip()
    if not guid:
        return False
    if source != "parent_list":
        _log_warm(
            "warm trigger", guid=guid, user_id=user_id,
            mode="detail", source=source)
    try:
        def _by_guid(_guid=guid, _user_id=user_id):
            session = None
            try:
                from backend.database import SessionLocal as _SL
                from backend.emby_server import models as _em

                session = _SL()
                item = session.query(_em.MediaItem).filter(
                    _em.MediaItem.guid == _guid).first()
                item_pk = item.id if item is not None else None
                session.close()
                session = None
                if item_pk is not None:
                    # 查完就放开这条会话，预检那一秒不再占着连接。
                    _warm_thread_body(item_pk, _user_id, "detail")
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    if session is not None:
                        session.close()
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_by_guid, daemon=True, name="first-play-warm").start()
        return True
    except Exception:  # noqa: BLE001
        return False

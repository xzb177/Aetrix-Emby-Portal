# -*- coding: utf-8 -*-
"""按需媒体信息探测：把 ffprobe 的结果写回条目。

背景：全库 40 万+条目全在 Google Drive 上，全量 ffprobe 会烧穿 Drive 配额。
策略（抄 Jellyfin + 生态共识）：只在用户真正访问条目（详情页/播放）且条目缺
媒体信息时入队，后台 worker 限流探测。热门内容自然补全，冷门零成本。

本模块只做「一次探测」的纯逻辑（解析地址 → ffprobe → 写回）；
队列/限流/调度在 ``probe_worker.py``。

复用：
- ``scanner.probe_metadata``：远程 Range/双 Range/mp4-moov/MediaInfo 回退，
  线上跑了很久的探测实现，不再造第二套。
- ``mounts``：``mount://<id>/<rel>`` 解析成本机路径或带鉴权头的直链 URL。
"""

import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional

from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import mediainfo_persist as persist_lib
from backend.emby_server.scanner import probe_metadata

logger = logging.getLogger(__name__)

# 探测一次最多尝试次数（含首次）；超限标 failed，不再打扰
PROBE_MAX_ATTEMPTS = max(1, int(os.getenv("PROBE_MAX_ATTEMPTS", "3") or 3))
# 重试退避基数（秒）：60/120/240…，与 enrich_worker 同口径，保护 Drive 配额
PROBE_RETRY_BASE_SEC = max(10, int(os.getenv("PROBE_RETRY_BASE_SEC", "60") or 60))
# 退避上限（秒）：指数退避不无限增长
PROBE_RETRY_MAX_SEC = max(60, int(os.getenv("PROBE_RETRY_MAX_SEC", "21600") or 21600))
# 配额耗尽长退避（秒）：Drive 配额按 24h 窗口重置，配额类 403 永不判死，
# 24h 后再试一次（C-P1-1）。固定间隔，不指数增长，避免越等越久。
PROBE_QUOTA_RETRY_SEC = 24 * 3600
# 这些 HTTP 状态码说明地址本身有问题，重试没用，直接判 failed。
# 注意：
# - 403 不在此列——403 一律按配额耗尽处理（见 probe_one / probe_worker.process_item），
#   走 24h 长退避重试，永不判死。
# - 401 也不在此列——SA token 轮换间隙的 401 是暂时的（见 #575），给
#   PROBE_AUTH_MAX_ATTEMPTS 次带退避重试机会，不再一次判死。
NO_RETRY_HTTP_CODES = frozenset({400, 404, 410})
# 临时性限流 / 服务端抖动：走 24h 长退避，永不判死（仿 C-P1-1 的 quota 处理）。
# 429 明确是被限流；500/502/503/504 多为服务端临时故障，重试有意义。
# 501（未实现）等明确永久性的错误不在此列，仍走普通重试→判死。
TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
# 401 认证失败的最大尝试次数（含首次）：首次 401 → 退避重试 1 次 → 再 401 才判死。
# 比普通 PROBE_MAX_ATTEMPTS 更严格——真权限问题的代价只是多一次探测。
PROBE_AUTH_MAX_ATTEMPTS = 2


def _probe_url(url: str) -> str:
    """探测用的直链修正（与播放链路 streaming.py 同口径）。

    ``.strm`` 里存的是 ``drive.google.com/uc?export=download&id=…`` legacy 直链，
    即使带上 SA 的 Bearer <redacted> 也只返回登录页/HTML——ffprobe 读到的是 HTML
    而不是视频，format 为空，白白记 failed（生产 99% 的探测失败都是这个原因）。
    探测走 Drive API ``alt=media`` 端点才能用 token 下载私有文件且支持 Range；
    非 uc 格式的 URL 原样返回，转换失败也不阻断探测。
    """
    try:
        from backend.emby_server import drive_auth
        return drive_auth.drive_api_media_url(url)
    except Exception:  # noqa: BLE001 — 转换失败就用原地址
        return url


def classify_resolve_error(exc: Exception) -> Optional[str]:
    """把解析阶段异常归类为可恢复的错误 kind。

    挂载 provider 的 ``resolve_final`` 在远端限流 / 服务抖动时会抛出带 HTTP
    状态码的异常（如 ``MountError("...HTTP 429")``）。以前这里被吞掉，调用方
    只能看到"无法解析探测地址"，3 次普通重试后判死——限流类错误被误判成永久
    失败。归类后调用方可对限流类走 24h 长退避。

    返回：None（未知/不可恢复）、"quota"、"ratelimit"、"server"、"auth"、"not_found"。
    """
    msg = f"{type(exc).__name__}: {exc}"
    m = re.search(r"HTTP\s*(\d{3})", msg)
    code = int(m.group(1)) if m else None
    low = msg.lower()
    if code == 403 or "ratelimitexceeded" in low or "quotaexceeded" in low \
            or "配额" in msg or "quota" in low:
        return "quota"
    if code == 429:
        return "ratelimit"
    if code in (500, 502, 503, 504):
        return "server"
    if code == 401:
        return "auth"
    if code == 404:
        return "not_found"
    return None


def resolve_probe_input(db, item) -> tuple:
    """把条目的 ``file_path`` 解析成 ``(resolved, error_kind)``。

    - ``resolved``：``(path, headers, size, container)`` 或 None。
      ``mount://<id>/<rel>``：经挂载 provider ``resolve_final`` 拿到直链 URL +
      鉴权头（Drive 的 alt=media 要 Authorization 头，ffprobe 靠 headers 传）。
      本机路径：直接返回，headers 为空。
    - ``error_kind``：解析失败时的错误归类（见 ``classify_resolve_error``），
      成功时为 None。调用方据此决定走普通重试还是 24h 长退避。

    解析失败（挂载被删/直链拿不到/文件不存在）resolved 为 None，调用方记重试。
    """
    fp = (getattr(item, "file_path", None) or "").strip()
    if not fp:
        return None, None
    size = int(getattr(item, "size", 0) or 0)
    container = (getattr(item, "container", None) or "").strip()

    parsed = mount_lib.parse_mount_path(fp)
    if parsed:
        mount_id, rel = parsed
        mount = db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()
        if mount is None:
            logger.warning("按需探测：挂载 %s 不存在 item=%s", mount_id, getattr(item, "id", "?"))
            return None, None
        try:
            provider = mount_lib.build_provider(mount, db)
            target = provider.resolve_final(rel)
        except Exception as exc:  # noqa: BLE001 — 挂载异常走重试，不抛给调用方
            kind = classify_resolve_error(exc)
            logger.warning("按需探测：直链解析失败 item=%s rel=%s kind=%s: %s",
                           getattr(item, "id", "?"), rel, kind, exc)
            return None, kind
        if not target or not getattr(target, "value", None):
            return None, None
        headers = dict(getattr(target, "headers", None) or {})
        return (_probe_url(target.value), headers, size, container), None

    # 本机 .strm：内容是直链，探测直链而不是这个文本文件（以前 ffprobe 读 .strm 文本
    # 必然失败，3 次后判 failed，白占名额）
    if fp.lower().endswith(".strm"):
        try:
            target = mount_lib.local_play_target(fp)
        except Exception as exc:  # noqa: BLE001
            logger.warning("按需探测：STRM 解析失败 item=%s: %s", getattr(item, "id", "?"), exc)
            return None, classify_resolve_error(exc)
        if getattr(target, "kind", "") == "url" and target.value:
            return (_probe_url(target.value), dict(target.headers or {}), size, container), None
        return None, None

    # 本机文件
    try:
        if os.path.isfile(fp):
            return (fp, {}, size, container), None
    except OSError:
        pass
    logger.warning("按需探测：本机文件不存在 item=%s path=%s",
                   getattr(item, "id", "?"), fp[:120])
    return None, None


def _has_useful_data(probe: dict) -> bool:
    """探测结果里有没有值得写回的东西（任一关键字段非空即可）。"""
    if not probe:
        return False
    return bool(probe.get("video_codec") or probe.get("width") or probe.get("duration_ticks"))


_STREAM_TYPES = {"video": "Video", "audio": "Audio", "subtitle": "Subtitle"}


def has_internal_streams(db, item) -> bool:
    """条目有没有内封轨道行（外挂字幕不算）。"""
    return db.query(em.MediaStream.id).filter(
        em.MediaStream.item_id == item.id,
        em.MediaStream.is_external.isnot(True),
    ).first() is not None


def replace_streams(db, item, streams) -> int:
    """用探测结果重建内封轨道（外挂字幕行不动）。返回写入条数。

    以前按需探测只把编码/宽高/时长写到条目上，**从不写轨道行**——scan 阶段不探测的
    库（strm / 远程挂载）MediaSources[].MediaStreams 永远是空的，客户端「媒体信息」
    只剩一个路径。口径与 scanner 写轨道一致（列白名单 + 超长截断）。
    """
    from backend.emby_server.scanner import _PROBE_STREAM_COLS

    rows = []
    for s in streams or []:
        if not isinstance(s, dict):
            continue
        stype = _STREAM_TYPES.get(str(s.get("stream_type") or "").lower())
        if not stype:
            continue  # data / attachment 轨不给客户端
        rows.append((stype, s))
    if not rows:
        return 0
    db.query(em.MediaStream).filter(
        em.MediaStream.item_id == item.id,
        em.MediaStream.is_external.isnot(True),
    ).delete(synchronize_session=False)
    # 外挂字幕的 index 不能撞：内封轨道的 index 就是 ffprobe 的原序号，
    # 外挂字幕历史上按 max(index)+n 分配；撞了就把外挂的整体挪到内封之后
    ext = db.query(em.MediaStream).filter(
        em.MediaStream.item_id == item.id,
        em.MediaStream.is_external.is_(True),
    ).order_by(em.MediaStream.stream_index).all()
    used = set()
    for stype, s in rows:
        st = em.MediaStream(item_id=item.id, **{
            k: v for k, v in s.items() if k in _PROBE_STREAM_COLS})
        st.stream_type = stype
        em.sanitize_stream_strings(st)
        db.add(st)
        used.add(st.stream_index)
    if ext and any(e.stream_index in used for e in ext):
        nxt = max(used) + 1 if used else 0
        for e in ext:
            e.stream_index = nxt
            nxt += 1
    return len(rows)


def write_back(db, item, probe: dict) -> None:
    """把探测结果写回条目（只写非空值，不覆盖已有有效数据）。

    - 编码：有值才写。
    - 宽/高/码率/大小/时长：>0 才写（0 可能是探测没拿到，不能把库里已有的清掉）。
    - 成功清零 attempts；degraded（信息不全但可播）同样落库，下次不再白跑。
    """
    if probe.get("video_codec"):
        item.video_codec = probe["video_codec"]
    if probe.get("audio_codec"):
        item.audio_codec = probe["audio_codec"]
    for field in ("width", "height", "bitrate", "size"):
        try:
            value = int(probe.get(field) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            setattr(item, field, value)
    try:
        ticks = int(probe.get("duration_ticks") or 0)
    except (TypeError, ValueError):
        ticks = 0
    if ticks > 0:
        item.duration_ticks = ticks
    if probe.get("moov_position"):
        item.moov_position = probe["moov_position"]
    try:
        replace_streams(db, item, probe.get("streams"))
    except Exception as exc:  # noqa: BLE001 — 轨道写失败不影响条目级信息落库
        logger.warning("按需探测：轨道写库失败 item=%s: %s", getattr(item, "id", "?"), exc)
    item.last_probed_at = datetime.now()
    item.probe_attempts = 0
    item.probe_next_retry_at = None
    item.probe_status = "degraded" if probe.get("_degraded") else "done"
    _clear_claim(item, error=None)
    db.commit()
    logger.info("按需探测成功 item=%s %s %sx%s audio=%s",
                item.id, item.video_codec or "?", item.width or "?",
                item.height or "?", item.audio_codec or "?")


def _clear_claim(item, error: Optional[str]) -> None:
    """释放抢单租约，记录（或清空）最近失败原因。老库没这两列时静默跳过。"""
    if hasattr(type(item), "probe_claimed_at"):
        item.probe_claimed_at = None
    if hasattr(type(item), "probe_last_error"):
        item.probe_last_error = (error or "")[:250] or None


def mark_failed(db, item, reason: str) -> None:
    """判死：不再重试。"""
    item.probe_status = "failed"
    item.probe_next_retry_at = None
    _clear_claim(item, error=reason)
    db.commit()
    logger.warning("按需探测放弃 item=%s（%s 次）: %s",
                   item.id, item.probe_attempts or 0, reason)


def mark_long_retry(db, item, reason: str, kind: str = "quota") -> None:
    """长退避重试：配额耗尽 / 限流 / 服务端抖动 / 持续超时等临时性故障专用。

    - 状态保持 pending，``PROBE_QUOTA_RETRY_SEC`` 约 24h（±30% 抖动）后再试；
    - 不增加 ``probe_attempts``（不消耗普通重试次数）；
    - 永不判死——配额恢复 / 限流解除 / 挂载恢复后自动自愈（C-P1-1）。
    - ``kind`` 仅用于日志与 ``probe_last_error`` 标记：
      "quota" / "timeout" / "ratelimit"（429）/ "server"（5xx）。

    防负优化：24h 一次的重试对 Drive 配额 / worker 槽位的影响可忽略；
    系统性挂载故障由 ``probe_worker.MountBreaker`` 熔断限流，不会打爆。
    """
    item.probe_status = "pending"
    # ±30% 随机抖动，打散重试脉冲（反风控）
    item.probe_next_retry_at = datetime.now() + timedelta(
        seconds=int(PROBE_QUOTA_RETRY_SEC * random.uniform(0.7, 1.3)))
    _clear_claim(item, error=f"[{kind}] {reason}（约24h后重试）")
    db.commit()
    logger.info("按需探测长退避 item=%s kind=%s（约24h后重试）: %s",
                item.id, kind, reason)


def mark_retry(db, item, reason: str, *, is_timeout: bool = False,
               max_attempts: Optional[int] = None) -> None:
    """记一次失败：次数+1，指数退避；超限转 failed。

    ``is_timeout=True``（ffprobe / 解析超时）时，超限后转 24h 长退避而不判死——
    超时多为挂载 / 网络临时故障，判死后只能人工 reset 才能恢复（C-P1-1 超时部分）。

    ``max_attempts`` 覆盖默认的 ``PROBE_MAX_ATTEMPTS``（如 401 认证失败只给
    ``PROBE_AUTH_MAX_ATTEMPTS`` 次机会）。计数器 ``probe_attempts`` 全局共享。

    退避带 ±20% 随机抖动：反风控（用户硬性要求，见 drive_changes._retry_sleep_sec），
    绝不固定间隔，也绝不接近 0。
    """
    limit = max_attempts if max_attempts else PROBE_MAX_ATTEMPTS
    attempts = (getattr(item, "probe_attempts", 0) or 0) + 1
    item.probe_attempts = attempts
    if attempts >= limit:
        if is_timeout:
            mark_long_retry(db, item, f"{reason}（已达 {limit} 次上限）",
                            kind="timeout")
            return
        mark_failed(db, item, f"{reason}（已达 {limit} 次上限）")
        return
    delay = min(PROBE_RETRY_MAX_SEC, PROBE_RETRY_BASE_SEC * (2 ** (attempts - 1)))
    delay = int(delay * random.uniform(0.8, 1.2))
    item.probe_status = "pending"
    item.probe_next_retry_at = datetime.now() + timedelta(seconds=delay)
    _clear_claim(item, error=reason)
    db.commit()
    logger.info("按需探测失败 item=%s（%d/%d，%ds 后重试）: %s",
                item.id, attempts, limit, delay, reason)


def probe_one(db, item) -> str:
    """对单个条目做一次探测，返回终态：done/degraded/failed/pending（待重试）。

    对标 StrmAssistant 的 ``OrchestrateMediaInfoProcessAsync``：
    持久化开启时先试 ``deserialize``（读 ``-mediainfo.json``），命中则零探测
    直接返回；未命中才走 ffprobe；探完自动 ``serialize`` 落盘。

    调用方需先把条目置为 probing（防重入）；本函数只负责探测与状态流转。
    任何异常都不抛给调用方——探测失败永远不能影响播放/详情页。
    """
    # 1. 先试 JSON 恢复（零探测）
    try:
        # 老版本落盘的 JSON 没有轨道：恢复了条目级信息但没有轨道行时继续走 ffprobe，
        # 否则「媒体信息」永远只有编码一行
        if persist_lib.deserialize(db, item) and has_internal_streams(db, item):
            return "done"
    except Exception as exc:  # noqa: BLE001 — 恢复失败就走正常探测
        logger.debug("媒体信息 JSON 恢复异常 item=%s，走 ffprobe: %s",
                     getattr(item, "id", "?"), exc)

    # .iso 是光盘镜像（UDF/ISO9660 文件系统）：ffprobe 没有 ISO 解复用器，
    # 怎么探都拿不到流信息。直接记 degraded 终态，不发起任何网络探测
    # （93GB 级文件一次 ffprobe 会烧掉大量 Drive 配额），客户端照常可播。
    if (getattr(item, "container", "") or "").lower() == "iso":
        item.probe_status = "degraded"
        item.probe_next_retry_at = None
        _clear_claim(item, error="iso 光盘镜像：ffprobe 无 ISO 解复用器，不探测流信息")
        db.commit()
        logger.info("探测跳过 iso 镜像 item=%s：直接记 degraded",
                    getattr(item, "id", "?"))
        return "degraded"

    resolved, resolve_kind = resolve_probe_input(db, item)
    if not resolved:
        # 解析阶段透出原始错误类型：限流 / 服务端抖动类走 24h 长退避，
        # 不再一律 3 次普通重试后判死。
        if resolve_kind == "quota":
            mark_long_retry(db, item, "解析探测地址：远端配额耗尽", kind="quota")
        elif resolve_kind == "ratelimit":
            mark_long_retry(db, item, "解析探测地址：远端限流（HTTP 429）",
                            kind="ratelimit")
        elif resolve_kind == "server":
            mark_long_retry(db, item, "解析探测地址：远端服务异常", kind="server")
        elif resolve_kind == "auth":
            mark_retry(db, item, "解析探测地址：远端认证失败（HTTP 401）",
                       max_attempts=PROBE_AUTH_MAX_ATTEMPTS)
        else:
            mark_retry(db, item, "无法解析探测地址")
        return item.probe_status

    path, headers, size, container = resolved
    try:
        probe = probe_metadata(path, headers, size=size, container=container)
    except Exception as exc:  # noqa: BLE001
        mark_retry(db, item, f"ffprobe 异常: {exc}")
        return item.probe_status

    http_code = (probe or {}).get("_http_code")
    err = (probe or {}).get("_error")
    # 配额 403 永不判死：走 24h 长退避，配额恢复后自动自愈（C-P1-1）。
    # scanner.probe_metadata 把 403 统一标为 _error="quota"；即使 _error 缺失，
    # 403 本身也按配额处理（真权限问题的代价只是 24h 后多一次探测，可接受）。
    if err == "quota" or http_code == 403:
        mark_long_retry(db, item, "远端配额耗尽（HTTP 403）", kind="quota")
        return item.probe_status
    # 429 限流 / 5xx 服务端抖动：同样走 24h 长退避，永不判死。
    # 以前这类错误走普通重试，3 次后被永久判死（与"无时长"策略失败混在一起）。
    if err == "ratelimit" or http_code == 429:
        mark_long_retry(db, item, "远端限流（HTTP 429），约24h后重试",
                        kind="ratelimit")
        return item.probe_status
    if err == "server_error" or http_code in TRANSIENT_HTTP_CODES:
        mark_long_retry(db, item, f"远端服务异常（HTTP {http_code}），约24h后重试",
                        kind="server")
        return item.probe_status
    # 401 认证失败：SA token 轮换间隙的 401 是暂时的，给 2 次带退避重试机会，
    # 不再一次判死。
    if err == "auth" or http_code == 401:
        mark_retry(db, item, f"远端认证失败（HTTP 401）",
                   max_attempts=PROBE_AUTH_MAX_ATTEMPTS)
        return item.probe_status
    if http_code in NO_RETRY_HTTP_CODES:
        mark_failed(db, item, f"远端返回 HTTP {http_code}，地址无效")
        return item.probe_status

    if not _has_useful_data(probe):
        mark_retry(db, item, "探测未返回有效媒体信息")
        return item.probe_status

    write_back(db, item, probe)
    # 2. 探完落盘（对标 SerializeMediaInfo）
    try:
        persist_lib.serialize(db, item)
    except Exception as exc:  # noqa: BLE001 — 落盘失败不影响已写库的结果
        logger.debug("媒体信息落盘异常 item=%s: %s", getattr(item, "id", "?"), exc)
    return item.probe_status

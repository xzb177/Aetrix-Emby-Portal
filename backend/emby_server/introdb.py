# -*- coding: utf-8 -*-
"""IntroDB 自动片头片尾（2026-10-17 用户拍板，学 raisemby 接入）。

只读众包库 ``api.introdb.app`` 的分段数据（免 key、匿名读），按需拉取 +
本地表 ``emby_intro_segments`` 缓存，汇入 intro_marker 的现成下发管道
（``to_chapters``），不另起一套下发机制。

故障隔离是第一要求：上游挂掉、超时、返回垃圾，全部在这里吞掉记 debug，
调用方（playback_info）永远当作「无分段」继续——绝不能拖垮播放信息。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

API_BASE = "https://api.introdb.app"
SEGMENTS_PATH = "/segments"
# 上游的分段类型（与我们 to_chapters 的 marker_type 直接对齐）
SEGMENT_TYPES = ("intro", "recap", "outro", "credits", "post-credits")
# 负缓存哨兵行：拉过但上游没分段，避免每次播放都外呼
_NEGATIVE_SENTINEL = "none"
# 缓存新鲜期：IntroDB 数据基本不变，正缓存 30 天、负缓存 7 天
POSITIVE_TTL = timedelta(days=30)
NEGATIVE_TTL = timedelta(days=7)
# 外呼上限：connect 1.5s / read 3s（绝不让上游拖住播放请求）
_CONNECT_TIMEOUT = 1.5
_READ_TIMEOUT = 3.0

# 按条目类型要哪些分段（设计口径）：电影只要片尾，剧集要片头/前情/片尾
_MOVIE_SEGMENTS = ("outro", "post-credits")
_EPISODE_SEGMENTS = ("intro", "recap", "outro")


def fetch_segments(imdb_id: str, season: Optional[int] = None,
                   episode: Optional[int] = None,
                   is_movie: bool = False) -> Optional[list[dict]]:
    """从 IntroDB 拉分段。任何异常（网络/超时/坏响应）返回 None，不抛。

    返回值：[{"segment_type": str, "start_ms": int, "end_ms": int}]，无分段
    时返回空列表（上游 200 但没数据 ≠ 出错）。
    """
    import httpx

    imdb_id = (imdb_id or "").strip()
    if not imdb_id:
        return None
    params: dict = {"imdb_id": imdb_id}
    if is_movie:
        params["is_movie"] = "true"
    elif season is not None and episode is not None:
        params["season"] = str(season)
        params["episode"] = str(episode)
    else:
        # 剧集缺季/集号：上游查不到，省一次外呼
        return None
    try:
        resp = httpx.get(
            API_BASE + SEGMENTS_PATH,
            params=params,
            headers={"User-Agent": "Aetrix/1.0 (+introdb-segments)"},
            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
        )
        if resp.status_code != 200:
            logger.debug("IntroDB 非 200 imdb=%s: %s", imdb_id, resp.status_code)
            return None
        return _parse_segments(resp.json())
    except Exception as exc:  # noqa: BLE001 - 上游任何故障都不外传
        logger.debug("IntroDB 拉取失败 imdb=%s: %s", imdb_id, exc)
        return None


def _parse_segments(payload) -> list[dict]:
    """解析 /segments 响应为毫秒分段。坏条目跳过、不抛。"""
    if isinstance(payload, dict):
        items = payload.get("segments", [])
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        stype = str(it.get("segment_type") or "").strip().lower()
        if stype not in SEGMENT_TYPES:
            continue
        try:
            start = float(it["start_sec"])
            end = float(it["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= start < end):
            continue
        out.append({
            "segment_type": stype,
            "start_ms": int(start * 1000),
            "end_ms": int(end * 1000),
        })
    return out


def _query_kind(item) -> Optional[str]:
    """条目适合查哪种分段：movie / episode；其他返回 None（季/剧本级不做）。"""
    return getattr(item, "item_type", None) if getattr(item, "item_type", None) in ("movie", "episode") else None


def get_segments_for_item(db, item) -> list[dict]:
    """取条目的 IntroDB 分段（带缓存）。全程 fail-safe，失败返回 []。

    流程：开关关/无 imdb_id/类型不对 → []；缓存新鲜 → 直接用；
    否则同步拉一次（带超时），拉到落库（替换该条目旧行），拉不到不写。
    """
    try:
        from backend.emby_server import playback_strategy as _ps
        from backend.emby_server import models as em
    except Exception:  # noqa: BLE001
        return []
    try:
        if not _ps.introdb_enabled(db):
            return []
        imdb_id = (getattr(item, "imdb_id", "") or "").strip()
        kind = _query_kind(item)
        if not imdb_id or not kind:
            return []
        rows = (
            db.query(em.IntroSegment)
            .filter(em.IntroSegment.item_id == item.id)
            .all()
        )
        now = datetime.now()
        if rows:
            newest = max(r.fetched_at or datetime.min for r in rows)
            has_segments = any(r.segment_type != _NEGATIVE_SENTINEL for r in rows)
            if has_segments and now - newest < POSITIVE_TTL:
                return [
                    {"segment_type": r.segment_type, "start_ms": r.start_ms,
                     "end_ms": r.end_ms, "source": "introdb"}
                    for r in rows if r.segment_type != _NEGATIVE_SENTINEL
                ]
            if not has_segments and now - newest < NEGATIVE_TTL:
                return []
        # 未命中/过期：同步拉一次
        if kind == "movie":
            fetched = fetch_segments(imdb_id, is_movie=True)
        else:
            fetched = fetch_segments(
                imdb_id,
                season=getattr(item, "season_number", None),
                episode=getattr(item, "episode_number", None),
            )
        if fetched is None:
            return []  # 上游故障：不写缓存，下次播放再试
        # 落库：替换该条目的全部旧行（负缓存写哨兵行）
        try:
            db.query(em.IntroSegment).filter(
                em.IntroSegment.item_id == item.id).delete(synchronize_session=False)
            if fetched:
                for seg in fetched:
                    db.add(em.IntroSegment(
                        item_id=item.id,
                        segment_type=seg["segment_type"],
                        start_ms=seg["start_ms"],
                        end_ms=seg["end_ms"],
                        source="introdb",
                        fetched_at=now,
                    ))
            else:
                db.add(em.IntroSegment(
                    item_id=item.id, segment_type=_NEGATIVE_SENTINEL,
                    start_ms=0, end_ms=0, source="introdb", fetched_at=now))
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.debug("IntroDB 缓存落库失败 item=%s: %s", getattr(item, "id", "?"), exc)
            return []
        return [
            {**seg, "source": "introdb"} for seg in fetched
        ]
    except Exception as exc:  # noqa: BLE001 - 绝不让 IntroDB 拖垮播放
        logger.debug("IntroDB 取分段失败: %s", exc)
        return []


def merge_segments(manual_markers: list[dict], introdb_segments: list[dict],
                   item_kind: Optional[str]) -> list[dict]:
    """手动标记与 IntroDB 分段合并（纯函数）。

    - 手动优先：同类型分段手动已存在时，IntroDB 的丢掉。
    - 按条目类型过滤：电影只要 outro/post-credits，剧集要 intro/recap/outro。
    返回可直接进 ``intro_marker.to_chapters`` 的标记列表。
    """
    manual_types = {m.get("marker_type") for m in manual_markers}
    if item_kind == "movie":
        allowed = _MOVIE_SEGMENTS
    elif item_kind == "episode":
        allowed = _EPISODE_SEGMENTS
    else:
        # 季/剧本级不做：IntroDB 分段不并入
        return list(manual_markers)
    merged = list(manual_markers)
    for seg in introdb_segments:
        stype = seg.get("segment_type")
        if stype not in allowed or stype in manual_types:
            continue
        merged.append({
            "marker_type": stype,
            "start_ms": seg.get("start_ms", 0),
            "end_ms": seg.get("end_ms", 0),
            "source": "introdb",
        })
    return merged

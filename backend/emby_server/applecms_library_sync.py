# -*- coding: utf-8 -*-
"""苹果 CMS 媒体库同步工人（新媒体库类型一期·同步单）。

把库行（``emby_libraries.kind == "applecms"``）配置的 CMS 源目录
同步成托管行，供播放器经 Emby 兼容层直接浏览/点播（用户 2026-11-02
定案：新建媒体库类型、挂载退休为方向）。设计稿见
``~/workspace/reports/applecms-library-type-design-20261102.md``。

口径（全部 2026-11-02 定案）：
- 只拉 provide ``ac=list`` 分页，**同步期不拉 detail**：播放地址绝不
  落库（点播时由 mapped 取流口现取 302），年份/海报等元数据不靠
  CMS 的残缺字段，由 TMDB 补全链补。
- 身份不豁免：行建成托管行后 ``enrich_status`` 保持 ``pending``，
  现有 enrich 工人对 ``upstream_source_id`` 非空的行自动走「只走
  TMDB」补身份分支；对不上身份的行按现行货架规矩不上架，本模块
  不做任何货架过滤。
- 去重：标题+年份键（``aggregate_key_for``），一行多映射；行的
  ``upstream_source_id`` 是先到源（主来源），其余源只挂映射。
- 一期只做电影：剧集形态（``vod_serial`` 解析出集数 > 1）跳过并
  计数，集行结构由三单预留；本工人只处理 movies 库（series 库
  一期不开，遇到直接 blocked=unsupported_collection）。
- 分类：``cms_type_map`` 按**顶级分类名**判定（子类沿 provide 的
  ``class`` 分类树回溯），``"out"`` 跳过、缺省收（空 = 全收）。
- 映射开关关 = 整批 blocked 不建行（没映射的行播不了，不建孤儿行，
  照 ``upstream_import`` 前置门口径）。
- 增量走 provide ``h=24`` 窗口（调度日跑）；全量不带 ``h``（月度
  对账单用）。每源独立 commit，单源失败不回滚其它源。
- 进度写回库行 ``last_scan_at`` / ``scan_stats``（标准计数键 +
  ``cms_sync`` 明细），管理端列表的最近同步结果直接可见。

调度：``APPLECMS_LIBRARY_SYNC_ENABLED`` 默认关（新能力灰度，照
既有苹果 CMS 同步的双闸先例）；间隔
``APPLECMS_LIBRARY_SYNC_INTERVAL_SEC`` 默认 21600（6 小时，轮内
h=24 窗口保证增量不漏）。分页间隔复用配对工人的
``APPLECMS_SYNC_PAGE_DELAY_SEC``（默认 0.5 秒）防触发源站限速。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import applecms_library as acl
from backend.emby_server import applecms_sync as acs
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_import as uimp
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as us
from backend.emby_server.pinyin_sort import make_sort_name
from backend.emby_server.upstream_playback import _source_headers

logger = logging.getLogger(__name__)

#: 库同步总开关 env 键（默认关）。
SYNC_ENABLED_ENV = "APPLECMS_LIBRARY_SYNC_ENABLED"
#: 同步调度间隔 env 键（秒），默认 6 小时。
SYNC_INTERVAL_ENV = "APPLECMS_LIBRARY_SYNC_INTERVAL_SEC"
DEFAULT_SYNC_INTERVAL_SEC = 21600.0
#: 增量窗口（小时）：provide ``h=24`` 只回最近 24 小时更新的条目。
INCREMENTAL_WINDOW_HOURS = 24
#: CMS 行在 enrich 队列的优先级：低于本地补全（默认 0），防海量
#: CMS 行淹没本地库的补身份队列；仍会被轮转认领，不饿死。
CMS_ENRICH_PRIORITY = -10
#: 映射置信度/证据：与配对工人同口径（标题+年份）。
CMS_MAPPING_CONFIDENCE = 0.7
CMS_MAPPING_EVIDENCE = "title+year"

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_lock = threading.Lock()


def sync_enabled() -> bool:
    return (os.environ.get(SYNC_ENABLED_ENV) or "").strip() == "1"


def sync_interval_sec(explicit: Optional[float] = None) -> float:
    if explicit is not None:
        return max(1.0, float(explicit))
    raw = (os.environ.get(SYNC_INTERVAL_ENV) or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_SYNC_INTERVAL_SEC
    except ValueError:
        value = DEFAULT_SYNC_INTERVAL_SEC
    return max(60.0, value)


def _zero_stats(library_id: int) -> Dict[str, object]:
    return {
        "library_id": library_id, "blocked": None,
        "added": 0, "updated": 0, "existing": 0, "mapped": 0,
        "pages": 0, "skipped_category": 0, "skipped_series_shape": 0,
        "skipped_invalid": 0, "skipped_other_library": 0,
        "dedupe_hits": 0, "errors": [], "seen_by_source": {},
    }


# ---------------------------------------------------------------------------
# 分类树与条目判定（纯函数）
# ---------------------------------------------------------------------------

def class_top_names(payload: dict) -> Dict[int, str]:
    """provide ``class`` 分类树 → {type_id: 顶级分类名}。

    子类沿 ``type_pid`` 回溯到 pid 为 0 的根；树缺失/成环时退回
    条目自身的 ``type_name``（调用方兜底），不猜更深的结构。
    """
    nodes: Dict[int, tuple] = {}
    for node in (payload or {}).get("class") or []:
        if not isinstance(node, dict):
            continue
        try:
            tid = int(node.get("type_id"))
        except (TypeError, ValueError):
            continue
        try:
            pid = int(node.get("type_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        nodes[tid] = (pid, str(node.get("type_name") or "").strip())
    tops: Dict[int, str] = {}
    for tid, (_pid, name) in nodes.items():
        cur, seen = tid, set()
        while cur in nodes and cur not in seen:
            seen.add(cur)
            parent, parent_name = nodes[cur]
            if parent == 0 or parent not in nodes:
                tops[tid] = parent_name if parent == 0 else name
                break
            cur = parent
        else:
            tops[tid] = name
    return tops


def _entry_top_category(entry: dict, tops: Dict[int, str]) -> str:
    try:
        tid = int(entry.get("type_id"))
    except (TypeError, ValueError):
        tid = None
    if tid is not None and tid in tops:
        return tops[tid]
    return str(entry.get("type_name") or "").strip()


def _is_series_shaped(entry: dict) -> bool:
    """剧集形态判定：``vod_serial`` 是当前集数，>1 即多集条目。

    结构信号（计数）而非内容名单：电影条目的 vod_serial 恒为
    0/1/空。一期电影库跳过这类条目，集行由三单处理。
    """
    raw = str(entry.get("vod_serial") or "").strip()
    if not raw:
        return False
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return False
    return int(digits) > 1


# ---------------------------------------------------------------------------
# 单库同步
# ---------------------------------------------------------------------------

def sync_library(db: Session, library, *, http_get=None, full: bool = False,
                 max_pages: Optional[int] = None,
                 sleep_fn: Optional[Callable[[float], None]] = None
                 ) -> Dict[str, object]:
    """同步一个 CMS 库的全部配置源（一期仅电影）。返回统计 dict。

    ``full=False`` 为增量轮（h=24 窗口）；``full=True`` 全量（对账
    单用，seen_by_source 才是完整的源目录清单）。本函数不查总开关
    （开关在调度层，照配对工人口径）；映射开关关时整批 blocked。
    """
    stats = _zero_stats(int(library.id))
    if not acl.is_applecms_library(library):
        stats["blocked"] = "not_applecms_library"
        return stats
    if (getattr(library, "collection_type", "") or "") != "movies":
        # 一期只做电影；剧集库不开，集行三单落地后再放行。
        stats["blocked"] = "unsupported_collection"
        return stats
    if not uim.mapping_enabled():
        stats["blocked"] = "mapping_off"
        return stats
    source_ids = acl.parse_upstream_source_ids(
        getattr(library, "upstream_source_ids", ""))
    if not source_ids:
        stats["blocked"] = "no_sources"
        return stats
    type_map = acl.parse_cms_type_map(
        getattr(library, "cms_type_map", "")) or {}
    started = time.monotonic()
    run_cache: Dict[str, em.MediaItem] = {}
    for source_id in source_ids:
        _sync_source(db, library, source_id, stats, run_cache,
                     type_map=type_map, http_get=http_get, full=full,
                     max_pages=max_pages, sleep_fn=sleep_fn)
    _write_progress(db, library, stats, started)
    return stats


def _sync_source(db: Session, library, source_id: int, stats: dict,
                 run_cache: dict, *, type_map: dict, http_get,
                 full: bool, max_pages, sleep_fn) -> None:
    source = us.get_source(db, source_id)
    if source is None:
        stats["errors"].append(f"source_missing:{source_id}")
        return
    if getattr(source, "source_type", None) != "applecms":
        stats["errors"].append(f"source_not_applecms:{source_id}")
        return
    if not getattr(source, "enabled", False):
        stats["errors"].append(f"source_disabled:{source_id}")
        return
    headers = _source_headers(source)
    seen: List[str] = []
    stats["seen_by_source"][source_id] = seen
    tops: Dict[int, str] = {}
    delay = acs._page_delay_sec()
    do_sleep = sleep_fn if sleep_fn is not None else time.sleep
    page_limit = max_pages or acf.DEFAULT_MAX_PAGES
    pg = 1
    try:
        while pg <= page_limit:
            payload = acf.fetch_page(
                source.base_url, ac="list", pg=pg,
                h=None if full else INCREMENTAL_WINDOW_HOURS,
                http_get=http_get, headers=headers)
            stats["pages"] += 1
            if pg == 1:
                tops = class_top_names(payload)
            entries = [e for e in (payload.get("list") or [])
                       if isinstance(e, dict)]
            for entry in entries:
                _process_entry(db, library, source, entry, stats,
                               run_cache, type_map=type_map, tops=tops,
                               seen=seen)
            try:
                pagecount = int(payload.get("pagecount") or 0)
            except (TypeError, ValueError):
                pagecount = 0
            if not entries or (pagecount and pg >= pagecount):
                break
            pg += 1
            if delay > 0:
                do_sleep(delay)
        db.commit()
    except acf.AppleCMSError as exc:
        db.rollback()
        stats["errors"].append(
            f"source_error:{source_id}:{exc.kind}")
        logger.warning(
            "applecms 库同步单源失败 library_id=%s source_id=%s kind=%s",
            library.id, source_id, exc.kind)
    except Exception:
        db.rollback()
        stats["errors"].append(f"source_error:{source_id}:unexpected")
        logger.exception(
            "applecms 库同步单源异常 library_id=%s source_id=%s",
            library.id, source_id)


def _process_entry(db: Session, library, source, entry: dict,
                   stats: dict, run_cache: dict, *, type_map: dict,
                   tops: dict, seen: List[str]) -> None:
    vod_id = str(entry.get("vod_id") or "").strip()
    if not vod_id:
        stats["skipped_invalid"] += 1
        return
    seen.append(vod_id)
    top = _entry_top_category(entry, tops)
    if type_map.get(top) == "out":
        stats["skipped_category"] += 1
        return
    if _is_series_shaped(entry):
        stats["skipped_series_shape"] += 1
        return
    record = acs.cms_entry_to_record(entry, source_id=str(source.id))
    if record is None:
        stats["skipped_invalid"] += 1
        return
    key = agg.aggregate_key_for(record)
    row = run_cache.get(key)
    if row is None:
        row = _find_row(db, library, source, vod_id, record, key)
        if row is not None:
            run_cache[key] = row
    if row is _OTHER_LIBRARY:
        stats["skipped_other_library"] += 1
        return
    if row is None:
        row = _create_row(db, library, source, vod_id, record)
        run_cache[key] = row
        stats["added"] += 1
    else:
        if _refresh_row(row, record):
            stats["updated"] += 1
        else:
            stats["existing"] += 1
        if row.upstream_source_id != source.id:
            stats["dedupe_hits"] += 1
    uim.record_mapping(
        db, local_item_id=row.id, source_id=source.id,
        upstream_item_id=vod_id,
        confidence=CMS_MAPPING_CONFIDENCE,
        evidence=CMS_MAPPING_EVIDENCE)
    stats["mapped"] += 1


_OTHER_LIBRARY = object()


def _find_row(db: Session, library, source, vod_id: str, record,
              key: str):
    """找已有行：先按本源映射（同源重跑）→ 再按 guid → 再按去重键。

    guid 命中但行属别的库时返回 ``_OTHER_LIBRARY`` 哨兵：一个源
    一期只喂一个库，跨库共用同一 guid 行会把行在两库间扯动，
    宁可跳过计数，不抢行。
    """
    mapping = (
        db.query(em.UpstreamItemMapping)
        .filter(em.UpstreamItemMapping.source_id == source.id,
                em.UpstreamItemMapping.upstream_item_id == vod_id,
                em.UpstreamItemMapping.episode_index.is_(None))
        .first())
    if mapping is not None:
        row = db.get(em.MediaItem, mapping.local_item_id)
        if row is not None and row.library_id == library.id:
            return row
    row = uimp._usable_row_by_guid(db, uimp.hosted_guid(source.id, vod_id))
    if row is not None:
        return row if row.library_id == library.id else _OTHER_LIBRARY
    candidates = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.library_id == library.id,
                em.MediaItem.item_type == "movie",
                em.MediaItem.name == record.name)
        .all())
    for cand in candidates:
        cand_record = agg.UpstreamCatalogRecord(
            source_id=str(source.id), upstream_item_id=vod_id,
            name=cand.name or "", item_type="Movie",
            production_year=cand.production_year,
            tmdb_id=cand.tmdb_id or None,
            imdb_id=cand.imdb_id or None)
        if agg.aggregate_key_for(cand_record) == key:
            return cand
    return None


def _create_row(db: Session, library, source, vod_id: str,
                record) -> em.MediaItem:
    row = em.MediaItem(
        guid=uimp.hosted_guid(source.id, vod_id),
        library_id=library.id,
        item_type="movie",
        name=record.name,
        sort_name=make_sort_name(record.name),
        production_year=record.production_year,
        upstream_source_id=source.id,
        probe_status="done",
        enrich_priority=CMS_ENRICH_PRIORITY,
    )
    db.add(row)
    db.flush()
    return row


def _refresh_row(row: em.MediaItem, record) -> bool:
    """重见时刷新可变字段（名字/年份）；有变化返回 True。

    只填新值非空且不同的字段，绝不清空已有值；身份字段
    （tmdb_id 等）归 enrich 链管，本工人不碰。
    """
    changed = False
    if record.name and row.name != record.name:
        row.name = record.name
        row.sort_name = make_sort_name(record.name)
        changed = True
    if (record.production_year is not None
            and row.production_year != record.production_year):
        row.production_year = record.production_year
        changed = True
    return changed


def _write_progress(db: Session, library, stats: dict,
                    started: float) -> None:
    detail = {
        "mapped": stats["mapped"], "pages": stats["pages"],
        "skipped_category": stats["skipped_category"],
        "skipped_series_shape": stats["skipped_series_shape"],
        "skipped_invalid": stats["skipped_invalid"],
        "skipped_other_library": stats["skipped_other_library"],
        "dedupe_hits": stats["dedupe_hits"],
        "errors": list(stats["errors"]),
        "sources": list(stats["seen_by_source"].keys()),
    }
    payload = {
        "added": stats["added"], "updated": stats["updated"],
        "unchanged": stats["existing"], "removed": 0,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "cms_sync": detail,
    }
    try:
        library.last_scan_at = datetime.now()
        library.scan_status = "failed" if (
            stats["errors"] and not stats["seen_by_source"]
        ) else ("partial" if stats["errors"] else "success")
        library.scan_stats = json.dumps(payload, ensure_ascii=False)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("applecms 库同步进度写回失败 library_id=%s",
                         library.id)


# ---------------------------------------------------------------------------
# 调度（开关默认关；照 applecms_scheduler 的循环/线程形态）
# ---------------------------------------------------------------------------

def run_library_sync_round(*, session_factory=None, http_get=None,
                           full: bool = False) -> List[dict]:
    """一轮调度：同步全部已启用的 CMS 库。总开关关时直接返回 []。"""
    if not sync_enabled():
        return []
    if session_factory is None:
        from backend.database import SessionLocal
        session_factory = SessionLocal
    results: List[dict] = []
    db = session_factory()
    try:
        libraries = (
            db.query(em.Library)
            .filter(em.Library.kind == acl.KIND_APPLECMS,
                    em.Library.is_enabled.is_(True))
            .all())
    finally:
        db.close()
    for library in libraries:
        lib_db = session_factory()
        try:
            row = lib_db.get(em.Library, library.id)
            if row is None:
                continue
            results.append(sync_library(
                lib_db, row, http_get=http_get, full=full))
        except Exception:
            logger.exception("applecms 库同步整库失败 library_id=%s",
                             library.id)
            try:
                lib_db.rollback()
            except Exception:
                pass
        finally:
            lib_db.close()
    return results


def run_scheduler_loop(stop_event=None, *, run_once=None,
                       interval_sec: Optional[float] = None) -> None:
    do_run = run_once if run_once is not None else run_library_sync_round
    interval = sync_interval_sec(interval_sec)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            do_run()
        except Exception:
            logger.exception("applecms 库同步整轮失败（下一轮继续）")
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


def start_library_sync_scheduler(stop_event=None) -> bool:
    """启动库同步调度线程；开关关或已启动时返回 False。"""
    global _scheduler_thread
    if not sync_enabled():
        return False
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return False
        thread = threading.Thread(
            target=run_scheduler_loop, args=(stop_event,),
            name="applecms-library-sync", daemon=True)
        thread.start()
        _scheduler_thread = thread
    return True

# -*- coding: utf-8 -*-
"""无界（CMS 上游）起播「暖地址」保鲜工人（一期，2026-11-05 用户亲批）。

为什么有这个工人
----------------
生产实证（emby_play_start_events 48 小时 + 容器内组件实测，见
PR #1856）：无界起播慢的大头不是解析、不是 detail 缓存未命中，
是**每播必做的同步探活**——线路 CDN 对服务器机房出口的首字节
耗时 1.9~9.7 秒，302 必须等探活验完才发。#1856 给「复播」加了
已验活地址快路：下发成功时把地址按映射 id 记入 CacheManager
（键 ``cmsvp:{mapping_id}``，窗口
``APPLECMS_VERIFIED_PLAY_TTL_SEC`` 默认 300 秒），窗口内同映射
复播免探活。但**首播**仍要全额付探活——用户体感最差的就是首播。

本工人把探活从点播路径挪到点播之前：不等用户点，后台按信号集
提前对候选线路走**与起播逐字相同**的解析＋探活
（``resolve_mapped_upstream_stream`` 定向到具体映射），解析成功
下发的那一刻，#1856 的写入点（``store_verified_play_address``）
就把地址落进同一份已验活缓存。用户随后点开，详情页线路口的
快路直接命中，首播的解析段从数秒降到一次缓存读。

衔接口径（不许漂移）
--------------------
- 只写缓存，不写任何业务数据：信号集全是只读查询，保鲜动作
  本身只有「解析 + 探活 + 缓存写入」，DB 侧零写入（解析路径
  内部的取流留痕/熔断喂数与真实点播逐字一致，是既有行为）。
- 不另起一套缓存：写入只经 upstream_playback 的既有收口，
  键与格式与 #1856 逐字一致；本模块连「读新鲜度」都走
  ``read_verified_play_address`` 同一口。
- 只覆盖苹果 CMS 型源：已验活快路只存在于 CMS detail 分支，
  非 CMS 型源的映射跳过并计数，不做无用探活。
- 不改下发地址集合 / 择优 / 熔断语义：走的就是点播同一条路，
  探活结论真假如实喂既有健康记录；源健康记录已判不可用
  （含熔断自动停用前的不可用态）→ 整源跳过不硬探。

信号集（只盯大概率要看的，优先级序，总量有帽）
----------------------------------------------
1. 追剧新集：``emby_series_follows`` 里 active 订阅的剧，
   订阅之后到货（date_added >= 订阅时间、与 series_follow
   「新集」同口径）、且落在回望窗内的单集；每剧取最新若干集。
2. 继续观看：近 N 天有播放进度（emby_user_media_data）或播放
   会话（emby_playback_sessions）的条目本体，外加单集的**下一集**
   （同剧按季/集号序的下一活集，跨季自动接首集）——续看与
   顺手点下一集是最高频的两个动作。
3. 新入库热门：入库未久、且近窗内已有若干次成功起播的条目
   （已经有人在看 = 还会有人看，包括点开者本人复访）。

资源纪律（硬约束，2026-10 定版「后台不吵主链路」）
--------------------------------------------------
- 低并发（默认 2，可配 1~8）+ 请求间随机抖动（反风控：不许
  让上游看出规律轮询）。
- 单轮双预算：总时长与总请求数（detail 抓取 + 探活 + 画质
  回填的出站请求都计数），任一耗尽即停，未轮到的记 budget。
- 让路：整轮开始前查近 5 分钟起播数与系统负载比，任一越线
  整轮跳过；轮中每条任务前复查负载，越线即停余下任务。
- 滚动保鲜：已验活窗口过期前（提前量可配）不重复验；进程内
  记每条映射的上次保鲜时刻，玩家真实点播写下的新鲜记录也
  认（读到即视为新鲜），多轮幂等。
- 失败只记日志与轮次统计，绝不冒泡；工人线程异常不退出循环。

状态可见性：每轮统计经 ``runtime_status()`` /
``status_snapshot()`` 暴露，并发布到缓存键
``aetrix:upstream_warm:status``（照 probe_worker 的形状），
管理后台任务总览据此出卡；线程存活另走 worker_registry
心跳（/api/health 可见）。
"""

import itertools
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import func

from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

#: worker_registry 登记名（/api/health 与管理总览按此名认人）。
WORKER_NAME = "upstream_warm"

#: 运行状态发布缓存键（照 probe_worker 的 STATUS_CACHE_KEY 形状）。
STATUS_CACHE_KEY = "aetrix:upstream_warm:status"
_STATUS_TTL_SEC = 1800


# ---------------------------------------------------------------------------
# 配置（全部 env 热读：改配置不用改代码，脏值回默认——与
# upstream_playback.cms_verified_play_ttl 同口径）
# ---------------------------------------------------------------------------

def _env_on(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def warm_config() -> dict:
    """保鲜工人有效配置（每轮热读一次）。

    键名与 env.example 一一对应；默认值全部保守：信号集宁可
    小、并发宁可低——保鲜是锦上添花，绝不许反客为主。
    """
    return {
        # 总开关（关 = 工人不启动、在跑的整轮让路退出）
        "enabled": _env_on("UPSTREAM_WARM_ENABLED", True),
        # 轮间隔（秒）：须小于已验活窗口（默认 300）才谈得上保鲜
        "interval_sec": env_float(
            "UPSTREAM_WARM_INTERVAL_SEC", 240.0, 30.0, 86400.0),
        # 轮内并发（条线路并行验活数）
        "concurrency": env_int("UPSTREAM_WARM_CONCURRENCY", 2, 1, 8),
        # 单轮目标上限（映射条数，三类信号合计、优先级序截断）
        "max_targets": env_int(
            "UPSTREAM_WARM_MAX_TARGETS_PER_ROUND", 120, 1, 2000),
        # 单轮总时长预算（秒）：到点不再开新任务
        "round_budget_sec": env_float(
            "UPSTREAM_WARM_ROUND_BUDGET_SEC", 600.0, 10.0, 7200.0),
        # 单轮出站请求预算（detail + 探活 + 画质回填都计数）
        "max_requests": env_int(
            "UPSTREAM_WARM_MAX_REQUESTS_PER_ROUND", 400, 1, 100000),
        # 每条目最多保鲜的线路数（展示序在前者优先）
        "mappings_per_item": env_int(
            "UPSTREAM_WARM_MAPPINGS_PER_ITEM", 2, 1, 10),
        # 保鲜提前量（秒）：已验活窗口还剩这么多时就该复验了
        "refresh_lead_sec": env_float(
            "UPSTREAM_WARM_REFRESH_LEAD_SEC", 60.0, 0.0, 3600.0),
        # 任务间随机抖动上限（秒）：0~该值均匀随机，反风控
        "jitter_sec": env_float(
            "UPSTREAM_WARM_JITTER_SEC", 3.0, 0.0, 60.0),
        # 让路判据一：近 5 分钟起播数 >= 该值 → 整轮跳过
        "busy_plays_5min": env_int(
            "UPSTREAM_WARM_BUSY_PLAYS_5MIN", 30, 1, 100000),
        # 让路判据二：1 分钟负载 / CPU 数 > 该值 → 跳过/停轮
        "load_ratio": env_float(
            "UPSTREAM_WARM_LOAD_RATIO", 0.85, 0.1, 10.0),
        # 信号一：新集回望窗（小时）与每剧集数上限
        "follow_lookback_hours": env_float(
            "UPSTREAM_WARM_FOLLOW_LOOKBACK_HOURS", 72.0, 1.0, 2160.0),
        "follow_per_series": env_int(
            "UPSTREAM_WARM_FOLLOW_PER_SERIES", 3, 1, 50),
        # 信号二：继续观看回望（天）与条目上限
        "resume_days": env_float(
            "UPSTREAM_WARM_RESUME_DAYS", 7.0, 0.5, 90.0),
        "resume_limit": env_int(
            "UPSTREAM_WARM_RESUME_LIMIT", 60, 1, 1000),
        # 信号三：热度统计窗（小时）/ 新入库界（天）/ 起播次数门槛
        "hot_window_hours": env_float(
            "UPSTREAM_WARM_HOT_WINDOW_HOURS", 48.0, 1.0, 720.0),
        "hot_new_days": env_float(
            "UPSTREAM_WARM_HOT_NEW_DAYS", 7.0, 0.5, 365.0),
        "hot_min_plays": env_int(
            "UPSTREAM_WARM_HOT_MIN_PLAYS", 3, 1, 1000),
    }


# ---------------------------------------------------------------------------
# 信号集（只读查询；软删由 ORM 全局钩子兜底，合并副本显式排除——
# 副本行不是可播入口，保鲜它纯属浪费请求）
# ---------------------------------------------------------------------------

def collect_follow_episode_ids(db, cfg, now=None) -> list:
    """信号一 · 追剧新集：返回单集条目 id（最新在前）。

    口径：active 订阅 × 订阅后到货（date_added >= 订阅 created_at，
    与 series_follow 的「新集」判定同口径）× 回望窗内；同一剧
    多个用户订阅只算一次；每剧最多取 follow_per_series 集。
    """
    from backend.emby_server import models as em

    now = now or datetime.now()
    lookback_start = now - timedelta(hours=cfg["follow_lookback_hours"])
    rows = (
        db.query(em.MediaItem.id, em.MediaItem.series_id)
        .join(em.SeriesFollow,
              em.SeriesFollow.series_item_id == em.MediaItem.series_id)
        .filter(
            em.SeriesFollow.status == "active",
            em.MediaItem.item_type == "episode",
            em.MediaItem.merged_into_id.is_(None),
            em.MediaItem.date_added.isnot(None),
            em.MediaItem.date_added >= em.SeriesFollow.created_at,
            em.MediaItem.date_added >= lookback_start,
        )
        .order_by(em.MediaItem.date_added.desc(), em.MediaItem.id.desc())
        .all())
    out, seen, per_series = [], set(), {}
    for item_id, series_id in rows:
        if item_id in seen:
            continue
        taken = per_series.get(series_id, 0)
        if taken >= cfg["follow_per_series"]:
            continue
        per_series[series_id] = taken + 1
        seen.add(item_id)
        out.append(item_id)
    return out


def next_episode_id(db, episode) -> Optional[int]:
    """同剧下一活集：按（季号，集号）序紧邻的下一集（跨季接首集）。

    季号/集号缺失（None 按 0 计季、集号缺失不推导）时返回 None——
    猜不出顺序就不猜。只认未合并的活集（软删走 ORM 钩子）。
    """
    from backend.emby_server import models as em

    series_id = getattr(episode, "series_id", None)
    ep_no = getattr(episode, "episode_number", None)
    if not series_id or ep_no is None:
        return None
    cur_key = (getattr(episode, "season_number", None) or 0, ep_no)
    rows = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.series_id == series_id,
            em.MediaItem.item_type == "episode",
            em.MediaItem.merged_into_id.is_(None),
            em.MediaItem.episode_number.isnot(None),
        )
        .all())
    best = None
    for row in rows:
        key = (row.season_number or 0, row.episode_number)
        if key > cur_key and (best is None or key < best[0]):
            best = (key, row.id)
    return best[1] if best is not None else None


def collect_resume_item_ids(db, cfg, now=None) -> list:
    """信号二 · 继续观看：返回条目 id（最近活动在前，含下一集）。

    本体 = 近 resume_days 天有播放进度（user_media_data 的
    last_played_at / updated_at 任一落在窗内）或有播放会话
    （playback_sessions.last_update_at 在窗内）的条目，按最近
    活动倒序取 resume_limit 条；随后为其中的单集逐个展开
    **下一集**（续看后顺手点下一集是最高频动作），下一集不占
    本体名额、跟在本体之后进目标构建（总量另有总帽兜底）。
    """
    from backend.emby_server import models as em

    now = now or datetime.now()
    cutoff = now - timedelta(days=cfg["resume_days"])
    limit = cfg["resume_limit"]
    recency = {}

    def _note(item_id, ts):
        if item_id is None or ts is None:
            return
        if ts > recency.get(item_id, datetime.min):
            recency[item_id] = ts

    data_rows = (
        db.query(em.UserMediaData.item_id, em.UserMediaData.last_played_at,
                 em.UserMediaData.updated_at)
        .join(em.MediaItem, em.MediaItem.id == em.UserMediaData.item_id)
        .filter(
            em.MediaItem.merged_into_id.is_(None),
            (em.UserMediaData.last_played_at >= cutoff)
            | (em.UserMediaData.updated_at >= cutoff),
        )
        .all())
    for item_id, last_played, updated in data_rows:
        _note(item_id, last_played or updated)
    session_rows = (
        db.query(em.PlaybackSession.item_id,
                 em.PlaybackSession.last_update_at)
        .join(em.MediaItem, em.MediaItem.id == em.PlaybackSession.item_id)
        .filter(
            em.MediaItem.merged_into_id.is_(None),
            em.PlaybackSession.last_update_at >= cutoff,
        )
        .all())
    for item_id, last_update in session_rows:
        _note(item_id, last_update)

    ordered = [item_id for item_id, _ts in sorted(
        recency.items(), key=lambda kv: kv[1], reverse=True)][:limit]
    out = list(ordered)
    seen = set(out)
    for item_id in ordered:
        item = (db.query(em.MediaItem)
                .filter(em.MediaItem.id == item_id).first())
        if item is None or item.item_type != "episode":
            continue
        nxt = next_episode_id(db, item)
        if nxt is not None and nxt not in seen:
            seen.add(nxt)
            out.append(nxt)
    return out


def collect_hot_item_ids(db, cfg, now=None) -> list:
    """信号三 · 新入库热门：返回条目 id（成功起播次数多者在前）。

    口径：入库在 hot_new_days 内 × hot_window_hours 内成功起播
    （emby_play_start_events.result = 'ok'）次数 >= hot_min_plays。
    已经有人看 = 大概率还会有人看（含首看者本人复访别集/复播）。
    """
    from backend.emby_server import models as em

    now = now or datetime.now()
    window_start = now - timedelta(hours=cfg["hot_window_hours"])
    new_start = now - timedelta(days=cfg["hot_new_days"])
    plays = func.count(em.PlayStartEvent.id)
    rows = (
        db.query(em.PlayStartEvent.item_id, plays)
        .join(em.MediaItem, em.MediaItem.id == em.PlayStartEvent.item_id)
        .filter(
            em.PlayStartEvent.result == "ok",
            em.PlayStartEvent.item_id.isnot(None),
            em.PlayStartEvent.created_at >= window_start,
            em.MediaItem.date_added.isnot(None),
            em.MediaItem.date_added >= new_start,
            em.MediaItem.merged_into_id.is_(None),
        )
        .group_by(em.PlayStartEvent.item_id)
        .having(plays >= cfg["hot_min_plays"])
        .order_by(plays.desc())
        .all())
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# 衔接缝（与既有机制的唯一接触面；测试在本模块上 monkeypatch 这些
# 函数，等同仓库既有「patch 类/模块属性」的口径）
# ---------------------------------------------------------------------------

def _displayable_mappings(db, item_id):
    """条目的可展示映射（与详情页线路下发逐字同口径）。"""
    from backend.emby_server import upstream_playback as up

    return up.get_displayable_mappings(db, item_id)


def _verified_ttl() -> int:
    """已验活快路窗口（秒）：读 #1856 的同一配置口。

    快路不存在/读不到时按其默认值 300 兜底（本工人落到正式
    基线时 #1856 已合入，此兜底只防导入期异常）。
    """
    try:
        from backend.emby_server import upstream_playback as up

        fn = getattr(up, "cms_verified_play_ttl", None)
        if fn is not None:
            return int(fn())
    except Exception:  # noqa: BLE001 — 读不到按默认窗口，不许把工人打挂
        pass
    return 300


def _read_verified(mapping_id):
    """读已验活地址（#1856 同一口）：无记录/快路关一律 None。"""
    try:
        from backend.emby_server import upstream_playback as up

        fn = getattr(up, "read_verified_play_address", None)
        if fn is not None:
            return fn(mapping_id)
    except Exception:  # noqa: BLE001 — 缓存故障不外溢（与快路同口径）
        return None
    return None


def _source_unavailable(source_id) -> bool:
    """源健康记录是否已判不可用（熔断/探活状态机的既有结论）。

    健康存储读不到（Redis 故障等）一律按可用计（fail-open，
    与起播路径同口径）：保鲜多探一次的代价远小于把好源整源
    漏保。源被自动停用（enabled=False）的更早一步已被
    _displayable_mappings 的展示口径剔除，不会走到这里。
    """
    try:
        from backend.emby_server import upstream_health as uh

        store = uh.RedisHealthStore()
        record = store.get_source(str(int(source_id)))
        return not uh.is_source_available(record)
    except Exception:  # noqa: BLE001 — fail-open，见 docstring
        return False


def _load_ratio() -> float:
    """1 分钟负载 / CPU 数（取不到按 0：不许因测不准饿死工人）。"""
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        return 0.0
    return load1 / float(os.cpu_count() or 1)


def _resolve_for_warm(db, item_id, mapping_id, http) -> str:
    """保鲜的解析本体：与点播逐字同一条路（定向到具体映射）。

    成功返回下发地址的同时，#1856 的收口已把地址记入已验活
    缓存——本函数不直接碰缓存，这正是「不另起一套」的落点。
    失败抛 UpstreamPlaybackError（调用方记账，不冒泡）。
    """
    from backend.emby_server import upstream_playback as up

    return up.resolve_mapped_upstream_stream(
        db, item_id, mapping_id=mapping_id,
        http_get=http.get, http_probe=http.probe,
        http_get_text=http.get_text)


class _RequestCounter:
    """轮内出站请求计数（线程安全）：预算判据的唯一数据源。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.count = 0

    def add(self, n=1):
        with self._lock:
            self.count += n
        return self.count


def _make_http(counter):
    """计数版出站三件套：逐字委托既有默认实现，只多计一次数。

    detail 抓取（acf 默认）、Range 探活（up 默认）、画质回填
    文本抓取（up 默认）的行为与点播路径完全一致——保鲜若换了
    探法，验出的结论就不是点播要用的那个结论。
    """
    from backend.emby_server import applecms_fetcher as acf
    from backend.emby_server import upstream_playback as up

    def http_get(url, headers, timeout):
        counter.add(1)
        return acf._default_cms_http_get(url, headers, timeout)

    def http_probe(url, headers, timeout):
        counter.add(1)
        return up._default_http_range_probe(url, headers, timeout)

    def http_get_text(url, headers, timeout):
        counter.add(1)
        return up._default_http_get_text(url, headers, timeout)

    return SimpleNamespace(get=http_get, probe=http_probe,
                           get_text=http_get_text)


# ---------------------------------------------------------------------------
# 目标构建（信号 → 映射线路）
# ---------------------------------------------------------------------------

def build_targets(db, cfg, now=None):
    """把三类信号收敛成保鲜目标列表 ``[{item_id, mapping_id, source_id}]``。

    优先级序 follow > resume > hot，条目去重、映射去重；
    每条目按展示序最多取 mappings_per_item 条 **CMS 型**线路
    （非 CMS 跳过计数）；源健康已判不可用的整源跳过（计数）；
    总量到 max_targets 即停。返回 (targets, stats)。
    """
    now = now or datetime.now()
    follow_ids = collect_follow_episode_ids(db, cfg, now)
    resume_ids = collect_resume_item_ids(db, cfg, now)
    hot_ids = collect_hot_item_ids(db, cfg, now)
    stats = {
        "follow_items": len(follow_ids),
        "resume_items": len(resume_ids),
        "hot_items": len(hot_ids),
        "items": 0,
        "skipped_not_cms": 0,
        "skipped_source_unavailable": 0,
    }
    targets, seen_items, seen_mappings = [], set(), set()
    unavailable_cache = {}
    for item_id in itertools.chain(follow_ids, resume_ids, hot_ids):
        if len(targets) >= cfg["max_targets"]:
            break
        if item_id in seen_items:
            continue
        seen_items.add(item_id)
        try:
            pairs = _displayable_mappings(db, item_id)
        except Exception:  # noqa: BLE001 — 单条目映射读失败不拖垮整轮
            logger.warning("暖地址目标构建读映射失败 item=%s",
                           item_id, exc_info=True)
            continue
        added = 0
        for mapping, source in pairs:
            if len(targets) >= cfg["max_targets"]:
                break
            if added >= cfg["mappings_per_item"]:
                break
            if getattr(source, "source_type", None) != "applecms":
                stats["skipped_not_cms"] += 1
                continue
            source_id = int(source.id)
            if source_id not in unavailable_cache:
                unavailable_cache[source_id] = _source_unavailable(source_id)
            if unavailable_cache[source_id]:
                stats["skipped_source_unavailable"] += 1
                continue
            mapping_id = int(mapping.id)
            if mapping_id in seen_mappings:
                continue
            seen_mappings.add(mapping_id)
            targets.append({
                "item_id": int(item_id),
                "mapping_id": mapping_id,
                "source_id": source_id,
            })
            added += 1
    stats["items"] = len(seen_items)
    return targets, stats


# ---------------------------------------------------------------------------
# 保鲜执行（一轮）
# ---------------------------------------------------------------------------

#: 进程内保鲜时刻表：mapping_id → epoch 秒。只管「何时该复验」，
# 不是地址缓存本体（本体在 #1856 的 CacheManager 里）。
_warmed_at: dict = {}
_warmed_lock = threading.Lock()

#: 最近一轮统计（runtime_status / 管理总览的数据源）。
_last_round: dict = {}
_last_lock = threading.Lock()

_warm_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_start_lock = threading.Lock()


def _already_fresh(mapping_id, cfg) -> bool:
    """该映射的已验活记录是否还新鲜（不用再验）。

    两路证据任一成立即新鲜：①本工人刚保鲜过（进程内时刻表，
    在「窗口 - 提前量」内）；②已验活缓存里读得到地址（真实
    点播或上一轮保鲜写下的，年龄至多一个窗口）——读到时顺手
    把时刻表对齐到现在：缓存条目自身的 TTL 才是地址有效期
    的硬界，本表只决定复验频率，对齐最坏只多等一个提前量。
    """
    ttl = _verified_ttl()
    if ttl <= 0:
        return False
    margin = max(10.0, ttl - cfg["refresh_lead_sec"])
    now_ts = time.time()
    with _warmed_lock:
        last = _warmed_at.get(mapping_id)
    if last is not None and now_ts - last < margin:
        return True
    if _read_verified(mapping_id):
        with _warmed_lock:
            _warmed_at[mapping_id] = now_ts
        return True
    return False


def _recent_play_starts(db, minutes, now=None) -> int:
    """近 N 分钟全站起播数（让路判据；读失败按 0，不许饿死工人）。"""
    from backend.emby_server import models as em

    now = now or datetime.now()
    cutoff = now - timedelta(minutes=minutes)
    try:
        return int(
            db.query(func.count(em.PlayStartEvent.id))
            .filter(em.PlayStartEvent.created_at >= cutoff)
            .scalar() or 0)
    except Exception:  # noqa: BLE001 — 判据读不到不拦保鲜，见 docstring
        return 0


def _warm_one(target, cfg, counter, session_factory, round_start, http):
    """保鲜一条线路，返回结果码（warmed/fresh/failed/budget/load）。

    永不抛异常：任何失败都收敛成 "failed" 记账——保鲜是后台
    锦上添花，不许把异常漏进工人循环、更不许漏进起播路径
    （起播路径根本不经过这里）。
    """
    mapping_id = target["mapping_id"]
    if time.monotonic() - round_start > cfg["round_budget_sec"]:
        return "budget"
    if counter.count >= cfg["max_requests"]:
        return "budget"
    if _load_ratio() > cfg["load_ratio"]:
        return "load"
    try:
        if _already_fresh(mapping_id, cfg):
            return "fresh"
        jitter = cfg["jitter_sec"]
        if jitter > 0:
            time.sleep(random.uniform(0, jitter))
        db = session_factory()
        try:
            _resolve_for_warm(db, target["item_id"], mapping_id, http)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — 失败只记账，见 docstring
        logger.info("暖地址保鲜失败 item=%s mapping=%s: %s",
                    target["item_id"], mapping_id, exc)
        return "failed"
    with _warmed_lock:
        _warmed_at[mapping_id] = time.time()
    return "warmed"


def _default_session_factory():
    from backend.database import SessionLocal

    return SessionLocal


def _finish_round(stats, started_monotonic):
    """收尾：补齐时间字段、存最近轮、发布状态、清理时刻表。"""
    stats["elapsed_sec"] = round(time.monotonic() - started_monotonic, 2)
    stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
    with _last_lock:
        _last_round.clear()
        _last_round.update(stats)
    horizon = time.time() - 3600.0
    with _warmed_lock:
        for mid in [m for m, ts in _warmed_at.items() if ts < horizon]:
            _warmed_at.pop(mid, None)
    _publish_status()


def warm_once(session_factory=None) -> dict:
    """跑一轮保鲜，返回本轮统计（任何情况下都返回，不抛）。

    让路顺序：总开关 → 快路关（保鲜无处落，直接跳过）→ 系统
    负载 → 起播繁忙 → 构建目标 → 低并发执行（轮中双预算与
    负载复查在 _warm_one 里逐条守）。
    """
    cfg = warm_config()
    started = time.monotonic()
    stats = {
        "ran": False, "reason": None,
        "signals": {"follow": 0, "resume": 0, "hot": 0},
        "items": 0, "targets": 0,
        "warmed": 0, "fresh": 0, "failed": 0, "budget": 0,
        "load_skipped": 0,
        "skipped_not_cms": 0, "skipped_source_unavailable": 0,
        "requests": 0, "elapsed_sec": 0.0,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
    }
    try:
        if not cfg["enabled"]:
            stats["reason"] = "disabled"
            return stats
        if _verified_ttl() <= 0:
            # 已验活快路被关（APPLECMS_VERIFIED_PLAY_TTL_SEC=0）：
            # 保鲜写不进去，整轮免跑，不许白烧上游请求。
            stats["reason"] = "fastpath_off"
            return stats
        if _load_ratio() > cfg["load_ratio"]:
            stats["reason"] = "load"
            return stats
        factory = session_factory or _default_session_factory()
        db = factory()
        try:
            if _recent_play_starts(db, 5) >= cfg["busy_plays_5min"]:
                stats["reason"] = "busy"
                return stats
            targets, sig_stats = build_targets(db, cfg)
        finally:
            db.close()
        stats["signals"] = {
            "follow": sig_stats.get("follow_items", 0),
            "resume": sig_stats.get("resume_items", 0),
            "hot": sig_stats.get("hot_items", 0),
        }
        stats["items"] = sig_stats.get("items", 0)
        stats["targets"] = len(targets)
        stats["skipped_not_cms"] = sig_stats.get("skipped_not_cms", 0)
        stats["skipped_source_unavailable"] = \
            sig_stats.get("skipped_source_unavailable", 0)
        counter = _RequestCounter()
        http = _make_http(counter)
        outcomes = {"warmed": 0, "fresh": 0, "failed": 0,
                    "budget": 0, "load": 0}
        if targets:
            with ThreadPoolExecutor(
                    max_workers=cfg["concurrency"],
                    thread_name_prefix="upstream-warm") as pool:
                futures = [
                    pool.submit(_warm_one, target, cfg, counter,
                                factory, started, http)
                    for target in targets]
                for future in futures:
                    try:
                        outcomes[future.result()] += 1
                    except Exception:  # noqa: BLE001 — _warm_one 本应不抛
                        outcomes["failed"] += 1
        stats["warmed"] = outcomes["warmed"]
        stats["fresh"] = outcomes["fresh"]
        stats["failed"] = outcomes["failed"]
        stats["budget"] = outcomes["budget"]
        stats["load_skipped"] = outcomes["load"]
        stats["requests"] = counter.count
        stats["ran"] = True
        return stats
    finally:
        _finish_round(stats, started)


# ---------------------------------------------------------------------------
# 状态上报（照 probe_worker 形状：进程内最近轮 + 缓存发布跨进程读）
# ---------------------------------------------------------------------------

def runtime_status() -> dict:
    with _last_lock:
        last = dict(_last_round) if _last_round else None
    thread = _warm_thread
    return {
        "running": bool(thread is not None and thread.is_alive()),
        "enabled": warm_config()["enabled"],
        "last_round": last,
        "reported_at": datetime.now().isoformat(timespec="seconds"),
    }


def _publish_status() -> None:
    try:
        from backend.database import CacheManager

        CacheManager.set(
            STATUS_CACHE_KEY,
            json.dumps(runtime_status(), ensure_ascii=False, default=str),
            ttl=_STATUS_TTL_SEC)
    except Exception:  # noqa: BLE001 — 状态发布失败不影响保鲜本体
        pass


def status_snapshot() -> dict:
    """管理总览读口：本进程在跑给本地状态，否则读工人发布的状态。"""
    thread = _warm_thread
    if thread is not None and thread.is_alive():
        snap = runtime_status()
        snap["source"] = "local"
        return snap
    try:
        from backend.database import CacheManager

        raw = CacheManager.get(STATUS_CACHE_KEY)
        if raw:
            snap = json.loads(raw)
            snap["source"] = "worker"
            return snap
    except Exception:  # noqa: BLE001 — 读不到就回本地空状态
        pass
    snap = runtime_status()
    snap["source"] = "none"
    return snap


# ---------------------------------------------------------------------------
# 工人线程（照 merge_versions_worker 的 start/stop/registry 形状）
# ---------------------------------------------------------------------------

def _warm_loop():
    logger.info("上游地址保鲜 worker 启动（间隔 %.0f 秒）",
                warm_config()["interval_sec"])
    while not _stop_event.is_set():
        try:
            stats = warm_once()
            if stats.get("ran"):
                logger.info(
                    "暖地址保鲜一轮：信号 %s，目标 %d，保鲜 %d，"
                    "已新鲜 %d，失败 %d，请求 %d，耗时 %.1fs",
                    stats["signals"], stats["targets"], stats["warmed"],
                    stats["fresh"], stats["failed"], stats["requests"],
                    stats["elapsed_sec"])
            elif stats.get("reason") not in (None, "disabled"):
                logger.info("暖地址保鲜本轮让路：%s", stats["reason"])
        except Exception as exc:  # noqa: BLE001 — 循环体异常不许退出
            logger.warning("暖地址保鲜一轮异常: %s", exc, exc_info=True)
        finally:
            try:
                from backend.emby_server import worker_registry as _wr

                _wr.heartbeat(WORKER_NAME)
            except Exception:  # noqa: BLE001
                pass
        _stop_event.wait(warm_config()["interval_sec"])


def start() -> bool:
    """启动保鲜后台线程（幂等；总开关关时不启动）。"""
    global _warm_thread
    if not warm_config()["enabled"]:
        logger.info("上游地址保鲜已禁用（UPSTREAM_WARM_ENABLED=0）")
        return False
    with _start_lock:
        if _warm_thread is not None and _warm_thread.is_alive():
            return True
        _stop_event.clear()
        _warm_thread = threading.Thread(
            target=_warm_loop, name="upstream-warm", daemon=True)
        _warm_thread.start()
        from backend.emby_server import worker_registry as _wr

        _wr.register(WORKER_NAME, _warm_thread)
        return True


def stop():
    _stop_event.set()

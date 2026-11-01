# -*- coding: utf-8 -*-
"""指纹沉淀 worker（转存等待压缩期三，2026-11-01 用户拍板全套推进）。

定位（方案 transfer-wait-compression-design-20261101 §2）：跨
provider 转存的秒传接力只认整文件 SHA1，而 SHA1 今天只来自转存
副产物——全站第一次转某份内容时，总有人要在点播关键路径上付
一整遍读源（大文件数十分钟）。本 worker 把这一遍挪到闲时：后
台按信号选片、用 SA 池既有 download_range 口径流式读源算整文
件 SHA1、登记进既有可信哈希登记（MediaItem.content_hash，写入
走 transfer_variant._persist_item_sha1，与 #1538/#1559 接力同一
套登记/查询路径；本模块不新造任何登记面）。

碰源次数账（硬约束）：被选中的内容提前读 1 次整遍，之后任何
路径（探针 sign_check 稀疏读不计）不再整遍读——每内容全生命
周期仍恰好 1 次整遍读，只是提前。防白读靠三重护栏：信号只吃
有真实需求的档、日字节预算封顶、转化率熔断。

选片信号（本期只开两档，用户 2026-11-01 拍板）：
1. demand 需求档：近 7 天 pending/failed 的跨 Drive→115 单触及、
   指纹未登记的内容——已经有人在等的单，沉淀完直接喂探针道/
   省掉大文件的哈希遍；
2. watching 追剧档：近 14 天播放历史（emby_play_start_events；
   emby_series_follows 生产 0 行，不能只靠它）推导的在追剧，
   每部剧以最近播过的那一集为当前集、取同季后续 3 集（算法与
   排序复用 transfer_cleanup 的追剧候选口径）。
热门档（hot）与新入库档（new）本期只有开关位、默认关、不选片，
留待转化率数据出来后再议。

护栏：
- 闲置门（方案 §2.4）：追剧档只吃闲置产能——每轮以既有
  queue_stats 判队列状态，due 单 > 0 或在跑单 > 0 时追剧档整
  档跳过；需求档本身就是在救队列里的单，豁免闲置门（只受预
  算/熔断/总开关约束）。不新造任何队列监控口径。
- 日预算：transfer_sediment_daily_budget_gib（默认 200 GiB，
  按记账表当日 bytes_read 累计），本轮预计超限即停。
- 转化率熔断：沉淀满一个窗口期（默认 30 天）的批次里，未被
  任何转存/变体消费的比例超过阈值（默认 70%）且样本 ≥ 20，
  自动落 transfer_sediment_fused=true 停沉淀并响亮记日志；
  管理员在后台清掉该状态即可恢复（口径：方案 §2.3 的「消费比
  例低于 30% 熔断」等价式，窗口/阈值均可配）。
- 源侧异常：读源被 SA 拒（_CrossAuthError，凭据/配额面出事的
  沉淀可达信号）→ 本轮立即停、24 小时内不再沉淀（进程内暂停，
  不写配置），绝不跟转存主路抢着撞墙。

配置（纯 SystemConfig 键，不进 env.example，#1527 先例）：
transfer 家族注册＋管理端转存卡字段＋校验＋回读四齐，读取实
现就在本模块（delivery_plan 先例）。调度挂 worker.py 既有后台
线程框架（与 sample_hash_worker 同进程同形态），不起新进程；
线程常驻、每轮热读开关，保证后台改开关/清熔断不用重启。

观测：每只沉淀落一行 emby_transfer_sediment_records（档位/
字节/指纹/时间/消费回填），每轮收尾一条结构化日志
transfer_sediment round ...，供按方案 §5 口径验覆盖率与转化率。
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置键（SystemConfig，transfer 家族；读取 fail-safe 回默认）
# ---------------------------------------------------------------------------

KEY_SEDIMENT_ENABLED = "transfer_sediment_enabled"
KEY_TIER_DEMAND_ENABLED = "transfer_sediment_tier_demand_enabled"
KEY_TIER_WATCHING_ENABLED = "transfer_sediment_tier_watching_enabled"
KEY_TIER_HOT_ENABLED = "transfer_sediment_tier_hot_enabled"
KEY_TIER_NEW_ENABLED = "transfer_sediment_tier_new_enabled"
KEY_DAILY_BUDGET_GIB = "transfer_sediment_daily_budget_gib"
KEY_INTERVAL_SEC = "transfer_sediment_interval_sec"
KEY_MAX_ITEMS_PER_ROUND = "transfer_sediment_max_items_per_round"
KEY_FUSE_WINDOW_DAYS = "transfer_sediment_fuse_window_days"
KEY_FUSE_UNCONSUMED_RATIO = "transfer_sediment_fuse_unconsumed_ratio"
#: 熔断状态键（运行时落行）：true = 转化率熔断已触发、沉淀停摆，
#: 等管理员核查后在后台清回 false。不是管理员日常开关，但同走
#: 转存卡回读/保存，方便人工恢复。
KEY_FUSED = "transfer_sediment_fused"

DAILY_BUDGET_GIB_DEFAULT = 200
INTERVAL_SEC_DEFAULT = 1800
MAX_ITEMS_PER_ROUND_DEFAULT = 20
FUSE_WINDOW_DAYS_DEFAULT = 30
FUSE_UNCONSUMED_RATIO_DEFAULT = 0.70
#: 熔断判定的最小样本：样本太少时比例噪声大，不判（防误熔断）。
FUSE_MIN_SAMPLE = 20

TIER_KEYS = {
    "demand": KEY_TIER_DEMAND_ENABLED,
    "watching": KEY_TIER_WATCHING_ENABLED,
    "hot": KEY_TIER_HOT_ENABLED,
    "new": KEY_TIER_NEW_ENABLED,
}
#: 本期实现了选片的档位；hot/new 只有开关位，打开也不选片。
TIERS_IMPLEMENTED = ("demand", "watching")
TIER_DEFAULTS = {"demand": True, "watching": True,
                 "hot": False, "new": False}

DEMAND_LOOKBACK_DAYS = 7
WATCHING_LOOKBACK_DAYS = 14  # 与 transfer_cleanup 追剧口径同窗
WATCHING_NEXT_EPISODES = 3
WATCHING_MAX_SERIES = 50
GIB = 1024 ** 3
#: 读源被拒后的进程内暂停（秒）：源侧凭据/配额出事时沉淀先停。
QUOTA_PAUSE_SEC = 24 * 3600


def sediment_enabled(db=None):
    """沉淀总开关：SystemConfig 热读，默认开，读失败回默认开。"""
    return tv._system_config_bool(db, KEY_SEDIMENT_ENABLED, True)


def tier_enabled(db, tier):
    """单档开关：demand/watching 默认开，hot/new 默认关。"""
    key = TIER_KEYS[tier]
    return tv._system_config_bool(db, key, TIER_DEFAULTS[tier])


def daily_budget_bytes(db=None):
    """日读源字节预算：SystemConfig（GiB）> 默认 200，钳 [1, 10000]。"""
    configured = tv._system_config_int(db, KEY_DAILY_BUDGET_GIB)
    gib = (configured if configured is not None
           else DAILY_BUDGET_GIB_DEFAULT)
    return max(1, min(10000, gib)) * GIB


def interval_sec(db=None):
    """沉淀轮次间隔（秒）：SystemConfig > 默认 1800，钳 [300, 86400]。"""
    configured = tv._system_config_int(db, KEY_INTERVAL_SEC)
    if configured is not None:
        return max(300, min(86400, configured))
    return INTERVAL_SEC_DEFAULT


def max_items_per_round(db=None):
    """每轮最多沉淀条数：SystemConfig > 默认 20，钳 [1, 200]。"""
    configured = tv._system_config_int(db, KEY_MAX_ITEMS_PER_ROUND)
    if configured is not None:
        return max(1, min(200, configured))
    return MAX_ITEMS_PER_ROUND_DEFAULT


def fuse_window_days(db=None):
    """转化率熔断窗口（天）：SystemConfig > 默认 30，钳 [7, 90]。"""
    configured = tv._system_config_int(db, KEY_FUSE_WINDOW_DAYS)
    if configured is not None:
        return max(7, min(90, configured))
    return FUSE_WINDOW_DAYS_DEFAULT


def fuse_unconsumed_ratio(db=None):
    """熔断未消费比例阈值：SystemConfig > 默认 0.70，钳 [0.1, 1.0]。"""
    if db is None:
        return FUSE_UNCONSUMED_RATIO_DEFAULT
    try:
        from backend.integrations import store
        raw = (store.get_value(db, KEY_FUSE_UNCONSUMED_RATIO, "")
               or "").strip()
        value = float(raw)
    except Exception:
        return FUSE_UNCONSUMED_RATIO_DEFAULT
    return max(0.1, min(1.0, value))


def sediment_fused(db=None):
    """熔断状态：SystemConfig 热读，默认未熔断。"""
    return tv._system_config_bool(db, KEY_FUSED, False)


def sediment_config_payload(db):
    """管理端转存卡回包（沉淀段）：各键当前生效值（默认已折算）。"""
    return {
        KEY_SEDIMENT_ENABLED: sediment_enabled(db),
        KEY_TIER_DEMAND_ENABLED: tier_enabled(db, "demand"),
        KEY_TIER_WATCHING_ENABLED: tier_enabled(db, "watching"),
        KEY_TIER_HOT_ENABLED: tier_enabled(db, "hot"),
        KEY_TIER_NEW_ENABLED: tier_enabled(db, "new"),
        KEY_DAILY_BUDGET_GIB: daily_budget_bytes(db) // GIB,
        KEY_INTERVAL_SEC: interval_sec(db),
        KEY_MAX_ITEMS_PER_ROUND: max_items_per_round(db),
        KEY_FUSE_WINDOW_DAYS: fuse_window_days(db),
        KEY_FUSE_UNCONSUMED_RATIO: fuse_unconsumed_ratio(db),
        KEY_FUSED: sediment_fused(db),
    }


def write_sediment_config(db, *, transfer_sediment_enabled=None,
                          transfer_sediment_tier_demand_enabled=None,
                          transfer_sediment_tier_watching_enabled=None,
                          transfer_sediment_tier_hot_enabled=None,
                          transfer_sediment_tier_new_enabled=None,
                          transfer_sediment_daily_budget_gib=None,
                          transfer_sediment_interval_sec=None,
                          transfer_sediment_max_items_per_round=None,
                          transfer_sediment_fuse_window_days=None,
                          transfer_sediment_fuse_unconsumed_ratio=None,
                          transfer_sediment_fused=None):
    """管理端沉淀配置写入：走 config_service 的 transfer 家族校验。

    None = 该键不改。非法值抛 ConfigValueError（ValueError 子类，
    端点转 400），整批不落库。返回 sediment_config_payload。
    """
    values = {}
    for key, val in (
            (KEY_SEDIMENT_ENABLED, transfer_sediment_enabled),
            (KEY_TIER_DEMAND_ENABLED,
             transfer_sediment_tier_demand_enabled),
            (KEY_TIER_WATCHING_ENABLED,
             transfer_sediment_tier_watching_enabled),
            (KEY_TIER_HOT_ENABLED, transfer_sediment_tier_hot_enabled),
            (KEY_TIER_NEW_ENABLED, transfer_sediment_tier_new_enabled),
            (KEY_FUSED, transfer_sediment_fused)):
        if val is not None:
            values[key] = "true" if val else "false"
    for key, val in (
            (KEY_DAILY_BUDGET_GIB, transfer_sediment_daily_budget_gib),
            (KEY_INTERVAL_SEC, transfer_sediment_interval_sec),
            (KEY_MAX_ITEMS_PER_ROUND,
             transfer_sediment_max_items_per_round),
            (KEY_FUSE_WINDOW_DAYS, transfer_sediment_fuse_window_days)):
        if val is not None:
            values[key] = str(val)
    if transfer_sediment_fuse_unconsumed_ratio is not None:
        values[KEY_FUSE_UNCONSUMED_RATIO] = str(
            transfer_sediment_fuse_unconsumed_ratio)
    if values:
        from backend import config_service
        config_service.save_config(db, values, schema="transfer")
    return sediment_config_payload(db)


# ---------------------------------------------------------------------------
# 候选资格与选片
# ---------------------------------------------------------------------------

def _sedimentable(item):
    """条目是否值得且能够沉淀（纯读判定，不查外表）。

    条件：movie/episode、未软删未合并、有 Drive 源 fid 与大小、
    file_path 是 mount:// 源路径、且尚无可信 sha1（已有登记的
    再读就是白读——「同内容只整遍读一遍」红线的第一道闸）。
    """
    if item is None:
        return False
    if (getattr(item, "item_type", "") or "") not in ("movie", "episode"):
        return False
    if getattr(item, "deleted_at", None) is not None:
        return False
    if getattr(item, "merged_into_id", None) is not None:
        return False
    fid = (getattr(item, "drive_file_id", "") or "").strip()
    if not fid:
        return False
    size = tv._to_int(getattr(item, "size", None))
    if not size or size <= 0:
        return False
    path = (getattr(item, "file_path", "") or "").strip()
    if not path.startswith("mount://"):
        return False
    if tv._trusted_item_sha1(item, size):
        return False
    return True


def demand_candidates(db, now=None, limit=None):
    """需求档候选：近 DEMAND_LOOKBACK_DAYS 天 pending/failed 的跨
    Drive→115 单触及的未登记条目，按最近需求在前排序、去重。"""
    now = now or datetime.now()
    cutoff = now - timedelta(days=DEMAND_LOOKBACK_DAYS)
    jobs = (db.query(em.TransferJob)
            .filter(em.TransferJob.provider == "drive",
                    em.TransferJob.status.in_(("pending", "failed")),
                    em.TransferJob.created_at >= cutoff)
            .order_by(em.TransferJob.created_at.desc())
            .all())
    item_ids = []
    seen = set()
    for job in jobs:
        if not tv._is_cross_job(job):
            continue
        if not (getattr(job, "dest_drive_id", "") or "").startswith(
                "pan115:"):
            continue
        if job.item_id in seen:
            continue
        seen.add(job.item_id)
        item_ids.append(job.item_id)
    if not item_ids:
        return []
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.id.in_(item_ids)).all())
    by_id = {row.id: row for row in rows}
    out = [by_id[i] for i in item_ids
           if i in by_id and _sedimentable(by_id[i])]
    return out[:limit] if limit else out


def watching_candidates(db, now=None, limit=None):
    """追剧档候选：近 WATCHING_LOOKBACK_DAYS 天播放历史里每部剧
    最近播过的那一集为当前集，取同季后续 WATCHING_NEXT_EPISODES
    集中的未登记条目（排序复用 transfer_cleanup._episode_order_key）。

    信号源只用播放历史：emby_series_follows 生产 0 行（设计稿
    §0 实测），靠它选片等于永不触发。
    """
    from backend.emby_server.transfer_cleanup import _episode_order_key

    now = now or datetime.now()
    cutoff = now - timedelta(days=WATCHING_LOOKBACK_DAYS)
    events = (db.query(em.PlayStartEvent)
              .filter(em.PlayStartEvent.created_at >= cutoff,
                      em.PlayStartEvent.item_id.isnot(None))
              .order_by(em.PlayStartEvent.created_at.desc())
              .all())
    if not events:
        return []
    played_ids = []
    seen_ids = set()
    for ev in events:
        if ev.item_id not in seen_ids:
            seen_ids.add(ev.item_id)
            played_ids.append(ev.item_id)
    played = (db.query(em.MediaItem)
              .filter(em.MediaItem.id.in_(played_ids)).all())
    played_by_id = {row.id: row for row in played}
    # 每部剧的当前集 = 该剧最近一次被播的那一集（事件按时间倒序，
    # 先撞到的即当前集）；剧数封顶防历史太杂时一轮选爆。
    current_by_series = {}
    for ev in events:
        item = played_by_id.get(ev.item_id)
        if item is None:
            continue
        if (getattr(item, "item_type", "") or "") != "episode":
            continue
        series_id = getattr(item, "series_id", None)
        if series_id is None or series_id in current_by_series:
            continue
        current_by_series[series_id] = item
        if len(current_by_series) >= WATCHING_MAX_SERIES:
            break
    out = []
    for series_id, current in current_by_series.items():
        episodes = (db.query(em.MediaItem)
                    .filter(em.MediaItem.series_id == series_id,
                            em.MediaItem.item_type == "episode",
                            em.MediaItem.deleted_at.is_(None),
                            em.MediaItem.merged_into_id.is_(None))
                    .all())
        ordered = sorted(episodes, key=_episode_order_key)
        keys = [_episode_order_key(ep) for ep in ordered]
        try:
            idx = keys.index(_episode_order_key(current))
        except ValueError:
            continue
        following = ordered[idx + 1: idx + 1 + WATCHING_NEXT_EPISODES]
        for ep in following:
            if _sedimentable(ep):
                out.append(ep)
    return out[:limit] if limit else out


# ---------------------------------------------------------------------------
# 记账：日预算用量 / 消费回填 / 转化率熔断
# ---------------------------------------------------------------------------

def today_bytes(db, now=None):
    """当日已沉淀读源字节（记账表口径，跨进程/重启不丢）。"""
    now = now or datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = (db.query(em.TransferSedimentRecord)
             .filter(em.TransferSedimentRecord.created_at >= start)
             .all())
    return sum(int(r.bytes_read or 0) for r in total)


def refresh_consumption(db, now=None):
    """回填消费标记：沉淀之后该条目出现过新转存单、或有转存单在
    沉淀后办成（done 的 updated_at 落在沉淀后）、或出现 ready 变
    体，即视为这份指纹被真实消费。返回本轮新标记条数。"""
    now = now or datetime.now()
    marked = 0
    records = (db.query(em.TransferSedimentRecord)
               .filter(em.TransferSedimentRecord.consumed_at.is_(None))
               .all())
    for rec in records:
        stamps = []
        job = (db.query(em.TransferJob)
               .filter(em.TransferJob.item_id == rec.item_id,
                       (em.TransferJob.created_at >= rec.created_at)
                       | ((em.TransferJob.status == "done")
                          & (em.TransferJob.updated_at
                             >= rec.created_at)))
               .order_by(em.TransferJob.created_at)
               .first())
        if job is not None:
            stamps.append(job.created_at or now)
        variant = (db.query(em.TransferVariant)
                   .filter(em.TransferVariant.item_id == rec.item_id,
                           em.TransferVariant.status == "ready",
                           em.TransferVariant.created_at >= rec.created_at)
                   .order_by(em.TransferVariant.created_at)
                   .first())
        if variant is not None:
            stamps.append(variant.created_at or now)
        if stamps:
            rec.consumed_at = min(stamps)
            marked += 1
    if marked:
        db.commit()
    return marked


def evaluate_fuse(db, now=None):
    """转化率熔断判定：已满一个窗口期的沉淀批次（沉淀时间落在
    [now-2*窗口, now-窗口]）里未消费比例超阈值且样本足够 →
    落 transfer_sediment_fused=true 停沉淀（响亮记日志），等
    管理员核查后手动清状态。已熔断直接返回 True。"""
    now = now or datetime.now()
    if sediment_fused(db):
        return True
    window = fuse_window_days(db)
    cohort = (db.query(em.TransferSedimentRecord)
              .filter(
                  em.TransferSedimentRecord.created_at
                  <= now - timedelta(days=window),
                  em.TransferSedimentRecord.created_at
                  >= now - timedelta(days=2 * window))
              .all())
    if len(cohort) < FUSE_MIN_SAMPLE:
        return False
    unconsumed = sum(1 for r in cohort if r.consumed_at is None)
    ratio = unconsumed / len(cohort)
    if ratio <= fuse_unconsumed_ratio(db):
        return False
    from backend import config_service
    config_service.save_config(db, {KEY_FUSED: "true"}, schema="transfer")
    logger.error(
        "transfer_sediment outcome=fused 转化率熔断：窗口 %d 天内沉淀 "
        "%d 份、未消费 %d 份（%.0f%% > 阈值 %.0f%%），沉淀已自动停摆；"
        "核查选片信号后在管理端清「沉淀熔断状态」恢复",
        window, len(cohort), unconsumed, ratio * 100,
        fuse_unconsumed_ratio(db) * 100)
    return True


# ---------------------------------------------------------------------------
# 单条沉淀与轮次主流程
# ---------------------------------------------------------------------------

_quota_pause_until = [0.0]  # 进程内源侧暂停截止（time.monotonic 基准）


def _quota_paused():
    return time.monotonic() < _quota_pause_until[0]


def sediment_item(db, item, drive_api, tier, now=None):
    """给一只条目算整文件 SHA1 并登记（+记账行）。

    开读前再查一次登记：转存主路刚沉淀过的直接跳过、零读源
    （红线：同一内容全生命周期只整遍读一遍，登记就是共用的
    「已算」事实）。读源走 SA 池 download_range 分段流式喂
    _HashingReader（与跨执行器哈希遍同款组合），字节只过内存。
    返回 {"outcome": registered|already_registered|failed, ...}。
    """
    now = now or datetime.now()
    size = tv._to_int(getattr(item, "size", None)) or 0
    if tv._trusted_item_sha1(item, size):
        return {"outcome": "already_registered", "bytes_read": 0}
    source_drive = tv._source_drive_id(db, item, "drive")
    fid = (getattr(item, "drive_file_id", "") or "").strip()
    if not source_drive:
        return {"outcome": "failed", "error_kind": "no_drive",
                "bytes_read": 0}
    try:
        info = drive_api.get_file(source_drive, fid)
        real_size = tv._to_int((info or {}).get("size")) or size
    except (tv._NotFound, tv._Forbidden) as exc:
        return {"outcome": "failed", "error_kind": "dead",
                "error_detail": str(exc)[:200], "bytes_read": 0}
    except Exception as exc:  # noqa: BLE001 — 元数据读失败按瞬态记账
        return {"outcome": "failed", "error_kind": "retriable",
                "error_detail": str(exc)[:200], "bytes_read": 0}
    if real_size <= 0:
        return {"outcome": "failed", "error_kind": "unsupported",
                "bytes_read": 0}

    def _fetch(start, end):
        return drive_api.download_range(source_drive, fid, start, end)

    reader = tv._HashingReader(tv._SegmentReader(real_size, _fetch),
                               real_size)
    bytes_read = 0
    try:
        while True:
            chunk = reader.read(tv._CROSS_SEGMENT)
            if not chunk:
                break
            bytes_read += len(chunk)
    except tv._CrossAuthError as exc:
        # SA 源凭据/配额面出事：本条判失败并给整轮发暂停信号，
        # 不跟转存主路抢着撞墙（见模块头注护栏）。
        return {"outcome": "failed", "error_kind": "auth_error",
                "error_detail": str(exc)[:200],
                "bytes_read": bytes_read, "quota_signal": True}
    except Exception as exc:  # noqa: BLE001 — 读源瞬态失败，下轮再试
        return {"outcome": "failed", "error_kind": "retriable",
                "error_detail": str(exc)[:200], "bytes_read": bytes_read}
    sha1 = reader.sha1_hexdigest() or ""
    if not sha1:
        return {"outcome": "failed", "error_kind": "hash_incomplete",
                "bytes_read": bytes_read}
    tv._persist_item_sha1(db, item, sha1, origin="sediment")
    if not tv._trusted_item_sha1(item, real_size):
        # 登记没落住（_persist 内部失败已自行记日志）：账不能虚报。
        return {"outcome": "failed", "error_kind": "persist_failed",
                "bytes_read": bytes_read}
    db.add(em.TransferSedimentRecord(
        item_id=item.id, drive_file_id=fid, sha1=sha1, tier=tier,
        bytes_read=bytes_read, created_at=now))
    db.commit()
    return {"outcome": "registered", "bytes_read": bytes_read,
            "sha1": sha1}


def _queue_busy(db):
    """闲置门判据：既有 queue_stats 口径（due 单 >0 或在跑 >0）。"""
    stats = tv.queue_stats(db)
    return (stats.get("due_count", 0) > 0
            or stats.get("running", 0) > 0)


def run_once(db, drive_api=None, now=None):
    """跑一轮沉淀（供后台线程与测试共用；不启动线程）。

    顺序：总开关 → 消费回填＋熔断判定 → 源侧暂停 → 选片（需求
    档豁免闲置门、追剧档过闲置门）→ 按日预算逐只沉淀。返回账目
    dict：skipped_reason（整轮跳过原因，None=跑了）、selected /
    registered / already_registered / failed / bytes_read /
    budget_exhausted / by_tier（各档登记数）/ skip_reasons（分档
    跳过原因）。
    """
    now = now or datetime.now()
    stats = {"skipped_reason": None, "selected": 0, "registered": 0,
             "already_registered": 0, "failed": 0, "bytes_read": 0,
             "budget_exhausted": False, "by_tier": {},
             "skip_reasons": {}}
    if not sediment_enabled(db):
        stats["skipped_reason"] = "disabled"
        return stats
    try:
        refresh_consumption(db, now)
    except Exception:  # noqa: BLE001 — 回填失败不挡沉淀主流程
        logger.warning("transfer_sediment 消费回填失败", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
    if sediment_fused(db) or evaluate_fuse(db, now):
        stats["skipped_reason"] = "fused"
        return stats
    if _quota_paused():
        stats["skipped_reason"] = "quota_pause"
        return stats
    if drive_api is None:
        drive_api = tv.DriveApiClient()

    candidates = []  # [(tier, item)]，需求档在前、同条目只入一次
    seen_ids = set()
    if tier_enabled(db, "demand"):
        for item in demand_candidates(db, now):
            if item.id not in seen_ids:
                seen_ids.add(item.id)
                candidates.append(("demand", item))
    watching_gated = False
    if tier_enabled(db, "watching"):
        if _queue_busy(db):
            watching_gated = True
            stats["skip_reasons"]["watching"] = "queue_busy"
        else:
            for item in watching_candidates(db, now):
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    candidates.append(("watching", item))
    # hot/new 档本期未实现选片：开关打开也恒无候选（见模块头注）。
    candidates = candidates[:max_items_per_round(db)]
    stats["selected"] = len(candidates)
    if not candidates:
        if watching_gated:
            stats["skipped_reason"] = "queue_busy"
        return stats

    budget = daily_budget_bytes(db)
    spent = today_bytes(db, now)
    for tier, item in candidates:
        size = tv._to_int(getattr(item, "size", None)) or 0
        if spent + stats["bytes_read"] + size > budget:
            stats["budget_exhausted"] = True
            break
        result = sediment_item(db, item, drive_api, tier, now=now)
        stats["bytes_read"] += int(result.get("bytes_read") or 0)
        outcome = result.get("outcome")
        if outcome == "registered":
            stats["registered"] += 1
            stats["by_tier"][tier] = stats["by_tier"].get(tier, 0) + 1
        elif outcome == "already_registered":
            stats["already_registered"] += 1
        else:
            stats["failed"] += 1
            logger.info(
                "transfer_sediment outcome=failed item=%s tier=%s "
                "kind=%s detail=%s", item.id, tier,
                result.get("error_kind"), result.get("error_detail"))
            if result.get("quota_signal"):
                _quota_pause_until[0] = (time.monotonic()
                                         + QUOTA_PAUSE_SEC)
                stats["skipped_reason"] = "quota_pause"
                logger.error(
                    "transfer_sediment outcome=quota_pause 读源被 SA 拒"
                    "（item=%s），沉淀暂停 24 小时，转存主路不受影响",
                    item.id)
                break
    logger.info(
        "transfer_sediment round selected=%s registered=%s "
        "already=%s failed=%s bytes=%s budget_exhausted=%s by_tier=%s "
        "skip_reasons=%s", stats["selected"], stats["registered"],
        stats["already_registered"], stats["failed"],
        stats["bytes_read"], stats["budget_exhausted"],
        stats["by_tier"], stats["skip_reasons"])
    return stats


# ---------------------------------------------------------------------------
# 后台线程（worker.py 既有框架内启动，形态照 sample_hash_worker；
# 与它不同的是线程常驻不按开关决定起停——开关/熔断都是热读，
# 后台改配置须不用重启即生效，轮次内 run_once 自行短路。）
# ---------------------------------------------------------------------------

_stop_event = threading.Event()
_start_lock = threading.Lock()
_sediment_thread = None


def _sediment_loop():
    logger.info("指纹沉淀 worker 启动")
    while not _stop_event.is_set():
        wait_sec = INTERVAL_SEC_DEFAULT
        try:
            from backend.database import SessionLocal

            db = SessionLocal()
            try:
                stats = run_once(db)
                wait_sec = interval_sec(db)
            finally:
                db.close()
            if stats.get("registered"):
                logger.info("指纹沉淀轮次完成：%s", stats)
        except Exception as e:  # noqa: BLE001 — 后台轮次失败只记日志
            logger.warning("指纹沉淀轮次失败: %s", e)
        finally:
            try:
                from backend.emby_server import worker_registry as _wr

                _wr.heartbeat("transfer_sediment")
            except Exception:  # noqa: BLE001 — 注册表缺失可忽略
                pass
        _stop_event.wait(wait_sec)


def start() -> bool:
    """启动沉淀后台线程（常驻；开关在轮次内热读判定）。"""
    global _sediment_thread
    with _start_lock:
        if _sediment_thread and _sediment_thread.is_alive():
            return True
        _stop_event.clear()
        _sediment_thread = threading.Thread(
            target=_sediment_loop, name="transfer-sediment", daemon=True)
        _sediment_thread.start()
        try:
            from backend.emby_server import worker_registry as _wr

            _wr.register("transfer_sediment", _sediment_thread)
        except Exception:  # noqa: BLE001 — 注册失败不影响线程本身
            pass
        return True


def stop():
    """停沉淀后台线程（测试/关机用）。"""
    _stop_event.set()

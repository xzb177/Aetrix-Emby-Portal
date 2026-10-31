"""映射消费抽样验证（2026-10-31 上游聚合链优化 O-09）。

回答的问题只有一个：聚合落库的映射到底对不对、播不播得了——
终结设计稿 P-08 的「建了没验」。分层（从便宜到贵）：

- 第 1 层（数据层抽样，只读 SQL）：按 evidence 分层（tmdb / imdb /
  title+year / series+episode）各抽 N 条（默认各 30，SystemConfig
  可配），查本地条目活行、源存在且 enabled、置信度分布，只产
  报表不写映射；
- 第 2 层（上游存在性抽样，只读 HTTP）：对可验样本逐个带源凭据
  GET 对方 ``/Items/{id}``（凭据解析与传输复用 upstream_aggregate_
  run 的唯一实现，本模块不另起一套），核 200 且对方返回的
  Tmdb/Imdb 与配对证据一致；title+year 样本核归一化名称 + 年份；
  series+episode 样本（O-08 系列级配对）有自身身份先核身份、
  否则核季集号（IndexNumber/ParentIndexNumber 对本地
  episode_number/season_number）——配错集后果最重，此层重点覆盖。
  不一致率（mismatch+missing / 已判定数）超阈值（默认 5%）响亮
  报红（ERROR 日志 + 报表 alert）；凭据失效整源同样报红；
- 第 3 层（播放层抽样）：只做一次性手动入口
  :func:`verify_playback_samples`（管理端触发），对第 2 层通过的
  样本调 resolve_upstream_stream 核 302 地址，不常驻——避免无谓
  打对方额度；每次调用都会产生真实的 upstream 起播探活事件；
- 第 4 层（真机）：依赖客户端上游入口（设计稿未知项 U-02，未
  定位），不在本模块范围，如实登记为缺口，不自创入口。

常驻化：第 1、2 层挂 janitor（maintenance.janitor_tick 重活区），
默认开、默认每周一轮。周期门与上次运行时刻走 SystemConfig 状态
键（照 image_store 水位键先例）；运行参数走 upstream_aggregate
SystemConfig 家族（config_schema 登记），不新增 env 键。

只读纪律：全程只读映射——验错了只报数，绝不删 / 改任何映射行
（删除走 O-04 / O-06 的宽限口径，不在本模块）。

范围注记：苹果 CMS 型源的映射同样进第 1 层报表，但第 2 层的
/Items/{id} 是 Emby/Jellyfin 口径，CMS 样本在第 2 层计 skipped
（其存在性由 CMS 同步链自有口径负责，不在本层冒充已验）。
"""

from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import upstream_aggregate_run as uar
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_playback as upb

logger = logging.getLogger(__name__)

#: 分层抽样的 evidence 全集（与聚合配对产出的证据短码一致；
#: series+episode 是 O-08 系列级单集配对的证据，按其风险项要求
#: 纳入本巡检的默认分层重点覆盖）。
EVIDENCE_STRATA = ("tmdb", "imdb", "title+year", "series+episode")

#: 第 2 层可核验的源类型（Emby/Jellyfin 的 /Items 口径）。
CHECKABLE_SOURCE_TYPES = ("emby", "jellyfin")

# ---------------------------------------------------------------------------
# 配置（upstream_aggregate SystemConfig 家族，纯 SystemConfig 键）
# ---------------------------------------------------------------------------

#: 巡检总开关：默认开（站内开关默认开的既定口径）。
KEY_PATROL_ENABLED = "upstream_aggregate_mapping_patrol_enabled"
#: 巡检间隔（天）：默认 7（每周），钳 [1, 30]。
KEY_PATROL_INTERVAL_DAYS = "upstream_aggregate_mapping_patrol_interval_days"
#: 每 evidence 层抽样条数：默认 30，钳 [1, 200]。
KEY_PATROL_SAMPLE_SIZE = "upstream_aggregate_mapping_patrol_sample_size"
#: 不一致率报红阈值（百分比）：默认 5，钳 [0, 100]，严格大于才报红。
KEY_PATROL_ALERT_PCT = "upstream_aggregate_mapping_patrol_alert_pct"

#: 上次运行时刻（内部状态键，不进配置注册表；照水位键先例）。
STATE_KEY_LAST_RUN_AT = "upstream_aggregate_mapping_patrol_last_run_at"

PATROL_INTERVAL_DAYS_DEFAULT = 7
PATROL_SAMPLE_SIZE_DEFAULT = 30
PATROL_ALERT_PCT_DEFAULT = 5

_CREDENTIAL_FAILED = object()


def _config_int(db, key):
    """SystemConfig 整数热读：与 upstream_aggregate_run 同口径。

    无行 / 非法 / 异常一律 None（回退代码默认）；0 是合法值，
    只有负数按未设置处理。
    """
    if db is None:
        return None
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _config_bool(db, key, default: bool) -> bool:
    if db is None:
        return default
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def patrol_enabled(db) -> bool:
    """巡检总开关：SystemConfig > 默认开。"""
    return _config_bool(db, KEY_PATROL_ENABLED, True)


def patrol_interval_days(db) -> int:
    configured = _config_int(db, KEY_PATROL_INTERVAL_DAYS)
    if configured is not None:
        return max(1, min(30, configured))
    return PATROL_INTERVAL_DAYS_DEFAULT


def patrol_sample_size(db) -> int:
    configured = _config_int(db, KEY_PATROL_SAMPLE_SIZE)
    if configured is not None:
        return max(1, min(200, configured))
    return PATROL_SAMPLE_SIZE_DEFAULT


def patrol_alert_pct(db) -> int:
    configured = _config_int(db, KEY_PATROL_ALERT_PCT)
    if configured is not None:
        return max(0, min(100, configured))
    return PATROL_ALERT_PCT_DEFAULT


# ---------------------------------------------------------------------------
# 抽样与预取（只读）
# ---------------------------------------------------------------------------

def sample_mappings(db: Session, per_evidence: int, *,
                    source_id: Optional[int] = None) -> list:
    """按 evidence 分层随机抽样（每层至多 per_evidence 条）。"""
    rows: list = []
    for evidence in EVIDENCE_STRATA:
        query = (db.query(em.UpstreamItemMapping)
                 .filter(em.UpstreamItemMapping.evidence == evidence))
        if source_id is not None:
            query = query.filter(
                em.UpstreamItemMapping.source_id == source_id)
        rows.extend(query.order_by(func.random())
                    .limit(per_evidence).all())
    return rows


def _prefetch(db: Session, rows: list):
    item_ids = {row.local_item_id for row in rows}
    source_ids = {row.source_id for row in rows}
    items = {}
    if item_ids:
        items = {item.id: item for item in db.query(em.MediaItem)
                 .filter(em.MediaItem.id.in_(item_ids)).all()}
    sources = {}
    if source_ids:
        sources = {src.id: src for src in db.query(em.UpstreamSource)
                   .filter(em.UpstreamSource.id.in_(source_ids)).all()}
    return items, sources


def _item_live(item) -> bool:
    """本地条目活行口径：与 upstream_item_mappings._local_item_live
    逐字对齐（行在、未软删、未合并、未隐藏）。"""
    return (item is not None
            and item.deleted_at is None
            and item.merged_into_id is None
            and not item.is_hidden)


def _confidence_buckets(rows: list) -> dict:
    buckets = {"high": 0, "mid": 0, "low": 0}
    for row in rows:
        value = float(row.confidence or 0.0)
        if value >= 0.9:
            buckets["high"] += 1
        elif value >= 0.7:
            buckets["mid"] += 1
        else:
            buckets["low"] += 1
    return buckets


# ---------------------------------------------------------------------------
# 第 2 层：上游存在性 + 身份一致性（只读 HTTP）
# ---------------------------------------------------------------------------

def _identity_verdict(row, item, body: dict) -> str:
    """按配对证据核对方返回的身份：ok / mismatch。

    本地身份缺失（声称 tmdb 证据却无 tmdb_id 等）本身就是不一致，
    按 mismatch 计——映射声称的证据与本地行对不上，不算验过。
    """
    providers = body.get("ProviderIds") or {}
    if row.evidence == "tmdb":
        local_id = str(getattr(item, "tmdb_id", None) or "").strip()
        remote_id = str(providers.get("Tmdb") or "").strip()
        return "ok" if local_id and local_id == remote_id else "mismatch"
    if row.evidence == "imdb":
        local_id = str(getattr(item, "imdb_id", None) or "").strip()
        remote_id = str(providers.get("Imdb") or "").strip()
        return "ok" if local_id and local_id == remote_id else "mismatch"
    if row.evidence == "series+episode":
        return _episode_verdict(item, body, providers)
    # title+year：归一化名称（与聚合 aggregate_key 同一归一实现）
    # + 年份双对上才算数；本地年份按聚合口径（scan 快照优先）。
    local_title, local_year = uar._local_title_year(item)
    remote_title = agg._normalize_title(body.get("Name") or "")
    if (not local_title or local_year is None or not remote_title
            or agg._normalize_title(local_title) != remote_title):
        return "mismatch"
    try:
        remote_year = int(body.get("ProductionYear") or 0)
    except (TypeError, ValueError):
        return "mismatch"
    return "ok" if remote_year == local_year else "mismatch"


def _episode_verdict(item, body: dict, providers: dict) -> str:
    """series+episode（O-08）样本核验：本地单集有自身身份时先核
    身份（tmdb 优先、其次 imdb，与配对证据优先级同序）；无自身
    身份时核季集号——对方 IndexNumber / ParentIndexNumber 与本地
    episode_number / season_number 双对上才算数，任一缺失或对不上
    按 mismatch 计（核不了不能算验过）。"""
    local_tmdb = str(getattr(item, "tmdb_id", None) or "").strip()
    remote_tmdb = str(providers.get("Tmdb") or "").strip()
    if local_tmdb and remote_tmdb:
        return "ok" if local_tmdb == remote_tmdb else "mismatch"
    local_imdb = str(getattr(item, "imdb_id", None) or "").strip()
    remote_imdb = str(providers.get("Imdb") or "").strip()
    if local_imdb and remote_imdb:
        return "ok" if local_imdb == remote_imdb else "mismatch"
    try:
        remote_ep = int(body.get("IndexNumber"))
        remote_season = int(body.get("ParentIndexNumber"))
        local_ep = int(getattr(item, "episode_number"))
        local_season = int(getattr(item, "season_number"))
    except (TypeError, ValueError):
        return "mismatch"
    return ("ok" if remote_ep == local_ep
            and remote_season == local_season else "mismatch")


def check_existence(db: Session, rows: list, items: dict, sources: dict,
                    *, http_get=None, http_post=None):
    """第 2 层主循环：逐样本判定，返回 (计数 dict, {映射 id: 判定})。

    判定全集：ok / mismatch / missing（对方 404）/ unreachable
    （断连、超时、5xx）/ credential_failed（凭据失效，同源只解析
    一次、失败后该源余下样本直接同判）/ skipped（本地死行、源
    失效、或非 Emby/Jellyfin 型源——不在本层口径内）。
    """
    get = uar._default_http_get if http_get is None else http_get
    counts = {"ok": 0, "mismatch": 0, "missing": 0, "unreachable": 0,
              "credential_failed": 0, "skipped": 0}
    verdicts: dict = {}
    api_keys: dict = {}

    def _tally(row, verdict):
        counts[verdict] += 1
        verdicts[row.id] = verdict

    for row in rows:
        item = items.get(row.local_item_id)
        source = sources.get(row.source_id)
        if (not _item_live(item) or source is None or not source.enabled
                or source.source_type not in CHECKABLE_SOURCE_TYPES):
            _tally(row, "skipped")
            continue
        if source.id not in api_keys:
            try:
                api_keys[source.id] = uar._resolve_api_key(
                    source, http_post=http_post)
            except uar.UpstreamAggregateRunError:
                api_keys[source.id] = _CREDENTIAL_FAILED
        api_key = api_keys[source.id]
        if api_key is _CREDENTIAL_FAILED:
            _tally(row, "credential_failed")
            continue
        base = (source.base_url or "").rstrip("/")
        params = {"Fields": "ProviderIds"}
        if api_key:
            params["api_key"] = api_key
        url = (f"{base}/Items/"
               f"{urllib.parse.quote(str(row.upstream_item_id), safe='')}"
               f"?{urllib.parse.urlencode(params)}")
        status, body = get(url, uar._source_headers(source),
                           uar.HTTP_TIMEOUT_SEC)
        if status == 200 and isinstance(body, dict):
            _tally(row, _identity_verdict(row, item, body))
        elif status == 404:
            _tally(row, "missing")
        elif status in (401, 403):
            # 中途凭据被拒：整源按凭据失效收口，余下样本不再逐个打
            api_keys[source.id] = _CREDENTIAL_FAILED
            _tally(row, "credential_failed")
        else:
            _tally(row, "unreachable")
    return counts, verdicts


# ---------------------------------------------------------------------------
# 第 1 + 2 层：常驻巡检主体
# ---------------------------------------------------------------------------

def run_mapping_patrol(db: Session, *, http_get=None, http_post=None,
                       now: Optional[datetime] = None) -> dict:
    """跑一轮第 1、2 层抽样验证并产出报表（只读映射）。

    计数日志恒打 INFO 一行；报红（不一致率超阈值或凭据失效）另打
    ERROR 一行，运维不用翻报表就知道出事了。
    """
    per_evidence = patrol_sample_size(db)
    rows = sample_mappings(db, per_evidence)
    items, sources = _prefetch(db, rows)

    totals = {
        evidence: count for evidence, count in (
            db.query(em.UpstreamItemMapping.evidence, func.count())
            .group_by(em.UpstreamItemMapping.evidence).all())
    }
    by_evidence: dict = {}
    for evidence in EVIDENCE_STRATA:
        stratum = [row for row in rows if row.evidence == evidence]
        by_evidence[evidence] = {
            "sampled": len(stratum),
            "local_dead": sum(
                1 for row in stratum
                if not _item_live(items.get(row.local_item_id))),
            "source_dead": sum(
                1 for row in stratum
                if sources.get(row.source_id) is None
                or not sources[row.source_id].enabled),
            "confidence": _confidence_buckets(stratum),
        }

    existence, _verdicts = check_existence(
        db, rows, items, sources,
        http_get=http_get, http_post=http_post)
    decided = existence["ok"] + existence["mismatch"] + existence["missing"]
    mismatch = existence["mismatch"] + existence["missing"]
    mismatch_rate = (mismatch / decided) if decided else 0.0
    alert = bool(
        (decided and mismatch_rate * 100 > patrol_alert_pct(db))
        or existence["credential_failed"] > 0)
    report = {
        "sampled": len(rows),
        "totals": {evidence: int(totals.get(evidence, 0))
                   for evidence in EVIDENCE_STRATA},
        "by_evidence": by_evidence,
        "existence": existence,
        "mismatch_rate": mismatch_rate,
        "alert": alert,
        "ran_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }
    logger.info(
        "upstream_mapping_patrol sampled=%s ok=%s mismatch=%s missing=%s "
        "unreachable=%s credential_failed=%s skipped=%s "
        "mismatch_rate=%.3f alert=%s",
        report["sampled"], existence["ok"], existence["mismatch"],
        existence["missing"], existence["unreachable"],
        existence["credential_failed"], existence["skipped"],
        mismatch_rate, alert)
    if alert:
        logger.error(
            "upstream_mapping_patrol ALERT 不一致率 %.1f%% 超阈值 %s%% "
            "或凭据失效：mismatch=%s missing=%s credential_failed=%s "
            "decided=%s",
            mismatch_rate * 100, patrol_alert_pct(db),
            existence["mismatch"], existence["missing"],
            existence["credential_failed"], decided)
    return report


# ---------------------------------------------------------------------------
# 周期门（janitor 入口）
# ---------------------------------------------------------------------------

def _last_run_at(db: Session) -> Optional[datetime]:
    try:
        from backend.integrations import store
        raw = store.get_value(db, STATE_KEY_LAST_RUN_AT, "", ttl=0)
    except Exception:
        return None
    try:
        return datetime.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _mark_run(db: Session, when: datetime) -> None:
    from backend.integrations import store
    store.write_values(
        db, {STATE_KEY_LAST_RUN_AT: when.isoformat(timespec="seconds")},
        {STATE_KEY_LAST_RUN_AT: "上游映射抽样巡检上次运行时刻"
                                "（O-09 内部状态键）"})
    db.commit()


def maybe_run_mapping_patrol(db: Session, *,
                             now: Optional[datetime] = None,
                             http_get=None, http_post=None) -> Optional[dict]:
    """janitor 入口：开关关 / 未到周期返回 None；到点先落运行标记
    （先占位再跑：janitor 在 api 与 worker 双进程都会 tick，占位
    让另一进程同窗口内看到已跑，不重复打对方额度），再跑一轮。"""
    if not patrol_enabled(db):
        return None
    now = now or datetime.now()
    last = _last_run_at(db)
    if last is not None and now - last < timedelta(
            days=patrol_interval_days(db)):
        return None
    _mark_run(db, now)
    return run_mapping_patrol(db, http_get=http_get, http_post=http_post,
                              now=now)


# ---------------------------------------------------------------------------
# 第 3 层：播放层一次性验证（手动触发，不常驻）
# ---------------------------------------------------------------------------

def verify_playback_samples(db: Session, *, source_id: Optional[int] = None,
                            http_get=None, http_post=None,
                            resolver=None) -> dict:
    """第 3 层：分层抽样 → 第 2 层通过的样本才调
    resolve_upstream_stream 核 302 地址，失败按错误码计数。

    每次调用都会让通过样本在上游产生真实起播探活（provider=
    upstream 的起播事件随之出现，与历史 0 条形成对照基线）——
    因此只许手动触发，绝不挂周期任务。播放总闸
    UPSTREAM_PLAYBACK_ENABLED 关着时 resolve 会按
    upstream_playback_disabled 失败、照实计入 failed，不假装能播。
    """
    rows = sample_mappings(db, patrol_sample_size(db), source_id=source_id)
    items, sources = _prefetch(db, rows)
    existence, verdicts = check_existence(
        db, rows, items, sources,
        http_get=http_get, http_post=http_post)
    resolve = upb.resolve_upstream_stream if resolver is None else resolver
    resolved = 0
    failed: dict = {}
    for row in rows:
        if verdicts.get(row.id) != "ok":
            continue
        try:
            url = resolve(db, row.source_id, row.upstream_item_id,
                          episode_index=row.episode_index or 1)
        except upb.UpstreamPlaybackError as exc:
            failed[exc.code] = failed.get(exc.code, 0) + 1
            continue
        if url:
            resolved += 1
        else:
            failed["empty_url"] = failed.get("empty_url", 0) + 1
    report = {
        "sampled": len(rows),
        "existence": existence,
        "existence_ok": existence["ok"],
        "resolved": resolved,
        "failed": failed,
    }
    logger.info(
        "upstream_mapping_patrol playback-verify sampled=%s "
        "existence_ok=%s resolved=%s failed=%s",
        report["sampled"], report["existence_ok"], resolved, failed)
    return report

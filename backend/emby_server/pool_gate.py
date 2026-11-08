"""公共贡献池判定层（goal-33 PR-1 地基 + PR-2 进池）。

PR-1 只落地基：配置读取、池资格判定（``pool_eligible``）、新判定
入口（``ensure_playback_allowed_for_item``）、池候选资格服务函数
（``grant_pool_candidate``）与绑定上限新口径
（``binding_limit_for_user``）。

PR-2 进池（本单）：选盘（``pick_pool_member``）、建池单
（``create_pool_job``）、池转存门禁（``ensure_pool_copy_gate``）。
PlaybackInfo 已接线到新判定入口（见 api.playback_info）：已绑
用户恒走原链（``ensure_user_copy_gate`` 逐字不动）；未绑用户且
池 eligible 时走池门禁。

PR-3 播出（本单）：serve 态正式接通——PlaybackInfo / video_stream
按池门禁确认的变体行下发池副本（见 api 的池播出段）；本模块补
记账写口（``note_pool_play_served``：play_starts 累加 + 变体
last_played_at + H1 原始事件 pool_play_events）、成员令牌口
（``pool_member_access_token``）与 sign 轨池判定
（``pool_sign_allowed``）。

硬闸：池总开关关时一切按池不存在（未绑用户仍走既有门禁原 403，
回归测试 test_pool_foundation
.test_redline_existing_gate_unchanged_when_pool_enabled 钉住）。

判定顺序（design §4.1，新入口内部）：
① CMS 免门禁 → not_applicable；② 已下发 play_sign 消费不追溯；
③ 已绑定 → bound；④ 池 eligible → pool；⑤ 都不满足 → 抛原 403
（文案选择与 playback_gate 逐字一致，含下线 provider 专用文案）。
软件授权前置：本入口在判定之首查（#1883 前置不动，已绑路径内层
ensure_* 会再查一次，纯读无副作用）；池转存门禁
ensure_pool_copy_gate 在判定之首同样查（直接调用方不漏）。
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import playback_gate as _gate
from backend.emby_server import transfer_decisions as _decisions
from backend.emby_server import transfer_variant as _tv
from backend.integrations import store

logger = logging.getLogger(__name__)

_GIB = 1024 ** 3

#: 配置家族 pool 的代码默认值（与 config_schema.POOL_CONFIG_SCHEMA
#: 同口径；字节默认值按 25TB 补充节修正值，不是 design §3 旧默认）。
POOL_CONFIG_DEFAULTS: dict = {
    "pool_enabled": False,
    "pool_audience": "all",
    "pool_join_mode": "apply_review",
    "pool_enabled_providers": "drive",
    "pool_member_capacity_bytes": 4_500_000_000_000,
    "pool_member_reserve_free_bytes": 300 * _GIB,
    "pool_member_daily_transfer_bytes": 600 * _GIB,
    "pool_member_daily_transfer_jobs": 0,
    "pool_item_max_copies": 2,
    "pool_copy_idle_evict_days": 30,
    "pool_global_concurrency": 2,
    "pool_enqueue_jitter_sec": 120,
    "pool_breaker_quota_hits_per_day": 3,
    "pool_breaker_cooldown_sec": 3600,
    "pool_pick_top_k": 3,
    "pool_member_max_bindings_per_user": 10,
}

#: 视为「有池候选/成员资格」的成员状态集合：这些状态下绑定上限
#: 走 pool_member_max_bindings_per_user。终态（exited / rejected /
#: ejected）不在集合内——退出后回落个人上限（任务书口径写死）。
POOL_LIMIT_MEMBER_STATUSES = ("candidate", "pending", "active", "suspended")

#: 池 preparing 态给观众的说明（GA-034）：池观众没有绑定盘，不许
#: 套用转存门禁缺省的「转存到你的绑定盘」文案。raise_for_transfer_
#: gate 优先用门禁自带 detail，故本模块所有 preparing 返回都带
#: 这一句；绑盘用户走 ensure_user_copy_gate，文案不受影响。
POOL_PREPARING_DETAIL = (
    "转存准备中：这部片正在往公共池里备，备好后就能看，请稍后再试")

#: 池播出记账的写时去重窗口（分钟，GA-033）：同一观众对同一副本
#: 窗口内只计首次——与 design §11.2 防刷口径一致，前移到写口是
#: 因为 pool_usage_daily.play_starts 是聚合计数、事后无法去重，
#: 写口必须同时管住事件行与聚合两处。其余防刷判定仍归 PR-6。
POOL_PLAY_DEDUP_WINDOW_MIN = 30


def _raw_config(db: Session) -> dict[str, str]:
    defaults = {
        key: ("true" if value is True else "false" if value is False
              else str(value))
        for key, value in POOL_CONFIG_DEFAULTS.items()
    }
    try:
        return store.get_values(db, list(POOL_CONFIG_DEFAULTS), defaults)
    except Exception:  # noqa: BLE001 配置层异常不许把判定打成 500
        logger.warning("公共池配置读取失败，回落代码默认", exc_info=True)
        return defaults


def pool_config_payload(db: Session) -> dict:
    """池配置生效值（类型化）：存量行优先，缺行/非法回代码默认。

    管理端 GET /api/admin/pool/config 与判定层共用这一份 payload，
    不许两处各解析一遍。
    """
    raw = _raw_config(db)
    out: dict = {}
    for key, default in POOL_CONFIG_DEFAULTS.items():
        value = raw.get(key, "")
        if isinstance(default, bool):
            out[key] = str(value).strip().lower() == "true"
        elif isinstance(default, int):
            try:
                out[key] = int(str(value).strip())
            except (TypeError, ValueError):
                out[key] = default
        else:
            out[key] = str(value) if value not in (None, "") else default
    return out


def pool_enabled(db: Session) -> bool:
    return bool(pool_config_payload(db)["pool_enabled"])


def _audience_eligible(db: Session, user) -> bool:
    """人群判定（design §3 pool_audience）：只走既有原语，不另立名单。

    - all：全部登录用户（付费墙在门禁之前，本判定不越付费墙）；
    - whitelist：codes.is_whitelist_account 唯一口径；
    - subscribers：subscriptions.has_active_subscription 口径。
    判定异常一律按人群外处理（fail-closed 回 off，不许把人放进池）。
    """
    audience = pool_config_payload(db)["pool_audience"]
    if audience == "all":
        return True
    if audience == "whitelist":
        from backend import codes

        return bool(codes.is_whitelist_account(db, user))
    if audience == "subscribers":
        from backend import subscriptions

        return bool(subscriptions.has_active_subscription(db, user.id))
    return False


def pool_eligible(db: Session, user, item) -> str:
    """池资格判定（纯函数）：返回 "off" 或 "eligible"。

    off 的三种情形（design §4.1）：池总开关关、条目免门禁（CMS
    库——池不介入）、人群外（含未登录/取不到用户 id）。异常同样
    回 off（fail-closed）。
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return "off"
    try:
        if not pool_enabled(db):
            return "off"
        if item is not None and not _gate.binding_required_for_item(db, item):
            return "off"
        if not _audience_eligible(db, user):
            return "off"
    except Exception:  # noqa: BLE001 判定异常不许放行
        logger.warning("公共池资格判定异常，按 off 处理", exc_info=True)
        return "off"
    return "eligible"


@dataclass
class PoolDecision:
    """新判定入口的裁决结果（PR-3 接线时消费）。

    kind：not_applicable（CMS 免门禁）/ issued_sign（消费已下发
    链接）/ bound（已绑定）/ pool（池放行）。blocked 不返回
    decision——直接抛 403（与既有门禁同口径）。
    """

    allowed: bool
    kind: str


def ensure_playback_allowed_for_item(
    db: Session, user, item, request=None
) -> PoolDecision:
    """新判定入口（PR-2 起由 PlaybackInfo 接线消费；PR-3 再接取流）。

    顺序见模块头。①–④ 任一命中即返回裁决；都不满足时不自抛，
    回落既有门禁本体 ``playback_gate.ensure_binding_playback_allowed``
    裁决（403 文案与执法点全站只此一份，不在此复制第二套）：它抛
    403 即本入口抛 403；它放行时（仅判定间隙边角）按已过门禁处理。
    软件授权前置在判定之首查（#1883 前置不动：CMS 免门禁条目
    同样先过授权；已绑路径内层 ensure_* 会再查一次，纯读无副
    作用）。
    """
    _gate._ensure_license_playback_allowed(db)
    if not _gate.binding_required_for_item(db, item):
        return PoolDecision(allowed=True, kind="not_applicable")
    if request is not None and _gate._request_uses_issued_play_sign(
            request, db):
        return PoolDecision(allowed=True, kind="issued_sign")
    user_id = getattr(user, "id", None)
    if user_id is not None and _gate.has_active_binding(db, user_id):
        return PoolDecision(allowed=True, kind="bound")
    if pool_eligible(db, user, item) == "eligible":
        return PoolDecision(allowed=True, kind="pool")
    _gate.ensure_binding_playback_allowed(db, user, request)
    return PoolDecision(allowed=True, kind="bound")


# ---------------------------------------------------------------- 成员资格


def grant_pool_candidate(db: Session, user_id: int) -> em.PoolMember:
    """给站内账号建池候选资格行（管理员预批，通审决议 2）。

    幂等：已有状态 ∈ POOL_LIMIT_MEMBER_STATUSES 的候选/成员行时
    直接返回该行，不重复建。本单只落服务函数，管理端入口 PR-4
    再接。候选行 binding_id=NULL、provider=NULL（还没绑盘）。
    """
    if user_id is None:
        raise ValueError("grant_pool_candidate 需要 user_id")
    existing = (
        db.query(em.PoolMember)
        .filter(
            em.PoolMember.user_id == user_id,
            em.PoolMember.status.in_(POOL_LIMIT_MEMBER_STATUSES),
        )
        .order_by(em.PoolMember.id.asc())
        .first()
    )
    if existing is not None:
        return existing
    now = datetime.now()
    row = em.PoolMember(
        binding_id=None, user_id=user_id, provider=None,
        status="candidate", status_changed_at=now)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def binding_limit_for_user(db: Session, user) -> int:
    """绑定上限新口径（drive_oauth 建绑校验唯一判定）。

    用户有 status ∈ POOL_LIMIT_MEMBER_STATUSES 的 pool_members
    行 → 上限取配置 pool_member_max_bindings_per_user（默认 10，
    改配置即生效）；否则个人上限不变（user_bindings
    .binding_limit_per_user：env BYOS_BINDING_LIMIT_PER_USER，
    默认 3）。``user`` 可传 WebUser 或用户 id。
    """
    user_id = getattr(user, "id", user)
    if user_id is not None:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            uid = None
        if uid is not None:
            hit = (
                db.query(em.PoolMember.id)
                .filter(
                    em.PoolMember.user_id == uid,
                    em.PoolMember.status.in_(POOL_LIMIT_MEMBER_STATUSES),
                )
                .first()
            )
            if hit is not None:
                return int(pool_config_payload(db)[
                    "pool_member_max_bindings_per_user"])
    from backend.emby_server import user_bindings as _ub

    return _ub.binding_limit_per_user()

# ---------------------------------------------------------------- 选盘与建单（PR-2 进池单）
# design §6.1/§6.2：选盘打分 + create_pool_job + 池单并发计数。

#: 池配额读数缓存 TTL（秒）：about.get 口径沿 quota_dashboard 路径，
#: 读数缓存 10 分钟，免每次选盘都打 Drive about。
_POOL_QUOTA_CACHE_TTL_SEC = 600
_POOL_QUOTA_CACHE: dict = {}


def clear_pool_quota_cache() -> None:
    """清空池配额读数缓存（测试用；生产靠 TTL 自然过期）。"""
    _POOL_QUOTA_CACHE.clear()


def _member_quota(db: Session, member, quota_fn=None):
    """成员盘剩余（about.get 口径）：返回 {"limit","usage","free"} 或 None。

    quota_fn 可注入（测试用，签名 quota_fn(db, binding)）；默认走
    transfer_variant._default_drive_quota（UserDriveApiClient
    .about_storage，沿 quota_dashboard 同款路径）。读数按 binding_id
    缓存 10 分钟。读不到/读失败回 None（未知）：选盘时按容量未知
    处理（不因一次 about 抖动把成员判死），不抛。
    """
    binding_id = getattr(member, "binding_id", None)
    if binding_id is None:
        return None
    now = datetime.now()
    hit = _POOL_QUOTA_CACHE.get(binding_id)
    if hit is not None and (now - hit[0]).total_seconds() < _POOL_QUOTA_CACHE_TTL_SEC:
        return hit[1]
    quota = None
    try:
        binding = (db.query(em.UserStorageBinding)
                   .filter(em.UserStorageBinding.id == binding_id).first())
        if binding is not None:
            if quota_fn is not None:
                quota = quota_fn(db, binding)
            else:
                quota = _tv._default_drive_quota(db, binding)
    except Exception:  # noqa: BLE001 — 配额读失败不挡选盘，按未知处理
        logger.warning("池成员 %s 配额读取失败，按未知处理",
                       getattr(member, "id", None), exc_info=True)
        quota = None
    _POOL_QUOTA_CACHE[binding_id] = (now, quota)
    return quota


def _member_daily_usage(db: Session, member_id: int):
    """成员当日用量（transfer_jobs, transfer_bytes），无行回 (0, 0)。"""
    row = (db.query(em.PoolUsageDaily)
           .filter(em.PoolUsageDaily.member_id == member_id,
                   em.PoolUsageDaily.day == datetime.now().date()).first())
    if row is None:
        return 0, 0
    return int(row.transfer_jobs or 0), int(row.transfer_bytes or 0)


def _bump_pool_usage(db: Session, member_id: int, *, jobs=0, bytes_=0,
                     quota_hits=0, play_starts=0):
    """成员当日用量累加（get-or-create，不 commit，调用方提交）。"""
    today = datetime.now().date()
    row = (db.query(em.PoolUsageDaily)
           .filter(em.PoolUsageDaily.member_id == member_id,
                   em.PoolUsageDaily.day == today).first())
    if row is None:
        row = em.PoolUsageDaily(member_id=member_id, day=today,
                                transfer_jobs=0, transfer_bytes=0,
                                play_starts=0, quota_hits=0)
        db.add(row)
    row.transfer_jobs = int(row.transfer_jobs or 0) + jobs
    row.transfer_bytes = int(row.transfer_bytes or 0) + bytes_
    row.quota_hits = int(row.quota_hits or 0) + quota_hits
    row.play_starts = int(row.play_starts or 0) + play_starts
    return row


def _enabled_providers(cfg: dict):
    """池启用 provider 集合（逗号配置，默认 drive）。"""
    raw = str(cfg.get("pool_enabled_providers") or "drive")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))

def _score_member(db: Session, member, item, cfg: dict, *,
                  quota_fn=None):
    """成员打分（design §6.1）：返回 (eligible, score, 明细 dict)。

    硬过滤：成员非 active、绑定非 active 或 provider 不在池启用
    列表、余量（free − 保底）< 文件大小、日字节余量 < 文件大小、
    日单数已满。配额读不到（free 未知）时容量项按中性 0.5 计、
    不因未知判死。score = 0.7 × 容量比 + 0.3 × 日字节比（各钳
    [0, 1]；日限 0 = 不限，日字节比按 1.0 计）。
    """
    file_size = _tv._to_int(getattr(item, "size", None)) or 0
    detail = {"member_id": int(member.id), "eligible": False,
              "score": 0.0, "reason": ""}
    binding = (db.query(em.UserStorageBinding)
               .filter(em.UserStorageBinding.id == member.binding_id)
               .first()) if getattr(member, "binding_id", None) else None
    if member.status != "active":
        detail["reason"] = f"member_status={member.status}"
        return False, 0.0, detail
    if binding is None or binding.status != "active":
        detail["reason"] = "binding_inactive"
        return False, 0.0, detail
    if (binding.provider or "") not in _enabled_providers(cfg):
        detail["reason"] = f"provider={binding.provider}"
        return False, 0.0, detail
    capacity_cap = int(getattr(member, "capacity_cap_bytes", None)
                       or cfg["pool_member_capacity_bytes"])
    reserve = int(cfg["pool_member_reserve_free_bytes"])
    quota = _member_quota(db, member, quota_fn=quota_fn)
    free = None
    if quota is not None:
        free = _tv._to_int(quota.get("free"))
    if free is not None:
        free_after_reserve = free - reserve
        if free_after_reserve < file_size:
            detail["reason"] = (f"free_after_reserve={free_after_reserve} "
                                f"< file_size={file_size}")
            detail["free_bytes"] = free
            return False, 0.0, detail
        capacity_ratio = _clamp01(free_after_reserve / max(1, capacity_cap))
        detail["free_bytes"] = free
    else:
        capacity_ratio = 0.5
    used_jobs, used_bytes = _member_daily_usage(db, member.id)
    daily_bytes_limit = int(cfg["pool_member_daily_transfer_bytes"])
    daily_jobs_limit = int(cfg["pool_member_daily_transfer_jobs"])
    if daily_jobs_limit > 0 and used_jobs >= daily_jobs_limit:
        detail["reason"] = (f"daily_jobs={used_jobs} "
                            f">= limit={daily_jobs_limit}")
        return False, 0.0, detail
    if daily_bytes_limit > 0:
        daily_left = daily_bytes_limit - used_bytes
        if daily_left < file_size:
            detail["reason"] = (f"daily_bytes_left={daily_left} "
                                f"< file_size={file_size}")
            return False, 0.0, detail
        daily_ratio = _clamp01(daily_left / daily_bytes_limit)
        detail["daily_bytes_left"] = daily_left
    else:
        daily_ratio = 1.0
    score = 0.7 * capacity_ratio + 0.3 * daily_ratio
    detail.update({"eligible": True, "score": round(score, 4),
                   "reason": "ok"})
    return True, score, detail


def _serve_score(db: Session, member, *, quota_fn=None) -> float:
    """播出选副本打分：只看容量健康度（free / 容量上限）。

    服务既有副本不走进池限额（日限/保底不拦播出）；配额未知按
    0.5 中性计。返回 [0, 1]。
    """
    cfg = pool_config_payload(db)
    capacity_cap = int(getattr(member, "capacity_cap_bytes", None)
                       or cfg["pool_member_capacity_bytes"])
    quota = _member_quota(db, member, quota_fn=quota_fn)
    free = None
    if quota is not None:
        free = _tv._to_int(quota.get("free"))
    if free is None:
        return 0.5
    return _clamp01(free / max(1, capacity_cap))


def pick_pool_member(db: Session, item, *, force=False, quota_fn=None):
    """选盘（design §6.1）：返回 (member|None, candidates)。

    candidates：每位 active 成员一行 {member_id, eligible, score,
    reason, ...}（打分明细，供 note_decision 留痕）。合格者按分
    排序取 top-k（pool_pick_top_k，默认 3）再均匀随机挑一个——
    分高者中选概率高、又不把全部新片压到同一盘。24h 内已有本条
    目池单的成员不参与（force 越过，供门禁重触发）。无可用盘时
    记一笔 decision=none 的留痕；有可用盘时留痕记在建单成功处。
    """
    cfg = pool_config_payload(db)
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status == "active")
               .order_by(em.PoolMember.id.asc()).all())
    candidates = []
    scored = []
    for member in members:
        if not force and _tv._recent_job_exists(
                db, getattr(item, "id", None), "pool", str(member.id)):
            candidates.append({"member_id": int(member.id),
                               "eligible": False, "score": 0.0,
                               "reason": "recent_pool_job_24h"})
            continue
        eligible, score, detail = _score_member(
            db, member, item, cfg, quota_fn=quota_fn)
        candidates.append(detail)
        if eligible:
            scored.append((score, member))
    if not scored:
        _decisions.note_decision(
            scope="pool_pick", resource_key=f"item:{item.id}",
            decision="none", reason="no eligible member",
            candidates=candidates)
        return None, candidates
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_k = max(1, int(cfg["pool_pick_top_k"]))
    top = scored[:top_k]
    return random.choice([member for _, member in top]), candidates

def _pool_inflight_count(db: Session) -> int:
    """进行中池单数（pending + running，全池）。"""
    return (db.query(em.TransferJob)
            .filter(em.TransferJob.owner_scope == "pool",
                    em.TransferJob.status.in_(("pending", "running")))
            .count())


def _latest_pool_job(db: Session, item_id, statuses=None):
    """该条目最新的一行池单（或 None）；statuses 限状态。"""
    q = (db.query(em.TransferJob)
         .filter(em.TransferJob.item_id == item_id,
                 em.TransferJob.owner_scope == "pool"))
    if statuses is not None:
        q = q.filter(em.TransferJob.status.in_(statuses))
    return q.order_by(em.TransferJob.id.desc()).first()


def _pool_retrigger_budget_ok(db: Session, item_id) -> bool:
    """池重触发预算：24h 内同条目池单 < 2 行才许重建（沿门禁既有口径：
    一次失败 + 一次重触发 = 2 行，第 3 次起不再建，防播放器重试
    刷成任务洪水）。"""
    now = datetime.now()
    count = (db.query(em.TransferJob)
             .filter(em.TransferJob.item_id == item_id,
                     em.TransferJob.owner_scope == "pool",
                     _tv._throttle_window_filter(now))
             .count())
    return count < 2


def _pool_failed_detail(db: Session, item, job) -> str:
    """池失败透传文案：带任务行上的人话原因，不只给笼统失败。

    共享原件 fid 一律不进文案（出现即替换为占位，防红线外泄）。
    """
    reason = (getattr(job, "error_detail", "") or "").strip()
    orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
    if orig_fid and orig_fid in reason:
        reason = reason.replace(orig_fid, "[共享原件]")
    if reason:
        return f"池转存失败：{reason}"
    return "池转存失败，请稍后重试（不走共享播放）"


def create_pool_job(db: Session, item, *, trigger="playback",
                    force=False, quota_fn=None):
    """建池单（design §6.2）：选盘 → 限额 → 落行 → 留痕 → 唤醒。

    限额（任一命中抛 TransferError，不落行；错误优先级沿
    create_user_job：节流 → 源校验 → 绑定校验）：
    - 池总开关关 → pool_disabled；
    - 转存总闸关 → master_off；
    - provider 不在池启用列表 → pool_provider_unsupported；
    - Drive 条目取不到源盘 → no_drive；
    - 进行中池单数 ≥ pool_global_concurrency → throttled；
    - 无可用盘 → no_member；
    - 目标成员 24h 内已有本条目池单（force 越过）→ throttled。
    建单：owner_scope=pool、owner_ref=成员 id、
    dest_drive_id=pooldrive:<成员id>、drive_id=源盘、
    trigger=playback；next_retry_at = now + [0, jitter] 均匀抖动
    （jitter=0 时留 NULL，工人立即认领）；cooldown 落字；当日
    用量 transfer_jobs +1；note_decision 留痕；唤醒工人。
    成功返回已提交的任务行。
    """
    cfg = pool_config_payload(db)
    if not cfg["pool_enabled"]:
        raise _tv.TransferError("pool_disabled", "公共贡献池未开启")
    if not _tv.master_enabled():
        raise _tv.TransferError("master_off", "转存变体总闸未开")
    provider = _tv._item_provider(db, item)
    if provider is None or provider not in _enabled_providers(cfg):
        raise _tv.TransferError(
            "pool_provider_unsupported",
            f"条目 provider={provider} 不在池启用列表")
    source_drive_id = _tv._source_drive_id(db, item, provider)
    if provider == "drive" and not source_drive_id:
        raise _tv.TransferError("no_drive", "Drive 条目取不到源盘标识")
    if _pool_inflight_count(db) >= max(1, int(cfg["pool_global_concurrency"])):
        raise _tv.TransferError("throttled", "池进行中任务已达全局并发上限")
    member, candidates = pick_pool_member(
        db, item, force=force, quota_fn=quota_fn)
    if member is None:
        # 24h 节流挡住全部候选（design §6.2 第 6 步）→ throttled，
        # 与“根本无可用盘”（no_member）区分：前者是限流、后者是缺盘。
        if any(c.get("reason") == "recent_pool_job_24h"
               for c in candidates):
            raise _tv.TransferError(
                "throttled", "24h 内该成员已有本条目池单")
        raise _tv.TransferError("no_member", "暂无可用贡献盘")
    if not force and _tv._recent_job_exists(
            db, getattr(item, "id", None), "pool", str(member.id)):
        raise _tv.TransferError(
            "throttled", "24h 内该成员已有本条目池单")
    binding = (db.query(em.UserStorageBinding)
               .filter(em.UserStorageBinding.id == member.binding_id)
               .first())
    if binding is None or binding.status != "active":
        raise _tv.TransferError(
            "no_binding", "成员 Drive 绑定已失效，不建池单")
    jitter_sec = int(cfg["pool_enqueue_jitter_sec"])
    job = em.TransferJob(
        item_id=item.id, trigger=trigger, drive_id=source_drive_id,
        provider=provider, owner_scope="pool", owner_ref=str(member.id),
        dest_drive_id=f"pooldrive:{member.id}", status="pending",
        next_retry_at=(datetime.now() + timedelta(
            seconds=random.uniform(0, jitter_sec))) if jitter_sec > 0 else None)
    db.add(job)
    db.flush()
    _tv._stamp_job_cooldown(job)
    _bump_pool_usage(db, member.id, jobs=1)
    score = next((c.get("score", 0.0) for c in candidates
                  if c.get("member_id") == int(member.id)), 0.0)
    _decisions.note_decision(
        scope="pool_pick", resource_key=f"item:{item.id}",
        decision=f"member:{member.id}",
        reason=f"score={score:.3f} member={member.id}",
        candidates=candidates)
    db.commit()
    _tv.notify_transfer_wakeup(job.id)
    return job

# ---------------------------------------------------------------- 池转存门禁（PR-2）


def _pick_ready_pool_variant(db: Session, item_id, *, quota_fn=None):
    """挑一份可播的 ready 池副本：返回 (变体行, 成员行) 或 None。

    判定与 ensure_pool_copy_gate 的 serve 分支同一份：变体行
    status=ready 且 stale_count=0、成员 active 且未在 breaker
    冷却内；多份时按 §6.1 播出打分（_serve_score）挑最健康一份。
    纯读、无副作用。
    """
    ready_rows = (db.query(em.TransferVariant)
                  .filter(em.TransferVariant.item_id == item_id,
                          em.TransferVariant.owner_scope == "pool",
                          em.TransferVariant.status == "ready",
                          em.TransferVariant.stale_count == 0)
                  .order_by(em.TransferVariant.id.desc()).all())
    best = None
    best_score = -1.0
    for row in ready_rows:
        try:
            member_id = int((getattr(row, "owner_ref", "") or "").strip())
        except (TypeError, ValueError):
            continue
        member = (db.query(em.PoolMember)
                  .filter(em.PoolMember.id == member_id).first())
        if member is None or member.status != "active":
            continue
        breaker_until = getattr(member, "breaker_until", None)
        if breaker_until is not None and breaker_until > datetime.now():
            continue
        score = _serve_score(db, member, quota_fn=quota_fn)
        if score > best_score:
            best = (row, member)
            best_score = score
    return best


def pool_serve_candidate(db: Session, item):
    """该条目当下可播的池副本候选：(变体行, 成员 id) 或 None。

    供变体清单信号（api._pool_copy_available）与 sign 轨判定
    （pool_sign_allowed）复核变体行——信号与行双对上才算数。
    """
    picked = _pick_ready_pool_variant(db, getattr(item, "id", None))
    if picked is None:
        return None
    row, member = picked
    return row, int(member.id)


def ensure_pool_copy_gate(db: Session, item, user, request=None, *,
                          quota_fn=None) -> dict:
    """池转存门禁（PR-2）：未绑用户的池副本判定（design §4.2/§6.2）。

    调用方保证：用户未绑定、池 eligible（见
    ensure_playback_allowed_for_item）。本函数只做池内判定：
    ① 软件授权前置（与 ensure_user_copy_gate 同款，不可绕过）；
    ② 池关 → failed/pool_disabled（防御口径，正常不可达）；
    ③ 有 ready 池副本（stale_count==0、成员 active、未熔断）→
       serve_pool_copy（多副本按 §6.1 打分挑最健康一份；播出由
       PR-3 接通：PlaybackInfo / video_stream 按池副本下发，
       serve 态不再映射 failed）；
    ④ 在途池单 → preparing/job_in_flight；
    ⑤ 最新池单失败 → retriable 预算内重触发 / 终态透传原因 /
       未归类 preparing/throttled（沿门禁既有口径）；
    ⑥ done 单无 ready 副本 → 预算内重建 job_rebuilt；
    ⑦ 无在途/失败 → create_pool_job → preparing/job_created，
       限额命中 → preparing/throttled。
    返回 dict 恒带 "pool": True（调用方据此跳过用户盘当场先办）。
    任何返回都不含共享原件 fid 与 SA 令牌。
    """
    _gate._ensure_license_playback_allowed(db)
    if not pool_enabled(db):
        return {"verdict": "failed", "reason": "pool_disabled",
                "detail": "公共贡献池未开启", "pool": True}
    item_id = getattr(item, "id", None)
    # ③ 就绪池副本（stale_count==0、成员 active、未熔断）。
    picked = _pick_ready_pool_variant(db, item_id, quota_fn=quota_fn)
    if picked is not None:
        row, member = picked
        return {"verdict": "serve_pool_copy",
                "reason": "has_ready_variant", "variant": row,
                "member_id": int(member.id), "pool": True}
    # ④ 在途池单（任一成员）。
    inflight = _latest_pool_job(db, item_id, statuses=("pending", "running"))
    if inflight is not None:
        return {"verdict": "preparing", "reason": "job_in_flight",
                "job_id": inflight.id, "pool": True,
                "detail": POOL_PREPARING_DETAIL}
    # ⑤ 最新池单失败。
    failed = _latest_pool_job(db, item_id, statuses=("failed",))
    if failed is not None:
        kind = (getattr(failed, "error_kind", "") or "").strip()
        if kind == "retriable":
            if _pool_retrigger_budget_ok(db, item_id):
                try:
                    new_job = create_pool_job(
                        db, item, force=True, quota_fn=quota_fn)
                except _tv.TransferError as exc:
                    if exc.kind == "throttled":
                        return {"verdict": "preparing",
                                "reason": "throttled",
                                "job_id": failed.id, "pool": True,
                                "detail": POOL_PREPARING_DETAIL}
                    return {"verdict": "failed", "reason": exc.kind,
                            "job_id": failed.id,
                            "detail": str(exc) or None, "pool": True}
                return {"verdict": "preparing",
                        "reason": "job_retriggered",
                        "job_id": new_job.id, "pool": True,
                        "detail": POOL_PREPARING_DETAIL}
            return {"verdict": "failed", "reason": "retriable",
                    "job_id": failed.id,
                    "detail": _pool_failed_detail(db, item, failed),
                    "pool": True}
        if (kind in _tv._TERMINAL_JOB_KINDS
                or kind in _tv._AUTH_TERMINAL_KINDS
                or kind == "insufficient_space"):
            return {"verdict": "failed", "reason": kind,
                    "job_id": failed.id,
                    "detail": _pool_failed_detail(db, item, failed),
                    "pool": True}
        return {"verdict": "preparing", "reason": "throttled",
                "job_id": failed.id, "pool": True,
                "detail": POOL_PREPARING_DETAIL}
    # ⑥ done 单无 ready 副本（副本已释放/变 stale）：预算内重建。
    done = _latest_pool_job(db, item_id, statuses=("done",))
    if done is not None:
        if _pool_retrigger_budget_ok(db, item_id):
            try:
                new_job = create_pool_job(
                    db, item, force=True, quota_fn=quota_fn)
            except _tv.TransferError as exc:
                if exc.kind == "throttled":
                    return {"verdict": "preparing",
                            "reason": "throttled",
                            "job_id": done.id, "pool": True,
                            "detail": POOL_PREPARING_DETAIL}
                return {"verdict": "failed", "reason": exc.kind,
                        "job_id": done.id,
                        "detail": str(exc) or None, "pool": True}
            return {"verdict": "preparing", "reason": "job_rebuilt",
                    "job_id": new_job.id, "pool": True,
                    "detail": POOL_PREPARING_DETAIL}
        return {"verdict": "preparing", "reason": "throttled",
                "job_id": done.id, "pool": True,
                "detail": POOL_PREPARING_DETAIL}
    # ⑦ 建单。
    try:
        new_job = create_pool_job(db, item, quota_fn=quota_fn)
    except _tv.TransferError as exc:
        if exc.kind == "throttled":
            return {"verdict": "preparing", "reason": "throttled",
                    "pool": True, "detail": POOL_PREPARING_DETAIL}
        return {"verdict": "failed", "reason": exc.kind,
                "detail": str(exc) or None, "pool": True}
    return {"verdict": "preparing", "reason": "job_created",
            "job_id": new_job.id, "pool": True,
            "detail": POOL_PREPARING_DETAIL}


# ---------------------------------------------------------------- 播出记账与令牌口（PR-3）


def note_pool_play_served(member_id, variant_id, item_id,
                          viewer_user_id) -> None:
    """池播出成功下发记账（design §5.3 + §11.5 H1）。

    与 play_starts 累加同点调用（serve_pool_copy 每次成功下发/
    302 时）：一把写三样——``pool_usage_daily.play_starts`` +1、
    变体行 ``last_played_at`` 更新、``pool_play_events`` 落一条
    原始事件。独立会话写、失败静默（沿 PlayStartEvent 纪律）：
    记账失败绝不打挂已经下发成功的播放，本函数永不抛异常，
    也不进起播热路径的同步事务（调用方经 run_db 或直接调都行，
    内部另开会话）。

    写时窗口去重（GA-033）：同一观众对同一副本
    ``POOL_PLAY_DEDUP_WINDOW_MIN`` 分钟内只计首次——窗口内已有
    事件行时不再新增事件、也不再累加 play_starts（聚合计数事后
    无法去重，两处必须同管）；跨端点同一次播放（PlaybackInfo
    直链下发后客户端再打 /stream）由此只记一笔。
    ``last_played_at`` 不是计数、是淘汰热度输入，真实起播照常
    刷新。其余防刷判定（本人/同人互看、日上限冻结）仍归 PR-6
    聚合纯函数，本函数不做。
    """
    try:
        mid = int(member_id)
        vid = int(variant_id)
        iid = int(item_id)
        uid = int(viewer_user_id)
    except (TypeError, ValueError):
        return
    db = None
    try:
        from backend.database import SessionLocal

        db = SessionLocal()
        now = datetime.now()
        window_start = now - timedelta(
            minutes=POOL_PLAY_DEDUP_WINDOW_MIN)
        dup = (db.query(em.PoolPlayEvent)
               .filter(em.PoolPlayEvent.viewer_user_id == uid,
                       em.PoolPlayEvent.variant_id == vid,
                       em.PoolPlayEvent.created_at >= window_start)
               .first())
        counted = dup is None
        if counted:
            _bump_pool_usage(db, mid, play_starts=1)
        row = (db.query(em.TransferVariant)
               .filter(em.TransferVariant.id == vid).first())
        if row is not None:
            row.last_played_at = now
        if counted:
            db.add(em.PoolPlayEvent(
                member_id=mid, variant_id=vid, item_id=iid,
                viewer_user_id=uid, created_at=now))
        db.commit()
    except Exception:  # noqa: BLE001 — 记账失败静默，不挡播放
        logger.warning(
            "池播出记账失败 member=%s variant=%s item=%s viewer=%s",
            member_id, variant_id, item_id, viewer_user_id,
            exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def reconcile_dead_pool_copy(db: Session, variant) -> dict:
    """池副本 serve 路径实证不可用（302 前存在性校验 404）后的对账（GA-032）。

    与用户副本 ``reconcile_dead_drive_copy`` 同形状：先自愈——
    副本可能只是 anyone-reader 被贡献者收回（本体仍在成员盘
    里），绕过已公开缓存、用成员绑定令牌经
    ``ensure_variant_copy_public`` 重授；授成返回 healed，调用方
    重走存在性校验即可放行。自愈失败（文件真被删 / 成员绑定
    失效）记一次 stale strike（``mark_variant_stale_once``）：
    ``_pick_ready_pool_variant`` 只挑 stale_count==0 的行，门禁
    下轮不再判 serve、改走池建单重建，死副本不再反复下发。
    本函数永不抛异常进播放热路径。
    """
    fid = (getattr(variant, "variant_file_id", "") or "").strip()
    member_id = None
    try:
        member_id = int(getattr(variant, "owner_ref", "") or "")
    except (TypeError, ValueError):
        member_id = None
    if fid and member_id is not None:
        try:
            _tv.copy_public_cache_drop(f"pool:{member_id}", fid)
            if _tv.ensure_variant_copy_public(db, variant, fid):
                return {"action": "healed"}
        except Exception:  # noqa: BLE001
            logger.warning("池副本死链自愈异常 variant=%s",
                           getattr(variant, "id", None), exc_info=True)
    try:
        _tv.mark_variant_stale_once(db, getattr(variant, "id", None))
    except Exception:  # noqa: BLE001
        logger.warning("池副本死链记 strike 异常", exc_info=True)
    return {"action": "stale_marked"}


def pool_member_access_token(db: Session, member_id) -> str:
    """成员绑定令牌（池中继专用口，绝不下发给观众）。

    成员行 → 绑定行 → 绑定级令牌口（drive_oauth
    .get_binding_drive_access_token，与 BYOS 中继同源）。只许取
    成员自己绑定的令牌：成员不存在、绑定缺失/非 active、令牌取
    不到一律回 ""，永不抛异常，绝不回落到 SA 令牌或观众令牌。
    本口只许服务端内部取流（video_stream 的贡献者令牌中继腿）
    使用；把返回值写进任何观众可见响应（PlaybackInfo 的
    RequiredHttpHeaders 等）即 GA-030 泄漏，已按用户裁定禁止。
    """
    try:
        member = (db.query(em.PoolMember)
                  .filter(em.PoolMember.id == int(member_id)).first())
        if member is None:
            return ""
        binding_id = getattr(member, "binding_id", None)
        if binding_id is None:
            return ""
        binding = (db.query(em.UserStorageBinding)
                   .filter(em.UserStorageBinding.id == binding_id)
                   .first())
        if binding is None or binding.status != "active":
            return ""
        from backend.emby_server import drive_oauth as _do

        return _do.get_binding_drive_access_token(db, binding) or ""
    except Exception:  # noqa: BLE001 — 令牌取不到按无令牌处理
        logger.warning("池成员令牌读取失败 member=%s",
                       member_id, exc_info=True)
        return ""


def pool_sign_allowed(db: Session, user_id, item) -> bool:
    """sign 轨池判定（design §4.1）：未绑用户可签名的池条件。

    池 eligible（人群判定要用户行，按 id 取）且该条目有 ready
    池副本才可签；不 ready 不签——客户端直打流端点时由流端点
    现场走池门禁回 409 准备中。异常一律 False（不签是安全侧）。
    """
    try:
        if user_id is None:
            return False
        from backend import models as _web_models

        user = (db.query(_web_models.WebUser)
                .filter(_web_models.WebUser.id == int(user_id)).first())
        if user is None:
            return False
        if pool_eligible(db, user, item) != "eligible":
            return False
        return pool_serve_candidate(db, item) is not None
    except Exception:  # noqa: BLE001 — 判定异常不许签
        logger.warning("池 sign 判定异常，按不可签处理", exc_info=True)
        return False

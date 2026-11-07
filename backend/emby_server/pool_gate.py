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

PR-4 生命周期（本单）：成员状态机全套（申请/批准/拒绝/暂停/
恢复/清退/退出）收口在文末「成员生命周期」一节——所有状态
变更经 ``transition_member_status`` 落 H2 流水
（``pool_member_status_events``）；清退违规标志在成员行
``violation_*`` 列；绑定失效/解绑连带钩子（``on_binding_failed``
/ ``on_binding_deleted``）由 user_bindings 两处落态口调用。
管理端入口在 backend/api/pool_admin.py，用户端三口在 portal.py。

PR-5 保护收尾（本单）：熔断判定本体在本模块「熔断与副本计数」
一节（成员级 trip/半开试探恢复 + 池级暂停建单判定 + 可下发
副本计数）；周期执行面（巡检/淘汰/补位/半开试探调度）在
``pool_janitor.py``，只调本模块与 transfer_variant 的既有口，
不另立判定。GA-036 的熔断类死键（pool_breaker_quota_hits_
per_day / pool_breaker_cooldown_sec）与 pool_item_max_copies
自本单起有真实消费方。

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

# ---------------------------------------------------------------- 熔断与副本计数（PR-5）
# design §6.3 熔断 + §6.2 条目级副本上限 + 补充节 S4 补位共用计数。
#
# 成员级熔断：当日 quota_hits（pool_usage_daily；记数点在
# transfer_variant 池单 copy 配额失败处与 api 两处 serve 探测
# 处）达 pool_breaker_quota_hits_per_day → 经
# transition_member_status 收口落 suspended(breaker) +
# breaker_until = now + 冷却。同回合内再熔断冷却翻倍、上限
# 24h；回合以 breaker_recovered 事件为界（恢复成功即清零）。
# 半开试探：冷却到期后由 pool_janitor 调 breaker_recover_probe
# 对该成员一份 ready 副本跑 quota 探测，非 quota 才恢复
# active，再命中立即回熔断（breaker_retrip 事件留痕）。
#
# 池级熔断：今日参与成员人均 quota_hits 达同一阈值 → 全池
# 暂停建单（create_pool_job 抛 pool_breaker）。单盘命中只摘
# 单盘、全池普遍命中才摘全池，与成员级共用同一阈值键。

#: 熔断冷却上限（design §6.3：翻倍上限 24h）。
BREAKER_MAX_COOLDOWN_SEC = 24 * 3600


def _member_usage_today(db: Session, member_id: int):
    """成员当日用量行（无行回 None）。"""
    return (db.query(em.PoolUsageDaily)
            .filter(em.PoolUsageDaily.member_id == member_id,
                    em.PoolUsageDaily.day == datetime.now().date())
            .first())


def _member_quota_hits_today(db: Session, member_id: int) -> int:
    row = _member_usage_today(db, member_id)
    return int(row.quota_hits or 0) if row is not None else 0


def _breaker_episode_level(db: Session, member_id: int) -> int:
    """当前熔断回合内已发生的熔断次数（trip + retrip 事件数）。

    事件按 id 倒序走，碰到 breaker_recovered 即停：恢复成功
    之后的历史熔断不算进本回合。
    """
    events = (db.query(em.PoolMemberStatusEvent)
              .filter(em.PoolMemberStatusEvent.member_id == member_id)
              .order_by(em.PoolMemberStatusEvent.id.desc())
              .limit(200).all())
    level = 0
    for event in events:
        if event.reason == "breaker_recovered":
            break
        if event.reason in ("breaker", "breaker_retrip"):
            level += 1
    return level


def _breaker_cooldown_sec(db: Session, member, cfg: dict) -> int:
    """本次熔断冷却秒数：基础值 × 2^回合层数，上限 24h。

    在写本次事件之前调用：层数只数此前已落的熔断事件，首次
    熔断层数 0 → 基础冷却；试探失败再熔断层数 1 → 翻倍。
    """
    base = int(cfg["pool_breaker_cooldown_sec"])
    level = _breaker_episode_level(db, member.id)
    return min(base * (2 ** level), BREAKER_MAX_COOLDOWN_SEC)


def trip_member_breaker(db: Session, member, *, detail: str = "",
                        now=None) -> bool:
    """把 active 成员打进熔断（不 commit，随调用方事务）。

    落 suspended(breaker) + breaker_until，并记 pool_breaker
    决策留痕（scope=pool_breaker，design §2.3）。非 active
    成员不重复打，返回 False。
    """
    if member is None or member.status != "active":
        return False
    cfg = pool_config_payload(db)
    now = now or datetime.now()
    cooldown = _breaker_cooldown_sec(db, member, cfg)
    member.suspended_reason = "breaker"
    member.breaker_until = now + timedelta(seconds=cooldown)
    transition_member_status(db, member, "suspended", reason="breaker")
    _decisions.note_decision(
        scope="pool_breaker", resource_key=f"member:{member.id}",
        decision="trip",
        reason=f"cooldown_sec={cooldown} {detail}".strip(),
        candidates=[])
    return True


def maybe_trip_member_breaker(db: Session, member, *, now=None) -> bool:
    """当日 quota_hits 达阈值则熔断该成员（design §6.3）。

    阈值 pool_breaker_quota_hits_per_day=0 表示不熔断。不
    commit（骑调用方事务）。
    """
    if member is None or member.status != "active":
        return False
    cfg = pool_config_payload(db)
    threshold = int(cfg["pool_breaker_quota_hits_per_day"])
    if threshold <= 0:
        return False
    hits = _member_quota_hits_today(db, member.id)
    if hits < threshold:
        return False
    return trip_member_breaker(
        db, member, detail=f"quota_hits={hits}>={threshold}", now=now)


def note_pool_quota_hit(member_id) -> None:
    """池副本 quota 命中记数 + 熔断检查（serve 路专用记数口）。

    与 note_pool_play_served 同纪律：独立会话写、失败静默——
    记数失败绝不打挂已经裁决完的播放，本函数永不抛异常。
    copy 路的命中在工人会话内 _bump_pool_usage +
    maybe_trip_member_breaker，不经本口（防重复记数）。
    """
    try:
        mid = int(member_id)
    except (TypeError, ValueError):
        return
    db = None
    try:
        from backend.database import SessionLocal

        db = SessionLocal()
        member = (db.query(em.PoolMember)
                  .filter(em.PoolMember.id == mid).first())
        if member is None:
            return
        _bump_pool_usage(db, mid, quota_hits=1)
        db.flush()
        maybe_trip_member_breaker(db, member)
        db.commit()
    except Exception:  # noqa: BLE001 — 记数失败静默，不挡播放
        logger.warning("池 quota 命中记数失败 member=%s",
                       member_id, exc_info=True)
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


def pool_wide_breaker_open(db: Session, cfg: dict | None = None) -> bool:
    """池级熔断判定：今日参与成员人均 quota_hits 达单盘阈值。

    participants = 今日有 PoolUsageDaily 行的成员数，total =
    今日 quota_hits 总和；total ≥ 阈值 × participants 即全池
    暂停建单。阈值 0（不熔断）或今日无参与成员时恒 False。
    """
    cfg = cfg or pool_config_payload(db)
    threshold = int(cfg["pool_breaker_quota_hits_per_day"])
    if threshold <= 0:
        return False
    from sqlalchemy import func

    participants, total = (
        db.query(func.count(em.PoolUsageDaily.id),
                 func.coalesce(func.sum(em.PoolUsageDaily.quota_hits), 0))
        .filter(em.PoolUsageDaily.day == datetime.now().date())
        .one())
    participants = int(participants or 0)
    if participants <= 0:
        return False
    return int(total or 0) >= threshold * participants


def _servable_pool_rows(db: Session, item_id):
    """条目当下「可下发」的池副本行 + 所属成员（design §6.3
    名额口径）：status=ready、stale_count=0、成员 active 且不在
    熔断冷却内——即 _pick_ready_pool_variant 会真挑中的行。
    熔断/暂停/stale 的行不算（否则一盘熔断会把条目锁死）。
    纯读、无副作用，返回 [(变体行, 成员行), ...]。
    """
    if item_id is None:
        return []
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item_id,
                    em.TransferVariant.owner_scope == "pool",
                    em.TransferVariant.status == "ready",
                    em.TransferVariant.stale_count == 0)
            .all())
    now = datetime.now()
    out = []
    for row in rows:
        try:
            member_id = int((row.owner_ref or "").strip())
        except (TypeError, ValueError):
            continue
        member = (db.query(em.PoolMember)
                  .filter(em.PoolMember.id == member_id).first())
        if member is None or member.status != "active":
            continue
        if (member.breaker_until is not None
                and member.breaker_until > now):
            continue
        out.append((row, member))
    return out


def _servable_pool_copy_count(db: Session, item_id) -> int:
    """条目可下发池副本数（pool_item_max_copies 的计数口径）。"""
    return len(_servable_pool_rows(db, item_id))


def _servable_pool_holder_ids(db: Session, item_id) -> set:
    """持有该条目可下发副本的成员 id 集合（PR-5 补位排除集：
    补位只许往还没有该副本的盘上打，不重复占同一盘）。"""
    return {int(member.id) for _, member in _servable_pool_rows(db, item_id)}


def _pool_inflight_for_item(db: Session, item_id) -> int:
    """条目在途池单数（pending + running）。"""
    if item_id is None:
        return 0
    return (db.query(em.TransferJob)
            .filter(em.TransferJob.item_id == item_id,
                    em.TransferJob.owner_scope == "pool",
                    em.TransferJob.status.in_(("pending", "running")))
            .count())


def breaker_recover_probe(db: Session, member, *, probe_fn=None,
                          now=None) -> str:
    """熔断半开试探（design §6.3，pool_janitor 冷却到期时调用）。

    冷却未到期 / 成员不在熔断暂停态 → "not_due"（不动任何
    状态）。到期：取该成员一份 ready 副本跑 quota 探测
    （probe_fn(variant) 可注入；默认走 transfer_variant
    .probe_user_copy_quota_decision，键 poolvar:<variant_id>
    与 serve 路同口径）；无 ready 副本可探时直接恢复（没有
    副本在下发，恢复后选盘自会再校验容量与绑定）。
    探测非 quota → 恢复 active（breaker_recovered 事件、回合
    清零），返回 "recovered"；仍 quota → 立即回熔断且冷却
    翻倍（breaker_retrip 事件 + breaker_until 顺延），返回
    "retripped"。全程经状态收口/流水落痕，不 commit。
    """
    now = now or datetime.now()
    if (member is None or member.status != "suspended"
            or member.suspended_reason != "breaker"
            or member.breaker_until is None
            or member.breaker_until > now):
        return "not_due"
    variant = (db.query(em.TransferVariant)
               .filter(em.TransferVariant.owner_scope == "pool",
                       em.TransferVariant.owner_ref == str(member.id),
                       em.TransferVariant.status == "ready")
               .order_by(em.TransferVariant.id.desc()).first())
    verdict = None
    if variant is not None:
        if probe_fn is not None:
            verdict = probe_fn(variant)
        else:
            verdict = _tv.probe_user_copy_quota_decision(
                f"poolvar:{variant.id}",
                (variant.variant_file_id or "").strip())
    if verdict == "quota":
        cfg = pool_config_payload(db)
        cooldown = _breaker_cooldown_sec(db, member, cfg)
        member.breaker_until = now + timedelta(seconds=cooldown)
        record_member_status_event(
            db, member, "suspended", "suspended", "breaker_retrip")
        _decisions.note_decision(
            scope="pool_breaker", resource_key=f"member:{member.id}",
            decision="retrip",
            reason=f"cooldown_sec={cooldown} probe=quota",
            candidates=[])
        return "retripped"
    member.suspended_reason = None
    member.breaker_until = None
    transition_member_status(db, member, "active",
                             reason="breaker_recovered")
    _decisions.note_decision(
        scope="pool_breaker", resource_key=f"member:{member.id}",
        decision="recover",
        reason=f"probe={verdict or 'no_variant'}", candidates=[])
    return "recovered"


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


def pick_pool_member(db: Session, item, *, force=False, quota_fn=None,
                     exclude_member_ids=None):
    """选盘（design §6.1）：返回 (member|None, candidates)。

    candidates：每位 active 成员一行 {member_id, eligible, score,
    reason, ...}（打分明细，供 note_decision 留痕）。合格者按分
    排序取 top-k（pool_pick_top_k，默认 3）再均匀随机挑一个——
    分高者中选概率高、又不把全部新片压到同一盘。24h 内已有本条
    目池单的成员不参与（force 越过，供门禁重触发）。无可用盘时
    记一笔 decision=none 的留痕；有可用盘时留痕记在建单成功处。
    exclude_member_ids（PR-5 补位用）：调用方明示排除的成员
    （如已持有该条目可下发副本的盘，补位不许再往同一盘打）。
    """
    cfg = pool_config_payload(db)
    excluded = {int(mid) for mid in (exclude_member_ids or ())}
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status == "active")
               .order_by(em.PoolMember.id.asc()).all())
    candidates = []
    scored = []
    for member in members:
        if int(member.id) in excluded:
            candidates.append({"member_id": int(member.id),
                               "eligible": False, "score": 0.0,
                               "reason": "excluded_by_caller"})
            continue
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
                    force=False, quota_fn=None, exclude_member_ids=None):
    """建池单（design §6.2）：选盘 → 限额 → 落行 → 留痕 → 唤醒。

    限额（任一命中抛 TransferError，不落行；错误优先级沿
    create_user_job：节流 → 源校验 → 绑定校验）：
    - 池总开关关 → pool_disabled；
    - 转存总闸关 → master_off；
    - provider 不在池启用列表 → pool_provider_unsupported；
    - Drive 条目取不到源盘 → no_drive；
    - 进行中池单数 ≥ pool_global_concurrency → throttled；
    - 池级熔断打开（PR-5）→ pool_breaker；
    - 条目可下发副本 + 在途池单 ≥ pool_item_max_copies
      （PR-5）→ copies_max；
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
    if pool_wide_breaker_open(db, cfg):
        raise _tv.TransferError(
            "pool_breaker",
            "公共池今天额度紧张，已保护性暂停接收新片，"
            "可以绑定自己的网盘立刻看，或稍后再试")
    max_copies = max(1, int(cfg["pool_item_max_copies"]))
    if (_servable_pool_copy_count(db, item.id)
            + _pool_inflight_for_item(db, item.id)) >= max_copies:
        raise _tv.TransferError(
            "copies_max",
            "这个片子的公共副本已达上限份数，"
            "可以绑定自己的网盘立刻看，或稍后再试")
    member, candidates = pick_pool_member(
        db, item, force=force, quota_fn=quota_fn,
        exclude_member_ids=exclude_member_ids)
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
                "job_id": inflight.id, "pool": True}
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
                                "job_id": failed.id, "pool": True}
                    return {"verdict": "failed", "reason": exc.kind,
                            "job_id": failed.id,
                            "detail": str(exc) or None, "pool": True}
                return {"verdict": "preparing",
                        "reason": "job_retriggered",
                        "job_id": new_job.id, "pool": True}
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
                "job_id": failed.id, "pool": True}
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
                            "job_id": done.id, "pool": True}
                return {"verdict": "failed", "reason": exc.kind,
                        "job_id": done.id,
                        "detail": str(exc) or None, "pool": True}
            return {"verdict": "preparing", "reason": "job_rebuilt",
                    "job_id": new_job.id, "pool": True}
        return {"verdict": "preparing", "reason": "throttled",
                "job_id": done.id, "pool": True}
    # ⑦ 建单。
    try:
        new_job = create_pool_job(db, item, quota_fn=quota_fn)
    except _tv.TransferError as exc:
        if exc.kind == "throttled":
            return {"verdict": "preparing", "reason": "throttled",
                    "pool": True}
        return {"verdict": "failed", "reason": exc.kind,
                "detail": str(exc) or None, "pool": True}
    return {"verdict": "preparing", "reason": "job_created",
            "job_id": new_job.id, "pool": True}


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
    内部另开会话）。事件只如实记录，不做防刷判定（PR-6 纯函数）。
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
        _bump_pool_usage(db, mid, play_starts=1)
        row = (db.query(em.TransferVariant)
               .filter(em.TransferVariant.id == vid).first())
        if row is not None:
            row.last_played_at = datetime.now()
        db.add(em.PoolPlayEvent(
            member_id=mid, variant_id=vid, item_id=iid,
            viewer_user_id=uid, created_at=datetime.now()))
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


def pool_member_access_token(db: Session, member_id) -> str:
    """成员绑定令牌（池播出回落/中继专用口）。

    成员行 → 绑定行 → 绑定级令牌口（drive_oauth
    .get_binding_drive_access_token，与 BYOS 中继同源）。只许取
    成员自己绑定的令牌：成员不存在、绑定缺失/非 active、令牌取
    不到一律回 ""，永不抛异常，绝不回落到 SA 令牌或观众令牌。
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


# ---------------------------------------------------------------- 成员生命周期（PR-4）
# design §2.4 状态机 + §6.4 下线连带 + §11.5 钩子 H2。
#
# 状态变更唯一收口是 transition_member_status：改 status /
# status_changed_at 并落 pool_member_status_events 流水一行。
# 本节所有流转（申请/批准/拒绝/暂停/恢复/清退/退出/绑定失效
# 连带）都经它，PR-5 熔断连带也必须经它——绕过收口直接写
# member.status 会让在线天数结算（PR-6）缺区间数据。
#
# 事务口径：收口与内部流转不 commit，公共服务函数在末尾统一
# commit；绑定钩子（on_binding_failed / on_binding_deleted）
# 骑调用方事务，不自提交。

#: 成员终态（不可再流转；exited/rejected 允许对同一绑定重新申请，
#: ejected 不许——清退是管理员决定，翻案走 clear/人工处置）。
POOL_MEMBER_TERMINAL_STATUSES = ("exited", "rejected", "ejected")

#: 退出/清退时同步删文件的上限（请求内 best-effort，防大池成员
#: 在退出请求里串行删几千个文件；超出的行照样标 released——
#: 文件残留在贡献者自己盘里、与站无关，见 design §7 风险 3）。
_EXIT_DELETE_LIMIT = 50


class PoolLifecycleError(Exception):
    """生命周期操作被拒：kind 是机器可读原因，message 是人话。

    kind：not_found / invalid_state / no_binding /
    binding_inactive / provider_not_enabled / needs_rebind /
    assign_required / ejected。
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def record_member_status_event(db: Session, member, from_status,
                               to_status, reason) -> em.PoolMemberStatusEvent:
    """落一笔 H2 状态流水（不 commit，随调用方事务）。"""
    event = em.PoolMemberStatusEvent(
        member_id=member.id, from_status=from_status,
        to_status=to_status, reason=reason)
    db.add(event)
    return event


def transition_member_status(db: Session, member, to_status: str, *,
                             reason: str):
    """状态变更唯一收口：改状态 + 变更时刻 + H2 流水（不 commit）。"""
    from_status = member.status
    if from_status == to_status:
        return member
    member.status = to_status
    member.status_changed_at = datetime.now()
    db.flush()
    record_member_status_event(db, member, from_status, to_status, reason)
    return member


def get_pool_member(db: Session, member_id) -> em.PoolMember:
    member = (db.query(em.PoolMember)
              .filter(em.PoolMember.id == member_id).first())
    if member is None:
        raise PoolLifecycleError("not_found", "池成员不存在。")
    return member


def _member_binding(db: Session, member) -> em.UserStorageBinding | None:
    if member is None or member.binding_id is None:
        return None
    return (db.query(em.UserStorageBinding)
            .filter(em.UserStorageBinding.id == member.binding_id)
            .first())


def _check_binding_pool_ready(db: Session, binding) -> None:
    """入池/恢复时对绑定行的硬查（design §2.5）：provider 在配置
    名单内、drive 含 drive.file 范围。不合格抛 PoolLifecycleError。"""
    cfg = pool_config_payload(db)
    if binding.provider not in _enabled_providers(cfg):
        raise PoolLifecycleError(
            "provider_not_enabled",
            "这个盘的类型不在公共池准入名单内。")
    if binding.provider == "drive":
        from backend.emby_server import drive_oauth as _do

        if _do.binding_needs_rebind(binding.granted_scopes):
            raise PoolLifecycleError(
                "needs_rebind",
                "这个盘的授权范围不足（缺写入范围），"
                "请先在「我的网盘」里对它重新授权，再申请加入。")


def _require_binding_active(db: Session, member) -> em.UserStorageBinding:
    binding = _member_binding(db, member)
    if binding is None:
        raise PoolLifecycleError(
            "no_binding", "该成员尚未绑定盘，无法执行此操作。")
    if binding.status != "active":
        raise PoolLifecycleError(
            "binding_inactive",
            "成员绑定的盘已失效，先恢复绑定再操作。")
    return binding


def _join_target_status(db: Session, *, has_candidate: bool) -> str:
    """按 pool_join_mode 定申请落态（design §3/§8.3）。

    apply_review → pending（等管理员审）；open → active；
    admin_assign → 只有已有候选资格行（管理员已授予）才 active，
    没有则拒绝自助申请。
    """
    mode = pool_config_payload(db)["pool_join_mode"]
    if mode == "open":
        return "active"
    if mode == "admin_assign":
        if has_candidate:
            return "active"
        raise PoolLifecycleError(
            "assign_required",
            "公共池由管理员指定成员，请联系管理员授予资格后再绑定申请。")
    return "pending"


def _land_application(db: Session, member, *, reason="apply"):
    """申请落态：按 join_mode 定目标状态并经收口流转（末尾 commit）。"""
    target = _join_target_status(
        db, has_candidate=(member.status == "candidate"))
    if target == "active" and member.joined_at is None:
        member.joined_at = datetime.now()
    transition_member_status(db, member, target, reason=reason)
    db.commit()
    db.refresh(member)
    return member


def apply_pool_membership(db: Session, user_id: int, binding_id: int):
    """贡献者对自己某个绑定申请入池（用户端 apply 口）。

    幂等：该绑定已有非终态成员行直接返回；终态行（exited /
    rejected）重新申请走同一行流转回 pending/active；ejected
    行拒绝再申请。候选行（管理员预批、binding_id 为空）优先
    挂接本绑定。校验：绑定属本人、active、provider 在名单、
    范围满足（§2.5）。
    """
    binding = (db.query(em.UserStorageBinding)
               .filter(em.UserStorageBinding.id == binding_id,
                       em.UserStorageBinding.user_id == user_id)
               .first())
    if binding is None:
        raise PoolLifecycleError(
            "not_found", "绑定不存在或不属于你。")
    if binding.status != "active":
        raise PoolLifecycleError(
            "binding_inactive",
            "这个盘当前不可用（绑定已失效），先恢复绑定再申请。")
    _check_binding_pool_ready(db, binding)
    existing = (db.query(em.PoolMember)
                .filter(em.PoolMember.binding_id == binding.id)
                .order_by(em.PoolMember.id.asc()).first())
    if existing is not None:
        if existing.status == "ejected":
            raise PoolLifecycleError(
                "ejected", "该盘已被管理员清退，无法再次申请。")
        if existing.status not in POOL_MEMBER_TERMINAL_STATUSES:
            return existing
        return _land_application(db, existing)
    candidate = (db.query(em.PoolMember)
                 .filter(em.PoolMember.user_id == user_id,
                         em.PoolMember.status == "candidate",
                         em.PoolMember.binding_id.is_(None))
                 .order_by(em.PoolMember.id.asc()).first())
    if candidate is not None:
        candidate.binding_id = binding.id
        candidate.provider = binding.provider
        db.flush()
        return _land_application(db, candidate)
    # 无候选行：admin_assign 模式在此拒绝（有候选行才轮到建行）
    target = _join_target_status(db, has_candidate=False)
    now = datetime.now()
    row = em.PoolMember(
        binding_id=binding.id, user_id=user_id,
        provider=binding.provider, status=target,
        joined_at=now if target == "active" else None,
        status_changed_at=now)
    db.add(row)
    db.flush()
    record_member_status_event(db, row, None, target, "apply")
    db.commit()
    db.refresh(row)
    return row


def approve_pool_member(db: Session, member_id: int, *, actor_id=None):
    """待审 → active（管理员批准）。批准前复查绑定仍合格。"""
    member = get_pool_member(db, member_id)
    if member.status != "pending":
        raise PoolLifecycleError(
            "invalid_state",
            f"成员当前是「{member.status}」，只有待审状态可以批准。")
    binding = _require_binding_active(db, member)
    _check_binding_pool_ready(db, binding)
    if member.joined_at is None:
        member.joined_at = datetime.now()
    transition_member_status(db, member, "active", reason="approve")
    db.commit()
    db.refresh(member)
    return member


def reject_pool_member(db: Session, member_id: int, *, actor_id=None):
    """候选/待审 → rejected（管理员拒绝）。"""
    member = get_pool_member(db, member_id)
    if member.status not in ("candidate", "pending"):
        raise PoolLifecycleError(
            "invalid_state",
            f"成员当前是「{member.status}」，只有候选/待审状态可以拒绝。")
    transition_member_status(db, member, "rejected", reason="reject")
    db.commit()
    db.refresh(member)
    return member


def suspend_pool_member(db: Session, member_id: int, *, actor_id=None,
                        reason: str = "manual"):
    """active → suspended（管理员暂停；熔断连带走 on_* 钩子/PR-5）。"""
    member = get_pool_member(db, member_id)
    if member.status != "active":
        raise PoolLifecycleError(
            "invalid_state",
            f"成员当前是「{member.status}」，只有在池成员可以暂停。")
    member.suspended_reason = reason
    member.breaker_until = None
    transition_member_status(db, member, "suspended", reason=reason)
    db.commit()
    db.refresh(member)
    return member


def resume_pool_member(db: Session, member_id: int, *, actor_id=None):
    """suspended → active（管理员恢复）。绑定已死/范围不足时拒绝，
    防把死盘放回选盘候选集。"""
    member = get_pool_member(db, member_id)
    if member.status != "suspended":
        raise PoolLifecycleError(
            "invalid_state",
            f"成员当前是「{member.status}」，只有暂停状态可以恢复。")
    binding = _require_binding_active(db, member)
    _check_binding_pool_ready(db, binding)
    member.suspended_reason = None
    member.breaker_until = None
    transition_member_status(db, member, "active", reason="resume")
    db.commit()
    db.refresh(member)
    return member


def release_member_copies(db: Session, member, *,
                          client_factory=None) -> dict:
    """成员下线时的副本处置（design §6.4）：ready/orphan 池副本
    先用成员令牌 best-effort 删盘内文件（失败只计数不阻塞），
    行一律标 released。不 commit（随调用方事务）。

    client_factory(db, binding) 可注入（测试替身）；默认按成员
    绑定建 UserDriveApiClient（与池单执行同一客户端，令牌只用
    成员自己的，绝不回落 SA）。绑定已死则不尝试删除——文件随
    账号生死，行标 released 收尾（§7 风险 3）。
    """
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.owner_scope == "pool",
                    em.TransferVariant.owner_ref == str(member.id),
                    em.TransferVariant.status.in_(("ready", "orphan")))
            .order_by(em.TransferVariant.id.asc())
            .all())
    stats = {"released": 0, "delete_failed": 0, "delete_skipped": 0}
    if not rows:
        return stats
    binding = _member_binding(db, member)
    client = None
    if binding is not None and binding.status == "active":
        try:
            if client_factory is not None:
                client = client_factory(db, binding)
            else:
                client = _tv.UserDriveApiClient(db, binding)
        except Exception:  # noqa: BLE001 — 客户端建不出来按无令牌处理
            logger.warning("池成员副本清理客户端构建失败 member=%s",
                           member.id, exc_info=True)
            client = None
    marker = f"myDrive:{member.user_id}"
    for idx, row in enumerate(rows):
        fid = (row.variant_file_id or "").strip()
        if client is not None and fid and idx < _EXIT_DELETE_LIMIT:
            try:
                client.delete_file_ignore_missing(marker, fid)
            except Exception:  # noqa: BLE001 — 删不掉不阻塞下线
                logger.warning("池副本删文件失败 member=%s fid=%s",
                               member.id, fid, exc_info=True)
                stats["delete_failed"] += 1
        elif client is not None and fid:
            stats["delete_skipped"] += 1
        row.status = "released"
        stats["released"] += 1
    db.flush()
    return stats


def _exit_member(db: Session, member, *, reason: str,
                 client_factory=None) -> dict:
    """退出/连带下线的内部实现（不 commit）：流转 + 副本处置。"""
    transition_member_status(db, member, "exited", reason=reason)
    member.suspended_reason = reason
    stats = release_member_copies(
        db, member, client_factory=client_factory)
    stats["member_id"] = member.id
    return stats


def exit_pool_membership(db: Session, member_id: int, *,
                         reason: str = "contributor_exit",
                         client_factory=None) -> dict:
    """成员退出（贡献者主动退出/解绑连带共用）：→ exited + 副本
    处置。返回处置统计（含 member_id/released/delete_failed）。"""
    member = get_pool_member(db, member_id)
    if member.status in POOL_MEMBER_TERMINAL_STATUSES:
        raise PoolLifecycleError(
            "invalid_state",
            f"成员已是终态「{member.status}」，不能再退出。")
    stats = _exit_member(db, member, reason=reason,
                         client_factory=client_factory)
    db.commit()
    return stats


def eject_pool_member(db: Session, member_id: int, *, actor_id=None,
                      violation: bool = False,
                      violation_reason: str | None = None,
                      client_factory=None):
    """管理员清退：→ ejected + 副本处置（同退出清理）。

    violation=True 时置违规标志（谁置位/时间/原因落
    pool_members.violation_*），PR-6 结算据此对该期不结
    （design §11.2 第 4 条）；普通清退不置位，历史贡献照常结。
    """
    member = get_pool_member(db, member_id)
    if member.status in POOL_MEMBER_TERMINAL_STATUSES:
        raise PoolLifecycleError(
            "invalid_state",
            f"成员已是终态「{member.status}」，不能再清退。")
    if violation:
        member.violation_flagged = True
        member.violation_reason = (violation_reason or "").strip() or None
        member.violation_flagged_at = datetime.now()
        member.violation_flagged_by = actor_id
        member.violation_cleared_at = None
        member.violation_cleared_by = None
    member.suspended_reason = "admin_eject"
    transition_member_status(db, member, "ejected", reason="admin_eject")
    release_member_copies(db, member, client_factory=client_factory)
    db.commit()
    db.refresh(member)
    return member


def clear_member_violation(db: Session, member_id: int, *,
                           actor_id=None):
    """清除违规标志（管理员翻案）：落 cleared_by/at，置位痕迹
    （flagged_by/at/reason）保留备查，成员状态不变。"""
    member = get_pool_member(db, member_id)
    if not member.violation_flagged:
        raise PoolLifecycleError(
            "invalid_state", "该成员没有可清除的违规标志。")
    member.violation_flagged = False
    member.violation_cleared_at = datetime.now()
    member.violation_cleared_by = actor_id
    db.commit()
    db.refresh(member)
    return member


def member_violation_ejected(db: Session, member_id: int) -> bool:
    """PR-6 结算判定依据：该成员是否「违规清退」（ejected +
    违规标志在位）。标志被清除后按普通清退处理（可结）。"""
    member = (db.query(em.PoolMember)
              .filter(em.PoolMember.id == member_id).first())
    return bool(member is not None and member.status == "ejected"
                and member.violation_flagged)


def release_pool_copy(db: Session, variant_id: int, *,
                      client_factory=None):
    """管理员手动下线一份池副本（design §8.1）：标 stale 停发 +
    best-effort 删盘内文件（删除失败不阻塞标态，巡检再收尾）。"""
    row = (db.query(em.TransferVariant)
           .filter(em.TransferVariant.id == variant_id).first())
    if row is None or row.owner_scope != "pool":
        raise PoolLifecycleError("not_found", "池副本不存在。")
    member = (db.query(em.PoolMember)
              .filter(em.PoolMember.id == int(row.owner_ref or 0))
              .first()) if (row.owner_ref or "").isdigit() else None
    if member is not None:
        binding = _member_binding(db, member)
        fid = (row.variant_file_id or "").strip()
        if binding is not None and binding.status == "active" and fid:
            try:
                client = (client_factory(db, binding)
                          if client_factory is not None
                          else _tv.UserDriveApiClient(db, binding))
                client.delete_file_ignore_missing(
                    f"myDrive:{member.user_id}", fid)
            except Exception:  # noqa: BLE001 — 删不掉只记日志
                logger.warning("池副本手动下线删文件失败 variant=%s",
                               row.id, exc_info=True)
    row.status = "stale"
    db.commit()
    db.refresh(row)
    return row


# ------------------------------------------------------------ 绑定连带钩子
# 调用点只有两处（user_bindings._mark_failed 落失效态、
# user_bindings.delete_binding 删行前段），都以 try/except 包住
# 本节函数——连带失败绝不阻塞绑定主流程。两钩子均不 commit，
# 骑调用方事务（_mark_failed 由其调用方提交；delete_binding
# 末尾统一提交）。


def on_binding_failed(db: Session, binding) -> None:
    """绑定失效连带（design §2.4）：该绑定的 active/pending 成员
    自动 suspended(binding_invalid)；恢复走管理员 resume（绑定
    恢复 active 后由管理员确认，不自动回 active——同 §2.4 口径）。"""
    if binding is None or binding.id is None:
        return
    member = (db.query(em.PoolMember)
              .filter(em.PoolMember.binding_id == binding.id,
                      em.PoolMember.status.in_(("active", "pending")))
              .order_by(em.PoolMember.id.asc()).first())
    if member is None:
        return
    member.suspended_reason = "binding_invalid"
    member.breaker_until = None
    transition_member_status(db, member, "suspended",
                             reason="binding_invalid")
    db.flush()


def on_binding_deleted(db: Session, binding) -> None:
    """解绑连带（design §6.4）：删行前段、令牌尚有效时触发——
    该绑定的非终态成员按贡献者退出同链处置（→ exited + 副本
    删除/标 released）。候选行（无绑定）不受影响。"""
    if binding is None or binding.id is None:
        return
    member = (db.query(em.PoolMember)
              .filter(em.PoolMember.binding_id == binding.id,
                      ~em.PoolMember.status.in_(
                          POOL_MEMBER_TERMINAL_STATUSES))
              .order_by(em.PoolMember.id.asc()).first())
    if member is None:
        return
    _exit_member(db, member, reason="contributor_exit")
    db.flush()


# ------------------------------------------------------------ 用户端贡献状态


def _member_stats(db: Session, member) -> dict:
    """成员聚合小账（用户端「我的贡献」与管理端列表共用口径）：
    副本数/占用按变体行现算，今日量按 pool_usage_daily 当日行。"""
    from sqlalchemy import func

    copies, copies_bytes = (
        db.query(func.count(em.TransferVariant.id),
                 func.coalesce(func.sum(em.TransferVariant.size_bytes), 0))
        .filter(em.TransferVariant.owner_scope == "pool",
                em.TransferVariant.owner_ref == str(member.id),
                em.TransferVariant.status.in_(("ready", "stale", "orphan")))
        .one())
    from datetime import date as _date

    usage = (db.query(em.PoolUsageDaily)
             .filter(em.PoolUsageDaily.member_id == member.id,
                     em.PoolUsageDaily.day == _date.today())
             .first())
    return {
        "copies_count": int(copies or 0),
        "copies_bytes": int(copies_bytes or 0),
        "today_play_starts": int(usage.play_starts) if usage else 0,
        "today_transfer_bytes": int(usage.transfer_bytes) if usage else 0,
        "today_transfer_jobs": int(usage.transfer_jobs) if usage else 0,
        "today_quota_hits": int(usage.quota_hits) if usage else 0,
    }


def member_public_payload(db: Session, member) -> dict:
    """成员行对用户端的安全序列化（绝不含凭据/令牌材料）。"""
    payload = {
        "id": member.id,
        "binding_id": member.binding_id,
        "provider": member.provider,
        "status": member.status,
        "suspended_reason": member.suspended_reason,
        "violation_flagged": bool(member.violation_flagged),
        "capacity_cap_bytes": member.capacity_cap_bytes,
        "joined_at": (member.joined_at.isoformat()
                      if member.joined_at else None),
        "status_changed_at": (member.status_changed_at.isoformat()
                              if member.status_changed_at else None),
    }
    payload.update(_member_stats(db, member))
    return payload


def _apply_block_reason(db: Session, binding, member, candidate) -> str | None:
    """用户端 can_apply=False 时的人话原因；可申请返回 None。"""
    if member is not None and member.status not in (
            POOL_MEMBER_TERMINAL_STATUSES):
        return "这个盘已在公共池流程中"
    if member is not None and member.status == "ejected":
        return "该盘已被管理员清退"
    if binding.status != "active":
        return "绑定已失效，先恢复绑定"
    cfg = pool_config_payload(db)
    if binding.provider not in _enabled_providers(cfg):
        return "这个盘的类型不在公共池准入名单内"
    if binding.provider == "drive":
        from backend.emby_server import drive_oauth as _do

        if _do.binding_needs_rebind(binding.granted_scopes):
            return "授权范围不足，请先对这个盘重新授权"
    if (cfg["pool_join_mode"] == "admin_assign" and candidate is None
            and member is None):
        return "公共池由管理员指定成员，请联系管理员授予资格"
    return None


def contribution_status(db: Session, user) -> dict:
    """GET /pool/contribution 的数据面（design §8.3）：我的每个
    绑定一行（成员状态 + 能否申请 + 不能申请的人话原因），外加
    候选资格行与池配置摘要。无绑定/未加入都出明确状态不报错。"""
    cfg = pool_config_payload(db)
    bindings = (db.query(em.UserStorageBinding)
                .filter(em.UserStorageBinding.user_id == user.id)
                .order_by(em.UserStorageBinding.id.asc())
                .all())
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.user_id == user.id)
               .order_by(em.PoolMember.id.asc())
               .all())
    by_binding = {m.binding_id: m for m in members
                  if m.binding_id is not None}
    candidate = next(
        (m for m in members
         if m.binding_id is None and m.status == "candidate"), None)
    entries = []
    for binding in bindings:
        member = by_binding.get(binding.id)
        block = _apply_block_reason(db, binding, member, candidate)
        entries.append({
            "binding_id": binding.id,
            "account_label": binding.account_label,
            "provider": binding.provider,
            "binding_status": binding.status,
            "member": (member_public_payload(db, member)
                       if member is not None else None),
            "can_apply": block is None,
            "apply_block_reason": block,
        })
    return {
        "pool_enabled": bool(cfg["pool_enabled"]),
        "join_mode": cfg["pool_join_mode"],
        "candidate": (member_public_payload(db, candidate)
                      if candidate is not None else None),
        "bindings": entries,
    }

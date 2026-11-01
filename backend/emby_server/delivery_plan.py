# -*- coding: utf-8 -*-
"""播放交付编排器 v1（换代线 step 5，2026-11-01，蓝图 v2 附二）。

形状（可行性报告 §七）：把「这个用户播这个条目，第一口字节
应该走哪条路」从点播热路径里的即时步行，改成**平时预计算、
热路径只读**的交付计划：

- 计划＝(用户, 条目) → 有序腿清单，落 ``playback_delivery_plans``
  一行：本人 ready 副本直出 → 本人副本制备中（带交付进度）→
  挂载直读 → 上游 302 → 现行兜底序（legacy 终局腿恒在）。
- 腿的可用性由探针在**平时**算（PlaybackInfo 顺手刷新、用户
  转存单完成时刷新）：副本状态走 transfer_variant 的同一查询
  口径、制备进度读 transfer_deliveries 落盘、挂载腿问
  mount_serving.resolve_local_file、上游腿问
  upstream_item_mappings.get_best_mapping。每个探针独立
  fail-open：探针炸了只是这条腿缺席，绝不打挂建计划。
- 点播热路径（video_stream）只读未过期计划：顶腿是本人
  ready 副本时，按 PK 复核变体行（status/归属/条目全对）才
  采用其 fid；其余一切情形（无计划/过期/开关关/复核不过）
  逐字回现行 ``resolve_play_file_id`` 选路。计划不是新的选
  路真相源，只是把现行第一优先级的结论提前算好。
- 门禁（绑定门禁、首播转存门禁、play_sign）一律不在本模块
  范围内：计划只影响「选哪个 fid」，不影响「准不准播」。

开关四齐（config_schema 的 delivery_plan 家族校验＋本模块
读取/payload/write＋管理端 /delivery-plan-config 端点）：
``delivery_plan_enabled`` 默认开，关 = 不建计划、热路径不读
计划；``delivery_plan_ttl_sec`` 默认 120 秒（30–3600）。纯
SystemConfig 键，不进 env.example（#1527 先例）。

可观测：建计划打结构化日志 ``delivery_plan outcome=built
legs=...``，热路径命中打 ``outcome=hit``、复核不过打
``outcome=fallback``（miss/stale 只 debug，避免热路径刷屏）；
计划本体在 playback_delivery_plans 表与管理端配置端点可查。
"""
import json
import logging
from datetime import datetime, timedelta

from backend.emby_server import models as em

logger = logging.getLogger(__name__)

KEY_DELIVERY_PLAN_ENABLED = "delivery_plan_enabled"
KEY_DELIVERY_PLAN_TTL_SEC = "delivery_plan_ttl_sec"

DELIVERY_PLAN_TTL_DEFAULT = 120
DELIVERY_PLAN_TTL_MIN = 30
DELIVERY_PLAN_TTL_MAX = 3600

DESCRIPTIONS = {
    KEY_DELIVERY_PLAN_ENABLED: (
        "播放交付编排器总开关（默认开；关=不预计算计划、点播热路径不读计划）"),
    KEY_DELIVERY_PLAN_TTL_SEC: (
        "交付计划有效期（秒，30–3600，默认 120）：过期计划热路径当不存在"),
}

_FALSE_WORDS = ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# 配置（SystemConfig 热读，fail-safe 回默认）
# ---------------------------------------------------------------------------

def _raw_config(db, key):
    if db is None:
        return ""
    try:
        from backend.integrations import store
        return str(store.get_value(db, key, "") or "")
    except Exception:  # noqa: BLE001 — 配置读失败回默认，不打挂播放
        return ""


def delivery_plan_enabled(db=None):
    """编排器总开关：未配行默认开；0/false/no/off 视为关。"""
    raw = _raw_config(db, KEY_DELIVERY_PLAN_ENABLED).strip().lower()
    if not raw:
        return True
    return raw not in _FALSE_WORDS


def plan_ttl_sec(db=None):
    """计划 TTL（秒）：未配/非法回默认 120，钳制 [30, 3600]。"""
    raw = _raw_config(db, KEY_DELIVERY_PLAN_TTL_SEC).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DELIVERY_PLAN_TTL_DEFAULT
    return max(DELIVERY_PLAN_TTL_MIN,
               min(DELIVERY_PLAN_TTL_MAX, value))


def config_payload(db=None):
    """管理后台读取：当前值 + 默认值。"""
    return {
        "enabled": delivery_plan_enabled(db),
        "ttl_sec": plan_ttl_sec(db),
        "defaults": {"enabled": True, "ttl_sec": DELIVERY_PLAN_TTL_DEFAULT},
    }


def write_config(db, *, enabled: bool, ttl_sec: int) -> dict:
    """管理后台写入：走 config_service 的 delivery_plan 家族校验
    （TTL 越界抛 ValueError，端点转 400），保存即生效。"""
    from backend import config_service
    from backend.integrations import store

    values = {
        KEY_DELIVERY_PLAN_ENABLED: "true" if enabled else "false",
        KEY_DELIVERY_PLAN_TTL_SEC: str(int(ttl_sec)),
    }
    config_service.save_config(db, values, schema="delivery_plan",
                               descriptions=DESCRIPTIONS)
    store.invalidate(KEY_DELIVERY_PLAN_ENABLED, KEY_DELIVERY_PLAN_TTL_SEC)
    db.commit()
    return config_payload(db)


# ---------------------------------------------------------------------------
# 探针（每个独立 fail-open：炸了只是这条腿缺席）
# ---------------------------------------------------------------------------

def _probe_user_copy_ready(db, user_id, item):
    """本人 ready 副本腿：与 transfer_variant._newest_ready 的
    user 分支同口径（item + owner_scope=user + owner_ref 本人
    + status=ready，id 倒序取 1）。绝不把别人的副本写进计划。"""
    row = (db.query(em.TransferVariant)
           .filter(em.TransferVariant.item_id == item.id,
                   em.TransferVariant.owner_scope == "user",
                   em.TransferVariant.owner_ref == str(user_id),
                   em.TransferVariant.status == "ready")
           .order_by(em.TransferVariant.id.desc())
           .first())
    if row is None:
        return None
    return {"leg": "user_copy_ready", "state": "ready",
            "variant_id": row.id,
            "variant_drive_id": row.variant_drive_id or ""}


def _probe_user_copy_preparing(db, user_id, item):
    """本人副本制备中腿：本人在该条目上有未终态转存单时成立，
    进度取该单的 transfer_deliveries 落盘已提交偏移（无落盘
    行时只报在途、不编造进度）。"""
    job = (db.query(em.TransferJob)
           .filter(em.TransferJob.item_id == item.id,
                   em.TransferJob.owner_scope == "user",
                   em.TransferJob.owner_ref == str(user_id),
                   em.TransferJob.status.in_(("pending", "running")))
           .order_by(em.TransferJob.id.desc())
           .first())
    if job is None:
        return None
    leg = {"leg": "user_copy_preparing", "state": "preparing",
           "job_id": job.id, "job_status": job.status}
    delivery = (db.query(em.TransferDelivery)
                .filter_by(job_id=job.id).one_or_none())
    if delivery is not None and int(delivery.size or 0) > 0:
        committed = int(delivery.committed_offset or 0)
        leg["progress_pct"] = max(
            0, min(100, committed * 100 // int(delivery.size)))
    return leg


def _probe_mount_direct(db, item):
    """挂载直读腿：只记可用性，不把本地绝对路径写进计划。"""
    from backend.emby_server import mount_serving
    path = mount_serving.resolve_local_file(db, item)
    if not path:
        return None
    return {"leg": "mount_direct", "state": "ready"}


def _probe_upstream(db, item):
    """上游腿：有最佳映射即成立（302 目标在消费时现算）。"""
    from backend.emby_server import upstream_item_mappings
    mapping = upstream_item_mappings.get_best_mapping(db, item.id)
    if mapping is None:
        return None
    return {"leg": "upstream", "state": "ready",
            "mapping_id": getattr(mapping, "id", None)}


_PROBES = (
    ("user_copy_ready", _probe_user_copy_ready, True),
    ("user_copy_preparing", _probe_user_copy_preparing, True),
    ("mount_direct", _probe_mount_direct, False),
    ("upstream", _probe_upstream, False),
)


# ---------------------------------------------------------------------------
# 计划的建与读
# ---------------------------------------------------------------------------

def build_plan(db, user_id, item):
    """为 (用户, 条目) 算一份新计划并落库（同键原地更新一行）。

    开关关/用户或条目缺失回 None、不落行。探针全 fail-open。
    user_copy_ready 与 user_copy_preparing 互斥：副本已 ready
    时制备中腿没有意义（同一副本的成熟态优先）。
    """
    if db is None or user_id is None or item is None:
        return None
    if not delivery_plan_enabled(db):
        return None
    legs = []
    ready_leg = None
    for name, probe, needs_user in _PROBES:
        if name == "user_copy_preparing" and ready_leg is not None:
            continue
        try:
            leg = (probe(db, user_id, item) if needs_user
                   else probe(db, item))
        except Exception:  # noqa: BLE001 — 探针失败只是腿缺席
            logger.warning("delivery_plan probe failed leg=%s "
                           "user=%s item=%s", name, user_id,
                           getattr(item, "id", None), exc_info=True)
            leg = None
        if leg is not None:
            legs.append(leg)
            if name == "user_copy_ready":
                ready_leg = leg
    legs.append({"leg": "legacy", "state": "fallback"})
    now = datetime.now()
    expires = now + timedelta(seconds=plan_ttl_sec(db))
    plan = {"user_id": int(user_id), "item_id": int(item.id),
            "legs": legs,
            "computed_at": now.isoformat(timespec="seconds"),
            "expires_at": expires.isoformat(timespec="seconds")}
    try:
        row = (db.query(em.PlaybackDeliveryPlan)
               .filter_by(user_id=int(user_id), item_id=int(item.id))
               .one_or_none())
        if row is None:
            row = em.PlaybackDeliveryPlan(user_id=int(user_id),
                                          item_id=int(item.id))
            db.add(row)
        row.plan_json = json.dumps(plan, ensure_ascii=False)
        row.computed_at = now
        row.expires_at = expires
        db.commit()
    except Exception:  # noqa: BLE001 — 落库失败不打挂调用方
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("delivery_plan persist failed user=%s item=%s",
                       user_id, getattr(item, "id", None), exc_info=True)
        return None
    logger.info("delivery_plan outcome=built user=%s item=%s legs=%s",
                user_id, item.id,
                ",".join(leg["leg"] for leg in legs))
    return plan


def get_fresh_plan(db, user_id, item_id):
    """读未过期计划（解析后的 dict）；无行/过期/开关关回 None。"""
    if db is None or user_id is None or item_id is None:
        return None
    if not delivery_plan_enabled(db):
        return None
    try:
        row = (db.query(em.PlaybackDeliveryPlan)
               .filter_by(user_id=int(user_id), item_id=int(item_id))
               .one_or_none())
    except Exception:  # noqa: BLE001 — 读计划失败当无计划
        try:
            db.rollback()
        except Exception:
            pass
        return None
    if row is None or row.expires_at is None \
            or row.expires_at <= datetime.now():
        return None
    try:
        plan = json.loads(row.plan_json or "")
    except Exception:
        return None
    return plan if isinstance(plan, dict) else None


def ensure_plan(db, user, item):
    """PlaybackInfo 触发口：有新鲜计划直接回，没有就现算一份。

    user 是 WebUser 对象（取 .id）；全程 fail-open 语义在
    build_plan/get_fresh_plan 内，调用方仍应 try 包裹。
    """
    user_id = getattr(user, "id", None)
    if user_id is None or item is None:
        return None
    fresh = get_fresh_plan(db, user_id, item.id)
    if fresh is not None:
        return fresh
    return build_plan(db, user_id, item)


def refresh_plan_for_job(db, job):
    """转存单完成事件触发口：为该单的 (用户, 条目) 重算计划。

    只处理 user 归属单；条目已删/用户不可解析时静默跳过。
    """
    if db is None or job is None:
        return None
    if (getattr(job, "owner_scope", "") or "") != "user":
        return None
    try:
        user_id = int(getattr(job, "owner_ref", "") or "")
    except (TypeError, ValueError):
        return None
    item = (db.query(em.MediaItem).filter_by(id=job.item_id)
            .one_or_none() if job.item_id else None)
    if item is None:
        return None
    return build_plan(db, user_id, item)


# ---------------------------------------------------------------------------
# 热路径消费（只读计划；采用前复核，绝不信任计划本身）
# ---------------------------------------------------------------------------

def plan_ready_leg(db, item, user):
    """video_stream 消费口：计划顶腿是本人 ready 副本时返回
    (variant_file_id, variant_row)，其余一切情形回 None——调用
    方据此逐字走现行 resolve_play_file_id。

    复核（任一不过即回落并打 outcome=fallback 日志）：计划新
    鲜、顶腿为 user_copy_ready、变体行按 PK 存在且 status=
    ready、owner_scope/owner_ref 与当前用户逐字对、item 对得
    上、fid 非空；另与现行选路同闸：转存总闸（master_enabled）
    关时不采用。
    """
    user_id = getattr(user, "id", None)
    if db is None or item is None or user_id is None:
        return None
    if not delivery_plan_enabled(db):
        return None
    try:
        from backend.emby_server import transfer_variant as _tv
        if not _tv.master_enabled():
            return None
    except Exception:  # noqa: BLE001 — 总闸读不到按不采用处理
        return None
    plan = get_fresh_plan(db, user_id, item.id)
    if plan is None:
        logger.debug("delivery_plan outcome=miss user=%s item=%s",
                     user_id, item.id)
        return None
    legs = plan.get("legs") or []
    top = legs[0] if legs else {}
    if top.get("leg") != "user_copy_ready":
        logger.debug("delivery_plan outcome=miss user=%s item=%s "
                     "top=%s", user_id, item.id, top.get("leg"))
        return None
    variant_id = top.get("variant_id")
    row = None
    if variant_id is not None:
        try:
            row = (db.query(em.TransferVariant)
                   .filter_by(id=int(variant_id)).one_or_none())
        except Exception:  # noqa: BLE001 — 复核查询失败按回落
            row = None
    ok = (row is not None
          and row.status == "ready"
          and row.owner_scope == "user"
          and row.owner_ref == str(user_id)
          and int(row.item_id) == int(item.id)
          and bool((row.variant_file_id or "").strip()))
    if not ok:
        logger.info("delivery_plan outcome=fallback user=%s item=%s "
                    "reason=variant_recheck_failed variant_id=%s",
                    user_id, item.id, variant_id)
        return None
    logger.info("delivery_plan outcome=hit user=%s item=%s "
                "variant_id=%s", user_id, item.id, row.id)
    return row.variant_file_id, row

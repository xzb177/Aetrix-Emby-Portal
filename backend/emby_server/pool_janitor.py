# -*- coding: utf-8 -*-
"""公共贡献池巡检 janitor（goal-33 PR-5 保护收尾）。

定位：池的周期性后台收尾工，挂在 worker.py 既有后台线程框架
里（与 transfer_sediment 同进程同形态），不起新进程。判定本体
都在 pool_gate（熔断/选盘/建单/状态收口），本模块只做周期
调度与批量执行，不另立判定口径。每轮按固定顺序跑七步：

1. 熔断触发扫：active 成员当日 quota_hits 达阈值 → 熔断
   （pool_gate.maybe_trip_member_breaker；copy/serve 记数点已
   当场查过，这里是兜底扫，防记数点之外的累积无人收口）；
2. 熔断半开：冷却到期的 suspended(breaker) 成员跑试探探测
   （pool_gate.breaker_recover_probe），通了恢复、再命中翻倍
   冷却回熔断；
3. 绑定失效兜底：成员仍 active/pending 但绑定行已非 active
   → 走 PR-4 的 on_binding_failed 同口径落 suspended；
   binding_invalid 暂停成员名下的 ready 副本一律标 stale
   停发（补充节 S4：失效盘副本立即下线）；
4. 令牌探活：逐个 active 成员取一次令牌（刷新失败在
   drive_oauth 内自落绑定状态），落态后成员没跟上的用
   on_binding_failed 补齐；
5. 死副本对账：ready 池副本用成员令牌验活（成员盘文件 SA
   看不见，故 transfer_variant.patrol_once 对池行整行跳过、
   归本步）——条目没了标 orphan、源 fid 对不上标 stale、
   文件 404/403 记 strike（满 2 次标 stale，与 patrol 同
   口径 STALE_STRIKES）；成员当场点播侧的死副本对账
   （GA-032）是热路径自愈，本步是冷路径全量收尾；
6. 容量淘汰（design §6.5，仅池开时）：成员盘用量超上限或
   剩余低于保底 → 按 orphan → stale → 闲置冷副本（热度升
   序）的顺序淘汰，删文件后标 released、删失败标 orphan
   等下轮；淘汰到上限 95% 滞后带即停，防反复横跳。闲置主
   动清理是本步的独立分支（pool_idle_evict_proactive_
   enabled 开时，仅池开时）：不等容量触发，闲置超
   pool_copy_idle_evict_days 天的 ready 副本直接走同一删
   除路径清掉（同每成员每轮上限、同保护口径），盘不紧张
   时冷副本也不会无限躺。退出/清退成员的残留文件不在本
   步（见第 7 步）；
7. 退出残留收尾：exited/ejected 成员名下 status=released
   但文件可能残留的行（PR-4 同步删文件上限 50 的超出部分、
   以及当时令牌已死没删成的），绑定还活着时逐个核对删
   除；删完/确认已不存在/绑定已死无法再删的，落
   retention_reason 标记收口，不再重复处理（文件在贡献者
   自己盘里，删不掉不阻塞任何事，design §7 风险 3）。

热条目补位（refill，补充节 S4）在淘汰之后单独一步（仅池开
且转存队列闲置时）：近 7 天有起播、因成员失效/副本下线跌破
pool_item_max_copies 份的条目，按「24h 起播多 → 7 天起播多
→ 体积小」排序走 create_pool_job(trigger="refill") 补到
目标份数；补位单照常吃选盘打分与单盘日限额，优先级低于
观众当场点播的进池单（闲置门 + 每轮上限双保险）。

主链路资源纪律（后台红线）：本 janitor 不抢播放/API 的
CPU 与 DB 连接——单线程顺序执行、每步有批量上限（对账 40
行/轮、淘汰 20 份/成员/轮、残留 30 行/轮、补位 3 单/轮、
令牌探活 20 人/轮）、轮间隔 JANITOR_INTERVAL_SEC（30
分钟）、Drive 调用只经成员令牌的 about/get/delete 小请求，
不读文件字节。每步独立 try/except + 独立提交，一步失败只
记 stats、不拖垮整轮。每轮收尾一条结构化日志。

配置：行为阈值全走既有 pool 配置键（熔断两键、容量/保底/
闲置淘汰天数、副本上限），闲置主动清理开关走 pool_janitor
家族键；轮间隔与批量上限是运维节律、
非业务口径，作模块常量（同 PR-4 _EXIT_DELETE_LIMIT 先例）。
池总开关关时：第 6/8 步（淘汰/补位）跳过（池不存在就不
生产新动作），其余卫生步照跑（无成员/无数据时天然空转，
线上零变化）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from backend.emby_server import models as em
from backend.emby_server import pool_gate
from backend.emby_server import transfer_variant as _tv

logger = logging.getLogger(__name__)

#: 轮间隔（秒）：低频收尾活，30 分钟一轮足够（熔断有记数点
#: 当场触发兜底，本轮间隔只影响兜底扫与半开试探的时效）。
JANITOR_INTERVAL_SEC = 1800
#: 每轮死副本对账行数上限。
RECONCILE_BATCH = 40
#: 每轮每成员淘汰份数上限。
EVICT_MAX_PER_MEMBER = 20
#: 每轮退出残留收尾行数上限。
CLEANUP_BATCH = 30
#: 每轮令牌探活成员数上限。
TOKEN_CHECK_BATCH = 20
#: 每轮补位建单上限。
REFILL_MAX_PER_ROUND = 3
#: 补位热度窗（天）：近窗内有起播才算热条目（补充节 S4）。
REFILL_HOT_WINDOW_DAYS = 7
#: 残留收尾完成标记（写 retention_reason，防每轮重复核对）：
#: cleaned = 文件已删或已确认不存在；closed = 绑定已死、无
#: 法再删，按 design §7 风险 3 收口（行已 released 即完结）。
CLEANED_MARKER = "pool_janitor_cleaned"
CLOSED_MARKER = "pool_janitor_closed"


# ------------------------------------------------------------ 通用小件


def _member_client(db, member, client_factory=None):
    """成员盘客户端：绑定 active 才建得出来，否则回 None。

    client_factory(db, binding) 可注入（测试替身）；默认
    UserDriveApiClient（成员自己的令牌，绝不回落 SA）。
    """
    binding = pool_gate._member_binding(db, member)
    if binding is None or binding.status != "active":
        return None, binding
    try:
        if client_factory is not None:
            return client_factory(db, binding), binding
        return _tv.UserDriveApiClient(db, binding), binding
    except Exception:  # noqa: BLE001 — 建不出来按无令牌处理
        logger.warning("池巡检客户端构建失败 member=%s",
                       getattr(member, "id", None), exc_info=True)
        return None, binding


def _actively_playing_variant_ids(db, now) -> set:
    """正在播的池副本 id 集合（淘汰保护）：窗口内有起播事件
    的变体行。口径沿 transfer_cleanup 的正在播窗口（600 秒，
    形态复用：那边按用户会话判，本池按副本起播事件判——池
    副本的属主是成员、观众是别人，会话表对不上副本行）。"""
    from backend.emby_server.transfer_cleanup import (
        ACTIVE_PLAYING_WINDOW_SEC)

    since = now - timedelta(seconds=ACTIVE_PLAYING_WINDOW_SEC)
    rows = (db.query(em.PoolPlayEvent.variant_id)
            .filter(em.PoolPlayEvent.created_at >= since)
            .distinct().all())
    return {int(r[0]) for r in rows if r[0] is not None}


# ------------------------------------------------------------ 1/2 熔断扫与半开


def _sweep_breaker_trips(db, now) -> dict:
    stats = {"tripped": 0}
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status == "active")
               .order_by(em.PoolMember.id.asc()).all())
    for member in members:
        if pool_gate.maybe_trip_member_breaker(db, member, now=now):
            stats["tripped"] += 1
    db.commit()
    return stats


def _sweep_breaker_recovery(db, now, *, probe_fn=None) -> dict:
    stats = {"recovered": 0, "retripped": 0}
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status == "suspended",
                       em.PoolMember.suspended_reason == "breaker",
                       em.PoolMember.breaker_until.isnot(None),
                       em.PoolMember.breaker_until <= now)
               .order_by(em.PoolMember.id.asc()).all())
    for member in members:
        outcome = pool_gate.breaker_recover_probe(
            db, member, probe_fn=probe_fn, now=now)
        if outcome == "recovered":
            stats["recovered"] += 1
        elif outcome == "retripped":
            stats["retripped"] += 1
    db.commit()
    return stats


# ------------------------------------------------------------ 3/4 绑定兜底与令牌探活


def _sweep_binding_backstop(db, now) -> dict:
    stats = {"suspended": 0, "copies_staled": 0}
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status.in_(("active", "pending")),
                       em.PoolMember.binding_id.isnot(None))
               .order_by(em.PoolMember.id.asc()).all())
    for member in members:
        binding = pool_gate._member_binding(db, member)
        if binding is not None and binding.status != "active":
            pool_gate.on_binding_failed(db, binding)
            stats["suspended"] += 1
    db.commit()
    # 失效盘副本立即下线（S4）：binding_invalid 暂停成员名下
    # 还挂着 ready 的行一律标 stale（serve 口径本就跳过暂停
    # 成员，这里把行状态也对齐，防成员恢复后死副本复活）。
    dead = (db.query(em.PoolMember)
            .filter(em.PoolMember.status == "suspended",
                    em.PoolMember.suspended_reason == "binding_invalid")
            .all())
    for member in dead:
        rows = (db.query(em.TransferVariant)
                .filter(em.TransferVariant.owner_scope == "pool",
                        em.TransferVariant.owner_ref == str(member.id),
                        em.TransferVariant.status == "ready")
                .all())
        for row in rows:
            row.status = "stale"
            stats["copies_staled"] += 1
    db.commit()
    return stats


def _sweep_token_probe(db, now) -> dict:
    stats = {"checked": 0, "suspended": 0}
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status == "active",
                       em.PoolMember.binding_id.isnot(None))
               .order_by(em.PoolMember.id.asc())
               .limit(TOKEN_CHECK_BATCH).all())
    for member in members:
        binding = pool_gate._member_binding(db, member)
        if binding is None or binding.status != "active":
            continue
        stats["checked"] += 1
        # 取令牌本身即探活：刷新失败在 drive_oauth 内落绑定
        # 状态（_apply_refresh_failure → _mark_failed → 池连带
        # on_binding_failed）；本口吞异常回 ""，故探完回读
        # 绑定与成员状态对账，没跟上的用同一钩子补齐。
        pool_gate.pool_member_access_token(db, member.id)
        db.expire_all()
        binding = pool_gate._member_binding(db, member)
        fresh = (db.query(em.PoolMember)
                 .filter(em.PoolMember.id == member.id).first())
        if (binding is not None and binding.status != "active"
                and fresh is not None and fresh.status == "active"):
            pool_gate.on_binding_failed(db, binding)
            stats["suspended"] += 1
    db.commit()
    return stats


# ------------------------------------------------------------ 5 死副本对账


def _sweep_reconcile_copies(db, now, *, client_factory=None) -> dict:
    stats = {"checked": 0, "stale": 0, "orphaned": 0, "errors": 0}
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.owner_scope == "pool",
                    em.TransferVariant.status == "ready")
            .order_by(em.TransferVariant.last_checked_at.asc()
                      .nulls_first(),
                      em.TransferVariant.id.asc())
            .limit(RECONCILE_BATCH).all())
    clients: dict = {}
    for row in rows:
        try:
            member_id = int((row.owner_ref or "").strip())
        except (TypeError, ValueError):
            continue
        member = (db.query(em.PoolMember)
                  .filter(em.PoolMember.id == member_id).first())
        if member is None or member.status != "active":
            # 非 active 成员的副本下线归第 3 步/生命周期口，
            # 本步只验活在池成员的副本。
            continue
        item = (db.query(em.MediaItem)
                .filter(em.MediaItem.id == row.item_id,
                        em.MediaItem.deleted_at.is_(None))
                .first())
        if item is None:
            row.status = "orphan"
            stats["orphaned"] += 1
            continue
        item_fid = (getattr(item, "drive_file_id", "") or "").strip()
        if item_fid and row.source_file_id and \
                item_fid != row.source_file_id:
            # 源条目换了 fid：这份副本是旧片，立即下线（§6.4）。
            row.status = "stale"
            stats["stale"] += 1
            continue
        if member.id not in clients:
            clients[member.id] = _member_client(
                db, member, client_factory=client_factory)
        client, _binding = clients[member.id]
        if client is None:
            continue
        fid = (row.variant_file_id or "").strip()
        if not fid:
            continue
        try:
            client.get_file(f"myDrive:{member.user_id}", fid)
        except (_tv._NotFound, _tv._Forbidden):
            row.stale_count = int(row.stale_count or 0) + 1
            row.last_checked_at = now
            stats["checked"] += 1
            if row.stale_count >= _tv.STALE_STRIKES:
                row.status = "stale"
                stats["stale"] += 1
        except Exception:  # noqa: BLE001 — 429/5xx/网络：不记 strike
            stats["errors"] += 1
            logger.warning("池副本对账 get_file 异常 variant=%s"
                           "（不记 strike）", row.id, exc_info=True)
            continue
        else:
            row.stale_count = 0
            row.last_checked_at = now
            stats["checked"] += 1
    db.commit()
    return stats


# ------------------------------------------------------------ 6 容量淘汰


def _evict_candidates(db, member, cfg, now, playing_ids) -> list:
    """淘汰候选（已排序）：orphan → stale → 闲置冷 ready。

    闲置口径（design §6.5）：last_played_at 为空取 created_at，
    超过 pool_copy_idle_evict_days 天的 ready 行才入选（0 =
    不按闲置淘汰）；同档内按热度升序（近 7 天起播数少者在
    前、再按 last_played_at 旧者在前）。retention_until 未到
    与正在播的行恒跳过。
    """
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.owner_scope == "pool",
                    em.TransferVariant.owner_ref == str(member.id),
                    em.TransferVariant.status.in_(
                        ("ready", "stale", "orphan")))
            .all())

    def _blocked(row) -> bool:
        if row.id in playing_ids:
            return True
        until = getattr(row, "retention_until", None)
        return until is not None and until > now

    orphans = sorted(
        (r for r in rows if r.status == "orphan" and not _blocked(r)),
        key=lambda r: r.id)
    stales = sorted(
        (r for r in rows if r.status == "stale" and not _blocked(r)),
        key=lambda r: r.id)
    idle_days = int(cfg["pool_copy_idle_evict_days"])
    readies = []
    if idle_days > 0:
        from sqlalchemy import func

        since7 = now - timedelta(days=7)
        ready_ids = [r.id for r in rows if r.status == "ready"]
        play_counts = dict(
            db.query(em.PoolPlayEvent.variant_id,
                     func.count(em.PoolPlayEvent.id))
            .filter(em.PoolPlayEvent.created_at >= since7,
                    em.PoolPlayEvent.variant_id.in_(ready_ids))
            .group_by(em.PoolPlayEvent.variant_id).all()) \
            if ready_ids else {}
        for row in rows:
            if row.status != "ready" or _blocked(row):
                continue
            basis = row.last_played_at or row.created_at
            if basis is None or (now - basis).days < idle_days:
                continue
            readies.append((
                int(play_counts.get(row.id, 0)),
                row.last_played_at or datetime.min,
                row.id, row))
        readies.sort(key=lambda t: (t[0], t[1], t[2]))
    return orphans + stales + [t[3] for t in readies]


def _execute_evictions(db, member, client, candidates, *,
                       should_stop=None, usage=0,
                       reason_for) -> dict:
    """淘汰删除执行（容量触发与闲置主动清理共用的唯一删除路径）。

    按候选顺序逐个删文件：成功标 released、失败标 orphan 等
    下轮；每成员每轮上限 EVICT_MAX_PER_MEMBER；should_stop
    (stats, usage) 在每份删除前判定、真则提前收手（容量分支
    的滞后带停口）；收尾留一条 pool_evict 决策痕并提交。
    reason_for(stats, usage) 在收尾时按最终计数拼 reason。
    """
    stats = {"evicted": 0, "delete_failed": 0, "freed_bytes": 0}
    snapshot = []
    marker = f"myDrive:{member.user_id}"
    for row in candidates:
        if stats["evicted"] >= EVICT_MAX_PER_MEMBER:
            break
        if should_stop is not None and should_stop(stats, usage):
            break
        fid = (row.variant_file_id or "").strip()
        snapshot.append({"variant_id": int(row.id),
                         "item_id": int(row.item_id),
                         "status": row.status,
                         "size_bytes": int(row.size_bytes or 0)})
        if fid:
            try:
                client.delete_file_ignore_missing(marker, fid)
            except Exception:  # noqa: BLE001 — 删失败标 orphan 等下轮
                logger.warning("池淘汰删文件失败 member=%s variant=%s",
                               member.id, row.id, exc_info=True)
                row.status = "orphan"
                stats["delete_failed"] += 1
                continue
        row.status = "released"
        usage -= int(row.size_bytes or 0)
        stats["freed_bytes"] += int(row.size_bytes or 0)
        stats["evicted"] += 1
    if snapshot:
        from backend.emby_server import transfer_decisions as _decisions

        _decisions.note_decision(
            scope="pool_evict", resource_key=f"member:{member.id}",
            decision=f"evicted:{stats['evicted']}",
            reason=reason_for(stats, usage),
            candidates=snapshot)
    db.commit()
    return stats


def _evict_member_once(db, member, cfg, now, *, client_factory=None,
                       quota_fn=None) -> dict:
    """单盘容量淘汰一轮：触发判定 + 按候选顺序删到滞后带为止。"""
    stats = {"evicted": 0, "delete_failed": 0, "freed_bytes": 0,
             "triggered": False}
    cap = int(member.capacity_cap_bytes
              or cfg["pool_member_capacity_bytes"])
    reserve = int(cfg["pool_member_reserve_free_bytes"])
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.owner_scope == "pool",
                    em.TransferVariant.owner_ref == str(member.id),
                    em.TransferVariant.status.in_(
                        ("ready", "stale", "orphan")))
            .all())
    usage = sum(int(r.size_bytes or 0) for r in rows)
    quota = pool_gate._member_quota(db, member, quota_fn=quota_fn)
    free = None
    if quota is not None:
        free = _tv._to_int(quota.get("free"))
    over_cap = cap > 0 and usage > cap
    low_free = free is not None and free < reserve
    if not over_cap and not low_free:
        return stats
    stats["triggered"] = True
    target_usage = int(cap * 0.95) if cap > 0 else 0
    client, _binding = _member_client(
        db, member, client_factory=client_factory)
    if client is None:
        # 令牌建不出来：删不了文件，本轮只记账不假装已淘。
        stats["no_client"] = True
        return stats
    playing_ids = _actively_playing_variant_ids(db, now)
    candidates = _evict_candidates(db, member, cfg, now, playing_ids)

    def _at_band(inner, cur_usage):
        cap_ok = (not over_cap) or cur_usage <= target_usage
        free_ok = (not low_free) or (
            free is not None
            and free + inner["freed_bytes"] >= reserve)
        return cap_ok and free_ok

    done = _execute_evictions(
        db, member, client, candidates, should_stop=_at_band,
        usage=usage,
        reason_for=lambda _s, final_usage: (
            f"usage={final_usage} cap={cap} free={free} "
            f"reserve={reserve}"))
    stats.update(done)
    return stats


def _sweep_evictions(db, cfg, now, *, client_factory=None,
                     quota_fn=None) -> dict:
    stats = {"members": 0, "evicted": 0, "delete_failed": 0,
             "freed_bytes": 0}
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status == "active")
               .order_by(em.PoolMember.id.asc()).all())
    for member in members:
        one = _evict_member_once(
            db, member, cfg, now, client_factory=client_factory,
            quota_fn=quota_fn)
        if one.get("triggered"):
            stats["members"] += 1
        stats["evicted"] += one["evicted"]
        stats["delete_failed"] += one["delete_failed"]
        stats["freed_bytes"] += one["freed_bytes"]
    return stats


# ------------------------------------------ 6b 闲置主动清理


def _idle_proactive_enabled(db) -> bool:
    """闲置主动清理开关（pool_janitor 家族键，默认开）：读取
    异常按关处理（fail-closed，不许因读不到配置就多删文件）。"""
    try:
        return bool(pool_gate.janitor_config_payload(
            db)["pool_idle_evict_proactive_enabled"])
    except Exception:  # noqa: BLE001
        logger.warning("闲置主动清理开关读取失败，按关处理",
                       exc_info=True)
        return False


def _evict_idle_member_once(db, member, cfg, now, *,
                            client_factory=None) -> dict:
    """单盘闲置主动清理一轮（不依赖容量触发）：只取闲置超标的
    ready 副本（候选口径与容量淘汰共用 _evict_candidates——
    正在播、retention_until 未到的恒跳过），删除/标态/决策
    留痕与容量淘汰共用 _execute_evictions。"""
    stats = {"evicted": 0, "delete_failed": 0, "freed_bytes": 0,
             "triggered": False}
    idle_days = int(cfg["pool_copy_idle_evict_days"])
    if idle_days <= 0:
        # 闲置天数 0 = 闲置淘汰关：与容量分支同口径不淘。
        return stats
    playing_ids = _actively_playing_variant_ids(db, now)
    candidates = [
        row for row in _evict_candidates(db, member, cfg, now,
                                         playing_ids)
        if row.status == "ready"]
    if not candidates:
        return stats
    client, _binding = _member_client(
        db, member, client_factory=client_factory)
    if client is None:
        # 令牌建不出来：删不了文件，本轮只记账不假装已清。
        stats["no_client"] = True
        return stats
    stats["triggered"] = True
    done = _execute_evictions(
        db, member, client, candidates,
        reason_for=lambda _s, _u: f"proactive_idle days={idle_days}")
    stats.update(done)
    return stats


def _sweep_idle_proactive_evictions(db, cfg, now, *,
                                    client_factory=None) -> dict:
    """闲置主动清理一轮（第 6 步的独立分支）：逐个 active 成员
    清闲置超标的 ready 副本，不看容量上限与剩余空间。"""
    stats = {"members": 0, "evicted": 0, "delete_failed": 0,
             "freed_bytes": 0}
    members = (db.query(em.PoolMember)
               .filter(em.PoolMember.status == "active")
               .order_by(em.PoolMember.id.asc()).all())
    for member in members:
        one = _evict_idle_member_once(
            db, member, cfg, now, client_factory=client_factory)
        if one.get("triggered"):
            stats["members"] += 1
        stats["evicted"] += one["evicted"]
        stats["delete_failed"] += one["delete_failed"]
        stats["freed_bytes"] += one["freed_bytes"]
    return stats


# ------------------------------------------------------------ 7 退出残留收尾


def _sweep_leftover_cleanup(db, now, *, client_factory=None) -> dict:
    stats = {"cleaned": 0, "closed": 0, "errors": 0}
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.owner_scope == "pool",
                    em.TransferVariant.status == "released",
                    em.TransferVariant.retention_reason.is_(None))
            .order_by(em.TransferVariant.id.asc())
            .limit(CLEANUP_BATCH).all())
    clients: dict = {}
    for row in rows:
        try:
            member_id = int((row.owner_ref or "").strip())
        except (TypeError, ValueError):
            row.retention_reason = CLOSED_MARKER
            stats["closed"] += 1
            continue
        member = (db.query(em.PoolMember)
                  .filter(em.PoolMember.id == member_id).first())
        if member is None or member.status not in (
                pool_gate.POOL_MEMBER_TERMINAL_STATUSES):
            # 还在池的成员名下 released 行不归本步（淘汰已当场
            # 删文件、生命周期下线另有口径），不碰。
            continue
        if member.id not in clients:
            clients[member.id] = _member_client(
                db, member, client_factory=client_factory)
        client, binding = clients[member.id]
        fid = (row.variant_file_id or "").strip()
        if client is None or not fid:
            # 绑定已死/无文件 id：删不掉也不必再删——文件在
            # 贡献者自己盘里，行已 released 即完结（§7 风险 3）。
            row.retention_reason = CLOSED_MARKER
            stats["closed"] += 1
            continue
        try:
            client.get_file(f"myDrive:{member.user_id}", fid)
        except (_tv._NotFound, _tv._Forbidden):
            row.retention_reason = CLEANED_MARKER
            stats["cleaned"] += 1
            continue
        except Exception:  # noqa: BLE001 — 暂时性错误，下轮再核
            stats["errors"] += 1
            continue
        try:
            client.delete_file_ignore_missing(
                f"myDrive:{member.user_id}", fid)
        except Exception:  # noqa: BLE001 — 删失败下轮再试，不标完成
            logger.warning("池残留删文件失败 variant=%s",
                           row.id, exc_info=True)
            stats["errors"] += 1
            continue
        row.retention_reason = CLEANED_MARKER
        stats["cleaned"] += 1
    db.commit()
    return stats


# ------------------------------------------------------------ 8 热条目补位


def _sweep_refill(db, cfg, now, *, quota_fn=None) -> dict:
    stats = {"created": 0, "blocked": 0, "skipped_busy": 0}
    queue = _tv.queue_stats(db, now=now)
    if int(queue.get("due_count") or 0) > 0 or \
            int(queue.get("running") or 0) > 0:
        # 只吃闲置产能：转存队列有人在等/在跑时补位整步跳过。
        stats["skipped_busy"] = 1
        return stats
    from sqlalchemy import case, func

    since7 = now - timedelta(days=REFILL_HOT_WINDOW_DAYS)
    since24 = now - timedelta(days=1)
    hot = (db.query(
        em.PoolPlayEvent.item_id,
        func.count(em.PoolPlayEvent.id).label("plays_7d"),
        func.sum(case(
            (em.PoolPlayEvent.created_at >= since24, 1), else_=0)
        ).label("plays_24h"))
        .filter(em.PoolPlayEvent.created_at >= since7)
        .group_by(em.PoolPlayEvent.item_id)
        .all())
    max_copies = max(1, int(cfg["pool_item_max_copies"]))
    enriched = []
    for item_id, plays_7d, plays_24h in hot:
        item = (db.query(em.MediaItem)
                .filter(em.MediaItem.id == item_id,
                        em.MediaItem.deleted_at.is_(None))
                .first())
        if item is None:
            continue
        enriched.append((
            -int(plays_24h or 0), -int(plays_7d or 0),
            int(getattr(item, "size", None) or 0), item,
            int(plays_7d or 0), int(plays_24h or 0)))
    # S4 补位排序：24h 起播降序 → 7 天起播降序 → 体积升序。
    enriched.sort(key=lambda t: (t[0], t[1], t[2]))
    for _n24, _n7, _size, item, plays_7d, plays_24h in enriched:
        if stats["created"] >= REFILL_MAX_PER_ROUND:
            break
        servable = (pool_gate._servable_pool_copy_count(db, item.id)
                    + pool_gate._pool_inflight_for_item(db, item.id))
        if servable >= max_copies:
            continue
        # 只补「因下线跌破份数」的：有不可下发的池行（stale/
        # released/orphan），或有 ready 行挂在不可下发的成员
        # 名下（暂停/熔断/失效），才算丢过副本；从没满过份数
        # 的热片走观众点播进池，不归补位（§6.4 下线条目不
        # 自动重进的边界，S4 例外只限成员失效跌破这一种）。
        holders = pool_gate._servable_pool_holder_ids(db, item.id)
        all_rows = (db.query(em.TransferVariant)
                    .filter(em.TransferVariant.item_id == item.id,
                            em.TransferVariant.owner_scope == "pool")
                    .all())
        lost = False
        for row in all_rows:
            if row.status in ("stale", "released", "orphan"):
                lost = True
                break
            if row.status == "ready":
                try:
                    owner_id = int((row.owner_ref or "").strip())
                except (TypeError, ValueError):
                    owner_id = None
                if owner_id not in holders:
                    lost = True
                    break
        if not lost:
            continue
        try:
            job = pool_gate.create_pool_job(
                db, item, trigger="refill", quota_fn=quota_fn,
                exclude_member_ids=holders)
        except _tv.TransferError:
            stats["blocked"] += 1
            continue
        from backend.emby_server import transfer_decisions as _decisions

        _decisions.note_decision(
            scope="pool_pick", resource_key=f"item:{item.id}",
            decision=f"member:{job.owner_ref}",
            reason=(f"refill plays_24h={plays_24h} "
                    f"plays_7d={plays_7d} servable={servable}"
                    f"<max={max_copies}"),
            candidates=[])
        stats["created"] += 1
    return stats


# ------------------------------------------------------------ 轮次收口与守护线程


def run_pool_janitor_once(db, now=None, *, client_factory=None,
                          quota_fn=None, probe_fn=None) -> dict:
    """跑一整轮巡检（七步 + 补位），返回各步统计 dict。

    永不抛异常：每步独立 try/except + 出错回滚该步事务，
    一步坏不拖垮整轮（守护线程与测试共用本入口）。
    """
    now = now or datetime.now()
    cfg = pool_gate.pool_config_payload(db)
    enabled = bool(cfg["pool_enabled"])
    stats: dict = {"pool_enabled": enabled}

    def _step(name, fn):
        try:
            stats[name] = fn()
        except Exception:  # noqa: BLE001 — 单步失败不拖垮整轮
            logger.warning("池巡检步骤失败 step=%s", name, exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            stats[name] = {"error": True}

    _step("breaker_trips", lambda: _sweep_breaker_trips(db, now))
    _step("breaker_recovery", lambda: _sweep_breaker_recovery(
        db, now, probe_fn=probe_fn))
    _step("binding_backstop", lambda: _sweep_binding_backstop(db, now))
    _step("token_probe", lambda: _sweep_token_probe(db, now))
    _step("reconcile", lambda: _sweep_reconcile_copies(
        db, now, client_factory=client_factory))
    if enabled:
        _step("evict", lambda: _sweep_evictions(
            db, cfg, now, client_factory=client_factory,
            quota_fn=quota_fn))
        if _idle_proactive_enabled(db):
            _step("idle_proactive", lambda:
                  _sweep_idle_proactive_evictions(
                      db, cfg, now, client_factory=client_factory))
    _step("leftover_cleanup", lambda: _sweep_leftover_cleanup(
        db, now, client_factory=client_factory))
    if enabled:
        _step("refill", lambda: _sweep_refill(
            db, cfg, now, quota_fn=quota_fn))
    logger.info("pool_janitor round %s", stats)
    return stats


_janitor_thread: threading.Thread | None = None
_stop_event = threading.Event()
_start_lock = threading.Lock()


def _janitor_loop():
    # 起步先等一小段，避开 worker 启动高峰；之后每
    # JANITOR_INTERVAL_SEC 一轮，开关/成员状态都在轮内现读，
    # 后台改配置不用重启。
    if _stop_event.wait(60):
        return
    while not _stop_event.is_set():
        try:
            from backend.database import SessionLocal

            db = SessionLocal()
            try:
                run_pool_janitor_once(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — 线程永不因一轮异常死掉
            logger.warning("池巡检轮次异常", exc_info=True)
        _stop_event.wait(JANITOR_INTERVAL_SEC)


def start() -> bool:
    """启动池巡检后台线程（常驻；无成员/池关时轮内天然空转）。"""
    global _janitor_thread
    with _start_lock:
        if _janitor_thread and _janitor_thread.is_alive():
            return True
        _stop_event.clear()
        _janitor_thread = threading.Thread(
            target=_janitor_loop, name="pool-janitor", daemon=True)
        _janitor_thread.start()
        try:
            from backend.emby_server import worker_registry as _wr

            _wr.register("pool_janitor", _janitor_thread)
        except Exception:  # noqa: BLE001 — 注册失败不影响线程本身
            pass
        return True


def stop():
    """停池巡检后台线程（测试/关机用）。"""
    _stop_event.set()

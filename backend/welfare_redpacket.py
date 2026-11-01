# -*- coding: utf-8 -*-
"""红包业务逻辑（公益服模块3-娱乐板块）

规则：
- 发红包：从发送者能量扣除 total_amount + 手续费，创建红包（24小时过期）
- 抢红包：随机金额（剩余金额随机分配，保证每人至少 1 能量）
- 防重复：同一用户不能重复领取同一红包
- 红包过期后剩余能量退回发送者（领取时检查）

金额分配：随机红包算法，最后一个领取者拿剩余全部。

P0 统一货币体系：
- 手续费：默认 5%，可通过 SystemConfig redpacket_fee_pct 配置（0=不收）
- 频率限制：7天窗口，可通过 SystemConfig 配置（0=不限）
  - redpacket_send_limit_7d：7天内最多发送次数（默认20）
  - redpacket_recv_limit_7d：7天内最多领取次数（默认10，管理员发出的不计）
- 账本类型：redpacket（发出/抢到/退回）、redpacket_fee（手续费扣除）
"""

from __future__ import annotations

import logging
import math
import random
import threading
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.integrations.store import switch_env_value as _switch_env_value
from backend.api import economy

logger = logging.getLogger(__name__)

# 红包有效期（小时）
PACKET_TTL_HOURS = 24


def _get_int_config(db: Session, key: str, default: int) -> int:
    """读 SystemConfig 整数配置（读不到或非法则用默认值）"""
    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    try:
        return int(cfg.value) if cfg and cfg.value else default
    except (ValueError, TypeError):
        return default


def send_packet(
    db: Session,
    sender: models.WebUser,
    total_amount: int,
    total_count: int,
    idempotency_key: str | None = None,
) -> models.RedPacket:
    """发红包

    校验：金额>0，个数>0，金额>=个数（每人至少 1 能量），余额充足（含手续费）
    频率：7天内发送次数上限（redpacket_send_limit_7d，0=不限）
    手续费：按 redpacket_fee_pct 比例额外扣除，记 redpacket_fee 账本
    停用账号（PA-019）不得发红包

    幂等：idempotency_key（如 tg:<update_id>）有唯一约束；TG poller
    at-least-once 重放时，第二次 insert 撞唯一约束 → 回滚并返回已存在的包，
    不再重复扣款（P1 修复）。
    """
    if not getattr(sender, "is_active", True):
        raise ValueError("账号已被停用，无法发红包")
    if total_amount <= 0:
        raise ValueError("红包金额必须大于0")
    if total_count <= 0:
        raise ValueError("红包个数必须大于0")
    if total_amount < total_count:
        raise ValueError("红包金额不能小于个数（每人至少 1 能量）")
    if total_count > 100:
        raise ValueError("红包个数不能超过100")

    # 幂等预检（必须在扣款/频率检查之前）：SQLite 存量表加不上唯一约束
    #（create_all 不改旧表），先查一次；PG/MySQL 另有库级唯一约束防并发竞态
    #（下方的 IntegrityError 分支）。
    if idempotency_key:
        existing = db.query(models.RedPacket).filter(
            models.RedPacket.idempotency_key == idempotency_key
        ).first()
        if existing is not None:
            logger.info("红包重放幂等命中（预检）: key=%s packet_id=%s",
                        idempotency_key, existing.id)
            return existing

    # 发送频率检查
    send_limit = _get_int_config(db, "redpacket_send_limit_7d", 20)
    if send_limit > 0:
        # 锁发送方用户行后再计数，并发连发不能绕过 7 天次数上限
        economy.lock_user_row(db, sender.id)
        week_ago = datetime.now() - timedelta(days=7)
        sent = db.query(models.RedPacket).filter(
            models.RedPacket.sender_id == sender.id,
            models.RedPacket.created_at >= week_ago,
        ).count()
        if sent >= send_limit:
            raise ValueError(f"7天内最多发送 {send_limit} 次红包")

    # 手续费（向上取整）
    fee_pct = _get_int_config(db, "redpacket_fee_pct", 5)
    fee = math.ceil(total_amount * fee_pct / 100) if fee_pct > 0 else 0
    if fee > 0 and economy._whitelist_fee_exempt(db, sender):
        # 白名单（永久）账号免红包手续费（2026-11-02 独一无二体验包，
        # 判定与转账共用 economy._whitelist_fee_exempt）：fee 归 0 后，
        # 下游记账与 fee_pct 配置为 0 时逐字一致（不写 redpacket_fee
        # 台账行、日志 fee=0）。收款方行为不变。
        fee = 0
    need = total_amount + fee

    balance = int(sender.points or 0)
    if balance < need:
        if fee > 0:
            raise ValueError(
                f"能量不足，需要 {need} 能量（含手续费 {fee} 能量），当前 {balance} 能量")
        raise ValueError(f"能量不足，需要 {need} 能量，当前 {balance} 能量")

    # 扣分：红包本体 + 手续费（分开记账）
    # 原子条件扣减：上面的余额检查读的是 ORM 内存旧值，并发连发多个红包都能通过它；
    # 「够不够」交给 UPDATE ... WHERE points >= x，扣不到整笔回滚（防双花/余额变负）
    try:
        economy._spend_points(db, sender, total_amount, "redpacket", f"发出红包 {total_amount} 能量")
        if fee > 0:
            economy._spend_points(
                db, sender, fee, "redpacket_fee", f"红包手续费 {fee} 能量（{fee_pct}%）")
    except economy.InsufficientPoints:
        db.rollback()
        raise ValueError(f"能量不足，需要 {need} 能量")

    packet = models.RedPacket(
        sender_id=sender.id,
        total_amount=total_amount,
        total_count=total_count,
        remaining_amount=total_amount,
        remaining_count=total_count,
        expires_at=datetime.now() + timedelta(hours=PACKET_TTL_HOURS),
        idempotency_key=idempotency_key,
    )
    db.add(packet)
    try:
        db.commit()
    except IntegrityError:
        # 幂等重放：同一 idempotency_key 的包已存在 → 回滚本次扣款，返回已存在的包
        db.rollback()
        existing = db.query(models.RedPacket).filter(
            models.RedPacket.idempotency_key == idempotency_key
        ).first() if idempotency_key else None
        if existing is not None:
            logger.info("红包重放幂等命中: key=%s packet_id=%s", idempotency_key, existing.id)
            return existing
        raise
    db.refresh(packet)
    logger.info("红包发出: sender=%s amount=%s count=%s fee=%s",
                sender.id, total_amount, total_count, fee)
    return packet


def claim_packet(db: Session, user: models.WebUser, packet_id: int) -> dict:
    """抢红包

    校验：红包存在、未过期、还有剩余、未领过
    频率：7天内领取次数上限（redpacket_recv_limit_7d，0=不限；管理员发出的不计）
    停用账号（PA-019）不得抢红包；发送者（PA-017）不得抢自己发的红包
    金额：随机 1 ~ (剩余金额-剩余个数+1)，最后一个拿全部剩余
    并发：红包行加行锁（FOR UPDATE），同一红包的抢领串行化，防超发；
    领取记录有唯一约束 (packet_id, user_id) 兜底，竞态重复走 IntegrityError
    """
    # 行锁取红包行：高并发下同一红包的抢领串行化，防超发。
    # 锁从这里一直持有到最后 commit，金额计算→扣减→写记录在同一事务内完成。
    packet = db.query(models.RedPacket).filter(
        models.RedPacket.id == packet_id).with_for_update().first()
    if packet is None:
        raise ValueError("红包不存在")
    if not getattr(user, "is_active", True):
        raise ValueError("账号已被停用，无法抢红包")

    # 领取频率检查（管理员发出的红包不计入）
    recv_limit = _get_int_config(db, "redpacket_recv_limit_7d", 10)
    if recv_limit > 0:
        # 锁领取方用户行后再计数（锁序：红包行 → 用户行），并发抢多个红包不能绕过上限
        economy.lock_user_row(db, user.id)
        week_ago = datetime.now() - timedelta(days=7)
        claimed = db.query(models.RedPacketClaim).join(
            models.RedPacket,
            models.RedPacketClaim.packet_id == models.RedPacket.id,
        ).join(
            models.WebUser,
            models.RedPacket.sender_id == models.WebUser.id,
        ).filter(
            models.RedPacketClaim.user_id == user.id,
            models.RedPacketClaim.created_at >= week_ago,
            models.WebUser.is_staff == False,  # noqa: E712
        ).count()
        if claimed >= recv_limit:
            raise ValueError(f"7天内最多领取 {recv_limit} 次红包")

    now = datetime.now()
    if packet.expires_at and packet.expires_at < now:
        raise ValueError("红包已过期")
    if packet.remaining_count <= 0:
        raise ValueError("红包已被抢完")

    # 发送者不能抢自己发的红包（PA-017）：行锁内、过期/抢完校验后拦截，
    # 否则“分给别人”的语义会被自抢大头掏空。
    if user.id == packet.sender_id:
        raise ValueError("不能抢自己发的红包")

    # 防重复领取
    dup = db.query(models.RedPacketClaim).filter(
        models.RedPacketClaim.packet_id == packet_id,
        models.RedPacketClaim.user_id == user.id,
    ).first()
    if dup:
        raise ValueError("你已经领过这个红包了")

    # 随机金额
    if packet.remaining_count == 1:
        amount = packet.remaining_amount
    else:
        max_amount = packet.remaining_amount - packet.remaining_count + 1
        amount = random.randint(1, max_amount)

    # 更新红包
    packet.remaining_amount -= amount
    packet.remaining_count -= 1

    # 给领取者加分
    economy._add_points(db, user, amount, "redpacket", f"抢到红包 {amount} 能量")

    # 写领取记录（唯一约束 (packet_id, user_id) 兜底并发重复）
    db.add(models.RedPacketClaim(
        packet_id=packet.id, user_id=user.id, amount=amount))
    try:
        db.commit()
    except IntegrityError:
        # 并发竞态：另一请求已先写入同一 (packet_id, user_id) 的领取记录
        db.rollback()
        raise ValueError("你已经领过这个红包了")

    logger.info("红包领取: packet=%s user=%s amount=%s", packet_id, user.id, amount)
    return {
        "packet_id": packet.id,
        "amount": amount,
        "remaining_count": packet.remaining_count,
    }


def refund_expired(db: Session) -> int:
    """过期红包退款（供定时任务调用）：剩余能量退回发送者。

    行锁（FOR UPDATE SKIP LOCKED）+ 锁后二次校验 + 逐包提交，幂等：
    多 worker 并发跑也不会重复退款。
    """
    now = datetime.now()
    count = 0
    while True:
        packet = (
            db.query(models.RedPacket)
            .filter(
                models.RedPacket.expires_at < now,
                models.RedPacket.remaining_count > 0,
                models.RedPacket.remaining_amount > 0,
            )
            .with_for_update(skip_locked=True)
            .first()
        )
        if packet is None:
            break
        # 锁后二次校验：锁等待期间可能已被抢完
        if packet.remaining_count <= 0 or packet.remaining_amount <= 0:
            db.rollback()
            continue
        sender = db.query(models.WebUser).filter(
            models.WebUser.id == packet.sender_id).first()
        if sender is None:
            # 发送者已删除：清零避免每轮重复扫描，退款无处可去
            packet.remaining_amount = 0
            packet.remaining_count = 0
            db.commit()
            logger.warning("过期红包退款跳过：发送者不存在 packet=%s", packet.id)
            continue
        refund = packet.remaining_amount
        economy._add_points(
            db, sender, refund, "redpacket", f"红包过期退回 {refund} 能量")
        packet.remaining_amount = 0
        packet.remaining_count = 0
        db.commit()
        count += 1

    if count:
        logger.info("过期红包退款: %s 个", count)
    return count


_REFUND_SCHEDULER_STARTED = False
_REFUND_SCHEDULER_LOCK = threading.Lock()

# 退款扫描间隔（秒）：默认 5 分钟，最小 1 分钟
REFUND_INTERVAL_DEFAULT_SEC = 300
REFUND_INTERVAL_MIN_SEC = 60


def _get_str_config(db: Session, key: str, default: str) -> str:
    """读 SystemConfig 字符串配置（读不到则用默认值）"""
    # A 类摘面板开关（2026-10-21）：env 显式设置即接管，口径见 integrations.store
    _env_v = _switch_env_value(key)
    if _env_v is not None:
        return _env_v
    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    return str(cfg.value) if cfg and cfg.value is not None else default


def _refund_interval_sec(db: Session) -> int:
    """退款扫描间隔（秒），管理后台可配，最小 60 秒"""
    v = _get_int_config(
        db, "redpacket_refund_interval_sec", REFUND_INTERVAL_DEFAULT_SEC)
    return max(v, REFUND_INTERVAL_MIN_SEC)


def start_redpacket_refund_scheduler() -> bool:
    """启动过期红包自动退款调度（daemon 线程，同一进程只启动一次）

    每隔 redpacket_refund_interval_sec（默认 300s，最小 60s）扫描一次；
    总开关 redpacket_refund_enabled（默认 true）关闭时跳过本轮。
    间隔每次循环重读，管理后台改完即时生效。
    """
    global _REFUND_SCHEDULER_STARTED
    with _REFUND_SCHEDULER_LOCK:
        if _REFUND_SCHEDULER_STARTED:
            return False
        _REFUND_SCHEDULER_STARTED = True

    def _tick():
        try:
            from backend.database import SessionLocal
            db = SessionLocal()
            try:
                if _get_str_config(
                        db, "redpacket_refund_enabled", "true").lower() != "true":
                    return
                n = refund_expired(db)
                if n:
                    logger.info("红包过期自动退款完成: %s 个", n)
            finally:
                db.close()
        except Exception:
            logger.exception("redpacket refund scheduler tick failed")

    def _loop():
        while True:
            try:
                from backend.database import SessionLocal
                db = SessionLocal()
                try:
                    interval = _refund_interval_sec(db)
                finally:
                    db.close()
            except Exception:
                logger.exception(
                    "redpacket refund scheduler interval read failed")
                interval = REFUND_INTERVAL_DEFAULT_SEC
            threading.Event().wait(interval)
            _tick()

    threading.Thread(
        target=_loop, daemon=True, name="redpacket-refund-scheduler").start()
    logger.info("红包过期退款调度已启动")
    return True

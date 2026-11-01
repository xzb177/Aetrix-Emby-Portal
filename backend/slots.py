"""能量老虎机核心服务（用户 2026-10-30 亲批设计，TG 机器人先行）。

公平口径：结果只认 Telegram 原生 sendDice（emoji 🎰）返回的 dice.value
（1-64），服务端不自造随机数。解码：v=value-1，三轴分别为
v//16、(v//4)%4、v%4，符号序 [BAR, 葡萄, 柠檬, 7]（value=64 即 777）。

赔付（倍数 × 注额，默认）：777×20 / BAR 三连×10 / 葡萄·柠檬三连×5 /
恰好两个 7×1（返注）。全 64 值等概率下基础返还率 = 49/64 ≈ 76.6%。

头奖池：每注抽 slots_pool_rake_pct%（默认 5%）进池；出 777 时奖金 =
基础赔付 + 整池，池随即重置为种子额（默认 100）。池余额存自有表
slots_pool（单行 + 行锁 + 百分之一能量余数位精确累计），绝不放
SystemConfig（有缓存与并发竞态）。池抽水只记池账、不进用户台账。

账务：扣注 / 赔付 / 头奖 / 退注全部走 backend.api.economy 的台账原语
（_spend_points 原子扣、_add_points 入账），type 分别为 slots_bet /
slots_win / slots_jackpot / slots_refund，ref_id = "slots:<spin_key>"。

资格（镜像群抽奖 join_round 的现行校验，逐条对照）：
- 已绑定 TG 账号（调用方 resolve 后传入 WebUser，未绑定进不到这里）；
- 公益服口径：当前服必须是 free realm（能量只在公益服流通）；
- 抽奖黑名单（lottery.is_blacklisted，同一张名单）；
- 新号限制（lottery.min_account_age_days，与抽奖共用配置）。
不镜像抽奖的「身份门」（getChatMember 群成员校验）：群里下注的点击
本身就发生在名单群内，不再另查成员状态；也不镜像「每日参加次数」：
老虎机自有每日下注额封顶。

私聊与群聊共用本服务（2026-10-30 用户亲令群里可玩，推翻初版仅私聊
口径）：群注由 callbacks 按点击者逐个调用——扣点击者本人的能量、
赢归点击者、封顶按点击者计；群开关 slots_group_enabled 只决定群
入口开不开，不进资格判定。台账描述带来源标记：群注在描述末尾加
「（群聊）」，私聊保持原文逐字不变（type 不变）。

护栏：每日下注总额封顶（默认 200）、每日赢分封顶（默认 1000，只计
基础赔付，777 的头奖池部分不计）；一人同时只许一局在飞——下注到结算
全程以 spin_key（TG callback id）幂等，重复回调只认已存在的 spin；
pending 超过 STALE_SECONDS 的残局在下一局开始时自动判失败并退注。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.api import economy

logger = logging.getLogger(__name__)

STALE_SECONDS = 120

SYMBOL_KEYS = ("bar", "grape", "lemon", "seven")
SYMBOL_LABEL = {
    "bar": "BAR",
    "grape": "🍇 葡萄",
    "lemon": "🍋 柠檬",
    "seven": "7️⃣",
}
SYMBOL_EMOJI = {"bar": "🅱️", "grape": "🍇", "lemon": "🍋", "seven": "7️⃣"}

REASON_MESSAGES = {
    "disabled": "🎰 老虎机暂未开启",
    "account_disabled": "账号已被停用，无法玩老虎机",
    "not_free_realm": "🎰 老虎机仅在公益服开放",
    "blacklisted": "你暂无老虎机资格",
    "too_new": "账号注册满 {min_days} 天后才能玩老虎机",
    "bad_tier": "注额档位无效",
    "daily_bet_cap": "今日下注已达上限，明天再来",
    "daily_win_cap": "今日赢分已达上限",
    "in_flight": "上一局还没结算，请稍候",
    "insufficient": "能量不足，先去签到攒一点再来",
    "settled": "本局已结算",
}


def reason_message(reason: str, **kw) -> str:
    text = REASON_MESSAGES.get(reason, "系统繁忙，请稍后再试")
    try:
        return text.format(**kw)
    except (KeyError, IndexError):
        return text


def _origin_suffix(origin: str) -> str:
    """台账描述的来源标记：群注加「（群聊）」，私聊为空（原文不变）。"""
    return "（群聊）" if origin == "group" else ""


# ==================== 配置（SystemConfig，默认值 = 亲批定版） ====================

def is_enabled(db: Session) -> bool:
    """总开关 slots_enabled，默认开。"""
    return economy._get_bool_config(db, "slots_enabled", True)


def group_enabled(db: Session) -> bool:
    """群里可玩开关 slots_group_enabled，默认开（用户亲令群里能玩）。"""
    return economy._get_bool_config(db, "slots_group_enabled", True)


def bet_tiers(db: Session) -> list[int]:
    """注额档位 slots_bet_tiers（逗号分隔），默认 5,10,20。"""
    raw = economy._get_config(db, "slots_bet_tiers", "5,10,20")
    tiers: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in tiers:
            tiers.append(value)
    return tiers or [5, 10, 20]


def default_pays() -> dict[str, int]:
    return {"seven": 20, "bar": 10, "fruit": 5, "two_seven": 1}


def pay_table(db: Session) -> dict[str, int]:
    """赔付倍数表（slots_pay_*），默认亲批定版。"""
    return {
        "seven": max(0, economy._get_int_config(db, "slots_pay_777", 20)),
        "bar": max(0, economy._get_int_config(db, "slots_pay_bar", 10)),
        "fruit": max(0, economy._get_int_config(db, "slots_pay_fruit", 5)),
        "two_seven": max(0, economy._get_int_config(db, "slots_pay_two7", 1)),
    }


def pool_rake_pct(db: Session) -> int:
    return min(100, max(0, economy._get_int_config(db, "slots_pool_rake_pct", 5)))


def pool_seed(db: Session) -> int:
    return max(0, economy._get_int_config(db, "slots_pool_seed", 100))


def daily_bet_cap(db: Session) -> int:
    """每日下注封顶（0 = 不限制），默认 200。"""
    return max(0, economy._get_int_config(db, "slots_daily_bet_cap", 200))


def daily_win_cap(db: Session) -> int:
    """每日赢分封顶（只计基础赔付；0 = 不限制），默认 1000。"""
    return max(0, economy._get_int_config(db, "slots_daily_win_cap", 1000))


# ==================== 解码与赔付（纯函数） ====================

def decode_dice(value: int) -> tuple[str, str, str]:
    """dice.value（1-64）→ 三轴符号。越界值钳制到 [1, 64]。"""
    v = min(64, max(1, int(value))) - 1
    return (SYMBOL_KEYS[v // 16], SYMBOL_KEYS[(v // 4) % 4], SYMBOL_KEYS[v % 4])


def payout_for(reels: tuple[str, str, str], bet: int,
               pays: dict[str, int]) -> tuple[int, bool]:
    """按三轴结果算基础赔付，返回 (基础赔付, 是否 777)。"""
    if reels == ("seven", "seven", "seven"):
        return bet * pays["seven"], True
    if reels[0] == reels[1] == reels[2]:
        if reels[0] == "bar":
            return bet * pays["bar"], False
        return bet * pays["fruit"], False  # 葡萄 / 柠檬三连同价
    if reels.count("seven") == 2:
        return bet * pays["two_seven"], False
    return 0, False


def expected_return(pays: dict[str, int] | None = None) -> float:
    """全 64 值等概率下、单位注额的基础返还率（不含头奖池）。"""
    pays = pays or default_pays()
    total = 0
    for value in range(1, 65):
        base, _ = payout_for(decode_dice(value), 1, pays)
        total += base
    return total / 64.0


def reels_text(reels: tuple[str, str, str]) -> str:
    return " ".join(SYMBOL_EMOJI[s] for s in reels)


# ==================== 头奖池（自有表 + 行锁） ====================

def _pool_row(db: Session, *, lock: bool = False) -> models.SlotsPool:
    """取池单行（id=1），不存在时以种子额创建（flush 不 commit）。"""
    query = db.query(models.SlotsPool).filter(models.SlotsPool.id == 1)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        row = models.SlotsPool(
            id=1, balance=pool_seed(db), remainder_hundredths=0)
        db.add(row)
        db.flush()
    return row


def pool_balance(db: Session) -> int:
    """当前头奖池整数余额（只读展示口径：无行时即种子额）。"""
    row = db.query(models.SlotsPool).filter(models.SlotsPool.id == 1).first()
    return int(row.balance) if row is not None else pool_seed(db)


def _pool_add_hundredths(db: Session, hundredths: int) -> None:
    """池账加 hundredths（百分之一能量），整数位进 balance、余数留存。"""
    row = _pool_row(db, lock=True)
    total = int(row.balance) * 100 + int(row.remainder_hundredths) + int(hundredths)
    if total < 0:
        total = 0
    row.balance, row.remainder_hundredths = divmod(total, 100)
    row.updated_at = datetime.now()


# ==================== 资格（镜像抽奖口径，见模块 docstring） ====================

def check_eligibility(db: Session, user: models.WebUser) -> dict:
    """返回 {"ok": True} 或 {"ok": False, "reason": ...}。"""
    if not is_enabled(db):
        return {"ok": False, "reason": "disabled"}
    if not getattr(user, "is_active", True):
        return {"ok": False, "reason": "account_disabled"}
    from backend import subscriptions

    if not subscriptions.is_free_realm(db):
        return {"ok": False, "reason": "not_free_realm"}
    from backend import lottery

    if lottery.is_blacklisted(db, user.id):
        return {"ok": False, "reason": "blacklisted"}
    min_days = lottery.min_account_age_days(db)
    if min_days > 0:
        created = getattr(user, "created_at", None)
        if created is not None and (datetime.now() - created).days < min_days:
            return {"ok": False, "reason": "too_new", "min_days": min_days}
    return {"ok": True}


# ==================== 对局流程 ====================

def _today_bet_sum(db: Session, user_id: int) -> int:
    today = economy._today_start_utc_naive()
    total = db.query(func.coalesce(func.sum(models.SlotsSpin.bet), 0)).filter(
        models.SlotsSpin.user_id == user_id,
        models.SlotsSpin.status.in_(("pending", "settled")),
        models.SlotsSpin.created_at >= today,
    ).scalar() or 0
    return int(total)


def _today_base_win_sum(db: Session, user_id: int) -> int:
    today = economy._today_start_utc_naive()
    total = db.query(func.coalesce(func.sum(models.SlotsSpin.base_payout), 0)).filter(
        models.SlotsSpin.user_id == user_id,
        models.SlotsSpin.status == "settled",
        models.SlotsSpin.created_at >= today,
    ).scalar() or 0
    return int(total)


def _get_spin(db: Session, spin_key: str) -> models.SlotsSpin | None:
    return db.query(models.SlotsSpin).filter(
        models.SlotsSpin.spin_key == spin_key).first()


def _fail_spin_locked(db: Session, spin: models.SlotsSpin, reason: str,
                      origin: str = "private") -> None:
    """判失败并退注（调用方已持用户行锁、负责 commit）。重复调用不重复退。"""
    if spin.status != "pending":
        return
    user = db.query(models.WebUser).filter(
        models.WebUser.id == spin.user_id).first()
    if user is not None and spin.bet > 0:
        economy._add_points(
            db, user, int(spin.bet), "slots_refund",
            f"老虎机下注退回 {int(spin.bet)} 能量{_origin_suffix(origin)}",
            ref_id=f"slots:{spin.spin_key}")
    if spin.rake_hundredths:
        _pool_add_hundredths(db, -int(spin.rake_hundredths))
        spin.rake_hundredths = 0
    spin.status = "failed"
    spin.settled_at = datetime.now()
    logger.info("slots spin failed+refunded: key=%s reason=%s", spin.spin_key, reason)


def begin_spin(db: Session, user: models.WebUser, bet: int, spin_key: str,
               origin: str = "private") -> dict:
    """开局：校验 → 锁用户行 → 幂等/残局/封顶 → 建 spin → 原子扣注 → 池抽水 → commit。

    返回 {"ok": True, "spin": spin, "duplicate": bool} 或
    {"ok": False, "reason": ...}。duplicate 时 spin 为已存在的那一局
    （pending 表示同一回调重放且尚未结算，settled 表示已结算完）。
    """
    bet = int(bet)
    existing = _get_spin(db, spin_key)
    if existing is not None:
        return {"ok": True, "spin": existing, "duplicate": True}

    eligibility = check_eligibility(db, user)
    if not eligibility.get("ok"):
        return eligibility
    if bet not in bet_tiers(db):
        return {"ok": False, "reason": "bad_tier"}

    economy.lock_user_row(db, user.id)
    try:
        existing = _get_spin(db, spin_key)
        if existing is not None:
            db.rollback()
            return {"ok": True, "spin": existing, "duplicate": True}

        pending = (
            db.query(models.SlotsSpin)
            .filter(models.SlotsSpin.user_id == user.id,
                    models.SlotsSpin.status == "pending")
            .order_by(models.SlotsSpin.id.desc())
            .first()
        )
        if pending is not None:
            age = (datetime.now() - pending.created_at).total_seconds() \
                if pending.created_at else 0
            if age < STALE_SECONDS:
                db.rollback()
                return {"ok": False, "reason": "in_flight"}
            _fail_spin_locked(db, pending, "stale")

        cap = daily_bet_cap(db)
        if cap > 0 and _today_bet_sum(db, user.id) + bet > cap:
            db.rollback()
            return {"ok": False, "reason": "daily_bet_cap"}

        spin = models.SlotsSpin(
            spin_key=spin_key,
            user_id=user.id,
            bet=bet,
            status="pending",
            rake_hundredths=bet * pool_rake_pct(db),
        )
        db.add(spin)
        db.flush()
        try:
            economy._spend_points(
                db, user, bet, "slots_bet",
                f"老虎机下注 {bet} 能量{_origin_suffix(origin)}",
                ref_id=f"slots:{spin_key}")
        except economy.InsufficientPoints:
            db.rollback()
            return {"ok": False, "reason": "insufficient"}
        if spin.rake_hundredths:
            _pool_add_hundredths(db, int(spin.rake_hundredths))
        db.commit()
        db.refresh(spin)
        return {"ok": True, "spin": spin, "duplicate": False}
    except IntegrityError:
        # spin_key 唯一约束兜底（并发同 key）：回滚后按已存在局返回
        db.rollback()
        existing = _get_spin(db, spin_key)
        if existing is not None:
            return {"ok": True, "spin": existing, "duplicate": True}
        raise


def settle_spin(db: Session, spin: models.SlotsSpin, dice_value: int,
                origin: str = "private") -> dict:
    """结算：按 dice.value 解码赔付（基础赔付受每日赢分封顶，池奖不计）。

    已 settled 的 spin 重复结算只返回既有结果、不重复发奖。
    """
    if spin.status == "settled":
        return _settle_result(spin, duplicate=True)
    if spin.status != "pending":
        return {"ok": False, "reason": "settled"}

    economy.lock_user_row(db, spin.user_id)
    db.refresh(spin)
    if spin.status == "settled":
        db.rollback()
        return _settle_result(spin, duplicate=True)
    if spin.status != "pending":
        db.rollback()
        return {"ok": False, "reason": "settled"}

    reels = decode_dice(dice_value)
    base, is_jackpot = payout_for(reels, int(spin.bet), pay_table(db))

    cap = daily_win_cap(db)
    if cap > 0 and base > 0:
        base = max(0, min(base, cap - _today_base_win_sum(db, spin.user_id)))

    pool_payout = 0
    if is_jackpot:
        pool = _pool_row(db, lock=True)
        pool_payout = int(pool.balance)
        pool.balance = pool_seed(db)  # 抱走整池后重置种子，余数位保留
        pool.updated_at = datetime.now()

    spin.dice_value = int(dice_value)
    spin.reels = ",".join(reels)
    spin.base_payout = int(base)
    spin.pool_payout = int(pool_payout)
    spin.status = "settled"
    spin.settled_at = datetime.now()

    user = db.query(models.WebUser).filter(
        models.WebUser.id == spin.user_id).first()
    if user is not None:
        if base > 0:
            economy._add_points(
                db, user, int(base), "slots_win",
                f"老虎机中奖 +{int(base)} 能量{_origin_suffix(origin)}",
                ref_id=f"slots:{spin.spin_key}")
        if pool_payout > 0:
            economy._add_points(
                db, user, int(pool_payout), "slots_jackpot",
                f"老虎机头奖池 +{int(pool_payout)} 能量{_origin_suffix(origin)}",
                ref_id=f"slots:{spin.spin_key}")
    db.commit()
    db.refresh(spin)
    return _settle_result(spin, duplicate=False)


def _settle_result(spin: models.SlotsSpin, *, duplicate: bool) -> dict:
    reels = tuple(spin.reels.split(",")) if spin.reels else ()
    return {
        "ok": True,
        "spin": spin,
        "duplicate": duplicate,
        "reels": reels,
        "base_payout": int(spin.base_payout or 0),
        "pool_payout": int(spin.pool_payout or 0),
    }


def fail_spin(db: Session, spin: models.SlotsSpin, reason: str = "dice_failed",
              origin: str = "private") -> dict:
    """发送骰子失败等场景：判失败并全额退注（幂等）。"""
    if spin.status != "pending":
        return {"ok": True, "spin": spin, "duplicate": True}
    economy.lock_user_row(db, spin.user_id)
    db.refresh(spin)
    if spin.status != "pending":
        db.rollback()
        return {"ok": True, "spin": spin, "duplicate": True}
    _fail_spin_locked(db, spin, reason, origin=origin)
    db.commit()
    db.refresh(spin)
    return {"ok": True, "spin": spin, "duplicate": False}

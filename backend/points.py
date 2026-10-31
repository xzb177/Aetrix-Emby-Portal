# -*- coding: utf-8 -*-
"""公益服能量模块（模块2）

定位：公益服的"货币"层。复用 economy.py 的基础设施
（WebUser.points 余额 + PointsLog 流水 + _add_points 原子更新），
只提供公益服专用的业务逻辑：

- award_chat_points：发言奖励（每日上限）
- get_points_summary：能量概览
- redeem_welfare：能量兑换公益天数
- welfare_signin：【已废弃 2026-10-09】转发到 backend.api.economy._do_checkin_core，
  签到已统一到 POST /api/user/economy/checkin（配置在系统设置→每日签到）

SystemConfig 键（实际生效的签到键在 backend/api/economy._checkin_rules 读取，
管理后台「系统设置 → 每日签到」可配）：
- checkin_base_min / checkin_base_max（基础能量随机范围）
- checkin_streak_bonus（连签加成）
- points_chat_daily_cap=20, points_chat_per_msg=0.1（可为两位小数，整数
  余额/台账下走「小数累计」：按分累计、满 1 结算、余数跨日保留）
- points_redeem_7d=100, points_redeem_30d=300

兑换门槛（2026-10-30 用户亲点方案，管理后台「积分与公益配置 → 获取与兑换」可配）：
- welfare_redeem_self_earned_only=true：兑换资格只认自挣能量（见下方来源分类）
- welfare_redeem_min_account_age_days=3：自助兑换的注册满天数（0=关）
- welfare_redeem_min_active_days=2：自助兑换需近 7 天活跃天数（0=关）
只卡 redeem_welfare 这一个自助消费口；管理员发放（含后续机器人申请流批准）
与买查看行、转让、下注等其余消费一律不走这道门。
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.api import economy

logger = logging.getLogger(__name__)


# ==================== 能量来源分类（全仓唯一实现） ====================
# PointsLog.type → 是否「自挣」。自挣＝本人真实行为入账（签到、发言/影评）；
# 其余一律非自挣：转入、邀请类、红包、卡码、充值/退款、管理员发放、抽奖、
# 老虎机赢分/退注、观影榜活动发分。未知 type 按非自挣处理（fail-closed：
# 新增入账来源必须先来这里登记分类，才可能被认作自挣）。
#
# 口径说明：
# - 站内影评与 TG 发言共用 award_chat_points 入账，type 同为 "chat"。
# - 观影本身不直接产生能量入账（观影奖励走活力值，不进能量台账）；
#   rankpush 是观影榜活动奖品（名次发分），按活动口径计非自挣。
# - 回款类（slots_refund、红包过期退回、订单 refund）不是行为入账，
#   不恢复自挣额——与「支出先扣自挣」的公式口径一致（见 self_earned_balance）。
# - 支出类 type（redeem/view_unlock/...）分类无实际作用（公式只对正额入账
#   按分类汇总），列全是为了让覆盖测试能逐一对账。
POINT_TYPE_SELF_EARNED: dict[str, bool] = {
    # 自挣（本人行为）
    "checkin": True,
    "chat": True,
    # 非自挣入账
    "transfer_in": False,
    "invitee": False,
    "rebate": False,
    "promotion": False,
    "redpacket": False,
    "code_redeem": False,
    "exchange": False,
    "recharge": False,
    "refund": False,
    "admin_grant": False,
    "lottery_win": False,
    "slots_win": False,
    "slots_jackpot": False,
    "slots_refund": False,
    "rankpush": False,
    # 支出（分类仅作登记，公式不消费）
    "redeem": False,
    "view_unlock": False,
    "vitality_recharge": False,
    "subscription_buy": False,
    "subscription_auto_renew": False,
    "transfer_out": False,
    "transfer_fee": False,
    "slots_bet": False,
    "admin_deduct": False,
    "redpacket_fee": False,
}

SELF_EARNED_POINT_TYPES = frozenset(
    t for t, earned in POINT_TYPE_SELF_EARNED.items() if earned
)


def is_self_earned_point_type(type_: str) -> bool:
    """某个 PointsLog.type 是否算自挣能量（未登记的一律 False）。"""
    return POINT_TYPE_SELF_EARNED.get(type_, False)


def self_earned_balance(db: Session, user: models.WebUser) -> int:
    """用户当前余额中可归于「自挣」的部分（台账纯函数，不新增余额字段）。

    公式（2026-10-30 定版，存量用户上线即按此口径折算、无特殊过渡分支）：
        max(0, min(当前余额, 自挣类累计入账 − 累计全部支出))
    即：花钱先花自挣挣来的部分。这样富余余额里混着多少转入/赠送，
    都不会抬高可兑换资格的额度；正常自挣自花的用户则恒等于其余额
    （自挣入账 − 支出 = 余额，当且仅当没有非自挣入账时）。
    """
    self_inflow = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user.id,
        models.PointsLog.amount > 0,
        models.PointsLog.type.in_(SELF_EARNED_POINT_TYPES),
    ).scalar() or 0
    total_spent = -(db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user.id,
        models.PointsLog.amount < 0,
    ).scalar() or 0)
    balance = int(
        db.query(models.WebUser.points).filter(models.WebUser.id == user.id).scalar() or 0
    )
    return max(0, min(balance, int(self_inflow) - int(total_spent)))


def _recent_active_days(db: Session, user_id: int, window_days: int = 7) -> int:
    """近 window_days 个自然日（含今天，上海日界）内的活跃天数。

    硬信号（签到 CheckinRecord、播放 PlaybackSession）出现的天数直接计数；
    发言（chat 类入账）出现的天数最多只计 1 天——发言是圈外主攒分通道，
    若全额计数，水发言即可刷满活跃门，等于没加门（方案 A2 口径）。
    签到对非公益用户同样落记录（奖励 0），故新人可用签到攒活跃天数。
    """
    today_start = economy._today_start_utc_naive()
    window_start = today_start - timedelta(days=window_days - 1)

    def _bucket(ts) -> int:
        return (ts - window_start).days

    hard_days: set[int] = set()
    for (ts,) in db.query(models.CheckinRecord.checkin_date).filter(
        models.CheckinRecord.user_id == user_id,
        models.CheckinRecord.checkin_date >= window_start,
    ).all():
        if ts:
            hard_days.add(_bucket(ts))
    from backend.emby_server import models as em_models
    for (ts,) in db.query(em_models.PlaybackSession.start_time).filter(
        em_models.PlaybackSession.user_id == user_id,
        em_models.PlaybackSession.start_time >= window_start,
    ).all():
        if ts:
            hard_days.add(_bucket(ts))
    chat_days: set[int] = set()
    for (ts,) in db.query(models.PointsLog.created_at).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == "chat",
        models.PointsLog.amount > 0,
        models.PointsLog.created_at >= window_start,
    ).all():
        if ts:
            chat_days.add(_bucket(ts))
    return len(hard_days) + (1 if chat_days - hard_days else 0)


def _get_int(db: Session, key: str, default: int) -> int:
    """读整数配置"""
    return economy._get_int_config(db, key, default)


# ==================== 发言奖励小数累计（2026-10-31） ====================
# WebUser.points 与 PointsLog.amount 都是 Integer，全链整数、不迁列类型。
# 每条奖励可为小数（points_chat_per_msg 默认 0.1）：按「分」（0.01 能量）
# 为最小单位累计到 WebUser.chat_frac_cents，满 100 分结算 1 能量进余额
# 并写台账；余数跨日保留不清零。当日封顶按 WebUser.chat_day_cents
# （当日已累计、含已结算部分）判定，小数累计部分同样占额度、不许绕开。
_CHAT_PER_MSG_DEFAULT = Decimal("0.1")
_CENT = 100  # 1 能量 = 100 分


def _get_decimal(db: Session, key: str, default: Decimal) -> Decimal:
    """读小数配置（按原始字符串解析 Decimal，不经 float 中转）。"""
    raw = economy._get_config(db, key, str(default))
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return default


def _chat_today_str() -> str:
    """上海自然日日期串（与 chat_day 落库口径一致）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _settled_chat_today_cents(db: Session, user_id: int) -> int:
    """今日已结算进台账的发言能量（分）：PointsLog chat 正额合计 ×100。"""
    today = economy._today_start_utc_naive()  # r34：上海自然日（UTC 入库帧）
    settled = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == "chat",
        models.PointsLog.amount > 0,
        models.PointsLog.created_at >= today,
    ).scalar() or 0
    return int(settled) * _CENT


def _chat_day_cents(db: Session, user: models.WebUser) -> int:
    """今日已累计发言能量（分，含未结算小数）。

    chat_day 是今日 → 直接读 chat_day_cents；日期不符（跨日未刷新、或
    升级前存量行 chat_day 为 NULL）→ 以今日已结算流水折算：升级当天
    旧口径已发的整数能量照常占今日额度，不会因切换重复发放。
    """
    if getattr(user, "chat_day", None) == _chat_today_str():
        return int(user.chat_day_cents or 0)
    return _settled_chat_today_cents(db, user.id)


def award_chat_points(db: Session, user_id: int) -> int:
    """发言奖励（供聊天/评论模块调用）

    每条发言奖励 points_chat_per_msg 能量（可为小数，默认 0.1），每日上限
    points_chat_daily_cap 能量（按当日累计口径、含未结算小数判定）。
    小数部分按分累计、满 1 能量结算进余额；返回本次实际结算的整数能量
    （未满 1 时为 0）。达到上限后返回 0，不再奖励。
    """
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is None:
        return 0

    cap = _get_int(db, "points_chat_daily_cap", 20)
    if cap < 0:
        # R18-P3：与 per_msg 同口径——负数记日志回落默认 20。旧实现 earned_today>=cap
        # 恒成立（earned_today>=0 > 负 cap），奖励被静默永久关闭且无日志，配错了都不知道。
        logger.warning("points_chat_daily_cap 配置为负数 %s，已回落默认值 20", cap)
        cap = 20
    per_msg = _get_decimal(db, "points_chat_per_msg", _CHAT_PER_MSG_DEFAULT)
    # P2 修复（完美审查 B-P2-5）：负配置会反向扣分——非法值记日志回落默认；
    # 0 视为关闭奖励，直接返回。
    if per_msg < 0:
        logger.warning("points_chat_per_msg 配置为负数 %s，已回落默认值 %s",
                       per_msg, _CHAT_PER_MSG_DEFAULT)
        per_msg = _CHAT_PER_MSG_DEFAULT
    if per_msg == 0:
        # R18-P3：奖励关闭的早退移到行锁 + SUM 查询之前——读配置不需要锁，
        # 每条聊天消息白拿一次 FOR UPDATE 行锁是纯浪费。
        return 0
    per_msg_cents = int(
        (per_msg * _CENT).to_integral_value(rounding=ROUND_HALF_UP)
    )
    if per_msg_cents <= 0:
        # 管理端校验已钳到 2 位小数；直改库写了 0.001 这类不足 0.01 的值时
        # 按分取整为 0，记日志按关闭处理，不静默吞掉。
        logger.warning("points_chat_per_msg=%s 不足 0.01 能量，按关闭处理", per_msg)
        return 0

    # P2 修复（审查）：每日上限是"先 SUM 后判"，并发两请求都读到同一旧用量、
    # 都判定未超限 → 超发。先锁用户行再统计（与红包频率检查同口径）。
    economy.lock_user_row(db, user_id)
    # 锁后刷新：等锁期间他人刚提交的累计值要读到最新，否则余数会被旧值覆盖。
    db.refresh(user)

    day_cents = _chat_day_cents(db, user)
    cap_cents = cap * _CENT
    if day_cents >= cap_cents:
        # P2 修复（完美审查 B-P2-4）：上限分支也要先 rollback 再返回——
        # 否则行锁持有到请求结束，阻塞同用户并发写入。
        db.rollback()
        return 0

    # 避免单次奖励超过上限（按分补到上限为止，与旧整数口径同型）
    grant_cents = min(per_msg_cents, cap_cents - day_cents)
    total_frac = int(user.chat_frac_cents or 0) + grant_cents
    settle, frac = divmod(total_frac, _CENT)
    user.chat_frac_cents = frac
    user.chat_day = _chat_today_str()
    user.chat_day_cents = day_cents + grant_cents
    if settle:
        economy._add_points(db, user, settle, "chat", "发言奖励")
    db.commit()
    return settle


def get_chat_points_status(db: Session, user_id: int) -> dict:
    """发言能量只读状态（供 TG /chatpoints 等自助查询）。

    口径与 award_chat_points 完全一致：今日已得 = 当日累计发言能量（含
    未结算小数，chat_day_cents 口径；日期不符时以今日已结算流水折算）；
    上限/每条取 points_chat_daily_cap / points_chat_per_msg，负数回落默认
    （与发分侧同口径，避免查询显示与实际发分对不上）。只读，不落库。
    earned_today / per_msg 可为小数（float）；total_earned 只计已结算
    进台账的整数能量（小数余数未结算前不计入，与余额口径一致）。
    """
    cap = _get_int(db, "points_chat_daily_cap", 20)
    if cap < 0:
        logger.warning("points_chat_daily_cap 配置为负数 %s，已回落默认值 20", cap)
        cap = 20
    per_msg = _get_decimal(db, "points_chat_per_msg", _CHAT_PER_MSG_DEFAULT)
    if per_msg < 0:
        logger.warning("points_chat_per_msg 配置为负数 %s，已回落默认值 %s",
                       per_msg, _CHAT_PER_MSG_DEFAULT)
        per_msg = _CHAT_PER_MSG_DEFAULT

    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    if user is not None:
        earned_cents = _chat_day_cents(db, user)
    else:
        earned_cents = _settled_chat_today_cents(db, user_id)

    total_earned = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == "chat",
        models.PointsLog.amount > 0,
    ).scalar() or 0

    return {
        "earned_today": earned_cents / _CENT,
        "daily_cap": int(cap),
        "per_msg": float(per_msg),
        "total_earned": int(total_earned),
    }


def get_points_summary(db: Session, user: models.WebUser) -> dict:
    """能量概览：余额 / 今日获得 / 累计获得"""
    balance = int(user.points or 0)
    today = economy._today_start_utc_naive()  # r34：上海自然日（UTC 入库帧），此前服务器本地口径在北京时间 08:00 才重置

    today_earned = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user.id,
        models.PointsLog.amount > 0,
        models.PointsLog.created_at >= today,
    ).scalar() or 0

    total_earned = db.query(func.coalesce(func.sum(models.PointsLog.amount), 0)).filter(
        models.PointsLog.user_id == user.id,
        models.PointsLog.amount > 0,
    ).scalar() or 0

    return {
        "balance": balance,
        "today_earned": int(today_earned),
        "total_earned": int(total_earned),
    }


def get_welfare_status(db: Session, user: models.WebUser) -> dict:
    """当前用户福利资格状态（只读本人行）。

    无 user_id 参数，调用方只能传已鉴权的当前用户对象，无法越权看他人。
    welfare_expires_at 为空时原样返回 null：公益用户为空表示不过期，
    非公益用户为空表示未开通，两者以 is_welfare 区分（与现有代码口径一致）。
    """
    row = db.query(models.WebUser).filter(models.WebUser.id == user.id).first()
    if row is None:
        return {"is_welfare": False, "welfare_expires_at": None}
    exp = getattr(row, "welfare_expires_at", None)
    return {
        "is_welfare": bool(getattr(row, "is_welfare", False)),
        "welfare_expires_at": exp.isoformat() if exp else None,
    }


def redeem_welfare(db: Session, user: models.WebUser, days_option: int) -> dict:
    """能量兑换公益天数

    days_option: 只能是 7 或 30
    流程：过兑换门槛（来源限定＋双钥匙）→ 原子扣能量 → portal.grant_welfare
    注意：grant_welfare 内部会 db.commit()，所以扣能量后不再单独 commit

    门槛（2026-10-30 定版，只卡这个自助口）：
    1. 来源限定（welfare_redeem_self_earned_only，默认开）：花费额必须能被
       自挣可用额覆盖（见 self_earned_balance），转入/赠送/中奖的能量不能
       用来兑换资格——掐断「A 攒分供 B 开号」与小号矩阵变现。
    2. 号龄（welfare_redeem_min_account_age_days，默认 3，0=关）。
    3. 活跃（welfare_redeem_min_active_days，默认 2，0=关）：近 7 天活跃
       天数，口径见 _recent_active_days。
    判定全程持用户行锁，与随后的原子扣减同一事务：否则并发两笔兑换可
    各自通过自挣额判定、合计兑出超过自挣额的天数（总余额原子扣只防
    余额双花，防不住归属双花）。不满足时明确报错差什么，不静默拒绝；
    管理员发放是豁免通道，不经过这里。
    """
    if days_option not in (7, 30):
        raise ValueError("仅支持兑换 7 天或 30 天")

    cost_key = "points_redeem_7d" if days_option == 7 else "points_redeem_30d"
    default_cost = 100 if days_option == 7 else 300
    cost = _get_int(db, cost_key, default_cost)

    economy.lock_user_row(db, user.id)
    try:
        balance = int(
            db.query(models.WebUser.points).filter(
                models.WebUser.id == user.id).scalar() or 0
        )
        if balance < cost:
            raise ValueError(f"能量不足，需要 {cost} 能量，当前 {balance} 能量")

        if economy._get_bool_config(db, "welfare_redeem_self_earned_only", True):
            earned = self_earned_balance(db, user)
            if earned < cost:
                raise ValueError(
                    f"自挣能量不足：兑换公益资格只能使用自己挣的能量"
                    f"（签到、发言等），当前自挣能量 {earned}，需要 {cost}；"
                    f"转入、赠送、中奖的能量不能用于兑换资格"
                )

        min_age_days = max(
            0, _get_int(db, "welfare_redeem_min_account_age_days", 3))
        if min_age_days > 0:
            created_at = user.created_at
            age_days = (datetime.now() - created_at).days if created_at else 0
            if age_days < min_age_days:
                raise ValueError(
                    f"账号注册未满 {min_age_days} 天（当前 {age_days} 天，"
                    f"还差 {min_age_days - age_days} 天），暂时不能自助兑换"
                    f"公益资格；可联系管理员发放"
                )

        min_active_days = min(
            7, max(0, _get_int(db, "welfare_redeem_min_active_days", 2)))
        if min_active_days > 0:
            active_days = _recent_active_days(db, user.id)
            if active_days < min_active_days:
                raise ValueError(
                    f"近 7 天活跃天数不足（当前 {active_days} 天，"
                    f"需要 {min_active_days} 天；签到、播放算活跃，"
                    f"发言最多计 1 天），暂时不能自助兑换公益资格；"
                    f"可联系管理员发放"
                )
    except ValueError:
        db.rollback()  # 释放行锁（与 award_chat_points 的回滚口径一致）
        raise

    # P1 修复（审查）：「先读余额判断、再 _add_points 扣」是读-改-写双花。
    # _spend_points 把「够不够」放进 UPDATE 的 WHERE 由数据库原子判定。
    from backend.api.economy import _spend_points, InsufficientPoints
    try:
        new_balance = _spend_points(
            db, user, cost, "redeem", f"能量兑换公益{days_option}天"
        )
    except InsufficientPoints as exc:
        raise ValueError(str(exc)) from exc

    # 再开通（内部 commit）
    from backend.emby_server import portal
    expires_at = portal.grant_welfare(
        db, user, channel="points_redeem", days=days_option
    )

    return {
        "days": days_option,
        "cost": cost,
        "balance": new_balance,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def welfare_signin(db: Session, user: models.WebUser) -> dict:
    """【已废弃 2026-10-09】公益服签到已统一到 backend.api.economy._do_checkin_core。

    此函数仅为兼容保留（POST /api/points/signin 无人调用），转发到统一核心。
    新代码请直接调用 economy._do_checkin_core。

    返回：{'points': 实际得分, 'streak': 连签天数,
           'balance': 余额, 'penalty': 是否触发惩罚}
    """
    logger.warning(
        "deprecated backend.points.welfare_signin called for user %s; "
        "use backend.api.economy._do_checkin_core instead", user.id,
    )
    from backend.api.economy import _do_checkin_core
    result = _do_checkin_core(db, user)
    return {
        "points": result["points_awarded"],
        "streak": result["streak"],
        "balance": result["balance"],
        "penalty": result.get("penalty", False),
    }

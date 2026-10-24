"""卡码体系：注册码 / 续期码 / 白名单码 / 诱饵码 / 指名码

借鉴 twilight-kotomi 的 RegCode 模型：一份数据结构靠 ``code_type`` 区分用途，
``days`` 表示授予或叠加的会员天数，诱饵码（蜜罐）靠 ``HONEY-`` 码前缀识别
（**零数据库表结构改动**：registration_codes 表不加列，只看码字符串本身），
``target_username`` 为指名码。

本项目与参考实现的差异：会员口径以 ``UserSubscription(end_date)`` 为**单一事实来源**
（v2.5.1 起 is_vip / 付费墙 / 后台订阅总览都按它现算），因此卡码的「天数」最终落到订阅上：
无生效订阅则新建一条，有则在其到期时间上叠加。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger(__name__)

# 卡码类型（对齐参考项目 type 1/2/3）
CODE_TYPE_REGISTER = 1  # 注册码：为尚无会员的账号开通会员
CODE_TYPE_RENEW = 2  # 续期码：在原有到期时间上叠加
CODE_TYPE_WHITELIST = 3  # 白名单码：置为长期有效
CODE_TYPE_NAMES = {
    CODE_TYPE_REGISTER: "注册码",
    CODE_TYPE_RENEW: "续期码",
    CODE_TYPE_WHITELIST: "白名单码",
}
CODE_TYPE_PREFIX = {CODE_TYPE_REGISTER: "REG", CODE_TYPE_RENEW: "REN", CODE_TYPE_WHITELIST: "VIP"}

# 诱饵码（蜜罐）标识：码字符串以 "HONEY-" 开头即为诱饵码。
# 设计原则：零 DB 改动——registration_codes 表不加列，识别只看码本身
# （见 is_honeypot）；创建时由 render_code(decoy=True) 生成 HONEY- 前缀。
HONEY_PREFIX = "HONEY"
HONEY_MARK = "HONEY-"


def is_honeypot(raw) -> bool:
    """是否为诱饵码（蜜罐）

    接受 RegistrationCode 对象或原始码字符串：以 ``HONEY-``（大小写不敏感）
    开头的都是诱饵码。使用它的人会被自动封禁，兑换入口统一返回「卡码无效」，
    绝不暴露它是陷阱。
    """
    code = getattr(raw, "code", raw)
    return bool(code) and str(code).strip().upper().startswith(HONEY_MARK)


def discount_pct_text(pct) -> str:
    """实付百分比 → 中文折扣文案：85 → "8.5折"，90 → "9折"。

    discount_pct 在全站是「实付百分比」口径（85=八五折，实付 85%），不是「折扣百分比」。
    以前各处写 f"{pct}折"（→「85折」）或 f"{100 - pct}折"（→「15折」）都是错的，
    用户可见折扣文案统一走本函数（横切能力只许一套）。
    钳制到 1..99，与 economy._exchange_discount_amount 的 max(1, min(99, ...)) 同口径。
    """
    pct = max(1, min(99, int(pct or 0)))
    return f"{pct / 10:g}折"

PERMANENT_DAYS = 36500  # 永久近似（100 年）
DEFAULT_DAYS = 30
MAX_RANDOM_LEN = 40

# 易抄写字符集（去掉 0/1/I/O 等易混淆字符），与参考项目 base32-* 一致
_SAFE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_HEX_ALPHABET = "0123456789ABCDEF"
_URLSAFE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

RANDOM_ALGORITHMS = {
    "base32-16": (_SAFE_ALPHABET, 16),
    "base32-20": (_SAFE_ALPHABET, 20),
    "base32-24": (_SAFE_ALPHABET, 24),
    "base32-32": (_SAFE_ALPHABET, 32),
    "hex20": (_HEX_ALPHABET, 20),
    "hex32": (_HEX_ALPHABET, 32),
    "digits-12": ("0123456789", 12),
    "digits-16": ("0123456789", 16),
    "urlsafe-24": (_URLSAFE_ALPHABET, 24),
    "urlsafe-32": (_URLSAFE_ALPHABET, 32),
}
DEFAULT_ALGORITHM = "base32-20"
DEFAULT_FORMAT = "{type}-{random}"


def normalize_days(days: Optional[int]) -> int:
    """天数规范化：0（未填）按 30 天；负数统一为 -1（永久）"""
    if days is None:
        return DEFAULT_DAYS
    days = int(days)
    if days == 0:
        return DEFAULT_DAYS
    return -1 if days < 0 else days


def format_days(days: Optional[int]) -> str:
    """给前端展示的天数文案"""
    days = normalize_days(days)
    return "永久" if days < 0 else f"{days} 天"


def random_part(algorithm: str = DEFAULT_ALGORITHM) -> str:
    alphabet, length = RANDOM_ALGORITHMS.get(algorithm, RANDOM_ALGORITHMS[DEFAULT_ALGORITHM])
    return "".join(secrets.choice(alphabet) for _ in range(length))


def render_code(
    *,
    code_type: int,
    days: int,
    index: int = 1,
    template: str = DEFAULT_FORMAT,
    algorithm: str = DEFAULT_ALGORITHM,
    decoy: bool = False,
) -> str:
    """按模板生成卡码

    支持占位符 ``{random}`` / ``{type}`` / ``{days}`` / ``{index}``；
    模板不含 ``{random}`` 时自动追加 ``-{random}``，避免批量生成出重复码。

    ``decoy=True`` 时生成 ``HONEY-<random>``（诱饵码）：用前缀标识身份，
    不占数据库列——这就是蜜罐的「零表结构改动」方案。
    """
    template = (template or DEFAULT_FORMAT).strip() or DEFAULT_FORMAT
    code = (
        template.replace("{type}", CODE_TYPE_PREFIX.get(code_type, "REG"))
        .replace("{days}", "PERM" if days < 0 else str(days))
        .replace("{index}", str(index))
    )
    if decoy:
        # 诱饵码强制 HONEY- 前缀：不走模板，避免自定义模板把标识吞掉
        code = f"{HONEY_MARK}{random_part(algorithm)}"
    elif "{random}" in template:
        code = code.replace("{random}", random_part(algorithm))
    else:
        code = f"{code}-{random_part(algorithm)}"
    return code[:64].upper()


# ==================== 会员天数授予 ====================


def code_realm_id(db: Session, code: Optional[models.RegistrationCode] = None) -> int:
    """这张卡码开通/续期的是**哪个服**的会员

    卡码是一个服一个的：乙服的注册码只能开通乙服的会员，不能在甲服播放。
    老数据（升级前生成、``realm_id`` 未标注）落到当前服——与全站 ``claim()``
    的口径一致，单服部署下就是那个唯一的服，行为与以前完全相同。
    """
    from backend import realms

    if code is not None and getattr(code, "realm_id", None):
        return int(code.realm_id)
    return realms.claim(db, None)


def realm_label(db: Session, realm_id: Optional[int]) -> str:
    """服名（给提示文案用；查不到时给个能指认的写法）"""
    from backend import realms

    realm = realms.get_realm(db, realm_id)
    return realm.name if realm else f"#{realm_id}"


def _resolve_plan(db: Session, realm_id: Optional[int] = None):
    """卡码授予天数所用套餐

    优先配置 ``code_default_plan_id``（只在该套餐确实属于这个服时采用，
    否则乙服的卡码会拿甲服的套餐开会员），其次**这个服**启用中的套餐，
    最后回落到该服隐藏的「卡码开通」套餐（is_active=False，不在商店出现）。
    """
    from backend import realms

    target = realms.claim(db, realm_id)
    config = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "code_default_plan_id"
    ).first()
    if config and str(config.value or "").strip().isdigit():
        plan = db.query(models.SubscriptionPlan).filter(
            models.SubscriptionPlan.id == int(str(config.value).strip())
        ).first()
        if plan and (plan.realm_id is None or int(plan.realm_id) == target):
            return plan

    plan = (
        realms.scope(db.query(models.SubscriptionPlan), models.SubscriptionPlan.realm_id, target)
        .filter(models.SubscriptionPlan.is_active == True)  # noqa: E712
        .order_by(models.SubscriptionPlan.sort_order)
        .first()
    )
    if plan:
        return plan

    plan = db.query(models.SubscriptionPlan).filter(
        models.SubscriptionPlan.name == "卡码开通",
        models.SubscriptionPlan.realm_id == target,
    ).first()
    if not plan:
        plan = models.SubscriptionPlan(
            name="卡码开通", description="卡码开通/续期自动创建（不在商店展示）",
            price=0, duration_days=DEFAULT_DAYS, is_active=False, sort_order=999,
            realm_id=target,
        )
        db.add(plan)
        # 不提交：这一段的调用方（卡码核销 / 注册）稍后与「发天数 + 记消耗」一起提交，
        # 建计划与开会员必须同一个事务，否则会出现「计划建好了、会员没开成」的中间态
        db.flush()
    return plan


def grant_membership_days(db: Session, user: models.WebUser, days: int,
                          realm_id: Optional[int] = None) -> models.UserSubscription:
    """按天数开通或延长**某个服**的会员，返回生效中的订阅

    会员是一个服一个：续期只在**同一个服**里叠加（乙服的续期码不该延长甲服的会员），
    新建时把归属服写进 ``UserSubscription.realm_id``——付费墙是按服严格匹配的，
    写空的话这张卡码开的会员在 EA 上根本放不了。

    ``realm_id`` 为空时归当前服（``realms.claim``）：单服部署下就是原来那个唯一的服。

    `with_for_update()` 在 PostgreSQL 下锁住该订阅行，避免同一用户并发叠加天数时
    两边读到同一到期时间、后提交的覆盖前者（丢天数）；SQLite 忽略该子句。

    **不提交**：调用方必须把「记卡码消耗」和「发天数」放在同一个事务里提交。
    旧实现在这里就 commit（``consume`` 里再 commit 一次），于是注册流程里两次提交之间
    崩一次就成了「码已经烧掉、会员没到账」，反过来顺序则可能是「会员到账、码还能再用」
    ——两阶段提交不一致（见 docs/performance.md）。
    """
    from backend import realms

    total = PERMANENT_DAYS if days < 0 else max(int(days), 1)
    now = datetime.now()
    target = realms.claim(db, realm_id)
    sub = (
        db.query(models.UserSubscription)
        .filter(
            models.UserSubscription.user_id == user.id,
            models.UserSubscription.realm_id == target,
            models.UserSubscription.status == "active",
            models.UserSubscription.end_date > now,
        )
        .order_by(models.UserSubscription.end_date.desc())
        .with_for_update()
        .first()
    )
    if sub:
        # 同服已有生效订阅：叠加（续期语义），而非覆盖
        sub.end_date = sub.end_date + timedelta(days=total)
        sub.status = "active"
    else:
        plan = _resolve_plan(db, target)
        sub = models.UserSubscription(
            user_id=user.id, plan_id=plan.id, start_date=now,
            end_date=now + timedelta(days=total), status="active",
            realm_id=target,
        )
        db.add(sub)
    db.flush()   # 拿主键；提交由调用方负责（与 economy._grant_subscription 同一口径）
    # 订阅分档权益（PR-1）：本函数是卡码核销/邀请奖励发天的发放口（不走
    # _grant_subscription），同样要清该用户的权益解析缓存（见
    # backend/entitlements.py；跨进程靠 60 秒 TTL 兜底）。
    from backend import entitlements as _entitlements

    _entitlements.invalidate(user_id=user.id)
    return sub


def has_active_membership(db: Session, user: models.WebUser, realm_id: Optional[int] = None) -> bool:
    """用户**在这个服**是否已有生效会员

    口径与付费墙完全一致（``subscriptions.has_active_subscription``）：甲服的会员
    不算乙服的会员，否则乙服的注册码会被误判成「已有会员」而拒绝。
    """
    from backend import subscriptions

    return subscriptions.has_active_subscription(db, user.id, realm_id)


# ==================== 卡码识别与核销 ====================


def find_reg_code(db: Session, raw: str) -> Optional[models.RegistrationCode]:
    if not raw:
        return None
    return (
        db.query(models.RegistrationCode)
        .filter(func.upper(models.RegistrationCode.code) == raw.strip().upper())
        .first()
    )


REDEMPTION_KIND_REG = "reg"
REDEMPTION_KIND_EXCHANGE = "exchange"


def user_already_redeemed(db: Session, kind: str, code, user_id: int) -> bool:
    """这个用户是否已经用过这张码（H5）：核销记录表 + 升级前留在 ``used_by`` 里的审计"""
    if user_id is None:
        return False
    hit = db.query(models.CodeRedemption.id).filter(
        models.CodeRedemption.code_kind == kind,
        models.CodeRedemption.code_id == code.id,
        models.CodeRedemption.user_id == user_id,
    ).first()
    if hit is not None:
        return True
    used = {i.strip() for i in str(getattr(code, "used_by", "") or "").split(",") if i.strip()}
    return str(user_id) in used


def record_redemption(db: Session, kind: str, code_id: int, user_id: int) -> bool:
    """在当前事务里登记「这个人用过这张码」；唯一约束冲突（已用过 / 并发重复提交）返回 False

    冲突时会 ``rollback`` 整个事务（SQLite 的 SAVEPOINT 不可靠，不做嵌套事务）；
    调用方拿到 False 必须直接拒绝，不要再继续写。
    """
    from sqlalchemy.exc import IntegrityError

    db.add(models.CodeRedemption(code_kind=kind, code_id=code_id, user_id=user_id))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    return True


def claim_code(db: Session, code: models.RegistrationCode, user_id: int) -> bool:
    """**原子占位**一次卡码消耗：抢不到（已停用 / 过期 / 用尽）返回 False

    「先读 use_count 判断、再写 use_count + 1」是读-改-写：同一张 ``max_uses=1`` 的卡码
    被两个请求同时提交时，两边都读到 ``use_count=0``，于是各发一份会员天数（重复核销）。
    这里改成条件 ``UPDATE``：只有「仍然可用」的那一行会被 +1，并发下的第二个请求拿到
    ``rowcount=0``，直接拒绝（与 ``economy.redeem_exchange_code`` 同一套写法）。

    条件里把「是否可用」表达完整，所以它同时也是可用性判定：
    停用 / 过期 / 用尽的卡码都进不了 WHERE。**不提交**——消耗与发奖必须同一个事务
    （见 ``grant_membership_days``），调用方提交前崩溃时两者一起回滚，
    不会留下「码烧了、会员没到账」或「会员到账、码还能再用」。
    """
    # H5：同一个人同一张码最多一次——先在本事务里登记（唯一约束），冲突即拒绝
    if user_already_redeemed(db, REDEMPTION_KIND_REG, code, user_id):
        return False
    if not record_redemption(db, REDEMPTION_KIND_REG, code.id, user_id):
        return False
    now = datetime.now()
    claimed = (
        db.query(models.RegistrationCode)
        .filter(
            models.RegistrationCode.id == code.id,
            models.RegistrationCode.is_active.is_(True),
            or_(models.RegistrationCode.expires_at.is_(None),
                models.RegistrationCode.expires_at > now),
            or_(models.RegistrationCode.max_uses.is_(None),
                models.RegistrationCode.use_count < models.RegistrationCode.max_uses),
        )
        .update({models.RegistrationCode.use_count:
                 func.coalesce(models.RegistrationCode.use_count, 0) + 1},
                synchronize_session=False)
    )
    if not claimed:
        return False
    # 同一事务里补上审计字段与「用满自动停用」；读的是本事务刚 +1 后的值
    code.use_count = (code.use_count or 0) + 1
    used = [i for i in str(code.used_by or "").split(",") if i.strip()]
    used.append(str(user_id))
    code.used_by = ",".join(dict.fromkeys(used))[:500]
    if code.max_uses and code.use_count >= code.max_uses:
        code.is_active = False
    return True


def grant_days_for(code: models.RegistrationCode) -> int:
    """该卡码应授予的天数：白名单码一律长期有效，其余取 days"""
    if code.code_type == CODE_TYPE_WHITELIST:
        return -1
    return normalize_days(code.days)


# 核销失败机器码（error_code）：随失败结果一起返回，供端点/前端区分
# 「码本身明确无效」与限流、系统错误。用户侧文案仍走 message/detail 人话。
CODE_ERROR_USED_UP = "CODE_USED_UP"        # 已被用尽 / 并发抢占失败（已被别人使用）
CODE_ERROR_DISABLED = "CODE_DISABLED"      # 已停用
CODE_ERROR_EXPIRED = "CODE_EXPIRED"          # 已过期
CODE_ERROR_NOT_FOUND = "CODE_NOT_FOUND"      # 不存在
CODE_ERROR_SELF_USED = "CODE_SELF_USED"      # 自己已使用过（每个账号每张码限用一次）
CODE_ERROR_NAMED_OTHER = "CODE_NAMED_OTHER"  # 指名码，限指定账号使用

# 「明确无效」集合：命中这些状态的尝试，再试多少次答案都一样，与限流无关。
# 端点层对这类结果直接返回业务文案，不许用 429 把真原因盖掉（见 api/user.py）。
CLEARLY_INVALID_CODE_ERRORS = frozenset({
    CODE_ERROR_USED_UP, CODE_ERROR_DISABLED, CODE_ERROR_EXPIRED,
    CODE_ERROR_NOT_FOUND, CODE_ERROR_SELF_USED, CODE_ERROR_NAMED_OTHER,
})

# 已用尽 / 已被使用的统一用户文案。旧文案有「卡码已用尽」（预检/reg_code_error）
# 与「卡码已被使用」（并发抢占失败）两种说法，用户分不清是不是一回事，统一到这句。
CODE_USED_UP_MESSAGE = "该注册码已被使用"


def reg_code_error(code: models.RegistrationCode) -> Optional[str]:
    """卡码自身可用性检查（不论使用者）"""
    if not code.is_active:
        return "卡码已停用"
    if code.expires_at and code.expires_at < datetime.now():
        return "卡码已过期"
    if code.max_uses and code.use_count >= code.max_uses:
        return CODE_USED_UP_MESSAGE
    return None


def reg_code_error_code(code: models.RegistrationCode) -> Optional[str]:
    """reg_code_error 的机器码版本（判定顺序必须与 reg_code_error 一致：停用→过期→用尽）"""
    if not code.is_active:
        return CODE_ERROR_DISABLED
    if code.expires_at and code.expires_at < datetime.now():
        return CODE_ERROR_EXPIRED
    if code.max_uses and code.use_count >= code.max_uses:
        return CODE_ERROR_USED_UP
    return None


def preview_code(db: Session, raw: str, username: Optional[str] = None) -> dict:
    """统一识别卡码 / 邀请码 / 兑换码 / 优惠券，供前端唯一那个核销入口预览

    对齐参考项目的 previewCode：一个入口自动识别来源，前端无需分类调用。

    v2.10.1：优惠券也并进这一个入口。此前它是钱包页上另起的一条输入框，
    与「卡码 · 兑换码」并排成两个「输入码 → 应用」的面板——同一页两个兑换入口，
    用户要先猜自己手里那张码该填哪边。现在由服务端判定来源，前端只渲染结果。
    """
    raw = (raw or "").strip()
    if not raw:
        return {"valid": False, "kind": "unknown", "message": "请输入卡码"}

    code = find_reg_code(db, raw)
    if code:
        error = reg_code_error(code)
        named_ok = True
        if code.target_username:
            named_ok = bool(username) and code.target_username.strip().lower() == username.strip().lower()
        realm_id = code_realm_id(db, code)
        reward_type = getattr(code, 'reward_type', 'subscription') or 'subscription'
        preview = {
            "valid": error is None and named_ok,
            "kind": "code",
            "code_type": code.code_type,
            "type_name": CODE_TYPE_NAMES.get(code.code_type, "卡码"),
            "reward_type": reward_type,
            "days": normalize_days(code.days),
            "days_text": format_days(code.days),
            "is_named": bool(code.target_username),
            "target_username": code.target_username,
            "remaining_uses": max((code.max_uses or 0) - (code.use_count or 0), 0),
            # 卡码是一个服一个的：用户有权在核销前就知道这开的是哪个服的会员
            "realm_id": realm_id,
            "realm_name": realm_label(db, realm_id),
            "message": error or ("该卡码限指定账号使用" if not named_ok else "卡码有效"),
        }
        # M1：非订阅奖励类型在预览中说明
        if reward_type == 'points':
            preview["points_value"] = getattr(code, 'points_value', 0) or 0
            preview["message"] = error or (f"积分卡码（{preview['points_value']} 积分）" if named_ok else "该卡码限指定账号使用")
        elif reward_type == 'discount':
            preview["discount_pct"] = getattr(code, 'discount_pct', 0) or 0
            preview["message"] = error or (f"折扣卡码（{discount_pct_text(preview['discount_pct'])}）" if named_ok else "该卡码限指定账号使用")
        # 明确无效时附机器码：端点层据此把业务文案优先于限流 429 返回
        _ec = reg_code_error_code(code) or (CODE_ERROR_NAMED_OTHER if not named_ok else None)
        if _ec:
            preview["error_code"] = _ec
        return preview

    invitation = db.query(models.InvitationCode).filter(
        func.upper(models.InvitationCode.code) == raw.upper()
    ).first()
    if invitation:
        return {
            "valid": False, "kind": "invite", "days": 0, "days_text": "-",
            "message": "这是邀请码，请在注册页填写",
        }

    exchange = db.query(models.ExchangeCode).filter(
        func.upper(models.ExchangeCode.code) == raw.upper()
    ).first()
    if exchange:
        return {
            "valid": False, "kind": "exchange", "days": exchange.duration_days or 0,
            "days_text": format_days(exchange.duration_days) if exchange.type == "subscription" else "-",
            "discount_text": discount_pct_text(exchange.discount_pct) if exchange.type == "discount" else "-",
            # 兑换码在同一个入口里直接核销，不再指向已经收掉的「钱包 → 兑换码」
            "message": "这是兑换码，点「使用」直接核销",
        }

    # 优惠券（v2.10.0）：付费时抵扣，与卡码/兑换码同一个入口识别，
    # 真正的额度与门槛校验在试算时由 coupons.quote 按商品给出
    coupon = db.query(models.CouponCode).filter(
        func.upper(models.CouponCode.code) == raw.upper()
    ).first()
    if coupon:
        return {
            "valid": bool(coupon.is_active),
            "kind": "coupon",
            "days": 0,
            "days_text": "-",
            "message": ("这是优惠券，点「使用」按当前商品试算折扣"
                        if coupon.is_active else "该优惠券已停用"),
        }

    return {"valid": False, "kind": "unknown", "message": "卡码不存在",
            "error_code": CODE_ERROR_NOT_FOUND}


def redeem_code(db: Session, user: models.WebUser, raw: str) -> dict:
    """核销卡码：注册码 / 续期码 / 白名单码

    - 注册码：仅用于尚无生效会员的账号（已有会员请用续期码，与参考实现口径一致）
    - 续期码：在当前到期时间上叠加天数
    - 白名单码：置为长期有效
    - 诱饵码：不暴露身份，统一返回「卡码无效」，同时封禁账号并落安全日志
    - 指名码：非空 target_username 仅限该用户使用
    """
    from backend.authlog import record_event

    raw = (raw or "").strip()
    if not raw:
        return {"success": False, "message": "请输入卡码"}

    code = find_reg_code(db, raw)
    if not code:
        preview = preview_code(db, raw)
        result = {"success": False, "message": preview.get("message") or "卡码无效"}
        if preview.get("error_code"):
            result["error_code"] = preview["error_code"]
        return result

    if is_honeypot(code):
        # 蜜罐：诱饵码只应出现在盗版/破解渠道，使用即视为违规。
        # 顺序是关键：诱饵判定必须在可用性检查之前——陷阱不能因为
        # 「已用尽 / 已过期 / 已停用」就失效；也不走 claim_code（不占 use_count，
        # 陷阱永不烧尽）。管理员想收网时直接在后台停用/删除该码即可。
        user.is_active = False
        logger.warning(
            "诱饵码触发：用户 %s (id=%s) 于 %s 使用了诱饵码 %s，账号已自动封禁",
            user.username, user.id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), code.code,
        )
        record_event(
            db, username=user.username, user_id=user.id, success=False,
            reason="decoy_code", detail=f"使用了诱饵码 {code.code}，账号已自动封禁",
        )
        db.commit()
        return {"success": False, "message": "卡码无效"}

    error = reg_code_error(code)
    if error:
        return {"success": False, "message": error,
                "error_code": reg_code_error_code(code)}

    if code.target_username and code.target_username.strip().lower() != (user.username or "").lower():
        return {"success": False, "message": "该卡码限指定账号使用",
                "error_code": CODE_ERROR_NAMED_OTHER}

    days = grant_days_for(code)
    # 这张卡码属于哪个服，就判定/开通哪个服的会员（甲服的会员不算乙服的）
    target = code_realm_id(db, code)
    target_name = realm_label(db, target)
    if code.code_type == CODE_TYPE_REGISTER and has_active_membership(db, user, target):
        return {"success": False,
                "message": f"你在「{target_name}」已有生效中的会员，请使用续期码（本卡码只能开通新会员）"}

    # 先原子占位再发奖：并发提交同一张卡码时只有一个请求能拿到那一行（见 claim_code）。
    # 抢不到 = 这张码在这次请求之前已经被用掉，如实告诉用户，不发天数。
    if user_already_redeemed(db, REDEMPTION_KIND_REG, code, user.id):
        return {"success": False, "message": "你已经使用过这张卡码（每个账号每张码限用一次）",
                "error_code": CODE_ERROR_SELF_USED}
    if not claim_code(db, code, user.id):
        return {"success": False, "message": CODE_USED_UP_MESSAGE,
                "error_code": CODE_ERROR_USED_UP}

    # 占位与发奖在同一个事务里（本函数不提交，由调用方提交）；
    # 中间崩溃会一起回滚，不会出现「码烧了、会员没到账」
    # M1 统一码系统：根据 reward_type 发放不同奖励
    reward_type = getattr(code, 'reward_type', 'subscription') or 'subscription'
    if reward_type == 'points':
        points = getattr(code, 'points_value', 0) or 0
        if points <= 0:
            return {"success": False, "message": "该码的积分面额无效"}
        from backend.api.economy import _add_points
        _add_points(db, user, points, "code_redeem", f"卡码核销 {code.code}")
        type_name = CODE_TYPE_NAMES.get(code.code_type, "卡码")
        return {
            "success": True,
            "message": f"{type_name}核销成功，获得 {points} 积分",
            "code_type": code.code_type,
            "reward_type": "points",
            "points": points,
        }
    if reward_type == 'discount':
        pct = getattr(code, 'discount_pct', 0) or 0
        if pct <= 0 or pct >= 100:
            return {"success": False, "message": "该码的折扣无效"}
        # 发放一张折扣权益（复用 ExchangeDiscountCredit 表）
        credit = models.ExchangeDiscountCredit(
            user_id=user.id,
            exchange_code_id=0,  # 统一码系统：来源为 registration_codes
            discount_pct=pct,
            status='unused',
            expires_at=code.expires_at,
        )
        # 记录来源码信息到备注（表结构无来源字段，用 user 备注区分）
        db.add(credit)
        type_name = CODE_TYPE_NAMES.get(code.code_type, "卡码")
        return {
            "success": True,
            "message": f"{type_name}核销成功，获得 {discount_pct_text(pct)} 优惠券一张",
            "code_type": code.code_type,
            "reward_type": "discount",
            "discount_pct": pct,
        }
    # 默认：subscription，会员天数
    sub = grant_membership_days(db, user, days, target)

    type_name = CODE_TYPE_NAMES.get(code.code_type, "卡码")
    return {
        "success": True,
        "message": (f"{type_name}核销成功，「{target_name}」会员到期时间 "
                    f"{sub.end_date.strftime('%Y-%m-%d %H:%M')}"),
        "code_type": code.code_type,
        "reward_type": "subscription",
        "days": days,
        "realm_id": target,
        "realm_name": target_name,
        "end_date": sub.end_date.isoformat(),
    }


def classify_invalid_code(db: Session, user: Optional[models.WebUser], raw: str) -> Optional[dict]:
    """只读判定一张码是否处于「明确无效」状态（不核销、不写库）。

    命中返回 {"error_code": ..., "message": ...}，未命中返回 None。
    供核销端点在限流拒绝时兜底：码的状态已经确定时（已被使用 / 已停用 /
    已过期 / 不存在 / 指名他人 / 自己已用过），把业务文案给用户，不用
    一句 429「操作过于频繁」把真原因盖掉。
    蜜罐码故意返回 None——必须走正常核销流程触发封禁与安全日志，
    不能在这里提前短路，更不能暴露它与普通无效码的区别。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    code = find_reg_code(db, raw)
    if code is None:
        preview = preview_code(db, raw, user.username if user else None)
        if preview.get("kind") == "unknown":
            return {"error_code": CODE_ERROR_NOT_FOUND,
                    "message": preview.get("message") or "卡码不存在"}
        return None
    if is_honeypot(code):
        return None
    error_code = reg_code_error_code(code)
    if error_code:
        return {"error_code": error_code, "message": reg_code_error(code)}
    if (code.target_username and user is not None
            and code.target_username.strip().lower() != (user.username or "").lower()):
        return {"error_code": CODE_ERROR_NAMED_OTHER, "message": "该卡码限指定账号使用"}
    if user is not None and user_already_redeemed(db, REDEMPTION_KIND_REG, code, user.id):
        return {"error_code": CODE_ERROR_SELF_USED,
                "message": "你已经使用过这张卡码（每个账号每张码限用一次）"}
    return None

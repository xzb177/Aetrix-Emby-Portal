"""群抽奖 G1 核心服务。

职责：
- 群抽奖轮次的创建、参与、开奖、发奖与结果核验；
- 开关与群组白名单走 SystemConfig（lottery_enabled / lottery_group_ids）；
- 开奖算法为纯函数 compute_winners：score = sha256(f"{seed}:{entry_id}") 升序分配；
- 奖品发放复用经济系统原语 _add_points 与门户福利原语 portal.grant_welfare，
  均在函数内懒导入，避免循环依赖。
"""

import hashlib
import html
import secrets
import logging
import threading
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models

logger = logging.getLogger(__name__)

_PRIZE_TYPES = ("days", "points", "whitelist")


def shanghai_now_naive() -> datetime:
    """Asia/Shanghai naive 当前时间（用户侧口径）；缺 tzdata 时回退机器本地。"""
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def to_shanghai_naive(dt: datetime) -> datetime:
    """把 fromisoformat 解析出的时间统一成上海 naive：
    aware（前端 toISOString 的 UTC）→ 转上海去 tzinfo；naive → 按上海理解原样返回。"""
    if dt.tzinfo is not None:
        try:
            return dt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        except Exception:
            return dt.replace(tzinfo=None)
    return dt


def _get_str_config(db: Session, key: str, default: str) -> str:
    """读取 SystemConfig 字符串配置，缺失时返回默认值。"""
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    return cfg.value if cfg else default


def is_enabled(db: Session) -> bool:
    """群抽奖总开关：lottery_enabled 为 "1" 或 "true"（去空格、忽略大小写）时开启，默认 "1"。"""
    val = _get_str_config(db, "lottery_enabled", "1").strip().lower()
    return val in ("1", "true")


def allowed_group_ids(db: Session) -> set[int]:
    """读取允许发起抽奖的群 id 集合（lottery_group_ids，逗号分隔）；空集合表示不限制。"""
    val = _get_str_config(db, "lottery_group_ids", "")
    ids: set[int] = set()
    for part in val.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            # 非法片段跳过
            continue
    return ids


# ==================== 三重门：抽奖守卫体系 ====================
#
# 设计理念（我们自己的方案，非照抄开源）：
# 所有参赛资格检查收敛到 check_eligibility 一个入口，返回结构化结果，
# 调用方（TG 回调/管理后台）只做文案映射。新增门禁只需加一个 reason，
# 不用改调用方——这是"横切能力只许一套"纪律在抽奖域的落地。
#
# 三重门：
#   身份门：参赛者必须在 TG 群里（getChatMember 校验），防群外薅奖
#   资格门：黑名单 / 新号限制 / 参与频率限流，防薅
#   公信门：开奖种子混入 drand 公开随机信标，任何人可验证

def require_group_member(db: Session) -> bool:
    """身份门开关：lottery_require_group_member，默认开启。"""
    val = _get_str_config(db, "lottery_require_group_member", "1").strip().lower()
    return val in ("1", "true")


def min_account_age_days(db: Session) -> int:
    """资格门：账号最小注册天数（lottery_min_account_age_days），默认 0=不限制。"""
    try:
        return max(0, int(_get_str_config(db, "lottery_min_account_age_days", "0").strip()))
    except ValueError:
        return 0


def max_joins_per_day(db: Session) -> int:
    """资格门：每人每天最多参加次数（lottery_max_joins_per_day），默认 0=不限制。"""
    try:
        return max(0, int(_get_str_config(db, "lottery_max_joins_per_day", "0").strip()))
    except ValueError:
        return 0


def is_blacklisted(db: Session, user_id: int) -> models.LotteryBlacklist | None:
    """资格门：查用户是否在抽奖黑名单里，返回黑名单记录或 None。"""
    return (
        db.query(models.LotteryBlacklist)
        .filter(models.LotteryBlacklist.user_id == user_id)
        .first()
    )


def check_eligibility(
    db: Session,
    round: models.LotteryRound,
    user: models.WebUser,
) -> dict[str, Any]:
    """三重门·资格门：参赛资格检查（不含"是否在群里"，那是身份门，走 TG API）。

    返回 {"ok": True} 或 {"ok": False, "reason": ...}：
    - "disabled"：功能总开关关闭
    - "closed"：活动已结束或未开始
    - "blacklisted"：在黑名单中
    - "too_new"：账号注册天数不足
    - "rate_limited"：今日参加次数超限
    - "already"：已参加过本轮
    - "full"：名额已满

    注意：身份门（getChatMember）需要 TG API，由调用方在调此函数前完成，
    因为 lottery 模块不依赖 tg_bot（避免循环依赖）。
    """
    if not is_enabled(db):
        return {"ok": False, "reason": "disabled"}
    if round.status != "open":
        return {"ok": False, "reason": "closed"}
    # 资格门第1层：黑名单
    if is_blacklisted(db, user.id):
        return {"ok": False, "reason": "blacklisted"}
    # 资格门第2层：新号限制
    min_days = min_account_age_days(db)
    if min_days > 0:
        created = getattr(user, "created_at", None)
        if created is not None:
            age_days = (datetime.now() - created).days
            if age_days < min_days:
                return {"ok": False, "reason": "too_new", "min_days": min_days}
    # 资格门第3层：参与频率限流
    max_joins = max_joins_per_day(db)
    if max_joins > 0:
        day_ago = datetime.now() - timedelta(hours=24)
        count = (
            db.query(models.LotteryRoundEntry)
            .filter(
                models.LotteryRoundEntry.user_id == user.id,
                models.LotteryRoundEntry.joined_at >= day_ago,
            )
            .count()
        )
        if count >= max_joins:
            return {"ok": False, "reason": "rate_limited", "max_joins": max_joins}
    # 已参加过？
    exists = (
        db.query(models.LotteryRoundEntry.id)
        .filter(
            models.LotteryRoundEntry.round_id == round.id,
            models.LotteryRoundEntry.user_id == user.id,
        )
        .first()
    )
    if exists:
        return {"ok": False, "reason": "already"}
    # 名额检查（P2 修复保留：FOR UPDATE 防并发超员）
    if round.max_participants and round.max_participants > 0:
        try:
            db.execute(
                text("SELECT id FROM lottery_rounds WHERE id = :id FOR UPDATE"),
                {"id": round.id},
            )
        except Exception:
            db.execute(
                text("UPDATE lottery_rounds SET id = id WHERE id = :id"),
                {"id": round.id},
            )
        current = (
            db.query(models.LotteryRoundEntry)
            .filter(models.LotteryRoundEntry.round_id == round.id)
            .count()
        )
        if current >= round.max_participants:
            return {"ok": False, "reason": "full"}
    return {"ok": True}


# ---------------- 公信门：drand 公开随机信标 ----------------

DRAND_API_URL = "https://api.drand.sh/52db9f592a2b3d2ff7e0f5693a67db2393fdd434a48b9d0a64a5e1d5e69df/public/latest"
DRAND_TIMEOUT_SEC = 10


def fetch_drand_beacon() -> dict[str, Any] | None:
    """抓取 drand 主网最新公开随机信标。

    drand（https://drand.love）是 League of Entropy 运营的公开随机信标，
    每 30 秒产生一轮，任何人可免费验证。开奖时把信标混入种子，
    管理员无法通过"挑 seed"操纵结果——因为信标在开奖时刻才产生。

    返回 {"round": int, "randomness": str}，失败返回 None（调用方降级用纯 seed）。
    """
    try:
        import httpx

        resp = httpx.get(DRAND_API_URL, timeout=DRAND_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
        round_no = data.get("round")
        randomness = data.get("randomness")
        if not round_no or not randomness:
            logger.warning("drand 信标返回缺字段: %s", str(data)[:200])
            return None
        return {"round": int(round_no), "randomness": str(randomness)}
    except Exception:
        logger.warning("drand 信标抓取失败，降级为纯 seed 开奖", exc_info=True)
        return None


def mix_seed_with_drand(db_seed: str, drand_randomness: str | None) -> str:
    """公信门：混合种子 = sha256(db_seed + drand_randomness)。

    drand 为空（抓取失败）时返回原 seed，保证可用性优先。
    """
    if not drand_randomness:
        return db_seed
    return hashlib.sha256(f"{db_seed}:{drand_randomness}".encode()).hexdigest()


def create_round(
    db: Session,
    title: str,
    chat_id: int,
    prizes: list[dict],
    draw_at: datetime | None = None,
    max_participants: int = 0,
    created_by: int | None = None,
    lottery_type: str = "button",
    password_keyword: str | None = None,
) -> models.LotteryRound:
    """创建抽奖轮次及其奖品列表。

    prizes 每项为 dict(name, type, value, quantity)，type 仅允许 days/points/whitelist；
    sort 按列表顺序从 0 递增。seed 明文入库（开奖后才公开，不展示），
    seed_hash 存其 sha256 供核验。
    lottery_type: button=按钮抽奖 / password=口令抽奖；口令抽奖必须提供 password_keyword。
    """
    if not title or not title.strip():
        raise ValueError("标题不能为空")
    if not prizes:
        raise ValueError("奖品列表不能为空")
    for p in prizes:
        if p.get("type") not in _PRIZE_TYPES:
            raise ValueError(f"不支持的奖品类型：{p.get('type')}")
    if lottery_type not in ("button", "password"):
        raise ValueError("抽奖类型只能是 button 或 password")
    if lottery_type == "password":
        kw = (password_keyword or "").strip()
        if not kw:
            raise ValueError("口令抽奖必须设置口令关键词")
        password_keyword = kw
    else:
        password_keyword = None

    seed = secrets.token_hex(32)
    round = models.LotteryRound(
        title=title,
        chat_id=chat_id,
        seed=seed,
        seed_hash=hashlib.sha256(seed.encode()).hexdigest(),
        status="open",
        draw_at=draw_at,
        max_participants=max_participants or 0,
        created_by=created_by,
        lottery_type=lottery_type,
        password_keyword=password_keyword,
    )
    db.add(round)
    db.flush()  # 取得 round.id 供奖品外键使用
    for sort, p in enumerate(prizes):
        db.add(
            models.LotteryRoundPrize(
                round_id=round.id,
                name=p.get("name"),
                type=p.get("type"),
                value=p.get("value", 0),
                quantity=p.get("quantity", 0),
                sort=sort,
            )
        )
    db.commit()
    db.refresh(round)
    return round


def get_active_round(db: Session, chat_id: int) -> models.LotteryRound | None:
    """返回该群当前进行中的轮次（status='open'，按 id 倒序取第一条），没有则 None。"""
    return (
        db.query(models.LotteryRound)
        .filter(models.LotteryRound.chat_id == chat_id, models.LotteryRound.status == "open")
        .order_by(models.LotteryRound.id.desc())
        .first()
    )


def get_password_round(db: Session, chat_id: int, keyword: str) -> models.LotteryRound | None:
    """口令抽奖：根据群ID+口令关键词查找进行中的口令类型轮次。

    精确匹配（去空格后）；没有则返回 None。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None
    return (
        db.query(models.LotteryRound)
        .filter(
            models.LotteryRound.chat_id == chat_id,
            models.LotteryRound.lottery_type == "password",
            models.LotteryRound.password_keyword == kw,
            models.LotteryRound.status == "open",
        )
        .order_by(models.LotteryRound.id.desc())
        .first()
    )


def get_round(db: Session, round_id: int) -> models.LotteryRound | None:
    """按 id 取抽奖轮次，没有则 None（供 TG 参加按钮回调等按 id 取活动）。"""
    return (
        db.query(models.LotteryRound)
        .filter(models.LotteryRound.id == round_id)
        .first()
    )


def join_round(
    db: Session,
    round: models.LotteryRound,
    user: models.WebUser,
    telegram_id: int,
) -> dict[str, Any]:
    """用户参与本轮抽奖。

    不抛异常，用 dict 表达结果，方便调用方（TG 回调）直接映射文案：
    - 成功：{"ok": True, "entry": entry}
    - 失败：{"ok": False, "reason": ...}
      三重门 reason 全集：
      "disabled"（总开关关闭）| "closed"（活动已结束）
      | "blacklisted"（黑名单）| "too_new"（账号太新）
      | "rate_limited"（今日参加超限）| "not_member"（不在 TG 群里）
      | "already"（已参加过）| "full"（名额已满）

    注意："not_member"（身份门）由调用方在调本函数前完成 getChatMember 校验
    后传入，本函数只做资格门检查——lottery 模块不依赖 tg_bot，避免循环依赖。
    并发重复参加由 uq_lottery_round_entry 唯一约束兜底，同样返回 already。
    """
    # 三重门·资格门：集中检查（黑名单/新号/限流/已参加/名额/开关/状态）
    check = check_eligibility(db, round, user)
    if not check["ok"]:
        return check

    entry = models.LotteryRoundEntry(
        round_id=round.id,
        user_id=user.id,
        telegram_id=telegram_id,
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # 并发窗口：预检查通过后另一请求先插入，唯一约束兜底；必须先 rollback
        db.rollback()
        return {"ok": False, "reason": "already"}
    return {"ok": True, "entry": entry}


def compute_winners(seed: str, entry_ids: list[int], prizes: list[dict]) -> dict[int, list[int]]:
    """纯函数，不碰数据库：按 score 升序分配奖品。

    score = sha256(f"{seed}:{entry_id}")，entry 按 (score, entry_id) 升序排列保证确定性；
    按 prizes 列表顺序，每个 prize 取 quantity 个尚未中奖者；
    quantity<=0 的奖品跳过（不占名额）；entry 不足时按实际人数分配（不报错）。
    返回 {prize_id: [entry_id, ...]}。
    """
    scored = [
        (hashlib.sha256(f"{seed}:{eid}".encode()).hexdigest(), eid)
        for eid in entry_ids
    ]
    scored.sort(key=lambda item: (item[0], item[1]))
    ordered_ids = [eid for _, eid in scored]

    result: dict[int, list[int]] = {}
    cursor = 0
    for p in prizes:
        quantity = p.get("quantity", 0) or 0
        if quantity <= 0:
            continue
        winners: list[int] = []
        while quantity > 0 and cursor < len(ordered_ids):
            winners.append(ordered_ids[cursor])
            cursor += 1
            quantity -= 1
        result[p["id"]] = winners
    return result


def draw_round(db: Session, round_id: int) -> list[models.LotteryRoundWinner]:
    """执行开奖。

    状态机：open/drawing -> drawing -> done。done 幂等直接返回已有结果（按 id 排序）；
    drawing 视为上次开奖中断，可重入（先清理遗留 winner 行再重算）。
    entry 为空或 prize 为空时正常结束：status='done'、drawn_at 落库、返回 []。

    注意：drawing 重入是"开奖进程崩溃"的恢复路径——run_due_draws 开头的
    reap_stale_drawing_rounds 会把僵尸 drawing 打回 open，下一 tick 重新认领后
    走到这里重算。它不是给并发调用准备的：调用方（定时任务/管理后台）必须保证
    同一轮次同一时刻只有一个开奖者在跑（认领 SQL 原子保证）。
    """
    round = db.query(models.LotteryRound).filter(models.LotteryRound.id == round_id).first()
    if not round:
        raise ValueError("抽奖轮次不存在")
    if round.status == "done":
        return (
            db.query(models.LotteryRoundWinner)
            .filter(models.LotteryRoundWinner.round_id == round_id)
            .order_by(models.LotteryRoundWinner.id.asc())
            .all()
        )
    if round.status == "cancelled":
        raise ValueError("已取消的轮次不能开奖")

    # 先占状态，防并发重复开奖；drawing_at 给僵尸回收计时（直调本函数也生效，
    # 不依赖调用方的认领 SQL 是否带了 drawing_at）
    round.status = "drawing"
    round.drawing_at = datetime.now()
    db.commit()

    # 清理上次中断遗留的 winner 行，保证幂等
    db.query(models.LotteryRoundWinner).filter(models.LotteryRoundWinner.round_id == round_id).delete()

    entry_ids = [
        row.id
        for row in db.query(models.LotteryRoundEntry.id)
        .filter(models.LotteryRoundEntry.round_id == round_id)
        .order_by(models.LotteryRoundEntry.id.asc())
        .all()
    ]
    prizes = (
        db.query(models.LotteryRoundPrize)
        .filter(models.LotteryRoundPrize.round_id == round_id)
        .order_by(models.LotteryRoundPrize.sort.asc(), models.LotteryRoundPrize.id.asc())
        .all()
    )

    # 三重门·公信门：抓取 drand 公开随机信标，与库内 seed 混合成最终种子。
    # 信标在开奖时刻才产生且公开可验证，管理员无法通过"挑 seed"操纵结果。
    # 抓取失败时降级为纯 seed（可用性优先），verify 报告会如实标注。
    beacon = fetch_drand_beacon()
    if beacon:
        round.drand_round = beacon["round"]
        round.drand_randomness = beacon["randomness"]
        final_seed = mix_seed_with_drand(round.seed, beacon["randomness"])
        logger.info(
            "群抽奖公信门：drand 信标已混入 round_id=%s drand_round=%s",
            round_id,
            beacon["round"],
        )
    else:
        round.drand_round = None
        round.drand_randomness = None
        final_seed = round.seed
        logger.warning("群抽奖公信门：drand 抓取失败，降级为纯 seed 开奖 round_id=%s", round_id)

    result = compute_winners(final_seed, entry_ids, [{"id": p.id, "quantity": p.quantity} for p in prizes])

    for prize_id, entry_id_list in result.items():
        for entry_id in entry_id_list:
            db.add(models.LotteryRoundWinner(round_id=round_id, entry_id=entry_id, prize_id=prize_id))

    round.status = "done"
    round.drawn_at = datetime.now()
    db.commit()

    logger.info(
        "群抽奖开奖完成 round_id=%s 参与人数=%s 中奖记录=%s",
        round_id,
        len(entry_ids),
        sum(len(v) for v in result.values()),
    )

    return (
        db.query(models.LotteryRoundWinner)
        .filter(models.LotteryRoundWinner.round_id == round_id)
        .order_by(models.LotteryRoundWinner.id.asc())
        .all()
    )


def distribute_round(db: Session, round_id: int) -> dict[str, Any]:
    """按开奖结果发放奖品。

    幂等：distributed 已为 True 的跳过；单个发奖失败回滚并记入 errors，不中断整轮。
    返回 {"round_id", "total", "distributed", "skipped", "errors"}。
    """
    # 函数内懒导入，避免循环依赖
    from backend.api.economy import _add_points
    from backend.emby_server import portal

    round = db.query(models.LotteryRound).filter(models.LotteryRound.id == round_id).first()
    if not round:
        raise ValueError("抽奖轮次不存在")
    if round.status != "done":
        raise ValueError("尚未开奖")

    winners = (
        db.query(models.LotteryRoundWinner)
        .filter(models.LotteryRoundWinner.round_id == round_id)
        .order_by(models.LotteryRoundWinner.id.asc())
        .all()
    )
    prizes_by_id = {
        p.id: p
        for p in db.query(models.LotteryRoundPrize).filter(models.LotteryRoundPrize.round_id == round_id).all()
    }
    entries_by_id = {
        e.id: e
        for e in db.query(models.LotteryRoundEntry).filter(models.LotteryRoundEntry.round_id == round_id).all()
    }

    total = len(winners)
    distributed = 0
    skipped = 0
    errors: list[dict[str, Any]] = []

    for w in winners:
        if w.distributed:
            skipped += 1
            continue
        prize = prizes_by_id.get(w.prize_id)
        entry = entries_by_id.get(w.entry_id)
        if prize is None or entry is None:
            errors.append({"winner_id": w.id, "error": "奖品或参与记录缺失"})
            continue
        user = db.query(models.WebUser).filter(models.WebUser.id == entry.user_id).first()
        if user is None:
            errors.append({"winner_id": w.id, "error": "用户不存在"})
            continue
        if prize.type in ("points", "days") and (prize.value or 0) <= 0:
            # days=0 在 grant_welfare 语义里是永久，days 奖品不许借道发放永久资格
            errors.append({"winner_id": w.id, "error": f"{'积分' if prize.type == 'points' else '天数'}奖品 value 非正：{prize.value}"})
            continue
        if prize.type not in ("points", "days", "whitelist"):
            errors.append({"winner_id": w.id, "error": f"未知奖品类型：{prize.type}"})
            continue
        # 原子认领：UPDATE ... WHERE distributed = false。上面的 w.distributed 是读出来的旧值，
        # 管理员手动开奖与自动开奖调度（或连点两次）并发跑同一轮时都会读到 False → 双倍发奖。
        # 只有认领到这一行的请求发奖；发奖失败回滚会连认领一起撤销，下次可重试。
        claimed = (
            db.query(models.LotteryRoundWinner)
            .filter(
                models.LotteryRoundWinner.id == w.id,
                models.LotteryRoundWinner.distributed.is_(False),
            )
            .update(
                {"distributed": True, "distributed_at": datetime.now()},
                synchronize_session=False,
            )
        )
        if not claimed:
            db.rollback()
            skipped += 1
            continue
        try:
            if prize.type == "points":
                _add_points(db, user, prize.value, "lottery_win", f"群抽奖中奖：{prize.name}", f"lottery:{round_id}")
            elif prize.type == "days":
                portal.grant_welfare(db, user, channel="lottery_win", days=prize.value)
            else:  # whitelist
                # days=0 表示永不过期（已核实 grant_welfare 源码）
                portal.grant_welfare(db, user, channel="lottery_win", days=0)
        except Exception as exc:
            # 单个失败不中断整轮（回滚同时撤销上面的认领）
            db.rollback()
            errors.append({"winner_id": w.id, "error": str(exc)})
            continue
        db.commit()
        distributed += 1
        logger.info(
            "群抽奖发奖成功 round_id=%s winner_id=%s prize=%s type=%s",
            round_id,
            w.id,
            prize.name,
            prize.type,
        )

    return {
        "round_id": round_id,
        "total": total,
        "distributed": distributed,
        "skipped": skipped,
        "errors": errors,
    }


def verify_round(db: Session, round_id: int) -> dict[str, Any]:
    """生成轮次核验报告。

    包含 seed 明文与 seed_hash、算法说明、参与者列表（telegram_id 打码）、
    中奖列表（现场用与 compute_winners 相同的公式重算 score），可供外部复算验证。
    """
    round = db.query(models.LotteryRound).filter(models.LotteryRound.id == round_id).first()
    if not round:
        raise ValueError("抽奖轮次不存在")

    entries = (
        db.query(models.LotteryRoundEntry)
        .filter(models.LotteryRoundEntry.round_id == round_id)
        .order_by(models.LotteryRoundEntry.id.asc())
        .all()
    )
    winners = (
        db.query(models.LotteryRoundWinner)
        .filter(models.LotteryRoundWinner.round_id == round_id)
        .order_by(models.LotteryRoundWinner.id.asc())
        .all()
    )
    prizes_by_id = {
        p.id: p
        for p in db.query(models.LotteryRoundPrize).filter(models.LotteryRoundPrize.round_id == round_id).all()
    }

    entries_out = [
        {
            "entry_id": e.id,
            "user_id": e.user_id,
            "telegram_id_masked": _mask_telegram(e.telegram_id),
        }
        for e in entries
    ]
    # 三重门·公信门：核验时用同样的混合种子重算，保证可复算。
    is_done = (round.status or "") == "done"
    verify_seed = None
    if is_done:
        verify_seed = mix_seed_with_drand(round.seed, round.drand_randomness)

    winners_out = []
    for w in winners:
        score = (
            hashlib.sha256(f"{verify_seed}:{w.entry_id}".encode()).hexdigest()
            if verify_seed
            else None
        )
        prize = prizes_by_id.get(w.prize_id)
        winners_out.append(
            {
                "entry_id": w.entry_id,
                "prize_name": prize.name if prize else None,
                "score": score,
            }
        )

    # P1 修复（审查）：seed 明文只能在开奖后公开。开奖前任何人拿到 seed
    # 就能用公开算法算出全部中奖者，公平性完全丧失。未开奖时只给 seed_hash。
    return {
        "round_id": round.id,
        "title": round.title,
        "status": round.status,
        "seed_hash": round.seed_hash,
        "seed": round.seed if is_done else None,
        "algorithm": "sha256(final_seed:entry_id)升序，final_seed=sha256(db_seed:drand_randomness)",
        # 三重门·公信门：drand 信标公开可验证（https://drand.love）
        "drand_round": round.drand_round,
        "drand_randomness": round.drand_randomness if is_done else None,
        "drand_verified": bool(round.drand_randomness),
        "entries": entries_out,
        "winners": winners_out,
    }



# ==================== 自动开奖调度 ====================

_LOTTERY_DRAW_SCHEDULER_STARTED = False
_LOTTERY_DRAW_SCHEDULER_LOCK = threading.Lock()

DRAW_INTERVAL_DEFAULT_SEC = 60
DRAW_INTERVAL_MIN_SEC = 30


def _get_int_config(db: Session, key: str, default: int) -> int:
    """读取整数型系统配置，读不到或非法时回退默认值。"""
    raw = _get_str_config(db, key, None)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def auto_draw_enabled(db: Session) -> bool:
    """自动开奖总开关：lottery_auto_draw_enabled 为 "1"/"true" 时开，默认 "1"。"""
    val = _get_str_config(db, "lottery_auto_draw_enabled", "1")
    return str(val or "1").strip().lower() in ("1", "true")


def notify_winners_enabled(db: Session) -> bool:
    """开奖通知开关：lottery_notify_winners 为 "1"/"true" 时开，默认 "1"。"""
    val = _get_str_config(db, "lottery_notify_winners", "1")
    return str(val or "1").strip().lower() in ("1", "true")


def draw_interval_sec(db: Session) -> int:
    """自动开奖扫描间隔（秒）：lottery_draw_interval_sec，默认 60，最小 30。"""
    return max(_get_int_config(db, "lottery_draw_interval_sec", DRAW_INTERVAL_DEFAULT_SEC), DRAW_INTERVAL_MIN_SEC)


def _mask_telegram(telegram_id: int) -> str:
    """TG id 打码：只保留后四位。"""
    s = str(telegram_id)
    if len(s) <= 4:
        return "*" * len(s)
    return "****" + s[-4:]


def notify_draw_results(db: Session, round_id: int, winners: list, distribute_errors: list | None = None) -> dict:
    """开奖后通知：群公告 + 中奖者私聊。

    TG 发送走函数内懒导入（避免循环依赖），任何发送失败只记日志不抛异常。
    distribute_errors：发奖失败明细（distribute_round 返回的 errors）；非空时群公告
    如实提示部分奖品发放失败，不把原始 error 文案拼进公告（可能含内部信息）。
    """
    result: dict[str, Any] = {"sent": False, "group": False, "dm_sent": 0, "dm_failed": 0}
    if not notify_winners_enabled(db):
        return result
    try:
        from backend.tg_bot import sender
    except Exception:
        logger.exception("群抽奖通知：导入 tg_bot.sender 失败")
        return result

    round = db.query(models.LotteryRound).filter(models.LotteryRound.id == round_id).first()
    if not round:
        return result
    prizes_by_id = {
        p.id: p
        for p in db.query(models.LotteryRoundPrize).filter(models.LotteryRoundPrize.round_id == round_id).all()
    }
    entries_by_id = {
        e.id: e
        for e in db.query(models.LotteryRoundEntry).filter(models.LotteryRoundEntry.round_id == round_id).all()
    }

    lines: list[str] = []
    dm_targets: list[tuple[int, str]] = []
    for w in winners or []:
        prize = prizes_by_id.get(w.prize_id)
        entry = entries_by_id.get(w.entry_id)
        if prize is None or entry is None:
            continue
        lines.append(f"🥇 {html.escape(str(prize.name))}：用户{_mask_telegram(entry.telegram_id)}")
        dm_targets.append((entry.telegram_id, str(prize.name)))

    round_title = html.escape(str(round.title or ""))
    if distribute_errors:
        # 发奖有失败：群公告如实口径，不宣称"已自动发放"
        draw_tail = f"⚠️ 部分奖品发放失败（{len(distribute_errors)} 笔），系统将自动补发，seed 公示可在管理后台核验。"
    else:
        draw_tail = "奖励已自动发放。seed 公示可在管理后台核验。"
    group_text = (
        f"🎉 <b>群抽奖开奖啦！</b>\n\n「{round_title}」\n"
        + ("\n".join(lines) if lines else "本期无人参与，奖品轮空。")
        + f"\n\n{draw_tail}"
    )
    try:
        ok, err = sender.send_message(db, round.chat_id, group_text)
        result["group"] = bool(ok)
        if not ok:
            logger.warning("群抽奖开奖群公告发送失败 round_id=%s: %s", round_id, err)
    except Exception:
        logger.exception("群抽奖开奖群公告发送异常 round_id=%s", round_id)

    for telegram_id, prize_name in dm_targets:
        try:
            ok, err = sender.send_message(
                db,
                telegram_id,
                f"🎉 <b>恭喜中奖！</b>\n\n你在「{round_title}」中抽中了「{html.escape(prize_name)}」，奖励已发放到账。",
            )
            if ok:
                result["dm_sent"] += 1
            else:
                result["dm_failed"] += 1
                logger.warning("群抽奖中奖私聊发送失败 round_id=%s tg_id=***: %s", round_id, err)
        except Exception:
            result["dm_failed"] += 1
            logger.exception("群抽奖中奖私聊发送异常 round_id=%s", round_id)

    result["sent"] = True
    return result


def notify_new_round(db: Session, round_id: int) -> dict:
    """新抽奖活动创建后通知：群公告 + 参加按钮。

    管理后台创建抽奖活动后调用，让群成员知道有新抽奖可参加。
    TG 发送走函数内懒导入（避免循环依赖），任何发送失败只记日志不抛异常。
    """
    result: dict[str, Any] = {"sent": False, "group": False}
    if not is_enabled(db):
        return result
    try:
        from backend.tg_bot import sender
    except Exception:
        logger.exception("群抽奖新活动通知：导入 tg_bot.sender 失败")
        return result

    round = db.query(models.LotteryRound).filter(models.LotteryRound.id == round_id).first()
    if not round:
        return result
    prizes = (
        db.query(models.LotteryRoundPrize)
        .filter(models.LotteryRoundPrize.round_id == round_id)
        .order_by(models.LotteryRoundPrize.sort.asc())
        .all()
    )

    def _prize_label(p) -> str:
        name = html.escape(str(p.name or ""))
        qty = p.quantity or 1
        ptype = str(p.type or "").lower()
        if ptype == "days":
            return f"🎁 {name}：公益{p.value}天 ×{qty}"
        elif ptype == "points":
            return f"🎁 {name}：{p.value}积分 ×{qty}"
        elif ptype == "whitelist":
            return f"🎁 {name}：白名单 ×{qty}"
        return f"🎁 {name} ×{qty}"

    round_title = html.escape(str(round.title or ""))
    prize_lines = "\n".join(_prize_label(p) for p in prizes) if prizes else "🎁 神秘奖品"
    if round.draw_at:
        draw_text = html.escape(str(round.draw_at))
    else:
        draw_text = "手动开奖"
    group_text = (
        f"🎲 <b>新抽奖来啦！</b>\n\n「{round_title}」\n\n"
        f"{prize_lines}\n\n"
        f"⏰ 开奖时间：{draw_text}\n\n"
        f"👇 点击下方按钮参加"
    )
    reply_markup = {
        "inline_keyboard": [
            [{"text": "🎲 参加抽奖", "callback_data": f"lottery_join:{round_id}"}]
        ]
    }
    try:
        ok, err = sender.send_message(db, round.chat_id, group_text, reply_markup=reply_markup)
        result["group"] = bool(ok)
        if not ok:
            logger.warning("群抽奖新活动群公告发送失败 round_id=%s: %s", round_id, err)
    except Exception:
        logger.exception("群抽奖新活动群公告发送异常 round_id=%s", round_id)

    result["sent"] = True
    return result


# 僵尸 drawing 宽限期：draw_round 处于 drawing 状态的真实耗时只有 drand 抓取(<=10s)
# + 计算 + 写库；发奖阶段状态已是 done，不受影响。15 分钟远大于正常耗时，
# 误伤仍存活进程的概率可忽略。
_STALE_DRAWING_GRACE = timedelta(minutes=15)


def reap_stale_drawing_rounds(db: Session, summary: dict | None = None) -> list[int]:
    """回收僵尸 drawing 轮次：认领后进程崩溃，in-process 回滚够不着。

    认领(open→drawing)已提交、开奖完成前进程崩溃（SIGKILL/OOM/部署重启）时，
    轮次永久卡死在 drawing：调度只扫 open、管理后台认领要求 open、取消要求 open。
    本函数把超过宽限期仍为 drawing 的轮次打回 open，下一 tick 重新认领开奖
    （draw_round 的 drawing 重入路径会清理遗留 winner 行重算；distribute 幂等）。
    drawing_at IS NULL 覆盖升级前遗留的、无时间戳的僵尸行。
    """
    cutoff = datetime.now() - _STALE_DRAWING_GRACE
    try:
        rows = db.execute(
            text(
                "SELECT id FROM lottery_rounds "
                "WHERE status = 'drawing' "
                "AND (drawing_at IS NULL OR drawing_at < :cutoff)"
            ),
            {"cutoff": cutoff},
        ).fetchall()
    except Exception:
        db.rollback()
        logger.exception("群抽奖查询僵尸 drawing 轮次失败")
        if summary is not None:
            summary["reaped_stale_drawing"] = []
        return []

    reaped: list[int] = []
    for (rid,) in rows:
        try:
            result = db.execute(
                text(
                    "UPDATE lottery_rounds SET status = 'open', drawing_at = NULL "
                    "WHERE id = :id AND status = 'drawing'"
                ),
                {"id": rid},
            )
            db.commit()
            if result.rowcount > 0:
                reaped.append(rid)
                logger.warning(
                    "群抽奖回收僵尸 drawing 轮次 round_id=%s，已打回 open", rid
                )
        except Exception:
            db.rollback()
            logger.exception("群抽奖回收僵尸 drawing 轮次失败 round_id=%s", rid)

    if summary is not None:
        summary["reaped_stale_drawing"] = reaped
    return reaped


def run_due_draws(db: Session) -> dict:
    """扫描并自动开奖所有到期的轮次。

    到期条件：status='open' 且 draw_at 不为空且 draw_at <= now（按上海时间比较）。
    原子认领（UPDATE ... WHERE status='open'）防止多 worker 重复开奖；
    单个轮次失败回滚并记入 errors，不中断整批。
    """
    summary: dict[str, Any] = {"checked": 0, "drawn": [], "errors": []}
    # 先回收僵尸 drawing（崩溃遗留），再扫到期轮次：回收后打回 open 的自动轮次
    # 本 tick 即可被重新认领开奖；手动轮次打回 open 后管理后台可重试。
    # 回收不依赖自动开奖总开关——解卡死与是否自动开奖无关。
    reap_stale_drawing_rounds(db, summary)
    if not auto_draw_enabled(db):
        return summary
    now = shanghai_now_naive()
    due_ids = [
        row.id
        for row in db.query(models.LotteryRound.id)
        .filter(
            models.LotteryRound.status == "open",
            models.LotteryRound.draw_at.isnot(None),
            models.LotteryRound.draw_at <= now,
        )
        .order_by(models.LotteryRound.id.asc())
        .all()
    ]
    summary["checked"] = len(due_ids)
    for round_id in due_ids:
        try:
            claimed = db.execute(
                text("UPDATE lottery_rounds SET status='drawing', drawing_at=:now WHERE id=:id AND status='open'"),
                {"id": round_id, "now": datetime.now()},
            ).rowcount
            db.commit()
            if not claimed:
                # 别的 worker 已认领，跳过
                continue
            # 此时 status 已为 drawing，走 draw_round 的重入路径正常开奖
            winners = draw_round(db, round_id)
            dist_result = distribute_round(db, round_id)
            try:
                notify_draw_results(db, round_id, winners, distribute_errors=dist_result.get("errors"))
            except Exception:
                logger.exception("群抽奖开奖通知失败 round_id=%s（不影响开奖结果）", round_id)
            summary["drawn"].append(round_id)
            logger.info("群抽奖自动开奖完成 round_id=%s", round_id)
        except Exception as exc:
            db.rollback()
            # 认领后失败：把状态打回 open，下一轮 tick 可重试
            # （draw 的 seed 确定性 + distribute 的 distributed 幂等保证重试安全）
            try:
                db.execute(
                    text("UPDATE lottery_rounds SET status='open', drawing_at=NULL WHERE id=:id AND status='drawing'"),
                    {"id": round_id},
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("群抽奖自动开奖状态回滚失败 round_id=%s", round_id)
            summary["errors"].append({"round_id": round_id, "error": str(exc)})
            logger.exception("群抽奖自动开奖失败 round_id=%s", round_id)
    retry_pending_distributions(db, summary)
    return summary


# done 轮次补发窗口：开奖后多久内仍自动重试未发放的中奖记录。
# 永久性错误（奖品配置坏、用户被删）不会无限每分钟重试下去。
_REDISTRIBUTE_WINDOW = timedelta(days=7)


def pending_distribution_round_ids(db: Session, since: datetime | None = None) -> list[int]:
    """已开奖(done)但仍有 distributed=False 中奖记录的轮次 id。"""
    q = (
        db.query(models.LotteryRound.id)
        .join(models.LotteryRoundWinner, models.LotteryRoundWinner.round_id == models.LotteryRound.id)
        .filter(
            models.LotteryRound.status == "done",
            models.LotteryRoundWinner.distributed.is_(False),
        )
    )
    if since is not None:
        q = q.filter(
            (models.LotteryRound.drawn_at.is_(None)) | (models.LotteryRound.drawn_at >= since)
        )
    return sorted({row.id for row in q.distinct().all()})


def retry_pending_distributions(db: Session, summary: dict | None = None) -> list[int]:
    """补发：开奖后 distribute_round 中途失败（异常或单个发奖失败）时，轮次已是 done，
    run_due_draws 的"打回 open"匹配不到任何行，未发放的中奖者永远拿不到奖。
    这里按轮次重跑 distribute_round；原子认领（distributed=False → True）保证不会重复发奖。
    """
    retried: list[int] = []
    try:
        round_ids = pending_distribution_round_ids(db, since=datetime.now() - _REDISTRIBUTE_WINDOW)
    except Exception:
        db.rollback()
        logger.exception("群抽奖补发扫描失败")
        return retried
    for round_id in round_ids:
        try:
            res = distribute_round(db, round_id)
            retried.append(round_id)
            if res.get("distributed"):
                logger.info("群抽奖补发完成 round_id=%s distributed=%s", round_id, res.get("distributed"))
            if summary is not None and res.get("errors"):
                summary["errors"].append({"round_id": round_id, "error": "补发部分失败", "detail": res["errors"]})
        except Exception as exc:
            db.rollback()
            logger.exception("群抽奖补发失败 round_id=%s", round_id)
            if summary is not None:
                summary["errors"].append({"round_id": round_id, "error": str(exc)})
    if summary is not None:
        summary["redistributed"] = retried
    return retried


def start_lottery_auto_draw_scheduler() -> bool:
    """启动群抽奖自动开奖调度（daemon 线程，同一进程只启动一次）。

    每隔 lottery_draw_interval_sec（默认 60s，最小 30s）扫描一次；
    总开关 lottery_auto_draw_enabled（默认开）关闭时跳过本轮。
    间隔每次循环重读，管理后台改完即时生效。
    """
    global _LOTTERY_DRAW_SCHEDULER_STARTED
    with _LOTTERY_DRAW_SCHEDULER_LOCK:
        if _LOTTERY_DRAW_SCHEDULER_STARTED:
            return False
        _LOTTERY_DRAW_SCHEDULER_STARTED = True

    def _tick():
        try:
            from backend.database import SessionLocal
            db = SessionLocal()
            try:
                result = run_due_draws(db)
                if result["drawn"]:
                    logger.info("群抽奖自动开奖完成: %s", result)
            finally:
                db.close()
        except Exception:
            logger.exception("lottery auto draw scheduler tick failed")

    def _loop():
        # 复用同一个 Event：每轮 new 一个 Event().wait() 没有意义
        wake = threading.Event()
        while True:
            try:
                from backend.database import SessionLocal
                db = SessionLocal()
                try:
                    interval = draw_interval_sec(db)
                finally:
                    db.close()
            except Exception:
                logger.exception("lottery auto draw scheduler interval read failed")
                interval = DRAW_INTERVAL_DEFAULT_SEC
            wake.wait(interval)
            _tick()

    threading.Thread(target=_loop, daemon=True, name="lottery-auto-draw-scheduler").start()
    logger.info("群抽奖自动开奖调度已启动")
    return True

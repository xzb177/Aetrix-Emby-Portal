from __future__ import annotations
import logging
import threading
from datetime import date, datetime
from fastapi import HTTPException
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from backend import models
from backend.integrations import store

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "vitality_enabled"
CONFIG_MAX = "vitality_max"
CONFIG_DAILY_COST = "vitality_daily_cost"
CONFIG_LIMIT_THRESHOLD = "vitality_limit_threshold"
CONFIG_POINT_COST = "vitality_point_cost"
# 新增：观影奖励 / 每日免费获取上限 / 归档钳制
CONFIG_WATCH_REWARD = "vitality_watch_reward"
CONFIG_DAILY_GAIN_LIMIT = "vitality_daily_gain_limit"
CONFIG_ARCHIVE_CLAMP = "vitality_archive_clamp"

VITALITY_DEFAULTS = {
    CONFIG_ENABLED: "true",
    CONFIG_MAX: "14",
    CONFIG_DAILY_COST: "1",
    CONFIG_LIMIT_THRESHOLD: "3",
    CONFIG_POINT_COST: "10",
    CONFIG_WATCH_REWARD: "1",
    CONFIG_DAILY_GAIN_LIMIT: "2",
    CONFIG_ARCHIVE_CLAMP: "3",
}

# 每日免费获取计入上限的 reason（付费 recharge / 管理员 admin_grant 不计入）
FREE_GAIN_REASONS = ("checkin", "watch_reward")


def _raw(db: Session, key: str) -> str:
    return store.get_value(db, key, "")


def _cfg_bool(db, key, default):
    raw = _raw(db, key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() == "true"


def _cfg_int(db, key, default):
    raw = _raw(db, key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def validate_vitality_config(values: dict) -> dict:
    """校验活力值配置：返回 {key: error_msg}，空 dict 表示全部合法。

    规则：
    - max > 0；daily_cost >= 0；limit_threshold >= 0 且 <= max
    - point_cost > 0；watch_reward >= 0；daily_gain_limit >= 0
    - archive_clamp >= 0 且 <= max
    """
    errors = {}

    def _as_int(v):
        try:
            return int(float(str(v).strip()))
        except (TypeError, ValueError, AttributeError):
            return None

    max_v = _as_int(values.get(CONFIG_MAX, VITALITY_DEFAULTS[CONFIG_MAX]))
    if max_v is None or max_v <= 0:
        errors[CONFIG_MAX] = "活力上限必须是正整数"
        max_v = int(VITALITY_DEFAULTS[CONFIG_MAX])

    cost = _as_int(values.get(CONFIG_DAILY_COST, VITALITY_DEFAULTS[CONFIG_DAILY_COST]))
    if cost is None or cost < 0:
        errors[CONFIG_DAILY_COST] = "每日扣减不能为负数"

    threshold = _as_int(values.get(CONFIG_LIMIT_THRESHOLD, VITALITY_DEFAULTS[CONFIG_LIMIT_THRESHOLD]))
    if threshold is None or threshold < 0:
        errors[CONFIG_LIMIT_THRESHOLD] = "观影阈值不能为负数"
    elif threshold > max_v:
        errors[CONFIG_LIMIT_THRESHOLD] = "观影阈值不能超过活力上限"

    point_cost = _as_int(values.get(CONFIG_POINT_COST, VITALITY_DEFAULTS[CONFIG_POINT_COST]))
    if point_cost is None or point_cost <= 0:
        errors[CONFIG_POINT_COST] = "续活兑换率必须是正整数"

    watch_reward = _as_int(values.get(CONFIG_WATCH_REWARD, VITALITY_DEFAULTS[CONFIG_WATCH_REWARD]))
    if watch_reward is None or watch_reward < 0:
        errors[CONFIG_WATCH_REWARD] = "观影奖励不能为负数"

    gain_limit = _as_int(values.get(CONFIG_DAILY_GAIN_LIMIT, VITALITY_DEFAULTS[CONFIG_DAILY_GAIN_LIMIT]))
    if gain_limit is None or gain_limit < 0:
        errors[CONFIG_DAILY_GAIN_LIMIT] = "每日获取上限不能为负数"

    archive_clamp = _as_int(values.get(CONFIG_ARCHIVE_CLAMP, VITALITY_DEFAULTS[CONFIG_ARCHIVE_CLAMP]))
    if archive_clamp is None or archive_clamp < 0:
        errors[CONFIG_ARCHIVE_CLAMP] = "归档钳制不能为负数"
    elif archive_clamp > max_v:
        errors[CONFIG_ARCHIVE_CLAMP] = "归档钳制不能超过活力上限"

    return errors


def get_vitality_config(db: Session) -> dict:
    return {
        "enabled": _cfg_bool(db, CONFIG_ENABLED, True),
        "max": _cfg_int(db, CONFIG_MAX, 14),
        "daily_cost": _cfg_int(db, CONFIG_DAILY_COST, 1),
        "limit_threshold": _cfg_int(db, CONFIG_LIMIT_THRESHOLD, 3),
        "point_cost": _cfg_int(db, CONFIG_POINT_COST, 10),
        "watch_reward": _cfg_int(db, CONFIG_WATCH_REWARD, 1),
        "daily_gain_limit": _cfg_int(db, CONFIG_DAILY_GAIN_LIMIT, 2),
        "archive_clamp": _cfg_int(db, CONFIG_ARCHIVE_CLAMP, 3),
    }


def vitality_status(db: Session, user) -> dict:
    cfg = get_vitality_config(db)
    v = int(getattr(user, "vitality", None) or 0)
    return {
        "vitality": v,
        "max": cfg["max"],
        "limit_threshold": cfg["limit_threshold"],
        "can_play": (not cfg["enabled"]) or (not getattr(user, "is_welfare", False)) or v >= cfg["limit_threshold"],
        "suspended": v <= 0,
    }


def ensure_can_play(db: Session, user) -> None:
    cfg = get_vitality_config(db)
    if not cfg["enabled"]:
        return
    if not getattr(user, "is_welfare", False):
        return
    if getattr(user, "is_staff", False):
        return
    v = int(getattr(user, "vitality", None) or 0)
    if v >= cfg["limit_threshold"]:
        return
    raise HTTPException(status_code=403, detail=f"活力值不足（{v}/{cfg['limit_threshold']}），签到或用积分续活力后可继续观影")


def _lock_user_row(db: Session, user_id: int) -> None:
    """对用户行加写锁，持有到本事务提交/回滚（「查用量-判上限-写」三步的串行化原语）。

    与 backend/api/economy.py::lock_user_row 同语义；本模块独立一份以避免循环导入
    （economy.py 引用了本模块）。
    - PostgreSQL：SELECT ... FOR UPDATE（READ COMMITTED 下锁后的新语句看得到已提交数据）；
    - SQLite：无行锁，用无副作用 UPDATE 取得写锁（同样持有到提交）。
    必须在统计用量之前调用：并发第二个请求在锁上等前者提交后，重新统计就能看到
    前者已写入的记录，上限不会被绕过。
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT id FROM web_users WHERE id = :id FOR UPDATE"), {"id": user_id})
    else:
        db.execute(text("UPDATE web_users SET id = id WHERE id = :id"), {"id": user_id})


def _add_vitality(db: Session, user_id: int, delta: int, reason: str) -> int:
    """原子增减活力值：单条 UPDATE 完成加减 + 钳制到 [0, max]，返回钳制后的值。

    旧实现是「UPDATE 加减 → SELECT 读回 → 越界再 UPDATE」三步，
    并发下两个线程可能都读到钳制前的值导致最终越界。现在用 CASE
    在一条语句里完成，数据库保证原子性。
    """
    cfg = get_vitality_config(db)
    max_vitality = int(cfg.get("max", VITALITY_DEFAULTS[CONFIG_MAX]))

    # PostgreSQL / SQLite 都支持的写法：用 RETURNING 取回钳制后的值
    stmt = text(
        "UPDATE web_users SET vitality = CASE "
        "WHEN COALESCE(vitality, 0) + :delta < 0 THEN 0 "
        "WHEN COALESCE(vitality, 0) + :delta > :max_v THEN :max_v "
        "ELSE COALESCE(vitality, 0) + :delta END "
        "WHERE id = :uid "
        "RETURNING vitality"
    )
    try:
        row = db.execute(stmt, {"delta": delta, "max_v": max_vitality, "uid": user_id}).fetchone()
        final = int(row[0]) if row and row[0] is not None else 0
    except Exception:
        # 极少数方言不支持 RETURNING：回退到旧的两步写法
        logger.warning("RETURNING not supported, fallback to two-step vitality update")
        db.query(models.WebUser).filter(models.WebUser.id == user_id).update(
            {models.WebUser.vitality: func.coalesce(models.WebUser.vitality, 0) + delta},
            synchronize_session="fetch",
        )
        row = db.query(models.WebUser.vitality).filter(models.WebUser.id == user_id).first()
        current = int(row[0]) if row and row[0] is not None else 0
        final = max(0, min(max_vitality, current))
        if final != current:
            db.query(models.WebUser).filter(models.WebUser.id == user_id).update(
                {models.WebUser.vitality: final},
                synchronize_session="fetch",
            )
    db.add(models.VitalityLog(user_id=user_id, delta=delta, balance_after=final, reason=reason))
    return final


def get_today_free_gain(db: Session, user_id: int) -> int:
    """今天通过免费途径（签到/观影奖励）已获得的活力值。"""
    # r34 口径收口：VitalityLog.created_at 以 UTC naive 入库（容器时区 UTC），
    # 此前用服务器本地日界，上限在北京时间 08:00 才重置。函数内导入 economy
    # 是为了避开循环导入（economy.py 顶层导入了本模块）。
    from backend.api.economy import _today_start_utc_naive
    today_start = _today_start_utc_naive()
    total = (
        db.query(func.coalesce(func.sum(models.VitalityLog.delta), 0))
        .filter(
            models.VitalityLog.user_id == user_id,
            models.VitalityLog.delta > 0,
            models.VitalityLog.reason.in_(FREE_GAIN_REASONS),
            models.VitalityLog.created_at >= today_start,
        )
        .scalar()
    )
    return int(total or 0)


def award_watch_reward(db: Session, user_id: int) -> int:
    """观影奖励：按 vitality_watch_reward 发放，受每日免费获取上限钳制。

    返回实际发放的点数（0 表示未发放：功能关闭 / 已达上限 / 非公益服用户）。
    幂等性由调用方保证（每次有效观影调用一次）。

    并发安全：先对用户行加锁，再「查今日已得 → 判上限 → 发放」，三步在同一事务内
    串行化。第二个并发请求在锁上等待前者提交后，能看到前者已发放的记录，
    不会超发（修复 TOCTOU）。
    """
    cfg = get_vitality_config(db)
    if not cfg["enabled"]:
        return 0
    reward = int(cfg.get("watch_reward", 0) or 0)
    if reward <= 0:
        return 0
    limit = int(cfg.get("daily_gain_limit", 0) or 0)
    try:
        # 先锁行：把"查今日免费获取"和"发放"包进同一事务，避免并发超发
        _lock_user_row(db, user_id)
        # 非公益服用户不参与活力值体系
        user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
        if not user or not getattr(user, "is_welfare", False):
            db.rollback()
            return 0
        if limit > 0:
            gained = get_today_free_gain(db, user_id)
            remaining = limit - gained
            if remaining <= 0:
                db.rollback()
                return 0
            reward = min(reward, remaining)
        _add_vitality(db, user_id, reward, "watch_reward")
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("award_watch_reward failed for user %s", user_id)
        return 0
    return reward


def clamp_on_archive(db: Session, user_id: int) -> int:
    """归档/退群时把活力值钳制到 vitality_archive_clamp（只降不升）。

    返回钳制后的活力值。

    并发安全：先对用户行加锁，读-改-写在同一事务内完成。第二个并发调用在锁上
    等待前者提交后，读到的是已钳制的值（current <= clamp）直接返回，不会把值
    错误归零（修复读-改-写竞态）；且天然幂等。
    """
    cfg = get_vitality_config(db)
    clamp = int(cfg.get("archive_clamp", 0) or 0)
    current = None
    try:
        # 先锁行：读 current → 算 delta → _add_vitality 写回，三步原子
        _lock_user_row(db, user_id)
        row = db.query(models.WebUser.vitality).filter(models.WebUser.id == user_id).first()
        if not row:
            db.rollback()
            return 0
        current = int(row[0]) if row[0] is not None else 0
        if current <= clamp:
            db.rollback()
            return current
        delta = clamp - current  # 负数；clamp 已校验 <= max，落在 [0, max] 内
        final = _add_vitality(db, user_id, delta, "archive_clamp")
        db.commit()
        return final
    except Exception:
        db.rollback()
        logger.exception("clamp_on_archive failed for user %s", user_id)
        return current if current is not None else 0


_DEDUCT_BATCH = 500


def daily_deduct(db: Session) -> dict:
    """每日 00:00 扣减公益服用户活力值"""
    cfg = get_vitality_config(db)
    if not cfg["enabled"]:
        return {"deducted": 0, "suspended": 0, "failed": 0, "skipped": True}
    cost = cfg["daily_cost"]
    deducted = 0
    suspended = 0
    failed = 0
    last_id = 0
    # 键集分页（id > last_id）而不是 offset：过滤条件是 vitality > 0，本批扣到 0 的用户
    # 会从结果集里消失，offset 继续往后跳就会整批漏扣后面的用户
    while True:
        rows = (db.query(models.WebUser.id)
                .filter(models.WebUser.is_welfare == True, models.WebUser.vitality > 0,
                        models.WebUser.id > last_id)
                .order_by(models.WebUser.id).limit(_DEDUCT_BATCH).all())
        if not rows:
            break
        for (uid,) in rows:
            try:
                # PG 事务中任一语句失败后整个事务 aborted，后续语句全失败；存点把单个用户失败
                # 隔离在 ROLLBACK TO SAVEPOINT 范围内，不污染本批其他用户
                with db.begin_nested():
                    new_v = _add_vitality(db, uid, -cost, "daily_deduct")
                deducted += 1
                if new_v <= 0:
                    suspended += 1
            except Exception:
                failed += 1
                logger.exception("vitality daily_deduct failed for user %s", uid)
        # commit 失败语义不明不能重跑（可能重复扣减），停跑保留已提交批次
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("vitality daily_deduct batch commit failed, stop")
            break
        last_id = rows[-1][0]
    return {"deducted": deducted, "suspended": suspended, "failed": failed}

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()
CONFIG_LAST_DEDUCT_DATE = "vitality_last_deduct_date"

def _today_str() -> str:
    """「今天」的日期字符串（Asia/Shanghai 自然日），用于每日扣减认领标记。

    r34 口径收口：此前是服务器本地日期，生产容器为 UTC，「每日 00:00 扣减」
    实际在北京时间 08:00 才触发。认领标记只做「今天是否已扣」的幂等判断，
    改为上海日期后扣减在北京时间 00:00 过后 60 秒内触发。
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        return date.today().isoformat()

def claim_daily_deduct(db: Session, today: str) -> bool:
    """原子认领「今天的活力值扣减」：认领成功返回 True（本进程负责扣减），否则 False。

    旧实现是「读上次日期 → 扣减 → 再写日期」：多个 worker 进程/实例各有一个调度线程，
    会同时读到旧日期、各扣一遍（公益服用户活力被双倍扣减，提前停播）；扣减中途崩溃
    重启也会再扣一遍。现在先用 UPDATE ... WHERE value != today（无行时 INSERT，唯一键兜底）
    认领并提交，再扣减。代价：认领后扣减中途崩溃，当天少扣（对用户有利，不会重复扣）。
    """
    from sqlalchemy import or_
    from sqlalchemy.exc import IntegrityError

    claimed = (
        db.query(models.SystemConfig)
        .filter(
            models.SystemConfig.key == CONFIG_LAST_DEDUCT_DATE,
            or_(models.SystemConfig.value.is_(None), models.SystemConfig.value != today),
        )
        .update({"value": today}, synchronize_session=False)
    )
    if claimed:
        db.commit()
        return True
    exists = db.query(models.SystemConfig.id).filter(
        models.SystemConfig.key == CONFIG_LAST_DEDUCT_DATE).first()
    if exists:
        db.rollback()
        return False
    db.add(models.SystemConfig(key=CONFIG_LAST_DEDUCT_DATE, value=today))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def start_vitality_scheduler() -> bool:
    """启动每日活力值扣减调度（daemon 线程，同一进程只启动一次）

    上次执行日期持久化在 SystemConfig(vitality_last_deduct_date)：worker 重启
    当天不再重复扣减。
    """
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    def _tick():
        try:
            from backend.database import SessionLocal
            db = SessionLocal()
            try:
                today = _today_str()
                if store.get_value(db, CONFIG_LAST_DEDUCT_DATE, "") == today:
                    return
                # 先原子认领当天，再扣减：多进程/多实例同时跑调度时只有一个能扣
                if not claim_daily_deduct(db, today):
                    return
                store.invalidate(CONFIG_LAST_DEDUCT_DATE)
                result = daily_deduct(db)
                logger.info("活力值每日扣减完成: %s", result)
            finally:
                db.close()
        except Exception:
            logger.exception("vitality scheduler tick failed")

    def _loop():
        while True:
            threading.Event().wait(60)
            _tick()

    threading.Thread(target=_loop, daemon=True, name="vitality-scheduler").start()
    logger.info("活力值调度已启动（每 60 秒检查一次，每日 00:00 扣减）")
    return True

__all__ = [
    "VITALITY_DEFAULTS", "get_vitality_config", "vitality_status",
    "ensure_can_play", "daily_deduct", "claim_daily_deduct", "start_vitality_scheduler",
    "validate_vitality_config", "award_watch_reward", "clamp_on_archive",
    "get_today_free_gain", "FREE_GAIN_REASONS",
]

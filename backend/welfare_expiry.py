from datetime import datetime, timedelta
import logging
import threading
import time
from sqlalchemy.orm import Session
from backend import models
from backend.api import economy

logger = logging.getLogger(__name__)


def _get_int(db: Session, key: str, default: int) -> int:
    return economy._get_int_config(db, key, default)


def _safe_get_int(db: Session, key: str, default: int) -> int:
    """读取整型配置，读取失败时记录错误并返回默认值。"""
    try:
        return _get_int(db, key, default)
    except Exception as e:
        logger.error("读取配置 %s 失败，使用默认值 %s: %s", key, default, e, exc_info=True)
        return default


_SCAN_BATCH = 500


def _try_streak_renew(db: Session, user: models.WebUser, now: datetime) -> bool:
    """连签保资格：已过期用户若连签 alive 且 streak >= 阈值，续期 30 天、不扣分。

    alive：最近一条签到记录落在昨天或今天（上海自然日帧，与 CheckinRecord.checkin_date
    写入帧一致）；长度：该条记录的 streak 字段。两者缺一不可：断过（streak 被重置）
    或最近记录已 stale（3 天前），都不续。
    续期后 expires_at = now + 30 天 > now，下一轮 tick 不再过期、不重复续期（幂等）。
    查看凭证（EmbyViewUnlock）一律不动：查看到期仍需花 50 分续。
    """
    min_streak = _safe_get_int(db, "welfare_streak_renew_min", 30)
    today_start = economy._today_start_utc_naive()
    yesterday_start = today_start - timedelta(days=1)
    latest = (
        db.query(models.CheckinRecord)
        .filter(models.CheckinRecord.user_id == user.id)
        .order_by(models.CheckinRecord.checkin_date.desc())
        .first()
    )
    if latest is None:
        return False
    if not (yesterday_start <= latest.checkin_date < today_start + timedelta(days=1)):
        return False
    if (latest.streak or 0) < min_streak:
        return False
    user.welfare_expires_at = now + timedelta(days=30)
    db.add(models.WelfareGrantLog(user_id=user.id, channel="streak_renew", days=30,
                                  granted_by=None))
    db.commit()
    return True


def check_welfare_expiry(db: Session) -> dict:
    """扫描已过期的公益用户。

    连签保资格（在保留期/取消之前先判）：已过期且连签 alive、streak >= 阈值
    （welfare_streak_renew_min，默认 30）的用户直接续期 30 天、不扣分，记
    channel="streak_renew" 的 WelfareGrantLog，不走保留期/取消路径。
    过期时间在保留期（welfare_grace_days）内的用户仅记录，不修改状态；
    超过保留期的用户自动取消公益身份（is_welfare=False），
    并写入 channel="auto_expire" 的 WelfareGrantLog。
    单个用户处理失败时记录错误日志并继续处理下一个用户。
    键集分页（id > last_id）而不是 offset：过期用户被置 is_welfare=False 后会从
    结果集里消失，offset 继续往后跳就会整批漏扫后面的用户（与 vitality.daily_deduct
    同一口径）。续期用户仍是 is_welfare=True，但 last_id 按整批末尾推进，
    同一轮内不会重复处理。
    """
    now = datetime.now()
    grace = _safe_get_int(db, "welfare_grace_days", 7)
    grace_ids, expired_ids, renewed_ids = [], [], []
    checked = 0
    last_id = 0
    while True:
        try:
            users = (
                db.query(models.WebUser)
                .filter(models.WebUser.is_welfare == True, models.WebUser.id > last_id)
                .order_by(models.WebUser.id)
                .limit(_SCAN_BATCH)
                .all()
            )
        except Exception as e:
            logger.error("查询公益用户列表失败: %s", e, exc_info=True)
            return {"checked": checked, "grace": grace_ids, "expired": expired_ids,
                    "renewed": renewed_ids}
        if not users:
            break
        checked += len(users)
        for u in users:
            try:
                exp = u.welfare_expires_at
                if exp is None or exp > now:
                    continue
                if _try_streak_renew(db, u, now):
                    renewed_ids.append(u.id)
                    continue
                expired_days = (now - exp).days
                if expired_days < grace:
                    grace_ids.append(u.id)
                else:
                    u.is_welfare = False
                    db.add(models.WelfareGrantLog(user_id=u.id, channel="auto_expire", days=0, granted_by=None))
                    db.commit()
                    expired_ids.append(u.id)
            except Exception as e:
                db.rollback()
                logger.error("处理公益过期用户 %s 失败: %s", getattr(u, "id", "?"), e, exc_info=True)
                continue
        last_id = users[-1].id
    return {"checked": checked, "grace": grace_ids, "expired": expired_ids,
            "renewed": renewed_ids}


def check_inactive_users(db: Session) -> dict:
    """扫描长期不活跃的公益用户并禁用。

    仅检查未过期（welfare_expires_at 为空或晚于当前时间）的公益用户；
    以 last_login_at（缺失时回退到 welfare_granted_at）作为最后活跃时间，
    超过 inactive_days（welfare_inactive_days）未活跃的用户取消公益身份，
    并写入 channel="auto_inactive" 的 WelfareGrantLog。
    单个用户处理失败时记录错误日志并继续处理下一个用户。
    键集分页（id > last_id）而不是 offset：禁用用户被置 is_welfare=False 后会从
    结果集里消失，offset 继续往后跳就会整批漏扫后面的用户（与 vitality.daily_deduct
    同一口径）。
    """
    now = datetime.now()
    inactive_days = _safe_get_int(db, "welfare_inactive_days", 30)
    disabled_ids = []
    checked = 0
    last_id = 0
    while True:
        try:
            users = (
                db.query(models.WebUser)
                .filter(models.WebUser.is_welfare == True, models.WebUser.id > last_id)
                .order_by(models.WebUser.id)
                .limit(_SCAN_BATCH)
                .all()
            )
        except Exception as e:
            logger.error("查询公益用户列表失败: %s", e, exc_info=True)
            return {"checked": checked, "disabled": disabled_ids}
        if not users:
            break
        checked += len(users)
        for u in users:
            try:
                exp = u.welfare_expires_at
                if exp is not None and exp <= now:
                    continue
                last_active = u.last_login_at or u.welfare_granted_at
                if last_active is None:
                    continue
                if (now - last_active).days >= inactive_days:
                    u.is_welfare = False
                    db.add(models.WelfareGrantLog(user_id=u.id, channel="auto_inactive", days=0, granted_by=None))
                    db.commit()
                    disabled_ids.append(u.id)
            except Exception as e:
                db.rollback()
                logger.error("处理不活跃公益用户 %s 失败: %s", getattr(u, "id", "?"), e, exc_info=True)
                continue
        last_id = users[-1].id
    return {"checked": checked, "disabled": disabled_ids}
_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()


def start_welfare_expiry_scheduler() -> bool:
    """启动公益到期检查调度器（每 24 小时一次）。同一进程只启动一次。

    返回 True 表示本次调用成功启动，False 表示调度器已在运行。
    """
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    def _run():
        # 延迟导入 SessionLocal，避免启动时循环导入
        from backend.database import SessionLocal

        while True:
            db = SessionLocal()
            try:
                welfare_result = check_welfare_expiry(db)
                inactive_result = check_inactive_users(db)
                logger.info(
                    "welfare-expiry-scheduler tick: check_welfare_expiry=%s, check_inactive_users=%s",
                    welfare_result,
                    inactive_result,
                )
            except Exception as exc:
                logger.error("welfare-expiry-scheduler tick failed: %s", exc, exc_info=True)
            finally:
                db.close()
            time.sleep(86400)

    t = threading.Thread(target=_run, name="welfare-expiry-scheduler", daemon=True)
    t.start()
    return True
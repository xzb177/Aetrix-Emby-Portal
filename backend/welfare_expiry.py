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


def check_welfare_expiry(db: Session) -> dict:
    """扫描已过期的公益用户。

    过期时间在保留期（welfare_grace_days）内的用户仅记录，不修改状态；
    超过保留期的用户自动取消公益身份（is_welfare=False），
    并写入 channel="auto_expire" 的 WelfareGrantLog。
    单个用户处理失败时记录错误日志并继续处理下一个用户。
    键集分页（id > last_id）而不是 offset：过期用户被置 is_welfare=False 后会从
    结果集里消失，offset 继续往后跳就会整批漏扫后面的用户（与 vitality.daily_deduct
    同一口径）。
    """
    now = datetime.now()
    grace = _safe_get_int(db, "welfare_grace_days", 7)
    grace_ids, expired_ids = [], []
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
            return {"checked": checked, "grace": grace_ids, "expired": expired_ids}
        if not users:
            break
        checked += len(users)
        for u in users:
            try:
                exp = u.welfare_expires_at
                if exp is None or exp > now:
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
    return {"checked": checked, "grace": grace_ids, "expired": expired_ids}


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


def bulk_extend_welfare(db: Session, min_expired_days: int, max_expired_days: int, add_days: int, granted_by=None) -> dict:
    """批量延期：找出过期天数在 [min_expired_days, max_expired_days] 的公益用户，每人加 add_days 天。

    .. deprecated::
        已无调用方——管理后台走 ``api/welfare_admin.py::welfare_bulk_extend``
        的键集分页实现。保留仅防外部脚本引用，请勿新增调用，不删除。
    """
    from backend.emby_server import portal
    now = datetime.now()
    # (now - exp).days >= min  <=>  exp <= now - min days
    # (now - exp).days <= max  <=>  exp >  now - (max + 1) days
    lower = now - timedelta(days=max_expired_days + 1)
    upper = now - timedelta(days=min_expired_days)
    users = (
        db.query(models.WebUser)
        .filter(
            models.WebUser.is_welfare == True,
            models.WebUser.welfare_expires_at.isnot(None),
            models.WebUser.welfare_expires_at > lower,
            models.WebUser.welfare_expires_at <= upper,
        )
        .all()
    )
    extended = []
    for u in users:
        exp = u.welfare_expires_at
        ed = (now - exp).days
        if min_expired_days <= ed <= max_expired_days:
            portal.grant_welfare(db, u, channel="admin_bulk", days=add_days, granted_by=granted_by)
            extended.append(u.id)
    return {"extended": extended, "count": len(extended)}


def get_expiring_soon(db: Session, days: int = 3) -> list:
    """找出 days 天内到期的公益用户。"""
    now = datetime.now()
    # (exp - now).days >= 0  <=>  exp > now
    # (exp - now).days <= days  <=>  exp < now + (days + 1) days
    users = (
        db.query(models.WebUser)
        .filter(
            models.WebUser.is_welfare == True,
            models.WebUser.welfare_expires_at.isnot(None),
            models.WebUser.welfare_expires_at > now,
            models.WebUser.welfare_expires_at < now + timedelta(days=days + 1),
        )
        .all()
    )
    result = []
    for u in users:
        exp = u.welfare_expires_at
        days_left = (exp - now).days
        if 0 <= days_left <= days:
            result.append({"user_id": u.id, "expires_at": exp.isoformat(), "days_left": days_left})
    return result


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
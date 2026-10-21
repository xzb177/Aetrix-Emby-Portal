"""
管理后台 · 订阅到期提醒 API

续费提醒的执行逻辑在 `backend/reminders.py`（周期任务 + 去重），这里只提供面板需要的三件事：

- **看口径**：开关、提醒档位、当前待发条数、最近发送记录 —— 面板要能回答
  「这个功能到底有没有在跑」，而不只是显示一个开关的状态；
- **立即执行**：`dry_run=true` 只预览会发给谁（不落库、不发消息），
  `dry_run=false` 真发一轮（去重表保证不会重复打扰）；
- **改配置**：开关与提醒档位（默认 7/3/1 天）白名单写 `SystemConfig`。

单独成文件是因为 `backend/api/admin.py` 已经很大（既有的经济设置项在那里）。
鉴权口径与管理端完全一致：复用 `get_current_admin` + JWT。
"""
import logging

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import config_service, models, reminders
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_reminders_router = APIRouter(prefix="/api/admin", tags=["管理后台·到期提醒"])

# 可由面板改写的配置项白名单（其余键一律忽略）
# A 类开关摘面板（2026-10-21）：expiry_reminder_enabled 不再经面板写——
# 改走 env EXPIRY_REMINDER_ENABLED（缺省=开）；面板只剩提醒档位。
REMINDER_CONFIG_KEYS = (reminders.CONFIG_DAYS,)


class ReminderSettingsRequest(BaseModel):
    settings: dict


@admin_reminders_router.get("/economy/expiry-reminders")
async def get_expiry_reminders(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """到期提醒口径：开关 / 档位 / 待发条数 / 最近发送记录"""
    return reminders.reminder_status(db)


@admin_reminders_router.put("/economy/expiry-reminders/settings")
def update_expiry_reminder_settings(
    request: ReminderSettingsRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """更新到期提醒配置

    ``expiry_reminder_days`` 里非法的档位会被忽略，解析后为空则回落到默认 7/3/1，
    不会让后台任务因为一段写错的配置整体失效。
    """
    changed = {}
    values = {}
    for key, value in request.settings.items():
        if key not in REMINDER_CONFIG_KEYS:
            continue
        if key == reminders.CONFIG_ENABLED:
            # 摘面板后忽略（见 REMINDER_CONFIG_KEYS 注释），不再落库
            continue
        else:
            text = "" if value is None else str(value).strip()
            values[key] = text
            changed[key] = text
    if values:
        # 配置瘦身第 4 批 4-4：走统一写入通道（校验→落库→写时失效）；
        # store.write_values 内部已按 store 纪律失效，4-1 的失效覆盖由正统通道承接。
        config_service.save_config(db, values, schema="reminders")
    db.commit()

    status = reminders.reminder_status(db)
    _audit(db, current_admin, "update_expiry_reminder_settings", "system_config", None, changed)
    db.commit()
    return {"success": True, "settings": changed, "status": status}


@admin_reminders_router.post("/economy/expiry-reminders/run")
async def run_expiry_reminders(
    dry_run: bool = False,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """立即执行一轮到期提醒（dry_run=true 只看会发给谁）

    提醒本体（`reminders.run_expiry_reminders`）要 await 通知推送，留在事件循环上；
    它之后的审计是同步写库，单独下放线程池。
    """
    admin_id = current_admin.id

    def _audit_run() -> None:
        _audit(db, admin_id, "run_expiry_reminders", "subscription", None,
               {"reminded": summary["reminded"],
                "expired_notified": summary["expired_notified"]})
        db.commit()

    summary = await reminders.run_expiry_reminders(db, dry_run=dry_run)
    if not dry_run and (summary["reminded"] or summary["expired_notified"]):
        await run_in_threadpool(_audit_run)
    return summary


__all__ = ["admin_reminders_router"]

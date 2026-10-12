"""
管理后台 · 系统告警 API

系统告警的执行逻辑在 `backend/system_alerts.py`（定时检查 + 冷却去重 +
自动恢复），这里只提供面板需要的四件事：

- **看规则**：各事件的阈值 / 渠道 / 接收人 / 冷却 / 开关状态
- **改规则**：阈值、渠道、接收人、冷却、启用/停用（全部可配，不写死）
- **看记录**：告警历史（open / resolved），可手动确认
- **总开关**：一键开/关整个告警系统；测试按钮发一条测试告警

鉴权口径与管理端完全一致：复用 `get_current_admin` + JWT。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models, system_alerts
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_alerts_router = APIRouter(prefix="/api/admin", tags=["管理后台·系统告警"])

# 允许的渠道名白名单（防写错配置导致整轮检查失效）
ALLOWED_CHANNELS = ("in_app", "email", "telegram", "webhook")
ALLOWED_SEVERITIES = ("info", "warning", "critical")


def _rule_to_dict(rule: models.AlertRule) -> dict:
    try:
        channels = json.loads(rule.notification_channels) \
            if rule.notification_channels else []
        if not isinstance(channels, list):
            channels = []
    except Exception:  # noqa: BLE001
        channels = []
    try:
        recipients = json.loads(rule.recipients) if rule.recipients else {"staff": True}
        if not isinstance(recipients, dict):
            recipients = {"staff": True}
    except Exception:  # noqa: BLE001
        recipients = {"staff": True}
    return {
        "id": rule.id,
        "name": rule.name,
        "event_type": rule.event_type,
        "threshold": rule.threshold,
        "severity": rule.severity,
        "channels": channels,
        "recipients": recipients,
        "cooldown_sec": rule.cooldown_sec,
        "is_active": bool(rule.is_active),
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


class RuleUpsertRequest(BaseModel):
    name: str = Field("", max_length=100)
    threshold: float = 0
    severity: str = "warning"
    channels: list[str] = Field(default_factory=list)
    recipients: dict = Field(default_factory=lambda: {"staff": True})
    cooldown_sec: int = 3600
    is_active: bool = True


def _sanitize_rule_input(req: RuleUpsertRequest) -> dict:
    severity = req.severity if req.severity in ALLOWED_SEVERITIES else "warning"
    channels = [c for c in (req.channels or []) if c in ALLOWED_CHANNELS] or ["in_app"]
    recipients = req.recipients if isinstance(req.recipients, dict) else {"staff": True}
    # 只认两种接收人形态，其他一律回落为全体管理员（不让写错的配置静默丢告警）
    if not (recipients.get("staff") or recipients.get("user_ids")):
        recipients = {"staff": True}
    cooldown = max(60, min(86400 * 7, int(req.cooldown_sec or 3600)))
    return {
        "severity": severity,
        "channels": channels,
        "recipients": recipients,
        "cooldown_sec": cooldown,
    }


@admin_alerts_router.get("/alerts/rules")
def list_alert_rules(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """列出全部事件型告警规则（不存在时先按内置默认播种）。"""
    system_alerts.ensure_default_rules(db)
    rules = db.query(models.AlertRule).filter(
        models.AlertRule.event_type.isnot(None)
    ).order_by(models.AlertRule.id).all()
    return {
        "enabled": system_alerts.enabled(db),
        "events": list(system_alerts.ALL_EVENTS),
        "allowed_channels": list(ALLOWED_CHANNELS),
        "rules": [_rule_to_dict(r) for r in rules],
    }


@admin_alerts_router.put("/alerts/rules/{rule_id}")
def update_alert_rule(
    rule_id: int,
    req: RuleUpsertRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """修改一条规则的阈值 / 渠道 / 接收人 / 冷却 / 严重度 / 开关。"""
    rule = db.query(models.AlertRule).filter(
        models.AlertRule.id == rule_id,
        models.AlertRule.event_type.isnot(None),
    ).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    clean = _sanitize_rule_input(req)
    if req.name:
        rule.name = req.name[:100]
    rule.threshold = float(req.threshold)
    rule.severity = clean["severity"]
    rule.notification_channels = json.dumps(clean["channels"])
    rule.recipients = json.dumps(clean["recipients"])
    rule.cooldown_sec = clean["cooldown_sec"]
    rule.is_active = bool(req.is_active)
    db.commit()
    _audit(db, current_admin, "update_alert_rule", "alert_rule", rule_id,
           {"event_type": rule.event_type, **clean})
    return _rule_to_dict(rule)


class AlertSettingsRequest(BaseModel):
    enabled: bool = False
    check_interval_sec: int = 600


@admin_alerts_router.get("/alerts/settings")
def get_alert_settings(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """告警总开关与检查间隔。"""
    import os
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == system_alerts.MASTER_SWITCH_KEY).first()
    return {
        "enabled": system_alerts.enabled(db),
        "enabled_source": "env" if os.getenv(system_alerts.MASTER_SWITCH_ENV)
        and system_alerts._truthy(os.getenv(system_alerts.MASTER_SWITCH_ENV))
        else "db",
        "enabled_value": (row.value if row else "0"),
        "check_interval_sec": system_alerts._env_int(
            system_alerts.CHECK_INTERVAL_ENV,
            system_alerts.DEFAULT_CHECK_INTERVAL_SEC),
    }


@admin_alerts_router.put("/alerts/settings")
def update_alert_settings(
    req: AlertSettingsRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """总开关与检查间隔（环境变量优先：设置了环境变量时 DB 开关不生效）。"""
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == system_alerts.MASTER_SWITCH_KEY).first()
    val = "1" if req.enabled else "0"
    if row:
        row.value = val
    else:
        db.add(models.SystemConfig(
            key=system_alerts.MASTER_SWITCH_KEY, value=val,
            description="系统告警总开关（1=开，0=关）"))
    db.commit()
    _audit(db, current_admin, "update_alert_settings", "system_config", None,
           {"system_alerts_enabled": val})
    return {"enabled": system_alerts.enabled(db)}


@admin_alerts_router.get("/alerts")
def list_alerts(
    status: str = "open",
    limit: int = 50,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """告警记录：open（待处理）/ resolved（已恢复）/ all。"""
    q = db.query(models.Alert)
    if status in ("open", "resolved", "acknowledged"):
        q = q.filter(models.Alert.status == status)
    rows = q.order_by(models.Alert.created_at.desc()).limit(
        max(1, min(200, limit))).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "rule_id": r.rule_id,
            "event_type": r.rule.event_type if r.rule else None,
            "severity": r.severity,
            "title": r.title,
            "message": r.message,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        })
    return {"alerts": out}


@admin_alerts_router.post("/alerts/{alert_id}/ack")
def ack_alert(
    alert_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动确认一条告警（open → acknowledged，不再参与自动恢复计数）。"""
    from datetime import datetime
    row = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    if row.status == "open":
        row.status = "acknowledged"
        db.commit()
        _audit(db, current_admin, "ack_alert", "alert", alert_id, {})
    return {"id": row.id, "status": row.status}


class TestAlertRequest(BaseModel):
    channel: str = "in_app"


@admin_alerts_router.post("/alerts/test")
async def send_test_alert(
    req: TestAlertRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """发一条测试告警（走真实渠道，只发给当前管理员，用于验证渠道配置）。

    总开关关闭时也允许测试——否则配完渠道没法验证。
    """
    channel = req.channel if req.channel in ALLOWED_CHANNELS else "in_app"
    from backend import notifications as _ntf
    svc = _ntf.get_notification_service()
    title = "系统告警测试"
    content = "这是一条测试告警，说明告警渠道工作正常。"
    ok = False
    if channel == "webhook":
        ch = svc.channels.get("webhook")
        ok = bool(ch and getattr(ch, "enabled", False) and await ch.send(
            current_admin.id, title, content,
            data={"event_type": "test", "severity": "info"}))
    else:
        results = await svc.send(
            user_id=current_admin.id, title=title, content=content,
            channels=[channel], message_type="alert.test", db=db)
        ok = bool(results.get(channel))
    _audit(db, current_admin, "send_test_alert", "system", None,
           {"channel": channel, "ok": ok})
    return {"ok": ok, "channel": channel}

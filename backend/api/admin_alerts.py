"""
管理后台 · 系统告警 API

系统告警的执行逻辑在 `backend/system_alerts.py`（定时检查 + 冷却去重 +
自动恢复），这里只提供面板需要的五件事：

- **看规则**：各事件的阈值 / 渠道 / 接收人 / 冷却 / 开关状态
- **改规则**：阈值、渠道、接收人、冷却、启用/停用（全部可配，不写死）
- **看记录**：告警历史（open / resolved / acknowledged），可手动确认
- **总开关与设置**：一键开/关整个告警系统；检查间隔、webhook 通知 URL 与
  签名密钥（R16 P1-1/P2-4：以前这些是"能填但无效"的假控件，现在真写库、
  保存后立即生效）；测试按钮发一条测试告警

鉴权口径与管理端完全一致：复用 `get_current_admin` + JWT。
"""
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from backend import config_service, models, system_alerts
from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

admin_alerts_router = APIRouter(prefix="/api/admin", tags=["管理后台·系统告警"])

# 允许的渠道名白名单（防写错配置导致整轮检查失效）
ALLOWED_CHANNELS = ("in_app", "email", "telegram", "webhook")
ALLOWED_SEVERITIES = ("info", "warning", "critical")


def _rule_to_dict(rule: models.AlertRule) -> dict:
    # R16 P3-14：notification_channels 是 JSON 列，直接读 list（兼容老的
    # json.dumps 字符串写法，双写过渡期两边都读得出来）
    raw_channels = rule.notification_channels
    if isinstance(raw_channels, list):
        channels = raw_channels
    else:
        try:
            channels = json.loads(raw_channels) if raw_channels else []
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
    # R16 P3-6：负数钳掉（阈值不该是负的）
    rule.threshold = max(0.0, float(req.threshold))
    rule.severity = clean["severity"]
    # R16 P3-14：JSON 列直接写 list；recipients 是 Text 列，仍写 json 字符串
    rule.notification_channels = clean["channels"]
    rule.recipients = json.dumps(clean["recipients"])
    rule.cooldown_sec = clean["cooldown_sec"]
    rule.is_active = bool(req.is_active)
    db.commit()
    _audit(db, current_admin, "update_alert_rule", "alert_rule", rule_id,
           {"event_type": rule.event_type, **clean})
    return _rule_to_dict(rule)


def _get_config_value(db: Session, key: str) -> Optional[str]:
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    return row.value if row else None


class AlertSettingsRequest(BaseModel):
    # R16 P1-1：全部 Optional——前端做"只改间隔"的部分更新时，不带 enabled
    # 的 body 不再被 pydantic 缺省 False 顺手关掉总开关
    enabled: Optional[bool] = None
    check_interval_sec: Optional[int] = None
    # R16 P2-4：webhook 配置入口（以前只能直连 DB 手写）
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None


@admin_alerts_router.get("/alerts/settings")
def get_alert_settings(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """告警总开关、检查间隔、webhook 配置（secret 只返回是否已设置，不回显）。"""
    return {
        "enabled": system_alerts.enabled(db),
        # R16 P3-5：环境变量设了"0"也算 env 在控制（实际 effective 是关）
        "enabled_source": "env" if os.getenv(
            system_alerts.MASTER_SWITCH_ENV) is not None else "db",
        "enabled_value": (_get_config_value(
            db, system_alerts.MASTER_SWITCH_KEY) or "0"),
        # R16 P1-1：返回真实生效值（DB 配置 > 环境变量 > 默认）
        "check_interval_sec": system_alerts.current_interval_sec(db),
        "webhook_url": _get_config_value(db, "webhook_alert_url") or "",
        "webhook_secret_set": bool(_get_config_value(
            db, "webhook_alert_secret")),
    }


@admin_alerts_router.put("/alerts/settings")
def update_alert_settings(
    req: AlertSettingsRequest,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """总开关 / 检查间隔 / webhook 配置（环境变量优先：设置了环境变量时
    DB 开关不生效）。只应用请求里非 None 的字段——部分更新不会误关总开关
    （R16 P1-1）。"""
    changed: dict = {}
    values: dict = {}  # 实际落库的 {SystemConfig 键: 待校验值}（走 config_service 统一通道）

    if req.enabled is not None:
        values[system_alerts.MASTER_SWITCH_KEY] = req.enabled
        changed["system_alerts_enabled"] = "1" if req.enabled else "0"

    if req.check_interval_sec is not None:
        # R16 P1-1：钳制 60~86400 保留在端点层（业务行为不变）；注册表范围与钳制边界对齐
        iv = max(60, min(86400, int(req.check_interval_sec)))
        values[system_alerts.INTERVAL_KEY] = iv
        changed["alert_check_interval_sec"] = iv

    if req.webhook_url is not None:
        url = (req.webhook_url or "").strip()
        values["webhook_alert_url"] = url
        changed["webhook_alert_url"] = url

    secret_changed = False
    if req.webhook_secret is not None:
        # 空串走统一通道的「未设置」语义（删行）；读侧 bool() 口径与空行一致，行为不变
        values["webhook_alert_secret"] = req.webhook_secret or ""
        # 注意：secret 本身不进审计日志，只记"改了"
        changed["webhook_secret_set"] = bool(req.webhook_secret)
        secret_changed = True

    if changed:
        # 配置瘦身第 4 批 4-3：走统一写入通道（校验→落库→写时失效）。
        # store.write_values 内部已按 store 纪律失效，4-1 的失效覆盖由正统通道承接。
        config_service.save_config(
            db, values, schema="alerts",
            descriptions={
                system_alerts.MASTER_SWITCH_KEY: "系统告警总开关（1=开，0=关）",
                system_alerts.INTERVAL_KEY: "系统告警检查间隔（秒），60~86400",
                "webhook_alert_url": "告警 webhook 通知 URL（可选）",
                "webhook_alert_secret": "告警 webhook HMAC 签名密钥（可选）",
            },
        )
        db.commit()
        # R16 P3-11：webhook 后配也要 refresh_channels()，否则单例缓存里
        # 的渠道还是旧配置（邮件/Telegram 保存时已经是这个口径）
        if "webhook_alert_url" in changed or secret_changed:
            from backend import notifications as _ntf
            _ntf.refresh_channels()
        _audit(db, current_admin, "update_alert_settings", "system_config",
               None, changed)

    return {
        "enabled": system_alerts.enabled(db),
        "check_interval_sec": system_alerts.current_interval_sec(db),
        "changed": changed,
    }


@admin_alerts_router.get("/alerts")
def list_alerts(
    status: str = "open",
    limit: int = 50,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """告警记录：open（待处理）/ acknowledged（已确认）/ resolved（已恢复）/ all。

    R16 P2-1：joinedload 预取 rule，一次查完（以前逐行懒加载，最坏 200 次查询）。
    R16 P1-2/P2-3：返回 notified_ok（区分已送达/发送失败）与 acknowledged_at。
    """
    q = db.query(models.Alert).options(joinedload(models.Alert.rule))
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
            "notified_ok": bool(r.notified_ok),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "acknowledged_at": r.acknowledged_at.isoformat()
            if r.acknowledged_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        })
    return {"alerts": out}


@admin_alerts_router.post("/alerts/{alert_id}/ack")
def ack_alert(
    alert_id: int,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """手动确认一条告警（open → acknowledged）。

    R16 P2-3：确认后冷却期内同一规则不再重复打扰（notify_alert 去重认
    acknowledged）；条件恢复后自动 resolved（生命周期闭环）。R16 P3-4：
    记录确认时间（把原来没用到的 datetime 导入用起来）。
    """
    from datetime import datetime
    row = db.query(models.Alert).filter(models.Alert.id == alert_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    if row.status == "open":
        row.status = "acknowledged"
        row.acknowledged_at = datetime.now()
        db.commit()
        _audit(db, current_admin, "ack_alert", "alert", alert_id, {})
    return {"id": row.id, "status": row.status,
            "acknowledged_at": row.acknowledged_at.isoformat()
            if row.acknowledged_at else None}


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

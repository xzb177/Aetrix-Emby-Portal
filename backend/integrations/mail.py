"""能力：邮件与模板（SMTP）

站内通知渠道里的「邮件」其实早就实现了投递（``backend/notifications.py`` 的 ``EmailChannel``：
465 走 SSL、其余走 STARTTLS），但一直**没有任何界面能把它配起来**——这个模块补上配置与测试。

配置键沿用既有的 ``email_*``（通知服务正是按这个前缀加载渠道），所以保存后刷新一次通知服务，
站内通知就会同时投递到用户邮箱，不需要改调用点。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

SPEC = {
    "title": "邮件与模板",
    "desc": "SMTP、发件人与发件地址；保存后站内通知会同时投递到用户邮箱。",
    "group": "邮件与消息",
    "docs_hint": "端口 465 走 SSL，其余端口走 STARTTLS（例如 587）。",
    "fields": [
        {"key": "email_enabled", "label": "启用邮件通知", "type": "bool", "default": "false"},
        {"key": "email_smtp_host", "label": "SMTP 服务器", "type": "str", "required": True,
         "placeholder": "smtp.example.com"},
        {"key": "email_smtp_port", "label": "端口", "type": "int", "default": "587"},
        {"key": "email_smtp_user", "label": "账号", "type": "str", "placeholder": "noreply@example.com"},
        {"key": "email_smtp_password", "label": "密码 / 授权码", "type": "secret"},
        {"key": "email_from_name", "label": "发件人显示名", "type": "str", "default": ""},
        {"key": "email_from_email", "label": "发件地址", "type": "str", "default": "",
         "hint": "留空则用 SMTP 账号"},
    ],
    "test_label": "发送测试邮件",
}

CONFIG_KEYS = ("email_enabled", "email_smtp_host", "email_smtp_port", "email_smtp_user",
               "email_smtp_password", "email_from_name", "email_from_email")


def load_config(db: Session) -> dict:
    """统一热读（只许这一套）：短 TTL 缓存，保存时失效"""
    from backend.integrations import store
    return store.get_values(db, CONFIG_KEYS, {k: "" for k in CONFIG_KEYS})


def build_channel(db: Session):
    """按当前配置构造通知渠道（与通知服务用的是同一个类）"""
    from backend.notifications import EmailChannel

    cfg = load_config(db)
    if cfg.get("email_enabled", "").lower() != "true":
        return None
    if not cfg.get("email_smtp_host") or not cfg.get("email_smtp_user"):
        return None
    try:
        port = int(cfg.get("email_smtp_port") or 587)
    except ValueError:
        port = 587
    return EmailChannel(
        smtp_host=cfg.get("email_smtp_host"),
        smtp_port=port,
        smtp_user=cfg.get("email_smtp_user"),
        smtp_password=cfg.get("email_smtp_password"),
        from_name=cfg.get("email_from_name") or "",
        from_email=cfg.get("email_from_email") or "",
    )


def apply(db: Session) -> None:
    """保存后刷新通知渠道：不重启也能让邮件渠道立刻生效 / 失效"""
    from backend.notifications import refresh_channels

    refresh_channels()


def is_configured(values: dict) -> bool:
    """字段是否齐全（不看开关）"""
    return bool(values.get("email_smtp_host")) and bool(values.get("email_smtp_user"))


def is_enabled(values: dict) -> bool:
    """当前是否真的能发信"""
    return str(values.get("email_enabled") or "").lower() == "true" and is_configured(values)


def test(db: Session, payload: dict) -> dict:
    """真实发一封信（默认发给「发件地址」或 SMTP 账号）"""
    cfg = load_config(db)
    channel = build_channel(db)
    if channel is None:
        return {"ok": False, "message": "邮件渠道未启用或缺少 SMTP 服务器 / 账号（先保存再测）"}
    to_email = (payload.get("to") or cfg.get("email_from_email")
                or cfg.get("email_smtp_user") or "").strip()
    if not to_email:
        return {"ok": False, "message": "请填写收件地址"}
    ok, error = channel._deliver(  # noqa: SLF001 — 直接验证投递路径，与通知走同一段代码
        to_email,
        "测试邮件 · 邮件与模板",
        "这是一封来自后台「系统设置 → 邮件与模板」的测试邮件。\n"
        "收到它说明 SMTP 配置可用，站内通知会同时投递到这个邮箱。",
    )
    if ok:
        return {"ok": True, "message": f"已投递到 {to_email}", "detail": {"to": to_email}}
    return {"ok": False, "message": f"投递失败：{error}", "detail": {"to": to_email}}

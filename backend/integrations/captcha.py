"""能力：人机验证（Turnstile / reCAPTCHA / hCaptcha）

为登录、注册等动作加一道人机验证。站点密钥与私钥由管理员自己申请后填进来——项目不内置任何
密钥，也不锁定任何一家提供方。

行为约定：

- 没选提供方 / 没填密钥 → **直接放行**（不能把站点锁在门外），后台只提示「未配置」；
- 打开保护的动作要求请求体带 ``captcha_token``（前端挂件生成），校验失败一律 400；
- 校验走提供方 ``siteverify``，失败原因原样返回（便于排查密钥、域名或时效问题）；
- 受保护的动作清单由字段表派生（``captcha_protect_<动作>``），所以加一个动作只要加一行字段，
  前端挂件与后台表单都会跟着出现。

读配置走一次批量查询（原先挂件一次要问 5 个键 = 5 次往返）。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from backend.integrations import store

SPEC = {
    "title": "人机验证",
    "desc": "为登录、注册等动作配置 Turnstile、reCAPTCHA 或 hCaptcha。",
    "group": "网络与安全",
    "docs_hint": "保护的是用户端登录 / 注册与管理后台登录；私钥只报「已配置」，前端挂件用站点密钥。",
    "fields": [
        {"key": "captcha_provider", "label": "提供方", "type": "select", "default": "none",
         "options": ["none", "turnstile", "recaptcha", "hcaptcha"]},
        {"key": "captcha_site_key", "label": "站点密钥（Site Key）", "type": "str", "default": ""},
        {"key": "captcha_secret_key", "label": "私钥（Secret Key）", "type": "secret", "default": ""},
    ],
    "test_label": "测试密钥",
}

VERIFY_URLS = {
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    "recaptcha": "https://www.google.com/recaptcha/api/siteverify",
    "hcaptcha": "https://hcaptcha.com/siteverify",
}
PROVIDER_LABELS = {"turnstile": "Cloudflare Turnstile", "recaptcha": "Google reCAPTCHA",
                   "hcaptcha": "hCaptcha"}
# 前端挂件需要的脚本地址（后台下发，前端不写死）
WIDGET_SCRIPTS = {
    "turnstile": "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
    "recaptcha": "https://www.google.com/recaptcha/api.js?render=explicit",
    "hcaptcha": "https://js.hcaptcha.com/1/api.js?render=explicit",
}

# 受保护的动作：与字段表里的 captcha_protect_<动作> 一一对应
# （后台登录也走同一套，否则唯一没被保护的就剩后台那个最值钱的入口）
ACTIONS: tuple[str, ...] = ("login", "register", "admin_login")

_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}
_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
# A 类开关摘面板（2026-10-21）：三个保护动作开关不再出现在能力表单里，
# 改走 env CAPTCHA_PROTECT_LOGIN / _REGISTER / _ADMIN_LOGIN；缺省与原
# 表单缺省（=生产现值）一致：登录/注册开、管理后台登录关。读取集合保留。
# 能力框架的原始值读取补读（卡片/挂件口径与运行时一致）
EXTRA_READ_KEYS = {
    "captcha_protect_login": "true",
    "captcha_protect_register": "true",
    "captcha_protect_admin_login": "false",
}
_PROTECT_DEFAULTS = {
    "captcha_protect_login": "true",
    "captcha_protect_register": "true",
    "captcha_protect_admin_login": "false",
}
_FIELD_KEYS = _FIELD_KEYS + list(_PROTECT_DEFAULTS)
_FIELD_DEFAULTS = {**_FIELD_DEFAULTS, **_PROTECT_DEFAULTS}


def _config(db: Session) -> dict:
    """一次查询取回本能力的全部配置"""
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def _provider_of(cfg: dict) -> str:
    name = (cfg.get("captcha_provider") or "none").strip().lower()
    return name if name in VERIFY_URLS else "none"


def provider(db: Session) -> str:
    return _provider_of(_config(db))


def secret_key(db: Session) -> str:
    return _config(db).get("captcha_secret_key") or ""


def _protected(cfg: dict, action: str) -> bool:
    if _provider_of(cfg) == "none" or not (cfg.get("captcha_secret_key") or ""):
        return False
    return str(cfg.get(f"captcha_protect_{action}") or "").strip().lower() == "true"


def is_protected(db: Session, action: str) -> bool:
    """某个动作（login / register / admin_login）是否要校验"""
    return _protected(_config(db), action)


def widget_info(db: Session) -> dict:
    """给前端挂件的信息（只含公开的站点密钥；未启用时不给任何密钥）"""
    cfg = _config(db)
    name = _provider_of(cfg)
    site_key = cfg.get("captcha_site_key") or ""
    enabled = name != "none" and bool(cfg.get("captcha_secret_key")) and bool(site_key)
    return {
        "enabled": enabled,
        "provider": name if enabled else "none",
        "label": PROVIDER_LABELS.get(name, "") if enabled else "",
        "site_key": site_key if enabled else "",
        "script_url": WIDGET_SCRIPTS.get(name, "") if enabled else "",
        "actions": {a: _protected(cfg, a) for a in ACTIONS},
    }


def _verify(db: Session, cfg: dict, token: Optional[str], ip: str = "") -> tuple[bool, str]:
    """调用提供方 siteverify；未配置时视为通过"""
    name = _provider_of(cfg)
    secret = cfg.get("captcha_secret_key") or ""
    if name == "none" or not secret:
        return True, ""
    token = (token or "").strip()
    if not token:
        return False, "请完成人机验证"
    import httpx

    data = {"secret": secret, "response": token}
    if ip:
        data["remoteip"] = ip
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(VERIFY_URLS[name], data=data)
        body = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001
        # P2 修复（审查）：此前注释声称"不能把用户挡在门外太久"，但代码实际是
        # fail-closed（返回 False → guard 抛 400）。现明确产品决策：
        # 验证码服务不可达时 fail-closed（登录/注册暂停），安全优先于可用性。
        # 防爆破不依赖验证码单点，另有登录限流兜底。
        logger.warning("人机验证服务不可达（fail-closed）: %s", type(exc).__name__)
        return False, f"人机验证服务不可达：{type(exc).__name__}"
    if body.get("success"):
        return True, ""
    codes = body.get("error-codes") or []
    return False, f"人机验证失败（{', '.join(str(c) for c in codes) or resp.status_code}）"


def verify_token(db: Session, token: Optional[str], ip: str = "") -> tuple[bool, str]:
    return _verify(db, _config(db), token, ip)


def verify_request(db: Session, action: str, token: Optional[str], ip: str = "") -> tuple[bool, str]:
    """端点里用的入口：没开保护直接放行（一次查询搞定）"""
    cfg = _config(db)
    if not _protected(cfg, action):
        return True, ""
    return _verify(db, cfg, token, ip)


def guard(db: Session, request, action: str, token: Optional[str],
          username: Optional[str] = None) -> None:
    """端点里的守卫：校验失败一律 400（并把提供方原因带上）+ 落一条安全日志

    用户在用户端登录、注册与管理员在后台登录都走这一个入口——三处各写一遍校验的话，
    迟早有一处漏掉「失败要留痕」这件事。
    """
    from fastapi import HTTPException

    from backend.authlog import client_ip, record_event, user_agent

    ip = client_ip(request)
    ok, message = verify_request(db, action, token, ip)
    if ok:
        return
    record_event(
        db, username=username, ip=ip, agent=user_agent(request),
        success=False, reason="captcha_failed", detail=message[:255],
    )
    raise HTTPException(status_code=400, detail=message)


def is_configured(values: dict) -> bool:
    """选了提供方并把两把密钥都填了才算配过"""
    name = str(values.get("captcha_provider") or "none").lower()
    if name not in VERIFY_URLS:
        return False
    return bool(values.get("captcha_secret_key")) and bool(values.get("captcha_site_key"))


def is_enabled(values: dict) -> bool:
    """当前是否真在拦人"""
    return is_configured(values)


def test(db: Session, payload: dict) -> dict:
    """拿一个必然无效的 token 打一次 siteverify：

    能收到 ``invalid-input-response`` 说明**密钥有效、域名可达**（如果密钥错会返回
    ``invalid-input-secret``）。这是在不依赖前端挂件的前提下，能对密钥做的最直接验证。
    """
    cfg = _config(db)
    name = _provider_of(cfg)
    if name == "none":
        return {"ok": False, "message": "未选择提供方（先在「提供方」里选一家并填密钥）"}
    secret = cfg.get("captcha_secret_key") or ""
    if not secret:
        return {"ok": False, "message": "未填私钥（Secret Key）"}
    import httpx

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(VERIFY_URLS[name], data={"secret": secret, "response": "codebuff-probe"})
        body = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"{PROVIDER_LABELS.get(name, name)} 不可达：{type(exc).__name__}: {exc}"}
    codes = [str(c) for c in (body.get("error-codes") or [])]
    if "invalid-input-secret" in codes or "invalid-secret" in codes:
        return {"ok": False, "message": "私钥无效（invalid-input-secret），请核对 Secret Key",
                "detail": {"provider": name, "error_codes": codes}}
    if body.get("success"):
        return {"ok": True, "message": "密钥有效（这个 token 竟然通过了，请确认没有把 token 校验关掉）",
                "detail": {"provider": name}}
    return {"ok": True,
            "message": f"密钥有效：{PROVIDER_LABELS.get(name, name)} 已接受该私钥（对无效 token 返回 {', '.join(codes) or 'ok'}）",
            "detail": {"provider": name, "error_codes": codes}}

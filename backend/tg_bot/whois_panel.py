# -*- coding: utf-8 -*-
"""TG Bot /whois 管理员面板（二期，EmbyBoss /kk 式）。

入口：handlers.handle_whois 在定位到具体用户（群里回复/带参、私聊
回复/带参）时返回 (精简卡文本, 本模块按钮)；翻页与写操作走
callbacks 的 whois:* 回调，每次点击都重新校验点击者 is_staff。

页面：
- main：第一页 = whois.render_whois_lite 精简卡（字段一字不改）＋
  子视图/写操作按钮；
- bind：绑定详情（各 provider 的状态与最近验证时间；绑定表无到期
  字段，不编造）；
- cred：资格与凭证（公益资格状态/到期、各服查看凭证状态/到期）；
- gift：赠送菜单（预设档位按钮，无自由输入）；
- ask/do：写操作二次确认——ask 只出确认页（写明对谁、做什么、
  多少），do 才真正执行；执行后回 main 页并回显最新状态。

写操作只许三类（用户 2026-11-02 点选），全部复用现行服务、不另
写一套：封禁/解封走 backend.user_lifecycle.set_user_active（与网站
管理端同一实现，停用同事务吊销 Emby 令牌＋抬 token_version）；
赠能量走 backend.api.economy._add_points（admin_grant 台账，说明
注明机器人管理员赠送＋操作人）；赠资格走
backend.emby_server.portal.grant_welfare；赠凭证走
backend.codes._grant_bundled_view_unlock（points_spent=0 赠送口径）。
每次写操作另写一条治理审计（governance.record_governance_audit）。
不做删号。
"""
from __future__ import annotations

import html
import logging
from datetime import datetime

from backend import models
from backend.tg_bot import layout, whois

logger = logging.getLogger(__name__)

#: 写操作预设档位（op → (动作名, 内容说明)），不做自由输入。
OPS: dict[str, tuple[str, str]] = {
    "ban": ("封禁账号", "停用账号并吊销其全部 Emby 令牌"),
    "unban": ("解封账号", "恢复账号启用状态"),
    "gp50": ("赠送能量", "+50 能量"),
    "gp100": ("赠送能量", "+100 能量"),
    "gp500": ("赠送能量", "+500 能量"),
    "gw7": ("赠送公益资格", "+7 天"),
    "gw30": ("赠送公益资格", "+30 天"),
    "gv30": ("赠送查看凭证", "+30 天"),
}

_GIFT_OPS = ("gp50", "gp100", "gp500", "gw7", "gw30", "gv30")


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def main_markup(user) -> dict:
    """面板第一页按钮：子视图＋写操作入口（封禁/解封按当前状态二选一）。"""
    uid = user.id
    ban_btn = (
        {"text": "✅ 解封账号", "callback_data": f"whois:ask:{uid}:unban"}
        if not bool(getattr(user, "is_active", True))
        else {"text": "⛔ 封禁账号", "callback_data": f"whois:ask:{uid}:ban"}
    )
    return {"inline_keyboard": [
        [
            {"text": "🔗 绑定详情", "callback_data": f"whois:bind:{uid}"},
            {"text": "🎟️ 资格与凭证", "callback_data": f"whois:cred:{uid}"},
        ],
        [
            ban_btn,
            {"text": "🎁 赠送", "callback_data": f"whois:gift:{uid}"},
        ],
    ]}


def _back_markup(user, page: str = "main") -> dict:
    return {"inline_keyboard": [[
        {"text": "⬅️ 返回面板", "callback_data": f"whois:{page}:{user.id}"},
    ]]}


def main_page(db, user, tg_display_name: str | None = None,
              note: str | None = None) -> tuple[str, dict]:
    """面板第一页：精简卡（字段不动）＋按钮；note 为操作结果回显。"""
    text = whois.render_whois_lite(db, user, tg_display_name)
    if note:
        text = f"{text}\n\n{note}"
    return text, main_markup(user)


def bindings_page(db, user) -> tuple[str, dict]:
    """绑定详情页：每个绑定一行（provider＋账号名＋状态＋最近验证）。"""
    from backend.emby_server import models as em
    from backend.tg_bot.queries import BINDING_STATUS_TEXT, PROVIDER_TEXT

    rows = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user.id)
        .order_by(em.UserStorageBinding.id)
        .all()
    )
    lines = []
    if not rows:
        lines.append(layout.row("🔗", "网盘绑定", "未绑定任何网盘"))
    for row in rows:
        provider = PROVIDER_TEXT.get(row.provider, _esc(row.provider))
        status = BINDING_STATUS_TEXT.get(row.status, _esc(row.status))
        verified = (
            row.last_verified_at.strftime("%Y-%m-%d %H:%M")
            if row.last_verified_at else "—")
        lines.append(layout.row(
            "🔗", "网盘绑定",
            f"{provider}（{_esc(row.account_label)}）｜{status}"
            f"，最近验证 {verified}"))
    text = layout.card(
        layout.title("绑定详情", f"{user.username}（#{user.id}）"), lines)
    return text, _back_markup(user)


def cred_page(db, user) -> tuple[str, dict]:
    """资格与凭证页：公益资格状态/到期＋各服查看凭证状态/到期。"""
    from backend.tg_bot import features

    welfare = features._welfare_line(user).split("：", 1)[-1]
    if not bool(getattr(user, "is_welfare", False)):
        welfare_detail = "未开通"
    elif getattr(user, "welfare_expires_at", None) is None:
        welfare_detail = "永不过期"
    else:
        welfare_detail = user.welfare_expires_at.strftime("%Y-%m-%d %H:%M")
    lines = [
        layout.row("📊", "公益资格", welfare),
        layout.row("📅", "资格到期", welfare_detail),
    ]
    unlocks = (
        db.query(models.EmbyViewUnlock)
        .filter(models.EmbyViewUnlock.user_id == user.id)
        .order_by(models.EmbyViewUnlock.id)
        .all()
    )
    if not unlocks:
        lines.append(layout.row("🎟️", "查看凭证", "未开通"))
    now = datetime.now()
    for row in unlocks:
        if row.expires_at is None:
            state = "生效中（永久）"
        elif row.expires_at > now:
            state = f"生效中，至 {row.expires_at.strftime('%Y-%m-%d')}"
        else:
            state = f"已过期（{row.expires_at.strftime('%Y-%m-%d')}）"
        lines.append(layout.row(
            "🎟️", "查看凭证", f"服 #{row.realm_id}｜{state}"))
    text = layout.card(
        layout.title("资格与凭证", f"{user.username}（#{user.id}）"), lines)
    return text, _back_markup(user)


def gift_page(db, user) -> tuple[str, dict]:
    """赠送菜单页：三类赠送的预设档位按钮（点档位先进确认页）。"""
    uid = user.id
    text = layout.card(
        layout.title("赠送", f"{user.username}（#{user.id}）"),
        [
            layout.row("🎁", "赠送对象", f"<b>{_esc(user.username)}</b>（#{uid}）"),
            layout.row("👇", "选择档位", "点下方按钮选档位，下一步还要确认"),
        ],
    )
    markup = {"inline_keyboard": [
        [
            {"text": "✨ +50", "callback_data": f"whois:ask:{uid}:gp50"},
            {"text": "✨ +100", "callback_data": f"whois:ask:{uid}:gp100"},
            {"text": "✨ +500", "callback_data": f"whois:ask:{uid}:gp500"},
        ],
        [
            {"text": "📊 资格 +7 天", "callback_data": f"whois:ask:{uid}:gw7"},
            {"text": "📊 资格 +30 天", "callback_data": f"whois:ask:{uid}:gw30"},
        ],
        [
            {"text": "🎟️ 凭证 +30 天", "callback_data": f"whois:ask:{uid}:gv30"},
        ],
        [{"text": "⬅️ 返回面板", "callback_data": f"whois:main:{uid}"}],
    ]}
    return text, markup


def confirm_page(db, user, op: str) -> tuple[str, dict]:
    """写操作确认页：写明对谁、做什么、多少；确认按钮才执行。"""
    action_name, detail = OPS[op]
    uid = user.id
    text = layout.card(
        layout.title("确认操作"),
        [
            layout.row("👤", "操作对象", f"<b>{_esc(user.username)}</b>（#{uid}）"),
            layout.row("🛠️", "执行动作", action_name),
            layout.row("📦", "操作内容", detail),
            layout.row("⚠️", "提示说明", "点「确认执行」才会真正生效"),
        ],
    )
    back_page = "gift" if op in _GIFT_OPS else "main"
    markup = {"inline_keyboard": [
        [{"text": "✅ 确认执行", "callback_data": f"whois:do:{uid}:{op}"}],
        [{"text": "⬅️ 取消", "callback_data": f"whois:{back_page}:{uid}"}],
    ]}
    return text, markup


def _audit(db, action: str, admin, target, detail: dict) -> None:
    from backend import governance

    governance.record_governance_audit(
        db, action=action, actor_user_id=admin.id,
        target_user_id=target.id, target_ref=f"user:{target.id}",
        detail=detail)


def execute(db, admin, target, op: str) -> str:
    """执行已确认的写操作，返回结果回显文案（面板 main 页展示）。

    全部走现行服务（见模块头注），本函数只做编排＋审计＋提交。
    """
    from backend.user_lifecycle import set_user_active

    name = _esc(target.username)
    if op == "ban":
        if target.id == admin.id:
            return "⛔ 不能封禁自己的账号（与网站管理端口径一致）。"
        changed = set_user_active(db, target, False)
        if changed:
            _audit(db, "whois_ban", admin, target, {"via": "tg_whois_panel"})
            db.commit()
            return f"✅ 已封禁 <b>{name}</b>：账号已停用，Emby 令牌已全部吊销。"
        return f"ℹ️ <b>{name}</b> 已经是停用状态，未做变更。"
    if op == "unban":
        changed = set_user_active(db, target, True)
        if changed:
            _audit(db, "whois_unban", admin, target, {"via": "tg_whois_panel"})
            db.commit()
            return f"✅ 已解封 <b>{name}</b>：账号恢复启用（旧令牌不自动恢复）。"
        return f"ℹ️ <b>{name}</b> 已经是启用状态，未做变更。"
    if op in ("gp50", "gp100", "gp500"):
        from backend.api.economy import _add_points

        amount = int(op[2:])
        balance = _add_points(
            db, target, amount, "admin_grant",
            f"机器人管理员赠送（操作人 {admin.username}）",
            f"tg_whois:{admin.id}")
        _audit(db, "whois_gift_points", admin, target,
               {"amount": amount, "balance": balance})
        db.commit()
        return f"✅ 已给 <b>{name}</b> 赠送 {amount} 能量，当前余额 {balance}。"
    if op in ("gw7", "gw30"):
        from backend.emby_server.portal import grant_welfare

        days = int(op[2:])
        expires = grant_welfare(
            db, target, "tg_whois_gift", days, granted_by=admin.id)
        _audit(db, "whois_gift_welfare", admin, target, {"days": days})
        db.commit()
        exp_text = (
            expires.strftime("%Y-%m-%d") if expires else "永不过期")
        return f"✅ 已给 <b>{name}</b> 赠送公益资格 {days} 天，到期 {exp_text}。"
    if op == "gv30":
        from backend import codes, realms

        realm_id = realms.legacy_realm_id(db)
        if realm_id is None:
            realm_id = realms.ensure_default_realm(db).id
        codes._grant_bundled_view_unlock(db, target, realm_id, 30)
        _audit(db, "whois_gift_view", admin, target,
               {"days": 30, "realm_id": realm_id})
        db.commit()
        row = (
            db.query(models.EmbyViewUnlock)
            .filter(models.EmbyViewUnlock.user_id == target.id,
                    models.EmbyViewUnlock.realm_id == realm_id)
            .first())
        exp_text = (
            row.expires_at.strftime("%Y-%m-%d")
            if row is not None and row.expires_at else "永久")
        return f"✅ 已给 <b>{name}</b> 赠送查看凭证 30 天，到期 {exp_text}。"
    return "未知操作。"


def render(db, admin, user, page: str, op: str | None = None,
           ) -> tuple[str, dict]:
    """面板翻页统一渲染口（callbacks 调用；admin 已过 is_staff 校验）。"""
    if page == "bind":
        return bindings_page(db, user)
    if page == "cred":
        return cred_page(db, user)
    if page == "gift":
        return gift_page(db, user)
    if page == "ask" and op in OPS:
        return confirm_page(db, user, op)
    if page == "do" and op in OPS:
        note = execute(db, admin, user, op)
        db.refresh(user)
        return main_page(db, user, note=note)
    return main_page(db, user)

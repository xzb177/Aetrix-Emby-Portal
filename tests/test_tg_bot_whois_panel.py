# -*- coding: utf-8 -*-
"""TG Bot /kk 管理员面板（二期）：翻页子视图＋封禁/赠送写操作。

口径（用户 2026-11-02 二期定夺）：
- /kk 面板第一页 = 现行精简卡（render_whois_lite 字段一字不改），
  带按钮翻页：绑定详情页（provider＋状态＋最近验证）、资格与凭证页
  （资格到期、凭证状态）；面板只经回复/带参定位到具体用户时出现。
- 写操作只有封禁/解封与赠送三项（能量 +50/+100/+500、资格 +7/+30 天、
  凭证 +30 天，预设档位按钮、无自由输入），不做删号。
- 封禁必须走与网站管理端同一服务 backend.user_lifecycle.set_user_active
  （停用同事务吊销 Emby 令牌＋抬 token_version，PA-002 链）；赠送分别走
  economy._add_points（admin_grant 台账）、portal.grant_welfare、
  codes._grant_bundled_view_unlock 现行口径，并写治理审计留痕。
- 每个写操作二次确认：点动作只出确认页（状态不变），点确认才执行；
  每次点击都校验点击者是管理员，非管理员点击直接拒绝、不带目标数据。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em
from backend.realms import ensure_default_realm
from backend.tg_bot import callbacks, handlers, sender

STAFF_TG = 700001
PLAIN_TG = 700002
TARGET_TG = 700003
GROUP = -100123


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, tg, name, staff=False, **kw):
    fields = {"password_hash": "x", "telegram_id": tg, "points": 10,
              "vitality": 14, "is_active": True, "is_staff": staff}
    # 生产 staff 必有角色；测试默认 operator（可写），显式传 role 的用例
    # （PA-056 守卫等）不受影响。角色门用例见 test_tg_bot_admin_roles.py。
    if staff and "admin_role" not in kw:
        fields["admin_role"] = "operator"
    fields.update(kw)
    u = models.WebUser(username=name, **fields)
    db.add(u)
    db.commit()
    return u


def _click(db, monkeypatch, data, tg=STAFF_TG, chat_id=GROUP):
    edited = []

    def fake_answer(db_, cq_id, text=None, show_alert=False):
        return (True, None)

    def fake_edit(db_, cid, mid, text_, reply_markup=None, parse_mode="HTML"):
        edited.append((text_, reply_markup))
        return (True, None)

    monkeypatch.setattr(sender, "answer_callback_query", fake_answer)
    monkeypatch.setattr(sender, "edit_message_text", fake_edit)
    cq = {
        "id": "cq1", "data": data, "from": {"id": tg, "first_name": "Clicker"},
        "message": {"message_id": 5, "chat": {"id": chat_id, "type": "group"}},
    }
    callbacks.handle_callback(db, cq)
    return edited


def _datas(markup):
    return [b.get("callback_data") for row in (markup or {}).get("inline_keyboard", [])
            for b in row if b.get("callback_data")]


def _panel(db):
    staff = _user(db, STAFF_TG, "boss", staff=True)
    target = _user(db, TARGET_TG, "targetx")
    return staff, target


# ---------- 第一页：精简卡＋面板按钮 ----------

def test_whois_group_opens_panel_with_buttons(db):
    _staff, target = _panel(db)
    reply = handlers.handle_whois(
        db, {"id": STAFF_TG}, GROUP, "targetx", in_group=True)
    assert isinstance(reply, tuple)
    text, markup = reply
    assert "账号速览" in text or "targetx" in text
    assert "能量余额" in text  # 精简卡字段在
    datas = _datas(markup)
    assert f"whois:bind:{target.id}" in datas
    assert f"whois:cred:{target.id}" in datas
    assert f"whois:gift:{target.id}" in datas
    assert f"whois:ask:{target.id}:ban" in datas  # 正常账号出封禁入口


def test_whois_panel_bindings_and_cred_pages(db, monkeypatch):
    _staff, target = _panel(db)
    realm = ensure_default_realm(db)
    db.add(em.UserStorageBinding(
        user_id=target.id, provider="drive", account_label="主号",
        credential_id=1, status="active", last_verified_at=datetime(2026, 11, 1, 8, 0)))
    db.add(models.EmbyViewUnlock(
        user_id=target.id, realm_id=realm.id,
        unlocked_at=datetime(2026, 10, 20),
        expires_at=datetime(2026, 12, 1), points_spent=0))
    db.commit()
    edited = _click(db, monkeypatch, f"whois:bind:{target.id}")
    assert edited
    text, _m = edited[0]
    assert "谷歌网盘" in text and "正常" in text
    assert "2026-11-01" in text  # 最近验证时间
    edited = _click(db, monkeypatch, f"whois:cred:{target.id}")
    text, _m = edited[0]
    assert "查看凭证" in text and "2026-12-01" in text


# ---------- 管理员校验：非管理员点击拒绝 ----------

def test_whois_panel_click_by_non_staff_rejected(db, monkeypatch):
    # PA-057 口径：非管理员点击不再改写共享面板（旧拒绝卡已废），
    # 只给点按者弹 toast 拒绝提示；面板原文不动、目标数据不泄露。
    _staff, target = _panel(db)
    _user(db, PLAIN_TG, "plainuser")
    edited, answers = _click_with_answers(
        db, monkeypatch, f"whois:bind:{target.id}", tg=PLAIN_TG)
    assert edited == []
    assert "仅管理员可用" in answers


# ---------- 二次确认：点动作不出事，点确认才执行 ----------

def test_ban_requires_confirmation_and_uses_shared_service(db, monkeypatch):
    _staff, target = _panel(db)
    token = em.EmbyApiToken(user_id=target.id, token="tok-1", is_revoked=False)
    db.add(token)
    db.commit()
    version_before = int(target.token_version or 0)
    # 第一步：点封禁只出确认页，状态不动
    edited = _click(db, monkeypatch, f"whois:ask:{target.id}:ban")
    assert edited
    text, markup = edited[0]
    assert "确认" in text and "targetx" in text and "封禁" in text
    db.refresh(target)
    assert target.is_active is True
    assert f"whois:do:{target.id}:ban" in _datas(markup)
    # 第二步：点确认才执行——停用＋令牌吊销＋token_version+1（PA-002 链）
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:ban")
    assert edited
    db.refresh(target)
    db.refresh(token)
    assert target.is_active is False
    assert token.is_revoked is True
    assert int(target.token_version or 0) == version_before + 1
    assert "已停用" in edited[0][0]  # 结果回显最新状态
    # 审计留痕
    from backend import governance
    actions = [r.action for r in governance.list_audit_logs(db, limit=10)]
    assert "whois_ban" in actions


def test_unban_restores_active(db, monkeypatch):
    _staff, target = _panel(db)
    target.is_active = False
    db.commit()
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:unban")
    assert edited
    db.refresh(target)
    assert target.is_active is True


def test_admin_cannot_ban_self_via_panel(db, monkeypatch):
    staff, _target = _panel(db)
    edited = _click(db, monkeypatch, f"whois:do:{staff.id}:ban")
    assert edited
    db.refresh(staff)
    assert staff.is_active is True  # 不能封自己（与网站管理端口径一致）


# ---------- 赠送三项：预设档位落账 ----------

def _do_data_from_ask(db, monkeypatch, target, op):
    """走 ask 确认页取 do 回调（含一次性 nonce），贴近真实点击链路。"""
    edited = _click(db, monkeypatch, f"whois:ask:{target.id}:{op}")
    assert edited
    datas = [d for d in _datas(edited[0][1])
             if d.startswith(f"whois:do:{target.id}:{op}")]
    assert datas, "确认页须带 do 回调"
    return datas[0]


def test_gift_points_lands_with_ledger_and_audit(db, monkeypatch):
    _staff, target = _panel(db)
    before = int(target.points or 0)
    edited = _click(db, monkeypatch,
                    _do_data_from_ask(db, monkeypatch, target, "gp100"))
    assert edited
    db.refresh(target)
    assert int(target.points or 0) == before + 100
    log = (db.query(models.PointsLog)
           .filter(models.PointsLog.user_id == target.id)
           .order_by(models.PointsLog.id.desc()).first())
    assert log is not None and int(log.amount) == 100
    assert log.type == "admin_grant"
    assert "机器人管理员赠送" in (log.description or "")
    assert "boss" in (log.description or "")  # 注明操作人
    from backend import governance
    actions = [r.action for r in governance.list_audit_logs(db, limit=10)]
    assert "whois_gift_points" in actions


def test_gift_welfare_lands(db, monkeypatch):
    _staff, target = _panel(db)
    edited = _click(db, monkeypatch,
                    _do_data_from_ask(db, monkeypatch, target, "gw7"))
    assert edited
    db.refresh(target)
    assert target.is_welfare is True
    assert target.welfare_expires_at is not None
    assert target.welfare_expires_at > datetime.now() + timedelta(days=6)


def test_gift_view_unlock_lands(db, monkeypatch):
    _staff, target = _panel(db)
    realm = ensure_default_realm(db)
    edited = _click(db, monkeypatch,
                    _do_data_from_ask(db, monkeypatch, target, "gv30"))
    assert edited
    row = (db.query(models.EmbyViewUnlock)
           .filter(models.EmbyViewUnlock.user_id == target.id,
                   models.EmbyViewUnlock.realm_id == realm.id).first())
    assert row is not None
    assert int(row.points_spent or 0) == 0  # 赠送标记，不扣能量
    assert row.expires_at is not None
    assert row.expires_at > datetime.now() + timedelta(days=29)


# ---------- PA-057：拒绝分支只弹 toast，绝不改写共享面板 ----------

def _click_with_answers(db, monkeypatch, data, tg=STAFF_TG, chat_id=GROUP):
    edited = []
    answers = []

    def fake_answer(db_, cq_id, text=None, show_alert=False):
        answers.append(text)
        return (True, None)

    def fake_edit(db_, cid, mid, text_, reply_markup=None, parse_mode="HTML"):
        edited.append((text_, reply_markup))
        return (True, None)

    monkeypatch.setattr(sender, "answer_callback_query", fake_answer)
    monkeypatch.setattr(sender, "edit_message_text", fake_edit)
    cq = {
        "id": "cq1", "data": data, "from": {"id": tg, "first_name": "Clicker"},
        "message": {"message_id": 5, "chat": {"id": chat_id, "type": "group"}},
    }
    callbacks.handle_callback(db, cq)
    return edited, answers


def test_pa057_non_staff_click_leaves_shared_panel_intact(db, monkeypatch):
    _staff, target = _panel(db)
    _user(db, PLAIN_TG, "plainuser")
    edited, answers = _click_with_answers(
        db, monkeypatch, f"whois:bind:{target.id}", tg=PLAIN_TG)
    assert edited == []  # 群共享面板原文不动
    assert "仅管理员可用" in answers  # 仅点按者收到拒绝提示


def test_pa057_missing_target_click_leaves_shared_panel_intact(db, monkeypatch):
    _staff, _target = _panel(db)
    edited, answers = _click_with_answers(
        db, monkeypatch, "whois:bind:999999")
    assert edited == []  # 目标不存在也不改写共享面板
    assert "目标账号不存在" in answers
# ---------- PA-056：面板封禁/解封须复用网站端管理员目标守卫 ----------

def _admin(db, tg, name, role):
    return _user(db, tg, name, staff=True, admin_role=role)


def test_pa056_operator_cannot_ban_super(db, monkeypatch):
    operator = _admin(db, 700011, "op", "operator")
    target = _admin(db, 700012, "sup", "super")
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:ban", tg=700011)
    assert edited
    db.refresh(target)
    assert target.is_active is True  # 拒绝，未封禁
    assert "超级管理员" in edited[0][0]


def test_pa056_operator_cannot_unban_super(db, monkeypatch):
    operator = _admin(db, 700011, "op", "operator")
    target = _admin(db, 700012, "sup", "super")
    target.is_active = False
    db.commit()
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:unban", tg=700011)
    assert edited
    db.refresh(target)
    assert target.is_active is False  # 拒绝，未解封
    assert "超级管理员" in edited[0][0]


def test_pa056_operator_cannot_ban_other_admin(db, monkeypatch):
    operator = _admin(db, 700011, "op", "operator")
    target = _admin(db, 700013, "op2", "operator")
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:ban", tg=700011)
    assert edited
    db.refresh(target)
    assert target.is_active is True
    assert "超级管理员" in edited[0][0]


def test_pa056_super_can_ban_operator(db, monkeypatch):
    sup = _admin(db, 700011, "sup", "super")
    target = _admin(db, 700013, "op2", "operator")
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:ban", tg=700011)
    assert edited
    db.refresh(target)
    assert target.is_active is False  # 超管封普通管理员，口径不变
    assert "已封禁" in edited[0][0]


def test_pa056_last_loginable_super_cannot_be_banned(db, monkeypatch):
    # 仅剩一名可登录超管时，连超管身份的操作者（自身已停用）也不能封他；
    # 网站端同款口径（_locked_loginable_super_count <= 1 → 拒绝）。
    actor = _admin(db, 700011, "sup_off", "super")
    actor.is_active = False
    target = _admin(db, 700012, "sup", "super")
    db.commit()
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:ban", tg=700011)
    assert edited
    db.refresh(target)
    assert target.is_active is True
    assert "超级管理员" in edited[0][0]


def test_pa056_super_ban_super_with_two_active_allowed(db, monkeypatch):
    sup1 = _admin(db, 700011, "sup1", "super")
    sup2 = _admin(db, 700012, "sup2", "super")
    edited = _click(db, monkeypatch, f"whois:do:{sup2.id}:ban", tg=700011)
    assert edited
    db.refresh(sup2)
    assert sup2.is_active is False  # 还有另一名可登录超管，允许
    assert "已封禁" in edited[0][0]

# ---------- PA-058：赠送确认一次性 nonce，同一 ask 双击只发一次 ----------

def test_pa058_double_click_gifts_once(db, monkeypatch):
    _staff, target = _panel(db)
    before = int(target.points or 0)
    do_data = _do_data_from_ask(db, monkeypatch, target, "gp100")
    edited = _click(db, monkeypatch, do_data)
    assert edited
    db.refresh(target)
    assert int(target.points or 0) == before + 100
    # 同一 ask 的 do 回调第二次到达：不再落账，只回已执行提示
    edited = _click(db, monkeypatch, do_data)
    assert edited
    db.refresh(target)
    assert int(target.points or 0) == before + 100
    assert "已经执行过" in edited[0][0]


def test_pa058_do_without_nonce_refused(db, monkeypatch):
    _staff, target = _panel(db)
    before = int(target.points or 0)
    # 不经 ask、直接拼旧格式 do 回调：无有效 nonce，拒绝落账
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:gp100")
    assert edited
    db.refresh(target)
    assert int(target.points or 0) == before
    assert "已失效" in edited[0][0]


# ---------- 结构守卫：网站管理端与面板共用同一封禁服务 ----------

def test_admin_api_uses_same_set_user_active_service():
    src = open("backend/api/admin.py", encoding="utf-8").read()
    assert "set_user_active" in src
    import backend.user_lifecycle as lifecycle
    assert callable(lifecycle.set_user_active)

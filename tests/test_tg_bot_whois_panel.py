# -*- coding: utf-8 -*-
"""TG Bot /whois 管理员面板（二期）：翻页子视图＋封禁/赠送写操作。

口径（用户 2026-11-02 二期定夺）：
- /whois 面板第一页 = 现行精简卡（render_whois_lite 字段一字不改），
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
    fields.update(kw)
    u = models.WebUser(username=name, **fields)
    db.add(u)
    db.commit()
    return u


def _click(db, monkeypatch, data, tg=STAFF_TG, chat_id=GROUP):
    edited = []

    def fake_answer(db_, cq_id):
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
    _staff, target = _panel(db)
    _user(db, PLAIN_TG, "plainuser")
    edited = _click(db, monkeypatch, f"whois:bind:{target.id}", tg=PLAIN_TG)
    assert edited
    text, _m = edited[0]
    assert "仅管理员可用" in text
    assert "targetx" not in text


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

def test_gift_points_lands_with_ledger_and_audit(db, monkeypatch):
    _staff, target = _panel(db)
    before = int(target.points or 0)
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:gp100")
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
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:gw7")
    assert edited
    db.refresh(target)
    assert target.is_welfare is True
    assert target.welfare_expires_at is not None
    assert target.welfare_expires_at > datetime.now() + timedelta(days=6)


def test_gift_view_unlock_lands(db, monkeypatch):
    _staff, target = _panel(db)
    realm = ensure_default_realm(db)
    edited = _click(db, monkeypatch, f"whois:do:{target.id}:gv30")
    assert edited
    row = (db.query(models.EmbyViewUnlock)
           .filter(models.EmbyViewUnlock.user_id == target.id,
                   models.EmbyViewUnlock.realm_id == realm.id).first())
    assert row is not None
    assert int(row.points_spent or 0) == 0  # 赠送标记，不扣能量
    assert row.expires_at is not None
    assert row.expires_at > datetime.now() + timedelta(days=29)


# ---------- 结构守卫：网站管理端与面板共用同一封禁服务 ----------

def test_admin_api_uses_same_set_user_active_service():
    src = open("backend/api/admin.py", encoding="utf-8").read()
    assert "set_user_active" in src
    import backend.user_lifecycle as lifecycle
    assert callable(lifecycle.set_user_active)

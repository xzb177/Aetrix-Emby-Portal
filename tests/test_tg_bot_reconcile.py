# -*- coding: utf-8 -*-
"""TG Bot 群-号一致性对账 /reconcile（embyboss 借鉴第 3 项）。

口径：
- 仅私聊、仅网站管理员（is_staff 现行判定，与 handle_admin 同一口径）；
  群里发命令由 router private_only 拦 + handler 层私聊 guard 双保险。
- 默认只报告、零自动处置：报告阶段不写任何行、不改任何字段。
- 处置只有一类（取消公益资格），且必须二次确认：报告卡按钮（ask）
  只出确认卡、不执行；确认卡按钮（do）才执行。执行口径与退群处置
  （#1527）一致：is_welfare=False + WelfareGrantLog(channel=
  "group_leave", days=0, granted_by=操作管理员)，另写 AdminLog 审计；
  不删号、不动能量与查看凭证。执行前对每个目标逐一复核在群状态，
  已回群/接口失败的一律跳过。
- 覆盖必须如实：Telegram Bot API 没有群成员枚举接口，「在群未绑号」
  不可全量统计；②③ 只覆盖已绑号用户且逐一 getChatMember 核验，
  接口失败（api_error）单独计「未核验」，绝不计入「不在群」。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.integrations import store
from backend.tg_bot import ephemeral, handlers, reconcile, router, sender

STAFF_TG = 700100
GROUP_ID = -100777

# tg_id -> (is_member, status)：模拟 getChatMember 结果
MEMBER_MAP = {
    STAFF_TG: (True, "administrator"),
    700101: (True, "member"),        # alice：在群
    700102: (False, "left"),         # bob：不在群、无资格 → ②
    700103: (False, "kicked"),       # carol：不在群、有资格 → ③
    700104: (None, "api_error"),     # dave：接口失败 → 未核验
}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    for table in (
        models.WebUser.__table__,
        models.SystemConfig.__table__,
        models.WelfareGrantLog.__table__,
        models.AdminLog.__table__,
    ):
        table.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    router._user_cmd_limits.clear()
    router._group_windows.clear()
    reconcile.clear_cache()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None),
    )
    monkeypatch.setattr(
        sender, "send_message_ex",
        lambda db_, chat_id, text, reply_markup=None, **kw: out.append(text) or (True, None, 9001),
    )
    monkeypatch.setattr(ephemeral, "schedule_delete", lambda *a, **kw: True)
    return out


@pytest.fixture()
def edited(monkeypatch):
    out = []

    def _edit(db_, chat_id, message_id, text, reply_markup=None, **kw):
        out.append({"chat_id": chat_id, "message_id": message_id,
                    "text": text, "markup": reply_markup})
        return True, None

    monkeypatch.setattr(sender, "edit_panel", _edit)
    monkeypatch.setattr(sender, "answer_callback_query",
                        lambda db_, cq_id, text=None, show_alert=False: (True, None))
    return out


@pytest.fixture()
def member(monkeypatch):
    calls = []
    state = dict(MEMBER_MAP)

    def _check(db_, chat_id, user_id):
        calls.append((chat_id, user_id))
        return state.get(int(user_id), (False, "not_found"))

    monkeypatch.setattr(sender, "get_chat_member", _check)
    return {"calls": calls, "state": state}


def _user(db, username, tg_id=None, staff=False, **kw):
    u = models.WebUser(username=username, telegram_id=tg_id)
    u.password_hash = ""
    u.is_staff = staff
    for k, v in kw.items():
        setattr(u, k, v)
    db.add(u)
    db.commit()
    return u


def _seed(db, with_group=True):
    if with_group:
        db.add(models.SystemConfig(key="bot_group_ids", value=str(GROUP_ID)))
    admin = _user(db, "boss", STAFF_TG, staff=True, is_welfare=True)
    _user(db, "alice", 700101, is_welfare=True)
    _user(db, "bob", 700102, is_welfare=False)
    _user(db, "carol", 700103, is_welfare=True)
    _user(db, "dave", 700104, is_welfare=True)
    _user(db, "erin", None, is_welfare=True)  # 未绑 TG：不在对账总体内
    db.commit()
    return admin


def _snapshot(db):
    users = {
        u.id: (u.username, u.is_welfare, u.points)
        for u in db.query(models.WebUser).all()
    }
    return (
        users,
        db.query(models.WelfareGrantLog).count(),
        db.query(models.AdminLog).count(),
    )


def _msg_update(tg_id, chat_id, text, chat_type="private"):
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "text": text,
            "from": {"id": tg_id, "first_name": "T"},
            "chat": {"id": chat_id, "type": chat_type},
        },
    }


def _cq_update(tg_id, data, chat_id=None):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": tg_id, "first_name": "T"},
            "data": data,
            "message": {
                "message_id": 55,
                "chat": {"id": chat_id if chat_id is not None else tg_id,
                         "type": "private"},
            },
        },
    }


def test_classification_and_report(db, member):
    _seed(db)
    text, markup = handlers.handle_reconcile(db, {"id": STAFF_TG}, STAFF_TG, "")
    # 总体：已绑号 5 人（erin 未绑 TG 不计）
    assert "<b>已绑号用户</b> | 5" in text
    # ② 已绑但不在群：bob + carol = 2
    assert "<b>已绑但不在群</b> | 2 人" in text
    assert "bob" in text and "carol" in text
    # ③ 其中仍有公益资格：carol = 1
    assert "<b>仍有公益资格</b> | 1 人" in text
    # 未核验：dave = 1，且不被算进「不在群」
    assert "<b>未核验</b> | 1 人" in text
    assert "dave" in text
    # ① 不可枚举的覆盖声明必须在报告里
    assert "无法全量统计" in text
    # 报告卡带处置入口按钮（只进确认，不直接执行）
    datas = [
        b["callback_data"]
        for row in markup["inline_keyboard"] for b in row
    ]
    assert "reconcile:ask" in datas
    assert "reconcile:do" not in datas


def test_report_only_writes_nothing(db, member):
    _seed(db)
    before = _snapshot(db)
    handlers.handle_reconcile(db, {"id": STAFF_TG}, STAFF_TG, "")
    assert _snapshot(db) == before


def test_non_staff_rejected(db, member):
    _seed(db)
    text = handlers.handle_reconcile(db, {"id": 700101}, 700101, "")
    assert "仅管理员可用" in text
    assert member["calls"] == []  # 没过门就不许触发任何核验


def test_unbound_gets_bind_guide(db, member):
    _seed(db)
    text = handlers.handle_reconcile(db, {"id": 799999}, 799999, "")
    assert "如何绑定账号" in text
    assert member["calls"] == []


def test_group_chat_not_executed(db, sent, member):
    _seed(db)
    router.dispatch(db, _msg_update(STAFF_TG, GROUP_ID, "/reconcile", "group"))
    assert any("请私聊" in t for t in sent)
    assert member["calls"] == []


def test_two_clicks_required_then_execute(db, sent, edited, member):
    admin = _seed(db)
    before = _snapshot(db)
    router.dispatch(db, _msg_update(STAFF_TG, STAFF_TG, "/reconcile"))
    assert _snapshot(db) == before  # 报告零写入

    # 第一次点击：只出确认卡，仍然零写入
    router.dispatch(db, _cq_update(STAFF_TG, "reconcile:ask"))
    assert edited and "确认" in edited[-1]["text"]
    assert "carol" in edited[-1]["text"]
    assert _snapshot(db) == before

    # 第二次点击：才执行
    router.dispatch(db, _cq_update(STAFF_TG, "reconcile:do"))
    carol = db.query(models.WebUser).filter_by(username="carol").one()
    assert carol.is_welfare is False
    log = db.query(models.WelfareGrantLog).filter_by(user_id=carol.id).one()
    assert log.channel == "group_leave"
    assert log.days == 0
    assert log.granted_by == admin.id
    audit = db.query(models.AdminLog).filter_by(target_id=carol.id).one()
    assert audit.admin_user_id == admin.id
    assert audit.action == "welfare_revoke"
    # bob 无资格不在处置名单：没有任何留痕
    bob = db.query(models.WebUser).filter_by(username="bob").one()
    assert db.query(models.WelfareGrantLog).filter_by(user_id=bob.id).count() == 0
    # 回执卡列明实际处置名单
    assert "carol" in edited[-1]["text"]


def test_non_staff_callback_blocked(db, edited, member):
    _seed(db)
    handlers.handle_reconcile(db, {"id": STAFF_TG}, STAFF_TG, "")
    before = _snapshot(db)
    router.dispatch(db, _cq_update(700101, "reconcile:ask"))
    router.dispatch(db, _cq_update(700101, "reconcile:do"))
    assert _snapshot(db) == before


def test_execute_rechecks_membership(db, edited, member):
    _seed(db)
    handlers.handle_reconcile(db, {"id": STAFF_TG}, STAFF_TG, "")
    # 确认期间 carol 已回群：执行时必须跳过
    member["state"][700103] = (True, "member")
    router.dispatch(db, _cq_update(STAFF_TG, "reconcile:ask"))
    router.dispatch(db, _cq_update(STAFF_TG, "reconcile:do"))
    carol = db.query(models.WebUser).filter_by(username="carol").one()
    assert carol.is_welfare is True
    assert db.query(models.WelfareGrantLog).filter_by(user_id=carol.id).count() == 0
    assert "carol" in edited[-1]["text"]  # 回执里列明被跳过


def test_no_groups_configured(db, member):
    _seed(db, with_group=False)
    result = handlers.handle_reconcile(db, {"id": STAFF_TG}, STAFF_TG, "")
    text = result[0] if isinstance(result, tuple) else result
    assert "未配置名单群" in text
    assert member["calls"] == []


def test_confirm_card_truncation_noted(db, member, monkeypatch):
    _seed(db)
    monkeypatch.setattr(reconcile, "CONFIRM_LIMIT", 0)
    handlers.handle_reconcile(db, {"id": STAFF_TG}, STAFF_TG, "")
    text, _markup = reconcile.render_confirm(db, STAFF_TG)
    assert "等共 1 人" in text


def test_switch_off_replies_closed(db, sent, member):
    _seed(db)
    db.add(models.SystemConfig(key="bot_cmd_reconcile", value="false"))
    db.commit()
    router.dispatch(db, _msg_update(STAFF_TG, STAFF_TG, "/reconcile"))
    assert any("该功能已关闭" in t for t in sent)
    assert member["calls"] == []

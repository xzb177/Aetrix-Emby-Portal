"""A-110 回归：退群处置与对账的“在群”定义须一致。

退群事件只凭“离了当前群”就取消公益资格是错的：用户可能仍在
其他名单群。对账口径是“任一群在群即在群”，退群处置处置前必须
用同一口径逐群复核。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.integrations import store
from backend.tg_bot import router, sender

GROUP_A = -100111
GROUP_B = -100222
USER_TG = 700201


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
    yield session
    session.close()
    store.invalidate()


@pytest.fixture()
def _sent(monkeypatch):
    monkeypatch.setattr(
        sender, "send_message",
        lambda db_, chat_id, text, reply_markup=None, **kw: (True, None),
    )


def _user(db, tg_id, is_welfare=True):
    u = models.WebUser(username="u%d" % tg_id, telegram_id=tg_id)
    u.password_hash = ""
    u.is_welfare = is_welfare
    db.add(u)
    db.commit()
    return u


def _stub_members(monkeypatch, per_chat):
    def _check(db_, chat_id, user_id):
        return per_chat.get(int(chat_id), (False, "not_found"))

    monkeypatch.setattr(sender, "get_chat_member", _check)


def _leave_event(tg_id, chat_id):
    return {
        "chat": {"id": chat_id, "type": "supergroup"},
        "old_chat_member": {"user": {"id": tg_id, "is_bot": False}, "status": "member"},
        "new_chat_member": {"user": {"id": tg_id, "is_bot": False}, "status": "left"},
    }


def _cfg(groups):
    return {"bot_group_ids": groups, "bot_leave_action": "cancel_welfare"}


def _group_leave_logs(db):
    return (
        db.query(models.WelfareGrantLog)
        .filter(models.WelfareGrantLog.channel == "group_leave")
        .count()
    )


def _refresh(db, user):
    db.expire(user)
    return db.query(models.WebUser).filter(models.WebUser.id == user.id).first()


def test_leave_one_group_still_member_elsewhere_keeps_welfare(
    db, _sent, monkeypatch
):
    """(a) 群 A=left、群 B=member → 不取消、无日志。"""
    user = _user(db, USER_TG, is_welfare=True)
    _stub_members(monkeypatch, {GROUP_A: (False, "left"), GROUP_B: (True, "member")})
    router._handle_chat_member(db, _cfg(f"{GROUP_A},{GROUP_B}"), _leave_event(USER_TG, GROUP_A))
    assert _refresh(db, user).is_welfare is True
    assert _group_leave_logs(db) == 0


def test_leave_all_groups_cancels(db, _sent, monkeypatch):
    """(b) 两群皆 left → 取消且有日志。"""
    user = _user(db, USER_TG, is_welfare=True)
    _stub_members(monkeypatch, {GROUP_A: (False, "left"), GROUP_B: (False, "left")})
    router._handle_chat_member(db, _cfg(f"{GROUP_A},{GROUP_B}"), _leave_event(USER_TG, GROUP_A))
    assert _refresh(db, user).is_welfare is False
    assert _group_leave_logs(db) == 1


def test_leave_other_group_api_error_skips(db, _sent, monkeypatch):
    """(c) 群 B 接口失败（None）→ fail-closed，不取消。"""
    user = _user(db, USER_TG, is_welfare=True)
    _stub_members(monkeypatch, {GROUP_A: (False, "left"), GROUP_B: (None, "api_error")})
    router._handle_chat_member(db, _cfg(f"{GROUP_A},{GROUP_B}"), _leave_event(USER_TG, GROUP_A))
    assert _refresh(db, user).is_welfare is True
    assert _group_leave_logs(db) == 0


def test_leave_single_group_cancels(db, _sent, monkeypatch):
    """(d) 单群配置离群 → 照常取消。"""
    user = _user(db, USER_TG, is_welfare=True)
    _stub_members(monkeypatch, {GROUP_A: (False, "left")})
    router._handle_chat_member(db, _cfg(f"{GROUP_A}"), _leave_event(USER_TG, GROUP_A))
    assert _refresh(db, user).is_welfare is False
    assert _group_leave_logs(db) == 1

# -*- coding: utf-8 -*-
"""TG Bot /whois 群内管理员精简卡（用户 2026-11-01 拍板）。

口径：
- 仅管理员（is_staff，与私聊 /whois 同一判定）、仅群聊模式出精简卡；
  私聊 /whois 仍出审计全卡，行为逐字不变。
- 入口两种：回复某人的消息发 /whois（凭被回复者 TG 身份定位），
  或 /whois 站内用户名 / TG 号带参定位。
- 精简卡字段白名单：站内用户名、TG 显示名、等级、公益资格、订阅
  到期、能量余额、网盘绑定（只 provider＋状态）、账号状态。
  IP / 设备 / 登录记录 / 播放记录 / 绑定 label / 凭据 id / 密码
  一律不许出现在卡里（断言型拦截）。
- 非管理员触发只回现行 🔒 拒绝卡，绝不带被查人任何信息；
  查无此人 / 对方未绑定回明示文案，不报错、不建行（全只读）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.integrations import store
from backend.tg_bot import ephemeral, handlers, router, sender

STAFF_TG = 700100
TARGET_TG = 700300
PLAIN_TG = 700200
GROUP_ID = -100777


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    for table in (
        models.WebUser.__table__,
        models.SystemConfig.__table__,
        models.LoginLog.__table__,
        models.UserSubscription.__table__,
        em.UserStorageBinding.__table__,
        em.MediaItem.__table__,
        em.PlaybackSession.__table__,
    ):
        table.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    router._user_cmd_limits.clear()
    router._group_windows.clear()
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


def _user(db, username, tg_id=None, staff=False, **kw):
    u = models.WebUser(username=username, telegram_id=tg_id)
    u.password_hash = kw.pop("password_hash", "")
    u.is_staff = staff
    for k, v in kw.items():
        setattr(u, k, v)
    db.add(u)
    db.commit()
    return u


def _staff(db):
    return _user(db, "boss", tg_id=STAFF_TG, staff=True)


def _rich_target(db):
    """资料齐全的被查人：等级/公益/订阅/能量/绑定/敏感痕迹全备。"""
    u = _user(
        db, "lite_target", tg_id=TARGET_TG,
        points=321,
        is_welfare=True, welfare_expires_at=datetime(2027, 1, 15),
        password_hash="SECRET_HASH_SENTINEL",
        emby_password="SECRET_EMBY_PW_SENTINEL",
    )
    db.add(models.UserSubscription(
        user_id=u.id, plan_id=1, status="active",
        start_date=datetime(2026, 1, 1), end_date=datetime(2027, 3, 1)))
    db.add(em.UserStorageBinding(
        user_id=u.id, account_label="绝密主盘LABEL", provider="drive",
        credential_id=987654, status="active"))
    db.add(em.UserStorageBinding(
        user_id=u.id, account_label="绝密副盘LABEL", provider="pan115",
        credential_id=987655, status="expired"))
    db.add(models.LoginLog(
        user_id=u.id, username=u.username, ip="9.9.9.9",
        user_agent="SecretClient/9.9 (SecretPhone)", success=True,
        reason="portal_login", created_at=datetime(2026, 10, 30, 10, 0, 0)))
    item = em.MediaItem(
        id=201, guid="guid-201", library_id=1, item_type="movie",
        name="保密片目", file_path="115:/保密片目.mkv")
    db.add(item)
    db.add(em.PlaybackSession(
        session_key="sess-lite", user_id=u.id, item_id=201,
        remote_addr="9.9.9.9", client_name="SecretClient",
        device_name="SecretPhone X",
        start_time=datetime(2026, 10, 30, 11, 0, 0)))
    db.commit()
    return u


def _group_update(text, tg_id=STAFF_TG, reply_from=None):
    msg = {
        "message_id": 7,
        "from": {"id": tg_id, "first_name": "Boss"},
        "chat": {"id": GROUP_ID, "type": "supergroup"},
        "text": text,
    }
    if reply_from is not None:
        msg["reply_to_message"] = {
            "message_id": 3,
            "from": reply_from,
            "chat": {"id": GROUP_ID, "type": "supergroup"},
            "text": "大家好",
        }
    return {"update_id": 3, "message": msg}


def _private_update(text, tg_id=STAFF_TG):
    return {"update_id": 4, "message": {
        "message_id": 8,
        "from": {"id": tg_id, "first_name": "Boss"},
        "chat": {"id": tg_id, "type": "private"},
        "text": text,
    }}


# ---------- 群里出卡：回复定位 / 带参定位 ----------

def test_group_reply_lookup_renders_lite_card(db, sent):
    _staff(db)
    _rich_target(db)
    router.dispatch(db, _group_update(
        "/whois",
        reply_from={"id": TARGET_TG, "first_name": "小明", "last_name": "王"}))
    assert len(sent) == 1
    card = sent[0]
    assert "lite_target" in card          # 站内用户名
    assert "小明" in card                  # TG 显示名（来自被回复消息）
    assert "Lv.1" in card                  # 等级
    assert "2027-01-15" in card            # 公益资格到期
    assert "2027-03-01" in card            # 订阅到期
    assert "321" in card                   # 能量余额
    assert "谷歌网盘" in card              # 绑定 provider
    assert "115 网盘" in card
    assert "已过期" in card                # 绑定状态
    assert "正常" in card                  # 账号状态


def test_group_args_lookup_by_username_and_tg_id(db, sent):
    _staff(db)
    _rich_target(db)
    router.dispatch(db, _group_update("/whois lite_target"))
    assert len(sent) == 1
    assert "lite_target" in sent[0]
    router._user_cmd_limits.clear()
    router.dispatch(db, _group_update(f"/whois {TARGET_TG}"))
    assert len(sent) == 2
    assert "lite_target" in sent[1]


def test_handler_group_direct_call_renders_lite(db):
    _staff(db)
    _rich_target(db)
    out = handlers.handle_whois(
        db, {"id": STAFF_TG}, GROUP_ID, "",
        in_group=True,
        reply_from={"id": TARGET_TG, "first_name": "小明"})
    assert "lite_target" in out
    assert "小明" in out


# ---------- 非管理员：只有现行拒绝卡，零被查人信息 ----------

def test_group_non_staff_gets_no_target_info(db, sent):
    _staff(db)
    _user(db, "plainuser", tg_id=PLAIN_TG)
    _rich_target(db)
    router.dispatch(db, _group_update(
        "/whois lite_target", tg_id=PLAIN_TG))
    assert len(sent) == 1
    assert "仅管理员可用" in sent[0]
    assert "lite_target" not in sent[0]
    assert "321" not in sent[0]
    router._user_cmd_limits.clear()
    router.dispatch(db, _group_update(
        "/whois", tg_id=PLAIN_TG,
        reply_from={"id": TARGET_TG, "first_name": "小明"}))
    assert len(sent) == 2
    assert "仅管理员可用" in sent[1]
    assert "lite_target" not in sent[1]
    assert "小明" not in sent[1]


def test_group_unbound_caller_gets_bind_guide(db, sent):
    _staff(db)
    _rich_target(db)
    router.dispatch(db, _group_update("/whois lite_target", tg_id=799999))
    assert len(sent) == 1
    assert "如何绑定" in sent[0]
    assert "lite_target" not in sent[0]


# ---------- 敏感字段断言：精简卡绝不带审计痕迹 ----------

def test_lite_card_has_no_sensitive_fields(db, sent):
    _staff(db)
    _rich_target(db)
    router.dispatch(db, _group_update("/whois lite_target"))
    assert len(sent) == 1
    card = sent[0]
    assert "lite_target" in card
    for banned in (
        "9.9.9.9",                 # 登录/播放 IP
        "SecretClient",            # UA / 客户端名
        "SecretPhone",             # 设备名
        "绝密主盘LABEL", "绝密副盘LABEL",  # 绑定 label
        "987654", "987655",        # 凭据 id
        "SECRET_HASH_SENTINEL", "SECRET_EMBY_PW_SENTINEL",
        "最近登录", "最近播放", "保密片目",
    ):
        assert banned not in card, banned


# ---------- 查不到 / 未绑定 / 用法：明示文案不报错 ----------

def test_group_reply_unbound_target_message(db, sent):
    _staff(db)
    router.dispatch(db, _group_update(
        "/whois",
        reply_from={"id": 755555, "first_name": "路人"}))
    assert len(sent) == 1
    assert "还没有绑定站内账号" in sent[0]


def test_group_args_not_found_message(db, sent):
    _staff(db)
    _rich_target(db)
    router.dispatch(db, _group_update("/whois zzz_no_one"))
    assert len(sent) == 1
    assert "没查到" in sent[0]


def test_group_no_args_no_reply_shows_usage(db, sent):
    _staff(db)
    router.dispatch(db, _group_update("/whois"))
    assert len(sent) == 1
    assert "回复" in sent[0]
    assert "/whois" in sent[0]


# ---------- 只读 & 私聊全卡不变 ----------

def test_group_lookup_is_readonly(db, sent):
    _staff(db)
    _rich_target(db)

    def counts():
        return (
            db.query(models.WebUser).count(),
            db.query(models.LoginLog).count(),
            db.query(models.UserSubscription).count(),
            db.query(em.UserStorageBinding).count(),
            db.query(em.PlaybackSession).count(),
        )

    before = counts()
    router.dispatch(db, _group_update("/whois lite_target"))
    router.dispatch(db, _group_update(
        "/whois", reply_from={"id": TARGET_TG, "first_name": "小明"}))
    assert counts() == before


def test_private_whois_full_card_unchanged(db, sent):
    _staff(db)
    _rich_target(db)
    router.dispatch(db, _private_update("/whois lite_target"))
    assert len(sent) == 1
    card = sent[0]
    assert "lite_target" in card
    assert "9.9.9.9" in card        # 私聊全卡仍带登录 IP（审计口径不变）
    assert "最近登录" in card
    assert "最近播放" in card

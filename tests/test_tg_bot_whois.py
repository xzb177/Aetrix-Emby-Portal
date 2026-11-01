# -*- coding: utf-8 -*-
"""TG Bot 管理员审计反查 /whois（embyboss 借鉴第 2 项）。

口径：
- 审计全卡仅私聊、仅网站管理员（is_staff 现行判定，与 handle_admin
  同一口径）；非管理员回现行拒绝卡（🔒 仅管理员可用），未绑定回
  绑定指引，handler 层对未声明群上下文的直调仍有私聊 guard。
- 群聊模式（2026-11-01 用户拍板）：管理员在群里发 /whois 出精简卡
  （字段白名单、无审计痕迹），见 test_tg_bot_whois_group_lite.py。
- 全只读：只查 web_users / login_logs / emby_playback_sessions /
  user_storage_bindings / emby_items，不建行不改行不提交。
- 结果绝不出现 password_hash / emby_password / 凭据等秘密字段；
  IP 对管理员完整显示（管理员自查工具）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

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
from backend.tg_bot import ephemeral, handlers, policy, poller, router, sender

STAFF_TG = 700100
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


def _login(db, user, ip="1.2.3.4", ua="SenPlayer/1.0 (iPhone)", when=None,
           success=True, reason="portal_login"):
    row = models.LoginLog(
        user_id=user.id, username=user.username, ip=ip, user_agent=ua,
        success=success, reason=reason,
        created_at=when or datetime(2026, 10, 30, 10, 0, 0))
    db.add(row)
    db.commit()
    return row


def _item(db, item_id, name):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type="movie", name=name, file_path=f"115:/{name}.mkv")
    db.add(row)
    db.commit()
    return row


def _play(db, user, item_id, key, when=None, ip="1.2.3.4",
          client="SenPlayer", device="iPhone 15"):
    row = em.PlaybackSession(
        session_key=key, user_id=user.id, item_id=item_id,
        remote_addr=ip, client_name=client, device_name=device,
        start_time=when or datetime(2026, 10, 30, 11, 0, 0))
    db.add(row)
    db.commit()
    return row


def _staff(db):
    return _user(db, "boss", tg_id=STAFF_TG, staff=True)


def _private_update(text, tg_id=STAFF_TG):
    return {"update_id": 1, "message": {
        "message_id": 5,
        "from": {"id": tg_id, "first_name": "Boss"},
        "chat": {"id": tg_id, "type": "private"},
        "text": text,
    }}


def _group_update(text, tg_id=STAFF_TG):
    return {"update_id": 2, "message": {
        "message_id": 6,
        "from": {"id": tg_id, "first_name": "Boss"},
        "chat": {"id": GROUP_ID, "type": "supergroup"},
        "text": text,
    }}


# ---------- 门禁：非管理员 / 未绑定 / 群聊 ----------

def test_non_staff_rejected_with_admin_card(db):
    _staff(db)
    plain = _user(db, "plainuser", tg_id=700200)
    _login(db, plain)
    out = handlers.handle_whois(db, {"id": 700200}, 700200, "1.2.3.4")
    assert "仅管理员可用" in out
    assert "plainuser" not in out


def test_unbound_gets_bind_guide(db):
    _staff(db)
    out = handlers.handle_whois(db, {"id": 799999}, 799999, "whatever")
    assert "如何绑定" in out


def test_handler_group_chat_double_guard(db):
    _staff(db)
    target = _user(db, "targetx", tg_id=700300)
    _login(db, target)
    out = handlers.handle_whois(db, {"id": STAFF_TG}, GROUP_ID, "targetx")
    assert "群里不查" in out
    assert "targetx" not in out


def test_router_group_whois_staff_gets_lite_card(db, sent):
    # 2026-11-01 起 /whois 不再被 router private_only 拦：管理员群里
    # 带参查人出精简卡（用户名在、登录 IP 等审计痕迹不在）。
    _staff(db)
    target = _user(db, "grouptarget", tg_id=700301)
    _login(db, target)
    router.dispatch(db, _group_update("/whois grouptarget"))
    assert len(sent) == 1
    assert "grouptarget" in sent[0]
    assert "1.2.3.4" not in sent[0]
    assert "最近登录" not in sent[0]


# ---------- 命中形态 ----------

def test_ip_hit_lists_all_accounts(db):
    _staff(db)
    alice = _user(db, "alice_w", tg_id=700401)
    bob = _user(db, "bob_w", tg_id=700402)
    carol = _user(db, "carol_w", tg_id=700403)
    item = _item(db, 101, "测试片甲")
    _login(db, alice, ip="9.9.9.9")
    _login(db, bob, ip="9.9.9.9")
    _login(db, bob, ip="8.8.8.8")
    _play(db, carol, item.id, "sess-carol", ip="9.9.9.9")
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "9.9.9.9")
    assert "alice_w" in out
    assert "bob_w" in out
    assert "carol_w" in out
    assert "9.9.9.9" in out


def test_username_fuzzy_and_emby_name_hit(db):
    _staff(db)
    u = _user(db, "moviefan88", tg_id=700501, emby_username="emby_shadow")
    _login(db, u)
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "moviefan")
    assert "moviefan88" in out
    out2 = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "emby_shadow")
    assert "moviefan88" in out2


def test_tg_id_hit(db):
    _staff(db)
    u = _user(db, "tghit", tg_id=700601)
    _login(db, u)
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "700601")
    assert "tghit" in out


def test_client_name_hit(db):
    _staff(db)
    u = _user(db, "clientfan", tg_id=700701)
    item = _item(db, 102, "测试片乙")
    _play(db, u, item.id, "sess-client", client="WeirdClientX")
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "WeirdClientX")
    assert "clientfan" in out


def test_no_hit_says_not_found(db):
    _staff(db)
    _user(db, "someone", tg_id=700801)
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "zzz_no_such_thing")
    assert "没查到" in out


def test_empty_args_shows_usage(db):
    _staff(db)
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "  ")
    assert "/whois" in out


# ---------- 卡片内容：最近 5 条登录/播放、绑定状态、脱敏 ----------

def test_recent_five_logins_and_plays(db):
    _staff(db)
    u = _user(db, "historyuser", tg_id=700901)
    base = datetime(2026, 10, 30, 8, 0, 0)
    for i in range(6):
        _login(db, u, ip=f"10.0.0.{i}", when=base + timedelta(hours=i))
    item = _item(db, 103, "历史片目")
    for i in range(6):
        _play(db, u, item.id, f"sess-hist-{i}",
              when=base + timedelta(hours=i))
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "historyuser")
    # 最近 5 条：最早的 10.0.0.0 应被截掉，其余在
    assert "10.0.0.0" not in out
    assert "10.0.0.5" in out
    assert "历史片目" in out


def test_bindings_status_shown(db):
    _staff(db)
    u = _user(db, "binder", tg_id=701001)
    row = em.UserStorageBinding(
        user_id=u.id, account_label="我的主盘", provider="drive",
        credential_id=424242, status="active")
    db.add(row)
    db.commit()
    _login(db, u)
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "binder")
    assert "我的主盘" in out
    assert "424242" not in out


def test_no_secrets_in_output(db):
    _staff(db)
    u = _user(db, "secretuser", tg_id=701101,
              password_hash="SECRET_HASH_SENTINEL",
              emby_password="SECRET_EMBY_PW_SENTINEL")
    _login(db, u)
    item = _item(db, 104, "保密片")
    _play(db, u, item.id, "sess-secret")
    out = handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "secretuser")
    assert "secretuser" in out
    assert "SECRET_HASH_SENTINEL" not in out
    assert "SECRET_EMBY_PW_SENTINEL" not in out


def test_lookup_is_readonly(db):
    _staff(db)
    u = _user(db, "readonlyuser", tg_id=701201)
    _login(db, u)
    before = (
        db.query(models.WebUser).count(),
        db.query(models.LoginLog).count(),
        db.query(models.SystemConfig).count(),
        db.query(em.PlaybackSession).count(),
        db.query(em.UserStorageBinding).count(),
    )
    handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "readonlyuser")
    handlers.handle_whois(db, {"id": STAFF_TG}, STAFF_TG, "1.2.3.4")
    after = (
        db.query(models.WebUser).count(),
        db.query(models.LoginLog).count(),
        db.query(models.SystemConfig).count(),
        db.query(em.PlaybackSession).count(),
        db.query(em.UserStorageBinding).count(),
    )
    assert before == after


# ---------- 注册三处 ----------

def test_command_registered(db, sent):
    _staff(db)
    assert policy.COMMAND_SWITCHES.get("/whois") == "bot_cmd_whois"
    assert ("whois", "[群/私聊] 账号反查（管理员）") in poller.BOT_COMMANDS
    router.dispatch(db, _private_update("/whois nothinghere"))
    assert len(sent) == 1
    assert "未知命令" not in sent[0]
    assert "没查到" in sent[0]


def test_command_switch_off(db, sent):
    _staff(db)
    db.add(models.SystemConfig(key="bot_cmd_whois", value="false"))
    db.commit()
    router.dispatch(db, _private_update("/whois nothinghere"))
    assert len(sent) == 1
    assert "该功能已关闭" in sent[0]

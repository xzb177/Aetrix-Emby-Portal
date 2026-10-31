"""抽奖公告根治回归测试（2026-10-31）。

背景（用户亲报，round 4 实证）：
1. 公告发送结果不落库：没有 message_id，公告卡事后无法置顶、无法编辑、
   人数无法更新；发送失败只剩一条日志，管理员无从知晓、无法补发。
2. 口令参加在群里零反馈：参加成功/重复参加都没有带人数的确认消息。
3. 创建活动时不校验机器人能否在目标群发言，能建出一场永远发不出
   公告的活动且全程静默。

本文件断言根治后的行为；修复前这些用例应当全红（见 PR 说明）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import sys
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import lottery, models


@pytest.fixture()
def db():
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for tbl in (
        models.WebUser.__table__,
        models.SystemConfig.__table__,
        models.LotteryRound.__table__,
        models.LotteryRoundPrize.__table__,
        models.LotteryRoundEntry.__table__,
        models.LotteryRoundWinner.__table__,
        models.LotteryBlacklist.__table__,
        models.AdminLog.__table__,
    ):
        tbl.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = value
    else:
        db.add(models.SystemConfig(key=key, value=value))
    db.commit()


def _prizes():
    return [{"name": "能量奖励", "type": "points", "value": 500, "quantity": 88}]


def _make_user(db, username, tg_id):
    user = models.WebUser(username=username, password_hash="", telegram_id=tg_id)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_entry(db, round_id, user):
    db.add(models.LotteryRoundEntry(
        round_id=round_id, user_id=user.id, telegram_id=user.telegram_id))
    db.commit()


def _capture_sender():
    """同时打桩新旧发送口，返回 (sent_list, patches 上下文列表)。

    sent_list 元素为 dict(chat_id, text, reply_markup, message_id)。
    """
    sent = []

    def fake_ex(db_, chat_id, text, reply_markup=None, parse_mode="HTML"):
        sent.append({"chat_id": chat_id, "text": text,
                     "reply_markup": reply_markup, "message_id": 4242})
        return True, None, 4242

    def fake_plain(db_, chat_id, text, reply_markup=None, parse_mode="HTML"):
        sent.append({"chat_id": chat_id, "text": text,
                     "reply_markup": reply_markup, "message_id": None})
        return True, None

    patches = [
        patch("backend.tg_bot.sender.send_message_ex", side_effect=fake_ex),
        patch("backend.tg_bot.sender.send_message", side_effect=fake_plain),
        patch("backend.tg_bot.sender.pin_message",
              return_value=(True, None), create=True),
        patch("backend.tg_bot.sender.unpin_message",
              return_value=(True, None), create=True),
    ]
    return sent, patches


class _Stack:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        self.mocks = [p.__enter__() for p in self.patches]
        return self.mocks

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.__exit__(*exc)
        return False


# ---------- 1. 公告落库 + 置顶 ----------

def test_notify_new_round_records_message_id_and_pins(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(db, title="按钮场", chat_id=-1001, prizes=_prizes())
    sent, patches = _capture_sender()
    with _Stack(patches) as mocks:
        result = lottery.notify_new_round(db, r.id)
    db.refresh(r)
    assert result["group"] is True
    assert getattr(r, "announce_message_id", None) == 4242
    assert getattr(r, "announce_status", None) == "sent"
    pin_mock = mocks[2]
    pin_mock.assert_called_once()
    assert pin_mock.call_args[0][2] == 4242  # (db, chat_id, message_id)


def test_notify_new_round_failure_is_recorded(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(db, title="失败场", chat_id=-1001, prizes=_prizes())
    with patch("backend.tg_bot.sender.send_message_ex",
               return_value=(False, "chat not found", None)), \
         patch("backend.tg_bot.sender.send_message",
               return_value=(False, "chat not found")):
        result = lottery.notify_new_round(db, r.id)
    db.refresh(r)
    assert result["group"] is False
    assert getattr(r, "announce_status", None) == "failed"
    assert "chat not found" in (getattr(r, "announce_error", None) or "")


# ---------- 2. 公告卡形态：口令型 / 按钮型 ----------

def test_password_round_card_has_keyword_and_copy_button(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(
        db, title="口令场", chat_id=-1001, prizes=_prizes(),
        lottery_type="password", password_keyword="夏夜姐姐最美丽",
    )
    sent, patches = _capture_sender()
    with _Stack(patches):
        lottery.notify_new_round(db, r.id)
    assert sent, "公告未发出"
    card = sent[0]
    assert "夏夜姐姐最美丽" in card["text"]
    assert "0 人" in card["text"]  # 正文带当前参与人数
    markup = str(card["reply_markup"])
    assert "copy_text" in markup and "夏夜姐姐最美丽" in markup
    assert "lottery_join" not in markup  # 口令型不给绕过口令的参加按钮


def test_button_round_card_shows_count_and_join_button(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(db, title="按钮场", chat_id=-1001, prizes=_prizes())
    _add_entry(db, r.id, _make_user(db, "u1", 111))
    _add_entry(db, r.id, _make_user(db, "u2", 222))
    sent, patches = _capture_sender()
    with _Stack(patches):
        lottery.notify_new_round(db, r.id)
    card = sent[0]
    assert "2 人" in card["text"]
    markup = str(card["reply_markup"])
    assert "lottery_join:" in markup
    assert "2 人已参与" in markup  # 按钮带人数


# ---------- 3. 公告卡人数编辑（节流） ----------

def test_refresh_announce_card_edits_count_and_throttles(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(db, title="编辑场", chat_id=-1001, prizes=_prizes())
    sent, patches = _capture_sender()
    with _Stack(patches):
        lottery.notify_new_round(db, r.id)
    user = _make_user(db, "u1", 111)
    lottery.join_round(db, r, user, 111)
    # 公告发送时盖了节流时间戳；回拨到一小时前模拟窗口已过
    r.announce_last_edit_at = lottery.datetime.now() - timedelta(hours=1)
    db.commit()

    edits = []

    def fake_edit(db_, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        edits.append({"text": text, "reply_markup": reply_markup})
        return True, None

    with patch("backend.tg_bot.sender.edit_message_text", side_effect=fake_edit):
        first = lottery.refresh_announce_card(db, r.id)
        second = lottery.refresh_announce_card(db, r.id)
        forced = lottery.refresh_announce_card(db, r.id, force=True)
    assert first["edited"] is True
    assert "1 人" in edits[0]["text"]
    assert second["edited"] is False  # 节流：刚编辑过不重复编辑
    assert forced["edited"] is True
    assert len(edits) == 2


# ---------- 4. 人数提醒消息（里程碑 + 开奖前最后一叫） ----------

def test_run_announce_reminders_milestone_once_per_step(db):
    _set_config(db, "lottery_enabled", "1")
    _set_config(db, "lottery_reminder_step", "5")
    r = lottery.create_round(db, title="里程碑场", chat_id=-1001, prizes=_prizes())
    for i in range(5):
        _add_entry(db, r.id, _make_user(db, f"u{i}", 1000 + i))
    sent, patches = _capture_sender()
    with _Stack(patches):
        lottery.run_announce_reminders(db)
        lottery.run_announce_reminders(db)  # 同台阶不重发
    assert len(sent) == 1
    assert "5 人" in sent[0]["text"]
    for i in range(5, 10):
        _add_entry(db, r.id, _make_user(db, f"u{i}", 1000 + i))
    with _Stack(_capture_sender()[1]):
        pass
    sent2, patches2 = _capture_sender()
    with _Stack(patches2):
        lottery.run_announce_reminders(db)
    assert len(sent2) == 1
    assert "10 人" in sent2[0]["text"]


def test_run_announce_reminders_final_call_once(db):
    _set_config(db, "lottery_enabled", "1")
    draw_at = lottery.shanghai_now_naive() + timedelta(minutes=10)
    r = lottery.create_round(
        db, title="临开奖场", chat_id=-1001, prizes=_prizes(), draw_at=draw_at)
    sent, patches = _capture_sender()
    with _Stack(patches):
        lottery.run_announce_reminders(db)
        lottery.run_announce_reminders(db)
    assert len(sent) == 1
    assert "提醒" in sent[0]["text"]


# ---------- 5. 创建前机器人发言能力校验 ----------

def _patch_bot_member(status_member):
    return [
        patch("backend.tg_bot.sender.get_me",
              return_value=(True, None, {"id": 999}), create=True),
        patch("backend.tg_bot.sender.get_chat_member_raw",
              return_value=(True, None, status_member), create=True),
    ]


def test_check_bot_can_post_variants(db):
    with _Stack(_patch_bot_member({"status": "left"})):
        res = lottery.check_bot_can_post(db, -1001)
    assert res["ok"] is False and res["reason"] == "not_member"

    with _Stack(_patch_bot_member(
            {"status": "administrator", "can_post_messages": False})):
        res = lottery.check_bot_can_post(db, -1001)
    assert res["ok"] is False and res["reason"] == "no_permission"

    with _Stack(_patch_bot_member({"status": "member"})):
        res = lottery.check_bot_can_post(db, -1001)
    assert res["ok"] is True

    with patch("backend.tg_bot.sender.get_me",
               return_value=(False, "network", None), create=True):
        res = lottery.check_bot_can_post(db, -1001)
    assert res["ok"] is False and res["reason"] == "unknown"


# ---------- 6. 开奖结果公告顺手取消公告置顶 ----------

def test_notify_draw_results_unpins_announcement(db):
    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(db, title="开奖场", chat_id=-1001, prizes=_prizes())
    r.announce_message_id = 777
    r.announce_status = "sent"
    r.announce_pinned = True
    r.status = "done"
    db.commit()
    sent, patches = _capture_sender()
    with _Stack(patches) as mocks:
        lottery.notify_draw_results(db, r.id, [])
    unpin_mock = mocks[3]
    unpin_mock.assert_called_once()
    assert unpin_mock.call_args[0][2] == 777
    db.refresh(r)
    assert r.announce_pinned is False


# ---------- 7. 管理端：创建拦截 + 公告补发 ----------

@pytest.fixture()
def client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool  # noqa: F401  （与既有端点测试同口径）
    from backend.api import welfare_admin
    from backend.integrations import store

    staff = models.WebUser(username="boss", password_hash="x", is_staff=True)
    db.add(staff)
    db.commit()
    app = FastAPI()
    app.include_router(welfare_admin.admin_router)
    app.dependency_overrides[welfare_admin.get_current_admin] = lambda: staff

    def _get_db():
        yield db

    app.dependency_overrides[welfare_admin.get_db] = _get_db
    store.invalidate()
    with TestClient(app) as c:
        yield c
    store.invalidate()


def _create_body():
    return {
        "title": "接口场",
        "chat_id": -1001,
        "prizes": [{"name": "能量奖励", "type": "points", "value": 500, "quantity": 88}],
        "lottery_type": "button",
    }


def test_admin_create_blocked_when_bot_not_in_group(db, client):
    _set_config(db, "lottery_group_ids", "-1001")
    with patch.object(lottery, "check_bot_can_post",
                      return_value={"ok": False, "reason": "not_member",
                                    "detail": "机器人不在该群"}, create=True):
        resp = client.post("/api/admin/welfare/lottery/rounds", json=_create_body())
    assert resp.status_code == 400
    assert "机器人" in resp.json()["detail"]
    # 没有建出活动
    assert db.query(models.LotteryRound).count() == 0


def test_admin_create_proceeds_when_bot_check_unknown(db, client):
    _set_config(db, "lottery_group_ids", "-1001")
    with patch.object(lottery, "check_bot_can_post",
                      return_value={"ok": False, "reason": "unknown",
                                    "detail": "网络失败"}, create=True), \
         patch("backend.tg_bot.sender.send_message_ex",
               return_value=(False, "token 未配置", None)), \
         patch("backend.tg_bot.sender.send_message",
               return_value=(False, "token 未配置")):
        resp = client.post("/api/admin/welfare/lottery/rounds", json=_create_body())
    assert resp.status_code == 200
    body = resp.json()
    # 公告失败必须明示在创建响应里，不许静默
    assert body["notify"]["status"] == "failed"


def test_admin_resend_announce_endpoint(db, client):
    _set_config(db, "lottery_group_ids", "-1001")
    r = lottery.create_round(db, title="补发场", chat_id=-1001, prizes=_prizes())
    sent, patches = _capture_sender()
    with _Stack(patches):
        resp = client.post(f"/api/admin/welfare/lottery/rounds/{r.id}/announce/resend")
    assert resp.status_code == 200
    db.refresh(r)
    assert r.announce_message_id == 4242
    assert r.announce_status == "sent"
    resp404 = client.post("/api/admin/welfare/lottery/rounds/99999/announce/resend")
    assert resp404.status_code == 404


# ---------- 8. 口令参加反馈（成功带人数 / 重复也反馈，均自毁） ----------

def _password_round(db):
    _set_config(db, "lottery_enabled", "1")
    return lottery.create_round(
        db, title="口令场", chat_id=100, prizes=_prizes(),
        lottery_type="password", password_keyword="抽我",
    )


def test_password_join_feedback_success_with_count_and_autodelete(db):
    from backend.tg_bot import callbacks

    r = _password_round(db)
    user = _make_user(db, "u1", 111)
    sent = []

    def fake_ex(db_, chat_id, text, reply_markup=None, parse_mode="HTML"):
        sent.append(text)
        return True, None, 555

    with patch.object(callbacks, "resolve", return_value=user), \
         patch("backend.tg_bot.sender.get_chat_member", return_value=(True, "member")), \
         patch("backend.tg_bot.sender.send_message_ex", side_effect=fake_ex), \
         patch("backend.tg_bot.ephemeral.schedule_delete", return_value=True) as mock_del:
        consumed = callbacks.handle_lottery_password(
            db, 100, {"id": 111, "first_name": "张三"}, "抽我")
    assert consumed is True
    assert len(sent) == 1
    assert "参加成功" in sent[0]
    assert "1 人" in sent[0]  # 带当前总人数
    mock_del.assert_called_once()
    assert mock_del.call_args[0][1] == 555  # 自毁挂在反馈消息上


def test_password_join_feedback_on_duplicate(db):
    from backend.tg_bot import callbacks

    r = _password_round(db)
    user = _make_user(db, "u1", 111)
    lottery.join_round(db, r, user, 111)  # 先参加一次
    sent = []

    def fake_ex(db_, chat_id, text, reply_markup=None, parse_mode="HTML"):
        sent.append(text)
        return True, None, 556

    with patch.object(callbacks, "resolve", return_value=user), \
         patch("backend.tg_bot.sender.get_chat_member", return_value=(True, "member")), \
         patch("backend.tg_bot.sender.send_message_ex", side_effect=fake_ex), \
         patch("backend.tg_bot.ephemeral.schedule_delete", return_value=True):
        consumed = callbacks.handle_lottery_password(
            db, 100, {"id": 111, "first_name": "张三"}, "抽我")
    assert consumed is True
    assert len(sent) == 1
    assert "已参加" in sent[0]  # 重复发口令不许静默吞掉
    assert "1 人" in sent[0]


def test_button_join_triggers_announce_refresh(db):
    from backend.tg_bot import callbacks

    _set_config(db, "lottery_enabled", "1")
    r = lottery.create_round(db, title="按钮场", chat_id=100, prizes=_prizes())
    user = _make_user(db, "u1", 111)
    cb = {
        "id": "cb1",
        "from": {"id": 111},
        "message": {"chat": {"id": 100}},
        "data": f"lottery_join:{r.id}",
    }
    with patch.object(callbacks, "resolve", return_value=user), \
         patch.object(callbacks, "_answer_callback", lambda *a, **k: None), \
         patch("backend.tg_bot.sender.get_chat_member", return_value=(True, "member")), \
         patch.object(lottery, "refresh_announce_card",
                      return_value={"edited": True}, create=True) as mock_refresh:
        callbacks.handle_callback(db, cb)
    mock_refresh.assert_called_once()


# ---------- 9. 新配置键注册（#1454 口径） ----------

def test_new_config_keys_registered():
    from backend import config_schema
    from backend.api import welfare_admin

    schema = config_schema.WELFARE_CONFIG_SCHEMA
    for key in ("lottery_announce_pin_enabled", "lottery_reminder_step",
                "lottery_final_call_minutes", "lottery_announce_edit_min_interval_sec"):
        assert key in schema, f"{key} 未注册进 config_schema"
        assert key in welfare_admin.WELFARE_CONFIG_KEYS, f"{key} 未登记默认值"
    assert welfare_admin.WELFARE_CONFIG_KEYS["lottery_reminder_step"] == "25"
    assert welfare_admin.WELFARE_CONFIG_KEYS["lottery_final_call_minutes"] == "30"

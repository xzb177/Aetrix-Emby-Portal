"""TG Bot P0+P1：心跳报错可见、失败提示、自助查询（只读）。

只测本模块：DB 用内存 SQLite；Telegram 发送全部 patch 掉，不发真实请求；
_item_provider 在 drive 分支用 monkeypatch 替身（115:/ 与 byos:// 走真实判定）。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.api import tg_bot_admin
from backend.emby_server import models as em
from backend.tg_bot import callbacks, handlers, policy, poller, queries, router, sender


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        models.WebUser.__table__,
        models.SystemConfig.__table__,
        em.UserStorageBinding.__table__,
        em.TransferJob.__table__,
        em.TransferVariant.__table__,
        em.MediaItem.__table__,
    ):
        table.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _user(db, username="p1user", tg_id=900001):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        u.password_hash = ""
    db.add(u)
    db.commit()
    return u


def _binding(db, user_id, label="我的谷歌", provider="drive", status="active",
             kind=None):
    row = em.UserStorageBinding(
        user_id=user_id, account_label=label, provider=provider,
        credential_id=999999, status=status, last_error_kind=kind)
    db.add(row)
    db.commit()
    return row


def _item(db, item_id, name, path="115:/movie.mkv"):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type="movie", name=name, file_path=path)
    db.add(row)
    db.commit()
    return row


def _job(db, item_id, user_id, status="pending", trigger="playback", kind=None):
    row = em.TransferJob(
        item_id=item_id, trigger=trigger, provider="pan115",
        owner_scope="user", owner_ref=str(user_id), status=status,
        error_kind=kind)
    db.add(row)
    db.commit()
    return row


def _variant(db, item_id, user_id, status="ready", revoked=False):
    row = em.TransferVariant(
        item_id=item_id, source_file_id="src-1", variant_file_id="var-1",
        variant_drive_id=f"userdrive:{user_id}", owner_scope="user",
        owner_ref=str(user_id), status=status,
        share_revoked_at=datetime.now() if revoked else None)
    db.add(row)
    db.commit()
    return row


def _tg(tg_id=900001):
    return {"id": tg_id, "first_name": "P1"}


# ---------- policy：新命令默认开、可关 ----------

def test_policy_new_commands_default_on_and_switchable(db):
    cfg = policy.read_bot_config(db)
    for cmd in ("/bindings", "/transfers", "/whyplay", "/wants"):
        assert policy.is_command_enabled(cfg, cmd) is True
    db.add(models.SystemConfig(key="bot_cmd_whyplay", value="false"))
    db.commit()
    cfg = policy.read_bot_config(db)
    assert policy.is_command_enabled(cfg, "/whyplay") is False
    assert policy.is_command_enabled(cfg, "/bindings") is True


def test_help_lists_new_commands(db):
    # 命令不再以消息罗列，改走左下角原生菜单；注册表覆盖见
    # test_tg_bot_account_display.test_bot_commands_cover_router_commands
    text = handlers.handle_help(db, _tg(), 900001, "")
    assert "可用命令" not in text
    assert "左下角" in text


# ---------- 查绑定 ----------

def test_bindings_lists_own_only_and_no_credential_leak(db):
    u = _user(db)
    other = _user(db, "other", tg_id=900002)
    _binding(db, u.id, label="我的谷歌", provider="drive", status="active")
    _binding(db, u.id, label="我的115", provider="pan115", status="invalid",
             kind="auth_error")
    _binding(db, other.id, label="别人的盘", provider="drive", status="active")
    text = handlers.handle_bindings(db, _tg(), 900001, "")
    assert "我的谷歌" in text and "谷歌网盘" in text and "正常" in text
    assert "我的115" in text and "已失效" in text
    assert "账号凭据不对" in text
    assert "别人的盘" not in text
    assert "999999" not in text  # credential_id 绝不进回复


def test_bindings_unbound_tg_returns_guide(db):
    assert "绑定" in handlers.handle_bindings(db, _tg(777001), 777001, "")


def test_bindings_bound_but_no_drive_bindings(db):
    _user(db)
    text = handlers.handle_bindings(db, _tg(), 900001, "")
    assert "还没有绑定网盘" in text


# ---------- 查转存 / 查想看 ----------

def test_transfers_lists_own_only_with_status_text(db):
    u = _user(db)
    other = _user(db, "other", tg_id=900002)
    _item(db, 11, "片甲")
    _item(db, 12, "片乙")
    _job(db, 11, u.id, status="running")
    _job(db, 12, u.id, status="failed", kind="no_binding")
    _job(db, 12, other.id, status="done")
    text = handlers.handle_transfers(db, _tg(), 900001, "")
    assert "片甲" in text and "转存中" in text
    assert "片乙" in text and "绑定已失效" in text
    assert text.count("片乙") == 1  # 别人的 done 行不混进来


def test_transfers_empty(db):
    _user(db)
    assert "还没有转存任务" in handlers.handle_transfers(db, _tg(), 900001, "")


def test_wants_only_store_want_trigger(db):
    u = _user(db)
    _item(db, 21, "想看片")
    _item(db, 22, "普通转存片")
    _job(db, 21, u.id, status="pending", trigger="store_want")
    _job(db, 22, u.id, status="pending", trigger="playback")
    text = handlers.handle_wants(db, _tg(), 900001, "")
    assert "想看片" in text
    assert "普通转存片" not in text


def test_queries_do_not_write(db):
    u = _user(db)
    _item(db, 31, "只读片")
    _job(db, 31, u.id, status="pending")
    before = (db.query(em.TransferJob).count(),
              db.query(em.UserStorageBinding).count())
    handlers.handle_transfers(db, _tg(), 900001, "")
    handlers.handle_wants(db, _tg(), 900001, "")
    handlers.handle_bindings(db, _tg(), 900001, "")
    after = (db.query(em.TransferJob).count(),
             db.query(em.UserStorageBinding).count())
    assert before == after


# ---------- 为什么播不了 ----------

def test_whyplay_no_args_usage(db):
    _user(db)
    text = handlers.handle_whyplay(db, _tg(), 900001, "")
    assert "/whyplay" in text and "片名" in text


def test_whyplay_not_found(db):
    _user(db)
    assert "没找到" in handlers.handle_whyplay(db, _tg(), 900001, "不存在的片")


def test_whyplay_multiple_lists_names(db):
    _user(db)
    _item(db, 41, "嫁金钗 第 1 集")
    _item(db, 42, "嫁金钗 第 2 集")
    text = handlers.handle_whyplay(db, _tg(), 900001, "嫁金钗")
    assert "更完整" in text
    assert "第 1 集" in text and "第 2 集" in text


def test_whyplay_no_binding(db):
    u = _user(db)
    _item(db, 51, "单片", path="115:/a.mkv")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "还没有绑定网盘" in text


def test_whyplay_cross_provider(db, monkeypatch):
    u = _user(db)
    _binding(db, u.id, label="我的115", provider="pan115", status="active")
    _item(db, 52, "单片", path="mount://3/a.mkv")
    monkeypatch.setattr(queries, "_item_provider", lambda db_, item: "drive")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "绑哪播哪" in text


def test_whyplay_binding_invalid(db, monkeypatch):
    u = _user(db)
    _binding(db, u.id, label="我的115", provider="pan115", status="invalid",
             kind="auth_error")
    _item(db, 53, "单片", path="115:/a.mkv")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "已失效" in text


def test_whyplay_job_in_flight(db):
    u = _user(db)
    _binding(db, u.id, label="我的115", provider="pan115", status="active")
    _item(db, 54, "单片", path="115:/a.mkv")
    _job(db, 54, u.id, status="running")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "正在转存" in text


def test_whyplay_failed_job(db):
    u = _user(db)
    _binding(db, u.id, label="我的115", provider="pan115", status="active")
    _item(db, 55, "单片", path="115:/a.mkv")
    _job(db, 55, u.id, status="failed", kind="dead")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "转存失败" in text and "原件已失效" in text


def test_whyplay_ready_variant(db):
    u = _user(db)
    _binding(db, u.id, label="我的115", provider="pan115", status="active")
    _item(db, 56, "单片", path="115:/a.mkv")
    _variant(db, 56, u.id, status="ready")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "副本已就绪" in text
    assert "继续观看" in text
    assert "http" not in text  # 绝不发播放链接


def test_whyplay_revoked_variant(db):
    u = _user(db)
    _binding(db, u.id, label="我的115", provider="pan115", status="active")
    _item(db, 57, "单片", path="115:/a.mkv")
    _variant(db, 57, u.id, status="ready", revoked=True)
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "权限曾被收回" in text


def test_whyplay_byos(db):
    _user(db)
    _item(db, 58, "单片", path="byos://x/y.mkv")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "在你自己的盘里" in text


def test_whyplay_no_job_yet(db):
    u = _user(db)
    _binding(db, u.id, label="我的115", provider="pan115", status="active")
    _item(db, 59, "单片", path="115:/a.mkv")
    text = handlers.handle_whyplay(db, _tg(), 900001, "单片")
    assert "还没有转存" in text


# ---------- 私聊 guard ----------

def test_p1_group_chat_refused(db, monkeypatch):
    _user(db)
    # 群聊 chat_id != tg id：一律只回私聊提示，不泄露数据
    text = handlers.handle_bindings(db, _tg(), -100123, "")
    assert "私聊" in text
    update = {
        "update_id": 1,
        "message": {"message_id": 1, "from": _tg(), "chat": {"id": -100123, "type": "group"},
                    "text": "/bindings"},
    }
    sent = []

    def fake_send(db_, chat_id, text_, reply_markup=None, parse_mode="HTML"):
        sent.append(text_)
        return (True, None)

    import backend.tg_bot.router as router_mod
    monkeypatch.setattr(sender, "send_message", fake_send)
    router_mod.dispatch(db, update)
    assert sent and "私聊" in sent[0]


# ---------- P0：失败可见 ----------

def test_router_dispatch_failure_replies_visibly(db, monkeypatch):
    _user(db)
    update = {
        "update_id": 2,
        "message": {"message_id": 1, "from": _tg(), "chat": {"id": 900001, "type": "private"},
                    "text": "/bindings"},
    }
    sent = []

    def fake_send(db_, chat_id, text_, reply_markup=None, parse_mode="HTML"):
        sent.append(text_)
        return (True, None)

    def boom(db_, tg_user, chat_id, args):
        raise RuntimeError("db down")

    monkeypatch.setattr(sender, "send_message", fake_send)
    monkeypatch.setattr(handlers, "handle_bindings", boom)
    router.dispatch(db, update)
    assert sent == ["系统繁忙，请稍后再试"]


def test_poller_last_error_record_and_clear(db):
    poller._record_poll_error(db, RuntimeError("boom"))
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == poller.LAST_ERROR_KEY).first()
    assert row is not None and "boom" in (row.value or "")
    poller._clear_poll_error(db)
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == poller.LAST_ERROR_KEY).first()
    assert (row.value or "") == ""


def test_admin_status_builder(db):
    db.add(models.SystemConfig(
        key=poller.HEARTBEAT_KEY, value=datetime.now().isoformat()))
    db.add(models.SystemConfig(
        key=poller.LAST_ERROR_KEY, value="2026-10-28T10:00:00 boom"))
    db.commit()
    status = tg_bot_admin.bot_status(db)
    assert status["poller_active"] is True
    assert "boom" in status["last_error"]
    assert status["last_heartbeat"] != ""
    db.query(models.SystemConfig).filter(
        models.SystemConfig.key == poller.HEARTBEAT_KEY).update(
        {"value": (datetime.now() - timedelta(hours=2)).isoformat()})
    db.commit()
    assert tg_bot_admin.bot_status(db)["poller_active"] is False


# ---------- 菜单 ----------

def test_menu_contains_new_buttons(db, monkeypatch):
    _user(db)
    text, markup = handlers.menu_main(db, _tg(), True)
    # 私聊首行可能是一键登录 URL 按钮（无 callback_data，属合法混排；
    # 进程级 store 缓存会让它在全量顺序下出现，单跑时不出现），只对回调按钮断言。
    flat = [b["callback_data"] for row in markup["inline_keyboard"] for b in row
            if "callback_data" in b]
    for action in ("menu:bindings", "menu:transfers", "menu:whyplay", "menu:wants"):
        assert action in flat


def test_menu_callbacks_render_new_pages(db, monkeypatch):
    u = _user(db)
    _binding(db, u.id, label="我的谷歌", provider="drive", status="active")
    _item(db, 61, "单片", path="115:/a.mkv")
    answered, edits = [], []

    def fake_answer(db_, cq_id, text=None, show_alert=False):
        answered.append(cq_id)
        return (True, None)

    def fake_edit(db_, chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
        edits.append(text)
        return (True, None)

    monkeypatch.setattr(sender, "answer_callback_query", fake_answer)
    monkeypatch.setattr(sender, "edit_message_text", fake_edit)
    for action, needle in (
        ("bindings", "我的网盘绑定"),
        ("transfers", "转存进度"),
        ("whyplay", "为什么播不了"),
        ("wants", "我的想看"),
    ):
        edits.clear()
        cq = {"id": "cq-1", "from": _tg(),
              "data": f"menu:{action}",
              "message": {"message_id": 55, "chat": {"id": 900001}}}
        callbacks.handle_callback(db, cq)
        assert edits and needle in edits[0]

"""TG Bot 老虎机面板「主界面」死循环回归锁（2026-10-30 用户真机截图实证）。

现场：私聊下注成功后结果图卡正常；点「主界面」后机器人又发了一份
一模一样的老虎机结果卡，主菜单没出来，可反复触发。生产库核对：
该局只有一条 spin 记录、两张结果卡之间没有骰子消息——第二张卡只能
来自 _handle_slots_bet 的 duplicate 分支（重放已结算的同一 callback
时无条件重发结果卡）。poller 是 at-least-once（整批处理完才存
offset，崩溃/重启会重放整批），批次 [下注, 点主界面] 被重放时：
下注重放先重发结果卡，主界面重放编辑原卡又因内容未变被 TG 拒绝，
而 _handle_menu 对编辑失败只记 debug 日志、没有任何回落，用户就被
困在老虎机面板上。

本文件锁两条：
1. 已结算 spin 的 callback 重放只回 toast，绝不再发结果卡；
2. 面板导航编辑被 TG 拒绝时，回落必须是「删旧消息＋发新目标面板」，
   目标是用户点的那个面板（主菜单），不是重发当前面板。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
import backend.emby_server.models  # noqa: F401,E402  注册 emby_items 表，单文件跑也不缺 FK 目标
from backend.tg_bot import callbacks, sender  # noqa: E402

TG = 920777


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(
        name="公益服", slug="main", access_mode="free"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, tg=TG, points=1000):
    row = models.WebUser(
        username="navloop", password_hash="x", telegram_id=tg,
        points=points, vitality=14, is_staff=False)
    db.add(row)
    db.commit()
    return row


def _cq(data, mid=55, cq_id="cq-nav-1", photo=False):
    msg = {"message_id": mid, "chat": {"id": TG}}
    if photo:
        msg["photo"] = [{"file_id": "ph1", "width": 90, "height": 90}]
    return {"id": cq_id, "from": {"id": TG, "first_name": "Nav"},
            "data": data, "message": msg}


# ---------- 1. 重放已结算下注：只回 toast，不重发结果卡 ----------

def test_replayed_settled_bet_does_not_resend_result_panel(db, monkeypatch):
    user = _user(db, points=1000)
    sent_panels, edits, answers = [], [], []

    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False:
            answers.append(text) or (True, None))
    monkeypatch.setattr(
        sender, "send_dice",
        lambda db_, chat_id, emoji="🎰": (True, None, 47, 777))
    monkeypatch.setattr(
        sender, "send_panel",
        lambda db_, chat_id, text, markup=None, **kw:
            sent_panels.append(text) or (True, None))
    monkeypatch.setattr(
        sender, "edit_panel",
        lambda db_, chat_id, mid, text, markup=None, has_photo=False:
            edits.append(text) or (True, None))

    bet = _cq("slots_bet:20", cq_id="cq-bet-1")
    callbacks.handle_callback(db, bet)
    assert len(sent_panels) == 1
    assert "老虎机" in sent_panels[0] and "结果" in sent_panels[0]
    balance_after_bet = db.query(models.WebUser.points).filter(
        models.WebUser.id == user.id).scalar()

    # 用户点主界面：原地编辑成主菜单（导航本身成功）
    callbacks.handle_callback(db, _cq("menu:main", photo=True, cq_id="cq-home-1"))
    assert any("请选择功能" in t for t in edits)

    # 同一批被 at-least-once 重放：同一下注 callback 再次到达
    callbacks.handle_callback(db, bet)
    # 不许再发任何面板（旧行为会再发一份一模一样的老虎机结果卡）
    assert len(sent_panels) == 1
    # 只回 toast，且不重复扣款/结算
    assert any("已结算" in (t or "") for t in answers)
    assert db.query(models.WebUser.points).filter(
        models.WebUser.id == user.id).scalar() == balance_after_bet
    assert db.query(models.SlotsSpin).count() == 1


# ---------- 2. 导航编辑被拒：删旧＋发新目标面板 ----------

class _Transport:
    """在 sender 传输层录制全部 Bot API 调用；可指定哪些方法失败。"""

    def __init__(self, fail_methods=()):
        self.calls = []  # [(method, payload)]
        self.fail = set(fail_methods)

    def _record(self, method, payload):
        self.calls.append((method, payload))
        if method in self.fail:
            return False, f"Bad Request: {method} rejected"
        return True, None

    def install(self, monkeypatch):
        def fake_call(db_, method, payload):
            ok, err = self._record(method, payload)
            return ok, err

        def fake_call_ex(db_, method, payload):
            ok, err = self._record(method, payload)
            if not ok:
                return False, err, None
            return True, None, {"message_id": 999}

        def fake_mp(db_, method, payload, files):
            ok, err = self._record(method, payload)
            return ok, err

        def fake_mp_ex(db_, method, payload, files):
            ok, err = self._record(method, payload)
            if not ok:
                return False, err, None
            return True, None, {"message_id": 999}

        monkeypatch.setattr(sender, "_call", fake_call)
        monkeypatch.setattr(sender, "_call_ex", fake_call_ex)
        monkeypatch.setattr(sender, "_call_multipart", fake_mp)
        monkeypatch.setattr(sender, "_call_multipart_ex", fake_mp_ex)

    def methods(self):
        return [m for m, _ in self.calls]

    def sent_bodies(self):
        bodies = []
        for method, payload in self.calls:
            if method in ("sendPhoto", "sendMessage"):
                bodies.append(payload.get("caption") or payload.get("text") or "")
        return bodies


def test_home_from_slots_result_photo_edit_rejected_falls_back(db, monkeypatch):
    """老虎机结果图卡点主界面、caption 编辑被拒 → 删旧卡＋发主菜单新卡。"""
    _user(db)
    tp = _Transport(fail_methods={"editMessageCaption"})
    tp.install(monkeypatch)

    callbacks.handle_callback(db, _cq("menu:main", mid=999, photo=True))

    methods = tp.methods()
    assert "editMessageCaption" in methods  # 先走原地编辑
    assert "deleteMessage" in methods      # 被拒后删旧
    bodies = tp.sent_bodies()
    assert len(bodies) == 1                 # 只发一份新面板
    assert "请选择功能" in bodies[0]        # 目标是主菜单
    assert "再来一局" not in bodies[0]      # 不是重发老虎机结果卡
    # 顺序：删旧在发新之前，且删的是被点的那条消息
    dele = next(p for m, p in tp.calls if m == "deleteMessage")
    assert dele["message_id"] == 999
    assert methods.index("deleteMessage") < methods.index(
        "sendPhoto" if "sendPhoto" in methods else "sendMessage")


def test_home_from_slots_opening_photo_edit_rejected_falls_back(db, monkeypatch):
    """老虎机开局图卡点主界面、caption 编辑被拒 → 同样落到主菜单。"""
    _user(db)
    state = {"edits": 0}

    def fake_call(db_, method, payload):
        if method == "editMessageCaption":
            state["edits"] += 1
            if state["edits"] >= 2:  # 第二次编辑（主界面）被拒
                return False, "Bad Request: message is not modified"
            return True, None
        return True, None

    monkeypatch.setattr(sender, "_call", fake_call)
    monkeypatch.setattr(
        sender, "_call_ex",
        lambda db_, method, payload: (True, None, {"message_id": 999}))
    sent = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files:
            sent.append((method, payload)) or (True, None))
    monkeypatch.setattr(
        sender, "_call_multipart_ex",
        lambda db_, method, payload, files:
            sent.append((method, payload)) or (True, None, {"message_id": 999}))
    deleted = []
    orig_call = sender._call

    def fake_call2(db_, method, payload):
        if method == "deleteMessage":
            deleted.append(payload)
            return True, None
        return orig_call(db_, method, payload)

    monkeypatch.setattr(sender, "_call", fake_call2)

    # 先打开老虎机开局面板（编辑成功，落在图卡上）
    callbacks.handle_callback(db, _cq("menu:slots", mid=998, photo=True, cq_id="cq-open"))
    # 再点主界面：这次编辑被拒，必须删旧＋发主菜单新面板
    callbacks.handle_callback(db, _cq("menu:main", mid=998, photo=True, cq_id="cq-home"))

    assert deleted and deleted[0]["message_id"] == 998
    bodies = [p.get("caption") or p.get("text") or "" for _m, p in sent]
    assert any("请选择功能" in b for b in bodies)
    assert all("再来一局" not in b for b in bodies)


def test_home_from_text_panel_edit_rejected_falls_back(db, monkeypatch):
    """纯文本面板点主界面、text 编辑被拒 → 删旧＋发新主菜单（文本形态）。"""
    _user(db)
    tp = _Transport(fail_methods={"editMessageText"})
    tp.install(monkeypatch)

    callbacks.handle_callback(db, _cq("menu:main", mid=77, photo=False))

    methods = tp.methods()
    assert "deleteMessage" in methods
    bodies = tp.sent_bodies()
    assert len(bodies) == 1 and "请选择功能" in bodies[0]


def test_home_edit_success_no_fallback(db, monkeypatch):
    """编辑成功时不许触发回落：不删消息、不发新面板。"""
    _user(db)
    tp = _Transport()
    tp.install(monkeypatch)

    callbacks.handle_callback(db, _cq("menu:main", mid=999, photo=True))

    methods = tp.methods()
    assert "editMessageCaption" in methods
    assert "deleteMessage" not in methods
    assert tp.sent_bodies() == []

"""TG Bot 红包消息本体封面图（用户 2026-10-30 群内真机截图实证）。

#1523 只给「发红包发起面板」换了专属封面；红包消息本体（群里真正发出的
带抢按钮那条）仍是纯文本。本单口径：
- 本体发送走封面照片消息（assets/redpacket-cover.jpg + caption=现行文案
  + 现行抢按钮）；封面缺失/照片发送失败一律回落纯文本，红包绝不发不出；
- 被抢后的本体更新按消息形态分发：照片本体走 editMessageCaption
  （失败回落文本编辑），存量纯文本本体仍走 editMessageText 原路；
- 本体永不自毁的 router 分发豁免保持不变（照片形态同样豁免）。

DB 用内存 SQLite；Telegram 发送全部 patch 掉。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.integrations import store
from backend.tg_bot import callbacks, layout, redpacket_common, router, sender

GROUP_CHAT = -100777


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        models.WebUser.__table__,
        models.TgBindCode.__table__,
        models.SystemConfig.__table__,
        models.RedPacket.__table__,
        models.RedPacketClaim.__table__,
        models.PointsLog.__table__,
        models.CheckinRecord.__table__,
        models.VitalityLog.__table__,
        models.UserSubscription.__table__,
        models.SubscriptionPlan.__table__,
    ):
        table.create(engine, checkfirst=True)
    store.invalidate()
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_user(db, username="u1", tg_id=None, points=0):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        try:
            u.password_hash = ""
        except Exception:
            pass
    u.points = points
    db.add(u)
    db.commit()
    return u


def _msg(tg_id, chat_id, text, chat_type="supergroup", message_id=4242,
         update_id=99):
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": tg_id, "first_name": "T"},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


@pytest.fixture()
def wires(monkeypatch):
    """劫持发送层与自毁调度，记录 photo/plain/ex/caption_edits/text_edits/scheduled。"""
    from backend.tg_bot import ephemeral

    rec = {"photo": [], "plain": [], "ex": [], "cap_edits": [],
           "text_edits": [], "scheduled": [], "answers": []}

    def fake_photo(db_, chat_id, caption, reply_markup=None, photo_path=None,
                   parse_mode="HTML"):
        rec["photo"].append((chat_id, caption, reply_markup, photo_path))
        return (True, None)

    def fake_plain(db_, chat_id, text, reply_markup=None, **kw):
        rec["plain"].append((chat_id, text, reply_markup))
        return (True, None)

    def fake_ex(db_, chat_id, text, reply_markup=None, **kw):
        rec["ex"].append((chat_id, text, reply_markup))
        return (True, None, 9001)

    def fake_cap_edit(db_, chat_id, message_id, caption, reply_markup=None,
                      parse_mode="HTML"):
        rec["cap_edits"].append((chat_id, message_id, caption, reply_markup))
        return (True, None)

    def fake_text_edit(db_, chat_id, message_id, text, reply_markup=None,
                       parse_mode="HTML"):
        rec["text_edits"].append((chat_id, message_id, text, reply_markup))
        return (True, None)

    def fake_schedule(chat_id, message_id, delay):
        rec["scheduled"].append((chat_id, message_id, delay))
        return True

    def fake_answer(db_, cq_id, text=None, show_alert=False):
        rec["answers"].append((cq_id, text, show_alert))
        return (True, None)

    monkeypatch.setattr(sender, "send_photo", fake_photo)
    monkeypatch.setattr(sender, "send_message", fake_plain)
    monkeypatch.setattr(sender, "send_message_ex", fake_ex)
    monkeypatch.setattr(sender, "edit_message_caption", fake_cap_edit)
    monkeypatch.setattr(sender, "edit_message_text", fake_text_edit)
    monkeypatch.setattr(sender, "answer_callback_query", fake_answer)
    monkeypatch.setattr(ephemeral, "schedule_delete", fake_schedule)
    return rec


# ---------- send_packet_message：本体发送形态与回落 ----------

def _sample_packet(db, total=50, count=5):
    from backend import welfare_redpacket
    sender_user = _make_user(db, "s_body", 700101, 1000)
    packet = welfare_redpacket.send_packet(db, sender_user, total, count)
    return packet, "s_body"


def test_body_sent_as_photo_with_cover(db, wires):
    packet, name = _sample_packet(db)
    text = redpacket_common.packet_text(packet, name)
    markup = redpacket_common.claim_markup(packet.id)
    ok, err = redpacket_common.send_packet_message(db, GROUP_CHAT, text, markup)
    assert ok and err is None
    assert wires["plain"] == []
    assert len(wires["photo"]) == 1
    chat_id, caption, got_markup, photo_path = wires["photo"][0]
    assert chat_id == GROUP_CHAT
    assert photo_path == layout.REDPACKET_IMAGE
    assert caption == text
    assert "总额 50 能量" in caption and "剩余 5 个" in caption
    assert got_markup == markup
    btn = got_markup["inline_keyboard"][0][0]
    assert btn["text"] == "🧧 抢红包"
    assert btn["callback_data"] == f"redpacket_claim:{packet.id}"


def test_body_photo_api_failure_falls_back_to_text(db, wires, monkeypatch):
    packet, name = _sample_packet(db)
    text = redpacket_common.packet_text(packet, name)
    monkeypatch.setattr(sender, "send_photo",
                        lambda *a, **kw: (False, "api boom"))
    ok, err = redpacket_common.send_packet_message(
        db, GROUP_CHAT, text, redpacket_common.claim_markup(packet.id))
    assert ok and err is None
    assert len(wires["plain"]) == 1
    assert wires["plain"][0][1] == text


def test_body_cover_asset_missing_falls_back_to_text(db, wires, monkeypatch, tmp_path):
    packet, name = _sample_packet(db)
    text = redpacket_common.packet_text(packet, name)
    monkeypatch.setattr(layout, "REDPACKET_IMAGE", tmp_path / "nope.jpg")
    ok, err = redpacket_common.send_packet_message(
        db, GROUP_CHAT, text, redpacket_common.claim_markup(packet.id))
    assert ok and err is None
    assert wires["photo"] == []
    assert len(wires["plain"]) == 1
    assert wires["plain"][0][1] == text


def test_packet_texts_fit_caption_limit(db):
    packet, name = _sample_packet(db)
    assert layout.caption_fits(redpacket_common.packet_text(packet, name))
    assert layout.caption_fits(
        redpacket_common.finished_text(packet, name, "empty"))
    assert layout.caption_fits(
        redpacket_common.finished_text(packet, name, "expired"))


# ---------- router：群/私聊发红包都走照片本体且本体永不自毁 ----------

def test_router_group_redpacket_body_is_photo_and_exempt(db, wires):
    _make_user(db, "g1", 710101, points=1000)
    router.dispatch(db, _msg(710101, GROUP_CHAT, "/redpacket 100 10",
                             message_id=4301))
    assert db.query(models.RedPacket).count() == 1
    assert wires["plain"] == [] and wires["ex"] == []
    assert len(wires["photo"]) == 1
    assert "总额 100 能量" in wires["photo"][0][1]
    assert wires["photo"][0][3] == layout.REDPACKET_IMAGE
    # 本体不挂自毁；只有用户命令消息挂 3 秒
    assert wires["scheduled"] == [(GROUP_CHAT, 4301, 3)]


def test_router_private_redpacket_body_is_photo(db, wires):
    _make_user(db, "p1", 710102, points=1000)
    router.dispatch(db, _msg(710102, 710102, "/redpacket 50 5",
                             chat_type="private", message_id=4302))
    assert db.query(models.RedPacket).count() == 1
    assert len(wires["photo"]) == 1
    assert wires["plain"] == []
    assert wires["scheduled"] == []


def test_router_redpacket_photo_failure_still_sends_text(db, wires, monkeypatch):
    _make_user(db, "g2", 710103, points=1000)
    monkeypatch.setattr(sender, "send_photo",
                        lambda *a, **kw: (False, "api boom"))
    router.dispatch(db, _msg(710103, GROUP_CHAT, "/redpacket 100 10",
                             message_id=4303))
    assert db.query(models.RedPacket).count() == 1
    assert any("总额 100 能量" in t for (_c, t, _m) in wires["plain"])


# ---------- 抢后更新：按消息形态分发 ----------

def _claim_cq(packet_id, claimer_tg, photo):
    msg = {"message_id": 99, "chat": {"id": GROUP_CHAT, "type": "supergroup"}}
    if photo:
        msg["photo"] = [{"file_id": "cover-file-id"}]
    return {"id": "cq-body", "from": {"id": claimer_tg},
            "data": f"redpacket_claim:{packet_id}", "message": msg}


def _setup_claimable(db, total=50, count=5):
    from backend import welfare_redpacket
    sender_user = _make_user(db, "s_claim", 700201, 1000)
    packet = welfare_redpacket.send_packet(db, sender_user, total, count)
    claimer = _make_user(db, "c_claim", 700202, 0)
    return packet, claimer


def test_claim_on_photo_body_edits_caption(db, wires):
    packet, claimer = _setup_claimable(db)
    callbacks.handle_callback(db, _claim_cq(packet.id, 700202, photo=True))
    assert wires["text_edits"] == []
    assert len(wires["cap_edits"]) == 1
    assert "剩余 4 个" in wires["cap_edits"][0][2]
    btn = wires["cap_edits"][0][3]["inline_keyboard"][0][0]
    assert btn["text"] == "🧧 抢红包"
    assert db.query(models.RedPacketClaim).count() == 1


def test_claim_on_legacy_text_body_edits_text(db, wires):
    packet, claimer = _setup_claimable(db)
    callbacks.handle_callback(db, _claim_cq(packet.id, 700202, photo=False))
    assert wires["cap_edits"] == []
    assert len(wires["text_edits"]) == 1
    assert "剩余 4 个" in wires["text_edits"][0][2]


def test_claim_photo_caption_edit_failure_falls_back_to_text(db, wires, monkeypatch):
    packet, claimer = _setup_claimable(db)
    monkeypatch.setattr(sender, "edit_message_caption",
                        lambda *a, **kw: (False, "caption boom"))
    callbacks.handle_callback(db, _claim_cq(packet.id, 700202, photo=True))
    assert len(wires["text_edits"]) == 1
    assert "剩余 4 个" in wires["text_edits"][0][2]


def test_last_claim_on_photo_body_finishes_via_caption(db, wires):
    packet, claimer = _setup_claimable(db, total=30, count=1)
    callbacks.handle_callback(db, _claim_cq(packet.id, 700202, photo=True))
    assert wires["text_edits"] == []
    assert len(wires["cap_edits"]) == 1
    assert "已抢完" in wires["cap_edits"][0][2]
    assert wires["cap_edits"][0][3] == {"inline_keyboard": []}


def test_expired_photo_body_finishes_via_caption(db, wires):
    sender_user = _make_user(db, "s_exp2", 700301, 1000)
    packet = models.RedPacket(
        sender_id=sender_user.id, total_amount=50, total_count=2,
        remaining_amount=50, remaining_count=2,
        expires_at=datetime.now() - timedelta(hours=1))
    db.add(packet)
    db.commit()
    _make_user(db, "c_exp2", 700302, 0)
    callbacks.handle_callback(db, _claim_cq(packet.id, 700302, photo=True))
    assert wires["text_edits"] == []
    assert len(wires["cap_edits"]) == 1
    assert "已过期" in wires["cap_edits"][0][2]
    assert wires["cap_edits"][0][3] == {"inline_keyboard": []}


# ---------- 本体标签四字统一（用户 2026-10-30 亲点，补 #1535 存疑项 1） ----------

import re

# 本体标签旧名（layout.row 标签位）——统一四字后不得再出现在标签位
_OLD_BODY_LABELS = {"红包", "总额", "剩余", "有效期", "状态", "退款"}
_CJK4 = re.compile(r"^[一-鿿]{4}$")


def _body_row_labels(text):
    """抽取卡片信息行的标签（兼容 #1535 标签加粗前后的两种渲染形态）。"""
    labels = []
    for m in re.finditer(r"^· \S+ (.+?) \| ", text, re.M):
        labels.append(m.group(1).replace("<b>", "").replace("</b>", ""))
    return labels


def test_packet_body_labels_unified_four_chars(db):
    packet, name = _sample_packet(db)
    packet_labels = _body_row_labels(redpacket_common.packet_text(packet, name))
    assert packet_labels == ["红包主人", "红包总额", "剩余份数", "有效期限"]
    empty_labels = _body_row_labels(
        redpacket_common.finished_text(packet, name, "empty"))
    assert empty_labels == ["红包主人", "红包总额", "红包状态"]
    expired_labels = _body_row_labels(
        redpacket_common.finished_text(packet, name, "expired"))
    assert expired_labels == ["红包主人", "红包总额", "红包状态", "过期退款"]
    for label in packet_labels + empty_labels + expired_labels:
        assert _CJK4.match(label), label
        assert label not in _OLD_BODY_LABELS

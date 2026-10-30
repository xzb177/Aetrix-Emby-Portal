"""TG Bot 发红包面板专属封面图（用户 2026-10-30 亲批黑猫竖版封面）。

只测本单：发红包发起面板（按钮入口 menu:redpacket 的图卡渲染）改用
assets/redpacket-cover.jpg；其他面板仍用品牌图；封面图缺失时回退品牌图、
面板照常打开。DB 用内存 SQLite；Telegram 发送全部 patch 掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import callbacks, layout, sender

TG = 900001


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------- 资产与图解析 ----------

def test_redpacket_cover_asset_exists():
    assert layout.REDPACKET_IMAGE.is_file()
    data = layout.REDPACKET_IMAGE.read_bytes()
    assert data[:3] == b"\xff\xd8\xff"  # JPEG
    assert len(data) <= 300 * 1024


def test_panel_image_resolution():
    assert layout.panel_image() == layout.BRAND_IMAGE
    assert layout.panel_image(None) == layout.BRAND_IMAGE
    assert layout.panel_image(layout.REDPACKET_IMAGE) == layout.REDPACKET_IMAGE
    # 覆盖图缺失 → 回退品牌图，绝不把缺失路径交给发送层
    assert layout.panel_image(Path("/nonexistent/cover.jpg")) == layout.BRAND_IMAGE


# ---------- sender：面板图覆盖参数 ----------

def test_send_photo_cover_upload_name_and_mime(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: calls.append((method, payload, files)) or (True, None))
    ok, err = sender.send_photo(db, 123, "▎发红包", None, photo_path=layout.REDPACKET_IMAGE)
    assert ok and err is None
    method, _payload, files = calls[0]
    assert method == "sendPhoto"
    name, _fileobj, mime = files["photo"]
    assert name == "redpacket-cover.jpg" and mime == "image/jpeg"


def test_send_photo_missing_override_falls_back_to_brand(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: calls.append(files) or (True, None))
    ok, err = sender.send_photo(
        db, 123, "▎发红包", None, photo_path=Path("/nonexistent/cover.jpg"))
    assert ok and err is None
    name, _fileobj, mime = calls[0]["photo"]
    assert name == "aetrix-card.png" and mime == "image/png"


def test_send_panel_passes_photo_override(monkeypatch, db):
    seen = {}
    monkeypatch.setattr(
        sender, "send_photo",
        lambda db_, chat_id, text, markup=None, photo_path=None, parse_mode="HTML":
            seen.update(photo_path=photo_path) or (True, None))
    ok, _ = sender.send_panel(db, 1, "短文本", photo_path=layout.REDPACKET_IMAGE)
    assert ok and seen["photo_path"] == layout.REDPACKET_IMAGE
    seen.clear()
    ok, _ = sender.send_panel(db, 1, "短文本")
    assert ok and seen["photo_path"] is None


# ---------- sender：editMessageMedia 换图 ----------

def test_edit_message_media_payload(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: calls.append((method, payload, files)) or (True, None))
    ok, err = sender.edit_message_media(
        db, 123, 456, "▎发红包", {"inline_keyboard": []},
        photo_path=layout.REDPACKET_IMAGE)
    assert ok and err is None
    method, payload, files = calls[0]
    assert method == "editMessageMedia"
    media = json.loads(payload["media"])
    assert media["type"] == "photo" and media["media"] == "attach://photo"
    assert media["caption"] == "▎发红包"
    name, _fileobj, mime = files["photo"]
    assert name == "redpacket-cover.jpg" and mime == "image/jpeg"


def test_edit_message_media_missing_override_falls_back_to_brand(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: calls.append(files) or (True, None))
    ok, _ = sender.edit_message_media(
        db, 123, 456, "▎发红包", None, photo_path=Path("/nonexistent/cover.jpg"))
    assert ok
    name, _fileobj, mime = calls[0]["photo"]
    assert name == "aetrix-card.png" and mime == "image/png"


def test_edit_message_media_rejects_overlong_caption(monkeypatch, db):
    calls = []
    monkeypatch.setattr(
        sender, "_call_multipart",
        lambda db_, method, payload, files: calls.append(1) or (True, None))
    ok, err = sender.edit_message_media(db, 123, 456, "x" * 1025)
    assert not ok and "1024" in err and calls == []


# ---------- callbacks：只有发红包面板换图 ----------

def _patch_menu_sender(monkeypatch):
    state = {"media": [], "panel": []}
    monkeypatch.setattr(
        sender, "answer_callback_query",
        lambda db_, cq_id, text=None, show_alert=False: (True, None))
    monkeypatch.setattr(
        sender, "edit_message_media",
        lambda db_, chat_id, mid, caption, markup=None, photo_path=None, parse_mode="HTML":
            state["media"].append(photo_path) or (True, None))
    monkeypatch.setattr(
        sender, "edit_panel",
        lambda db_, chat_id, mid, text, markup=None, has_photo=False, parse_mode="HTML":
            state["panel"].append(has_photo) or (True, None))
    return state


def _cq(action, photo=True):
    msg = {"chat": {"id": TG, "type": "private"}, "message_id": 9}
    if photo:
        msg["photo"] = [{"file_id": "x"}]
    return {"id": "cq1", "data": f"menu:{action}", "from": {"id": TG, "first_name": "Alice"},
            "message": msg}


def test_redpacket_menu_uses_cover_image(monkeypatch, db):
    state = _patch_menu_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("redpacket"))
    assert state["media"] == [layout.REDPACKET_IMAGE]
    assert state["panel"] == []


def test_other_menu_keeps_brand_flow(monkeypatch, db):
    state = _patch_menu_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("group_help"))
    assert state["media"] == []
    assert state["panel"] == [True]  # 仍走 caption 编辑（品牌图不变）


def test_redpacket_menu_media_failure_falls_back_to_caption_edit(monkeypatch, db):
    state = _patch_menu_sender(monkeypatch)
    monkeypatch.setattr(
        sender, "edit_message_media",
        lambda db_, chat_id, mid, caption, markup=None, photo_path=None, parse_mode="HTML":
            state["media"].append(photo_path) or (False, "boom"))
    callbacks.handle_callback(db, _cq("redpacket"))
    assert state["media"] == [layout.REDPACKET_IMAGE]
    assert state["panel"] == [True]  # 换图失败 → 普通图卡编辑，面板照常打开


def test_redpacket_menu_on_text_message_stays_text(monkeypatch, db):
    state = _patch_menu_sender(monkeypatch)
    callbacks.handle_callback(db, _cq("redpacket", photo=False))
    assert state["media"] == []
    assert state["panel"] == [False]

"""TG Bot 面板信息行标签守卫：全 4 字汉字＋标签加粗＋跨面板一致。

用户 2026-10-30 定版：信息行统一为 · emoji <b>标签</b> | 值，
标签一律恰好 4 个汉字（不留拉丁字母，如 ID 必须汉字化）；
同一数据在 /start dashboard 与 /account 必须同名。
"""
from __future__ import annotations

import re
import sys

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import features, handlers, layout

TG = 900001

LABEL_RE = re.compile(r"^· \S+ <b>([^<]+)</b> \|", re.M)
CJK4 = re.compile(r"^[一-鿿]{4}$")

DASHBOARD_LABELS = [
    "用户编号", "公益资格", "查看凭证", "能量余额", "活力数值",
    "当前等级", "网盘绑定", "连签天数", "成就进度",
]
SHARED_LABELS = [
    "用户编号", "能量余额", "公益资格", "查看凭证", "活力数值",
    "当前等级", "成就进度",
]


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


def _user(db, tg=TG, name="alice", points=42, vitality=14, is_welfare=True):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=vitality, is_welfare=is_welfare,
    )
    db.add(row)
    db.commit()
    return row


def _labels(text):
    return LABEL_RE.findall(text)


def test_layout_row_bolds_label():
    assert layout.row("🍒", "能量余额", 16) == "· 🍒 <b>能量余额</b> | 16"


def test_dashboard_labels_are_the_nine_four_char(db):
    user = _user(db)
    text = features.user_panel(db, user)
    assert _labels(text) == DASHBOARD_LABELS
    for label in _labels(text):
        assert CJK4.match(label), label


def test_account_labels_four_char_and_consistent_with_dashboard(db):
    user = _user(db)
    text = handlers.handle_account(db, {"id": TG, "first_name": "Alice"}, TG, "")
    labels = _labels(text)
    assert labels, "account 面板应有信息行"
    for label in labels:
        assert CJK4.match(label), label
    dash_labels = _labels(features.user_panel(db, user))
    for label in SHARED_LABELS:
        assert label in labels, label
        assert label in dash_labels, label


def test_points_panel_labels(db):
    user = _user(db)
    text = handlers.handle_points(db, {"id": TG}, TG, "")
    labels = _labels(text)
    assert "能量余额" in labels
    assert "活力数值" in labels
    for label in labels:
        assert CJK4.match(label), label


def test_no_legacy_wording_in_panels(db):
    user = _user(db)
    for text in (
        features.user_panel(db, user),
        handlers.handle_account(db, {"id": TG}, TG, ""),
        handlers.handle_points(db, {"id": TG}, TG, ""),
    ):
        assert "积分" not in text
        assert "用户 ID" not in text
        assert "会员等级" not in text

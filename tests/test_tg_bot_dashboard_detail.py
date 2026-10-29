"""TG Bot /start 已绑定用户 dashboard 详细版：批准版式各字段状态。

只测本模块：DB 用内存 SQLite；Telegram 发送全部 patch 掉，不发真实请求。
版式（用户 2026-10-29 08:44 亲批，取代 00:15 版）：问候＋账号·等级行＋
资格／状态／成长三组；活力条按上限满格、馆灵条 10 格按本阶段进度；
末尾两行指引已删；订阅与最近设备不进精简面板。
"""
from __future__ import annotations

import sys
from datetime import datetime

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em
from backend.tg_bot import features, handlers

TICKS = 10_000_000
GROUP = -100123
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


def _user(db, tg=TG, name="alice", points=42, vitality=14,
          is_welfare=False, welfare_expires_at=None):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=vitality,
        is_welfare=is_welfare, welfare_expires_at=welfare_expires_at,
    )
    db.add(row)
    db.commit()
    return row


def _tg(tg=TG):
    return {"id": tg, "first_name": "Alice"}


def _watch(db, user_id, hours_list):
    for i, h in enumerate(hours_list):
        item = em.MediaItem(
            guid=f"test-item-{user_id}-{i}", library_id=1,
            item_type="movie", name=f"片{i}",
            duration_ticks=int(h * 3600 * TICKS),
        )
        db.add(item)
        db.flush()
        db.add(em.PlaybackSession(
            user_id=user_id, item_id=item.id,
            position_ticks=int(h * 3600 * TICKS),
            session_key=f"test-sess-{user_id}-{i}",
        ))
    db.commit()


def _achievements(db, user_id):
    db.add(models.PanelAchievementDef(
        key="a1", name="首夜", desc="d", icon="🌙", sort=1, enabled=True))
    db.add(models.PanelAchievementDef(
        key="b1", name="追剧达人", desc="d", icon="🎬", sort=2, enabled=True))
    db.add(models.PanelAchievementDef(
        key="c1", name="隐藏", desc="d", icon="🙈", sort=3, enabled=False))
    db.add(models.PanelAchievementUnlock(user_id=user_id, ach_key="a1"))
    db.add(models.PanelAchievementUnlock(user_id=user_id, ach_key="c1"))
    db.commit()


def _unlock(db, user_id, expires_at, realm_id=1):
    db.add(models.EmbyViewUnlock(
        user_id=user_id, realm_id=realm_id, expires_at=expires_at))
    db.commit()


def _binding(db, user_id, provider, status="active", label="盘"):
    db.add(em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=1, status=status))
    db.commit()


def _checkin(db, user_id, streak, date=datetime(2026, 10, 28)):
    db.add(models.CheckinRecord(
        user_id=user_id, checkin_date=date, streak=streak))
    db.commit()


# ---------- 公益资格三态 ----------

def test_welfare_states(db):
    off = _user(db, tg=TG, name="off")
    assert "公益资格：未开通" in features.compact_dashboard(db, off)
    dated = _user(db, tg=TG + 1, name="dated", is_welfare=True,
                  welfare_expires_at=datetime(2026, 11, 27))
    assert "公益资格：至 2026-11-27" in features.compact_dashboard(db, dated)
    perm = _user(db, tg=TG + 2, name="perm", is_welfare=True,
                 welfare_expires_at=None)
    assert "公益资格：长期有效" in features.compact_dashboard(db, perm)


# ---------- 查看凭证 ----------

def test_view_unlock_states(db):
    none = _user(db, tg=TG, name="none")
    assert "查看凭证：未开通" in features.compact_dashboard(db, none)
    dated = _user(db, tg=TG + 1, name="dated")
    _unlock(db, dated.id, datetime(2030, 1, 1))
    assert "查看凭证：至 2030-01-01" in features.compact_dashboard(db, dated)
    perm = _user(db, tg=TG + 2, name="perm")
    _unlock(db, perm.id, None)
    assert "查看凭证：长期有效" in features.compact_dashboard(db, perm)
    expired = _user(db, tg=TG + 3, name="expired")
    _unlock(db, expired.id, datetime(2020, 1, 1))
    assert "查看凭证：未开通" in features.compact_dashboard(db, expired)
    mixed = _user(db, tg=TG + 4, name="mixed")
    _unlock(db, mixed.id, datetime(2020, 1, 1), realm_id=1)
    _unlock(db, mixed.id, datetime(2030, 6, 1), realm_id=2)
    assert "查看凭证：至 2030-06-01" in features.compact_dashboard(db, mixed)


# ---------- 网盘绑定 ----------

def test_bindings_line(db):
    none = _user(db, tg=TG, name="none")
    assert "网盘：未绑定，绑定后才能点播" in features.compact_dashboard(db, none)
    drive = _user(db, tg=TG + 1, name="drive")
    _binding(db, drive.id, "drive")
    assert "网盘：Drive 已绑 ✓" in features.compact_dashboard(db, drive)
    pan = _user(db, tg=TG + 2, name="pan")
    _binding(db, pan.id, "pan115")
    assert "网盘：115 已绑 ✓" in features.compact_dashboard(db, pan)
    both = _user(db, tg=TG + 3, name="both")
    _binding(db, both.id, "drive")
    _binding(db, both.id, "pan115")
    text = features.compact_dashboard(db, both)
    assert "Drive 已绑" in text and "115 已绑" in text
    invalid = _user(db, tg=TG + 4, name="invalid")
    _binding(db, invalid.id, "drive", status="invalid")
    assert "网盘：未绑定，绑定后才能点播" in features.compact_dashboard(db, invalid)


# ---------- 活力值／上限／低于阈值提示 ----------

def test_vitality_line(db):
    ok = _user(db, tg=TG, name="ok", vitality=14)
    text = features.compact_dashboard(db, ok)
    assert "活力：██████████████ 14/14" in text
    assert "暂时播不了" not in text
    low = _user(db, tg=TG + 1, name="low", vitality=2)
    text = features.compact_dashboard(db, low)
    assert "活力：██░░░░░░░░░░░░ 2/14" in text
    assert "暂时播不了" in text


# ---------- 馆灵下一阶段差值与满级 ----------

def test_pet_next_and_max(db):
    row = _user(db, tg=TG, name="mid")
    _watch(db, row.id, [100, 20])
    text = features.compact_dashboard(db, row)
    assert "夜枭" in text and "120.0" in text
    assert "距守夜者还差 80.0 小时 █████░░░░░" in text
    top = _user(db, tg=TG + 1, name="top")
    _watch(db, top.id, [1200])
    text = features.compact_dashboard(db, top)
    assert "已满级" in text


# ---------- 连签 ----------

def test_streak_line(db):
    none = _user(db, tg=TG, name="none")
    assert "连签：0 天" in features.compact_dashboard(db, none)
    row = _user(db, tg=TG + 1, name="streak")
    _checkin(db, row.id, 5)
    text = features.compact_dashboard(db, row)
    assert "连签：5 天" in text
    assert "满 30 天自动续公益资格" in text


# ---------- 批准版式整版：字段齐全、两行已删、精简面板不展开订阅 ----------

def test_approved_layout_full(db):
    row = _user(db, tg=TG, name="alice", points=16, vitality=14,
                is_welfare=True, welfare_expires_at=datetime(2026, 11, 27))
    _watch(db, row.id, [100, 20])
    _achievements(db, row.id)
    _unlock(db, row.id, datetime(2026, 11, 27))
    _binding(db, row.id, "drive")
    _checkin(db, row.id, 7)
    reply = handlers.handle_start(db, _tg(), GROUP, "")
    assert isinstance(reply, str)
    for frag in ("✨ 欢迎回来，Alice", "👤 alice · ⭐ Lv.1 初幕",
                 "公益资格：至 2026-11-27",
                 "查看凭证：至 2026-11-27",
                 "能量：16", "活力：██████████████ 14/14",
                 "网盘：Drive 已绑 ✓",
                 "夜枭", "距守夜者还差 80.0 小时 █████░░░░░",
                 "成就：1/2", "连签：7 天"):
        assert frag in reply, frag
    assert "左下角" not in reply
    assert "一键免密登录请私聊" not in reply
    assert "订阅" not in reply
    assert "最近设备" not in reply

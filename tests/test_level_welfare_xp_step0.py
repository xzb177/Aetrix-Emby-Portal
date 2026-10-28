"""等级公益服活动治理 Step 0：公益原生经验来源。

观影（与活力同分档同结算点、每条目每日 1 次）／签到（＋1／日）／成就（＋10／项
一次性）；可刷部分（观影＋签到）日封顶 10；MemberXpLog source／ref_id 标记；
Step 0 总开关关闭时三源皆为 0。

全部用隔离的内存 SQLite，不碰生产库。
"""
import os
import sys
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend import member_level as ml
from backend.integrations import store as _store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    ml.ensure_member_levels_seeded(s)
    # 配置有进程内 TTL 热缓存：别的测试关掉过的开关不能串进来
    _store.invalidate(
        ml.XP_WELFARE_SOURCES_SWITCH,
        "vitality_enabled",
        "vitality_watch_tier1_minutes", "vitality_watch_tier1_reward",
        "vitality_watch_tier2_minutes", "vitality_watch_tier2_reward")
    yield s
    s.close()


def _user(db, name):
    u = models.WebUser(username=name, password_hash="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _prefill_farmable(db, user_id, amount, source="watch", ref="prefill"):
    """直接插一条当日正向可刷流水（模拟已发的观影／签到经验）。"""
    db.add(models.MemberXpLog(user_id=user_id, xp_delta=amount, xp_after=amount,
                              source=source, ref_id=ref, created_at=datetime.now()))
    db.commit()


def _xp_logs(db, user_id, source=None):
    q = db.query(models.MemberXpLog).filter(models.MemberXpLog.user_id == user_id)
    if source:
        q = q.filter(models.MemberXpLog.source == source)
    return q.all()


def _xp_total(db, user_id):
    return sum(r.xp_delta for r in _xp_logs(db, user_id))


# ---------- 观影 ----------

def test_watch_tier_boundaries(db):
    """观影档位边界：29→0、30→1、59→1、60→2、120→2（每例独立用户，互不占上限）。"""
    cases = [(29, 0), (30, 1), (59, 1), (60, 2), (120, 2)]
    for i, (minutes, expected) in enumerate(cases):
        u = _user(db, f"tier{i}")
        got = ml.award_watch_xp(db, u.id, minutes, item_id=1000 + i)
        assert got == expected
        assert _xp_total(db, u.id) == expected


def test_watch_per_item_per_day_once(db):
    """同一条目每日只计 1 次：同条目第二次 Stopped 为 0，换条目仍可发。"""
    u = _user(db, "dedup")
    assert ml.award_watch_xp(db, u.id, 60, item_id=42) == 2
    assert ml.award_watch_xp(db, u.id, 60, item_id=42) == 0
    assert ml.award_watch_xp(db, u.id, 30, item_id=43) == 1
    logs = _xp_logs(db, u.id, source="watch")
    assert sorted(r.ref_id for r in logs) == ["watch:42", "watch:43"]


def test_watch_daily_cap_truncation(db):
    """可刷日封顶 10：已发 9 时 60 分钟档按剩余量截断为 1；已发满 10 时为 0。"""
    u = _user(db, "cap9")
    _prefill_farmable(db, u.id, 9)
    assert ml.award_watch_xp(db, u.id, 60, item_id=1) == 1
    assert _xp_total(db, u.id) == 10

    u2 = _user(db, "cap10")
    _prefill_farmable(db, u2.id, 10)
    assert ml.award_watch_xp(db, u2.id, 60, item_id=1) == 0
    assert _xp_total(db, u2.id) == 10


def test_watch_xp_levels_up(db):
    """经验一流动自然升级：99 XP＋30 分钟观影＝100，升 Lv.2（阈值表不动）。"""
    u = _user(db, "levelup")
    ml.add_xp(db, u, 99, "recharge", ref_id="recharge:seed")
    db.commit()
    assert ml.award_watch_xp(db, u.id, 30, item_id=7) == 1
    db.refresh(u)
    assert u.member_xp == 100
    assert u.member_level == 2


# ---------- 签到 ----------

def test_checkin_xp_basic(db):
    """签到＋1／日：首发 1，同一签到日重复调用为 0，流水 source／ref 标记正确。"""
    u = _user(db, "checkin")
    assert ml.award_checkin_xp(db, u) == 1
    db.commit()
    assert ml.award_checkin_xp(db, u) == 0
    logs = _xp_logs(db, u.id, source="checkin")
    assert len(logs) == 1
    assert logs[0].xp_delta == 1
    assert logs[0].ref_id.startswith("checkin:")


def test_checkin_xp_daily_cap(db):
    """签到受可刷日封顶钳制：已发满 10 时签到 XP 为 0。"""
    u = _user(db, "checkincap")
    _prefill_farmable(db, u.id, 10)
    assert ml.award_checkin_xp(db, u) == 0


# ---------- 成就 ----------

def test_achievement_xp_once(db):
    """成就＋10／项一次性：首发 10，重复调用为 0，流水 source／ref 标记正确。"""
    u = _user(db, "ach")
    assert ml.award_achievement_xp(db, u.id, "first_night") == 10
    assert ml.award_achievement_xp(db, u.id, "first_night") == 0
    logs = _xp_logs(db, u.id, source="achievement")
    assert len(logs) == 1
    assert logs[0].xp_delta == 10
    assert logs[0].ref_id == "first_night"


def test_achievement_not_counted_in_cap(db):
    """成就一次性不计入日封顶：可刷已发满 10，成就仍可发 10。"""
    u = _user(db, "achcap")
    _prefill_farmable(db, u.id, 10)
    assert ml.award_achievement_xp(db, u.id, "streak7") == 10
    assert _xp_total(db, u.id) == 20


# ---------- 总开关 ----------

def _switch_off(db):
    _store.write_values(db, {ml.XP_WELFARE_SOURCES_SWITCH: "false"})
    _store.invalidate(ml.XP_WELFARE_SOURCES_SWITCH)


def test_switch_off_blocks_all_sources(db):
    """总开关关闭时三源皆为 0，且不写任何新流水。"""
    u = _user(db, "off")
    _switch_off(db)
    assert ml.welfare_xp_enabled(db) is False
    assert ml.award_watch_xp(db, u.id, 60, item_id=1) == 0
    assert ml.award_checkin_xp(db, u) == 0
    assert ml.award_achievement_xp(db, u.id, "first_night") == 0
    assert _xp_logs(db, u.id) == []


def test_switch_on_by_default(db):
    """总开关缺省=开：不配行时三源正常发。"""
    assert ml.welfare_xp_enabled(db) is True
    u = _user(db, "on")
    assert ml.award_watch_xp(db, u.id, 30, item_id=1) == 1

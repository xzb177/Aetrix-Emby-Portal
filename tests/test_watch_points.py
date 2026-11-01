"""观影能量（货币再设计第一期）：当日累计分钟分档发能量。

口径：与活力观影奖同结算点（Stopped）、同输入（单条目本轮最远点分钟数），
但按「当日累计」结算——同条目重复停播只计超出当日基线的增量，看完后
基线清零。首看 20 分钟 +4、一档 45 分钟 +8、二档 120 分钟 +7，日封顶 16。
幂等凭证是 points_logs 的 ref_id（watch_points:<上海日期>:<档位>）；
跨日按 Asia/Shanghai 日界重置。全部用隔离的内存 SQLite。
"""
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend import watch_points as wp
from backend.integrations import store as _store

_KEYS = [
    "watch_points_first", "watch_points_first_minutes",
    "watch_points_tier1", "watch_points_tier1_minutes",
    "watch_points_tier2", "watch_points_tier2_minutes",
    "watch_points_daily_cap",
]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    # 配置有进程内 TTL 热缓存：别的测试改过的键不能串进来
    _store.invalidate(*_KEYS)
    yield s
    s.close()


def _user(db, name, welfare=True):
    u = models.WebUser(username=name, password_hash="x", is_welfare=welfare)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set_config(db, **kv):
    for k, v in kv.items():
        db.add(models.SystemConfig(key=k, value=str(v)))
    db.commit()
    _store.invalidate(*kv.keys())


def _points(db, user_id):
    return int(db.query(models.WebUser.points).filter(
        models.WebUser.id == user_id).scalar() or 0)


def _logs(db, user_id):
    return (db.query(models.PointsLog)
            .filter(models.PointsLog.user_id == user_id,
                    models.PointsLog.type == "watch_points")
            .order_by(models.PointsLog.id).all())


def _today_sh() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def test_first_award_boundary(db):
    """首看边界：累计 19 分钟不发，满 20 分钟发 +4（跨条目累计）。"""
    u = _user(db, "first")
    assert wp.award_watch_points(db, u.id, 19, item_id=1) == 0
    assert _points(db, u.id) == 0
    assert wp.award_watch_points(db, u.id, 1, item_id=2) == 4
    assert _points(db, u.id) == 4
    logs = _logs(db, u.id)
    assert len(logs) == 1
    assert logs[0].amount == 4
    assert logs[0].ref_id == f"watch_points:{_today_sh()}:first"
    assert logs[0].balance_after == 4


def test_cumulative_across_items_to_tier1(db):
    """跨条目累计：一档 45 分钟在第二个条目结算时触发 +8。"""
    u = _user(db, "cum")
    assert wp.award_watch_points(db, u.id, 20, item_id=1) == 4
    assert wp.award_watch_points(db, u.id, 25, item_id=2) == 8
    assert _points(db, u.id) == 12
    assert [r.ref_id.rsplit(":", 1)[-1] for r in _logs(db, u.id)] == ["first", "tier1"]


def test_same_item_repeat_stop_no_double_count(db):
    """同条目重复停播只计增量：同位置再停 +0，推进到新位置才继续累计。"""
    u = _user(db, "repeat")
    assert wp.award_watch_points(db, u.id, 20, item_id=7) == 4
    assert wp.award_watch_points(db, u.id, 20, item_id=7) == 0
    assert wp.award_watch_points(db, u.id, 44, item_id=7) == 0
    assert wp.award_watch_points(db, u.id, 45, item_id=7) == 8
    assert _points(db, u.id) == 12


def test_tier2_and_daily_cap(db):
    """一次看满 120 分钟：4+8+7 被日封顶 16 钳到 16；之后再看不发。"""
    u = _user(db, "cap")
    assert wp.award_watch_points(db, u.id, 120, item_id=1) == 16
    assert _points(db, u.id) == 16
    amounts = [r.amount for r in _logs(db, u.id)]
    assert amounts == [4, 8, 4]
    assert wp.award_watch_points(db, u.id, 30, item_id=2) == 0
    assert _points(db, u.id) == 16


def test_cap_zero_means_unlimited(db):
    """日封顶 0 = 不限（与活力每日上限同口径）：三档全额 19。"""
    _set_config(db, watch_points_daily_cap=0)
    u = _user(db, "nocap")
    assert wp.award_watch_points(db, u.id, 120, item_id=1) == 19
    assert _points(db, u.id) == 19


def test_non_welfare_gets_zero(db):
    """非公益用户不发放：看满 120 分钟也得 0，且不建累计行。"""
    u = _user(db, "plain", welfare=False)
    assert wp.award_watch_points(db, u.id, 120, item_id=1) == 0
    assert _points(db, u.id) == 0
    assert _logs(db, u.id) == []
    assert db.query(models.WatchPointsDaily).filter(
        models.WatchPointsDaily.user_id == u.id).count() == 0


def test_cross_day_reset(db):
    """跨日重置：昨日累计行不影响今日，首看奖次日可再发。"""
    u = _user(db, "rollover")
    assert wp.award_watch_points(db, u.id, 25, item_id=1) == 4
    # 模拟跨日：累计行与流水都拨回昨天（真实跨日时 ref 日期与创建时间同为昨天）
    row = db.query(models.WatchPointsDaily).filter(
        models.WatchPointsDaily.user_id == u.id).one()
    row.day = "2000-01-01"
    from datetime import timedelta
    for log in _logs(db, u.id):
        log.ref_id = log.ref_id.replace(_today_sh(), "2000-01-01")
        log.created_at = datetime.now() - timedelta(days=1)
    db.commit()
    assert wp.award_watch_points(db, u.id, 25, item_id=1) == 4
    assert _points(db, u.id) == 8
    days = sorted(r.day for r in db.query(models.WatchPointsDaily).filter(
        models.WatchPointsDaily.user_id == u.id).all())
    assert days == ["2000-01-01", _today_sh()]


def test_finished_resets_baseline_rewatch_counts(db):
    """看完后基线清零：同条目重看的分钟重新计入累计，可推到二档。"""
    u = _user(db, "rewatch")
    assert wp.award_watch_points(db, u.id, 50, item_id=9, finished=True) == 12
    assert wp.award_watch_points(db, u.id, 20, item_id=9) == 0
    assert wp.award_watch_points(db, u.id, 120, item_id=9) == 4  # tier2 被封顶钳到 4
    assert _points(db, u.id) == 16


def test_tiers_grant_once_on_later_settlements(db):
    """每档每日只发一次：达档后后续结算（含补结算）不重发。"""
    u = _user(db, "once")
    assert wp.award_watch_points(db, u.id, 20, item_id=1) == 4
    assert wp.award_watch_points(db, u.id, 25, item_id=2) == 8
    assert wp.award_watch_points(db, u.id, 10, item_id=3) == 0
    assert wp.award_watch_points(db, u.id, 10, item_id=3) == 0
    assert len(_logs(db, u.id)) == 2


def test_zero_reward_tier_skipped(db):
    """某档奖励配 0 = 该档关闭：不发、不写流水，其余档正常。"""
    _set_config(db, watch_points_first=5, watch_points_first_minutes=10,
                watch_points_tier1=0, watch_points_tier2=0)
    u = _user(db, "custom")
    assert wp.award_watch_points(db, u.id, 10, item_id=1) == 5
    assert wp.award_watch_points(db, u.id, 130, item_id=2) == 0
    assert [r.amount for r in _logs(db, u.id)] == [5]


def test_validation():
    """校验：默认合法；分钟数倒挂、负数报错。"""
    assert wp.validate_watch_points_config({}) == {}
    errors = wp.validate_watch_points_config({
        "watch_points_first_minutes": "50",
        "watch_points_tier1_minutes": "45",
    })
    assert "watch_points_tier1_minutes" in errors
    errors = wp.validate_watch_points_config({
        "watch_points_tier1_minutes": "130",
        "watch_points_tier2_minutes": "120",
    })
    assert "watch_points_tier2_minutes" in errors
    errors = wp.validate_watch_points_config({"watch_points_first": "-1"})
    assert "watch_points_first" in errors
    errors = wp.validate_watch_points_config({"watch_points_daily_cap": "-1"})
    assert "watch_points_daily_cap" in errors


def test_config_defaults(db):
    """无配置行时走默认值：首看 4/20、一档 8/45、二档 7/120、封顶 16。"""
    cfg = wp.get_watch_points_config(db)
    assert cfg == {
        "first": 4, "first_minutes": 20,
        "tier1": 8, "tier1_minutes": 45,
        "tier2": 7, "tier2_minutes": 120,
        "daily_cap": 16,
    }

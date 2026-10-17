"""r34 后端完美迭代：用户权益类「日/月界线」口径统一收口（Asia/Shanghai 自然日）。

F1: 签到日界线 economy._today_start → 上海自然日（UTC 入库帧）
F2: 聊天积分每日上限 + 积分概览「今日获得」points._today_start → 共享 helper
F3: 活力值每日扣减日期 vitality._today_str → 上海日期
F4: 活力值每日免费获取上限 get_today_free_gain → 上海日界
F5: 求片每日额度 media_seek.used_today → 上海日界
F6: 求片月度额度 media_seek.used_this_month / welfare_requests._month_start → 上海月界
F7: 管理后台今日签到数 admin_economy 与签到写入帧同口径（F1 后不跟进会恒 0）
F8: 趋势图签到系列按上海日期映射（F1 后 func.date 会整列错位一天）
小修: panel_achievements.evaluate 删从未使用的 today_start 死变量
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import media_seek, models, points, vitality, welfare_requests
from backend.api import admin_economy, economy
from backend.emby_server import models as em_models


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _make_user(db, username="u1", **kw):
    u = models.WebUser(username=username, password_hash="", points=0,
                       is_active=True, **kw)
    db.add(u)
    db.commit()
    return u


def _set_cfg(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _shanghai_today_start_utc():
    """测试侧独立算出的期望值：上海自然日 00:00 → UTC naive。"""
    now_sh = datetime.now(ZoneInfo("Asia/Shanghai")).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return now_sh.astimezone(timezone.utc).replace(tzinfo=None)


# ---------------- F1: 签到日界线 ----------------

def test_today_start_is_shanghai_midnight_utc_frame():
    ts = economy._today_start()
    assert ts.tzinfo is None
    assert ts == _shanghai_today_start_utc()
    sh = ts.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    assert (sh.hour, sh.minute, sh.second, sh.microsecond) == (0, 0, 0, 0)


def test_checkin_writes_shanghai_day_start(db):
    u = _make_user(db)
    economy._do_checkin_core(db, u)
    rec = db.query(models.CheckinRecord).filter(
        models.CheckinRecord.user_id == u.id).one()
    assert rec.checkin_date == _shanghai_today_start_utc()


def test_checkin_duplicate_still_rejected(db):
    u = _make_user(db, username="u2")
    economy._do_checkin_core(db, u)
    with pytest.raises(HTTPException) as ei:
        economy._do_checkin_core(db, u)
    assert ei.value.status_code == 400


# ---------------- F2: 聊天积分上限 / 今日获得 ----------------

def _beijing_4am_today_utc():
    """北京时间今天 04:00 对应的 UTC naive 时间（旧口径下算昨天、新口径下算今天）"""
    now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
    four = now_sh.replace(hour=4, minute=0, second=0, microsecond=0)
    return four.astimezone(timezone.utc).replace(tzinfo=None)


def test_chat_points_cap_uses_shanghai_day(db):
    u = _make_user(db, username="u3")
    _set_cfg(db, "points_chat_daily_cap", 5)
    _set_cfg(db, "points_chat_per_msg", 5)
    # 北京时间今天 04:00 的一条发言奖励流水：新口径下已占满今日上限
    db.add(models.PointsLog(user_id=u.id, amount=5, balance_after=5,
                            type="chat", description="t",
                            created_at=_beijing_4am_today_utc()))
    db.commit()
    assert points.award_chat_points(db, u.id) == 0


def test_points_summary_today_uses_shanghai_day(db):
    u = _make_user(db, username="u4")
    db.add(models.PointsLog(user_id=u.id, amount=7, balance_after=7,
                            type="chat", description="t",
                            created_at=_beijing_4am_today_utc()))
    db.commit()
    summary = points.get_points_summary(db, u)
    assert summary["today_earned"] == 7


# ---------------- F3/F4: 活力值 ----------------

def test_vitality_today_str_is_shanghai_date():
    expected = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert vitality._today_str() == expected


def test_vitality_free_gain_uses_shanghai_day(db):
    u = _make_user(db, username="u5")
    db.add(models.VitalityLog(user_id=u.id, delta=2, balance_after=2,
                              reason="checkin",
                              created_at=_beijing_4am_today_utc()))
    db.commit()
    assert vitality.get_today_free_gain(db, u.id) == 2


# ---------------- F5/F6: 求片日/月额度 ----------------

def test_media_seek_used_today_uses_shanghai_day(db):
    u = _make_user(db, username="u6")
    db.add(models.MovieRequest(user_id=u.id, movie_name="t",
                               created_at=_beijing_4am_today_utc()))
    db.commit()
    assert media_seek.used_today(db, u.id) == 1


def test_month_start_utc_naive_frame():
    ms = economy._month_start_utc_naive()
    assert ms.tzinfo is None
    sh = ms.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    assert (sh.day, sh.hour, sh.minute) == (1, 0, 0)


def test_media_seek_used_this_month_uses_shanghai_month(db):
    u = _make_user(db, username="u7")
    now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
    first_4am = now_sh.replace(day=1, hour=4, minute=0, second=0, microsecond=0)
    created = first_4am.astimezone(timezone.utc).replace(tzinfo=None)
    db.add(models.MovieRequest(user_id=u.id, movie_name="t", created_at=created))
    db.commit()
    assert media_seek.used_this_month(db, u.id) == 1


def test_welfare_monthly_quota_uses_shanghai_month(db):
    u = _make_user(db, username="u8")  # welfare_expires_at=None → 公益服
    now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
    first_4am = now_sh.replace(day=1, hour=4, minute=0, second=0, microsecond=0)
    created = first_4am.astimezone(timezone.utc).replace(tzinfo=None)
    db.add(models.MediaRequest(user_id=u.id, tmdb_id="1", media_type="movie",
                               title="t", created_at=created))
    db.commit()
    quota = welfare_requests.get_monthly_quota(db, u)
    assert quota["used"] == 1


# ---------------- F7/F8: 管理后台 ----------------

def test_checkin_day_key_maps_to_shanghai_date():
    # 上海 10-18 00:00 的 UTC-naive 标记（10-17 16:00 UTC）→ "10-18"
    val = datetime(2026, 10, 17, 16, 0, 0)
    assert admin_economy._checkin_day_key(val) == "2026-10-18"
    # 脏数据防御：非 datetime 不抛异常
    assert admin_economy._checkin_day_key(None) == "None"


# ---------------- r43 P3-2：趋势图上海自然日口径 ----------------

def test_trend_checkin_0030_cst_lands_on_shanghai_label(db, monkeypatch):
    """00:30 CST 的签到/播放必须落在当天上海 label 上，不能在图上消失。

    时钟冻结在上海 00:30：真实 now 下若落在 00:00–08:00 窗口外，旧代码
    也是绿的（绿对绿），冻结后才构成真正的回归守卫。签到走已验证正确的
    _checkin_day_key；播放走 _series（r43 P2 修复），冻结下 revert 即变红。
    """
    # 把趋势函数看到的 now 钉在上海时间 00:30（只影响 admin_economy 模块内
    # 的 datetime.now；timedelta 等不受影响）
    frozen_sh = datetime(2026, 10, 18, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    frozen_utc_naive = frozen_sh.astimezone(timezone.utc).replace(tzinfo=None)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_sh if tz is not None else frozen_utc_naive

    monkeypatch.setattr(admin_economy, "datetime", _FrozenDateTime)

    u = _make_user(db, username="u9")
    # 00:30 CST 的签到：入库标记是「上海自然日 00:00」的 UTC-naive（r34 F1 口径）
    marker = (frozen_sh.replace(hour=0, minute=0, second=0, microsecond=0)
              .astimezone(timezone.utc).replace(tzinfo=None))
    db.add(models.CheckinRecord(user_id=u.id, checkin_date=marker,
                                points_awarded=0))
    # 00:30 CST 的播放：入库时间戳是 UTC-naive（事件真实时间）
    db.add(em_models.PlaybackSession(user_id=u.id, start_time=frozen_utc_naive))
    db.commit()
    resp = admin_economy.get_stats_trend(days=7, current_admin=None, db=db)
    sh_today = "2026-10-18"
    labels = [p["date"] for p in resp["series"]]
    assert labels[-1] == sh_today, f"label 轴应以今天（上海）结尾，实际 {labels}"
    today_row = resp["series"][-1]
    assert today_row["checkins"] == 1, f"00:30 CST 签到在图上消失了：{today_row}"
    assert today_row["plays"] == 1, f"00:30 CST 播放被算到前一天 label：{today_row}"

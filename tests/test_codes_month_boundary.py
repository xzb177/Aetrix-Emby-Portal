# -*- coding: utf-8 -*-
"""资格码月配额计数月界口径回归：Asia/Shanghai 自然月（UTC 入库帧对齐）。

bug：codes.welfare_codes_created_this_month 此前用服务器本地
datetime.now() 取月界——生产容器时钟 UTC，每月 1 日要到北京时间
08:00 才重置配额，晚 8 小时。全站口径（r34 收口）为上海自然月：
economy._month_start_utc_naive 把上海月界换算成 UTC-naive，与
RegistrationCode.created_at 的入库帧（default=datetime.now，
生产 UTC-naive）比较。

冻结手法沿用 r34 的 _FrozenDateTime 模式：同时把 codes 与 economy
两个模块的 datetime 钉在同一瞬间——旧实现读 codes.datetime、
新实现经 economy helper 读 economy.datetime——保证修前修后用
同一断言口径验证（patch 模块属性，不 patch 实例）。
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import codes, models
from backend.api import economy

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _make_user(db, username):
    u = models.WebUser(username=username, password_hash="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _stored(sh_dt):
    """上海 aware 时刻 → RegistrationCode.created_at 入库帧（UTC-naive）。"""
    return sh_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _make_code(db, code_str, admin_id, sh_dt, reward_type=codes.REWARD_WELFARE):
    c = models.RegistrationCode(
        code=code_str,
        code_type=codes.CODE_TYPE_RENEW,
        max_uses=1,
        use_count=0,
        is_active=True,
        days=30,
        reward_type=reward_type,
        created_by=admin_id,
        created_at=_stored(sh_dt),
    )
    db.add(c)
    db.commit()
    return c


def _freeze_now(monkeypatch, frozen_sh):
    frozen_utc_naive = frozen_sh.astimezone(timezone.utc).replace(tzinfo=None)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_sh if tz is not None else frozen_utc_naive

    monkeypatch.setattr(codes, "datetime", _FrozenDateTime)
    monkeypatch.setattr(economy, "datetime", _FrozenDateTime)


def test_quota_count_resets_at_shanghai_month_start(db, monkeypatch):
    """上海 11-01 00:30：10-31 晚（上海）生成的码已属上月，不占 11 月配额。

    旧实现按 UTC 月界（10-01 00:00 UTC-naive）数，10-31 20:00 CST
    （入库 12:00 UTC）会被误计入 → 本断言修前红、修后绿。
    """
    admin = _make_user(db, "admin_a")
    other = _make_user(db, "admin_b")
    _freeze_now(monkeypatch, datetime(2026, 11, 1, 0, 30, tzinfo=SH))
    # 上月（上海 10-31 20:00）
    _make_code(db, "WELFARE-PREV", admin.id,
               datetime(2026, 10, 31, 20, 0, tzinfo=SH))
    # 本月（上海 11-01 00:15）
    _make_code(db, "WELFARE-CUR", admin.id,
               datetime(2026, 11, 1, 0, 15, tzinfo=SH))
    # 对照：非资格码不计、他人码不计
    _make_code(db, "OTHER-TYPE", admin.id,
               datetime(2026, 11, 1, 0, 20, tzinfo=SH), reward_type="points")
    _make_code(db, "OTHER-ADMIN", other.id,
               datetime(2026, 11, 1, 0, 20, tzinfo=SH))
    assert codes.welfare_codes_created_this_month(db, admin.id) == 1


def test_quota_count_boundary_exact_instant(db, monkeypatch):
    """上海 11-01 00:00:00 整：月界入库帧 = 10-31 16:00:00 UTC。

    恰在月界生成的码计入（>=），月界前 1 秒的不计。
    旧实现月界为 10-01，两张都计 → 修前红、修后绿。
    """
    admin = _make_user(db, "admin_c")
    _freeze_now(monkeypatch, datetime(2026, 11, 1, 0, 0, 0, tzinfo=SH))
    _make_code(db, "EDGE-IN", admin.id,
               datetime(2026, 11, 1, 0, 0, 0, tzinfo=SH))
    _make_code(db, "EDGE-OUT", admin.id,
               datetime(2026, 10, 31, 23, 59, 59, tzinfo=SH))
    assert codes.welfare_codes_created_this_month(db, admin.id) == 1


def test_quota_count_still_october_late_on_31st_shanghai(db, monkeypatch):
    """上海 10-31 23:30 仍是 10 月：当天码计入、9 月码不计（月界另一侧守卫）。"""
    admin = _make_user(db, "admin_d")
    _freeze_now(monkeypatch, datetime(2026, 10, 31, 23, 30, tzinfo=SH))
    _make_code(db, "OCT-LATE", admin.id,
               datetime(2026, 10, 31, 20, 0, tzinfo=SH))
    _make_code(db, "SEP-LATE", admin.id,
               datetime(2026, 9, 30, 23, 0, tzinfo=SH))
    assert codes.welfare_codes_created_this_month(db, admin.id) == 1

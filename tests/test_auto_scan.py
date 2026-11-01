"""定时扫描配置：时间校验 / 读写 / 到点判定"""
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, "/opt/aetrix-portal")

from backend.emby_server import auto_scan
from backend import models as base_models


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_parse_time_ok():
    assert auto_scan.parse_time("03:00") == "03:00"
    assert auto_scan.parse_time(" 23:59 ") == "23:59"
    assert auto_scan.parse_time("3:00") is None      # 必须两位
    assert auto_scan.parse_time("24:00") is None
    assert auto_scan.parse_time("03:60") is None
    assert auto_scan.parse_time("") is None
    assert auto_scan.parse_time(None) is None
    assert auto_scan.parse_time("abc") is None


def test_config_defaults(db):
    # 2026-10-21 A 类摘面板现值对齐：enabled 缺省翻为开（生产现值）
    cfg = auto_scan.get_config(db)
    assert cfg == {"enabled": True, "time": "03:00", "last_run": ""}


def test_save_and_get(db):
    cfg = auto_scan.save_config(db, True, "04:30")
    assert cfg["enabled"] is True
    assert cfg["time"] == "04:30"
    cfg2 = auto_scan.get_config(db)
    assert cfg2["enabled"] is True and cfg2["time"] == "04:30"


def test_save_bad_time_raises(db):
    with pytest.raises(ValueError):
        auto_scan.save_config(db, True, "25:00")
    with pytest.raises(ValueError):
        auto_scan.save_config(db, True, "not-a-time")


def test_should_run_today():
    now = datetime(2026, 9, 26, 3, 0)
    assert auto_scan._should_run_today(now, "03:00", "") is True
    assert auto_scan._should_run_today(now, "03:00", "2026-09-26") is False  # 今天跑过
    assert auto_scan._should_run_today(now, "04:00", "") is False              # 时间不对
    assert auto_scan._should_run_today(now, "03:00", "2026-09-25") is True     # 昨天跑过


def test_should_run_today_window_catchup():
    # PA-007：tick 落在目标分钟之后（相位漂移/重启延迟），当天仍应触发
    assert auto_scan._should_run_today(datetime(2026, 9, 26, 3, 1, 30), "03:00", "") is True
    assert auto_scan._should_run_today(datetime(2026, 9, 26, 3, 59), "03:00", "") is True
    assert auto_scan._should_run_today(datetime(2026, 9, 26, 23, 59), "03:00", "") is True
    # 未到点不触发
    assert auto_scan._should_run_today(datetime(2026, 9, 26, 2, 59, 59), "03:00", "") is False
    # 当天已跑过不重复
    assert auto_scan._should_run_today(datetime(2026, 9, 26, 3, 30), "03:00", "2026-09-26") is False
    # 次日未到点不触发，到点触发
    assert auto_scan._should_run_today(datetime(2026, 9, 27, 0, 30), "03:00", "2026-09-26") is False
    assert auto_scan._should_run_today(datetime(2026, 9, 27, 3, 0, 5), "03:00", "2026-09-26") is True


def test_restart_within_target_minute_catches_up():
    # PA-007：进程在目标分钟内重启，首个 tick 在 60 秒后（已过目标分钟），仍应补触发
    assert auto_scan._should_run_today(datetime(2026, 9, 26, 3, 1, 30), "03:00", "") is True


def test_busy_placeholder_blocks_same_day_next_day_triggers():
    # PA-007：忙跳过只记占位——当天后续 tick 不再触发，次日到点正常触发（现行语义保留）
    assert auto_scan._should_run_today(datetime(2026, 9, 26, 5, 0), "03:00", "2026-09-26") is False
    assert auto_scan._should_run_today(datetime(2026, 9, 27, 3, 0, 30), "03:00", "2026-09-26") is True


def _simulate_missed_days(period_seconds, days=60):
    """模拟 _loop：每 period 秒 tick 一次，返回 60 天内漏跑的日期（空表＝零漏跑）。"""
    from datetime import timedelta

    start = datetime(2026, 9, 26, 0, 0, 37)
    end = start + timedelta(days=days)
    last_run = ""
    hit_days = set()
    tick = start
    while tick < end:
        if auto_scan._should_run_today(tick, "03:00", last_run):
            last_run = tick.strftime("%Y-%m-%d")
            hit_days.add(last_run)
        tick += timedelta(seconds=period_seconds)
    all_days = {(start + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days)}
    return sorted(all_days - hit_days)


@pytest.mark.parametrize("period", [61, 62, 65])
def test_drift_simulation_no_missed_days(period):
    # PA-007：tick 周期 60 秒＋耗时导致相位漂移时，60 天零漏跑
    assert _simulate_missed_days(period) == []


def test_start_scheduler_idempotent():
    assert auto_scan.start_auto_scan_scheduler() in (True, False)
    # 第二次调用返回 False（已启动），不重复起线程
    assert auto_scan.start_auto_scan_scheduler() is False

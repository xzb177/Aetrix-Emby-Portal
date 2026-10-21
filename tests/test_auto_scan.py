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


def test_start_scheduler_idempotent():
    assert auto_scan.start_auto_scan_scheduler() in (True, False)
    # 第二次调用返回 False（已启动），不重复起线程
    assert auto_scan.start_auto_scan_scheduler() is False

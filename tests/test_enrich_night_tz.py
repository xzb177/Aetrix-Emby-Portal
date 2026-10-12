"""单集夜间窗口时区支持回归测试。

背景：`_in_night_window` 曾用 `datetime.now()`（服务器本地时间=UTC）判断窗口，
用户在后台设 02:27-04:27 期望北京时间，实际被按 UTC 理解，导致 5005 条单集
整晚被跳过。本文件钉住：

1. `_parse_tz`：合法时区名解析、非法回退默认、None 回退默认；
2. `_in_night_window`：同一 UTC 时刻在不同时区下判断结果不同（Asia/Shanghai
   比 UTC 快 8 小时），跨午夜窗口在时区转换后依然正确；
3. `_episode_night_config`：环境变量 ENRICH_NIGHT_TZ 生效、非法值回退默认、
   老配置无 tz 字段时回退默认（向后兼容）；
4. `save_episode_night_config`：tz 写库后 `get_episode_night_config` 能读回。

全部在隔离临时 SQLite 里跑，不连生产库；不测真实 TMDB 网络。
"""
import os
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import enrich_worker
from backend.integrations import store

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clear_night_cfg_cache():
    """每个测试前清掉夜间配置的 60 秒 TTL 缓存 + 环境变量，避免测试间污染"""
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None
    for k in (enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV,
              enrich_worker.ENRICH_NIGHT_START_ENV,
              enrich_worker.ENRICH_NIGHT_END_ENV,
              enrich_worker.ENRICH_NIGHT_TZ_ENV):
        os.environ.pop(k, None)
    yield
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None
    for k in (enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV,
              enrich_worker.ENRICH_NIGHT_START_ENV,
              enrich_worker.ENRICH_NIGHT_END_ENV,
              enrich_worker.ENRICH_NIGHT_TZ_ENV):
        os.environ.pop(k, None)


# ---------- _parse_tz ----------

def test_parse_tz_valid():
    z = enrich_worker._parse_tz("Asia/Shanghai")
    assert z.key == "Asia/Shanghai"
    assert enrich_worker._parse_tz("UTC").key == "UTC"
    # 前后空格容忍
    assert enrich_worker._parse_tz("  Europe/Paris  ").key == "Europe/Paris"


def test_parse_tz_invalid_falls_back_to_default():
    assert enrich_worker._parse_tz("Not/AZone").key == "Asia/Shanghai"
    assert enrich_worker._parse_tz("").key == "Asia/Shanghai"
    assert enrich_worker._parse_tz(None).key == "Asia/Shanghai"


def test_parse_tz_custom_default():
    assert enrich_worker._parse_tz("Bogus", default="UTC").key == "UTC"


def test_parse_tz_never_raises():
    # 各种脏输入都不抛异常
    for bad in ("", "  ", None, 123, "///", "Asia/"):
        enrich_worker._parse_tz(bad)


# ---------- _in_night_window 时区语义 ----------

def test_in_night_window_tz_shanghai_vs_utc():
    """同一 UTC 时刻：按 Asia/Shanghai 是凌晨 3 点（窗口内），按 UTC 是前一天 19 点（窗口外）。"""
    # 2026-10-11 19:00 UTC == 2026-10-12 03:00 Asia/Shanghai
    utc_dt = datetime(2026, 10, 11, 19, 0, tzinfo=timezone.utc)
    assert enrich_worker._in_night_window(utc_dt, "02:00", "06:00", "Asia/Shanghai") is True
    assert enrich_worker._in_night_window(utc_dt, "02:00", "06:00", "UTC") is False


def test_in_night_window_naive_treated_as_utc():
    """naive datetime 按 UTC 解释（与旧行为一致：服务器本地时间即 UTC）。"""
    naive = datetime(2026, 10, 11, 19, 0)  # naive
    assert enrich_worker._in_night_window(naive, "02:00", "06:00", "Asia/Shanghai") is True
    assert enrich_worker._in_night_window(naive, "02:00", "06:00", "UTC") is False


def test_in_night_window_default_tz_is_shanghai():
    """不传 tz 时默认 Asia/Shanghai（向后兼容：老调用方行为与北京时间一致）。"""
    utc_dt = datetime(2026, 10, 11, 19, 0, tzinfo=timezone.utc)
    assert enrich_worker._in_night_window(utc_dt, "02:00", "06:00") is True


def test_in_night_window_cross_midnight_with_tz():
    """跨午夜窗口在时区转换后依然正确：22:00-06:00 Asia/Shanghai。"""
    # 2026-10-11 15:00 UTC == 23:00 Shanghai（窗口内）
    assert enrich_worker._in_night_window(
        datetime(2026, 10, 11, 15, 0, tzinfo=timezone.utc),
        "22:00", "06:00", "Asia/Shanghai") is True
    # 2026-10-11 10:00 UTC == 18:00 Shanghai（窗口外）
    assert enrich_worker._in_night_window(
        datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc),
        "22:00", "06:00", "Asia/Shanghai") is False


def test_in_night_window_accepts_zoneinfo():
    z = ZoneInfo("America/New_York")
    # 2026-10-12 06:00 UTC == 02:00 EDT（窗口内）
    assert enrich_worker._in_night_window(
        datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc),
        "02:00", "06:00", z) is True


# ---------- _episode_night_config ----------

def test_config_tz_from_env(db):
    os.environ[enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV] = "1"
    os.environ[enrich_worker.ENRICH_NIGHT_TZ_ENV] = "America/New_York"
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["from_env"] is True
    assert cfg["night_tz"] == "America/New_York"


def test_config_tz_invalid_env_falls_back(db):
    os.environ[enrich_worker.ENRICH_NIGHT_TZ_ENV] = "Nope/Nowhere"
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["night_tz"] == "Asia/Shanghai"


def test_config_tz_from_db(db):
    store.write_values(db, {enrich_worker.CFG_NIGHT_TZ: "Europe/London"},
                       {enrich_worker.CFG_NIGHT_TZ: "test"})
    db.commit()
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["from_env"] is False
    assert cfg["night_tz"] == "Europe/London"


def test_config_tz_missing_in_db_falls_back(db):
    """老配置无 tz 字段：回退默认 Asia/Shanghai（向后兼容）。"""
    # 先确保库里没有 tz 残留（其它测试可能写过）
    store.write_values(db, {enrich_worker.CFG_NIGHT_TZ: ""},
                       {enrich_worker.CFG_NIGHT_TZ: "test"})
    db.commit()
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["night_tz"] == "Asia/Shanghai"


# ---------- save / get roundtrip ----------

def test_save_and_get_tz_roundtrip(db):
    enrich_worker.save_episode_night_config(
        db, True, "02:27", "04:27", "Asia/Shanghai")
    cfg = enrich_worker.get_episode_night_config(db)
    assert cfg["enabled"] is True
    assert cfg["night_start"] == "02:27"
    assert cfg["night_end"] == "04:27"
    assert cfg["night_tz"] == "Asia/Shanghai"


def test_save_tz_invalid_falls_back(db):
    enrich_worker.save_episode_night_config(
        db, True, "02:00", "06:00", "Bad/Zone")
    cfg = enrich_worker.get_episode_night_config(db)
    assert cfg["night_tz"] == "Asia/Shanghai"
    # in_window 字段存在且为 bool（抢单路径依赖）
    assert isinstance(cfg["in_window"], bool)

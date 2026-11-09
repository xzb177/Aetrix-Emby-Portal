# -*- coding: utf-8 -*-
"""指纹沉淀「晚高峰静默窗」回归测试（Drive API 降量批，2026-11-08）。

口径：
- 静默窗按 Asia/Shanghai 小时配，默认 19:00–23:00（起含、止不含）；
- 起止走 SystemConfig 键 transfer_sediment_quiet_start_hour /
  transfer_sediment_quiet_end_hour，支持跨午夜（起 > 止）；
- 起 == 止 = 静默窗关闭，行为与没有这道门时一致；
- 窗内 run_once 整轮跳过（skipped_reason="quiet_hours"），零读源；
  已在跑的轮次不受影响（门只在轮次入口判）。
- run_once 的 now 口径沿用全仓约定：aware 直接换算到上海时间，
  naive 按 UTC 理解再换算（enrich_worker._in_night_window 同口径）。

全部离线：Drive SA 客户端用桩替身，与 test_transfer_sediment 同款。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_sediment as ts
from backend.integrations import store

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    store.invalidate()
    session.add(em.StorageMount(id=3, name="sediment-src",
                                path="drive:src-drive-1"))
    session.commit()
    yield session
    session.close()
    store.invalidate()


def _set_config(db, key, value):
    row = (db.query(base_models.SystemConfig)
           .filter(base_models.SystemConfig.key == key).first())
    if row is None:
        db.add(base_models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()
    store.invalidate()


_counter = [7000]


def _item(db, payload_len=1024):
    _counter[0] += 1
    n = _counter[0]
    row = em.MediaItem(guid=f"sedq-{n}", library_id=1, item_type="movie",
                       name=f"静默窗测试片{n}",
                       drive_file_id=f"fid-q-{n}",
                       file_path=f"mount://3/电影/sedq{n}.mkv",
                       size=payload_len)
    db.add(row)
    db.commit()
    return row


def _cross_job(db, item, status="pending"):
    job = em.TransferJob(
        item_id=item.id, trigger="playback", provider="drive",
        owner_scope="user", owner_ref="42",
        dest_drive_id="pan115:user-42", drive_id="src-drive-1",
        status=status, attempts=0)
    db.add(job)
    db.commit()
    return job


class FakeDriveApi:
    def __init__(self, payloads):
        self.payloads = payloads
        self.range_calls = 0
        self.bytes_served = 0

    def get_file(self, drive_id, fid):
        spec = self.payloads[fid]
        return {"size": spec[0] if isinstance(spec, tuple) else len(spec)}

    def download_range(self, drive_id, fid, start, end):
        self.range_calls += 1
        spec = self.payloads[fid]
        if isinstance(spec, tuple):
            data = bytes([spec[1]]) * (end - start + 1)
        else:
            data = spec[start:end + 1]
        assert len(data) == end - start + 1
        self.bytes_served += len(data)
        return data


def _sh(hour, minute=0):
    return datetime(2026, 11, 8, hour, minute, tzinfo=SH)


def _demand_setup(db):
    payload = b"q" * 2048
    item = _item(db, payload_len=len(payload))
    _cross_job(db, item)
    return item, FakeDriveApi({item.drive_file_id: payload})


# ---------- 窗口判定（纯函数口径） ----------

def test_quiet_defaults(db):
    assert ts.quiet_start_hour(db) == 19
    assert ts.quiet_end_hour(db) == 23


def test_quiet_window_default_boundaries(db):
    assert ts.in_quiet_hours(db, _sh(18, 59)) is False
    assert ts.in_quiet_hours(db, _sh(19, 0)) is True
    assert ts.in_quiet_hours(db, _sh(22, 59)) is True
    assert ts.in_quiet_hours(db, _sh(23, 0)) is False
    assert ts.in_quiet_hours(db, _sh(12, 0)) is False


def test_quiet_window_naive_now_is_utc(db):
    # naive 按 UTC 理解：UTC 12:30 = 上海 20:30（窗内），
    # UTC 02:00 = 上海 10:00（窗外）。
    assert ts.in_quiet_hours(db, datetime(2026, 11, 8, 12, 30)) is True
    assert ts.in_quiet_hours(db, datetime(2026, 11, 8, 2, 0)) is False


def test_quiet_window_cross_midnight(db):
    _set_config(db, ts.KEY_QUIET_START_HOUR, 22)
    _set_config(db, ts.KEY_QUIET_END_HOUR, 6)
    assert ts.in_quiet_hours(db, _sh(21)) is False
    assert ts.in_quiet_hours(db, _sh(22)) is True
    assert ts.in_quiet_hours(db, _sh(23)) is True
    assert ts.in_quiet_hours(db, _sh(0)) is True
    assert ts.in_quiet_hours(db, _sh(5)) is True
    assert ts.in_quiet_hours(db, _sh(6)) is False
    assert ts.in_quiet_hours(db, _sh(12)) is False


def test_quiet_window_disabled_when_start_equals_end(db):
    _set_config(db, ts.KEY_QUIET_START_HOUR, 19)
    _set_config(db, ts.KEY_QUIET_END_HOUR, 19)
    assert ts.in_quiet_hours(db, _sh(20)) is False
    _set_config(db, ts.KEY_QUIET_START_HOUR, 0)
    _set_config(db, ts.KEY_QUIET_END_HOUR, 0)
    assert ts.in_quiet_hours(db, _sh(0)) is False


def test_quiet_hours_out_of_range_config_falls_back(db):
    _set_config(db, ts.KEY_QUIET_START_HOUR, 99)
    _set_config(db, ts.KEY_QUIET_END_HOUR, -3)
    assert ts.quiet_start_hour(db) == 19
    assert ts.quiet_end_hour(db) == 23


# ---------- 轮次入口 ----------

def test_run_once_skips_whole_round_in_quiet_hours(db):
    item, api = _demand_setup(db)
    # naive UTC 12:30 = 上海 20:30，落在默认窗 19:00–23:00 内。
    stats = ts.run_once(db, drive_api=api, now=datetime(2026, 11, 8, 12, 30))
    assert stats["skipped_reason"] == "quiet_hours"
    assert stats["selected"] == 0
    assert stats["registered"] == 0
    assert api.range_calls == 0


def test_run_once_runs_outside_quiet_hours(db):
    item, api = _demand_setup(db)
    # naive UTC 02:00 = 上海 10:00，窗外，正常沉淀。
    stats = ts.run_once(db, drive_api=api, now=datetime(2026, 11, 8, 2, 0))
    assert stats["skipped_reason"] is None
    assert stats["registered"] == 1
    assert api.range_calls > 0


def test_run_once_cross_midnight_window_skips(db):
    _set_config(db, ts.KEY_QUIET_START_HOUR, 22)
    _set_config(db, ts.KEY_QUIET_END_HOUR, 6)
    item, api = _demand_setup(db)
    # 上海 23:00（naive UTC 15:00）与上海 05:30（naive UTC 21:30）
    # 都在跨午夜窗内。
    stats = ts.run_once(db, drive_api=api, now=datetime(2026, 11, 8, 15, 0))
    assert stats["skipped_reason"] == "quiet_hours"
    stats = ts.run_once(db, drive_api=api, now=datetime(2026, 11, 8, 21, 30))
    assert stats["skipped_reason"] == "quiet_hours"
    assert api.range_calls == 0


def test_run_once_quiet_window_disabled_runs_in_peak(db):
    _set_config(db, ts.KEY_QUIET_START_HOUR, 19)
    _set_config(db, ts.KEY_QUIET_END_HOUR, 19)
    item, api = _demand_setup(db)
    stats = ts.run_once(db, drive_api=api, now=datetime(2026, 11, 8, 12, 30))
    assert stats["skipped_reason"] is None
    assert stats["registered"] == 1


# ---------- 配置回读/写入（四齐） ----------

def test_quiet_hours_in_config_payload_and_write(db):
    payload = ts.sediment_config_payload(db)
    assert payload[ts.KEY_QUIET_START_HOUR] == 19
    assert payload[ts.KEY_QUIET_END_HOUR] == 23
    out = ts.write_sediment_config(
        db, transfer_sediment_quiet_start_hour=21,
        transfer_sediment_quiet_end_hour=7)
    assert out[ts.KEY_QUIET_START_HOUR] == 21
    assert out[ts.KEY_QUIET_END_HOUR] == 7
    assert ts.quiet_start_hour(db) == 21
    assert ts.quiet_end_hour(db) == 7

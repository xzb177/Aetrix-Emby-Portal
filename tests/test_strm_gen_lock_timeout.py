"""Tests for .strm 生成器运行锁超时自愈（B1，2026-10-13）。

覆盖：
- 正常：获取 → 二次获取拒绝 → 释放 → 可再获取
- 僵死：持有超阈值 → 新轮次抢占成功，acquired_at 刷新
- 关闭：timeout=0 时僵死也不抢占（保持旧行为）
- _lock_stale：调度器"只看不抢"判定口径
- 并发：两个抢占者只有一个成功
- lock_timeout_minutes：默认值/钳制/非法值回退

纯内存，不碰网络/DB（_db_config 用 monkeypatch 隔离）。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg


@pytest.fixture(autouse=True)
def _clean_lock_state():
    with sg._lock:
        sg._progress["running"] = False
        sg._progress["acquired_at"] = None
        sg._progress["phase"] = "idle"
    old_cache = sg._lock_timeout_min_cache
    sg._lock_timeout_min_cache = sg.DEFAULT_LOCK_TIMEOUT_MIN
    yield
    with sg._lock:
        sg._progress["running"] = False
        sg._progress["acquired_at"] = None
    sg._lock_timeout_min_cache = old_cache


def _force_stale(minutes_ago: float = 200.0):
    """模拟僵死持有者：running=True，但获取时刻是 N 分钟前。"""
    with sg._lock:
        sg._progress["running"] = True
        sg._progress["acquired_at"] = time.monotonic() - minutes_ago * 60


class TestNormalAcquire:
    def test_acquire_release_reacquire(self):
        assert sg.try_acquire() is True
        assert sg.try_acquire() is False  # 二次获取拒绝
        sg._release_progress({}, db=None)
        assert sg.try_acquire() is True

    def test_release_clears_acquired_at(self):
        assert sg.try_acquire() is True
        assert sg._progress["acquired_at"] is not None
        sg._release_progress({}, db=None)
        assert sg._progress["acquired_at"] is None
        assert sg._progress["running"] is False

    def test_release_progress_db_none_ok(self):
        # _release_progress 的 db 参数用于落盘；None 时只做内存释放，不抛异常
        assert sg.try_acquire() is True
        sg._release_progress({"generated": 1}, db=None)
        assert sg._progress["running"] is False


class TestStaleSteal:
    def test_stale_lock_can_be_stolen(self):
        _force_stale(minutes_ago=200.0)  # 默认阈值 180 分钟
        assert sg.try_acquire() is True
        # 抢占后 acquired_at 刷新为现在
        with sg._lock:
            age = time.monotonic() - sg._progress["acquired_at"]
        assert age < 60

    def test_fresh_lock_not_stolen(self):
        assert sg.try_acquire() is True  # 刚获取，不僵死
        assert sg.try_acquire() is False

    def test_just_under_threshold_not_stolen(self):
        _force_stale(minutes_ago=179.0)
        assert sg.try_acquire() is False

    def test_stale_without_acquired_at_not_stolen(self):
        # 老版本/异常路径：running=True 但没记时间 → 保守不抢占
        with sg._lock:
            sg._progress["running"] = True
            sg._progress["acquired_at"] = None
        assert sg.try_acquire() is False


class TestDisabled:
    def test_timeout_zero_disables_steal(self):
        sg._lock_timeout_min_cache = 0
        _force_stale(minutes_ago=60 * 24)  # 持有 1 天也不抢
        assert sg.try_acquire() is False

    def test_stale_check_zero_timeout(self):
        sg._lock_timeout_min_cache = 0
        _force_stale(minutes_ago=60 * 24)
        assert sg._lock_stale() is False


class TestLockStaleHelper:
    def test_not_running_not_stale(self):
        assert sg._lock_stale() is False

    def test_running_fresh_not_stale(self):
        assert sg.try_acquire() is True
        assert sg._lock_stale() is False

    def test_running_stale(self):
        _force_stale(minutes_ago=200.0)
        assert sg._lock_stale() is True


class TestConcurrentSteal:
    def test_only_one_winner(self):
        _force_stale(minutes_ago=200.0)
        results = []
        barrier = threading.Barrier(8)

        def _try():
            barrier.wait()
            results.append(sg.try_acquire())

        threads = [threading.Thread(target=_try) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1
        assert results.count(False) == 7


class TestTokenGuard:
    def test_stolen_release_does_not_clear_new_holder(self):
        # A 获取锁，B 抢占，A 结束时用旧 token 释放 → B 的 running 不受影响
        assert sg.try_acquire() is True
        with sg._lock:
            token_a = sg._progress["lock_token"]
        _force_stale(minutes_ago=200.0)
        assert sg.try_acquire() is True  # B 抢占
        with sg._lock:
            token_b = sg._progress["lock_token"]
        assert token_a != token_b
        sg._release_progress({}, db=None, expect_token=token_a)
        with sg._lock:
            assert sg._progress["running"] is True  # B 还在跑
            assert sg._progress["lock_token"] == token_b
        # B 正常结束：token 对得上，释放成功
        sg._release_progress({}, db=None, expect_token=token_b)
        assert sg._progress["running"] is False

    def test_matching_token_releases(self):
        assert sg.try_acquire() is True
        with sg._lock:
            token = sg._progress["lock_token"]
        assert token  # 每次获取换新 token
        sg._release_progress({}, db=None, expect_token=token)
        assert sg._progress["running"] is False
        assert sg._progress["lock_token"] is None

    def test_token_refreshes_each_acquire(self):
        assert sg.try_acquire() is True
        with sg._lock:
            t1 = sg._progress["lock_token"]
        sg._release_progress({}, db=None)
        assert sg.try_acquire() is True
        with sg._lock:
            t2 = sg._progress["lock_token"]
        assert t1 != t2


class TestTimeoutConfig:
    def test_default(self, monkeypatch):
        monkeypatch.setattr(sg, "_db_config", lambda db, key, default="": default)
        assert sg.lock_timeout_minutes(db=object()) == 180

    def test_clamp(self, monkeypatch):
        monkeypatch.setattr(sg, "_db_config", lambda db, key, default="": "99999")
        assert sg.lock_timeout_minutes(db=object()) == 1440

    def test_invalid_fallback(self, monkeypatch):
        monkeypatch.setattr(sg, "_db_config", lambda db, key, default="": "abc")
        assert sg.lock_timeout_minutes(db=object()) == 180

    def test_zero_allowed(self, monkeypatch):
        monkeypatch.setattr(sg, "_db_config", lambda db, key, default="": "0")
        assert sg.lock_timeout_minutes(db=object()) == 0

    def test_cache_fallback_without_db(self):
        # 无 db 时返回上次热读缓存
        sg._lock_timeout_min_cache = 42
        assert sg.lock_timeout_minutes() == 42
        assert sg.lock_timeout_minutes(db=None) == 42


def _memdb_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend import models
    from backend.integrations import store
    store.invalidate()
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestAdminKnob:
    """管理后台 lock_timeout_minutes 旋钮：读写 + 校验。"""

    def _body(self, **kw):
        from backend.api import admin_strm_gen as ag
        base = dict(enabled=True, source_dir="MoviePilot/", drive_id="",
                    prune=False, interval_minutes=60, source_type="drive",
                    local_root="/media", drive_concurrency=3, api_qps=8.0)
        base.update(kw)
        return ag.StrmGenConfigSave(**base)

    def test_save_and_read(self):
        from backend.api import admin_strm_gen as ag
        db = _memdb_session()
        try:
            ag.strm_gen_config_save(self._body(lock_timeout_minutes=120), db)
            assert sg.lock_timeout_minutes(db) == 120
            cfg = ag._read_config(db)
            assert cfg["lock_timeout_minutes"] == 120
        finally:
            db.close()

    def test_none_means_no_change(self):
        from backend.api import admin_strm_gen as ag
        from backend.integrations import store
        db = _memdb_session()
        try:
            ag.strm_gen_config_save(self._body(lock_timeout_minutes=90), db)
            ag.strm_gen_config_save(self._body(lock_timeout_minutes=None), db)
            assert sg.lock_timeout_minutes(db) == 90
            assert store.get_value(db, sg.CONFIG_LOCK_TIMEOUT_MIN, "") == "90"
        finally:
            db.close()

    def test_zero_disables(self):
        from backend.api import admin_strm_gen as ag
        db = _memdb_session()
        try:
            ag.strm_gen_config_save(self._body(lock_timeout_minutes=0), db)
            assert sg.lock_timeout_minutes(db) == 0
        finally:
            db.close()

    def test_validation(self):
        from fastapi import HTTPException
        from backend.api import admin_strm_gen as ag
        db = _memdb_session()
        try:
            with pytest.raises(HTTPException):
                ag.strm_gen_config_save(self._body(lock_timeout_minutes=-1), db)
            with pytest.raises(HTTPException):
                ag.strm_gen_config_save(self._body(lock_timeout_minutes=1441), db)
        finally:
            db.close()

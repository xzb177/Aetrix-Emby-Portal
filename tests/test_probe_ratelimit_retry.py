# -*- coding: utf-8 -*-
"""429/5xx 长退避 + 401 有限重试 + 解析阶段错误透出（P1 probe 修复）。

背景：HTTP 429/5xx 和"解析地址失败"这类临时性错误以前走普通重试，
3 次后被永久判死；401 一次就判死。本文件钉住修复后的行为：

- 429/5xx → 24h 长退避，永不判死（仿 C-P1-1 的 quota 处理）
- 401 → PROBE_AUTH_MAX_ATTEMPTS(2) 次带退避重试，不再一次判死
- 解析阶段异常透出原始错误类型，限流类同样长退避
- 普通重试退避带 ±20% 抖动（反风控，绝不固定间隔）
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime
from unittest import mock

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import media_probe, probe_worker
from backend.emby_server import mounts as mount_lib

init_db()


def _guid():
    return uuid.uuid4().hex


def _make_lib(db, name=None):
    lib = em.Library(guid=_guid(), name=name or f"rl_{_guid()[:8]}",
                     collection_type="movies", paths="/tmp/rl_test")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _make_item(db, lib, probe_status="probing", probe_attempts=0):
    item = em.MediaItem(
        guid=_guid(), library_id=lib.id, name=f"item_{_guid()[:8]}",
        item_type="movie", file_path="/tmp/rl_test/a.mkv",
        video_codec=None, probe_status=probe_status,
        probe_attempts=probe_attempts, probe_priority=0,
        date_added=datetime.now(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_mount(db):
    m = em.StorageMount(name=f"m_{_guid()[:8]}")
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _cleanup(db, lib):
    _cleanup_by_id(db, lib.id)


def _cleanup_by_id(db, lib_id):
    ids = [r[0] for r in db.query(em.MediaItem.id)
           .filter(em.MediaItem.library_id == lib_id).all()]
    if ids:
        db.query(em.MediaStream).filter(em.MediaStream.item_id.in_(ids)) \
            .delete(synchronize_session=False)
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib_id) \
        .delete(synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib_id).delete()
    db.commit()


class TestClassifyResolveError:
    def test_429_is_ratelimit(self):
        assert media_probe.classify_resolve_error(
            mount_lib.MountError("读取失败: HTTP 429")) == "ratelimit"

    def test_503_is_server(self):
        assert media_probe.classify_resolve_error(
            RuntimeError("HTTP 503 Service Unavailable")) == "server"

    def test_500_is_server(self):
        assert media_probe.classify_resolve_error(
            RuntimeError("HTTP 500")) == "server"

    def test_403_is_quota(self):
        assert media_probe.classify_resolve_error(
            RuntimeError("HTTP 403")) == "quota"

    def test_401_is_auth(self):
        assert media_probe.classify_resolve_error(
            RuntimeError("HTTP 401")) == "auth"

    def test_404_is_not_found(self):
        assert media_probe.classify_resolve_error(
            RuntimeError("HTTP 404")) == "not_found"

    def test_unknown_is_none(self):
        assert media_probe.classify_resolve_error(
            RuntimeError("boom")) is None


class TestProbeOneRatelimit:
    """probe_one：429/5xx 走长退避，401 有限重试。"""

    def _run(self, db, item, fake_probe):
        with mock.patch.object(media_probe, "probe_metadata",
                               return_value=fake_probe), \
             mock.patch.object(media_probe, "resolve_probe_input",
                               return_value=(("/tmp/x.mkv", {}, 0, ""), None)):
            return media_probe.probe_one(db, item)

    def test_429_goes_long_retry(self):
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            item = _make_item(db, lib)
            status = self._run(db, item, {"_http_code": 429, "_error": "ratelimit",
                                          "_error_detail": "限流"})
            db.refresh(item)
            assert status == "pending"
            assert item.probe_status == "pending"
            assert item.probe_attempts == 0  # 长退避不计次数
            delta = (item.probe_next_retry_at - datetime.now()).total_seconds()
            assert 16 * 3600 <= delta <= 32 * 3600, delta  # 约24h
            assert "[ratelimit]" in (item.probe_last_error or "")
        finally:
            _cleanup(db, lib)
            db.close()

    def test_429_without_error_flag_still_long_retry(self):
        # 防御性：即使 _error 缺失，429 本身也按限流处理（不判死）
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            item = _make_item(db, lib)
            status = self._run(db, item, {"_http_code": 429})
            db.refresh(item)
            assert status == "pending"
            assert item.probe_status == "pending"
            assert item.probe_attempts == 0
        finally:
            _cleanup(db, lib)
            db.close()

    def test_503_goes_long_retry(self):
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            item = _make_item(db, lib)
            status = self._run(db, item, {"_http_code": 503, "_error": "server_error",
                                          "_error_detail": "服务异常"})
            db.refresh(item)
            assert status == "pending"
            assert item.probe_status == "pending"
            assert item.probe_attempts == 0
            assert "[server]" in (item.probe_last_error or "")
        finally:
            _cleanup(db, lib)
            db.close()

    def test_500_without_error_flag_still_long_retry(self):
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            item = _make_item(db, lib)
            status = self._run(db, item, {"_http_code": 500})
            db.refresh(item)
            assert status == "pending"
            assert item.probe_status == "pending"
        finally:
            _cleanup(db, lib)
            db.close()

    def test_401_retries_then_fails(self):
        # 401：第一次 → pending（attempts=1）；第二次 → failed（达到上限）
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            item = _make_item(db, lib)
            fake = {"_http_code": 401, "_error": "auth"}
            status = self._run(db, item, fake)
            db.refresh(item)
            assert status == "pending"
            assert item.probe_status == "pending"
            assert item.probe_attempts == 1

            status = self._run(db, item, fake)
            db.refresh(item)
            assert status == "failed"
            assert item.probe_status == "failed"
        finally:
            _cleanup(db, lib)
            db.close()

    def test_404_still_fails_fast(self):
        # 回归：404 依然直接判死
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            item = _make_item(db, lib)
            status = self._run(db, item, {"_http_code": 404, "_error": "not_found"})
            db.refresh(item)
            assert status == "failed"
            assert item.probe_status == "failed"
            assert item.probe_attempts == 0
        finally:
            _cleanup(db, lib)
            db.close()


class TestResolveStageErrorKind:
    """解析阶段：异常透出 kind，限流类走长退避。"""

    def _fake_provider(self, exc):
        provider = mock.Mock()
        provider.resolve_final.side_effect = exc
        return provider

    def test_resolve_429_returns_ratelimit_kind(self):
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            m = _make_mount(db)
            item = _make_item(db, lib)
            item.file_path = f"mount://{m.id}/a.mkv"
            db.commit()
            with mock.patch.object(mount_lib, "build_provider",
                                   return_value=self._fake_provider(
                                       mount_lib.MountError("直链失败: HTTP 429"))):
                resolved, kind = media_probe.resolve_probe_input(db, item)
            assert resolved is None
            assert kind == "ratelimit"
        finally:
            db.query(em.StorageMount).filter(em.StorageMount.id == m.id).delete()
            db.commit()
            _cleanup(db, lib)
            db.close()

    def test_resolve_429_leads_to_long_retry(self):
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            m = _make_mount(db)
            item = _make_item(db, lib)
            item.file_path = f"mount://{m.id}/a.mkv"
            db.commit()
            with mock.patch.object(mount_lib, "build_provider",
                                   return_value=self._fake_provider(
                                       mount_lib.MountError("直链失败: HTTP 429"))):
                status = media_probe.probe_one(db, item)
            db.refresh(item)
            assert status == "pending"
            assert item.probe_status == "pending"
            assert item.probe_attempts == 0
            assert "[ratelimit]" in (item.probe_last_error or "")
        finally:
            db.query(em.StorageMount).filter(em.StorageMount.id == m.id).delete()
            db.commit()
            _cleanup(db, lib)
            db.close()

    def test_resolve_unknown_error_still_normal_retry(self):
        # 回归：未知解析错误仍走普通重试
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            m = _make_mount(db)
            item = _make_item(db, lib)
            item.file_path = f"mount://{m.id}/a.mkv"
            db.commit()
            with mock.patch.object(mount_lib, "build_provider",
                                   return_value=self._fake_provider(
                                       RuntimeError("boom"))):
                status = media_probe.probe_one(db, item)
            db.refresh(item)
            assert status == "pending"
            assert item.probe_status == "pending"
            assert item.probe_attempts == 1
        finally:
            db.query(em.StorageMount).filter(em.StorageMount.id == m.id).delete()
            db.commit()
            _cleanup(db, lib)
            db.close()


class TestRetryJitter:
    """普通重试退避带 ±20% 抖动（反风控）。"""

    def test_delay_within_jitter_band(self):
        db = SessionLocal()
        lib = _make_lib(db)
        try:
            item = _make_item(db, lib)
            delays = []
            for _ in range(30):
                item.probe_attempts = 0
                item.probe_status = "probing"
                db.commit()
                media_probe.mark_retry(db, item, "boom")
                db.refresh(item)
                delta = (item.probe_next_retry_at - datetime.now()).total_seconds()
                delays.append(delta)
            # 基数 60s × [0.8, 1.2] → [48, 72]，留 5s 执行余量
            assert all(43 <= d <= 77 for d in delays), delays
            # 不是固定值：抖动真的在生效
            assert len(set(int(d) for d in delays)) > 1
        finally:
            _cleanup(db, lib)
            db.close()


class TestProcessItemRatelimit:
    """process_item 端到端：429 → retry（长退避），不再 3 次判死。"""

    def test_process_item_429_is_retry_not_failed(self):
        db = SessionLocal()
        lib = _make_lib(db)
        lib_id = lib.id
        try:
            item = _make_item(db, lib, probe_status="probing")
            item.probe_claimed_at = datetime.now()
            item.file_path = "mount://8/m/x.mkv"
            db.commit()
            item_id = item.id
            db.close()

            with mock.patch.object(media_probe, "resolve_probe_input",
                                   return_value=(("http://x/a.mkv", {}, 1, "mkv"),
                                                 None)), \
                 mock.patch.object(media_probe, "probe_metadata",
                                   return_value={"_http_code": 429,
                                                 "_error": "ratelimit"}), \
                 mock.patch.object(media_probe.persist_lib, "deserialize",
                                   return_value=False):
                # 连续跑 5 次：以前第 3 次后判死，现在永远 pending（长退避不计次数）
                for _ in range(5):
                    sdb = SessionLocal()
                    it = sdb.query(em.MediaItem).get(item_id)
                    it.probe_status = "probing"
                    it.probe_claimed_at = datetime.now()
                    sdb.commit()
                    sdb.close()
                    assert probe_worker.process_item(item_id) == "retry"

            db = SessionLocal()
            it = db.query(em.MediaItem).get(item_id)
            assert it.probe_status == "pending"
            assert it.probe_attempts == 0
            assert "[ratelimit]" in (it.probe_last_error or "")
            db.close()
        finally:
            db = SessionLocal()
            _cleanup_by_id(db, lib_id)
            db.close()

    def test_process_item_401_eventually_fails(self):
        db = SessionLocal()
        lib = _make_lib(db)
        lib_id = lib.id
        try:
            item = _make_item(db, lib, probe_status="probing")
            item.probe_claimed_at = datetime.now()
            item.file_path = "mount://8/m/x.mkv"
            db.commit()
            item_id = item.id
            db.close()

            with mock.patch.object(media_probe, "resolve_probe_input",
                                   return_value=(("http://x/a.mkv", {}, 1, "mkv"),
                                                 None)), \
                 mock.patch.object(media_probe, "probe_metadata",
                                   return_value={"_http_code": 401,
                                                 "_error": "auth"}), \
                 mock.patch.object(media_probe.persist_lib, "deserialize",
                                   return_value=False):
                assert probe_worker.process_item(item_id) == "retry"
                # 第二次：达到 PROBE_AUTH_MAX_ATTEMPTS=2 → failed
                sdb = SessionLocal()
                it = sdb.query(em.MediaItem).get(item_id)
                it.probe_status = "probing"
                it.probe_claimed_at = datetime.now()
                sdb.commit()
                sdb.close()
                assert probe_worker.process_item(item_id) == "failed"

            db = SessionLocal()
            it = db.query(em.MediaItem).get(item_id)
            assert it.probe_status == "failed"
            db.close()
        finally:
            db = SessionLocal()
            _cleanup_by_id(db, lib_id)
            db.close()

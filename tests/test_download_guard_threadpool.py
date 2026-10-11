"""DownloadGuard：_verdict 经线程池执行，不阻塞事件循环。"""
import os, sys
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import asyncio
import time
import unittest
from unittest import mock

import backend.subscriptions
import backend.emby_server.auth
import backend.database

from backend.download_guard import DownloadGuardMiddleware


class _FakeUser:
    id = 1
    username = "tester"
    is_staff = False
    is_admin = False
    is_superuser = False


class _FakeSession:
    def close(self):
        pass


def _fake_resolve_request_user(*args, **kwargs):
    return _FakeUser()


def _slow_download_allowed(*args, **kwargs):
    # 模拟慢 DB：阻塞 0.5s，且判定为不允许下载
    time.sleep(0.5)
    return False


def _blocked_message(*args, **kwargs):
    return "blocked"


def _fake_session_factory(*args, **kwargs):
    return _FakeSession()


DOWNLOAD_SCOPE = {
    "type": "http",
    "path": "/emby/Items/123/Download",
    "headers": [(b"user-agent", b"test")],
    "client": ("1.2.3.4", 1234),
}


class DownloadGuardThreadpoolTest(unittest.IsolatedAsyncioTestCase):

    async def _measure_heartbeat(self, work):
        """跑一个 50ms 心跳协程，同时执行 work()，返回心跳时间戳列表。"""
        stamps = []
        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                stamps.append(time.monotonic())
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        # 先让心跳跑起来并记录若干时间戳，避免首拍误差干扰
        await asyncio.sleep(0.1)
        try:
            await work()
            await asyncio.sleep(0.2)
        finally:
            stop.set()
            await hb
        return stamps

    @staticmethod
    def _max_gap(stamps):
        return max(b - a for a, b in zip(stamps, stamps[1:]))

    async def test_verdict_does_not_block_event_loop(self):
        sent = []

        async def fake_send(message):
            sent.append(message)

        mw = DownloadGuardMiddleware(app=None)

        async def work():
            await mw(DOWNLOAD_SCOPE, None, fake_send)

        with mock.patch.object(
            backend.subscriptions, "download_allowed", _slow_download_allowed
        ), mock.patch.object(
            backend.emby_server.auth,
            "resolve_request_user",
            _fake_resolve_request_user,
        ), mock.patch.object(
            backend.subscriptions, "download_gate_message", _blocked_message
        ), mock.patch.object(
            backend.database, "SessionLocal", _fake_session_factory
        ):
            stamps = await self._measure_heartbeat(work)

        self.assertGreaterEqual(len(stamps), 2, "心跳未采集到足够样本")
        max_gap = self._max_gap(stamps)
        self.assertLess(
            max_gap,
            0.3,
            "事件循环被阻塞了 %.3fs：_verdict 疑似仍在事件循环上同步执行" % max_gap,
        )

        starts = [m for m in sent if m.get("type") == "http.response.start"]
        self.assertTrue(starts, "未收到 http.response.start")
        self.assertEqual(starts[0]["status"], 403)

    async def test_heartbeat_method_detects_blocking(self):
        """对照组：直接在事件循环里 time.sleep(0.5)，心跳间隔必须被检出 > 0.4。"""

        async def work():
            time.sleep(0.5)

        stamps = await self._measure_heartbeat(work)

        self.assertGreaterEqual(len(stamps), 2, "心跳未采集到足够样本")
        max_gap = self._max_gap(stamps)
        self.assertGreater(
            max_gap,
            0.4,
            "心跳测量方法失效：同步阻塞 %.3fs 未被检出" % max_gap,
        )
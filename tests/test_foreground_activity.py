"""前台活跃打点：后台重活（缩略图补生成）在用户活跃时让路。"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("DATABASE_TYPE", "sqlite")

from backend.emby_server import foreground_activity as fa  # noqa: E402


class TestForegroundActivity:
    def setup_method(self):
        fa.reset_for_tests()

    def teardown_method(self):
        fa.reset_for_tests()

    def test_never_noted_is_inactive(self):
        assert fa.active(60) is False
        assert fa.last_seen_ago() is None

    def test_note_then_active(self):
        fa.note()
        assert fa.active(60) is True
        ago = fa.last_seen_ago()
        assert ago is not None and 0 <= ago < 5

    def test_window_expiry(self):
        fa.note()
        assert fa.active(0.05) is True
        time.sleep(0.08)
        assert fa.active(0.05) is False

    def test_thread_safe(self):
        errors = []

        def worker():
            try:
                for _ in range(200):
                    fa.note()
                    fa.active(60)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert fa.active(60) is True

    def test_never_raises(self):
        # 极端输入也不抛异常（中间件里调用，绝不能影响主链路）
        fa.active(-1)
        fa.active(float("inf"))
        fa.note()

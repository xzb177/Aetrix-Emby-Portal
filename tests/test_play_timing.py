"""播放链路耗时日志（[play-timing]）测试。

- PlayTimer：mark 记录、done 输出格式、毫秒单位
- 开关：PLAY_TIMING_LOG=0 时 new_timer 返回 no-op，不输出
- 性能：关闭时 mark/done 零开销（不抛异常、不写日志）
"""
from __future__ import annotations

import logging
import time

import backend.emby_server.play_timing as pt_mod
from backend.emby_server.play_timing import PlayTimer, _NoopTimer, new_timer


def test_timer_done_emits_play_timing_line(caplog):
    t = PlayTimer("playbackinfo item=abc123")
    t.mark("auth")
    time.sleep(0.01)  # 确保有可测量的间隔
    t.mark("cache_miss")
    with caplog.at_level(logging.INFO, logger="backend.emby_server.play_timing"):
        t.done()
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert msg.startswith("[play-timing] playbackinfo item=abc123 ")
    assert "auth=" in msg and "ms" in msg
    assert "cache_miss=" in msg
    assert "total=" in msg


def test_timer_marks_in_order(caplog):
    t = PlayTimer("stream item=xyz")
    t.mark("a")
    t.mark("b")
    t.mark("c")
    with caplog.at_level(logging.INFO, logger="backend.emby_server.play_timing"):
        t.done()
    msg = caplog.records[0].getMessage()
    ia, ib, ic = msg.index("a="), msg.index("b="), msg.index("c=")
    assert ia < ib < ic


def test_timer_marks_are_milliseconds(caplog):
    t = PlayTimer("x")
    time.sleep(0.05)
    t.mark("wait")
    with caplog.at_level(logging.INFO, logger="backend.emby_server.play_timing"):
        t.done()
    msg = caplog.records[0].getMessage()
    # 50ms 睡眠：mark 值应在合理区间（>=40ms，避免 CI 机器抖动误杀）
    import re
    m = re.search(r"wait=(\d+)ms", msg)
    assert m, msg
    assert int(m.group(1)) >= 40


def test_new_timer_returns_noop_when_disabled(caplog, monkeypatch):
    monkeypatch.setattr(pt_mod, "_ENABLED", False)
    t = new_timer("anything")
    assert isinstance(t, _NoopTimer)
    # no-op 调用不抛异常、不写日志
    with caplog.at_level(logging.INFO, logger="backend.emby_server.play_timing"):
        t.mark("a")
        t.done()
    assert len(caplog.records) == 0


def test_new_timer_returns_real_when_enabled(monkeypatch):
    monkeypatch.setattr(pt_mod, "_ENABLED", True)
    t = new_timer("anything")
    assert isinstance(t, PlayTimer)


def test_is_enabled_reflects_module_flag(monkeypatch):
    monkeypatch.setattr(pt_mod, "_ENABLED", True)
    assert pt_mod.is_enabled() is True
    monkeypatch.setattr(pt_mod, "_ENABLED", False)
    assert pt_mod.is_enabled() is False


def test_done_emits_without_root_logging_config():
    """EA（emby_api/main.py）无 basicConfig、uvicorn 不配 root 时，
    [play-timing] 仍须输出（回归：生产 EA 曾一条日志都没有）。"""
    import subprocess
    import sys

    code = (
        "import logging, sys; "
        "assert not logging.getLogger().handlers, 'root must be unconfigured'; "
        "from backend.emby_server.play_timing import new_timer; "
        "t = new_timer('ea-smoke'); t.mark('a'); t.done()"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "[play-timing] ea-smoke" in out, out


def test_no_duplicate_handler_when_root_configured():
    """EM（backend/main.py 有 basicConfig）时走传播，不自加 handler，避免重复。"""
    import subprocess
    import sys

    code = (
        "import logging; logging.basicConfig(level=logging.INFO); "
        "import backend.emby_server.play_timing as m; "
        "print(len(m.logger.handlers), m.logger.propagate)"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert "0 True" in r.stdout, r.stdout

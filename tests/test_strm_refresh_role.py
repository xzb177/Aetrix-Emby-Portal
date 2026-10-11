"""strm 签名刷新的容器职责划分（方案 1）回归测试。

背景：.strm 签名 TTL 1 小时，过期靠 strm_sig_refresh_tick() 刷新；刷新要写
/strm（.tmp + os.replace）。worker / EA 容器的 /strm 是只读挂载，只有 api
容器是 rw —— 所以刷新只能跑在 api：
- worker（backend/worker.py）与 EA（emby_api/main.py）的 janitor 必须
  start_janitor(run_strm_refresh=False)，不再跑刷新；
- api（backend/main.py，AETRIX_ROLE=api）起专属线程 start_strm_sig_refresh_loop()；
- 单体模式（未设 AETRIX_ROLE）保持默认 True，向后兼容；
- docker-compose.yml 的挂载配置本 PR 不动（worker 只读的设计意图保留）。
"""
import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_step_skips_refresh_when_disabled(monkeypatch):
    """worker / EA：run_strm_refresh=False 时不调 tick，只记 skipped。"""
    from backend.emby_server import maintenance

    called = []

    def _boom(*a, **k):  # 调了就说明门没关严
        called.append(1)
        raise AssertionError("tick 不该被调用")

    monkeypatch.setattr(maintenance, "strm_sig_refresh_tick", _boom)
    result = {}
    maintenance._strm_sig_refresh_step(result, run_strm_refresh=False)
    assert called == []
    assert result["strm_sig_refresh"] == {"skipped": True, "reason": "not_api_role"}


def test_step_runs_refresh_when_enabled(monkeypatch):
    """api / 单体：run_strm_refresh=True（默认）时调 tick 并记录结果。"""
    from backend.emby_server import maintenance

    sentinel = {"refreshed": 3, "errors": 0}
    monkeypatch.setattr(
        maintenance, "strm_sig_refresh_tick", lambda *a, **k: sentinel
    )
    result = {}
    maintenance._strm_sig_refresh_step(result, run_strm_refresh=True)
    assert result["strm_sig_refresh"] is sentinel


def test_step_isolates_tick_exception(monkeypatch, caplog):
    """tick 抛异常（理论上永不抛）也不能把 janitor 拖死：记 warning + skipped。"""
    from backend.emby_server import maintenance

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(maintenance, "strm_sig_refresh_tick", _raise)
    result = {}
    with caplog.at_level(logging.WARNING, logger=maintenance.logger.name):
        maintenance._strm_sig_refresh_step(result, run_strm_refresh=True)
    assert result["strm_sig_refresh"] == {"skipped": True, "reason": "error"}
    assert any("签名刷新失败" in r.getMessage() for r in caplog.records)


class _BreakLoop(BaseException):
    """跳出被测后台循环（except Exception 抓不到 BaseException）。"""


def _capture_thread_target(monkeypatch, module):
    """把 threading.Thread 换成只记录 target 的假类，返回 targets 列表。"""
    targets = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            targets.append({"target": target, "daemon": daemon, "name": name})

        def start(self):
            pass

    class FastEvent:
        def wait(self, timeout=None):
            return True

    monkeypatch.setattr(module.threading, "Thread", FakeThread)
    monkeypatch.setattr(module.threading, "Event", FastEvent)
    return targets


def test_start_janitor_forwards_run_strm_refresh_false(monkeypatch):
    """worker 入口语义：start_janitor(run_strm_refresh=False) 落到 janitor_tick。"""
    from backend.emby_server import maintenance

    seen = []

    def fake_tick(*a, **k):
        seen.append((a, k))
        raise _BreakLoop()

    monkeypatch.setattr(maintenance, "janitor_tick", fake_tick)
    monkeypatch.setattr(maintenance, "_JANITOR_STARTED", False)
    targets = _capture_thread_target(monkeypatch, maintenance)

    assert maintenance.start_janitor(run_strm_refresh=False) is True
    assert targets and targets[0]["daemon"] is True
    assert targets[0]["name"] == "maintenance-janitor"
    with pytest.raises(_BreakLoop):
        targets[0]["target"]()
    assert seen == [((), {"run_strm_refresh": False})]


def test_start_janitor_defaults_run_strm_refresh_true(monkeypatch):
    """单体模式向后兼容：默认仍跑刷新。"""
    from backend.emby_server import maintenance

    seen = []

    def fake_tick(*a, **k):
        seen.append((a, k))
        raise _BreakLoop()

    monkeypatch.setattr(maintenance, "janitor_tick", fake_tick)
    monkeypatch.setattr(maintenance, "_JANITOR_STARTED", False)
    targets = _capture_thread_target(monkeypatch, maintenance)

    assert maintenance.start_janitor() is True
    with pytest.raises(_BreakLoop):
        targets[0]["target"]()
    assert seen == [((), {"run_strm_refresh": True})]


def test_start_strm_sig_refresh_loop_single_start_and_daemon(monkeypatch):
    """api 专属刷新线程：只启动一次；daemon；循环体调 tick。"""
    from backend.emby_server import maintenance

    monkeypatch.setattr(maintenance, "_STRM_REFRESH_LOOP_STARTED", False)
    targets = _capture_thread_target(monkeypatch, maintenance)

    assert maintenance.start_strm_sig_refresh_loop() is True
    assert maintenance.start_strm_sig_refresh_loop() is False  # 第二次不再起
    assert len(targets) == 1
    assert targets[0]["daemon"] is True
    assert targets[0]["name"] == "strm-sig-refresh"

    calls = []

    def fake_tick(*a, **k):
        calls.append(1)
        raise _BreakLoop()

    monkeypatch.setattr(maintenance, "strm_sig_refresh_tick", fake_tick)
    with pytest.raises(_BreakLoop):
        targets[0]["target"]()
    assert calls == [1]


def test_worker_disables_refresh_in_janitor():
    """backend/worker.py：worker 的 janitor 显式关闭签名刷新。"""
    text = (ROOT / "backend" / "worker.py").read_text(encoding="utf-8")
    assert "start_janitor(run_strm_refresh=False)" in text


def test_ea_disables_refresh_in_janitor():
    """emby_api/main.py：EA（/strm 只读）同样关闭，避免只读 OSError 洪水。"""
    text = (ROOT / "emby_api" / "main.py").read_text(encoding="utf-8")
    assert "start_janitor(run_strm_refresh=False)" in text


def test_api_role_starts_refresh_loop():
    """backend/main.py：AETRIX_ROLE=api 分支启动专属刷新线程。"""
    text = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    api_branch = text.split("if _is_api_role:")[1].split("if not _is_api_role:")[0]
    assert "start_strm_sig_refresh_loop" in api_branch


def test_compose_mounts_untouched_by_this_change():
    """本 PR 不改 docker-compose 挂载（#602 已把 worker 改为 rw，当前口径
    ro=1、rw=2；本 PR 只动代码不动挂载，worker 只读设计的恢复由 parent 定夺）。
    """
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert (
        text.count("${STRM_MOUNT_DIR:-/opt/strm}:${STRM_CONTAINER_PATH:-/strm}:ro")
        == 1
    ), "EA 保持只读"
    assert (
        text.count("${STRM_MOUNT_DIR:-/opt/strm}:${STRM_CONTAINER_PATH:-/strm}:rw")
        == 2
    ), "api + worker（#602 口径）"

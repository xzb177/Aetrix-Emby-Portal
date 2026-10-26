"""115 sidecar 生产接线：分发切换 + 启动时显式拉起。

覆盖（不碰真 115、不读库）：
- build_provider：115 类型开闸走 sidecar、关闸走进程内；sidecar 构造
  失败直接抛，不静默回落进程内（#1251 立定的失败语义）。
- supervisor：关闸不拉起；已有健康 sidecar 时复用不重复拉起；
  真拉起子进程并握手验活、stop 只停自己拉起的；子进程早退时返回
  False 且 last_error 含 returncode 与 socket（可诊断）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import mounts as _mounts_first  # noqa: F401 — 先导，避既有循环导入
from backend.emby_server import mount_sidecar
from backend.emby_server import p115_sidecar_supervisor as sup
from backend.emby_server.mounts import MountError, Pan115Mount, build_provider
from backend.emby_server.p115_sidecar import P115SidecarHandler
from backend.emby_server.provider_runtime import (
    P115_EXTENSIONS,
    ProviderRuntimeClient,
    ProviderRuntimeServer,
)


def _mount():
    return SimpleNamespace(
        id=7, mount_type="115", name="test-115",
        config=json.dumps({"cookie": "cookie-123", "cid": "0"}), path="")


# ---- 分发切换（假 sidecar 类注入，不碰 UDS） ----

class _FakeSidecar:
    seen = []

    def __init__(self, mount, db=None, library=None):
        _FakeSidecar.seen.append(mount)
        self.mount = mount


def test_build_provider_routes_115_to_sidecar_when_enabled(monkeypatch):
    monkeypatch.setattr(mount_sidecar, "sidecar_enabled", lambda: True)
    monkeypatch.setattr(mount_sidecar, "P115SidecarMount", _FakeSidecar)
    _FakeSidecar.seen.clear()
    prov = build_provider(_mount())
    assert isinstance(prov, _FakeSidecar)
    assert _FakeSidecar.seen and _FakeSidecar.seen[0].mount_type == "115"


def test_build_provider_routes_115_to_inproc_when_disabled(monkeypatch):
    monkeypatch.setattr(mount_sidecar, "sidecar_enabled", lambda: False)
    prov = build_provider(_mount())
    assert isinstance(prov, Pan115Mount)


def test_build_provider_sidecar_failure_is_loud_not_fallback(monkeypatch):
    class _Boom:
        def __init__(self, *args, **kwargs):
            raise MountError("sidecar 未拉起（模拟）")

    monkeypatch.setattr(mount_sidecar, "sidecar_enabled", lambda: True)
    monkeypatch.setattr(mount_sidecar, "P115SidecarMount", _Boom)
    # 回落会悄悄返回 Pan115Mount；抛出来才是既有语义。
    with pytest.raises(MountError, match="sidecar 未拉起"):
        build_provider(_mount())


# ---- supervisor ----

@pytest.fixture
def _sup_env(monkeypatch):
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "1")
    sup._reset_state_for_tests()
    yield
    sup.stop()
    sup._reset_state_for_tests()


def _wait_sock(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


def test_supervisor_does_nothing_when_disabled(monkeypatch):
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    sup._reset_state_for_tests()
    try:
        assert sup.ensure_started(socket_path="/tmp/definitely-not-a-sidecar.sock") is True
        assert sup.status()["mode"] == "disabled"
        assert sup.status()["pid"] is None
    finally:
        sup._reset_state_for_tests()


def test_supervisor_reuses_healthy_sidecar_and_does_not_stop_it(tmp_path, _sup_env):
    sock = str(tmp_path / "p115" / "runtime.sock")
    server = ProviderRuntimeServer(P115SidecarHandler(), sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert _wait_sock(sock)
    try:
        assert sup.ensure_started(socket_path=sock) is True
        assert sup.status()["mode"] == "reused"
        sup.stop()  # 复用的不碰：停完握手仍通
        client = ProviderRuntimeClient("p115", sock, timeout_sec=2.0)
        assert client.handshake(P115_EXTENSIONS)["provider"] == "p115"
    finally:
        server.stop()
        thread.join(timeout=5)


def test_supervisor_spawns_real_sidecar_and_stops_it(tmp_path, _sup_env):
    sock = str(tmp_path / "p115" / "runtime.sock")
    assert sup.ensure_started(socket_path=sock, timeout_sec=30) is True
    st = sup.status()
    assert st["mode"] == "spawned"
    assert st["pid"] and st["alive"] is True
    client = ProviderRuntimeClient("p115", sock, timeout_sec=2.0)
    assert client.handshake(P115_EXTENSIONS)["provider"] == "p115"
    sup.stop()
    assert sup.status()["mode"] == "not_running"


def test_supervisor_early_exit_is_diagnosable(tmp_path, _sup_env):
    sock = str(tmp_path / "p115" / "runtime.sock")
    argv = [sys.executable, "-c", "import sys; sys.exit(3)"]
    assert sup.ensure_started(argv=argv, socket_path=sock, timeout_sec=5) is False
    st = sup.status()
    assert st["mode"] == "not_running"
    assert st["last_error"] is not None
    assert "returncode=3" in st["last_error"]
    assert sock in st["last_error"]

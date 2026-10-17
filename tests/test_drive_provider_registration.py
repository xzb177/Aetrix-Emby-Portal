"""drive 提供者自注册回归测试（生产事故：2026-10-17 EA 进程扫描报「不支持的挂载类型：drive」）

背景：
- ``backend/emby_server/mounts.py`` 只自导入了 ``mount_rclone``；drive 提供者之前靠
  ``backend.api.servers`` / ``change_watcher`` 的导入副作用注册。
- EA 进程（aetrix-ea）两者都不导入 → 它的 ``_PROVIDERS`` 里没有 drive，
  ``build_provider`` 对 ``mount_type='drive'`` 的挂载抛 MountError。
- 修复：``mounts.py`` 底部显式自导入 ``mount_drive``（导入即注册），
  与 rclone 同模式。

本文件用**子进程**形态断言，确保测的是「只 ``import mounts`` 的真空状态」——
不能在本测试进程里直接断言，因为其他测试文件可能早已导入过 ``mount_drive``
（导入注册是进程级全局状态），直接断言会假绿。
"""
import os
import subprocess
import sys

# 与 test_drive_mount.py 同款：必须在 import backend 之前钉住 sqlite，
# 否则本文件若先于其他测试被收集，backend.database 会按默认 PG 建引擎。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_VACUUM_PROBE = """
import os
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
from types import SimpleNamespace
from backend.emby_server import mounts

providers = sorted(mounts._PROVIDERS)
assert "drive" in providers, f"drive 未注册，真空导入后的提供者只有: {providers}"

# 构造一个 drive 挂载（不碰网络、不碰数据库：DriveMount.__init__ 只读 path/config，
# 凭据只在列目录时才取）
mount = SimpleNamespace(mount_type="drive", path="drive:test-drive-id", config=None)
provider = mounts.build_provider(mount)
assert type(provider).__name__ == "DriveMount", f"build_provider 造出的不是 DriveMount: {type(provider)}"
assert provider.drive_id == "test-drive-id"

# 反向确认：未知类型仍然明确报错（而不是静默）
bad = SimpleNamespace(mount_type="webdav", path="x", config=None)
try:
    mounts.build_provider(bad)
except Exception as exc:
    assert "不支持的挂载类型" in str(exc), f"未知类型的报错文案变了: {exc}"
else:
    raise AssertionError("未知类型没有抛 MountError")
print("vacuum-probe-ok")
"""


def _run_vacuum_probe() -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _VACUUM_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONPATH": REPO_ROOT},
    )
    return proc


def test_drive_provider_registered_in_vacuum_import():
    """只 import backend.emby_server.mounts 的全新进程里，drive 必须已注册。"""
    proc = _run_vacuum_probe()
    assert proc.returncode == 0, (
        f"真空子进程退出码 {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "vacuum-probe-ok" in proc.stdout


def test_build_provider_drive_vacuum_does_not_raise_mounterror():
    """真空导入下 build_provider(drive 挂载) 不抛「不支持的挂载类型」。"""
    proc = _run_vacuum_probe()
    assert "不支持的挂载类型" not in (proc.stdout + proc.stderr), (
        f"真空导入下仍报不支持 drive:\n{proc.stdout}\n{proc.stderr}"
    )
    assert proc.returncode == 0

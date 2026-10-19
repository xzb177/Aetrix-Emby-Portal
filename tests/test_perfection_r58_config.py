"""perfection r58：配置纪律——同一环境变量只许一处默认值。

R58-1：MOUNT_UA 在 mounts.py（默认 PAN115_UA）与 streaming.py（此前是另一套
短 Mozilla 默认）两处默认值不一致；现 streaming.REMOTE_UA 直接复用
mounts.MOUNT_UA。
R58-2：MOUNT_RCLONE_CONF 在 mount_rclone.py（此前默认 data/rclone/rclone.conf）
与 drive_changes.py / database.py / compose（/config/rclone/rclone.conf）默认
值不一致；未配该变量时后台粘贴的 rclone.conf 与 Drive Changes 轮询读的不是
同一个文件。现统一为 /config/rclone/rclone.conf。

用子进程测：模块级常量在 import 时按进程环境求值，同一进程内改 os.environ
无法重算，必须起干净环境分别验证「未设置」与「已设置」两种情形。
"""

import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = (
    "import os;"
    "from backend.emby_server import streaming, mounts, transfer115,"
    " mount_rclone, drive_changes;"
    "print(streaming.REMOTE_UA);"
    "print(mounts.MOUNT_UA);"
    "print(transfer115.PAN115_UA);"
    "print(mount_rclone.CONF_PATH);"
    "print(drive_changes.RCLONE_CONF)"
)


def _probe(extra_env: dict) -> list[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DATABASE_TYPE": "sqlite",
    }
    env.update(extra_env)
    # 确保这两个键真的不存在（而非空串）：空串时两处行为一致，无判别力
    env.pop("MOUNT_UA", None)
    env.pop("MOUNT_RCLONE_CONF", None)
    for k, v in extra_env.items():
        env[k] = v
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, f"probe failed: {out.stderr[-2000:]}"
    return out.stdout.splitlines()


def test_mount_ua_single_default_when_unset():
    remote_ua, mount_ua, pan115_ua, _, _ = _probe({})
    assert mount_ua == pan115_ua, "mounts.MOUNT_UA 默认应为 115 优化的 PAN115_UA"
    assert remote_ua == mount_ua, (
        "streaming.REMOTE_UA 必须复用 mounts.MOUNT_UA，不许另起一套默认值"
    )


def test_mount_ua_env_override_applies_to_both():
    remote_ua, mount_ua, _, _, _ = _probe({"MOUNT_UA": "X-Custom-UA/1.0"})
    assert mount_ua == "X-Custom-UA/1.0"
    assert remote_ua == "X-Custom-UA/1.0"


def test_rclone_conf_single_default_when_unset():
    _, _, _, conf_path, rclone_conf = _probe({})
    assert rclone_conf == "/config/rclone/rclone.conf"
    assert conf_path == rclone_conf, (
        "mount_rclone.CONF_PATH 默认必须与 drive_changes.RCLONE_CONF 一致，"
        "否则后台粘贴的 rclone.conf 与轮询读的不是同一文件"
    )


def test_rclone_conf_env_override_applies_to_both():
    _, _, _, conf_path, rclone_conf = _probe(
        {"MOUNT_RCLONE_CONF": "/tmp/custom-r58/rclone.conf"}
    )
    assert conf_path == "/tmp/custom-r58/rclone.conf"
    assert rclone_conf == "/tmp/custom-r58/rclone.conf"


def test_no_second_literal_default_for_mount_ua_in_streaming():
    """防漂移：streaming.py 不许再出现 os.getenv("MOUNT_UA", <字面量>)。"""
    src = open(os.path.join(REPO_ROOT, "backend/emby_server/streaming.py"),
               encoding="utf-8").read()
    assert 'os.getenv("MOUNT_UA"' not in src, (
        "MOUNT_UA 的默认值只许在 mounts.py 定义，streaming.py 应复用 mounts.MOUNT_UA"
    )

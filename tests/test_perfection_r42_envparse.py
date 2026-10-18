"""r42：env 解析统一迁入 backend.emby_server.env_util 的回归测试。

覆盖：
1. env_int/env_float 语义：非法/空/未设置→回落默认并记 warning；合法值按原钳制；
   lo/hi 为 None 时不钳制该侧（保持各调用点原有 max()/min() 语义）。
2. 模块顶层不再因非法 env 值在 import 时崩（r42 前 ValueError 直接炸服务）。
3. 迁移点 unset 默认值与旧实现一致（抽查代表点；全量对账见 r42_compare.py）。
"""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.emby_server.env_util import env_float, env_int

# 仓库根：由本文件位置推导（tests/ 在仓库根下一级），不要写死本机绝对路径，
# 否则在 CI 等其他机器上路径不存在会导致子进程 cwd 失败（r42 冒烟曾因此红）。
REPO_ROOT = str(Path(__file__).resolve().parents[1])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("R42_"):
            monkeypatch.delenv(k, raising=False)


def _set(monkeypatch, v):
    if v is None:
        monkeypatch.delenv("R42_X", raising=False)
    else:
        monkeypatch.setenv("R42_X", v)


@pytest.mark.parametrize("raw,expected", [
    (None, 100), ("", 100), ("   ", 100),
    ("50", 50), ("-3", -3), ("007", 7),
    ("abc", 100), ("12.9", 100), ("0x10", 100),
])
def test_env_int_fallback(monkeypatch, caplog, raw, expected):
    _set(monkeypatch, raw)
    with caplog.at_level("WARNING", logger="backend.emby_server.env_util"):
        assert env_int("R42_X", 100) == expected
    if raw in ("abc", "12.9", "0x10"):
        assert "R42_X" in caplog.text  # 非法值记 warning


def test_env_int_clamp(monkeypatch):
    monkeypatch.setenv("R42_X", "5")
    assert env_int("R42_X", 100, 10) == 10
    assert env_int("R42_X", 100, 1, 200) == 5
    monkeypatch.setenv("R42_X", "500")
    assert env_int("R42_X", 100, 10, 200) == 200
    assert env_int("R42_X", 100, 10) == 500  # 无 hi 不钳制上侧
    monkeypatch.setenv("R42_X", "-50")
    assert env_int("R42_X", 100) == -50  # 无 lo 不钳制下侧


@pytest.mark.parametrize("raw,expected", [
    (None, 2.0), ("", 2.0), ("1.5", 1.5), ("-1.5", -1.5),
    ("xyz", 2.0), ("nan-ok", 2.0),
])
def test_env_float_fallback(monkeypatch, raw, expected):
    _set(monkeypatch, raw)
    assert env_float("R42_X", 2.0) == expected


def test_env_float_clamp(monkeypatch):
    monkeypatch.setenv("R42_X", "0.1")
    assert env_float("R42_X", 2.0, 1.0) == 1.0
    monkeypatch.setenv("R42_X", "0.7")
    assert env_float("R42_X", 0.2, 0.0, 0.5) == 0.5
    monkeypatch.setenv("R42_X", "3.3")
    assert env_float("R42_X", 0.2, 0.0) == 3.3  # 无 hi 不钳制


def _import_fresh(modname, env_extra):
    """子进程全新 import 指定模块（避免本进程已 import 的缓存干扰）。"""
    code = (
        "import importlib, json, sys; "
        "m = importlib.import_module(sys.argv[1]); "
        "print(json.dumps({'ok': True, 'file': m.__file__}))"
    )
    import os
    env = dict(os.environ)
    env.update(env_extra)
    r = subprocess.run([sys.executable, "-c", code, modname],
                       capture_output=True, text=True, timeout=120,
                       cwd=REPO_ROOT,
                       env={**env, "PYTHONPATH": REPO_ROOT})
    return r


@pytest.mark.parametrize("modname,env_extra", [
    ("backend.emby_server.enrich_worker", {"ENRICH_BATCH": "abc", "ENRICH_TMDB_BATCH": "xyz"}),
    ("backend.emby_server.tmdb", {"TMDB_TIMEOUT": "xyz", "TMDB_NET_RETRIES": "nope"}),
    ("backend.emby_server.scanner", {"PROBE_REMOTE_RANGE_BYTES": "junk"}),
    ("backend.emby_server.mounts", {"MOUNT_REMOTE_CONCURRENCY": "junk"}),
    ("backend.security", {"ACCESS_TOKEN_EXPIRE_MINUTES": "junk"}),
    ("backend.emby_server.media_probe", {"PROBE_MAX_ATTEMPTS": "junk"}),
])
def test_garbage_env_no_longer_crashes_import(modname, env_extra):
    """r42 前：任一非法值 → 模块 import 直接 ValueError，服务起不来。"""
    r = _import_fresh(modname, env_extra)
    assert r.returncode == 0, f"{modname} import 失败: {r.stderr[-500:]}"
    assert json.loads(r.stdout)["ok"] is True


def test_garbage_env_falls_back_to_default():
    r = _import_fresh("backend.emby_server.enrich_worker", {"ENRICH_BATCH": "abc"})
    assert r.returncode == 0
    code = (
        "import importlib, sys; "
        "m = importlib.import_module('backend.emby_server.enrich_worker'); "
        "print(m.ENRICH_BATCH)"
    )
    import os
    env = dict(os.environ, ENRICH_BATCH="abc",
               PYTHONPATH=REPO_ROOT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=120, cwd=REPO_ROOT, env=env)
    assert r.returncode == 0
    assert r.stdout.strip() == "100"


@pytest.mark.parametrize("modname,attr,expected", [
    ("backend.emby_server.enrich_worker", "ENRICH_BATCH", 100),
    ("backend.emby_server.tmdb", "TMDB_TIMEOUT", 10.0),
    ("backend.emby_server.tmdb", "TMDB_NET_RETRIES", 2),
    ("backend.emby_server.mounts", "MOUNT_REMOTE_CONCURRENCY", 2),
    ("backend.emby_server.mounts", "CHASE_REMOTE_CONCURRENCY", 1),
    ("backend.emby_server.scan_queue", "SCAN_MAX_PARALLEL", 2),
    ("backend.emby_server.drive_changes", "POLL_JITTER_RATIO", 0.2),
    ("backend.emby_server.task_scheduler", "ENRICH_PREEMPT_MIN_THREADS", 4),
    ("backend.security", "ACCESS_TOKEN_EXPIRE_MINUTES", 120),
    ("backend.node_auth", "SIGN_MAX_SKEW", 300),
    ("backend.reminders", "REMINDER_INTERVAL", 3600),
])
def test_unset_defaults_unchanged(modname, attr, expected):
    """unset 时各迁移点取值与迁移前一致（全量对账另见 hidden_files/r42_verify.py）。"""
    m = importlib.import_module(modname)
    assert getattr(m, attr) == expected

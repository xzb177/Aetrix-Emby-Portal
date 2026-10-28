"""找源写端点必须进鉴权覆盖扫描。

回归：#1415 的 POST /confirm 没登记进 scripts/check_auth_coverage.py
的 ROUTER_MODULES，扫描 224 个写端点却扫不到它——将来有人把它的
get_current_user 删了，扫描也不会红。本测试直接跑扫描的收集逻辑：
confirm 必须被收集到且被判定为已鉴权。
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "check_auth_coverage", REPO_ROOT / "scripts" / "check_auth_coverage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_confirm_is_scanned_and_guarded():
    module = _load_script()
    endpoints = module.collect_write_endpoints()
    key = ("POST", "/api/user/source-discovery/confirm")
    assert key in endpoints, (
        "POST /confirm 未进鉴权扫描清单（ROUTER_MODULES 漏登记）")
    endpoint, router_auth = endpoints[key]
    assert module.guarded(endpoint, router_auth) is True

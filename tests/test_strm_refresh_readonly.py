"""strm_sig_refresh_tick 只读 /strm 提前跳过（P3）。

背景：revert #605（方案2）后，EA 容器的 janitor 也会调 strm_sig_refresh_tick，
而 EA 的 /strm 是只读挂载。旧行为：全树 walk 之后每个需刷新的文件写 .tmp 都
OSError（被单文件 except 吞掉），最后汇总出一条误导性的"刷新失败请检查日志"
warning。新行为：isdir 检查后先探写一次，不可写直接返回
{"skipped": True, "reason": "read_only"}。

本测试在旧代码上失败（返回的是完整 result dict，没有 read_only），
在新代码上通过。
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _no_db_throttle(monkeypatch):
    # 同 test_strm_refresh_cadence.py：节流时间戳改走 DB（全局 SessionLocal）
    # 后成了跨用例共享状态，CI 共享 sqlite 里上一个用例写的时间戳会把本文件的
    # tick 在 throttle 处提前跳过（reason=interval），到不了 read_only 检查。
    # 本文件只测只读探针行为，隔离掉 DB 会话、走 /tmp 回落路径。
    from backend.emby_server import maintenance
    monkeypatch.setattr(maintenance, "_refresh_ts_session", lambda: None)


def test_readonly_strm_dir_skips_early(tmp_path, monkeypatch):
    from backend.emby_server import maintenance

    strm_dir = tmp_path / "strm_ro"
    strm_dir.mkdir()
    # 绕过节流与配置：上次运行文件指向不存在的路径，strm 目录走 tmp
    monkeypatch.setattr(
        maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "never.last"))
    monkeypatch.setattr(
        maintenance, "_strm_refresh_settings", lambda db=None: (str(strm_dir), 3600))

    real_open = open

    def _ro_open(path, mode="r", *args, **kwargs):
        # 只让探针文件的写失败（模拟只读挂载），其余 open 走真实逻辑
        if ".write_probe_" in str(path) and "w" in mode:
            raise OSError(30, "Read-only file system")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _ro_open)

    result = maintenance.strm_sig_refresh_tick(db=None)
    assert result == {"skipped": True, "reason": "read_only"}
    # 探针文件不能残留
    assert list(strm_dir.glob(".write_probe_*")) == []


def test_writable_strm_dir_still_refreshes(tmp_path, monkeypatch):
    """可写目录不受影响：空目录走完 walk，返回正常 result（非 skipped）。"""
    from backend.emby_server import maintenance

    strm_dir = tmp_path / "strm_rw"
    strm_dir.mkdir()
    monkeypatch.setattr(
        maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "never.last"))
    monkeypatch.setattr(
        maintenance, "_strm_refresh_settings", lambda db=None: (str(strm_dir), 3600))

    result = maintenance.strm_sig_refresh_tick(db=None)
    assert result.get("skipped") is not True
    assert result["refreshed"] == 0
    assert list(strm_dir.glob(".write_probe_*")) == []

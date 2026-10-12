"""P1 修复回归测试（DeepSeek 生成）：revoke_all_user_tokens、transcode_slot、hwaccel 参数、find_active_transcode 并发安全。"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import threading

import pytest
from unittest import mock


def _import_or_skip(name: str):
    try:
        mod = __import__(name, fromlist=["*"])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{name} 导入失败，跳过: {exc!r}")
    return mod


def test_revoke_all_user_tokens_commit_and_rollback():
    """A-P1-1: revoke_all_user_tokens 正常时 commit，异常时 rollback 且不抛异常。"""
    security = _import_or_skip("backend.security")

    # 正常路径
    db = mock.MagicMock()
    security.revoke_all_user_tokens(db, 123)
    assert db.query.return_value.filter.return_value.update.called, "应调用 update"
    assert db.commit.called, "应调用 commit"
    assert not db.rollback.called, "正常路径不应回滚"

    # 异常路径
    db2 = mock.MagicMock()
    db2.query.return_value.filter.return_value.update.side_effect = RuntimeError("boom")
    security.revoke_all_user_tokens(db2, 123)  # 不应抛异常
    assert db2.rollback.called, "异常时应 rollback"


def test_transcode_slot_is_contextmanager():
    """D-P1-1: transcode_slot 是上下文管理器，进入时调用 ensure_slot_or_503。"""
    transcode = _import_or_skip("backend.emby_server.transcode")
    slot = getattr(transcode, "transcode_slot", None)
    assert callable(slot), "缺少 transcode_slot"

    with mock.patch.object(transcode, "ensure_slot_or_503") as m_ensure:
        with transcode.transcode_slot():
            pass
    assert m_ensure.called, "进入上下文时应调用 ensure_slot_or_503"


def test_hw_params():
    """D-P1-3: 各后端设备参数与滤镜参数。"""
    hwaccel = _import_or_skip("backend.emby_server.hwaccel")
    assert hwaccel.get_hw_device_args("vaapi") == ["-vaapi_device", "/dev/dri/renderD128"]
    assert hwaccel.get_hw_device_args("software") == []
    assert hwaccel.get_hw_device_args("nvenc") == []
    assert hwaccel.get_hw_filter("vaapi") == "format=nv12,hwupload"
    assert hwaccel.get_hw_filter("qsv") == "format=nv12,hwupload=extra_hw_frames=64"
    assert hwaccel.get_hw_filter("software") == ""


def test_find_active_transcode_no_runtime_error():
    """D-P1-2: _TRANSCODE_PROCS 被并发 pop 时遍历不抛 RuntimeError。"""
    streaming = _import_or_skip("backend.emby_server.streaming")
    procs = streaming._TRANSCODE_PROCS
    assert isinstance(procs, dict), "_TRANSCODE_PROCS 应为 dict"
    original = dict(procs)
    stop = threading.Event()

    def _popper():
        while not stop.is_set():
            try:
                procs.popitem()
            except KeyError:
                break

    try:
        procs.clear()
        for i in range(50):
            procs[f"sid-{i}"] = {"user_id": 1, "item_guid": "g"}
        t = threading.Thread(target=_popper, daemon=True)
        t.start()
        try:
            for _ in range(100):
                try:
                    streaming.find_active_transcode(1, "g")
                except RuntimeError as exc:
                    pytest.fail(f"并发遍历抛出 RuntimeError: {exc!r}")
        finally:
            stop.set()
            t.join(timeout=5)
    finally:
        procs.clear()
        procs.update(original)


def test_production_filter_scale_before_hwupload():
    """P1-5: 生产滤镜链必须是 scale 在前、hwupload 在后。

    vaapi/qsv 的 hwupload 之后是硬件帧，软件 scale 处理不了；
    顺序反了（format=nv12,hwupload,scale）转码必失败。
    """
    hwaccel = _import_or_skip("backend.emby_server.hwaccel")
    assert hwaccel.get_production_filter("vaapi", height=720) == \
        "scale=-2:720,format=nv12,hwupload"
    assert hwaccel.get_production_filter("qsv", height=720) == \
        "scale=-2:720,format=nv12,hwupload=extra_hw_frames=64"
    # nvenc/software 无 hw 片段：纯 scale；无 height：空串
    assert hwaccel.get_production_filter("nvenc", height=720) == "scale=-2:720"
    assert hwaccel.get_production_filter("software", height=720) == "scale=-2:720"
    assert hwaccel.get_production_filter("vaapi") == "format=nv12,hwupload"
    assert hwaccel.get_production_filter("software") == ""

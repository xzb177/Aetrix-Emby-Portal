"""第 33 轮完美主义迭代回归测试（R33）。

覆盖（2026-10-14 08:28 快捷方式解析功能整体取消后，快捷方式相关
P1-1/P1-2/P2-1/P2-2/P2-3/P2-5 的实现与回归测试已同步删除；列举遇到
快捷方式条目时当普通条目处理——无视频扩展名则自然跳过）。
注（2026-10-17）：strm_gen 删除，依赖 sg.iter_drive_videos 的快捷方式
列举测试与 strm_sig_refresh_tick 节奏测试同步删除：
- TestR33ShortcutSkipped：iter_drive_videos 已随 strm_gen 删除
- P2-4：token=null 的 rclone.conf → DriveAuthError（不是 AttributeError）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))



class TestR33P24NonDictToken:
    def test_null_token_conf_raises_drive_auth_error(self, tmp_path, monkeypatch):
        from backend.emby_server import drive_changes as dc
        conf = tmp_path / "rclone.conf"
        conf.write_text("[myremote]\nclient_id = cid\nclient_secret = csec\n"
                        "token = null\n", encoding="utf-8")
        monkeypatch.setattr(dc, "RCLONE_CONF", str(conf))
        # token=null 是合法 JSON 但非对象 → None（不是 AttributeError）
        assert dc._oauth_conf_for_remote("myremote") is None
        with pytest.raises(dc.DriveAuthError):
            dc._oauth_token_for_remote("myremote")

    def test_numeric_token_conf_raises_drive_auth_error(self, tmp_path, monkeypatch):
        from backend.emby_server import drive_changes as dc
        conf = tmp_path / "rclone.conf"
        conf.write_text("[myremote]\nclient_id = cid\nclient_secret = csec\n"
                        "token = 123\n", encoding="utf-8")
        monkeypatch.setattr(dc, "RCLONE_CONF", str(conf))
        assert dc._oauth_conf_for_remote("myremote") is None
        with pytest.raises(dc.DriveAuthError):
            dc._oauth_token_for_remote("myremote")

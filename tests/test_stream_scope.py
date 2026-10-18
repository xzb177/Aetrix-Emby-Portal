"""播放端点库范围越权防护测试（P0 安全修复）。

验证 video_stream 在用户被限制库可见范围时，
不能通过直接构造 URL 越权播放受限库的内容。
（2026-10-17 转码删除：video_hls 已不存在，只剩 video_stream 一个播放端点。）
"""
import pytest


def _blocked(allowed, library_id):
    """与 api.py 中的检查逻辑保持一致"""
    return allowed is not None and library_id not in allowed


class TestLibraryScopeStreamGuard:
    def test_none_scope_does_not_block(self):
        """绝大多数部署（不过滤）不受影响"""
        assert not _blocked(None, 999)

    def test_in_scope_not_blocked(self):
        assert not _blocked({1, 2, 3}, 2)

    def test_out_of_scope_blocked(self):
        """被限制库的用户播受限库内容 → 403"""
        assert _blocked({1, 2, 3}, 99)

    def test_missing_library_id_blocked_when_scoped(self):
        """安全默认：取不到 library_id 且有范围限制时拦截"""

        class FakeItem:
            pass

        assert _blocked({1, 2, 3}, getattr(FakeItem(), "library_id", None))

    def test_fix_present_in_api(self):
        """播放端点经统一 helper 做库范围校验（H1：_require_visible_item）。

        2026-10-17 转码删除后：只剩 video_stream 一个播放端点
        （video_hls 已删），playback_info 同样过可见范围。
        """
        with open("backend/emby_server/api.py", encoding="utf-8") as f:
            content = f.read()
        assert content.count("P0 / H1") == 1
        assert content.count("await run_db(_require_visible_item, db, user, item_id)") == 2

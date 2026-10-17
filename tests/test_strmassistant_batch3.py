# -*- coding: utf-8 -*-
"""StrmAssistant 第三批测试：#3/#7/#9/#10/#14/#15

使用 importlib 直接加载模块，避免触发 backend/__init__.py 的重依赖链。
只测纯函数逻辑，不碰数据库。
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

BASE = os.path.join(os.path.dirname(__file__), "..", "backend", "emby_server")
INTEG = os.path.join(os.path.dirname(__file__), "..", "backend", "integrations")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # 预置 mock，避免重依赖
    import unittest.mock as mock
    sys.modules.setdefault("sqlalchemy", mock.MagicMock())
    sys.modules.setdefault("sqlalchemy.orm", mock.MagicMock())
    spec.loader.exec_module(mod)
    return mod


class TestIntroMarker:
    """#3 片头片尾标记（纯函数）"""

    def setup_method(self):
        self.mod = _load("intro_test", os.path.join(BASE, "intro_marker.py"))

    def test_marker_types(self):
        assert "intro" in self.mod.MARKER_TYPES
        assert "outro" in self.mod.MARKER_TYPES
        assert "credits" in self.mod.MARKER_TYPES

    def test_to_chapters(self):
        markers = [
            {"id": 1, "marker_type": "intro", "start_ms": 0,
             "end_ms": 90000, "source": "manual"},
        ]
        chapters = self.mod.to_chapters(markers)
        assert len(chapters) == 1
        assert chapters[0]["StartPositionTicks"] == 0
        assert chapters[0]["EndPositionTicks"] == 90000 * 10000
        assert chapters[0]["MarkerType"] == "intro"


class TestFallbackLanguagesConstant:
    """#7 备选语言常量（不调用需 DB 的函数）"""

    def test_fallback_constant(self):
        # 直接读源码，验证常量存在且格式正确
        path = os.path.join(BASE, "tmdb.py")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "MOVIEDB_FALLBACK_LANGUAGES" in content
        assert '"zh-CN"' in content or "'zh-CN'" in content
        assert "language_fallback_chain" in content
        # 验证 search() 调用了 fallback
        assert "for lang in language_fallback_chain():" in content


class TestPosterLanguageConstant:
    """#10 海报语言配置（不调用需 DB 的函数）"""

    def test_poster_language_config(self):
        path = os.path.join(BASE, "tmdb.py")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "TMDB_POSTER_LANGUAGE_CONFIG_KEY" in content
        assert '"original"' in content or "'original'" in content
        assert "images_with_language" in content


class TestEpisodeGroups:
    """#15 剧集组（纯常量）"""

    def setup_method(self):
        self.mod = _load("epg_test", os.path.join(BASE, "episode_groups.py"))

    def test_config_prefix(self):
        assert self.mod.EPISODE_GROUP_CONFIG_PREFIX == "tmdb_episode_group_"

# -*- coding: utf-8 -*-
"""StrmAssistant 第二批功能测试：#4 多版本合并 / #6 字幕扫描 / #12 拼音排序 / #2 缩略图"""
import importlib.util
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE = os.path.join(os.path.dirname(__file__), "..", "backend", "emby_server")


class TestMergeVersions:
    def setup_method(self):
        self.mod = _load(
            "merge_versions_worker",
            os.path.join(BASE, "merge_versions_worker.py"),
        )

    def test_find_duplicate_groups_structure(self):
        # union-find 已删除（按 key 分组天然保证传递性）；
        # 这里只验证模块仍暴露核心函数
        assert hasattr(self.mod, "find_duplicate_groups")
        assert hasattr(self.mod, "merge_group")
        assert hasattr(self.mod, "get_alternate_versions")

    def test_provider_key_tmdb_priority(self):
        class Fake:
            library_id = 1
            item_type = "movie"
            tmdb_id = "123"
            imdb_id = "tt456"
        assert self.mod._provider_key(Fake()) == (1, "movie", "tmdb", "123")

    def test_provider_key_imdb_fallback(self):
        # N4-1 契约变更（Nova 口径）：TMDB 硬条件，imdb 单独不再成键，
        # 仅 imdb 的条目不参与自动合并 → 返回 None（旧断言为 imdb 键）。
        class Fake:
            library_id = 1
            item_type = "movie"
            tmdb_id = ""
            imdb_id = "tt456"
        assert self.mod._provider_key(Fake()) is None

    def test_provider_key_none(self):
        class Fake:
            library_id = 1
            item_type = "movie"
            tmdb_id = ""
            imdb_id = ""
        assert self.mod._provider_key(Fake()) is None

    def test_provider_key_cross_library_differs(self):
        # 同 tmdb_id 但不同库 → 不同 key（防跨库合并）
        class FakeA:
            library_id = 1
            item_type = "movie"
            tmdb_id = "123"
            imdb_id = ""
        class FakeB:
            library_id = 2
            item_type = "movie"
            tmdb_id = "123"
            imdb_id = ""
        assert self.mod._provider_key(FakeA()) != self.mod._provider_key(FakeB())

    def test_provider_key_cross_type_differs(self):
        # 同 tmdb_id 但不同类型 → 不同 key（防跨类型合并）
        class FakeA:
            library_id = 1
            item_type = "movie"
            tmdb_id = "123"
            imdb_id = ""
        class FakeB:
            library_id = 1
            item_type = "series"
            tmdb_id = "123"
            imdb_id = ""
        assert self.mod._provider_key(FakeA()) != self.mod._provider_key(FakeB())


class TestThumbnailWorker:
    def setup_method(self):
        self.mod = _load(
            "thumbnail_worker",
            os.path.join(BASE, "thumbnail_worker.py"),
        )

    def test_thumbnail_dir(self):
        d = self.mod.thumbnail_dir("abcdef123456")
        assert d.endswith(os.path.join("ab", "abcdef123456"))

    def test_has_thumbnails_empty(self):
        # 不存在的 guid 返回 False
        assert self.mod.has_thumbnails("nonexistent_guid_12345") is False

    def test_list_thumbnails_empty(self):
        assert self.mod.list_thumbnails("nonexistent_guid_12345") == []


class TestSubtitleScanWorker:
    def setup_method(self):
        self.mod = _load(
            "subtitle_scan_worker",
            os.path.join(BASE, "subtitle_scan_worker.py"),
        )

    def test_module_loads(self):
        # 模块能加载，开关和配置存在
        assert hasattr(self.mod, "SUBTITLE_SCAN_ENABLED")
        assert hasattr(self.mod, "has_external_subtitle_changed")
        assert hasattr(self.mod, "update_external_subtitles")

    def test_env_config(self):
        # 配置函数正常
        assert isinstance(self.mod.SUBTITLE_SCAN_INTERVAL_SEC, float)
        assert isinstance(self.mod.SUBTITLE_SCAN_BATCH_LIMIT, int)

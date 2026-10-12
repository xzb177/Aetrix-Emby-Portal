"""边缘缓存通用能力测试：EDGE_CACHE_MODE 环境变量三档"""
import os
from unittest import mock

from backend.emby_server import edge_cache


class TestEdgeCacheMode:
    def test_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(edge_cache.ENV_EDGE_CACHE_MODE, None)
            assert edge_cache.mode() == "off"

    def test_off_explicit(self):
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "off"}):
            assert edge_cache.mode() == "off"

    def test_query_ignore(self):
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "query_ignore"}):
            assert edge_cache.mode() == "query_ignore"

    def test_query_ignore_alias_dash(self):
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "query-ignore"}):
            assert edge_cache.mode() == "query_ignore"

    def test_header_reserved(self):
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "header"}):
            assert edge_cache.mode() == "header"

    def test_invalid_falls_back_to_off(self):
        # 配错了也不开缓存声明（fail-closed）
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "bogus"}):
            assert edge_cache.mode() == "off"

    def test_case_insensitive(self):
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "QUERY_IGNORE"}):
            assert edge_cache.mode() == "query_ignore"

    def test_hot_read(self):
        # 热读：改环境变量即刻生效，不用重启
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "off"}):
            assert edge_cache.mode() == "off"
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "query_ignore"}):
            assert edge_cache.mode() == "query_ignore"


class TestDebugHeader:
    def test_off_no_header(self):
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "off"}):
            assert edge_cache.debug_header_value() is None

    def test_query_ignore_has_header(self):
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "query_ignore"}):
            assert edge_cache.debug_header_value() == "query-ignore"

    def test_header_mode_no_debug_header(self):
        # header 是预留位，本期不改变行为
        with mock.patch.dict(os.environ, {edge_cache.ENV_EDGE_CACHE_MODE: "header"}):
            assert edge_cache.debug_header_value() is None

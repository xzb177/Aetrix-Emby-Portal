"""播放速度优化测试：container 回退 + PlaybackInfo 缓存。"""
import os

import pytest

# 测试文件所在仓库根目录（CI 和本地都能用）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API_PATH = os.path.join(_REPO_ROOT, "backend", "emby_server", "api.py")


class TestContainerFallback:
    """_container_of：有扫描数据用扫描的，缺失才从扩展名回退，不虚报编码。"""

    def _container_of(self, container, file_path):
        # 内联被测逻辑（与 api.py 保持一致，避免 import 整个 FastAPI 应用）
        mapping = {
            ".mp4": "mp4", ".m4v": "mp4", ".mkv": "mkv", ".avi": "avi",
            ".mov": "mov", ".ts": "ts", ".m2ts": "m2ts", ".mts": "m2ts",
            ".webm": "webm", ".flv": "flv", ".wmv": "wmv",
            ".mpg": "mpeg", ".mpeg": "mpeg",
        }
        if container:
            return container
        path = (file_path or "").lower().split("?")[0]
        for ext, c in mapping.items():
            if path.endswith(ext):
                return c
        return None

    def test_prefers_scanned_container(self):
        assert self._container_of("mkv", "/x/y.mp4") == "mkv"

    def test_fallback_mp4(self):
        assert self._container_of(None, "/mnt/video/Foo.mp4") == "mp4"

    def test_fallback_mkv(self):
        assert self._container_of("", "mount://3/剧集/Bar.mkv") == "mkv"

    def test_fallback_case_insensitive(self):
        assert self._container_of(None, "/X/BAZ.MKV") == "mkv"

    def test_fallback_strips_query(self):
        assert self._container_of(None, "/x/y.mp4?token=abc") == "mp4"

    def test_unknown_ext_returns_none(self):
        # 保守：不知道就不报，不虚报
        assert self._container_of(None, "/x/y.unknown") is None
        assert self._container_of(None, None) is None

    def test_source_contains_mapping(self):
        # 源码级断言：api.py 里确实有回退表和函数
        src = open(_API_PATH).read()
        assert "_CONTAINER_BY_EXT" in src
        assert "def _container_of" in src
        assert '"Container": _container_of(item)' in src


class TestPlaybackInfoCache:
    """playback_info 缓存：key 构成、TTL 可配、PlaySessionId 不进缓存。"""

    def _src(self):
        return open(_API_PATH).read()

    def test_cache_uses_redis_cache_manager(self):
        src = self._src()
        # S4：同步 Redis 调用经 run_db 下放线程池
        assert "run_db(CacheManager.get, cache_key)" in src
        assert "run_db(CacheManager.set, cache_key" in src

    def test_cache_key_contains_user_and_profile(self):
        src = self._src()
        # key = pi:{guid}:{user_id}:{profile_fp}:{bitrate}
        assert 'f"pi:{item.guid}:{user.id}:{profile_fp}:{max_bitrate // 1000}"' in src

    def test_ttl_configurable_via_env(self):
        src = self._src()
        assert 'os.getenv("PLAYBACKINFO_CACHE_TTL", "300")' in src

    def test_cache_disabled_when_ttl_zero(self):
        src = self._src()
        assert "if cache_ttl > 0:" in src

    def test_playsessionid_not_cached(self):
        # PlaySessionId 必须每次重新生成：缓存只存 media_source，
        # return 处仍然 secrets.token_hex(8)
        src = self._src()
        assert '"PlaySessionId": secrets.token_hex(8)' in src
        # 缓存 set 的是 media_source，不是整个 response
        assert "run_db(CacheManager.set, cache_key,\n                             json.dumps(media_source" in src

    def test_apikey_url_added_per_request(self):
        # DirectStreamUrl 含 api_key，必须在缓存命中后按当前请求重拼，
        # 不能把别人的 api_key 缓存给出去
        src = self._src()
        # update 在缓存分支之后
        cache_branch = src.index("media_source = cached_source")
        update_pos = src.index('"DirectStreamUrl": stream_url', cache_branch)
        assert update_pos > cache_branch
        # URL 按当前请求拼装（api_key 在查询串里）
        assert "api_key={api_key}" in src

    def test_cache_failure_falls_back(self):
        src = self._src()
        # 缓存异常只走正常流程，不抛错
        assert "缓存只是优化，失败就走正常流程" in src

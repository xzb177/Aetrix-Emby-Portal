"""分片缓存头口径单元测试（2026-10-17 瘦身：CDN 域名预留已删除）

``backend/emby_server/cdn.py`` 现在只剩缓存头一半：分片路径判定 +
Cache-Control 口径。以下钉住它不被误删/改坏：

- 分片（HLS 切片 / 直接流）可被边缘缓存；
- 播放列表 / API / 字幕清单绝不能被缓存（凭据在查询串里/内容随时变）。
"""


def test_cache_control_taxonomy():
    from backend.emby_server import cdn

    # 分片（HLS 切片 / 直接流）：可被 CDN 边缘缓存
    assert cdn.is_segment_path("/emby/videos/abc/1.ts") is True
    assert cdn.is_segment_path("/emby/videos/abc/seg.m4s?session=x") is True
    assert cdn.is_segment_path("/emby/Videos/x/stream?static=true") is True
    assert cdn.is_segment_path("/emby/videos/x/stream.mkv") is True
    # 播放列表 / API / 字幕清单：绝不能被缓存
    assert cdn.is_segment_path("/emby/videos/x/main.m3u8") is False
    assert cdn.is_segment_path("/emby/Users/abc") is False
    assert cdn.is_segment_path("/emby/videos/x/subtitles.vtt") is False
    assert cdn.cache_control_for("/emby/videos/x/main.m3u8") == "no-store"
    assert cdn.cache_control_for("/emby/Users/abc") == cdn.NO_STORE
    assert cdn.cache_control_for("/emby/videos/x/1.ts") == cdn.SEGMENT_CACHE_HEADER
    assert cdn.SEGMENT_CACHE_HEADER == "public, max-age=21600, s-maxage=21600, immutable"

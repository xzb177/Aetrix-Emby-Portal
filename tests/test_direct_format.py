"""直链格式一致化（2026-10-27）：严格播放器按 MIME/后缀认格式。

背景：#1353 上线直链优先后，服务端 play_start 全是 direct_302 ok，
但用户手机播放器报 Invalid media format。已实测缺口：PlaybackInfo
下发的 /stream 地址无容器后缀、最终谷歌 usercontent 地址无后缀且
MIME 为 application/octet-stream + nosniff——申报 SupportsDirectPlay=True
与 container=mkv，却给了一个按 MIME/后缀认不出来的地址。

本文件只测“地址层”：后缀判定纯函数 + 扩展名路由注册。后者复用
test_stream_ext_routes 的路由表口径（扩展名路由须在通配路由之前）。
最终 302 目标的 MIME 由 alt=media 层保证：alt=media + Bearer 头实测回
206 video/x-matroska；token 放 URL 查询参数时谷歌回 403（生产探针
实测），所以凭据只能走 RequiredHttpHeaders，不能放 URL。
"""

from backend.emby_server import api


def test_direct_stream_suffix_known_containers():
    assert api._direct_stream_suffix("mkv") == ".mkv"
    assert api._direct_stream_suffix("mp4") == ".mp4"
    assert api._direct_stream_suffix("avi") == ".avi"
    assert api._direct_stream_suffix("webm") == ".webm"
    assert api._direct_stream_suffix("m2ts") == ".m2ts"


def test_direct_stream_suffix_case_and_unknown():
    assert api._direct_stream_suffix("MKV") == ".mkv"
    assert api._direct_stream_suffix(" Mp4 ") == ".mp4"
    # iso 不是流式播放容器、未知容器一律不加后缀（保持旧地址，不造坏路由）
    assert api._direct_stream_suffix("iso") == ""
    assert api._direct_stream_suffix("xyz") == ""
    assert api._direct_stream_suffix(None) == ""
    assert api._direct_stream_suffix("") == ""


def test_direct_stream_suffix_never_injects_path():
    # 容器名来自扫描数据：含路径分隔符/查询符时绝不能拼进 URL
    assert api._direct_stream_suffix("mkv/../x") == ""
    assert api._direct_stream_suffix("mkv?x=1") == ""


def test_all_suffix_routes_registered_before_catch_all():
    paths = [getattr(r, "path", "") for r in api.emby_router.routes]
    for ext in sorted(api._DIRECT_STREAM_EXTS):
        for prefix in ("/emby/videos/", "/emby/Videos/"):
            route = f"{prefix}{{item_id}}/stream.{ext}"
            assert route in paths, f"路由缺失: {route}"
    catch_all = "/emby/videos/{item_id}/{transcode_path:path}"
    if catch_all in paths:
        for ext in sorted(api._DIRECT_STREAM_EXTS):
            assert paths.index(f"/emby/videos/{{item_id}}/stream.{ext}") < paths.index(
                catch_all
            )


def test_alt_media_url_shape_no_token_in_url():
    # alt=media 地址本身绝不带 token（凭据走 RequiredHttpHeaders）；
    # token 放 URL 时谷歌回 403，已由生产探针实测钉死，故这里断言无 token。
    url = api._drive_alt_media_url("abc123")
    assert url == (
        "https://www.googleapis.com/drive/v3/files/abc123"
        "?alt=media&supportsAllDrives=true"
    )
    assert "access_token" not in url
    assert "ya29" not in url


def test_apply_alt_media_direct_replaces_urls_and_headers():
    src = {
        "DirectPlayUrl": "http://x/emby/Videos/g/stream.mkv",
        "DirectStreamUrl": "http://x/emby/Videos/g/stream.mkv",
        "RequiredHttpHeaders": {},
    }
    assert api._apply_alt_media_direct(src, "abc123", "ya29.test") is True
    assert src["DirectPlayUrl"] == (
        "https://www.googleapis.com/drive/v3/files/abc123"
        "?alt=media&supportsAllDrives=true"
    )
    assert src["DirectStreamUrl"] == src["DirectPlayUrl"]
    assert src["RequiredHttpHeaders"] == {"Authorization": "Bearer ya29.test"}


def test_apply_alt_media_direct_missing_fid_or_token_keeps_old():
    src = {
        "DirectPlayUrl": "http://x/emby/Videos/g/stream.mkv",
        "DirectStreamUrl": "http://x/emby/Videos/g/stream.mkv",
        "RequiredHttpHeaders": {},
    }
    assert api._apply_alt_media_direct(src, "", "ya29.test") is False
    assert api._apply_alt_media_direct(src, "abc123", "") is False
    assert api._apply_alt_media_direct(src, "abc123", None) is False
    assert api._apply_alt_media_direct(src, None, "ya29.test") is False
    # 旧地址与空头原样保留（调用方走 /stream 旧链路，不造半截地址）
    assert src["DirectPlayUrl"] == "http://x/emby/Videos/g/stream.mkv"
    assert src["RequiredHttpHeaders"] == {}

"""客户端「媒体信息」页与「链接」区的字段完整性。

对照其它 Emby 服务端发现我们的详情页很空：视频只有编码/码率几行，没有
帧率、动态范围、Profile、Level、位深、像素格式、画面比例；音频没有采样率、
位深、声道布局；链接区只有一个来源。

根因是两层都在丢字段：``probe_metadata`` 没从 ffprobe 取，``_stream_dto``
也没往外发。这次两层一起补。
"""
from types import SimpleNamespace

# 运行期导入：顶层 import 会让 backend.emby_server.* 在别的测试建库之前被 import，
# 抢走 SessionLocal 绑定，污染同批次的其它测试（与 test_enrich_container_items 同因）。
import importlib


def _api():
    return importlib.import_module("backend.emby_server.api")


def _scanner():
    return importlib.import_module("backend.emby_server.scanner")


def _ffprobe_video():
    return {
        "format": {"duration": "1200.0", "bit_rate": "8000000", "size": "1200000000"},
        "streams": [{
            "index": 0, "codec_type": "video", "codec_name": "h264",
            "width": 1920, "height": 1080, "profile": "High", "level": 40,
            "pix_fmt": "yuv420p10le", "avg_frame_rate": "29.970003",
            "r_frame_rate": "29.970003", "bit_rate": "6202000",
            "color_transfer": "smpte2084", "color_primaries": "bt2020",
            "sample_aspect_ratio": "1:1", "bits_per_raw_sample": "10",
            "tags": {"language": "jpn", "title": "1080p H264"},
        }, {
            "index": 1, "codec_type": "audio", "codec_name": "aac",
            "channels": 2, "channel_layout": "stereo", "sample_rate": "48000",
            "sample_fmt": "fltp", "bit_rate": "187000",
            "bits_per_sample": "16", "tags": {"language": "jpn"},
        }],
    }


def test_probe_extracts_video_detail_fields(monkeypatch):
    sc = _scanner()
    monkeypatch.setattr(sc, "_ffprobe", lambda *a, **k: _ffprobe_video())
    monkeypatch.setattr(sc, "_mediainfo", lambda *a, **k: None)
    info = _scanner().probe_metadata("/tmp/x.mkv", size=1200000000)
    v = info["streams"][0]
    assert v["frame_rate"] == "29.970003", v
    assert v["video_range"] == "HDR10", v
    assert v["profile"] == "High", v
    assert v["level"] == "40", v
    assert v["pixel_format"] == "yuv420p10le", v
    assert v["bit_depth"] == 10, v
    a = info["streams"][1]
    assert a["sample_rate"] == 48000, a
    assert a["channel_layout"] == "stereo", a
    assert a["bit_depth"] == 16, a


def test_detect_video_range_variants():
    assert _scanner()._detect_video_range({"color_transfer": "smpte2084"}, {}) == "HDR10"
    assert _scanner()._detect_video_range({"color_transfer": "arib-std-b67"}, {}) == "HLG"
    assert _scanner()._detect_video_range({}, {"DOVI": "Dolby Vision Profile 5"}) == "DolbyVision"
    assert _scanner()._detect_video_range({}, {}) == "SDR"


def test_stream_dto_sends_detail_fields():
    s = SimpleNamespace(
        stream_index=0, stream_type="Video", codec="h264", language="jpn",
        display_title="1080p H264", title=None, is_default=False, is_forced=False,
        is_external=False, channels=None, bit_rate=6202000,
        frame_rate="29.970003", video_range="HDR10", profile="High", level="40",
        pixel_format="yuv420p10le", aspect_ratio="1:1", bit_depth=10,
        sample_rate=None, channel_layout=None, sample_format=None,
    )
    item = SimpleNamespace(bitrate=6202000, width=1920, height=1080, guid="g" * 32)
    dto = _api()._stream_dto(s, "http://t", item, "")
    for key in ("FrameRate", "VideoRange", "Profile", "Level",
                "PixelFormat", "AspectRatio", "BitDepth"):
        assert key in dto, f"{key} 未发送：{dto}"
    assert dto["VideoRange"] == "HDR10"


def test_stream_dto_computes_aspect_ratio_when_missing():
    """没有 stream 里的画面比例时，用分辨率算一个（客户端要显示这一行）"""
    s = SimpleNamespace(
        stream_index=0, stream_type="Video", codec="h264", language=None,
        display_title=None, title=None, is_default=False, is_forced=False,
        is_external=False, channels=None, bit_rate=0,
        frame_rate=None, video_range="SDR", profile=None, level=None,
        pixel_format=None, aspect_ratio=None, bit_depth=None,
        sample_rate=None, channel_layout=None, sample_format=None,
    )
    item = SimpleNamespace(bitrate=0, width=1920, height=1080, guid="g" * 32)
    dto = _api()._stream_dto(s, "http://t", item, "")
    assert dto["AspectRatio"] == "16:9", dto


def test_external_urls_from_tmdb_and_imdb():
    item = SimpleNamespace(
        name="剧名", production_year=2020, item_type="series",
        tmdb_id="12345", imdb_id="tt678", douban_id=None, bangumi_id=None,
        metadata_source="tmdb",
    )
    urls = _api()._external_urls(item)
    names = {u["Name"] for u in urls}
    assert {"TMDB", "IMDb"} <= names, urls
    tmdb = next(u for u in urls if u["Name"] == "TMDB")
    assert "/tv/12345" in tmdb["Url"], tmdb


def test_external_urls_empty_when_no_ids():
    item = SimpleNamespace(
        name="剧名", production_year=None, item_type="movie",
        tmdb_id=None, imdb_id=None, douban_id=None, bangumi_id=None,
        metadata_source=None,
    )
    assert _api()._external_urls(item) == []


def _stream_ns(**over):
    base = dict(
        stream_index=0, stream_type="Video", codec="h264", language="jpn",
        display_title=None, title=None, is_default=False, is_forced=False,
        is_external=False, channels=None, bit_rate=6202000,
        frame_rate=None, video_range="SDR", profile=None, level=None,
        pixel_format=None, aspect_ratio=None, bit_depth=None,
        sample_rate=None, channel_layout=None, sample_format=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_stream_dto_numeric_fields_are_numbers_not_strings():
    """回归：Hills（Dart）报 type 'String' is not a subtype of type 'num?'。

    frame_rate/level 在库里存字符串（ffprobe 原始值 / str(level)），但 Emby
    协议里 FrameRate/Level 是数字，Dart 客户端按 num? 强转拿到字符串整页崩。
    _stream_dto 下发时必须转成 JSON 数字。
    """
    item = SimpleNamespace(bitrate=6202000, width=1920, height=1080, guid="g" * 32)
    dto = _api()._stream_dto(
        _stream_ns(frame_rate="30000/1001", level="51"), "http://t", item, "")
    assert isinstance(dto["FrameRate"], float) and dto["FrameRate"] == 29.97, dto
    assert isinstance(dto["Level"], int) and dto["Level"] == 51, dto

    dto = _api()._stream_dto(
        _stream_ns(frame_rate="29.970003", level="40"), "http://t", item, "")
    assert dto["FrameRate"] == 29.97 and not isinstance(dto["FrameRate"], str), dto
    assert dto["Level"] == 40, dto


def test_stream_dto_omits_unparseable_numeric_fields():
    """转不动的脏值宁可不发，也不能发字符串把强类型客户端打崩。"""
    item = SimpleNamespace(bitrate=0, width=1920, height=1080, guid="g" * 32)
    dto = _api()._stream_dto(
        _stream_ns(frame_rate="N/A", level=""), "http://t", item, "")
    assert "FrameRate" not in dto and "Level" not in dto, dto

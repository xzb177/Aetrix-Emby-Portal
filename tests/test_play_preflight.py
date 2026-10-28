# -*- coding: utf-8 -*-
"""播放前体检（第二批第 2 单）测试。

只测纯预检函数，不发 HTTP、不碰数据库、不下发播放链接、不改动
变体清单。本地 Python 3.12 + pyshim 口径，全量以 GitHub CI
（Python 3.11）为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import play_preflight as pf


def _streams(video_range="SDR", audio=True, audio_default=True):
    streams = [{"stream_type": "Video", "codec": "h264",
                "video_range": video_range, "is_default": True}]
    if audio:
        streams.append({"stream_type": "Audio", "codec": "aac",
                        "video_range": "", "is_default": audio_default})
    return streams


def test_ready_direct_selected_first_in_list_order():
    results = pf.preflight_all(
        ["drive_direct", "drive_relay"], container="mkv",
        video_codec="h264", audio_codec="aac",
        streams=_streams())
    assert [r.verdict for r in results] == ["ready", "ready"]
    selected = pf.select_best_direct(results)
    assert selected is not None and selected.variant == "drive_direct"
    assert selected.fallback_variant is None


def test_format_mismatch_blocked_409_and_never_falls_back_to_relay():
    result = pf.preflight_variant(
        "drive_direct", ["drive_direct", "drive_relay"], container="iso",
        video_codec="h264", audio_codec="aac", streams=_streams())
    assert result.verdict == "blocked"
    assert result.error_status == 409
    assert any("container_not_streamable" in f for f in result.failures)
    assert result.fallback_variant is None
    # 选直链时：直链被挡 → 不静默回落中继，返回 None。
    results = pf.preflight_all(
        ["drive_direct", "drive_relay"], container="iso",
        video_codec="h264", audio_codec="aac", streams=_streams())
    assert pf.select_best_direct(results) is None


def test_unknown_variant_404_and_unavailable_variant_409():
    unknown = pf.preflight_variant(
        "no_such_variant", ["drive_direct"], container="mkv",
        streams=_streams())
    assert unknown.verdict == "blocked" and unknown.error_status == 404
    unavailable = pf.preflight_variant(
        "drive_relay", ["drive_direct"], container="mkv",
        streams=_streams())
    assert unavailable.verdict == "blocked" and unavailable.error_status == 409


def test_missing_hdr_and_audio_degrade_but_do_not_block():
    # HDR 未申报 + 音轨未申报 → degraded，仍可选直链（降级策略）。
    result = pf.preflight_variant(
        "drive_direct", ["drive_direct"], container="mp4",
        video_codec="h265", audio_codec=None, streams=[])
    assert result.verdict == "degraded"
    assert result.error_status is None
    assert any("video_stream_undeclared" in w for w in result.warnings)
    assert any("audio_undeclared" in w for w in result.warnings)
    selected = pf.select_best_direct([result])
    assert selected is not None and selected.variant == "drive_direct"


def test_missing_container_and_codec_degrade_not_fail():
    result = pf.preflight_variant(
        "drive_direct", ["drive_direct"], container=None,
        video_codec=None, audio_codec="aac",
        streams=[{"stream_type": "Video", "codec": "h264",
                  "video_range": "HDR10", "is_default": True}])
    assert result.verdict == "degraded"
    assert result.failures == ()
    assert any("container_undeclared" in w for w in result.warnings)
    assert any("video_codec_undeclared" in w for w in result.warnings)


def test_audio_without_default_still_passes_via_first_track_fallback():
    result = pf.preflight_variant(
        "drive_direct", ["drive_direct"], container="mp4",
        video_codec="h264", audio_codec="aac",
        streams=_streams(audio_default=False))
    assert result.verdict == "ready"
    assert result.warnings == ()


def test_local_file_skips_streamable_container_check():
    result = pf.preflight_variant(
        "local_file", ["local_file"], container="iso",
        video_codec="h264", audio_codec="aac", streams=_streams())
    assert result.verdict == "ready"
    # 但选直链时本机文件变体永远不被选中（不是直链）。
    assert pf.select_best_direct([result]) is None


def test_preflight_does_not_mutate_inputs():
    available = ["drive_direct", "drive_relay"]
    streams = _streams()
    before = (list(available), [dict(s) for s in streams])
    pf.preflight_all(available, container="mkv", video_codec="h264",
                     audio_codec="aac", streams=streams)
    assert available == before[0] and streams == before[1]


def test_switch_off_by_default(monkeypatch):
    monkeypatch.delenv("PLAY_PREFLIGHT_ENABLED", raising=False)
    assert pf.preflight_enabled() is False
    monkeypatch.setenv("PLAY_PREFLIGHT_ENABLED", "1")
    assert pf.preflight_enabled() is True


def test_result_dict_contains_no_play_url():
    result = pf.preflight_variant(
        "drive_direct", ["drive_direct"], container="mp4",
        video_codec="h264", audio_codec="aac", streams=_streams())
    body = pf.result_to_dict(result)
    assert "url" not in str(body).lower().replace("fallback_variant", "")
    assert body["fallback_variant"] is None


# ---- R4 审查修复回归：容器别名归一 ----

def test_container_aliases_map_to_streamable_names():
    # 扫描器把 container 存为原始扩展名；m4v/mts/mpg 是白名单容器
    # mp4/m2ts/mpeg 的别名（api._CONTAINER_BY_EXT 同款映射），
    # 体检不得把它们误判为不可流式而 blocked。
    for alias in ("m4v", "mts", "mpg"):
        result = pf.preflight_variant(
            "drive_direct", ["drive_direct"], container=alias,
            video_codec="h264", audio_codec="aac", streams=_streams())
        assert result.verdict in ("ready", "degraded"), alias
        assert not result.failures, alias
    # 真不可流式（iso）仍 blocked，不变。
    result = pf.preflight_variant(
        "drive_direct", ["drive_direct"], container="iso",
        video_codec="h264", audio_codec="aac", streams=_streams())
    assert result.verdict == "blocked"

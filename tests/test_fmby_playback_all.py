# -*- coding: utf-8 -*-
"""FMBY 播放机制形状全部学（用户 2026-10-27 亲令“全部学”）的钉死测试。

只测机制形状，不测 FMBY 本体（闭源、无源码可抄）：
1. 清单与取流分离：描述子不带任何 URL 键；
2. 就绪态分级：failed/preparing/error/ready/unavailable 五态；
3. 稳定 id：高度优先、标签 slug 兜底、冲突 -2/-3；
4. 选错硬报错：未知 404、可用清单外 409、清单内放行（不静默改走）；
5. 核心换线一次：502 与轮换后 403 可换线，其余不换线；
6. 交付形态：302 直链 vs 令牌流分清、Range 206 对齐纯函数。

全部离线纯函数测试，不碰 DB、不碰网络。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import play_variants, playback_delivery, variant_switch


def _variants():
    return [
        play_variants.Variant("drive_relay", "Drive 中继", "Http", False, True),
        play_variants.Variant("drive_direct", "Drive 直链", "Http", True, False),
    ]


# --- 1. 清单描述子：不带地址、便宜、可缓存 -------------------------------


def test_descriptors_carry_no_url_keys():
    descs = play_variants.build_variant_descriptors(_variants())
    assert len(descs) == 2
    for d in descs:
        assert not any("url" in k.lower() for k in d), d
        assert set(d) == {
            "stable_id", "kind", "label", "is_default",
            "requires_transfer", "readiness",
        }


def test_descriptors_default_is_first_only():
    descs = play_variants.build_variant_descriptors(_variants())
    assert descs[0]["is_default"] is True
    assert descs[1]["is_default"] is False
    assert descs[0]["kind"] == "drive_relay"
    assert descs[1]["kind"] == "drive_direct"


def test_descriptors_preparing_keeps_variant_listed():
    # 转存未完成时变体仍在清单里、处于准备中态，不是删掉。
    descs = play_variants.build_variant_descriptors(
        _variants(), transfer_verdict="preparing", requires_transfer=True)
    assert len(descs) == 2
    assert all(d["readiness"] == "preparing" for d in descs)
    assert all(d["requires_transfer"] is True for d in descs)


# --- 2. 就绪态分级 --------------------------------------------------------


def test_compute_readiness_matrix():
    assert play_variants.compute_readiness(True, "failed") == "failed"
    assert play_variants.compute_readiness(True, "preparing") == "preparing"
    assert play_variants.compute_readiness(True, "error") == "unavailable"
    assert play_variants.compute_readiness(True, "proceed") == "ready"
    assert play_variants.compute_readiness(True, "serve_115_copy") == "ready"
    assert play_variants.compute_readiness(True, None) == "ready"
    assert play_variants.compute_readiness(False, None) == "unavailable"
    assert play_variants.compute_readiness(False, "proceed") == "unavailable"


def test_apply_transfer_status_keeps_variant_and_sets_readiness():
    sources = [{"Id": "g:drive_direct", "DirectPlayUrl": "http://x/stream",
                "SupportsDirectPlay": True, "SupportsDirectStream": True}]
    play_variants.apply_transfer_status(sources, "preparing")
    # 保留在清单里（没删条），但点不了、就绪态 preparing。
    assert len(sources) == 1
    assert sources[0]["Readiness"] == "preparing"
    assert sources[0]["SupportsDirectPlay"] is False
    assert "DirectPlayUrl" not in sources[0]
    play_variants.apply_transfer_status(sources, "failed")
    assert sources[0]["Readiness"] == "failed"


def test_build_media_sources_default_readiness_ready():
    base = {"Name": "片名"}
    out = play_variants.build_media_sources(
        base, _variants(), guid="g", stream_url="http://x/stream",
        direct_stream_ok=True)
    assert all(s["Readiness"] == "ready" for s in out)
    assert out[0]["Id"] == "g:drive_relay"
    assert out[1]["Id"] == "g:drive_direct"


# --- 3. 稳定 id -----------------------------------------------------------


def test_stable_variant_id_height_first():
    assert play_variants.stable_variant_id("drive_direct", 1080, "Drive 直链") == "drive_direct-h1080"
    assert play_variants.stable_variant_id("drive_direct", None, "Drive 直链") == "drive-直链"
    assert play_variants.stable_variant_id("drive_direct", None, "") == "drive_direct"


def test_assign_stable_variant_ids_conflict_suffix():
    ids = play_variants.assign_stable_variant_ids([
        ("drive_direct", None, "Drive 直链"),
        ("drive_direct", None, "Drive 直链"),
        ("drive_direct", None, "Drive 直链"),
        ("drive_relay", 720, "Drive 中继"),
    ])
    assert ids == ["drive-直链", "drive-直链-2", "drive-直链-3", "drive_relay-h720"]


# --- 4. 选错硬报错 --------------------------------------------------------


def test_resolve_requested_variant_hard_errors():
    # 未知变体 → 404；已知但清单外 → 409；清单内 → 放行；不静默改走。
    assert play_variants.resolve_requested_variant(
        {"variant": "no_such"}, ["drive_direct"]) == (None, 404)
    assert play_variants.resolve_requested_variant(
        {"variant": "drive_relay"}, ["drive_direct"]) == (None, 409)
    assert play_variants.resolve_requested_variant(
        {"variant": "drive_direct"}, ["drive_direct"]) == ("drive_direct", None)
    assert play_variants.resolve_requested_variant(
        {"variant": "drive_direct", "MediaSourceId": "g:drive_relay"},
        ["drive_direct", "drive_relay"]) == (None, 404)


# --- 5. 核心换线一次 ------------------------------------------------------


def test_switch_eligible_502_and_403_after_rotation_only():
    assert variant_switch.is_switch_eligible_relay_status(502) is True
    assert variant_switch.is_switch_eligible_relay_status(403) is True
    for status in (401, 404, 429, 500, 503):
        assert variant_switch.is_switch_eligible_relay_status(status) is False
    assert variant_switch.MAX_FALLBACKS == 1


# --- 6. 交付形态与 Range --------------------------------------------------


def test_delivery_mode_mapping():
    assert playback_delivery.delivery_mode_for_variant("drive_direct") == "direct_302"
    assert playback_delivery.delivery_mode_for_variant("byos115_direct") == "direct_302"
    assert playback_delivery.delivery_mode_for_variant("drive_relay") == "token_stream"
    assert playback_delivery.delivery_mode_for_variant("mount_relay") == "token_stream"
    assert playback_delivery.delivery_mode_for_variant("byos_drive_relay") == "token_stream"
    assert playback_delivery.delivery_mode_for_variant("local_file") == "token_stream"
    # 未知变体 fail-closed：走令牌流，不当直链下发。
    assert playback_delivery.delivery_mode_for_variant("no_such") == "token_stream"


def test_parse_range_header_matrix():
    p = playback_delivery.parse_range_header
    assert p("bytes=100-199", 1000) == (100, 199)
    assert p("bytes=100-", 1000) == (100, 999)
    assert p("bytes=-500", 1000) == (500, 999)
    assert p("bytes=0-9999", 1000) == (0, 999)
    assert p(None, 1000) is None
    assert p("bytes=1000-2000", 1000) is None
    assert p("bytes=200-100", 1000) is None
    assert p("bytes=0-100,200-300", 1000) is None
    assert p("items=0-100", 1000) is None


def test_content_range_header():
    assert playback_delivery.content_range_header(100, 199, 1000) == "bytes 100-199/1000"

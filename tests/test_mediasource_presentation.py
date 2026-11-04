# -*- coding: utf-8 -*-
"""播放源呈现口径（站主 2026-11-02 亲定）回归。

背景：第三方播放器里别家源卡显示「Dolby＋17.4 GB＋19.02Mbps」并按
分辨率进 4K 页签，我方源卡只剩「文件」二字、无大小无码率、落进
「其他」页签。钉死的新口径：

1. MediaSource.Name 一律纯画质标签（分辨率＋HDR/杜比组合，如
   「4K Dolby」「1080P HDR」），不带片名/源名/服务器名；探测
   （宽高/video_range）优先、文件名解析补缺；判不出回落条目名
   （Emby 默认形态），不许编造。
2. 本地条目 Size/Bitrate/MediaStreams 从真实探测数据下发；
   无探测时除文件名画质标外留空（补探测走既有按需入队）。
3. CMS 托管条目：每条可展示映射一条 MediaSource（Id 带映射
   id），画质取自取流路径回填的缓存（m3u8 主清单/备注标记）；
   取流口按 MediaSourceId 定向解析所选映射，选错硬报错 404，
   绝不偷偷换线；旧形态（只到 :upstream）保持最佳映射口径。
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.database import CacheManager, SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import quality_label as ql
from backend.emby_server import upstream_playback as up
from backend.security import hash_password

init_db()

TICKS = 10_000_000
PW = "MsPres#2026"
BOUND = "mspres_bound"
LIB_LOCAL = "画质呈现测试库"
LIB_CMS = "画质测试在线库"
SRC_A = "画质源甲"
SRC_B = "画质源乙"
CMS_MOVIE = "画质托管片"


# ---------------------------------------------------------------------------
# 纯函数：画质标签与 m3u8 主清单解析
# ---------------------------------------------------------------------------

def test_quality_label_from_probe_values():
    assert ql.label_for(3840, 2160, "HDR10") == "4K HDR"
    assert ql.label_for(3840, 2160, "DolbyVision") == "4K Dolby"
    assert ql.label_for(1920, 1080, "SDR") == "1080P"
    assert ql.label_for(1280, 720, None) == "720P"
    assert ql.label_for(0, 0, "DolbyVision") == "Dolby"
    assert ql.label_for(0, 0, "SDR") is None
    assert ql.label_for(0, 0, None) is None


def test_quality_label_filename_fallback_and_no_guessing():
    assert ql.label_for(0, 0, None,
                        "某片.2024.2160p.Dolby.Vision.WEB-DL.mkv") == "4K Dolby"
    assert ql.label_for(0, 0, None, "某片.1080p.x264.mkv") == "1080P"
    # DVDRip 不是杜比：裸 DV 只在独立成词时才算标记
    assert ql.label_for(0, 0, None, "某片.DVDRip.mkv") is None
    # 探测与文件名分量独立：探测有分辨率时不因文件名没标记丢范围
    assert ql.label_for(3840, 2160, None, "某片.HDR.mkv") == "4K HDR"
    # 文件名没数据不许猜
    assert ql.label_for(0, 0, None, "plain.mkv") is None


def test_quality_label_from_cms_text_marks():
    assert ql.label_from_text("4K") == "4K"
    assert ql.label_from_text("BD1080P") == "1080P"
    assert ql.label_from_text("杜比视界 2160P") == "4K Dolby"
    # 裸 HD 语义含糊，不猜
    assert ql.label_from_text("HD") is None
    assert ql.label_from_text("更新至第12集") is None
    assert ql.label_from_text("") is None


def test_parse_m3u8_master_picks_top_bandwidth():
    text = (
        "#EXTM3U\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,'
        'CODECS="avc1.64001f,mp4a.40.2"\n'
        "720.m3u8\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=18500000,RESOLUTION=3840x2160,'
        'CODECS="dvhe.08.06,mp4a.40.2"\n'
        "2160.m3u8\n"
    )
    master = ql.parse_m3u8_master(text)
    assert master == {"width": 3840, "height": 2160,
                      "bandwidth": 18500000, "video_range": "DolbyVision"}
    assert ql.label_for_master(master) == "4K Dolby"
    # 单码率清单没有变体行 → 判不出，不猜
    assert ql.parse_m3u8_master("#EXTM3U\n#EXTINF:10,\nseg.ts\n") is None
    assert ql.parse_m3u8_master("") is None


# ---------------------------------------------------------------------------
# TestClient 种子（本地条目 + CMS 托管条目同库盘）
# ---------------------------------------------------------------------------

def _guid():
    return uuid.uuid4().hex


def _cleanup(db):
    for lib_name in (LIB_LOCAL, LIB_CMS):
        for lib in db.query(em.Library).filter(
                em.Library.name == lib_name).all():
            ids = [r[0] for r in db.query(em.MediaItem.id).filter(
                em.MediaItem.library_id == lib.id).all()]
            if ids:
                db.query(em.UpstreamItemMapping).filter(
                    em.UpstreamItemMapping.local_item_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(em.MediaStream).filter(
                    em.MediaStream.item_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(em.MediaItem).filter(
                    em.MediaItem.id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(em.Library).filter(
                em.Library.id == lib.id).delete(synchronize_session=False)
    for src in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name.in_([SRC_A, SRC_B])).all():
        db.delete(src)
    u = db.query(models.WebUser).filter(
        models.WebUser.username == BOUND).first()
    if u is not None:
        cred_ids = [r[0] for r in db.query(em.UserStorageBinding.credential_id)
                    .filter(em.UserStorageBinding.user_id == u.id).all()]
        db.query(em.UserStorageBinding).filter(
            em.UserStorageBinding.user_id == u.id).delete(
            synchronize_session=False)
        if cred_ids:
            db.query(em.MountCredential).filter(
                em.MountCredential.id.in_(cred_ids)).delete(
                synchronize_session=False)
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == u.id).delete(
            synchronize_session=False)
        db.delete(u)
    db.commit()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    user = models.WebUser(username=BOUND, password_hash=hash_password(PW),
                          is_active=True, is_staff=True, emby_username=BOUND,
                          emby_password=PW)
    db.add(user)
    db.flush()
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=user.id, sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    # account_label 必须全仓唯一：共享文件库里别家种子（如
    # test_emby_views_compat）teardown 删用户不删绑定，孤儿绑定残留后
    # 新用户复用其 id 时会撞 (user_id, provider, account_label) 唯一键
    # ——多家共用「种子盘」正是本文件 CI setup 5 连 ERROR 的成因。
    db.add(em.UserStorageBinding(user_id=user.id, provider="drive",
                                 account_label="画质呈现种子盘",
                                 credential_id=cred.id, status="active"))
    lib = em.Library(guid=_guid(), name=LIB_LOCAL,
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    probed = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="画质探测片",
        file_path="mount://3/电影/画质探测片 (2024)/画质探测片.mkv",
        container="mkv", size=17_400_000_000, bitrate=19_020_000,
        width=3840, height=2160, duration_ticks=2 * 3600 * TICKS,
        probe_status="done")
    db.add(probed)
    db.flush()
    db.add(em.MediaStream(item_id=probed.id, stream_index=0,
                          stream_type="Video", codec="hevc",
                          video_range="HDR10", bit_rate=19_020_000,
                          display_title="4K HEVC", is_default=True))
    db.add(em.MediaStream(item_id=probed.id, stream_index=1,
                          stream_type="Audio", codec="aac", channels=2,
                          is_default=True))
    dv_named = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="画质文件名片",
        file_path="mount://3/电影/某片 (2024)/某片.2024.2160p.Dolby.Vision.WEB-DL.mkv",
        container="mkv", size=8_000_000_000, duration_ticks=0,
        probe_status="failed", probe_attempts=99)
    db.add(dv_named)
    plain = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie",
        name="画质无标记片",
        file_path="mount://3/电影/无标记/plain.mkv",
        container="mkv", size=1_000_000_000, duration_ticks=0,
        probe_status="failed", probe_attempts=99)
    db.add(plain)
    # CMS 托管行：两个源各一条映射，甲源预置画质缓存（模拟已点播过
    # 一次、取流路径回填），乙源无缓存。
    cms_lib = em.Library(guid=_guid(), name=LIB_CMS,
                         collection_type="movies", is_enabled=True,
                         kind="applecms")
    db.add(cms_lib)
    db.flush()
    src_a = em.UpstreamSource(name=SRC_A, source_type="applecms",
                              base_url="http://cms-a.example", enabled=True)
    src_b = em.UpstreamSource(name=SRC_B, source_type="applecms",
                              base_url="http://cms-b.example", enabled=True)
    db.add_all([src_a, src_b])
    db.flush()
    cms_item = em.MediaItem(guid=_guid(), library_id=cms_lib.id,
                            item_type="movie", name=CMS_MOVIE,
                            production_year=2024,
                            upstream_source_id=src_a.id,
                            probe_status="done")
    db.add(cms_item)
    db.flush()
    map_a = em.UpstreamItemMapping(local_item_id=cms_item.id,
                                   source_id=src_a.id,
                                   upstream_item_id="101",
                                   confidence=0.9, evidence="title+year")
    map_b = em.UpstreamItemMapping(local_item_id=cms_item.id,
                                   source_id=src_b.id,
                                   upstream_item_id="55",
                                   confidence=0.8, evidence="title+year")
    db.add_all([map_a, map_b])
    db.commit()
    up.store_cms_quality(map_a.id, {
        "width": 3840, "height": 2160, "bandwidth": 18_500_000,
        "video_range": "DolbyVision", "label": "4K Dolby"})
    data = {"probed": probed.guid, "dv_named": dv_named.guid,
            "plain": plain.guid, "cms": cms_item.guid,
            "cms_id": cms_item.id,
            "map_a": map_a.id, "map_b": map_b.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        for mid in (data["map_a"], data["map_b"]):
            CacheManager.delete(up.cms_quality_cache_key(mid))
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client, seed):
    q = chr(34)
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": BOUND, "Pw": PW},
        headers={"X-Emby-Authorization":
                 f"MediaBrowser Client={q}MsPres{q}, Device={q}iOS{q}, "
                 f"DeviceId={q}mspres-dev-1{q}, Version={q}1.0{q}"})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _playback_sources(client, token, guid):
    r = client.post(f"/emby/Items/{guid}/PlaybackInfo",
                    headers={"X-Emby-Token": token}, json={})
    assert r.status_code == 200, r.text
    return r.json()["MediaSources"]


def test_local_probed_movie_source_presentation(client, token, seed):
    src = _playback_sources(client, token, seed["probed"])[0]
    assert src["Name"] == "4K HDR"
    assert src["Size"] == 17_400_000_000
    assert src["Bitrate"] == 19_020_000
    video = [s for s in src["MediaStreams"] if s["Type"] == "Video"][0]
    assert video["Width"] == 3840 and video["Height"] == 2160
    assert video["VideoRange"] == "HDR10"


def test_local_unprobed_movie_name_from_filename_only(client, token, seed):
    src = _playback_sources(client, token, seed["dv_named"])[0]
    # 画质标来自文件名解析（Emby 版本名惯例），不是编的探测值
    assert src["Name"] == "4K Dolby"
    # 无探测：轨道留空、码率不推导（无时长），不许编造
    assert src["MediaStreams"] == []
    assert "Bitrate" not in src


def test_local_unknown_quality_falls_back_to_item_name(client, token, seed):
    src = _playback_sources(client, token, seed["plain"])[0]
    assert src["Name"] == "画质无标记片"


def test_cms_item_one_source_per_mapping_with_quality(client, token, seed):
    sources = _playback_sources(client, token, seed["cms"])
    assert len(sources) == 2
    line_a, line_b = sources
    # 偏好序：置信度高的甲源在前；Id/地址都带各自映射 id
    assert line_a["Id"] == f"{seed['cms']}:upstream:{seed['map_a']}"
    assert line_b["Id"] == f"{seed['cms']}:upstream:{seed['map_b']}"
    for line, mid in ((line_a, seed["map_a"]), (line_b, seed["map_b"])):
        q = parse_qs(urlsplit(line["DirectStreamUrl"]).query)
        assert q["MediaSourceId"][0] == f"{seed['cms']}:upstream:{mid}"
        # 名字里绝不出现源名
        assert SRC_A not in line["Name"] and SRC_B not in line["Name"]
    # 甲源有画质缓存：纯画质标 + 宽高/范围摘要；清单 BANDWIDTH
    # 只是上游模板声明，不能当真实码率下发给客户端。
    assert line_a["Name"] == "4K Dolby"
    assert "Bitrate" not in line_a
    video = [s for s in line_a["MediaStreams"] if s["Type"] == "Video"][0]
    assert video["Width"] == 3840 and video["Height"] == 2160
    assert video["VideoRange"] == "DolbyVision"
    assert "BitRate" not in video
    # Size 推不出就不报实数（0/缺失都是未知口径），不编造
    assert not line_a.get("Size")
    # 乙源无缓存：回落 Emby 默认形态（条目名），不拿源名凑数
    assert line_b["Name"] == CMS_MOVIE


def test_cms_line_without_quality_cache_still_has_video_stream(
        client, token, seed):
    """无画质缓存的 CMS 线路也必须带一条视频轨（2026-11-03 回归）。

    Reflix 类按媒体规格选源的客户端把零轨道 MediaSource 判为
    不可用、直接报「没有可用的源」；占位视频轨只报结构事实，
    codec/宽高/码率一律不许编造（真值走取流回填的缓存分支）。
    """
    sources = _playback_sources(client, token, seed["cms"])
    line_b = [s for s in sources
              if s["Id"] == f"{seed['cms']}:upstream:{seed['map_b']}"][0]
    videos = [s for s in line_b["MediaStreams"] if s["Type"] == "Video"]
    assert len(videos) == 1
    video = videos[0]
    assert video["IsDefault"] is True
    assert "Width" not in video and "Height" not in video
    assert "Codec" not in video and "BitRate" not in video
    # 有缓存的线路仍走真实参数，不被占位覆盖
    line_a = [s for s in sources
              if s["Id"] == f"{seed['cms']}:upstream:{seed['map_a']}"][0]
    real = [s for s in line_a["MediaStreams"] if s["Type"] == "Video"][0]
    assert real["Width"] == 3840 and real["Height"] == 2160


def _assert_no_null_values(node, path="$"):
    """MediaSources 内任何层级都不许出现 JSON null（严格客户端口径）。"""
    if isinstance(node, dict):
        for key, value in node.items():
            assert value is not None, f"{path}.{key} 下发了 null"
            _assert_no_null_values(value, f"{path}.{key}")
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            _assert_no_null_values(value, f"{path}[{idx}]")


def test_cms_sources_json_has_no_null_fields(client, token, seed):
    """2026-11-04 实拍回归：Yamby 严格序列化被详情 JSON 的
    "DefaultAudioStreamIndex":null 噎死（SerializationException:
    Unexpected symbol 'n' in numeric literal）。

    触发条件＝CMS 线路画质缓存带宽高（取流回填过）而条目无音频轨：
    合成源在 _strip_nulls 之后赋值、线路组装又在 strip 之后塞
    "Path": None，null 一路漏到详情面（PlaybackInfo 面有路由末尾
    strip 兜底、详情面没有）。钉死口径：详情与 PlaybackInfo 两面
    的 MediaSources 内零 null；默认轨道数值字段出现时必为整数
    （与真 Emby 省略 null 字段的行为对齐）。
    """
    headers = {"X-Emby-Token": token}
    # —— PlaybackInfo 面 ——
    r = client.post(f"/emby/Items/{seed['cms']}/PlaybackInfo",
                    headers=headers, json={})
    assert r.status_code == 200, r.text
    assert '"DefaultAudioStreamIndex":null' not in r.text
    _assert_no_null_values(r.json()["MediaSources"], "$.MediaSources")
    # —— 详情面（本次漏出点） ——
    db = SessionLocal()
    try:
        uid = db.query(models.WebUser.id).filter(
            models.WebUser.username == BOUND).scalar()
    finally:
        db.close()
    r = client.get(f"/emby/Users/{uid}/Items/{seed['cms']}",
                   headers=headers)
    assert r.status_code == 200, r.text
    assert '"DefaultAudioStreamIndex":null' not in r.text
    sources = r.json()["MediaSources"]
    assert sources, "详情面应带线路"
    _assert_no_null_values(sources, "$.MediaSources")
    for source in sources:
        for key in ("DefaultAudioStreamIndex",
                    "DefaultSubtitleStreamIndex"):
            if key in source:
                assert isinstance(source[key], int)


# ---------------------------------------------------------------------------
# 身份组聚合（2026-11-03 回归）：同片重复行线路互通
# ---------------------------------------------------------------------------

LIB_GRP = "身份组在线库"
SRC_GA = "身份组源甲"
SRC_GB = "身份组源乙"
GRP_TMDB = "1518076"


def _grp_cleanup(db):
    for lib in db.query(em.Library).filter(
            em.Library.name == LIB_GRP).all():
        ids = [r[0] for r in db.query(em.MediaItem.id).filter(
            em.MediaItem.library_id == lib.id).all()]
        if ids:
            db.query(em.UpstreamItemMapping).filter(
                em.UpstreamItemMapping.local_item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(
            em.Library.id == lib.id).delete(synchronize_session=False)
    for src in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name.in_([SRC_GA, SRC_GB])).all():
        db.delete(src)
    db.commit()


@pytest.fixture()
def grp_seed():
    """同 tmdb 身份的两行重复：一行一条线路；另置一行异身份对照。"""
    db = SessionLocal()
    _grp_cleanup(db)
    lib = em.Library(guid=_guid(), name=LIB_GRP,
                     collection_type="movies", is_enabled=True,
                     kind="applecms")
    db.add(lib)
    db.flush()
    src_a = em.UpstreamSource(name=SRC_GA, source_type="applecms",
                              base_url="http://grp-a.example",
                              enabled=True)
    src_b = em.UpstreamSource(name=SRC_GB, source_type="applecms",
                              base_url="http://grp-b.example",
                              enabled=True)
    db.add_all([src_a, src_b])
    db.flush()
    row1 = em.MediaItem(guid=_guid(), library_id=lib.id,
                        item_type="movie", name="身份组片",
                        tmdb_id=GRP_TMDB, production_year=2025,
                        upstream_source_id=src_a.id, probe_status="done")
    row2 = em.MediaItem(guid=_guid(), library_id=lib.id,
                        item_type="movie", name="身份组片",
                        tmdb_id=GRP_TMDB, production_year=2025,
                        upstream_source_id=src_b.id, probe_status="done")
    other = em.MediaItem(guid=_guid(), library_id=lib.id,
                         item_type="movie", name="异身份片",
                         tmdb_id="999999", production_year=2025,
                         upstream_source_id=src_a.id, probe_status="done")
    db.add_all([row1, row2, other])
    db.flush()
    map1 = em.UpstreamItemMapping(local_item_id=row1.id,
                                  source_id=src_a.id,
                                  upstream_item_id="201",
                                  confidence=0.9, evidence="tmdb")
    map2 = em.UpstreamItemMapping(local_item_id=row2.id,
                                  source_id=src_b.id,
                                  upstream_item_id="202",
                                  confidence=0.8, evidence="tmdb")
    map3 = em.UpstreamItemMapping(local_item_id=other.id,
                                  source_id=src_a.id,
                                  upstream_item_id="203",
                                  confidence=0.9, evidence="tmdb")
    db.add_all([map1, map2, map3])
    db.commit()
    data = {"row1": row1.guid, "row2": row2.guid, "other": other.guid,
            "map1": map1.id, "map2": map2.id, "map3": map3.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _grp_cleanup(db)
    finally:
        db.close()


def test_playbackinfo_aggregates_identity_sibling_lines(
        client, token, grp_seed):
    """打开任一重复行，PlaybackInfo 都带全身份组的线路。

    2026-11-03《丛林猎凶》实证：代表行自身 2 线全死、活线在兄弟
    行上，只读自身映射时客户端报「没有可用的源」。
    """
    for guid in (grp_seed["row1"], grp_seed["row2"]):
        sources = _playback_sources(client, token, guid)
        ids = {s["Id"] for s in sources}
        assert f"{guid}:upstream:{grp_seed['map1']}" in ids
        assert f"{guid}:upstream:{grp_seed['map2']}" in ids
        # 异身份条目的线路绝不并入
        assert f"{guid}:upstream:{grp_seed['map3']}" not in ids
        # 每条线（含兄弟行的）都带视频轨，形态与自身线路一致
        for s in sources:
            assert any(st["Type"] == "Video"
                       for st in s["MediaStreams"])
    # 异身份条目自身只见自己的线路
    sources = _playback_sources(client, token, grp_seed["other"])
    assert {s["Id"] for s in sources} == {
        f"{grp_seed['other']}:upstream:{grp_seed['map3']}"}


def test_upstream_stream_endpoint_directed_and_hard_errors(
        client, token, seed, monkeypatch):
    calls = {}

    def fake_resolve(db, local_item_id, *, mapping_id=None, **kwargs):
        calls["mapping_id"] = mapping_id
        return "https://cdn.example.com/picked.m3u8"

    monkeypatch.setattr(up, "resolve_mapped_upstream_stream", fake_resolve)
    headers = {"X-Emby-Token": token}
    guid = seed["cms"]
    r = client.get(
        f"/emby/Videos/{guid}/upstream/stream",
        params={"MediaSourceId": f"{guid}:upstream:{seed['map_b']}"},
        headers=headers, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "https://cdn.example.com/picked.m3u8"
    assert calls["mapping_id"] == seed["map_b"]
    # 旧形态（只到 :upstream）→ 最佳映射口径（mapping_id=None）
    calls.clear()
    r = client.get(
        f"/emby/Videos/{guid}/upstream/stream",
        params={"MediaSourceId": f"{guid}:upstream"},
        headers=headers, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert calls["mapping_id"] is None
    # 形态错误（别条目的 id / id 段非数字）→ 硬 404，绝不进解析
    calls.clear()
    for bad in (f"deadbeef:upstream:{seed['map_b']}",
                f"{guid}:upstream:abc", f"{guid}:local"):
        r = client.get(
            f"/emby/Videos/{guid}/upstream/stream",
            params={"MediaSourceId": bad},
            headers=headers, follow_redirects=False)
        assert r.status_code == 404, (bad, r.text)
        assert r.json()["code"] == "upstream_mapping_not_found"
    assert calls == {}


# ---------------------------------------------------------------------------
# 函数级桩链：定向解析 + m3u8 画质回填（内存库，无网络）
# ---------------------------------------------------------------------------

@pytest.fixture()
def stub_db(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)
    from backend import models as base_models
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _stub_seed(db):
    for sid, host in ((7, "a.example"), (8, "b.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"桩源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(guid="lib-stub", name="桩库", collection_type="movies",
                     is_enabled=True, kind="applecms",
                     upstream_source_ids="7,8")
    db.add(lib)
    db.flush()
    item = em.MediaItem(guid="stub-item", library_id=lib.id,
                        item_type="movie", name="桩片",
                        upstream_source_id=7, probe_status="done")
    db.add(item)
    db.flush()
    map_a = em.UpstreamItemMapping(local_item_id=item.id, source_id=7,
                                   upstream_item_id="101", confidence=0.9,
                                   evidence="title+year")
    map_b = em.UpstreamItemMapping(local_item_id=item.id, source_id=8,
                                   upstream_item_id="55", confidence=0.8,
                                   evidence="title+year")
    other = em.MediaItem(guid="stub-other", library_id=lib.id,
                         item_type="movie", name="桩片二",
                         upstream_source_id=7, probe_status="done")
    db.add_all([map_a, map_b, other])
    db.flush()
    map_other = em.UpstreamItemMapping(local_item_id=other.id, source_id=7,
                                       upstream_item_id="999",
                                       confidence=0.9, evidence="title+year")
    db.add(map_other)
    db.commit()
    # 隔离：内存库的映射 id 会与 module 级 seed 夹具（文件库）撞号，
    # 而画质缓存按映射 id 存进程级 CacheManager——不清掉 seed 预置的
    # 完整缓存，PA-073 新鲜度门禁会让本夹具的取流回填被旧缓存挡住。
    for mid in (map_a.id, map_b.id, map_other.id):
        CacheManager.delete(up.cms_quality_cache_key(mid))
    return item, map_a, map_b, map_other


_DETAILS = {
    "a.example": {"vod_id": 101, "vod_name": "桩片",
                  "vod_remarks": "1080P",
                  "vod_play_from": "线路A",
                  "vod_play_url": "正片$https://cdn-a.example/movie.mp4"},
    "b.example": {"vod_id": 55, "vod_name": "桩片",
                  "vod_play_from": "线路B",
                  "vod_play_url": "正片$https://cdn-b.example/master.m3u8"},
}

_MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,'
    'CODECS="avc1.640028,mp4a.40.2"\n'
    "1080.m3u8\n"
)


def _stub_detail_get(url, headers=None, timeout=None):
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc
    return 200, {"code": 1, "list": [_DETAILS[host]]}


def _stub_text_get(url, headers=None, timeout=None):
    return 200, _MASTER


def _wait_quality(mapping_id, key="label", timeout=5.0):
    """等后台画质回填落缓存（回填已后台化，resolve 返回时可能未落袋）。

    回填本体整包一次性 store：等到目标字段出现即可断言其余字段；
    超时返回最后一次读数，调用方断言照常报错（不掩盖真失败）。
    与 test_cms_play_quality_background 的 _wait_quality 同口径。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        cached = up.read_cms_quality(mapping_id)
        if cached and cached.get(key):
            return cached
        time.sleep(0.05)
    return up.read_cms_quality(mapping_id)


def test_directed_resolve_picks_selected_mapping(stub_db):
    item, map_a, map_b, _ = _stub_seed(stub_db)
    url = up.resolve_mapped_upstream_stream(
        stub_db, item.id, mapping_id=map_b.id,
        http_get=_stub_detail_get, http_probe=lambda *a: (200, None),
        http_get_text=_stub_text_get)
    # 选乙源就只出乙源的地址，不许回落最佳（甲源）
    assert url == "https://cdn-b.example/master.m3u8"
    # 取流顺手回填画质缓存（m3u8 主清单真实数值）；回填已后台化，
    # 等落袋再断言，不许按旧同步契约立即读（与后台线程竞速必偶红）
    cached = _wait_quality(map_b.id, key="width")
    assert cached["width"] == 1920 and cached["height"] == 1080
    assert cached["bandwidth"] == 8_000_000
    assert cached["label"] == "1080P"
    # 甲源走最佳口径仍是甲源；备注标记也回填成标签
    url_a = up.resolve_mapped_upstream_stream(
        stub_db, item.id, http_get=_stub_detail_get,
        http_probe=lambda *a: (200, None))
    assert url_a == "https://cdn-a.example/movie.mp4"
    # 甲源 .mp4 的后台回填走默认有界跟随抓取（单跳 3 秒、至多 4 跳），
    # 等落袋预算按其上限给足，断言仍是备注标签原文
    assert _wait_quality(map_a.id, timeout=15.0)["label"] == "1080P"


def test_directed_resolve_hard_errors_no_fallback(stub_db):
    item, map_a, map_b, map_other = _stub_seed(stub_db)
    # 映射属于别的条目 → 404，绝不换线
    with pytest.raises(up.UpstreamPlaybackError) as ei:
        up.resolve_mapped_upstream_stream(
            stub_db, item.id, mapping_id=map_other.id,
            http_get=_stub_detail_get, http_probe=lambda *a: (200, None))
    assert ei.value.status == 404
    assert ei.value.code == "upstream_mapping_not_found"
    # 不存在的映射 id → 404
    with pytest.raises(up.UpstreamPlaybackError) as ei:
        up.resolve_mapped_upstream_stream(
            stub_db, item.id, mapping_id=424242,
            http_get=_stub_detail_get, http_probe=lambda *a: (200, None))
    assert ei.value.status == 404
    # 所选映射的源被停用 → 按读契约不可见，同 404 硬报错
    src = stub_db.query(em.UpstreamSource).filter(
        em.UpstreamSource.id == 8).one()
    src.enabled = False
    stub_db.commit()
    with pytest.raises(up.UpstreamPlaybackError) as ei:
        up.resolve_mapped_upstream_stream(
            stub_db, item.id, mapping_id=map_b.id,
            http_get=_stub_detail_get, http_probe=lambda *a: (200, None))
    assert ei.value.status == 404
    assert ei.value.code == "upstream_mapping_not_found"


# ---------------------------------------------------------------------------
# 身份组聚合（函数级）：组内定向/最佳解析 + 边界
# ---------------------------------------------------------------------------

def _grp_stub_seed(db):
    """同 tmdb 身份三行：行一/行二各挂线路（且有一条同源同 vod 的
    重复映射验去重）、行三无线路；外加同名无 tmdb 行作对照。"""
    for sid, host in ((7, "a.example"), (8, "b.example")):
        db.add(em.UpstreamSource(
            id=sid, name=f"组桩源{sid}", source_type="applecms",
            base_url=f"http://{host}", enabled=True,
            user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(guid="lib-grp", name="组桩库",
                     collection_type="movies", is_enabled=True,
                     kind="applecms", upstream_source_ids="7,8")
    db.add(lib)
    db.flush()

    def _movie(guid, tmdb):
        row = em.MediaItem(guid=guid, library_id=lib.id,
                           item_type="movie", name="组片",
                           tmdb_id=tmdb, upstream_source_id=7,
                           probe_status="done")
        db.add(row)
        db.flush()
        return row

    row1 = _movie("grp-1", "4242")
    row2 = _movie("grp-2", "4242")
    row3 = _movie("grp-3", "4242")
    noname = _movie("grp-noname", None)
    map1 = em.UpstreamItemMapping(local_item_id=row1.id, source_id=7,
                                  upstream_item_id="101",
                                  confidence=0.9, evidence="tmdb")
    map2 = em.UpstreamItemMapping(local_item_id=row2.id, source_id=8,
                                  upstream_item_id="55",
                                  confidence=0.8, evidence="tmdb")
    # 与 map1 同 (源, vod) 的重复映射：组内去重后只许出现一次
    map_dup = em.UpstreamItemMapping(local_item_id=row2.id, source_id=7,
                                     upstream_item_id="101",
                                     confidence=0.7, evidence="tmdb")
    map_noname = em.UpstreamItemMapping(
        local_item_id=noname.id, source_id=7, upstream_item_id="101",
        confidence=0.95, evidence="title+year")
    db.add_all([map1, map2, map_dup, map_noname])
    db.commit()
    for mid in (map1.id, map2.id, map_dup.id, map_noname.id):
        CacheManager.delete(up.cms_quality_cache_key(mid))
    return row1, row2, row3, noname, map1, map2, map_dup, map_noname


def test_grouped_mappings_merge_dedupe_and_boundaries(stub_db):
    from backend.emby_server import upstream_item_mappings as uim
    row1, row2, row3, noname, map1, map2, map_dup, map_noname = \
        _grp_stub_seed(stub_db)
    # 自身在前、兄弟续后、同（源, vod）去重
    grouped = uim.get_grouped_mappings(stub_db, row1.id)
    assert [m.id for m in grouped] == [map1.id, map2.id]
    # 无自身线路的行也能看到全组线路
    grouped3 = uim.get_grouped_mappings(stub_db, row3.id)
    assert [m.id for m in grouped3] == [map1.id, map2.id]
    # 同名但无 tmdb 身份：不进任何组（名字是启发式，播放不并线）
    assert [m.id for m in uim.get_grouped_mappings(
        stub_db, noname.id)] == [map_noname.id]
    assert map_noname.id not in [m.id for m in grouped]
    # 命中行已软删 → 整组不可见，不许靠兄弟行救活（软删走批量
    # 更新：全局软删钩子会把已删行从 ORM 查询里藏掉，提交后过期
    # 实例再读属性会 ObjectDeletedError，故先存 id、不再碰实例）
    import datetime as _dt
    row1_id = row1.id
    stub_db.query(em.MediaItem).filter(
        em.MediaItem.id == row1_id).update(
        {"deleted_at": _dt.datetime.now()}, synchronize_session=False)
    stub_db.commit()
    assert uim.get_grouped_mappings(stub_db, row1_id) == []


def test_grouped_resolve_directed_and_best(stub_db):
    row1, row2, row3, noname, map1, map2, map_dup, map_noname = \
        _grp_stub_seed(stub_db)
    probe = lambda *a: (200, None)  # noqa: E731
    # 定向解析兄弟行上的映射（展示下发了就必须认）
    url = up.resolve_mapped_upstream_stream(
        stub_db, row1.id, mapping_id=map2.id,
        http_get=_stub_detail_get, http_probe=probe,
        http_get_text=_stub_text_get)
    assert url == "https://cdn-b.example/master.m3u8"
    # 无自身线路的行走最佳口径 → 落到组内偏好序首位（行一的甲源）
    url = up.resolve_mapped_upstream_stream(
        stub_db, row3.id, http_get=_stub_detail_get, http_probe=probe)
    assert url == "https://cdn-a.example/movie.mp4"
    # 组外条目（无 tmdb 同名行）的映射定向解析仍硬 404
    with pytest.raises(up.UpstreamPlaybackError) as ei:
        up.resolve_mapped_upstream_stream(
            stub_db, row1.id, mapping_id=map_noname.id,
            http_get=_stub_detail_get, http_probe=probe)
    assert ei.value.status == 404
    assert ei.value.code == "upstream_mapping_not_found"


def test_grouped_mappings_episode_via_series_identity(stub_db):
    """集的聚合经所属 series 的 tmdb 身份成组，季集号必须相同。"""
    from backend.emby_server import upstream_item_mappings as uim
    db = stub_db
    db.add(em.UpstreamSource(
        id=7, name="集桩源", source_type="applecms",
        base_url="http://a.example", enabled=True,
        user_agent="Mozilla/5.0 (test)"))
    lib = em.Library(guid="lib-ep", name="集桩库",
                     collection_type="tvshows", is_enabled=True,
                     kind="applecms", upstream_source_ids="7")
    db.add(lib)
    db.flush()

    def _series(guid):
        row = em.MediaItem(guid=guid, library_id=lib.id,
                           item_type="series", name="组剧",
                           tmdb_id="777", probe_status="done")
        db.add(row)
        db.flush()
        return row

    def _episode(guid, series, ep_no):
        row = em.MediaItem(guid=guid, library_id=lib.id,
                           item_type="episode", name=f"第{ep_no}集",
                           series_id=series.id, season_number=1,
                           episode_number=ep_no, probe_status="done")
        db.add(row)
        db.flush()
        return row

    s1, s2 = _series("eps-s1"), _series("eps-s2")
    ep1 = _episode("eps-1", s1, 1)
    ep1_dup = _episode("eps-1d", s2, 1)
    ep2_dup = _episode("eps-2d", s2, 2)
    map_ep = em.UpstreamItemMapping(
        local_item_id=ep1.id, source_id=7, upstream_item_id="e101",
        confidence=0.9, evidence="tmdb", episode_index=1)
    db.add(map_ep)
    db.commit()
    CacheManager.delete(up.cms_quality_cache_key(map_ep.id))
    # 兄弟 series 下的同季同集行能看到这条线路
    assert [m.id for m in uim.get_grouped_mappings(
        db, ep1_dup.id)] == [map_ep.id]
    # 集号不同不并
    assert uim.get_grouped_mappings(db, ep2_dup.id) == []

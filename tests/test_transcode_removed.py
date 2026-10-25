# -*- coding: utf-8 -*-
"""转码全面删除后的三类断言（2026-10-24）：

1. 转码地址返回 404：video_hls 通配收到非 stream.* 路径（master.m3u8、
   切片、旧 HLS 会话地址）一律 404，不再起 ffmpeg；
2. PlaybackInfo 报转码 false：_media_source（PlaybackInfo 面与详情 DTO 面
   共用的同一函数）恒报 SupportsTranscoding=False 且无 TranscodingUrl，
   staff 与普通用户一致；
3. 变体清单里没有转码：list_variants 对 Drive / 挂载 / 本地条目都不再
   列出 transcode_hls；点 transcode_hls 解析即 404（已不在变体枚举里）。

全部离线：DB 用内存 SQLite，不碰网络、不碰生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.emby_server import api, play_variants
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _item(guid, *, file_path="/data/movie.mkv", drive_file_id=""):
    item = em.MediaItem()
    item.guid = guid
    item.name = "测试片"
    item.file_path = file_path
    item.drive_file_id = drive_file_id
    item.item_type = "movie"
    item.size = 1_000_000_000
    item.duration_ticks = 36_000_000_000
    item.bitrate = 2_000_000
    return item


def _user(uid=101, **kw):
    base = {"id": uid, "is_staff": False}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. 转码地址返回 404
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["master.m3u8", "seg0.ts", "hls1", "index.m3u8"])
def test_transcode_paths_return_404(path):
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(api.video_hls("g1", path, MagicMock(), MagicMock(),
                                  MagicMock()))
    assert excinfo.value.status_code == 404


def test_transcode_variant_name_resolves_to_404():
    assert not hasattr(play_variants, "TRANSCODE_HLS")
    assert "transcode_hls" not in play_variants._VARIANTS
    assert play_variants.resolve_requested_variant(
        {"variant": "transcode_hls"}, ["drive_relay", "drive_direct"]) == (None, 404)


# ---------------------------------------------------------------------------
# 2. PlaybackInfo 报转码 false（两面同源）
# ---------------------------------------------------------------------------

def test_media_source_always_reports_no_transcoding(db):
    item = _item("guid-removed")
    for user in (_user(), _user(is_staff=True)):
        src = api._media_source(item, "http://testserver", "", db, user=user)
        assert src["SupportsTranscoding"] is False
        assert "TranscodingUrl" not in src


# ---------------------------------------------------------------------------
# 3. 变体清单里没有转码
# ---------------------------------------------------------------------------

def test_variant_lists_contain_no_transcode(db):
    drive = _item("guid-drive", file_path="mount://3/电影/测试片.mkv",
                  drive_file_id="1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO")
    mount = _item("guid-mount", file_path="mount://7/剧集/a.mkv")
    local = _item("guid-local", file_path="/data/movie.mkv")
    # 空库缺省：直链开、中继关 → Drive 条目只剩直链
    assert [v.name for v in play_variants.list_variants(
        db, drive, drive_mount_locatable=True)] == ["drive_direct"]
    assert [v.name for v in play_variants.list_variants(db, mount)] == [
        "mount_relay"]
    assert [v.name for v in play_variants.list_variants(db, local)] == [
        "local_file"]

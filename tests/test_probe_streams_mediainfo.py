# -*- coding: utf-8 -*-
"""strm/远程条目「媒体信息」空白：按需探测把轨道写库、JSON 持久化带轨道、
老版本探过但没轨道的条目补探一次。"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("MEDIAINFO_JSON_DIR", "/tmp/test_mediainfo_json_streams")

import shutil
import uuid
from datetime import datetime, timedelta
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import media_probe, probe_worker
from backend.emby_server import mediainfo_persist as persist
from backend.emby_server import api as emby_api

init_db()

STRM = "/strm/剧集/国产剧/沦陷 (2026)/Season 1/沦陷 - S01E01 - 第 1 集 - 1080p WEB-DL.strm"

PROBE = {
    "video_codec": "H264", "audio_codec": "AAC", "width": 1920, "height": 1080,
    "bitrate": 4_000_000, "duration_ticks": 36 * 60 * 10_000_000,
    "streams": [
        {"stream_index": 0, "stream_type": "Video", "codec": "h264",
         "frame_rate": "25/1", "profile": "High", "pixel_format": "yuv420p"},
        {"stream_index": 1, "stream_type": "Audio", "codec": "aac", "channels": 2,
         "sample_rate": 48000, "channel_layout": "stereo", "language": "chi"},
        {"stream_index": 2, "stream_type": "data", "codec": "bin_data"},
    ],
}


@pytest.fixture()
def db():
    shutil.rmtree("/tmp/test_mediainfo_json_streams", ignore_errors=True)
    s = SessionLocal()
    lib = em.Library(guid=uuid.uuid4().hex, name=f"streams_{uuid.uuid4().hex[:6]}",
                     collection_type="tvshows", paths="/strm")
    s.add(lib)
    s.commit()
    s.info["_lib"] = lib
    try:
        yield s
    finally:
        ids = [r[0] for r in s.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == lib.id).all()]
        if ids:
            s.query(em.MediaStream).filter(em.MediaStream.item_id.in_(ids)) \
                .delete(synchronize_session=False)
        s.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id) \
            .delete(synchronize_session=False)
        s.query(em.Library).filter(em.Library.id == lib.id).delete()
        s.commit()
        s.close()


def _ep(db, **kw):
    it = em.MediaItem(guid=uuid.uuid4().hex, library_id=db.info["_lib"].id,
                      item_type="episode", name="第 1 集", file_path=STRM,
                      date_added=datetime.now(), **kw)
    db.add(it)
    db.commit()
    db.refresh(it)
    return it


def _streams(db, item):
    return db.query(em.MediaStream).filter(em.MediaStream.item_id == item.id) \
        .order_by(em.MediaStream.stream_index).all()


def test_write_back_persists_streams_and_keeps_external_subs(db):
    item = _ep(db, probe_status="probing")
    db.add(em.MediaStream(item_id=item.id, stream_index=1, stream_type="Subtitle",
                          codec="srt", is_external=True, external_path="/x.srt"))
    db.commit()
    media_probe.write_back(db, item, dict(PROBE))
    rows = _streams(db, item)
    kinds = [(r.stream_type, bool(r.is_external)) for r in rows]
    assert kinds == [("Video", False), ("Audio", False), ("Subtitle", True)]
    assert [r.stream_index for r in rows] == [0, 1, 2]  # 外挂字幕挪到内封之后
    assert rows[0].frame_rate == "25/1" and rows[1].channel_layout == "stereo"
    # 再探一次：内封轨道重建，不翻倍
    media_probe.write_back(db, item, dict(PROBE))
    assert len(_streams(db, item)) == 3


def test_persist_roundtrip_includes_streams(db):
    item = _ep(db, probe_status="probing", size=1, file_mtime=1.0)
    media_probe.write_back(db, item, dict(PROBE))
    assert persist.serialize(db, item) is True
    db.query(em.MediaStream).filter(em.MediaStream.item_id == item.id).delete()
    db.commit()
    assert not media_probe.has_internal_streams(db, item)
    assert persist.deserialize(db, item) is True
    assert [r.stream_type for r in _streams(db, item)] == ["Video", "Audio"]


def test_probe_one_old_json_without_streams_still_probes(db):
    item = _ep(db, probe_status="probing", video_codec="H264", width=1920,
               height=1080, size=1, file_mtime=1.0)
    with mock.patch.object(persist, "deserialize", return_value=True), \
            mock.patch.object(media_probe, "resolve_probe_input",
                              return_value=(("http://x/v.mkv", {}, 0, ""), None)), \
            mock.patch.object(media_probe, "probe_metadata", return_value=dict(PROBE)):
        assert media_probe.probe_one(db, item) == "done"
    assert media_probe.has_internal_streams(db, item)


def test_old_probed_item_without_streams_is_backfilled_once(db):
    old = datetime.now() - timedelta(days=3)
    item = _ep(db, probe_status="done", video_codec="H264",
               duration_ticks=10 ** 10, last_probed_at=old)
    with mock.patch.object(probe_worker, "PROBE_ENABLED", True):
        assert probe_worker.maybe_enqueue(db, item) is True
    assert item.probe_status == "pending"
    claimed = probe_worker._claim_batch(db, 10, library_id=db.info["_lib"].id)
    assert claimed == [item.id]
    # 本进程探过的（last_probed_at 在启动之后）不再补探 → 不会反复入队
    db.refresh(item)
    item.probe_status = "done"
    item.last_probed_at = datetime.now()
    db.commit()
    with mock.patch.object(probe_worker, "PROBE_ENABLED", True):
        assert probe_worker.maybe_enqueue(db, item) is False


def test_item_with_streams_not_backfilled(db):
    item = _ep(db, probe_status="done", video_codec="H264",
               duration_ticks=10 ** 10,
               last_probed_at=datetime.now() - timedelta(days=3))
    db.add(em.MediaStream(item_id=item.id, stream_index=0, stream_type="Video",
                          codec="h264"))
    db.commit()
    assert probe_worker.needs_stream_backfill(db, item) is False


# ---------- 详情页兜底：文件名拼最小轨道 ----------

def test_detail_synthesizes_streams_from_filename(db):
    item = _ep(db, video_resolution="1080p", media_source="WEB-DL",
               audio_codec="AAC")
    src = emby_api._media_source(item, "http://x", synthesize=True)
    v, a = src["MediaStreams"][:2]
    assert v["Type"] == "Video" and v["Height"] == 1080 and v["Width"] == 1920
    assert "WEB-DL" in v["DisplayTitle"] and "1080p" in v["DisplayTitle"]
    assert "Codec" not in v  # 文件名里没写编码就不虚报
    assert a["Type"] == "Audio" and a["Codec"] == "aac"
    # PlaybackInfo 口径（synthesize=False）不拼
    assert emby_api._media_source(item, "http://x")["MediaStreams"] == []


def test_synthesized_codec_normalized_and_index_avoids_external_sub(db):
    item = _ep(db, video_codec="H.265")
    db.add(em.MediaStream(item_id=item.id, stream_index=1, stream_type="Subtitle",
                          codec="srt", is_external=True, external_path="/x.srt"))
    db.commit()
    db.refresh(item)
    src = emby_api._media_source(item, "http://x", synthesize=True)
    idx = [(s["Type"], s["Index"]) for s in src["MediaStreams"]]
    assert idx[0] == ("Video", 0)
    assert src["MediaStreams"][0]["Codec"] == "hevc"
    assert len({i for _, i in idx}) == len(idx)


def test_real_streams_win_over_synthesis(db):
    item = _ep(db, video_resolution="1080p")
    db.add(em.MediaStream(item_id=item.id, stream_index=0, stream_type="Video",
                          codec="h264"))
    db.commit()
    db.refresh(item)
    src = emby_api._media_source(item, "http://x", synthesize=True)
    assert [s["Codec"] for s in src["MediaStreams"]] == ["h264"]

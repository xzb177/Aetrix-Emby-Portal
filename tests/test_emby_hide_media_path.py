"""strm 条目的协议响应：不泄露服务器媒体路径（EMBY_HIDE_MEDIA_PATH，默认开）。

走真实路由 + 真实 token（与 test_emby_views_compat 同口径），断言
/Items/{id}、/Users/{uid}/Items/{id}、PlaybackInfo 的响应里都不出现 strm 路径。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import api as emby_api
from backend.security import hash_password

init_db()

PW = "hide-path-pw"
UNAME = "hidepath_bob"
STRM = "/strm/剧集/国产剧/沦陷 (2026)/Season 1/沦陷 - S01E01 - 第 1 集 - 1080p WEB-DL.strm"


def _guid():
    return uuid.uuid4().hex


def _cleanup(db, lib_name):
    old = db.query(models.WebUser).filter(models.WebUser.username == UNAME).first()
    if old:
        # 种子绑定的清理与种子创建配对（先删绑定行再删凭据行，外键顺序）；
        # 否则重跑时同名绑定撞 (user_id, provider, account_label) 唯一约束。
        cred_ids = [r[0] for r in db.query(em.UserStorageBinding.credential_id)
                    .filter(em.UserStorageBinding.user_id == old.id).all()]
        db.query(em.UserStorageBinding).filter(
            em.UserStorageBinding.user_id == old.id).delete(synchronize_session=False)
        if cred_ids:
            db.query(em.MountCredential).filter(
                em.MountCredential.id.in_(cred_ids)).delete(synchronize_session=False)
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == old.id).delete(synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
    # 孤儿绑定（用户已不在、绑定残留，历史种子未清所致）无条件清掉——
    # 不能放在 if old 里：旧用户已被删、只剩孤儿行时 old 为空，正是最
    # 需要清孤儿的时候；否则新种子用户复用 id 时撞唯一约束。
    db.query(em.UserStorageBinding).filter(
        ~em.UserStorageBinding.user_id.in_(
            db.query(models.WebUser.id))).delete(synchronize_session=False)
    for lib in db.query(em.Library).filter(em.Library.name == lib_name).all():
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == lib.id).all()]
        if ids:
            db.query(em.MediaStream).filter(
                em.MediaStream.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db, "隐藏路径测试库")
    user = models.WebUser(username=UNAME, password_hash="x", is_active=True,
                          is_staff=True, emby_username=UNAME,
                          emby_password=hash_password(PW))
    db.add(user)
    lib = em.Library(guid=_guid(), name="隐藏路径测试库", collection_type="tvshows",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    # 播放门禁（2026-10-26 硬规矩，管理员同样过门禁）：种子用户须有 active
    # 绑定，否则 PlaybackInfo 在门禁处即 403，本文件测的“仍可播”无从谈起。
    cred = em.MountCredential(mount_id=None, scope="user", owner_user_id=user.id,
                              sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=user.id, provider="drive",
                                 account_label="种子盘", credential_id=cred.id,
                                 status="active"))
    db.flush()
    series = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="series",
                          name="沦陷", file_path="/strm/剧集/国产剧/沦陷 (2026)")
    db.add(series)
    db.flush()
    ep = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="episode",
                      name="第 1 集", series_id=series.id, season_number=1,
                      episode_number=1, file_path=STRM, container="strm",
                      probe_status="failed", probe_attempts=99,
                      video_resolution="1080p", media_source="WEB-DL")
    db.add(ep)
    db.commit()
    data = {"user_id": user.id, "ep": ep.guid, "series": series.guid}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db, "隐藏路径测试库")
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName", json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="HideTest", Device="iOS", '
                 'DeviceId="hide-dev-1", Version="1.0"'})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token):
    return {"X-Emby-Token": token}


def _no_path(text):
    assert "/strm/" not in text
    assert "沦陷 (2026)/Season 1" not in text


def test_item_detail_hides_path(client, token, seed):
    for url in (f"/emby/Items/{seed['ep']}",
                f"/emby/Users/{seed['user_id']}/Items/{seed['ep']}"):
        r = client.get(url, headers=_h(token))
        assert r.status_code == 200, r.text
        _no_path(r.text)
        src = r.json()["MediaSources"][0]
        assert "Path" not in src and "Path" not in r.json()
        assert src["Id"] == seed["ep"]


def test_playback_info_hides_path_but_still_playable(client, token, seed):
    for _ in range(2):  # 第二次可能命中 PlaybackInfo 缓存
        r = client.post(f"/emby/Items/{seed['ep']}/PlaybackInfo",
                        headers=_h(token), json={})
        assert r.status_code == 200, r.text
        _no_path(r.text)
        src = r.json()["MediaSources"][0]
        assert "Path" not in src
        assert f"/Videos/{seed['ep']}/stream" in src["DirectStreamUrl"]


def test_playback_info_strips_path_from_stale_cache(client, token, seed, monkeypatch):
    """升级前缓存进 Redis 的 media_source 带着 Path，也不能再发出去。"""
    from backend.database import CacheManager
    import json as _json

    def fake_get(key):
        return _json.dumps({"Id": seed["ep"], "Path": STRM, "MediaStreams": []})
    monkeypatch.setattr(CacheManager, "get", staticmethod(fake_get))
    r = client.post(f"/emby/Items/{seed['ep']}/PlaybackInfo",
                    headers=_h(token), json={})
    assert r.status_code == 200, r.text
    _no_path(r.text)


def test_path_kept_when_disabled(monkeypatch):
    monkeypatch.setenv("EMBY_HIDE_MEDIA_PATH", "0")
    item = em.MediaItem(guid="g" * 32, item_type="episode", name="x",
                        file_path=STRM, duration_ticks=0)
    item.streams = []
    src = emby_api._media_source(item, "http://x")
    assert src["Path"] == STRM
    monkeypatch.setenv("EMBY_HIDE_MEDIA_PATH", "1")
    assert "Path" not in emby_api._media_source(item, "http://x")


# ---------- 时长回退（「0秒钟」）----------

def test_runtime_from_tmdb_metadata():
    from backend.emby_server.tmdb import runtime_ticks_from, apply_runtime
    assert runtime_ticks_from({"runtime": 118}) == 118 * 60 * 10_000_000
    assert runtime_ticks_from({"episode_run_time": [0, 40]}) == 40 * 60 * 10_000_000
    assert runtime_ticks_from({"runtime": None, "episode_run_time": []}) == 0
    it = em.MediaItem(guid="a" * 32, item_type="series", name="s",
                      metadata_runtime_ticks=0)
    assert apply_runtime(it, {"episode_run_time": [36]})
    assert it.metadata_runtime_ticks == 36 * 60 * 10_000_000
    # 只补空
    assert not apply_runtime(it, {"episode_run_time": [99]})


def test_douban_iso_duration():
    from backend.emby_server.douban import parse_iso_duration
    assert parse_iso_duration("PT1H58M") == (3600 + 58 * 60) * 10_000_000
    assert parse_iso_duration("PT40M") == 40 * 60 * 10_000_000
    assert parse_iso_duration("") == 0 and parse_iso_duration("abc") == 0


def test_run_time_ticks_fallback_chain():
    series = em.MediaItem(guid="s" * 32, item_type="series", name="沦陷",
                          metadata_runtime_ticks=36 * 60 * 10_000_000)
    ep = em.MediaItem(guid="e" * 32, item_type="episode", name="第 1 集",
                      series_id=1, duration_ticks=0, metadata_runtime_ticks=0)
    ep.series = series
    assert emby_api._run_time_ticks(ep) == 36 * 60 * 10_000_000
    ep.metadata_runtime_ticks = 41 * 60 * 10_000_000
    assert emby_api._run_time_ticks(ep) == 41 * 60 * 10_000_000
    ep.duration_ticks = 123
    assert emby_api._run_time_ticks(ep) == 123  # 探测出的真实时长优先
    bare = em.MediaItem(guid="b" * 32, item_type="movie", name="x",
                        duration_ticks=0, metadata_runtime_ticks=0)
    assert emby_api._run_time_ticks(bare) == 0


def test_episode_detail_uses_series_runtime(client, token, seed):
    db = SessionLocal()
    try:
        s = db.query(em.MediaItem).filter(em.MediaItem.guid == seed["series"]).first()
        s.metadata_runtime_ticks = 36 * 60 * 10_000_000
        db.commit()
    finally:
        db.close()
    r = client.get(f"/emby/Items/{seed['ep']}", headers=_h(token))
    body = r.json()
    assert body["RunTimeTicks"] == 36 * 60 * 10_000_000
    assert body["MediaSources"][0]["RunTimeTicks"] == 36 * 60 * 10_000_000


def test_episode_detail_media_info_not_blank(client, token, seed):
    """未探测的 strm 单集：详情页 MediaStreams 至少有一条视频轨（1080p），
    PlaybackInfo 不拼猜测轨道。"""
    r = client.get(f"/emby/Users/{seed['user_id']}/Items/{seed['ep']}", headers=_h(token))
    streams = r.json()["MediaSources"][0]["MediaStreams"]
    assert streams and streams[0]["Type"] == "Video" and streams[0]["Height"] == 1080
    r = client.post(f"/emby/Items/{seed['ep']}/PlaybackInfo", headers=_h(token), json={})
    assert r.json()["MediaSources"][0]["MediaStreams"] == []

"""剧级 PlaybackInfo 落集回归。

背景（2026-10-30 生产取证，《眷思量》案）：SenPlayer 在剧页面点播放
时，直接对 series 本体发 PlaybackInfo（IsPlayback=true&
AutoOpenLiveStream=true），从不请求集级 guid。服务端对无 file_path
的容器条目一律回空 MediaSources，客户端拿空源止步、黑屏。第 1 集
本身的 PlaybackInfo 与详情源都正常——断点只在「对整剧点播没人接」。

钉死口径：
1. series + IsPlayback=true：落到目标集，返回的源与直接请求该集
   等价（源 Id / 地址指向目标集 guid、带可验的 play_sign）。
2. 选集：按季/集序号升序（NULL 排最后），只算有 file_path 的集；
   第一部「未看完」的集优先——看完只认 UserMediaData.played（现行
   已播标记）；有进度未看完（Resume 口径）与完全没看过的都算未看完；
   全部看完 → 第 1 集（重播从头）；无可播集 → 保持空源、不报错。
3. series + IsPlayback=false（信息查询）保持回空源，逐字不变。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import play_sign
from backend.security import hash_password

init_db()

PW = "series-pb-pw"
BOUND = "seriespb_bound"
LIB_NAME = "剧级落集测试库"
TICKS = 10_000_000


def _guid():
    return uuid.uuid4().hex


def _ep_path(series_name, n):
    return (f"mount://3/剧集/{series_name} (2026)/Season 1/"
            f"{series_name} - S01E0{n}.mkv")


def _cleanup(db):
    for uname in (BOUND,):
        old = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if old:
            cred_ids = [r[0] for r in db.query(em.UserStorageBinding.credential_id)
                        .filter(em.UserStorageBinding.user_id == old.id).all()]
            db.query(em.UserMediaData).filter(
                em.UserMediaData.user_id == old.id).delete(
                synchronize_session=False)
            db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == old.id).delete(
                synchronize_session=False)
            if cred_ids:
                db.query(em.MountCredential).filter(
                    em.MountCredential.id.in_(cred_ids)).delete(
                    synchronize_session=False)
            db.query(em.EmbyApiToken).filter(
                em.EmbyApiToken.user_id == old.id).delete(
                synchronize_session=False)
            db.query(models.WebUser).filter(
                models.WebUser.id == old.id).delete()
    db.query(em.UserStorageBinding).filter(
        ~em.UserStorageBinding.user_id.in_(
            db.query(models.WebUser.id))).delete(synchronize_session=False)
    for lib in db.query(em.Library).filter(em.Library.name == LIB_NAME).all():
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == lib.id).all()]
        if ids:
            db.query(em.UserMediaData).filter(
                em.UserMediaData.item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.MediaStream).filter(
                em.MediaStream.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


def _add_series(db, lib, user_id, name, ep_paths, watch):
    """ep_paths: {集号: file_path 或 None}；watch: {集号: UserMediaData 字段}"""
    series = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="series",
                          name=name, file_path=None)
    db.add(series)
    db.flush()
    eps = {}
    for no in sorted(ep_paths):
        ep = em.MediaItem(
            guid=_guid(), library_id=lib.id, item_type="episode",
            name=f"{name} 第{no}集", series_id=series.id, season_number=1,
            episode_number=no, file_path=ep_paths[no], container="mkv",
            size=1_000_000_000, duration_ticks=24 * 60 * TICKS,
            probe_status="failed", probe_attempts=99)
        db.add(ep)
        db.flush()
        eps[no] = ep
        if no in watch:
            db.add(em.UserMediaData(user_id=user_id, item_id=ep.id,
                                    **watch[no]))
            db.flush()
    return series.guid, {no: ep.guid for no, ep in eps.items()}


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    u = models.WebUser(username=BOUND, password_hash=hash_password(PW),
                       is_active=True, is_staff=True, emby_username=BOUND,
                       emby_password=PW)
    db.add(u)
    db.flush()
    bound_id = u.id
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=bound_id, sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=bound_id, provider="drive",
                                 account_label="种子盘", credential_id=cred.id,
                                 status="active"))
    lib = em.Library(guid=_guid(), name=LIB_NAME, collection_type="tvshows",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    data = {"bound_id": bound_id}
    # 甲剧：无任何观看记录 → 应落第 1 集；同剧兼测 IsPlayback=false。
    g, eps = _add_series(db, lib, bound_id, "落集甲剧",
                         {1: _ep_path("落集甲剧", 1), 2: _ep_path("落集甲剧", 2),
                          3: _ep_path("落集甲剧", 3)}, {})
    data["fresh"] = {"series": g, "eps": eps}
    # 乙剧：第 1 集已看完 → 应落第 2 集。
    g, eps = _add_series(db, lib, bound_id, "落集乙剧",
                         {1: _ep_path("落集乙剧", 1), 2: _ep_path("落集乙剧", 2)},
                         {1: {"played": True, "play_count": 1,
                              "playback_position_ticks": 0}})
    data["watched"] = {"series": g, "eps": eps}
    # 丙剧：第 1 集有进度未看完 → 应落第 1 集（续看）。
    g, eps = _add_series(db, lib, bound_id, "落集丙剧",
                         {1: _ep_path("落集丙剧", 1), 2: _ep_path("落集丙剧", 2)},
                         {1: {"played": False, "play_count": 1,
                              "playback_position_ticks": 30 * 60 * TICKS}})
    data["resume"] = {"series": g, "eps": eps}
    # 丁剧：全部看完 → 应落第 1 集（重播从头）。
    g, eps = _add_series(db, lib, bound_id, "落集丁剧",
                         {1: _ep_path("落集丁剧", 1), 2: _ep_path("落集丁剧", 2)},
                         {1: {"played": True, "play_count": 1},
                          2: {"played": True, "play_count": 2}})
    data["rewind"] = {"series": g, "eps": eps}
    # 戊剧：唯一的集没有 file_path → 保持空源。
    g, eps = _add_series(db, lib, bound_id, "落集戊剧", {1: None}, {})
    data["empty"] = {"series": g, "eps": eps}
    db.commit()
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}SeriesPbTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}series-pb-dev-1{q}, Version={q}1.0{q}")


@pytest.fixture(scope="module")
def bound_token(client, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": BOUND, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token):
    return {"X-Emby-Token": token}


def _playback_info(client, token, guid, user_id, is_playback=True):
    # 与生产取证逐字同形的请求：POST + 查询串 IsPlayback/AutoOpenLiveStream
    params = {"UserId": str(user_id),
              "IsPlayback": "true" if is_playback else "false"}
    if is_playback:
        params["AutoOpenLiveStream"] = "true"
    r = client.post(f"/emby/Items/{guid}/PlaybackInfo", params=params,
                    headers=_h(token), json={})
    assert r.status_code == 200, r.text
    return r.json()


def _assert_resolved_to(body, ep_guid, user_id):
    sources = body.get("MediaSources") or []
    assert sources, "应落集返回非空 MediaSources，而不是空源"
    src = sources[0]
    assert src.get("Id") == ep_guid, f"源应指向目标集 {ep_guid}，实际 {src.get('Id')}"
    url = src.get("DirectStreamUrl") or ""
    parts = urlsplit(url)
    assert parts.path == f"/emby/Videos/{ep_guid}/stream.mkv", url
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q.get("MediaSourceId") == ep_guid
    assert int(q["uid"]) == user_id
    assert play_sign.verify_play_sign(
        int(q["uid"]), ep_guid, int(q["exp"]), q["sign"])


def test_series_playback_no_history_resolves_first_episode(
        client, bound_token, seed):
    body = _playback_info(client, bound_token, seed["fresh"]["series"],
                          seed["bound_id"], is_playback=True)
    _assert_resolved_to(body, seed["fresh"]["eps"][1], seed["bound_id"])


def test_series_playback_watched_first_resolves_second(
        client, bound_token, seed):
    body = _playback_info(client, bound_token, seed["watched"]["series"],
                          seed["bound_id"], is_playback=True)
    _assert_resolved_to(body, seed["watched"]["eps"][2], seed["bound_id"])


def test_series_playback_in_progress_resolves_same_episode(
        client, bound_token, seed):
    body = _playback_info(client, bound_token, seed["resume"]["series"],
                          seed["bound_id"], is_playback=True)
    _assert_resolved_to(body, seed["resume"]["eps"][1], seed["bound_id"])


def test_series_playback_all_watched_rewinds_to_first(
        client, bound_token, seed):
    body = _playback_info(client, bound_token, seed["rewind"]["series"],
                          seed["bound_id"], is_playback=True)
    _assert_resolved_to(body, seed["rewind"]["eps"][1], seed["bound_id"])


def test_series_info_query_stays_empty(client, bound_token, seed):
    body = _playback_info(client, bound_token, seed["fresh"]["series"],
                          seed["bound_id"], is_playback=False)
    assert body.get("MediaSources") == []


def test_series_without_playable_episodes_stays_empty(
        client, bound_token, seed):
    body = _playback_info(client, bound_token, seed["empty"]["series"],
                          seed["bound_id"], is_playback=True)
    assert body.get("MediaSources") == []

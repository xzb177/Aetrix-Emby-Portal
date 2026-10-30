"""详情 MediaSource 补本站 stream 地址（甲方案）回归。

背景：详情（full=True）MediaSource 此前申报 SupportsDirectPlay=True 却
不带 DirectPlayUrl/DirectStreamUrl，不走 PlaybackInfo、直放详情源的
播放器（SenPlayer 关掉「始终播放原始路径」时）手里没地址、服务端零
请求直接黑屏。修复后详情源带与 PlaybackInfo 同形同构造点
（_signed_local_stream_url）的本站 Videos/stream 地址。

钉死口径：
1. 已绑定用户：详情 movie/episode 源带同形地址（带容器后缀、static、
   MediaSourceId、api_key 轨 + play_sign 轨），签名可验，且与同一
   条目 PlaybackInfo 下发的地址同形（归一化 exp/sign 后逐字一致）。
2. 详情仍不带 Path、不带真实路径、不出现 googleusercontent。
3. 容器类（series）详情源不加播放地址；列表响应仍无 MediaSources。
4. 未绑定用户：详情照常可看（浏览不拦），但地址不带签名轨；拿这个
   地址打流端点仍被播放门禁现场 403（签名是「已过门禁才下发」的
   凭证，详情面不许先发）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
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

PW = "detail-url-pw"
BOUND = "detailurl_bound"
FREE = "detailurl_free"
LIB_NAME = "详情地址测试库"
EP_PATH = "mount://3/剧集/详情测试剧 (2026)/Season 1/详情测试剧 - S01E01.mkv"
MOVIE_PATH = "mount://3/电影/详情测试片 (2026)/详情测试片.mp4"
TICKS = 10_000_000


def _guid():
    return uuid.uuid4().hex


def _cleanup(db):
    for uname in (BOUND, FREE):
        old = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if old:
            cred_ids = [r[0] for r in db.query(em.UserStorageBinding.credential_id)
                        .filter(em.UserStorageBinding.user_id == old.id).all()]
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
    _cleanup(db)
    users = {}
    for uname in (BOUND, FREE):
        u = models.WebUser(username=uname, password_hash=hash_password(PW),
                           is_active=True, is_staff=True, emby_username=uname,
                           emby_password=hash_password(PW))
        db.add(u)
        db.flush()
        users[uname] = u.id
    # 已绑定用户：active 的 Drive 绑定（播放门禁口径，管理员同样要绑）
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=users[BOUND], sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=users[BOUND], provider="drive",
                                 account_label="种子盘", credential_id=cred.id,
                                 status="active"))
    lib = em.Library(guid=_guid(), name=LIB_NAME, collection_type="tvshows",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    series = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="series",
                          name="详情测试剧",
                          file_path="mount://3/剧集/详情测试剧 (2026)")
    db.add(series)
    db.flush()
    ep = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="episode",
                      name="第 1 集", series_id=series.id, season_number=1,
                      episode_number=1, file_path=EP_PATH, container="mkv",
                      size=1_000_000_000, duration_ticks=45 * 60 * TICKS,
                      probe_status="failed", probe_attempts=99)
    db.add(ep)
    movie = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="movie",
                         name="详情测试片", file_path=MOVIE_PATH,
                         container="mp4", size=2_000_000_000,
                         duration_ticks=118 * 60 * TICKS,
                         probe_status="failed", probe_attempts=99)
    db.add(movie)
    db.commit()
    data = {"bound_id": users[BOUND], "free_id": users[FREE],
            "lib": lib.guid, "series": series.guid, "ep": ep.guid,
            "movie": movie.guid}
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
    return (f"MediaBrowser Client={q}DetailUrlTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}detail-url-dev-1{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def bound_token(client, seed):
    return _token(client, BOUND)


@pytest.fixture(scope="module")
def free_token(client, seed):
    return _token(client, FREE)


def _h(token):
    return {"X-Emby-Token": token}


def _detail_src(client, token, guid):
    r = client.get(f"/emby/Items/{guid}", headers=_h(token))
    assert r.status_code == 200, r.text
    return r


def _qs(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _assert_signed_shape(url, guid, suffix, user_id):
    assert urlsplit(url).path == f"/emby/Videos/{guid}/stream{suffix}"
    q = _qs(url)
    assert q.get("static") == "true"
    assert q.get("MediaSourceId") == guid
    assert q.get("api_key"), "Emby token 轨应在场（与 PlaybackInfo 同形）"
    uid, exp, sign = int(q["uid"]), int(q["exp"]), q["sign"]
    assert uid == user_id
    assert play_sign.verify_play_sign(uid, guid, exp, sign)


def test_detail_episode_has_signed_stream_url(client, bound_token, seed):
    src = _detail_src(client, bound_token, seed["ep"]).json()["MediaSources"][0]
    assert src["DirectPlayUrl"] == src["DirectStreamUrl"]
    _assert_signed_shape(src["DirectStreamUrl"], seed["ep"], ".mkv",
                         seed["bound_id"])


def test_detail_movie_has_signed_stream_url(client, bound_token, seed):
    src = _detail_src(client, bound_token, seed["movie"]).json()["MediaSources"][0]
    assert src["DirectPlayUrl"] == src["DirectStreamUrl"]
    _assert_signed_shape(src["DirectStreamUrl"], seed["movie"], ".mp4",
                         seed["bound_id"])


def test_detail_shape_matches_playbackinfo(client, bound_token, seed):
    detail_url = _detail_src(
        client, bound_token, seed["ep"]).json()["MediaSources"][0]["DirectStreamUrl"]
    r = client.post(f"/emby/Items/{seed['ep']}/PlaybackInfo",
                    headers=_h(bound_token), json={})
    assert r.status_code == 200, r.text
    pi_url = r.json()["MediaSources"][0]["DirectStreamUrl"]

    def norm(u):
        u = re.sub(r"exp=\d+", "exp=*", u)
        u = re.sub(r"sign=[^&]+", "sign=*", u)
        return u
    assert norm(detail_url) == norm(pi_url)


def test_detail_hides_path_and_no_usercontent(client, bound_token, seed):
    r = _detail_src(client, bound_token, seed["ep"])
    assert "googleusercontent" not in r.text
    assert "mount://3" not in r.text
    src = r.json()["MediaSources"][0]
    assert "Path" not in src


def test_detail_series_has_no_stream_url(client, bound_token, seed):
    src = _detail_src(client, bound_token, seed["series"]).json()["MediaSources"][0]
    assert "DirectPlayUrl" not in src
    assert "DirectStreamUrl" not in src


def test_list_items_have_no_mediasources(client, bound_token, seed):
    r = client.get(
        f"/emby/Users/{seed['bound_id']}/Items?ParentId={seed['lib']}",
        headers=_h(bound_token))
    assert r.status_code == 200, r.text
    items = r.json()["Items"]
    assert items, "种子库列表不应为空"
    # 列表 DTO 恒带 MediaSources 空数组（_item_dto 基础字段面），口径按
    # 实质钉死：列表里没有任何源、更没有任何播放地址。
    for it in items:
        assert it.get("MediaSources") in (None, [])
    assert "DirectStreamUrl" not in r.text
    assert "DirectPlayUrl" not in r.text


def test_unbound_user_detail_url_unsigned_and_stream_blocked(
        client, free_token, seed):
    # 详情本身可看（浏览不拦）
    r = _detail_src(client, free_token, seed["ep"])
    src = r.json()["MediaSources"][0]
    url = src["DirectStreamUrl"]
    # 未绑定用户拿到的地址不带签名轨（凭证不许在详情面先发）
    q = _qs(url)
    assert "sign" not in q and "uid" not in q and "exp" not in q
    # 拿这个地址打流端点：播放门禁现场 403 + 去绑定引导
    parts = urlsplit(url)
    target = parts.path + "?" + parts.query
    r2 = client.get(target, headers=_h(free_token))
    assert r2.status_code == 403, r2.text
    assert "请先绑定自己的网盘账号才能点播" in r2.text

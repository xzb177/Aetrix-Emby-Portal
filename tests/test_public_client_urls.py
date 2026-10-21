"""客户端绝对 URL 不得泄漏源站，反代后面也不能下发 http 播放地址。

两条必须同时成立：

- 配了 ``EMBY_PUBLIC_URL`` 时，PlaybackInfo（DirectStreamUrl / DirectPlayUrl /
  TranscodingUrl）、字幕 DeliveryUrl、本服务图片绝对地址、System/Info，以及门户
  交给播放器的导入 scheme，全部用这个地址。请求 Host 即使是源站 IP 也不许出现。
- 没配时，只有可信反代（回环 / ``TRUSTED_PROXIES`` / ``FORWARDED_ALLOW_IPS``）
  的 ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` 才生效；不可信对端伪造的头被忽略。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from starlette.requests import Request

from backend.emby_server import api as emby_api
from backend.emby_server import models as em
from backend.emby_server import portal
from backend.emby_server.public_url import client_base_url, configured_public_url

ORIGIN_IP = "15.204.95.101"
PUBLIC = "https://cdn.135505.autos"
ORIGIN_HOST = "emby.135505.autos"


def _request(
    *,
    scheme: str = "http",
    host: str = ORIGIN_IP,
    client: str = "203.0.113.9",
    forwarded_proto: str | None = None,
    forwarded_host: str | None = None,
    state: dict | None = None,
) -> Request:
    headers = [(b"host", host.encode())]
    if forwarded_proto is not None:
        headers.append((b"x-forwarded-proto", forwarded_proto.encode()))
    if forwarded_host is not None:
        headers.append((b"x-forwarded-host", forwarded_host.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "scheme": scheme,
        "server": ("127.0.0.1", 80),
        "client": (client, 50000),
        "root_path": "",
    }
    if state is not None:
        scope["state"] = state
    return Request(scope)


def _clear_public_env(monkeypatch):
    monkeypatch.delenv("EMBY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)


class _EmptyDb:
    def query(self, *_args, **_kwargs):
        raise RuntimeError("空配置")


def _abs_urls(obj):
    if isinstance(obj, str):
        if obj.startswith(("http://", "https://")):
            yield obj
        return
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _abs_urls(value)
        return
    if isinstance(obj, list):
        for value in obj:
            yield from _abs_urls(value)


# ==================== 基址本身 ====================

def test_public_url_ignores_origin_host_and_forged_forwarded_headers(monkeypatch):
    monkeypatch.setenv("EMBY_PUBLIC_URL", PUBLIC + "/")
    req = _request(
        host=ORIGIN_IP,
        client="203.0.113.9",
        forwarded_proto="http",
        forwarded_host=ORIGIN_HOST,
    )
    assert configured_public_url() == PUBLIC
    assert client_base_url(req) == PUBLIC
    assert emby_api._base_url(req) == PUBLIC
    assert emby_api._public_base_url(req) == PUBLIC


def test_trusted_loopback_proxy_upgrades_scheme_without_public_url(monkeypatch):
    _clear_public_env(monkeypatch)
    req = _request(
        scheme="http",
        host=ORIGIN_IP,
        client="127.0.0.1",
        forwarded_proto="https",
        forwarded_host="emby.example.com:443",
    )
    assert client_base_url(req) == "https://emby.example.com"
    assert portal._request_base_url(req) == "https://emby.example.com"


def test_docker_gateway_honored_only_when_explicitly_trusted(monkeypatch):
    """172.18.0.1 是容器看到的网关，默认不可信；写进信任名单后才采信 https。"""
    _clear_public_env(monkeypatch)
    req = _request(
        client="172.18.0.1",
        forwarded_proto="https",
        forwarded_host="emby.example.com",
    )
    assert client_base_url(req).startswith("http://")
    assert "emby.example.com" not in client_base_url(req)

    monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16")
    assert client_base_url(req) == "https://emby.example.com"

    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.18.0.1")
    assert client_base_url(req) == "https://emby.example.com"


def test_untrusted_peer_cannot_forge_https_or_host(monkeypatch):
    _clear_public_env(monkeypatch)
    req = _request(
        scheme="http",
        host=ORIGIN_IP,
        client="203.0.113.50",
        forwarded_proto="https",
        forwarded_host="evil.example",
    )
    base = client_base_url(req)
    assert base == f"http://{ORIGIN_IP}"
    assert "evil.example" not in base
    assert not base.startswith("https://")


def test_star_forwarded_allow_is_explicit_not_default(monkeypatch):
    _clear_public_env(monkeypatch)
    req = _request(client="198.51.100.8", forwarded_proto="https", forwarded_host="emby.example.com")
    assert client_base_url(req).startswith("http://")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    assert client_base_url(req) == "https://emby.example.com"


def test_restored_real_ip_still_trusts_forwarded_proto(monkeypatch):
    """CF 中间件把 client 改成用户 IP 之后，转发头仍然来自已经核过的反代。"""
    _clear_public_env(monkeypatch)
    req = _request(
        client="203.0.113.7",
        forwarded_proto="https",
        forwarded_host="emby.example.com",
        state={"real_ip_from_proxy": "203.0.113.7"},
    )
    assert client_base_url(req) == "https://emby.example.com"


def test_invalid_forwarded_proto_falls_back(monkeypatch):
    _clear_public_env(monkeypatch)
    req = _request(client="127.0.0.1", forwarded_proto="javascript", forwarded_host="emby.example.com")
    # 协议非法时用请求自己的 scheme，主机仍取可信的 X-Forwarded-Host
    assert client_base_url(req) == "http://emby.example.com"


# ==================== 图片绝对地址与导入 scheme ====================

def test_image_and_import_links_use_public_url_not_origin(monkeypatch):
    monkeypatch.setenv("EMBY_PUBLIC_URL", PUBLIC)
    req = _request(host=ORIGIN_IP, forwarded_proto="http", forwarded_host=ORIGIN_HOST)
    item = em.MediaItem(guid="a" * 32, item_type="movie", name="海报", poster_path="/tmp/poster.jpg")
    image = emby_api._image_url(client_base_url(req), item)
    assert image == f"{PUBLIC}/emby/Items/{item.guid}/Images/Primary"
    assert ORIGIN_IP not in image
    assert ORIGIN_HOST not in image

    # 第三方图床地址不是我们的源站，不能改写成 EMBY_PUBLIC_URL
    remote = em.MediaItem(
        guid="b" * 32, item_type="movie", name="远程",
        primary_image_url="https://image.tmdb.org/t/p/w500/x.jpg",
    )
    assert emby_api._image_url(client_base_url(req), remote).startswith("https://image.tmdb.org/")

    url, source = portal.resolve_emby_base_url_with_source(_EmptyDb(), None, req)
    assert url == PUBLIC
    assert source == portal.URL_SOURCE_ENV
    rex = portal._rex_import_link(url, "emby_user")
    vidhub = portal._vidhub_import_link(url, "emby_user")
    sen = portal._senplayer_import_link(url, "emby_user")
    for link in (rex, vidhub, sen):
        assert "cdn.135505.autos" in link
        assert ORIGIN_IP not in link
        assert ORIGIN_HOST not in link
    for link in (rex, vidhub):
        assert parse_qs(urlsplit(link).query)["scheme"] == ["https"]
        assert parse_qs(urlsplit(link).query)["host"] == ["cdn.135505.autos"]
    assert "address=https://cdn.135505.autos" in unquote(sen)


def test_import_scheme_is_https_behind_trusted_proxy_without_public_url(monkeypatch):
    _clear_public_env(monkeypatch)
    req = _request(
        client="127.0.0.1",
        host=ORIGIN_IP,
        forwarded_proto="https",
        forwarded_host="emby.example.com",
    )
    url, source = portal.resolve_emby_base_url_with_source(_EmptyDb(), None, req)
    assert url == "https://emby.example.com"
    assert source == portal.URL_SOURCE_REQUEST
    rex = portal._rex_import_link(url, "emby_user")
    vidhub = portal._vidhub_import_link(url, "emby_user")
    sen = portal._senplayer_import_link(url, "emby_user")
    assert "scheme=https" in rex
    assert "scheme=https" in vidhub
    assert "https%3A%2F%2Femby.example.com" in sen or "https://emby.example.com" in sen
    for link in (rex, vidhub, sen):
        assert not link.startswith("http://")
        assert ORIGIN_IP not in link


def test_subtitle_delivery_url_uses_same_base(monkeypatch):
    monkeypatch.setenv("EMBY_PUBLIC_URL", PUBLIC)
    item = em.MediaItem(guid="c" * 32, item_type="movie", name="字幕")
    stream = em.MediaStream(stream_index=2, stream_type="Subtitle", codec="subrip")
    item.streams = [stream]
    dto = emby_api._stream_dto(stream, client_base_url(_request(host=ORIGIN_IP)), item, "")
    delivery = dto["DeliveryUrl"]
    assert delivery.startswith(f"{PUBLIC}/emby/Videos/{item.guid}/")
    assert "/Subtitles/2/Stream.vtt" in delivery
    assert ORIGIN_IP not in delivery


# ==================== 真实 PlaybackInfo / System/Info ====================

PW = "public-url-pw"
UNAME = "publicurl_cara"


def _guid():
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def seed():
    from backend import models
    from backend.database import SessionLocal, init_db
    from backend.security import hash_password

    init_db()
    db = SessionLocal()
    old = db.query(models.WebUser).filter(models.WebUser.username == UNAME).first()
    if old:
        db.query(em.EmbyApiToken).filter(em.EmbyApiToken.user_id == old.id).delete(
            synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
    for lib in db.query(em.Library).filter(em.Library.name == "公开地址测试库").all():
        ids = [r[0] for r in db.query(em.MediaItem.id).filter(em.MediaItem.library_id == lib.id).all()]
        if ids:
            db.query(em.MediaStream).filter(em.MediaStream.item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()
    user = models.WebUser(
        username=UNAME, password_hash="x", is_active=True, is_staff=True,
        emby_username=UNAME, emby_password=hash_password(PW),
        enable_video_transcoding=True,
    )
    db.add(user)
    lib = em.Library(guid=_guid(), name="公开地址测试库", collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    movie = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie", name="公开地址电影",
        file_path="/tmp/none/public-url.mp4", container="mp4", poster_path="/tmp/none/poster.jpg",
    )
    db.add(movie)
    db.flush()
    db.add(em.MediaStream(
        item_id=movie.id, stream_index=2, stream_type="Subtitle", codec="subrip",
        display_title="中文",
    ))
    db.commit()
    data = {"movie": movie.guid, "user_id": user.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        row = db.query(em.Library).filter(em.Library.name == "公开地址测试库").first()
        if row is not None:
            ids = [r[0] for r in db.query(em.MediaItem.id).filter(em.MediaItem.library_id == row.id).all()]
            if ids:
                db.query(em.MediaStream).filter(em.MediaStream.item_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(em.Library).filter(em.Library.id == row.id).delete()
        db.query(models.WebUser).filter(models.WebUser.username == UNAME).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="module")
def app():
    from backend.main import app as fastapi_app
    return fastapi_app


@pytest.fixture(scope="module")
def token(app, seed):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="PublicUrl", Device="iOS", '
                 'DeviceId="public-url-dev", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _playback(app, token, movie, *, client_host: str, base_url: str, extra_headers: dict):
    from fastapi.testclient import TestClient

    client = TestClient(app, base_url=base_url, client=(client_host, 50000))
    headers = {"X-Emby-Token": token, **extra_headers}
    info = client.get("/emby/System/Info", headers=headers)
    play = client.post(f"/emby/Items/{movie}/PlaybackInfo", headers=headers, json={})
    return info, play


def test_playback_info_uses_public_url_when_host_is_origin_ip(monkeypatch, app, seed, token):
    monkeypatch.setenv("EMBY_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("PLAYBACKINFO_CACHE_TTL", "0")
    info, play = _playback(
        app, token, seed["movie"],
        client_host="203.0.113.9",
        base_url=f"http://{ORIGIN_IP}",
        extra_headers={
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": ORIGIN_HOST,
        },
    )
    assert info.status_code == 200, info.text
    body_info = info.json()
    assert body_info["LocalAddress"] == PUBLIC
    assert body_info["WanAddress"] == PUBLIC

    assert play.status_code == 200, play.text
    src = play.json()["MediaSources"][0]
    for key in ("DirectStreamUrl", "DirectPlayUrl", "TranscodingUrl"):
        assert src[key].startswith(PUBLIC + "/"), src[key]
        assert ORIGIN_IP not in src[key]
        assert ORIGIN_HOST not in src[key]
        assert not src[key].startswith("http://")
    assert src["DirectPlayUrl"] == src["DirectStreamUrl"]
    deliveries = [
        st["DeliveryUrl"] for st in src["MediaStreams"]
        if isinstance(st, dict) and st.get("DeliveryUrl")
    ]
    assert deliveries, src["MediaStreams"]
    for url in deliveries:
        assert url.startswith(PUBLIC + "/"), url
        assert "/Subtitles/" in url
        assert ORIGIN_IP not in url
    for url in _abs_urls(play.json()):
        assert url.startswith(PUBLIC), url
        assert ORIGIN_IP not in url
        assert ORIGIN_HOST not in url


def test_playback_info_is_https_behind_trusted_proxy_without_public_url(monkeypatch, app, seed, token):
    _clear_public_env(monkeypatch)
    monkeypatch.setenv("PLAYBACKINFO_CACHE_TTL", "0")
    info, play = _playback(
        app, token, seed["movie"],
        client_host="127.0.0.1",
        base_url=f"http://{ORIGIN_IP}",
        extra_headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "emby.example.com",
        },
    )
    assert info.status_code == 200, info.text
    assert info.json()["LocalAddress"] == "https://emby.example.com"
    assert info.json()["WanAddress"] == "https://emby.example.com"
    assert play.status_code == 200, play.text
    src = play.json()["MediaSources"][0]
    for key in ("DirectStreamUrl", "DirectPlayUrl", "TranscodingUrl"):
        assert src[key].startswith("https://emby.example.com/"), src[key]
        assert not src[key].startswith("http://")
        assert ORIGIN_IP not in src[key]
    for url in _abs_urls(play.json()):
        assert url.startswith("https://"), url
        assert not url.startswith("http://")
        assert ORIGIN_IP not in url


def test_playback_info_ignores_forged_https_from_untrusted_peer(monkeypatch, app, seed, token):
    _clear_public_env(monkeypatch)
    monkeypatch.setenv("PLAYBACKINFO_CACHE_TTL", "0")
    _info, play = _playback(
        app, token, seed["movie"],
        client_host="203.0.113.50",
        base_url=f"http://{ORIGIN_IP}",
        extra_headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.example",
        },
    )
    assert play.status_code == 200, play.text
    src = play.json()["MediaSources"][0]
    assert src["DirectStreamUrl"].startswith(f"http://{ORIGIN_IP}/")
    assert "evil.example" not in src["DirectStreamUrl"]
    assert src["TranscodingUrl"].startswith(f"http://{ORIGIN_IP}/")

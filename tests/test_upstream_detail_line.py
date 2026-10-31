"""详情页「上游」线路（U-02 客户端入口）回归。

锁住的口径（用户 2026-10-31 亲点形态：详情页来源列表多一条上游线路）：
- 本地条目有上游映射（源存在且启用）时，详情 DTO 与 PlaybackInfo 的
  MediaSources 末尾多一条上游线路：Id=``<guid>:upstream``、
  Name=``<片名> · 上游 · <源名>``，取流地址走我方签名口径
  （/emby/Videos/<guid>/upstream/stream），不把 (source_id,
  upstream_item_id) 裸交给前端；
- 无映射 / 源停用 → 线路不出现；
- SystemConfig 开关 upstream_detail_line_enabled 关 → 列表不出现、
  取流口 404 upstream_line_disabled；
- 未过播放门禁（无 active 绑定）→ 与本地主源同口径：列表里线路地址
  不带签名轨；取流口 403（#1341 门禁）；
- 取流口点播时现查映射复验：映射没了 → 404 upstream_mapping_not_found；
  源停用了 → 映射按 O-10 读契约（get_best_mapping 默认 validate=True）
  在现查时即不可见、同无映射口径 404，明示失败不回落任何其它线路
  （显式指定 source_id 的 resolve 路径仍回 409 upstream_source_disabled，
  不经本口）；
- 复验通过 → resolve_upstream_stream 出 302 到上游，且起播事件落
  provider=upstream 行（生产历史 0 条，本单就是补这个入口）。

测试形态照 test_detail_mediasource_stream_url.py：真 TestClient 打整
app、种子数据直落 app 库；resolve 的上游探活打桩（不发真网络请求）。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import backend.models as models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import play_events, play_sign, upstream_sources as us
from backend.security import hash_password

init_db()

TICKS = 10_000_000
PW = "DetailLine#2026"
BOUND = "upline_bound"
FREE = "upline_free"
LIB_NAME = "上游线路测试库"
SRC_ON = "乐观服"
SRC_OFF = "停用源"
UP_MOVIE_ID = "up-movie-1"
SWITCH_KEY = "upstream_aggregate_detail_line_enabled"


def _guid() -> str:
    return uuid.uuid4().hex


def _cleanup(db: Session) -> None:
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name.in_(["上游测试片", "上游停用片", "上游无映射集"])
    ).all()
    ids = [r.id for r in rows]
    if ids:
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    srcs = db.query(em.UpstreamSource).filter(
        em.UpstreamSource.name.in_([SRC_ON, SRC_OFF])).all()
    for s in srcs:
        db.delete(s)
    db.query(em.Library).filter(em.Library.name == LIB_NAME).delete()
    for uname in (BOUND, FREE):
        u = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if u is not None:
            db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == u.id).delete()
            db.query(em.MountCredential).filter(
                em.MountCredential.owner_user_id == u.id).delete()
            db.delete(u)
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
    cred = em.MountCredential(mount_id=None, scope="user",
                              owner_user_id=users[BOUND], sealed_credential="",
                              key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=users[BOUND], provider="drive",
                                 account_label="种子盘", credential_id=cred.id,
                                 status="active"))
    src_on = us.create_source(db, name=SRC_ON, source_type="emby",
                              base_url="https://up.example.com",
                              auth_method="api_key")
    us.enable_source(db, src_on)
    src_off = us.create_source(db, name=SRC_OFF, source_type="emby",
                               base_url="https://off.example.com",
                               auth_method="api_key")
    lib = em.Library(guid=_guid(), name=LIB_NAME, collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    movie = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie", name="上游测试片",
        file_path="mount://3/电影/上游测试片 (2026)/movie.mp4",
        container="mp4", size=2_000_000_000,
        duration_ticks=118 * 60 * TICKS,
        probe_status="failed", probe_attempts=99)
    movie_off = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="movie", name="上游停用片",
        file_path="mount://3/电影/上游停用片 (2026)/movie.mp4",
        container="mp4", size=2_000_000_000,
        duration_ticks=118 * 60 * TICKS,
        probe_status="failed", probe_attempts=99)
    ep = em.MediaItem(
        guid=_guid(), library_id=lib.id, item_type="episode",
        name="上游无映射集",
        file_path="mount://3/剧集/上游无映射剧/S01E01.mkv",
        container="mkv", size=1_000_000_000,
        duration_ticks=45 * 60 * TICKS,
        probe_status="failed", probe_attempts=99)
    db.add_all([movie, movie_off, ep])
    db.flush()
    db.add(em.UpstreamItemMapping(
        local_item_id=movie.id, source_id=src_on.id,
        upstream_item_id=UP_MOVIE_ID, confidence=0.95, evidence="tmdb"))
    db.add(em.UpstreamItemMapping(
        local_item_id=movie_off.id, source_id=src_off.id,
        upstream_item_id="up-movie-off", confidence=0.95, evidence="tmdb"))
    db.commit()
    data = {"bound_id": users[BOUND], "free_id": users[FREE],
            "src_on": src_on.id, "src_off": src_off.id,
            "movie": movie.guid, "movie_off": movie_off.guid,
            "ep": ep.guid}
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
    return TestClient(app, raise_server_exceptions=False)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}UpstreamLineTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}upline-dev-1{q}, Version={q}1.0{q}")


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


def _detail(client, token, guid):
    r = client.get(f"/emby/Items/{guid}", headers=_h(token))
    assert r.status_code == 200, r.text
    return r


def _qs(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _upstream_line(body):
    return [s for s in body.get("MediaSources") or []
            if str(s.get("Id", "")).endswith(":upstream")]


def _set_switch(value):
    """经生产配置路径（config_service + upstream_aggregate 家族）改开关。"""
    from backend import config_service
    db = SessionLocal()
    try:
        config_service.save_config(
            db, {SWITCH_KEY: value}, schema="upstream_aggregate")
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def switch_off():
    _set_switch(False)
    try:
        yield
    finally:
        _set_switch(None)


# ---------------------------------------------------------------- 列表面


def test_detail_with_mapping_has_upstream_line(client, bound_token, seed):
    r = _detail(client, bound_token, seed["movie"])
    body = r.json()
    lines = _upstream_line(body)
    assert len(lines) == 1, body.get("MediaSources")
    assert body["MediaSourceCount"] == 2
    line = lines[0]
    # 排在本地主源之后，标签带源名，与既有变体行同为「名 · 标签」形态
    assert body["MediaSources"][-1] is line or body["MediaSources"][-1] == line
    assert line["Name"] == "上游测试片 · 上游 · 乐观服"
    url = line["DirectPlayUrl"]
    assert line["DirectStreamUrl"] == url
    assert urlsplit(url).path == f"/emby/Videos/{seed['movie']}/upstream/stream"
    q = _qs(url)
    assert q.get("MediaSourceId") == f"{seed['movie']}:upstream"
    assert q.get("api_key"), "Emby token 轨应在场（与主源同形）"
    uid, exp, sign = int(q["uid"]), int(q["exp"]), q["sign"]
    assert uid == seed["bound_id"]
    assert play_sign.verify_play_sign(uid, seed["movie"], exp, sign)
    assert line["SupportsDirectPlay"] is True
    assert line["SupportsDirectStream"] is True
    assert line["SupportsTranscoding"] is False
    # 不把上游坐标裸交给前端：响应里不得出现上游域名 / 上游条目 id
    assert "up.example.com" not in r.text
    assert UP_MOVIE_ID not in r.text


def test_detail_without_mapping_no_upstream_line(client, bound_token, seed):
    body = _detail(client, bound_token, seed["ep"]).json()
    assert _upstream_line(body) == []
    assert body["MediaSourceCount"] == 1


def test_detail_source_disabled_no_upstream_line(client, bound_token, seed):
    body = _detail(client, bound_token, seed["movie_off"]).json()
    assert _upstream_line(body) == []
    assert body["MediaSourceCount"] == 1


def test_detail_switch_off_no_upstream_line(
        client, bound_token, seed, switch_off):
    body = _detail(client, bound_token, seed["movie"]).json()
    assert _upstream_line(body) == []
    assert body["MediaSourceCount"] == 1


def test_detail_unbound_user_line_unsigned(client, free_token, seed):
    # 与本地主源同口径（#1511）：未绑定的用户详情可看、线路在场，
    # 但地址不带签名轨（点播仍会被门禁拦，见取流口用例）。
    body = _detail(client, free_token, seed["movie"]).json()
    lines = _upstream_line(body)
    assert len(lines) == 1
    q = _qs(lines[0]["DirectStreamUrl"])
    assert "sign" not in q and "exp" not in q and "uid" not in q


def test_playbackinfo_has_upstream_line(client, bound_token, seed):
    r = client.post(f"/emby/Items/{seed['movie']}/PlaybackInfo",
                    headers=_h(bound_token), json={})
    assert r.status_code == 200, r.text
    lines = _upstream_line(r.json())
    assert len(lines) == 1
    line = lines[0]
    assert line["Name"] == "上游测试片 · 上游 · 乐观服"
    q = _qs(line["DirectStreamUrl"])
    assert play_sign.verify_play_sign(
        int(q["uid"]), seed["movie"], int(q["exp"]), q["sign"])


def test_playbackinfo_without_mapping_no_line(client, bound_token, seed):
    r = client.post(f"/emby/Items/{seed['ep']}/PlaybackInfo",
                    headers=_h(bound_token), json={})
    assert r.status_code == 200, r.text
    assert _upstream_line(r.json()) == []


# ---------------------------------------------------------------- 取流口


@pytest.fixture()
def upstream_env(monkeypatch):
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    from backend.emby_server import upstream_playback as up
    monkeypatch.setattr(
        up, "_default_http_range_probe",
        lambda url, headers=None, timeout=None: (206, None))


def _line_url(client, token, guid):
    body = _detail(client, token, guid).json()
    return _upstream_line(body)[0]["DirectStreamUrl"]


def test_line_stream_redirects_and_records_event(
        client, bound_token, seed, upstream_env):
    play_events.reset_for_tests()
    url = _line_url(client, bound_token, seed["movie"])
    path = re.sub(r"^https?://[^/]+", "", url)
    r = client.get(path, headers=_h(bound_token), follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers.get("location", "")
    assert "up.example.com" in loc and UP_MOVIE_ID in loc
    play_events.flush_pending()
    db = SessionLocal()
    try:
        rows = (db.query(em.PlayStartEvent)
                .filter(em.PlayStartEvent.provider == "upstream",
                        em.PlayStartEvent.user_id == seed["bound_id"])
                .all())
        assert rows, "起播事件应落 provider=upstream 行"
        assert rows[-1].method == "upstream"
    finally:
        db.close()


def test_line_stream_no_mapping_404(client, bound_token, seed, upstream_env):
    r = client.get(f"/emby/Videos/{seed['ep']}/upstream/stream",
                   headers=_h(bound_token), follow_redirects=False)
    assert r.status_code == 404, r.text
    assert r.json().get("code") == "upstream_mapping_not_found"
    assert "location" not in r.headers


def test_line_stream_source_disabled_404(
        client, bound_token, seed, upstream_env):
    # O-10 读契约对齐（#1619 合入后）：源停用的映射在 get_best_mapping
    # 默认 validate=True 下即不可见，取流口同无映射口径回 404，
    # 不再是变基前「先命中映射、resolve 再抛」的 409。
    r = client.get(f"/emby/Videos/{seed['movie_off']}/upstream/stream",
                   headers=_h(bound_token), follow_redirects=False)
    assert r.status_code == 404, r.text
    assert r.json().get("code") == "upstream_mapping_not_found"
    assert "location" not in r.headers


def test_line_stream_switch_off_404(
        client, bound_token, seed, upstream_env, switch_off):
    r = client.get(f"/emby/Videos/{seed['movie']}/upstream/stream",
                   headers=_h(bound_token), follow_redirects=False)
    assert r.status_code == 404, r.text
    assert r.json().get("code") == "upstream_line_disabled"


def test_line_stream_unbound_user_blocked_403(
        client, free_token, seed, upstream_env):
    url = _line_url(client, free_token, seed["movie"])
    path = re.sub(r"^https?://[^/]+", "", url)
    r = client.get(path, headers=_h(free_token), follow_redirects=False)
    assert r.status_code == 403, r.text
    assert "location" not in r.headers

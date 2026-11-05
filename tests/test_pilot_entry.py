"""pilot 独立入口（白名单专属线路改造，2026-11-05 用户亲定）回归。

锁住的口径：
- 入口识别只认请求头 X-Aetrix-Pilot-Entry：值与配置键
  pilot_entry_secret 完全一致且 pilot_entry_enabled 为开，才算
  pilot 入口；错值/未配密钥/开关关一律按普通入口处理（防外部
  伪造该头直连 emby 冒充）。两键均为纯 SystemConfig 键
  （upstream_aggregate 家族），读取走 integrations.store。
- pilot 入口 + 非白名单：登录（auth.issue_token 执法点，与播放器
  黑名单同处）与 PlaybackInfo 两处各拒一道，403 +
  code=pilot_entry_whitelist_only（覆盖拿别处令牌混进来）。
- pilot 入口 + 白名单：PlaybackInfo 的全部 applecms 线路改走
  飞牛腿——取流 URL 指向 wl-stream 口且 host 为飞牛基址的 origin
  （播放器后续取流经飞牛 nginx 时天然带标记头）；不按专属名单
  筛选；MediaSource 命名保持普通画质标（不加「专属线路」）、
  Id 不带 :wl。普通入口不再追加 :wl 专属线（旧形态撤除）。
- wl-stream 口：带合法 pilot 标记 + 白名单 → 任意 applecms 映射
  放行（跳过名单查）；无标记维持旧三查（名单外 404
  exclusive_line_not_listed、名单内放行）；无标记的普通 msid
  （无 :wl）仍 404 upstream_mapping_not_found。
- 管理口在既有专属线路配置口上扩展 pilot 字段：开关回显、
  密钥只回已配状态（原文不回浏览器）。

测试形态照 test_upstream_exclusive_line.py：真 TestClient 打整
app、种子直落 app 库；上游解析打桩（不发真网络请求）。密钥用
假值，只许出现在本测试文件。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
import uuid
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import backend.models as models
from backend import codes
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import play_sign, upstream_playback as up
from backend.security import hash_password

init_db()

PW = "PilotEntry#2026"
WL_USER = "pilotent_wl"
PLAIN_USER = "pilotent_plain"
LIB_NAME = "pilot入口测试库"
SRC_IN = "pilot名单内源"
SRC_OUT = "pilot名单外源"
ITEM_IN = "pilot名单内片"
ITEM_OUT = "pilot名单外片"
WL_CODE = "VIP-PILOTENT0001"
PILOT_BASE = "https://pilot.example.test/p/TESTTOKEN"
PILOT_ORIGIN = "https://pilot.example.test"
FAKE_SECRET = "pilot-test-secret-0001"
HEADER = "X-Aetrix-Pilot-Entry"
UP_URL = "https://v.fengbao14.com/play/xyz/index.m3u8?k=1"
IDS_KEY = up.KEY_EXCLUSIVE_SOURCE_IDS
BASE_KEY = up.KEY_FEINIU_PILOT_BASE_URL


def _guid() -> str:
    return uuid.uuid4().hex


def _cleanup(db: Session) -> None:
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name.in_([ITEM_IN, ITEM_OUT])).all()
    ids = [r.id for r in rows]
    if ids:
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    for s in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name.in_([SRC_IN, SRC_OUT])).all():
        db.delete(s)
    db.query(em.Library).filter(em.Library.name == LIB_NAME).delete()
    for uname in (WL_USER, PLAIN_USER):
        u = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if u is not None:
            db.query(models.CodeRedemption).filter(
                models.CodeRedemption.user_id == u.id
            ).delete(synchronize_session=False)
            db.delete(u)
    db.query(models.RegistrationCode).filter(
        models.RegistrationCode.code == WL_CODE
    ).delete(synchronize_session=False)
    db.commit()


def _save(values):
    from backend import config_service
    db = SessionLocal()
    try:
        config_service.save_config(db, values, schema="upstream_aggregate")
        db.commit()
    finally:
        db.close()


def _set_pilot_config(ids="__keep__", base="__keep__",
                      enabled="__keep__", secret="__keep__"):
    values = {}
    if ids != "__keep__":
        values[IDS_KEY] = (
            ",".join(str(i) for i in ids) if ids else None)
    if base != "__keep__":
        values[BASE_KEY] = base
    if enabled != "__keep__":
        values[up.KEY_PILOT_ENTRY_ENABLED] = enabled
    if secret != "__keep__":
        values[up.KEY_PILOT_ENTRY_SECRET] = secret
    _save(values)


def _clear_config():
    _set_pilot_config(ids=None, base=None, enabled=None, secret=None)


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    users = {}
    for uname in (WL_USER, PLAIN_USER):
        u = models.WebUser(username=uname, password_hash=hash_password(PW),
                           is_active=True, is_staff=True, emby_username=uname)
        setattr(u, "emby_password", PW)
        db.add(u)
        db.flush()
        users[uname] = u
    code = models.RegistrationCode(
        code=WL_CODE, max_uses=1, use_count=0, is_active=True,
        code_type=codes.CODE_TYPE_WHITELIST, days=-1,
        reward_type="subscription")
    db.add(code)
    db.commit()
    result = codes.redeem_code(db, users[WL_USER], WL_CODE)
    assert result["success"] is True, result
    db.commit()
    assert codes.is_whitelist_account(db, users[WL_USER]) is True
    assert codes.is_whitelist_account(db, users[PLAIN_USER]) is False

    def _src(name):
        s = em.UpstreamSource(
            name=name, source_type="applecms",
            base_url=f"https://{name}.example.com", auth_method="api_key",
            enabled=True)
        db.add(s)
        db.flush()
        return s

    src_in = _src(SRC_IN)
    src_out = _src(SRC_OUT)
    lib = em.Library(
        guid=_guid(), name=LIB_NAME, collection_type="movies",
        is_enabled=True,
        upstream_source_ids=f"{src_in.id},{src_out.id}")
    db.add(lib)
    db.flush()

    def _item(name, src, up_id):
        it = em.MediaItem(guid=_guid(), library_id=lib.id,
                          item_type="movie", name=name)
        db.add(it)
        db.flush()
        m = em.UpstreamItemMapping(
            local_item_id=it.id, source_id=src.id, upstream_item_id=up_id,
            confidence=0.95, evidence="tmdb")
        db.add(m)
        db.flush()
        return it, m

    item_in, map_in = _item(ITEM_IN, src_in, "vod-in-1")
    item_out, map_out = _item(ITEM_OUT, src_out, "vod-out-1")
    db.commit()
    data = {
        "wl_id": users[WL_USER].id, "plain_id": users[PLAIN_USER].id,
        "src_in": src_in.id, "src_out": src_out.id,
        "item_in": item_in.guid, "item_out": item_out.guid,
        "map_in": map_in.id, "map_out": map_out.id,
    }
    db.close()
    yield data
    db = SessionLocal()
    try:
        _clear_config()
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def pilot_config(seed):
    """专属名单只含内源 + 飞牛基址 + pilot 入口开 + 假密钥。"""
    _set_pilot_config(ids=[seed["src_in"]], base=PILOT_BASE,
                      enabled=True, secret=FAKE_SECRET)
    try:
        yield
    finally:
        _clear_config()


@pytest.fixture()
def resolve_stub(monkeypatch):
    monkeypatch.setattr(
        up, "resolve_mapped_upstream_stream",
        lambda db, local_item_id, **kw: UP_URL)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}PilotTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}pilotent-dev-1{q}, Version={q}1.0{q}")


def _login(client, uname, pilot_value=None):
    headers = {"X-Emby-Authorization": _auth_header()}
    if pilot_value is not None:
        headers[HEADER] = pilot_value
    return client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW}, headers=headers)


def _token(client, uname):
    r = _login(client, uname)
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def wl_token(client, seed):
    return _token(client, WL_USER)


@pytest.fixture(scope="module")
def plain_token(client, seed):
    return _token(client, PLAIN_USER)


def _h(token, pilot_value=None):
    headers = {"X-Emby-Token": token}
    if pilot_value is not None:
        headers[HEADER] = pilot_value
    return headers


def _playback_info(client, token, guid, pilot_value=None):
    return client.post(f"/emby/Items/{guid}/PlaybackInfo",
                       headers=_h(token, pilot_value), json={})


def _upstream_lines(body):
    return [s for s in body.get("MediaSources") or []
            if ":upstream:" in str(s.get("Id", ""))]


def _path_of(url):
    parts = urlsplit(url)
    out = parts.path
    if parts.query:
        out += f"?{parts.query}"
    return out


def _manual_wl_url(user_id, guid, map_id, wl_suffix=True):
    exp, sign = play_sign.issue_play_sign(user_id, guid)
    msid = f"{guid}:upstream:{map_id}" + (":wl" if wl_suffix else "")
    return (f"/emby/Videos/{guid}/upstream/wl-stream"
            f"?MediaSourceId={msid}&uid={user_id}&exp={exp}&sign={sign}")


class _Req:
    def __init__(self, headers):
        self.headers = headers


# ---------------------------------------------------------------- 读取口径


def test_pilot_entry_readers_and_detection(seed, pilot_config):
    db = SessionLocal()
    try:
        assert up.pilot_entry_enabled(db) is True
        assert up.pilot_entry_secret(db) == FAKE_SECRET
        assert up.pilot_entry_origin(db) == PILOT_ORIGIN
        assert up.is_pilot_entry(db, _Req({HEADER: FAKE_SECRET})) is True
        assert up.is_pilot_entry(db, _Req({HEADER: "wrong"})) is False
        assert up.is_pilot_entry(db, _Req({})) is False
        _set_pilot_config(enabled=False)
        assert up.is_pilot_entry(db, _Req({HEADER: FAKE_SECRET})) is False
        _set_pilot_config(enabled=True, secret=None)
        assert up.is_pilot_entry(db, _Req({HEADER: FAKE_SECRET})) is False
    finally:
        db.close()


# ---------------------------------------------------------------- 普通入口


def test_normal_entry_no_wl_lines_anymore(
        client, wl_token, seed, pilot_config):
    r = _playback_info(client, wl_token, seed["item_in"])
    assert r.status_code == 200, r.text
    lines = _upstream_lines(r.json())
    assert len(lines) == 1
    assert not str(lines[0]["Id"]).endswith(":wl")
    assert "wl-stream" not in r.text


# ---------------------------------------------------------------- pilot 选线


def test_pilot_entry_whitelist_all_lines_via_feiniu(
        client, wl_token, seed, pilot_config):
    for guid, map_id in ((seed["item_in"], seed["map_in"]),
                         (seed["item_out"], seed["map_out"])):
        r = _playback_info(client, wl_token, guid,
                           pilot_value=FAKE_SECRET)
        assert r.status_code == 200, r.text
        body = r.json()
        lines = _upstream_lines(body)
        # 名单外源同样出线（pilot 不按专属名单筛选）
        assert len(lines) == 1, body.get("MediaSources")
        line = lines[0]
        # 第一条即飞牛腿（托管行只有上游线路）
        assert body["MediaSources"][0] == line
        assert line["Id"] == f"{guid}:upstream:{map_id}"
        assert "专属线路" not in str(line.get("Name") or "")
        url = line["DirectStreamUrl"]
        assert line["DirectPlayUrl"] == url
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == PILOT_ORIGIN
        assert parts.path == f"/emby/Videos/{guid}/upstream/wl-stream"
        assert "TESTTOKEN" not in r.text


def test_pilot_entry_wl_stream_unlisted_source_allowed(
        client, wl_token, seed, pilot_config, resolve_stub):
    r = _playback_info(client, wl_token, seed["item_out"],
                       pilot_value=FAKE_SECRET)
    url = _upstream_lines(r.json())[0]["DirectStreamUrl"]
    resp = client.get(_path_of(url),
                      headers=_h(wl_token, FAKE_SECRET),
                      follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers.get("location") == (
        f"{PILOT_BASE}/x/v.fengbao14.com/play/xyz/index.m3u8?k=1")


# ---------------------------------------------------------------- pilot 拒绝


def test_pilot_entry_plain_user_login_rejected(
        client, seed, pilot_config):
    r = _login(client, PLAIN_USER, pilot_value=FAKE_SECRET)
    assert r.status_code == 403, r.text
    assert r.json().get("code") == "pilot_entry_whitelist_only"
    assert "白名单" in str(r.json().get("message") or "")
    # 白名单用户经 pilot 入口登录正常
    r = _login(client, WL_USER, pilot_value=FAKE_SECRET)
    assert r.status_code == 200, r.text


def test_pilot_entry_plain_user_playbackinfo_rejected(
        client, plain_token, seed, pilot_config):
    r = _playback_info(client, plain_token, seed["item_in"],
                       pilot_value=FAKE_SECRET)
    assert r.status_code == 403, r.text
    assert r.json().get("code") == "pilot_entry_whitelist_only"


def test_forged_pilot_header_treated_as_normal(
        client, wl_token, plain_token, seed, pilot_config):
    # 错值标记头：登录按普通入口放行（普通用户也能登）
    r = _login(client, PLAIN_USER, pilot_value="wrong-value")
    assert r.status_code == 200, r.text
    # 错值标记头：PlaybackInfo 按普通入口出普通线路（无飞牛腿）
    r = _playback_info(client, wl_token, seed["item_in"],
                       pilot_value="wrong-value")
    assert r.status_code == 200, r.text
    assert "wl-stream" not in r.text
    lines = _upstream_lines(r.json())
    assert len(lines) == 1
    assert PILOT_ORIGIN not in str(lines[0].get("DirectStreamUrl") or "")


# ---------------------------------------------------------------- wl-stream 旧口径


def test_wl_stream_no_marker_listed_still_allowed(
        client, wl_token, seed, pilot_config, resolve_stub):
    url = _manual_wl_url(seed["wl_id"], seed["item_in"], seed["map_in"])
    r = client.get(url, headers=_h(wl_token), follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == (
        f"{PILOT_BASE}/x/v.fengbao14.com/play/xyz/index.m3u8?k=1")


def test_wl_stream_no_marker_unlisted_still_404(
        client, wl_token, seed, pilot_config, resolve_stub):
    url = _manual_wl_url(seed["wl_id"], seed["item_out"], seed["map_out"])
    r = client.get(url, headers=_h(wl_token), follow_redirects=False)
    assert r.status_code == 404, r.text
    assert r.json().get("code") == "exclusive_line_not_listed"


def test_wl_stream_plain_msid_without_marker_404(
        client, wl_token, seed, pilot_config, resolve_stub):
    url = _manual_wl_url(seed["wl_id"], seed["item_in"], seed["map_in"],
                         wl_suffix=False)
    r = client.get(url, headers=_h(wl_token), follow_redirects=False)
    assert r.status_code == 404, r.text
    assert r.json().get("code") == "upstream_mapping_not_found"


# ---------------------------------------------------------------- 管理口


@pytest.fixture()
def admin_app(seed):
    from fastapi import FastAPI
    from backend.api import admin_upstream_sources  # noqa: F401
    from backend.database import get_db
    from backend.emby_server.portal import admin_emby_router, require_staff

    db = SessionLocal()
    staff = db.query(models.WebUser).filter(
        models.WebUser.username == WL_USER).first()
    app = FastAPI()
    app.include_router(admin_emby_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_staff] = lambda: staff
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_admin_pilot_entry_config_roundtrip(admin_app, seed):
    _clear_config()
    try:
        r = admin_app.get("/api/admin/emby/upstream-exclusive-line-config")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pilot_entry_enabled"] is False
        assert body["pilot_entry_configured"] is False
        r = admin_app.put(
            "/api/admin/emby/upstream-exclusive-line-config",
            json={"source_ids": [seed["src_in"]],
                  "pilot_entry_enabled": True,
                  "pilot_entry_secret": FAKE_SECRET})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pilot_entry_enabled"] is True
        assert body["pilot_entry_configured"] is True
        # 密钥原文永不回浏览器
        assert FAKE_SECRET not in r.text
        # 服务端读取口径与后台回写一致
        db = SessionLocal()
        try:
            assert up.pilot_entry_enabled(db) is True
            assert up.pilot_entry_secret(db) == FAKE_SECRET
        finally:
            db.close()
        # 不带 pilot 字段的回写不动 pilot 配置
        r = admin_app.put(
            "/api/admin/emby/upstream-exclusive-line-config",
            json={"source_ids": []})
        assert r.json()["pilot_entry_enabled"] is True
        assert r.json()["pilot_entry_configured"] is True
        # 空串清除密钥
        r = admin_app.put(
            "/api/admin/emby/upstream-exclusive-line-config",
            json={"source_ids": [], "pilot_entry_secret": ""})
        assert r.json()["pilot_entry_configured"] is False
    finally:
        _clear_config()

"""专属线路通用能力（2026-11-05 用户亲点批准形状）回归。

把原来写死的单条 pilot 配置（四个散键）改成「线路＝一条配置
记录、可多条」的通用能力，锁住的口径：

- 线路记录存 SystemConfig 键 ``exclusive_lines``（JSON 数组，
  upstream_aggregate 家族）：名称/入口地址（对用户展示）/取流
  基址（内部、含口令段、永不回显）/入口密钥（每线独立、永不
  回显）/准入人群（本期只有 whitelist）/适用范围（all 或指定
  源 id 列表）/启用开关/存量旧地址名单 legacy_source_ids。
- 入口认定改按线路匹配：标记头值与任一「启用中」线路的入口
  密钥逐字一致 → 认定为该线路入口；错密钥/禁用线路不认。
- 准入与执法按命中线路的人群判定：登录（auth.issue_token）与
  PlaybackInfo 两道拒绝口径与改造前逐字一致（403 +
  pilot_entry_whitelist_only）。
- PlaybackInfo 改写用命中线路的取流基址与适用范围：范围=all
  时全部 applecms 线路走该线；范围=指定源时范围外映射不下发
  （不回落普通直连）。
- 迁移：新键不存在时，旧四键（pilot_entry_enabled /
  pilot_entry_secret / feiniu_pilot_base_url /
  upstream_aggregate_exclusive_source_ids）一次性迁为第一条
  线路（id=pilot）；新键一旦落库，旧键不再是运行时判定来源。
  旧读取口（pilot_entry_enabled 等）改为默认线路的派生视图，
  旧管理口保留为默认线路的兼容门面，行为与改造前一致。
- 用户口 GET /api/user/exclusive-lines 三态：有资格 → 名称+
  入口地址列表（不含基址/密钥）；无资格 → 空列表；未登录 401。

测试形态照 test_pilot_entry.py：真 TestClient 打整 app、种子
直落 app 库；上游解析打桩。密钥/口令用假值，只许出现在本文件。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

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

PW = "XlineCap#2026"
WL_USER = "xline_wl"
PLAIN_USER = "xline_plain"
LIB_NAME = "线路能力测试库"
SRC_IN = "能力名单内源"
SRC_OUT = "能力名单外源"
ITEM_IN = "能力名单内片"
ITEM_OUT = "能力名单外片"
WL_CODE = "VIP-XLINE00001"
BASE_A = "https://a.example.test/p/TOKENAAA"
BASE_B = "https://b.example.test/p/TOKENBBB"
SECRET_A = "xline-secret-aaaa"
SECRET_B = "xline-secret-bbbb"
HEADER = "X-Aetrix-Pilot-Entry"
UP_URL = "https://v.fengbao14.com/play/xyz/index.m3u8?k=1"


def _guid() -> str:
    return uuid.uuid4().hex


def _save(values):
    from backend import config_service
    db = SessionLocal()
    try:
        config_service.save_config(db, values, schema="upstream_aggregate")
        db.commit()
    finally:
        db.close()


def _clear_all_config():
    _save({
        up.KEY_EXCLUSIVE_SOURCE_IDS: None,
        up.KEY_FEINIU_PILOT_BASE_URL: None,
        up.KEY_PILOT_ENTRY_ENABLED: None,
        up.KEY_PILOT_ENTRY_SECRET: None,
        up.KEY_EXCLUSIVE_LINES: None,
    })


def _set_old_keys(ids="__keep__", base="__keep__",
                  enabled="__keep__", secret="__keep__"):
    values = {}
    if ids != "__keep__":
        values[up.KEY_EXCLUSIVE_SOURCE_IDS] = (
            ",".join(str(i) for i in ids) if ids else None)
    if base != "__keep__":
        values[up.KEY_FEINIU_PILOT_BASE_URL] = base
    if enabled != "__keep__":
        values[up.KEY_PILOT_ENTRY_ENABLED] = enabled
    if secret != "__keep__":
        values[up.KEY_PILOT_ENTRY_SECRET] = secret
    _save(values)


def _line(line_id, name, base, secret, *, enabled=True,
          scope_mode="all", scope_ids=(), legacy_ids=(),
          entry_url=None):
    if entry_url is None:
        parts = urlsplit(base)
        entry_url = f"{parts.scheme}://{parts.netloc}"
    return {
        "id": line_id, "name": name, "entry_url": entry_url,
        "stream_base_url": base, "entry_secret": secret,
        "audience": "whitelist", "scope_mode": scope_mode,
        "scope_source_ids": list(scope_ids),
        "legacy_source_ids": list(legacy_ids), "enabled": enabled,
    }


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
            db.query(models.StationMessage).filter(
                (models.StationMessage.to_user_id == u.id)
                | (models.StationMessage.from_user_id == u.id)
            ).delete(synchronize_session=False)
            db.query(models.CodeRedemption).filter(
                models.CodeRedemption.user_id == u.id
            ).delete(synchronize_session=False)
            db.delete(u)
    db.query(models.RegistrationCode).filter(
        models.RegistrationCode.code == WL_CODE
    ).delete(synchronize_session=False)
    db.commit()


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
        _clear_all_config()
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def lines_cfg(seed):
    """两条启用线路 A/B（范围 all），外加旧键全清（新键为准）。"""
    _clear_all_config()
    db = SessionLocal()
    try:
        up.save_exclusive_lines(db, [
            _line("line-a", "线路甲", BASE_A, SECRET_A,
                  legacy_ids=[seed["src_in"]]),
            _line("line-b", "线路乙", BASE_B, SECRET_B),
        ])
        db.commit()
    finally:
        db.close()
    try:
        yield
    finally:
        _clear_all_config()


@pytest.fixture()
def resolve_stub(monkeypatch):
    monkeypatch.setattr(
        up, "resolve_mapped_upstream_stream",
        lambda db, local_item_id, **kw: UP_URL)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}XlineTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}xline-dev-1{q}, Version={q}1.0{q}")


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


class _Req:
    def __init__(self, headers):
        self.headers = headers


# ---------------------------------------------------------------- 迁移等价


def test_migration_from_old_keys(seed):
    _clear_all_config()
    _set_old_keys(ids=[seed["src_in"]], base=BASE_A,
                  enabled=True, secret=SECRET_A)
    db = SessionLocal()
    try:
        lines = up.exclusive_lines(db)
        assert len(lines) == 1
        line = lines[0]
        assert line["id"] == up.MIGRATED_LINE_ID
        assert line["enabled"] is True
        assert line["entry_secret"] == SECRET_A
        assert line["stream_base_url"] == BASE_A
        assert line["entry_url"] == "https://a.example.test"
        assert line["scope_mode"] == "all"
        assert line["legacy_source_ids"] == [seed["src_in"]]
        assert line["audience"] == "whitelist"
        # 旧读取口＝默认线路派生视图，与迁前逐项一致
        assert up.pilot_entry_enabled(db) is True
        assert up.pilot_entry_secret(db) == SECRET_A
        assert up.pilot_entry_origin(db) == "https://a.example.test"
        assert up.feiniu_pilot_base_url(db) == BASE_A
        assert up.exclusive_source_ids(db) == frozenset({seed["src_in"]})
        assert up.is_pilot_entry(db, _Req({HEADER: SECRET_A})) is True
    finally:
        db.close()
        _clear_all_config()


def test_no_config_no_lines(seed):
    _clear_all_config()
    db = SessionLocal()
    try:
        assert up.exclusive_lines(db) == []
        assert up.is_pilot_entry(db, _Req({HEADER: SECRET_A})) is False
    finally:
        db.close()


def test_saved_lines_shadow_old_keys(seed):
    """新键一旦落库，旧键改动不再影响运行时判定。"""
    _clear_all_config()
    db = SessionLocal()
    try:
        up.save_exclusive_lines(db, [
            _line("line-a", "线路甲", BASE_A, SECRET_A)])
        db.commit()
        _set_old_keys(ids=[seed["src_in"]], base=BASE_B,
                      enabled=True, secret=SECRET_B)
        lines = up.exclusive_lines(db)
        assert [l["id"] for l in lines] == ["line-a"]
        assert up.is_pilot_entry(db, _Req({HEADER: SECRET_A})) is True
        assert up.is_pilot_entry(db, _Req({HEADER: SECRET_B})) is False
    finally:
        db.close()
        _clear_all_config()


# ---------------------------------------------------------------- 入口匹配


def test_match_any_enabled_line(seed, lines_cfg):
    db = SessionLocal()
    try:
        hit = up.match_entry_line(db, _Req({HEADER: SECRET_B}))
        assert hit is not None and hit["id"] == "line-b"
        hit = up.match_entry_line(db, _Req({HEADER: SECRET_A}))
        assert hit is not None and hit["id"] == "line-a"
        assert up.match_entry_line(db, _Req({HEADER: "nope"})) is None
        assert up.match_entry_line(db, _Req({})) is None
        # 禁用线路不认
        lines = up.exclusive_lines(db)
        for l in lines:
            if l["id"] == "line-b":
                l["enabled"] = False
        up.save_exclusive_lines(db, lines)
        db.commit()
        assert up.match_entry_line(db, _Req({HEADER: SECRET_B})) is None
        assert up.match_entry_line(db, _Req({HEADER: SECRET_A})) is not None
    finally:
        db.close()


# ---------------------------------------------------------------- 执法


def test_login_enforcement_per_line(client, seed, lines_cfg):
    r = _login(client, PLAIN_USER, pilot_value=SECRET_B)
    assert r.status_code == 403, r.text
    assert r.json().get("code") == "pilot_entry_whitelist_only"
    r = _login(client, WL_USER, pilot_value=SECRET_B)
    assert r.status_code == 200, r.text


def test_playbackinfo_rejection_per_line(
        client, plain_token, seed, lines_cfg):
    r = _playback_info(client, plain_token, seed["item_in"],
                       pilot_value=SECRET_A)
    assert r.status_code == 403, r.text
    assert r.json().get("code") == "pilot_entry_whitelist_only"


# ---------------------------------------------------------------- 选线


def test_playbackinfo_uses_matched_line_base(
        client, wl_token, seed, lines_cfg):
    r = _playback_info(client, wl_token, seed["item_in"],
                       pilot_value=SECRET_B)
    assert r.status_code == 200, r.text
    lines = _upstream_lines(r.json())
    assert len(lines) == 1
    parts = urlsplit(lines[0]["DirectStreamUrl"])
    assert f"{parts.scheme}://{parts.netloc}" == "https://b.example.test"
    assert "TOKENBBB" not in r.text and "TOKENAAA" not in r.text
    r = _playback_info(client, wl_token, seed["item_in"],
                       pilot_value=SECRET_A)
    lines = _upstream_lines(r.json())
    parts = urlsplit(lines[0]["DirectStreamUrl"])
    assert f"{parts.scheme}://{parts.netloc}" == "https://a.example.test"


def test_scoped_line_omits_out_of_scope_mapping(
        client, wl_token, seed):
    _clear_all_config()
    db = SessionLocal()
    try:
        up.save_exclusive_lines(db, [
            _line("line-s", "范围线", BASE_A, SECRET_A,
                  scope_mode="sources", scope_ids=[seed["src_in"]])])
        db.commit()
    finally:
        db.close()
    try:
        r = _playback_info(client, wl_token, seed["item_in"],
                           pilot_value=SECRET_A)
        assert r.status_code == 200, r.text
        assert len(_upstream_lines(r.json())) == 1
        # 范围外映射不下发（不回落普通直连）
        r = _playback_info(client, wl_token, seed["item_out"],
                           pilot_value=SECRET_A)
        assert r.status_code == 200, r.text
        assert _upstream_lines(r.json()) == []
        assert "upstream/wl-stream" not in r.text
    finally:
        _clear_all_config()


def test_wl_stream_scoped_line_out_of_scope_404(
        client, wl_token, seed, resolve_stub):
    _clear_all_config()
    db = SessionLocal()
    try:
        up.save_exclusive_lines(db, [
            _line("line-s", "范围线", BASE_A, SECRET_A,
                  scope_mode="sources", scope_ids=[seed["src_in"]],
                  legacy_ids=[seed["src_in"]])])
        db.commit()
    finally:
        db.close()
    try:
        r = _playback_info(client, wl_token, seed["item_out"],
                           pilot_value=SECRET_A)
        # 范围外在 PlaybackInfo 已不下发；手工拼 pilot 形态地址直调
        # 取流口同样被范围挡下（404 exclusive_line_not_listed）。
        exp, sign = play_sign.issue_play_sign(seed["wl_id"],
                                              seed["item_out"])
        url = (f"/emby/Videos/{seed['item_out']}/upstream/wl-stream"
               f"?MediaSourceId={seed['item_out']}:upstream:"
               f"{seed['map_out']}&uid={seed['wl_id']}"
               f"&exp={exp}&sign={sign}")
        resp = client.get(url, headers=_h(wl_token, SECRET_A),
                          follow_redirects=False)
        assert resp.status_code == 404, resp.text
        assert resp.json().get("code") == "exclusive_line_not_listed"
        # 范围内正常 302 到该线基址
        r = _playback_info(client, wl_token, seed["item_in"],
                           pilot_value=SECRET_A)
        line_url = _upstream_lines(r.json())[0]["DirectStreamUrl"]
        resp = client.get(_path_of(line_url),
                          headers=_h(wl_token, SECRET_A),
                          follow_redirects=False)
        assert resp.status_code == 302, resp.text
        assert resp.headers.get("location") == (
            f"{BASE_A}/x/v.fengbao14.com/play/xyz/index.m3u8?k=1")
    finally:
        _clear_all_config()


# ---------------------------------------------------------------- 用户口


def _user_app(seed, user_key):
    from fastapi import FastAPI
    from backend.api.user import user_router, get_current_user
    from backend.database import get_db

    db = SessionLocal()
    user = None
    if user_key is not None:
        user = db.query(models.WebUser).filter(
            models.WebUser.username == user_key).first()
    app = FastAPI()
    app.include_router(user_router)
    app.dependency_overrides[get_db] = lambda: db
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False), db


def test_user_lines_endpoint_three_states(seed, lines_cfg):
    app, db = _user_app(seed, WL_USER)
    try:
        r = app.get("/api/user/exclusive-lines")
        assert r.status_code == 200, r.text
        body = r.json()
        names = {l["name"] for l in body["lines"]}
        assert names == {"线路甲", "线路乙"}
        urls = {l["entry_url"] for l in body["lines"]}
        assert urls == {"https://a.example.test", "https://b.example.test"}
        # 基址口令与入口密钥永不下发用户端
        assert "TOKENAAA" not in r.text and "TOKENBBB" not in r.text
        assert SECRET_A not in r.text and SECRET_B not in r.text
    finally:
        db.close()
    app, db = _user_app(seed, PLAIN_USER)
    try:
        r = app.get("/api/user/exclusive-lines")
        assert r.status_code == 200, r.text
        assert r.json()["lines"] == []
    finally:
        db.close()
    app, db = _user_app(seed, None)
    try:
        r = app.get("/api/user/exclusive-lines")
        assert r.status_code == 401, r.text
    finally:
        db.close()


# ---------------------------------------------------------------- 管理口


def _admin_app(seed):
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
    return TestClient(app, raise_server_exceptions=False), db


def test_admin_lines_crud(seed):
    _clear_all_config()
    app, db = _admin_app(seed)
    try:
        r = app.get("/api/admin/emby/exclusive-lines")
        assert r.status_code == 200, r.text
        assert r.json()["lines"] == []
        r = app.post("/api/admin/emby/exclusive-lines", json={
            "name": "新线路", "entry_url": "https://n.example.test",
            "stream_base_url": BASE_A, "entry_secret": SECRET_A,
            "audience": "whitelist", "scope_mode": "sources",
            "scope_source_ids": [seed["src_in"]], "enabled": True})
        assert r.status_code == 200, r.text
        created = r.json()["line"]
        assert created["stream_configured"] is True
        assert created["secret_configured"] is True
        # 原文不回浏览器
        assert SECRET_A not in r.text and "TOKENAAA" not in r.text
        line_id = created["id"]
        # 入口认定即刻按新线路生效
        db2 = SessionLocal()
        try:
            hit = up.match_entry_line(db2, _Req({HEADER: SECRET_A}))
            assert hit is not None and hit["id"] == line_id
        finally:
            db2.close()
        # 编辑：改名 + 关启用（不动基址/密钥）
        r = app.put(f"/api/admin/emby/exclusive-lines/{line_id}",
                    json={"name": "改名线", "enabled": False})
        assert r.status_code == 200, r.text
        assert r.json()["line"]["name"] == "改名线"
        assert r.json()["line"]["stream_configured"] is True
        db2 = SessionLocal()
        try:
            assert up.match_entry_line(
                db2, _Req({HEADER: SECRET_A})) is None
        finally:
            db2.close()
        # 重置密钥（管理员给新值，只回状态）
        r = app.post(
            f"/api/admin/emby/exclusive-lines/{line_id}/reset-secret",
            json={"secret": SECRET_B})
        assert r.status_code == 200, r.text
        assert r.json()["secret_configured"] is True
        assert SECRET_B not in r.text
        # 删除
        r = app.delete(f"/api/admin/emby/exclusive-lines/{line_id}")
        assert r.status_code == 200, r.text
        r = app.get("/api/admin/emby/exclusive-lines")
        assert r.json()["lines"] == []
    finally:
        db.close()
        _clear_all_config()


def test_admin_old_endpoint_facade_on_default_line(seed):
    """旧专属线路配置口保留为默认（迁移）线路的兼容门面。"""
    _clear_all_config()
    app, db = _admin_app(seed)
    try:
        r = app.get("/api/admin/emby/upstream-exclusive-line-config")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source_ids"] == []
        assert body["pilot_entry_enabled"] is False
        r = app.put("/api/admin/emby/upstream-exclusive-line-config",
                    json={"source_ids": [seed["src_in"]],
                          "pilot_base_url": BASE_A,
                          "pilot_entry_enabled": True,
                          "pilot_entry_secret": SECRET_A})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source_ids"] == [seed["src_in"]]
        assert body["pilot_configured"] is True
        assert body["pilot_entry_enabled"] is True
        assert body["pilot_entry_configured"] is True
        assert SECRET_A not in r.text and "TOKENAAA" not in r.text
        # 门面写入落到线路记录（新键），旧键仍为空
        db2 = SessionLocal()
        try:
            lines = up.exclusive_lines(db2)
            assert len(lines) == 1
            assert lines[0]["id"] == up.MIGRATED_LINE_ID
            assert lines[0]["legacy_source_ids"] == [seed["src_in"]]
        finally:
            db2.close()
    finally:
        db.close()
        _clear_all_config()

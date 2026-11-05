"""白名单专属线路（飞牛家宽回源，2026-11-04 用户亲令）回归。

2026-11-05 形态改定后本文件锁的是「旧形态残留口径」：普通入口
（详情/PlaybackInfo）不再下发 :wl 专属线（pilot 独立入口的新口径
在 test_pilot_entry.py）；专属名单与 wl-stream 取流口的旧三查
执法保留给存量 :wl 地址，逐字不回归。

锁住的口径：
- 专属名单是 SystemConfig（upstream_aggregate_exclusive_source_ids，
  管理后台可增删），粒度＝源；无默认——未配/空＝整体惰性，普通
  选线零影响。飞牛基址（feiniu_pilot_base_url，含路径 token）未配
  同样惰性；
- 普通入口对所有用户（含白名单）都不再出专属线：响应全文不出现
  wl-stream、MediaSource 无 :wl 后缀；
- 普通用户拿自己合法播放签名轨伪造专属地址打专属口 → 403
  exclusive_line_whitelist_only（签名只绑 (uid, guid)，线路资格
  在取流口现算白名单）；
- 专属口执法：基址/名单未配 → 404 exclusive_line_unavailable；
  映射不在身份组 → 404 upstream_mapping_not_found；源被移出名单
  → 404 exclusive_line_not_listed（后台删一条立刻不可达）；
  全过 → 定向解析上游地址、改写成飞牛通用代理地址 302 下发
  （{基址}/x/{host}{path}，查询串原样保留）；
- 名单内的 Emby 型源不出专属线（飞牛线只服务 CMS 播放主机）；
- :wl 后缀打普通取流口 → 严格解析对不上、404，不偷偷换线；
- 管理端配置口（/api/admin/emby/upstream-exclusive-line-config）：
  名单全量回写、基址只回已配状态（token 原文不回浏览器）、
  基址缺省不动/空串清除；未登录管理端不可达。

测试形态照 test_upstream_detail_line.py：真 TestClient 打整 app、
种子直落 app 库；取流口的上游解析打桩（不发真网络请求），解析
本体由 upstream_playback 既有测试覆盖。
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
from backend import codes
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import play_sign, upstream_playback as up
from backend.security import hash_password

init_db()

PW = "WlLine#2026"
WL_USER = "wlline_wl"
PLAIN_USER = "wlline_plain"
LIB_NAME = "专属线路测试库"
SRC_IN = "专属内源"
SRC_OUT = "专属外源"
SRC_EMBY = "专属Emby源"
ITEM_IN = "专属内片"
ITEM_OUT = "专属外片"
ITEM_EMBY = "专属Emby片"
WL_CODE = "VIP-WLLINE000001"
PILOT_BASE = "https://pilot.example.test/p/TESTTOKEN"
UP_URL = "https://v.fengbao14.com/play/xyz/index.m3u8?k=1"
IDS_KEY = up.KEY_EXCLUSIVE_SOURCE_IDS
BASE_KEY = up.KEY_FEINIU_PILOT_BASE_URL


def _guid() -> str:
    return uuid.uuid4().hex


def _cleanup(db: Session) -> None:
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name.in_([ITEM_IN, ITEM_OUT, ITEM_EMBY])).all()
    ids = [r.id for r in rows]
    if ids:
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    for s in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name.in_([SRC_IN, SRC_OUT, SRC_EMBY])).all():
        db.delete(s)
    db.query(em.Library).filter(em.Library.name == LIB_NAME).delete()
    for uname in (WL_USER, PLAIN_USER):
        u = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if u is not None:
            # 站内信先清（与 test_pilot_entry._cleanup 同口径）：
            # to_user_id 非空而 ORM 删用户会试图置空它，孤儿站内信
            # 顶爆 teardown 时整个清理回滚、种子行泄漏污染后续文件
            # （2026-11-05 #1854 CI 实证）。
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


def _set_config(ids=None, base="__keep__"):
    """经生产配置路径（config_service + upstream_aggregate 家族）写配置。

    ids/base 传 None＝删行（未配）；base 传 "__keep__"＝不动基址。
    """
    from backend import config_service
    # int_list 保存口径是逗号串；空 list 与 None 同义（删行＝未配）
    values = {IDS_KEY: (",".join(str(i) for i in ids) if ids else None)}
    if base != "__keep__":
        values[BASE_KEY] = base
    db = SessionLocal()
    try:
        config_service.save_config(db, values, schema="upstream_aggregate")
        db.commit()
    finally:
        db.close()


def _clear_config():
    _set_config(ids=None, base=None)


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    users = {}
    for uname in (WL_USER, PLAIN_USER):
        u = models.WebUser(username=uname, password_hash=hash_password(PW),
                           is_active=True, is_staff=True, emby_username=uname)
        # emby 密码走 setattr 落值：直写 emby_password=<值> 的字面会被
        # 落盘链路的凭据脱敏吞字节（2026-10-30 既有教训），改此形态。
        setattr(u, "emby_password", PW)
        db.add(u)
        db.flush()
        users[uname] = u
    # 白名单用户：生成白名单码并核销（#1668 口径，三处永久在位）。
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

    def _src(name, source_type):
        s = em.UpstreamSource(
            name=name, source_type=source_type,
            base_url=f"https://{name}.example.com", auth_method="api_key",
            enabled=True)
        db.add(s)
        db.flush()
        return s

    src_in = _src(SRC_IN, "applecms")
    src_out = _src(SRC_OUT, "applecms")
    src_emby = _src(SRC_EMBY, "emby")
    lib = em.Library(
        guid=_guid(), name=LIB_NAME, collection_type="movies",
        is_enabled=True,
        upstream_source_ids=f"{src_in.id},{src_out.id},{src_emby.id}")
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
    item_emby, map_emby = _item(ITEM_EMBY, src_emby, "vod-emby-1")
    db.commit()
    data = {
        "wl_id": users[WL_USER].id, "plain_id": users[PLAIN_USER].id,
        "src_in": src_in.id, "src_out": src_out.id, "src_emby": src_emby.id,
        "item_in": item_in.guid, "item_out": item_out.guid,
        "item_emby": item_emby.guid,
        "map_in": map_in.id, "map_out": map_out.id, "map_emby": map_emby.id,
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


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}WlLineTest{q}, Device={q}iOS{q}, "
            f"DeviceId={q}wlline-dev-1{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def wl_token(client, seed):
    return _token(client, WL_USER)


@pytest.fixture(scope="module")
def plain_token(client, seed):
    return _token(client, PLAIN_USER)


@pytest.fixture()
def wl_config(seed):
    """名单含内源 + 基址已配；用后清回未配。"""
    _set_config(ids=[seed["src_in"]], base=PILOT_BASE)
    try:
        yield
    finally:
        _clear_config()


@pytest.fixture()
def resolve_stub(monkeypatch):
    monkeypatch.setattr(
        up, "resolve_mapped_upstream_stream",
        lambda db, local_item_id, **kw: UP_URL)


def _h(token):
    return {"X-Emby-Token": token}


def _qs(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _playback_info(client, token, guid):
    r = client.post(f"/emby/Items/{guid}/PlaybackInfo",
                    headers=_h(token), json={})
    assert r.status_code == 200, r.text
    return r


def _wl_lines(body):
    return [s for s in body.get("MediaSources") or []
            if str(s.get("Id", "")).endswith(":wl")]


def _regular_lines(body):
    return [s for s in body.get("MediaSources") or []
            if ":upstream:" in str(s.get("Id", ""))
            and not str(s.get("Id", "")).endswith(":wl")]


def _path_of(url):
    return re.sub(r"^https?://[^/]+", "", url)


# ---------------------------------------------------------------- 列表面


def test_playbackinfo_whitelist_gets_exclusive_line(
        client, wl_token, seed, wl_config):
    # 2026-11-05 形态改定：普通入口不再追加 :wl 专属线（已撤除，
    # 专属走线改走 pilot 独立入口，见 test_pilot_entry.py）。本例
    # 改锁「普通入口白名单用户也只有普通线路」。
    r = _playback_info(client, wl_token, seed["item_in"])
    body = r.json()
    regular = _regular_lines(body)
    assert len(regular) == 1, body.get("MediaSources")
    assert _wl_lines(body) == []
    assert "wl-stream" not in r.text
    # 上游坐标不裸下发：响应全文不得出现上游域名/上游条目 id/试点 token
    assert "TESTTOKEN" not in r.text
    assert "vod-in-1" not in r.text


def test_detail_whitelist_gets_exclusive_line(
        client, wl_token, seed, wl_config):
    # 同上改定：详情面普通入口同样不再追加专属线。
    r = client.get(f"/emby/Items/{seed['item_in']}", headers=_h(wl_token))
    assert r.status_code == 200, r.text
    assert _wl_lines(r.json()) == []


def test_playbackinfo_plain_user_no_exclusive_line(
        client, plain_token, seed, wl_config):
    r = _playback_info(client, plain_token, seed["item_in"])
    body = r.json()
    assert len(_regular_lines(body)) == 1
    assert _wl_lines(body) == []
    assert "wl-stream" not in r.text


def test_playbackinfo_no_config_no_exclusive_line(
        client, wl_token, seed):
    _clear_config()
    body = _playback_info(client, wl_token, seed["item_in"]).json()
    assert len(_regular_lines(body)) == 1
    assert _wl_lines(body) == []


def test_playbackinfo_source_not_listed_no_exclusive_line(
        client, wl_token, seed, wl_config):
    body = _playback_info(client, wl_token, seed["item_out"]).json()
    assert len(_regular_lines(body)) == 1
    assert _wl_lines(body) == []


def test_playbackinfo_emby_source_listed_still_no_exclusive_line(
        client, wl_token, seed, monkeypatch):
    # Emby 型托管行的媒体参数现查会真发请求——打桩成查不到（回落空壳）
    monkeypatch.setattr(up, "fetch_upstream_media_params",
                        lambda *a, **kw: None)
    _set_config(ids=[seed["src_in"], seed["src_emby"]], base=PILOT_BASE)
    try:
        body = _playback_info(client, wl_token, seed["item_emby"]).json()
        assert _wl_lines(body) == []
    finally:
        _clear_config()


# ---------------------------------------------------------------- 取流口


def _wl_url(client, token, guid):
    """手工拼旧专属线地址（:wl 形态）：普通入口已不下发此形态，
    存量地址靠 (uid, guid) 播放签名自签——签名不绑线路形态，
    专属口现算资格执法（见模块 docstring）。"""
    db = SessionLocal()
    try:
        item = db.query(em.MediaItem).filter(
            em.MediaItem.guid == guid).first()
        mapping = db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id == item.id).first()
        user = db.query(models.WebUser).filter(
            models.WebUser.username == WL_USER).first()
        exp, sign = play_sign.issue_play_sign(user.id, guid)
        return (f"/emby/Videos/{guid}/upstream/wl-stream"
                f"?MediaSourceId={guid}:upstream:{mapping.id}:wl"
                f"&uid={user.id}&exp={exp}&sign={sign}")
    finally:
        db.close()


def test_wl_stream_redirects_to_pilot(
        client, wl_token, seed, wl_config, resolve_stub):
    url = _wl_url(client, wl_token, seed["item_in"])
    r = client.get(_path_of(url), headers=_h(wl_token),
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == (
        f"{PILOT_BASE}/x/v.fengbao14.com/play/xyz/index.m3u8?k=1")


def test_wl_stream_plain_user_forged_403(
        client, plain_token, seed, wl_config, resolve_stub):
    # 普通用户拿自己合法签名的普通线路地址，改路径+msid 伪造专属调用
    body = _playback_info(client, plain_token, seed["item_in"]).json()
    regular_url = _regular_lines(body)[0]["DirectStreamUrl"]
    forged = regular_url.replace("/upstream/stream", "/upstream/wl-stream")
    forged = forged.replace(
        f"MediaSourceId={seed['item_in']}:upstream:{seed['map_in']}",
        f"MediaSourceId={seed['item_in']}:upstream:{seed['map_in']}:wl")
    assert forged != regular_url
    r = client.get(_path_of(forged), headers=_h(plain_token),
                   follow_redirects=False)
    assert r.status_code == 403, r.text
    assert r.json().get("code") == "exclusive_line_whitelist_only"
    assert "location" not in r.headers


def test_wl_stream_source_removed_from_list_404(
        client, wl_token, seed, wl_config, resolve_stub):
    url = _wl_url(client, wl_token, seed["item_in"])
    # 后台把该源移出名单（名单仍非空、基址仍在）→ 立刻不可达
    _set_config(ids=[seed["src_out"]])
    try:
        r = client.get(_path_of(url), headers=_h(wl_token),
                       follow_redirects=False)
        assert r.status_code == 404, r.text
        assert r.json().get("code") == "exclusive_line_not_listed"
    finally:
        _set_config(ids=[seed["src_in"]])


def test_wl_stream_pilot_base_cleared_404(
        client, wl_token, seed, wl_config, resolve_stub):
    url = _wl_url(client, wl_token, seed["item_in"])
    _set_config(base=None)
    try:
        r = client.get(_path_of(url), headers=_h(wl_token),
                       follow_redirects=False)
        assert r.status_code == 404, r.text
        assert r.json().get("code") == "exclusive_line_unavailable"
    finally:
        _set_config(base=PILOT_BASE)


def test_wl_msid_on_regular_stream_404(
        client, wl_token, seed, wl_config, resolve_stub):
    url = _wl_url(client, wl_token, seed["item_in"])
    path = _path_of(url).replace("/upstream/wl-stream", "/upstream/stream")
    r = client.get(path, headers=_h(wl_token), follow_redirects=False)
    assert r.status_code == 404, r.text
    assert r.json().get("code") == "upstream_mapping_not_found"


def test_wl_stream_bad_msid_404(client, wl_token, seed, wl_config):
    r = client.get(
        f"/emby/Videos/{seed['item_in']}/upstream/wl-stream"
        f"?MediaSourceId={seed['item_in']}:upstream:{seed['map_in']}",
        headers=_h(wl_token), follow_redirects=False)
    assert r.status_code == 404, r.text
    assert r.json().get("code") == "upstream_mapping_not_found"


# ---------------------------------------------------------------- 读取口径


def test_exclusive_readers_defaults_and_parse():
    db = SessionLocal()
    try:
        _clear_config()
        assert up.exclusive_source_ids(db) == frozenset()
        assert up.feiniu_pilot_base_url(db) == ""
        assert up.exclusive_line_ready(db) is False
        _set_config(ids=[9, 13], base="https://pilot.example.test/p/T/")
        assert up.exclusive_source_ids(db) == frozenset({9, 13})
        # 基址尾部斜杠剥净
        assert up.feiniu_pilot_base_url(db) == "https://pilot.example.test/p/T"
        assert up.exclusive_line_ready(db) is True
        _set_config(ids=[], base=PILOT_BASE)
        assert up.exclusive_source_ids(db) == frozenset()
        assert up.exclusive_line_ready(db) is False
    finally:
        _clear_config()
        db.close()
    assert up.exclusive_source_ids(None) == frozenset()
    assert up.feiniu_pilot_base_url(None) == ""


def test_feiniu_pilot_rewrite():
    base = "https://pilot.example.test/p/T"
    assert up.feiniu_pilot_rewrite(base, UP_URL) == (
        f"{base}/x/v.fengbao14.com/play/xyz/index.m3u8?k=1")
    assert up.feiniu_pilot_rewrite(
        base, "http://h.example.com/a/b.m3u8") == f"{base}/x/h.example.com/a/b.m3u8"
    assert up.feiniu_pilot_rewrite(base, "ftp://h.example.com/a") is None
    assert up.feiniu_pilot_rewrite(base, "") is None
    assert up.feiniu_pilot_rewrite("", UP_URL) is None


# ---------------------------------------------------------------- 管理端配置口


@pytest.fixture()
def admin_app(seed):
    from fastapi import FastAPI
    from backend.api import admin_upstream_sources  # noqa: F401 触发路由注册
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


def test_admin_exclusive_line_config_roundtrip(admin_app, seed):
    _clear_config()
    try:
        r = admin_app.get("/api/admin/emby/upstream-exclusive-line-config")
        assert r.status_code == 200, r.text
        assert r.json() == {"source_ids": [], "pilot_configured": False,
                            "pilot_entry_enabled": False,
                            "pilot_entry_configured": False}
        r = admin_app.put(
            "/api/admin/emby/upstream-exclusive-line-config",
            json={"source_ids": [seed["src_in"]],
                  "pilot_base_url": PILOT_BASE})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source_ids"] == [seed["src_in"]]
        assert body["pilot_configured"] is True
        # 基址含 token：任何响应都不回原文
        assert "TESTTOKEN" not in r.text
        # 不带 pilot_base_url 的回写不动基址
        r = admin_app.put(
            "/api/admin/emby/upstream-exclusive-line-config",
            json={"source_ids": []})
        assert r.json()["pilot_configured"] is True
        # 空串清除基址
        r = admin_app.put(
            "/api/admin/emby/upstream-exclusive-line-config",
            json={"source_ids": [seed["src_in"]], "pilot_base_url": ""})
        assert r.json()["pilot_configured"] is False
        # 服务端读取口径与后台回写一致（选线执法同一份配置）
        db = SessionLocal()
        try:
            assert up.exclusive_source_ids(db) == frozenset({seed["src_in"]})
        finally:
            db.close()
    finally:
        _clear_config()


def test_admin_exclusive_line_config_requires_staff(seed):
    from backend.main import app
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/admin/emby/upstream-exclusive-line-config")
    assert r.status_code in (401, 403), r.status_code

# -*- coding: utf-8 -*-
"""115 大文件取链回落（2026-10-31 生产实证）。

根因：webapi ``/files/download`` 对大文件回 state=False、msg_code=50028
「文件大小超出限制，请使用115电脑端下载」，转存副本播不了、巡检还把
副本记 strike 标 stale。本文件锁三件事：

1. 50028 必须被分类成通道限制错误（Pan115ChannelLimitError），不是
   普通业务错误；
2. download_url 在 50028 时回落 app chrome 加密通道取到地址；
   播放器场景（带 ua）走 app 通道优先（直链绑 UA，网页端用播放器
   UA 签的地址拉流会被 CDN 拒，生产探针实证）；
3. 巡检对通道限制不记 strike；播放侧 resolve 对通道限制不落绑定
   失败态、kind=size_limit。

全部离线：115 API 一律用 _http_json 桩 / 客户端桩。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.integrations import store
from backend.emby_server import stream_limits as sl


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    sl.clear_table_policy_cache()
    store.invalidate()
    yield session
    session.close()
    sl.clear_table_policy_cache()
    store.invalidate()


AUTH = "UID=123456_A1_1700000000; CID=x; SEID=y"
APP_URL = "https://cdnfhnfile.115cdn.net/fake/app-file.mkv"
WEB_URL = "https://cdnfhnfile.115.com/fake/web-file.mkv"
SIZE_LIMIT_BODY = {"state": False, "msg_code": 50028,
                   "msg": "文件大小超出限制，请使用115电脑端下载"}


# app 通道的响应 data 是服务端用私钥签出的加密串，离线无法自造
# （公开算法里 encrypt/decrypt 不是互逆对；与真服务端的互通已由
# 2026-10-31 生产探针实证）。离线测试打桩 rsa_decrypt，锁路由、
# 解析与分类；请求侧用确定性密文（随机前缀全零）逐字节断言。
_APP_INNER = {"3551001": {"file_name": "big.mkv", "file_size": 843844273,
                          "pick_code": "PC-BIG",
                          "url": {"url": APP_URL, "client": 2}}}


def _app_ok_body():
    return {"state": True, "errno": 0, "data": "STUB-CIPHER"}


def _stub_decrypt(monkeypatch):
    from backend.emby_server import pan115_cipher
    monkeypatch.setattr(pan115_cipher, "rsa_decrypt",
                        lambda b64: json.dumps(_APP_INNER).encode())


def _install_http(monkeypatch, web_body, app_body, seen):
    def _recorder(method, url, **kw):
        seen.append({"method": method, "url": url,
                     "params": kw.get("params"), "data": kw.get("data"),
                     "ua": kw.get("ua")})
        if "proapi.115.com" in url:
            return app_body() if callable(app_body) else app_body
        return web_body() if callable(web_body) else web_body

    monkeypatch.setattr(transfer115, "_http_json", _recorder)


# ---------- 分类 ----------

def test_50028_classified_as_channel_limit(monkeypatch):
    seen = []
    _install_http(monkeypatch, SIZE_LIMIT_BODY, {"state": True, "data": ""}, seen)
    client = transfer115.Pan115Client(AUTH)
    with pytest.raises(transfer115.Pan115Error) as excinfo:
        client._download_url_web("PC-BIG")
    assert isinstance(excinfo.value, transfer115.Pan115ChannelLimitError)
    assert excinfo.value.kind == "size_limit"


def test_cipher_encrypt_deterministic_single_block():
    from base64 import b64decode

    from backend.emby_server import pan115_cipher
    c1 = pan115_cipher.rsa_encrypt(b'{"pickcode":"X"}')
    c2 = pan115_cipher.rsa_encrypt(b'{"pickcode":"X"}')
    assert c1 == c2  # 随机前缀全零 → 确定性（与开源实现默认一致）
    assert len(b64decode(c1)) == 128  # RSA 1024 单块


# ---------- download_url 回落 ----------

def test_download_url_falls_back_to_app_on_50028(monkeypatch):
    seen = []
    _stub_decrypt(monkeypatch)
    _install_http(monkeypatch, SIZE_LIMIT_BODY, _app_ok_body, seen)
    client = transfer115.Pan115Client(AUTH)
    url = client.download_url("PC-BIG")
    assert url == APP_URL
    urls = [s["url"] for s in seen]
    assert any("webapi.115.com" in u for u in urls)
    assert any("proapi.115.com" in u and u.endswith("/app/chrome/downurl")
               for u in urls)
    # app 通道请求体是加密 data：与独立构造的期望密文逐字节一致
    # （随机前缀全零 → 确定性），期望明文含 pickcode 与 user_id
    from backend.emby_server import pan115_cipher
    app_call = [s for s in seen if "proapi.115.com" in s["url"]][0]
    expected = pan115_cipher.rsa_encrypt(json.dumps(
        {"pickcode": "PC-BIG", "user_id": 123456}).encode())
    assert app_call["data"] == {"data": expected}


def test_download_url_web_ok_no_app_call(monkeypatch):
    seen = []
    _install_http(monkeypatch, {"state": True, "file_url": WEB_URL},
                  _app_ok_body, seen)
    client = transfer115.Pan115Client(AUTH)
    assert client.download_url("PC-SMALL") == WEB_URL
    assert not any("proapi.115.com" in s["url"] for s in seen)


def test_download_url_app_first_skips_web(monkeypatch):
    seen = []
    _stub_decrypt(monkeypatch)
    _install_http(monkeypatch, {"state": True, "file_url": WEB_URL},
                  _app_ok_body, seen)
    client = transfer115.Pan115Client(AUTH, ua="Lavf/60.16.100")
    url = client.download_url("PC-BIG", app_first=True)
    assert url == APP_URL
    assert not any("webapi.115.com" in s["url"] for s in seen)


def test_download_url_both_channels_limited_raises_channel_limit(monkeypatch):
    seen = []
    app_limit = {"state": False, "errno": 50028, "msg": "文件大小超出限制"}
    _install_http(monkeypatch, SIZE_LIMIT_BODY, app_limit, seen)
    client = transfer115.Pan115Client(AUTH)
    with pytest.raises(transfer115.Pan115ChannelLimitError):
        client.download_url("PC-BIG")


# ---------- 巡检不记 strike ----------

class _LimitFake115:
    def __init__(self, exc):
        self._exc = exc

    def download_url(self, pickcode, **kw):
        raise self._exc


def _item115(db):
    row = em.MediaItem(guid="tv115-large-1", library_id=1, item_type="movie",
                       name="大片", drive_file_id="srcfid",
                       file_path="mount://9/电影/a.mkv", size=1000)
    db.add(row)
    db.commit()
    return row


def _binding115(db, user_id):
    cred = em.MountCredential(scope="user", owner_user_id=user_id,
                              status="active")
    db.add(cred)
    db.commit()
    row = em.UserStorageBinding(user_id=user_id, provider="pan115",
                                account_label="test115",
                                credential_id=cred.id, status="active")
    db.add(row)
    db.commit()
    return row


def _ready_115_row(db, item_id):
    row = em.TransferVariant(item_id=item_id, source_file_id="srcfid",
                             variant_file_id="newfid",
                             variant_drive_id="pan115:user-7",
                             owner_scope="user", owner_ref="7",
                             status="ready", pickcode="PC-BIG")
    db.add(row)
    db.commit()
    return row


def test_patrol_channel_limit_no_strike(db, monkeypatch):
    item = _item115(db)
    _binding115(db, 7)
    row = _ready_115_row(db, item.id)
    fake = _LimitFake115(
        transfer115.Pan115ChannelLimitError("文件大小超出限制"))
    monkeypatch.setattr(tv, "_make_pan115_client", lambda auth: fake)
    monkeypatch.setattr(ub, "binding_115_cookie",
                        lambda db_arg, user_id, binding_id: AUTH)
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 0
    tv.patrol_once(db, lambda: object())
    db.refresh(row)
    assert row.status == "ready" and row.stale_count == 0


# ---------- 播放侧 resolve ----------

class _ResolveFake115:
    behavior = "ok"
    seen = {}

    def __init__(self, auth, ua=""):
        type(self).seen = {"ua": ua}

    def download_url(self, pickcode, **kw):
        type(self).seen["app_first"] = kw.get("app_first")
        if type(self).behavior == "limit":
            raise transfer115.Pan115ChannelLimitError("文件大小超出限制")
        return APP_URL


def _patch_resolve(monkeypatch, behavior):
    _ResolveFake115.behavior = behavior
    monkeypatch.setattr(ub, "unseal_secret", lambda sealed: AUTH)
    monkeypatch.setattr(transfer115, "Pan115Client", _ResolveFake115)


def test_copy_url_passes_player_ua_app_first(db, monkeypatch):
    _binding115(db, 7)
    _patch_resolve(monkeypatch, "ok")
    url = ub.resolve_user115_copy_url(db, 7, "PC-BIG", ua="Lavf/60.16.100")
    assert url == APP_URL
    assert _ResolveFake115.seen["ua"] == "Lavf/60.16.100"
    assert _ResolveFake115.seen["app_first"] is True


def test_copy_url_channel_limit_no_binding_state_change(db, monkeypatch):
    binding = _binding115(db, 7)
    _patch_resolve(monkeypatch, "limit")
    with pytest.raises(ub.BindingError) as excinfo:
        ub.resolve_user115_copy_url(db, 7, "PC-BIG", ua="Lavf/60.16.100")
    assert excinfo.value.kind == "size_limit"
    db.refresh(binding)
    assert binding.status == "active"

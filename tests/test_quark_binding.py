"""夸克网盘用户绑定（一期：绑定管理 + 验活 + 名单 + 播放门禁）。

钉死的范围（多一件不做）：
- provider="quark" 接入 user_bindings：Cookie 建绑即验活（/config +
  列根目录）、失效三档（auth/expired/throttled，照 pan115 口径）、
  重验恢复、解绑删双行——全部复用既有绑定行 + 凭据行，不另起存储；
- Cookie 轮换（__puus）验活后回写密封凭据，只存初始值会慢性失效；
- providers 名单：挂载映射加 quark、BYOS_EXTRA_BINDING_PROVIDERS
  允许集加 quark、SystemConfig 开关 quark_bind_enabled（默认开）
  关掉时名单与建绑都拒；
- 播放门禁：active 夸克绑定计入放行（门禁本体 provider 无关，
  本文件锁死这条口径不被改坏）。
- 会员校验（2026-10-31 追加，照 115 验 VIP 对齐）：验活通过后调
  /member 判会员三态，确凿非会员落 invalid + quark_vip_required；
  开关 quark_bind_vip_check_enabled（SystemConfig，默认开）关时
  不拦；查不清（unknown / 接口异常）不误杀、照常放行。
  MINI_VIP（电视端小会员）不算会员（用户 2026-10-31 亲定）：
  只有 MINI_VIP 信号、无正经档（SUPER_VIP/VIP/Z_VIP）→ 非会员；
  member_type 字面 "MINI_VIP" 含 "VIP" 子串，不许子串命中。

不做：夸克转存与副本点播（二期）、按绑定扫描、前端。
夸克网络全部用桩替身，不碰真实夸克。
"""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_gate
from backend.emby_server import quarkdrive
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import unseal_secret
from backend.integrations import store


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
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _quark_switch_cache_isolation():
    # store 的 TTL 缓存进程级只按键名索引、不分库（#1457 同款污染）：
    # 每个用例前后失效夸克开关键，防前序用例写的开关值串场。
    keys = [ub.KEY_QUARK_BIND_ENABLED]
    vip_key = getattr(ub, "KEY_QUARK_BIND_VIP_CHECK", None)
    if vip_key:
        keys.append(vip_key)
    for key in keys:
        store.invalidate(key)
    yield
    for key in keys:
        store.invalidate(key)


FAKE_COOKIE = "__puus=old-token; __pus=abc; other=1"


class _StubQuarkClient:
    """夸克客户端桩：行为由类属性定，调用次数可数，可模拟 Cookie 轮换。"""

    calls = {"check": 0, "list_root": 0}
    behavior = "ok"
    rotate_to = None
    vip_result = "vip"
    member_calls = 0

    def __init__(self, cookie):
        self.cookie = cookie

    def _behavior_error(self):
        """行为档 → 要抛的异常（无则 None）；三步验活共用同一口径。"""
        behavior = type(self).behavior
        if behavior == "auth_error":
            return quarkdrive.QuarkAuthError("夸克登录态失效")
        if behavior == "auth_detailed":
            # 生产 binding 427 的真实形状（合成复刻）：验活第一步
            # HTTP 401 + code=31001 + require login [guest]。
            return quarkdrive.QuarkAuthError(
                "夸克返回 HTTP 401（登录态失效），上游 code=31001"
                "：require login [guest]",
                step="check", upstream_code=31001,
                upstream_message="require login [guest]")
        if behavior == "risk_auth":
            # 风控文案混着登录字样（合成）：类型是鉴权异常，但
            # 上游原话是环境风险——不许定性 expired。
            return quarkdrive.QuarkAuthError(
                "夸克登录态失效：登录环境存在风险，请稍后再试",
                step="check", upstream_code=31003,
                upstream_message="登录环境存在风险，请稍后再试")
        if behavior == "throttled":
            return quarkdrive.QuarkError("夸克返回 HTTP 429（请求频繁）")
        if behavior == "transient":
            return quarkdrive.QuarkError("夸克返回 HTTP 500")
        return None

    def check(self):
        type(self).calls["check"] += 1
        err = self._behavior_error()
        if err is not None:
            raise err
        if type(self).rotate_to:
            self.cookie = type(self).rotate_to
        return {"ok": True}

    def list_root(self):
        type(self).calls["list_root"] += 1
        err = self._behavior_error()
        if err is not None:
            raise err
        return []

    def list_dir(self, pdir_fid, page_size=100):
        # 验活第三步（list_dir 全量档，2026-11-02 口径对齐）：
        # 桩与真客户端同形，行为档与前两步一致。
        type(self).calls["list_dir"] = (
            type(self).calls.get("list_dir", 0) + 1)
        err = self._behavior_error()
        if err is not None:
            raise err
        return []

    def vip_status(self):
        type(self).member_calls += 1
        if type(self).vip_result == "raise":
            raise quarkdrive.QuarkError("夸克返回 HTTP 500")
        return type(self).vip_result


@pytest.fixture()
def _stub_client(monkeypatch):
    _StubQuarkClient.calls = {"check": 0, "list_root": 0}
    _StubQuarkClient.behavior = "ok"
    _StubQuarkClient.rotate_to = None
    _StubQuarkClient.vip_result = "vip"
    _StubQuarkClient.member_calls = 0
    monkeypatch.setattr(ub.quarkdrive, "QuarkClient", _StubQuarkClient)
    return _StubQuarkClient


def _bind(db, user_id=7, label="我的夸克"):
    return ub.create_binding(db, user_id, "quark", label, FAKE_COOKIE)


def _stored_cookie(db, binding):
    cred = db.query(em.MountCredential).filter(
        em.MountCredential.id == binding.credential_id).first()
    return unseal_secret(cred.sealed_credential)


# ---------- 建绑 + 验活 ----------

def test_create_quark_binding_success(db, _stub_client):
    binding = _bind(db)
    assert binding.status == "active"
    assert binding.provider == "quark"
    assert binding.last_verified_at is not None
    assert _StubQuarkClient.calls == {"check": 1, "list_root": 1, "list_dir": 1}
    # 凭据只存密封引用，解封后与入参一致；序列化不出凭据材料
    assert _stored_cookie(db, binding) == FAKE_COOKIE
    data = ub.serialize_binding(binding)
    assert data["provider"] == "quark"
    assert data["has_credential"] is True
    assert data["error_message"] is None
    assert ub.get_active_binding(db, 7, "quark").id == binding.id


def test_create_quark_auth_error_persists_expired(db, _stub_client):
    _StubQuarkClient.behavior = "auth_error"
    binding = _bind(db)
    assert binding.status == "expired"
    assert binding.last_error_kind == "expired"
    data = ub.serialize_binding(binding)
    assert "夸克" in (data["error_message"] or "")


def test_create_quark_throttled_persists_invalid(db, _stub_client):
    _StubQuarkClient.behavior = "throttled"
    binding = _bind(db)
    assert binding.status == "invalid"
    assert binding.last_error_kind == "throttled"


def test_create_quark_transient_rolls_back(db, _stub_client):
    _StubQuarkClient.behavior = "transient"
    with pytest.raises(ub.BindingError) as excinfo:
        _bind(db)
    assert excinfo.value.kind == "transient"
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.MountCredential).count() == 0


def test_create_quark_rotation_resealed(db, _stub_client):
    rotated = "__puus=new-token; __pus=abc; other=1"
    _StubQuarkClient.rotate_to = rotated
    binding = _bind(db)
    assert binding.status == "active"
    # __puus 轮换后必须回写密封凭据，只存初始值会慢性失效
    assert _stored_cookie(db, binding) == rotated


# ---------- 重验 ----------

def test_verify_quark_recovers_expired_and_rotates(db, _stub_client):
    _StubQuarkClient.behavior = "auth_error"
    binding = _bind(db)
    assert binding.status == "expired"
    _StubQuarkClient.behavior = "ok"
    rotated = "__puus=recovered-token; __pus=abc; other=1"
    _StubQuarkClient.rotate_to = rotated
    updated = ub.verify_binding(db, 7, binding.id)
    assert updated.status == "active"
    assert updated.last_error_kind is None
    assert _stored_cookie(db, updated) == rotated


def test_verify_quark_transient_keeps_status(db, _stub_client):
    binding = _bind(db)
    _StubQuarkClient.behavior = "transient"
    with pytest.raises(ub.BindingError) as excinfo:
        ub.verify_binding(db, 7, binding.id)
    assert excinfo.value.kind == "transient"
    db.refresh(binding)
    assert binding.status == "active"


# ---------- 会员校验（照 115 对齐） ----------

def _set_vip_switch(db, value):
    db.add(base_models.SystemConfig(
        key=ub.KEY_QUARK_BIND_VIP_CHECK, value=value))
    db.commit()
    store.invalidate(ub.KEY_QUARK_BIND_VIP_CHECK)


def test_create_quark_non_vip_blocked(db, _stub_client):
    _StubQuarkClient.vip_result = "not_vip"
    binding = _bind(db)
    assert binding.status == "invalid"
    assert binding.last_error_kind == "quark_vip_required"
    data = ub.serialize_binding(binding)
    assert "会员" in (data["error_message"] or "")
    # 非会员不许说成 Cookie 失效：凭据验活本身是通过的
    assert _StubQuarkClient.calls == {"check": 1, "list_root": 1, "list_dir": 1}
    assert _StubQuarkClient.member_calls == 1


def test_create_quark_vip_check_switch_off_allows_non_vip(db, _stub_client):
    _set_vip_switch(db, "false")
    assert ub.quark_bind_vip_check_enabled(db) is False
    _StubQuarkClient.vip_result = "not_vip"
    binding = _bind(db)
    assert binding.status == "active"
    # 开关关时根本不查会员
    assert _StubQuarkClient.member_calls == 0


def test_create_quark_vip_unknown_allows(db, _stub_client):
    _StubQuarkClient.vip_result = "unknown"
    binding = _bind(db)
    assert binding.status == "active"


def test_create_quark_vip_member_error_allows(db, _stub_client):
    # /member 异常（网络/上游）不误杀：查不清照常放行
    _StubQuarkClient.vip_result = "raise"
    binding = _bind(db)
    assert binding.status == "active"


def test_verify_quark_non_vip_marks_invalid(db, _stub_client):
    binding = _bind(db)
    assert binding.status == "active"
    _StubQuarkClient.vip_result = "not_vip"
    updated = ub.verify_binding(db, 7, binding.id)
    assert updated.status == "invalid"
    assert updated.last_error_kind == "quark_vip_required"


# ---------- MINI_VIP 不算会员（真实客户端全链，用户 2026-10-31 亲定） ----------

def _route_quark_http(monkeypatch, member_data):
    """按 URL 路由的夸克 HTTP 桩：验活两步放行、/member 返指定 data。

    不替换 QuarkClient 本体，让 create_binding 走真实 vip_status
    解析——MINI_VIP 剔除的口径只能在真实解析里验，桩 vip_result
    验不出来。
    """
    def fake_send(method, url, **kwargs):
        if url.endswith("/member"):
            return _FakeResp(body={"code": 0, "data": member_data})
        if url.endswith("/file/sort"):
            return _FakeResp(body={"code": 0, "data": {"list": []}})
        return _FakeResp(body={"code": 0, "data": {}})

    monkeypatch.setattr(quarkdrive, "_send", fake_send)


def _member_status(**flags):
    status = {"SUPER_VIP": "", "VIP": "", "Z_VIP": "", "MINI_VIP": ""}
    status.update(flags)
    return status


def test_create_quark_mini_vip_status_flag_only_blocked(db, monkeypatch):
    # member_status 只有 MINI_VIP 档持有、无正经档 → 非会员落 invalid
    _route_quark_http(monkeypatch, {
        "member_type": "NORMAL",
        "member_status": _member_status(MINI_VIP="1")})
    binding = _bind(db)
    assert binding.status == "invalid"
    assert binding.last_error_kind == "quark_vip_required"


def test_create_quark_mini_vip_member_type_blocked(db, monkeypatch):
    # member_type 字面就是 "MINI_VIP"（含 "VIP" 子串）→ 同样非会员
    _route_quark_http(monkeypatch, {
        "member_type": "MINI_VIP",
        "member_status": _member_status()})
    binding = _bind(db)
    assert binding.status == "invalid"
    assert binding.last_error_kind == "quark_vip_required"


def test_create_quark_mini_plus_svip_active(db, monkeypatch):
    # MINI_VIP 与正经档共存：有正经档信号即会员，照常建绑
    _route_quark_http(monkeypatch, {
        "member_type": "MINI_VIP",
        "member_status": _member_status(SUPER_VIP="1", MINI_VIP="1")})
    binding = _bind(db)
    assert binding.status == "active"


def test_quark_vip_check_default_on(db):
    assert ub.quark_bind_vip_check_enabled(db) is True
    _set_vip_switch(db, "0")
    assert ub.quark_bind_vip_check_enabled(db) is False


# ---------- 开关 ----------

def _set_quark_switch(db, value):
    db.add(base_models.SystemConfig(key=ub.KEY_QUARK_BIND_ENABLED, value=value))
    db.commit()
    store.invalidate(ub.KEY_QUARK_BIND_ENABLED)


def test_quark_switch_off_rejects_create(db, _stub_client):
    _set_quark_switch(db, "false")
    assert ub.quark_bind_enabled(db) is False
    with pytest.raises(ub.BindingError) as excinfo:
        _bind(db)
    assert excinfo.value.kind == "unsupported"
    assert db.query(em.UserStorageBinding).count() == 0


def test_quark_switch_default_on(db):
    assert ub.quark_bind_enabled(db) is True


# ---------- providers 名单 ----------

def _mount(db, name, mount_type, is_enabled=True):
    row = em.StorageMount(name=name, mount_type=mount_type, path="",
                          is_enabled=is_enabled)
    db.add(row)
    db.commit()
    return row


def test_providers_quark_via_mount_mapping(db):
    _mount(db, "m-quark", "quark")
    assert ub.list_binding_providers(db) == ["quark"]


def test_providers_quark_via_extra_env(db, monkeypatch):
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "quark")
    assert ub.list_binding_providers(db) == ["quark"]


def test_providers_quark_hidden_when_switch_off(db, monkeypatch):
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "quark")
    _set_quark_switch(db, "0")
    assert ub.list_binding_providers(db) == []


def test_providers_existing_order_unchanged(db):
    _mount(db, "m-drive", "drive")
    _mount(db, "m-115", "115")
    _mount(db, "m-quark", "quark")
    assert ub.list_binding_providers(db) == ["drive", "pan115", "quark"]


# ---------- 播放门禁 ----------

def test_gate_counts_active_quark_binding(db, _stub_client):
    assert playback_gate.has_active_binding(db, 7) is False
    binding = _bind(db)
    assert playback_gate.has_active_binding(db, 7) is True
    binding.status = "expired"
    db.commit()
    assert playback_gate.has_active_binding(db, 7) is False


# ---------- 夸克客户端本体（桩 HTTP 出口） ----------

class _FakeHeaders(dict):
    def get_list(self, name):
        value = self.get(name.lower())
        if value is None:
            return []
        return [value] if isinstance(value, str) else list(value)


class _FakeResp:
    def __init__(self, status_code=200, body=None, set_cookie=None):
        self.status_code = status_code
        self._body = body if body is not None else {"code": 0, "data": {}}
        headers = {}
        if set_cookie:
            headers["set-cookie"] = set_cookie
        self.headers = _FakeHeaders(headers)

    def json(self):
        return self._body


@pytest.fixture()
def _stub_send(monkeypatch):
    box = {"resp": _FakeResp(), "seen": None}

    def fake_send(method, url, **kwargs):
        box["seen"] = (method, url, kwargs)
        return box["resp"]

    monkeypatch.setattr(quarkdrive, "_send", fake_send)
    return box


def test_client_check_ok_and_cookie_rotation(_stub_send):
    _stub_send["resp"] = _FakeResp(
        body={"code": 0, "data": {}},
        set_cookie="__puus=fresh-token; Path=/; HttpOnly")
    client = quarkdrive.QuarkClient("__puus=stale-token; other=1")
    assert client.check() == {"ok": True}
    assert "__puus=fresh-token" in client.cookie
    assert "other=1" in client.cookie
    method, url, kwargs = _stub_send["seen"]
    assert method == "GET"
    assert url.endswith("/config")
    assert kwargs["params"]["pr"] == "ucpro"
    assert kwargs["headers"]["Referer"] == "https://pan.quark.cn/"


def test_client_envelope_auth_message_raises_auth_error(_stub_send):
    _stub_send["resp"] = _FakeResp(
        body={"code": 31001, "message": "登录已过期，请重新登录"})
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    with pytest.raises(quarkdrive.QuarkAuthError):
        client.check()


def test_client_http_403_raises_auth_error(_stub_send):
    _stub_send["resp"] = _FakeResp(status_code=403)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    with pytest.raises(quarkdrive.QuarkAuthError):
        client.check()


def test_client_http_429_classifies_throttled(_stub_send):
    _stub_send["resp"] = _FakeResp(status_code=429)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    with pytest.raises(quarkdrive.QuarkError) as excinfo:
        client.check()
    classified = ub._classify_quark_error(excinfo.value)
    assert classified is not None
    assert classified[0] == "throttled"


def test_client_list_root_parses_list(_stub_send):
    _stub_send["resp"] = _FakeResp(body={
        "code": 0,
        "data": {"list": [{"fid": "f1", "file_name": "a.mkv"}]},
    })
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    files = client.list_root()
    assert files and files[0]["fid"] == "f1"
    _method, url, kwargs = _stub_send["seen"]
    assert url.endswith("/file/sort")
    assert kwargs["params"]["pdir_fid"] == "0"


# ---------- 客户端会员三态（桩 HTTP 出口，/member 响应形状） ----------

_MEMBER_VIP_BODY = {
    "code": 0,
    "data": {
        "member_type": "SUPER_VIP",
        "member_status": {
            "SUPER_VIP": "1", "VIP": "", "Z_VIP": "", "MINI_VIP": ""},
        "total_capacity": 6597069766656,
        "use_capacity": 1024,
    },
}


def test_client_vip_status_svip(_stub_send):
    _stub_send["resp"] = _FakeResp(body=_MEMBER_VIP_BODY)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "vip"
    _method, url, _kwargs = _stub_send["seen"]
    assert url.endswith("/member")


def test_client_vip_status_member_type_vip(_stub_send):
    body = {"code": 0, "data": {
        "member_type": "VIP",
        "member_status": {
            "SUPER_VIP": "", "VIP": "", "Z_VIP": "", "MINI_VIP": ""}}}
    _stub_send["resp"] = _FakeResp(body=body)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "vip"


def test_client_vip_status_free_account(_stub_send):
    body = {"code": 0, "data": {
        "member_type": "NORMAL",
        "member_status": {
            "SUPER_VIP": "", "VIP": "", "Z_VIP": "", "MINI_VIP": ""},
        "total_capacity": 10737418240,
        "use_capacity": 0}}
    _stub_send["resp"] = _FakeResp(body=body)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "not_vip"


def test_client_vip_status_missing_fields_unknown(_stub_send):
    _stub_send["resp"] = _FakeResp(body={"code": 0, "data": {}})
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "unknown"


def test_client_vip_status_z_vip_flag_kept(_stub_send):
    # Z_VIP 仍算正经会员：本轮只剔 MINI_VIP，Z 档口径不动
    body = {"code": 0, "data": {
        "member_type": "NORMAL",
        "member_status": {
            "SUPER_VIP": "", "VIP": "", "Z_VIP": "1", "MINI_VIP": ""}}}
    _stub_send["resp"] = _FakeResp(body=body)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "vip"


def test_client_vip_status_mini_vip_status_flag_only_not_vip(_stub_send):
    # 只有 MINI_VIP 档持有、无正经档 → not_vip（用户 2026-10-31 亲定）
    body = {"code": 0, "data": {
        "member_type": "NORMAL",
        "member_status": {
            "SUPER_VIP": "", "VIP": "", "Z_VIP": "", "MINI_VIP": "1"}}}
    _stub_send["resp"] = _FakeResp(body=body)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "not_vip"


def test_client_vip_status_mini_vip_member_type_not_vip(_stub_send):
    # member_type 字面 "MINI_VIP" 含 "VIP" 子串，不许被子串命中判会员
    body = {"code": 0, "data": {
        "member_type": "MINI_VIP",
        "member_status": {
            "SUPER_VIP": "", "VIP": "", "Z_VIP": "", "MINI_VIP": ""}}}
    _stub_send["resp"] = _FakeResp(body=body)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "not_vip"


def test_client_vip_status_mini_plus_svip_is_vip(_stub_send):
    # MINI_VIP 与正经档共存 → 有正经档即 vip
    body = {"code": 0, "data": {
        "member_type": "MINI_VIP",
        "member_status": {
            "SUPER_VIP": "1", "VIP": "", "Z_VIP": "", "MINI_VIP": "1"}}}
    _stub_send["resp"] = _FakeResp(body=body)
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    assert client.vip_status() == "vip"


# ---------- 风控误判 expired 修复 + 验活失败留痕（2026-11-02） ----------
#
# 背景：新绑定一绑就落 expired 且无从查因。合成信封锁死三件事：
# 1. 风控/限频文案（哪怕混着登录字样）不许定性 expired，落
#    throttled（稍后再试），无论它走信封还是 HTTP 401/403 通道；
# 2. 真鉴权失败（401 guest 形状）照旧 expired，字段齐全；
# 3. 验活失败必须在绑定层日志留下 step/upstream_code/message
#    （已脱敏），且日志绝不带 Cookie 值。


def test_client_http_401_guest_still_auth_error(_stub_send):
    # 生产 binding 427 的回话形状（合成复刻）：401 + code=31001
    # + require login [guest] = 上游眼里根本没登录态 → expired。
    _stub_send["resp"] = _FakeResp(
        status_code=401,
        body={"code": 31001, "message": "require login [guest]"})
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    with pytest.raises(quarkdrive.QuarkAuthError) as excinfo:
        client.check()
    assert excinfo.value.step == "check"
    assert excinfo.value.upstream_code == 31001
    classified = ub._classify_quark_error(excinfo.value)
    assert classified is not None and classified[0] == "expired"


def test_client_envelope_risk_message_is_throttled_not_auth(_stub_send):
    # 信封文案混着「登录」二字，但本质是风控：不许 QuarkAuthError。
    _stub_send["resp"] = _FakeResp(
        body={"code": 31003, "message": "登录环境存在风险，请稍后再试"})
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    with pytest.raises(quarkdrive.QuarkError) as excinfo:
        client.check()
    assert not isinstance(excinfo.value, quarkdrive.QuarkAuthError)
    classified = ub._classify_quark_error(excinfo.value)
    assert classified is not None and classified[0] == "throttled"


def test_client_http_403_risk_body_is_throttled_not_auth(_stub_send):
    # HTTP 403 的响应体明说操作频繁：风控优先于 403 的鉴权定性。
    _stub_send["resp"] = _FakeResp(
        status_code=403,
        body={"code": 31001, "message": "操作频繁，请稍后再试"})
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    with pytest.raises(quarkdrive.QuarkError) as excinfo:
        client.check()
    assert not isinstance(excinfo.value, quarkdrive.QuarkAuthError)
    classified = ub._classify_quark_error(excinfo.value)
    assert classified is not None and classified[0] == "throttled"


def test_client_envelope_auth_failure_logged(_stub_send, caplog):
    # 信封鉴权失败此前抛错前不打日志，失败形状只剩 kind 可见；
    # 现在须与 HTTP 401/403 同款留痕（step/code/message）。
    _stub_send["resp"] = _FakeResp(
        body={"code": 31001, "message": "登录已过期，请重新登录"})
    client = quarkdrive.QuarkClient(FAKE_COOKIE)
    with caplog.at_level(logging.WARNING,
                         logger="backend.emby_server.quarkdrive"):
        with pytest.raises(quarkdrive.QuarkAuthError):
            client.check()
    assert "31001" in caplog.text
    assert "check" in caplog.text


def test_create_quark_risk_auth_persists_throttled(db, _stub_client):
    # 鉴权异常类型 + 风控原话 → 落 throttled（invalid），不落
    # expired；文案说人话：临时拦截、稍后再试，不许说已过期。
    _StubQuarkClient.behavior = "risk_auth"
    binding = _bind(db)
    assert binding.status == "invalid"
    assert binding.last_error_kind == "throttled"
    data = ub.serialize_binding(binding)
    assert "登录已过期" not in (data["error_message"] or "")
    assert "稍后再试" in (data["error_message"] or "")


def test_verify_quark_risk_auth_marks_throttled_not_expired(
        db, _stub_client):
    _StubQuarkClient.behavior = "auth_error"
    binding = _bind(db)
    assert binding.status == "expired"
    _StubQuarkClient.behavior = "risk_auth"
    out = ub.verify_binding(db, 7, binding.id)
    assert out.status == "invalid"
    assert out.last_error_kind == "throttled"


def test_create_quark_auth_failure_logs_detail_without_cookie(
        db, _stub_client, caplog):
    # 绑定层留痕：kind/step/upstream_code/上游原话（脱敏）可查，
    # Cookie 值绝不进日志（FAKE_COOKIE 的 __puus 值 old-token）。
    _StubQuarkClient.behavior = "auth_detailed"
    with caplog.at_level(logging.WARNING,
                         logger="backend.emby_server.user_bindings"):
        binding = _bind(db)
    assert binding.status == "expired"
    assert binding.last_error_kind == "expired"
    assert "kind=expired" in caplog.text
    assert "step=check" in caplog.text
    assert "31001" in caplog.text
    assert "require login [guest]" in caplog.text
    assert "old-token" not in caplog.text

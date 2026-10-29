"""115 绑定 VIP 校验（2026-10-29 用户亲定）。

钉死的范围：
- 建绑/重验时 Cookie 验活通过后追加 VIP 校验：确凿非 VIP → 落
  invalid + last_error_kind=pan115_vip_required + 明示文案（不许
  说成 Cookie 失效）；是 VIP 照常 active；
- 查不清（探针异常 / 响应形状对不上 → unknown）不拦、照常 active；
- 开关 pan115_bind_vip_check_enabled（SystemConfig，默认开）关掉
  时跳过校验、回现行行为，且不许发出 VIP 探针请求；
- Cookie 本身三档（expired/auth_error/throttled）行为逐字不变，
  且 Cookie 验活没过时不许先跑 VIP 探针；
- transfer115.Pan115Client.vip_status 的三态解析：vip/is_vip
  在场定真假、不在场回 unknown（不把字段缺失压成非 VIP）。

115 网络全部用桩替身，不碰真实 115。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import transfer115
from backend.emby_server.transfer115 import Pan115Client as _RealPan115Client
from backend.emby_server import user_bindings as ub
from backend.integrations import store

VIP_MESSAGE = "该 115 账号不是 VIP，115 绑定需要 VIP 账号，请开通后重新绑定。"


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


class _Stub115Client:
    """115 客户端桩：check/list_dir 行为与 vip 探针行为分开控。"""

    calls = {"check": 0, "list_dir": 0, "vip_status": 0}

    def __init__(self, cookie, ua=""):
        self.cookie = cookie

    def check(self):
        _Stub115Client.calls["check"] += 1
        if _Stub115Client.behavior == "auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if _Stub115Client.behavior == "throttled":
            raise transfer115.Pan115Error("115 返回 HTTP 429")
        if _Stub115Client.behavior == "transient":
            raise transfer115.Pan115Error("115 返回 HTTP 500")
        return {"ok": True, "uid": "u1", "vip": True}

    def list_dir(self, cid="0", limit=200):
        _Stub115Client.calls["list_dir"] += 1
        if _Stub115Client.behavior == "auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        return [{"name": "f.mkv", "pc": "PC1", "is_dir": False}]

    def vip_status(self):
        _Stub115Client.calls["vip_status"] += 1
        behavior = _Stub115Client.vip_behavior
        if behavior == "raise":
            raise transfer115.Pan115Error("115 返回 HTTP 500")
        return behavior  # "vip" / "not_vip" / "unknown"


_Stub115Client.behavior = "ok"
_Stub115Client.vip_behavior = "vip"


@pytest.fixture(autouse=True)
def _stub_client(monkeypatch):
    _Stub115Client.behavior = "ok"
    _Stub115Client.vip_behavior = "vip"
    _Stub115Client.calls = {"check": 0, "list_dir": 0, "vip_status": 0}
    monkeypatch.setattr(ub.transfer115, "Pan115Client", _Stub115Client)
    # store TTL 缓存只按键名索引、不分库：每用例前后都失效本键，
    # 防跨用例/跨文件把开关值读串（#1457 同款污染教训）。
    store.invalidate(ub.KEY_PAN115_BIND_VIP_CHECK)
    yield
    store.invalidate(ub.KEY_PAN115_BIND_VIP_CHECK)


def _set_switch(db, value):
    db.add(base_models.SystemConfig(
        key=ub.KEY_PAN115_BIND_VIP_CHECK, value=value))
    db.commit()
    store.invalidate(ub.KEY_PAN115_BIND_VIP_CHECK)


def _bind(db, user_id=7, label="主号"):
    return ub.create_binding(db, user_id, "pan115", label, "COOKIE-XYZ")


def _cred_of(db, binding):
    return (db.query(em.MountCredential)
            .filter(em.MountCredential.id == binding.credential_id)
            .first())


# ---------- 建绑 ----------

def test_create_vip_goes_active(db):
    binding = _bind(db)
    assert binding.status == "active"
    assert binding.last_error_kind is None
    assert _Stub115Client.calls["vip_status"] == 1
    assert _cred_of(db, binding).status == "active"


def test_create_not_vip_rejected_with_reason(db):
    _Stub115Client.vip_behavior = "not_vip"
    binding = _bind(db)
    assert binding.status == "invalid"
    assert binding.last_error_kind == "pan115_vip_required"
    cred = _cred_of(db, binding)
    assert cred.status == "invalid"
    assert cred.last_error_kind == "pan115_vip_required"
    # 接口出参：kind 可辨识、文案明示是 VIP 问题（不许说成 Cookie 失效）
    out = ub.serialize_binding(binding)
    assert out["last_error_kind"] == "pan115_vip_required"
    assert out["error_message"] == VIP_MESSAGE
    assert ub.error_message("pan115_vip_required") == VIP_MESSAGE


def test_create_vip_unknown_shape_passes(db):
    _Stub115Client.vip_behavior = "unknown"
    binding = _bind(db)
    assert binding.status == "active"
    assert binding.last_error_kind is None


def test_create_vip_probe_error_passes(db):
    _Stub115Client.vip_behavior = "raise"
    binding = _bind(db)
    assert binding.status == "active"


def test_create_switch_off_skips_vip_check(db):
    _set_switch(db, "false")
    _Stub115Client.vip_behavior = "not_vip"
    binding = _bind(db)
    assert binding.status == "active"
    assert _Stub115Client.calls["vip_status"] == 0


def test_create_cookie_auth_error_unchanged_and_no_vip_probe(db):
    _Stub115Client.behavior = "auth_error"
    binding = _bind(db)
    assert binding.status == "expired"
    assert binding.last_error_kind == "expired"
    assert _Stub115Client.calls["vip_status"] == 0


# ---------- 重验 ----------

def test_verify_not_vip_marks_vip_required(db):
    binding = _bind(db)
    assert binding.status == "active"
    _Stub115Client.vip_behavior = "not_vip"
    updated = ub.verify_binding(db, 7, binding.id)
    assert updated.status == "invalid"
    assert updated.last_error_kind == "pan115_vip_required"


def test_verify_vip_recovers_after_upgrade(db):
    _Stub115Client.vip_behavior = "not_vip"
    binding = _bind(db)
    assert binding.status == "invalid"
    _Stub115Client.vip_behavior = "vip"
    updated = ub.verify_binding(db, 7, binding.id)
    assert updated.status == "active"
    assert updated.last_error_kind is None


def test_verify_cookie_auth_error_unchanged(db):
    binding = _bind(db)
    _Stub115Client.behavior = "auth_error"
    updated = ub.verify_binding(db, 7, binding.id)
    assert updated.status == "expired"
    assert updated.last_error_kind == "expired"


# ---------- 开关 reader ----------

def test_switch_reader_defaults_on_and_honors_off(db):
    assert ub.pan115_bind_vip_check_enabled(db) is True
    _set_switch(db, "false")
    assert ub.pan115_bind_vip_check_enabled(db) is False


# ---------- transfer115 三态解析（真客户端 + 桩 HTTP 层） ----------

def _client_with_body(monkeypatch, body):
    # 注意：autouse 桩已把 transfer115.Pan115Client 换成桩类，解析
    # 测试必须用模块导入时捕获的真类构造，只桩它的 HTTP 层。
    monkeypatch.setattr(
        transfer115, "_http_json",
        lambda method, url, **kwargs: body)
    return _RealPan115Client("UID=1; CID=2")


def test_vip_status_parsing(monkeypatch):
    cases = [
        ({"state": True, "data": {"vip": 1, "uid": "9"}}, "vip"),
        ({"state": True, "data": {"is_vip": True}}, "vip"),
        ({"state": True, "data": {"vip": 0}}, "not_vip"),
        ({"state": True, "data": {"is_vip": False}}, "not_vip"),
        ({"state": True, "data": {"uid": "9"}}, "unknown"),
        ({"state": True, "data": [1, 2]}, "unknown"),
    ]
    for body, expected in cases:
        client = _client_with_body(monkeypatch, body)
        assert client.vip_status() == expected, body

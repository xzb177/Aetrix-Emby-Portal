"""BYOS P1（绑定管理 + 115 直链播放）。

钉死的范围（多一件不做）：
- 新表 user_storage_bindings（label 唯一、provider/status 约束）+
  凭据只存 mount_credentials 行引用，不另起第三套秘密存储；
- 建绑即验活（115 check + 列根目录）、解绑删双行、失效三档；
- 115 直链变体：清单追加 + 取流 302 + 凭据绝不跨用户（不走共享
  pickcode 缓存、不读写共享目录缓存）；
- 无绑定用户与共享库行为不变（清单不含 byos115_direct）。

不做：Drive OAuth（P2）、按绑定扫描（P3）、路径授权、前端。
115 网络全部用桩替身，不碰真实 115。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import play_variants as pv
from backend.emby_server import transfer115
from backend.emby_server import user_bindings as ub
from backend.emby_server.upstream_sources import unseal_secret


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
    """115 客户端桩：行为由构造参数定，调用次数可数。"""

    calls = {"check": 0, "list_dir": 0, "download_url": 0}

    def __init__(self, cookie, ua=""):
        self.cookie = cookie
        self.behavior = _Stub115Client.behavior

    def check(self):
        _Stub115Client.calls["check"] += 1
        if self.behavior == "auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        if self.behavior == "throttled":
            raise transfer115.Pan115Error("115 返回 HTTP 429")
        if self.behavior == "transient":
            raise transfer115.Pan115Error("115 返回 HTTP 500")
        return {"ok": True, "uid": "u1", "vip": False}

    def list_dir(self, cid="0", limit=200):
        _Stub115Client.calls["list_dir"] += 1
        if self.behavior == "auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        return [{"name": "f.mkv", "pc": "PC1", "is_dir": False}]

    def download_url(self, pickcode):
        _Stub115Client.calls["download_url"] += 1
        if self.behavior == "auth_error":
            raise transfer115.Pan115AuthError("Cookie 已失效")
        return f"https://down.115.com/{pickcode}"


_Stub115Client.behavior = "ok"


@pytest.fixture(autouse=True)
def _stub_client(monkeypatch):
    _Stub115Client.behavior = "ok"
    _Stub115Client.calls = {"check": 0, "list_dir": 0, "download_url": 0}
    monkeypatch.setattr(ub.transfer115, "Pan115Client", _Stub115Client)
    yield


def _mount115(db):
    m = em.StorageMount(name="m115", mount_type="115", path="115:/")
    db.add(m)
    db.commit()
    return m


def _item(db, mount, rel="/f.mkv"):
    lib = em.Library(guid="lib1", name="lib1")
    db.add(lib)
    db.commit()
    it = em.MediaItem(guid="g1", library_id=lib.id, item_type="movie",
                      name="片", file_path=f"mount://{mount.id}{rel}")
    db.add(it)
    db.commit()
    return it


def _bind(db, user_id=7, label="主号"):
    return ub.create_binding(db, user_id, "pan115", label, "COOKIE-XYZ")


# ---------- 模型约束 ----------

def test_label_unique_per_user_provider(db):
    _bind(db, 7, "主号")
    with pytest.raises(ub.BindingError):
        ub.create_binding(db, 7, "pan115", "主号", "COOKIE-2")
    # 不同用户可重名（唯一约束是 user_id + provider + label 三元）
    b3 = ub.create_binding(db, 8, "pan115", "主号", "COOKIE-3")
    assert b3.account_label == "主号"


def test_provider_and_status_check_constraints(db):
    cred = em.MountCredential(mount_id=0, scope="user", owner_user_id=7,
                              sealed_credential="x", key_id="fernet-v1")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(user_id=7, provider="bogus",
                                 account_label="a", credential_id=cred.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(em.UserStorageBinding(user_id=7, provider="pan115",
                                 account_label="a", credential_id=cred.id,
                                 status="bogus"))
    with pytest.raises(IntegrityError):
        db.commit()


# ---------- 凭据行约束（P1 实测修） ----------

def test_mount_attached_credential_unique_still_enforced(db):
    # 挂载附属的用户行仍受（一挂载一 scope 一用户一行）约束；
    # 只有 mount_id=NULL 的绑定行豁免（NULL 在唯一约束里互不相等）。
    # 注：服级行 owner_user_id=NULL 本就不受该约束限（#1230 原语义，
    # 本 PR 不改）；这里钉的是用户行那一半。
    db.add(em.MountCredential(mount_id=5, scope="user",
                              owner_user_id=7, sealed_credential="x"))
    db.commit()
    db.add(em.MountCredential(mount_id=5, scope="user",
                              owner_user_id=7, sealed_credential="y"))
    with pytest.raises(IntegrityError):
        db.commit()


# ---------- 建绑与验活 ----------

def test_create_binding_success_stores_reference_not_secret(db):
    b = _bind(db, 7, "主号")
    assert b.status == "active"
    assert b.last_verified_at is not None
    cred = db.query(em.MountCredential).filter(
        em.MountCredential.id == b.credential_id).first()
    assert cred is not None
    assert cred.scope == "user" and cred.owner_user_id == 7
    assert cred.status == "active"
    assert cred.sealed_credential != "COOKIE-XYZ"
    assert unseal_secret(cred.sealed_credential) == "COOKIE-XYZ"
    assert cred.key_id == "fernet-v1"
    out = ub.serialize_binding(b)
    assert "COOKIE-XYZ" not in str(out) and "sealed" not in str(out).lower()
    assert out["has_credential"] is True
    assert "credential_id" not in out


def test_create_binding_empty_credential_rejected(db):
    with pytest.raises(ub.BindingError):
        ub.create_binding(db, 7, "pan115", "空", "   ")
    assert db.query(em.UserStorageBinding).count() == 0


def test_create_binding_auth_error_persists_expired(db):
    _Stub115Client.behavior = "auth_error"
    b = ub.create_binding(db, 7, "pan115", "过期号", "COOKIE-OLD")
    assert b.status == "expired" and b.last_error_kind == "expired"
    cred = db.query(em.MountCredential).filter(
        em.MountCredential.id == b.credential_id).first()
    assert cred.status == "expired"
    assert ub.serialize_binding(b)["error_message"] == "115 登录已过期，请重新绑定。"


def test_create_binding_throttled_persists_invalid(db):
    _Stub115Client.behavior = "throttled"
    b = ub.create_binding(db, 7, "pan115", "被限号", "COOKIE-T")
    assert b.status == "invalid" and b.last_error_kind == "throttled"
    assert "稍后再试" in ub.serialize_binding(b)["error_message"]


def test_create_binding_transient_rolls_back(db):
    _Stub115Client.behavior = "transient"
    with pytest.raises(ub.BindingError) as excinfo:
        ub.create_binding(db, 7, "pan115", "抖动号", "COOKIE-J")
    assert excinfo.value.kind == "transient"
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.MountCredential).count() == 0


def test_create_binding_limit_from_config(db, monkeypatch):
    monkeypatch.setenv("BYOS_BINDING_LIMIT_PER_USER", "1")
    _bind(db, 7, "一号")
    with pytest.raises(ub.BindingError):
        ub.create_binding(db, 7, "pan115", "二号", "COOKIE-2")
    assert ub.binding_limit_per_user() == 1


def test_create_binding_drive_rejected_in_p1(db):
    # P2 已上线：Drive 仍不收明文凭据，拒绝文案改为指引 OAuth 口
    # （行为不变：kind=unsupported、不落行）。
    with pytest.raises(ub.BindingError) as excinfo:
        ub.create_binding(db, 7, "drive", "盘", "whatever")
    assert excinfo.value.kind == "unsupported"
    assert "oauth/start" in excinfo.value.message
    assert db.query(em.UserStorageBinding).count() == 0


# ---------- 解绑与重验 ----------

def test_delete_binding_removes_both_rows(db):
    b = _bind(db, 7, "主号")
    cid = b.credential_id
    ub.delete_binding(db, 7, b.id)
    assert db.query(em.UserStorageBinding).count() == 0
    assert db.query(em.MountCredential).filter(
        em.MountCredential.id == cid).count() == 0


def test_delete_foreign_binding_not_found(db):
    b = _bind(db, 7, "主号")
    with pytest.raises(ub.BindingError) as excinfo:
        ub.delete_binding(db, 8, b.id)
    assert excinfo.value.kind == "not_found"
    assert db.query(em.UserStorageBinding).count() == 1


def test_verify_binding_recovers_expired(db):
    _Stub115Client.behavior = "auth_error"
    b = ub.create_binding(db, 7, "pan115", "过期号", "COOKIE-OLD")
    assert b.status == "expired"
    _Stub115Client.behavior = "ok"
    b2 = ub.verify_binding(db, 7, b.id)
    assert b2.status == "active" and b2.last_error_kind is None


def test_get_active_binding_picks_earliest(db):
    _bind(db, 7, "先绑")
    _bind(db, 7, "后绑")
    got = ub.get_active_binding(db, 7, "pan115")
    assert got.account_label == "先绑"
    assert ub.get_active_binding(db, 8, "pan115") is None


# ---------- 115 直链解析 ----------

def test_resolve_user115_direct_url_fresh_each_time(db, monkeypatch):
    m = _mount115(db)
    it = _item(db, m)
    _bind(db, 7, "主号")

    def _fake_locate(self, rel):
        return {"pickcode": "PC1", "name": "f.mkv"}

    monkeypatch.setattr(mount_lib.Pan115Mount, "_locate", _fake_locate)
    url1 = ub.resolve_user115_direct_url(db, 7, it)
    url2 = ub.resolve_user115_direct_url(db, 7, it)
    assert url1 == url2 == "https://down.115.com/PC1"
    # 现签现取：两次都真调 download_url，不走共享 pickcode 缓存
    assert _Stub115Client.calls["download_url"] == 2


def test_resolve_user115_direct_url_no_binding(db):
    m = _mount115(db)
    it = _item(db, m)
    with pytest.raises(ub.BindingError) as excinfo:
        ub.resolve_user115_direct_url(db, 7, it)
    assert excinfo.value.kind == "not_found"


def test_resolve_user115_direct_url_non115_mount(db):
    m = em.StorageMount(name="local", mount_type="local", path="/media")
    db.add(m)
    db.commit()
    it = _item(db, m)
    _bind(db, 7, "主号")
    with pytest.raises(ub.BindingError):
        ub.resolve_user115_direct_url(db, 7, it)


def test_resolve_user115_direct_url_auth_marks_expired(db, monkeypatch):
    m = _mount115(db)
    it = _item(db, m)
    b = _bind(db, 7, "主号")

    def _fake_locate(self, rel):
        return {"pickcode": "PC1", "name": "f.mkv"}

    monkeypatch.setattr(mount_lib.Pan115Mount, "_locate", _fake_locate)
    _Stub115Client.behavior = "auth_error"
    with pytest.raises(ub.BindingError) as excinfo:
        ub.resolve_user115_direct_url(db, 7, it)
    assert excinfo.value.kind == "expired"
    db.refresh(b)
    assert b.status == "expired"


def test_byos_provider_instance_bypasses_shared_list_cache(db):
    # 绑定凭据实例不读写共享目录缓存（键不含凭据，复用即串用户）
    m = _mount115(db)
    p = mount_lib.Pan115Mount(m, db=db, cookie_override="COOKIE-U")
    assert p._byos_no_cache is True
    assert p._cookie() == "COOKIE-U"
    p2 = mount_lib.Pan115Mount(m, db=db)
    assert p2._byos_no_cache is False


# ---------- 变体清单 ----------

def test_list_variants_byos_appended_after_mainline(db):
    m = _mount115(db)
    it = _item(db, m)
    names = [v.name for v in pv.list_variants(
        db, it, user115_direct_available=True)]
    assert names == [pv.MOUNT_RELAY, pv.BYOS115_DIRECT]
    names_off = [v.name for v in pv.list_variants(db, it)]
    assert names_off == [pv.MOUNT_RELAY]
    # 非挂载条目：信号不得外溢
    local = em.MediaItem(guid="g2", library_id=it.library_id,
                         item_type="movie", name="本", file_path="/media/a.mkv")
    assert [v.name for v in pv.list_variants(
        db, local, user115_direct_available=True)] == [pv.LOCAL_FILE]


def test_resolve_requested_variant_byos(db):
    assert pv.resolve_requested_variant(
        {"variant": "byos115_direct"}, ["mount_relay", "byos115_direct"]) == (
        "byos115_direct", None)
    assert pv.resolve_requested_variant(
        {"variant": "byos115_direct"}, ["mount_relay"]) == (None, 409)
    assert pv.resolve_requested_variant(
        {"variant": "nope"}, ["mount_relay"]) == (None, 404)

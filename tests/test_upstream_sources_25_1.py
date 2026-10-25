"""上游源管理（FMBY 学习 #25 25-1：只做源管理）。

钉死的纪律：
① 枚举与约束同源：source_type / auth_method / credential_scope /
   health_status 的非法值在 DB 层就被 CHECK 拒掉（FMBY reason_code
   漂移 bug 不许重演）；
② 凭据密封：库里只存 Fernet 密封值，接口与日志只出 has_secret；
③ 默认关：新建恒关闭，启用是单独动作；关着的源对全站无影响
   （本文件用「播放/解析代码不 import 本模块」静态钉死无接线）；
④ 证据分界：苹果 CMS 型直接被拒；凭据 scope 默认源级一把，
   user 取值由 2026-10-24 用户亲令照抄显式重开（挂载凭据表用），
   "per_user" 仍不是允许值。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import admin_upstream_sources as admin_ups
from backend.emby_server import models as em
from backend.emby_server import upstream_sources as ups

PUBLIC_URL = "https://upstream.example.com:8096/emby/"


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


@pytest.fixture()
def staff():
    return SimpleNamespace(id=7, is_staff=True, username="admin")


def _create(db, **kw):
    params = {"name": "友服 A", "source_type": "emby", "base_url": PUBLIC_URL,
              "auth_method": "password", "username": "admin",
              "secret": "s3cret-pw"}
    params.update(kw)
    return ups.create_source(db, **params)


# ---------- 枚举与约束同源 ----------

def test_illegal_source_type_rejected_by_db(db):
    row = em.UpstreamSource(name="坏类型", source_type="apple_cms",
                            base_url="https://x.example.com",
                            auth_method="password", username="a",
                            credential_scope="source",
                            health_status="unknown")
    db.add(row)
    with pytest.raises(IntegrityError):
        db.flush()


def test_illegal_auth_and_scope_and_health_rejected_by_db(db):
    for field, value in (("auth_method", "oauth"),
                         ("credential_scope", "per_user"),
                         ("health_status", "degraded")):
        row = em.UpstreamSource(name=f"坏{field}", source_type="emby",
                                base_url="https://x.example.com",
                                auth_method="password", username="a",
                                credential_scope="source",
                                health_status="unknown")
        setattr(row, field, value)
        db.add(row)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


def test_apple_cms_rejected_by_module(db):
    with pytest.raises(ups.UpstreamSourceError):
        _create(db, name="CMS 源", source_type="apple_cms")


def test_credential_scope_enum_source_and_user():
    # 2026-10-24 用户亲令照抄重开 user 取值（挂载凭据表用）；
    # 默认仍为 source（见 create_source 默认值），per_user 仍被拒
    # （见 test_illegal_auth_and_scope_and_health_rejected_by_db）。
    assert ups.CREDENTIAL_SCOPES == ("source", "user")
    assert em.UPSTREAM_CREDENTIAL_SCOPES == ("source", "user")


# ---------- 凭据密封 ----------

def test_secret_stored_sealed_and_payload_hides_it(db):
    row = _create(db)
    assert row.sealed_secret is not None
    assert row.sealed_secret != "s3cret-pw"
    assert row.sealed_secret.startswith("fernet-v1:")
    assert ups.unseal_secret(row.sealed_secret) == "s3cret-pw"
    payload = ups.source_payload(row)
    assert payload["has_secret"] is True
    assert "sealed_secret" not in payload
    assert "secret" not in payload
    assert "s3cret-pw" not in str(payload)


def test_no_secret_means_anonymous_and_load_returns_none(db):
    row = _create(db, name="匿名源", secret=None)
    assert row.sealed_secret is None
    assert ups.source_payload(row)["has_secret"] is False
    assert ups.load_credential(row) is None


def test_load_credential_returns_plain_for_workers(db):
    row = _create(db)
    cred = ups.load_credential(row)
    assert cred == {"auth_method": "password", "username": "admin",
                    "secret": "s3cret-pw"}


def test_unseal_with_wrong_key_raises(monkeypatch, db):
    row = _create(db)
    import backend.security as security

    monkeypatch.setattr(security, "SECRET_KEY", "another-key-123456")
    with pytest.raises(ups.SealedSecretError):
        ups.unseal_secret(row.sealed_secret)


def test_api_key_form_needs_no_username_and_password_needs_one(db):
    row = _create(db, name="密钥源", auth_method="api_key", username=None,
                  secret="tok-123")
    assert row.username is None
    assert ups.unseal_secret(row.sealed_secret) == "tok-123"
    with pytest.raises(ups.UpstreamSourceError):
        _create(db, name="无用户名", auth_method="password", username=None)
    with pytest.raises(ups.UpstreamSourceError):
        _create(db, name="密钥带名", auth_method="api_key", username="someone")


# ---------- 地址校验与归一化 ----------

def test_base_url_normalized_trailing_slash_stripped(db):
    row = _create(db, base_url="https://UPSTREAM.Example.COM:8096/emby/")
    assert row.base_url == "https://upstream.example.com:8096/emby"


@pytest.mark.parametrize("bad_url", [
    "ftp://upstream.example.com",
    "https://",
    "http://127.0.0.1:8096",
    "http://10.0.0.5:8096",
    "http://192.168.1.10",
    "http://localhost:8096",
    "https://localhost",
    "",
])
def test_bad_base_urls_rejected(db, bad_url):
    with pytest.raises(ups.UpstreamSourceError):
        _create(db, name=f"坏地址{bad_url}", base_url=bad_url)


def test_public_ip_url_accepted(db):
    # 203.0.113.0/24 是文档保留段（is_private），不是真公网地址——
    # 用真公网 IP 作例（本测试不发任何网络请求，只做格式校验）。
    row = _create(db, name="公网 IP 源", base_url="http://1.1.1.1:8096")
    assert row.base_url == "http://1.1.1.1:8096"


# ---------- 默认关与启用/停用 ----------

def test_created_source_is_disabled_and_enable_disable_work(db):
    row = _create(db)
    assert row.enabled is False
    ups.enable_source(db, row)
    assert row.enabled is True
    assert [s.id for s in ups.enabled_sources(db)] == [row.id]
    ups.disable_source(db, row)
    assert row.enabled is False
    assert ups.enabled_sources(db) == []


def test_health_status_placeholder_only_for_probe_worker(db):
    row = _create(db)
    assert row.health_status == "unknown"
    assert row.last_checked_at is None
    ups.set_health_status(db, row, "healthy")
    assert row.health_status == "healthy"
    assert row.last_checked_at is not None
    with pytest.raises(ups.UpstreamSourceError):
        ups.set_health_status(db, row, "degraded")


# ---------- 名称唯一与编辑三态 ----------

def test_duplicate_name_rejected(db):
    _create(db)
    with pytest.raises(ups.DuplicateSourceName):
        _create(db, base_url="https://other.example.com")


def test_update_secret_keep_replace_clear(db):
    row = _create(db)
    ups.update_source(db, row, note="改备注", note_set=True)
    assert ups.unseal_secret(row.sealed_secret) == "s3cret-pw"
    assert row.note == "改备注"
    ups.update_source(db, row, secret="new-pw", secret_set=True)
    assert ups.unseal_secret(row.sealed_secret) == "new-pw"
    ups.update_source(db, row, secret="", secret_set=True)
    assert row.sealed_secret is None


def test_update_switch_to_api_key_clears_username(db):
    row = _create(db)
    ups.update_source(db, row, auth_method="api_key")
    assert row.auth_method == "api_key"
    assert row.username is None


def test_extra_headers_must_be_string_object(db):
    with pytest.raises(ups.UpstreamSourceError):
        _create(db, name="坏头", extra_headers_json='["a"]')
    with pytest.raises(ups.UpstreamSourceError):
        _create(db, name="坏头值", extra_headers_json='{"X-A": 1}')
    row = _create(db, name="好头", extra_headers_json='{"X-A": "b"}')
    assert ups.source_payload(row)["extra_headers"] == {"X-A": "b"}


# ---------- 管理端 CRUD（直接调函数，与审查队列测试同口径） ----------

def test_admin_crud_end_to_end(db, staff):
    created = admin_ups.create_upstream_source(
        admin_ups.UpstreamSourceCreateRequest(
            name="管理端源", source_type="jellyfin",
            base_url="https://jf.example.com", auth_method="api_key",
            secret="jf-token"), staff=staff, db=db)
    assert created["enabled"] is False
    assert created["has_secret"] is True
    source_id = created["id"]

    listed = admin_ups.list_upstream_sources(staff=staff, db=db)
    assert [item["id"] for item in listed["items"]] == [source_id]

    enabled = admin_ups.enable_upstream_source(source_id, staff=staff, db=db)
    assert enabled["enabled"] is True

    updated = admin_ups.update_upstream_source(
        source_id,
        admin_ups.UpstreamSourceUpdateRequest(note="备注"),
        staff=staff, db=db)
    assert updated["note"] == "备注"
    assert updated["has_secret"] is True  # 没传 secret = 凭据不动

    disabled = admin_ups.disable_upstream_source(source_id, staff=staff, db=db)
    assert disabled["enabled"] is False

    result = admin_ups.delete_upstream_source(source_id, staff=staff, db=db)
    assert result == {"success": True}
    assert admin_ups.list_upstream_sources(staff=staff, db=db)["items"] == []


def test_admin_create_duplicate_name_is_409(db, staff):
    from fastapi import HTTPException

    req = admin_ups.UpstreamSourceCreateRequest(
        name="重名源", source_type="emby", base_url=PUBLIC_URL,
        auth_method="password", username="a", secret="pw")
    admin_ups.create_upstream_source(req, staff=staff, db=db)
    with pytest.raises(HTTPException) as excinfo:
        admin_ups.create_upstream_source(req, staff=staff, db=db)
    assert excinfo.value.status_code == 409


def test_admin_get_missing_is_404(db, staff):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        admin_ups.get_upstream_source(999999, staff=staff, db=db)
    assert excinfo.value.status_code == 404


# ---------- 默认关的接线钉死：播放/解析代码不许 import 本模块 ----------

def test_no_playback_wiring_yet():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "backend" / "emby_server"
    # 只列本基线（83c6fd90）真实存在的文件：列不存在的文件会让测试
    # 在别的基线下静默放水——文件必须先存在断言，再查无接线。
    for filename in ("api.py", "streaming.py", "play_variants.py",
                     "scanner.py"):
        path = root / filename
        assert path.is_file(), filename
        assert "upstream_sources" not in path.read_text(encoding="utf-8"), filename

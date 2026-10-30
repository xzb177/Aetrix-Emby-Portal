"""resolve_cookie 的 is_enabled 口径（360° 审计 A-017）。

只用假凭据（fake- 前缀），不读生产。
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import pan115_mirror as mirror_mod
from backend.emby_server import transfer115 as t115


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


def _account(db, name, value, *, is_default=False, is_enabled=True):
    account = em.Pan115Account(name=name, cookie=value,
                               is_default=is_default, is_enabled=is_enabled)
    db.add(account)
    db.commit()
    return account


def _lib(account_id):
    return SimpleNamespace(id=999, account_115_id=account_id)


@pytest.fixture(autouse=True)
def _no_env_cookie(monkeypatch):
    monkeypatch.delenv("PAN115_COOKIE", raising=False)


def test_explicit_disabled_account_raises(db):
    account = _account(db, "disabled-one", "fake-disabled", is_enabled=False)
    with pytest.raises(t115.Pan115Error):
        t115.resolve_cookie(db, account_id=account.id)


def test_bound_disabled_account_raises_not_fallback(db):
    disabled = _account(db, "bound-disabled", "fake-bound-disabled", is_enabled=False)
    _account(db, "default-one", "fake-default", is_default=True, is_enabled=True)
    with pytest.raises(t115.Pan115Error):
        t115.resolve_cookie(db, library=_lib(disabled.id))


def test_bound_missing_account_falls_back_to_default(db):
    _account(db, "default-one", "fake-default", is_default=True, is_enabled=True)
    cookie, source = t115.resolve_cookie(db, library=_lib(123456))
    assert cookie == "fake-default"
    assert "默认账号" in source


def test_bound_enabled_account_used(db):
    bound = _account(db, "bound-one", "fake-bound", is_enabled=True)
    _account(db, "default-one", "fake-default", is_default=True, is_enabled=True)
    cookie, source = t115.resolve_cookie(db, library=_lib(bound.id))
    assert cookie == "fake-bound"
    assert "媒体库绑定账号" in source


def test_explicit_enabled_account_used(db):
    account = _account(db, "explicit-one", "fake-explicit", is_enabled=True)
    cookie, source = t115.resolve_cookie(db, account_id=account.id)
    assert cookie == "fake-explicit"
    assert "账号配置档" in source


def test_discover_scopes_only_enabled(db):
    _account(db, "on-one", "fake-on", is_enabled=True)
    _account(db, "off-one", "fake-off", is_enabled=False)
    scopes = mirror_mod._discover_scopes(db)
    assert [s["scope"] for s in scopes] == ["account:1"]

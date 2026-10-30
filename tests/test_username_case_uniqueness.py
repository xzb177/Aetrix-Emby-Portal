# -*- coding: utf-8 -*-
"""A-002 回归：用户名大小写唯一性与不敏感查找的唯一性。

旧行为：注册/接线名/AuthenticateByName 全是大小写敏感，Foo 与 foo 可并存；
转账与指名码却按大小写不敏感匹配并 .first() 任选一个，能量/指名码可能
落到异壳账号。修复口径：
- 新建账号（注册、upsert_admin）与设置接线名时，大小写不敏感撞名一律拒绝；
- 转账收款人、指名码 target 按不敏感匹配命中多个时明确拒绝，恰好一个时不变。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-a002-case-key-32chars-minimum")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import codes, models
from backend.admin_accounts import AdminAccountError, upsert_admin
from backend.api import admin_ops as ops
from backend.api import economy, emby_portal
from backend.emby_server.portal import SetPasswordRequest, set_emby_password


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(name="公益服", slug="main", is_active=True))
    session.add(models.SystemConfig(key="register_ratelimit_enabled", value="false"))
    session.add(models.SystemConfig(key="registration_mode", value="open"))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def _make_user(db, username, **kw):
    kw.setdefault("password_hash", "x")
    kw.setdefault("is_active", True)
    u = models.WebUser(username=username, **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _scope():
    return {"type": "http", "method": "POST", "path": "/",
            "headers": [], "client": ("1.2.3.4", 1234)}


def _register(db, username):
    return emby_portal.register(
        Request(_scope()),
        emby_portal.RegisterRequest(username=username, password="secret123"),
        db,
    )


# ==================== 注册：大小写撞名拒绝 ====================

def test_register_rejects_case_variants_of_existing_portal_name(db):
    _make_user(db, "Foo")
    for name in ("foo", "Foo", "FOO", "fOo"):
        with pytest.raises(HTTPException) as exc:
            _register(db, name)
        assert exc.value.status_code == 409
        assert exc.value.detail == "用户名已被注册"
    assert db.query(models.WebUser).count() == 1


def test_register_rejects_case_variant_of_existing_emby_name(db):
    owner = _make_user(db, "owner")
    owner.emby_username = "PlayName"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        _register(db, "playname")
    assert exc.value.status_code == 409
    assert exc.value.detail == "用户名已被占用"


def test_register_normal_new_name_still_works(db):
    _make_user(db, "Foo")
    out = _register(db, "Bar_9")
    assert out.user.username == "Bar_9"
    assert db.query(models.WebUser).filter_by(username="Bar_9").count() == 1


# ==================== 接线名：大小写撞名拒绝 ====================

def test_set_emby_username_rejects_case_variants(db):
    _make_user(db, "Foo")
    other_emby = _make_user(db, "someone")
    other_emby.emby_username = "PlayName"
    db.commit()
    user = _make_user(db, "newbie")
    for name in ("foo", "FOO", "playname", "PLAYNAME"):
        with pytest.raises(HTTPException) as exc:
            set_emby_password(SetPasswordRequest(username=name, password="secret123"), user, db)
        assert exc.value.status_code == 409
        assert exc.value.detail == "用户名已被占用"
    db.refresh(user)
    assert user.emby_username is None


def test_set_emby_username_unique_name_still_works(db):
    _make_user(db, "Foo")
    user = _make_user(db, "newbie")
    body = set_emby_password(
        SetPasswordRequest(username="newbie_play", password="secret123"), user, db)
    assert body["emby_username"] == "newbie_play"


def test_set_emby_username_may_match_own_portal_name_case_variant(db):
    # 同一账号自己的门户名大小写变体不是跨账号冲突，不应误伤。
    user = _make_user(db, "Carol")
    body = set_emby_password(
        SetPasswordRequest(username="carol", password="secret123"), user, db)
    assert body["emby_username"] == "carol"


# ==================== upsert_admin：新建路径同样拦截 ====================

def test_upsert_admin_rejects_case_variant_creation(db):
    _make_user(db, "Foo")
    with pytest.raises(AdminAccountError):
        upsert_admin(db, "foo", password="secret123")
    db.rollback()
    assert db.query(models.WebUser).count() == 1


def test_upsert_admin_exact_existing_user_still_upgrades(db):
    user = _make_user(db, "Foo")
    out, created, _applied = upsert_admin(db, "Foo")
    assert created is False
    assert out.id == user.id
    db.refresh(user)
    assert user.is_staff is True


# ==================== 转账：不敏感命中多个必须拒绝 ====================

@pytest.fixture()
def transfer_db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _transfer_user(db, username, points=0, is_active=True):
    u = models.WebUser(username=username, password_hash="", points=points,
                       is_active=is_active)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_transfer_ambiguous_case_match_rejected(transfer_db):
    sender = _transfer_user(transfer_db, "sender", points=1000)
    _transfer_user(transfer_db, "Foo", points=0)
    _transfer_user(transfer_db, "foo", points=0)
    with pytest.raises(ValueError, match="不唯一"):
        economy.transfer_points_core(transfer_db, sender, "FOO", 100)
    transfer_db.refresh(sender)
    assert sender.points == 1000
    assert transfer_db.query(models.PointsLog).count() == 0


def test_transfer_unique_case_match_still_works(transfer_db):
    sender = _transfer_user(transfer_db, "sender", points=1000)
    only = _transfer_user(transfer_db, "OnlyOne", points=0)
    res = economy.transfer_points_core(transfer_db, sender, "onlyone", 100)
    assert res["recipient"] == "OnlyOne"
    transfer_db.refresh(only)
    assert only.points == 100


# ==================== 指名码：生成与核销同口径 ====================

def _gen_request(**overrides):
    payload = {"count": 1, "code_type": 2, "reward_type": "welfare", "days": 30}
    payload.update(overrides)
    return ops.CodeGenerateRequest(**payload)


def test_generate_named_code_ambiguous_target_rejected(db):
    admin = _make_user(db, "admin_a002", is_staff=True)
    _make_user(db, "Foo")
    _make_user(db, "foo")
    with pytest.raises(HTTPException) as exc:
        ops.generate_codes(_gen_request(target_username="FOO"), admin, db)
    assert exc.value.status_code == 400
    assert "不唯一" in exc.value.detail
    assert db.query(models.RegistrationCode).count() == 0


def test_generate_named_code_unique_target_still_works(db):
    admin = _make_user(db, "admin_a002", is_staff=True)
    _make_user(db, "OnlyOne")
    res = ops.generate_codes(_gen_request(target_username="onlyone"), admin, db)
    assert res["success"] is True
    code = db.query(models.RegistrationCode).one()
    assert code.target_username == "onlyone"


def _make_welfare_code(db, code_str, **kw):
    kw.setdefault("code_type", codes.CODE_TYPE_RENEW)
    kw.setdefault("max_uses", 1)
    kw.setdefault("use_count", 0)
    kw.setdefault("is_active", True)
    kw.setdefault("days", 30)
    kw.setdefault("reward_type", "welfare")
    c = models.RegistrationCode(code=code_str, **kw)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def test_redeem_named_code_ambiguous_target_rejected_for_both(db):
    first = _make_user(db, "Target")
    second = _make_user(db, "target")
    code = _make_welfare_code(db, "WEL-A002-AMB", target_username="TARGET")
    for user in (first, second):
        result = codes.redeem_code(db, user, "WEL-A002-AMB")
        db.rollback()
        assert result["success"] is False
        assert "不唯一" in result["message"]
        db.refresh(user)
        assert not user.is_welfare
    db.refresh(code)
    assert (code.use_count or 0) == 0


def test_redeem_named_code_unique_target_still_works(db):
    user = _make_user(db, "Solo")
    _make_welfare_code(db, "WEL-A002-ONE", target_username="solo")
    result = codes.redeem_code(db, user, "WEL-A002-ONE")
    db.commit()
    assert result["success"] is True
    db.refresh(user)
    assert user.is_welfare is True

# -*- coding: utf-8 -*-
"""软件授权客户端校验（发行版专用，2026-11-07 授权售卖一期第 3 步）。

定版口径（方案 aetrix-license-plan-20261107 第四节之 3）：
- 总开关 env AETRIX_LICENSE_ENFORCE，默认关：关时零请求、零行为变化
  （自家生产走源码部署，永不受影响）；
- 开时启动后异步校验 + 每 24 小时复验，签名令牌本地缓存，本地验签；
- 授权服务连不上用缓存：令牌到期后 7 天宽限内一切照常；
- 无效（码错 / 被停用 / 过期且过宽限）只拦「开始播放」（执法点与
  绑定门禁同层，见 playback_gate），浏览 / 登录 / 后台 / 填新码不受
  影响，填入有效码校验通过立刻恢复；
- 校验请求只传授权码、指纹哈希、域名、版本号四样，绝不传用户与
  片库数据（白名单断言守住）。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import license_client as lc
from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import playback_gate as gate
from backend.integrations import store
from backend.version import app_version

CODE = "AX-TEST-CODE-0001"
DOMAIN = "buyer.example.com"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _license_env(monkeypatch):
    """每测独立：清状态缓存，默认关 enforce（用例按需打开）。

    store 的热缓存是进程级、只按键名索引（生产里一进程一库所以
    一致）；测试每例换新内存库，必须连它一起清，否则上一例的
    令牌会串进下一例。
    """
    lc.invalidate_state_cache()
    store.invalidate()
    monkeypatch.delenv("AETRIX_LICENSE_ENFORCE", raising=False)
    monkeypatch.delenv("AETRIX_LICENSE_CODE", raising=False)
    monkeypatch.delenv("AETRIX_LICENSE_DOMAIN", raising=False)
    monkeypatch.delenv("AETRIX_LICENSE_SERVER", raising=False)
    yield
    lc.invalidate_state_cache()
    store.invalidate()


@pytest.fixture()
def server_key(monkeypatch):
    """模拟授权服务端的签名密钥对；公钥经 env 注入客户端。"""
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setenv(
        "AETRIX_LICENSE_PUBKEY",
        base64.b64encode(pub_raw).decode("ascii"))
    return priv


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sign(priv, payload) -> str:
    sig = priv.sign(lc.canonical_bytes(payload))
    return base64.b64encode(sig).decode("ascii")


def _payload(code=CODE, domain=DOMAIN, fp_hex="f" * 64, *,
             issued=NOW, expires_days=365, grace_days=7, tier="pro"):
    return {
        "code": code,
        "tier": tier,
        "domain": domain,
        "fp_sha256": fp_hex,
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + timedelta(days=expires_days)),
        "grace_days": grace_days,
    }


def _seed_config(db, code=CODE, domain=DOMAIN):
    store.write_values(db, {lc.KEY_CODE: code, lc.KEY_DOMAIN: domain})
    db.commit()


def _seed_token(db, payload, signature):
    store.write_values(db, {
        lc.KEY_TOKEN: json.dumps(
            {"license": payload, "signature": signature}),
    })
    db.commit()


def _seed_fingerprint(db, fp="a" * 64):
    store.write_values(db, {lc.KEY_FINGERPRINT: fp})
    db.commit()
    return fp


def _server_poster(priv, expires_days=365):
    """模拟授权服务端：按请求体签发令牌（activate / verify 同形）。"""
    calls = []

    def poster(url, body, timeout):
        calls.append({"url": url, "body": dict(body)})
        fp_sha = hashlib.sha256(
            body["machine_fp"].encode("utf-8")).hexdigest()
        payload = _payload(
            code=body["code"].strip().upper(), domain=body["domain"],
            fp_hex=fp_sha, expires_days=expires_days)
        return 200, {"ok": True, "license": payload,
                     "signature": _sign(priv, payload)}

    poster.calls = calls
    return poster


def _fail_poster(reason, status=403):
    def poster(url, body, timeout):
        return status, {"ok": False, "reason": reason, "message": reason}
    return poster


def _down_poster(url, body, timeout):
    raise ConnectionError("授权服务连不上（模拟）")


# ---------------------------------------------------------------- 开关关


def test_enforce_off_zero_requests_and_no_block(db, monkeypatch):
    """enforce 关（默认）：不发任何请求、播放不拦、后台循环不起。"""
    _seed_config(db)

    def forbidden_poster(url, body, timeout):  # pragma: no cover
        raise AssertionError("enforce 关时不许发请求")

    result = lc.refresh(db, poster=forbidden_poster, now=NOW)
    assert result["state"] == "disabled"
    lc.ensure_playback_allowed(db)  # 不抛 = 放行
    assert lc.start() is False


def test_default_pubkey_matches_live_server():
    """内嵌默认公钥必须与授权服务线上公钥一致（2026-11-07 实测
    https://license.135505.autos/api/v1/pubkey）。"""
    assert lc.DEFAULT_PUBKEY_B64 == \
        "3dn939gNHaoWyqWNcaA8/ZY2jWJTrqjCspIHLthKt+o="


# ---------------------------------------------------------------- 激活/校验


def test_activate_then_cached_token_allows_offline(db, monkeypatch,
                                                   server_key):
    """首次 activate 拿令牌后，断网时凭缓存令牌本地验签放行。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    poster = _server_poster(server_key)

    state = lc.refresh(db, poster=poster, now=NOW)
    assert state["state"] == "ok"
    assert state["tier"] == "pro"
    assert len(poster.calls) == 1
    assert poster.calls[0]["url"].endswith("/api/v1/activate")

    # 第二次走 verify；之后断网，本地缓存仍放行且不再依赖网络
    state = lc.refresh(db, poster=poster, now=NOW)
    assert poster.calls[1]["url"].endswith("/api/v1/verify")
    offline = lc.evaluate(db, now=NOW + timedelta(hours=3))
    assert offline["state"] == "ok" and offline["allowed"] is True
    lc.ensure_playback_allowed(db)


def test_request_body_whitelist(db, monkeypatch, server_key):
    """隐私：发往授权服务的请求体只许四类字段，指纹只发哈希。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db, domain="https://Buyer.Example.COM:8443/x")
    poster = _server_poster(server_key)
    lc.refresh(db, poster=poster, now=NOW)
    body = poster.calls[0]["body"]
    assert set(body.keys()) == {"code", "domain", "machine_fp", "version"}
    assert body["code"] == CODE
    assert body["domain"] == DOMAIN  # 已归一化
    assert body["version"] == app_version()
    fp = store.read_value(db, lc.KEY_FINGERPRINT)
    assert body["machine_fp"] == fp
    assert len(fp) == 64
    int(fp, 16)  # 纯哈希（十六进制），不是机器信息原文


def test_fingerprint_generated_once_and_reused(db):
    """指纹首次生成后落库，之后（含换会话）一直复用同一个值。"""
    fp1 = lc.machine_fingerprint(db)
    fp2 = lc.machine_fingerprint(db)
    assert fp1 == fp2 and len(fp1) == 64
    int(fp1, 16)
    rows = (db.query(base_models.SystemConfig)
            .filter(base_models.SystemConfig.key == lc.KEY_FINGERPRINT)
            .all())
    assert len(rows) == 1 and rows[0].value == fp1


# ---------------------------------------------------------------- 宽限/失效


def _seed_valid_token(db, server_key, *, issued, expires_days):
    fp = _seed_fingerprint(db)
    fp_sha = hashlib.sha256(fp.encode("utf-8")).hexdigest()
    payload = _payload(fp_hex=fp_sha, issued=issued,
                       expires_days=expires_days)
    _seed_token(db, payload, _sign(server_key, payload))
    return payload


def test_grace_period_after_expiry_still_allowed(db, monkeypatch,
                                                 server_key):
    """令牌过期 2 天（宽限 7 天）：断网也照常放行，状态标记宽限中。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    _seed_valid_token(db, server_key, issued=NOW - timedelta(days=30),
                      expires_days=28)  # 已过期 2 天
    state = lc.evaluate(db, now=NOW)
    assert state["state"] == "grace"
    assert state["allowed"] is True and state["in_grace"] is True
    lc.ensure_playback_allowed(db)


def test_past_grace_blocks_playback_only(db, monkeypatch, server_key):
    """过期且过宽限：只拦开始播放（绑定门禁同层）；状态查询与
    重新校验通道不受影响（恢复路径见 test_new_code_recovers）。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    _seed_valid_token(db, server_key, issued=NOW - timedelta(days=40),
                      expires_days=30)  # 过期 10 天 > 宽限 7 天
    state = lc.evaluate(db, now=NOW)
    assert state["state"] == "blocked" and state["reason"] == "expired"
    with pytest.raises(HTTPException) as exc:
        lc.ensure_playback_allowed(db)
    assert exc.value.status_code == 403
    # 管理面状态照常可读（不被播放拦截连坐）
    status = lc.admin_status(db)
    assert status["state"] == "blocked" and status["tier"] == "pro"


def test_server_disabled_overrides_unexpired_token(db, monkeypatch,
                                                   server_key):
    """服务端明确停用：即便缓存令牌没到期，复验得知后立即拦截。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    _seed_valid_token(db, server_key, issued=NOW - timedelta(hours=1),
                      expires_days=365)
    assert lc.evaluate(db, now=NOW)["state"] == "ok"
    state = lc.refresh(db, poster=_fail_poster("disabled"), now=NOW)
    assert state["state"] == "blocked" and state["reason"] == "disabled"
    with pytest.raises(HTTPException):
        lc.ensure_playback_allowed(db)


def test_tampered_token_rejected(db, monkeypatch, server_key):
    """令牌被篡改（签名对不上）：本地验签不过 → 拦截。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    payload = _seed_valid_token(db, server_key, issued=NOW,
                                expires_days=365)
    forged = dict(payload, tier="flagship")  # 改载荷不改签名
    _seed_token(db, forged, _sign(server_key, payload))
    state = lc.evaluate(db, now=NOW)
    assert state["state"] == "blocked"
    assert state["reason"] == "invalid_token"
    with pytest.raises(HTTPException):
        lc.ensure_playback_allowed(db)


def test_binding_mismatch_token_rejected(db, monkeypatch, server_key):
    """令牌绑的是别人的域名：与本机配置对不上 → 拦截。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    fp = _seed_fingerprint(db)
    fp_sha = hashlib.sha256(fp.encode("utf-8")).hexdigest()
    payload = _payload(domain="other.example.com", fp_hex=fp_sha,
                       issued=NOW)
    _seed_token(db, payload, _sign(server_key, payload))
    state = lc.evaluate(db, now=NOW)
    assert state["state"] == "blocked"
    assert state["reason"] == "binding_mismatch"


def test_no_code_blocks_with_guidance(db, monkeypatch):
    """enforce 开但没填授权码：拦截且文案指路管理后台。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    state = lc.evaluate(db, now=NOW)
    assert state["state"] == "blocked" and state["reason"] == "no_code"
    with pytest.raises(HTTPException) as exc:
        lc.ensure_playback_allowed(db)
    assert "授权" in exc.value.detail


def test_unreachable_first_time_no_crash(db, monkeypatch):
    """从未成功校验且授权服务连不上：不崩，状态如实报未激活。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    state = lc.refresh(db, poster=_down_poster, now=NOW)
    assert state["state"] == "blocked"
    assert state["reason"] == "not_activated"
    assert lc.admin_status(db)["last_result"] == "unreachable"


def test_new_code_recovers(db, monkeypatch, server_key):
    """填错码被拦 → 换上有效码并校验 → 立刻恢复放行。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db, code="AX-WRONG-CODE-9999")
    state = lc.refresh(db, poster=_fail_poster("not_found", 404), now=NOW)
    assert state["state"] == "blocked"
    with pytest.raises(HTTPException):
        lc.ensure_playback_allowed(db)

    _seed_config(db, code=CODE)  # 管理员在后台改对码
    state = lc.refresh(db, poster=_server_poster(server_key), now=NOW)
    assert state["state"] == "ok"
    lc.ensure_playback_allowed(db)  # 恢复，不抛


def test_expiry_reminder_levels(db, monkeypatch, server_key):
    """到期前 30/7/1 天分级提醒（管理端授权区展示）。"""
    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    _seed_config(db)
    _seed_valid_token(db, server_key, issued=NOW, expires_days=20)
    assert lc.evaluate(db, now=NOW)["reminder"] == "30"
    _seed_valid_token(db, server_key, issued=NOW, expires_days=5)
    lc.invalidate_state_cache()
    assert lc.evaluate(db, now=NOW)["reminder"] == "7"
    _seed_valid_token(db, server_key, issued=NOW, expires_days=1)
    lc.invalidate_state_cache()
    state = lc.evaluate(db, now=NOW + timedelta(hours=12))
    assert state["reminder"] == "1"


# ---------------------------------------------------------------- 门禁同层


def _user(db, name):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider="drive", account_label="盘",
        credential_id=cred.id, status="active")
    db.add(binding)
    db.commit()
    return binding


def test_gate_layer_license_blocks_bound_user(db, monkeypatch, server_key):
    """执法同层：授权失效时，已绑定用户也在播放门禁处被拦；
    enforce 关时同一用户照常通过（零行为变化）。"""
    user = _user(db, "buyer")
    _binding(db, user.id)
    _seed_config(db)
    _seed_valid_token(db, server_key, issued=NOW - timedelta(days=40),
                      expires_days=30)  # 过宽限

    monkeypatch.setenv("AETRIX_LICENSE_ENFORCE", "1")
    lc.invalidate_state_cache()
    with pytest.raises(HTTPException) as exc:
        gate.ensure_binding_playback_allowed(db, user)
    assert exc.value.status_code == 403
    assert "授权" in exc.value.detail

    monkeypatch.delenv("AETRIX_LICENSE_ENFORCE", raising=False)
    lc.invalidate_state_cache()
    gate.ensure_binding_playback_allowed(db, user)  # 不抛 = 原行为

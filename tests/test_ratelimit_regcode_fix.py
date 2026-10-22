# -*- coding: utf-8 -*-
"""注册码/注册「请求频繁」误伤根治回归（2026-10-22）。

事故：跨机部署（前端机 → 后端机）下后端 TRUSTED_PROXIES 未覆盖前端机 IP，
全站用户共用一个限流桶，注册接口 24h 被误拒六千多次。本文件锁住代码侧
四件事（运维侧的 .env 名单与 nginx 透传见 docs/deploy-docker.md 第 9 节）：

B 网关规则表：登录专档必须是真实路由（旧 /api/user/login、/api/admin/emby/login
  在路由表里不存在、永不命中）；/api/user/auth/ 与 /api/site/ 拆独立桶。
C 计数语义：注册成功 reset、失败保留计数；preview/redeem 命中「明确无效」
  的码直接返回业务文案，不被 429 盖掉；preview 额度放宽到 60/分钟。
D 已用码提示：redeem/preview 失败带 error_code，已用尽/已被使用统一文案
  「该注册码已被使用」；前端 toUserMessage 对 400 的 detail 透传不许回退。
"""
import asyncio
import os
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import codes, models
from backend.ratelimit import (
    API_RATE_LIMITS,
    _limiter,
    check_rate_limit,
    get_client_ip,
)


# ---------------- 通用小工具 ----------------

def _make_request(direct_ip, xff=None, x_real_ip=None):
    req = MagicMock()
    req.client.host = direct_ip
    headers = {}
    if xff:
        headers["x-forwarded-for"] = xff
    if x_real_ip:
        headers["x-real-ip"] = x_real_ip
    req.headers.get = lambda k, default="": headers.get(k.lower(), default)
    return req


def _resolve_rule(path):
    """按中间件的口径（顺序首个 startswith 命中）解析一条路径落哪档。"""
    for prefix, limit_anon, limit_auth in API_RATE_LIMITS:
        if path.startswith(prefix):
            return prefix, limit_anon, limit_auth
    return None


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_user(db, username, **kw):
    u = models.WebUser(username=username, password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_code(db, code_str, **kw):
    kw.setdefault("code_type", codes.CODE_TYPE_RENEW)
    kw.setdefault("max_uses", 1)
    kw.setdefault("use_count", 0)
    kw.setdefault("is_active", True)
    kw.setdefault("days", 30)
    c = models.RegistrationCode(code=code_str, **kw)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---------------- B：网关规则表 ----------------

class TestGatewayRules:
    def test_user_login_hits_dedicated_rule(self):
        """用户登录真实路由 /api/user/auth/login 必须命中 10 次/分钟专档。"""
        prefix, anon, auth = _resolve_rule("/api/user/auth/login")
        assert prefix == "/api/user/auth/login"
        assert (anon, auth) == (10, 10)

    def test_admin_login_hits_dedicated_rule(self):
        """管理员登录真实路由 /api/admin/auth/login 同上。"""
        prefix, anon, auth = _resolve_rule("/api/admin/auth/login")
        assert prefix == "/api/admin/auth/login"
        assert (anon, auth) == (10, 10)

    def test_dead_login_prefixes_removed(self):
        """旧死条目（路由表里不存在的路径）不许留在规则表里冒充已生效。"""
        prefixes = [p for p, _, _ in API_RATE_LIMITS]
        assert "/api/user/login" not in prefixes
        assert "/api/admin/emby/login" not in prefixes

    def test_auth_prefix_paths_split_into_own_bucket(self):
        """注册链路的公开接口（captcha/register-config）落 /api/user/auth/ 独立桶，
        不再与全站 /api/ 通用桶混在一起被连坐。"""
        for path in ("/api/user/auth/captcha", "/api/user/auth/register-config",
                     "/api/user/auth/register"):
            prefix, _, _ = _resolve_rule(path)
            assert prefix == "/api/user/auth/", path

    def test_site_paths_split_into_own_bucket(self):
        prefix, _, _ = _resolve_rule("/api/site/branding")
        assert prefix == "/api/site/"

    def test_generic_api_still_generic(self):
        prefix, anon, auth = _resolve_rule("/api/user/profile")
        assert prefix == "/api/"
        assert (anon, auth) == (120, 600)

    def test_specific_rules_come_before_generic(self):
        """顺序护栏：具体前缀必须排在 /api/ 通用档之前，否则首命中永远轮不到。"""
        prefixes = [p for p, _, _ in API_RATE_LIMITS]
        generic = prefixes.index("/api/")
        for specific in ("/api/user/auth/login", "/api/admin/auth/login",
                         "/api/user/auth/", "/api/site/", "/api/admin/"):
            assert prefixes.index(specific) < generic, specific
        # 登录专档还要排在 /api/user/auth/ 之前（更具体者先行）
        assert prefixes.index("/api/user/auth/login") < prefixes.index("/api/user/auth/")


class TestCrossMachineRelay:
    """跨机中继：直连方是被信任的前端机（/32）时采信它硬写的 X-Real-IP，
    不同真实用户落不同桶（调查报告的回归项）。"""

    def test_relay_ip_trusted_via_32(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "172.18.0.0/16,203.0.113.10/32")
        req = _make_request("203.0.113.10", x_real_ip="1.2.3.4")
        assert get_client_ip(req) == "1.2.3.4"

    def test_different_real_ips_get_different_buckets(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "203.0.113.10/32")
        ip_a = get_client_ip(_make_request("203.0.113.10", x_real_ip="1.2.3.4"))
        ip_b = get_client_ip(_make_request("203.0.113.10", x_real_ip="5.6.7.8"))
        assert ip_a != ip_b
        key_a, key_b = f"gw-test:{ip_a}", f"gw-test:{ip_b}"
        _limiter.reset(key_a)
        _limiter.reset(key_b)
        try:
            assert check_rate_limit(key_a, 2, 60)[0] is True
            assert check_rate_limit(key_a, 2, 60)[0] is True
            assert check_rate_limit(key_a, 2, 60)[0] is False  # A 自己打满
            assert check_rate_limit(key_b, 2, 60)[0] is True   # B 不受连坐
        finally:
            _limiter.reset(key_a)
            _limiter.reset(key_b)


# ---------------- C：注册计数语义 ----------------

class TestRegisterCounting:
    IP = "198.51.100.23"
    KEY = f"register:{IP}"

    @pytest.fixture(autouse=True)
    def _clean(self, db):
        _limiter.reset(self.KEY)
        # 注册限流收紧到 2 次/窗口，让 reset/计数差异可观测
        for k, v in (("register_ratelimit_max", "2"),
                     ("register_ratelimit_window", "3600")):
            db.add(models.SystemConfig(key=k, value=v))
        db.commit()
        yield
        _limiter.reset(self.KEY)

    def _register(self, db, username):
        from backend.api.emby_portal import RegisterRequest, register

        req = _make_request(self.IP)
        return register(req, RegisterRequest(username=username, password="secret1"), db)

    def test_success_resets_bucket(self, db):
        """连着注册成功 3 次（额度只有 2）不应被限流：每次成功都退还计数。"""
        for name in ("rg_a1", "rg_a2", "rg_a3"):
            self._register(db, name)  # 不抛异常即成功

    def test_failures_still_count(self, db):
        """失败保留计数防爆破：同名重复注册（409）两次后，第 3 次尝试 429。"""
        self._register(db, "rg_b1")
        for _ in range(2):
            with pytest.raises(HTTPException) as exc:
                self._register(db, "rg_b1")
            assert exc.value.status_code == 409
        with pytest.raises(HTTPException) as exc:
            self._register(db, "rg_b2")
        assert exc.value.status_code == 429
        assert exc.value.detail == "注册过于频繁，请稍后再试"


# ---------------- C+D：核销 preview / redeem ----------------

class TestCodeEndpoints:
    @pytest.fixture(autouse=True)
    def _clean(self):
        yield
        for uid in getattr(self, "_uids", []):
            _limiter.reset(f"code_preview:{uid}")
            _limiter.reset(f"code_redeem:{uid}")

    def _track(self, *users):
        self._uids = [u.id for u in users]

    def test_preview_used_up_returns_message_not_429(self, db):
        """已用尽的码连试 30+ 次（烧满 preview 桶后继续试）：
        每次都拿到「该注册码已被使用」+ CODE_USED_UP，绝不被 429 盖掉。"""
        from backend.api.user import RedeemCodeRequest, preview_membership_code

        user = _make_user(db, "pv_user")
        self._track(user)
        _make_code(db, "USED-UP-001", max_uses=1, use_count=1)

        async def _preview():
            return await preview_membership_code(
                RedeemCodeRequest(code="USED-UP-001"), current_user=user, db=db)

        for _ in range(65):  # 超过新额度 60：烧满之后仍靠业务文案放行，不许变 429
            result = asyncio.run(_preview())
            assert result["valid"] is False
            assert result["message"] == "该注册码已被使用"
            assert result["error_code"] == codes.CODE_ERROR_USED_UP

    def test_preview_valid_code_still_rate_limited(self, db):
        """有效码的预检照常受限流保护（防枚举）：烧满 60 次后第 61 次 429。"""
        from backend.api.user import RedeemCodeRequest, preview_membership_code

        user = _make_user(db, "pv_user2")
        self._track(user)
        _make_code(db, "VALID-001", max_uses=5, use_count=0)
        key = f"code_preview:{user.id}"
        for _ in range(60):
            assert check_rate_limit(key, 60, 60)[0] is True

        async def _preview():
            return await preview_membership_code(
                RedeemCodeRequest(code="VALID-001"), current_user=user, db=db)

        with pytest.raises(HTTPException) as exc:
            asyncio.run(_preview())
        assert exc.value.status_code == 429

    def test_redeem_used_up_under_limit_returns_error_code(self, db):
        """限流内核销已用尽码：400 + detail 人话 + error_code，不再只有笼统报错。"""
        from backend.api.user import RedeemCodeRequest, redeem_membership_code

        user = _make_user(db, "rd_user")
        self._track(user)
        _make_code(db, "USED-UP-002", max_uses=1, use_count=1)

        resp = redeem_membership_code(
            RedeemCodeRequest(code="USED-UP-002"), current_user=user, db=db)
        assert resp.status_code == 400
        import json
        body = json.loads(resp.body)
        assert body["detail"] == "该注册码已被使用"
        assert body["error_code"] == codes.CODE_ERROR_USED_UP

    def test_redeem_used_up_over_limit_still_returns_message(self, db):
        """限流桶已满时核销已用尽码：仍给业务文案（只读分类兜底），不是 429。"""
        from backend.api.user import RedeemCodeRequest, redeem_membership_code

        user = _make_user(db, "rd_user2")
        self._track(user)
        _make_code(db, "USED-UP-003", max_uses=1, use_count=1)
        key = f"code_redeem:{user.id}"
        for _ in range(10):
            assert check_rate_limit(key, 10, 60)[0] is True

        resp = redeem_membership_code(
            RedeemCodeRequest(code="USED-UP-003"), current_user=user, db=db)
        assert resp.status_code == 400
        import json
        body = json.loads(resp.body)
        assert body["detail"] == "该注册码已被使用"
        assert body["error_code"] == codes.CODE_ERROR_USED_UP

    def test_redeem_valid_code_over_limit_is_429(self, db):
        """限流桶满 + 码本身有效：照常 429（防爆破不放松）。"""
        from backend.api.user import RedeemCodeRequest, redeem_membership_code

        user = _make_user(db, "rd_user3")
        self._track(user)
        _make_code(db, "VALID-002", max_uses=5, use_count=0)
        key = f"code_redeem:{user.id}"
        for _ in range(10):
            assert check_rate_limit(key, 10, 60)[0] is True

        with pytest.raises(HTTPException) as exc:
            redeem_membership_code(
                RedeemCodeRequest(code="VALID-002"), current_user=user, db=db)
        assert exc.value.status_code == 429

    def test_redeem_self_used_returns_self_used_code(self, db):
        """自己重复核销：沿用长文案 + CODE_SELF_USED。"""
        from backend.api.user import RedeemCodeRequest, redeem_membership_code

        user = _make_user(db, "rd_user4")
        self._track(user)
        realm = models.ServerRealm(name="rdrealm", slug="rd", is_active=True)
        db.add(realm)
        db.commit()
        _make_code(db, "SELF-001", max_uses=5, use_count=0, realm_id=realm.id)

        ok = redeem_membership_code(
            RedeemCodeRequest(code="SELF-001"), current_user=user, db=db)
        assert getattr(ok, "status_code", 200) == 200

        resp = redeem_membership_code(
            RedeemCodeRequest(code="SELF-001"), current_user=user, db=db)
        assert resp.status_code == 400
        import json
        body = json.loads(resp.body)
        assert body["error_code"] == codes.CODE_ERROR_SELF_USED
        assert "你已经使用过这张卡码" in body["detail"]


# ---------------- D：codes 层机器码与分类 ----------------

class TestCodesErrorCodes:
    def test_redeem_code_used_up(self, db):
        _make_user(db, "cc_user")
        user = db.query(models.WebUser).filter_by(username="cc_user").one()
        _make_code(db, "CC-USED", max_uses=1, use_count=1)
        result = codes.redeem_code(db, user, "CC-USED")
        db.rollback()
        assert result["success"] is False
        assert result["message"] == "该注册码已被使用"
        assert result["error_code"] == codes.CODE_ERROR_USED_UP

    def test_preview_not_found(self, db):
        result = codes.preview_code(db, "NO-SUCH-CODE")
        assert result["valid"] is False
        assert result["error_code"] == codes.CODE_ERROR_NOT_FOUND

    def test_classify_invalid_states(self, db):
        user = _make_user(db, "cc_user2")
        _make_code(db, "CC-USED2", max_uses=1, use_count=1)
        _make_code(db, "CC-VALID", max_uses=5, use_count=0)
        hit = codes.classify_invalid_code(db, user, "CC-USED2")
        assert hit == {"error_code": codes.CODE_ERROR_USED_UP,
                       "message": "该注册码已被使用"}
        assert codes.classify_invalid_code(db, user, "CC-VALID") is None
        miss = codes.classify_invalid_code(db, user, "CC-MISSING")
        assert miss["error_code"] == codes.CODE_ERROR_NOT_FOUND

    def test_classify_honeypot_not_short_circuited(self, db):
        """蜜罐码不许被分类短路：必须走正常核销流程触发封禁。"""
        user = _make_user(db, "cc_user3")
        _make_code(db, "HONEY-TRAP1", max_uses=1, use_count=0)
        assert codes.classify_invalid_code(db, user, "HONEY-TRAP1") is None

    def test_error_code_mapping_order(self, db):
        """停用优先于过期、过期优先于用尽（与 reg_code_error 同口径）。"""
        from datetime import datetime, timedelta

        disabled = _make_code(db, "CC-DIS", is_active=False, use_count=9, max_uses=1)
        assert codes.reg_code_error_code(disabled) == codes.CODE_ERROR_DISABLED
        expired = _make_code(db, "CC-EXP", use_count=9, max_uses=1,
                             expires_at=datetime.now() - timedelta(days=1))
        assert codes.reg_code_error_code(expired) == codes.CODE_ERROR_EXPIRED


# ---------------- D：前端 detail 透传（静态契约锁） ----------------

def test_frontend_400_detail_passthrough_locked():
    """toUserMessage 只对 401/403/429/500 做状态码优先映射；400 的人话
    detail 必须透传——核销失败文案（「该注册码已被使用」等）靠这条上屏，
    谁把 400 也加进状态码优先表，明确无效提示就会再次被吞掉。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "user_frontend/src/utils/toUserMessage.ts").read_text(encoding="utf-8")
    status_block = src.split("状态码映射（优先于 detail")[1].split("detail 解析")[0]
    assert "status === 429" in status_block
    assert "status === 400" not in status_block
    assert "return text" in src  # 人话 detail 原样透传

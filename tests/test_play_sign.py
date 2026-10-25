"""播放短期签名 + token 有效期 + 防盗链测试。

覆盖 backend/emby_server/play_sign.py 的纯逻辑，以及各处接入点的静态断言。
"""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backend.emby_server import play_sign


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    """测试用固定签名密钥，避免依赖环境变量。"""
    import backend.security as sec

    monkeypatch.setattr(sec, "SECRET_KEY", "test-secret-key-for-play-sign-0000")


class TestPlaySign:
    def test_issue_verify_roundtrip(self):
        exp, sign = play_sign.issue_play_sign(42, "item-guid-1")
        assert len(sign) == 32
        assert exp > int(time.time())
        assert play_sign.verify_play_sign(42, "item-guid-1", exp, sign)

    def test_expired_rejected(self):
        exp = int(time.time()) - 1
        sign = play_sign.make_play_sign(42, "item-guid-1", exp)
        assert not play_sign.verify_play_sign(42, "item-guid-1", exp, sign)

    def test_tampered_sign_rejected(self):
        exp, sign = play_sign.issue_play_sign(42, "item-guid-1")
        bad = ("0" if sign[0] != "0" else "1") + sign[1:]
        assert not play_sign.verify_play_sign(42, "item-guid-1", exp, bad)

    def test_wrong_item_rejected(self):
        exp, sign = play_sign.issue_play_sign(42, "item-guid-1")
        assert not play_sign.verify_play_sign(42, "item-guid-2", exp, sign)

    def test_wrong_user_rejected(self):
        exp, sign = play_sign.issue_play_sign(42, "item-guid-1")
        assert not play_sign.verify_play_sign(43, "item-guid-1", exp, sign)

    def test_malformed_inputs_rejected(self):
        assert not play_sign.verify_play_sign("x", "g", "y", "z")
        assert not play_sign.verify_play_sign(1, "g", int(time.time()) + 60, "")
        assert not play_sign.verify_play_sign(1, "g", int(time.time()) + 60, "short")

    def test_ttl_default_15min(self):
        assert play_sign.SIGN_TTL_SECONDS == 900
        exp, _ = play_sign.issue_play_sign(1, "g")
        assert 800 < exp - int(time.time()) <= 900

    def test_deterministic(self):
        """同一输入签名稳定（HMAC 确定性），跨进程可验。"""
        exp = int(time.time()) + 600
        assert play_sign.make_play_sign(7, "abc", exp) == play_sign.make_play_sign(7, "abc", exp)


class TestTokenExpiry:
    def test_usable_fresh_token(self):
        row = SimpleNamespace(is_revoked=False,
                              expires_at=datetime.now() + timedelta(days=30))
        assert play_sign.token_is_usable(row)

    def test_expired_token_unusable(self):
        row = SimpleNamespace(is_revoked=False,
                              expires_at=datetime.now() - timedelta(seconds=1))
        assert not play_sign.token_is_usable(row)

    def test_revoked_token_unusable(self):
        row = SimpleNamespace(is_revoked=True,
                              expires_at=datetime.now() + timedelta(days=30))
        assert not play_sign.token_is_usable(row)

    def test_null_expiry_grandfathered(self):
        """历史 token（expires_at 为空）视为永不过期，不强制老客户端重登。"""
        row = SimpleNamespace(is_revoked=False, expires_at=None)
        assert play_sign.token_is_usable(row)

    def test_none_row(self):
        assert not play_sign.token_is_usable(None)

    def test_default_ttl_30_days(self):
        assert play_sign.TOKEN_TTL_DAYS == 30
        exp = play_sign.token_expiry_default()
        assert timedelta(days=29) < exp - datetime.now() < timedelta(days=31)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """只够 SystemConfig 读写用的最小 fake。"""

    def __init__(self, config_value=None):
        self._row = (SimpleNamespace(key=play_sign.CONFIG_ALLOWED_REFERERS,
                                     value=config_value)
                     if config_value is not None else None)
        self.added = []
        self.committed = False

    def query(self, model):
        return _FakeQuery([self._row] if self._row else [])

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True


class _FakeURL:
    path = "/emby/Videos/abc/stream"


class _FakeRequest:
    def __init__(self, referer=None):
        self.headers = {"referer": referer} if referer else {}
        self.url = _FakeURL()


class TestReferer:
    def test_empty_whitelist_passes(self):
        play_sign.check_referer(_FakeRequest("https://evil.com/x"), _FakeDB(None))

    def test_matching_referer_passes(self):
        db = _FakeDB("cdn.example.com, cdn.example.com")
        play_sign.check_referer(
            _FakeRequest("https://cdn.example.com/emby/Videos/1/stream"), db)

    def test_non_matching_referer_blocked(self):
        from fastapi import HTTPException

        db = _FakeDB("cdn.example.com")
        with pytest.raises(HTTPException) as exc:
            play_sign.check_referer(_FakeRequest("https://evil.com/steal"), db)
        assert exc.value.status_code == 403

    def test_missing_referer_passes_with_whitelist(self):
        """原生客户端（Infuse/VLC）一般不带 Referer，不能一刀切拦掉。"""
        db = _FakeDB("cdn.example.com")
        play_sign.check_referer(_FakeRequest(None), db)

    def test_db_error_fail_open(self):
        """配置读失败时 fail-open 放行：播放链路不能因配置表读不到就全挂。

        回归：video_stream 的单测用 mock db（object()）直接调，
        防盗链不能在这种场景下炸掉整个播放。
        """
        play_sign.check_referer(_FakeRequest("https://evil.com/x"), object())

    def test_write_normalizes_hosts(self):
        # r36：写入走 config_service 统一通道，需要真实 session；
        # 提交由调用方决定，这里断言写后同会话立即可见（统一通道写时失效）。
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from backend import models
        from backend.database import Base
        from backend.emby_server import models as _em  # noqa: F401 注册模型

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            hosts = play_sign.write_allowed_referers(
                db, "https://CDN.Example.com/x, b.com, b.com, not a url??")
            assert hosts == ["cdn.example.com", "b.com"]
            row = db.query(models.SystemConfig).filter(
                models.SystemConfig.key == play_sign.CONFIG_ALLOWED_REFERERS
            ).first()
            assert row is not None and row.value == "cdn.example.com,b.com"
        finally:
            db.close()

    def test_get_parses_comma_list(self):
        db = _FakeDB(" a.com ,, b.com ")
        assert play_sign.get_allowed_referers(db) == ["a.com", "b.com"]


class TestWiring:
    """接入点静态断言：改动确实落到了各文件。"""

    def test_api_issues_signed_urls(self):
        with open("backend/emby_server/api.py", encoding="utf-8") as f:
            content = f.read()
        # playback_info 双轨发放签名参数
        assert "issue_play_sign" in content
        assert "&uid=" in content and "&exp=" in content and "&sign=" in content
        # 四个播放路由走签名优先的鉴权依赖
        # （video_stream / video_stream_ext / video_hls + #1248 上游兜底口，
        # 上游口与 video_stream 同口径走 get_play_user，故计入）
        assert content.count("Depends(get_play_user)") == 4
        # 防盗链检查挂在 video_stream（video_hls 转交 video_stream，
        # 同一检查点覆盖两个入口）
        assert content.count("play_sign.check_referer") == 1

    def test_auth_wiring(self):
        with open("backend/emby_server/auth.py", encoding="utf-8") as f:
            content = f.read()
        assert "def get_play_user" in content
        assert "expires_at=play_sign.token_expiry_default()" in content
        assert content.count("play_sign.token_is_usable(row)") == 2

    def test_model_has_expires_at(self):
        with open("backend/emby_server/models.py", encoding="utf-8") as f:
            content = f.read()
        assert "expires_at = Column(DateTime, nullable=True)" in content

    def test_maintenance_purges_tokens(self):
        with open("backend/emby_server/maintenance.py", encoding="utf-8") as f:
            content = f.read()
        assert "def purge_expired_emby_tokens" in content
        assert '"emby_tokens_purged"' in content

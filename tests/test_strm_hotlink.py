"""防盗链（Drive SA 凭据中转 + 播放签名可配置）单元测试。

注（2026-10-17）：strm_sign.py 已随 strm_gen 删除，纸条签名/验签/
刷新判定测试同步删除；local_play_target 的 .strm 内容直读分支保留。

覆盖：
- backend/emby_server/drive_auth.py：Drive 域名判定/凭据头构造/失败降级
- backend/emby_server/play_sign.py：防盗链开关 + TTL 热读/钳制/write_config 校验
- backend/emby_server/mounts.py：local_play_target 的 .strm 分支（三态）
- backend/emby_server/streaming.py：_redirect_headers 的 Drive 例外

用隔离的内存 SQLite，不碰生产库；不碰真实网络。
"""
from __future__ import annotations

import time
from unittest import mock

import pytest

DRIVE_URL = "https://drive.google.com/uc?export=download&id=ABC123&confirm=t"


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    """测试用固定签名密钥，避免依赖环境变量。"""
    import backend.security as sec

    monkeypatch.setattr(sec, "SECRET_KEY", "test-secret-key-for-strm-hotlink-00")


@pytest.fixture()
def db():
    """隔离的内存 SQLite（照抄 tests/test_cdn.py 的模式）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models
    from backend.integrations import store

    store.invalidate()
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        store.invalidate()


def _set_config(db, key, value):
    from backend import models
    from backend.integrations import store

    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()
    store.invalidate(key)

# ==================== drive_auth ====================

class TestDriveAuth:
    def test_is_drive_url(self):
        from backend.emby_server import drive_auth

        assert drive_auth.is_drive_url("https://drive.google.com/uc?id=1")
        assert drive_auth.is_drive_url("https://drive.usercontent.google.com/download?id=1")
        assert drive_auth.is_drive_url("https://docs.google.com/document/d/1")
        assert drive_auth.is_drive_url("https://www.googleapis.com/drive/v3/files/1")
        assert drive_auth.is_drive_url("HTTPS://DRIVE.GOOGLE.COM/uc?id=1")  # 大小写
        assert not drive_auth.is_drive_url("https://example.com/x")
        assert not drive_auth.is_drive_url("https://evildrive.google.com.evil.com/x")
        assert not drive_auth.is_drive_url("")

    def test_headers_non_drive_is_empty(self):
        from backend.emby_server import drive_auth

        assert drive_auth.drive_auth_headers("https://example.com/video.mp4") == {}

    def test_headers_no_token_is_empty(self):
        """拿不到 SA token 时降级为空 dict（不阻断播放）。"""
        from backend.emby_server import drive_auth

        drive_auth._reset_state()
        with mock.patch.object(drive_auth, "_pick_sa", return_value=None):
            assert drive_auth.drive_auth_headers(DRIVE_URL) == {}

    def test_headers_with_token(self):
        from backend.emby_server import drive_auth

        with mock.patch.object(drive_auth, "get_drive_bearer_token", return_value="TOK123"):
            headers = drive_auth.drive_auth_headers(DRIVE_URL)
        assert headers == {"Authorization": "Bearer TOK123"}

    def test_get_token_never_raises(self):
        from backend.emby_server import drive_auth

        drive_auth._reset_state()
        with mock.patch.object(drive_auth, "_pick_sa", side_effect=RuntimeError("boom")):
            assert drive_auth.get_drive_bearer_token() is None

    def test_token_cache_hit(self):
        """按 SA 分账号缓存：同一 SA 剩余有效期 >120s 直接返回，不再换 token。

        P1 修复后 get_drive_bearer_token() 每次都会走 _pick_sa() 轮询
        （这正是修复点），缓存命中体现在"不调 _sa_access_token"上。
        """
        from backend.emby_server import drive_auth

        drive_auth._reset_state()
        drive_auth._token_cache["sa-cache@x"] = ("CACHED", time.monotonic() + 3000)
        fake_changes = mock.MagicMock()
        fake_changes._sa_access_token.side_effect = AssertionError("should not be called")
        try:
            with mock.patch.object(
                drive_auth,
                "_pick_sa",
                return_value={"client_email": "sa-cache@x", "private_key": "pk"},
            ), mock.patch.object(
                drive_auth, "_load_drive_changes", return_value=fake_changes
            ):
                assert drive_auth.get_drive_bearer_token() == "CACHED"
        finally:
            drive_auth._reset_state()


# ==================== play_sign 配置 ====================

class TestPlaySignConfig:
    def test_hotlink_enabled_default_true(self):
        from backend.emby_server import play_sign

        assert play_sign.hotlink_enabled() is True

    def test_hotlink_enabled_values(self, db):
        from backend.emby_server import play_sign

        _set_config(db, play_sign.CONFIG_HOTLINK_ENABLED, "false")
        assert play_sign.hotlink_enabled(db) is False
        _set_config(db, play_sign.CONFIG_HOTLINK_ENABLED, "0")
        assert play_sign.hotlink_enabled(db) is False
        _set_config(db, play_sign.CONFIG_HOTLINK_ENABLED, "true")
        assert play_sign.hotlink_enabled(db) is True
        _set_config(db, play_sign.CONFIG_HOTLINK_ENABLED, "YES")
        assert play_sign.hotlink_enabled(db) is True

    def test_hotlink_enabled_illegal_falls_back_true(self, db):
        """非法值回落默认启用：不弱化验签语义。"""
        from backend.emby_server import play_sign

        _set_config(db, play_sign.CONFIG_HOTLINK_ENABLED, "maybe")
        assert play_sign.hotlink_enabled(db) is True
        _set_config(db, play_sign.CONFIG_HOTLINK_ENABLED, "")
        assert play_sign.hotlink_enabled(db) is True

    def test_play_sign_ttl_clamp(self, db):
        from backend.emby_server import play_sign

        assert play_sign.play_sign_ttl_seconds() == 900  # db=None 默认
        _set_config(db, play_sign.CONFIG_PLAY_SIGN_TTL, "1800")
        assert play_sign.play_sign_ttl_seconds(db) == 1800
        _set_config(db, play_sign.CONFIG_PLAY_SIGN_TTL, "10")  # <60 钳制
        assert play_sign.play_sign_ttl_seconds(db) == 60
        _set_config(db, play_sign.CONFIG_PLAY_SIGN_TTL, "999999")  # >86400 钳制
        assert play_sign.play_sign_ttl_seconds(db) == 86400
        _set_config(db, play_sign.CONFIG_PLAY_SIGN_TTL, "not-a-number")
        assert play_sign.play_sign_ttl_seconds(db) == 900  # 非法回落默认

    def test_write_config_roundtrip(self, db):
        from backend import models
        from backend.emby_server import play_sign

        payload = play_sign.write_config(db, enabled=False, play_sign_ttl=1800)
        assert payload["enabled"] is False
        assert payload["play_sign_ttl"] == 1800
        assert payload["defaults"] == {"enabled": True, "play_sign_ttl": 900}
        # 落库 + description
        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == play_sign.CONFIG_HOTLINK_ENABLED).first()
        assert row.value == "false"
        assert row.description
        # 热读即时生效（write_config 内已 invalidate）
        assert play_sign.hotlink_enabled(db) is False

    def test_write_config_rejects_out_of_range(self, db):
        from backend.emby_server import play_sign

        with pytest.raises(ValueError):
            play_sign.write_config(db, enabled=True, play_sign_ttl=30)
        with pytest.raises(ValueError):
            play_sign.write_config(db, enabled=True, play_sign_ttl=90000)
        with pytest.raises(ValueError):
            play_sign.write_config(db, enabled=True, play_sign_ttl="abc")

    def test_sign_ttl_seconds_constant_kept(self):
        """SIGN_TTL_SECONDS 常量保留（兼容旧引用）。"""
        from backend.emby_server import play_sign

        assert play_sign.SIGN_TTL_SECONDS == 900


# ==================== mounts.local_play_target ====================

class TestMountsStrmBranch:
    """local_play_target 的 .strm 分支（2026-10-17 新行为：纸条签名已随 strm_gen 删除，
    不再验签，直接用文件内容里的原文直链；Google 会忽略多余的 aexp/asig 参数）。"""

    def _write(self, tmp_path, name, content):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_strm_content_passes_through_asis(self, tmp_path):
        from backend.emby_server import mounts

        signed = DRIVE_URL + "&aexp=9999999999&asig=abcdef"
        path = self._write(tmp_path, "ok.strm", signed + "\n# 注释保留\n")
        with mock.patch(
            "backend.emby_server.drive_auth.drive_auth_headers",
            return_value={"Authorization": "Bearer <redacted>"},
        ):
            target = mounts.local_play_target(path)
        assert target.kind == "url"
        assert target.value == signed  # 原文直传，不再剥签名参数
        assert target.headers["Authorization"] == "Bearer <redacted>"
        assert "User-Agent" in target.headers

    def test_plain_strm_passes(self, tmp_path):
        from backend.emby_server import mounts

        path = self._write(tmp_path, "plain.strm", DRIVE_URL + "\n")
        with mock.patch(
            "backend.emby_server.drive_auth.drive_auth_headers", return_value={}
        ):
            target = mounts.local_play_target(path)
        assert target.kind == "url"
        assert target.value == DRIVE_URL

    def test_bad_signature_no_longer_rejected(self, tmp_path):
        """验签已删除：签名无效/过期的纸条也不再拒播，原文直传。"""
        from backend.emby_server import mounts

        path = self._write(tmp_path, "bad.strm", DRIVE_URL + "&aexp=1&asig=deadbeef\n")
        with mock.patch(
            "backend.emby_server.drive_auth.drive_auth_headers", return_value={}
        ):
            target = mounts.local_play_target(path)
        assert target.kind == "url"
        assert target.value == DRIVE_URL + "&aexp=1&asig=deadbeef"

    def test_empty_strm_still_raises_missing_link(self, tmp_path):
        from backend.emby_server import mounts

        path = self._write(tmp_path, "empty.strm", "\n# 空文件\n")
        with pytest.raises(mounts.MountError, match="没有可用的直链"):
            mounts.local_play_target(path)

    def test_resolve_final_strm_passthrough(self):
        """远程挂载入口 resolve_final 同样走直传（与 local_play_target 共用 helper）。"""
        from backend.emby_server import mounts

        signed = DRIVE_URL + "&aexp=9999999999&asig=abcdef"
        provider = mounts.MountProvider.__new__(mounts.MountProvider)
        provider.read_text = lambda rel: signed
        with mock.patch(
            "backend.emby_server.drive_auth.drive_auth_headers",
            return_value={"Authorization": "Bearer <redacted>"},
        ):
            target = provider.resolve_final("movie.strm")
        assert target.kind == "url"
        assert target.value == signed
        assert target.headers["Authorization"] == "Bearer <redacted>"

        provider.read_text = lambda rel: DRIVE_URL + "&aexp=1&asig=deadbeef"
        target = provider.resolve_final("movie.strm")
        assert target.kind == "url"
        assert target.value == DRIVE_URL + "&aexp=1&asig=deadbeef"


# ==================== streaming._redirect_headers ====================

class TestRedirectHeaders:
    def test_same_host_untouched(self):
        from backend.emby_server.streaming import _redirect_headers

        fwd = {"Authorization": "Bearer X", "Cookie": "c=1", "User-Agent": "ua"}
        assert _redirect_headers(fwd, "https://a.com/1", "https://a.com/2") == fwd

    def test_drive_to_drive_keeps_authorization_only(self):
        from backend.emby_server.streaming import _redirect_headers

        fwd = {
            "Authorization": "Bearer X",
            "Cookie": "c=1",
            "Proxy-Authorization": "p",
            "User-Agent": "ua",
        }
        out = _redirect_headers(
            fwd,
            "https://drive.google.com/uc?id=1",
            "https://drive.usercontent.google.com/download?id=1",
        )
        assert out == {"Authorization": "Bearer X", "User-Agent": "ua"}

    def test_drive_to_evil_strips_all(self):
        from backend.emby_server.streaming import _redirect_headers

        fwd = {"Authorization": "Bearer X", "Cookie": "c=1", "User-Agent": "ua"}
        out = _redirect_headers(fwd, "https://drive.google.com/uc?id=1", "https://evil.com/x")
        assert out == {"User-Agent": "ua"}

    def test_non_drive_cross_host_strips_all(self):
        """非 Drive 目标保持原逻辑：全剥（回归）。"""
        from backend.emby_server.streaming import _redirect_headers

        fwd = {"Authorization": "Basic X", "Cookie": "c=1", "User-Agent": "ua"}
        out = _redirect_headers(fwd, "https://webdav.example.com/a", "https://other.example.com/b")
        assert out == {"User-Agent": "ua"}

    def test_header_name_case_insensitive(self):
        from backend.emby_server.streaming import _redirect_headers

        fwd = {"AUTHORIZATION": "Bearer Y", "cookie": "c=1"}
        out = _redirect_headers(
            fwd, "https://drive.google.com/a", "https://docs.google.com/b"
        )
        assert out == {"AUTHORIZATION": "Bearer Y"}

"""播放签名按条目时长发放（2026-10-29 用户拍板）。

口径：15 分钟一刀切改为「电视剧一集的时间、电影一部电影的时间」——
条目播放签发的 play_sign 有效期 = 条目时长 + 缓冲（默认 15 分钟），
下限 30 分钟 / 上限 6 小时钳制，时长未知默认 60 分钟；四个旋钮走
SystemConfig 小写键热读（play_sign.duration_ttl_knobs）。

覆盖：
- 纯计算 ttl_for_runtime：剧集/电影/未知/极短/极长/边界；
- 旋钮 reader：默认、显式值、钳制、非法值回落、db=None、真实 DB 行；
- 签发+校验集成：按时长签发的 exp 落点正确、过期仍拒；
- 接入点静态断言：api.py 两个签发点都走 play_sign_ttl_for_runtime、
  固定 6 小时 env 口径 PLAY_SIGN_URL_TTL 已退役、全仓后端签发点只剩
  这两处（非播放签发点不存在共用 helper 的情况，口径未被扩大）。
"""
import pathlib
import time

import pytest

from backend.emby_server import play_sign

REPO = pathlib.Path(__file__).resolve().parent.parent

DEFAULT_KNOBS = dict(buffer_seconds=900, floor_seconds=1800,
                     cap_seconds=21600, unknown_seconds=3600)


@pytest.fixture(autouse=True)
def fixed_secret(monkeypatch):
    """测试用固定签名密钥，避免依赖环境变量（同 test_play_sign 口径）。"""
    import backend.security as sec

    monkeypatch.setattr(sec, "SECRET_KEY", "test-secret-key-for-play-sign-0000")


def _stub_store(monkeypatch, values):
    def fake_get(db, key, default=""):
        return values.get(key, default)

    monkeypatch.setattr(play_sign.store, "get_value", fake_get)


class TestTtlForRuntime:
    def test_episode_45min(self):
        # 45 分钟一集 + 15 分钟缓冲 = 60 分钟
        assert play_sign.ttl_for_runtime(2700, **DEFAULT_KNOBS) == 3600

    def test_movie_130min(self):
        # 130 分钟电影 + 15 分钟缓冲 = 145 分钟
        assert play_sign.ttl_for_runtime(7800, **DEFAULT_KNOBS) == 8700

    def test_unknown_runtime(self):
        assert play_sign.ttl_for_runtime(0, **DEFAULT_KNOBS) == 3600
        assert play_sign.ttl_for_runtime(None, **DEFAULT_KNOBS) == 3600
        assert play_sign.ttl_for_runtime(-5, **DEFAULT_KNOBS) == 3600
        assert play_sign.ttl_for_runtime("abc", **DEFAULT_KNOBS) == 3600

    def test_tiny_runtime_floored(self):
        # 1 分钟短片：60 + 900 = 960 < 下限 1800 → 1800
        assert play_sign.ttl_for_runtime(60, **DEFAULT_KNOBS) == 1800

    def test_huge_runtime_capped(self):
        # 10 小时：36000 + 900 远超上限 → 21600（6 小时）
        assert play_sign.ttl_for_runtime(36000, **DEFAULT_KNOBS) == 21600

    def test_floor_boundary(self):
        # 15 分钟时长 + 15 分钟缓冲 = 恰好下限 1800
        assert play_sign.ttl_for_runtime(900, **DEFAULT_KNOBS) == 1800


class TestDurationKnobsReader:
    def test_defaults_drive_durations(self, monkeypatch):
        _stub_store(monkeypatch, {})
        assert play_sign.play_sign_ttl_for_runtime(object(), 2700) == 3600
        assert play_sign.play_sign_ttl_for_runtime(object(), 7800) == 8700
        assert play_sign.play_sign_ttl_for_runtime(object(), 0) == 3600

    def test_db_none_uses_defaults(self):
        assert play_sign.play_sign_ttl_for_runtime(None, 2700) == 3600
        assert play_sign.duration_ttl_knobs(None) == DEFAULT_KNOBS

    def test_custom_buffer(self, monkeypatch):
        _stub_store(monkeypatch, {play_sign.CONFIG_PLAY_SIGN_TTL_BUFFER: "600"})
        assert play_sign.play_sign_ttl_for_runtime(object(), 2700) == 3300

    def test_custom_unknown(self, monkeypatch):
        _stub_store(monkeypatch, {play_sign.CONFIG_PLAY_SIGN_TTL_UNKNOWN: "5400"})
        assert play_sign.play_sign_ttl_for_runtime(object(), 0) == 5400
        # 未知默认低于下限时被下限抬起（未知 1200 → 1800）
        _stub_store(monkeypatch, {play_sign.CONFIG_PLAY_SIGN_TTL_UNKNOWN: "1200"})
        assert play_sign.play_sign_ttl_for_runtime(object(), 0) == 1800

    def test_custom_floor_and_cap(self, monkeypatch):
        _stub_store(monkeypatch, {play_sign.CONFIG_PLAY_SIGN_TTL_MIN: "3600"})
        assert play_sign.play_sign_ttl_for_runtime(object(), 60) == 3600
        _stub_store(monkeypatch, {play_sign.CONFIG_PLAY_SIGN_TTL_MAX: "3600"})
        assert play_sign.play_sign_ttl_for_runtime(object(), 7800) == 3600

    def test_bad_value_falls_back(self, monkeypatch):
        _stub_store(monkeypatch, {play_sign.CONFIG_PLAY_SIGN_TTL_BUFFER: "abc"})
        assert play_sign.play_sign_ttl_for_runtime(object(), 2700) == 3600

    def test_knob_itself_clamped(self, monkeypatch):
        # 下限旋钮配了超界值 999999 → 钳到 86400，有效期随之顶格
        _stub_store(monkeypatch, {play_sign.CONFIG_PLAY_SIGN_TTL_MIN: "999999"})
        assert play_sign.play_sign_ttl_for_runtime(object(), 2700) == 86400

    def test_real_db_rows(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from backend.database import Base
        from backend import models as core_models  # noqa: F401 注册 SystemConfig
        from backend.emby_server import models as em  # noqa: F401 注册模型
        from backend.integrations import store

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            db.add(core_models.SystemConfig(
                key=play_sign.CONFIG_PLAY_SIGN_TTL_BUFFER, value="600"))
            db.commit()
            store.invalidate()
            assert play_sign.play_sign_ttl_for_runtime(db, 2700) == 3300
            # 没配行的旋钮走默认：电影 7800 + 默认缓冲 900……本行已改缓冲
            # 为 600，故 7800 + 600 = 8400（上限内）
            assert play_sign.play_sign_ttl_for_runtime(db, 7800) == 8400
        finally:
            store.invalidate()
            db.close()


class TestIssueAndVerify:
    def test_issued_exp_matches_duration_ttl(self, monkeypatch):
        _stub_store(monkeypatch, {})
        ttl = play_sign.play_sign_ttl_for_runtime(object(), 2700)
        before = int(time.time())
        exp, sign = play_sign.issue_play_sign(7, "guid-x", ttl)
        assert before + 3500 < exp <= before + 3600 + 5
        assert play_sign.verify_play_sign(7, "guid-x", exp, sign)

    def test_expired_still_rejected(self):
        # 按时长口径签发不等于放宽校验：载荷 exp 一过照样拒
        exp = int(time.time()) - 1
        sign = play_sign.make_play_sign(7, "guid-x", exp)
        assert not play_sign.verify_play_sign(7, "guid-x", exp, sign)

    def test_sign_format_unchanged(self, monkeypatch):
        _stub_store(monkeypatch, {})
        ttl = play_sign.play_sign_ttl_for_runtime(object(), 7800)
        exp, sign = play_sign.issue_play_sign(7, "guid-x", ttl)
        assert len(sign) == 32
        assert sign == play_sign.make_play_sign(7, "guid-x", exp)


class TestWiring:
    def test_api_uses_duration_ttl(self):
        content = (REPO / "backend/emby_server/api.py").read_text(encoding="utf-8")
        # PlaybackInfo 与 _url_auth_qs 两个签发点都按时长发放
        assert content.count("play_sign_ttl_for_runtime") >= 2
        # 固定 6 小时 env 口径已退役（常量与读取一并清掉）
        assert "PLAY_SIGN_URL_TTL" not in content

    def test_only_two_issuance_sites_in_backend(self):
        # 非条目播放的签发点本单不改口径：先钉死现状——全后端调用
        # issue_play_sign 的只有 api.py 两处，不存在分享链接等共用
        # 同一 helper 被顺带改掉的风险。
        hits = []
        for path in (REPO / "backend").rglob("*.py"):
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "play_sign.issue_play_sign(" in line:
                    hits.append((path.relative_to(REPO).as_posix(), lineno))
        assert len(hits) == 2
        assert all(h[0] == "backend/emby_server/api.py" for h in hits)

    def test_self_heal_registers_new_keys(self):
        from backend import config_self_heal

        keys = {k for k, _default, _desc in config_self_heal.collect_system_config_defaults()}
        for key in (play_sign.CONFIG_PLAY_SIGN_TTL_BUFFER,
                    play_sign.CONFIG_PLAY_SIGN_TTL_MIN,
                    play_sign.CONFIG_PLAY_SIGN_TTL_MAX,
                    play_sign.CONFIG_PLAY_SIGN_TTL_UNKNOWN):
            assert key in keys

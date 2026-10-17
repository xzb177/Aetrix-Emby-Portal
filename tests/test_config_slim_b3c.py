# -*- coding: utf-8 -*-
"""配置瘦身第 3c 批：补入口键的后端语义与新/改端点。

- 防盗链 Referer 白名单：GET 回显归一化列表、PUT 同卡保存、老前端不传字段=不改原值。
- TMDB 密钥冷却时长：GET/PUT /scrape/tmdb-key-cooldowns 读写一致、≥1 秒、保存即失效设置缓存。
全部离线：store 用内存桩，ORM 用链式假 Session，不做真实网络与数据库请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest

from backend.emby_server import play_sign
from backend.integrations import store as _store


class _MemStore:
    """store 的内存桩（含失效记录，供断言用）"""

    def __init__(self):
        self.rows = {}
        self.invalidated = []

    def get_value(self, db, key, default="", **kwargs):
        return self.rows.get(key, default)

    def write_values(self, db, values, descriptions=None):
        self.rows.update(values)

    def invalidate(self, *keys):
        self.invalidated.extend(keys)


class _FakeDB:
    """最小 ORM 假 Session：只支持 query(Model).filter(col == key).first() / add / commit"""

    def __init__(self):
        self._rows = {}  # key -> SimpleNamespace（持久对象，row.value 赋值会落盘）
        self.added = []
        self.committed = 0

    @property
    def config_rows(self):
        return {k: r.value for k, r in self._rows.items()}

    @config_rows.setter
    def config_rows(self, mapping):
        self._rows = {k: SimpleNamespace(key=k, value=v, description="") for k, v in mapping.items()}

    def query(self, model):
        db = self

        class Q:
            def __init__(self):
                self._key = None

            def filter(self, cond):
                try:
                    self._key = cond.right.value
                except Exception:  # noqa: BLE001
                    self._key = None
                return self

            def first(self):
                return db._rows.get(self._key)

        return Q()

    def add(self, row):
        self.added.append(row)
        key = getattr(row, "key", None)
        if key is not None:
            self._rows[key] = SimpleNamespace(
                key=key, value=getattr(row, "value", ""), description="")

    def commit(self):
        self.committed += 1

    def expire_all(self):
        pass


@pytest.fixture()
def memstore(monkeypatch):
    ms = _MemStore()
    monkeypatch.setattr(_store, "get_value", ms.get_value)
    monkeypatch.setattr(_store, "write_values", ms.write_values)
    monkeypatch.setattr(_store, "invalidate", ms.invalidate)
    return ms


@pytest.fixture()
def fakedb():
    return _FakeDB()


def _staff():
    return SimpleNamespace(id=1, is_superuser=True, username="admin")


# ---------- 防盗链 Referer 白名单 ----------


class TestAllowedReferers:
    def test_write_normalizes_and_dedups(self, fakedb):
        hosts = play_sign.write_allowed_referers(
            fakedb, "https://CDN.Example.com/x, example.com , example.com, ,")
        assert hosts == ["cdn.example.com", "example.com"]
        assert fakedb.config_rows[play_sign.CONFIG_ALLOWED_REFERERS] == "cdn.example.com,example.com"

    def test_write_empty_clears(self, fakedb):
        fakedb.config_rows = {play_sign.CONFIG_ALLOWED_REFERERS: "a.com"}
        hosts = play_sign.write_allowed_referers(fakedb, "  , ")
        assert hosts == []
        assert fakedb.config_rows[play_sign.CONFIG_ALLOWED_REFERERS] == ""

    def test_get_returns_list_and_defaults_empty(self, fakedb):
        assert play_sign.get_allowed_referers(fakedb) == []
        fakedb.config_rows = {play_sign.CONFIG_ALLOWED_REFERERS: "b.com,a.com"}
        assert play_sign.get_allowed_referers(fakedb) == ["b.com", "a.com"]

    def test_config_payload_includes_referers(self, fakedb, memstore):
        fakedb.config_rows = {play_sign.CONFIG_ALLOWED_REFERERS: "c.com"}
        payload = play_sign.config_payload(fakedb)
        assert payload["allowed_referers"] == ["c.com"]

    def test_hotlink_put_without_referers_keeps_old(self, fakedb, memstore):
        from backend.api.admin_economy import HotlinkConfigRequest, hotlink_update_config

        fakedb.config_rows = {play_sign.CONFIG_ALLOWED_REFERERS: "old.com"}
        # 老前端：不带 allowed_referers 字段（缺省 None）
        req = HotlinkConfigRequest(enabled=True, play_sign_ttl=900, strm_sig_ttl=3600)
        assert req.allowed_referers is None
        hotlink_update_config(req, _staff(), fakedb)
        assert fakedb.config_rows[play_sign.CONFIG_ALLOWED_REFERERS] == "old.com"

    def test_hotlink_put_roundtrip_referers(self, fakedb, memstore):
        from backend.api.admin_economy import HotlinkConfigRequest, hotlink_update_config

        req = HotlinkConfigRequest(
            enabled=True, play_sign_ttl=900, strm_sig_ttl=3600,
            allowed_referers="NEW.com, other.com, NEW.com")
        result = hotlink_update_config(req, _staff(), fakedb)
        assert result["allowed_referers"] == ["new.com", "other.com"]
        assert fakedb.config_rows[play_sign.CONFIG_ALLOWED_REFERERS] == "new.com,other.com"

    def test_referer_check_respects_whitelist(self, fakedb):
        def _req(referer):
            return SimpleNamespace(
                headers={"referer": referer}, url=SimpleNamespace(path="/x"))

        # 白名单为空 = 不检查，放行
        play_sign.check_referer(_req("https://evil.com/x"), fakedb)
        # 命中白名单 = 放行
        fakedb.config_rows = {play_sign.CONFIG_ALLOWED_REFERERS: "good.com"}
        play_sign.check_referer(_req("https://good.com/v.m3u8"), fakedb)
        # 不在白名单 = 403
        import fastapi

        with pytest.raises(fastapi.HTTPException) as exc:
            play_sign.check_referer(_req("https://evil.com/x"), fakedb)
        assert exc.value.status_code == 403


# ---------- TMDB 密钥冷却时长 ----------


class TestTmdbKeyCooldowns:
    def test_get_returns_effective_and_defaults(self, fakedb, memstore):
        from backend.api.admin_scrape import get_tmdb_key_cooldowns

        res = get_tmdb_key_cooldowns(_staff(), fakedb)
        assert res["cooldown_sec"] == 900
        assert res["invalid_cooldown_sec"] == 21600
        assert res["defaults"]["cooldown_sec"] == 900
        assert res["defaults"]["invalid_cooldown_sec"] == 21600

    def test_put_roundtrip(self, fakedb, memstore):
        from backend.api.admin_scrape import (
            TmdbKeyCooldownsSaveRequest,
            get_tmdb_key_cooldowns,
            save_tmdb_key_cooldowns,
        )
        from backend.emby_server import tmdb as _tmdb

        req = TmdbKeyCooldownsSaveRequest(cooldown_sec=300, invalid_cooldown_sec=3600)
        res = save_tmdb_key_cooldowns(req, _staff(), fakedb)
        assert res["success"] is True
        assert memstore.rows[_tmdb.TMDB_KEY_COOLDOWN_CONFIG_KEY] == "300.0"
        assert memstore.rows[_tmdb.TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY] == "3600.0"
        # 读侧直查 DB（refresh=True 绕过 store 缓存）：把写入落盘后与写侧一致
        #（真实环境是同一张 SystemConfig 表，这里把内存桩的行同步进假 DB）
        fakedb.config_rows = {
            _tmdb.TMDB_KEY_COOLDOWN_CONFIG_KEY: "300.0",
            _tmdb.TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY: "3600.0",
        }
        got = get_tmdb_key_cooldowns(_staff(), fakedb)
        assert got["cooldown_sec"] == 300
        assert got["invalid_cooldown_sec"] == 3600

    def test_put_rejects_below_one_second(self, fakedb, memstore):
        from fastapi import HTTPException

        from backend.api.admin_scrape import (
            TmdbKeyCooldownsSaveRequest,
            save_tmdb_key_cooldowns,
        )

        with pytest.raises(HTTPException) as exc:
            save_tmdb_key_cooldowns(
                TmdbKeyCooldownsSaveRequest(cooldown_sec=0.5, invalid_cooldown_sec=10),
                _staff(), fakedb)
        assert exc.value.status_code == 400

    def test_db_value_honored_without_env(self, fakedb, memstore):
        from backend.emby_server import tmdb as _tmdb

        # _config_text 经 store 读（refresh=True 绕过 store 的 TTL 缓存、直取底层行）
        memstore.rows[_tmdb.TMDB_KEY_COOLDOWN_CONFIG_KEY] = "300.0"
        got = _tmdb.settings(fakedb, refresh=True)
        assert got["cooldown_sec"] == 300
        # 未配的另一键回落默认
        assert got["invalid_cooldown_sec"] == _tmdb.DEFAULT_KEY_INVALID_COOLDOWN_SEC

    def test_env_wins_over_db(self, fakedb, memstore, monkeypatch):
        from backend.emby_server import tmdb as _tmdb

        monkeypatch.setenv("TMDB_KEY_COOLDOWN_SEC", "120")
        memstore.rows[_tmdb.TMDB_KEY_COOLDOWN_CONFIG_KEY] = "300.0"
        got = _tmdb.settings(fakedb, refresh=True)
        assert got["cooldown_sec"] == 120

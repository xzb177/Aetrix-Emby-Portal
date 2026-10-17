"""批 1（刮削瘦身）：豆瓣降为可选开关，默认关闭。

覆盖：
1. ``douban.enabled(db)`` 在无配置行时返回 False（默认关）；
2. ``douban.enabled(db)`` 读配置抛异常时返回 False（fail-closed）；
3. 三个豆瓣调用点在开关关闭时短路、零豆瓣请求：
   a. 多源引擎的豆瓣源（``metasources/engine.py`` collect）；
   b. enrich 轻量豆瓣入口（``altmeta.search``，``enrich_worker.py``）；
   c. 豆瓣 rich 兜底（``_douban_fallback_on_tmdb_miss``）。
4. 开关开启时三处恢复原行为（回归）。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from types import SimpleNamespace
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.integrations import store
from backend.emby_server import douban as dbn
from backend.emby_server import altmeta
from backend.emby_server import enrich_worker as ew
from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import engine as ms_engine

init_db()


@pytest.fixture(autouse=True)
def _env():
    dbn.reset_ban()
    ew._douban_cfg_cache["at"] = 0.0
    ew._douban_cfg_cache["cfg"] = None
    ms_config.invalidate()
    # 每测默认关：显式写 0，消除测试间污染（尤其统一兜底链 flag——
    # 其它测试文件可能把它写开并残留，残留时 harness 会走统一路径而非
    # legacy else 分支）。
    db = SessionLocal()
    try:
        store.write_values(
            db,
            {dbn.DOUBAN_ENABLED_KEY: "0",
             ew.CFG_UNIFIED_FALLBACK: "0",
             ew.CFG_DOUBAN_BAN_DOWNGRADE: "0"},
            {dbn.DOUBAN_ENABLED_KEY: "批1测试",
             ew.CFG_UNIFIED_FALLBACK: "批1测试",
             ew.CFG_DOUBAN_BAN_DOWNGRADE: "批1测试"})
        db.commit()
    finally:
        db.close()
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)
    store.invalidate(ew.CFG_UNIFIED_FALLBACK)
    store.invalidate(ew.CFG_DOUBAN_BAN_DOWNGRADE)
    yield
    dbn.reset_ban()
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)
    store.invalidate(ew.CFG_UNIFIED_FALLBACK)
    store.invalidate(ew.CFG_DOUBAN_BAN_DOWNGRADE)


def _fresh_session_without_config_row():
    """全新空库会话（system_configs 里连豆瓣开关行都没有）：验证无配置行时的默认。"""
    import backend.models  # noqa: F401 — 注册 Base 模型
    import backend.emby_server.models  # noqa: F401
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = _ce(f"sqlite:///{path}")
    _dbmod.Base.metadata.create_all(bind=eng)
    return _sm(bind=eng)()


def _write_switch(on: bool):
    db = SessionLocal()
    try:
        store.write_values(db, {dbn.DOUBAN_ENABLED_KEY: "1" if on else "0"},
                           {dbn.DOUBAN_ENABLED_KEY: "批1测试"})
        db.commit()
    finally:
        db.close()
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)
    ew._douban_cfg_cache["at"] = 0.0
    ew._douban_cfg_cache["cfg"] = None


def _douban_only_snapshot():
    return ms_config.Snapshot(
        enabled=True, prefer_chinese=True, tmdb_miss_fallback=False,
        sources=[ms_config.SourceConfig(
            id="douban", label="豆瓣", enabled=True,
            requires_key=False, lang="zh")],
    )


def _series_item(**kw):
    defaults = dict(
        id=1, library_id=1, item_type="series", name="测试剧集",
        file_path=None, size=0, container="", production_year=2024,
        poster_path=None, primary_image_url=None, imdb_id=None, aliases=None,
        tmdb_id=None, overview=None, genres=None, metadata_locked=False,
        metadata_source=None, enrich_status="enriching", enrich_attempts=0,
        repair_requested_at=None, season_number=None, episode_number=None,
        series_id=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestSwitchDefault:
    def test_enabled_default_off_without_config(self):
        """无配置行时 enabled() 返回 False（默认关）。"""
        db = _fresh_session_without_config_row()
        try:
            assert dbn.enabled(db) is False
        finally:
            db.close()

    def test_enabled_fail_closed_on_store_error(self):
        """读配置抛异常时 enabled() 返回 False（fail-closed，不打豆瓣）。"""
        with mock.patch.object(store, "get_value", side_effect=RuntimeError("boom")):
            assert dbn.enabled(object()) is False

    def test_enabled_on_when_explicitly_opened(self):
        """显式写开后 enabled() 返回 True。"""
        _write_switch(True)
        db = SessionLocal()
        try:
            assert dbn.enabled(db) is True
        finally:
            db.close()


class TestEngineSourceGated:
    def test_switch_off_engine_skips_douban_source(self):
        """调用点 a：开关关闭时引擎跳过豆瓣源，零豆瓣请求。"""
        calls = []
        db = SessionLocal()
        try:
            with mock.patch.object(altmeta, "search",
                                   side_effect=lambda *a, **k: calls.append(1) or None):
                res = ms_engine.collect(db, "测试剧集", 2024, "series",
                                        snapshot=_douban_only_snapshot())
        finally:
            db.close()
        assert calls == [], "开关关闭时引擎必须零豆瓣请求"
        outcomes = {o.source: o for o in res.outcomes}
        assert outcomes["douban"].skipped == "豆瓣开关已关闭，已跳过"
        assert outcomes["douban"].hit is False

    def test_switch_on_engine_runs_douban_source(self):
        """开关开启时引擎豆瓣源恢复原行为。"""
        _write_switch(True)
        calls = []
        db = SessionLocal()
        try:
            with mock.patch.object(altmeta, "search",
                                   side_effect=lambda *a, **k: calls.append(1) or None):
                res = ms_engine.collect(db, "测试剧集", 2024, "series",
                                        snapshot=_douban_only_snapshot())
        finally:
            db.close()
        assert len(calls) == 1
        outcomes = {o.source: o for o in res.outcomes}
        assert outcomes["douban"].skipped == ""

    def test_switch_off_trial_collect_not_blocked(self):
        """后台手动试采集（only=douban）在开关关闭时也不拦。"""
        calls = []
        with mock.patch.object(altmeta, "search",
                               side_effect=lambda *a, **k: calls.append(1) or None):
            ms_engine.collect(None, "测试剧集", 2024, "series", only="douban",
                              ignore_switch=True,
                              snapshot=_douban_only_snapshot())
        assert len(calls) == 1


class TestLightweightEntryGated:
    def _run_post(self, douban_search_calls, bangumi_calls):
        """跑 _enrich_fetch_tmdb_post 的轻量兜底分支（走 else 分支）。"""
        from backend.emby_server import tmdb as tmdb_mod

        class _Prog:
            def note_stage(self, s):
                pass

        with mock.patch.object(type(tmdb_mod.tmdb_client), "configured",
                               new_callable=mock.PropertyMock,
                               return_value=True), \
                mock.patch.object(ew, "_douban_fallback_on_tmdb_miss",
                                  return_value=False), \
                mock.patch.object(ew, "_collect_multisource",
                                  return_value=False), \
                mock.patch.object(ew, "_should_multisource_on_tmdb_miss",
                                  return_value=False), \
                mock.patch.object(ew, "progress", _Prog()), \
                mock.patch.object(altmeta, "search",
                                  side_effect=lambda *a, **k: douban_search_calls.append(1) or None), \
                mock.patch.object(altmeta, "search_bangumi",
                                  side_effect=lambda *a, **k: bangumi_calls.append(1) or None), \
                mock.patch.object(ew, "_cached_altmeta_cfg",
                                  return_value=(True, 1.0, 1.0)), \
                mock.patch.object(tmdb_mod, "prewarm_images", return_value=[]), \
                mock.patch.object(tmdb_mod, "cast_list", return_value=[]):
            return ew._enrich_fetch_tmdb_post(
                {"tmdb_hit": None, "tmdb_details": None, "ok": True,
                 "error": None, "nfo_data": None},
                _series_item(),
                {"kind": "series", "needs_repair": True,
                 "tmdb_plan": {"op": "search", "name": "测试剧集",
                               "year": 2024, "kind": "series"}},
                batch_out={"hit": None, "details": None, "transient": False})

    def test_switch_off_lightweight_douban_search_skipped(self):
        """调用点 b：开关关闭时轻量豆瓣入口（altmeta.search）不打出豆瓣请求，
        Bangumi 兜底照常。"""
        douban_calls, bangumi_calls = [], []
        self._run_post(douban_calls, bangumi_calls)
        assert douban_calls == [], "开关关闭时轻量豆瓣搜索必须零调用"
        assert len(bangumi_calls) == 1, "Bangumi 兜底不受豆瓣开关影响"

    def test_switch_on_lightweight_douban_search_runs(self):
        """开关开启时轻量豆瓣入口恢复原行为。"""
        _write_switch(True)
        douban_calls, bangumi_calls = [], []
        self._run_post(douban_calls, bangumi_calls)
        assert len(douban_calls) == 1


class TestRichFallbackGated:
    def test_switch_off_rich_fallback_returns_false(self):
        """调用点 c：开关关闭时豆瓣 rich 兜底直接返回 False，不发请求。"""
        result = {}
        out = ew._douban_fallback_on_tmdb_miss(_series_item(), "series", result)
        assert out is False
        assert "_douban_fallback_tried" not in result

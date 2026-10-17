"""链路规划 Step 0.5：豆瓣封禁自动降级

flag：enrich_douban_ban_downgrade（SystemConfig，默认 0=关）。
flag 开 + 豆瓣封禁中（ban_remaining()>0）时：
  1. 预搜索不再标 douban_banned 延后，TMDB 照常跑；
  2. 豆瓣 rich 兜底直接跳过——封禁期零豆瓣请求（douban.py client 与
     altmeta.search 两个方向都断言）；
  3. 多源引擎的豆瓣源被跳过（outcome 记 "豆瓣封禁中，已跳过"）；
  4. 多源/TMDB 命中从严：标题+年份都对上才落库，宁可 none 不乱认。
flag 关时行为与现状一致；解封后自动恢复（逐条运行时判定）。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

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

FLAG = ew.CFG_DOUBAN_BAN_DOWNGRADE


@pytest.fixture(autouse=True)
def _env():
    dbn.reset_ban()
    ew._douban_cfg_cache["at"] = 0.0
    ew._douban_cfg_cache["cfg"] = None
    db = SessionLocal()
    try:
        store.write_values(db, {FLAG: "0", dbn.DOUBAN_ENABLED_KEY: "0"},
                           {FLAG: "Step 0.5 测试开关",
                            dbn.DOUBAN_ENABLED_KEY: "批1测试：豆瓣开关默认关"})
        db.commit()
    finally:
        db.close()
    store.invalidate(FLAG)
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)
    yield
    dbn.reset_ban()
    store.invalidate(FLAG)
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)


def _flag_on():
    db = SessionLocal()
    try:
        store.write_values(db, {FLAG: "1"},
                           {FLAG: "Step 0.5 测试开关"})
        db.commit()
    finally:
        db.close()
    store.invalidate(FLAG)


def _ban():
    dbn._trip_ban("step05-test")


def _douban_switch(on: bool):
    """批 1（刮削瘦身）：豆瓣降为可选开关——引擎的豆瓣源与轻量豆瓣入口
    只在开关开启时真正发请求。需要豆瓣方向发请求的测试先把开关写开。"""
    db = SessionLocal()
    try:
        store.write_values(db, {dbn.DOUBAN_ENABLED_KEY: "1" if on else "0"},
                           {dbn.DOUBAN_ENABLED_KEY: "批1测试：豆瓣开关"})
        db.commit()
    finally:
        db.close()
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)
    ew._douban_cfg_cache["at"] = 0.0
    ew._douban_cfg_cache["cfg"] = None


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


def _tmdb_configured():
    """让 tmdb_client.configured 为 True（property，需 PropertyMock）。"""
    from backend.emby_server import tmdb as tmdb_mod
    return mock.patch.object(type(tmdb_mod.tmdb_client), "configured",
                             new_callable=mock.PropertyMock, return_value=True)


# ============ 1. 降级激活判定 ============

class TestDowngradeActive:
    def test_flag_off_never_active(self):
        _ban()
        assert ew._douban_ban_downgrade_active() is False

    def test_flag_on_but_not_banned_not_active(self):
        _flag_on()
        assert ew._douban_ban_downgrade_active() is False

    def test_flag_on_and_banned_active(self):
        _flag_on()
        _ban()
        assert ew._douban_ban_downgrade_active() is True

    def test_unban_auto_recovers(self):
        _flag_on()
        _ban()
        assert ew._douban_ban_downgrade_active() is True
        dbn.reset_ban()  # 解封
        assert ew._douban_ban_downgrade_active() is False


# ============ 2. 预搜索：不再整条延后 ============

class TestPresearchSkip:
    def test_flag_off_ban_still_delays(self):
        """flag 关：封禁期行为与现状一致——标 douban_banned、TMDB 预搜索跳过。

        批 1：预搜索跳过的条件与豆瓣 rich 兜底触发口径一致（含"豆瓣兜底开着"），
        开关关闭时封禁机制自然休眠；本用例钉住"豆瓣开"时的行为，显式写开。"""
        _douban_switch(True)
        _ban()
        item = _series_item()
        with _tmdb_configured():
            result, tmdb_ctx = ew._enrich_fetch_pre(item)
        assert result.get("douban_banned") is True
        assert result.get("ok") is False
        assert tmdb_ctx["tmdb_plan"] == {"op": "none"}

    def test_switch_off_ban_no_delay_tmdb_runs(self):
        """批 1：豆瓣开关关闭时，封禁期也不整条延后——TMDB 预搜索照常跑
        （封禁机制随开关关闭自然休眠，条目不再"注定走豆瓣兜底→banned→延后"）。"""
        _ban()
        item = _series_item()
        with _tmdb_configured():
            result, tmdb_ctx = ew._enrich_fetch_pre(item)
        assert not result.get("douban_banned")
        assert result.get("ok") is not False
        assert tmdb_ctx["tmdb_plan"].get("op") == "search"

    def test_flag_on_ban_no_delay_tmdb_runs(self):
        """flag 开 + 封禁：不再延后，TMDB 预搜索照常跑。"""
        _flag_on()
        _ban()
        item = _series_item()
        with _tmdb_configured():
            result, tmdb_ctx = ew._enrich_fetch_pre(item)
        assert not result.get("douban_banned")
        assert result.get("ok") is not False
        assert tmdb_ctx["tmdb_plan"].get("op") == "search"

    def test_flag_on_no_ban_unchanged(self):
        """flag 开但未封禁：行为=现状（预搜索该跑跑）。"""
        _flag_on()
        item = _series_item()
        with _tmdb_configured():
            result, tmdb_ctx = ew._enrich_fetch_pre(item)
        assert not result.get("douban_banned")
        assert tmdb_ctx["tmdb_plan"].get("op") == "search"


# ============ 3. 兜底：跳过豆瓣 rich，零豆瓣请求 ============

def _post_ctx():
    return {"kind": "series", "needs_repair": False,
            "tmdb_plan": {"op": "search", "name": "测试剧集",
                          "year": 2024, "kind": "series"}}


def _run_post(result, item, tmdb_ctx, douban_calls, altmeta_calls,
              ms_guarded_ret=True):
    from backend.emby_server import tmdb as tmdb_mod
    stages = []

    class _Prog:
        def note_stage(self, s):
            stages.append(s)

    with _tmdb_configured(), \
            mock.patch.object(ew, "_douban_fallback_on_tmdb_miss",
                              side_effect=lambda *a: douban_calls.append(1) or "banned"), \
            mock.patch.object(ew, "_should_multisource_on_tmdb_miss",
                              return_value=True), \
            mock.patch.object(ew, "_collect_multisource_guarded",
                              side_effect=lambda *a: ms_guarded_ret), \
            mock.patch.object(ew, "progress", _Prog()), \
            mock.patch.object(altmeta, "search",
                              side_effect=lambda *a, **k: altmeta_calls.append(1)), \
            mock.patch.object(dbn.client, "search",
                              side_effect=lambda *a, **k: douban_calls.append(1)), \
            mock.patch.object(tmdb_mod, "prewarm_images", return_value=[]), \
            mock.patch.object(tmdb_mod, "cast_list", return_value=[]):
        out = ew._enrich_fetch_tmdb_post(result, item, tmdb_ctx,
                                         batch_out={"hit": None, "details": None,
                                                    "transient": False})
    return out, stages


class TestFallbackDowngrade:
    def test_flag_on_ban_skips_douban_fallback_zero_requests(self):
        """封禁期降级：豆瓣 rich 兜底一次都不调；豆瓣两个请求方向计数=0；
        走多源兜底（enrich_multisource_fallback 打点）。"""
        _flag_on()
        _ban()
        douban_calls, altmeta_calls = [], []
        item = _series_item()
        result = {"tmdb_hit": None, "tmdb_details": None,
                  "ok": True, "error": None, "nfo_data": None}
        out, stages = _run_post(result, item, _post_ctx(),
                                douban_calls, altmeta_calls)
        # rich 兜底函数一次都没被调
        assert len(douban_calls) == 0, "封禁期豆瓣 rich 兜底必须零调用"
        # 轻量 altmeta.search（豆瓣方向）零调用
        assert len(altmeta_calls) == 0, "封禁期轻量豆瓣搜索必须零调用"
        # 多源兜底分支被走到
        assert "enrich_multisource_fallback" in stages
        assert out.get("_ban_downgrade_skipped_douban") is True
        assert not out.get("douban_banned")

    def test_flag_off_ban_still_calls_douban_fallback(self):
        """flag 关：封禁期行为=现状——rich 兜底被调用（返回 banned→整条延后）。"""
        _ban()
        douban_calls, altmeta_calls = [], []
        item = _series_item()
        result = {"tmdb_hit": None, "tmdb_details": None,
                  "ok": True, "error": None, "nfo_data": None}
        out, stages = _run_post(result, item, _post_ctx(),
                                douban_calls, altmeta_calls)
        assert len(douban_calls) == 1
        assert out.get("douban_banned") is True
        assert out.get("ok") is False


# ============ 4. 多源引擎：封禁期跳过豆瓣源 ============

def _douban_only_snapshot():
    return ms_config.Snapshot(
        enabled=True, prefer_chinese=True, tmdb_miss_fallback=False,
        sources=[ms_config.SourceConfig(
            id="douban", label="豆瓣", enabled=True,
            requires_key=False, lang="zh")],
    )


class TestEngineBanAware:
    def test_banned_skips_douban_source_zero_requests(self):
        """封禁期：collect 跳过豆瓣源，outcome 记 skipped，零豆瓣请求。"""
        _ban()
        _douban_switch(True)
        db = SessionLocal()
        try:
            calls = []
            with mock.patch.object(altmeta, "search",
                                   side_effect=lambda *a, **k: calls.append(1)):
                res = ms_engine.collect(db, "测试剧集", 2024, "series",
                                        snapshot=_douban_only_snapshot())
        finally:
            db.close()
        assert calls == [], "封禁期多源引擎必须零豆瓣请求"
        outcomes = {o.source: o for o in res.outcomes}
        assert "douban" in outcomes
        assert outcomes["douban"].skipped == "豆瓣封禁中，已跳过"
        assert outcomes["douban"].hit is False
        assert res.any_hit is False

    def test_not_banned_douban_source_runs(self):
        """未封禁：豆瓣源正常被问（行为=现状）。"""
        _douban_switch(True)
        db = SessionLocal()
        try:
            calls = []
            with mock.patch.object(altmeta, "search",
                                   side_effect=lambda *a, **k: calls.append(1) or None):
                res = ms_engine.collect(db, "测试剧集", 2024, "series",
                                        snapshot=_douban_only_snapshot())
        finally:
            db.close()
        assert len(calls) == 1
        outcomes = {o.source: o for o in res.outcomes}
        assert outcomes["douban"].skipped == ""

    def test_switch_off_engine_skips_douban_source(self):
        """批 1：豆瓣开关关闭时，引擎内豆瓣源直接跳过，零豆瓣请求。"""
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

    def test_manual_probe_only_bypasses_ban_skip(self):
        """后台手动试采集（only=douban）不拦——那是管理员手动点的。"""
        _ban()
        calls = []
        with mock.patch.object(altmeta, "search",
                               side_effect=lambda *a, **k: calls.append(1) or None):
            ms_engine.collect(None, "测试剧集", 2024, "series", only="douban",
                              ignore_switch=True,
                              snapshot=_douban_only_snapshot())
        assert len(calls) == 1

    def test_manual_probe_only_bypasses_switch_off(self):
        """后台手动试采集（only=douban）在豆瓣开关关闭时也不拦——管理员手动点的。"""
        calls = []
        with mock.patch.object(altmeta, "search",
                               side_effect=lambda *a, **k: calls.append(1) or None):
            ms_engine.collect(None, "测试剧集", 2024, "series", only="douban",
                              ignore_switch=True,
                              snapshot=_douban_only_snapshot())
        assert len(calls) == 1


# ============ 5. 从严匹配 ============

class TestStrictMatch:
    @pytest.mark.parametrize("iname,iyear,ctitle,cyear,ok", [
        ("测试剧集", 2024, "测试剧集", 2024, True),        # 完全一致
        ("测试剧集", 2024, "测试剧集 第一季", 2024, True),  # 前缀
        ("测试剧集 第一季", 2024, "测试剧集", 2024, True),  # 反向前缀
        ("测试剧集", 2024, "测试 剧集", 2024, True),        # 清洗后一致
        ("测试剧集", 2024, "别的剧名", 2024, False),        # 标题不对
        ("测试剧集", 2024, "测试剧集", 2023, False),        # 年份不对
        ("测试剧集", None, "测试剧集", 2024, True),         # 条目无年份不否决
        ("测试剧集", 2024, "测试剧集", None, True),         # 候选无年份不否决
        ("测试剧集", 2024, "", 2024, False),               # 候选无标题否决
        ("", 2024, "测试剧集", 2024, False),               # 条目无标题否决
        ("落语朱音", 2024, "朱音落语", 2024, False),        # 易位不算
    ])
    def test_strict_hit_ok(self, iname, iyear, ctitle, cyear, ok):
        assert ew._ban_downgrade_strict_hit_ok(iname, iyear, ctitle, cyear) is ok

    def test_multisource_strict_reject_drops_result(self):
        """降级期多源归并标题/年份对不上 → 丢弃并视为未命中。"""
        _flag_on()
        _ban()
        fake_collected = ms_engine.CollectResult(
            fields={"title": "错的剧名", "year": 2023},
            external_ids={}, outcomes=[], prefer_chinese=True)
        item = _series_item(name="测试剧集", production_year=2024)
        result = {}
        with mock.patch.object(ms_engine, "collect",
                               return_value=fake_collected), \
                mock.patch.object(ms_config, "read_config",
                                  return_value=ms_config.Snapshot(
                                      enabled=True, prefer_chinese=True,
                                      sources=[])):
            ok = ew._collect_multisource(item, "series", result)
        assert ok is False
        assert "multisource" not in result
        assert result.get("multisource_strict_rejected") is True

    def test_multisource_strict_pass_keeps_result(self):
        """标题+年份都对上 → 正常落库。"""
        _flag_on()
        _ban()
        fake_collected = ms_engine.CollectResult(
            fields={"title": "测试剧集", "year": 2024},
            external_ids={"bangumi": "123"}, outcomes=[], prefer_chinese=True)
        item = _series_item(name="测试剧集", production_year=2024)
        result = {}
        with mock.patch.object(ms_engine, "collect",
                               return_value=fake_collected), \
                mock.patch.object(ms_config, "read_config",
                                  return_value=ms_config.Snapshot(
                                      enabled=True, prefer_chinese=True,
                                      sources=[])):
            ok = ew._collect_multisource(item, "series", result)
        assert ok is True
        assert result["multisource"]["fields"]["title"] == "测试剧集"

    def test_tmdb_hit_strict_reject_drops_hit(self):
        """降级期 TMDB search 命中标题/年份对不上 → 丢弃命中走多源。"""
        _flag_on()
        _ban()
        from backend.emby_server import tmdb as tmdb_mod

        stages = []

        class _Prog:
            def note_stage(self, s):
                stages.append(s)

        item = _series_item(name="测试剧集", production_year=2024)
        result = {"tmdb_hit": None, "tmdb_details": None,
                  "ok": True, "error": None, "nfo_data": None}
        bad_hit = {"id": 999, "title": "错的剧名", "name": "错的剧名",
                   "release_date": "2020-05-01"}
        with _tmdb_configured(), \
                mock.patch.object(ew, "_douban_fallback_on_tmdb_miss",
                                  return_value=False), \
                mock.patch.object(ew, "_should_multisource_on_tmdb_miss",
                                  return_value=True), \
                mock.patch.object(ew, "_collect_multisource_guarded",
                                  return_value=True), \
                mock.patch.object(ew, "progress", _Prog()), \
                mock.patch.object(tmdb_mod, "prewarm_images", return_value=[]), \
                mock.patch.object(tmdb_mod, "cast_list", return_value=[]):
            out = ew._enrich_fetch_tmdb_post(
                result, item, _post_ctx(),
                batch_out={"hit": dict(bad_hit), "details": {"id": 999},
                           "transient": False})
        assert out.get("tmdb_hit") is None, "从严丢弃：错标题 TMDB 命中必须被丢掉"
        assert out.get("tmdb_strict_rejected") is True
        assert "enrich_multisource_fallback" in stages

    def test_tmdb_hit_strict_pass_keeps_hit(self):
        """标题+年份都对上 → TMDB 命中保留，不走多源。"""
        _flag_on()
        _ban()
        from backend.emby_server import tmdb as tmdb_mod

        stages = []

        class _Prog:
            def note_stage(self, s):
                stages.append(s)

        item = _series_item(name="测试剧集", production_year=2024)
        result = {"tmdb_hit": None, "tmdb_details": None,
                  "ok": True, "error": None, "nfo_data": None}
        good_hit = {"id": 1001, "title": "测试剧集", "name": "测试剧集",
                    "release_date": "2024-03-01"}
        with _tmdb_configured(), \
                mock.patch.object(ew, "_douban_fallback_on_tmdb_miss",
                                  return_value=False), \
                mock.patch.object(ew, "_should_multisource_on_tmdb_miss",
                                  return_value=True), \
                mock.patch.object(ew, "_collect_multisource_guarded",
                                  side_effect=AssertionError("不应走到多源")), \
                mock.patch.object(ew, "progress", _Prog()), \
                mock.patch.object(tmdb_mod, "prewarm_images", return_value=[]), \
                mock.patch.object(tmdb_mod, "cast_list", return_value=[]):
            out = ew._enrich_fetch_tmdb_post(
                result, item, _post_ctx(),
                batch_out={"hit": dict(good_hit), "details": {"id": 1001},
                           "transient": False})
        assert out.get("tmdb_hit", {}).get("id") == 1001
        assert not out.get("tmdb_strict_rejected")
        assert "enrich_multisource_fallback" not in stages


# ============ 6. 后台 API ============

class TestAdminApi:
    def test_get_put_roundtrip(self):
        from backend.api import admin_scrape
        db = SessionLocal()
        try:
            staff = SimpleNamespace(is_superuser=True)
            got = admin_scrape.get_douban_ban_downgrade(staff=staff, db=db)
            assert got["success"] is True
            assert got["enabled"] is False  # 默认关
            assert got["banned"] is False
            saved = admin_scrape.save_douban_ban_downgrade(
                admin_scrape._DoubanBanDowngradeBody(enabled=True),
                staff=staff, db=db)
            assert saved["enabled"] is True
            # 开关写进 SystemConfig 后 worker 读得到
            store.invalidate(FLAG)
            assert ew._douban_ban_downgrade_flag_on() is True
            saved2 = admin_scrape.save_douban_ban_downgrade(
                admin_scrape._DoubanBanDowngradeBody(enabled=False),
                staff=staff, db=db)
            assert saved2["enabled"] is False
        finally:
            db.close()
        store.invalidate(FLAG)

    def test_get_shows_ban_status(self):
        from backend.api import admin_scrape
        _ban()
        db = SessionLocal()
        try:
            got = admin_scrape.get_douban_ban_downgrade(
                staff=SimpleNamespace(is_superuser=True), db=db)
            assert got["banned"] is True
            assert got["ban_remaining_sec"] > 0
        finally:
            db.close()

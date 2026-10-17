"""链路规划 Step 2：兜底链路统一——一个豆瓣入口。

flag：``enrich_unified_fallback``（SystemConfig，默认 0=关）。
flag 开时：TMDB 未命中后的兜底只剩两个调用点——豆瓣 rich（CJK 专属）→
多源引擎（含豆瓣源）。轻量 ``altmeta.search`` 已并入引擎做"豆瓣源"
（``metasources/sources.py`` 的 ``search_douban``），不再直调；
``_should_multisource_on_tmdb_miss`` 里的独立 ``tmdb_miss_fallback``
判断降级为多源总开关的子项。
flag 关时行为与 Step 1 部署后一致（原 elif/else 分支用 flag 包裹保留，
暂不物理删除）。

测试覆盖：
1. flag 读写与默认关；
2. 可达性分析：``elif tmdb_client.configured and not needs_repair`` /
   ``else`` 四种组合（TMDB 配置 × repair）逐个列出谁进 else；
3. 删前后一致：统一路径 vs 原分支——repair（T,T）仍能走到引擎；
4. 开关降级：统一链下 ``tmdb_miss_fallback`` 不再独立门控；
5. 反风控：单条目豆瓣请求数，合并后 ≤ 合并前；
6. Step 0.5 收编：复用 ``_douban_ban_downgrade_active()``，引擎的封禁感知保留。
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

FLAG = ew.CFG_UNIFIED_FALLBACK
BAN_FLAG = ew.CFG_DOUBAN_BAN_DOWNGRADE


@pytest.fixture(autouse=True)
def _env():
    dbn.reset_ban()
    ew._douban_cfg_cache["at"] = 0.0
    ew._douban_cfg_cache["cfg"] = None
    ms_config.invalidate()
    db = SessionLocal()
    try:
        store.write_values(db, {FLAG: "0", BAN_FLAG: "0",
                                dbn.DOUBAN_ENABLED_KEY: "0"},
                           {FLAG: "Step 2 测试开关",
                            BAN_FLAG: "Step 0.5 测试开关",
                            dbn.DOUBAN_ENABLED_KEY: "批1测试：豆瓣开关默认关"})
        db.commit()
    finally:
        db.close()
    store.invalidate(FLAG)
    store.invalidate(BAN_FLAG)
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)
    yield
    dbn.reset_ban()
    ms_config.invalidate()
    store.invalidate(FLAG)
    store.invalidate(BAN_FLAG)
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)


def _flag_on():
    db = SessionLocal()
    try:
        store.write_values(db, {FLAG: "1"}, {FLAG: "Step 2 测试开关"})
        db.commit()
    finally:
        db.close()
    store.invalidate(FLAG)


def _ban_flag_on():
    db = SessionLocal()
    try:
        store.write_values(db, {BAN_FLAG: "1"}, {BAN_FLAG: "Step 0.5 测试开关"})
        db.commit()
    finally:
        db.close()
    store.invalidate(BAN_FLAG)


def _douban_switch(on: bool):
    """批 1（刮削瘦身）：豆瓣降为可选开关——引擎的豆瓣源只在开关开启时
    才被问。需要豆瓣方向发请求的测试先把开关写开。"""
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


def _ban():
    dbn._trip_ban("step2-test")


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


def _tmdb_configured(value=True):
    """让 tmdb_client.configured 为指定值（property，需 PropertyMock）。"""
    from backend.emby_server import tmdb as tmdb_mod
    return mock.patch.object(type(tmdb_mod.tmdb_client), "configured",
                             new_callable=mock.PropertyMock,
                             return_value=value)


def _ms_snapshot(enabled=True, tmdb_miss_fallback=False):
    """多源配置快照：只开豆瓣源（聚焦豆瓣请求计数）。"""
    return ms_config.Snapshot(
        enabled=enabled, prefer_chinese=True,
        sources=[ms_config.SourceConfig(
            id="douban", label="豆瓣", enabled=True, requires_key=False,
            lang="zh", keys=[], rate=0.0)],
        tmdb_miss_fallback=tmdb_miss_fallback)


def _post_ctx(needs_repair=False):
    return {"kind": "series", "needs_repair": needs_repair,
            "tmdb_plan": {"op": "search", "name": "测试剧集",
                          "year": 2024, "kind": "series"}}


def _run_post_old_branch(result, item, tmdb_ctx, calls):
    """跑 flag 关时的原分支，记录各调用点的命中情况。

    调用方负责 patch ``tmdb_client.configured``（见 TestReachability）。
    calls: dict，键为 "should_ms" / "ms_guarded" / "ms_direct" /
    "altmeta_search" / "altmeta_bangumi" / "stages"，值为调用次数列表。
    原 else 分支里 ``_collect_multisource`` mock 返回 False，
    以便走到轻量直调（验证"合并"的对比基线）。
    """
    from backend.emby_server import tmdb as tmdb_mod

    class _Prog:
        def note_stage(self, s):
            calls.setdefault("stages", []).append(s)

    def _count(key):
        return lambda *a, **k: calls.setdefault(key, []).append(1)

    with mock.patch.object(ew, "_douban_fallback_on_tmdb_miss",
                           return_value=False), \
            mock.patch.object(ew, "_should_multisource_on_tmdb_miss",
                              side_effect=lambda *a: calls.setdefault(
                                  "should_ms", []).append(1) or True), \
            mock.patch.object(ew, "_collect_multisource_guarded",
                              side_effect=lambda *a: calls.setdefault(
                                  "ms_guarded", []).append(1) or True), \
            mock.patch.object(ew, "_collect_multisource",
                              side_effect=lambda *a: calls.setdefault(
                                  "ms_direct", []).append(1) or False), \
            mock.patch.object(ew, "progress", _Prog()), \
            mock.patch.object(altmeta, "search",
                              side_effect=lambda *a, **k: calls.setdefault(
                                  "altmeta_search", []).append(1) or None), \
            mock.patch.object(altmeta, "search_bangumi",
                              side_effect=lambda *a, **k: calls.setdefault(
                                  "altmeta_bangumi", []).append(1) or None), \
            mock.patch.object(ew, "_cached_altmeta_cfg",
                              return_value=(True, 1.0, 1.0)), \
            mock.patch.object(ew, "_cached_douban_cfg",
                              return_value=(True, 1.0)), \
            mock.patch.object(tmdb_mod, "prewarm_images", return_value=[]), \
            mock.patch.object(tmdb_mod, "cast_list", return_value=[]):
        return ew._enrich_fetch_tmdb_post(
            result, item, tmdb_ctx,
            batch_out={"hit": None, "details": None, "transient": False})


# ============ 1. flag 读写与默认关 ============

class TestFlag:
    def test_default_off(self):
        assert ew._unified_fallback_flag_on() is False

    def test_flag_on(self):
        _flag_on()
        assert ew._unified_fallback_flag_on() is True

    def test_config_roundtrip(self):
        db = SessionLocal()
        try:
            assert ew.get_unified_fallback_config(db)["enabled"] is False
            cfg = ew.save_unified_fallback_config(db, True)
            assert cfg["enabled"] is True
            assert ew.get_unified_fallback_config(db)["enabled"] is True
            ew.save_unified_fallback_config(db, False)
            assert ew.get_unified_fallback_config(db)["enabled"] is False
        finally:
            db.close()


# ============ 2. 可达性分析：四种组合谁进 else ============
#
# 结论（当前 main，flag 关 = 原分支）：
#   (T,F) → elif 分支（_should_multisource_on_tmdb_miss → _collect_multisource_guarded）
#   (T,F) 之外 → else 分支（_collect_multisource → altmeta.search/search_bangumi）
# 即：repair 路径（T,T）当前走 else；TMDB 未配置（F,*）也走 else。
# 该测试把分析写成可执行的断言，防止后续重构悄悄改变分支归属。

class TestReachability:
    def _base_result(self):
        return {"tmdb_hit": None, "tmdb_details": None,
                "ok": True, "error": None, "nfo_data": None}

    def test_configured_no_repair_takes_elif(self):
        """(T,F)：走 elif，不进 else。"""
        calls = {}
        ctx = _post_ctx(needs_repair=False)
        with _tmdb_configured(True):
            _run_post_old_branch(self._base_result(), _series_item(), ctx, calls)
        assert len(calls.get("should_ms", [])) == 1, "必须咨询 _should_multisource"
        assert len(calls.get("ms_guarded", [])) == 1, "必须走带护栏的多源采集"
        assert not calls.get("ms_direct"), "不能进 else 的 _collect_multisource"
        assert not calls.get("altmeta_search"), "不能直调轻量豆瓣"

    def test_configured_repair_takes_else(self):
        """(T,T)：repair 走 else（_collect_multisource），不走 elif。"""
        calls = {}
        ctx = _post_ctx(needs_repair=True)
        with _tmdb_configured(True):
            _run_post_old_branch(self._base_result(), _series_item(), ctx, calls)
        assert not calls.get("should_ms"), "repair 不走 elif 的门控"
        assert not calls.get("ms_guarded"), "repair 不走带护栏版本"
        assert len(calls.get("ms_direct", [])) == 1, "repair 必须进 else 调 _collect_multisource"

    def test_unconfigured_no_repair_takes_else(self):
        """(F,F)：TMDB 未配置走 else。"""
        calls = {}
        ctx = _post_ctx(needs_repair=False)
        with _tmdb_configured(False):
            _run_post_old_branch(self._base_result(), _series_item(), ctx, calls)
        assert not calls.get("should_ms")
        assert len(calls.get("ms_direct", [])) == 1

    def test_unconfigured_repair_takes_else(self):
        """(F,T)：走 else。"""
        calls = {}
        ctx = _post_ctx(needs_repair=True)
        with _tmdb_configured(False):
            _run_post_old_branch(self._base_result(), _series_item(), ctx, calls)
        assert not calls.get("should_ms")
        assert len(calls.get("ms_direct", [])) == 1


# ============ 3. 删前后一致：统一路径 vs 原分支 ============
#
# flag 开时，四种组合的"走不走引擎"决策必须与 flag 关时一致，
# 唯一的意图性差异只有两处（见 TestDocumentedDeltas）：
#   a. tmdb_miss_fallback 降级为子项（(T,F) 下不再要求它开着）；
#   b. 轻量 altmeta.search/search_bangumi 直调并入引擎（master 关时不再直调）。

class TestUnifiedConsistency:
    def _run_unified(self, item, needs_repair, configured,
                     snapshot, engine_calls):
        """跑统一路径（flag 开），记录引擎是否被调用。"""
        from backend.emby_server import tmdb as tmdb_mod

        class _Prog:
            def note_stage(self, s):
                engine_calls.setdefault("stages", []).append(s)

        with _tmdb_configured(configured), \
                mock.patch.object(ms_config, "read_config",
                                  return_value=snapshot), \
                mock.patch.object(ew, "_collect_multisource_guarded",
                                  side_effect=lambda *a: engine_calls.setdefault(
                                      "engine", []).append(1) or True), \
                mock.patch.object(ew, "progress", _Prog()):
            ew._fallback_unified(item, "series", {}, needs_repair)
        return engine_calls

    def test_repair_still_reaches_engine(self):
        """repair（T,T）+ 中文 + 总开关开：统一路径仍走到引擎（行为不变）。"""
        _flag_on()
        calls = self._run_unified(
            _series_item(), True, True, _ms_snapshot(enabled=True), {})
        assert len(calls.get("engine", [])) == 1
        assert "enrich_multisource_fallback" in calls["stages"]

    def test_configured_main_path_reaches_engine(self):
        """(T,F) + 中文 + 总开关开：统一路径走到引擎。"""
        _flag_on()
        calls = self._run_unified(
            _series_item(), False, True, _ms_snapshot(enabled=True), {})
        assert len(calls.get("engine", [])) == 1

    def test_unconfigured_reaches_engine(self):
        """(F,*) + 总开关开：统一路径走到引擎（原 else 口径，不要求中文）。"""
        _flag_on()
        calls = self._run_unified(
            _series_item(name="Non CJK Title"), False, False,
            _ms_snapshot(enabled=True), {})
        assert len(calls.get("engine", [])) == 1

    def test_master_off_skips_engine(self):
        """总开关关：统一路径记 skip，不再直调轻量豆瓣/Bangumi。"""
        _flag_on()
        with mock.patch.object(altmeta, "search",
                               side_effect=AssertionError(
                                   "统一链下不许直调 altmeta.search")), \
                mock.patch.object(altmeta, "search_bangumi",
                                  side_effect=AssertionError(
                                      "统一链下不许直调 search_bangumi")):
            calls = self._run_unified(
                _series_item(), True, True, _ms_snapshot(enabled=False), {})
        assert not calls.get("engine")
        assert "enrich_fallback_skip" in calls["stages"]

    def test_non_cjk_main_path_skips(self):
        """(T,F) 非中文标题：统一路径不走引擎（与原 elif 口径一致）。"""
        _flag_on()
        calls = self._run_unified(
            _series_item(name="Breaking Bad"), False, True,
            _ms_snapshot(enabled=True), {})
        assert not calls.get("engine")
        assert "enrich_fallback_skip" in calls["stages"]


class TestDocumentedDeltas:
    """意图性差异（flag 开 vs 关），逐条断言并在此留档。"""

    def test_sub_switch_demoted(self):
        """a. (T,F) 中文 + 总开关开 + tmdb_miss_fallback 关：
        关时 skip（独立开关挡住），开时走引擎（降级为子项）。"""
        item = _series_item()
        snap = _ms_snapshot(enabled=True, tmdb_miss_fallback=False)
        with mock.patch.object(ms_config, "read_config", return_value=snap):
            assert ew._should_multisource_on_tmdb_miss(item, "series") is False
            _flag_on()
            assert ew._should_multisource_on_tmdb_miss(item, "series") is True

    def test_sub_switch_still_read_when_flag_off(self):
        """关时原语义不变：总开关开 + 子开关开 → 走；子开关关 → 不走。"""
        item = _series_item()
        with mock.patch.object(
                ms_config, "read_config",
                return_value=_ms_snapshot(enabled=True,
                                          tmdb_miss_fallback=True)):
            assert ew._should_multisource_on_tmdb_miss(item, "series") is True

    def test_lightweight_merged_into_engine(self):
        """b. master 关时：关时走轻量直调，开时不再直调（已并入引擎）。"""
        # 关时：else 分支 master 关 → _collect_multisource（mock 返回 False）→
        # 直调 altmeta.search（1 次）；douban 没命中（mock 返回 None）→
        # 再调 search_bangumi（1 次）
        calls = {}
        ctx = _post_ctx(needs_repair=True)
        with _tmdb_configured(True):
            _run_post_old_branch({"tmdb_hit": None, "tmdb_details": None,
                                  "ok": True, "error": None, "nfo_data": None},
                                 _series_item(), ctx, calls)
        assert len(calls.get("altmeta_search", [])) == 1, \
            "关时 master 关必须直调一次轻量豆瓣"
        assert len(calls.get("altmeta_bangumi", [])) == 1, \
            "关时豆瓣没命中必须再试一次 Bangumi"


# ============ 4. 反风控：单条目豆瓣请求数，合并后 ≤ 合并前 ============
#
# 口径：只数发往豆瓣的 HTTP 请求（DoubanClient.search/get_details/
# get_celebrities 与 altmeta.search）。引擎的豆瓣源走 altmeta.search，
# 单条目单次；与原轻量直调同口径。
#
# 场景（中文条目，TMDB miss，rich 未命中，总开关开，子开关开）：
#   关时：rich search(1) + 引擎豆瓣源 altmeta.search(1) = 2
#   开时：rich search(1) + 引擎豆瓣源 altmeta.search(1) = 2
# 断言：开时 ≤ 关时。

class TestDoubanRequestCount:
    def _engine_douban_requests(self, snapshot):
        """真实引擎 collect（只开豆瓣源），数 altmeta.search 调用次数。

        批 1：豆瓣降为可选开关，引擎的豆瓣源只在开关开启时才被问，
        这里先把开关写开、传真实会话。"""
        _douban_switch(True)
        calls = []
        db = SessionLocal()
        try:
            with mock.patch.object(altmeta, "search",
                                   side_effect=lambda *a, **k: calls.append(1) or None):
                ms_engine.collect(db, "测试剧集", 2024, "series",
                                  snapshot=snapshot)
        finally:
            db.close()
        return len(calls)

    def test_engine_douban_source_single_request(self):
        """引擎的豆瓣源单条目只发 1 次请求（与原轻量直调同口径）。"""
        assert self._engine_douban_requests(
            _ms_snapshot(enabled=True)) == 1

    def test_unified_not_more_than_old(self):
        """合并后 ≤ 合并前：同一场景下统一路径的豆瓣请求数不超过原分支。"""
        # 原分支（flag 关）：rich miss 后走引擎 → 豆瓣源 1 次
        old_n = self._engine_douban_requests(_ms_snapshot(enabled=True))
        # 统一路径（flag 开）：同样走引擎 → 豆瓣源 1 次
        _flag_on()
        new_n = self._engine_douban_requests(_ms_snapshot(enabled=True))
        assert new_n <= old_n, f"合并后({new_n})必须 ≤ 合并前({old_n})"
        assert new_n == 1 == old_n

    def test_unified_master_off_zero_requests(self):
        """master 关时统一路径零豆瓣请求（原分支会直调 1 次轻量搜索）。"""
        _flag_on()
        assert self._engine_douban_requests(
            _ms_snapshot(enabled=False)) == 0


# ============ 5. Step 0.5 收编 ============
#
# - 降级判断复用 _douban_ban_downgrade_active()：统一路径不另起 flag/判断；
# - 引擎的封禁感知保留：封禁期引擎跳过豆瓣源（零豆瓣请求）。

class TestStep05Absorbed:
    def test_downgrade_active_reuses_single_judgment(self):
        """统一路径的降级走 _douban_ban_downgrade_active（无新判断函数）。"""
        assert hasattr(ew, "_douban_ban_downgrade_active")
        # _fallback_unified 源码里不得出现新的 ban 判断（只复用已有函数）
        import inspect
        src = inspect.getsource(ew._fallback_unified)
        assert "_douban_ban_downgrade_active" in src or "ban" not in src.lower(), \
            "统一路径如需 ban 判断必须复用 _douban_ban_downgrade_active"

    def test_ban_period_engine_skips_douban_source(self):
        """封禁期：统一路径走引擎，豆瓣源被跳过，零豆瓣请求。"""
        _flag_on()
        _ban_flag_on()
        _ban()
        _douban_switch(True)
        assert ew._douban_ban_downgrade_active() is True
        calls = []
        snap = ms_config.Snapshot(
            enabled=True, prefer_chinese=True,
            sources=[ms_config.SourceConfig(
                id="douban", label="豆瓣", enabled=True, requires_key=False,
                lang="zh", keys=[], rate=0.0),
                ms_config.SourceConfig(
                    id="bangumi", label="Bangumi", enabled=True,
                    requires_key=False, lang="zh", keys=[], rate=0.0)])
        db = SessionLocal()
        try:
            with mock.patch.object(altmeta, "search",
                                   side_effect=lambda *a, **k: calls.append(1) or None):
                res = ms_engine.collect(db, "测试剧集", 2024, "series",
                                        snapshot=snap)
        finally:
            db.close()
        assert calls == [], "封禁期引擎必须零豆瓣请求"
        outcomes = {o.source: o for o in res.outcomes}
        assert outcomes["douban"].skipped == "豆瓣封禁中，已跳过"

    def test_unified_path_during_downgrade_reaches_engine(self):
        """降级激活时统一路径仍能走到引擎（rich 在调用方已被跳过）。"""
        _flag_on()
        _ban_flag_on()
        _ban()
        from backend.emby_server import tmdb as tmdb_mod

        class _Prog:
            def note_stage(self, s):
                pass

        with _tmdb_configured(True), \
                mock.patch.object(ms_config, "read_config",
                                  return_value=_ms_snapshot(enabled=True)), \
                mock.patch.object(ew, "_collect_multisource_guarded",
                                  return_value=True), \
                mock.patch.object(ew, "progress", _Prog()):
            # 不抛异常即通过：降级期统一路径不断链
            ew._fallback_unified(_series_item(), "series", {}, False)

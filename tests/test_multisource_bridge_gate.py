"""多源桥 tmdb 候选复核门回归（2026-11-04，跟进 PR #1838）。

事故续集：#1838 的取数复核门拦下直搜候选 1784692 后，多源归并引擎
又把同一个编号经 extract_binding 的 multisource 桥以
confidence=search 提交落账（生产 worker 日志 + stage record 证据
binding_source=multisource 实证 4 条）。引擎的 TMDB 腿与直搜同走
tmdb_client.search，无精确条目时同样退化，桥候选落进 fetched 前
必须过同一道门：编号详情 → 现行认亲门 + search 标题核验；不过只
摘 tmdb 键、其余来源不连坐；判不出也不许放行未复核的 tmdb 编号。
"""
from types import SimpleNamespace
from unittest import mock

from backend.emby_server import enrich_worker as ew
from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import engine as ms_engine
from backend.emby_server.metasources.engine import CollectResult
from backend.emby_server.tmdb import TmdbTransientError

_XIANNI_DETAILS = {
    "id": 1784692,
    "title": "仙逆院线剧场版",
    "original_title": "仙逆院线剧场版:七式斩天",
    "release_date": "",
}


def _item(**over):
    base = dict(
        id=1, name="测试电影", item_type="movie", library_id=66,
        production_year=None, tmdb_id=None, imdb_id=None,
        enrich_status="pending")
    base.update(over)
    return SimpleNamespace(**base)


class _DummySession:
    def close(self):
        pass


class _FakeTmdbClient:
    configured = True

    def __init__(self, details_by_id=None, exc=None):
        self._details_by_id = details_by_id or {}
        self._exc = exc
        self.calls = []

    def details(self, tmdb_id, kind):
        self.calls.append((tmdb_id, kind))
        if self._exc is not None:
            raise self._exc
        return self._details_by_id.get(str(tmdb_id))


def _run_collect(item, collected, fake_client):
    result: dict = {}
    with mock.patch.object(ew, "SessionLocal", lambda: _DummySession()), \
            mock.patch.object(ms_config, "read_config",
                              return_value=SimpleNamespace(enabled=True)), \
            mock.patch.object(ms_engine, "collect",
                              return_value=collected), \
            mock.patch.object(ew, "_douban_ban_downgrade_active",
                              return_value=False), \
            mock.patch.object(tmdb_mod, "tmdb_client", fake_client):
        ok = ew._collect_multisource(item, "movie", result)
    return ok, result


def test_bridge_tmdb_title_mismatch_scrubbed_douban_kept():
    """事故复现：桥归并出 1784692（标题零交集）必须被摘，豆瓣键不连坐。"""
    item = _item(id=2243730, name="剧场版 超时空要塞F 虚空歌姬")
    collected = CollectResult()
    collected.external_ids = {"tmdb": "1784692", "douban": "30402939"}
    fake = _FakeTmdbClient({"1784692": dict(_XIANNI_DETAILS)})
    ok, result = _run_collect(item, collected, fake)
    assert ok is True
    assert result["multisource"]["external_ids"] == {"douban": "30402939"}
    assert result["multisource_tmdb_rejected"] == "title_mismatch"
    assert fake.calls == [("1784692", "movie")]


def test_bridge_tmdb_title_match_kept():
    """对照：桥候选与条目名有交集时照常保留，不误伤正常多源归并。"""
    item = _item(id=5150, name="剧场版 名侦探柯南 绯色的子弹")
    collected = CollectResult()
    collected.external_ids = {"tmdb": "81234"}
    fake = _FakeTmdbClient({"81234": {
        "id": 81234, "title": "名侦探柯南：绯色的子弹",
        "original_title": "名探偵コナン 緋色の弾丸",
        "release_date": "2021-04-16"}})
    ok, result = _run_collect(item, collected, fake)
    assert ok is True
    assert result["multisource"]["external_ids"] == {"tmdb": "81234"}
    assert "multisource_tmdb_rejected" not in result


def test_bridge_tmdb_same_as_existing_skips_review():
    """已绑同号不是新绑定：不复核、不动（详情接口一次都不许调）。"""
    item = _item(id=5151, name="剧场版 名侦探柯南 绯色的子弹",
                 tmdb_id="81234", enrich_status="done")
    collected = CollectResult()
    collected.external_ids = {"tmdb": "81234"}
    fake = _FakeTmdbClient(exc=AssertionError("不许调详情"))
    ok, result = _run_collect(item, collected, fake)
    assert ok is True
    assert result["multisource"]["external_ids"] == {"tmdb": "81234"}
    assert fake.calls == []


def test_bridge_tmdb_details_transient_scrubbed():
    """详情瞬态失败判不出：摘除留痕，本轮不落未复核编号，下轮可重采。"""
    item = _item(id=5152, name="剧场版 超时空要塞F 虚空歌姬")
    collected = CollectResult()
    collected.external_ids = {"tmdb": "1784692", "douban": "30402939"}
    fake = _FakeTmdbClient(exc=TmdbTransientError("boom"))
    ok, result = _run_collect(item, collected, fake)
    assert ok is True
    assert result["multisource"]["external_ids"] == {"douban": "30402939"}
    assert result["multisource_tmdb_rejected"] == "details_transient"

"""#14 补全：Frodo 桥的 IMDb 编号输入口径（清单第 14 项）。

背景：Frodo 桥模块与 IMDb 桥接本身已在 #1241 / 引擎接力里落地，
本单补的是输入缺口——补资料段与聚合通道此前只看条目列里的
imdb_id：条目只有 tmdb_id、而本轮已采集证据（tmdb_details / NFO）
里已有 imdb_id 时，桥走不上、退回片名搜。本单让桥用上本轮已有
的编号（不新增任何上游请求，不为搭桥另拉 TMDB 详情）。

关键不变量：
1. 编号解析顺序：条目已有 > tmdb_details > nfo_data（与
   extract_binding 的身份口径一致）；
2. 不传 fetched 时行为与此前逐字一致（片名搜兜底不变）；
3. 开关与熔断 / 封禁闸一律不动：直连腿仍受独立 Frodo 开关管，
   聚合腿仍受聚合开关管。
"""
import os
from types import SimpleNamespace

# 直调后端模块：先钉死 sqlite，与其它 DB 相关测试文件同一 guard 口径。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import identify_two_stage as ts
from backend.emby_server.metasources import zh_aggregate as zh

_SUBJECT = {
    "id": "1292052", "title": "肖申克的救赎",
    "overview": "豆瓣简介", "rating": 9.7,
    "image": "/douban_poster.jpg", "genres": ["犯罪", "剧情"],
}


def _item(**overrides):
    base = dict(
        id=1, name="肖申克的救赎", production_year=1994,
        tmdb_id=None, imdb_id=None,
        overview=None, community_rating=None,
        poster_path=None, backdrop_path=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeDB:
    """只够 apply_fill 走完 _upsert_stage_record 的桩：查不到即新建行。"""

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def add(self, *args, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _gates_on(monkeypatch):
    monkeypatch.setattr(
        "backend.emby_server.metasources.breaker.allow", lambda p: True)
    monkeypatch.setattr(
        "backend.emby_server.douban.ban_remaining", lambda: 0)


def _bridge(monkeypatch):
    """把 Frodo 桥换成记调用的假实现，返回固定 subject；片名搜禁调。"""
    calls = []

    def fake_find(imdb_id, year=None):
        calls.append(imdb_id)
        return dict(_SUBJECT)

    monkeypatch.setattr(
        "backend.emby_server.metasources.douban_frodo.find_by_imdb",
        fake_find)
    monkeypatch.setattr(
        "backend.emby_server.metasources.douban_frodo.search_by_title",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("不该退回片名搜")))
    return calls


def test_details_imdb_bridges_when_item_lacks_it(monkeypatch):
    """条目无 imdb_id、详情里有 → 走精确桥，不退回片名搜。"""
    calls = _bridge(monkeypatch)
    item = _item(tmdb_id="278")
    got = ts._frodo_candidates(
        item, {"tmdb_details": {"imdb_id": "tt0111161"}})
    assert calls == ["tt0111161"]
    assert got["overview"] == "豆瓣简介"
    assert got["community_rating"] == 9.7
    assert got["poster_path"] == "/douban_poster.jpg"


def test_item_imdb_wins_over_details(monkeypatch):
    """条目已有编号时详情里的不抢：条目优先。"""
    calls = _bridge(monkeypatch)
    item = _item(tmdb_id="278", imdb_id="tt0903747")
    ts._frodo_candidates(
        item, {"tmdb_details": {"imdb_id": "tt0111161"}})
    assert calls == ["tt0903747"]


def test_nfo_imdb_used_when_item_and_details_lack_it(monkeypatch):
    """条目与详情都没有时用 NFO 里的编号。"""
    calls = _bridge(monkeypatch)
    item = _item(tmdb_id="278")
    ts._frodo_candidates(
        item, {"tmdb_details": {}, "nfo_data": {"imdb_id": "tt0111161"}})
    assert calls == ["tt0111161"]


def test_no_imdb_anywhere_falls_back_to_title_search(monkeypatch):
    """三处都没有编号时退回片名搜（此前行为不变）。"""
    monkeypatch.setattr(
        "backend.emby_server.metasources.douban_frodo.find_by_imdb",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("没有编号不该调桥")))
    monkeypatch.setattr(
        "backend.emby_server.metasources.douban_frodo.search_by_title",
        lambda title, year=None, kind="": dict(_SUBJECT))
    item = _item(tmdb_id="278")
    got = ts._frodo_candidates(item, {"tmdb_details": {}})
    assert got["overview"] == "豆瓣简介"


def test_fill_from_frodo_passes_fetched(monkeypatch):
    """直连腿：fill_from_frodo 把 fetched 透传给内层。"""
    monkeypatch.setattr(ts, "IDENTIFY_FRODO_FILL_ENABLED", 1)
    calls = _bridge(monkeypatch)
    item = _item(tmdb_id="278")
    got = ts.fill_from_frodo(
        item, {"tmdb_details": {"imdb_id": "tt0111161"}})
    assert calls == ["tt0111161"]
    assert got["overview"] == "豆瓣简介"


def test_aggregate_frodo_source_passes_fetched(monkeypatch):
    """聚合腿：聚合的 Frodo 源把 fetched 透传给内层。"""
    calls = _bridge(monkeypatch)
    item = _item(tmdb_id="278")
    got = zh._source_douban_frodo(
        item, {"tmdb_details": {"imdb_id": "tt0111161"}})
    assert calls == ["tt0111161"]
    assert got["overview"] == "豆瓣简介"


def test_apply_fill_threads_fetched_imdb(monkeypatch):
    """端到端：补资料段用详情里的编号走桥填上简介与评分。"""
    monkeypatch.setattr(ts, "IDENTIFY_FRODO_FILL_ENABLED", 1)
    calls = _bridge(monkeypatch)
    item = _item(tmdb_id="278")
    filled = ts.apply_fill(
        _FakeDB(), item, {"tmdb_details": {"imdb_id": "tt0111161"}})
    assert calls == ["tt0111161"]
    assert item.overview == "豆瓣简介"
    assert item.community_rating == 9.7
    assert "overview" in filled and "community_rating" in filled

"""豆瓣兜底刮削（altmeta）：只在 TMDB 搜不到时补，绝不覆盖已有数据。

实测价值：生产 `metadata_source='none'` 的样本里，豆瓣能找回多数
（电锯人→链锯人：刺客篇、落语朱音→朱音落语、B PROJECT→B-PROJECT）。

关键不变量：
1. 只兜底不覆盖——已有 TMDB 命中的条目一律不动；
2. 匹配要严——首条标题对不上就丢弃，宁可漏也不错配；
3. subject_suggest 没有简介/评分，所以补到标题/年份/海报就算完成，
   否则这批条目会永远停在 pending 反复重试。
"""
import json
import os
from types import SimpleNamespace

# 本文件直调 enrich_worker（会 import backend.database）：先钉死 sqlite，
# 否则当本文件是进程内第一个 import backend 的模块时，
# DATABASE_TYPE 会按默认冻成 postgresql，后续模块的 init_db() 全炸
#（其它 DB 相关测试文件都有这一 guard，本文件此前漏了）。
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import altmeta
from backend.emby_server import douban as dbn
from backend.emby_server import enrich_worker as ew
from backend.integrations import store


@pytest.fixture(autouse=True)
def _env():
    # 批 1：豆瓣开关 60s 进程缓存 + store 热缓存跨测试污染——每测重置。
    ew._douban_cfg_cache["at"] = 0.0
    ew._douban_cfg_cache["cfg"] = None
    ew._altmeta_cfg_cache["at"] = 0.0
    ew._altmeta_cfg_cache["cfg"] = None
    store.invalidate(dbn.DOUBAN_ENABLED_KEY)
    yield
    ew._douban_cfg_cache["at"] = 0.0
    ew._douban_cfg_cache["cfg"] = None


def _item(**kw):
    base = dict(tmdb_id=None, last_scraped_at=None, overview=None,
                community_rating=None, primary_image_url=None, poster_path=None,
                backdrop_path=None, backdrop_image_url=None, name="原名",
                production_year=None, metadata_source=None, douban_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _resp(monkeypatch, payload):
    class _R:
        status = 200

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(altmeta.urllib.request, "urlopen", lambda *a, **k: _R())


def test_search_returns_hit_when_title_matches(monkeypatch):
    _resp(monkeypatch, [{"title": "电锯人", "year": "2026", "type": "tv",
                         "id": "1", "img": "http://img/p.jpg"}])
    hit = altmeta.search("电锯人", 2026, "series", min_interval=0)
    assert hit and hit["title"] == "电锯人"
    assert hit["year"] == "2026"


def test_search_discards_mismatched_title(monkeypatch):
    """首条标题对不上必须丢弃——宁可漏也不错配"""
    _resp(monkeypatch, [{"title": "完全不相干的片子", "year": "2020", "type": "tv"}])
    assert altmeta.search("电锯人", 2020, "series", min_interval=0) is None


def test_search_discards_year_mismatch(monkeypatch):
    _resp(monkeypatch, [{"title": "同名剧", "year": "2015", "type": "tv"}])
    assert altmeta.search("同名剧", 2023, "series", min_interval=0) is None


def test_search_rejects_movie_for_series(monkeypatch):
    """类型不符（电影 vs 剧集）不能当成剧集写入"""
    _resp(monkeypatch, [{"title": "同名", "year": "2020", "type": "movie"}])
    assert altmeta.search("同名", 2020, "series", min_interval=0) is None


def test_search_empty_query_returns_none():
    assert altmeta.search("", None, "series", min_interval=0) is None
    assert altmeta.search(None, None, "series", min_interval=0) is None


def test_search_survives_network_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(altmeta.urllib.request, "urlopen", boom)
    assert altmeta.search("任意片名", None, "series", min_interval=0) is None


def test_apply_does_not_overwrite_tmdb_data():
    """已有 TMDB 名字/海报/简介时，豆瓣不得覆盖"""
    it = _item(name="用户整理的名字", production_year=2020, overview="原简介",
               poster_path="/keep.jpg")
    altmeta.apply(it, {"title": "豆瓣名", "year": "2026", "image": "http://douban/x.jpg"})
    assert it.name == "用户整理的名字", it.name
    assert it.production_year == 2020, it.production_year
    assert it.overview == "原简介"
    assert it.poster_path == "/keep.jpg", it.poster_path
    assert it.metadata_source == "douban"


def test_apply_fills_gaps_for_unmatched_item():
    """什么都没有的条目：补标题/年份/海报，并打上 douban 来源"""
    it = _item()
    altmeta.apply(it, {"title": "朱音落语", "year": "2026",
                       "image": "http://douban/p.jpg"})
    assert it.name == "朱音落语", it.name
    assert it.production_year == 2026
    assert it.primary_image_url == "http://douban/p.jpg", it.primary_image_url
    assert it.metadata_source == "douban"


def test_min_interval_reads_config():
    # 统一热读走一次 IN 查询 + .all()（backend.integrations.store.get_values）
    db = SimpleNamespace(query=lambda *a, **k: SimpleNamespace(
        filter=lambda *x, **y: SimpleNamespace(
            all=lambda: [(altmeta.CONFIG_RATE, "12.5")])))
    assert altmeta.min_interval(db) == 12.5


def test_fetch_uses_own_session_not_write_session(monkeypatch):
    """回归：_enrich_fetch 只能收 item，没有 db 参数。

    我第一版在这里调 `altmeta.enabled(db)`，而 db 在这个函数里根本不存在 →
    线上每条兜底条目都报 `name 'db' is not defined`，被 except 吞成补全失败。
    单测只测了 altmeta.search（全是 mock），没走这条真实调用路径，CI 也没抓到。

    这里直接调真实的 _enrich_fetch，确保它自己能跑通。
    """
    from types import SimpleNamespace
    from backend.emby_server import enrich_worker

    item = SimpleNamespace(
        id=1, item_type="series", name="没命中的剧", production_year=2020,
        file_path="mount://3/x/剧名 (2020)/Season 1/e01.mkv",
        poster_path=None, primary_image_url=None, imdb_id=None, aliases=None,
        repair_requested_at=None,
    )
    monkeypatch.setattr(enrich_worker, "_scanfile_from_item", lambda i: None)

    opened = []

    class _CfgDB:
        # Phase 6b 起多源配置也走短会话：开两个都关。两个断言都要成立——
        # 「每个都关了」是本用例的真正保护点，「至少开过一个」防用例本身失效。
        def close(self):
            opened.append("closed")

    monkeypatch.setattr(enrich_worker, "SessionLocal", lambda: _CfgDB())
    monkeypatch.setattr("backend.emby_server.altmeta.enabled", lambda db: True)
    # 批 1：豆瓣降为可选开关——本用例钉住「豆瓣开」时的保留分支。
    # 注意：只 mock douban.enabled 不够，_cached_douban_cfg 还会调真实的
    # min_interval（在 _CfgDB 假会话上抛 AttributeError），所以直接 mock
    # _cached_douban_cfg（与 test_douban_ban.py 等文件的既有做法一致）。
    monkeypatch.setattr(enrich_worker, "_cached_douban_cfg", lambda: (True, 0.0))
    # rich 兜底直接判没命中：本用例只钉轻量 altmeta.search，不依赖真实豆瓣 HTTP
    #（真实请求偶发 403 会触发进程级封禁冷却，导致用例抖动）。
    monkeypatch.setattr(enrich_worker, "_douban_fallback_on_tmdb_miss",
                        lambda *a, **k: False)
    monkeypatch.setattr("backend.emby_server.altmeta.min_interval", lambda db: 0.0)
    monkeypatch.setattr("backend.emby_server.altmeta.search",
                        lambda *a, **k: {"title": "兜底名", "year": "2020",
                                         "image": "http://d/p.jpg"})
    # v2.42.9 父级快速失败：TMDB 已配置且搜过无命中 → 跳过兑底（不再走这条）。
    # 兑底保留分支是「TMDB 未配置」（豆瓣是主数据源）——本回归改走这条。
    class _T:
        configured = False
        api_key = None

        def search(self, *a, **k):
            return None

        def details(self, *a, **k):
            return None

        def apply(self, *a, **k):
            return None

        def apply_details(self, *a, **k):
            return None

        def apply_images(self, *a, **k):
            return False

        def enrich(self, *a, **k):
            return None
    monkeypatch.setattr("backend.emby_server.tmdb.tmdb_client", _T())

    res = enrich_worker._enrich_fetch(item)

    assert res["ok"] is True, res
    assert "db" not in (res.get("error") or ""), res
    assert res.get("douban_hit", {}).get("title") == "兜底名", res
    assert opened and set(opened) == {"closed"}, "配置会话必须被关闭"


def test_fetch_fallback_runs_when_tmdb_unconfigured(monkeypatch):
    """父级快速失败的另一面：TMDB 未配置时兑底是主数据源，必须照走（钉住保留分支）"""
    from types import SimpleNamespace
    from backend.emby_server import enrich_worker

    item = SimpleNamespace(
        id=2, item_type="series", name="没命中的剧", production_year=2020,
        file_path="mount://3/x/剧名 (2020)/Season 1/e01.mkv",
        poster_path=None, primary_image_url=None, imdb_id=None, aliases=None,
        repair_requested_at=None,
    )
    monkeypatch.setattr(enrich_worker, "_scanfile_from_item", lambda i: None)

    class _CfgDB:
        def close(self):
            pass

    monkeypatch.setattr(enrich_worker, "SessionLocal", lambda: _CfgDB())
    monkeypatch.setattr("backend.emby_server.altmeta.enabled", lambda db: True)
    # 批 1：豆瓣降为可选开关——本用例钉住「豆瓣开」时的保留分支。
    # 注意：只 mock douban.enabled 不够，_cached_douban_cfg 还会调真实的
    # min_interval（在 _CfgDB 假会话上抛 AttributeError），所以直接 mock
    # _cached_douban_cfg（与 test_douban_ban.py 等文件的既有做法一致）。
    monkeypatch.setattr(enrich_worker, "_cached_douban_cfg", lambda: (True, 0.0))
    # rich 兜底直接判没命中：本用例只钉轻量 altmeta.search，不依赖真实豆瓣 HTTP
    #（真实请求偶发 403 会触发进程级封禁冷却，导致用例抖动）。
    monkeypatch.setattr(enrich_worker, "_douban_fallback_on_tmdb_miss",
                        lambda *a, **k: False)
    monkeypatch.setattr("backend.emby_server.altmeta.min_interval", lambda db: 0.0)
    douban_calls = []
    monkeypatch.setattr("backend.emby_server.altmeta.search",
                        lambda *a, **k: douban_calls.append(1)
                        or {"title": "兑底名", "year": "2020", "image": "http://d/p.jpg"})

    class _T:
        configured = False
        api_key = None
    monkeypatch.setattr("backend.emby_server.tmdb.tmdb_client", _T())

    res = enrich_worker._enrich_fetch(item)
    assert res.get("douban_hit", {}).get("title") == "兑底名", res
    assert douban_calls == [1], "TMDB 未配置时豆瓣兑底必须被调用"


def test_rate_is_seconds_between_calls_not_calls_per_minute():
    """altmeta_douban_rate 的语义是「两次请求最小间隔秒数」

    踩过的坑：最初实现成「每分钟最多 N 次」，配置里 rate=1.0 就变成每分钟 1 次，
    153 条要跑两个多小时，追新时新条目得等两小时才补上——慢到没有实用价值。
    现在 rate=1.0 = 每秒 1 次（60/分）。
    """
    import time as _t
    limiter = altmeta._RateLimiter()
    limiter.acquire(0)          # 第一次：不等待
    t0 = _t.time()
    limiter.acquire(0.2)        # 第二次：应至少间隔 0.2s
    waited = _t.time() - t0
    assert waited >= 0.15, f"未按最小间隔限速（等了 {waited:.3f}s）"


def test_zero_interval_disables_throttling():
    import time as _t
    limiter = altmeta._RateLimiter()
    t0 = _t.time()
    for _ in range(20):
        limiter.acquire(0)
    assert _t.time() - t0 < 1.0, "interval=0 时不应限速"


# ==================== Bangumi ====================

def _bgm_resp(monkeypatch, payload):
    class _R:
        status = 200

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import urllib.request
    monkeypatch.setattr(altmeta.urllib.request, "urlopen", lambda *a, **k: _R())


def test_bangumi_matches_on_chinese_name(monkeypatch):
    """Bangumi 的价值就在 name_cn —— TMDB 缺的中文剧集靠它"""
    _bgm_resp(monkeypatch, {"list": [
        {"id": 1, "type": 2, "name": "B-PROJECT～鼓動＊Ambition～",
         "name_cn": "B-PROJECT～鼓动＊Ambitious～", "air_date": "2016-04-07",
         "images": {"large": "http://bg/l.jpg"}}]})
    hit = altmeta.search_bangumi("B PROJECT～鼓动＊Ambitious～ (2016)", 2016,
                                 "series", min_interval=0)
    assert hit and hit["title"] == "B-PROJECT～鼓动＊Ambitious～"
    assert hit["image"] == "http://bg/l.jpg"
    assert hit["year"] == "2016"


def test_bangumi_rejects_same_franchise_siblings(monkeypatch):
    """「电锯人」会搜到「电锯人~温泉旅行篇」等同系列作品，必须拒绝"""
    _bgm_resp(monkeypatch, {"list": [
        {"id": 1, "type": 2, "name": "チェンソーマン", "name_cn": "电锯人~温泉旅行篇"},
        {"id": 2, "type": 2, "name": "チェンソーマン", "name_cn": "电锯人 舞台剧"}]})
    assert altmeta.search_bangumi("电锯人", 2022, "series", min_interval=0) is None


def test_bangumi_rejects_non_anime_type(monkeypatch):
    _bgm_resp(monkeypatch, {"list": [
        {"id": 1, "type": 6, "name": "Leatherface", "name_cn": "人皮脸"}]})
    assert altmeta.search_bangumi("人皮脸", None, "series", min_interval=0) is None


def test_bangumi_empty_query_and_network_error(monkeypatch):
    assert altmeta.search_bangumi("", None, "series", min_interval=0) is None

    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(altmeta.urllib.request, "urlopen", boom)
    assert altmeta.search_bangumi("任意", None, "series", min_interval=0) is None


def test_apply_marks_source_bangumi():
    it = _item()
    altmeta.apply(it, {"title": "X", "year": "2020", "image": "http://b/p.jpg"},
                  source="bangumi")
    assert it.metadata_source == "bangumi"
    assert it.primary_image_url == "http://b/p.jpg"

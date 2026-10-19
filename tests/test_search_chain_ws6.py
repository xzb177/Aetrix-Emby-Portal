"""WS6 搜索链根治：繁体候选、长名短查、年份后缀兜底、错配不放行。

测试全部走 monkeypatch（类属性，不碰单例实例属性；见 AGENTS 纪律），
_search_raw 整个被替换，无真实 HTTP、无 TMDB 密钥依赖。
"""

import pytest

from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.tmdb import (
    TmdbClient,
    _search_candidates,
    year_from_name_suffix,
)
from backend.emby_server.zh_s2t import to_traditional
from backend.emby_server import enrich_worker as ew


@pytest.fixture(autouse=True)
def _one_lang(monkeypatch):
    # 语言链钉死单语言：候选链的验证只与候选有关，不与语言数有关
    monkeypatch.setattr(
        tmdb_mod, "language_fallback_chain", lambda db=None: ["zh-CN"])


# ---------------- zh_s2t：繁简映射 ----------------

def test_to_traditional_common_chars():
    assert to_traditional("人生切割术") == "人生切割術"
    assert to_traditional("龙的传人") == "龍的傳人"
    assert to_traditional("ABC 123") == "ABC 123"  # 非中文透传
    assert to_traditional("") == ""


# ---------------- _search_candidates：繁体候选 ----------------

def test_traditional_variant_in_candidates():
    cands = [q for q, _ in _search_candidates("人生切割术")]
    assert "人生切割術" in cands


def test_traditional_variant_deduped_when_same():
    # 纯繁体/无简繁差异的查询不产生重复候选
    cands = [q for q, _ in _search_candidates("ABC 123")]
    assert len(cands) == len(set(cands))


# ---------------- search()：繁体命中 ----------------

def _raw_trad(self, query, year, kind, lang=None):
    if "術" in (query or ""):
        return [{
            "id": 873797, "name": "人生切割術", "original_name": "人生切割術",
            "first_air_date": "2022-02-18", "original_language": "zh",
            "popularity": 50,
        }]
    return []


def test_traditional_query_hits_when_simplified_misses(monkeypatch):
    monkeypatch.setattr(TmdbClient, "_search_raw", _raw_trad)
    hit = TmdbClient().search("人生切割术", 2022, "series")
    assert hit is not None and str(hit["id"]) == "873797"


# ---------------- search()：长名短查 ----------------

def _raw_short(self, query, year, kind, lang=None):
    if query == "青山不改":
        return [{
            "id": 424242, "name": "青山不改", "original_name": "青山不改",
            "first_air_date": "2019-05-01", "original_language": "zh",
            "popularity": 10,
        }]
    return []


def test_long_name_short_query_hits(monkeypatch):
    monkeypatch.setattr(TmdbClient, "_search_raw", _raw_short)
    hit = TmdbClient().search("青山不改 - 导演剪辑加长版", 2019, "series")
    assert hit is not None and str(hit["id"]) == "424242"


# ---------------- search()：错配不放行 ----------------

def _raw_mismatch(self, query, year, kind, lang=None):
    return [{
        "id": 999, "name": "老虎和兔子", "original_name": "TIGER & BUNNY",
        "first_air_date": "2011-04-03", "original_language": "ja",
        "popularity": 100,
    }]


def test_short_mismatch_not_accepted(monkeypatch):
    # 「虎子」不能配到「老虎和兔子」：Tier1 lcs<6、Tier1.5 字数<3、
    # Tier1.2 无年份 → 全拒，宁缺毋滥
    monkeypatch.setattr(TmdbClient, "_search_raw", _raw_mismatch)
    assert TmdbClient().search("虎子", None, "series") is None


# ---------------- year_from_name_suffix ----------------

def test_year_from_name_suffix():
    assert year_from_name_suffix("人生切割术 (2022)") == 2022
    assert year_from_name_suffix("黑钱（2018）") == 2018
    assert year_from_name_suffix("黑钱") is None
    assert year_from_name_suffix("Wonder Woman 1984") is None  # 裸年份不是后缀口径
    assert year_from_name_suffix(None) is None
    assert year_from_name_suffix("") is None


# ---------------- _tmdb_plan_for_item：年份后缀兜底 ----------------

class _FakeItem:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _plan_item(**kw):
    base = dict(name="x", production_year=None, poster_path=None,
                primary_image_url=None, tmdb_id=None)
    base.update(kw)
    return _FakeItem(**base)


def test_plan_year_from_name_suffix(monkeypatch):
    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    monkeypatch.setattr(ew, "_alias_tmdb_id", lambda name: None)
    item = _plan_item(name="人生切割术 (2022)")
    plan = ew._tmdb_plan_for_item(item, None, "series", needs_repair=False)
    assert plan["op"] == "search"
    assert plan["year"] == 2022
    assert plan["year_from_name_suffix"] is True


def test_plan_no_suffix_year_stays_none(monkeypatch):
    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    monkeypatch.setattr(ew, "_alias_tmdb_id", lambda name: None)
    item = _plan_item(name="黑钱")
    plan = ew._tmdb_plan_for_item(item, None, "series", needs_repair=False)
    assert plan["op"] == "search"
    assert plan["year"] is None
    assert plan["year_from_name_suffix"] is False


def test_plan_explicit_year_not_overridden(monkeypatch):
    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    monkeypatch.setattr(ew, "_alias_tmdb_id", lambda name: None)
    item = _plan_item(name="黑钱 (2017)", production_year=2018)
    plan = ew._tmdb_plan_for_item(item, None, "series", needs_repair=False)
    assert plan["year"] == 2018  # 字段值优先，后缀不覆盖
    assert plan["year_from_name_suffix"] is False


# ---------------- search()：后缀年份写错时的年份回退 ----------------

def _raw_yearfilter(self, query, year, kind, lang=None):
    if year == 2022:
        return []  # 带年份过滤零结果（后缀年份写错的情形）
    if year is None and query == "人生切割术":
        return [{
            "id": 95396, "name": "人生切割术", "original_name": "人生切割术",
            "first_air_date": "2022-02-18", "original_language": "zh",
            "popularity": 60,
        }]
    return []


def test_wrong_suffix_year_falls_back_to_no_year(monkeypatch):
    # 后缀年份喂错了也不比原来差：search 内置"带年份零结果→去年份重试"
    monkeypatch.setattr(TmdbClient, "_search_raw", _raw_yearfilter)
    hit = TmdbClient().search("人生切割术", 2022, "series")
    assert hit is not None and str(hit["id"]) == "95396"

"""豆瓣链接在 DTO 里只许以一种一致形态出现

回归背景：详情 DTO 同时在 ProviderIds 发 "Douban"、又在 _external_urls 发
Name="豆瓣"，SenPlayer 自己按 ProviderIds 拼一份（TMDB/IMDb 因同名被它去重），
再把 ExternalUrls 追加进来——豆瓣因中英文名对不上没被去重，详情页出现两个豆瓣。

修法（两全）：_external_urls 的豆瓣条目改名 "Douban"（与 ProviderIds 键一致，
按名去重的客户端能对上），URL 改用 canonical
https://movie.douban.com/subject/{id}/（按 URL 去重的客户端也能对上）；
只读 ExternalUrls 的客户端链接不丢。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace  # noqa: E402

from backend.emby_server import api as ea_api  # noqa: E402


def _item(**kw):
    base = dict(tmdb_id=None, imdb_id=None, douban_id=None, bangumi_id=None,
                name="测试剧", item_type="series", metadata_source="tmdb")
    base.update(kw)
    return SimpleNamespace(**base)


def _names(urls):
    return [u["Name"] for u in urls]


class TestDoubanExternalUrl:
    def test_douban_single_consistent_form(self):
        item = _item(tmdb_id="100", imdb_id="tt100", douban_id="12345")
        urls = ea_api._external_urls(item)
        douban_entries = [u for u in urls if "ouban" in u["Name"].lower()]
        assert len(douban_entries) == 1, f"豆瓣条目必须恰好 1 个: {urls}"
        assert douban_entries[0]["Name"] == "Douban"
        assert douban_entries[0]["Url"] == "https://movie.douban.com/subject/12345/"

    def test_douban_search_fallback_consistent_name(self):
        item = _item(name="无ID剧")  # 无豆瓣 id 的剧 → 搜索兜底
        urls = ea_api._external_urls(item)
        douban_entries = [u for u in urls if "ouban" in u["Name"].lower()]
        assert len(douban_entries) == 1
        assert douban_entries[0]["Name"] == "Douban"
        assert "search_text=" in douban_entries[0]["Url"]

    def test_no_chinese_douban_name(self):
        item = _item(douban_id="12345")
        urls = ea_api._external_urls(item)
        assert "豆瓣" not in _names(urls), "ExternalUrls 里不许再出现中文名'豆瓣'"

    def test_others_unchanged(self):
        item = _item(tmdb_id="100", imdb_id="tt100", douban_id="1", bangumi_id="2")
        urls = ea_api._external_urls(item)
        by_name = {u["Name"]: u["Url"] for u in urls}
        assert by_name["TMDB"] == "https://www.themoviedb.org/tv/100"
        assert by_name["IMDb"] == "https://www.imdb.com/title/tt100/"
        assert by_name["Bangumi"] == "https://bgm.tv/subject/2"

    def test_movie_kind_still_canonical(self):
        item = _item(item_type="movie", douban_id="999")
        urls = ea_api._external_urls(item)
        douban_entries = [u for u in urls if u["Name"] == "Douban"]
        assert len(douban_entries) == 1
        assert douban_entries[0]["Url"] == "https://movie.douban.com/subject/999/"

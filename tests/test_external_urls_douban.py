"""豆瓣链接在 ExternalUrls 里整条不发（2026-10-16 用户拍板）

历史：详情 DTO 在 ExternalUrls 发豆瓣（先中文名「豆瓣」、#949 改名「Douban」
+ canonical /subject/ 形），SenPlayer 链接区仍出现两个豆瓣。亲验实锤
（《巨轮》id 671981，douban_id=None）：服务端下发里只有一个 Douban、
ProviderIds 里连 Douban 键都没有，SenPlayer 依旧显示「豆瓣、TMDB、Douban」
——排第一的「豆瓣」是 SenPlayer 自带的入口，与下发无关。用户拍板：
服务端整条不发，只留 SenPlayer 自己的那个。

本文件的口径随之翻转：ExternalUrls 里不许再出现任何豆瓣条目（有 id 的
直达链接、无 id 的搜索兜底都不发）；TMDB/IMDb/Bangumi 不受影响；
ProviderIds 的 Douban 键是编号元数据，照常保留。
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


class TestDoubanNotInExternalUrls:
    def test_no_douban_entry_with_id(self):
        item = _item(tmdb_id="100", imdb_id="tt100", douban_id="12345")
        urls = ea_api._external_urls(item)
        assert not any("ouban" in n.lower() for n in _names(urls)), urls
        assert "豆瓣" not in _names(urls)

    def test_no_douban_search_fallback(self):
        item = _item(name="无ID剧")  # 无豆瓣 id 的剧 → 以前会发搜索兜底
        urls = ea_api._external_urls(item)
        assert not any("ouban" in n.lower() for n in _names(urls)), urls
        assert not any("douban.com" in u["Url"] for u in urls), urls

    def test_no_douban_for_movie(self):
        item = _item(item_type="movie", douban_id="999")
        urls = ea_api._external_urls(item)
        assert not any("ouban" in n.lower() for n in _names(urls)), urls

    def test_others_unchanged(self):
        item = _item(tmdb_id="100", imdb_id="tt100", douban_id="1", bangumi_id="2")
        urls = ea_api._external_urls(item)
        by_name = {u["Name"]: u["Url"] for u in urls}
        assert by_name["TMDB"] == "https://www.themoviedb.org/tv/100"
        assert by_name["IMDb"] == "https://www.imdb.com/title/tt100/"
        assert by_name["Bangumi"] == "https://bgm.tv/subject/2"

    def test_provider_ids_keep_douban_metadata(self):
        # ProviderIds 是编号元数据（跨服匹配等），不是展示链接，照常保留
        item = _item(douban_id="12345")
        pids = ea_api._provider_ids(item)
        assert pids.get("Douban") == "12345"

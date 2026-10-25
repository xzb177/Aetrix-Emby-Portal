"""NFO 优先刮削：解析、落库、发现、剧集分组。

远端网盘已有刮削（tvshow.nfo / 同名 .nfo）时，扫描优先用 NFO，
不再调 TMDB 搜索（B 方案：TMDB 只补图）。
"""
import os
from types import SimpleNamespace

import pytest

from backend.emby_server import nfo as nfo_lib
from backend.emby_server import scanner
from backend.emby_server.scanner import (
    _find_nfos,
    _nfo_candidates,
    _ScanContext,
    _series_guid_of,
    ScanFile,
)

MOVIE_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>千与千寻</title>
  <originaltitle>千と千尋の神隠し</originaltitle>
  <plot>少女千寻误入神灵世界……</plot>
  <ratings>
    <rating name="tmdb" max="10" default="true"><value>8.5</value><votes>9000</votes></rating>
  </ratings>
  <year>2001</year>
  <genre>动画</genre>
  <genre>奇幻</genre>
  <studio>吉卜力</studio>
  <mpaa>PG</mpaa>
  <tmdbid>129</tmdbid>
  <imdb_id>tt0245429</imdb_id>
</movie>
"""

# 实测远端 tvshow.nfo 的结构（86-不存在的战区）：BOM + 中文简介 + 多种 id 写法
TVSHOW_NFO = (
    "﻿<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\" ?>\n"
    "<tvshow>\n"
    "  <title>86-不存在的战区</title>\n"
    "  <originaltitle>86―エイティシックス―</originaltitle>\n"
    "  <plot>少年少女的残酷战场……</plot>\n"
    "  <rating>8.0</rating>\n"
    "  <year>2021</year>\n"
    "  <genre>动画</genre>\n"
    "  <studio>A-1 Pictures</studio>\n"
    "  <mpaa>TV-14</mpaa>\n"
    "  <tmdbid>100565</tmdbid>\n"
    "  <imdb_id>tt13718450</imdb_id>\n"
    "  <uniqueid type=\"tmdb\" default=\"true\">100565</uniqueid>\n"
    "</tvshow>\n"
)

EPISODE_NFO = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<episodedetails>
  <title>送葬者</title>
  <plot>辛耶与蕾娜的初遇……</plot>
  <rating>8.2</rating>
  <season>1</season>
  <episode>1</episode>
  <aired>2021-04-11</aired>
</episodedetails>
"""


# ==================== parse_nfo ====================


def test_parse_movie_nfo():
    data = nfo_lib.parse_nfo(MOVIE_NFO)
    assert data["title"] == "千与千寻"
    assert data["originaltitle"] == "千と千尋の神隠し"
    assert data["plot"] == "少女千寻误入神灵世界……"
    assert data["rating"] == 8.5
    assert data["year"] == 2001
    assert data["genres"] == ["动画", "奇幻"]
    assert data["studios"] == ["吉卜力"]
    assert data["mpaa"] == "PG"
    assert data["tmdb_id"] == "129"
    assert data["imdb_id"] == "tt0245429"


def test_parse_tvshow_nfo_with_bom():
    """BOM 头不能导致解析失败；直写 <rating> 也要能取到"""
    data = nfo_lib.parse_nfo(TVSHOW_NFO)
    assert data is not None
    assert data["nfo_kind"] == "tvshow"
    assert data["title"] == "86-不存在的战区"
    assert data["tmdb_id"] == "100565"
    assert data["imdb_id"] == "tt13718450"
    assert data["rating"] == 8.0


def test_parse_episode_nfo():
    data = nfo_lib.parse_nfo(EPISODE_NFO)
    assert data["nfo_kind"] == "episodedetails"
    assert data["title"] == "送葬者"
    assert data["plot"].startswith("辛耶与蕾娜")
    assert data["season"] == "1"
    assert data["episode"] == "1"


def test_parse_uniqueid_fallback():
    """只有 <uniqueid type="tmdb"> 时也要能拿到 tmdb_id"""
    data = nfo_lib.parse_nfo(
        "<movie><title>X</title>"
        '<uniqueid type="tmdb" default="true">123</uniqueid>'
        '<uniqueid type="imdb">tt0000123</uniqueid></movie>'
    )
    assert data["tmdb_id"] == "123"
    assert data["imdb_id"] == "tt0000123"


def test_parse_outline_fallback():
    """plot 为空时退回 outline"""
    data = nfo_lib.parse_nfo("<movie><title>X</title><outline>梗概</outline></movie>")
    assert data["plot"] == "梗概"


@pytest.mark.parametrize("bad", [
    "",
    "not xml at all",
    "<movie><title>没闭合",
    "<book><title>X</title></book>",  # 不支持的根标签
    "<movie><title></title></movie>",  # 没有任何可用字段
])
def test_parse_invalid_nfo_returns_none(bad):
    assert nfo_lib.parse_nfo(bad) is None


# ==================== apply_nfo ====================


def _item():
    return SimpleNamespace(
        name="旧名", sort_name="旧名", original_title="", overview="",
        community_rating=None, official_rating="", genres="", studios="",
        tmdb_id=None, imdb_id=None, aliases="", production_year=None,
        last_scraped_at=None,
    )


def test_apply_nfo_movie_sets_text_fields_and_ids():
    item = _item()
    nfo_lib.apply_nfo(item, nfo_lib.parse_nfo(MOVIE_NFO), "movie")
    assert item.name == "千与千寻"
    # 拼音排序（StrmAssistant #12）：中文标题的 sort_name 是拼音首字母
    assert item.sort_name == "qyqx"
    assert item.original_title == "千と千尋の神隠し"
    assert item.overview == "少女千寻误入神灵世界……"
    assert item.community_rating == 8.5
    assert item.official_rating == "PG"
    assert item.genres == "动画,奇幻"
    assert item.studios == "吉卜力"
    assert item.tmdb_id == "129"
    assert item.imdb_id == "tt0245429"
    assert item.last_scraped_at is not None
    assert "千与千寻" in item.aliases and "千と千尋の神隠し" in item.aliases


def test_apply_nfo_keeps_existing_year():
    """文件名解析出的年份已存在时，NFO 年份不覆盖"""
    item = _item()
    item.production_year = 2001
    nfo_lib.apply_nfo(item, nfo_lib.parse_nfo(MOVIE_NFO), "movie")
    assert item.production_year == 2001


def test_apply_nfo_episode_only_plot_and_rating():
    """单集 NFO 不碰名称（调用方按展示格式拼 S01E01）"""
    item = _item()
    nfo_lib.apply_nfo(item, nfo_lib.parse_nfo(EPISODE_NFO), "episode")
    assert item.name == "旧名"
    assert item.overview.startswith("辛耶与蕾娜")
    assert item.community_rating == 8.2
    assert item.last_scraped_at is None  # 单集不参与刮削策略


MOVIE_NFO_WITH_ART = """<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>
<movie>
  <title>带图电影</title>
  <year>2020</year>
  <thumb aspect="poster">https://img.example.com/poster.jpg</thumb>
  <thumb aspect="banner">https://img.example.com/banner.jpg</thumb>
  <fanart>
    <thumb>https://img.example.com/fanart.jpg</thumb>
  </fanart>
</movie>
"""


def test_parse_nfo_thumb_and_fanart():
    """NFO 自带图：取 poster 那张 thumb 与 fanart 第一张，banner 不算海报"""
    data = nfo_lib.parse_nfo(MOVIE_NFO_WITH_ART)
    assert data["poster"] == "https://img.example.com/poster.jpg"
    assert data["fanart"] == "https://img.example.com/fanart.jpg"


def test_parse_nfo_relative_thumb_ignored():
    """相对路径的 thumb 在网盘语境取不到，不收（免得写成坏图）"""
    data = nfo_lib.parse_nfo(
        "<movie><title>X</title>"
        "<thumb aspect=\"poster\">poster.jpg</thumb></movie>")
    assert data["poster"] == ""


def test_apply_nfo_art_fills_only_when_empty():
    """NFO 图只填空缺：条目没图时落 NFO 的，已有图（side/TMDB）不覆盖"""
    item = _item()
    item.poster_path = None
    item.primary_image_url = None
    item.backdrop_path = None
    item.backdrop_image_url = None
    nfo_lib.apply_nfo(item, nfo_lib.parse_nfo(MOVIE_NFO_WITH_ART), "movie")
    assert item.primary_image_url == "https://img.example.com/poster.jpg"
    assert item.backdrop_image_url == "https://img.example.com/fanart.jpg"

    item2 = _item()
    item2.poster_path = "/local/poster.jpg"
    item2.primary_image_url = "https://image.tmdb.org/t/p/original/x.jpg"
    item2.backdrop_path = "/local/fanart.jpg"
    item2.backdrop_image_url = "https://image.tmdb.org/t/p/original/y.jpg"
    nfo_lib.apply_nfo(item2, nfo_lib.parse_nfo(MOVIE_NFO_WITH_ART), "movie")
    assert item2.primary_image_url == "https://image.tmdb.org/t/p/original/x.jpg"
    assert item2.backdrop_image_url == "https://image.tmdb.org/t/p/original/y.jpg"


# ==================== 候选文件名 ====================


def test_nfo_candidates_order():
    assert _nfo_candidates("movie", "xxx.1080p.mkv")[:2] == ["xxx.1080p.nfo", "xxx.1080p.mkv.nfo"]
    assert "movie.nfo" in _nfo_candidates("movie", "xxx.mkv")
    assert _nfo_candidates("series", "x") == ["tvshow.nfo"]
    # 实测远端：全名 + .nfo 优先
    assert _nfo_candidates("episode", "a.S01E01.1080p.mp4")[0] == "a.S01E01.1080p.mp4.nfo"


# ==================== _series_guid_of（远端季目录修正） ====================


def _remote_scanfile(dir_rel):
    return ScanFile(
        stored_path=f"mount://2{dir_rel}/x.S01E01.mp4", name="x.S01E01.mp4",
        local_dir=None, dir_rel=dir_rel, mount_id=2,
    )


def test_series_guid_remote_season_dir_rolls_up():
    """远端 Season 子目录：剧集 guid 取父目录（与本机分支同口径）"""
    g1 = _series_guid_of(_remote_scanfile("/video/剧集/动漫剧/86-不存在的战区 (2021)/Season 01"))
    g2 = _series_guid_of(_remote_scanfile("/video/剧集/动漫剧/86-不存在的战区 (2021)/Season 02"))
    assert g1 == g2  # 同一部剧的各季不再被拆成多个剧集


def test_series_guid_remote_flat_dir_unchanged():
    """远端单层目录：剧集 guid 就是本目录（不能上卷到父目录）"""
    g = _series_guid_of(_remote_scanfile("/video/剧集/动漫剧/86-不存在的战区 (2021)"))
    assert g != _series_guid_of(_remote_scanfile("/video/剧集/动漫剧/其他剧"))
    assert g != _series_guid_of(_remote_scanfile("/video/剧集/动漫剧"))


def test_series_guid_local_unchanged():
    """本机分支行为不变：文件目录的父目录"""
    sf = ScanFile(
        stored_path="/media/动漫/86/Season 01/x.S01E01.mp4", name="x.S01E01.mp4",
        local_dir="/media/动漫/86/Season 01",
    )
    assert _series_guid_of(sf) == scanner.item_guid("/media/动漫/86")


# ==================== _find_nfos（发现） ====================


class _Entry:
    def __init__(self, name):
        self.name = name
        self.size = 0


class _FakeProvider:
    """list_dir(dir_rel) → entries；read_text(rel) → 文本"""

    def __init__(self, tree):
        # tree: {dir_rel: {fname: text}}
        self.tree = tree
        self.list_calls = []

    def list_dir(self, dir_rel):
        self.list_calls.append(dir_rel)
        return [_Entry(n) for n in self.tree.get(dir_rel, {})]

    def read_text(self, rel):
        d, f = os.path.split(rel)
        return self.tree[d][f]


def _ctx():
    return _ScanContext(snap=SimpleNamespace(), lib_id=1, stats={})


def _ep_scanfile(provider, dir_rel, name="a.S01E01.1080p.mp4"):
    return ScanFile(
        stored_path=f"mount://9{dir_rel}/{name}", name=name,
        local_dir=None, dir_rel=dir_rel, mount_id=9, provider=provider,
    )


def test_find_episode_nfo_and_series_nfo():
    """Season 子目录结构：单集 NFO 在本目录，tvshow.nfo 在父目录，都能找到"""
    show = "/动漫/86-不存在的战区 (2021)"
    season = show + "/Season 01"
    tree = {
        season: {
            "a.S01E01.1080p.mp4.nfo": EPISODE_NFO,
            "season.nfo": "<?xml version=\"1.0\"?><season><title>第一季</title>"
                          "<plot>季简介</plot></season>",
        },
        show: {"tvshow.nfo": TVSHOW_NFO},
    }
    prov = _FakeProvider(tree)
    nfo_data, series_nfo, season_nfo = _find_nfos(_ctx(), _ep_scanfile(prov, season), "episode")
    assert nfo_data["title"] == "送葬者"
    assert series_nfo["title"] == "86-不存在的战区"
    assert series_nfo["tmdb_id"] == "100565"
    assert season_nfo["plot"] == "季简介"


def test_find_nfos_flat_layout():
    """单层结构：tvshow.nfo 就在本目录"""
    show = "/动漫/86"
    tree = {show: {"tvshow.nfo": TVSHOW_NFO}}
    prov = _FakeProvider(tree)
    _, series_nfo, _ = _find_nfos(_ctx(), _ep_scanfile(prov, show), "episode")
    assert series_nfo["title"] == "86-不存在的战区"


def test_find_nfos_none_when_missing():
    prov = _FakeProvider({"/x": {}})
    assert _find_nfos(_ctx(), _ep_scanfile(prov, "/x"), "episode") == (None, None, None)


def test_find_movie_nfo_local(tmp_path):
    """本机电影：movie.nfo"""
    d = tmp_path / "film"
    d.mkdir()
    (d / "movie.nfo").write_text(MOVIE_NFO, encoding="utf-8")
    sf = ScanFile(
        stored_path=str(d / "film.2021.mkv"), name="film.2021.mkv",
        local_dir=str(d),
    )
    nfo_data, _, _ = _find_nfos(_ctx(), sf, "movie")
    assert nfo_data["title"] == "千与千寻"


def test_find_nfos_negative_cache():
    """同一目录第二次不再读远端（阴性缓存也生效）"""
    prov = _FakeProvider({"/x": {}})
    ctx = _ctx()
    sf = _ep_scanfile(prov, "/x")
    _find_nfos(ctx, sf, "episode")
    calls = len(prov.list_calls)
    _find_nfos(ctx, sf, "episode")
    assert len(prov.list_calls) == calls  # 目录列举走缓存

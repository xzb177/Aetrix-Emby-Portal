"""单集编号下放：episode 无自身编号时继承所属剧的 Tmdb/Imdb（v2.58.x）。

覆盖 backend/emby_server/api.py 的 _provider_ids：
- 客户端（Reflix IntroDB / 跨服匹配）按单集 DTO 的 ProviderIds 查编号；
  episode 行的 tmdb_id/imdb_id 全库为空，编号只在 series 层。
- 语义：这是「所属剧的编号」供「剧编号+季集号」定位，不是单集专属编号。
"""
from types import SimpleNamespace

from backend.emby_server.api import _provider_ids


def _ep(tmdb=None, imdb=None, series=None, item_type="episode"):
    return SimpleNamespace(
        item_type=item_type, tmdb_id=tmdb, imdb_id=imdb,
        douban_id=None, bangumi_id=None, series=series,
    )


def _series(tmdb=None, imdb=None):
    return SimpleNamespace(tmdb_id=tmdb, imdb_id=imdb)


def test_episode_inherits_series_ids():
    item = _ep(series=_series(tmdb="245500", imdb="tt1234567"))
    assert _provider_ids(item) == {"Tmdb": "245500", "Imdb": "tt1234567"}


def test_episode_own_ids_win():
    item = _ep(tmdb="999", series=_series(tmdb="245500"))
    assert _provider_ids(item) == {"Tmdb": "999"}


def test_episode_partial_inherit():
    # 自身有 imdb、剧有 tmdb：imdb 用自身的，tmdb 继承
    item = _ep(imdb="tt999", series=_series(tmdb="245500", imdb="tt123"))
    assert _provider_ids(item) == {"Tmdb": "245500", "Imdb": "tt999"}


def test_episode_no_ids_anywhere():
    item = _ep(series=_series())
    assert _provider_ids(item) == {}


def test_episode_no_series():
    item = _ep(series=None)
    assert _provider_ids(item) == {}


def test_movie_does_not_inherit():
    item = _ep(item_type="movie", series=_series(tmdb="245500"))
    assert _provider_ids(item) == {}


def test_series_uses_own():
    item = _ep(item_type="series", tmdb="245500", series=_series(tmdb="1"))
    assert _provider_ids(item) == {"Tmdb": "245500"}

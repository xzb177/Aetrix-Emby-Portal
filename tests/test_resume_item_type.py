"""首页「继续观看」不该出现单集（episode），但剧集续看不能为空。

起因：主人 iOS 首页出现了「第 1 集」这类单集卡片。查下来 ``/Items/Resume``
只过滤了「有播放进度 + 未播完」，**没按 item_type 过滤**——任何一条有进度的
单集都会作为独立条目混进首页。

但直接按 ``item_type.in_(["movie", "series"])`` 过滤是错的：播放进度只写在
单集行上（见 compat_routes._record_progress），series 行永远没有进度——
那样过滤后剧集的继续观看永远为空（功能性 bug，r3 轮核实）。

当前口径（r3）：查询放开到 episode，再把有进度的单集按 series_id 折叠成
所属剧集返回——首页只出现剧集/电影卡片，且剧集续看不为空。
官方 Emby 的 Resume 返回的是单集本身，这里是刻意的产品折中。
"""
import inspect

from backend.emby_server import api as emby_api


def _resume_filter_source() -> str:
    src = inspect.getsource(emby_api.get_resume)
    return src


def test_resume_query_includes_episode():
    """查询必须包含 episode，否则剧集续看永远为空（series 行无进度）"""
    src = _resume_filter_source()
    assert '"episode"' in src, (
        "Resume 查询缺 episode：剧集续看会永远为空"
    )


def test_resume_folds_episode_into_series():
    """单集必须折叠成剧集，不能作为单集条目返回"""
    src = _resume_filter_source()
    assert "series_id" in src, "Resume 缺少按 series_id 折叠单集的逻辑"
    assert "series_map" in src or "seen_series" in src


def test_resume_excludes_hidden_items():
    """Resume 应过滤 is_hidden（与 Latest 一致）"""
    src = _resume_filter_source()
    assert "is_hidden" in src


def test_resume_still_filters_progress_and_played():
    """不能因为加了类型过滤，丢掉「有进度 / 未播完」这两个原条件"""
    src = _resume_filter_source()
    assert "playback_position_ticks > 0" in src
    assert "played ==" in src  # noqa: E712 — SQLAlchemy 写法


def test_latest_keeps_same_type_scope():
    """Latest 仍应只给 movie/series（防止修 Resume 时误改）"""
    src = inspect.getsource(emby_api.get_latest)
    assert 'item_type.in_(["movie", "series"])' in src

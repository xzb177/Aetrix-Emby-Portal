"""剧集 series 名必须来自剧集目录，而不是 episode 文件名。

线上截图里的「三生无殇 第 1 集」「非来不可 第 3 集」不是 episode 卡片，
而是 item_type=series 的伪系列：旧扫描器用 episode 文件名的解析名创建 series，
把 ``第 1 集``、``HHWEB`` 等尾巴一起写进了系列名。
"""
from types import SimpleNamespace

from backend.emby_server import scanner


def _remote(rel):
    return SimpleNamespace(local_dir=None, dir_rel=rel)


def test_series_name_comes_from_remote_series_directory():
    parsed = {"name": "非来不可 第 3 集 2 0 HHWEB", "year": 2024}
    f = _remote("/MoviePilot/剧集/国产剧/非来不可 (2024)/Season 1")
    assert scanner._series_name_of(f, parsed) == "非来不可"


def test_series_name_comes_from_remote_directory_without_season_folder():
    parsed = {"name": "三生无殇 第 1 集", "year": None}
    f = _remote("/MoviePilot/剧集/国产剧/三生无殇")
    assert scanner._series_name_of(f, parsed) == "三生无殇"


def test_series_name_comes_from_local_parent_of_season_folder(tmp_path):
    series = tmp_path / "JOJO的奇妙冒险 (2012)"
    season = series / "Season 1"
    season.mkdir(parents=True)
    parsed = {"name": "JOJO的奇妙冒险 第 1 集", "year": 2012}
    f = SimpleNamespace(local_dir=str(season), dir_rel="/")
    assert scanner._series_name_of(f, parsed) == "JOJO的奇妙冒险"


def test_fallback_to_parsed_name_when_directory_is_empty():
    parsed = {"name": "某剧 第 1 集", "year": None}
    f = _remote("/")
    assert scanner._series_name_of(f, parsed) == "某剧 第 1 集"


def test_series_name_trailing_year_stripped_and_backfilled():
    """目录名尾巴的年份：parsed 里没年份时也要剥净并回填年份栏。

    生产实证（2026-10-20）：「泪之女王 (2024)」整段当剧名入库、
    production_year 为空，客户端把年份显示两遍（用户亲令：名字
    后面不要带年份）。旧实现 year 为空时连 _strip_year 都不调。
    """
    parsed = {"name": "泪之女王 第 1 集", "year": None}
    f = _remote("/剧集/日韩剧/泪之女王 (2024)/Season 1")
    assert scanner._series_name_of(f, parsed) == "泪之女王"
    assert parsed["year"] == 2024


def test_series_name_fullwidth_year_brackets():
    parsed = {"name": "好想告诉你 第 1 集", "year": None}
    f = _remote("/剧集/日韩剧/好想告诉你（2023）/Season 1")
    assert scanner._series_name_of(f, parsed) == "好想告诉你"
    assert parsed["year"] == 2023


def test_series_name_non_year_brackets_kept():
    """括号里不是年份的不许误剥。"""
    parsed = {"name": "某某剧 第 1 集", "year": None}
    f = _remote("/剧集/国产剧/某某剧 (真人版)/Season 1")
    assert scanner._series_name_of(f, parsed) == "某某剧 (真人版)"
    assert parsed["year"] is None


def test_series_name_pure_numeric_title_kept():
    """纯数字剧名 + 尾巴年份：数字本体保留、年份剥净并回填。"""
    parsed = {"name": "11-11-11 第 1 集", "year": None}
    f = _remote("/剧集/欧美剧/11-11-11 (2011)/Season 1")
    assert scanner._series_name_of(f, parsed) == "11 11 11"
    assert parsed["year"] == 2011

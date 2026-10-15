"""单集占位标题判定与替换（v2.58.x）。

覆盖 backend/emby_server/tmdb.py：
- _is_placeholder_episode_title：严格占位判定（宁可漏判不误伤真标题）
- TmdbClient.apply_episode：占位名+TMDB 真标题→替换；TMDB 占位标题
  （"Episode N" / 中文"第 N 集"）不覆盖；已有真实标题不覆盖。
"""
from types import SimpleNamespace

import pytest

from backend.emby_server.tmdb import TmdbClient, _is_placeholder_episode_title


@pytest.mark.parametrize("name,expected", [
    ("第1集", True),
    ("第 22 集", True),
    ("与卿书 第22集 S01E22", True),
    ("五十公里桃花坞 第 4 集 S01E04", True),
    ("探索新境 第 6 集 PTerWEB S01E06", True),
    ("", True),
    ("   ", True),
    ("向前为救姜兰芝四处求人", False),
    ("摩登到访", False),
    ("五十公里桃花坞 S01E04", False),  # 宽松模式不收：可能是真名+后缀
])
def test_placeholder_detector(name, expected):
    assert _is_placeholder_episode_title(name) is expected


def _item(name, overview=""):
    return SimpleNamespace(name=name, overview=overview, sort_name="",
                           duration_ticks=0)


def _ep_data(name, overview=""):
    return {"name": name, "overview": overview, "still_path": None,
            "runtime": 2700}


def test_placeholder_replaced_by_real_title():
    item = _item("好团圆 第 1 集 S01E01")
    r = TmdbClient.apply_episode(item, _ep_data("向前为救姜兰芝四处求人"))
    assert r["updated"] is True
    assert item.name == "向前为救姜兰芝四处求人"
    assert item.sort_name  # 与 nfo.py:202 同口径同步


def test_real_title_not_overwritten():
    item = _item("向前为救姜兰芝四处求人")
    r = TmdbClient.apply_episode(item, _ep_data("另一个真名"))
    assert r["updated"] is False
    assert item.name == "向前为救姜兰芝四处求人"


def test_tmdb_english_placeholder_not_applied():
    item = _item("好团圆 第 1 集 S01E01")
    r = TmdbClient.apply_episode(item, _ep_data("Episode 5"))
    assert r["updated"] is False
    assert item.name == "好团圆 第 1 集 S01E01"


def test_tmdb_chinese_placeholder_not_applied():
    item = _item("好团圆 第 2 集 S01E02")
    r = TmdbClient.apply_episode(item, _ep_data("第 2 集"))
    assert r["updated"] is False
    assert item.name == "好团圆 第 2 集 S01E02"


def test_overview_only_fills_empty():
    item = _item("好团圆 第 3 集 S01E03")
    r = TmdbClient.apply_episode(item, _ep_data("", overview="第三集简介"))
    assert item.overview == "第三集简介"
    assert item.name == "好团圆 第 3 集 S01E03"  # 标题占位但 TMDB 无真标题→保留

"""季号目录兜底（缺集普查 A3，2026-10-19）。

文件名没给显式季号（裸 E05 / 绝对集数只给了默认的 season=1）时，
父目录名（Season 1 / S01 / 第1季 等既有可解析形态）确定性补季号；
文件名有显式季号、或目录名含糊时一律不动（宁漏勿错标）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server.scanner import parse_media_filename  # noqa: E402


def _p(path, lib="tvshows"):
    return parse_media_filename(path, lib)


def test_dirname_season_fallback():
    # 正例：三种目录形态都兜底
    assert _p("/lib/Show/Season 2/Show - E05.mp4")["season"] == 2
    assert _p("/lib/Show/第2季/E05.mp4")["season"] == 2
    assert _p("/lib/Show/S02/E05.mp4")["season"] == 2
    r = _p("/lib/Show/Season 2/Show - E05.mp4")
    assert (r["season"], r["episode"]) == (2, 5)


def test_explicit_season_in_filename_wins():
    # 反例：文件名有显式季号，目录名不得覆盖
    assert _p("/lib/Show/Season 2/Show S01E05.mp4")["season"] == 1


def test_ambiguous_dirname_not_touched():
    # 反例：目录名含糊（两个不同季号）→ 不动，宁漏勿错标
    assert _p("/lib/Show/Season 2 S01/E05.mp4")["season"] == 1
    # 反例：目录名无季号标记 → 不动
    assert _p("/lib/Show/E05.mp4")["season"] == 1


def test_specials_dir_not_misread():
    # 回归：Specials 目录不能被当成季号
    assert _p("/lib/Show/Specials/E05.mp4")["season"] == 1
    # 特别篇季（Season 0）按现有惯例照认
    assert _p("/lib/Show/Season 0/E01.mp4")["season"] == 0


def test_absolute_episode_with_dirname():
    # 绝对集数 + 目录季号：季号按目录来
    r = _p("/lib/Show/Season 3/Frieren - 28.mkv")
    assert (r["season"], r["episode"]) == (3, 28)

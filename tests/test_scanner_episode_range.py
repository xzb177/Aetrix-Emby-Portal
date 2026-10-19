"""合集区间解析（缺集普查 A2，2026-10-19）：S06E27-E28 这类「一文件多集」
命名必须解析为 (season, 起始集, episode_end)，不能让独立 E 模式把尾巴
咬走（生产实证：海绵宝宝 S06E27-E28 曾被标成 S1E28）。"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server.scanner import parse_media_filename  # noqa: E402


def _p(stem, lib="tvshows"):
    return parse_media_filename(stem, lib)


def test_range_start_and_end():
    r = _p("海绵宝宝 - S06E27-E28 - 第 27-28 集 - 1080p WEB-DL x264 AAC 2.0.mp4")
    assert (r["season"], r["episode"], r["episode_end"]) == (6, 27, 28)


def test_range_with_space_and_no_second_e():
    r = _p("海绵宝宝 - S01E26 - E27 - 第 26-27 集.mkv")
    assert (r["season"], r["episode"], r["episode_end"]) == (1, 26, 27)


def test_single_episode_unchanged():
    r = _p("海绵宝宝 - S01E28 - 第 28 集 - 1080p WEB-DL.mkv")
    assert (r["season"], r["episode"], r["episode_end"]) == (1, 28, None)
    r = _p("海绵宝宝 - S06E27 - 第 27 集 - 1080p WEB-DL.mkv")
    assert (r["season"], r["episode"], r["episode_end"]) == (6, 27, None)


def test_range_does_not_steal_year():
    # S01E2019 的 2019 不能被当成区间尾巴（4 位数不在 \d{1,3} 内）
    r = _p("某剧 S01E2019 2160p.mkv")
    assert (r["season"], r["episode"], r["episode_end"]) == (1, 2019, None)


def test_reversed_range_falls_back_to_single():
    # 尾巴 <= 起点：不认区间，按单集处理
    r = _p("某剧 S01E05-E03.mkv")
    assert (r["season"], r["episode"], r["episode_end"]) == (1, 5, None)


def test_range_name_digs_whole_span():
    # 片名里整个区间段被挖掉，不留 "-E28" 尾巴
    r = _p("海绵宝宝 - S06E27-E28 - 第 27-28 集 - 1080p WEB-DL x264 AAC 2.0.mp4")
    assert "E28" not in r["name"] and "S06E27" not in r["name"]


def test_absolute_episode_unchanged():
    r = _p("Frieren - 28.mkv")
    assert (r["season"], r["episode"], r["episode_end"]) == (1, 28, None)

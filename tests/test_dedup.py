"""系统性去重合并的回归测试。"""

import pytest

from backend.emby_server import dedup as dedup_lib


class FakeItem:
    def __init__(self, id, library_id, item_type, name,
                 tmdb_id=None, year=None, poster=None, overview=None,
                 guid=None):
        self.id = id
        self.library_id = library_id
        self.item_type = item_type
        self.name = name
        self.tmdb_id = tmdb_id
        self.production_year = year
        self.poster_path = poster
        self.primary_image_url = None
        self.overview = overview
        self.guid = guid or f"guid-{id}"


def test_normalize_name():
    assert dedup_lib.normalize_name("19号消防局") == dedup_lib.normalize_name("19号消防局 ")
    assert dedup_lib.normalize_name("24小时") == dedup_lib.normalize_name("24小 时")
    assert dedup_lib.normalize_name("A.B.C") == dedup_lib.normalize_name("a b c")
    assert dedup_lib.normalize_name("") == ""
    assert dedup_lib.normalize_name(None) == ""


def test_dedup_key_tmdb_priority():
    # 有 tmdb_id 时用 tmdb_id，不用标题
    a = FakeItem(1, 5, "series", "19号消防局", tmdb_id="76773", year=2018)
    b = FakeItem(2, 5, "series", "完全不同的名字", tmdb_id="76773", year=2020)
    assert dedup_lib.dedup_key(a) == dedup_lib.dedup_key(b)


def test_dedup_key_name_fallback():
    # 无 tmdb_id 时用归一化标题+年份
    a = FakeItem(1, 5, "series", "24小时", year=2001)
    b = FakeItem(2, 5, "series", "24小时 ", year=2001)
    assert dedup_lib.dedup_key(a) == dedup_lib.dedup_key(b)
    # 年份不同则不合并
    c = FakeItem(3, 5, "series", "24小时", year=2014)
    assert dedup_lib.dedup_key(a) != dedup_lib.dedup_key(c)
    # 不同库不合并
    d = FakeItem(4, 6, "series", "24小时", year=2001)
    assert dedup_lib.dedup_key(a) != dedup_lib.dedup_key(d)


def test_dedup_key_episode_not_deduped():
    # episode 不参与顶层去重
    e = FakeItem(1, 5, "episode", "第 1 集", tmdb_id="123")
    assert dedup_lib.dedup_key(e) is None


def test_deduplicate_items_same_tmdb():
    # 同 tmdb_id 的 4 条只保留 1 条
    items = [
        FakeItem(1, 5, "series", "19号消防局", tmdb_id="76773"),
        FakeItem(2, 5, "series", "19号消防局", tmdb_id="76773"),
        FakeItem(3, 5, "series", "19号消防局", tmdb_id="76773"),
        FakeItem(4, 5, "series", "19号消防局", tmdb_id="76773"),
    ]
    result = dedup_lib.deduplicate_items(items)
    assert len(result) == 1
    assert result[0].id == 1  # id 最小优先
    assert result[0]._merged_count == 3


def test_deduplicate_items_primary_with_poster():
    # 有海报的优先当主记录
    items = [
        FakeItem(1, 5, "series", "24小时", tmdb_id="1973"),
        FakeItem(2, 5, "series", "24小时", tmdb_id="1973", poster="/a.jpg"),
        FakeItem(3, 5, "series", "24小时", tmdb_id="1973"),
    ]
    result = dedup_lib.deduplicate_items(items)
    assert len(result) == 1
    assert result[0].id == 2  # 有海报的优先


def test_deduplicate_items_different_shows_kept():
    items = [
        FakeItem(1, 5, "series", "19号消防局", tmdb_id="76773"),
        FakeItem(2, 5, "series", "24小时", tmdb_id="1973"),
    ]
    result = dedup_lib.deduplicate_items(items)
    assert len(result) == 2


def test_deduplicate_items_movie():
    items = [
        FakeItem(1, 3, "movie", "爱情假说", tmdb_id="999", year=2026),
        FakeItem(2, 3, "movie", "爱情假说", tmdb_id="999", year=2026),
    ]
    result = dedup_lib.deduplicate_items(items)
    assert len(result) == 1


def test_deduplicate_items_preserves_order():
    items = [
        FakeItem(1, 5, "series", "A剧", tmdb_id="111"),
        FakeItem(2, 5, "series", "B剧", tmdb_id="222"),
        FakeItem(3, 5, "series", "A剧", tmdb_id="111"),
    ]
    result = dedup_lib.deduplicate_items(items)
    assert [r.id for r in result] == [1, 2]  # 保持首次出现顺序


def test_deduplicate_items_episode_passthrough():
    # episode 原样返回（集级别去重在 _dedup_primary_ids 做）
    items = [
        FakeItem(1, 5, "episode", "S01E01"),
        FakeItem(2, 5, "episode", "S01E01"),
    ]
    result = dedup_lib.deduplicate_items(items)
    assert len(result) == 2


def test_normalize_name_strips_broken_year_suffix():
    """缺集普查 2026-10-19 桶 C2：手工改名残留的未闭合年份后缀「(2023」
    要被当作残缺片段剥掉，让它与正名归一一致，避免认亲失败。"""
    assert dedup_lib.normalize_name("九尾狐传 (2023") == dedup_lib.normalize_name("九尾狐传")
    assert dedup_lib.normalize_name("Test Show [2024") == dedup_lib.normalize_name("Test Show")
    assert dedup_lib.normalize_name("剧名（2023") == dedup_lib.normalize_name("剧名")
    assert dedup_lib.normalize_name("剧名［2025") == dedup_lib.normalize_name("剧名")
    assert dedup_lib.normalize_name("(2023") == ""


def test_normalize_name_keeps_closed_year_suffix():
    """闭合的年份后缀故意不动：年份是跨年同名剧的区分信号，
    剥掉会把「X (2023)」和「X (2024)」误判成同一部。"""
    assert dedup_lib.normalize_name("九尾狐传 (2023)") == "九尾狐传2023"
    assert dedup_lib.normalize_name("Test Show (2024)") == "testshow2024"


def test_normalize_name_keeps_non_year_titles():
    """正常标题（含括号的正名）行为不动：只有「括号+四位年份」才剥。"""
    assert dedup_lib.normalize_name("xxx (导演剪辑版)") == "xxx导演剪辑版"
    # 未闭合但不是年份的括号也不动
    assert dedup_lib.normalize_name("xxx (导演剪辑版") == "xxx导演剪辑版"
    # 年份不在行尾的不动
    assert dedup_lib.normalize_name("2023 大作战") == "2023大作战"
    assert dedup_lib.normalize_name("Report (2023) Final") == "report2023final"
    # 三位数不是年份
    assert dedup_lib.normalize_name("剧名 (202") == "剧名202"

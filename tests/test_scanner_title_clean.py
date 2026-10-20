"""扫描器标题清洗（scanner.clean_name / parse_media_filename）。

根因（生产实证 2026-10-20）：文件名主干里的重复下载标记 / 版本号 token
被清洗链拆散，污染了入库时的显示名；且 TMDB 文字是「只填空不覆盖」
口径，正名盖不掉它。本文件锁定修复后的行为：

- `[N]` 主干末尾方括号标记 → 整段剔除；
- 标题后紧贴的 `-N`（其后是年份/质量标签时）→ 只有"冗余"才剔除：
  N 必须已作为数字出现在它前面的标题里（`月球叛军2：烙印之人-2` 的 `-2`
  是重复记号；`Delta Movie 02-00` 的 `-00` 是真标题的一部分，保留）；
- 紧贴标题文字的版本号 token（`2.0`、`-1.0`）→ 不得把 `.`/`-` 拆成空格；
- 真标题尾巴的数字（续集号/版本）→ 原样保留，一律不误伤。
"""

import pytest

from backend.emby_server.scanner import clean_name, parse_media_filename

LIB_MOVIES = "movies"


def _title(filename: str) -> str:
    return parse_media_filename(f"/电影/外语电影/{filename}", LIB_MOVIES).get("name")


class TestDupMarkerStripped:
    """正例：重复标记必须从标题里消失（生产库真实入库案例）。"""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            # 1. 方括号重复下载标记 [N] 整段剔除
            ("第四公民 (2014) - 1080p [2].mkv", "第四公民"),
            ("语义错误电影版 (2022) - 1080p [2].mkv", "语义错误电影版"),
            ("乱世佳人 (1939) - 2160p WEB-DL [2].mp4", "乱世佳人"),
            # 2. 标题后紧贴的 -N（其后是年份括号段）剔除
            (
                "月球叛军2：烙印之人-2 (2024) - 1080p.WEB-DL Netflix.mkv",
                "月球叛军2：烙印之人",
            ),
        ],
    )
    def test_dup_marker_gone(self, filename, expected):
        assert _title(filename) == expected

    def test_year_still_parsed(self):
        p = parse_media_filename(
            "/电影/外语电影/第四公民 (2014) - 1080p [2].mkv", LIB_MOVIES)
        assert p["year"] == 2014


class TestRedundantDashNumberStripped:
    """`-N` 只有"冗余"时才剔除（CI 冒烟实证的根因回归）。

    第一轮实现无条件剔除 `-N`（其后是年份时），把冒烟用例的
    `Delta Movie 02-00 (2020)` 等 25 个文件名全洗成同一个标题
    `Delta Movie 02`，撞上 `_claim_batch_guids` 的同名电影去重，
    把别人的行复用掉 → OFF 轮多出 added=1（side=202≠201）。
    现在只剔冗余的：N 必须已作为数字出现在它前面的标题里。
    """

    @pytest.mark.parametrize(
        "filename,expected",
        [
            # 冗余 → 剔除（生产实证：重复下载记号）
            (
                "月球叛军2：烙印之人-2 (2024) - 1080p.WEB-DL Netflix.mkv",
                "月球叛军2：烙印之人",
            ),
            (
                "月球叛军2：烙印之人-2.2024.1080p.WEB-DL.mkv",
                "月球叛军2：烙印之人",
            ),
            # 不冗余 → 保留，标题保持互不相同（冒烟用例口径）
            ("Delta Movie 02-00 (2020).mkv", "Delta Movie 02 00"),
            ("Delta Movie 07-24 (2020).mkv", "Delta Movie 07 24"),
            # 标题里没有数字 → 保留（可能是真续集号）
            ("Avatar-2 (2022) - 1080p WEB-DL.mkv", "Avatar 2"),
            ("流浪地球-2 (2024) - 1080p WEB-DL.mkv", "流浪地球 2"),
            ("Title-2.2024.1080p.WEB-DL.mkv", "Title 2"),
            # 纯数字标题守卫（r66）：标题没有任何非数字实义字符时，
            # `-N` 一律保留——`11-11-11` 是真实片名，不是重复记号
            ("11-11-11.2011.1080p.BluRay.mkv", "11 11 11"),
            ("11-11-11 (2011).mkv", "11 11 11"),
            ("24-24 (2024).mkv", "24 24"),
        ],
    )
    def test_redundant_dash_number(self, filename, expected):
        assert _title(filename) == expected


class TestVersionTokenKept:
    """正例：版本号 token 的 `.`/`-` 不得拆成空格（生产库真实入库案例）。"""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("梅根2.0 (2025) - 2160p WEB-DL.mkv", "梅根2.0"),
            ("哥斯拉-1.0 (2023) - 2160p WEB-DL.mkv", "哥斯拉-1.0"),
        ],
    )
    def test_version_token_intact(self, filename, expected):
        assert _title(filename) == expected


class TestRealTrailingDigitsKept:
    """反例：这些尾巴数字是片名本身，必须原样保留。"""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("Woodwalkers 2 (2026) - 1080p WEB-DL.mkv", "Woodwalkers 2"),
            ("罗小黑战记 2 (2025) - 1080p WEB-DL.mkv", "罗小黑战记 2"),
            ("致命录像带 85 (2023) - 1080p WEB-DL.mkv", "致命录像带 85"),
            ("Perfect 10 (2010) - 1080p WEB-DL.mkv", "Perfect 10"),
            ("剧场版 咒术回战 0 (2021) - 1080p WEB-DL.mkv", "剧场版 咒术回战 0"),
            ("非诚勿扰3 (2023) - 2160p WEB-DL.mkv", "非诚勿扰3"),
            ("自拍照 69 (2016) - 1080p WEB-DL.mkv", "自拍照 69"),
            ("误杀瞒天记 2 (2021) - 1080p WEB-DL.mkv", "误杀瞒天记 2"),
        ],
    )
    def test_real_digit_suffix_kept(self, filename, expected):
        assert _title(filename) == expected


class TestExistingBehavior:
    """既有清洗行为回归（本 PR 不改动）。"""

    def test_webdl_trailing_n(self):
        assert clean_name("Some Movie WEB-DL-3") == "Some Movie"

    def test_internal_digit_untouched(self):
        assert clean_name("比佛利拜金狗2") == "比佛利拜金狗2"

    def test_marker_only_name_goes_empty(self):
        """纯标记输入不再把标记吐回当片名（之前是「2」）。"""
        assert clean_name(" [2]") == ""

    def test_collision_suffix_does_not_break_episode_parsing(self):
        """Drive 真撞后缀 ` [2]`：季/集照样解析，标题回退到目录名。"""
        p = parse_media_filename("/综艺/同名节目/S01E01 [2].mkv", "tvshows")
        assert (p["season"], p["episode"]) == (1, 1)
        assert p["name"] == "同名节目"


class TestTrailingYearNotInName:
    """片名尾巴带括号的年份不许进显示名（2026-10-20 用户亲令）。

    生产实证 1,662 条：文件夹名「片名 (2024)」原样入库、年份栏反而
    为空，客户端把年份显示两遍。剥年份原本靠 _strip_year(name, year)，
    year 没解析出来时整段保留——本类锁定 year 缺失路径也剥净，
    且剥出的年份要落进年份栏。
    """

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("泪之女王 (2024).mkv", "泪之女王"),
            ("好想告诉你（2023）.mkv", "好想告诉你"),
            ("母亲往事 (2025).mkv", "母亲往事"),
        ],
    )
    def test_bracketed_year_stripped_from_name(self, filename, expected):
        assert _title(filename) == expected

    def test_bracketed_year_lands_in_year_field(self):
        p = parse_media_filename("/电影/日韩电影/泪之女王 (2024).mkv", LIB_MOVIES)
        assert p["year"] == 2024

    def test_non_year_brackets_kept(self):
        """括号里不是年份的（如版本说明）不许误剥。"""
        assert _title("某某电影 (真人版).mkv") == "某某电影 (真人版)"

    def test_bare_numeric_title_kept(self):
        """片名本体就是数字、且无括号年份时原样保留（《2012》类）。"""
        p = parse_media_filename("/电影/外语电影/2012.mkv", LIB_MOVIES)
        assert p["name"] == "2012"

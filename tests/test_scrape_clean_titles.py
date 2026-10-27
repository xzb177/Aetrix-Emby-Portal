"""扫描器标题清洗（2026-10-27 脏标题清理，v2 规则包）。

生产实证（只读查，不编数）：
- 1,508 条剧集名带文件后缀：``莫语者 第 14 集 DDP 2 0 ADWeb S01E14``、
  ``两天一夜 第 346 集 ADWeb S04E346``——解析出的名已含技术词，
  展示名又把 SxxExx 追加一次，集号出现两次；
- 241 条括号未闭合：``一念间 (2016``、``你好 (2026``——目录名本身
  的右括号在源头就丢了，闭合版年份正则永远命中不了；
- 没刮出来的剧 / 电影用干净标题重查：clean_query_title 只产查询串，
  不发请求、不做匹配判定，拿不准的仍进人工认亲。

反例（必须原样不动）：版本号 token、纯数字标题、冗余 -N 守卫、
非年份括号、真续集尾数——这些是既往生产事故钉住的底线，本批
新增的窄规则一律不得误伤它们。
"""

import pytest

from backend.emby_server import rule_pack as rp
from backend.emby_server.scanner import (
    _episode_display_name,
    clean_name,
    clean_query_title,
    clean_stored_display_name,
    parse_media_filename,
)


def _tv(path: str):
    return parse_media_filename(path, "tvshows")


class TestDirtyEpisodeSuffixCleaned:
    """正例：文件后缀 + 重复集号后缀必须同时消失，季集号本身保留。"""

    @pytest.mark.parametrize(
        "path,season,episode,expected_display",
        [
            (
                "/剧集/国产剧/莫语者 (2026)/Season 1/"
                "莫语者 - S01E14 - 第 14 集 - 2160p WEB-DL H265 DDP 2.0 - ADWeb.mkv",
                1, 14, "莫语者 第 14 集",
            ),
            (
                "/剧集/综艺/两天一夜 (2007)/Season 4/"
                "两天一夜 - S04E346 - 第 346 集 - 1080p WEB-DL H264 AAC - ADWeb.mkv",
                4, 346, "两天一夜 第 346 集",
            ),
            (
                "/剧集/动漫/逆天至尊 (2021)/Season 1/"
                "逆天至尊 - S01E561 - 第 561 集 - 2160p WEB-DL H265 AAC - ADWeb.mp4",
                1, 561, "逆天至尊 第 561 集",
            ),
        ],
    )
    def test_dirty_file_parses_to_clean_display(
            self, path, season, episode, expected_display):
        parsed = _tv(path)
        assert (parsed["season"], parsed["episode"]) == (season, episode)
        assert _episode_display_name(
            parsed["name"], parsed["season"], parsed["episode"]
        ) == expected_display

    def test_stored_dirty_name_cleans_without_touching_numbers(self):
        assert clean_name("莫语者 第 14 集 DDP 2 0 ADWeb") == "莫语者 第 14 集"
        assert clean_name("莫语者 第 14 集 DDP 2.0 ADWeb") == "莫语者 第 14 集"


class TestUnclosedParenYear:
    """正例：未闭合的尾巴年份整段剥，孤括号不留。"""

    @pytest.mark.parametrize("raw,expected", [
        ("一念间 (2016", "一念间"),
        ("你好 (2026", "你好"),
        ("一念间（2016", "一念间"),
    ])
    def test_unclosed_year_stripped(self, raw, expected):
        assert clean_stored_display_name(raw) == expected

    def test_clean_name_keeps_year_for_parse_backfill(self):
        # 分层契约：clean_name 不剥年份，否则解析路径回填年份会丢；
        # 存量清洗必须走 clean_stored_display_name。
        assert clean_name("一念间 (2016") == "一念间 (2016"

    def test_unclosed_year_still_parsed_as_year(self):
        parsed = parse_media_filename("/电影/一念间 (2016.mkv", "movies")
        assert parsed["name"] == "一念间"
        assert parsed["year"] == 2016

    def test_empty_parens_and_blu_ray_token_removed(self):
        assert clean_name("话 蓝光( ) 32 240") == "话 32 240"
        assert clean_name("某剧 蓝光 1080p") == "某剧"


class TestEpisodeDisplayNoDoubleSuffix:
    """展示名：已有中文集标记时不追加 SxxExx；无标记时旧口径不变。"""

    def test_chinese_episode_marker_suppresses_suffix(self):
        assert _episode_display_name("莫语者 第 14 集", 1, 14) == "莫语者 第 14 集"
        assert _episode_display_name("莫语者 第 14 话", 1, 14) == "莫语者 第 14 话"
        # 集号不一致时不算“已有标记”，仍按旧口径追加（宁可重复、不错标）。
        assert _episode_display_name("莫语者 第 13 集", 1, 14) == "莫语者 第 13 集 S01E14"

    def test_existing_contract_unchanged(self):
        assert _episode_display_name("3月的狮子", 1, 2) == "3月的狮子 S01E02"
        assert _episode_display_name("某剧", 2, None) == "某剧 S02"
        assert _episode_display_name("某剧", None, 3) == "某剧 E03"


class TestCleanQueryTitle:
    """重查查询串：只清洗，不匹配、不发请求。"""

    def test_query_title_strips_year_and_junk(self):
        assert clean_query_title("一念间 (2016") == "一念间"
        assert clean_query_title("泪之女王 (2024)") == "泪之女王"
        assert clean_query_title("黑道无边") == "黑道无边"
        assert clean_query_title("3体") == "3体"


class TestTrailingClosedYear:
    """正例：尾巴闭合年份剥掉，production_year 由字段管、名字里不留。

    注意：clean_name 是纯字符串函数，只返回干净名字，不读不写
    production_year。存量回填只写 name 一列，绝不拿名字里的年份
    覆盖已有字段（守护者 (2017) 而字段 2018、一念间残片 2016 而
    字段 1989，都是以字段为准的反例，见规则表 R4/R5）。
    """

    @pytest.mark.parametrize("raw,expected", [
        ("不死少女·杀人笑剧 (2023)", "不死少女·杀人笑剧"),
        ("LoveLive 爱与演唱会!超级明星!! (2021)",
         "LoveLive 爱与演唱会!超级明星!!"),
        ("舌尖上的中国 (2012)", "舌尖上的中国"),
        ("守护者 (2017)", "守护者"),
        ("宵夜江湖 (2019)", "宵夜江湖"),
    ])
    def test_closed_trailing_year_stripped(self, raw, expected):
        assert clean_stored_display_name(raw) == expected
        assert clean_query_title(raw) == expected


class TestGluedYearAntiExamples:
    """反例：中文名后直接粘着年份、无括号无分隔，一律原样不动。

    这类只进干跑的人工复核清单，不自动删：有的是片名本身
    （魔王2099、机动战士高达0079、1899、1122好夫妇、魔术快斗1412、
    唐探1900、阿根廷1985），有的是粘连的年份（学校2015），
    代码分不清，宁可不动、等人看。
    """

    @pytest.mark.parametrize("raw", [
        "魔王2099",
        "机动战士高达0079",
        "1899",
        "1122好夫妇",
        "魔术快斗1412",
        "学校2015",
        "唐探1900",
        "阿根廷1985",
        "超人家族2017",
    ])
    def test_glued_year_untouched(self, raw):
        assert clean_name(raw) == raw

    def test_glued_year_token_survives_separator_normalisation(self):
        # “你是谁 - 学校2015”：既有 CLEAN_RE 会把 - 归一成空格，
        # 但年份 token 学校2015 必须还在，不许被当成年份剥掉。
        assert clean_name("你是谁 - 学校2015") == "你是谁 学校2015"


class TestNoPosterQueryTitle:
    """没海报的剧：重查只产干净查询串，不贴海报、不判匹配。"""

    def test_no_poster_examples_query_only(self):
        assert clean_query_title("一念间 (2016") == "一念间"
        assert clean_query_title("Dadao Duxing： Die Long Bian") == \
            "Dadao Duxing： Die Long Bian"
        assert clean_query_title("黑野美子的宇宙恐怖秀") == "黑野美子的宇宙恐怖秀"


class TestRulePackV2:
    """v2 词表：ADWeb 与声道多词条进包，旧词行为不变。"""

    def test_pack_version_is_v2(self):
        assert rp.PACK_VERSION == "v2"
        assert rp.default_pack().version == "v2"

    def test_pack_strips_adweb_and_channel_terms(self):
        pack = rp.RulePack()
        assert pack.strip("Film ADWeb 1080p").strip() == "Film"
        assert pack.strip("Film DDP 2 0").strip() == "Film"
        assert pack.strip("Film DTS 5 1").strip() == "Film"

    def test_legacy_pack_behavior_unchanged(self):
        pack = rp.RulePack()
        assert pack.strip("Movie 1080p BluRay x265").strip() == "Movie"
        assert pack.strip("Show S01E01 2160p").strip() == "Show S01E01"


class TestAntiExamplesUntouched:
    """反例：新增窄规则不得误伤正经片名（既往事故底线）。"""

    @pytest.mark.parametrize("raw", [
        "梅根2.0",
        "哥斯拉-1.0",
        "比佛利拜金狗2",
        "11 11 11",
        "Delta Movie 02 00",
        "某某电影 (真人版)",
        "2012",
        "Woodwalkers 2",
        "Avatar 2",
        "流浪地球 2",
    ])
    def test_anti_examples(self, raw):
        assert clean_name(raw) == raw

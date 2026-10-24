# -*- coding: utf-8 -*-
"""FMBY #25 25-2：Emby 型目录聚合读模型。

只测本模块（纯函数 + 注入式取页），不发 HTTP、不碰数据库、
不写本地条目表。本地 Python 3.12 + pyshim 口径，全量以
GitHub CI 为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import upstream_aggregation as agg


def _movie(item_id, name="肖申克的救赎", tmdb="278", year=1994):
    return {
        "Id": item_id,
        "Name": name,
        "Type": "Movie",
        "ProductionYear": year,
        "ProviderIds": {"Tmdb": tmdb, "Imdb": "tt0111161"},
        "MediaSources": [{"Id": "ms-1", "Name": "1080p", "Path": "/srv/secret/path.mkv"}],
    }


def _record(source, item_id, **overrides):
    raw = _movie(item_id)
    raw.update(overrides)
    return agg.parse_emby_item(raw, source_id=source, category_id="cat-1")


def test_parse_emby_item_keeps_identity_drops_path_and_credentials():
    record = agg.parse_emby_item(
        {
            "Id": "abc",
            "Name": "肖申克的救赎",
            "Type": "Movie",
            "ProductionYear": 1994,
            "ProviderIds": {"Tmdb": "278", "Imdb": "tt0111161"},
            "ApiKey": "should-never-appear",
            "MediaSources": [
                {"Id": "ms-1", "Name": "1080p", "Path": "/srv/secret/path.mkv"}
            ],
        },
        source_id="src-1",
    )
    assert record.tmdb_id == "278"
    assert record.imdb_id == "tt0111161"
    assert record.versions[0].media_source_id == "ms-1"
    # 对方服务器路径与凭据绝不进记录。
    assert "/srv/secret" not in repr(record)
    assert "should-never-appear" not in repr(record)


def test_parse_emby_items_page_skips_idless_dirty_item():
    records, total = agg.parse_emby_items_page(
        {"Items": [{"Name": "no id"}, _movie("x")], "TotalRecordCount": 2},
        source_id="src-1",
    )
    assert [r.upstream_item_id for r in records] == ["x"]
    assert total == 2


def test_iter_emby_pages_stops_on_empty_page():
    calls = []

    def fetch_page(start, limit):
        calls.append((start, limit))
        if start == 0:
            return {"Items": [_movie("a")], "TotalRecordCount": 999}
        return {"Items": [], "TotalRecordCount": 999}

    records = list(
        agg.iter_emby_pages(fetch_page, source_id="src-1", page_size=100)
    )
    assert [r.upstream_item_id for r in records] == ["a"]
    assert len(calls) == 2  # 第二页空即停，不被说谎的总数拖进死循环


def test_aggregate_key_prefers_tmdb_then_title_year():
    assert agg.aggregate_key_for(_record("s", "a", tmdb="278")) == "tmdb:278"
    no_id = _record("s", "b", tmdb=None)
    no_id = agg.UpstreamCatalogRecord(
        source_id="s",
        upstream_item_id="b",
        name="肖申克的救赎",
        item_type="Movie",
        production_year=1994,
    )
    assert agg.aggregate_key_for(no_id).startswith("title:")
    assert "1994" in agg.aggregate_key_for(no_id)


def test_episode_key_includes_season_and_episode():
    ep1 = agg.parse_emby_item(
        {
            "Id": "e1",
            "Name": "E1",
            "Type": "Episode",
            "SeriesName": "莫语者",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
            "ProviderIds": {"Tmdb": "999"},
        },
        source_id="s",
    )
    ep2 = agg.parse_emby_item(
        {
            "Id": "e2",
            "Name": "E2",
            "Type": "Episode",
            "SeriesName": "莫语者",
            "ParentIndexNumber": 1,
            "IndexNumber": 2,
            "ProviderIds": {"Tmdb": "999"},
        },
        source_id="s",
    )
    assert agg.aggregate_key_for(ep1) != agg.aggregate_key_for(ep2)


def test_build_aggregates_same_work_two_sources_is_two_lines_not_versions():
    # 版本≠线路：同一作品两个源 = 一条聚合作品、两条线路；
    # 线路数绝不能被写成版本数。
    items = agg.build_aggregates(
        [_record("src-a", "a1"), _record("src-b", "b1")],
        category_bindings={"cat-1": "lib-local"},
    )
    assert len(items) == 1
    assert len(items[0].lines) == 2
    assert [line.line_key for line in items[0].lines] == [
        "upstream:src-a:a1",
        "upstream:src-b:b1",
    ]
    assert [line.line_index for line in items[0].lines] == [0, 1]
    assert items[0].category_binding == "lib-local"
    assert items[0].partition == "upstream:src-a"


def test_build_aggregates_same_source_two_items_is_two_lines():
    items = agg.build_aggregates([_record("src-a", "a1"), _record("src-a", "a2")])
    assert len(items) == 1
    assert len(items[0].lines) == 2


def test_build_aggregates_media_sources_are_versions_under_one_line():
    record = _record("src-a", "a1")
    record = agg.UpstreamCatalogRecord(
        source_id="src-a",
        upstream_item_id="a1",
        name="肖申克的救赎",
        item_type="Movie",
        production_year=1994,
        tmdb_id="278",
        versions=(
            agg.UpstreamVersion(media_source_id="ms-1", name="1080p"),
            agg.UpstreamVersion(media_source_id="ms-2", name="4K"),
        ),
    )
    items = agg.build_aggregates([record])
    assert len(items) == 1
    assert len(items[0].lines) == 1
    assert len(items[0].lines[0].versions) == 2
    # 新线路初始可用性一律 unknown，绝不虚报 available。
    assert items[0].lines[0].availability_status == agg.AVAILABILITY_UNKNOWN
    assert items[0].lines[0].last_checked_at is None
    # 播放地址不在聚合记录里（25-4 之前任何环节都不许落）。
    assert "play_url" not in repr(items[0])


def test_select_local_first_upstream_only_fills_missing():
    local = [
        agg.PresentationEntry(
            aggregate_key="tmdb:278", origin="local", name="肖申克的救赎"
        )
    ]
    upstream = agg.build_aggregates(
        [
            _record("src-a", "a1"),
            agg.parse_emby_item(
                {
                    "Id": "a2",
                    "Name": "另一部",
                    "Type": "Movie",
                    "ProductionYear": 2020,
                    "ProviderIds": {"Tmdb": "1000"},
                },
                source_id="src-a",
            ),
        ]
    )
    result = agg.select_for_presentation(
        local, upstream, aggregation_enabled=True
    )
    assert [e.origin for e in result] == ["local", "upstream:src-a"]
    assert result[0].aggregate_key == "tmdb:278"
    # 本地已有的作品，上游绝不顶掉、绝不插队到前面。
    assert result[1].aggregate_key == "tmdb:1000"


def test_select_disabled_is_local_only_verbatim():
    local = [
        agg.PresentationEntry(
            aggregate_key="tmdb:278", origin="local", name="肖申克的救赎"
        )
    ]
    upstream = agg.build_aggregates([_record("src-a", "a1")])
    assert agg.select_for_presentation(
        local, upstream, aggregation_enabled=False
    ) == local


def test_aggregate_sources_failure_isolated_to_that_source():
    results = agg.aggregate_sources(
        {
            "src-good": [_record("src-good", "g1")],
            "src-bad": RuntimeError("upstream 500"),
            "src-bad-str": "timeout",
        }
    )
    by_id = {r.source_id: r for r in results}
    assert len(by_id["src-good"].items) == 1
    assert by_id["src-good"].error is None
    assert by_id["src-bad"].items == []
    assert "500" in (by_id["src-bad"].error or "")
    assert by_id["src-bad-str"].items == []


def test_reconcile_empty_round_grace_then_withdraw():
    tracked = {}
    first = agg.reconcile(tracked, agg.build_aggregates([_record("s", "a1")]))
    assert first.added == ["tmdb:278"]
    tracked = first.tracked
    # 空轮 1、2：只记缺失，不下架。
    for _ in range(2):
        result = agg.reconcile(tracked, [])
        tracked = result.tracked
        assert result.withdrawn == []
        assert result.still_missing == ["tmdb:278"]
    # 空轮 3（默认宽限）：下架。
    result = agg.reconcile(tracked, [])
    assert result.withdrawn == ["tmdb:278"]
    assert result.tracked["tmdb:278"].withdrawn is True


def test_reconcile_failed_round_changes_nothing():
    first = agg.reconcile({}, agg.build_aggregates([_record("s", "a1")]))
    result = agg.reconcile(first.tracked, None, fetch_failed=True)
    assert result.round_trusted is False
    assert result.tracked["tmdb:278"].missing_rounds == 0
    assert result.added == [] and result.withdrawn == []


def test_reconcile_revival_keeps_external_ids():
    first = agg.reconcile({}, agg.build_aggregates([_record("s", "a1")]))
    tracked = first.tracked
    for _ in range(3):
        tracked = agg.reconcile(tracked, []).tracked
    assert tracked["tmdb:278"].withdrawn is True
    result = agg.reconcile(tracked, agg.build_aggregates([_record("s", "a1")]))
    assert result.tracked["tmdb:278"].withdrawn is False
    assert result.tracked["tmdb:278"].missing_rounds == 0
    assert result.tracked["tmdb:278"].tmdb_id == "278"


def test_reconcile_rejects_bad_grace():
    try:
        agg.reconcile({}, [], missing_grace_rounds=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")

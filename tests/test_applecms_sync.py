# -*- coding: utf-8 -*-
"""苹果 CMS 同步工人 W3 测试（只测 applecms_sync，不联网、不碰真实 DB）。

全部依赖用 fake 注入：抓取、绑定、本地候选、映射、健康、sleep。
本地 Python 3.12 + pyshim 口径，全量以 GitHub CI 为准。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest

from backend.emby_server import applecms_sync as sync


class FakeSource:
    def __init__(self, source_id=7):
        self.id = source_id
        self.base_url = "http://cms.example"
        self.headers = {}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "0")
    monkeypatch.setenv(sync.UPSTREAM_SYNC_ENABLED_ENV, "0")
    monkeypatch.setenv(sync.MAPPING_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.UNMATCHED_MODE_ENV, "parallel")
    monkeypatch.setenv(sync.PAGE_DELAY_ENV, "0.5")
    yield


def _enable(monkeypatch):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.UPSTREAM_SYNC_ENABLED_ENV, "1")


def _binding(category, enabled=True):
    return {"t": category, "sync_enabled": enabled}


def _cms_entry(vod_id, name, year, **extra):
    entry = {"vod_id": vod_id, "vod_name": name, "vod_year": year}
    entry.update(extra)
    return entry


def _page(entries, pagecount=1):
    return {"list": entries, "pagecount": pagecount}


def _run(source, *, fetch, bindings, locals_=None, maps=None, health=None,
         sleeps=None, **kwargs):
    maps = [] if maps is None else maps
    health = [] if health is None else health
    sleeps = [] if sleeps is None else sleeps
    return sync.sync_source(
        source,
        fetch_page=fetch,
        list_bindings=lambda src: bindings,
        list_locals=(lambda binding: locals_) if locals_ is not None else None,
        record_map=lambda **kw: maps.append(kw),
        set_health=lambda sid, h: health.append((sid, h)),
        sleep=sleeps.append,
        **kwargs,
    ), maps, health, sleeps


def test_gates_off_zero_side_effects(monkeypatch):
    calls = []

    def _fetch(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("两闸关闭时绝不许抓取")

    result, maps, health, _ = _run(
        FakeSource(), fetch=_fetch, bindings=[_binding(1)])
    assert result.health == "disabled"
    assert result.scanned == 0 and result.mapped == 0
    assert calls == [] and maps == [] and health == []


def test_only_one_gate_on_still_disabled(monkeypatch):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "1")
    result, maps, health, _ = _run(
        FakeSource(), fetch=lambda *a, **k: _page([]),
        bindings=[_binding(1)])
    assert result.health == "disabled"
    assert maps == [] and health == []


def test_only_sync_enabled_bindings_synced(monkeypatch):
    _enable(monkeypatch)
    seen = []

    def _fetch(source, category, page, h=None):
        seen.append(category)
        return _page([], pagecount=1)

    result, _, health, _ = _run(
        FakeSource(), fetch=_fetch,
        bindings=[_binding(1, True), _binding(2, False)])
    assert seen == [1]
    assert result.health == "healthy"
    assert health == [("7", "healthy")]


def test_title_year_match_confidence_capped(monkeypatch):
    _enable(monkeypatch)
    play_url = "http://secret.example/play/xxx.m3u8"
    entry = _cms_entry("vod-1", "肖申克的救赎", "1994",
                       vod_douban_id="999999",
                       vod_play_url=play_url)
    local = {"id": 101, "name": "肖申克的救赎", "production_year": 1994}
    result, maps, _, _ = _run(
        FakeSource(), fetch=lambda *a, **k: _page([entry]),
        bindings=[_binding(1)], locals_=[local])
    assert result.mapped == 1
    assert len(maps) == 1
    call = maps[0]
    assert call["evidence"] == "title+year"
    assert call["confidence"] == 0.7
    assert call["upstream_item_id"] == "vod-1"
    assert call["local_item_id"] == 101
    assert call["episode_index"] is None
    # 播放地址与豆瓣 id 绝不进映射调用参数。
    assert play_url not in repr(maps)
    assert "999999" not in repr(maps)


def test_series_episode_index_by_group_order(monkeypatch):
    _enable(monkeypatch)
    entry = _cms_entry("vod-9", "莫语者", "2023")
    locals_ = [
        {"id": 203, "name": "莫语者", "production_year": 2023,
         "item_type": "Episode", "episode_number": 3},
        {"id": 201, "name": "莫语者", "production_year": 2023,
         "item_type": "Episode", "episode_number": 1},
        {"id": 202, "name": "莫语者", "production_year": 2023,
         "item_type": "Episode", "episode_number": 2},
    ]
    result, maps, _, _ = _run(
        FakeSource(), fetch=lambda *a, **k: _page([entry]),
        bindings=[_binding(1)], locals_=locals_)
    assert result.mapped == 3
    # 组内按集号排，不从集标题文字解析：调用顺序即 1/2/3 集。
    assert [m["episode_index"] for m in maps] == [1, 2, 3]
    assert [m["local_item_id"] for m in maps] == [201, 202, 203]
    assert all(m["upstream_item_id"] == "vod-9" for m in maps)


def test_unmatched_parallel_and_list_listed(monkeypatch):
    _enable(monkeypatch)
    entry = _cms_entry("vod-2", "没本地的片", "2020")
    for mode in ("parallel", "list"):
        result, maps, _, _ = _run(
            FakeSource(), fetch=lambda *a, **k: _page([entry]),
            bindings=[_binding(1)], locals_=[],
            unmatched_mode=mode)
        assert result.mapped == 0 and maps == []
        assert len(result.unmatched) == 1 and result.hidden == 0


def test_unmatched_hide_excluded_and_counted(monkeypatch):
    _enable(monkeypatch)
    entry = _cms_entry("vod-3", "没本地的片", "2020",
                       vod_play_url="http://secret.example/play/hide.m3u8")
    result, maps, _, _ = _run(
        FakeSource(), fetch=lambda *a, **k: _page([entry]),
        bindings=[_binding(1)], locals_=[],
        unmatched_mode="hide")
    assert result.unmatched == [] and result.hidden == 1
    # 未匹配结果里也不许带播放地址（只记聚合键）。
    assert "secret.example" not in repr(result)


def test_year_missing_never_matched_on_title_alone(monkeypatch):
    _enable(monkeypatch)
    entry = _cms_entry("vod-4", "肖申克的救赎", "")
    local = {"id": 101, "name": "肖申克的救赎", "production_year": 1994}
    result, maps, _, _ = _run(
        FakeSource(), fetch=lambda *a, **k: _page([entry]),
        bindings=[_binding(1)], locals_=[local])
    assert maps == [] and result.mapped == 0
    assert len(result.unmatched) == 1


def test_detail_fills_missing_year(monkeypatch):
    _enable(monkeypatch)
    entry = _cms_entry("vod-5", "肖申克的救赎", "")
    local = {"id": 101, "name": "肖申克的救赎", "production_year": 1994}

    def _detail(source, vod_id):
        assert vod_id == "vod-5"
        return {"vod_id": "vod-5", "vod_name": "肖申克的救赎",
                "vod_year": "1994"}

    result, maps, _, _ = _run(
        FakeSource(), fetch=lambda *a, **k: _page([entry]),
        bindings=[_binding(1)], locals_=[local],
        fetch_detail=_detail)
    assert result.mapped == 1 and maps[0]["evidence"] == "title+year"


def test_429_backoff_retries_three_times_then_stops_source(monkeypatch):
    _enable(monkeypatch)
    attempts = []
    sleeps = []

    def _fetch(source, category, page, h=None):
        attempts.append(page)
        raise sync.RateLimitedError("429")

    result, _, health, _ = _run(
        FakeSource(), fetch=_fetch, bindings=[_binding(1), _binding(2)],
        sleeps=sleeps)
    # 首次 + 3 次重试 = 4 次尝试；每次重试前睡一次，共 3 次。
    assert len(attempts) == 4
    assert len(sleeps) == 3
    assert sleeps[0] < sleeps[1] < sleeps[2]
    # 第二个分类因停源未拉；错误已记。
    assert result.errors
    assert result.health == "unhealthy"
    assert health == [("7", "unhealthy")]


def test_single_run_entry_bound(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(sync, "MAX_ENTRIES_PER_RUN", 5)
    pages = []

    def _fetch(source, category, page, h=None):
        pages.append(page)
        return _page([_cms_entry(f"v{page}-{i}", "片", "2020")
                      for i in range(3)], pagecount=100)

    result, _, _, _ = _run(
        FakeSource(), fetch=_fetch, bindings=[_binding(1)], locals_=[])
    assert result.scanned == 5
    assert len(pages) == 2


def test_single_run_page_bound(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(sync, "MAX_PAGES_PER_CATEGORY", 2)
    pages = []

    def _fetch(source, category, page, h=None):
        pages.append(page)
        return _page([_cms_entry(f"v{page}", "片", "2020")], pagecount=100)

    result, _, _, _ = _run(
        FakeSource(), fetch=_fetch, bindings=[_binding(1)], locals_=[])
    assert len(pages) == 2
    assert any("页上限" in err for err in result.errors)


def test_health_per_pass_verdict(monkeypatch):
    _enable(monkeypatch)
    source = FakeSource(source_id=99)
    health = []
    state = {"ok": True}

    def _fetch(source_, category, page, h=None):
        if state["ok"]:
            return _page([], pagecount=1)
        raise RuntimeError("boom")

    def _once():
        return sync.sync_source(
            source, fetch_page=_fetch,
            list_bindings=lambda src: [_binding(1)],
            record_map=lambda **kw: None,
            set_health=lambda sid, h: health.append(h),
            sleep=lambda s: None)

    assert _once().health == "healthy"
    state["ok"] = False
    assert _once().health == "unhealthy"
    assert health[-1] == "unhealthy"
    state["ok"] = True
    assert _once().health == "healthy"


def test_mapping_gate_off_writes_nothing(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv(sync.MAPPING_ENABLED_ENV, "0")
    entry = _cms_entry("vod-6", "肖申克的救赎", "1994")
    local = {"id": 101, "name": "肖申克的救赎", "production_year": 1994}
    result, maps, _, _ = _run(
        FakeSource(), fetch=lambda *a, **k: _page([entry]),
        bindings=[_binding(1)], locals_=[local])
    assert maps == [] and result.mapped == 0
    assert result.health == "healthy" and result.scanned == 1


def test_h_incremental_param_passthrough(monkeypatch):
    _enable(monkeypatch)
    seen = []

    def _fetch(source, category, page, h=None):
        seen.append(h)
        return _page([], pagecount=1)

    _run(FakeSource(), fetch=_fetch, bindings=[_binding(1)], h="20261025080000")
    assert seen == ["20261025080000"]


def test_page_delay_sleep_between_pages(monkeypatch):
    _enable(monkeypatch)
    sleeps = []

    def _fetch(source, category, page, h=None):
        return _page([_cms_entry(f"v{page}", "片", "2020")], pagecount=2)

    _run(FakeSource(), fetch=_fetch, bindings=[_binding(1)], locals_=[],
         sleeps=sleeps)
    assert sleeps == [0.5]


def test_trigger_and_worker_gate(monkeypatch):
    _enable(monkeypatch)
    entry = _cms_entry("vod-7", "肖申克的救赎", "1994")
    local = {"id": 101, "name": "肖申克的救赎", "production_year": 1994}
    maps = []
    result = sync.trigger_applecms_sync(
        7,
        get_source=lambda sid: FakeSource(source_id=sid),
        fetch_page=lambda *a, **k: _page([entry]),
        list_bindings=lambda src: [_binding(1)],
        list_locals=lambda binding: [local],
        record_map=lambda **kw: maps.append(kw),
        set_health=lambda sid, h: None,
        sleep=lambda s: None,
    )
    assert result.mapped == 1 and maps[0]["upstream_item_id"] == "vod-7"

    # 两闸关闭：触发与工人都无副作用。
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "0")
    assert sync.trigger_applecms_sync(
        7, get_source=lambda sid: FakeSource(source_id=sid),
        fetch_page=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("disabled 时不许抓取")),
        list_bindings=lambda src: [_binding(1)],
        record_map=lambda **kw: None,
    ).health == "disabled"
    assert sync.start_applecms_sync_worker() is False

    # 两闸满足且给了源列出：工人跑一轮返回 True。
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "1")
    ran = []
    assert sync.start_applecms_sync_worker(
        list_sources=lambda: [FakeSource()],
        fetch_page=lambda *a, **k: _page([]),
        list_bindings=lambda src: [_binding(1)],
        record_map=lambda **kw: ran.append(kw),
        set_health=lambda sid, h: None,
        sleep=lambda s: None,
    ) is True

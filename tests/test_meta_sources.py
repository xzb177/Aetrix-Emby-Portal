"""多源元数据（Phase 6b）：引擎行为 + 配置读写 + 密钥池

全部离线：所有源都用替身，不发一个真实网络请求，不碰生产库。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """环境隔离：本机沙盒的 NO_PROXY 含 httpx 解析不了的条目（如 [::1]），
    构造 httpx.Client 会直接炸 InvalidURL。本模块测试从不需真实代理，
    这里先清掉（与 test_meta_key_storage 的同名 fixture 同口径）。"""
    for _var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(_var, raising=False)
    yield


from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import engine as ms_engine
from backend.emby_server.metasources import keypool as ms_keypool
from backend.emby_server.metasources import sources as ms_sources


# ---------------------------------------------------------------------------
# 替身：可控的源
# ---------------------------------------------------------------------------

class _Hit:
    """极简 hit 替身（只实现引擎用到的那几个属性）"""

    def __init__(self, **kwargs):
        for name in ("title", "original_title", "overview", "genres", "rating",
                     "year", "poster", "lang", "external_ids"):
            setattr(self, name, kwargs.get(name))
        if self.lang is None:
            self.lang = "en"
        if self.external_ids is None:
            self.external_ids = {}

    def has(self, name: str) -> bool:
        value = getattr(self, name, None)
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return value is not None


@pytest.fixture(autouse=True)
def _isolate_registry():
    """每个用例把自己的假源装进 ``SPEC_BY_ID``（引擎就是从那里找“怎么问这个源”），
    用完恢复 —— 不污染同文件里直接用真实源清单的用例"""
    original = dict(ms_sources.SPEC_BY_ID)
    yield
    ms_sources.SPEC_BY_ID.clear()
    ms_sources.SPEC_BY_ID.update(original)


def _spec(source_id: str, label: str, *, lang: str = "en",
          requires_key: bool = False, result=None, error: str = "") -> ms_sources.SourceSpec:
    def _search(title, year, kind, *, pool, gate):
        if error:
            raise ms_sources.SourceError(error)
        return result
    spec = ms_sources.SourceSpec(source_id, label, requires_key, lang, "测试源", _search)
    ms_sources.SPEC_BY_ID[source_id] = spec
    return spec


def _snapshot(*specs, enabled=True, prefer_chinese=True, **overrides):
    return ms_config.Snapshot(
        enabled=enabled,
        prefer_chinese=prefer_chinese,
        sources=[ms_config.SourceConfig(
            id=s.id, label=s.label, enabled=overrides.get(f"on_{s.id}", True),
            requires_key=s.requires_key, lang=s.lang, keys=overrides.get(f"keys_{s.id}", []),
            rate=0.0) for s in specs],
    )


# ---------------------------------------------------------------------------
# 1. 顺序：靠前的源先取，字段按序填充（不是一个源通吃）
# ---------------------------------------------------------------------------

def test_first_hit_wins_title_but_second_fills_missing_overview():
    """第一个源只给标题 → 简介应该从第二个源拿，而不是整条丢给第一个"""
    a = _spec("a", "甲", result=_Hit(title="中文名", overview=""))
    b = _spec("b", "乙", result=_Hit(title="English", overview="Plot"))
    result = ms_engine.collect(None, "片名", 2020, "movie", snapshot=_snapshot(a, b))

    assert result.fields["title"] == "中文名"
    assert result.fields["overview"] == "Plot"
    assert result.primary == "a"


def test_source_order_is_respected_when_two_both_have_same_field():
    a = _spec("a", "甲", result=_Hit(title="甲标题", overview="x"))
    b = _spec("b", "乙", result=_Hit(title="乙标题", overview="y"))
    snap = _snapshot(a, b)
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
    assert result.fields["title"] == "甲标题"

    # 顺序反过来，标题就该换成乙
    snap.sources.reverse()
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
    assert result.fields["title"] == "乙标题"


def test_disabled_source_is_not_asked_at_all():
    a = _spec("a", "甲", result=_Hit(title="甲"))
    b = _spec("b", "乙", result=_Hit(title="乙"))
    snap = _snapshot(a, b, on_b=False)
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
    assert result.fields["title"] == "甲"
    # 关掉的源也要在结果里说清楚为什么没问
    skipped = {o.source: o.skipped for o in result.outcomes if o.skipped}
    assert skipped == {"b": "已在后台关闭"}


def test_source_without_key_is_skipped_with_reason_not_error():
    a = _spec("a", "甲", requires_key=True, result=_Hit(title="甲"))
    b = _spec("b", "乙", result=_Hit(title="乙"))
    snap = _snapshot(a, b, keys_a=[])       # 要密钥但一把没配
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
    assert result.fields["title"] == "乙"
    skipped = [o for o in result.outcomes if o.skipped]
    assert skipped and skipped[0].source == "a" and "密钥" in skipped[0].skipped


# ---------------------------------------------------------------------------
# 2. 失败隔离：一个源挂了不影响其它源，失败原因原样带回
# ---------------------------------------------------------------------------

def test_source_failure_does_not_break_others_and_reason_is_kept():
    a = _spec("a", "甲", error="连接超时")
    b = _spec("b", "乙", result=_Hit(title="乙标题", overview="简介"))
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(a, b))

    assert result.fields["title"] == "乙标题"
    assert result.fields["overview"] == "简介"
    failed = [o for o in result.outcomes if not o.ok]
    assert len(failed) == 1 and failed[0].source == "a"
    assert "连接超时" in failed[0].error


def test_unexpected_exception_is_also_isolated():
    def _boom(title, year, kind, *, pool, gate):
        raise ValueError("炸了")

    spec = _spec("a", "甲", lang="zh")
    ms_sources.SPEC_BY_ID["a"] = ms_sources.SourceSpec("a", "甲", False, "zh", "测试", _boom)
    good = _spec("b", "乙", result=_Hit(title="乙"))
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(spec, good))
    assert result.fields["title"] == "乙"
    assert "ValueError" in [o.error for o in result.outcomes if not o.ok][0]


def test_all_sources_failing_returns_empty_but_reports():
    a = _spec("a", "甲", error="超时")
    b = _spec("b", "乙", error="超时")
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(a, b))
    assert result.fields == {}
    assert len([o for o in result.outcomes if not o.ok]) == 2


# ---------------------------------------------------------------------------
# 3. 中文优先开关
# ---------------------------------------------------------------------------

def test_prefer_chinese_promotes_chinese_title_over_higher_priority_english():
    # 语言由 hit 自己标（AniList 能看出 native 是不是中文），源属性只做默认值
    en = _spec("a", "甲", result=_Hit(title="English Name", overview="EN plot", lang="en"))
    zh = _spec("b", "乙", result=_Hit(title="中文名", overview="中文简介", lang="zh"))

    on = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(en, zh, prefer_chinese=True))
    assert on.fields["title"] == "中文名"
    assert on.fields["overview"] == "中文简介"

    off = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(en, zh, prefer_chinese=False))
    assert off.fields["title"] == "English Name"
    assert off.fields["overview"] == "EN plot"


def test_prefer_chinese_does_not_affect_types_or_rating():
    en = _spec("a", "甲", result=_Hit(title="EN", genres=["Drama"], rating=7.5, lang="en"))
    zh = _spec("b", "乙", result=_Hit(title="中文", genres=["剧情"], rating=9.1, lang="zh"))
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(en, zh, prefer_chinese=True))
    assert result.fields["title"] == "中文"
    # 类型与评分按原顺序（甲在前），中文优先不掺和
    assert result.fields["genres"] == ["Drama"]
    assert result.fields["rating"] == 7.5


# ---------------------------------------------------------------------------
# 4. 外部 ID 合并
# ---------------------------------------------------------------------------

def test_external_ids_are_merged_from_all_hits():
    a = _spec("a", "甲", result=_Hit(title="A", external_ids={"tmdb": "111", "imdb": "tt1"}))
    b = _spec("b", "乙", result=_Hit(title="B", external_ids={"douban": "222"}))
    c = _spec("c", "丙", result=_Hit(title="C", external_ids={"tmdb": "999"}))
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(a, b, c))
    # 同一个键保留**第一个**（靠前源说了算），不同键合并
    assert result.external_ids == {"tmdb": "111", "imdb": "tt1", "douban": "222"}


def test_external_ids_merge_survives_source_failure():
    a = _spec("a", "甲", error="超时")
    b = _spec("b", "乙", result=_Hit(title="B", external_ids={"tvmaze": "9"}))
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(a, b))
    assert result.external_ids == {"tvmaze": "9"}


# ---------------------------------------------------------------------------
# 5. 落库：只补缺项 / 外部 ID 回写独立列 / 不覆盖已有
# ---------------------------------------------------------------------------

def _item(**kwargs):
    base = dict(name="旧名字", overview="", genres="", production_year=None,
                community_rating=None, tmdb_id=None, imdb_id=None, external_ids=None,
                metadata_source=None, poster_path=None, primary_image_url=None)
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_apply_only_fills_missing_when_not_repair():
    item = _item(name="已有名字", overview="已有简介")
    result = ms_engine.CollectResult.from_dict({
        "fields": {"title": "源标题", "overview": "源简介", "genres": ["剧情"]},
        "external_ids": {"douban": "222"}, "primary": "douban"})
    changed = ms_engine.apply_to_item(item, result, fill_missing_only=True)
    assert item.name == "已有名字"
    assert item.overview == "已有简介"
    assert item.genres == "剧情"
    assert "name" not in changed and "overview" not in changed
    assert json.loads(item.external_ids) == {"douban": "222"}


def test_apply_overwrites_on_repair():
    item = _item(name="脏名字", overview="旧简介", metadata_source="nfo")
    result = ms_engine.CollectResult.from_dict({
        "fields": {"title": "正确名", "overview": "新简介"}, "primary": "douban"})
    changed = ms_engine.apply_to_item(item, result, fill_missing_only=False)
    assert item.name == "正确名"
    assert item.overview == "新简介"
    assert "name" in changed and "overview" in changed
    # 来源标记已有（NFO）就不覆盖
    assert item.metadata_source == "nfo"


def test_apply_writes_tmdb_and_imdb_columns_but_keeps_existing():
    item = _item(tmdb_id="60300")
    result = ms_engine.CollectResult.from_dict({
        "fields": {}, "external_ids": {"tmdb": "60400", "imdb": "tt7"}, "primary": "tmdb"})
    changed = ms_engine.apply_to_item(item, result, fill_missing_only=True)
    assert item.tmdb_id == "60300"          # 已有值不被覆盖
    assert item.imdb_id == "tt7"            # 空位被填上
    assert json.loads(item.external_ids) == {"tmdb": "60300", "imdb": "tt7"}
    assert "imdb_id" in changed


def test_apply_ignores_corrupt_external_ids_column():
    item = _item(external_ids="not-json")
    result = ms_engine.CollectResult.from_dict({
        "fields": {"title": "X"}, "external_ids": {"tvmaze": "1"}, "primary": "tvmaze"})
    ms_engine.apply_to_item(item, result, fill_missing_only=True)
    assert json.loads(item.external_ids) == {"tvmaze": "1"}


def test_apply_sets_poster_url_without_touching_local_file():
    item = _item()
    result = ms_engine.CollectResult.from_dict({
        "fields": {"poster": "http://img/p.jpg"}, "primary": "douban"})
    ms_engine.apply_to_item(item, result, fill_missing_only=True)
    assert item.primary_image_url == "http://img/p.jpg"
    assert item.poster_path is None


def test_apply_nothing_when_no_fields():
    item = _item()
    assert ms_engine.apply_to_item(item, ms_engine.CollectResult()) == []
    assert item.external_ids is None


# ---------------------------------------------------------------------------
# 6. 总开关 / 试采集开关
# ---------------------------------------------------------------------------

def test_switch_off_asks_nothing():
    asked = []

    def _spy(title, year, kind, *, pool, gate):
        asked.append(1)
        return None

    _spec("a", "甲", lang="zh")
    ms_sources.SPEC_BY_ID["a"] = ms_sources.SourceSpec("a", "甲", False, "zh", "测试", _spy)
    snap = _snapshot(ms_sources.SPEC_BY_ID["a"], enabled=False)
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap)
    assert asked == []
    assert result.fields == {} and result.outcomes == []


def test_probe_can_ask_a_disabled_source_without_switch(monkeypatch):
    spec = _spec("a", "甲", result=_Hit(title="甲标题"))
    snap = _snapshot(spec, enabled=False, on_a=False)
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap,
                               only="a", ignore_switch=True)
    assert result.fields["title"] == "甲标题"


def test_collect_limit_caps_how_many_sources_are_asked():
    specs = [_spec(f"s{i}", f"源{i}", result=_Hit(title=f"标题{i}")) for i in range(5)]
    snap = _snapshot(*specs)
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=snap, limit=2)
    assert len(result.outcomes) == 2


# ---------------------------------------------------------------------------
# 7. 配置：顺序解析与缺源自愈
# ---------------------------------------------------------------------------

def test_order_parsing_keeps_known_and_appends_missing():
    order = ms_config._parse_order('["tvdb", "不存在的源", "tmdb"]',
                                   [s.id for s in ms_sources.SPECS])
    assert order[:2] == ["tvdb", "tmdb"]
    assert "bangumi" in order and "不存在的源" not in order
    assert set(order) == {s.id for s in ms_sources.SPECS}


def test_order_parsing_survives_broken_json():
    order = ms_config._parse_order("{不是数组",
                                   [s.id for s in ms_sources.SPECS])
    assert order == ms_config.DEFAULT_ORDER


def test_ordered_filters_disabled_and_keyless():
    snap = _snapshot(
        _spec("a", "甲"),
        _spec("b", "乙", requires_key=True),
        _spec("c", "丙"),
        on_c=False, keys_b=[],
    )
    assert [s.id for s in snap.ordered()] == ["a"]


# ---------------------------------------------------------------------------
# 8. 密钥池：轮转 + 冷却 + 掩码
# ---------------------------------------------------------------------------

def test_pool_rotates_start_index_so_first_key_is_not_always_first():
    pool = ms_keypool.KeyPool("x", ["k1", "k2", "k3"])
    picked = [pool.pick() for _ in range(6)]
    assert picked == ["k1", "k2", "k3", "k1", "k2", "k3"]


def test_throttled_key_is_skipped_until_cooldown_expires():
    slept = []
    pool = ms_keypool.KeyPool("x", ["k1", "k2"], cooldown_sec=900)
    pool.note_throttled("k1")
    assert pool.pick() == "k2"
    # 全池冷却时不返回空：宁可撞限流也不能不发
    pool.note_throttled("k2")
    assert pool.pick_any() in ("k1", "k2")
    # 冷却记录与状态可见
    rows = pool.status()
    assert all(r["cooling"] for r in rows)
    assert "429" in rows[0]["reason"]
    assert pool.cooling_count() == 2
    assert pool.clear() == 2
    assert pool.cooling_count() == 0
    assert slept == []


def test_invalid_key_cools_longer():
    pool = ms_keypool.KeyPool("x", ["k1"])
    pool.note_invalid("k1")
    rows = pool.status()
    assert "401" in rows[0]["reason"]
    assert rows[0]["cooldown_remaining"] > 800


def test_status_never_leaks_raw_key():
    pool = ms_keypool.KeyPool("x", ["abcdef123456"])
    assert pool.status()[0]["masked"] == "****3456"
    assert "abcdef" not in json.dumps(pool.status())


def test_pool_for_reuses_pool_and_resize_drops_stale_cooldowns():
    ms_keypool.reset_all()
    pool = ms_keypool.pool_for("dup", ["a", "b"])
    pool.note_throttled("a")
    again = ms_keypool.pool_for("dup", ["a", "b"])
    assert again is pool
    assert again.cooling_count() == 1
    ms_keypool.pool_for("dup", ["b"])       # a 被删了
    assert again.cooling_count() == 0
    ms_keypool.reset_all()


# ---------------------------------------------------------------------------
# 9. 限速闸
# ---------------------------------------------------------------------------

def test_rate_gate_waits_between_calls():
    slept = []
    gate = ms_keypool.RateGate("x", 1.0)
    gate.acquire(sleep=slept.append)      # 第一次没有历史时间，不睡
    gate.acquire(sleep=slept.append)
    assert slept and all(s > 0 for s in slept)


def test_rate_gate_zero_is_unlimited():
    slept = []
    gate = ms_keypool.RateGate("x", 0.0)
    gate.acquire(sleep=slept.append)
    gate.acquire(sleep=slept.append)
    assert slept == []


# ---------------------------------------------------------------------------
# 10. 结果可序列化（IO 阶段与写库阶段之间要能传）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 11. 管理接口（直接调函数，不起 HTTP）：配置视图 + 密钥池增删
#     这些是前端那张卡直接消费的字段形状，钉住它们比钉住请求更重要
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """隔离的内存 SQLite（不连生产库）

    另外要清 ``store`` 的全局配置读缓存：它是**进程级、按 key 缓存**的，
    上一个用例写过的 ``meta_source_keys_tmdb`` 会泄进下一个用例的新库。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models as base_models
    from backend.integrations import store

    store.invalidate()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    store.invalidate()


def _view(db, **kwargs):
    from backend.api import admin_scrape
    return admin_scrape._meta_sources_view(db, **kwargs)


def test_view_lists_all_sources_in_order_with_masks_only(db):
    from backend.emby_server.metasources import config as cfg

    cfg.write_config(db, enabled=True, prefer_chinese=False,
                     order=["douban", "bangumi"], toggles={}, rates={})
    cfg.write_config(db, enabled=True, prefer_chinese=False,
                     order=["douban", "bangumi"], toggles={}, rates={},
                     keys={"tmdb": ["abcdef1234"]})
    # 2026-10-21 A 类摘面板：prefer_chinese 不再经 write_config 写，
    # 直接种存量行 "0" 表达「不优先中文」
    from backend.integrations import store as _store
    _store.write_values(db, {cfg.CONFIG_PREFER_CN: "0"}, {})
    db.commit()
    cfg.invalidate()
    view = _view(db)

    assert view["enabled"] is True
    assert view["prefer_chinese"] is False
    assert [r["id"] for r in view["sources"]][:2] == ["douban", "bangumi"]
    assert len(view["sources"]) == len(ms_sources.SPECS)
    tmdb_row = next(r for r in view["sources"] if r["id"] == "tmdb")
    assert tmdb_row["key_count"] == 1
    assert tmdb_row["keys"][0]["masked"] == "****1234"
    assert "abcdef" not in json.dumps(view)
    # 默认顺序里中文源在前（这是 Phase 6b 的治中文命中率那一招）
    assert view["default_order"][:2] == ["bangumi", "douban"]


def test_view_marks_disabled_and_keyless_sources(db):
    from backend.emby_server.metasources import config as cfg

    # 2026-10-21 A 类摘面板：逐源开关不再经 write_config 的 toggles 写，
    # 直接种存量行表达关闭
    from backend.integrations import store as _store
    _store.write_values(
        db, {cfg.CONFIG_SOURCE_ENABLED.format(id="tvmaze"): "0"}, {})
    db.commit()
    cfg.invalidate()
    rows = {r["id"]: r for r in _view(db)["sources"]}
    assert rows["tvmaze"]["skipped_reason"] == "已在后台关闭"
    assert rows["omdb"]["skipped_reason"] == "需要密钥但一个都没配"   # 要密钥没配
    assert rows["tvmaze"]["requires_key"] is False
    assert rows["omdb"]["requires_key"] is True


def test_view_active_count_counts_only_participating_sources(db):
    from backend.emby_server.metasources import config as cfg

    from backend.integrations import store as _store
    _store.write_values(
        db, {cfg.CONFIG_SOURCE_ENABLED.format(id=s): "0"
             for s in ("anilist", "tvmaze")}, {})
    # 豆瓣开关已收口到单一存储 douban_enabled（2026-10-23 去重），
    # 不再认 meta_source_enabled_douban
    _store.write_values(db, {"douban_enabled": "0"}, {})
    db.commit()
    cfg.invalidate()
    # 可参与 = 7 - 关掉的 3 - 缺密钥的 3（omdb/tvdb/tmdb）
    assert _view(db)["active_count"] == 1


def test_saving_config_does_not_wipe_key_pool(db):
    from backend.api import admin_scrape
    from backend.emby_server.metasources import config as cfg

    admin_scrape.add_meta_source_key(
        "omdb", admin_scrape.MetaSourceKeyAddRequest(key="key-one"), staff=None, db=db)
    admin_scrape.save_meta_sources(
        admin_scrape.MetaSourcesSaveRequest(enabled=True, prefer_chinese=False,
                                            order=["omdb", "douban"],
                                            toggles={"tvmaze": False}, rates={"omdb": 2.5}),
        staff=None, db=db)
    rows = {r["id"]: r for r in _view(db)["sources"]}
    assert rows["omdb"]["key_count"] == 1
    assert rows["omdb"]["rate"] == 2.5
    assert [r["id"] for r in _view(db)["sources"]][:2] == ["omdb", "douban"]
    # 2026-10-21 A 类摘面板：保存里的 toggles 不再生效，tvmaze 保持参与
    assert rows["tvmaze"]["enabled"] is True
    assert rows["douban"]["position"] == 2


def test_add_and_delete_source_key_by_index(db):
    from backend.api import admin_scrape

    admin_scrape.add_meta_source_key(
        "tvdb", admin_scrape.MetaSourceKeyAddRequest(key="aaaa1111"), staff=None, db=db)
    view = admin_scrape.add_meta_source_key(
        "tvdb", admin_scrape.MetaSourceKeyAddRequest(key="bbbb2222"), staff=None, db=db)
    row = next(r for r in view["sources"] if r["id"] == "tvdb")
    assert row["key_count"] == 2
    # 重复添加 → 409
    import pytest as _pytest
    with _pytest.raises(Exception):
        admin_scrape.add_meta_source_key(
            "tvdb", admin_scrape.MetaSourceKeyAddRequest(key="aaaa1111"), staff=None, db=db)
    # 按序号删第 1 把
    view = admin_scrape.delete_meta_source_key("tvdb", 1, staff=None, db=db)
    row = next(r for r in view["sources"] if r["id"] == "tvdb")
    assert row["key_count"] == 1
    assert row["keys"][0]["masked"] == "****2222"
    with _pytest.raises(Exception):
        admin_scrape.delete_meta_source_key("tvdb", 5, staff=None, db=db)


def test_key_test_cools_only_key_faults(db, monkeypatch):
    """一把真坏的进冷却；网络超时**不进**（不是密钥的锅，晾起来只会少一把能用的）"""
    from backend.api import admin_scrape

    def fake_search(title, year, kind, *, pool, gate):
        key = pool.pick_any()
        if key.startswith("AAAA"):
            return ms_sources.Hit(source="omdb", title="Some Movie")
        if key.startswith("BBBB"):
            raise ms_sources.SourceError("Error: Invalid API key!")
        raise ms_sources.SourceError("连接超时")

    original = ms_sources.SPEC_BY_ID["omdb"]
    ms_sources.SPEC_BY_ID["omdb"] = ms_sources.SourceSpec(
        "omdb", "OMDb（IMDb 数据）", True, "en", "替身", fake_search)
    try:
        for key in ("AAAA1111", "BBBB2222", "CCCC3333"):
            admin_scrape.add_meta_source_key(
                "omdb", admin_scrape.MetaSourceKeyAddRequest(key=key), staff=None, db=db)
        res = admin_scrape.test_meta_source_keys(
            "omdb", admin_scrape.MetaSourceProbeRequest(title="Some Movie", year=2020,
                                                        kind="movie"), staff=None, db=db)
        by_mask = {k["masked"]: k for k in
                   next(r for r in res["config"]["sources"] if r["id"] == "omdb")["keys"]}
        assert res["results"][0]["ok"] is True
        assert "搜到" in res["results"][0]["message"]
        assert by_mask["****1111"]["cooling"] is False          # 好的：待命
        assert by_mask["****2222"]["cooling"] is True           # 无效：冷却
        assert by_mask["****3333"]["cooling"] is False          # 超时：不冷却
        cleared = admin_scrape.reset_meta_source_cooldown("omdb", staff=None, db=db)
        assert cleared["cleared"] == 1
    finally:
        ms_sources.SPEC_BY_ID["omdb"] = original


def test_key_fault_classifier_separates_key_problems_from_network():
    from backend.api.admin_scrape import _is_key_fault

    for message in ("401", "Error: Invalid API key!", "token 已过期", "配额耗尽"):
        assert _is_key_fault(message) is True, message
    for message in ("连接超时", "HTTP 429 Too Many Requests", "被反爬拦截", "返回的不是 JSON"):
        assert _is_key_fault(message) is False, message


def test_unknown_source_is_rejected(db):
    from fastapi import HTTPException

    from backend.api import admin_scrape

    with pytest.raises(HTTPException) as err:
        admin_scrape.add_meta_source_key(
            "nope", admin_scrape.MetaSourceKeyAddRequest(key="abcdef12"), staff=None, db=db)
    assert err.value.status_code == 404


def test_collect_result_round_trips_through_dict():
    a = _spec("a", "甲", result=_Hit(title="甲标题", external_ids={"tmdb": "1"}))
    result = ms_engine.collect(None, "片名", None, "movie", snapshot=_snapshot(a))
    again = ms_engine.CollectResult.from_dict(result.as_dict())
    assert again.fields == result.fields
    assert again.external_ids == result.external_ids
    assert again.primary == result.primary == "a"
    assert again.outcomes[0].label == "甲"

# ---------------------------------------------------------------------------
# 12. TMDB-miss 兜底开关（v2.58.0）：配置读写 + 视图透出 + 密钥操作不动它
#     这个开关曾经「生产库里没有、页面上也没有」，谁都打不开 ——
#     这里把「默认关、写了生效、不写不动」三条全部钉住。
# ---------------------------------------------------------------------------

def test_tmdb_miss_fallback_defaults_off_and_round_trips(db):
    from backend.emby_server.metasources import config as cfg

    # 没写过 = 开（缺省保持 #1163 的开）
    cfg.write_config(db, enabled=True, prefer_chinese=True, order=[], toggles={})
    assert cfg.read_config(db, ms_sources.SPECS).tmdb_miss_fallback is True

    cfg.write_config(db, enabled=True, prefer_chinese=True, order=[], toggles={},
                     tmdb_miss_fallback=False)
    cfg.invalidate()
    assert cfg.read_config(db, ms_sources.SPECS).tmdb_miss_fallback is False

    cfg.write_config(db, enabled=True, prefer_chinese=True, order=[], toggles={},
                     tmdb_miss_fallback=True)
    cfg.invalidate()
    assert cfg.read_config(db, ms_sources.SPECS).tmdb_miss_fallback is True


def test_tmdb_miss_fallback_exposed_in_view(db):
    from backend.emby_server.metasources import config as cfg

    cfg.write_config(db, enabled=True, prefer_chinese=True, order=[], toggles={},
                     tmdb_miss_fallback=True)
    cfg.invalidate()
    view = _view(db)
    assert view["tmdb_miss_fallback"] is True


def test_tmdb_miss_fallback_survives_key_add_and_delete(db):
    """逐源增删密钥不能顺手把兜底开关关掉（不传 = 不写，保持原值）"""
    from backend.api import admin_scrape
    from backend.emby_server.metasources import config as cfg

    cfg.write_config(db, enabled=True, prefer_chinese=True, order=[], toggles={},
                     tmdb_miss_fallback=True)
    cfg.invalidate()

    admin_scrape.add_meta_source_key(
        "tvmaze", admin_scrape.MetaSourceKeyAddRequest(key="key123456"), staff=None, db=db)
    assert _view(db)["tmdb_miss_fallback"] is True

    admin_scrape.delete_meta_source_key("tvmaze", 1, staff=None, db=db)
    assert _view(db)["tmdb_miss_fallback"] is True


def test_save_meta_sources_carries_fallback_flag(db):
    """前端保存的那张卡把开关传下来、写进库、视图再吐出来 —— 全链路通"""
    from backend.api import admin_scrape

    view = admin_scrape.save_meta_sources(
        admin_scrape.MetaSourcesSaveRequest(enabled=True, prefer_chinese=True,
                                            tmdb_miss_fallback=True,
                                            order=[], toggles={}, rates={}),
        staff=None, db=db)
    assert view["tmdb_miss_fallback"] is True

    view = admin_scrape.save_meta_sources(
        admin_scrape.MetaSourcesSaveRequest(enabled=True, prefer_chinese=True,
                                            tmdb_miss_fallback=False,
                                            order=[], toggles={}, rates={}),
        staff=None, db=db)
    assert view["tmdb_miss_fallback"] is False


def test_save_without_flag_does_not_clobber_fallback(db):
    """老前端/脚本保存时没带该字段 → 开着的开关不能被一次保存关掉"""
    from backend.api import admin_scrape
    from backend.emby_server.metasources import config as cfg

    cfg.write_config(db, enabled=True, prefer_chinese=True, order=[], toggles={},
                     tmdb_miss_fallback=True)
    cfg.invalidate()

    view = admin_scrape.save_meta_sources(
        admin_scrape.MetaSourcesSaveRequest(enabled=True, prefer_chinese=True,
                                            order=[], toggles={}, rates={}),
        staff=None, db=db)
    assert view["tmdb_miss_fallback"] is True

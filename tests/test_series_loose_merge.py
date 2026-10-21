"""Q3 同剧拆条系统性治理回归测试（r29）。

D1 dedup.normalize_name_loose：回归对 / 虚词表 / 年份剥离 / 空串 / env 追加。
D2 scanner._claim_batch_guids series 防重：宽松复用 / 年份任一为空允许复用 /
    多 tmdb 歧义 key 不复用。
D3 merge_versions_worker._find_series_loose_groups：多 tmdb 否决 / 年份否决 /
    露露莉莉回归对 / 生产真拆条对 / 总开关。
D4 _follow_series_episodes：同集 pairwise 指主集 / 孤儿集 reparent。
D5 tmdb.search(loose_ok)：默认 False 零变化 / True 时宽松精确命中（mock）。
配置：series_merge_config 三层优先级（DB > 环境变量 > 缺省）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import dedup as dedup_lib
from backend.emby_server import merge_versions_worker as mvw
from backend.emby_server import models as em
import backend.models  # noqa: F401 — 注册 web_users 等表，供 create_all 建表
from backend.emby_server import scanner as _scanner
from backend.integrations import store as _store


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/q3.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    _store._ttl_cache.clear()
    s = Session()
    try:
        yield s
    finally:
        s.close()
        _store._ttl_cache.clear()


def _series(db, name, tmdb=None, year=None, lib=1):
    it = em.MediaItem(
        guid=f"g-{name}-{tmdb}-{year}-{lib}-{os.urandom(2).hex()}",
        library_id=lib, item_type="series", name=name,
        tmdb_id=tmdb, production_year=year, file_path="/x",
    )
    db.add(it)
    db.commit()
    return it


# ---------------- D1 ----------------

def test_loose_regression_pair():
    a = dedup_lib.normalize_name_loose("魔法的姐妹露露和莉莉 (2026)")
    b = dedup_lib.normalize_name_loose("魔法姐妹露露特莉莉")
    assert a == b == "魔法姐妹露露莉莉"


def test_loose_particles():
    for ch in "的之和与特·-＆&・･•":
        assert ch not in dedup_lib.normalize_name_loose(f"剧{ch}名")
    assert dedup_lib.normalize_name_loose("星球大战") == "星球大战"


def test_loose_year_strip():
    assert dedup_lib.normalize_name_loose("权欲第三章 (2021)") == "权欲第三章"
    assert dedup_lib.normalize_name_loose("权欲第三章2021") == "权欲第三章"
    # 非年份尾部数字不剥（集数/序号保护）
    assert dedup_lib.normalize_name_loose("24") == "24"
    assert dedup_lib.normalize_name_loose("") == ""


def test_loose_env_append(monkeypatch):
    monkeypatch.setenv("SERIES_LOOSE_PARTICLES", "XYZ")
    assert dedup_lib.normalize_name_loose("剧X名Y") == "剧名"


def test_strict_untouched():
    # 严格版行为零变化：虚词保留
    assert dedup_lib.normalize_name("魔法的姐妹露露和莉莉") == "魔法的姐妹露露和莉莉"


# ---------------- D3 ----------------

def _cfg(**kw):
    base = {"enabled": True, "loose_enabled": True, "require_year": True,
            "batch_limit": 1000}
    base.update(kw)
    return base


def test_loose_groups_regression_and_real_pairs(db):
    a = _series(db, "魔法的姐妹露露和莉莉", tmdb=None, year=2026)
    b = _series(db, "魔法姐妹露露特莉莉", tmdb="286361", year=2026)
    g = _series(db, "权欲第三章", tmdb="124394", year=2021)
    h = _series(db, "权欲第三章 (2021)", tmdb=None, year=2021)
    groups = mvw._find_series_loose_groups(db, _cfg())
    by = {frozenset(x.id for x in grp) for grp in groups}
    assert frozenset((a.id, b.id)) in by
    assert frozenset((g.id, h.id)) in by


def test_loose_groups_veto_multi_tmdb(db):
    c = _series(db, "星球大战", tmdb="11", year=1977)
    d = _series(db, "星球大战", tmdb="12", year=1977)
    groups = mvw._find_series_loose_groups(db, _cfg())
    for grp in groups:
        ids = {x.id for x in grp}
        assert not ({c.id, d.id} <= ids), "多 tmdb 组必须被守卫否决"


def test_loose_groups_veto_year(db):
    e = _series(db, "测试剧", tmdb=None, year=2020)
    f = _series(db, "测试剧", tmdb=None, year=2025)
    groups = mvw._find_series_loose_groups(db, _cfg())
    for grp in groups:
        ids = {x.id for x in grp}
        assert not ({e.id, f.id} <= ids), "年份 |Δ|>1 必须被否决"
    # require_year=0 时放宽的是"年份必须齐全"：一方为空即允许；
    # 但双方年份都在且 |Δ|>1 时 a/b 守卫照旧否决
    e2 = _series(db, "测试剧2", tmdb=None, year=2020)
    f2 = _series(db, "测试剧2", tmdb=None, year=None)
    groups3 = mvw._find_series_loose_groups(db, _cfg(require_year=False))
    by3 = {frozenset(x.id for x in grp) for grp in groups3}
    assert frozenset((e2.id, f2.id)) in by3
    groups4 = mvw._find_series_loose_groups(db, _cfg())
    for grp in groups4:
        ids = {x.id for x in grp}
        assert not ({e2.id, f2.id} <= ids), "=1 时年份缺失也否决"


def test_loose_groups_disabled(db):
    _series(db, "魔法的姐妹露露和莉莉", tmdb=None, year=2026)
    _series(db, "魔法姐妹露露特莉莉", tmdb="286361", year=2026)
    assert mvw._find_series_loose_groups(db, _cfg(enabled=False)) == []
    assert mvw._find_series_loose_groups(db, _cfg(loose_enabled=False)) == []


# ---------------- D4 ----------------

def _episode(db, series_id, season, ep, name):
    it = em.MediaItem(
        guid=f"e-{series_id}-{season}-{ep}-{os.urandom(2).hex()}",
        library_id=1, item_type="episode", name=name,
        series_id=series_id, season_number=season, episode_number=ep,
        file_path="/x",
    )
    db.add(it)
    db.commit()
    return it


def test_follow_series_episodes(db):
    p = _series(db, "魔法姐妹露露特莉莉", tmdb="286361", year=2026)
    d = _series(db, "魔法的姐妹露露和莉莉", tmdb=None, year=2026)
    p_e1 = _episode(db, p.id, 1, 1, "S01E01")
    p_e2 = _episode(db, p.id, 1, 2, "S01E02")
    d_e1 = _episode(db, d.id, 1, 1, "S01E01")
    d_e3 = _episode(db, d.id, 1, 3, "S01E03")  # 孤儿集：主 series 没有 E3
    group = [p, d]
    merged = mvw.merge_group(db, group)
    assert merged == 1
    mvw._follow_series_episodes(db, group)
    db.commit()
    db.refresh(d_e1)
    db.refresh(d_e3)
    # 同集 pairwise：不改 series_id，只指主集
    assert d_e1.merged_into_id == p_e1.id
    assert d_e1.series_id == d.id
    # 孤儿集 reparent 到主 series
    assert d_e3.series_id == p.id
    assert d_e3.merged_into_id is None
    # 主 series 自己的集不动
    db.refresh(p_e2)
    assert p_e2.merged_into_id is None and p_e2.series_id == p.id


# ---------------- D2 ----------------

def _pending(name, year, guid):
    return _scanner._Pending(
        scan_file=SimpleNamespace(stored_path=f"/x/{guid}.mp4"),
        guid=guid,
        parsed={"name": name, "year": year},
        item_type="series",
    )


def _ctx(lib_id=1):
    return _scanner._ScanContext(snap=None, lib_id=lib_id, stats={})


def test_claim_batch_series_loose_reuse(db):
    s1 = _series(db, "魔法姐妹露露特莉莉", tmdb="286361", year=2026)
    p1 = _pending("魔法的姐妹露露和莉莉", 2026, "pd2-1")
    _scanner._claim_batch_guids(db, [p1], _ctx())
    assert p1.claimed_id == s1.id, "宽松同剧应复用已有 series，不建新行"


def test_claim_batch_series_year_empty_reuse(db):
    s1 = _series(db, "无名剧", tmdb=None, year=None)
    p1 = _pending("无名剧", None, "pd2-2")
    _scanner._claim_batch_guids(db, [p1], _ctx())
    assert p1.claimed_id == s1.id, "年份任一为空允许复用"


def test_claim_batch_series_ambiguous_no_reuse(db):
    _series(db, "星球大战", tmdb="11", year=1977)
    _series(db, "星球大战", tmdb="12", year=1977)
    p1 = _pending("星球大战", 1977, "pd2-3")
    _scanner._claim_batch_guids(db, [p1], _ctx())
    assert p1.claimed_id is not None
    row = db.get(em.MediaItem, p1.claimed_id)
    # 歧义 key 不复用：应为新占位的行（不是 11/12 任一条）
    assert row.tmdb_id is None, "多 tmdb 歧义 key 不许复用已有行"


def test_claim_batch_series_new_when_no_match(db):
    p1 = _pending("从没见过的剧", 2024, "pd2-4")
    _scanner._claim_batch_guids(db, [p1], _ctx())
    assert p1.claimed_id is not None
    row = db.get(em.MediaItem, p1.claimed_id)
    assert row.name == "从没见过的剧"


def test_claim_batch_series_loose_disabled_by_master_switch(db, monkeypatch):
    _series(db, "魔法姐妹露露特莉莉", tmdb="286361", year=2026)
    monkeypatch.setenv("SERIES_MERGE_ENABLED", "0")
    _store._ttl_cache.clear()
    p1 = _pending("魔法的姐妹露露和莉莉", 2026, "pd2-5")
    _scanner._claim_batch_guids(db, [p1], _ctx())
    row = db.get(em.MediaItem, p1.claimed_id)
    assert row.tmdb_id is None, "总开关=0 时宽松复用全停"


# ---------------- D5 ----------------

def test_search_loose_ok_mock(monkeypatch):
    from backend.emby_server import tmdb as tmdb_mod

    client = tmdb_mod.TmdbClient()
    hit = {"id": 286361, "name": "魔法姐妹露露特莉莉",
           "original_name": "魔法姐妹露露特莉莉"}
    monkeypatch.setattr(client, "_search_raw",
                        lambda *a, **k: [hit])
    # 默认 False：严格通道搜不到 → None（现有行为零变化）
    assert client.search("魔法的姐妹露露和莉莉", 2026, "series") is None
    assert client.search("魔法的姐妹露露和莉莉", 2026, "series",
                         loose_ok=False) is None
    # True：宽松精确命中
    got = client.search("魔法的姐妹露露和莉莉", 2026, "series", loose_ok=True)
    assert got is not None and got["id"] == 286361


def test_search_loose_ok_never_fuzzy(monkeypatch):
    from backend.emby_server import tmdb as tmdb_mod

    client = tmdb_mod.TmdbClient()
    # 标题只有部分字相同 → 宽松归一化不等 → 不接受
    hit = {"id": 999, "name": "魔法姐妹",
           "original_name": "魔法姐妹"}
    monkeypatch.setattr(client, "_search_raw",
                        lambda *a, **k: [hit])
    assert client.search("魔法的姐妹露露和莉莉", 2026, "series",
                         loose_ok=True) is None


# ---------------- 配置 ----------------

def test_series_merge_config_defaults(db):
    cfg = mvw.series_merge_config(db)
    assert cfg == {"enabled": True, "loose_enabled": True,
                   "require_year": True, "interval_sec": 86400.0,
                   "batch_limit": 1000}


def test_series_merge_config_db_over_env(db, monkeypatch):
    # 2026-10-21 A 类摘面板后口径反转：开关类键统一 env > DB 存量行 >
    # 缺省（integrations.store 的 switch 钩子），服主关开关走 env 才算数
    import backend.models as bmodels
    db.add(bmodels.SystemConfig(key="series_merge_enabled", value="0"))
    db.commit()
    _store._ttl_cache.clear()
    monkeypatch.setenv("SERIES_MERGE_ENABLED", "1")
    cfg = mvw.series_merge_config(db)
    assert cfg["enabled"] is True, "env 优先于 DB 行（摘面板后统一口径）"


def test_series_merge_config_env_fallback(db, monkeypatch):
    monkeypatch.setenv("SERIES_MERGE_INTERVAL_SEC", "7200")
    _store._ttl_cache.clear()
    cfg = mvw.series_merge_config(db)
    assert cfg["interval_sec"] == 7200.0


def test_series_merge_config_bad_value_fallback(db, monkeypatch):
    monkeypatch.setenv("SERIES_MERGE_BATCH_LIMIT", "notanumber")
    _store._ttl_cache.clear()
    cfg = mvw.series_merge_config(db)
    assert cfg["batch_limit"] == 1000

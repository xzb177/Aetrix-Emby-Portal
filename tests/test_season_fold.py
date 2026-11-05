"""同步后自动按季折叠（season_fold）回归测试。

判定口径照 parent 2026-11-05 手工折叠验证版、自动化从严：
- 分组键 = 同库 + 底名（applecms_backfill.season_suffix_parts 剥尾部
  季号/动画版后缀与全部空白后，再走 dedup.normalize_name）；
- 只折明确标记行（第X季/第X部/Season N/Sxx），纯尾数不折；
- 逐行对正主判：TMDB 冲突 / 年份差 ≥2 / 集季号混装 → 跳过该行；
- 正主多候选（并列）→ 整组不折；
- 力学：改季号、集改挂正主季行、缺季新建、同季同集撞车成副本、
  既有副本改挂、子行改指、正主编号补空；幂等。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import applecms_backfill as bf
from backend.emby_server import merge_versions_worker as mvw
from backend.emby_server import models as em
from backend.emby_server import season_fold as sf
import backend.models  # noqa: F401 — 注册 web_users 等表
from backend.integrations import store as _store


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/sf.db",
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


def _cfg(**kw):
    base = {"enabled": True, "season_fold_enabled": True, "batch_limit": 1000}
    base.update(kw)
    return base


def _series(db, name, tmdb=None, year=None, lib=1, source_id=None):
    it = em.MediaItem(
        guid=f"s-{name}-{tmdb}-{year}-{lib}-{os.urandom(3).hex()}",
        library_id=lib, item_type="series", name=name,
        tmdb_id=tmdb, production_year=year, file_path="/x",
        upstream_source_id=source_id,
    )
    db.add(it)
    db.commit()
    return it


def _season(db, series, number):
    it = em.MediaItem(
        guid=f"sn-{series.id}-{number}-{os.urandom(3).hex()}",
        library_id=series.library_id, item_type="season",
        name=f"第 {number} 季", parent_id=series.id, series_id=series.id,
        season_number=number, upstream_source_id=series.upstream_source_id,
    )
    db.add(it)
    db.commit()
    return it


def _episode(db, series, season_row, season, ep_no, name=None):
    it = em.MediaItem(
        guid=f"e-{series.id}-{season}-{ep_no}-{os.urandom(3).hex()}",
        library_id=series.library_id, item_type="episode",
        name=name or f"E{ep_no}", parent_id=season_row.id if season_row else None,
        series_id=series.id, season_number=season, episode_number=ep_no,
        file_path="/x",
    )
    db.add(it)
    db.commit()
    return it


def _vis_eps(db, series_id):
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.series_id == series_id,
                    em.MediaItem.item_type == "episode",
                    em.MediaItem.merged_into_id.is_(None))
            .order_by(em.MediaItem.season_number, em.MediaItem.episode_number)
            .all())


# ---------------- 解析器（applecms_backfill 唯一实现） ----------------

def test_parser_buliangren_s4():
    base, season, marker = bf.season_suffix_parts("画江湖之不良人第四季 动画版")
    assert (base, season, marker) == ("画江湖之不良人", 4, "explicit")


def test_parser_space_variants():
    for name in ("画江湖之不良人 第四季", "画江湖之不良人　第四季",
                 "画江湖之不良人第四季", "画江湖之不良人 第 四 季"):
        base, season, marker = bf.season_suffix_parts(name)
        assert (base, season, marker) == ("画江湖之不良人", 4, "explicit"), name


def test_parser_digit_tail_not_explicit():
    base, season, marker = bf.season_suffix_parts("不良人7")
    assert season == 7 and marker == "digit"
    # 纯尾数不剥进底名：不许混进「不良人」组
    assert base == "不良人7"


def test_parser_bu_and_season_en():
    assert bf.season_suffix_parts("某剧第二部") == ("某剧", 2, "explicit")
    assert bf.season_suffix_parts("Some Show Season 3") == ("SomeShow", 3, "explicit")
    assert bf.season_suffix_parts("海贼王 S2") == ("海贼王", 2, "explicit")


def test_parser_no_marker_and_mid_name():
    assert bf.season_suffix_parts("普通剧名") == ("普通剧名", None, None)
    # 季号字样在名中部（非尾部）不动
    assert bf.season_suffix_parts("第一季的秘密") == ("第一季的秘密", None, None)
    # 整名就是后缀：不产出空底名
    base, season, marker = bf.season_suffix_parts("第三季")
    assert base and season is None


def test_parser_shared_impl_extended_bu():
    # 同一套实现：season_number_of / strip_season 同步认「第X部」
    assert bf.season_number_of("某剧第二部") == 2
    assert bf.strip_season("某剧第二部") == "某剧"
    assert bf.season_number_of("某剧第二季") == 2


# ---------------- 不良人形状：力学 + 安全阀 ----------------

def _buliangren(db):
    """总剧行含 S1+S7；二季挂另一编号（拦）；三季年份差大（拦）；
    四季无编号同季双行（折+撞车成副本）；五季无编号集已标 5（折）；
    六季无编号空壳带季行（折）。"""
    p = _series(db, "画江湖之不良人", tmdb="83463", year=2025)
    p_s1 = _season(db, p, 1)
    p_s7 = _season(db, p, 7)
    for i in (1, 2, 3):
        _episode(db, p, p_s1, 1, i)
    for i in (1, 2):
        _episode(db, p, p_s7, 7, i)
    # 二季：TMDB 冲突（216820 vs 83463）
    s2 = _series(db, "画江湖之不良人第二季 动画版", tmdb="216820", year=2025)
    s2_s = _season(db, s2, 1)
    for i in (1, 2):
        _episode(db, s2, s2_s, 1, i)
    # 三季：无编号但年份差 7（2018 vs 2025）
    s3 = _series(db, "画江湖之不良人第三季 动画版", tmdb=None, year=2018)
    s3_s = _season(db, s3, 1)
    for i in (1, 2):
        _episode(db, s3, s3_s, 1, i)
    # 四季 A 行：无编号，集标季 1
    s4 = _series(db, "画江湖之不良人第四季 动画版", tmdb=None, year=None)
    s4_s = _season(db, s4, 1)
    s4_e1 = _episode(db, s4, s4_s, 1, 1)
    s4_e2 = _episode(db, s4, s4_s, 1, 2)
    # 四季 B 行（同季双份，另一来源）：集与 A 行重号
    s4b = _series(db, "画江湖之不良人第四季", tmdb=None, year=None)
    s4b_s = _season(db, s4b, 1)
    s4b_e1 = _episode(db, s4b, s4b_s, 1, 1)
    s4b_e2 = _episode(db, s4b, s4b_s, 1, 2)
    # 已有子行：此前合并到四季 A 行的副本行
    child = _series(db, "画江湖之不良人第四季 动画版", tmdb=None, year=None)
    child.merged_into_id = s4.id
    db.commit()
    # 五季：无编号，集已标季 5（分布 == 目标季）
    s5 = _series(db, "画江湖之不良人第五季 动画版", tmdb=None, year=2024)
    s5_s = _season(db, s5, 5)
    s5_e1 = _episode(db, s5, s5_s, 5, 1)
    # 六季：无编号空壳（有季行、无集）
    s6 = _series(db, "画江湖之不良人第六季动画版", tmdb=None, year=None)
    s6_s = _season(db, s6, 1)
    return locals()


def test_fold_buliangren_mechanics(db):
    f = _buliangren(db)
    stats = sf.fold_season_groups(db, _cfg())
    assert stats["folded"] == 4  # 四季 A/B、五季、六季
    p = f["p"]
    # 正主集全景：S1 3 + S7 2 + S4 2(可见) + S5 1
    vis = _vis_eps(db, p.id)
    by_season = {}
    for e in vis:
        by_season.setdefault(e.season_number, []).append(e.episode_number)
    assert by_season[1] == [1, 2, 3]
    assert by_season[7] == [1, 2]
    assert by_season[4] == [1, 2]
    assert by_season[5] == [1]
    # 四季 A 的集改挂正主第 4 季行
    s4_season = (db.query(em.MediaItem)
                 .filter(em.MediaItem.series_id == p.id,
                         em.MediaItem.item_type == "season",
                         em.MediaItem.season_number == 4,
                         em.MediaItem.merged_into_id.is_(None)).one())
    db.refresh(f["s4_e1"])
    assert f["s4_e1"].series_id == p.id and f["s4_e1"].season_number == 4
    assert f["s4_e1"].parent_id == s4_season.id
    assert f["s4_e1"].merged_into_id is None
    # 四季 B 的同号集 → 集级合并副本，指向 A 行的存活集
    db.refresh(f["s4b_e1"])
    assert f["s4b_e1"].merged_into_id == f["s4_e1"].id
    assert f["s4b_e1"].series_id == p.id and f["s4b_e1"].season_number == 4
    # 被折行本体与季行
    db.refresh(f["s4"])
    assert f["s4"].merged_into_id == p.id
    db.refresh(f["s4_s"])
    assert f["s4_s"].merged_into_id == s4_season.id
    # 五季集原季号 5 保持、改挂正主
    db.refresh(f["s5_e1"])
    assert f["s5_e1"].series_id == p.id and f["s5_e1"].season_number == 5
    assert f["s5_e1"].merged_into_id is None
    # 六季空壳折叠，其季行并入正主新建的第 6 季
    db.refresh(f["s6"])
    assert f["s6"].merged_into_id == p.id
    s6_season = (db.query(em.MediaItem)
                 .filter(em.MediaItem.series_id == p.id,
                         em.MediaItem.item_type == "season",
                         em.MediaItem.season_number == 6,
                         em.MediaItem.merged_into_id.is_(None)).one())
    db.refresh(f["s6_s"])
    assert f["s6_s"].merged_into_id == s6_season.id
    # 子行改指正主
    db.refresh(f["child"])
    assert f["child"].merged_into_id == p.id


def test_fold_buliangren_safety_valves(db):
    f = _buliangren(db)
    sf.fold_season_groups(db, _cfg())
    # 二季（TMDB 冲突）不折、集不动
    db.refresh(f["s2"])
    assert f["s2"].merged_into_id is None
    assert len(_vis_eps(db, f["s2"].id)) == 2
    # 三季（年份差 ≥2）不折
    db.refresh(f["s3"])
    assert f["s3"].merged_into_id is None
    assert len(_vis_eps(db, f["s3"].id)) == 2


def test_fold_idempotent(db):
    _buliangren(db)
    first = sf.fold_season_groups(db, _cfg())
    assert first["folded"] == 4
    second = sf.fold_season_groups(db, _cfg())
    assert second["folded"] == 0
    assert second["episodes_moved"] == 0 and second["episodes_copied"] == 0


# ---------------- 其余安全阀 ----------------

def test_digit_tail_row_not_folded(db):
    p = _series(db, "机甲战士", tmdb=None, year=2023)
    ps = _season(db, p, 1)
    _episode(db, p, ps, 1, 1)
    digit = _series(db, "机甲战士2", tmdb=None, year=2023)
    ds = _season(db, digit, 1)
    _episode(db, digit, ds, 1, 1)
    s2 = _series(db, "机甲战士第二季", tmdb=None, year=2023)
    s2s = _season(db, s2, 1)
    _episode(db, s2, s2s, 1, 1)
    stats = sf.fold_season_groups(db, _cfg())
    assert stats["folded"] == 1  # 只有明确标记的第二季行
    db.refresh(digit)
    assert digit.merged_into_id is None
    db.refresh(s2)
    assert s2.merged_into_id == p.id


def test_mixed_distribution_not_folded(db):
    p = _series(db, "混装剧", tmdb=None, year=2023)
    s8 = _series(db, "混装剧第八季", tmdb=None, year=2023)
    s8s = _season(db, s8, 1)
    _episode(db, s8, s8s, 1, 1)
    _episode(db, s8, s8s, 2, 2)  # 季号混装（1 与 2 并存，目标季 8）
    stats = sf.fold_season_groups(db, _cfg())
    assert stats["folded"] == 0
    db.refresh(s8)
    assert s8.merged_into_id is None


def test_ambiguous_primary_group_skipped(db):
    a = _series(db, "双行剧", tmdb=None, year=2023)
    as_ = _season(db, a, 1)
    _episode(db, a, as_, 1, 1)
    b = _series(db, "双行剧", tmdb=None, year=2023)
    bs = _season(db, b, 1)
    _episode(db, b, bs, 1, 1)
    s2 = _series(db, "双行剧第二季", tmdb=None, year=2023)
    s2s = _season(db, s2, 1)
    _episode(db, s2, s2s, 1, 1)
    stats = sf.fold_season_groups(db, _cfg())
    assert stats["folded"] == 0, "正主并列多候选 → 整组不折"
    db.refresh(s2)
    assert s2.merged_into_id is None


def test_primary_elected_by_episode_count(db):
    small = _series(db, "择主剧", tmdb=None, year=2023)
    ss = _season(db, small, 1)
    _episode(db, small, ss, 1, 1)
    big = _series(db, "择主剧", tmdb=None, year=2023)
    bs = _season(db, big, 1)
    for i in (1, 2, 3):
        _episode(db, big, bs, 1, i)
    s2 = _series(db, "择主剧第二季", tmdb=None, year=2023)
    s2s = _season(db, s2, 1)
    _episode(db, s2, s2s, 1, 1)
    stats = sf.fold_season_groups(db, _cfg())
    assert stats["folded"] == 1
    db.refresh(s2)
    assert s2.merged_into_id == big.id
    db.refresh(small)
    assert small.merged_into_id is None, "未标记行不折（不是季行）"


def test_year_boundary(db):
    p = _series(db, "年份剧", tmdb=None, year=2020)
    ok = _series(db, "年份剧第二季", tmdb=None, year=2021)  # 差 1 → 折
    oks = _season(db, ok, 1)
    _episode(db, ok, oks, 1, 1)
    ng = _series(db, "年份剧第三季", tmdb=None, year=2022)  # 差 2 → 不折
    ngs = _season(db, ng, 1)
    _episode(db, ng, ngs, 1, 1)
    stats = sf.fold_season_groups(db, _cfg())
    assert stats["folded"] == 1
    db.refresh(ok)
    assert ok.merged_into_id == p.id
    db.refresh(ng)
    assert ng.merged_into_id is None


def test_same_season_year_conflict_both_skipped(db):
    p = _series(db, "同季剧", tmdb=None, year=None)
    a = _series(db, "同季剧第三季", tmdb=None, year=2018)
    as_ = _season(db, a, 1)
    _episode(db, a, as_, 1, 1)
    b = _series(db, "同季剧第三季 动画版", tmdb=None, year=2021)
    bs = _season(db, b, 1)
    _episode(db, b, bs, 1, 1)
    stats = sf.fold_season_groups(db, _cfg())
    assert stats["folded"] == 0, "同季两行年份差 ≥2 → 两行都跳过"
    s4 = _series(db, "同季剧第四季", tmdb=None, year=None)
    s4s = _season(db, s4, 1)
    _episode(db, s4, s4s, 1, 1)
    stats2 = sf.fold_season_groups(db, _cfg())
    assert stats2["folded"] == 1, "同季冲突不拖累其它季"


def test_tmdb_fill_when_group_consistent(db):
    p = _series(db, "补号剧", tmdb=None, year=2023)
    ps = _season(db, p, 1)
    _episode(db, p, ps, 1, 1)
    _episode(db, p, ps, 1, 2)
    s2 = _series(db, "补号剧第二季", tmdb="999", year=2023)
    s2s = _season(db, s2, 1)
    _episode(db, s2, s2s, 1, 1)
    s3 = _series(db, "补号剧第三季", tmdb="999", year=2023)
    s3s = _season(db, s3, 1)
    _episode(db, s3, s3s, 1, 1)
    sf.fold_season_groups(db, _cfg())
    db.refresh(p)
    assert p.tmdb_id == "999", "正主缺号且全组编号一致 → 补空"


def test_tmdb_no_fill_when_group_conflict(db):
    p = _series(db, "不补号剧", tmdb=None, year=2023)
    ps = _season(db, p, 1)
    _episode(db, p, ps, 1, 1)
    _episode(db, p, ps, 1, 2)
    s2 = _series(db, "不补号剧第二季", tmdb="999", year=2023)
    s2s = _season(db, s2, 1)
    _episode(db, s2, s2s, 1, 1)
    s3 = _series(db, "不补号剧第三季", tmdb="888", year=2023)
    s3s = _season(db, s3, 1)
    _episode(db, s3, s3s, 1, 1)
    sf.fold_season_groups(db, _cfg())
    db.refresh(p)
    assert not p.tmdb_id, "全组编号不一致 → 不补"


def test_copies_rebased(db):
    p = _series(db, "副本剧", tmdb=None, year=2023)
    ps1 = _season(db, p, 1)
    _episode(db, p, ps1, 1, 1)
    s2 = _series(db, "副本剧第二季", tmdb=None, year=2023)
    s2s = _season(db, s2, 1)
    main_ep = _episode(db, s2, s2s, 1, 1)
    copy_ep = _episode(db, s2, s2s, 1, 1, name="副本")
    copy_ep.merged_into_id = main_ep.id
    db.commit()
    sf.fold_season_groups(db, _cfg())
    db.refresh(copy_ep)
    db.refresh(main_ep)
    assert main_ep.series_id == p.id and main_ep.season_number == 2
    assert copy_ep.series_id == p.id and copy_ep.season_number == 2
    assert copy_ep.merged_into_id == main_ep.id, "既有副本随主集改挂、指向不变"


def test_hosted_primary_season_guid(db):
    from backend.emby_server.upstream_import import hosted_season_guid
    p = _series(db, "托管剧", tmdb=None, year=2023, source_id=9)
    s2 = _series(db, "托管剧第二季", tmdb=None, year=2023, source_id=9)
    s2s = _season(db, s2, 1)
    _episode(db, s2, s2s, 1, 1)
    sf.fold_season_groups(db, _cfg())
    created = (db.query(em.MediaItem)
               .filter(em.MediaItem.series_id == p.id,
                       em.MediaItem.item_type == "season",
                       em.MediaItem.season_number == 2,
                       em.MediaItem.merged_into_id.is_(None)).one())
    assert created.guid == hosted_season_guid(9, p.id, 2), \
        "托管正主新建季行须用托管 guid 锚，与进库幂等键一致"


def test_switch_off_noop(db):
    _buliangren(db)
    stats = sf.fold_season_groups(db, _cfg(season_fold_enabled=False))
    assert stats["folded"] == 0
    stats = sf.fold_season_groups(db, _cfg(enabled=False))
    assert stats["folded"] == 0


# ---------------- 配置 ----------------

def test_config_default_and_env(db, monkeypatch):
    cfg = mvw.series_merge_config(db)
    assert cfg["season_fold_enabled"] is True
    monkeypatch.setenv("SERIES_MERGE_SEASON_FOLD_ENABLED", "0")
    _store._ttl_cache.clear()
    cfg = mvw.series_merge_config(db)
    assert cfg["season_fold_enabled"] is False

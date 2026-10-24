# -*- coding: utf-8 -*-
"""拼音搜索派生字段（2026-10-24，FMBY 学习批二 #16）。

此前拼音只有首字母、且寄生在 sort_name 上（排序键兼职搜索，关掉拼音
排序开关就跟着哑）。本批把全拼 + 首字母做成 emby_items 的正式落库字段
（pinyin_full / pinyin_initials）：落库钩子按 name 生成、存量靠回填脚本
补齐、搜索预筛与精排新增拼音层（分值低于原文各层，只做加分项）。

钉住的不变量：
- 同一条目，原文 / 全拼 / 首字母三种打法都能命中（灵魂验收 1）；
- 拼音字段是落库的真实值，由钩子随 name 同步（灵魂验收 2）；
- 原文搜索结果与排序与现行一致、英文标题不受影响（灵魂验收 3）；
- 回填幂等：重复跑不改已填行。

全部离线 sqlite，不做真实网络与数据库请求。
"""
import importlib.util
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest  # noqa: F401
from sqlalchemy import create_engine, or_, text
from sqlalchemy.orm import sessionmaker

from backend.emby_server import models as em
import backend.models  # noqa: F401  # 解决跨 schema 外键的 create_all
from backend.emby_server.pinyin_sort import make_pinyin_fields, pinyin_full
from backend.emby_server.search import (
    TIER_ALIAS_CONTAINS,
    rank_items,
    score_item,
    search_variants,
)


def _load_backfill_module():
    """按路径加载回填脚本（同 test_enrich_observability 的先例）。"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "backfill_pinyin_search_fields.py")
    spec = importlib.util.spec_from_file_location(
        "backfill_pinyin_search_fields", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def db():
    path = "/tmp/test_pinyin_fields.db"
    if os.path.exists(path):
        os.remove(path)
    eng = create_engine(f"sqlite:///{path}")
    em.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    # 用 ORM 建行：拼音字段应由落库钩子按 name 自动生成
    s.add_all([
        em.MediaItem(guid="g1", library_id=1, item_type="movie",
                     name="流浪地球", is_hidden=False),
        em.MediaItem(guid="g2", library_id=1, item_type="movie",
                     name="流浪地球2", is_hidden=False),
        em.MediaItem(guid="g3", library_id=1, item_type="movie",
                     name="肖申克的救赎", is_hidden=False),
        em.MediaItem(guid="g4", library_id=1, item_type="movie",
                     name="The Shawshank Redemption", is_hidden=False),
        em.MediaItem(guid="g5", library_id=1, item_type="movie",
                     name="长津湖", is_hidden=False),
    ])
    s.commit()
    yield s
    s.close()


def _search(db, term):
    """与 _query_items / search_hints 同口径的 SQL 预筛（六列）+ 精排"""
    clauses = []
    for v in search_variants(term):
        like = f"%{v}%"
        clauses.extend([
            em.MediaItem.name.ilike(like),
            em.MediaItem.original_title.ilike(like),
            em.MediaItem.sort_name.ilike(like),
            em.MediaItem.aliases.ilike(like),
            em.MediaItem.pinyin_full.ilike(like),
            em.MediaItem.pinyin_initials.ilike(like),
        ])
    rows = db.query(em.MediaItem).filter(or_(*clauses)).all() if clauses else []
    return [r.name for r in rank_items(rows, term)]


# ---------- 生成函数 ----------

def test_make_pinyin_fields_values():
    assert make_pinyin_fields("流浪地球") == ("liulangdiqiu", "lldq")
    assert make_pinyin_fields("肖申克的救赎") == ("xiaoshenkedejiushu", "xskdjs")
    # 多音字取 pypinyin 默认读音（长的 zhang 读法），与 sort_name 口径一致
    assert make_pinyin_fields("长津湖") == ("zhangjinhu", "zjh")
    # 纯英文：紧凑小写（与排序键保留空格的口径不同，搜索键一律紧凑）
    assert make_pinyin_fields("The Shawshank Redemption") == (
        "theshawshankredemption", "theshawshankredemption")
    assert make_pinyin_fields("") == ("", "")
    assert make_pinyin_fields(None) == ("", "")
    assert pinyin_full("流浪地球") == "liulangdiqiu"


# ---------- 落库钩子 ----------

def test_hook_fills_fields_on_insert(db):
    item = db.query(em.MediaItem).filter_by(guid="g1").one()
    assert item.pinyin_full == "liulangdiqiu"
    assert item.pinyin_initials == "lldq"
    en = db.query(em.MediaItem).filter_by(guid="g4").one()
    assert en.pinyin_full == "theshawshankredemption"


def test_hook_follows_rename_on_update(db):
    item = db.query(em.MediaItem).filter_by(guid="g1").one()
    item.name = "流浪地球前传"
    db.commit()
    db.expire_all()
    item = db.query(em.MediaItem).filter_by(guid="g1").one()
    assert item.pinyin_full == "liulangdiqiuqianchuan"
    assert item.pinyin_initials == "lldqqc"


# ---------- 三种打法命中同一条（灵魂验收 1） ----------

def test_three_ways_hit_same_item(db):
    assert _search(db, "流浪地球")[0] == "流浪地球"          # 原文
    assert _search(db, "liulangdiqiu")[0] == "流浪地球"     # 全拼
    assert _search(db, "lldq")[0] == "流浪地球"             # 首字母


def test_full_pinyin_prefix_and_contains(db):
    assert "流浪地球" in _search(db, "liulang")   # 全拼前缀
    assert "流浪地球" in _search(db, "diqiu")     # 全拼包含
    assert "肖申克的救赎" in _search(db, "xiaoshenke")


def test_polyphone_default_reading_hit(db):
    # 「长」按默认读音 zhang：zjh / zhangjinhu 都能命中长津湖
    assert "长津湖" in _search(db, "zjh")
    assert "长津湖" in _search(db, "zhangjinhu")


def test_pinyin_no_false_hits(db):
    names = _search(db, "lldq")
    assert "肖申克的救赎" not in names
    assert "长津湖" not in names
    assert "The Shawshank Redemption" not in names
    assert _search(db, "buxidede") == []


# ---------- 原文语义不回退（灵魂验收 3） ----------

def test_chinese_original_still_hits(db):
    # 拼音串不含中文：中文原名命中只能靠原文通道，钉住它仍在
    assert _search(db, "救赎") == ["肖申克的救赎"]
    names = _search(db, "流浪地球")
    assert names[0] == "流浪地球" and "流浪地球2" in names


def test_english_unaffected(db):
    assert _search(db, "shawshank") == ["The Shawshank Redemption"]
    # 英文条目的紧凑全拼形也能命中它自己，且不误伤中文条目
    assert _search(db, "theshawshankredemption") == ["The Shawshank Redemption"]


def test_pinyin_tier_below_original(db):
    # 拼音层分值必须低于原文别名包含层：拼音只做加分项
    item = db.query(em.MediaItem).filter_by(guid="g1").one()
    score, level = score_item(item, "liulangdiqiu")
    assert level == "pinyin_full"
    assert 0 < score < TIER_ALIAS_CONTAINS
    score_i, level_i = score_item(item, "lldq")
    assert level_i in ("pinyin_initials", "sort_name")  # 首字母另有旧通道
    assert score_i < 70  # 低于标题包含层


# ---------- 回填幂等 ----------

def test_backfill_idempotent(db):
    mod = _load_backfill_module()
    # Core 直插绕开钩子，造两行拼音为 NULL 的存量行
    db.execute(text(
        "INSERT INTO emby_items (guid, library_id, item_type, name, is_hidden)"
        " VALUES ('g10',1,'movie','寻龙诀',0),('g11',1,'movie','Avatar',0)"))
    db.commit()

    stats = mod.backfill_pinyin_fields(db, batch_size=2)
    assert stats["filled"] == 2
    row = db.query(em.MediaItem).filter_by(guid="g10").one()
    assert (row.pinyin_full, row.pinyin_initials) == ("xunlongjue", "xlj")
    row = db.query(em.MediaItem).filter_by(guid="g11").one()
    assert (row.pinyin_full, row.pinyin_initials) == ("avatar", "avatar")

    # 第二遍：已填行被查询条件排除，一行都不许再动
    stats2 = mod.backfill_pinyin_fields(db)
    assert stats2 == {"scanned": 0, "filled": 0}
    row = db.query(em.MediaItem).filter_by(guid="g10").one()
    assert (row.pinyin_full, row.pinyin_initials) == ("xunlongjue", "xlj")

    # 回填后拼音可搜（灵魂验收 1 在存量行上同样成立）
    assert "寻龙诀" in _search(db, "xunlongjue")
    assert "寻龙诀" in _search(db, "xlj")

    # 单列被改回 NULL 时只补这一行
    db.execute(text(
        "UPDATE emby_items SET pinyin_full=NULL WHERE guid='g10'"))
    db.commit()
    stats3 = mod.backfill_pinyin_fields(db)
    assert stats3["filled"] == 1
    row = db.query(em.MediaItem).filter_by(guid="g10").one()
    assert row.pinyin_full == "xunlongjue"

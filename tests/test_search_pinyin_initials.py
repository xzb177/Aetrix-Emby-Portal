# -*- coding: utf-8 -*-
"""拼音首字母搜片（2026-10-17，用户批准，对标 raisemby）。

sort_name 存的是无空格拼音首字母串（"长津湖"→"cjh"），搜索在 SQL 预筛
阶段只拿原样词去 ILIKE —— 用户在手机键盘上常带空格输入（"c j h"），
预筛捞不到候选。本改动在 search_variants 里给纯 ASCII 字母输入追加
去空格小写变体，两个搜索出口（/Items?SearchTerm=、/Search/Hints）
共用同一构造，一处改全覆盖。

全部离线 sqlite，不做真实网络与数据库请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest  # noqa: F401
from sqlalchemy import create_engine, or_, text
from sqlalchemy.orm import sessionmaker

from backend.emby_server import models as em
import backend.models  # noqa: F401  # 解决跨 schema 外键的 create_all
from backend.emby_server.search import rank_items, search_variants


@pytest.fixture()
def db():
    path = "/tmp/test_pinyin_search.db"
    if os.path.exists(path):
        os.remove(path)
    eng = create_engine(f"sqlite:///{path}")
    em.Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO emby_items (guid, library_id, item_type, name, sort_name, is_hidden) VALUES "
            "('g1',1,'movie','长津湖','cjh',0),"
            "('g2',1,'movie','长津湖之水门桥','cjhzsmq',0),"
            "('g3',1,'movie','The Shawshank Redemption','the shawshank redemption',0),"
            "('g4',1,'movie','肖申克的救赎','xskdjsh',0),"
            "('g5',1,'movie','长安十二时辰','cajesesc',0)"))
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _search(db, term):
    """与 _query_items / search_hints 同口径的 SQL 预筛 + 精排"""
    clauses = []
    for v in search_variants(term):
        like = f"%{v}%"
        clauses.extend([
            em.MediaItem.name.ilike(like),
            em.MediaItem.original_title.ilike(like),
            em.MediaItem.sort_name.ilike(like),
            em.MediaItem.aliases.ilike(like),
        ])
    rows = db.query(em.MediaItem).filter(or_(*clauses)).all() if clauses else []
    return [r.name for r in rank_items(rows, term)]


# ---------- variants 层面 ----------

def test_spaced_pinyin_adds_compact_variant():
    assert "cjh" in search_variants("c j h")


def test_plain_pinyin_unchanged():
    assert search_variants("cjh") == ["cjh"]


def test_chinese_term_unchanged():
    assert "cjh" not in search_variants("长津湖")


def test_mixed_term_unchanged():
    # 中英混合输入不追加（维持原逻辑零变化）
    assert search_variants("长津湖 cjh") == ["长津湖 cjh"]


def test_single_letter_not_added():
    assert search_variants("c") == ["c"]


# ---------- 端到端 ----------

def test_full_initials_hit(db):
    names = _search(db, "cjh")
    assert "长津湖" in names and "长津湖之水门桥" in names


def test_spaced_initials_hit(db):
    # 手机键盘带空格输入：之前预筛捞不到候选
    names = _search(db, "c j h")
    assert "长津湖" in names and "长津湖之水门桥" in names


def test_uppercase_initials_hit(db):
    assert "长津湖" in _search(db, "CJH")


def test_partial_initials_hit(db):
    assert _search(db, "xsk") == ["肖申克的救赎"]


def test_pinyin_does_not_hit_unrelated(db):
    # 反例：cjh 不许误命中首字母串不含它的条目
    names = _search(db, "cjh")
    assert "肖申克的救赎" not in names
    assert "The Shawshank Redemption" not in names
    assert "长安十二时辰" not in names


def test_chinese_term_regression(db):
    names = _search(db, "长津湖")
    assert "长津湖" in names and "长津湖之水门桥" in names
    assert "肖申克的救赎" not in names


def test_english_term_regression(db):
    assert _search(db, "shawshank") == ["The Shawshank Redemption"]
    # 纯英文串不应被拼音逻辑污染
    assert "长津湖" not in _search(db, "shawshank")


def test_no_match_empty(db):
    assert _search(db, "zzz") == []

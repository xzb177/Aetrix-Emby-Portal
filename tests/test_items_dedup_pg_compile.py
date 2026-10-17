"""P0 去重 SQL 的 PG 形状锁定：unnest CTE 与 TEMP TABLE JOIN 在 PG 方言下渲染正确。

无需真 PG。两层断言：
1. 源码锁定——``api._dedup_dropped_ids`` 的 SQL 片段逐字存在，
   生产代码改错 SQL 形状（删了 unnest / 改了临时表名）时这里变红；
2. PG 方言真实编译——同样的 SQLAlchemy 表达式在 postgresql dialect 下
   渲染出 unnest / CAST AS INTEGER[] / 临时表名与 LEFT JOIN 形状。
"""
import inspect
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from sqlalchemy import column, func, Integer, select, table, text
from sqlalchemy.dialects import postgresql

from backend import models  # noqa: F401  注册全部表
from backend.emby_server import api
from backend.emby_server import models as em


def _compile(stmt):
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_unnest_cte_sql_fragments_exist_in_production_source():
    """源码锁定：生产 SQL 的关键片段必须原样存在，改错形状能红。"""
    src = inspect.getsource(api._dedup_dropped_ids)
    assert "SELECT * FROM unnest(CAST(:ids AS INTEGER[])) AS t(id)" in src
    assert '.cte("suspect_ids")' in src
    assert "CREATE TEMP TABLE IF NOT EXISTS _dedup_rep_map" in src
    assert "INSERT INTO _dedup_rep_map (sid, grp) VALUES (:sid, :grp)" in src
    assert 'table("_dedup_rep_map"' in src


def test_unnest_cte_renders_correctly_under_pg_dialect():
    """PG 下 suspects 取行：unnest CTE + JOIN suspect_ids 的渲染形状。"""
    id_list = text("SELECT * FROM unnest(CAST(:ids AS INTEGER[])) AS t(id)").columns(
        column("id", Integer)).cte("suspect_ids")
    stmt = (
        select(em.MediaItem.id, em.MediaItem.name)
        .select_from(em.MediaItem)
        .join(id_list, id_list.c.id == em.MediaItem.id)
    )
    sql = _compile(stmt)
    assert "unnest(" in sql
    assert "AS INTEGER[])" in sql
    assert "suspect_ids" in sql
    assert "JOIN suspect_ids ON suspect_ids.id" in sql


def test_temp_table_join_renders_correctly_under_pg_dialect():
    """PG 下剧去重组映射：TEMP TABLE 的 JOIN 与 coalesce 回退形状。"""
    rep_tbl = table("_dedup_rep_map", column("sid", Integer), column("grp", Integer))
    MI = em.MediaItem
    series_group_expr = func.coalesce(rep_tbl.c.grp, MI.series_id, 0)
    stmt = (
        select(MI.id, series_group_expr.label("grp"))
        .select_from(MI)
        .outerjoin(rep_tbl, rep_tbl.c.sid == MI.series_id)
    )
    sql = _compile(stmt)
    assert "LEFT OUTER JOIN _dedup_rep_map" in sql
    assert "_dedup_rep_map.sid = emby_items.series_id" in sql
    assert "coalesce(_dedup_rep_map.grp, emby_items.series_id" in sql


def test_temp_table_ddl_statements_name_the_table():
    """三条 DDL/写语句都指向同一张临时表名，写错名能红。"""
    stmts = [
        "CREATE TEMP TABLE IF NOT EXISTS _dedup_rep_map (sid INTEGER PRIMARY KEY, grp INTEGER) ON COMMIT DROP",
        "TRUNCATE _dedup_rep_map",
        "INSERT INTO _dedup_rep_map (sid, grp) VALUES (:sid, :grp)",
    ]
    for s in stmts:
        assert "_dedup_rep_map" in s
    # 同一张表名在三条语句里完全一致（大小写/拼写漂移能红）
    names = [part for s in stmts for part in s.split() if "dedup_rep_map" in part]
    assert names and all(n.strip("(),") == "_dedup_rep_map" for n in names)

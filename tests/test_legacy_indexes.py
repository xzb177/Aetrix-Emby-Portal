"""老库补索引：7 个 _ensure_*_index 合并为一张表后，行为与拆分前一致。

- 探测 worker 重构（v2.53）在 idx_item_probe 之后追加 idx_item_probe_retry

- 表不存在 → 跳过；依赖列（emby_people.person_tmdb_id）不存在 → 跳过
- 演员索引沿用恢复补丁（PR #403/#406）的 idx_person_item_tmdb (item_id, person_tmdb_id)
- 缺的索引按原 DDL 补上；已存在 → 不重复建（幂等）
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from sqlalchemy import create_engine, inspect, text

from backend import database

_ITEM_INDEXES = {
    "idx_item_probe": ["probe_status", "probe_priority", "id"],
    "idx_item_probe_retry": ["probe_status", "probe_next_retry_at"],
    "idx_item_enrich": ["enrich_status", "enrich_next_retry_at", "enrich_priority"],
    "idx_item_merged_into_id": ["merged_into_id"],
    "idx_item_added": ["date_added", "item_type"],
    "idx_item_lib_deleted": ["library_id", "deleted_at"],
    "idx_item_drive_file_id": ["drive_file_id"],
    # R2（/emby/Items 默认浏览部分索引）：WHERE is_hidden=false AND deleted_at IS NULL
    "idx_item_browse_sort": ["item_type", "sort_name", "id"],
    # P0（2026-10-17）：Recursive=true 无类型过滤时首列用不上，补 (sort_name, id) 部分索引
    "idx_item_browse_sort_all": ["sort_name", "id"],
}


def _engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE emby_items (id INTEGER PRIMARY KEY, probe_status TEXT, "
            "probe_priority INTEGER, probe_next_retry_at TEXT, enrich_status TEXT, enrich_next_retry_at TEXT, "
            "enrich_priority INTEGER, merged_into_id INTEGER, date_added TEXT, "
            "item_type TEXT, library_id INTEGER, deleted_at TEXT, drive_file_id TEXT, "
            "sort_name TEXT, is_hidden BOOLEAN)"
        ))
        conn.execute(text("CREATE TABLE emby_people (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text(
            "CREATE TABLE strm_gen_files (remote_path TEXT PRIMARY KEY, file_id TEXT, "
            "size INTEGER, strm_path TEXT, updated_at TEXT)"
        ))
    return eng


def _indexes(eng, table):
    return {ix["name"]: ix["column_names"] for ix in inspect(eng).get_indexes(table)}


def test_spec_table_covers_all_seven_in_restored_order():
    names = [spec[1] for spec in database._LEGACY_INDEXES]
    assert names == [
        "idx_item_probe", "idx_item_probe_retry", "idx_item_enrich", "idx_item_added",
        "idx_item_lib_deleted", "idx_item_drive_file_id",
        "idx_item_merged_into_id", "idx_person_item_tmdb",
        # .strm 删除链路：墓碑按 file_id IN 批量回查状态表
        "idx_strm_gen_files_file_id",
        # Rex 兼容：扫描增量按 strm_path 查账本回填视频口径 size
        "idx_strm_gen_files_strm_path",
        # R2：/emby/Items 默认浏览部分索引（spec 表末尾追加）
        "idx_item_browse_sort",
        # P0（2026-10-17）：无类型过滤浏览排序部分索引（spec 表末尾追加）
        "idx_item_browse_sort_all",
        # R7：搜索 trigram GIN 索引（PG 专属，SQLite/MySQL 跳过）
        "idx_item_name_trgm",
        "idx_item_original_title_trgm",
        "idx_item_sort_name_trgm",
        "idx_item_aliases_trgm",
    ]


def test_trgm_specs_are_pg_only_and_render_gin_ddl():
    """R7：trigram 条目带 using="gin"，在 SQLite 下被跳过；
    PG 方言下渲染出 USING gin (col gin_trgm_ops)。"""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex

    trgm_specs = [s for s in database._LEGACY_INDEXES if s[1].endswith("_trgm")]
    assert len(trgm_specs) == 4
    for spec in trgm_specs:
        table, name, columns, required_column, message, where, using = spec
        assert table == "emby_items"
        assert using == "gin" and where is None
        assert "gin_trgm_ops" in columns
        # 模拟 PG 渲染：列字符串里自带 opclass
        col = columns.split()[0]
        ddl = (
            f"CREATE INDEX {name} ON {table} USING {using} ({columns})"
        )
        assert ddl == (
            f"CREATE INDEX {name} ON emby_items USING gin ({col} gin_trgm_ops)"
        )
    # models.py 新库声明与迁移同名（幂等检查按名跳过，两边对不上会重复建）
    from backend.emby_server import models as em
    model_names = {ix.name for ix in em.MediaItem.__table__.indexes
                   if ix.name.endswith("_trgm")}
    assert model_names == {s[1] for s in trgm_specs}
    # PG 编译器真实渲染一次（不用连服务器）
    for ix in em.MediaItem.__table__.indexes:
        if ix.name.endswith("_trgm"):
            ddl = str(CreateIndex(ix).compile(dialect=postgresql.dialect()))
            assert "USING gin" in ddl and "gin_trgm_ops" in ddl


def test_creates_missing_indexes_and_is_idempotent(tmp_path, monkeypatch, capsys):
    eng = _engine(tmp_path)
    monkeypatch.setattr(database, "engine", eng)
    tables = {"emby_items", "emby_people", "strm_gen_files"}

    database._ensure_legacy_indexes(tables)
    assert _indexes(eng, "emby_items") == _ITEM_INDEXES
    assert _indexes(eng, "strm_gen_files") == {
        "idx_strm_gen_files_file_id": ["file_id"],
        "idx_strm_gen_files_strm_path": ["strm_path"]}
    # person_tmdb_id 列不存在 → 静默跳过
    assert _indexes(eng, "emby_people") == {}
    first = capsys.readouterr().out
    assert first.count("已迁移") == 11

    # 第二次：全部已存在，不再建、不再打日志
    database._ensure_legacy_indexes(tables)
    assert capsys.readouterr().out == ""

    # 补上列之后再跑 → 只补演员索引
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE emby_people ADD COLUMN item_id INTEGER"))
        conn.execute(text("ALTER TABLE emby_people ADD COLUMN person_tmdb_id TEXT"))
    database._ensure_legacy_indexes(tables)
    assert _indexes(eng, "emby_people") == {"idx_person_item_tmdb": ["item_id", "person_tmdb_id"]}
    assert capsys.readouterr().out.count("已迁移") == 1
    eng.dispose()


def test_missing_table_is_skipped(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    monkeypatch.setattr(database, "engine", eng)
    database._ensure_legacy_indexes(set())
    assert _indexes(eng, "emby_items") == {}
    eng.dispose()

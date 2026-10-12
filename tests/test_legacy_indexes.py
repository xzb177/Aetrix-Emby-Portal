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
}


def _engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE emby_items (id INTEGER PRIMARY KEY, probe_status TEXT, "
            "probe_priority INTEGER, probe_next_retry_at TEXT, enrich_status TEXT, enrich_next_retry_at TEXT, "
            "enrich_priority INTEGER, merged_into_id INTEGER, date_added TEXT, "
            "item_type TEXT, library_id INTEGER, deleted_at TEXT, drive_file_id TEXT)"
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
    ]


def test_creates_missing_indexes_and_is_idempotent(tmp_path, monkeypatch, capsys):
    eng = _engine(tmp_path)
    monkeypatch.setattr(database, "engine", eng)
    tables = {"emby_items", "emby_people", "strm_gen_files"}

    database._ensure_legacy_indexes(tables)
    assert _indexes(eng, "emby_items") == _ITEM_INDEXES
    assert _indexes(eng, "strm_gen_files") == {
        "idx_strm_gen_files_file_id": ["file_id"]}
    # person_tmdb_id 列不存在 → 静默跳过
    assert _indexes(eng, "emby_people") == {}
    first = capsys.readouterr().out
    assert first.count("已迁移") == 8

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

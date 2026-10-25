"""#13 PR-1：scan 身份列与回填（只写、不改任何判定）。

覆盖：
- MediaItem 五列存在且类型与定稿一致；
- _auto_migrate 的 emby_items 补列条目包含五列（对空表跑迁移验证）；
- 回填：电影行、剧集行（剧名口径）、结构行不碰、幂等第二遍零写入；
- 守卫：刮削链（metasources/engine.py）不写 scan 列——只有扫描器写。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.emby_server import models as em  # noqa: E402


def test_model_columns_exist():
    cols = em.MediaItem.__table__.columns
    assert cols["scan_title"].type.length == 300
    assert cols["scan_year"].type.__class__.__name__ == "Integer"
    assert cols["content_hash"].type.length == 40
    assert cols["content_hash_algo"].type.length == 16
    assert cols["identity_json"].type.__class__.__name__ == "Text"
    assert cols["scan_title"].nullable is True
    assert cols["scan_year"].nullable is True


def test_auto_migrate_adds_columns_to_old_table(tmp_path):
    """对一张缺五列的旧 emby_items 表跑 _auto_migrate，列须补齐。

    全量建表后再 DROP 五列（模拟升级上来的老库），避免 _auto_migrate
    后段全模型遍历时因缺表报 NoReferencedTableError。
    """
    import backend.database as dbmod
    import backend.models  # noqa: F401 — 注册 WebUser 等表进同一 Base.metadata

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    dbmod.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # content_hash 带 index=True，建表时自动建了索引，DROP 列前先删索引
        conn.exec_driver_sql("DROP INDEX IF EXISTS ix_emby_items_content_hash")
        for col in ("scan_title", "scan_year", "content_hash",
                    "content_hash_algo", "identity_json"):
            conn.exec_driver_sql(f"ALTER TABLE emby_items DROP COLUMN {col}")
    assert "scan_title" not in {
        c["name"] for c in inspect(engine).get_columns("emby_items")}
    old_engine = dbmod.engine
    dbmod.engine = engine
    try:
        dbmod._auto_migrate()
    finally:
        dbmod.engine = old_engine
    names = {c["name"] for c in inspect(engine).get_columns("emby_items")}
    for col in ("scan_title", "scan_year", "content_hash", "content_hash_algo",
                "identity_json"):
        assert col in names


def _make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    em.Base.metadata.create_all(engine, tables=[
        em.Library.__table__, em.MediaItem.__table__])
    return sessionmaker(bind=engine)()


def test_backfill_movie_episode_and_idempotent(tmp_path):
    from scripts.backfill_scan_identity import backfill_scan_identity

    s = _make_session(tmp_path)
    lib_m = em.Library(guid="lib-m", name="m", collection_type="movies")
    lib_t = em.Library(guid="lib-t", name="t", collection_type="tvshows")
    s.add_all([lib_m, lib_t])
    s.flush()
    movie = em.MediaItem(guid="g1", library_id=lib_m.id, item_type="movie",
                         name="x", file_path="/movies/肖申克的救赎 (1994).mkv")
    ep = em.MediaItem(guid="g2", library_id=lib_t.id, item_type="episode",
                      name="x",
                      file_path="/tv/泪之女王 (2024)/Season 1/泪之女王 S01E01.mkv")
    series = em.MediaItem(guid="g3", library_id=lib_t.id, item_type="series",
                          name="泪之女王", file_path=None)
    nolib = em.MediaItem(guid="g4", library_id=99999, item_type="movie",
                         name="x", file_path="/movies/阿甘正传 (1994).mkv")
    s.add_all([movie, ep, series, nolib])
    s.commit()

    stats = backfill_scan_identity(s)
    assert stats["filled"] == 2
    assert stats["skipped_no_library"] == 1
    s.refresh(movie)
    s.refresh(ep)
    s.refresh(series)
    assert movie.scan_title == "肖申克的救赎"
    assert movie.scan_year == 1994
    assert ep.scan_title == "泪之女王"
    assert ep.scan_year == 2024
    assert series.scan_title is None  # 结构行不碰
    stats2 = backfill_scan_identity(s)
    assert stats2["filled"] == 0  # 第二遍零写入


def test_backfill_mount_path_episode(tmp_path):
    """mount:// 路径的剧集行：dir_rel 推导剧名，离线可算。"""
    from scripts.backfill_scan_identity import backfill_scan_identity

    s = _make_session(tmp_path)
    lib_t = em.Library(guid="lib-t", name="t", collection_type="tvshows")
    s.add(lib_t)
    s.flush()
    ep = em.MediaItem(guid="g9", library_id=lib_t.id, item_type="episode",
                      name="x",
                      file_path="mount://3/剧集/三体 (2023)/Season 1/三体 S01E01.mkv")
    s.add(ep)
    s.commit()
    stats = backfill_scan_identity(s)
    assert stats["filled"] == 1
    s.refresh(ep)
    assert ep.scan_title == "三体"
    assert ep.scan_year == 2023


def test_enrich_chain_does_not_write_scan_columns():
    """守卫：只有扫描器写 scan 列——刮削链源码里不许出现赋值。"""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "backend/emby_server/metasources/engine.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert "scan_title" not in src
    assert "scan_year" not in src

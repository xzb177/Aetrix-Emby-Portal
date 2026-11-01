"""上游托管行（无 file_path + upstream_source_id）补全回归。

生产事故（2026-11-01）：6074 条上游托管电影入库后 enrich 0 条 done，
已尝试的全部失败，enrich_last_error 统一为
``无 file_path，无法重建 ScanFile``。

根因：``_enrich_fetch_pre`` 里本有托管分支（episode/movie 且
``upstream_source_id`` 非空 → 只走 TMDB、不判死），但流水线在进 IO 阶段前
把 ORM 条目转成 ``_EnrichItemSnapshot``（``_process_item`` /
``_process_batch`` 都这么走），而快照的字段清单漏了 ``upstream_source_id``
——IO 阶段 ``getattr(item, "upstream_source_id", None)`` 恒为 None，
托管分支成死代码，托管行全落判死分支。

本文件刻意走生产同款路径：先 ``_snapshot_item(orm_like)`` 再喂
``_enrich_fetch`` / ``_enrich_fetch_pre``。不像
``test_enrich_container_items`` 那样把 namespace 直喂 ``_enrich_fetch``
——直喂绕过快照投影，测不出本 bug。

与该文件同口径：不碰真实数据库（TMDB / side / NFO 全 mock），
条目用 SimpleNamespace 构造齐 ``_snapshot_item`` 读的全部字段。
"""
from types import SimpleNamespace
from unittest import mock

from backend.emby_server import enrich_worker


def _ew():
    return enrich_worker


def _orm_item(item_type, name, year=2020, file_path=None, tmdb_id=None,
              upstream_source_id=None, series_id=None):
    """模拟 _claim_batch 交出来的 ORM 条目（字段集与 MediaItem 对齐）。"""
    return SimpleNamespace(
        id=1, library_id=1, enrich_attempts=0,
        item_type=item_type, name=name, file_path=file_path,
        size=0, container="", production_year=year,
        poster_path=None, primary_image_url=None,
        imdb_id=None, aliases=None, tmdb_id=tmdb_id,
        repair_requested_at=None, overview=None,
        metadata_locked=False,
        series_id=series_id, parent_id=None,
        season_number=None, episode_number=None,
        enrich_claim_token=None,
        upstream_source_id=upstream_source_id,
    )


def test_snapshot_carries_upstream_source_id():
    """快照投影必须带 upstream_source_id（本 bug 的直接回归点）。"""
    snap = _ew()._snapshot_item(_orm_item(
        "movie", "托管片", tmdb_id="603", upstream_source_id=7))
    assert snap.upstream_source_id == 7
    snap_plain = _ew()._snapshot_item(_orm_item("movie", "普通片"))
    assert snap_plain.upstream_source_id is None


def test_hosted_movie_without_file_path_enriches_via_tmdb():
    """托管电影：经快照后不判死，走 TMDB 并拿到命中。"""
    orm_item = _orm_item("movie", "托管电影", 2021, file_path=None,
                         tmdb_id="603", upstream_source_id=7)
    snapshot = _ew()._snapshot_item(orm_item)

    # pre 层：不判死 + 规划出 TMDB 动作
    with mock.patch.object(_ew(), "_scanfile_from_item", return_value=None), \
         mock.patch.object(_ew(), "_alias_tmdb_id", return_value=None), \
         mock.patch("backend.emby_server.tmdb.tmdb_client") as m_client:
        m_client.configured = True
        result, tmdb_ctx = _ew()._enrich_fetch_pre(snapshot)
    assert result["ok"] is True, f"托管电影不该被判失败：{result}"
    assert result["error"] is None
    assert tmdb_ctx is not None
    assert tmdb_ctx["tmdb_plan"]["op"] in ("search", "details")

    # 全链（pre → TMDB post）：mock TMDB 命中后 ok 且带 hit
    snapshot2 = _ew()._snapshot_item(orm_item)
    with mock.patch.object(_ew(), "_scanfile_from_item", return_value=None), \
         mock.patch.object(_ew(), "_alias_tmdb_id", return_value=None), \
         mock.patch("backend.emby_server.scanner._tmdb_work",
                    return_value=({"id": 603}, {"id": 603})) as m_tmdb, \
         mock.patch("backend.emby_server.tmdb.tmdb_client") as m_client, \
         mock.patch("backend.emby_server.douban.enabled", return_value=False):
        m_client.configured = True
        res = _ew()._enrich_fetch(snapshot2)
    assert res["ok"] is True, f"托管电影不该被判失败：{res}"
    assert res["error"] is None
    assert m_tmdb.called, "托管电影必须走 TMDB"
    assert res["tmdb_hit"] == {"id": 603}


def test_hosted_episode_without_file_path_is_not_a_failure():
    """托管单集：经快照后不判死（单集 TMDB 走父级口径，这里只验不判死）。"""
    orm_item = _orm_item("episode", "第 1 集", 2021, file_path=None,
                         tmdb_id="1399", upstream_source_id=7, series_id=99)
    snapshot = _ew()._snapshot_item(orm_item)
    with mock.patch.object(_ew(), "_scanfile_from_item", return_value=None), \
         mock.patch("backend.emby_server.tmdb.tmdb_client") as m_client:
        m_client.configured = True
        res = _ew()._enrich_fetch(snapshot)
    assert res["ok"] is True, f"托管单集不该被判失败：{res}"
    assert res["error"] is None


def test_plain_movie_without_file_path_still_fails():
    """行为不变：普通电影（无 upstream_source_id）无 file_path 仍判死。"""
    snapshot = _ew()._snapshot_item(_orm_item(
        "movie", "阿凡达", 2009, file_path=None, upstream_source_id=None))
    with mock.patch.object(_ew(), "_scanfile_from_item", return_value=None):
        res = _ew()._enrich_fetch(snapshot)
    assert res["ok"] is False
    assert "file_path" in (res["error"] or "")


def test_plain_episode_without_file_path_still_fails():
    """行为不变：普通单集无 file_path 仍判死。"""
    snapshot = _ew()._snapshot_item(_orm_item(
        "episode", "第 1 集", 2020, file_path=None, upstream_source_id=None))
    with mock.patch.object(_ew(), "_scanfile_from_item", return_value=None):
        res = _ew()._enrich_fetch(snapshot)
    assert res["ok"] is False
    assert "file_path" in (res["error"] or "")


def test_series_container_via_snapshot_unchanged():
    """行为不变：容器条目经快照后仍只走 TMDB、不判死。"""
    snapshot = _ew()._snapshot_item(_orm_item(
        "series", "1000磅姐妹", 2020, file_path=None))
    with mock.patch.object(_ew(), "_scanfile_from_item", return_value=None), \
         mock.patch.object(_ew(), "_alias_tmdb_id", return_value=None), \
         mock.patch("backend.emby_server.scanner._tmdb_work",
                    return_value=({"id": 98410}, {"id": 98410})) as m_tmdb, \
         mock.patch("backend.emby_server.tmdb.tmdb_client") as m_client, \
         mock.patch("backend.emby_server.douban.enabled", return_value=False):
        m_client.configured = True
        res = _ew()._enrich_fetch(snapshot)
    assert res["ok"] is True
    assert m_tmdb.called
    assert res["tmdb_hit"] == {"id": 98410}

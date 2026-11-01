"""上游独有资源进库展示 · 一期（电影先行）回归。

锁住的口径（方案 reports/upstream-only-import-proposal-20261101.md，
用户 2026-11-01 09:10 按推荐方案拍板）：

- 聚合后未配对、Type=Movie、带 tmdb_id 的独有条目按上游库名落本地
  同名库（非虚拟、同名取 id 最小）建「上游托管」行（MediaItem.
  upstream_source_id=源 id、无 file_path、probe 跳过）并写映射；
  无同名库跳过计数；按 guid/映射幂等；进库函数带 limit（内部口）；
- 按源开关 import_enabled（upstream_sources 字段，默认开）：关时
  不进库，且已进库托管行只隐藏不删，重开恢复；
- 播放三环：无 file_path 的托管电影详情 DTO 有上游线路、
  PlaybackInfo 只回上游线路一条源（不进转存流水线）、取流口过
  绑定门禁（未绑定 403、已绑定 302）；合成源媒体参数现查上游；
- 扫描豁免：混合库清理跳过托管行，删除预算分母不计托管行；
- 合并跟随：选主「有本地文件优先」，被合并行的映射迁到存活行，
  冲突保留置信高者；
- enrich 对无 file_path 托管行不判失败（仅走 TMDB）。

DB 纯逻辑部分用内存 SQLite（照 test_upstream_aggregate_run 形态，
不重绑全局 engine）；播放面用真 TestClient（照
test_upstream_detail_line 形态），上游探活/现查全部打桩。
"""

from __future__ import annotations

import hashlib
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
import uuid
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.models as models
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_sources as ups


# ---------------------------------------------------------------------------
# 内存 DB 夹具（进库 / 扫描 / 合并）
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _source(db, *, name="src-import", enabled=True, import_enabled=True):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        enabled=enabled, credential_scope="source",
        health_status="unknown", import_enabled=import_enabled)
    db.add(row)
    db.flush()
    return row


def _library(db, *, name, is_virtual=False):
    row = em.Library(guid=uuid.uuid4().hex, name=name,
                     collection_type="movies", is_enabled=True,
                     is_virtual=is_virtual)
    db.add(row)
    db.flush()
    return row


def _agg_movie(source_id, upstream_id, name, tmdb, year=2021,
               item_type="Movie"):
    line = agg.UpstreamLine(
        line_index=0, line_key=f"{source_id}:{upstream_id}",
        source_id=str(source_id), upstream_item_id=upstream_id)
    return agg.UpstreamAggregateItem(
        aggregate_key=(f"tmdb:{tmdb}" if tmdb else f"title:{name}:{year}"),
        name=name, item_type=item_type, production_year=year,
        tmdb_id=tmdb, lines=[line])


def _hosted_guid(source_id, upstream_item_id):
    return hashlib.md5(
        f"upstream:{source_id}:{upstream_item_id}".encode()).hexdigest()


@pytest.fixture(autouse=True)
def _mapping_on(monkeypatch):
    monkeypatch.setenv("UPSTREAM_ITEM_MAPPING_ENABLED", "1")


# ---------------------------------------------------------------------------
# 进库执行
# ---------------------------------------------------------------------------

def test_import_lands_in_same_name_library(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    lib = _library(db, name="华语电影")
    _library(db, name="华语电影", is_virtual=True)  # 虚拟同名库不许落
    items = [_agg_movie(src.id, "up-1", "独有片甲", "900001")]
    stats = uimp.import_unpaired_movies(
        db, src, items, set(), upstream_library_name="华语电影")
    db.commit()
    assert stats["candidates"] == 1
    assert stats["imported"] == 1
    row = db.query(em.MediaItem).filter(
        em.MediaItem.guid == _hosted_guid(src.id, "up-1")).one()
    assert row.library_id == lib.id
    assert row.item_type == "movie"
    assert row.tmdb_id == "900001"
    assert row.file_path is None
    assert row.upstream_source_id == src.id
    assert row.probe_status == "done"  # probe 跳过：无本地文件可探
    assert row.sort_name  # 走 make_sort_name，不为空
    mapping = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.local_item_id == row.id).one()
    assert mapping.source_id == src.id
    assert mapping.upstream_item_id == "up-1"


def test_import_skips_and_counts_without_same_name_library(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="别的库")
    items = [_agg_movie(src.id, "up-2", "无落点片", "900002")]
    stats = uimp.import_unpaired_movies(
        db, src, items, set(), upstream_library_name="不存在的库")
    db.commit()
    assert stats["candidates"] == 1
    assert stats["imported"] == 0
    assert stats["no_library"] == 1
    assert db.query(em.MediaItem).count() == 0


def test_import_idempotent_second_run(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="华语电影")
    items = [_agg_movie(src.id, "up-3", "幂等片", "900003")]
    first = uimp.import_unpaired_movies(
        db, src, items, set(), upstream_library_name="华语电影")
    db.commit()
    second = uimp.import_unpaired_movies(
        db, src, items, set(), upstream_library_name="华语电影")
    db.commit()
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["existing"] == 1
    assert db.query(em.MediaItem).count() == 1
    assert db.query(em.UpstreamItemMapping).count() == 1


def test_import_limit_only_builds_limit_rows(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="华语电影")
    items = [_agg_movie(src.id, f"up-l{i}", f"限额片{i}", f"9100{i:02d}")
             for i in range(12)]
    stats = uimp.import_unpaired_movies(
        db, src, items, set(), upstream_library_name="华语电影", limit=10)
    db.commit()
    assert stats["imported"] == 10
    assert stats["limited"] == 2
    assert db.query(em.MediaItem).count() == 10


def test_import_ignores_paired_series_and_no_tmdb(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="华语电影")
    items = [
        _agg_movie(src.id, "up-paired", "已配对片", "900010"),
        _agg_movie(src.id, "up-series", "独有剧", "900011",
                   item_type="Series"),
        _agg_movie(src.id, "up-notmdb", "无身份片", None),
    ]
    stats = uimp.import_unpaired_movies(
        db, src, items, {"up-paired"}, upstream_library_name="华语电影")
    db.commit()
    assert stats["candidates"] == 0
    assert stats["imported"] == 0
    assert db.query(em.MediaItem).count() == 0


def test_import_switch_off_blocks_and_hides_existing(db):
    from backend.emby_server import upstream_import as uimp
    src = _source(db)
    _library(db, name="华语电影")
    items = [_agg_movie(src.id, "up-5", "开关片", "900005")]
    stats = uimp.import_unpaired_movies(
        db, src, items, set(), upstream_library_name="华语电影")
    db.commit()
    assert stats["imported"] == 1
    row = db.query(em.MediaItem).one()
    assert row.is_hidden in (False, None)

    # 关开关：不再进库；已进库行只隐藏不删
    ups.update_source(db, src, import_enabled=False)
    db.commit()
    stats2 = uimp.import_unpaired_movies(
        db, src, [_agg_movie(src.id, "up-6", "开关片二", "900006")],
        set(), upstream_library_name="华语电影")
    db.commit()
    assert stats2["blocked"] == "switch_off"
    assert stats2["imported"] == 0
    db.expire_all()
    row = db.query(em.MediaItem).one()  # 仍只有一行，没被删
    assert row.is_hidden is True

    # 重开：恢复可见
    ups.update_source(db, src, import_enabled=True)
    db.commit()
    db.expire_all()
    row = db.query(em.MediaItem).one()
    assert row.is_hidden is False
    payload = ups.source_payload(src)
    assert payload["import_enabled"] is True


def test_run_hook_imports_unpaired_movies_with_limit(db, monkeypatch):
    """聚合主流程接线：未配对独有电影经 run 一并进库，import_limit 生效。"""
    from backend.emby_server import upstream_aggregate_run as uar

    src = _source(db, name="src-run")
    target = _library(db, name="库")
    other = _library(db, name="本地库")
    # 已配对片：本地已有同 tmdb 行（全量模式全局配对）
    db.add(em.MediaItem(
        guid="guid-local-paired", library_id=other.id, item_type="movie",
        name="已配对片", tmdb_id="920001", file_path="mount://3/x.mp4"))
    db.flush()

    def _raw(item_id, name, tmdb):
        return {"Id": item_id, "Name": name, "Type": "Movie",
                "ProductionYear": 2020,
                "ProviderIds": {"Tmdb": tmdb},
                "MediaSources": [{"Id": f"ms-{item_id}", "Name": "1080p"}]}

    class _Fake:
        def get(self, url, headers, timeout):
            if url.split("?")[0].endswith("/Library/MediaFolders"):
                return 200, {"Items": [{"Id": "lib-1", "Name": "库"}]}
            return 200, {"Items": [
                _raw("r-paired", "已配对片", "920001"),
                _raw("r-only-1", "独有片一", "920002"),
                _raw("r-only-2", "独有片二", "920003"),
            ], "TotalRecordCount": 3}

    result = uar.run_manual_aggregation(
        db, src, http_get=_Fake().get, import_limit=1)
    db.commit()
    assert result["imported"] == 1
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src.id).all()
    assert len(rows) == 1
    assert rows[0].library_id == target.id

    # 不限量再跑一轮：剩下一部进库，已进的不重复
    result2 = uar.run_manual_aggregation(db, src, http_get=_Fake().get)
    db.commit()
    assert result2["imported"] == 1
    assert db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src.id).count() == 2


# ---------------------------------------------------------------------------
# 扫描豁免
# ---------------------------------------------------------------------------

def _mixed_library(db):
    lib = _library(db, name="混合库")
    seen_item = em.MediaItem(
        guid="guid-seen", library_id=lib.id, item_type="movie",
        name="在席片", file_path="mount://3/在席片/movie.mp4")
    missing_item = em.MediaItem(
        guid="guid-missing", library_id=lib.id, item_type="movie",
        name="缺席片", file_path="mount://3/缺席片/movie.mp4")
    hosted = em.MediaItem(
        guid="guid-hosted", library_id=lib.id, item_type="movie",
        name="托管片", file_path=None, upstream_source_id=7,
        probe_status="done")
    db.add_all([seen_item, missing_item, hosted])
    db.flush()
    return lib, seen_item, missing_item, hosted


def test_removal_budget_ignores_hosted_rows(db):
    from backend.emby_server import scanner
    lib, seen_item, _missing, _hosted = _mixed_library(db)
    for i in range(25):
        db.add(em.MediaItem(
            guid=f"guid-hosted-{i}", library_id=lib.id, item_type="movie",
            name=f"托管片{i}", file_path=None, upstream_source_id=7,
            probe_status="done"))
    db.flush()
    allowed, why, total = scanner._removal_budget(db, lib, {"guid-seen"})
    # 分母只算有本地文件的行：1 条；若把 26 条托管行算进去，
    # total-seen=26 会越过删除预算阈值把整轮清理拦停。
    assert total == 2
    assert allowed, why


def test_cleanup_skips_hosted_rows(db):
    from backend.emby_server import scanner
    lib, _seen, missing, hosted = _mixed_library(db)
    scanner._remove_missing_items(db, lib, {"guid-seen"})
    db.commit()
    db.expire_all()
    hosted_row = db.query(em.MediaItem).filter(
        em.MediaItem.guid == "guid-hosted").one()
    assert hosted_row.deleted_at is None
    assert (hosted_row.missing_count or 0) == 0
    missing_row = db.query(em.MediaItem).filter(
        em.MediaItem.guid == "guid-missing").one()
    assert (missing_row.missing_count or 0) == 1  # 本地行照旧走宽限


# ---------------------------------------------------------------------------
# 合并跟随
# ---------------------------------------------------------------------------

def test_pick_primary_prefers_row_with_local_file():
    from backend.emby_server import dedup
    hosted = SimpleNamespace(
        id=1, file_path=None, poster_path="/p.jpg", tmdb_id="930001",
        overview="有简介")
    local = SimpleNamespace(
        id=2, file_path="mount://3/x.mp4", poster_path=None,
        tmdb_id="930001", overview=None)
    # 旧口径（海报 > tmdb > 简介）会选托管行；新口径有本地文件优先
    assert dedup.pick_primary([hosted, local]) is local


def test_merge_group_migrates_mappings_keeps_higher_confidence(db):
    from backend.emby_server import merge_versions_worker as mvw
    src = _source(db)
    lib = _library(db, name="合并库")
    local = em.MediaItem(
        guid="guid-merge-local", library_id=lib.id, item_type="movie",
        name="合并片", tmdb_id="930002", file_path="mount://3/m.mp4")
    hosted = em.MediaItem(
        guid="guid-merge-hosted", library_id=lib.id, item_type="movie",
        name="合并片", tmdb_id="930002", file_path=None,
        upstream_source_id=src.id, probe_status="done",
        primary_image_url="https://img.example/p.jpg")
    db.add_all([local, hosted])
    db.flush()
    # 同一上游条目两边都有映射：托管侧置信更高 → 迁移后保留高者
    db.add(em.UpstreamItemMapping(
        local_item_id=local.id, source_id=src.id,
        upstream_item_id="up-same", confidence=0.5, evidence="title+year"))
    db.add(em.UpstreamItemMapping(
        local_item_id=hosted.id, source_id=src.id,
        upstream_item_id="up-same", confidence=0.9, evidence="tmdb"))
    # 托管侧独有映射：整体改挂存活行
    db.add(em.UpstreamItemMapping(
        local_item_id=hosted.id, source_id=src.id,
        upstream_item_id="up-only", confidence=0.8, evidence="tmdb"))
    db.flush()

    merged = mvw.merge_group(db, [local, hosted])
    db.commit()
    assert merged == 1
    db.expire_all()
    hosted_row = db.query(em.MediaItem).filter(
        em.MediaItem.guid == "guid-merge-hosted").one()
    assert hosted_row.merged_into_id == local.id
    rows = db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.local_item_id == local.id).all()
    by_up = {r.upstream_item_id: r for r in rows}
    assert set(by_up) == {"up-same", "up-only"}
    assert by_up["up-same"].confidence == pytest.approx(0.9)
    assert by_up["up-same"].evidence == "tmdb"
    assert db.query(em.UpstreamItemMapping).filter(
        em.UpstreamItemMapping.local_item_id == hosted.id).count() == 0


# ---------------------------------------------------------------------------
# enrich 兜底
# ---------------------------------------------------------------------------

def test_enrich_hosted_movie_without_file_path_not_a_failure():
    from backend.emby_server import enrich_worker
    item = SimpleNamespace(
        id=1, item_type="movie", name="托管电影", production_year=2021,
        file_path=None, size=0, container="", library_id=1,
        tmdb_id="940001", poster_path=None, primary_image_url=None,
        imdb_id=None, aliases=None, repair_requested_at=None,
        upstream_source_id=7)
    with mock.patch.object(
            enrich_worker, "_scanfile_from_item", return_value=None), \
            mock.patch("backend.emby_server.scanner._tmdb_work",
                       return_value=({"id": 940001}, {"id": 940001})) as m, \
            mock.patch("backend.emby_server.tmdb.tmdb_client") as m_client, \
            mock.patch("backend.emby_server.douban.enabled",
                       return_value=False):
        m_client.configured = True
        res = enrich_worker._enrich_fetch(item)
    assert res["ok"] is True, f"托管行不该被判失败：{res}"
    assert res["error"] is None
    assert m.called, "托管行应走 TMDB 补全"


# ---------------------------------------------------------------------------
# 播放三环（真 TestClient）
# ---------------------------------------------------------------------------

from backend.database import SessionLocal, init_db  # noqa: E402
from backend.security import hash_password  # noqa: E402

init_db()

PW = "ImportP1#2026"
BOUND = "importp1_bound"
FREE = "importp1_free"
LIB2_NAME = "进库播放测试库"
SRC2_NAME = "进库测试源"
HOSTED_NAME = "托管独有片"
UP_ID = "up-hosted-1"


def _cleanup_client_seed(db):
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name == HOSTED_NAME).all()
    ids = [r.id for r in rows]
    if ids:
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    db.query(em.UpstreamSource).filter(
        em.UpstreamSource.name == SRC2_NAME).delete(
            synchronize_session=False)
    db.query(em.Library).filter(em.Library.name == LIB2_NAME).delete(
        synchronize_session=False)
    for uname in (BOUND, FREE):
        u = db.query(models.WebUser).filter(
            models.WebUser.username == uname).first()
        if u is not None:
            db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == u.id).delete(
                    synchronize_session=False)
            db.query(em.MountCredential).filter(
                em.MountCredential.owner_user_id == u.id).delete(
                    synchronize_session=False)
            db.delete(u)
    db.commit()


@pytest.fixture(scope="module")
def pseed():
    db = SessionLocal()
    _cleanup_client_seed(db)
    users = {}
    for uname in (BOUND, FREE):
        u = models.WebUser(
            username=uname, password_hash=hash_password(PW), is_active=True,
            is_staff=True, emby_username=uname,
            emby_password=hash_password(PW))
        db.add(u)
        db.flush()
        users[uname] = u.id
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=users[BOUND],
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    db.add(em.UserStorageBinding(
        user_id=users[BOUND], provider="drive", account_label="种子盘",
        credential_id=cred.id, status="active"))
    src = ups.create_source(
        db, name=SRC2_NAME, source_type="emby",
        base_url="https://up2.example.com", auth_method="api_key")
    ups.enable_source(db, src)
    lib = em.Library(guid=uuid.uuid4().hex, name=LIB2_NAME,
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    hosted = em.MediaItem(
        guid=_hosted_guid(src.id, UP_ID), library_id=lib.id,
        item_type="movie", name=HOSTED_NAME, production_year=2021,
        tmdb_id="950001", file_path=None, upstream_source_id=src.id,
        probe_status="done")
    db.add(hosted)
    db.flush()
    db.add(em.UpstreamItemMapping(
        local_item_id=hosted.id, source_id=src.id, upstream_item_id=UP_ID,
        confidence=1.0, evidence="tmdb"))
    db.commit()
    data = {"guid": hosted.guid, "src": src.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup_client_seed(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def pclient():
    from backend.main import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


def _auth_header():
    q = chr(34)
    return (f"MediaBrowser Client={q}ImportP1{q}, Device={q}iOS{q}, "
            f"DeviceId={q}importp1-dev{q}, Version={q}1.0{q}")


def _token(client, uname):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": _auth_header()})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def bound_token(pclient, pseed):
    return _token(pclient, BOUND)


@pytest.fixture(scope="module")
def free_token(pclient, pseed):
    return _token(pclient, FREE)


def _upstream_line(body):
    return [s for s in body.get("MediaSources") or []
            if str(s.get("Id", "")).endswith(":upstream")]


def test_detail_hosted_movie_has_upstream_line(pclient, bound_token, pseed):
    r = pclient.get(f"/emby/Items/{pseed['guid']}",
                    headers={"X-Emby-Token": bound_token})
    assert r.status_code == 200, r.text
    body = r.json()
    lines = _upstream_line(body)
    assert len(lines) == 1, body.get("MediaSources")
    assert lines[0]["Name"] == f"{HOSTED_NAME} · 上游 · {SRC2_NAME}"
    # 本地合成源不带取流地址（没有本地文件可取），只有上游线路能播
    local = [s for s in body["MediaSources"]
             if not str(s.get("Id", "")).endswith(":upstream")]
    assert local and not local[0].get("DirectPlayUrl")


def test_playbackinfo_hosted_movie_only_upstream_source(
        pclient, bound_token, pseed, monkeypatch):
    # 合成源媒体参数现查上游：打桩返回参数，应落进线路源
    from backend.emby_server import upstream_playback as up
    monkeypatch.setattr(
        up, "fetch_upstream_media_params",
        lambda source, upstream_item_id, **kw: {
            "size": 3_500_000_000, "run_time_ticks": 5_400_000_000,
            "container": "mkv", "bitrate": 5_200_000,
            "media_streams": [
                {"Index": 0, "Type": "Video", "Codec": "hevc",
                 "Width": 3840, "Height": 2160, "IsDefault": True}]})
    r = pclient.post(f"/emby/Items/{pseed['guid']}/PlaybackInfo",
                     headers={"X-Emby-Token": bound_token}, json={})
    assert r.status_code == 200, r.text
    sources = r.json().get("MediaSources") or []
    assert len(sources) == 1, sources  # 只有上游线路一条源
    line = sources[0]
    assert line["Id"] == f"{pseed['guid']}:upstream"
    assert line["Size"] == 3_500_000_000
    assert line["RunTimeTicks"] == 5_400_000_000
    assert line["Container"] == "mkv"
    assert line["MediaStreams"][0]["Codec"] == "hevc"


def test_playbackinfo_hosted_unbound_user_blocked(
        pclient, free_token, pseed):
    r = pclient.post(f"/emby/Items/{pseed['guid']}/PlaybackInfo",
                     headers={"X-Emby-Token": free_token}, json={})
    assert r.status_code == 403, r.text  # 绑定门禁在入口拦下
    assert not _upstream_line(r.json() if r.headers.get(
        "content-type", "").startswith("application/json") else {})


def test_stream_hosted_bound_redirects_free_blocked(
        pclient, bound_token, free_token, pseed, monkeypatch):
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    from backend.emby_server import upstream_playback as up
    monkeypatch.setattr(
        up, "_default_http_range_probe",
        lambda url, headers=None, timeout=None: (206, None))
    monkeypatch.setattr(
        up, "fetch_upstream_media_params",
        lambda source, upstream_item_id, **kw: None)
    detail = pclient.get(f"/emby/Items/{pseed['guid']}",
                         headers={"X-Emby-Token": bound_token}).json()
    url = _upstream_line(detail)[0]["DirectStreamUrl"]
    path = re.sub(r"^https?://[^/]+", "", url)

    r = pclient.get(path, headers={"X-Emby-Token": bound_token},
                    follow_redirects=False)
    assert r.status_code == 302, r.text
    assert "up2.example.com" in r.headers.get("location", "")

    detail_free = pclient.get(
        f"/emby/Items/{pseed['guid']}",
        headers={"X-Emby-Token": free_token}).json()
    url_free = _upstream_line(detail_free)[0]["DirectStreamUrl"]
    path_free = re.sub(r"^https?://[^/]+", "", url_free)
    r2 = pclient.get(path_free, headers={"X-Emby-Token": free_token},
                     follow_redirects=False)
    assert r2.status_code == 403, r2.text
    assert "location" not in r2.headers


def test_merge_group_none_db_skips_mapping_migration():
    """回归（#1653 首轮 CI 唯一红项）：merge_group(db=None) 不许为迁映射炸掉。

    test_perfection_r17::test_c_p2_1_merge_group_marks_done 以 db=None 调
    merge_group；进库一期在 merge_group 内无条件调 migrate_mappings，
    None.query 直接 AttributeError，冒烟 8568 passed 里唯一失败。
    修法：db=None 时跳过迁映射（无会话本就无映射可迁），纯内存合并照常完成。
    """
    from backend.emby_server import merge_versions_worker

    def _item(i):
        return SimpleNamespace(id=i, merged_into_id=None, name=f"m{i}",
                               enrich_status="pending", poster_path=None,
                               primary_image_url=None, tmdb_id="",
                               overview="", file_path=f"/x/m{i}.mkv")

    group = [_item(1), _item(2)]
    n = merge_versions_worker.merge_group(None, group)
    assert n == 1
    assert group[1].merged_into_id == group[0].id
    assert group[1].enrich_status == "done"

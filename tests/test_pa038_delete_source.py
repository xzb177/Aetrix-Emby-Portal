"""PA-038：删源必须联动清理，否则托管行滞留货架成死条目。

删源前：源行＋托管行（电影＋剧→季→集子树）＋映射＋自动建库
关系＋托管生命周期＋分类绑定＋聚合任务（queued／succeeded／
running 各一）＋一个本地条目及其到该源的映射。
删源后断言：货架口径 0 托管行（软删而非硬删）、映射／关系／
生命周期／绑定／非 running 任务行清零、running 任务行保留、
源行消失、上游线路读口 None、他源不受影响、同名重建不复活。
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import soft_delete, upstream_playback, upstream_sources as ups


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _library(db):
    lib = em.Library(guid="lib-" + uuid.uuid4().hex[:12], name="测试库")
    db.add(lib)
    db.flush()
    return lib


def _source(db, name="src-1"):
    row = em.UpstreamSource(
        name=name, source_type="emby",
        base_url="https://up.example.com/emby", auth_method="none",
        username=None, sealed_secret=None, enabled=True,
        credential_scope="source", health_status="unknown")
    db.add(row)
    db.flush()
    return row


def _hosted(db, lib, source, item_type, name, **kw):
    row = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
        name=name, tmdb_id=kw.pop("tmdb_id", "603"), file_path=None,
        upstream_source_id=source.id, **kw)
    db.add(row)
    db.flush()
    return row


def _seed(db, source_name="src-1"):
    lib = _library(db)
    src = _source(db, source_name)
    movie = _hosted(db, lib, src, "movie", "托管片")
    series = _hosted(db, lib, src, "series", "托管剧", tmdb_id="1399")
    season = _hosted(db, lib, src, "season", "托管季", tmdb_id="1399",
                     parent_id=series.id)
    episode = _hosted(db, lib, src, "episode", "托管集", tmdb_id="1399",
                      parent_id=season.id, series_id=series.id)
    local = em.MediaItem(guid=uuid.uuid4().hex, library_id=lib.id,
                         item_type="movie", name="本地片")
    db.add(local)
    db.flush()
    db.add(em.UpstreamItemMapping(local_item_id=local.id, source_id=src.id,
                                  upstream_item_id="up-1"))
    db.add(em.UpstreamLibraryLink(source_id=src.id, upstream_library_id="up-lib",
                                  local_library_id=lib.id))
    db.add(em.UpstreamHostedLifecycle(source_id=src.id, local_item_id=movie.id))
    db.add(em.SourceCategoryBinding(source_id=src.id,
                                    upstream_category_id="cat-1"))
    db.add(em.UpstreamAggregateTask(task_id="t-queued", source_id=src.id,
                                    status="queued"))
    db.add(em.UpstreamAggregateTask(task_id="t-done", source_id=src.id,
                                    status="succeeded"))
    db.add(em.UpstreamAggregateTask(task_id="t-running", source_id=src.id,
                                    status="running"))
    db.commit()
    return lib, src, local, [movie, series, season, episode]


def _count(db, model, source_id):
    return db.query(model).filter(model.source_id == source_id).count()


def test_delete_source_cascades(db):
    lib, src, local, hosted = _seed(db)
    src_id = src.id
    stats = ups.delete_source(db, src)
    db.commit()
    assert stats["hosted_rows_deleted"] == len(hosted)
    # 货架口径：托管行全部不可见。注意必须用 .all() 而非 .count()：
    # Query.count() 会绕过软删钩子（PA-035 已证），数出来仍是 4。
    assert len(db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src_id).all()) == 0
    # 软删而非硬删：行还在，只是带 deleted_at。
    with soft_delete.include_deleted():
        rows = db.query(em.MediaItem).filter(
            em.MediaItem.upstream_source_id == src_id).all()
    assert len(rows) == len(hosted)
    assert all(r.deleted_at is not None for r in rows)
    # 残留行清零；running 任务行保留（在跑线程拦不住，留待自然失败）。
    assert _count(db, em.UpstreamItemMapping, src_id) == 0
    assert _count(db, em.UpstreamLibraryLink, src_id) == 0
    assert _count(db, em.UpstreamHostedLifecycle, src_id) == 0
    assert _count(db, em.SourceCategoryBinding, src_id) == 0
    tasks = db.query(em.UpstreamAggregateTask).filter(
        em.UpstreamAggregateTask.source_id == src_id).all()
    assert [t.task_id for t in tasks] == ["t-running"]
    # 源行消失、上游线路读口无路。
    assert db.query(em.UpstreamSource).filter(
        em.UpstreamSource.id == src_id).count() == 0
    assert upstream_playback.get_displayable_mapping(db, local.id) is None


def test_other_source_untouched(db):
    lib, src, local, hosted = _seed(db)
    src2 = _source(db, "src-2")
    other = _hosted(db, lib, src2, "movie", "他源片")
    db.add(em.UpstreamItemMapping(local_item_id=local.id, source_id=src2.id,
                                  upstream_item_id="up-2"))
    db.commit()
    ups.delete_source(db, src)
    db.commit()
    assert len(db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src2.id).all()) == 1
    assert _count(db, em.UpstreamItemMapping, src2.id) == 1
    assert other.deleted_at is None


def test_rebuild_same_name_does_not_resurrect(db):
    lib, src, local, hosted = _seed(db)
    src_id = src.id
    ups.delete_source(db, src)
    db.commit()
    src_new = _source(db, "src-1")
    db.commit()
    assert len(db.query(em.MediaItem).filter(
        em.MediaItem.upstream_source_id == src_new.id).all()) == 0
    with soft_delete.include_deleted():
        old = db.query(em.MediaItem).filter(
            em.MediaItem.upstream_source_id == src_id).all()
    assert len(old) == len(hosted)
    assert all(r.deleted_at is not None for r in old)

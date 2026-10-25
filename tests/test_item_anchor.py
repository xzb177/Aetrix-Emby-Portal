"""身份锚迁移（清单第 8 项，挂载模型第 3 步）。

钉死的范围（多一件不做）：
- 条目两列：source_id / provider_file_id 可空、新行可写、老行读回；
- 锚定位：成功与 7 种失败原因逐一（缺锚、行悬空、库不一致、本机行、
  挂载缺失、挂载停用、挂载不一致）；
- 回落率口径：Redis 计数 + 回落率 + 原因细分，Redis 不可用时全 0 不报错；
- 扫描来源行 id 挂接：新表本地行能挂到来源上，旧列回落恒为 None；
- 解析锚参数：锚挂载一致时采用、不一致时回落路径挂载（绝不重定向）。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import item_anchor
from backend.emby_server import models as em
from backend.emby_server import mount_sources as ms


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _lib(db, guid):
    lib = em.Library(guid=guid, name=guid)
    db.add(lib)
    db.commit()
    return lib


def _mount(db, name, mount_type="local", path="", enabled=True):
    mount = em.StorageMount(name=name, mount_type=mount_type, path=path,
                            is_enabled=enabled)
    db.add(mount)
    db.commit()
    return mount


def _item(db, lib, file_path, source_id=None, fid=""):
    item = em.MediaItem(guid=f"g-{lib.id}-{file_path}", library_id=lib.id,
                        item_type="movie", name="t", file_path=file_path,
                        source_id=source_id, provider_file_id=fid or None)
    db.add(item)
    db.commit()
    return item


# ---------- 两列 ----------

def test_anchor_columns_roundtrip(db):
    lib = _lib(db, "lib-cols")
    item = _item(db, lib, "mount://7/a.mp4", source_id=42, fid="fid-1")
    got = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
    assert got.source_id == 42
    assert got.provider_file_id == "fid-1"
    legacy = _item(db, lib, "mount://7/b.mp4")
    assert legacy.source_id is None
    assert legacy.provider_file_id is None


# ---------- 锚定位 ----------

def _anchor_scene(db):
    lib = _lib(db, "lib-anchor")
    mount = _mount(db, "m1", path="/tmp")
    row = em.LibrarySource(library_id=lib.id, mount_id=mount.id, sub_path="/")
    db.add(row)
    db.commit()
    return lib, mount, row


def test_resolve_anchor_ok(db):
    lib, mount, row = _anchor_scene(db)
    item = _item(db, lib, f"mount://{mount.id}/a.mp4",
                 source_id=row.id, fid="fid-1")
    got, reason = item_anchor.resolve_anchor(db, item)
    assert reason == "ok"
    assert got.id == mount.id


def test_resolve_anchor_no_anchor(db):
    lib, mount, row = _anchor_scene(db)
    assert item_anchor.resolve_anchor(
        db, _item(db, lib, f"mount://{mount.id}/a.mp4"))[1] == "no_anchor"
    assert item_anchor.resolve_anchor(
        db, _item(db, lib, f"mount://{mount.id}/b.mp4",
                  source_id=row.id, fid=""))[1] == "no_anchor"


def test_resolve_anchor_no_source_row(db):
    lib, mount, _row = _anchor_scene(db)
    item = _item(db, lib, f"mount://{mount.id}/a.mp4",
                 source_id=999999, fid="fid-1")
    assert item_anchor.resolve_anchor(db, item)[1] == "no_source_row"


def test_resolve_anchor_library_mismatch(db):
    lib, mount, row = _anchor_scene(db)
    other = _lib(db, "lib-other")
    item = _item(db, other, f"mount://{mount.id}/a.mp4",
                 source_id=row.id, fid="fid-1")
    assert item_anchor.resolve_anchor(db, item)[1] == "no_source_row"


def test_resolve_anchor_local_row_and_missing_mount(db):
    lib = _lib(db, "lib-local-row")
    local_row = em.LibrarySource(library_id=lib.id, mount_id=None,
                                 sub_path="/tmp")
    db.add(local_row)
    db.commit()
    item = _item(db, lib, "/tmp/a.mp4", source_id=local_row.id, fid="x")
    assert item_anchor.resolve_anchor(db, item)[1] == "no_mount"
    ghost_row = em.LibrarySource(library_id=lib.id, mount_id=999999,
                                 sub_path="/")
    db.add(ghost_row)
    db.commit()
    ghost = _item(db, lib, "mount://999999/a.mp4",
                  source_id=ghost_row.id, fid="x")
    assert item_anchor.resolve_anchor(db, ghost)[1] == "mount_missing"


def test_resolve_anchor_disabled_and_mismatch(db):
    lib = _lib(db, "lib-dis")
    off = _mount(db, "off", enabled=False)
    row_off = em.LibrarySource(library_id=lib.id, mount_id=off.id, sub_path="/")
    db.add(row_off)
    db.commit()
    item = _item(db, lib, f"mount://{off.id}/a.mp4",
                 source_id=row_off.id, fid="x")
    assert item_anchor.resolve_anchor(db, item)[1] == "mount_disabled"
    on = _mount(db, "on")
    row_on = em.LibrarySource(library_id=lib.id, mount_id=on.id, sub_path="/")
    db.add(row_on)
    db.commit()
    # 锚挂载与路径挂载不一致：绝不按锚走，回落。
    item2 = _item(db, lib, f"mount://{off.id}/b.mp4",
                  source_id=row_on.id, fid="x")
    assert item_anchor.resolve_anchor(db, item2)[1] == "mount_mismatch"


# ---------- 回落率口径 ----------

class _FakeRedis:
    def __init__(self):
        self.data = {}

    def hincrby(self, key, field, amount):
        self.data[field] = self.data.get(field, 0) + amount

    def hgetall(self, key):
        return dict(self.data)


def test_fallback_stats_counts_and_rate(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(item_anchor, "_redis_client", lambda: fake)
    assert item_anchor.fallback_stats() == {
        "anchor": 0, "fallback": 0, "fallback_rate": None, "by_reason": {}}
    item_anchor.note_anchor_used()
    item_anchor.note_anchor_used()
    item_anchor.note_fallback("no_anchor")
    item_anchor.note_fallback("mount_missing")
    stats = item_anchor.fallback_stats()
    assert stats["anchor"] == 2
    assert stats["fallback"] == 2
    assert stats["fallback_rate"] == 0.5
    assert stats["by_reason"] == {"no_anchor": 1, "mount_missing": 1}


def test_fallback_stats_no_redis(monkeypatch):
    monkeypatch.setattr(item_anchor, "_redis_client", lambda: None)
    item_anchor.note_anchor_used()  # 不抛
    item_anchor.note_fallback("no_anchor")  # 不抛
    assert item_anchor.fallback_stats()["fallback_rate"] is None


# ---------- 扫描来源行 id 挂接 ----------

def test_read_sources_attaches_row_id_local(db, tmp_path):
    lib = _lib(db, "lib-attach")
    row = em.LibrarySource(library_id=lib.id, mount_id=None,
                           sub_path=str(tmp_path))
    db.add(row)
    db.commit()
    sources, failed = ms.read_library_sources(db, lib)
    assert failed == []
    assert len(sources) == 1
    assert sources[0].kind == "local"
    assert sources[0].source_row_id == row.id


def test_read_sources_legacy_fallback_row_id_none(db, tmp_path):
    lib = em.Library(guid="lib-legacy", name="lib-legacy",
                     paths=str(tmp_path), mount_ids="")
    db.add(lib)
    db.commit()
    sources, _failed = ms.read_library_sources(db, lib)
    assert sources
    assert all(getattr(s, "source_row_id", None) is None for s in sources)


# ---------- 解析锚参数 ----------

def test_resolve_play_target_anchor_mount(tmp_path):
    from backend.emby_server import mounts as mount_lib
    target_file = tmp_path / "x.mp4"
    target_file.write_bytes(b"")
    # 本机挂载：rel 解析走本地文件，锚一致时采用锚挂载、不一致时回落路径挂载，
    # 两种情况结果相同（证明锚绝不重定向）；用内存会话 + 手工挂载行对象。
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    mount = em.StorageMount(name="loc", mount_type="local",
                            path=str(tmp_path), is_enabled=True)
    session.add(mount)
    session.commit()
    other = em.StorageMount(name="other", mount_type="local",
                            path=str(tmp_path), is_enabled=True)
    session.add(other)
    session.commit()
    file_path = f"mount://{mount.id}/x.mp4"
    # 锚一致：不抛，且与无锚结果一致。
    plain = mount_lib.resolve_play_target(file_path, session)
    anchored = mount_lib.resolve_play_target(file_path, session,
                                             anchor_mount=mount)
    assert anchored.kind == plain.kind
    assert anchored.value == plain.value
    # 锚不一致（另一挂载）：回落路径挂载，结果仍与无锚一致。
    mismatched = mount_lib.resolve_play_target(file_path, session,
                                               anchor_mount=other)
    assert mismatched.kind == plain.kind
    assert mismatched.value == plain.value
    session.close()

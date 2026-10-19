"""扫描误删防护：子目录列举失败 / 空来源 / 改名指纹（三处 Hark 定位的问题）

事故形状：扫描时某个子目录列举报错（错误被吞掉），或某个来源 transient 返回空，
清理阶段会把这些"没见到"的文件条目软删除——连带播放进度/收藏一起下架。

本文件钉住三道修复：
1. 子目录列举报错 → 必须抛出来（上层记进 failed_roots 跳过清理），不能吞掉；
2. 多源片库里某个来源本轮列出 0 个文件、但库里该来源下还有条目 → 视为不可用
   （failed_roots），不能当成"文件全删了"；
3. 本地/115 改名（无 file_id）→ 用 (大小, 解析身份) 指纹认出是改名，复用旧行，
   保住播放进度与元数据；歧义一律放弃。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import scanner
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _lib(db, paths=""):
    row = em.Library(guid="gres", name="测试库", collection_type="movies",
                     paths=paths, storage_backends="")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _movie(db, lib, guid, path, size=1000, deleted_at=None):
    row = em.MediaItem(guid=guid, library_id=lib.id, item_type="movie",
                       name=guid, file_path=path, size=size,
                       deleted_at=deleted_at)
    db.add(row)
    db.commit()
    return row


# ==================== 1. 子目录列举失败必须抛出来 ====================

class _FakeRemote(mount_lib.RemoteMount):
    """假远端挂载：/dir_b 列目录抛错，其余正常"""
    what = "测试远端"

    def __init__(self):
        pass  # 跳过 MountProvider.__init__（要读挂载配置）

    def walk_workers(self):
        return 2

    def list_dir(self, rel="/"):
        if rel == "/dir_b":
            raise mount_lib.MountError("permission denied: /dir_b")
        if rel == "/":
            return [
                mount_lib.MountEntry(name="dir_a", rel="/dir_a", is_dir=True, size=0),
                mount_lib.MountEntry(name="dir_b", rel="/dir_b", is_dir=True, size=0),
                mount_lib.MountEntry(name="root.mkv", rel="/root.mkv", is_dir=False, size=10),
            ]
        if rel == "/dir_a":
            return [
                mount_lib.MountEntry(name="a1.mkv", rel="/dir_a/a1.mkv", is_dir=False, size=11),
            ]
        return []


def test_remote_walk_subdir_error_raises():
    """远端子目录列举失败：walk_media 收尾必须抛 MountError（不能吞掉）"""
    prov = _FakeRemote()
    got = []
    with pytest.raises(mount_lib.MountError) as ei:
        for mf in prov.walk_media(root="/"):
            got.append(mf.rel)
    assert "dir_b" in str(ei.value), f"错误信息要带上失败的子目录: {ei.value}"
    # 好目录的文件照常产出（不中断整库扫描）
    assert "/root.mkv" in got
    assert "/dir_a/a1.mkv" in got


def test_local_mount_walk_subdir_error_raises(tmp_path, monkeypatch):
    """本地挂载子目录读不了：walk_media 收尾必须抛 MountError"""
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.mkv").write_bytes(b"x" * 10)
    bad = tmp_path / "badsub"
    bad.mkdir()

    # 用假 os.walk 模拟子目录列举失败（root 下跑测试时 chmod 000 无效）
    import os as _os
    _real_walk = _os.walk  # 先捕获，patch 后 _os.walk 就是 fake 自身了

    def fake_walk(top, topdown=True, onerror=None, followlinks=False):
        if onerror is not None:
            err = PermissionError(13, "Permission denied",
                                  str(bad))
            onerror(err)
        return _real_walk(str(good), topdown=topdown)

    monkeypatch.setattr(mount_lib.os, "walk", fake_walk)

    class _FakeLocal(mount_lib.LocalMount):
        def __init__(self, path):
            self._p = path

        def _require_path(self):
            return self._p

    prov = _FakeLocal(str(tmp_path))
    with pytest.raises(mount_lib.MountError) as ei:
        list(prov.walk_media("/"))
    assert "badsub" in str(ei.value)


def test_local_dir_files_records_subdir_in_failed_roots(tmp_path, monkeypatch):
    """_local_dir_files：子目录失败要进 failed_roots，且以来源标签开头（供 _source_entry 归因）"""
    root = str(tmp_path)

    def fake_walk(top, onerror=None):
        if onerror is not None:
            onerror(PermissionError(13, "Permission denied",
                                    os.path.join(root, "sub", "bad")))
        return iter([])

    monkeypatch.setattr(scanner.os, "walk", fake_walk)
    failed = []
    list(scanner._local_dir_files(root, failed))
    assert len(failed) == 1
    assert failed[0].startswith(f"{root}: "), f"必须以来源标签开头: {failed[0]}"
    assert "bad" in failed[0]


# ==================== 2. 空来源视为不可用 ====================

def _src(kind, label, path="", mount_id=None, subpath="/", local_root=None):
    return SimpleNamespace(
        kind=kind, label=label, path=path,
        mount=SimpleNamespace(id=mount_id) if mount_id else None,
        provider=SimpleNamespace(local_root=local_root),
        subpath=subpath,
    )


def test_source_path_prefix_local():
    src = _src("local", "/mnt/mp/series", path="/mnt/mp/series")
    assert scanner._source_path_prefix(src) == "/mnt/mp/series/"


def test_source_path_prefix_remote_mount():
    src = _src("mount", "115剧集", mount_id=7, subpath="/剧集")
    assert scanner._source_path_prefix(src) == "mount://7/剧集/"


def test_source_path_prefix_remote_mount_root():
    src = _src("mount", "115", mount_id=7, subpath="/")
    assert scanner._source_path_prefix(src) == "mount://7/"


def test_empty_source_with_items_is_unavailable(db, tmp_path):
    """来源本轮 0 产出、但库里该来源下还有条目 → failed_roots（跳过清理）"""
    lib = _lib(db, str(tmp_path))
    _movie(db, lib, "g1", str(tmp_path / "a.mkv"))
    _movie(db, lib, "g2", str(tmp_path / "b.mkv"))

    src = _src("local", str(tmp_path), path=str(tmp_path))
    failed = []
    out = list(scanner._watch_empty_source(
        str(tmp_path), src, iter([]), db, lib.id, failed))
    assert out == []
    assert len(failed) == 1
    assert "0 个文件" in failed[0]


def test_empty_source_truly_empty_ok(db, tmp_path):
    """来源本轮 0 产出、库里该来源下也没有条目 → 真空，不记 failed_roots"""
    lib = _lib(db, str(tmp_path))
    src = _src("local", str(tmp_path), path=str(tmp_path))
    failed = []
    out = list(scanner._watch_empty_source(
        str(tmp_path), src, iter([]), db, lib.id, failed))
    assert out == []
    assert failed == []


def test_nonempty_source_no_check(db, tmp_path):
    """来源有产出 → 不做任何判定"""
    lib = _lib(db, str(tmp_path))
    _movie(db, lib, "g1", str(tmp_path / "a.mkv"))
    src = _src("local", str(tmp_path), path=str(tmp_path))
    failed = []

    class _F:
        stored_path = "x"
    out = list(scanner._watch_empty_source(
        str(tmp_path), src, iter([_F()]), db, lib.id, failed))
    assert len(out) == 1
    assert failed == []


def test_targeted_scan_empty_source_not_flagged(db, tmp_path):
    """定向扫描（check_empty=False）：来源 0 产出是正常的，不记 failed_roots"""
    lib = _lib(db, str(tmp_path))
    _movie(db, lib, "g1", str(tmp_path / "a.mkv"))
    src = _src("local", str(tmp_path), path=str(tmp_path))
    failed = []
    out = list(scanner._watch_empty_source(
        str(tmp_path), src, iter([]), db, lib.id, failed, check_empty=False))
    assert out == []
    assert failed == []


# ==================== 3. 改名指纹匹配 ====================

class _FakeSF:
    def __init__(self, stored_path, size, file_id=""):
        self.stored_path = stored_path
        self.size = size
        self.file_id = file_id


class _FakePending:
    def __init__(self, scan_file, parsed, item_type, series_name=""):
        self.scan_file = scan_file
        self.parsed = parsed
        self.item_type = item_type
        self.series_name = series_name
        self.item = None
        self.renamed = False


class _FakeCtx:
    def __init__(self, lib_id):
        self.lib_id = lib_id
        self.seen_guids = set()
        self.stats = {}


def test_fingerprint_rename_movie(db, tmp_path):
    """电影改名：同大小 + 同标题同年 → 复用旧行"""
    lib = _lib(db, str(tmp_path))
    old = _movie(db, lib, "gold", str(tmp_path / "OldName (2020).mkv"), size=12345)

    # 真实改名场景：同一目录下文件名加了清晰度/来源标签，标题和年份不变
    new_path = str(tmp_path / "OldName (2020).1080p.WEB-DL.mkv")
    parsed = scanner.parse_media_filename(new_path, "movies")
    assert parsed["name"] == "OldName" and parsed["year"] == 2020
    sf = _FakeSF(new_path, 12345)
    p = _FakePending(sf, parsed, "movie")
    ctx = _FakeCtx(lib.id)
    scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
    assert p.renamed is True
    assert p.item is not None and p.item.id == old.id
    assert ctx.stats.get("renamed") == 1


def test_fingerprint_rename_episode(db, tmp_path):
    """剧集改名：同大小 + 同剧名同季集 → 复用旧行"""
    lib = _lib(db, str(tmp_path))
    old_path = str(tmp_path / "MyShow" / "Season 1" / "MyShow.S01E05.mkv")
    row = em.MediaItem(guid="gep", library_id=lib.id, item_type="episode",
                       name="gep", file_path=old_path, size=999)
    db.add(row)
    db.commit()

    sf = _FakeSF(str(tmp_path / "MyShow" / "Season 1" / "MyShow.S01E05.1080p.mkv"), 999)
    p = _FakePending(sf, {"name": "MyShow", "year": None, "season": 1,
                          "episode": 5}, "episode", series_name="MyShow")
    ctx = _FakeCtx(lib.id)
    scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
    assert p.renamed is True
    assert p.item is not None and p.item.id == row.id


def test_fingerprint_rename_episode_mount_path(db, tmp_path):
    """剧集改名（远端 mount:// 路径）：dir_rel 取目录，剧名推导正确"""
    lib = _lib(db, str(tmp_path))
    old_path = "mount://7/MyShow/Season 1/MyShow.S01E05.mkv"
    row = em.MediaItem(guid="gep2", library_id=lib.id, item_type="episode",
                       name="gep2", file_path=old_path, size=999)
    db.add(row)
    db.commit()

    new_path = "mount://7/MyShow/Season 1/MyShow.S01E05.1080p.mkv"
    parsed = scanner.parse_media_filename(new_path, "tvshows")
    assert (parsed["season"], parsed["episode"]) == (1, 5)
    # pending 侧的 series_name 在真实流程里由 _series_name_of 算出，这里同口径构造
    from types import SimpleNamespace as _NS
    series_name = scanner._series_name_of(
        _NS(local_dir=None, dir_rel="/MyShow/Season 1"), parsed)
    assert series_name == "MyShow"
    sf = _FakeSF(new_path, 999)
    p = _FakePending(sf, parsed, "episode", series_name=series_name)
    ctx = _FakeCtx(lib.id)
    scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
    assert p.renamed is True
    assert p.item is not None and p.item.id == row.id


def test_fingerprint_ambiguous_skipped(db, tmp_path):
    """同一指纹 2 个候选 → 歧义，放弃（不张冠李戴）"""
    lib = _lib(db, str(tmp_path))
    _movie(db, lib, "g1", str(tmp_path / "OldName (2020).mkv"), size=12345)
    _movie(db, lib, "g2", str(tmp_path / "sub" / "OldName (2020).mkv"), size=12345)

    new_path = str(tmp_path / "OldName (2020).2160p.mkv")
    parsed = scanner.parse_media_filename(new_path, "movies")
    sf = _FakeSF(new_path, 12345)
    p = _FakePending(sf, parsed, "movie")
    ctx = _FakeCtx(lib.id)
    scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
    assert p.renamed is False
    assert p.item is None


def test_fingerprint_size_mismatch_skipped(db, tmp_path):
    """大小变了 → 不是改名，不匹配"""
    lib = _lib(db, str(tmp_path))
    _movie(db, lib, "g1", str(tmp_path / "OldName (2020).mkv"), size=12345)

    new_path = str(tmp_path / "OldName (2020).2160p.mkv")
    parsed = scanner.parse_media_filename(new_path, "movies")
    sf = _FakeSF(new_path, 54321)
    p = _FakePending(sf, parsed, "movie")
    ctx = _FakeCtx(lib.id)
    scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
    assert p.renamed is False


def test_fingerprint_with_file_id_skipped(db, tmp_path):
    """有 file_id 的文件不走指纹（file_id 版是权威）"""
    lib = _lib(db, str(tmp_path))
    _movie(db, lib, "g1", str(tmp_path / "OldName (2020).mkv"), size=12345)

    new_path = str(tmp_path / "OldName (2020).2160p.mkv")
    parsed = scanner.parse_media_filename(new_path, "movies")
    sf = _FakeSF(new_path, 12345, file_id="FID_X")
    p = _FakePending(sf, parsed, "movie")
    ctx = _FakeCtx(lib.id)
    scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
    assert p.renamed is False
    assert p.item is None


def test_fingerprint_seen_guid_excluded(db, tmp_path):
    """候选行的 guid 本轮还见到了 → 是重复文件不是改名，不匹配"""
    lib = _lib(db, str(tmp_path))
    _movie(db, lib, "g1", str(tmp_path / "OldName (2020).mkv"), size=12345)

    new_path = str(tmp_path / "OldName (2020).2160p.mkv")
    parsed = scanner.parse_media_filename(new_path, "movies")
    sf = _FakeSF(new_path, 12345)
    p = _FakePending(sf, parsed, "movie")
    ctx = _FakeCtx(lib.id)
    ctx.seen_guids.add("g1")
    scanner._match_renames_by_fingerprint(db, [p], {}, ctx)
    assert p.renamed is False


def test_attach_known_items_keeps_rename_match():
    """改名匹配挂上来的旧行，不能被 known 覆盖（回归：之前无条件覆盖导致改名失效）"""
    sentinel = object()

    class _P:
        def __init__(self):
            self.item = sentinel  # 改名匹配已挂上旧行
            self.guid = "new-guid"

    p = _P()
    scanner._attach_known_items([p], {"new-guid": None})
    assert p.item is sentinel

    p2 = _P()
    p2.item = None
    row = SimpleNamespace(id=4242)  # r60：attach 返回 claimed 行 id 集合，dummy 需带 id
    scanner._attach_known_items([p2], {"new-guid": row})
    assert p2.item is row

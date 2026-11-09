"""分层扫描（v2.40.0）：文件指纹秒跳 + L1 极简入库。

- 文件指纹：本机=path|size|mtime，远程=path|size；
- 秒跳：指纹命中且 enrich_status='done' → 零 IO，不提交任何预取；
- L1：新文件/指纹变化 → 极简入库，enrich_status='pending'，等后台补全。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
# 测试用独立临时 DB：避免与其它测试文件共用默认 DB 导致数据污染，
# 也避免在容器里误连 /data 生产库。
# 注意：backend.database.engine 是模块级单例，必须在设置 DATABASE_URL 后
# 重建 engine，否则后导入的测试文件会沿用先导入文件的 DB。
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
# backend.database.engine 是模块级单例：后导入的测试文件必须重建 engine，
# 否则会沿用先导入文件的 DB，导致测试间污染。只重建 engine/SessionLocal，
# 不 reload 整个模块（避免 models.Base 元数据错乱）。
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。

import uuid
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import scanner



@pytest.fixture(scope="module", autouse=True)
def _hygiene_module_engine():
    """测试卫生：本文件独占临时库在模块夹具内换绑、模块结束原样还原。

    导入期顶层换绑会让全局 engine/SessionLocal 在 collection 后停在本文件
    的临时库上，后面的文件/用例撞 no such table 或读错库（#1668 同族）；
    换绑只在本模块用例期间生效。
    """
    _hyg_saved_engine = _dbmod.engine
    _hyg_saved_factory = _dbmod.SessionLocal._factory
    _hyg_saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
    _dbmod.engine = _ce(os.environ["DATABASE_URL"])
    _dbmod.configure_session_local(_sm(bind=_dbmod.engine))
    init_db()
    _hyg_engine = _dbmod.engine
    try:
        yield
    finally:
        _dbmod.engine = _hyg_saved_engine
        _dbmod.configure_session_local(_hyg_saved_factory)
        if _hyg_saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _hyg_saved_url
        _hyg_engine.dispose()

@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _guid():
    return uuid.uuid4().hex


def _mkfile(path="/v/x.mp4", size=100, mtime_ns=123):
    # 本机文件：local_target 给一个假的播放目标，避免 probe_input() 走
    # provider.resolve_final（测试里没有 provider）。用 SimpleNamespace
    # 模拟 target 的 .value/.headers 接口。
    from types import SimpleNamespace as _SN
    return scanner.ScanFile(
        stored_path=path, name=os.path.basename(path),
        local_dir=os.path.dirname(path), size=size, mtime_ns=mtime_ns,
        local_target=_SN(value=path, headers={}),
    )


def test_file_fingerprint_local_uses_mtime():
    a = _mkfile("/v/a.mp4", size=100, mtime_ns=111)
    b = _mkfile("/v/a.mp4", size=100, mtime_ns=222)
    c = _mkfile("/v/a.mp4", size=100, mtime_ns=111)
    assert scanner._file_fingerprint(a) != scanner._file_fingerprint(b)
    assert scanner._file_fingerprint(a) == scanner._file_fingerprint(c)


def test_file_fingerprint_remote_no_mtime():
    a = scanner.ScanFile(stored_path="mount://2/a.mp4", name="a.mp4",
                         local_dir=None, size=100, mtime_ns=None)
    b = scanner.ScanFile(stored_path="mount://2/a.mp4", name="a.mp4",
                         local_dir=None, size=200, mtime_ns=None)
    assert scanner._file_fingerprint(a) != scanner._file_fingerprint(b)


def test_fast_skip_no_io(db):
    """指纹命中 + 已补全 → 秒跳：不提交 side/NFO/TMDB/probe"""
    lib = em.Library(guid=_guid(), name="t", collection_type="movies")
    db.add(lib)
    db.flush()
    sf = _mkfile("/v/x.mp4")
    fp = scanner._file_fingerprint(sf)
    guid = scanner.item_guid(sf.stored_path)
    item = em.MediaItem(guid=guid, library_id=lib.id, item_type="movie",
                        name="x", file_path=sf.stored_path,
                        file_fingerprint=fp, enrich_status="done")
    db.add(item)
    db.commit()

    snap = SimpleNamespace(
        library_id=lib.id, collection_type="movies", scrape_policy="smart")
    ctx = scanner._ScanContext(snap=snap, lib_id=lib.id, stats={})
    with mock.patch.object(scanner, "_dir_fingerprint",
                           side_effect=AssertionError("不应算目录指纹")):
        prepared = scanner._prepare_and_prefetch(db, [sf], ctx, pool=None)
    assert len(prepared) == 1
    p = prepared[0]
    assert p.fast_skipped is True
    assert p.side is None and p.nfo is None and p.tmdb is None
    assert p.probe is None and not p.probe_deferred
    assert ctx.stats.get("unchanged") == 1


def test_fingerprint_mismatch_not_skipped(db):
    """指纹对不上（文件变了）→ 走正常流程"""
    lib = em.Library(guid=_guid(), name="t", collection_type="movies")
    db.add(lib)
    db.flush()
    sf = _mkfile("/v/y.mp4", size=100, mtime_ns=111)
    guid = scanner.item_guid(sf.stored_path)
    item = em.MediaItem(guid=guid, library_id=lib.id, item_type="movie",
                        name="y", file_path=sf.stored_path,
                        file_fingerprint="stale", enrich_status="done")
    db.add(item)
    db.commit()

    # 文件大小变了 → 指纹变化
    sf2 = _mkfile("/v/y.mp4", size=999, mtime_ns=111)
    snap = SimpleNamespace(
        library_id=lib.id, collection_type="movies", scrape_policy="smart")
    ctx = scanner._ScanContext(snap=snap, lib_id=lib.id, stats={})
    with mock.patch.object(scanner, "SCAN_LAYERED", False):
        prepared = scanner._prepare_and_prefetch(db, [sf2], ctx, pool=None)
    assert len(prepared) == 1
    assert prepared[0].fast_skipped is False


def test_pending_enrich_not_fast_skipped(db):
    """指纹命中但 enrich_status='pending'（L2 没补完）→ 不秒跳，等补全"""
    lib = em.Library(guid=_guid(), name="t", collection_type="movies")
    db.add(lib)
    db.flush()
    sf = _mkfile("/v/z.mp4")
    fp = scanner._file_fingerprint(sf)
    guid = scanner.item_guid(sf.stored_path)
    item = em.MediaItem(guid=guid, library_id=lib.id, item_type="movie",
                        name="z", file_path=sf.stored_path,
                        file_fingerprint=fp, enrich_status="pending")
    db.add(item)
    db.commit()

    snap = SimpleNamespace(
        library_id=lib.id, collection_type="movies", scrape_policy="smart")
    ctx = scanner._ScanContext(snap=snap, lib_id=lib.id, stats={})
    with mock.patch.object(scanner, "SCAN_LAYERED", False):
        prepared = scanner._prepare_and_prefetch(db, [sf], ctx, pool=None)
    assert prepared[0].fast_skipped is False


def test_layered_new_file_minimal(db):
    """分层 L1：远程新文件只做极简入库，不提交 IO，enrich_status='pending'"""
    lib = em.Library(guid=_guid(), name="t", collection_type="movies")
    db.add(lib)
    db.flush()
    # 远程文件：local_dir=None
    sf = scanner.ScanFile(stored_path="mount://1/v/new.mp4", name="new.mp4",
                          local_dir=None, size=100, mtime_ns=123)
    snap = SimpleNamespace(
        library_id=lib.id, collection_type="movies", scrape_policy="smart")
    ctx = scanner._ScanContext(snap=snap, lib_id=lib.id, stats={})
    # SCAN_LAYERED 默认就是 1，这里显式确认行为
    assert scanner.SCAN_LAYERED is True
    prepared = scanner._prepare_and_prefetch(db, [sf], ctx, pool=None)
    assert len(prepared) == 1
    p = prepared[0]
    assert p.layered is True
    assert p.side is None and p.nfo is None and p.tmdb is None


def test_layered_local_file_does_side_nfo(db):
    """分层 L1：本机文件照常提交 side/NFO（本地 IO 快，保证海报立即可见）"""
    import concurrent.futures
    lib = em.Library(guid=_guid(), name="t2", collection_type="movies")
    db.add(lib)
    db.flush()
    sf = _mkfile("/v/local.mp4")  # local_dir="/v" → 本机文件
    snap = SimpleNamespace(
        library_id=lib.id, collection_type="movies", scrape_policy="smart")
    ctx = scanner._ScanContext(snap=snap, lib_id=lib.id, stats={})
    assert scanner.SCAN_LAYERED is True
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        prepared = scanner._prepare_and_prefetch(db, [sf], ctx, pool=pool)
    p = prepared[0]
    assert p.layered is True
    assert p.layered_local_fast is True
    # 本机文件：side/NFO 已提交（Future 非空），TMDB 仍跳过
    assert p.side is not None
    assert p.nfo is not None
    assert p.tmdb is None

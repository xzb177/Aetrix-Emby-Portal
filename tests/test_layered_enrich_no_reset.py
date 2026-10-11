"""分层扫描 enrich_status 误重置回归测试（2026-10-11 生产事故）。

事故：change_watcher 检测到 85,758 个 .strm 文件 mtime 变化，触发 11 个库
自动扫描。scanner.py 分层模式无条件执行 ``item.enrich_status = "pending"``，
把 23,896 条已 done 的条目打回待补全队列，积压 0→25000。

修复：分层模式下只有新文件才标 pending；已存在的条目保持原 enrich_status。
"""
import os
import time

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import mounts as _mounts  # noqa: F401 -- 必须先于 scanner 导入，避免循环导入
from backend.emby_server import scanner

init_db()


def _guid():
    return uuid.uuid4().hex


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _cleanup(db, lib):
    item_ids = [r[0] for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.library_id == lib.id).all()]
    if item_ids:
        db.query(em.MediaStream).filter(
            em.MediaStream.item_id.in_(item_ids)).delete(
                synchronize_session=False)
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib.id).delete(
        synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib.id).delete()
    db.commit()


def _scanfile(path, size=None):
    st = os.stat(path)
    return scanner.ScanFile(
        stored_path=str(path), name=path.name,
        local_dir=str(path.parent),
        size=size if size is not None else st.st_size,
        container="mp4",
        mtime_ns=st.st_mtime_ns,
    )


def _run_scan(monkeypatch, tmp_path, db, lib):
    """用假遍历器跑一轮完整扫描。"""
    monkeypatch.setattr(scanner, "PROBE_BACKGROUND", True)
    monkeypatch.setattr(
        scanner, "probe_metadata",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("background 模式不应探测")))

    files = sorted(tmp_path.iterdir())

    def fake_sources(snap, library, db, failed_roots, report=None):
        yield "test", iter([_scanfile(f) for f in files])

    monkeypatch.setattr(scanner, "iter_scan_sources", fake_sources)
    return scanner.scan_library_sync(db, lib)


def test_layered_new_file_gets_pending(tmp_path, monkeypatch, db):
    """新文件在分层模式下仍正确标 pending（原有行为不变）。"""
    movie = tmp_path / "New Movie (2024).mp4"
    movie.write_bytes(b"\x00" * 64)

    lib = em.Library(guid=_guid(), name="新文件测试库",
                     collection_type="movies", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        _run_scan(monkeypatch, tmp_path, db, lib)
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "pending", \
            f"新文件应标 pending，实际 {row.enrich_status}"
    finally:
        _cleanup(db, lib)


def test_layered_size_change_triggers_reenrich(tmp_path, monkeypatch, db):
    """已 done 条目文件大小真实变化 → 分层扫描应标 pending 重新补全。

    注意：mtime 不可靠（.strm 批量重生成的生产事故），只有大小变化
    才视为内容真实变更（与 scanner.py 下方 probe 优先级的 size_changed
    口径一致）。round3 重做版。
    """
    movie = tmp_path / "Changed Movie (2024).mp4"
    movie.write_bytes(b"\x00" * 64)

    lib = em.Library(guid=_guid(), name="大小变更测试库",
                     collection_type="movies", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        _run_scan(monkeypatch, tmp_path, db, lib)
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        # 模拟 enrich 完成
        row.enrich_status = "done"
        db.commit()
        db.expire_all()

        # 改文件内容（大小 64→128）+ mtime 变
        movie.write_bytes(b"\x01" * 128)
        new_mtime = time.time() + 3600
        os.utime(movie, (new_mtime, new_mtime))

        _run_scan(monkeypatch, tmp_path, db, lib)
        db.expire_all()
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "pending", \
            f"大小变更的老条目应重新 pending，实际 {row.enrich_status}"
    finally:
        _cleanup(db, lib)


def test_layered_failed_self_heals_on_size_change(tmp_path, monkeypatch, db):
    """enrich 失败的老条目 + 文件大小变化 → 标 pending 自愈，
    且 attempts 清零、next_retry_at 设为未来（1 小时退避）。

    不清零/不退避会导致「扫描→pending→worker 立刻抢走→尝试→又 failed→
    下次扫描又 pending」无限空转，每轮都烧 TMDB 配额。round3 重做版。
    """
    from datetime import datetime

    movie = tmp_path / "Failed Movie (2024).mp4"
    movie.write_bytes(b"\x00" * 64)

    lib = em.Library(guid=_guid(), name="失败自愈测试库",
                     collection_type="movies", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        _run_scan(monkeypatch, tmp_path, db, lib)
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        # 模拟 enrich 重试超限转 failed
        row.enrich_status = "failed"
        row.enrich_attempts = 5
        row.enrich_next_retry_at = None
        db.commit()
        db.expire_all()

        # 文件大小变化 + mtime 变
        movie.write_bytes(b"\x01" * 128)
        new_mtime = time.time() + 3600
        os.utime(movie, (new_mtime, new_mtime))

        before = datetime.now()
        _run_scan(monkeypatch, tmp_path, db, lib)
        db.expire_all()
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "pending", \
            f"failed 条目大小变化后应自愈为 pending，实际 {row.enrich_status}"
        assert (row.enrich_attempts or 0) == 0, \
            f"自愈必须清零 attempts，实际 {row.enrich_attempts}"
        assert row.enrich_next_retry_at is not None and \
            row.enrich_next_retry_at > before, \
            "自愈必须设置未来时间的退避，否则 worker 立刻抢走造成空转"
    finally:
        _cleanup(db, lib)


def test_layered_failed_stays_on_mtime_only(tmp_path, monkeypatch, db):
    """enrich 失败的老条目仅 mtime 变化（大小不变）→ 保持 failed，
    不触发自愈。防止 .strm 这类 mtime 抖动把 failed 条目反复打回队列
    造成空转烧配额。round3 重做版。"""
    movie = tmp_path / "Failed Mtime Movie (2024).mp4"
    movie.write_bytes(b"\x00" * 64)

    lib = em.Library(guid=_guid(), name="失败mtime测试库",
                     collection_type="movies", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        _run_scan(monkeypatch, tmp_path, db, lib)
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        row.enrich_status = "failed"
        row.enrich_attempts = 5
        row.enrich_next_retry_at = None
        db.commit()
        db.expire_all()

        # 只改 mtime，大小不变
        new_mtime = time.time() + 3600
        os.utime(movie, (new_mtime, new_mtime))

        _run_scan(monkeypatch, tmp_path, db, lib)
        db.expire_all()
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "failed", \
            f"仅 mtime 变化不应触发 failed 自愈，实际 {row.enrich_status}"
    finally:
        _cleanup(db, lib)


def test_layered_mtime_change_keeps_done(tmp_path, monkeypatch, db):
    """已 done 条目仅 mtime 变化 → 分层扫描不得重置为 pending（事故复现）。

    模拟：文件已入库且 enrich 已完成（done），之后文件 mtime 变化
    （如 .strm 批量重生成），再次扫描时 enrich_status 必须保持 done。
    """
    movie = tmp_path / "Old Movie (2023).mp4"
    movie.write_bytes(b"\x00" * 64)

    lib = em.Library(guid=_guid(), name="mtime测试库",
                     collection_type="movies", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        # 第一轮：入库（分层 → pending）
        _run_scan(monkeypatch, tmp_path, db, lib)
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "pending"

        # 模拟 enrich_worker 补全完成
        row.enrich_status = "done"
        db.commit()
        db.expire_all()

        # 只改 mtime（内容不变），模拟 .strm 重生成
        new_mtime = time.time() + 3600
        os.utime(movie, (new_mtime, new_mtime))

        # 第二轮：mtime 变化触发重新处理
        _run_scan(monkeypatch, tmp_path, db, lib)
        db.expire_all()
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "done", \
            f"已 done 条目仅 mtime 变化不应被重置，实际 {row.enrich_status}"
    finally:
        _cleanup(db, lib)


def test_layered_existing_pending_stays_pending(tmp_path, monkeypatch, db):
    """已 pending 的老条目再次扫描 → 保持 pending（不被意外改动）。"""
    movie = tmp_path / "Pending Movie (2022).mp4"
    movie.write_bytes(b"\x00" * 64)

    lib = em.Library(guid=_guid(), name="pending保持测试库",
                     collection_type="movies", paths=str(tmp_path))
    db.add(lib)
    db.commit()
    try:
        _run_scan(monkeypatch, tmp_path, db, lib)
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "pending"

        # mtime 变化再扫一次
        new_mtime = time.time() + 7200
        os.utime(movie, (new_mtime, new_mtime))
        _run_scan(monkeypatch, tmp_path, db, lib)
        db.expire_all()
        row = db.query(em.MediaItem).filter(
            em.MediaItem.library_id == lib.id).one()
        assert row.enrich_status == "pending", \
            f"pending 条目应保持 pending，实际 {row.enrich_status}"
    finally:
        _cleanup(db, lib)

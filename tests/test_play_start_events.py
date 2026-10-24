"""起播事件打点（2026-10-24，FMBY 学习批一 #2 / 魂 6 状态集中维护、魂 3 调度与数据面分离）。

口径钉死：
- 起播事件只经 play_events.note_play_start 入队、后台批量落库到
  emby_play_start_events；播放入口（_play_target 解析收口）负责记解析段。
- **打点永不拖慢/打断起播**：写库抛错、note 本身抛错、队列满、开关关，
  起播都照常走完（本文件的核心钉）。
- 聚合口径：按 (provider, mount_id) 分组给 total/errors/avg/max 解析耗时，
  挂载行补挂载名与类型；错误另按 error_kind 分布。

打点状态是进程全局的：本文件 fixture 在每个用例前后都 reset（照
test_provider_breaker 的熔断隔离先例）。
"""
import queue
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import api as emby_api
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import play_events


@pytest.fixture(autouse=True)
def _isolate_play_events():
    play_events.reset_for_tests()
    yield
    play_events.reset_for_tests()


@pytest.fixture()
def db_factory(monkeypatch):
    """内存 SQLite + 把 play_events 落库的 SessionLocal 指向它。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", factory)
    return factory


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _mount_item(**overrides):
    item = SimpleNamespace(
        id=42, file_path="mount://7/电影/x.mkv", drive_file_id="",
        library=None)
    for key, value in overrides.items():
        setattr(item, key, value)
    return item


def _events(db):
    return db.query(em.PlayStartEvent).order_by(em.PlayStartEvent.id).all()


# ---------------------------------------------------------------- 入队与落库


def test_note_and_flush_writes_row(db, db_factory):
    play_events.note_play_start(
        provider="drive", method="relay", user_id=9, item_id=42,
        result="ok", resolve_ms=123)
    assert play_events.flush_pending() == 1
    rows = _events(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "drive"
    assert row.method == "relay"
    assert row.user_id == 9
    assert row.item_id == 42
    assert row.result == "ok"
    assert row.error_kind is None
    assert row.resolve_ms == 123
    assert play_events.play_event_stats()["written"] == 1


def test_note_drops_when_disabled(monkeypatch, db):
    monkeypatch.setattr(play_events, "_ENABLED", False)
    play_events.note_play_start(provider="drive", method="relay")
    assert play_events.flush_pending() == 0
    assert _events(db) == []
    assert play_events.play_event_stats()["dropped"] == 1


def test_queue_full_drops_silently(monkeypatch):
    monkeypatch.setattr(play_events, "_QUEUE", queue.Queue(maxsize=1))
    play_events.note_play_start(provider="drive")
    play_events.note_play_start(provider="drive")  # 队列已满：静默丢弃
    stats = play_events.play_event_stats()
    assert stats["noted"] == 1
    assert stats["dropped"] == 1


# ------------------------------------------------------- 解析收口（_play_target）


def test_play_target_records_ok_event(monkeypatch, db):
    target = mount_lib.PlayTarget("url", "http://example.invalid/x.mkv", {})
    monkeypatch.setattr(mount_lib, "resolve_play_target",
                        lambda path, db_, library=None: target)
    got = emby_api._play_target(db, _mount_item())
    assert got is target
    play_events.flush_pending()
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].provider == "mount"
    assert rows[0].mount_id == 7
    assert rows[0].method == "resolve"
    assert rows[0].result == "ok"
    # _play_target 签名冻结（多处桩替身共用），解析事件不带 user_id
    assert rows[0].user_id is None
    assert rows[0].resolve_ms is not None


def test_play_target_records_mount_error_and_reraises(monkeypatch, db):
    def _boom(path, db_, library=None):
        raise mount_lib.MountError("挂载已停用")

    monkeypatch.setattr(mount_lib, "resolve_play_target", _boom)
    with pytest.raises(HTTPException) as excinfo:
        emby_api._play_target(db, _mount_item())
    assert excinfo.value.status_code == 404  # 原语义不变
    play_events.flush_pending()
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].result == "error"
    assert rows[0].error_kind == "mount_unavailable"


def test_play_target_records_auth_error_and_reraises(monkeypatch, db):
    def _boom(path, db_, library=None):
        raise mount_lib.MountAuthError("Cookie 失效")

    monkeypatch.setattr(mount_lib, "resolve_play_target", _boom)
    with pytest.raises(HTTPException) as excinfo:
        emby_api._play_target(db, _mount_item())
    assert excinfo.value.status_code == 503  # 原语义不变
    play_events.flush_pending()
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0].result == "error"
    assert rows[0].error_kind == "auth"


# -------------------------------------- 必钉：打点失败绝不影响起播


def test_write_failure_never_breaks_playback(monkeypatch, db):
    """落库整批抛错时：flush 不向上抛（写失败计数），起播照常返回。"""
    def _broken_factory():
        raise RuntimeError("数据库挂了")

    monkeypatch.setattr("backend.database.SessionLocal", _broken_factory)
    target = mount_lib.PlayTarget("url", "http://example.invalid/x.mkv", {})
    monkeypatch.setattr(mount_lib, "resolve_play_target",
                        lambda path, db_, library=None: target)
    got = emby_api._play_target(db, _mount_item())
    assert got is target                       # 起播不受影响
    assert play_events.flush_pending() == 1    # 取出 1 条，写失败被吞
    stats = play_events.play_event_stats()
    assert stats["write_errors"] == 1
    assert stats["written"] == 0
    assert stats["dropped"] >= 1


def test_note_exception_never_breaks_playback(monkeypatch, db):
    """连 note_play_start 本身抛错（被换成坏桩）时，起播也照常走完——
    钩子调用点的安全口（_note_play_start_safe）必须把异常吃掉。"""
    def _bad_note(**kwargs):
        raise RuntimeError("打点炸了")

    monkeypatch.setattr(play_events, "note_play_start", _bad_note)
    target = mount_lib.PlayTarget("url", "http://example.invalid/x.mkv", {})
    monkeypatch.setattr(mount_lib, "resolve_play_target",
                        lambda path, db_, library=None: target)
    got = emby_api._play_target(db, _mount_item())
    assert got is target


# ---------------------------------------------------------------- 聚合口径


def test_aggregate_groups_by_provider_and_error_kind(db):
    mount = em.StorageMount(name="115主号", mount_type="115", path="115:/")
    db.add(mount)
    db.commit()
    db.refresh(mount)
    db.add_all([
        em.PlayStartEvent(provider="drive", method="relay", result="ok",
                          resolve_ms=100),
        em.PlayStartEvent(provider="drive", method="direct_302", result="ok",
                          resolve_ms=300),
        em.PlayStartEvent(provider="drive", method="relay", result="error",
                          error_kind="upstream_403", resolve_ms=50),
        em.PlayStartEvent(provider="mount", mount_id=mount.id,
                          method="resolve", result="error",
                          error_kind="auth", resolve_ms=20),
        em.PlayStartEvent(provider="mount", mount_id=mount.id,
                          method="resolve", result="ok", resolve_ms=80),
    ])
    db.commit()

    report = play_events.aggregate_play_start_events(db, hours=24)
    assert report["window_hours"] == 24
    by_key = {(row["provider"], row["mount_id"]): row
              for row in report["by_provider"]}
    drive = by_key[("drive", None)]
    assert drive["total"] == 3
    assert drive["errors"] == 1
    assert drive["avg_resolve_ms"] == 150.0
    assert drive["max_resolve_ms"] == 300
    mount_row = by_key[("mount", mount.id)]
    assert mount_row["total"] == 2
    assert mount_row["errors"] == 1
    assert mount_row["mount_name"] == "115主号"
    assert mount_row["mount_type"] == "115"
    kinds = {row["error_kind"]: row["total"] for row in report["by_error_kind"]}
    assert kinds == {"upstream_403": 1, "auth": 1}


def test_reset_clears_state():
    play_events.note_play_start(provider="drive")
    play_events.reset_for_tests()
    stats = play_events.play_event_stats()
    assert stats["noted"] == 0
    assert stats["queued"] == 0

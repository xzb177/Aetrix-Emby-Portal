"""数据库定时备份：时间校验 / 配置读写 / 备份+轮转"""
import gzip
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, "/opt/aetrix-portal")

from backend.emby_server import db_backup
from backend import models as base_models


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def fake_db_file(tmp_path, monkeypatch):
    """一个真实的 sqlite 文件 + 劫持 get_db_file_path 指向它。"""
    db_file = tmp_path / "aetrix_unified.db"
    con = sqlite3.connect(str(db_file))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('hello')")
    con.commit()
    con.close()
    monkeypatch.setattr(db_backup, "get_db_file_path", lambda: db_file)
    return db_file


def test_parse_time_ok():
    assert db_backup.parse_time("03:00") == "03:00"
    assert db_backup.parse_time(" 23:59 ") == "23:59"


def test_parse_time_bad():
    for bad in ("3:00", "24:00", "03:60", "", None, "abc", "03-00"):
        assert db_backup.parse_time(bad) is None


def test_config_defaults(db):
    cfg = db_backup.get_config(db)
    assert cfg["enabled"] is True          # 开箱即有
    assert cfg["time"] == "03:00"
    assert cfg["keep_days"] == 7
    assert cfg["last_run"] == ""
    assert cfg["backups"] == []


def test_save_and_get(db):
    cfg = db_backup.save_config(db, True, "04:30", 10)
    assert cfg["enabled"] is True
    assert cfg["time"] == "04:30"
    assert cfg["keep_days"] == 10
    cfg2 = db_backup.get_config(db)
    assert cfg2["enabled"] is True and cfg2["time"] == "04:30" and cfg2["keep_days"] == 10


def test_save_bad_time_raises(db):
    with pytest.raises(ValueError):
        db_backup.save_config(db, True, "25:00", 7)


@pytest.mark.parametrize("bad_days", [0, 31, -1])
def test_save_bad_keep_days_raises(db, bad_days):
    with pytest.raises(ValueError):
        db_backup.save_config(db, True, "03:00", bad_days)


def test_should_run_today():
    now = datetime(2026, 9, 29, 3, 0)
    assert db_backup._should_run_today(now, "03:00", "") is True
    assert db_backup._should_run_today(now, "03:00", "2026-09-29") is False
    assert db_backup._should_run_today(now, "04:00", "") is False
    assert db_backup._should_run_today(datetime(2026, 9, 29, 3, 1), "03:00", "") is True  # 过点未跑自动补跑（>= 语义）


def test_run_backup_now_creates_gzipped_backup(fake_db_file):
    result = db_backup.run_backup_now(reason="test")
    p = Path(result["path"])
    assert p.exists()
    assert p.name.endswith(".db.gz")
    assert result["size"] > 0
    # 解压后是有效 sqlite 且数据一致
    raw = p.with_suffix("").with_suffix(".db")  # 去掉 .gz
    with gzip.open(p, "rb") as f_in, open(raw, "wb") as f_out:
        f_out.write(f_in.read())
    con = sqlite3.connect(str(raw))
    try:
        rows = con.execute("SELECT v FROM t").fetchall()
    finally:
        con.close()
    assert rows == [("hello",)]
    assert db_backup.get_backup_dir() == fake_db_file.parent / "backups"


def test_run_backup_now_no_db(monkeypatch):
    monkeypatch.setattr(db_backup, "get_db_file_path", lambda: None)
    with pytest.raises(RuntimeError):
        db_backup.run_backup_now()


def test_prune_old_backups(fake_db_file):
    backup_dir = db_backup.get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    old = backup_dir / "aetrix_unified-20200101-000000.db.gz"
    new = backup_dir / "aetrix_unified-20990101-000000.db.gz"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    old_time = time.time() - 10 * 86400  # 10 天前
    os.utime(old, (old_time, old_time))
    removed = db_backup.prune_old_backups(keep_days=7)
    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_list_backups_sorted(fake_db_file):
    backup_dir = db_backup.get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in ("aetrix_unified-20260101-000000.db.gz", "aetrix_unified-20260201-000000.db.gz"):
        (backup_dir / name).write_bytes(b"z")
    rows = db_backup.list_backups()
    assert [r["name"] for r in rows] == [
        "aetrix_unified-20260201-000000.db.gz",
        "aetrix_unified-20260101-000000.db.gz",
    ]
    assert all(r["size"] == 1 and r["created_at"] for r in rows)


def test_tick_runs_once_per_day(db, fake_db_file, monkeypatch):
    """daemon _tick：到点跑一次，同一天第二次不再跑。"""
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 29, 3, 0)  # 03:00，正好到点
    monkeypatch.setattr(db_backup, "datetime", FakeDateTime)
    monkeypatch.setattr(db_backup, "SessionLocal", lambda: db)
    calls = []
    monkeypatch.setattr(db_backup, "run_backup_now",
                        lambda reason="": calls.append(reason) or {"name": "x"})
    db_backup._tick()
    db_backup._tick()
    assert calls == ["scheduled"]  # 第二次被 last_run 挡住
    cfg = db_backup.get_config(db)
    assert cfg["last_run"] == "2026-09-29"


def test_tick_disabled_skips(db, fake_db_file, monkeypatch):
    # 2026-10-21 A 类摘面板：save_config 不再写开关，关备份直接种存量行
    from backend import models as _models
    from backend.integrations import store as _store
    db.add(_models.SystemConfig(key=db_backup.CONFIG_ENABLED, value="0"))
    db.commit()
    _store.invalidate(db_backup.CONFIG_ENABLED)
    monkeypatch.setattr(db_backup, "SessionLocal", lambda: db)
    called = []
    monkeypatch.setattr(db_backup, "run_backup_now",
                        lambda reason="": called.append(1) or {"name": "x"})
    db_backup._tick()
    assert called == []


def test_backup_dirs_pg_mode_includes_pg_dir(tmp_path, monkeypatch, fake_db_file):
    # 目的：PG 模式下 _backup_dirs() 必须包含 PG 备份目录（备份持久化卷的挂载点）。
    pg_dir = tmp_path / "pg_backups"
    pg_dir.mkdir(parents=True, exist_ok=True)

    sqlite_dir = db_backup.get_backup_dir()
    sqlite_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("PG_BACKUP_DIR", str(pg_dir))
    monkeypatch.setattr(db_backup, "_is_postgres", lambda: True)

    dirs = {p.resolve() for p in db_backup._backup_dirs()}
    assert pg_dir.resolve() in dirs
    assert sqlite_dir.resolve() in dirs


def test_prune_old_backups_removes_expired_pg_dumps(tmp_path, monkeypatch):
    # 目的：prune_old_backups 能删掉过期的 .dump.gz（PG 分支）。
    pg_dir = tmp_path / "pg_backups"
    pg_dir.mkdir(parents=True, exist_ok=True)

    old = pg_dir / "aetrix-20200101-000000.dump.gz"
    new = pg_dir / "aetrix-20990101-000000.dump.gz"
    old.write_bytes(b"x")
    new.write_bytes(b"y")

    old_time = time.time() - 10 * 86400
    os.utime(old, (old_time, old_time))

    monkeypatch.setenv("PG_BACKUP_DIR", str(pg_dir))
    monkeypatch.setattr(db_backup, "_is_postgres", lambda: True)

    removed = db_backup.prune_old_backups(keep_days=7)
    assert removed == 1
    assert not old.exists()
    assert new.exists()

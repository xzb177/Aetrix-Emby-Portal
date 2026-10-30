"""备份下载 + 校验信息（GET /api/admin/system/backup/files/{name}/*）。

- find_backup 只认备份目录里真实存在的文件名：路径穿越/非法后缀/不存在
  一律 None（绝不拿请求参数拼路径）
- backup_info：sha256 与文件字节对账、gzip 全量解压校验、SQLite/PGDMP
  文件头判定、损坏 gzip 明示失败
- 下载端点写审计 AdminLog；不存在回 404
- 本单只做下载 + 校验展示，不做一键覆盖恢复（端点里没有恢复动作）
"""
import gzip
import hashlib
import os
import sqlite3
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.api import admin_services
from backend.emby_server import db_backup

ADMIN = SimpleNamespace(id=3)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def fake_db_file(tmp_path, monkeypatch):
    """一个真实的 sqlite 文件 + 劫持 get_db_file_path 指向它（同 test_db_backup）。"""
    db_file = tmp_path / "aetrix_unified.db"
    con = sqlite3.connect(str(db_file))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('hello')")
    con.commit()
    con.close()
    monkeypatch.setattr(db_backup, "get_db_file_path", lambda: db_file)
    return db_file


@pytest.fixture()
def one_backup(fake_db_file):
    result = db_backup.run_backup_now(reason="test")
    return result["name"]


# ---------- find_backup ----------

def test_find_backup_rejects_traversal_and_missing(one_backup):
    assert db_backup.find_backup(one_backup) is not None
    for bad in ("../aetrix_unified.db", "..\\x.db.gz", "/etc/passwd",
                "a/b.db.gz", "", ".", "..", "x.txt", "not-exist.db.gz"):
        assert db_backup.find_backup(bad) is None, bad


# ---------- backup_info ----------

def test_backup_info_sqlite_roundtrip(fake_db_file, one_backup):
    info = db_backup.backup_info(one_backup)
    assert info is not None
    assert info["format"] == "sqlite"
    assert info["header_ok"] is True
    assert info["gzip_ok"] is True
    assert info["uncompressed_size"] == fake_db_file.stat().st_size
    path = db_backup.find_backup(one_backup)
    assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "停服" in info["restore_hint"]


def test_backup_info_pg_dump_format(fake_db_file):
    backups_dir = fake_db_file.parent / "backups"
    backups_dir.mkdir(exist_ok=True)
    name = "aetrix-20990101-000000.dump.gz"
    with gzip.open(backups_dir / name, "wb") as fh:
        fh.write(b"PGDMP" + b"\x00" * 64)
    info = db_backup.backup_info(name)
    assert info["format"] == "pg_dump"
    assert info["header_ok"] is True
    assert info["gzip_ok"] is True


def test_backup_info_corrupt_gzip(fake_db_file):
    backups_dir = fake_db_file.parent / "backups"
    backups_dir.mkdir(exist_ok=True)
    name = "aetrix_unified-20990101-000000.db.gz"
    (backups_dir / name).write_bytes(b"this is not gzip at all")
    info = db_backup.backup_info(name)
    assert info["gzip_ok"] is False
    assert info["gzip_error"] != ""
    assert info["format"] == "unknown"
    assert info["header_ok"] is False
    assert info["uncompressed_size"] is None
    assert "不要" in info["restore_hint"]


def test_backup_info_missing_returns_none(fake_db_file):
    assert db_backup.backup_info("not-exist.db.gz") is None


# ---------- 端点 ----------

def test_download_endpoint_returns_file_and_audits(db, one_backup):
    resp = admin_services.download_backup_file(one_backup, admin=ADMIN, db=db)
    assert isinstance(resp, FileResponse)
    assert resp.filename == one_backup
    logs = db.query(base_models.AdminLog).all()
    assert len(logs) == 1
    assert logs[0].action == "backup.download"
    assert logs[0].admin_user_id == 3
    assert logs[0].details == {"name": one_backup}


def test_download_endpoint_404_and_no_audit(db, fake_db_file):
    with pytest.raises(HTTPException) as ei:
        admin_services.download_backup_file("../secret.db.gz", admin=ADMIN, db=db)
    assert ei.value.status_code == 404
    assert db.query(base_models.AdminLog).count() == 0


def test_info_endpoint(db, one_backup):
    res = admin_services.get_backup_file_info(one_backup, admin=ADMIN, db=db)
    assert res["success"] is True
    assert res["format"] == "sqlite"
    with pytest.raises(HTTPException) as ei:
        admin_services.get_backup_file_info("not-exist.db.gz", admin=ADMIN, db=db)
    assert ei.value.status_code == 404

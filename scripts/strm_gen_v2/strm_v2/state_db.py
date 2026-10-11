"""状态层：SQLite 持久化。

D-ARCH-2: 独立 SQLite 文件，不连生产 PostgreSQL。
理由：① 断点续跑需要事务性 checkpoint；② 批量任务远离生产库（压测教训）；
③ 脚本要在后端停机时独立运行。

表结构：
- files: file_id → md5/size/path/strm_path/status（上次同步状态）
- checkpoints: (shard_id, phase) → cursor（分片断点）
- meta: key → value（游标、版本号等）
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from .models import FileState

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id   TEXT PRIMARY KEY,
    path      TEXT NOT NULL,
    md5       TEXT,
    size      INTEGER NOT NULL,
    strm_path TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'done',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_path ON files(path);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);

CREATE TABLE IF NOT EXISTS checkpoints (
    shard_id   TEXT NOT NULL,
    phase      TEXT NOT NULL,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (shard_id, phase)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_VERSION = "2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateDB:
    """线程安全的 SQLite 状态库。"""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # check_same_thread=False + 锁：支持多线程并发写
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── files 表 ──
    def get(self, file_id: str) -> Optional[FileState]:
        with self._lock:
            row = self._conn.execute(
                "SELECT file_id, path, md5, size, strm_path, status, updated_at"
                " FROM files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        if not row:
            return None
        return FileState(*row)

    def get_by_path(self, path: str) -> Optional[FileState]:
        """按 Drive 路径查（用于 ORPHAN_STRM 检测）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT file_id, path, md5, size, strm_path, status, updated_at"
                " FROM files WHERE path = ?",
                (path,),
            ).fetchone()
        if not row:
            return None
        return FileState(*row)

    def upsert(self, state: FileState) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO files(file_id, path, md5, size, strm_path, status, updated_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(file_id) DO UPDATE SET"
                " path=excluded.path, md5=excluded.md5, size=excluded.size,"
                " strm_path=excluded.strm_path, status=excluded.status,"
                " updated_at=excluded.updated_at",
                (state.file_id, state.path, state.md5, state.size,
                 state.strm_path, state.status, _now()),
            )

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def delete(self, file_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))

    def mark_verify_failed(self, strm_path: str) -> None:
        """D-VERIFY-3: 校验失败打标（线程安全）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE files SET status='verify_failed' WHERE strm_path=?",
                (strm_path,))

    def all_file_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT file_id FROM files").fetchall()
        return {r[0] for r in rows}

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM files GROUP BY status"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ── checkpoints 表 (D-INCR-5: 分片 × 阶段 checkpoint) ──
    def save_checkpoint(self, shard_id: str, phase: str, cursor: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO checkpoints(shard_id, phase, cursor, updated_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(shard_id, phase) DO UPDATE SET"
                " cursor=excluded.cursor, updated_at=excluded.updated_at",
                (shard_id, phase, cursor, _now()),
            )
            self._conn.commit()

    def load_checkpoint(self, shard_id: str, phase: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM checkpoints WHERE shard_id=? AND phase=?",
                (shard_id, phase),
            ).fetchone()
        return row[0] if row else None

    # ── meta 表 ──
    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    # ── v1 迁移 ──
    def migrate_from_v1(self, v1_path: str) -> int:
        """从 v1 的 processed 表导入。返回导入条数。

        v1 表结构：processed(remote_path, file_id, size, strm_path, updated_at)
        v1 没有 md5，导入后 md5=NULL，首次 diff 时会触发一次 md5 回填（标记为更新）。
        """
        if not os.path.exists(v1_path):
            return 0
        # 已迁移过则跳过
        if self.get_meta("v1_migrated") == "1":
            return 0
        src = sqlite3.connect(v1_path)
        try:
            rows = src.execute(
                "SELECT remote_path, file_id, size, strm_path FROM processed"
            ).fetchall()
        finally:
            src.close()
        count = 0
        with self._lock:
            for remote_path, file_id, size, strm_path in rows:
                self._conn.execute(
                    "INSERT OR IGNORE INTO files"
                    "(file_id, path, md5, size, strm_path, status, updated_at)"
                    " VALUES(?,?,?,?,?,'done',?)",
                    (file_id, remote_path, None, size, strm_path, _now()),
                )
                count += 1
            self._conn.commit()
        self.set_meta("v1_migrated", "1")
        return count

"""第 14 轮完美主义迭代回归测试（P1-1 / P2-2 / P3-2~P3-6；
P2-1/P3-1 跳转缓存已删、P3-5 跳转跟随已随挂载中继删除）。

实现者编写（workbuddy 网关本轮可用，但改动均为片段级精确修复，
人工编写 + 逐行核验）。
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models


# ---------------------------------------------------------------------------
# P1-1：update_admin 角色变更路径按"可登录 super"口径计数
# ---------------------------------------------------------------------------

def _make_admin_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/r14_admins.db", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine, checkfirst=True)
    return sessionmaker(bind=engine)


def _mk_admin(db, username, role, is_active):
    u = models.WebUser(username=username, email=f"{username}@x.com",
                       password_hash="x", is_staff=True,
                       admin_role=role, is_active=is_active)
    db.add(u)
    db.flush()
    return u


def _dummy_request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


def test_p1_1_demotion_blocked_when_last_loginable_super(tmp_path):
    """死局场景：活跃 super A + 已停用 super B，降级 A 必须被 400 拦截。

    只看角色的计数会放行（角色计数=2），之后可登录 super 归零，
    后台再无账号能登录恢复，只能直连数据库手工改。

    说明：经 API 调用时调用方必为活跃 super（get_current_admin 门控），
    字面死局需调用方门控被绕过才可达；这里直接调端点函数、用 operator
    调用方模拟门控被绕过的情形——端点自身的护栏必须独立成立
    （纵深防御；且与停用/撤销路径的"可登录 super"口径对称）。
    """
    from backend.api import admins_admin
    Session = _make_admin_session(tmp_path)
    db = Session()
    try:
        a = _mk_admin(db, "superA", "super", True)
        _mk_admin(db, "superB", "super", False)  # 已停用
        caller = _mk_admin(db, "opC", "operator", True)
        db.commit()

        with pytest.raises(HTTPException) as ei:
            admins_admin.update_admin(
                a.id, admins_admin.UpdateAdminRequest(role="operator"),
                _dummy_request(), caller, db)
        assert ei.value.status_code == 400
        assert "可登录的超级管理员" in ei.value.detail
        db.refresh(a)
        assert a.admin_role == "super"  # 没被降级
    finally:
        db.close()


def test_p1_1_demotion_of_inactive_super_allowed(tmp_path):
    """目标账号本身已停用时，降级不影响可登录数，不拦截（与停用路径语义对称）。"""
    from backend.api import admins_admin
    Session = _make_admin_session(tmp_path)
    db = Session()
    try:
        _mk_admin(db, "superA", "super", True)
        b = _mk_admin(db, "superB", "super", False)  # 已停用
        caller = _mk_admin(db, "superC", "super", True)
        db.commit()

        result = admins_admin.update_admin(
            b.id, admins_admin.UpdateAdminRequest(role="operator"),
            _dummy_request(), caller, db)
        assert result["success"] is True
        assert result["changed"]["role"] == "operator"
    finally:
        db.close()


def test_p1_1_demotion_allowed_when_two_loginable(tmp_path):
    """有两个可登录 super 时，降级其中一个放行（不误伤正常操作）。"""
    from backend.api import admins_admin
    Session = _make_admin_session(tmp_path)
    db = Session()
    try:
        a = _mk_admin(db, "superA", "super", True)
        _mk_admin(db, "superD", "super", True)
        caller = _mk_admin(db, "superC", "super", True)
        db.commit()

        result = admins_admin.update_admin(
            a.id, admins_admin.UpdateAdminRequest(role="operator"),
            _dummy_request(), caller, db)
        assert result["success"] is True
        assert result["changed"]["role"] == "operator"
    finally:
        db.close()



# ---------------------------------------------------------------------------
# P2-2：PG 备份目录轮转 / 列表 / 残留清扫
# ---------------------------------------------------------------------------

@pytest.fixture()
def pg_backup_env(tmp_path, monkeypatch):
    """伪装 PG 部署：_is_postgres=True，PG_BACKUP_DIR=tmp 目录，sqlite 目录置空。"""
    from backend.emby_server import db_backup
    monkeypatch.setattr(db_backup, "_is_postgres", lambda: True)
    monkeypatch.setattr(db_backup, "get_backup_dir", lambda: None)
    pg_dir = tmp_path / "pgbackups"
    pg_dir.mkdir()
    monkeypatch.setenv("PG_BACKUP_DIR", str(pg_dir))
    return pg_dir


def _touch(path, mtime):
    path.write_bytes(b"x" * 100)
    os.utime(path, (mtime, mtime))


def test_p2_2_prune_rotates_pg_dumps(pg_backup_env):
    """PG 目录的 *.dump.gz 按 keep_days 轮转（之前 prune 直接 return 0）。"""
    from backend.emby_server import db_backup
    now = time.time()
    old = pg_backup_env / "aetrix-20200101-000000.dump.gz"
    new = pg_backup_env / f"aetrix-{datetime.now():%Y%m%d-%H%M%S}.dump.gz"
    _touch(old, now - 20 * 86400)
    _touch(new, now)
    removed = db_backup.prune_old_backups(keep_days=7)
    assert removed == 1
    assert not old.exists() and new.exists()


def test_p2_2_sweep_cleans_pg_part_files(pg_backup_env):
    """PG 分支崩溃残留的 .part-*.dump.gz 被清扫（之前 sweep 只认 sqlite 目录）。"""
    from backend.emby_server import db_backup
    now = time.time()
    part = pg_backup_env / ".part-123-abcdef.dump.gz"
    _touch(part, now - 7200)          # 超过 1 小时 → 删
    fresh = pg_backup_env / ".part-999-abcdef.dump.gz"
    _touch(fresh, now)                # 太新 → 保留
    removed = db_backup.sweep_stale_temp_files()
    assert removed == 1
    assert not part.exists() and fresh.exists()


def test_p2_2_list_backups_sees_pg_dumps(pg_backup_env):
    """PG 目录的 *.dump.gz 出现在列表里（之前恒为 []）。"""
    from backend.emby_server import db_backup
    f = pg_backup_env / "aetrix-20260101-120000.dump.gz"
    _touch(f, time.time())
    names = [r["name"] for r in db_backup.list_backups()]
    assert f.name in names


def test_p2_2_backup_dirs_includes_both(tmp_path, monkeypatch):
    """_backup_dirs 双目录口径：sqlite 目录 + PG 目录都纳入。"""
    from backend.emby_server import db_backup
    sqlite_dir = tmp_path / "backups"
    sqlite_dir.mkdir()
    pg_dir = tmp_path / "pg"
    pg_dir.mkdir()
    monkeypatch.setattr(db_backup, "get_backup_dir", lambda: sqlite_dir)
    monkeypatch.setattr(db_backup, "_is_postgres", lambda: True)
    monkeypatch.setenv("PG_BACKUP_DIR", str(pg_dir))
    dirs = db_backup._backup_dirs()
    assert sqlite_dir in dirs and pg_dir in dirs


# ---------------------------------------------------------------------------
# P3-4：premint 持锁写回前复查
# ---------------------------------------------------------------------------

def test_p3_4_premint_writeback_recheck_discards_on_race(monkeypatch):
    """启动瞬间与 get_drive_bearer_token 并发：对方已写入有效 token 时，
    本次换 token 结果被丢弃，不覆盖。"""
    import backend.emby_server.drive_auth as da
    email = "race-sa@r14.test"
    monkeypatch.setattr(da, "_all_sa_dicts", lambda: [{"client_email": email}])

    def fake_mint(sa, min_remaining):
        # 模拟并发：换 token 期间别人已写入有效 token
        da._token_cache[email] = ("RACETOKEN", time.monotonic() + 9999)
        return "NEWTOKEN"

    monkeypatch.setattr(da, "_load_drive_changes",
                        lambda: SimpleNamespace(_sa_access_token=fake_mint))
    saved = dict(da._token_cache)
    da._token_cache.clear()
    try:
        result = da.premint_tokens(max_workers=1)
        assert result["ok"] == 1
        assert da._token_cache[email][0] == "RACETOKEN"
    finally:
        da._token_cache.clear()
        da._token_cache.update(saved)


# ---------------------------------------------------------------------------
# P3-6：audit_extra 序列化守卫
# ---------------------------------------------------------------------------

def test_p3_6_json_safe_guard():
    from backend.emby_server.audit import _json_safe
    # 可序列化的原样返回
    assert _json_safe({"a": 1}, "k", "/p") == {"a": 1}
    assert _json_safe("x", "k", "/p") == "x"
    assert _json_safe([1, 2], "k", "/p") == [1, 2]
    # datetime 降级为 repr（含日期信息，不丢）
    r = _json_safe(datetime(2026, 1, 1, 12, 0, 0), "when", "/p")
    assert isinstance(r, str) and "2026, 1, 1" in r
    # 任意对象降级为 repr
    r2 = _json_safe(object(), "k", "/p")
    assert isinstance(r2, str) and "object" in r2

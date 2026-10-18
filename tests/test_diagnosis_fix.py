# -*- coding: utf-8 -*-
"""一键体检 · 自动修复（阶段二）测试。

覆盖：
1. 白名单：只有 disk_space / probe_queue / workers_alive 可修复；
   db_health 等 6 条危险规则不在白名单，只能手动
2. 安全修复成功路径：disk_space 清过期字幕缓存（tmp 目录实测端到端；
   2026-10-17 转码删除后，遗留转码目录清理已随转码移除）
3. 危险操作被拒绝：db_health → 400 not_fixable（附手动步骤）
4. 未知规则被拒绝：400 unknown_rule
5. 二次确认缺失被拒绝：confirm=False/缺省 → 400 confirm_required
6. 修复失败不抛异常：200 {"ok": false, "error": "fix_failed"}
7. 每次修复都写审计日志：成功 / 失败 / 被拒绝都有 admin_logs 行
8. 阈值可配：DIAG_FIX_* 环境变量覆盖默认值
9. workers 修复：死线程有 restart 回调 → 重启并报告；
   没有回调 → 不动，如实报告 not_restartable
"""
import os
import threading
import uuid

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException

from backend import diagnosis, diagnosis_fix
from backend import models
from backend.api import admin_diagnosis
from backend.api.admin_diagnosis import DiagnosisFixRequest
from backend.database import SessionLocal, init_db

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        try:
            s.rollback()
        except Exception:  # noqa: BLE001
            pass
        s.close()


_CREATED_USER_IDS = []


def _staff(db, username=None):
    username = username or f"diagfix_{uuid.uuid4().hex[:8]}"
    u = models.WebUser(username=username, password_hash="x",
                       is_staff=True, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    _CREATED_USER_IDS.append(u.id)
    return u


@pytest.fixture()
def admin(db):
    u = _staff(db)
    uid = int(u.id)  # 先取成纯值：删行后 ORM 属性会过期，不能再读 u.id
    yield u
    db.query(models.AdminLog).filter(
        models.AdminLog.admin_user_id == uid).delete()
    db.query(models.WebUser).filter(models.WebUser.id == uid).delete(
        synchronize_session=False)
    db.commit()
    _CREATED_USER_IDS.remove(uid)


def _last_audit(db, admin):
    return (db.query(models.AdminLog)
            .filter(models.AdminLog.action == "diagnosis.fix",
                    models.AdminLog.admin_user_id == admin.id)
            .order_by(models.AdminLog.id.desc()).first())


def _expect_reject(db, admin, body, error):
    with pytest.raises(HTTPException) as ei:
        admin_diagnosis.fix_diagnosis_rule(body, current_admin=admin, db=db)
    assert ei.value.status_code == 400
    assert ei.value.detail["error"] == error
    assert ei.value.detail["ok"] is False
    return ei.value.detail


# ---------------------------------------------------------------- 1. 白名单

def test_whitelist_only_safe_rules():
    assert set(diagnosis_fix.FIXES) == {"disk_space", "probe_queue",
                                        "workers_alive"}
    dangerous = {"db_health", "probe_breakers", "enrich_stall",
                 "scan_failed", "sa_health", "api_errors"}
    assert not (set(diagnosis_fix.FIXES) & dangerous)
    # 9 条规则全覆盖：3 可修 + 6 只手动，不重不漏
    assert set(diagnosis_fix.FIXES) | set(diagnosis_fix.MANUAL_ONLY) == \
        {r["rule_id"] for r in diagnosis.RULES}
    assert not (set(diagnosis_fix.FIXES) & set(diagnosis_fix.MANUAL_ONLY))
    for rid, spec in diagnosis_fix.FIXES.items():
        assert spec.confirm_text and spec.description  # 二次确认文案必填


# ---------------------------------------------------------------- 2. 安全修复成功路径（端到端）

def test_disk_space_fix_prunes_subtitle_cache_end_to_end(db, admin, tmp_path,
                                                          monkeypatch):
    """disk_space 只删可再生的过期字幕缓存（2026-10-17 转码删除后，
    遗留转码目录清理已随转码移除，不再有 removed_transcode_dirs）。"""
    from backend.emby_server import maintenance, subtitles

    # 造三个"过期"字幕缓存文件（mtime 设为 30 天前，必超 SUB_CACHE_MAX_DAYS）
    import time
    old = time.time() - 30 * 86400
    for name in ("sub_a.vtt", "sub_b.vtt", "sub_c.vtt"):
        p = tmp_path / name
        p.write_bytes(b"WEBVTT\n")
        os.utime(p, (old, old))
    (tmp_path / "fresh.vtt").write_bytes(b"WEBVTT\n")  # 新文件不动

    monkeypatch.setattr(subtitles, "SUB_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(maintenance, "SUB_CACHE_MAX_DAYS", 7.0)
    monkeypatch.setattr(maintenance, "SUB_CACHE_MAX_FILES", 10_000)
    monkeypatch.setenv("DIAG_FIX_SUBTITLE_CACHE", "1")

    out = admin_diagnosis.fix_diagnosis_rule(
        DiagnosisFixRequest(rule_id="disk_space", confirm=True),
        current_admin=admin, db=db)

    assert out["ok"] is True
    assert out["rule_id"] == "disk_space"
    assert out["result"]["removed_subtitle_files"] == 3
    assert "removed_transcode_dirs" not in out["result"]
    assert not (tmp_path / "sub_a.vtt").exists()
    assert (tmp_path / "fresh.vtt").exists()  # 没过期的没动

    audit = _last_audit(db, admin)
    assert audit is not None
    assert audit.details["rule_id"] == "disk_space"
    assert audit.details["ok"] is True


# ---------------------------------------------------------------- 3/4/5. 拒绝路径

def test_dangerous_rule_rejected_with_manual_steps(db, admin):
    detail = _expect_reject(
        db, admin,
        DiagnosisFixRequest(rule_id="db_health", confirm=True),
        "not_fixable")
    assert detail["manual_steps"]  # 给了手动步骤
    assert detail["reason"]
    audit = _last_audit(db, admin)
    assert audit is not None
    assert audit.details["error"] == "not_fixable"
    assert audit.details["ok"] is False


def test_unknown_rule_rejected(db, admin):
    _expect_reject(db, admin,
                   DiagnosisFixRequest(rule_id="no_such_rule", confirm=True),
                   "unknown_rule")
    assert _last_audit(db, admin).details["error"] == "unknown_rule"


@pytest.mark.parametrize("body", [
    DiagnosisFixRequest(rule_id="disk_space"),                    # 缺省 False
    DiagnosisFixRequest(rule_id="disk_space", confirm=False),      # 显式 False
])
def test_confirm_required(db, admin, body):
    _expect_reject(db, admin, body, "confirm_required")
    assert _last_audit(db, admin).details["error"] == "confirm_required"


# ---------------------------------------------------------------- 6. 修复失败不抛，结构化返回

def test_fix_failure_returns_structured_error(db, admin, monkeypatch):
    from backend.emby_server import maintenance

    def boom():
        raise RuntimeError("磁盘只读，删不动")

    monkeypatch.setattr(maintenance, "prune_subtitle_cache", boom)

    out = admin_diagnosis.fix_diagnosis_rule(
        DiagnosisFixRequest(rule_id="disk_space", confirm=True),
        current_admin=admin, db=db)

    assert out["ok"] is False
    assert out["error"] == "fix_failed"
    assert "磁盘只读" in out["message"]
    audit = _last_audit(db, admin)
    assert audit.details["ok"] is False
    assert audit.details["error"] == "fix_failed"


# ---------------------------------------------------------------- 8. 阈值可配

def test_fix_thresholds_configurable(monkeypatch):
    # 2026-10-17 转码删除后，transcode_min_age_sec 阈值已随转码移除；
    # 现有阈值：subtitle_cache（1=清过期字幕缓存，0=不动）
    assert diagnosis_fix.get_fix_threshold("subtitle_cache") == 1.0
    monkeypatch.setenv("DIAG_FIX_SUBTITLE_CACHE", "0")
    assert diagnosis_fix.get_fix_threshold("subtitle_cache") == 0.0
    monkeypatch.setenv("DIAG_FIX_SUBTITLE_CACHE", "not-a-number")
    assert diagnosis_fix.get_fix_threshold("subtitle_cache") == 1.0
    with pytest.raises(KeyError):
        diagnosis_fix.get_fix_threshold("transcode_min_age_sec")


# ---------------------------------------------------------------- 9. workers 修复

def test_workers_fix_restarts_registered_and_reports_unregistered():
    from backend.emby_server import worker_registry

    restarted_marker = []

    def dead_thread():
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start()
        t.join()
        return t

    worker_registry.register("diagfix_dead_nocb", dead_thread())
    worker_registry.register("diagfix_dead_cb", dead_thread(),
                             restart=lambda: restarted_marker.append(1))
    try:
        res = diagnosis_fix._fix_workers_dead(None)
        assert "diagfix_dead_cb" in res["restarted"]
        assert restarted_marker == [1]
        # 没有 restart 回调的不动，如实报告
        assert "diagfix_dead_nocb" in res["not_restartable"]
        assert "重启了" in res["detail"]
    finally:
        worker_registry.unregister("diagfix_dead_nocb")
        worker_registry.unregister("diagfix_dead_cb")


def test_probe_fix_reclaims_only_stuck(db, monkeypatch):
    from backend.emby_server import probe_worker

    monkeypatch.setattr(probe_worker, "reclaim_stale",
                        lambda db, ttl_sec=None: 7)
    res = diagnosis_fix._fix_probe_stuck(db)
    assert res["reclaimed"] == 7
    assert "放回" in res["detail"]

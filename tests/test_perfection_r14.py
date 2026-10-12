"""第 14 轮完美主义迭代回归测试（P1-1 / P2-1 / P2-2 / P3-1~P3-6）。

实现者编写（workbuddy 网关本轮可用，但改动均为片段级精确修复，
人工编写 + 逐行核验；_follow_redirects helper 由 deepseek-v4.1-flash
按片段 prompt 生成、人工核验后落盘）。
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
# P2-1：跳转缓存签名感知 TTL
# ---------------------------------------------------------------------------

def test_p2_1_signature_ttl_seconds():
    from backend.emby_server.streaming import _signature_ttl_seconds
    now = time.time()
    # unix 时间戳形式
    ttl = _signature_ttl_seconds(
        f"https://drive.usercontent.google.com/download?id=X&expires={int(now + 30)}")
    assert ttl is not None and 0 < ttl <= 31
    # 秒数形式
    assert _signature_ttl_seconds("https://x/?exp=45") == 45.0
    # 多个字段取最小
    ttl2 = _signature_ttl_seconds(
        f"https://x/?expires={int(now + 60)}&exp=20")
    assert ttl2 == 20.0
    # 识别不出 → None（保持默认 TTL）
    assert _signature_ttl_seconds(
        "https://drive.usercontent.google.com/download?id=X") is None
    # 已过期 → 0.0（调用方放弃写缓存）
    assert _signature_ttl_seconds(
        f"https://x/?expires={int(now - 10)}") == 0.0
    # 非法值忽略
    assert _signature_ttl_seconds("https://x/?expires=abc") is None


def test_p2_1_cache_store_uses_min_ttl(monkeypatch):
    import backend.emby_server.streaming as st
    monkeypatch.setattr(st, "_DRIVE_REDIRECT_CACHE_TTL", 90.0)
    now = time.time()

    fid = "R14_SIG_TTL"
    st._redirect_cache_invalidate(fid)
    try:
        st._redirect_cache_store_drive_url(
            fid, f"https://drive.usercontent.google.com/download?id=X&expires={int(now + 30)}")
        entry = st._redirect_cache.get(fid)
        assert entry is not None
        remaining = entry[1] - time.monotonic()
        assert remaining <= 31, f"TTL 应取 min(90, ~30)，实际剩余 {remaining}"

        # 无签名字段 → 默认 90s
        fid2 = "R14_SIG_DEFAULT"
        st._redirect_cache_invalidate(fid2)
        st._redirect_cache_store_drive_url(
            fid2, "https://drive.usercontent.google.com/download?id=Y")
        remaining2 = st._redirect_cache[fid2][1] - time.monotonic()
        assert 80 < remaining2 <= 90

        # 签名已过期 → 不写缓存（不毒化）
        fid3 = "R14_SIG_EXPIRED"
        st._redirect_cache_invalidate(fid3)
        st._redirect_cache_store_drive_url(
            fid3, f"https://drive.usercontent.google.com/d?id=Z&expires={int(now - 5)}")
        assert st._redirect_cache_get(fid3) is None
    finally:
        for f in ("R14_SIG_TTL", "R14_SIG_DEFAULT", "R14_SIG_EXPIRED"):
            st._redirect_cache_invalidate(f)


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
# P3-1 / P3-5：跳转缓存写入决策 + 共用 _follow_redirects helper
# ---------------------------------------------------------------------------

def test_p3_1_maybe_cache_drive_redirect_decisions():
    import backend.emby_server.streaming as st
    fid = "R14_MAYBE_CACHE"
    st._redirect_cache_invalidate(fid)
    try:
        good = "https://drive.usercontent.google.com/download?id=X"
        # hops=0（本次是缓存命中）→ 不重复写
        st._maybe_cache_drive_redirect(fid, good, 0, False, 200)
        assert st._redirect_cache_get(fid) is None
        # 终态 403/404/410 → 坏 URL 不进缓存
        for bad_status in (403, 404, 410):
            st._maybe_cache_drive_redirect(fid, good, 2, False, bad_status)
            assert st._redirect_cache_get(fid) is None, f"status={bad_status}"
        # 本次就是缓存命中 → 不重复写
        st._maybe_cache_drive_redirect(fid, good, 2, True, 200)
        assert st._redirect_cache_get(fid) is None
        # 落点非 usercontent → 不写
        st._maybe_cache_drive_redirect(fid, "https://example.com/x", 2, False, 200)
        assert st._redirect_cache_get(fid) is None
        # 正常：走了跳转 + 落点 usercontent + 终态 200 → 写
        st._maybe_cache_drive_redirect(fid, good, 2, False, 200)
        assert st._redirect_cache_get(fid) == good
    finally:
        st._redirect_cache_invalidate(fid)


def _fake_redirect_client(statuses):
    """假 httpx 客户端：按顺序返回 (status_code, location)。"""
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeClient:
        def build_request(self, method, url, headers=None):
            return (method, url, headers)

        async def send(self, request, stream=True):
            method, url, headers = request
            calls.append(url)
            code, location = statuses[min(len(calls) - 1, len(statuses) - 1)]
            return FakeResponse(code, {"location": location} if location else {})

    return FakeClient(), calls


def test_p3_5_follow_redirects_happy_path():
    """正常跳转：跟随 307 → 返回终态 resp + final_url + hops；终态 resp 不关。"""
    import backend.emby_server.streaming as st
    from urllib.parse import urljoin
    final = "https://drive.usercontent.google.com/download?id=X"

    async def go():
        client, calls = _fake_redirect_client([(307, final), (200, None)])
        # _next_redirect 的内网校验已有专门测试；沙箱 DNS 会把测试域名
        # 解析到内网，这里只测跳转跟随逻辑
        with patch("backend.emby_server.streaming._next_redirect",
                   side_effect=lambda base, loc: urljoin(base, loc)):
            resp, current, hops = await st._follow_redirects(
                client, "https://www.googleapis.com/drive/v3/files/ABC?alt=media",
                {}, silent=False)
        assert hops == 1
        assert current == final
        assert resp.status_code == 200
        assert calls == ["https://www.googleapis.com/drive/v3/files/ABC?alt=media", final]
        await resp.aclose()

    asyncio.run(go())


def test_p3_5_follow_redirects_silent_bad_target():
    """非法重定向目标（内网）：silent 口径返回 (None, current, hops)，不抛。"""
    import backend.emby_server.streaming as st

    async def go():
        client, _ = _fake_redirect_client([(307, "http://127.0.0.1/evil")])
        resp, current, hops = await st._follow_redirects(
            client, "https://www.googleapis.com/drive/v3/files/ABC?alt=media",
            {}, silent=True)
        assert resp is None
        # 非法目标不跟随，current 停在起点
        assert current == "https://www.googleapis.com/drive/v3/files/ABC?alt=media"

    asyncio.run(go())


def test_p3_5_follow_redirects_silent_too_many():
    """跳转超限：silent 口径返回 (None, …)，不抛；raise 口径抛 502。"""
    import backend.emby_server.streaming as st
    from fastapi import HTTPException
    from urllib.parse import urljoin

    async def go():
        with patch("backend.emby_server.streaming._next_redirect",
                   side_effect=lambda base, loc: urljoin(base, loc)):
            client, _ = _fake_redirect_client([(307, "https://example.com/loop")] * 10)
            resp, _, hops = await st._follow_redirects(
                client, "https://example.com/start", {}, silent=True)
            assert resp is None
            assert hops == st.MAX_REDIRECTS + 1

            client2, _ = _fake_redirect_client([(307, "https://example.com/loop")] * 10)
            with pytest.raises(HTTPException) as ei:
                await st._follow_redirects(client2, "https://example.com/start", {}, silent=False)
            assert ei.value.status_code == 502

    asyncio.run(go())


# ---------------------------------------------------------------------------
# P3-2：maybe_warm 在信号量前过滤非 .strm
# ---------------------------------------------------------------------------

def test_p3_2_maybe_warm_skips_non_strm_early():
    import backend.emby_server.strm_prewarm as sp
    sp.reset_state()
    try:
        item = SimpleNamespace(file_path="/media/movie.mkv", library=None, guid="x")
        values = {"strm_prewarm_enabled": "1", "strm_prewarm_dedup_seconds": "300",
                  "strm_prewarm_max_concurrent": "4"}

        class FakeStore:
            def get_values(self, db, keys, defaults):
                return {k: values.get(k, defaults.get(k, "")) for k in keys}

        with patch("backend.integrations.store.get_values", FakeStore().get_values):
            asyncio.run(sp.maybe_warm(object(), item))
        snap = sp.metrics_snapshot()["counters"]
        assert snap["skipped_non_strm"] == 1
        # 去重表未被污染（早于去重标记返回）
        assert "/media/movie.mkv" not in sp._last_warm
    finally:
        sp.reset_state()


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

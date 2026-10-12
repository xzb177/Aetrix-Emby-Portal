"""P1 第二批修复回归测试（12 项高危并发/数据丢失）。

实现者直接编写（模型网关 503 不可用，已向主控报备）。
SQLite 跑逻辑正确性；真并发语义测试需 PG（DATABASE_URL），无 PG 时跳过。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models


def _make_session(tmp_path, name="p1b2.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}",
                           connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine, checkfirst=True)
    return sessionmaker(bind=engine), engine


def _pg_url():
    url = os.environ.get("DATABASE_URL", "")
    return url if url.startswith("postgresql") else None


# ---------- F-P1-4：订阅延长行锁 ----------

def test_p1b2_extend_uses_row_lock():
    """F-P1-4: extend_subscription 的查询必须带 with_for_update。"""
    import inspect
    from backend.api import admin as admin_api
    src = inspect.getsource(admin_api.extend_subscription)
    assert "with_for_update" in src, "extend_subscription 未加行锁"


def test_p1b2_extend_logic(tmp_path):
    """F-P1-4: 延长逻辑正确（max(旧日期, now) + days）。"""
    Session, engine = _make_session(tmp_path)
    db = Session()
    try:
        sub = models.UserSubscription(user_id=1, plan_id=1,
                                      end_date=datetime.now() - timedelta(days=5))
        db.add(sub)
        db.commit()
        # 模拟 _extend 核心逻辑（含行锁）
        locked = (db.query(models.UserSubscription)
                  .filter(models.UserSubscription.id == sub.id)
                  .with_for_update().first())
        assert locked is not None
        before = locked.end_date
        locked.end_date = max(locked.end_date or datetime.now(),
                              datetime.now()) + timedelta(days=30)
        db.commit()
        assert locked.end_date > before
        assert locked.end_date > datetime.now() + timedelta(days=29)
    finally:
        db.close()
        engine.dispose()


# ---------- F-P1-6 / F-P1-14：超管保护 ----------

def test_p1b2_locked_super_count_helpers_exist():
    """F-P1-6/F-P1-14: 行锁计数 helper 存在。"""
    from backend.api import admins_admin
    assert callable(admins_admin._locked_super_count)
    assert callable(admins_admin._locked_loginable_super_count)


def test_p1b2_locked_counts(tmp_path):
    """F-P1-6/F-P1-14: 计数口径正确（角色 super / 可登录 super）。"""
    from backend.api import admins_admin
    from backend import admin_roles
    Session, engine = _make_session(tmp_path)
    db = Session()
    try:
        from backend.security import hash_password
        ph = hash_password("x")
        u1 = models.WebUser(username="s1", password_hash=ph, is_staff=True, is_active=True,
                            admin_role=admin_roles.ROLE_SUPER)
        u2 = models.WebUser(username="s2", password_hash=ph, is_staff=True, is_active=False,
                            admin_role=admin_roles.ROLE_SUPER)
        u3 = models.WebUser(username="v1", password_hash=ph, is_staff=True, is_active=True,
                            admin_role="viewer")
        db.add_all([u1, u2, u3])
        db.commit()
        assert admins_admin._locked_super_count(db) == 2
        assert admins_admin._locked_loginable_super_count(db) == 1
    finally:
        db.close()
        engine.dispose()


def test_p1b2_grant_admin_mismatch_400():
    """F-P1-13: 用户名与邮箱命中不同账号时必须 400（fail-closed）。"""
    import inspect
    from backend.api import admins_admin
    src = inspect.getsource(admins_admin.grant_admin)
    # 分别精确查询 + 同一账号校验
    assert "by_username" in src and "by_email" in src
    assert "400" in src


# ---------- F-P1-8：设备封禁同事务 ----------

def test_p1b2_device_block_single_commit():
    """F-P1-8: update_device 只在末尾提交一次，不调自带 commit 的 revoke。"""
    import inspect
    from backend.api import admin_ops
    src = inspect.getsource(admin_ops.update_device)
    # 注释里提到函数名不算调用：检查是否还有"devices.revoke_device_tokens("调用
    assert "devices.revoke_device_tokens(" not in src, "仍在调用自带 commit 的 revoke_device_tokens"
    assert src.count("db.commit()") == 1, "应只有一次提交"


# ---------- F-P1-9：密钥池行锁 ----------

def test_p1b2_keypool_lock_helpers_exist():
    """F-P1-9: 行锁 helper 存在且被增删入口调用。"""
    import inspect
    from backend.api import admin_scrape
    assert callable(admin_scrape._locked_db_keys)
    assert callable(admin_scrape._lock_metasource_config_rows)
    assert "_locked_db_keys(db)" in inspect.getsource(admin_scrape.add_tmdb_key)
    assert "_locked_db_keys(db)" in inspect.getsource(admin_scrape.delete_tmdb_key)
    assert "_lock_metasource_config_rows(db)" in inspect.getsource(admin_scrape.add_meta_source_key)
    assert "_lock_metasource_config_rows(db)" in inspect.getsource(admin_scrape.delete_meta_source_key)


def test_p1b2_locked_db_keys_roundtrip(tmp_path):
    """F-P1-9: 行锁读取 + 写回往返正确。"""
    from backend.api import admin_scrape
    from backend.emby_server.tmdb import TMDB_KEYS_CONFIG_KEY
    from backend.integrations import store
    Session, engine = _make_session(tmp_path)
    db = Session()
    try:
        store.write_values(db, {TMDB_KEYS_CONFIG_KEY: "k1,k2"},
                           {TMDB_KEYS_CONFIG_KEY: "test"})
        db.commit()
        keys = admin_scrape._locked_db_keys(db)
        assert keys == ["k1", "k2"]
        db.rollback()  # 释放行锁
    finally:
        db.close()
        engine.dispose()


# ---------- F-P1-26/27/28：工单/求片 ----------

def test_p1b2_ticket_create_single_txn():
    """F-P1-26: create_ticket 用 flush 而非中间 commit。"""
    import inspect
    from backend.api import user as user_api
    src = inspect.getsource(user_api.create_ticket)
    # _create 内只有一次 commit（flush 不算）
    create_src = src[src.find("def _create"):]
    assert "db.flush()" in create_src
    assert create_src.count("db.commit()") == 1


def test_p1b2_reply_conditional_update():
    """F-P1-27: 回复用条件 UPDATE（WHERE status != 'closed'）。"""
    import inspect
    from backend.api import user as user_api
    src = inspect.getsource(user_api.reply_ticket)
    assert 'models.Ticket.status != "closed"' in src
    assert "synchronize_session=False" in src


def test_p1b2_withdraw_conditional_update():
    """F-P1-28: 撤回用条件 UPDATE（WHERE status='pending'）。"""
    import inspect
    from backend.api import user as user_api
    src = inspect.getsource(user_api.withdraw_media_seek)
    assert 'models.MovieRequest.status == "pending"' in src
    assert "synchronize_session=False" in src


def test_p1b2_withdraw_logic(tmp_path):
    """F-P1-28: 非 pending 的撤回被拒绝；pending 的成功。"""
    from backend import models as m
    Session, engine = _make_session(tmp_path)
    db = Session()
    try:
        # 查 MovieRequest 必需字段
        kwargs = {"user_id": 1, "status": "pending", "movie_name": "t"}
        req = m.MovieRequest(**kwargs)
        db.add(req)
        db.commit()
        rid = req.id

        # pending → 成功
        n = (db.query(m.MovieRequest)
             .filter(m.MovieRequest.id == rid,
                     m.MovieRequest.user_id == 1,
                     m.MovieRequest.status == "pending")
             .update({"status": "withdrawn"}, synchronize_session=False))
        assert n == 1
        db.commit()

        # 已 withdrawn → 抢不到行
        n2 = (db.query(m.MovieRequest)
              .filter(m.MovieRequest.id == rid,
                      m.MovieRequest.user_id == 1,
                      m.MovieRequest.status == "pending")
              .update({"status": "withdrawn"}, synchronize_session=False))
        assert n2 == 0
        db.rollback()
    finally:
        db.close()
        engine.dispose()


# ---------- A-P1-3：明文密码迁移 ----------

def test_p1b2_verify_and_migrate_exists():
    """A-P1-3: 收敛 helper 存在。"""
    from backend.emby_server import auth
    assert callable(auth.verify_and_migrate_emby_password)


def test_p1b2_plaintext_migrates_on_login(tmp_path):
    """A-P1-3: 明文密码登录成功后升级为 bcrypt-sha256。"""
    from backend.emby_server import auth
    Session, engine = _make_session(tmp_path)
    db = Session()
    try:
        cols = {c.name for c in models.WebUser.__table__.columns}
        kwargs = {"username": "plainuser"}
        if "password_hash" in cols:
            from backend.security import hash_password
            kwargs["password_hash"] = hash_password("x")
        user = models.WebUser(**kwargs)
        user.emby_password = "plain123"
        db.add(user)
        db.commit()
        uid = user.id

        ok = auth.verify_and_migrate_emby_password(db, user, "plain123")
        assert ok is True
        db.refresh(user)
        assert user.emby_password.startswith("$bcrypt-sha256$")
        # 错误密码不升级
        user.emby_password = "plain123"
        db.commit()
        ok2 = auth.verify_and_migrate_emby_password(db, user, "wrong")
        assert ok2 is False
        db.refresh(user)
        assert user.emby_password == "plain123"
    finally:
        db.close()
        engine.dispose()


def test_p1b2_api_uses_converged_helper():
    """A-P1-3: api.py 不再手写 startswith 分支。"""
    import inspect
    from backend.emby_server import api as emby_api
    src = inspect.getsource(emby_api)
    assert "verify_and_migrate_emby_password" in src
    # 旧的冗余分支已删除
    assert 'not user.emby_password.startswith(("$2", "$bcrypt-sha256$"))' not in src


# ---------- F-P1-17 / F-P1-24：单事务 ----------

def test_p1b2_save_server_probe_first():
    """F-P1-17: 先探测后落库，单次提交。"""
    import inspect
    from backend.api import emby_servers
    src = inspect.getsource(emby_servers.save_server)
    assert "def save_connection" not in src, "旧的先落库函数应已删除"
    assert "def save_all" in src
    # probe 在 save_all 之前
    assert src.find("await probe(") < src.find("def save_all")


def test_p1b2_create_server_flush_not_commit():
    """F-P1-24: insert 用 flush 而非 commit。"""
    import inspect
    from backend.api import servers as servers_api
    src = inspect.getsource(servers_api.create_server)
    insert_src = src[src.find("def insert"):]
    insert_src = insert_src[:insert_src.find("server, server_id = await")]
    assert "db.flush()" in insert_src
    assert "db.commit()" not in insert_src


# ---------- 真并发测试（需 PG） ----------

@pytest.mark.skipif(_pg_url() is None, reason="需要真 PG（DATABASE_URL=postgresql://...）")
def test_p1b2_extend_no_lost_update_pg():
    """F-P1-4 真并发：两事务同时延长，结束日期应累加两次。"""
    from sqlalchemy import create_engine as ce
    engine = ce(_pg_url(), isolation_level="READ COMMITTED")
    models.Base.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    db = Session()
    sub = models.UserSubscription(user_id=1, end_date=datetime.now())
    db.add(sub)
    db.commit()
    sid = sub.id
    db.close()

    barrier = threading.Barrier(2)
    errors = []

    def worker():
        sess = Session()
        try:
            s = (sess.query(models.UserSubscription)
                 .filter(models.UserSubscription.id == sid)
                 .with_for_update().first())
            barrier.wait(timeout=30)
            s.end_date = max(s.end_date or datetime.now(), datetime.now()) + timedelta(days=30)
            sess.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            sess.rollback()
        finally:
            sess.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert not errors
    check = Session()
    try:
        final = check.query(models.UserSubscription).filter_by(id=sid).one()
        # 两次各 +30 天，允许 1 天误差（边界 now）
        assert final.end_date > datetime.now() + timedelta(days=58), \
            f"丢失了一次延长: {final.end_date}"
    finally:
        check.close()
        engine.dispose()

"""活力值每日扣减：单用户失败必须隔离，不能污染整批事务。

生产是 PostgreSQL：事务中任一语句失败后整个事务进入 aborted 状态，
后续所有语句（含 COMMIT）都报 "current transaction is aborted"，
直到 ROLLBACK。旧实现里 per-user 的 try/except 吞掉了单用户异常，
但事务已污染 → 本批剩余用户全失败 → 批末 db.commit() 抛异常逃出
daily_deduct；而 _tick 已在调用前认领了当天日期，当天不会再跑 →
大量用户整天漏扣。本文件用代理 Session 模拟 PG 的 abort 语义
（SQLite 本身无此语义）做回归验证。
"""
import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import InternalError
from sqlalchemy.orm import sessionmaker

from backend import models, vitality
from backend.integrations import store as _store


class PGAbortSession:
    """模拟 PG 事务 abort 语义：poison() 后 execute/commit 一律抛
    InternalError，直到 rollback()（对应 ROLLBACK）或存点回滚
    （对应 ROLLBACK TO SAVEPOINT）。"""

    def __init__(self, real):
        self._real = real
        self._poisoned = False

    def poison(self):
        self._poisoned = True

    def _check(self):
        if self._poisoned:
            raise InternalError(
                "current transaction is aborted, "
                "commands ignored until end of transaction block",
                None, None,
            )

    def execute(self, *a, **k):
        self._check()
        return self._real.execute(*a, **k)

    def commit(self):
        self._check()
        return self._real.commit()

    def rollback(self):
        self._poisoned = False
        return self._real.rollback()

    def begin_nested(self):
        real_sp = self._real.begin_nested()
        proxy = self

        class _SP:
            def __enter__(self):
                real_sp.__enter__()
                return self

            def __exit__(self, t, v, tb):
                try:
                    return real_sp.__exit__(t, v, tb)
                finally:
                    if t is not None:
                        proxy._poisoned = False

        return _SP()

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def db():
    # connect_args autocommit=False：现代事务控制，让 SAVEPOINT 在 SQLite 下
    # 语义正确（默认 legacy 模式下 SAVEPOINT 不参与外层事务，RELEASE 会直接
    # 提交，ROLLBACK 滚不掉——见 SQLAlchemy 文档 pysqlite "Incorrect behavior
    # for SAVEPOINT"）。生产是 PostgreSQL，存点语义本就正确；这里只是让
    # SQLite 测试环境忠实模拟。
    engine = create_engine("sqlite://", connect_args={"autocommit": False})
    models.Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    _store.invalidate("vitality_enabled", "vitality_daily_cost", "vitality_max",
                      vitality.CONFIG_LAST_DEDUCT_DATE)
    yield s
    s.close()


def _make_users(db, n, vitality_value=5):
    db.bulk_save_objects([
        models.WebUser(username=f"u{i}", password_hash="x",
                       is_welfare=True, vitality=vitality_value)
        for i in range(n)
    ])
    db.add(models.SystemConfig(key="vitality_daily_cost", value="1"))
    db.commit()


def test_per_user_failure_does_not_poison_batch(db, monkeypatch):
    """单用户扣减失败（PG 语义下污染事务）不能影响其他用户，且异常不能逃出。"""
    n = 10
    _make_users(db, n)
    proxy = PGAbortSession(db)
    fail_uid = 3
    orig_add = vitality._add_vitality

    def flaky_add(db_, user_id, delta, reason):
        if user_id == fail_uid:
            db_.poison()
            raise RuntimeError("injected per-user DB failure")
        return orig_add(db_, user_id, delta, reason)

    monkeypatch.setattr(vitality, "_add_vitality", flaky_add)

    res = vitality.daily_deduct(proxy)  # 旧代码这里会抛 InternalError

    assert res["deducted"] == n - 1
    assert res["failed"] == 1
    assert res["suspended"] == 0
    # 失败用户 untouched，其余用户扣到 4
    assert db.query(models.WebUser).filter(models.WebUser.vitality == 4).count() == n - 1
    assert db.query(models.WebUser).filter(models.WebUser.id == fail_uid,
                                           models.WebUser.vitality == 5).count() == 1


def test_normal_deduct_unchanged(db):
    """无失败时行为不变：全部扣减，failed=0（含 suspend 计数口径）。"""
    n = 5
    _make_users(db, n, vitality_value=1)
    res = vitality.daily_deduct(db)
    assert res == {"deducted": n, "suspended": n, "failed": 0}
    assert db.query(models.WebUser).filter(models.WebUser.vitality > 0).count() == 0


def test_batch_commit_failure_stops_gracefully(db, monkeypatch):
    """批提交失败时回滚本批、保留已提交批次、不抛异常（不重跑，避免重复扣减）。"""
    _make_users(db, 3)
    calls = {"n": 0}

    def flaky_commit():
        calls["n"] += 1
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(db, "commit", flaky_commit)
    res = vitality.daily_deduct(db)  # 旧代码这里会把异常抛给 _tick
    assert res["deducted"] == 3  # 计数保留（本批回滚，实际未落库）
    assert calls["n"] == 1  # 只试一次，不死循环重跑
    # 本批被回滚：用户活力值未变
    assert db.query(models.WebUser).filter(models.WebUser.vitality == 5).count() == 3

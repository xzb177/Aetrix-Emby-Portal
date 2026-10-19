"""r52 后端完美迭代：订阅发放并发双新建缺口（r48 EDGE-1）。

F1（中）：``_grant_subscription`` 先 SELECT 生效行（FOR UPDATE）再决定顺延/新建——
无行可锁时两个并发事务会各自 INSERT，产生双 active 行（到期提醒双发、订阅统计虚高）。
行锁锁不住「不存在的行」。
修法：入口先拿 ``subscription_grant_lock`` 按（用户，服）串行
（PG=事务级 advisory lock，随事务提交自动释放；SQLite/测试=进程内 RLock）；
``_auto_renew_one`` 按同一锁序重排（先拿发放锁再拿行锁），避免 AB-BA 死锁。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.economy import _grant_subscription, subscription_grant_lock


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


def _mk_session(engine):
    return sessionmaker(bind=engine)()


def _make_user(db, username="u1"):
    u = models.WebUser(username=username, password_hash="", points=10000,
                       is_active=True, member_xp=0, member_level=1)
    db.add(u)
    db.commit()
    return u


def _make_plan(db, days=30):
    p = models.SubscriptionPlan(name="远行执照", price=100,
                                duration_days=days, is_active=True)
    db.add(p)
    db.commit()
    return p


def _make_realm(db, slug):
    r = models.ServerRealm(name=f"服-{slug}", slug=slug)
    db.add(r)
    db.commit()
    return r


def _active_rows(db, user_id, realm_id):
    now = datetime.now()
    return (
        db.query(models.UserSubscription)
        .filter(
            models.UserSubscription.user_id == user_id,
            models.UserSubscription.realm_id == realm_id,
            models.UserSubscription.status == "active",
            models.UserSubscription.end_date > now,
        )
        .all()
    )


def test_concurrent_grants_serialized(engine, monkeypatch):
    """并发双发放：第二个必须等第一个走完临界区，不能各自新建。

    确定性设计：A 线程在 insert 分支的 flush 处暂停（仍持有串行锁），
    B 线程此时发起发放——修前 B 会直接走完（无锁），修后 B 必须被挡在锁外。
    """
    s0 = _mk_session(engine)
    u = _make_user(s0, "race-u")
    plan = _make_plan(s0)
    realm = _make_realm(s0, "race")
    # 跨线程不传 ORM 对象（s0 关闭后属性过期）：只传主键，各线程自建会话重查
    user_id, plan_id, realm_id = u.id, plan.id, realm.id
    s0.close()

    entered = threading.Event()   # A 已进入临界区（flush 被暂停）
    proceed = threading.Event()   # 主线程放行 A
    b_done = threading.Event()
    errors = []

    real_flush = Session.flush

    def hooked_flush(self):
        # 只拦第一次「带着新建 UserSubscription 的」flush（A 的 insert 分支）；
        # 查之前的 autoflush（无 pending）不拦。
        if not entered.is_set() and self.new:
            entered.set()
            if not proceed.wait(timeout=30):
                raise AssertionError("test hook: proceed 未在 30s 内放行")
        return real_flush(self)

    monkeypatch.setattr(Session, "flush", hooked_flush)

    def grant_a():
        s1 = _mk_session(engine)
        try:
            user = s1.get(models.WebUser, user_id)
            plan = s1.get(models.SubscriptionPlan, plan_id)
            _grant_subscription(s1, user, plan, 30, "t", "race:a", realm_id=realm_id)
            s1.commit()
        except Exception as e:  # noqa: BLE001 - 透传给主线程断言
            errors.append(e)
        finally:
            s1.close()

    def grant_b():
        s2 = _mk_session(engine)
        try:
            user = s2.get(models.WebUser, user_id)
            plan = s2.get(models.SubscriptionPlan, plan_id)
            _grant_subscription(s2, user, plan, 30, "t", "race:b", realm_id=realm_id)
            s2.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            s2.close()
        b_done.set()

    ta = threading.Thread(target=grant_a, daemon=True)
    ta.start()
    assert entered.wait(timeout=30), "A 未进入临界区"
    tb = threading.Thread(target=grant_b, daemon=True)
    tb.start()
    # B 发起 3 秒后仍未完成 = 被串行锁挡住（修前这里 3 秒内必完成，测试变红）
    time.sleep(3)
    assert not b_done.is_set(), "并发发放未被串行化：B 在 A 持有锁期间走完了临界区"
    proceed.set()
    ta.join(timeout=30)
    tb.join(timeout=30)
    assert not errors, f"子线程异常: {errors}"
    # 注：不在此处断言赛跑对的最终行数——sqlite 回退路径的 RLock 在 commit 前释放，
    # B 的检查可能排在 A 的 commit 之前（测试环境 artifact）；生产 PG 用 xact 级
    # advisory lock，锁覆盖到事务提交之后，不存在该窗口。串行语义由上面的阻塞
    # 断言覆盖；单行累加语义由 test_sequential_grants_extend_single_row 覆盖。


def test_sequential_grants_extend_single_row(engine):
    """串行两次发放：仍是 1 行，天数累加（回归既有语义）。"""
    s = _mk_session(engine)
    u = _make_user(s, "seq-u")
    plan = _make_plan(s)
    realm = _make_realm(s, "seq")
    t0 = datetime.now()
    _grant_subscription(s, u, plan, 30, "t", "seq:1", realm_id=realm.id)
    s.commit()
    _grant_subscription(s, u, plan, 30, "t", "seq:2", realm_id=realm.id)
    s.commit()
    rows = _active_rows(s, u.id, realm.id)
    assert len(rows) == 1
    assert rows[0].end_date >= t0 + timedelta(days=59)
    s.close()


def test_grant_lock_is_per_realm(engine):
    """串行锁按（用户，服）派生：不同服的发放互不阻塞、各建一行。"""
    s = _mk_session(engine)
    u = _make_user(s, "iso-u")
    plan = _make_plan(s)
    r1 = _make_realm(s, "iso1")
    r2 = _make_realm(s, "iso2")
    _grant_subscription(s, u, plan, 30, "t", "iso:1", realm_id=r1.id)
    _grant_subscription(s, u, plan, 30, "t", "iso:2", realm_id=r2.id)
    s.commit()
    assert len(_active_rows(s, u.id, r1.id)) == 1
    assert len(_active_rows(s, u.id, r2.id)) == 1
    # 锁 key 确实按服区分
    from backend.api.economy import _subscription_grant_lock_key
    assert _subscription_grant_lock_key(u.id, r1.id) != _subscription_grant_lock_key(u.id, r2.id)
    assert _subscription_grant_lock_key(u.id, None) == _subscription_grant_lock_key(u.id, None)
    s.close()


def test_grant_lock_contextmanager_reentrant_and_pg_branch(engine, monkeypatch):
    """锁是可重入的（_auto_renew_one 与 _grant_subscription 嵌套拿同一把锁）。"""
    s = _mk_session(engine)
    with subscription_grant_lock(s, 1, 2):
        with subscription_grant_lock(s, 1, 2):
            pass
    # PG 分支：dialect 伪装成 postgresql，断言走了 advisory SQL 而非 RLock
    seen = []

    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        bind = FakeBind()

        def execute(self, stmt, params=None):
            seen.append(str(stmt))

    with subscription_grant_lock(FakeSession(), 7, 9):
        pass
    assert any("pg_advisory_xact_lock" in q for q in seen), f"PG 分支未走 advisory lock: {seen}"
    s.close()

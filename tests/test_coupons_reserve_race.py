"""reserve() 单人限领并发回归

根因：``per_user_limit > 0`` 时的 ``INSERT ... SELECT ... WHERE (count < limit)``
在 PostgreSQL READ COMMITTED 下**不是**原子的——两个并发事务各自快照都看到
count=0，会都插入成功，绕过每人限领（复现脚本见本轮 workflow 的 race_repro）。
修复：``reserve()`` 入口对券行加 ``with_for_update()`` 行锁，把同一张券的占位
串行化（``backend/coupons.py``）。

- ``test_reserve_per_user_limit_concurrent_pg``：真 PG 上的双线程竞态；
  未修复时两个线程都成功（测试失败），修复后恰好一个成功、另一个 400。
  只有 ``DATABASE_URL`` 指向 postgres 时才跑，否则跳过（SQLite 的写串行化
  复现不出这个竞态）。
- ``test_reserve_per_user_limit_sequential``：串行语义，SQLite 可跑；
  修前修后都应通过——保证加锁没把正常路径搞坏。
- ``test_reserve_unlimited_still_allows_many``：``per_user_limit=0`` 的券
  不受单人限领约束，加锁后依然能连续占位。
"""

import decimal
import os
import sys
import threading
import time
import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")

from backend import coupons, models  # noqa: E402


def _pg_url():
    """CI backend-pg job 的 DATABASE_URL；非 PG 返回 None（调用方跳过）。"""
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    if url.startswith("postgresql://"):
        # 与 backend/database.py 同一口径：没装 psycopg(v3) 就显式走 psycopg2
        try:
            import psycopg  # noqa: F401
        except ImportError:
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _placeholder(col):
    try:
        py = col.type.python_type
    except (AttributeError, NotImplementedError):  # pragma: no cover
        py = str
    if py is int:
        return 1
    if py is float:
        return 1.0
    if py is bool:
        return False
    if py is decimal.Decimal:
        return decimal.Decimal("1.00")
    if py is datetime:
        return datetime.now()
    return f"t_{col.name}"


def _make(model, **overrides):
    kwargs = {}
    for col in model.__table__.columns:
        if col.name in overrides or col.primary_key:
            continue
        if col.nullable or col.default is not None or col.server_default is not None:
            continue
        kwargs[col.name] = _placeholder(col)
    kwargs.update(overrides)
    return model(**kwargs)


def _seed(Session, *, per_user_limit=1):
    """造数：用户名/优惠码每次唯一，避免 PG 库在多次运行间残留数据时撞唯一约束。"""
    suffix = uuid.uuid4().hex[:8]
    s = Session()
    try:
        user = _make(models.WebUser, username=f"race_{suffix}")
        coupon = _make(
            models.CouponCode,
            code=f"RACE-{suffix}", kind="all", discount_type="percent", value=85,
            per_user_limit=per_user_limit, max_uses=0, use_count=0,
            is_active=True,
        )
        s.add_all([user, coupon])
        s.commit()
        return user.id, coupon.id
    finally:
        s.close()


def _reserve_kwargs():
    return dict(
        kind="subscription",
        list_price=decimal.Decimal("100.00"),
        discount=decimal.Decimal("15.00"),
        paid=decimal.Decimal("85.00"),
    )


def _active_count(db, coupon_id, user_id):
    return db.query(models.CouponUsage).filter(
        models.CouponUsage.coupon_id == coupon_id,
        models.CouponUsage.user_id == user_id,
        models.CouponUsage.status.in_(coupons.ACTIVE_STATUSES),
    ).count()


@pytest.mark.skipif(_pg_url() is None, reason="需要真 PG（DATABASE_URL=postgresql://...）才能复现该竞态")
def test_reserve_per_user_limit_concurrent_pg():
    engine = create_engine(_pg_url(), isolation_level="READ COMMITTED")
    models.Base.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    user_id, coupon_id = _seed(Session)

    barrier = threading.Barrier(2)
    outcomes = {}
    run_tag = uuid.uuid4().hex[:8]

    def worker(tag, order_id):
        sess = Session()
        try:
            u = sess.query(models.WebUser).filter_by(id=user_id).one()
            c = sess.query(models.CouponCode).filter_by(id=coupon_id).one()
            barrier.wait(timeout=30)  # 两个事务尽量同时起跑
            usage = coupons.reserve(sess, coupon=c, user=u, order_id=order_id,
                                    **_reserve_kwargs())
            # 两个事务的 INSERT...SELECT 都执行完之后才各自提交：
            # 保证双方快照都看不到对方未提交的插入（PG READ COMMITTED 语义）。
            # 注意：修复后第二个线程会卡在 reserve() 的行锁上直到第一个提交，
            # 所以这里用 sleep 而不是第二个 barrier（barrier 会和行锁互锁）。
            time.sleep(2.0)
            sess.commit()
            outcomes[tag] = ("ok", usage.id)
        except HTTPException as exc:
            sess.rollback()
            outcomes[tag] = ("http", exc.status_code)
        except Exception as exc:  # noqa: BLE001
            sess.rollback()
            outcomes[tag] = ("error", f"{type(exc).__name__}: {exc}")
        finally:
            sess.close()

    threads = [
        threading.Thread(target=worker, args=("t1", f"RACE-PG-A-{run_tag}"), daemon=True),
        threading.Thread(target=worker, args=("t2", f"RACE-PG-B-{run_tag}"), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    assert not any(t.is_alive() for t in threads), f"worker 卡住: {outcomes}"

    oks = [k for k, v in outcomes.items() if v[0] == "ok"]
    rejected = [k for k, v in outcomes.items() if v == ("http", 400)]
    assert len(oks) == 1, f"应恰好一笔成功: {outcomes}"
    assert len(rejected) == 1, f"另一笔应被 400 拒绝: {outcomes}"

    check = Session()
    try:
        assert _active_count(check, coupon_id, user_id) == 1
        assert check.query(models.CouponCode).filter_by(id=coupon_id).one().use_count == 1
    finally:
        check.close()
        engine.dispose()


def test_reserve_per_user_limit_sequential(tmp_path):
    """串行：第一次占位成功，第二次同用户 400；释放后可再占。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'coupons_reserve_seq.db'}")
    for m in (models.WebUser, models.CouponCode, models.CouponUsage):
        m.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    user_id, coupon_id = _seed(Session)

    s = Session()
    try:
        u = s.query(models.WebUser).filter_by(id=user_id).one()
        c = s.query(models.CouponCode).filter_by(id=coupon_id).one()
        usage = coupons.reserve(s, coupon=c, user=u, order_id="SEQ-1", **_reserve_kwargs())
        s.commit()
        assert usage.status == coupons.RESERVED

        with pytest.raises(HTTPException) as excinfo:
            coupons.reserve(s, coupon=c, user=u, order_id="SEQ-2", **_reserve_kwargs())
        assert excinfo.value.status_code == 400
        s.rollback()

        # 释放后名额回来，可以再占
        coupons.release(s, usage.id)
        s.commit()
        usage2 = coupons.reserve(s, coupon=c, user=u, order_id="SEQ-3", **_reserve_kwargs())
        s.commit()
        assert usage2.status == coupons.RESERVED
        assert _active_count(s, coupon_id, user_id) == 1
    finally:
        s.close()
        engine.dispose()


def test_reserve_unlimited_still_allows_many(tmp_path):
    """per_user_limit=0（不限）：加锁后连续占位不受影响。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'coupons_reserve_unlimited.db'}")
    for m in (models.WebUser, models.CouponCode, models.CouponUsage):
        m.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    user_id, coupon_id = _seed(Session, per_user_limit=0)

    s = Session()
    try:
        u = s.query(models.WebUser).filter_by(id=user_id).one()
        c = s.query(models.CouponCode).filter_by(id=coupon_id).one()
        for i in range(3):
            coupons.reserve(s, coupon=c, user=u, order_id=f"UNL-{i}", **_reserve_kwargs())
        s.commit()
        assert _active_count(s, coupon_id, user_id) == 3
        assert s.query(models.CouponCode).filter_by(id=coupon_id).one().use_count == 3
    finally:
        s.close()
        engine.dispose()

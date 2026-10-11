# tests/test_invitation_code_claim_race.py
"""邀请码名额占位的并发回归测试。

根因：apply_invitation 里 use_count 是读-改-写 TOCTOU——
    if code.max_uses and code.use_count >= code.max_uses: ...   # 读 + 判断
    ...
    code.use_count = (code.use_count or 0) + 1                  # 写
同一张 max_uses=2 的邀请码被 6 个注册请求同时提交（SQLite 文件库 + 6 线程 barrier 对齐）时，
6 个线程全部读到 use_count=0、全部返回 applied=True，落了 6 条 InvitationRecord、发了 6 份
双向奖励，最终 use_count=1（写回互相覆盖）——超发（发奖数 > max_uses）+ 丢更新。

修复：与 backend/codes.py::claim_code 同一写法，把"是否可用"表达成一次条件 UPDATE
（id 相符 + is_active + 未过期 + max_uses 为空/0/未用尽），只有仍然可用的那一行会被 +1，
并发下抢不到名额的请求 rowcount=0，直接拒绝；占位与发奖同一事务、不提交。

覆盖：
- test_concurrent_claim_respects_max_uses：核心回归。修前失败（6 applied / 6 记录 / use_count=1），
  修后通过（恰好 2 applied / 2 记录 / use_count=2）。
- test_concurrent_claim_pg：真 PG 并发版本（无 DATABASE_URL 时自动 skip）。
- test_sequential_single_use_code：串行语义没被原子占位搞坏。
- test_exhausted_code_rejected_without_side_effects：停用码 / 已用尽码被拒绝且无副作用。
"""

from __future__ import annotations

import os
import sys
import threading
import uuid

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.invitation import apply_invitation

try:
    from backend.core.store import store
except ImportError:  # 进程级热缓存可选，缺了也不影响
    store = None


def _invalidate_store() -> None:
    """清掉进程级热缓存（邀请配置等），避免上一个测试/上一次运行污染"""
    if store is None:
        return
    try:
        store.invalidate()
    except Exception:  # noqa: BLE001
        pass


def _user(db, username: str) -> models.WebUser:
    """建一个可用用户，只填模型真实存在的列（WebUser 没有 role 列）"""
    cols = {c.name for c in models.WebUser.__table__.columns}
    kwargs = {"username": username}
    for field, value in (("password_hash", "x"), ("is_active", True)):
        if field in cols:
            kwargs[field] = value
    u = models.WebUser(**kwargs)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_code(db, inviter_id: int, code_str: str, max_uses):
    """建一张邀请码（use_count 从 0 起）"""
    code = models.InvitationCode(
        code=code_str,
        user_id=inviter_id,
        is_active=True,
        max_uses=max_uses,
        use_count=0,
    )
    db.add(code)
    db.commit()
    db.refresh(code)
    return code


def _sqlite_file_engine(tmp_path):
    """SQLite 文件库：多线程各自独立连接，timeout 让写锁等待而不是立刻抛 OperationalError"""
    db_path = tmp_path / "invite-race.db"
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
        future=True,
    )


def _sqlite_memory_engine():
    """SQLite 内存库：串行测试用，StaticPool 让同一连接被复用"""
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )


def _pg_url() -> str:
    """真 PG 连接串；未配置时返回空串（对应测试直接 skip）"""
    url = os.getenv("DATABASE_URL", "")
    return url if url.startswith("postgres") else ""


def test_concurrent_claim_respects_max_uses(tmp_path):
    """核心回归：max_uses=2 的码被 6 个并发注册请求抢，只能有 2 个成功。

    修前：6 个 applied、6 条邀请记录、use_count=1（超发 + 丢更新）→ 断言失败；
    修后：恰好 2 个 applied、2 条记录、use_count=2。
    """
    _invalidate_store()
    engine = _sqlite_file_engine(tmp_path)
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

    tag = uuid.uuid4().hex[:8]
    setup = SessionLocal()
    try:
        inviter = _user(setup, f"race-inviter-{tag}")
        code = _make_code(setup, inviter.id, f"RACE{tag.upper()}", 2)
        code_str = code.code
    finally:
        setup.close()

    n = 6
    barrier = threading.Barrier(n)
    results: list = []
    errors: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        db = SessionLocal()
        try:
            # 先各自建好被邀请人并 commit，再对齐起跑线
            invitee = _user(db, f"race-invitee-{tag}-{i}")
            barrier.wait(timeout=30)
            try:
                res = apply_invitation(db, invitee, code_str)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                with lock:
                    errors.append(exc)
                return
            if res.get("applied"):
                db.commit()  # 模拟注册流程调用方提交
            else:
                db.rollback()
            with lock:
                results.append(bool(res.get("applied")))
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发过程出现异常: {errors!r}"
    assert sum(results) == 2, f"应恰好 2 个成功，实际 {results!r}"

    check = SessionLocal()
    try:
        records = check.query(models.InvitationRecord).count()
        row = (
            check.query(models.InvitationCode)
            .filter(models.InvitationCode.code == code_str)
            .first()
        )
        assert records == 2, f"邀请记录应恰好 2 条，实际 {records}"
        assert row.use_count == 2, f"use_count 应为 2，实际 {row.use_count}"
    finally:
        check.close()
        engine.dispose()
    _invalidate_store()


@pytest.mark.skipif(not _pg_url(), reason="需要 DATABASE_URL 指向 PostgreSQL")
def test_concurrent_claim_pg():
    """真 PG 并发：max_uses=1，3 个线程 barrier 对齐 → 恰好 1 个成功"""
    _invalidate_store()
    engine = create_engine(_pg_url(), future=True, pool_pre_ping=True)
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

    tag = uuid.uuid4().hex[:8]
    setup = SessionLocal()
    try:
        inviter = _user(setup, f"pg-inviter-{tag}")
        code = _make_code(setup, inviter.id, f"PGRACE{tag.upper()}", 1)
        code_str, code_id = code.code, code.id
    finally:
        setup.close()

    n = 3
    barrier = threading.Barrier(n)
    results: list = []
    errors: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        db = SessionLocal()
        try:
            invitee = _user(db, f"pg-invitee-{tag}-{i}")
            barrier.wait(timeout=30)
            try:
                res = apply_invitation(db, invitee, code_str)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                with lock:
                    errors.append(exc)
                return
            if res.get("applied"):
                db.commit()
            else:
                db.rollback()
            with lock:
                results.append(bool(res.get("applied")))
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"并发过程出现异常: {errors!r}"
    assert sum(results) == 1, f"应恰好 1 个成功，实际 {results!r}"

    check = SessionLocal()
    try:
        # 共享库上按 code_id 过滤，避免别的用例/历史数据干扰
        records = (
            check.query(models.InvitationRecord)
            .filter(models.InvitationRecord.code_id == code_id)
            .count()
        )
        row = (
            check.query(models.InvitationCode)
            .filter(models.InvitationCode.id == code_id)
            .first()
        )
        assert records == 1, f"该码应恰好 1 条邀请记录，实际 {records}"
        assert row.use_count == 1, f"use_count 应为 1，实际 {row.use_count}"
    finally:
        check.close()
        engine.dispose()
    _invalidate_store()


def test_sequential_single_use_code():
    """串行语义不变：max_uses=1 的码，第一个人成功、第二个人被拒"""
    _invalidate_store()
    engine = _sqlite_memory_engine()
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

    tag = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        inviter = _user(db, f"seq-inviter-{tag}")
        code = _make_code(db, inviter.id, f"SEQ{tag.upper()}", 1)
        code_str = code.code

        first = _user(db, f"seq-first-{tag}")
        assert apply_invitation(db, first, code_str)["applied"] is True
        db.commit()

        second = _user(db, f"seq-second-{tag}")
        assert apply_invitation(db, second, code_str)["applied"] is False
        db.rollback()

        db.expire_all()
        row = (
            db.query(models.InvitationCode)
            .filter(models.InvitationCode.code == code_str)
            .first()
        )
        assert row.use_count == 1, f"use_count 应为 1，实际 {row.use_count}"
        assert db.query(models.InvitationRecord).count() == 1
    finally:
        db.close()
        engine.dispose()
        _invalidate_store()


def test_exhausted_code_rejected_without_side_effects():
    """停用码 / 已用尽码被拒绝，且不产生任何副作用（无新积分流水、无新邀请记录）"""
    _invalidate_store()
    engine = _sqlite_memory_engine()
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

    tag = uuid.uuid4().hex[:8]
    db = SessionLocal()
    try:
        inviter = _user(db, f"ex-inviter-{tag}")

        # 先正常用掉一次，制造"已用尽"状态
        used = _make_code(db, inviter.id, f"EX{tag.upper()}", 1)
        first = _user(db, f"ex-first-{tag}")
        assert apply_invitation(db, first, used.code)["applied"] is True
        db.commit()

        points_before = db.query(models.PointsLog).count()
        records_before = db.query(models.InvitationRecord).count()

        # 1) 停用码：读查询直接查不到 → 拒绝
        used.is_active = False
        db.commit()
        second = _user(db, f"ex-second-{tag}")
        assert apply_invitation(db, second, used.code)["applied"] is False
        db.rollback()

        # 2) use_count 已等于 max_uses：可用性检查拒绝，原子占位也不会命中
        exhausted = _make_code(db, inviter.id, f"EXD{tag.upper()}", 1)
        exhausted.use_count = 1
        db.commit()
        third = _user(db, f"ex-third-{tag}")
        assert apply_invitation(db, third, exhausted.code)["applied"] is False
        db.rollback()

        db.expire_all()
        assert db.query(models.PointsLog).count() == points_before, "被拒绝的邀请码不应发积分"
        assert db.query(models.InvitationRecord).count() == records_before, "被拒绝的邀请码不应建关系"
    finally:
        db.close()
        engine.dispose()
        _invalidate_store()

"""回归测试：share_guard.note_session 并发准入的 check-then-act 竞态。

Bug
---
`note_session` 内部先"数活会话"再判上限，调用方随后"建会话行 → commit"。
两步之间存在窗口：并发两路新会话会各自数到 0 个活会话，双双放行，
并发上限形同虚设。

修复
----
`note_session` 在判定前调用 `economy.lock_user_row(db, user.id)` 对用户行加锁
（SQLite 文件库上表现为写事务/行锁），锁由本事务持有到 commit/rollback，
从而把"判定 + 建行 + 提交"整体串行化。

测试 1 在未修复代码上必失败的原理
--------------------------------
测试 1 用两阶段事件编排制造确定性交错：
  T1 先拿到放行判决（此时它已持锁，但尚未提交建行），停在 t1_ready；
  T2 随后进入 note_session 并在 t2_entered 打点。
修复后：T2 会卡在 `lock_user_row` 上，直到 T1 提交，之后才数到 1 个活会话 → 被拦。
未修复：T2 不会被锁住，直接数到 0 个活会话 → 放行，`t2_blocked` 为 False → 断言失败。
"""

import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend import share_guard
from backend.emby_server import models as em
from backend.integrations import store


@pytest.fixture
def factory(tmp_path):
    store.invalidate()
    share_guard._notified_at.clear()

    db_path = tmp_path / "race.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    mk = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield mk
    finally:
        engine.dispose()
        store.invalidate()
        share_guard._notified_at.clear()


def _seed(mk):
    db = mk()
    try:
        user = models.WebUser(
            username="racer", password_hash="x", is_active=True, is_staff=False
        )
        db.add(user)
        db.commit()
        uid = user.id

        for key, value in (
            (share_guard.CONFIG_CONCURRENT_ACTION, "enforce"),
            (share_guard.CONFIG_CONCURRENT_LIMIT, "1"),
            (share_guard.CONFIG_TRAVEL_ACTION, "off"),
        ):
            db.add(models.SystemConfig(key=key, value=value))
        db.commit()
    finally:
        db.close()

    store.invalidate()
    return uid


@pytest.fixture(autouse=True)
def _no_city(monkeypatch):
    monkeypatch.setattr(share_guard, "city_of", lambda *a, **k: "", raising=False)


def test_concurrent_admissions_are_serialized(factory):
    mk = factory
    uid = _seed(mk)

    t1_ready = threading.Event()
    t2_entered = threading.Event()
    t1_go_commit = threading.Event()
    outcome = {}

    def t1_body():
        db = mk()
        try:
            user = db.get(models.WebUser, uid)
            verdict = share_guard.note_session(
                db, user, "race-k1", "203.0.113.9", commit=True
            )
            assert verdict is None, f"第一路必须放行，实际 verdict={verdict!r}"
            t1_ready.set()

            assert t1_go_commit.wait(timeout=15), "等待提交信号超时"

            db.add(
                em.PlaybackSession(session_key="race-k1", user_id=uid, item_id=1)
            )
            db.commit()
            outcome["t1"] = "admitted"
        except Exception as exc:  # noqa: BLE001
            outcome["t1"] = f"error: {exc!r}"
            db.rollback()
        finally:
            db.close()

    def t2_body():
        db = mk()
        try:
            assert t1_ready.wait(timeout=15), "等待 T1 放行超时"
            user = db.get(models.WebUser, uid)
            t2_entered.set()
            verdict = share_guard.note_session(
                db, user, "race-k2", "203.0.113.9", commit=True
            )
            outcome["t2_blocked"] = bool(verdict and verdict.get("blocked"))
        except Exception as exc:  # noqa: BLE001
            outcome["t2"] = f"error: {exc!r}"
        finally:
            db.rollback()
            db.close()

    th1 = threading.Thread(target=t1_body, name="t1")
    th2 = threading.Thread(target=t2_body, name="t2")
    th1.start()
    th2.start()

    assert t2_entered.wait(timeout=15), "T2 未进入 note_session"
    # 确保 T2 已经真正走到判定点：修复后它正卡在用户行锁上，
    # 未修复时它已经数完活会话直接返回。
    time.sleep(0.5)

    t1_go_commit.set()
    th1.join(20)
    th2.join(20)

    assert not th1.is_alive(), "T1 线程未在超时内结束"
    assert not th2.is_alive(), "T2 线程未在超时内结束"

    assert outcome.get("t1") == "admitted", (
        f"T1 应当被放行并成功提交，实际 outcome={outcome!r}"
    )
    assert outcome.get("t2_blocked") is True, (
        "并发第二路应在 T1 提交后被并发上限拦下（blocked=True），"
        f"实际 outcome={outcome!r}（未修复时此处会因 check-then-act 竞态放行）"
    )


def test_serial_second_session_still_blocked(factory):
    mk = factory
    uid = _seed(mk)

    db = mk()
    try:
        user = db.get(models.WebUser, uid)
        first = share_guard.note_session(
            db, user, "s1", "203.0.113.9", commit=True
        )
        assert first is None, f"串行第一路必须放行，实际 verdict={first!r}"
        db.add(em.PlaybackSession(session_key="s1", user_id=uid, item_id=1))
        db.commit()

        second = share_guard.note_session(
            db, user, "s2", "203.0.113.9", commit=True
        )
        assert second is not None, "串行第二路应被拦下，实际返回 None（放行）"
        assert second.get("blocked") is True, (
            f"串行第二路 verdict 应 blocked=True，实际 {second!r}"
        )
    finally:
        db.close()

    verify = mk()
    try:
        events = (
            verify.query(models.ShareGuardEvent)
            .filter(models.ShareGuardEvent.kind == "concurrent")
            .all()
        )
        assert len(events) >= 1, (
            "被拦下的并发准入应落一条 kind='concurrent' 的审计事件，"
            f"实际查到 {len(events)} 行"
        )
    finally:
        verify.close()


def test_guard_off_passes_without_judgement(factory):
    mk = factory
    uid = _seed(mk)

    setup = mk()
    try:
        row = (
            setup.query(models.SystemConfig)
            .filter(models.SystemConfig.key == "share_concurrent_action")
            .one()
        )
        row.value = "off"
        setup.commit()
    finally:
        setup.close()
    store.invalidate()

    db = mk()
    try:
        for i in range(3):
            db.add(
                em.PlaybackSession(
                    session_key=f"live-{i}", user_id=uid, item_id=1
                )
            )
        db.commit()

        before = db.query(models.ShareGuardEvent).count()

        user = db.get(models.WebUser, uid)
        verdict = share_guard.note_session(
            db, user, "off-k1", "203.0.113.9", commit=True
        )
        assert verdict is None, (
            f"off 档不应判定，必须放行，实际 verdict={verdict!r}"
        )

        after = db.query(models.ShareGuardEvent).count()
        assert after == before, (
            "off 档不判定也不写库，ShareGuardEvent 不应新增行，"
            f"实际 before={before} after={after}"
        )
    finally:
        db.close()

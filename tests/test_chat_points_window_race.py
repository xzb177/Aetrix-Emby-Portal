"""群发言积分分钟窗口并发竞态回归测试。

语义：chat_points_minute_window（秒）内同一用户只应得 1 次发言积分。
handle_group_message 的 recent 窗口检查曾在 lock_user_row 之前（无锁 check-then-act）：
poller 4 个工作线程并发处理同一用户的两条消息时，双方检查都发生在对方 commit
之前 → 双发积分，窗口限制被绕过（复现脚本 5/5 双发）。

修复：在持锁后用新 cutoff 二次校验窗口。本测试在未修复代码上失败、修复后通过。
全部用隔离的临时 SQLite 文件库（多线程共享），不碰生产库。
"""
import os
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.tg_bot import chat_points


@pytest.fixture()
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/chatpoints_window_race.db",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _seed(Session):
    db = Session()
    db.add(models.WebUser(username="window_racer", password_hash="x",
                           points=0, telegram_id=888001))
    db.add_all([
        models.SystemConfig(key="chat_points_enabled", value="true"),
        models.SystemConfig(key="chat_points_group_ids", value="-10077"),
        models.SystemConfig(key="chat_points_min_len", value="2"),
        models.SystemConfig(key="chat_points_minute_window", value="60"),
        models.SystemConfig(key="chat_points_daily_cap", value="20"),
        models.SystemConfig(key="chat_points_per_message", value="1"),
    ])
    db.commit()
    db.close()


def _make_update(message_id):
    return {"message": {
        "message_id": message_id,
        "chat": {"id": -10077, "type": "supergroup"},
        "from": {"id": 888001, "is_bot": False},
        "text": "hello world hello",
        "date": 1700000000,
    }}


def test_concurrent_messages_within_window_award_only_once(file_engine):
    """同一用户两条消息并发处理：60 秒窗口内只发 1 次积分。"""
    Session = sessionmaker(bind=file_engine)
    _seed(Session)

    for trial in range(3):
        # 每轮清空上一轮记录，窗口检查只看 60 秒内
        db = Session()
        db.query(models.ChatPointsLog).delete()
        db.query(models.PointsLog).delete()
        db.query(models.WebUser).filter_by(
            username="window_racer").update({"points": 0})
        db.commit()
        db.close()

        gate = threading.Barrier(2)
        results = {}

        def run(name, message_id):
            s = Session()
            try:
                gate.wait(timeout=10)  # 双线程同时起跑，最大化检查重叠
                results[name] = chat_points.handle_group_message(
                    s, _make_update(message_id))
            except Exception as exc:  # noqa: BLE001
                results[name] = f"EXC {type(exc).__name__}: {exc}"
            finally:
                s.close()

        base = trial * 100
        ta = threading.Thread(target=run, args=("A", base + 1))
        tb = threading.Thread(target=run, args=("B", base + 2))
        ta.start(); tb.start()
        ta.join(30); tb.join(30)
        assert not ta.is_alive() and not tb.is_alive(), "工作线程卡死"

        db = Session()
        n_logs = db.query(models.ChatPointsLog).count()
        points = db.query(models.WebUser.points).filter_by(
            username="window_racer").scalar()
        db.close()
        assert n_logs == 1, f"trial {trial}: 窗口内重复发奖 results={results}"
        assert int(points or 0) == 1, f"trial {trial}: 积分异常 results={results}"
        assert sorted(results.values()) == [0, 1], \
            f"trial {trial}: 应恰好一笔成功 results={results}"


def test_sequential_second_message_within_window_still_rejected(file_engine):
    """串行场景不受影响：窗口内第二条消息仍被拒绝（锁前快速检查仍有效）。"""
    Session = sessionmaker(bind=file_engine)
    _seed(Session)
    db = Session()
    assert chat_points.handle_group_message(db, _make_update(1)) == 1
    assert chat_points.handle_group_message(db, _make_update(2)) == 0
    assert db.query(models.ChatPointsLog).count() == 1
    db.close()

"""grant_welfare 并发丢失更新的回归测试。

背景：portal.grant_welfare 是公益资格发放的全站唯一写入点，
6 条链路共用（管理后台单发/批量、积分兑换、抽奖开奖、批量延期）。
旧实现是裸读-改-写：读 user.welfare_expires_at → Python 算新值 → 赋值 → commit。
两路并发（积分兑换 7 天 + 抽奖 30 天）同时读到 T0，各自算出 T0+7 / T0+30，
后提交覆盖先提交，实测丢失 30 天（复现脚本见工作流工作目录
repro_grant_welfare_race.py）。

修复：Compare-And-Swap + 有界重试（5 次）：每轮重读最新
(is_welfare, welfare_expires_at)，用"行没被改过"做 UPDATE 的 WHERE，
rowcount==0 则重试。本测试在未修复代码上失败、修复后通过。
全部用隔离的临时 SQLite 文件库（多线程共享），不碰生产库。
"""
import os
import threading
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import portal


@pytest.fixture()
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/grant_welfare_race.db",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    models.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _seed(Session, username, is_welfare, expires_at):
    db = Session()
    db.add(models.WebUser(username=username, password_hash="x",
                           is_welfare=is_welfare,
                           welfare_expires_at=expires_at))
    db.commit()
    db.close()


class _GatedDateTime(portal.datetime):
    """把各线程钉在 grant_welfare 入口的 datetime.now() 处。

    只在每个线程第一次调用时同步：修复后的代码会在 CAS 重试时多次调用 now()，
    重试时的调用不能再进 barrier（否则 barrier 会被已结束的线程打断）。
    """
    _barrier = None
    _seen = set()
    _lock = threading.Lock()

    @classmethod
    def now(cls, tz=None):
        tid = threading.get_ident()
        with cls._lock:
            first = tid not in cls._seen
            cls._seen.add(tid)
        if first:
            cls._barrier.wait(timeout=15)
        return super().now(tz)


def _run_concurrent(Session, username, grants, monkeypatch):
    """grants: [(channel, days), ...]，每个元素一个线程。返回各线程返回值。"""
    _GatedDateTime._barrier = threading.Barrier(len(grants))
    _GatedDateTime._seen = set()
    monkeypatch.setattr(portal, "datetime", _GatedDateTime)

    results = {}
    errors = {}

    def run(name, days):
        db = Session()
        try:
            user = db.query(models.WebUser).filter_by(username=username).first()
            # 预加载属性，排除 grant 内读库的干扰，只测"算-写"竞态
            _ = (user.is_welfare, user.welfare_expires_at)
            results[name] = portal.grant_welfare(db, user, channel=name, days=days)
        except Exception as exc:  # noqa: BLE001 — 收敛到主线程断言
            errors[name] = exc
        finally:
            db.close()

    threads = [threading.Thread(target=run, args=(name, days))
               for name, days in grants]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, f"线程异常: {errors}"
    assert all(not t.is_alive() for t in threads), "线程未在 30s 内结束"

    db = Session()
    final = db.query(models.WebUser.welfare_expires_at).filter_by(
        username=username).scalar()
    db.close()
    return results, final


def test_concurrent_grants_accumulate_without_loss(file_engine, monkeypatch):
    """两路并发发放 7 天 + 30 天：最终必须是 T0+37 天，不许丢。"""
    Session = sessionmaker(bind=file_engine)
    T0 = datetime(2026, 10, 20, 12, 0, 0)
    _seed(Session, "race_accumulate", True, T0)

    _, final = _run_concurrent(
        Session, "race_accumulate",
        [("race_points", 7), ("race_lottery", 30)],
        monkeypatch,
    )
    assert final == T0 + timedelta(days=37), (
        f"并发发放丢失天数：期望 {T0 + timedelta(days=37)}，实际 {final}"
    )


def test_concurrent_grant_with_permanent_keeps_permanent(file_engine, monkeypatch):
    """并发：一路发 30 天、一路发永久（days=0）：最终必须是永久。

    逐行语义里 days=0 就是永久，且"永久用户加有限天数保持永久"，
    无论两路谁先谁后，永久都不能被有限天数覆盖。
    """
    Session = sessionmaker(bind=file_engine)
    T0 = datetime(2026, 10, 20, 12, 0, 0)
    _seed(Session, "race_permanent", True, T0)

    _, final = _run_concurrent(
        Session, "race_permanent",
        [("race_limited", 30), ("race_whitelist", 0)],
        monkeypatch,
    )
    assert final is None, f"永久资格被并发覆盖：期望 None，实际 {final}"

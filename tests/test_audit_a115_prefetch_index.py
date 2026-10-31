# -*- coding: utf-8 -*-
"""A-115：每日预取上限 COUNT 必须走索引，不能对无界增长的转存表全表扫。

证据（R40-16.md §A-115）：transfer_variant._prefetch_today_count 按
(owner_scope, owner_ref, trigger, created_at) 过滤后 .count()，是已绑定用户
打开未 ready 条目详情页的热路径；TransferJob 旧 3 索引首列均为 item_id /
status，本查询一个都用不上；转存表无任何删除语句、历史行永久累积。

本文件两项断言：
1. 模型必须声明 ix_transfer_job_prefetch_daily 复合索引（旧码无此索引 → 红）。
2. 对 5000+ 行数据跑 _prefetch_today_count 同款 COUNT 的 EXPLAIN QUERY PLAN，
   必须是 SEARCH（走索引），不能是 SCAN（全表扫）；计数本身必须正确（34）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em

N_NOISE = 5000
N_MATCH = 34


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(session):
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    # 噪声行：刻意不命中四元组（换用户 / 换 trigger / 昨天），且 item_id 全不相同，
    # 避免规划器对旧 ix_transfer_job_owner 起 skip-scan、掩盖 SCAN 真相。
    for i in range(N_NOISE):
        if i % 3 == 0:
            rows.append({"item_id": 100000 + i, "owner_scope": "user",
                         "owner_ref": str(9000 + i), "trigger": "prefetch",
                         "created_at": today_start + timedelta(hours=1),
                         "status": "done"})
        elif i % 3 == 1:
            rows.append({"item_id": 100000 + i, "owner_scope": "user",
                         "owner_ref": "7", "trigger": "playback",
                         "created_at": today_start + timedelta(hours=1),
                         "status": "done"})
        else:
            rows.append({"item_id": 100000 + i, "owner_scope": "user",
                         "owner_ref": "7", "trigger": "prefetch",
                         "created_at": today_start - timedelta(days=1),
                         "status": "done"})
    # 命中行：今天、该用户、prefetch。
    for j in range(N_MATCH):
        rows.append({"item_id": 200000 + j, "owner_scope": "user",
                     "owner_ref": "7", "trigger": "prefetch",
                     "created_at": today_start + timedelta(hours=2),
                     "status": "pending"})
    session.execute(em.TransferJob.__table__.insert(), rows)
    session.commit()
    return today_start


def _count_stmt(today_start):
    return (select(func.count()).select_from(em.TransferJob)
            .where(em.TransferJob.owner_scope == "user",
                   em.TransferJob.owner_ref == "7",
                   em.TransferJob.trigger == "prefetch",
                   em.TransferJob.created_at >= today_start))


def test_model_declares_prefetch_daily_index():
    names = {ix.name for ix in em.TransferJob.__table__.indexes}
    assert "ix_transfer_job_prefetch_daily" in names


def test_legacy_index_entry_creates_index_on_old_db(tmp_path, monkeypatch):
    """老库兜底：_LEGACY_INDEXES 的 A-115 条目真能给旧表补上索引，且幂等。"""
    from sqlalchemy import inspect

    from backend import database

    eng = create_engine(f"sqlite:///{tmp_path / 'legacy_transfer.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE emby_transfer_jobs (id INTEGER PRIMARY KEY, "
            "item_id INTEGER, owner_scope TEXT, owner_ref TEXT, "
            "trigger TEXT, created_at TEXT, status TEXT)"))
    monkeypatch.setattr(database, "engine", eng)
    database._ensure_legacy_indexes({"emby_transfer_jobs"})
    idx = {ix["name"]: ix["column_names"]
           for ix in inspect(eng).get_indexes("emby_transfer_jobs")}
    assert idx == {"ix_transfer_job_prefetch_daily":
                   ["owner_scope", "owner_ref", "trigger", "created_at"]}
    database._ensure_legacy_indexes({"emby_transfer_jobs"})
    idx2 = {ix["name"]: ix["column_names"]
            for ix in inspect(eng).get_indexes("emby_transfer_jobs")}
    assert idx2 == idx
    eng.dispose()


def test_prefetch_today_count_uses_index_not_scan():
    session = _session()
    try:
        today_start = _seed(session)
        count = session.execute(_count_stmt(today_start)).scalar()
        assert count == N_MATCH
        compiled = _count_stmt(today_start).compile(
            compile_kwargs={"literal_binds": True})
        plan = session.execute(
            text("EXPLAIN QUERY PLAN " + str(compiled))).fetchall()
        detail = " | ".join(row[-1] for row in plan)
        assert "SEARCH" in detail, detail
        assert "SCAN" not in detail, detail
    finally:
        session.close()

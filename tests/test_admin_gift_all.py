# -*- coding: utf-8 -*-
"""管理端「全员赠送能量」能力（feat/admin-gift-all-points，2026-10-31 用户亲点）。

背景：2026-10-28 的全站送 50 能量是一次性手工数据操作（台账幂等键
gift:all50:20261028），没有端点、没有管理端入口、不可复用。本单把它做成
正式能力，只做能力不写死业务：金额、范围（all/welfare）、备注全是建批参数。

口径：
- 每笔台账 type='gift_all'（非自挣，须在 points.POINT_TYPE_SELF_EARNED 登记），
  ref_id='gift_all:<批次号>'，description=批次备注；
- 幂等靠 points_gift_grants 的 (batch_id, user_id) 唯一约束：重跑/续跑不重发；
- 发放走 worker 分块批处理（backend.gift_all），端点只建批不同步发分；
- WebUser 无软删列，注销即行不存在：scope=all 即现存全部 web_users 行，
  is_active 是禁用态、与注销不同口径，不在本单排除面内。
"""

import os
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import gift_all, models
from backend.api import admin_economy


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_user(db, username, **kw):
    kw.setdefault("points", 0)
    u = models.WebUser(username=username, password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_admin(db, username="root_admin"):
    return _make_user(db, username, is_staff=True)


def _seed_users(db):
    """普通 2 + 公益有效 1 + 公益永久 1 + 公益已过期 1（is_welfare 但过期）。"""
    now = datetime.now()
    plain1 = _make_user(db, "plain1")
    plain2 = _make_user(db, "plain2")
    wel_ok = _make_user(db, "wel_ok", is_welfare=True,
                        welfare_expires_at=now + timedelta(days=10))
    wel_perm = _make_user(db, "wel_perm", is_welfare=True, welfare_expires_at=None)
    wel_exp = _make_user(db, "wel_exp", is_welfare=True,
                         welfare_expires_at=now - timedelta(days=1))
    return plain1, plain2, wel_ok, wel_perm, wel_exp


def _gift_logs(db, batch_id=None):
    q = db.query(models.PointsLog).filter(models.PointsLog.type == "gift_all")
    if batch_id is not None:
        q = q.filter(models.PointsLog.ref_id == f"gift_all:{batch_id}")
    return q.order_by(models.PointsLog.user_id).all()


# ==================== 1. 金额与范围校验 ====================

def test_amount_must_be_positive_int():
    with pytest.raises(ValidationError):
        admin_economy.GiftAllCreateRequest(amount=0)
    with pytest.raises(ValidationError):
        admin_economy.GiftAllCreateRequest(amount=-50)
    with pytest.raises(ValidationError):
        admin_economy.GiftAllCreateRequest(amount=1.5)
    ok = admin_economy.GiftAllCreateRequest(amount=50)
    assert ok.amount == 50 and ok.scope == "all"


def test_preview_rejects_bad_amount_and_scope(db):
    admin = _make_admin(db)
    with pytest.raises(HTTPException) as exc:
        admin_economy.gift_all_preview(amount=0, scope="all",
                                       current_admin=admin, db=db)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        admin_economy.gift_all_preview(amount=10, scope="vip",
                                       current_admin=admin, db=db)
    assert exc.value.status_code == 400


# ==================== 2. 预览计数（scope 过滤） ====================

def test_preview_counts_by_scope(db):
    admin = _make_admin(db)
    _seed_users(db)
    total_users = db.query(models.WebUser).count()  # 含管理员本人一行

    all_prev = admin_economy.gift_all_preview(amount=30, scope="all",
                                              current_admin=admin, db=db)
    assert all_prev["user_count"] == total_users
    assert all_prev["total_amount"] == total_users * 30

    wel_prev = admin_economy.gift_all_preview(amount=30, scope="welfare",
                                              current_admin=admin, db=db)
    # 只有有效期内的（含永久）公益用户：过期资格不算
    assert wel_prev["user_count"] == 2
    assert wel_prev["total_amount"] == 60


# ==================== 3. 建批：落参数快照 + 审计 ====================

def test_create_batch_pending_snapshot_and_audit(db):
    admin = _make_admin(db)
    _seed_users(db)
    total_users = db.query(models.WebUser).count()

    out = admin_economy.gift_all_create_batch(
        request=admin_economy.GiftAllCreateRequest(
            amount=25, scope="all", reason="国庆福利"),
        current_admin=admin, db=db)

    assert out["status"] == "pending"
    assert out["amount"] == 25 and out["scope"] == "all"
    assert out["reason"] == "国庆福利"
    assert out["target_count"] == total_users
    assert out["granted_count"] == 0

    batch = db.query(models.PointsGiftBatch).filter(
        models.PointsGiftBatch.id == out["id"]).one()
    assert batch.created_by == admin.id
    # 建批只落批次，不同步发分
    assert _gift_logs(db) == []
    assert db.query(models.WebUser).filter(
        models.WebUser.username == "plain1").one().points == 0

    logs = db.query(models.AdminLog).filter(
        models.AdminLog.action == "economy_gift_all_create").all()
    assert len(logs) == 1
    assert logs[0].target_id == batch.id
    assert logs[0].details["amount"] == 25


# ==================== 4. 发放执行：全员到账、台账齐 ====================

def test_process_grants_everyone_once(db):
    admin = _make_admin(db)
    users = _seed_users(db)
    out = admin_economy.gift_all_create_batch(
        request=admin_economy.GiftAllCreateRequest(
            amount=30, scope="all", reason="系统维护补偿"),
        current_admin=admin, db=db)
    batch_id = out["id"]

    gift_all.process_pending_batches(db)

    batch = db.query(models.PointsGiftBatch).filter(
        models.PointsGiftBatch.id == batch_id).one()
    assert batch.status == "done"
    assert batch.granted_count == out["target_count"]
    assert batch.finished_at is not None

    total_users = db.query(models.WebUser).count()
    logs = _gift_logs(db, batch_id)
    assert len(logs) == total_users
    for log in logs:
        assert log.amount == 30
        assert log.description == "系统维护补偿"
    for u in users:
        db.refresh(u)
        assert u.points == 30


def test_process_welfare_scope_only_active_welfare(db):
    admin = _make_admin(db)
    plain1, plain2, wel_ok, wel_perm, wel_exp = _seed_users(db)
    out = admin_economy.gift_all_create_batch(
        request=admin_economy.GiftAllCreateRequest(
            amount=10, scope="welfare", reason="公益专属"),
        current_admin=admin, db=db)

    gift_all.process_pending_batches(db)

    for u, want in ((wel_ok, 10), (wel_perm, 10), (wel_exp, 0),
                    (plain1, 0), (plain2, 0)):
        db.refresh(u)
        assert u.points == want, u.username
    batch = db.query(models.PointsGiftBatch).filter(
        models.PointsGiftBatch.id == out["id"]).one()
    assert batch.status == "done" and batch.granted_count == 2
    # 预览计数与实际发放一致
    prev = admin_economy.gift_all_preview(amount=10, scope="welfare",
                                          current_admin=admin, db=db)
    assert prev["user_count"] == batch.granted_count


# ==================== 5. 幂等：重跑不重发、失败续跑补齐 ====================

def test_rerun_never_double_grants(db):
    admin = _make_admin(db)
    _seed_users(db)
    out = admin_economy.gift_all_create_batch(
        request=admin_economy.GiftAllCreateRequest(amount=15, scope="all"),
        current_admin=admin, db=db)

    gift_all.process_pending_batches(db)
    gift_all.process_pending_batches(db)  # 整轮重跑
    gift_all.process_batch(db, out["id"])  # 单批重跑

    total_users = db.query(models.WebUser).count()
    assert len(_gift_logs(db, out["id"])) == total_users
    for u in db.query(models.WebUser).all():
        assert u.points == 15


def test_resume_after_mid_failure(db, monkeypatch):
    admin = _make_admin(db)
    _seed_users(db)
    out = admin_economy.gift_all_create_batch(
        request=admin_economy.GiftAllCreateRequest(amount=20, scope="all"),
        current_admin=admin, db=db)
    batch_id = out["id"]

    real_add = gift_all._add_points
    calls = {"n": 0}

    def flaky(db_, user, amount, type_, description, ref_id=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模拟发放中途故障")
        return real_add(db_, user, amount, type_, description, ref_id)

    monkeypatch.setattr(gift_all, "_add_points", flaky)
    gift_all.process_pending_batches(db)

    batch = db.query(models.PointsGiftBatch).filter(
        models.PointsGiftBatch.id == batch_id).one()
    assert batch.status == "failed"
    assert batch.granted_count == 2  # 前两人已到账、已落明细
    assert len(_gift_logs(db, batch_id)) == 2

    monkeypatch.setattr(gift_all, "_add_points", real_add)
    gift_all.process_pending_batches(db)  # 续跑：failed 批次被重新认领

    batch = db.query(models.PointsGiftBatch).filter(
        models.PointsGiftBatch.id == batch_id).one()
    total_users = db.query(models.WebUser).count()
    assert batch.status == "done"
    assert batch.granted_count == total_users
    assert len(_gift_logs(db, batch_id)) == total_users
    for u in db.query(models.WebUser).all():
        assert u.points == 20  # 每人恰好一份，没有重发


# ==================== 6. 批次列表回读 ====================

def test_list_batches_readback(db):
    admin = _make_admin(db)
    _seed_users(db)
    out = admin_economy.gift_all_create_batch(
        request=admin_economy.GiftAllCreateRequest(
            amount=7, scope="welfare", reason="回读校验"),
        current_admin=admin, db=db)

    listed = admin_economy.gift_all_list_batches(limit=50,
                                                 current_admin=admin, db=db)
    rows = [b for b in listed["batches"] if b["id"] == out["id"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["amount"] == 7 and row["scope"] == "welfare"
    assert row["reason"] == "回读校验" and row["status"] == "pending"
    assert row["created_by_name"] == admin.username


# ==================== 7. 台账来源分类登记 ====================

def test_gift_all_type_registered_as_not_self_earned():
    from backend import points as points_mod
    assert points_mod.POINT_TYPE_SELF_EARNED["gift_all"] is False

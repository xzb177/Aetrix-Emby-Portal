# -*- coding: utf-8 -*-
"""白名单（永久）账号独一无二体验包·特权组回归测试（2026-11-02 用户亲点）。

三处特权，判定一律复用 codes 口径（is_whitelist_account /
whitelist_account_ids：核销痕迹＋三处永久现算），不另立名单、不加开关：
1. 求片 VIP：月度额度豁免（公益 3 条/月与付费额度都不拦），提交口与
   额度快照（media_seek.monthly_quota）同一口径；每日防刷上限不在此列。
2. 求片置顶：管理端求片队列（admin.get_media_seeks）里白名单用户的
   请求置顶（默认序与 order=hot 都是），普通用户相对序不变。
3. 转存 VIP：claim_due_jobs / _pick_candidates 里白名单 owner 最高
   优先级（同轮先行），普通 owner 仍按原公平序（在途数→最老等待→键）
   推进、不饿死；vip_owners=None 时排序逐字不变。
4. 红包/转让免手续费：白名单发送方 fee=0（与 fee_pct 配置为 0 时
   逐字一致：不写 fee 台账行、返回 fee=0），普通用户仍 5%。
造数一律走真实核销路径（codes.redeem_code），与 #1668/#1670 同批同结果。
"""
import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import database as _dbmod  # noqa: E402

_dbmod.engine = create_engine(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(sessionmaker(bind=_dbmod.engine))
# 换绑后立刻建全表：防组合跑时后导入文件的 fixture 撞 no such table
# 级联毒化（与 test_whitelist_vitality_exempt 同款纪律）。
_dbmod.init_db()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from backend import codes, media_seek, models, welfare_redpacket  # noqa: E402
from backend.api import admin as admin_api  # noqa: E402
from backend.api import economy as economy_api  # noqa: E402
from backend.api import user as user_api  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import transfer_variant as tv  # noqa: E402
from backend.integrations import store as _store  # noqa: E402


@pytest.fixture()
def db():
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    _store.invalidate()
    tv._paused_until.clear()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        _store.invalidate()
        tv._paused_until.clear()


def _user(db, username, **kw):
    fields = {"password_hash": "x", "is_active": True}
    fields.update(kw)
    u = models.WebUser(username=username, **fields)
    db.add(u)
    db.flush()
    return u


def _whitelist_user(db, username, **kw):
    """走真实核销路径把账号变成白名单（永久）账号。"""
    u = _user(db, username, **kw)
    c = models.RegistrationCode(
        code=f"VIP-{username.upper()[:12]:0<12}",
        max_uses=1, use_count=0, is_active=True,
        code_type=codes.CODE_TYPE_WHITELIST, days=-1,
        reward_type="subscription")
    db.add(c)
    db.commit()
    result = codes.redeem_code(db, u, c.code)
    assert result["success"] is True
    db.refresh(u)
    return u


def _seek_row(db, user, name, *, created_at=None, status="pending",
              vote_count=0):
    row = models.MovieRequest(
        user_id=user.id, movie_name=name, type="movie", status=status,
        vote_count=vote_count)
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ==================== 1. 求片 VIP：月度额度豁免 ====================

def test_monthly_quota_exempt_for_whitelist(db):
    vip = _whitelist_user(db, "vip_quota")
    for i in range(3):
        _seek_row(db, vip, f"已求片{i}")
    snap = media_seek.monthly_quota(db, vip)
    # 公益口径月度上限仍是 3（快照如实），但白名单豁免：剩余不被用满卡死
    assert snap["monthly_limit"] == media_seek.DEFAULT_MONTHLY_WELFARE
    assert snap["monthly_used"] == 3
    assert snap["monthly_exempt"] is True
    assert snap["monthly_remaining"] > 0


def test_monthly_quota_normal_welfare_user_not_exempt(db):
    plain = _user(db, "plain_quota", is_welfare=True, welfare_expires_at=None)
    db.commit()
    for i in range(3):
        _seek_row(db, plain, f"已求片{i}")
    snap = media_seek.monthly_quota(db, plain)
    assert snap["monthly_exempt"] is False
    assert snap["monthly_remaining"] == 0


def test_submit_beyond_monthly_limit_whitelist_passes_normal_blocked(db):
    vip = _whitelist_user(db, "vip_submit")
    plain = _user(db, "plain_submit", is_welfare=True,
                  welfare_expires_at=None)
    db.commit()
    for i in range(3):
        _seek_row(db, vip, f"白名单已求{i}")
        _seek_row(db, plain, f"普通已求{i}")

    def _payload(name):
        return user_api.MediaSeekRequest(movie_name=name, type="movie")

    # 普通公益用户：第 4 条撞月度额度 429（每日上限 5 未到，卡的是月度）
    with pytest.raises(HTTPException) as exc:
        user_api._create_media_seek_sync(db, plain, "普通第四条",
                                         _payload("普通第四条"))
    assert exc.value.status_code == 429
    assert "本月" in str(exc.value.detail)

    # 白名单用户：同样的第 4 条放行落库
    rid = user_api._create_media_seek_sync(db, vip, "白名单第四条",
                                           _payload("白名单第四条"))
    row = db.query(models.MovieRequest).filter(
        models.MovieRequest.id == rid).first()
    assert row is not None and row.status == "pending"


# ==================== 2. 求片置顶：管理端队列 ====================

def test_admin_queue_pins_whitelist_first(db):
    vip = _whitelist_user(db, "vip_pin")
    plain = _user(db, "plain_pin")
    db.commit()
    now = datetime.now()
    vip_row = _seek_row(db, vip, "白名单的片",
                        created_at=now - timedelta(hours=3))
    _seek_row(db, plain, "普通较新的片", created_at=now - timedelta(hours=1))
    _seek_row(db, plain, "普通最新的片", created_at=now)

    rows = admin_api.get_media_seeks(
        status_filter="pending", realm_id=0, order=None,
        current_admin=plain, db=db)
    assert rows[0]["user_id"] == vip.id and rows[0]["id"] == vip_row.id


def test_admin_queue_hot_order_still_pins_whitelist(db):
    vip = _whitelist_user(db, "vip_hot")
    plain = _user(db, "plain_hot")
    db.commit()
    now = datetime.now()
    _seek_row(db, vip, "白名单冷门片", created_at=now - timedelta(hours=2),
              vote_count=0)
    _seek_row(db, plain, "普通热门片", created_at=now, vote_count=9)

    rows = admin_api.get_media_seeks(
        status_filter="pending", realm_id=0, order="hot",
        current_admin=plain, db=db)
    assert rows[0]["user_id"] == vip.id


def test_admin_queue_order_unchanged_without_whitelist(db):
    plain = _user(db, "plain_only")
    db.commit()
    now = datetime.now()
    older = _seek_row(db, plain, "较旧的片",
                      created_at=now - timedelta(hours=2))
    newer = _seek_row(db, plain, "较新的片", created_at=now)

    rows = admin_api.get_media_seeks(
        status_filter="pending", realm_id=0, order=None,
        current_admin=plain, db=db)
    # 无白名单时与现行逐字一致：created_at 降序
    assert [r["id"] for r in rows] == [newer.id, older.id]


# ==================== 3. 转存 VIP：调度优先级 ====================

def _job(db, owner_ref, *, created_ago_sec=0, trigger="playback"):
    row = em.TransferJob(
        item_id=1, trigger=trigger, drive_id="drive1",
        owner_scope="user", owner_ref=str(owner_ref), status="pending")
    db.add(row)
    db.commit()
    if created_ago_sec:
        row.created_at = datetime.now() - timedelta(seconds=created_ago_sec)
        db.commit()
    db.refresh(row)
    return row


def test_pick_candidates_vip_first_but_normal_not_starved(db):
    vip_job1 = _job(db, "777", created_ago_sec=10)
    vip_job2 = _job(db, "777", created_ago_sec=5)
    normal_job = _job(db, "888", created_ago_sec=3600)
    picked = tv._pick_candidates(
        [vip_job1, vip_job2, normal_job], 2, {}, 10,
        vip_owners={("user", "777")})
    # 同轮内白名单先行，普通单同轮仍拿到名额（不饿死）
    assert picked == [vip_job1, normal_job]


def test_pick_candidates_vip_internal_fairness_kept(db):
    # 两名白名单 owner：在途多的排后（档内仍按原公平键）
    busy_vip = _job(db, "777", created_ago_sec=3600)
    idle_vip = _job(db, "999", created_ago_sec=10)
    picked = tv._pick_candidates(
        [busy_vip, idle_vip], 1, {("user", "777"): 2}, 10,
        vip_owners={("user", "777"), ("user", "999")})
    assert picked == [idle_vip]


def test_pick_candidates_without_vip_unchanged(db):
    older_owner_job = _job(db, "888", created_ago_sec=3600)
    newer_owner_job = _job(db, "777", created_ago_sec=10)
    picked = tv._pick_candidates(
        [newer_owner_job, older_owner_job], 1, {}, 10)
    # 无 VIP 集时现行公平序：最老等待的 owner 先
    assert picked == [older_owner_job]


def test_claim_due_jobs_claims_whitelist_first(db):
    vip = _whitelist_user(db, "vip_claim")
    plain = _user(db, "plain_claim")
    db.commit()
    vip_job = _job(db, vip.id, created_ago_sec=10)
    _job(db, plain.id, created_ago_sec=3600)
    claimed = tv.claim_due_jobs(db, limit=1)
    # 名额只有 1 个：普通公平序会先认领等得久的普通单，
    # 白名单 VIP 必须抢到这一个名额
    assert claimed == [vip_job.id]


# ==================== 4. 红包 / 转让免手续费 ====================

def _fee_logs(db, user_id, log_type):
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user_id,
        models.PointsLog.type == log_type).all()


def test_transfer_fee_zero_for_whitelist(db):
    vip = _whitelist_user(db, "vip_send", points=1000)
    recv = _user(db, "recv_a")
    db.commit()
    result = economy_api.transfer_points_core(db, vip, "recv_a", 100)
    assert result["fee"] == 0
    db.refresh(vip)
    assert vip.points == 900  # 只扣本金，无手续费
    assert _fee_logs(db, vip.id, "transfer_fee") == []


def test_transfer_fee_normal_user_still_charged(db):
    plain = _user(db, "plain_send", points=1000)
    _user(db, "recv_b")
    db.commit()
    result = economy_api.transfer_points_core(db, plain, "recv_b", 100)
    assert result["fee"] == 5  # 100 × 5%
    db.refresh(plain)
    assert plain.points == 895
    fees = _fee_logs(db, plain.id, "transfer_fee")
    assert len(fees) == 1 and fees[0].amount == -5


def test_redpacket_fee_zero_for_whitelist(db):
    vip = _whitelist_user(db, "vip_packet", points=1000)
    db.commit()
    welfare_redpacket.send_packet(db, vip, 100, 5)
    db.refresh(vip)
    assert vip.points == 900  # 只扣红包本体
    assert _fee_logs(db, vip.id, "redpacket_fee") == []


def test_redpacket_fee_normal_user_still_charged(db):
    plain = _user(db, "plain_packet", points=1000)
    db.commit()
    welfare_redpacket.send_packet(db, plain, 100, 5)
    db.refresh(plain)
    assert plain.points == 895
    fees = _fee_logs(db, plain.id, "redpacket_fee")
    assert len(fees) == 1 and fees[0].amount == -5

"""等级治理 Step 3：活动提案与直接发起（backend.lottery_governance）隔离测试。

沿用 tests/test_lottery_round.py 的内存 SQLite + 只建表模式。
覆盖：各等级提案/发起权限、月限、提案批准流（批准建轮次/拒绝/重复处理）、
事后审计记录（提案行/直接发起行/AdminLog）、free realm 门、原有抽奖路径不受影响、
提案动作不产生经验。
"""
import sys

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base  # noqa: F401 保持与其它测试相同的导入习惯
from backend import lottery_governance as gov
from backend import models


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # 只建需要的表
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.ServerRealm.__table__.create(engine, checkfirst=True)
    models.MemberLevel.__table__.create(engine, checkfirst=True)
    models.LotteryRound.__table__.create(engine, checkfirst=True)
    models.LotteryRoundPrize.__table__.create(engine, checkfirst=True)
    models.LotteryActivityProposal.__table__.create(engine, checkfirst=True)
    models.LotteryDirectLaunch.__table__.create(engine, checkfirst=True)
    # 停权接线后 submit/direct 入口会读停权表，未建表会在此夹具下报缺表。
    models.GovernanceFreeze.__table__.create(engine, checkfirst=True)
    models.AdminLog.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _set_config(db, key, value):
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _make_realm(db, access_mode="free"):
    realm = models.ServerRealm(
        name="公益服" if access_mode == "free" else "付费服",
        slug="main",
        access_mode=access_mode,
        is_active=True,
    )
    db.add(realm)
    db.commit()
    db.refresh(realm)
    _set_config(db, "active_realm_id", str(realm.id))
    return realm


def _make_levels(db):
    for level, threshold in [(1, 0), (2, 100), (3, 500), (4, 1500), (5, 5000), (6, 15000)]:
        db.add(
            models.MemberLevel(
                level=level,
                name=f"Lv.{level}",
                xp_threshold=threshold,
                discount_pct=0,
                is_active=True,
            )
        )
    db.commit()


def _make_user(db, username, xp=0, tg_id=None):
    user = models.WebUser(
        username=username,
        password_hash="",
        telegram_id=tg_id if tg_id is not None else abs(hash(username)) % 10**9,
        member_xp=xp,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _enable_lottery(db, groups="999"):
    _set_config(db, "lottery_enabled", "1")
    _set_config(db, "lottery_group_ids", groups)


def _payload(**over):
    p = {
        "title": "用户提案活动",
        "chat_id": 999,
        "prizes": [{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
        "draw_at": None,
        "max_participants": None,
        "lottery_type": "button",
        "password_keyword": None,
    }
    p.update(over)
    return p


def _setup_free(db):
    _make_realm(db, "free")
    _make_levels(db)
    _enable_lottery(db)


# ==================== 等级判定 ====================


def test_user_level_thresholds(db):
    _setup_free(db)
    cases = [(0, 1), (99, 1), (100, 2), (499, 2), (500, 3), (1499, 3),
             (1500, 4), (4999, 4), (5000, 5), (14999, 5), (15000, 6), (10**9, 6)]
    for xp, expected in cases:
        user = _make_user(db, f"u{xp}", xp=xp)
        assert gov.user_level(db, user.id) == expected


def test_user_level_falls_back_to_defaults_when_table_empty(db):
    _make_realm(db, "free")
    _enable_lottery(db)
    # 不建 MemberLevel 行：回落 DEFAULT_LEVELS（0/100/500/1500/5000/15000）
    user = _make_user(db, "lv5", xp=5000)
    assert gov.user_level(db, user.id) == 5


def test_user_level_does_not_write_back(db):
    _setup_free(db)
    user = _make_user(db, "lv5", xp=5000)
    assert gov.user_level(db, user.id) == 5
    db.refresh(user)
    # 纯计算：不写回 member_level 缓存列（该列仍是建用户时的默认值 1）
    assert user.member_level == 1


# ==================== 提案提交 ====================


def test_submit_lv1_forbidden(db):
    _setup_free(db)
    user = _make_user(db, "lv1", xp=0)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, user.id, _payload())
    assert ei.value.status_code == 403


def test_submit_lv2_ok_pending_no_round_no_xp(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    proposal = gov.submit_proposal(db, user.id, _payload())
    assert proposal.status == "pending"
    assert proposal.level_snapshot == 2
    assert proposal.round_id is None
    assert proposal.chat_id == 999
    assert db.query(models.LotteryRound).count() == 0
    db.refresh(user)
    assert user.member_xp == 100  # 提案不产生经验


def test_submit_paid_realm_forbidden(db):
    _make_realm(db, "paid")
    _make_levels(db)
    _enable_lottery(db)
    user = _make_user(db, "lv2", xp=100)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, user.id, _payload())
    assert ei.value.status_code == 403


def test_submit_lottery_disabled_forbidden(db):
    _setup_free(db)
    _set_config(db, "lottery_enabled", "0")
    user = _make_user(db, "lv2", xp=100)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, user.id, _payload())
    assert ei.value.status_code == 403


def test_submit_group_not_allowed_forbidden(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, user.id, _payload(chat_id=777))
    assert ei.value.status_code == 403


def test_submit_auto_chat_single_group(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    proposal = gov.submit_proposal(db, user.id, _payload(chat_id=None))
    assert proposal.chat_id == 999


def test_submit_auto_chat_ambiguous_rejected(db):
    _setup_free(db)
    _set_config(db, "lottery_group_ids", "999,888")
    user = _make_user(db, "lv2", xp=100)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, user.id, _payload(chat_id=None))
    assert ei.value.status_code == 400


def test_submit_monthly_cap_lv2_one_lv4_two(db):
    _setup_free(db)
    lv2 = _make_user(db, "lv2", xp=100)
    gov.submit_proposal(db, lv2.id, _payload(title="提案1"))
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, lv2.id, _payload(title="提案2"))
    assert ei.value.status_code == 400

    lv4 = _make_user(db, "lv4", xp=1500)
    gov.submit_proposal(db, lv4.id, _payload(title="提案A"))
    gov.submit_proposal(db, lv4.id, _payload(title="提案B"))
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, lv4.id, _payload(title="提案C"))
    assert ei.value.status_code == 400


def test_submit_rejected_proposal_still_counts_toward_cap(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    admin = _make_user(db, "admin", xp=0)
    proposal = gov.submit_proposal(db, user.id, _payload())
    gov.reject_proposal(db, admin.id, proposal.id)
    # 被拒绝的提案仍占月限（防 spam 刷提案）
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, user.id, _payload(title="再提"))
    assert ei.value.status_code == 400


def test_submit_payload_validation_messages(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    cases = [
        (_payload(title="  "), "活动标题不能为空"),
        (_payload(prizes=[]), "至少需要配置 1 个奖品"),
        (_payload(prizes=[{"name": "x", "type": "gold", "value": 1, "quantity": 1}]),
         "奖品类型只能是 days/points/whitelist"),
        (_payload(prizes=[{"name": "x", "type": "points", "value": 0, "quantity": 1}]),
         "积分奖品的价值必须大于 0"),
        (_payload(prizes=[{"name": "x", "type": "days", "value": 0, "quantity": 1}]),
         "天数奖品的价值必须大于 0"),
        (_payload(prizes=[{"name": "x", "type": "whitelist", "value": -1, "quantity": 1}]),
         "奖品价值不能为负数"),
        (_payload(prizes=[{"name": "x", "type": "points", "value": 1, "quantity": 0}]),
         "奖品数量至少为 1"),
        (_payload(lottery_type="password", password_keyword=None), "口令抽奖需要设置口令"),
        (_payload(max_participants=0), "最大参与人数至少为 1"),
        (_payload(draw_at="not-a-date"), "draw_at 时间格式无效"),
    ]
    for payload, message in cases:
        with pytest.raises(gov.LotteryGovernanceError) as ei:
            gov.submit_proposal(db, user.id, payload)
        assert ei.value.status_code == 400
        assert ei.value.detail == message


# ==================== 提案批准 / 拒绝 ====================


def test_approve_creates_round_and_audit(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    admin = _make_user(db, "admin", xp=0)
    proposal = gov.submit_proposal(db, user.id, _payload())
    approved, round_obj = gov.approve_proposal(db, admin.id, proposal.id, note="通过")
    assert approved.status == "approved"
    assert approved.round_id == round_obj.id
    assert approved.reviewer_id == admin.id
    assert approved.reviewed_at is not None
    assert round_obj.status == "open"
    assert round_obj.created_by == user.id  # 轮次归属提案人，不是审批人
    assert round_obj.chat_id == 999
    prizes = db.query(models.LotteryRoundPrize).filter_by(round_id=round_obj.id).all()
    assert len(prizes) == 1 and prizes[0].type == "points" and prizes[0].value == 100
    # AdminLog 审计行
    logs = db.query(models.AdminLog).filter_by(action="lottery_proposal_approve").all()
    assert len(logs) == 1
    assert logs[0].admin_user_id == admin.id
    assert logs[0].target_id == proposal.id
    db.refresh(user)
    assert user.member_xp == 100  # 批准不产生经验


def test_approve_twice_rejected(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    admin = _make_user(db, "admin", xp=0)
    proposal = gov.submit_proposal(db, user.id, _payload())
    gov.approve_proposal(db, admin.id, proposal.id)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.approve_proposal(db, admin.id, proposal.id)
    assert ei.value.status_code == 400


def test_approve_missing_proposal_404(db):
    _setup_free(db)
    admin = _make_user(db, "admin", xp=0)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.approve_proposal(db, admin.id, 99999)
    assert ei.value.status_code == 404


def test_approve_active_round_conflict(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    admin = _make_user(db, "admin", xp=0)
    proposal = gov.submit_proposal(db, user.id, _payload())
    # 批准前同群已有进行中活动（另一轮次）-> 批准被拒，提案仍 pending
    from backend import lottery

    lottery.create_round(db, title="已有活动", chat_id=999, prizes=_payload()["prizes"])
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.approve_proposal(db, admin.id, proposal.id)
    assert ei.value.status_code == 400
    db.refresh(proposal)
    assert proposal.status == "pending"


def test_approve_paid_realm_forbidden(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    admin = _make_user(db, "admin", xp=0)
    proposal = gov.submit_proposal(db, user.id, _payload())
    # 服切回付费后批准被拒（提案本身不被删除）
    realm = db.query(models.ServerRealm).first()
    realm.access_mode = "paid"
    db.commit()
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.approve_proposal(db, admin.id, proposal.id)
    assert ei.value.status_code == 403


def test_reject_flow_and_audit(db):
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    admin = _make_user(db, "admin", xp=0)
    proposal = gov.submit_proposal(db, user.id, _payload())
    rejected = gov.reject_proposal(db, admin.id, proposal.id, note="奖品不合理")
    assert rejected.status == "rejected"
    assert rejected.review_note == "奖品不合理"
    assert rejected.round_id is None
    assert db.query(models.LotteryRound).count() == 0
    logs = db.query(models.AdminLog).filter_by(action="lottery_proposal_reject").all()
    assert len(logs) == 1
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.reject_proposal(db, admin.id, proposal.id)
    assert ei.value.status_code == 400


def test_approve_draw_at_not_shifted(db):
    """批准时 draw_at 不漂移：入库的上海 naive 时间原样进轮次（回归 +8h 隐患）。

    提交串为 aware UTC 04:00 -> 入库上海 naive 12:00；批准若把已入库值再转一次，
    轮次会漂成 20:00，本测试钉死 12:00。
    """
    _setup_free(db)
    user = _make_user(db, "lv2", xp=100)
    admin = _make_user(db, "admin", xp=0)
    proposal = gov.submit_proposal(
        db, user.id, _payload(draw_at="2026-11-01T04:00:00+00:00")
    )
    assert (proposal.draw_at.hour, proposal.draw_at.minute) == (12, 0)
    _, round_obj = gov.approve_proposal(db, admin.id, proposal.id)
    assert round_obj.draw_at is not None
    assert (round_obj.draw_at.hour, round_obj.draw_at.minute) == (12, 0)


# ==================== 直接发起 ====================


def test_direct_lv4_forbidden_lv5_ok(db):
    _setup_free(db)
    lv4 = _make_user(db, "lv4", xp=1500)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, lv4.id, _payload())
    assert ei.value.status_code == 403

    lv5 = _make_user(db, "lv5", xp=5000)
    round_obj, record = gov.direct_launch(db, lv5.id, _payload())
    assert round_obj.status == "open"
    assert round_obj.created_by == lv5.id
    assert record.round_id == round_obj.id
    assert record.level_snapshot == 5
    db.refresh(lv5)
    assert lv5.member_xp == 5000  # 发起不产生经验


def test_direct_monthly_cap_lv5_one_lv6_two(db):
    _setup_free(db)
    lv5 = _make_user(db, "lv5", xp=5000)
    gov.direct_launch(db, lv5.id, _payload(title="直发1"))
    # 第一轮仍 open 时第二发会被同群冲突拦——先取消它，再测月限
    from backend import lottery

    rnd = db.query(models.LotteryRound).filter_by(title="直发1").first()
    rnd.status = "cancelled"
    db.commit()
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, lv5.id, _payload(title="直发2"))
    assert ei.value.status_code == 400  # 月限（不是同群冲突）

    lv6 = _make_user(db, "lv6", xp=15000)
    gov.direct_launch(db, lv6.id, _payload(title="直发A"))
    rnd = db.query(models.LotteryRound).filter_by(title="直发A").first()
    rnd.status = "cancelled"
    db.commit()
    gov.direct_launch(db, lv6.id, _payload(title="直发B"))
    rnd = db.query(models.LotteryRound).filter_by(title="直发B").first()
    rnd.status = "cancelled"
    db.commit()
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, lv6.id, _payload(title="直发C"))
    assert ei.value.status_code == 400


def test_direct_paid_realm_forbidden(db):
    _make_realm(db, "paid")
    _make_levels(db)
    _enable_lottery(db)
    user = _make_user(db, "lv5", xp=5000)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, user.id, _payload())
    assert ei.value.status_code == 403


def test_direct_active_round_conflict(db):
    _setup_free(db)
    user = _make_user(db, "lv5", xp=5000)
    from backend import lottery

    lottery.create_round(db, title="已有活动", chat_id=999, prizes=_payload()["prizes"])
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, user.id, _payload())
    assert ei.value.status_code == 400


# ==================== 列表与路由注册 ====================


def test_list_proposals_user_and_admin_scopes(db):
    _setup_free(db)
    u1 = _make_user(db, "u1", xp=100)
    u2 = _make_user(db, "u2", xp=100)
    gov.submit_proposal(db, u1.id, _payload(title="u1提案"))
    gov.submit_proposal(db, u2.id, _payload(title="u2提案"))
    total, items = gov.list_proposals(db, user_id=u1.id)
    assert total == 1 and items[0]["title"] == "u1提案"
    total, items = gov.list_proposals(db, user_id=None, status="pending")
    assert total == 2
    total, items = gov.list_proposals(db, user_id=None, status="approved")
    assert total == 0


def test_config_cap_override(db):
    _setup_free(db)
    _set_config(db, "lottery_governance_proposal_cap_base", "2")
    user = _make_user(db, "lv2", xp=100)
    gov.submit_proposal(db, user.id, _payload(title="提案1"))
    gov.submit_proposal(db, user.id, _payload(title="提案2"))
    with pytest.raises(gov.LotteryGovernanceError):
        gov.submit_proposal(db, user.id, _payload(title="提案3"))
    # 非法键值回落默认值 1
    _set_config(db, "lottery_governance_proposal_cap_base", "not-a-number")
    lv2b = _make_user(db, "lv2b", xp=100)
    gov.submit_proposal(db, lv2b.id, _payload(title="提案X"))
    with pytest.raises(gov.LotteryGovernanceError):
        gov.submit_proposal(db, lv2b.id, _payload(title="提案Y"))


def test_routes_registered():
    """端点已挂上路由（用户端已 include 的 lottery 路由 + 管理端 admin 路由）。"""
    from backend.api.lottery import router as lottery_router
    import backend.api.welfare_admin  # noqa: F401 导入即执行装饰器，把端点挂上 admin_router
    from backend.api.admin_core import admin_router

    lottery_paths = {(r.path, tuple(r.methods or [])) for r in lottery_router.routes}
    assert any(p == "/api/lottery/proposals" and "POST" in m for p, m in lottery_paths)
    assert any(p == "/api/lottery/proposals/mine" and "GET" in m for p, m in lottery_paths)
    assert any(p == "/api/lottery/rounds/direct" and "POST" in m for p, m in lottery_paths)
    # admin_router 自带 /api/admin 前缀（main.py 挂载口径）
    admin_paths = {(r.path, tuple(r.methods or [])) for r in admin_router.routes}
    assert any(
        p == "/api/admin/welfare/lottery/proposals" and "GET" in m for p, m in admin_paths
    )
    assert any(
        p == "/api/admin/welfare/lottery/proposals/{proposal_id}/approve" and "POST" in m
        for p, m in admin_paths
    )
    assert any(
        p == "/api/admin/welfare/lottery/proposals/{proposal_id}/reject" and "POST" in m
        for p, m in admin_paths
    )

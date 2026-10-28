# -*- coding: utf-8 -*-
"""等级治理停权接线测试：Step 4 的 is_governance_frozen 接进 Step 1/2/3 用户侧入口。

语义（Step 4 定版，见 backend/governance.py 模块注释）：
- Step 1 附议：停权只冻治理特权、保留基础附议 1 票——被停权用户新附议的
  weight_snapshot 强制为 1，不按等级权重；取消附议不受停权影响；历史票不追溯。
- Step 2 举报与复核：被停权用户新建举报、参与复核投票均被拒绝（403）；
  与 Step 2 自身的自动暂停（GovernanceSuspension）是两套独立机制，互不替代。
- Step 3 提案与直接发起：被停权用户提交提案、直接发起均被拒绝（403）。
- 不动项：管理端审批、只读路径、tg_bot /request 均不在本文件覆盖（无接线）。

全部用隔离的内存 SQLite＋全量建表；fixture 在 setup/teardown 双向失效
进程内 store 缓存（TTL 缓存不分库，前序文件污染曾导致 #1457 全量失败）。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import governance, governance_report as report_svc
from backend import lottery_governance as lottery_gov
from backend import media_seek, models, subscriptions
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    previous = subscriptions._realm_resolver
    subscriptions.set_process_realm_resolver(None)
    try:
        yield session
    finally:
        subscriptions.set_process_realm_resolver(previous)
        store.invalidate()
        session.close()


def _user(db, username, level=1, xp=0):
    user = models.WebUser(
        username=username, password_hash="x", is_active=True,
        member_level=level, member_xp=xp,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _admin(db, username="admin"):
    return _user(db, username)


def _freeze(db, admin, user):
    governance.freeze_governance(
        db, user_id=user.id, reason="接线测试停权", days=None, admin_id=admin.id,
    )
    db.commit()


def _free_realm(db):
    realm = models.ServerRealm(
        name="公益服", slug="main", access_mode="free", is_active=True,
    )
    db.add(realm)
    db.commit()
    db.refresh(realm)
    return realm


def _set_config(db, key, value):
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        db.add(models.SystemConfig(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def _seek(db, user, realm_id):
    row = models.MovieRequest(
        user_id=user.id, movie_name="测试影片", type="movie",
        status="pending", realm_id=realm_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _vote_row(db, request_id, user_id):
    return db.query(models.MovieRequestVote).filter(
        models.MovieRequestVote.request_id == request_id,
        models.MovieRequestVote.user_id == user_id,
    ).first()


def _lottery_payload(**over):
    p = {
        "title": "接线测试活动",
        "chat_id": 999,
        "prizes": [{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
        "draw_at": None,
        "max_participants": None,
        "lottery_type": "button",
        "password_keyword": None,
    }
    p.update(over)
    return p


def _lottery_setup_free(db):
    realm = _free_realm(db)
    _set_config(db, "active_realm_id", realm.id)
    _set_config(db, "lottery_enabled", "1")
    _set_config(db, "lottery_group_ids", "999")
    return realm


# ==================== Step 1 附议 ====================


def test_vote_frozen_user_weight_forced_to_one(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    voter = _user(db, "voter6", level=6)
    admin = _admin(db)
    seek = _seek(db, owner, realm.id)
    _freeze(db, admin, voter)
    out = media_seek.toggle_vote(db, voter, seek.id)
    assert out["voted"] is True
    assert out["weighted_vote_count"] == 1
    assert _vote_row(db, seek.id, voter.id).weight_snapshot == 1


def test_vote_unfrozen_user_weight_by_level(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    voter = _user(db, "voter6", level=6)
    seek = _seek(db, owner, realm.id)
    out = media_seek.toggle_vote(db, voter, seek.id)
    assert out["weighted_vote_count"] == 6
    assert _vote_row(db, seek.id, voter.id).weight_snapshot == 6


def test_vote_cancel_not_blocked_by_freeze(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    voter = _user(db, "voter6", level=6)
    admin = _admin(db)
    seek = _seek(db, owner, realm.id)
    media_seek.toggle_vote(db, voter, seek.id)
    _freeze(db, admin, voter)
    out = media_seek.toggle_vote(db, voter, seek.id)
    assert out["voted"] is False
    assert _vote_row(db, seek.id, voter.id) is None


def test_vote_history_not_rewritten_by_freeze(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    voter = _user(db, "voter6", level=6)
    admin = _admin(db)
    seek = _seek(db, owner, realm.id)
    media_seek.toggle_vote(db, voter, seek.id)
    assert _vote_row(db, seek.id, voter.id).weight_snapshot == 6
    _freeze(db, admin, voter)
    assert media_seek.weighted_votes_for(db, [seek.id])[seek.id] == 6
    assert _vote_row(db, seek.id, voter.id).weight_snapshot == 6


# ==================== Step 2 举报与复核 ====================


def test_report_frozen_reporter_forbidden(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    reporter = _user(db, "reporter")
    admin = _admin(db)
    seek = _seek(db, owner, realm.id)
    _freeze(db, admin, reporter)
    with pytest.raises(report_svc.GovernanceError) as ei:
        report_svc.create_report(db, reporter, seek.id, "spam")
    assert ei.value.status_code == 403
    assert "冻结" in ei.value.detail


def test_report_unfrozen_reporter_ok(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    reporter = _user(db, "reporter")
    seek = _seek(db, owner, realm.id)
    row = report_svc.create_report(db, reporter, seek.id, "spam")
    assert row["status"] == "pending"


def test_review_vote_frozen_reviewer_forbidden(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    reporter = _user(db, "reporter")
    reviewer = _user(db, "reviewer", level=3)
    admin = _admin(db)
    seek = _seek(db, owner, realm.id)
    report = report_svc.create_report(db, reporter, seek.id, "spam")
    _freeze(db, admin, reviewer)
    with pytest.raises(report_svc.GovernanceError) as ei:
        report_svc.cast_review_vote(db, reviewer, report["id"], "agree")
    assert ei.value.status_code == 403
    assert "冻结" in ei.value.detail


def test_review_vote_unfrozen_reviewer_ok(db):
    realm = _free_realm(db)
    owner = _user(db, "owner")
    reporter = _user(db, "reporter")
    reviewer = _user(db, "reviewer", level=3)
    seek = _seek(db, owner, realm.id)
    report = report_svc.create_report(db, reporter, seek.id, "spam")
    out = report_svc.cast_review_vote(db, reviewer, report["id"], "agree")
    assert out["status"] == "pending"


# ==================== Step 3 提案与直接发起 ====================


def test_submit_proposal_frozen_forbidden(db):
    _lottery_setup_free(db)
    user = _user(db, "lv2", xp=100)
    admin = _admin(db)
    _freeze(db, admin, user)
    with pytest.raises(lottery_gov.LotteryGovernanceError) as ei:
        lottery_gov.submit_proposal(db, user.id, _lottery_payload())
    assert ei.value.status_code == 403
    assert "冻结" in ei.value.detail
    assert db.query(models.LotteryActivityProposal).count() == 0


def test_submit_proposal_unfrozen_ok(db):
    _lottery_setup_free(db)
    user = _user(db, "lv2", xp=100)
    proposal = lottery_gov.submit_proposal(db, user.id, _lottery_payload())
    assert proposal.status == "pending"
    assert proposal.level_snapshot == 2


def test_direct_launch_frozen_forbidden(db):
    _lottery_setup_free(db)
    user = _user(db, "lv5", xp=5000)
    admin = _admin(db)
    _freeze(db, admin, user)
    with pytest.raises(lottery_gov.LotteryGovernanceError) as ei:
        lottery_gov.direct_launch(db, user.id, _lottery_payload())
    assert ei.value.status_code == 403
    assert "冻结" in ei.value.detail
    assert db.query(models.LotteryRound).count() == 0


def test_direct_launch_unfrozen_ok(db):
    _lottery_setup_free(db)
    user = _user(db, "lv5", xp=5000)
    round_obj, record = lottery_gov.direct_launch(db, user.id, _lottery_payload())
    assert round_obj is not None
    assert record is not None

# -*- coding: utf-8 -*-
"""等级公益服活动治理 Step 1 加权附议测试

覆盖（设计稿 Step 1＋Q1／Q3／Q11）：
1. 权重快照：公益服按投票时刻等级（Lv.1–Lv.6 → 1–6，封顶 6）；付费服恒为 1
2. 快照不追溯：投票后升级，历史票权重不变；取消删行即扣减该票权重
3. 加权排序：1 张 Lv.6 票（和 6）排在 3 张 Lv.1 票（和 3）之前，尽管人数少
4. 原始票数展示：RequestRecord 同时含 vote_count 与 weighted_vote_count
5. 每日上限 20：默认 20 次；第 21 张新票 ValueError；取消删行即释放名额
   （附议取消是硬删除，无残留行可计“撤回不退还”——与求片撤回软删除不同）；
   上限可配置（SystemConfig 覆盖）与脏值回默认
6. 原有附议规则回归：不能自投、仅 pending、可取消（回归项，详见邻近测试文件）

全部用隔离的内存 SQLite。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import media_seek, models
from backend.api import store_requests
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _user(db, username, level=None):
    kwargs = dict(username=username, password_hash="x", is_active=True)
    if level is not None:
        kwargs["member_level"] = level
    user = models.WebUser(**kwargs)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _realm(db, slug, access_mode):
    realm = models.ServerRealm(name=slug, slug=slug, access_mode=access_mode)
    db.add(realm)
    db.commit()
    db.refresh(realm)
    return realm


def _req(db, user, name="测试影片", realm_id=None):
    r = models.MovieRequest(
        user_id=user.id, movie_name=name, type="movie", status="pending",
        realm_id=realm_id,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg is None:
        cfg = models.SystemConfig(key=key, value=value)
        db.add(cfg)
    else:
        cfg.value = value
    db.commit()


def _vote_row(db, request_id, user_id):
    return db.query(models.MovieRequestVote).filter(
        models.MovieRequestVote.request_id == request_id,
        models.MovieRequestVote.user_id == user_id,
    ).first()


# ==================== 权重快照 ====================

def test_weight_snapshot_free_realm_by_level(db):
    realm = _realm(db, "free1", "free")
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=realm.id)
    voter1 = _user(db, "v1", level=1)
    voter3 = _user(db, "v3", level=3)
    voter6 = _user(db, "v6", level=6)
    media_seek.toggle_vote(db, voter1, req.id)
    media_seek.toggle_vote(db, voter3, req.id)
    media_seek.toggle_vote(db, voter6, req.id)
    assert _vote_row(db, req.id, voter1.id).weight_snapshot == 1
    assert _vote_row(db, req.id, voter3.id).weight_snapshot == 3
    assert _vote_row(db, req.id, voter6.id).weight_snapshot == 6
    assert media_seek.weighted_votes_for(db, [req.id])[req.id] == 10


def test_weight_snapshot_paid_realm_always_one(db):
    realm = _realm(db, "paid1", "paid")
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=realm.id)
    voter6 = _user(db, "v6", level=6)
    media_seek.toggle_vote(db, voter6, req.id)
    assert _vote_row(db, req.id, voter6.id).weight_snapshot == 1
    assert media_seek.weighted_votes_for(db, [req.id])[req.id] == 1


def test_weight_snapshot_legacy_realm_when_request_has_no_realm(db):
    # 老行 realm_id 为空 → 归默认服（id 最小）；默认服是公益服则按等级计
    _realm(db, "main", "free")
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=None)
    voter3 = _user(db, "v3", level=3)
    media_seek.toggle_vote(db, voter3, req.id)
    assert _vote_row(db, req.id, voter3.id).weight_snapshot == 3


def test_weight_snapshot_no_realm_falls_back_to_paid(db):
    # 全站无服 → 安全默认 paid，权重恒为 1
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=None)
    voter6 = _user(db, "v6", level=6)
    media_seek.toggle_vote(db, voter6, req.id)
    assert _vote_row(db, req.id, voter6.id).weight_snapshot == 1


def test_weight_snapshot_cap_and_missing_level(db):
    realm = _realm(db, "free1", "free")
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=realm.id)
    voter99 = _user(db, "v99", level=99)
    voter_none = _user(db, "vnone")  # member_level 缺省 → Lv.1
    media_seek.toggle_vote(db, voter99, req.id)
    media_seek.toggle_vote(db, voter_none, req.id)
    assert _vote_row(db, req.id, voter99.id).weight_snapshot == 6
    assert _vote_row(db, req.id, voter_none.id).weight_snapshot == 1


# ==================== 快照不追溯 ====================

def test_snapshot_immutable_on_level_change_and_cancel_subtracts(db):
    realm = _realm(db, "free1", "free")
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=realm.id)
    a = _user(db, "a", level=1)
    b = _user(db, "b", level=6)
    media_seek.toggle_vote(db, a, req.id)
    media_seek.toggle_vote(db, b, req.id)
    assert media_seek.weighted_votes_for(db, [req.id])[req.id] == 7
    # A 升到 Lv.3：历史票权重不变（仍按 1 计）
    a.member_level = 3
    db.commit()
    assert media_seek.weighted_votes_for(db, [req.id])[req.id] == 7
    # A 取消：扣减的是当初快照的 1，不是现在的 3
    media_seek.toggle_vote(db, a, req.id)
    assert media_seek.weighted_votes_for(db, [req.id])[req.id] == 6
    db.refresh(req)
    assert req.vote_count == 1


# ==================== 加权排序 ====================

def test_weighted_ordering_beats_raw_count(db):
    realm = _realm(db, "free1", "free")
    creator = _user(db, "creator")
    low = _req(db, creator, name="三票片", realm_id=realm.id)
    high = _req(db, creator, name="一票高权重片", realm_id=realm.id)
    for i in range(3):
        media_seek.toggle_vote(db, _user(db, f"lv1_{i}", level=1), low.id)
    media_seek.toggle_vote(db, _user(db, "lv6", level=6), high.id)
    rows = (
        db.query(models.MovieRequest)
        .filter(models.MovieRequest.id.in_([low.id, high.id]))
        .order_by(media_seek.weighted_vote_ordering().desc())
        .all()
    )
    # 加权和：high=6 > low=3；原始人数则相反（1 < 3）
    assert [r.id for r in rows] == [high.id, low.id]


# ==================== 原始票数展示 ====================

def test_record_carries_both_raw_and_weighted(db):
    realm = _realm(db, "free1", "free")
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=realm.id)
    media_seek.toggle_vote(db, _user(db, "v1", level=1), req.id)
    media_seek.toggle_vote(db, _user(db, "v6", level=6), req.id)
    db.refresh(req)
    rec = store_requests._record(
        req, in_library=False, library_item_id=None, voted=False, message="",
        weighted_vote_count=media_seek.weighted_votes_for(db, [req.id])[req.id],
    )
    assert rec.vote_count == 2
    assert rec.weighted_vote_count == 7


# ==================== 每日上限 ====================

def test_daily_vote_limit_default_twenty(db):
    assert media_seek.daily_vote_limit(db) == 20
    creator = _user(db, "creator")
    voter = _user(db, "voter", level=1)
    reqs = [_req(db, creator, name=f"片{i}") for i in range(21)]
    for r in reqs[:20]:
        media_seek.toggle_vote(db, voter, r.id)
    assert media_seek.daily_votes_used(db, voter.id) == 20
    with pytest.raises(ValueError, match="今日附议已达上限"):
        media_seek.toggle_vote(db, voter, reqs[20].id)


def test_daily_vote_limit_cancel_frees_slot(db):
    # 附议取消是硬删除：删行即释放名额（榜单热度只反映现存票，投→撤不留膨胀）
    creator = _user(db, "creator")
    voter = _user(db, "voter", level=1)
    reqs = [_req(db, creator, name=f"片{i}") for i in range(21)]
    for r in reqs[:20]:
        media_seek.toggle_vote(db, voter, r.id)
    media_seek.toggle_vote(db, voter, reqs[0].id)
    assert media_seek.daily_votes_used(db, voter.id) == 19
    media_seek.toggle_vote(db, voter, reqs[20].id)
    assert media_seek.daily_votes_used(db, voter.id) == 20


def test_daily_vote_limit_configurable_and_dirty_fallback(db):
    _set_config(db, media_seek.CONFIG_DAILY_VOTE_LIMIT, "2")
    assert media_seek.daily_vote_limit(db) == 2
    creator = _user(db, "creator")
    voter = _user(db, "voter", level=1)
    reqs = [_req(db, creator, name=f"片{i}") for i in range(3)]
    media_seek.toggle_vote(db, voter, reqs[0].id)
    media_seek.toggle_vote(db, voter, reqs[1].id)
    with pytest.raises(ValueError, match="今日附议已达上限"):
        media_seek.toggle_vote(db, voter, reqs[2].id)
    _set_config(db, media_seek.CONFIG_DAILY_VOTE_LIMIT, "nonsense")
    assert media_seek.daily_vote_limit(db) == 20


def test_toggle_vote_return_includes_weighted(db):
    realm = _realm(db, "free1", "free")
    creator = _user(db, "creator")
    req = _req(db, creator, realm_id=realm.id)
    voter = _user(db, "voter", level=3)
    result = media_seek.toggle_vote(db, voter, req.id)
    assert result["voted"] is True
    assert result["vote_count"] == 1
    assert result["weighted_vote_count"] == 3


# ==================== 原有规则回归 ====================

def test_vote_rules_unchanged(db):
    creator = _user(db, "creator")
    req = _req(db, creator)
    voter = _user(db, "voter", level=1)
    # 不能自投
    with pytest.raises(ValueError, match="不能给自己的求片附议"):
        media_seek.toggle_vote(db, creator, req.id)
    # 正常附议后取消
    assert media_seek.toggle_vote(db, voter, req.id)["voted"] is True
    assert media_seek.toggle_vote(db, voter, req.id)["voted"] is False
    # 非 pending 禁止
    req.status = "completed"
    db.commit()
    with pytest.raises(ValueError, match="已处理"):
        media_seek.toggle_vote(db, voter, req.id)

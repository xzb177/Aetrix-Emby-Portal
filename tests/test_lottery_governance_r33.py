"""审计 R33 切片 09：A-101（治理自铸）+ A-102（治理月限并发）回归。

A-101：治理层独立奖品上限（单奖 points/days 上限、每轮总价值上限；
直接发起与提案批准共用，管理端创建端点不走本模块校验）+ 发起人禁参赛
+ 直接发起路径禁用 whitelist 奖品。
A-102：提案/直接发起先锁用户行再计数，锁覆盖计数→写行→提交全程；
并发双建月限 1 时成功数 <= 1；同群并发仍只有一轮 open。
"""
import sys

sys.path.insert(0, ".")

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import lottery, lottery_governance as gov
from backend import models

_TABLES = [
    models.WebUser,
    models.SystemConfig,
    models.ServerRealm,
    models.MemberLevel,
    models.LotteryRound,
    models.LotteryRoundPrize,
    models.LotteryRoundEntry,
    models.LotteryRoundWinner,
    models.LotteryBlacklist,
    models.LotteryActivityProposal,
    models.LotteryDirectLaunch,
    models.GovernanceFreeze,
    models.AdminLog,
]


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for t in _TABLES:
        t.__table__.create(engine, checkfirst=True)
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


def _make_realm(db):
    realm = models.ServerRealm(name="公益服", slug="main", access_mode="free", is_active=True)
    db.add(realm)
    db.commit()
    db.refresh(realm)
    _set_config(db, "active_realm_id", str(realm.id))
    return realm


def _make_levels(db):
    for level, threshold in [(1, 0), (2, 100), (3, 500), (4, 1500), (5, 5000), (6, 15000)]:
        db.add(models.MemberLevel(level=level, name=f"Lv.{level}", xp_threshold=threshold, discount_pct=0, is_active=True))
    db.commit()


def _make_user(db, username, xp=0):
    user = models.WebUser(username=username, password_hash="", telegram_id=abs(hash(username)) % 10**9, member_xp=xp)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _enable_lottery(db, groups="999"):
    _set_config(db, "lottery_enabled", "1")
    _set_config(db, "lottery_group_ids", groups)


def _setup(db, groups="999"):
    _make_realm(db)
    _make_levels(db)
    _enable_lottery(db, groups)


def _payload(**over):
    p = {
        "title": "治理活动",
        "chat_id": 999,
        "prizes": [{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
        "draw_at": None,
        "max_participants": None,
        "lottery_type": "button",
        "password_keyword": None,
    }
    p.update(over)
    return p


# ---------------- A-101：奖品上限 ----------------

def test_direct_points_value_over_default_cap_rejected(db):
    _setup(db)
    user = _make_user(db, "lv5", xp=5000)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, user.id, _payload(prizes=[{"name": "x", "type": "points", "value": 10**9, "quantity": 1}]))
    assert ei.value.status_code == 400
    assert db.query(models.LotteryRound).count() == 0


def test_direct_whitelist_banned(db):
    _setup(db)
    user = _make_user(db, "lv5", xp=5000)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, user.id, _payload(prizes=[{"name": "x", "type": "whitelist", "value": 0, "quantity": 1}]))
    assert ei.value.status_code == 400
    assert db.query(models.LotteryRound).count() == 0


def test_direct_days_value_over_default_cap_rejected(db):
    _setup(db)
    user = _make_user(db, "lv5", xp=5000)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, user.id, _payload(prizes=[{"name": "x", "type": "days", "value": 31, "quantity": 1}]))
    assert ei.value.status_code == 400


def test_direct_total_value_over_default_cap_rejected(db):
    # 单奖 1000 未超单奖上限，但 1000*6=6000 超每轮总价值上限 5000。
    _setup(db)
    user = _make_user(db, "lv5", xp=5000)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.direct_launch(db, user.id, _payload(prizes=[{"name": "x", "type": "points", "value": 1000, "quantity": 6}]))
    assert ei.value.status_code == 400


def test_direct_at_caps_allowed(db):
    # 边界：单奖 == 上限、总量 == 上限时放行（上限是 <= 语义）。
    _setup(db)
    user = _make_user(db, "lv5", xp=5000)
    round_obj, _rec = gov.direct_launch(db, user.id, _payload(prizes=[{"name": "x", "type": "points", "value": 1000, "quantity": 5}]))
    assert round_obj.status == "open"


def test_proposal_over_cap_rejected(db):
    # 提案提交与批准共用同一上限：超限提案提交即 400。
    _setup(db)
    user = _make_user(db, "lv5", xp=5000)
    with pytest.raises(gov.LotteryGovernanceError) as ei:
        gov.submit_proposal(db, user.id, _payload(prizes=[{"name": "x", "type": "points", "value": 10**9, "quantity": 1}]))
    assert ei.value.status_code == 400


# ---------------- A-101：发起人禁参赛 ----------------

def test_creator_cannot_join_own_round(db):
    _setup(db)
    creator = _make_user(db, "creator", xp=5000)
    other = _make_user(db, "other", xp=0)
    round_obj = lottery.create_round(db, title="t", chat_id=999,
                                     prizes=[{"name": "x", "type": "points", "value": 100, "quantity": 1}],
                                     created_by=creator.id)
    res = lottery.join_round(db, round_obj, creator, creator.telegram_id)
    assert res == {"ok": False, "reason": "creator"}
    assert db.query(models.LotteryRoundEntry).count() == 0
    res2 = lottery.join_round(db, round_obj, other, other.telegram_id)
    assert res2["ok"] is True


def test_single_seat_round_empty_after_creator_excluded(db):
    _setup(db)
    creator = _make_user(db, "creator", xp=5000)
    round_obj = lottery.create_round(db, title="t", chat_id=999,
                                     prizes=[{"name": "x", "type": "points", "value": 100, "quantity": 3}],
                                     max_participants=1, created_by=creator.id)
    res = lottery.join_round(db, round_obj, creator, creator.telegram_id)
    assert res["reason"] == "creator"
    winners = lottery.draw_round(db, round_obj.id)
    assert winners == []
    dist = lottery.distribute_round(db, round_obj.id)
    assert dist["distributed"] == 0
    db.refresh(creator)
    assert creator.points == 0


def test_admin_created_round_unaffected(db):
    # 对照：管理端创建端点不走治理校验——同值奖品（1e9）建轮成功，
    # 且普通用户参加链不受发起人排除影响（created_by 为管理员）。
    _setup(db)
    admin = _make_user(db, "admin", xp=0)
    player = _make_user(db, "player", xp=0)
    round_obj = lottery.create_round(db, title="t", chat_id=999,
                                     prizes=[{"name": "x", "type": "points", "value": 10**9, "quantity": 1}],
                                     created_by=admin.id)
    assert round_obj.status == "open"
    res = lottery.join_round(db, round_obj, player, player.telegram_id)
    assert res["ok"] is True


# ---------------- A-102：并发月限 ----------------

@pytest.fixture()
def file_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gov.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    for t in _TABLES:
        t.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    s = Session()
    _setup(s, groups="999,888")
    lv5 = _make_user(s, "lv5", xp=5000)
    lv2 = _make_user(s, "lv2", xp=100)
    lv5_id, lv2_id = lv5.id, lv2.id
    s.close()
    yield Session, lv5_id, lv2_id
    engine.dispose()


def _run_two(Session, fn):
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(*args):
        s = Session()
        try:
            barrier.wait(timeout=10)
            fn(s, *args)
            outcomes.append("ok")
        except gov.LotteryGovernanceError:
            outcomes.append("gov400")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"error:{exc!r}")
        finally:
            try:
                s.rollback()
            except Exception:  # noqa: BLE001
                pass
            s.close()

    return outcomes, worker


def test_concurrent_direct_launch_cross_group_cap_one(file_db):
    Session, lv5_id, _lv2_id = file_db
    outcomes, worker = _run_two(Session, lambda s, chat: gov.direct_launch(
        s, lv5_id, _payload(title=f"直接{chat}", chat_id=chat)))
    t1 = threading.Thread(target=worker, args=(999,))
    t2 = threading.Thread(target=worker, args=(888,))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive()
    assert outcomes.count("ok") <= 1
    assert [o for o in outcomes if o.startswith("error")] == []
    assert outcomes.count("ok") + outcomes.count("gov400") == 2
    s = Session()
    try:
        assert s.query(models.LotteryDirectLaunch).filter_by(user_id=lv5_id).count() <= 1
        assert s.query(models.LotteryRound).filter_by(created_by=lv5_id).count() <= 1
    finally:
        s.close()


def test_concurrent_proposals_cap_one(file_db):
    Session, _lv5_id, lv2_id = file_db
    outcomes, worker = _run_two(Session, lambda s, title: gov.submit_proposal(
        s, lv2_id, _payload(title=title)))
    t1 = threading.Thread(target=worker, args=("提案A",))
    t2 = threading.Thread(target=worker, args=("提案B",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive()
    assert outcomes.count("ok") <= 1
    assert [o for o in outcomes if o.startswith("error")] == []
    assert outcomes.count("ok") + outcomes.count("gov400") == 2
    s = Session()
    try:
        assert s.query(models.LotteryActivityProposal).filter_by(user_id=lv2_id).count() <= 1
    finally:
        s.close()


def test_concurrent_direct_same_group_single_open(file_db):
    # 对照：同群并发双建仍只有一轮 open（生产 PG 靠部分唯一索引兜底；
    # sqlite 测试库无该索引，串行化后的同群进行中检查给出同一 400 结果）。
    Session, lv5_id, _lv2_id = file_db
    outcomes, worker = _run_two(Session, lambda s, title: gov.direct_launch(
        s, lv5_id, _payload(title=title, chat_id=999)))
    t1 = threading.Thread(target=worker, args=("同群A",))
    t2 = threading.Thread(target=worker, args=("同群B",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive()
    assert [o for o in outcomes if o.startswith("error")] == []
    s = Session()
    try:
        assert s.query(models.LotteryRound).filter_by(chat_id=999, status="open").count() == 1
    finally:
        s.close()

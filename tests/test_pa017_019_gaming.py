"""PA-017/018/019（R9-B9 博彩小游戏）回归测试。

全部用隔离的内存 SQLite，不碰生产库。先红后绿：本文件用例在修复前
应为红（缺陷签名），修复后全绿。
"""
import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import lottery
from backend import models
from backend import slots as slots_svc
from backend import welfare_redpacket as rp


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    # 全量建表：economy 记账（PointsLog）、白名单判定（codes）等旁路
    # 也会查表，缺表会在红阶段之外制造夹具噪音。
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_user(db, username, points=10000, is_active=True):
    u = models.WebUser(
        username=username, password_hash="x", points=points,
        is_active=is_active)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _make_free_realm(db):
    realm = models.ServerRealm(name="公益服", slug="free", url="",
                               access_mode="free")
    db.add(realm)
    db.commit()
    db.refresh(realm)
    _set_config(db, "active_realm_id", realm.id)
    return realm


# ---------- PA-017：发送者不能抢自己的红包 ----------

def test_sender_cannot_claim_own_packet(db):
    """发送者自抢必须被拒，且红包剩余不受影响。"""
    sender = _make_user(db, "sender17")
    packet = rp.send_packet(db, sender, 100, 2)
    with pytest.raises(ValueError, match="不能抢自己发的红包"):
        rp.claim_packet(db, sender, packet.id)
    db.refresh(packet)
    assert packet.remaining_count == 2
    assert packet.remaining_amount == 100


def test_other_user_can_claim(db):
    """他人正常抢不受影响。"""
    sender = _make_user(db, "sender17b")
    other = _make_user(db, "other17")
    packet = rp.send_packet(db, sender, 100, 2)
    result = rp.claim_packet(db, other, packet.id)
    assert 1 <= result["amount"] <= 99
    assert result["remaining_count"] == 1


# ---------- PA-018：开奖私聊须分发奖成败 ----------

def _make_round_with_two_winners(db, monkeypatch):
    import backend.tg_bot.sender as sender_mod

    sent = []

    def fake_send(db_, chat_id, text, reply_markup=None):
        sent.append((chat_id, text))
        return True, None

    def fake_get_member(db_, chat_id, user_id):
        return False, "user not found", None

    monkeypatch.setattr(sender_mod, "send_message", fake_send)
    monkeypatch.setattr(sender_mod, "get_chat_member_raw", fake_get_member)

    rnd = lottery.create_round(
        db, title="私聊分流测试", chat_id=-100333,
        prizes=[
            {"name": "能量奖励", "type": "points", "value": 500, "quantity": 1},
            {"name": "公益月卡", "type": "days", "value": 30, "quantity": 1},
        ],
        draw_at=None,
    )
    user_a = _make_user(db, "site_user_a")
    user_b = _make_user(db, "site_user_b")
    # telegram_id 用于私聊投递；直接写列，绕开 join 的 TG 身份门。
    user_a.telegram_id = 111111111
    user_b.telegram_id = 222222222
    db.commit()
    entries = {}
    for u in (user_a, user_b):
        e = models.LotteryRoundEntry(
            round_id=rnd.id, user_id=u.id, telegram_id=u.telegram_id)
        db.add(e)
        db.commit()
        db.refresh(e)
        entries[u.id] = e
    prizes = {
        p.name: p
        for p in db.query(models.LotteryRoundPrize)
        .filter(models.LotteryRoundPrize.round_id == rnd.id).all()
    }
    winners = [
        models.LotteryRoundWinner(
            round_id=rnd.id, entry_id=entries[user_a.id].id,
            prize_id=prizes["能量奖励"].id, distributed=True),
        models.LotteryRoundWinner(
            round_id=rnd.id, entry_id=entries[user_b.id].id,
            prize_id=prizes["公益月卡"].id, distributed=False),
    ]
    db.add_all(winners)
    db.commit()
    return rnd, winners, sent, user_a, user_b


def test_notify_dm_split_by_distributed(db, monkeypatch):
    """已发放者收到“已到账”，未发放者不得收到“已到账”，应收到补发口径。"""
    rnd, winners, sent, user_a, user_b = _make_round_with_two_winners(
        db, monkeypatch)
    result = lottery.notify_draw_results(db, rnd.id, winners)
    assert result["group"] is True
    dms_a = [t for c, t in sent if c == user_a.telegram_id]
    dms_b = [t for c, t in sent if c == user_b.telegram_id]
    assert len(dms_a) == 1
    assert "已发放到账" in dms_a[0]
    assert len(dms_b) == 1
    assert "已发放到账" not in dms_b[0]
    assert "自动补发" in dms_b[0]


# ---------- PA-019：停用账号不得经机器人玩博彩 ----------

def test_lottery_disabled_account_rejected(db):
    """停用账号参加抽奖被拒。"""
    _set_config(db, "lottery_min_account_age_days", 0)
    rnd = lottery.create_round(
        db, title="停用门测试", chat_id=-100444,
        prizes=[{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
    )
    user = _make_user(db, "disabled_lot", is_active=False)
    res = lottery.check_eligibility(db, rnd, user)
    assert res["ok"] is False
    assert res["reason"] == "account_disabled"


def test_slots_disabled_account_rejected(db):
    """停用账号玩老虎机被拒。"""
    _make_free_realm(db)
    user = _make_user(db, "disabled_slots", is_active=False)
    res = slots_svc.check_eligibility(db, user)
    assert res["ok"] is False
    assert res["reason"] == "account_disabled"


def test_redpacket_send_disabled_rejected(db):
    """停用账号发红包被拒。"""
    user = _make_user(db, "disabled_send", is_active=False)
    with pytest.raises(ValueError, match="已被停用"):
        rp.send_packet(db, user, 100, 2)


def test_redpacket_claim_disabled_rejected(db):
    """停用账号抢红包被拒。"""
    sender = _make_user(db, "sender19")
    packet = rp.send_packet(db, sender, 100, 2)
    user = _make_user(db, "disabled_claim", is_active=False)
    with pytest.raises(ValueError, match="已被停用"):
        rp.claim_packet(db, user, packet.id)


def test_active_accounts_unaffected(db):
    """正常账号三条路径均不受影响。"""
    _set_config(db, "lottery_min_account_age_days", 0)
    _make_free_realm(db)
    user = _make_user(db, "active19")
    rnd = lottery.create_round(
        db, title="正常账号测试", chat_id=-100555,
        prizes=[{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
    )
    assert lottery.check_eligibility(db, rnd, user)["ok"] is True
    assert slots_svc.check_eligibility(db, user)["ok"] is True
    packet = rp.send_packet(db, user, 100, 2)
    assert packet.remaining_count == 2

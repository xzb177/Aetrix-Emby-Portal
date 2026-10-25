"""每日/滚动额度并发绕过：检查前必须先锁用户行（economy.lock_user_row）。"""
from __future__ import annotations

import sys
import threading

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.api import economy


@pytest.fixture()
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/race.db",
        connect_args={"timeout": 15, "check_same_thread": False},
    )
    for t in (models.WebUser, models.SystemConfig, models.PointsLog):
        t.__table__.create(engine, checkfirst=True)
    yield engine
    engine.dispose()


def test_transfer_daily_cap_not_bypassed_by_concurrent_requests(file_engine, monkeypatch):
    Session = sessionmaker(bind=file_engine)
    s = Session()
    s.add_all([
        models.WebUser(username="snd", password_hash="", points=1000, is_active=True),
        models.WebUser(username="rcv", password_hash="", points=0, is_active=True),
        models.SystemConfig(key="points_transfer_daily_cap", value="100"),
        models.SystemConfig(key="points_transfer_fee_pct", value="0"),
    ])
    s.commit()
    sender_id = s.query(models.WebUser.id).filter_by(username="snd").scalar()
    s.close()

    a_spent = threading.Event()
    b_checked = threading.Event()
    real_spend = economy._spend_points

    def spend(db, user, cost, *a, **k):
        if threading.current_thread().name == "B":
            b_checked.set()  # B 已通过额度检查
            return real_spend(db, user, cost, *a, **k)
        r = real_spend(db, user, cost, *a, **k)
        a_spent.set()
        b_checked.wait(2)  # A 未提交时给 B 机会读旧用量
        return r

    monkeypatch.setattr(economy, "_spend_points", spend)
    results = {}

    def run(name):
        db = Session()
        try:
            if name == "B":
                a_spent.wait(5)
            u = db.get(models.WebUser, sender_id)
            economy.transfer_points_core(db, u, "rcv", 80)
            results[name] = "ok"
        except ValueError as e:
            results[name] = str(e)
        finally:
            db.close()

    ta = threading.Thread(target=run, args=("A",), name="A")
    tb = threading.Thread(target=run, args=("B",), name="B")
    ta.start(); tb.start(); ta.join(20); tb.join(20)

    db = Session()
    out = db.query(models.PointsLog).filter_by(user_id=sender_id, type="transfer_out").count()
    db.close()
    assert out == 1, results
    assert sorted(results.values())[0] == "ok"





def test_redpacket_send_and_claim_lock_user_before_counting(monkeypatch):
    from backend import welfare_redpacket
    calls = []
    monkeypatch.setattr(economy, "lock_user_row", lambda db, uid: calls.append(uid))
    engine = create_engine("sqlite:///:memory:")
    for t in (models.WebUser, models.SystemConfig, models.PointsLog, models.RedPacket, models.RedPacketClaim):
        t.__table__.create(engine, checkfirst=True)
    db = sessionmaker(bind=engine)()
    a = models.WebUser(username="rp_a", password_hash="", points=1000)
    b = models.WebUser(username="rp_b", password_hash="", points=0)
    db.add_all([a, b]); db.commit()
    p = welfare_redpacket.send_packet(db, a, 10, 2)
    welfare_redpacket.claim_packet(db, b, p.id)
    assert calls == [a.id, b.id]

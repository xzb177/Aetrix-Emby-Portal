"""审查修复回归（第三批）：redeem 双花、红包重放幂等。"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.database import SessionLocal, init_db
from backend import models

init_db()


def _make_user(username: str, points: int) -> models.WebUser:
    db = SessionLocal()
    old = db.query(models.WebUser).filter(models.WebUser.username == username).first()
    if old:
        db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
        db.commit()
    u = models.WebUser(username=username, password_hash="x", is_active=True,
                       points=points)
    db.add(u)
    db.commit()
    db.refresh(u)
    uid = u.id
    db.close()
    db2 = SessionLocal()
    user = db2.query(models.WebUser).filter(models.WebUser.id == uid).first()
    return user, db2


def test_redeem_welfare_atomic_spend():
    """redeem_welfare 用原子扣减：余额不足时抛 ValueError 且余额不变。"""
    from backend import points
    user, db = _make_user("review_redeem_u", 50)
    try:
        try:
            points.redeem_welfare(db, user, 7)  # 默认 100 分，不够
            raise AssertionError("应抛 ValueError")
        except ValueError as e:
            assert "能量不足" in str(e)
        db.refresh(user)
        assert int(user.points) == 50, "扣款失败时余额不应变化"
    finally:
        db.close()


def test_redpacket_replay_idempotent():
    """同一 idempotency_key 重放只建一个包、只扣一次款。"""
    from backend import welfare_redpacket
    user, db = _make_user("review_rp_u", 10000)
    uid = user.id
    try:
        # 关掉频率限制干扰：直接调 send_packet
        p1 = welfare_redpacket.send_packet(db, user, 100, 5,
                                           idempotency_key="tg:999999")
        bal_after_first = db.query(models.WebUser.points).filter(
            models.WebUser.id == uid).scalar()
        # 重放同一 key
        p2 = welfare_redpacket.send_packet(db, user, 100, 5,
                                           idempotency_key="tg:999999")
        bal_after_replay = db.query(models.WebUser.points).filter(
            models.WebUser.id == uid).scalar()
        assert p1.id == p2.id, "重放应返回同一个包"
        assert bal_after_first == bal_after_replay, (
            f"重放不应重复扣款：{bal_after_first} vs {bal_after_replay}")
        # 包总数应为 1
        n = db.query(models.RedPacket).filter(
            models.RedPacket.idempotency_key == "tg:999999").count()
        assert n == 1
    finally:
        db.query(models.RedPacket).filter(
            models.RedPacket.sender_id == uid).delete()
        db.query(models.PointsLog).filter(
            models.PointsLog.user_id == uid).delete(synchronize_session=False)
        db.commit()
        db.close()
        db3 = SessionLocal()
        db3.query(models.WebUser).filter(models.WebUser.id == uid).delete()
        db3.commit()
        db3.close()

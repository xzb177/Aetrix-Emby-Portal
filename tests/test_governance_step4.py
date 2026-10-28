"""等级公益服活动治理 Step 4：排行榜等级列＋审计日志只读＋停权工具。"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import governance, models
from backend.tg_bot import features


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def seed_levels(db):
    names = ["初幕", "影迷", "鉴赏家", "放映师", "造梦者", "传奇"]
    thresholds = [0, 100, 500, 1500, 5000, 15000]
    for i, (name, th) in enumerate(zip(names, thresholds), 1):
        db.add(models.MemberLevel(level=i, name=name, xp_threshold=th, is_active=True))
    db.commit()


def seed_free_realm(db):
    db.add(models.ServerRealm(name="公益服", slug="main", access_mode="free", is_active=True))
    db.commit()


def user(db, tg, name, points=0, level=1, xp=0):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg, points=points,
        member_level=level, member_xp=xp,
    )
    db.add(row)
    db.commit()
    return row


def test_rank_shows_level_column_only_in_free_realm(db):
    seed_levels(db)
    seed_free_realm(db)
    user(db, 900001, "low", points=10, level=1, xp=0)
    user(db, 900002, "high", points=100, level=2, xp=120)
    text = features.handle_rank(db, {"id": 900001}, 900001, "")
    assert "high" in text and "low" in text
    assert text.index("high") < text.index("low")  # 仍按积分降序
    assert "Lv.2 影迷" in text and "Lv.1 初幕" in text


def test_rank_unchanged_in_paid_realm(db):
    seed_levels(db)
    user(db, 900001, "mine", points=10, level=3, xp=600)
    user(db, 900002, "top", points=100, level=5, xp=6000)
    text = features.handle_rank(db, {"id": 900001}, 900001, "")
    assert "top" in text and "mine" in text and "Lv." not in text


def test_govlog_lv6_reads_recent_entries(db):
    seed_levels(db)
    seed_free_realm(db)
    admin = user(db, 900000, "admin", level=6, xp=20000)
    lv6 = user(db, 900001, "legend", level=6, xp=16000)
    target = user(db, 900002, "target", level=1, xp=0)
    governance.freeze_governance(db, user_id=target.id, reason="刷榜", days=None, admin_id=admin.id)
    db.commit()
    victim = user(db, 900003, "victim", level=3, xp=600)
    governance.governance_downgrade(db, user_id=victim.id, xp=200, reason="恶意举报", admin_id=admin.id)
    db.commit()
    text = features.handle_govlog(db, {"id": lv6.telegram_id}, lv6.telegram_id, "")
    assert "停权" in text and "降级" in text
    assert text.index("降级") < text.index("停权")  # 时间倒序


def test_govlog_denies_unbound_below_lv6_and_paid_realm(db):
    seed_levels(db)
    seed_free_realm(db)
    user(db, 900001, "lv5", level=5, xp=6000)
    assert "仅 Lv.6" in features.handle_govlog(db, {"id": 900001}, 900001, "")
    assert "绑定" in features.handle_govlog(db, {"id": 999999}, 999999, "")


def test_govlog_denied_in_paid_realm(db):
    seed_levels(db)
    user(db, 900001, "legend", level=6, xp=16000)
    assert "仅在公益服开放" in features.handle_govlog(db, {"id": 900001}, 900001, "")


def test_freeze_release_expiry_and_validation(db):
    admin = user(db, 900000, "admin")
    target = user(db, 900001, "target")
    row = governance.freeze_governance(db, user_id=target.id, reason="刷榜", days=30, admin_id=admin.id)
    db.commit()
    assert governance.is_governance_frozen(db, target.id) is True
    assert governance.active_freeze(db, target.id).reason == "刷榜"
    with pytest.raises(ValueError):
        governance.freeze_governance(db, user_id=target.id, reason="再冻", days=None, admin_id=admin.id)
    with pytest.raises(ValueError):
        governance.freeze_governance(db, user_id=target.id, reason="  ", days=None, admin_id=admin.id)
    with pytest.raises(ValueError):
        governance.freeze_governance(db, user_id=target.id, reason="x", days=0, admin_id=admin.id)
    governance.release_governance_freeze(db, user_id=target.id, admin_id=admin.id)
    db.commit()
    assert governance.is_governance_frozen(db, target.id) is False
    with pytest.raises(ValueError):
        governance.release_governance_freeze(db, user_id=target.id, admin_id=admin.id)
    # 过期即失效（无需管理员手动解封）
    from datetime import datetime, timedelta
    governance.freeze_governance(db, user_id=target.id, reason="过期测试", days=1, admin_id=admin.id)
    db.commit()
    row = governance.active_freeze(db, target.id)
    row.expires_at = datetime.now() - timedelta(days=1)
    db.commit()
    assert governance.is_governance_frozen(db, target.id) is False


def test_downgrade_clamps_and_audits_without_touching_points(db):
    seed_levels(db)
    admin = user(db, 900000, "admin")
    victim = user(db, 900001, "victim", points=777, level=3, xp=600)
    result = governance.governance_downgrade(db, user_id=victim.id, xp=200, reason="恶意举报", admin_id=admin.id)
    db.commit()
    assert result == {"xp_before": 600, "xp_after": 400, "level_before": 3, "level_after": 2}
    row = db.query(models.WebUser).filter(models.WebUser.id == victim.id).one()
    assert row.points == 777  # 积分不动
    log = db.query(models.GovernanceAuditLog).filter_by(action="downgrade").one()
    detail = json.loads(log.detail)
    assert detail["reason"] == "恶意举报" and detail["level_before"] == 3 and detail["level_after"] == 2
    # 扣到 0 钳制，不扣成负数
    result = governance.governance_downgrade(db, user_id=victim.id, xp=9999, reason="清零", admin_id=admin.id)
    assert result["xp_after"] == 0 and result["level_after"] == 1
    with pytest.raises(ValueError):
        governance.governance_downgrade(db, user_id=victim.id, xp=0, reason="x", admin_id=admin.id)
    with pytest.raises(ValueError):
        governance.governance_downgrade(db, user_id=victim.id, xp=10, reason="  ", admin_id=admin.id)
    with pytest.raises(ValueError):
        governance.governance_downgrade(db, user_id=123456, xp=10, reason="x", admin_id=admin.id)


def test_audit_log_filtered_read_and_stable_across_reads(db):
    admin = user(db, 900000, "admin")
    target = user(db, 900001, "target")
    governance.freeze_governance(db, user_id=target.id, reason="r", days=None, admin_id=admin.id)
    governance.record_governance_audit(db, action="vote", actor_user_id=target.id, target_ref="seek:1")
    governance.record_governance_audit(db, action="freeze", actor_user_id=admin.id, target_user_id=target.id)
    db.commit()
    all_rows = governance.list_audit_logs(db, limit=50)
    assert len(all_rows) == 3
    assert all_rows[0].id > all_rows[1].id > all_rows[2].id  # 时间倒序
    assert [r.action for r in governance.list_audit_logs(db, action="freeze")] == ["freeze", "freeze"]
    assert len(governance.list_audit_logs(db, limit=1)) == 1
    assert db.query(models.GovernanceAuditLog).count() == 3  # 只读查询不改数据


def test_admin_endpoints_and_route_registration(db):
    from fastapi import HTTPException
    from backend.api import governance_admin as ga
    from backend.api.admin_core import admin_router

    seed_levels(db)
    seed_free_realm(db)
    admin = user(db, 900000, "admin")
    target = user(db, 900001, "target", level=3, xp=600)
    db.commit()

    paths = {(r.path, tuple(r.methods or ())) for r in admin_router.routes}
    assert ("/api/admin/governance/audit-logs", ("GET",)) in paths
    assert ("/api/admin/governance/freeze", ("POST",)) in paths
    assert ("/api/admin/governance/freeze/release", ("POST",)) in paths
    assert ("/api/admin/governance/downgrade", ("POST",)) in paths
    assert not any(p.startswith("/api/admin/governance/audit-logs") and "DELETE" in m for p, m in paths)
    assert not any(p.startswith("/api/admin/governance/audit-logs") and "PUT" in m for p, m in paths)

    out = ga.governance_freeze(ga.FreezeRequest(user_id=target.id, reason="刷榜", days=30),
                               current_admin=admin, db=db)
    assert out["success"] is True and out["freeze_id"]
    with pytest.raises(HTTPException) as exc:
        ga.governance_freeze(ga.FreezeRequest(user_id=target.id, reason="再冻", days=None),
                             current_admin=admin, db=db)
    assert exc.value.status_code == 409
    fresh = user(db, 900009, "fresh")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        ga.governance_freeze(ga.FreezeRequest(user_id=fresh.id, reason="  ", days=None),
                             current_admin=admin, db=db)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        ga.governance_freeze(ga.FreezeRequest(user_id=999999, reason="x", days=None),
                             current_admin=admin, db=db)
    assert exc.value.status_code == 404

    logs = ga.governance_audit_logs(limit=50, current_admin=admin, db=db)
    assert any(item["action"] == "freeze" for item in logs["items"])

    out = ga.governance_downgrade(ga.DowngradeRequest(user_id=target.id, xp=200, reason="恶意举报"),
                                  current_admin=admin, db=db)
    assert out["success"] is True and out["level_after"] == 2

    out = ga.governance_freeze_release(ga.FreezeReleaseRequest(user_id=target.id),
                                       current_admin=admin, db=db)
    assert out["success"] is True
    with pytest.raises(HTTPException) as exc:
        ga.governance_freeze_release(ga.FreezeReleaseRequest(user_id=target.id),
                                     current_admin=admin, db=db)
    assert exc.value.status_code == 404
    assert governance.is_governance_frozen(db, target.id) is False


def test_admin_endpoints_reject_paid_realm(db):
    from fastapi import HTTPException
    from backend.api import governance_admin as ga

    admin = user(db, 900000, "admin")
    target = user(db, 900001, "target")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        ga.governance_freeze(ga.FreezeRequest(user_id=target.id, reason="x", days=None),
                             current_admin=admin, db=db)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        ga.governance_audit_logs(limit=50, current_admin=admin, db=db)
    assert exc.value.status_code == 403

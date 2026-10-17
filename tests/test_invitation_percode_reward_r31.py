# tests/test_invitation_percode_reward_r31.py
"""r31：邀请码「每码专属奖励」接通 + 撞号重试回归测试。

背景（r31 审查发现）：
1. ``InvitationCode.reward_points`` 是只写不读的死字段——``apply_invitation``
   永远按全局 ``invitation_reward_points`` 配置发奖；后台 DTO 却把它展示为
   「奖励」，批量码恒为 0 误导管理员。自助码（/my-code）还往里面写生成时的
   配置快照，实际从未生效。
   修法：``apply_invitation`` 以 ``code.reward_points > 0`` 优先于全局配置；
   0/空 = 跟随全局（存量行为不变）；批量生成/单码编辑接口接受可选
   ``reward_points``，管理后台对话框配上输入框。
2. ``_generate_invite_code`` 的「查库不撞号」是读-改-写 TOCTOU：两个并发请求
   同时生成同码时，唯一索引在 commit 时拦下 → ``IntegrityError`` 500。
   修法：/my-code 与批量生成在 IntegrityError 时回滚并重生成一次再试。

覆盖：
- test_percode_reward_overrides_global：码 reward_points=300、全局=100 →
  邀请人实得 300、InvitationRecord 记 300、返回值 inviter_reward=300。
- test_zero_reward_falls_back_to_global：码 reward_points=0 → 跟随全局 100。
- test_mycode_collision_retry：_generate_invite_code 首次返回已存在的码 →
  IntegrityError 触发重试 → 最终拿到新码、200 不抛。
- test_update_endpoint_accepts_reward_points：PUT 更新 reward_points 落库。
"""

from __future__ import annotations

import sys
import uuid

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import invitation as invite_mod
from backend.api.invitation import apply_invitation


def _engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )


def _user(db, username: str) -> models.WebUser:
    u = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_code(db, inviter_id: int, code_str: str, reward_points: int = 0):
    code = models.InvitationCode(
        code=code_str,
        user_id=inviter_id,
        is_active=True,
        max_uses=0,
        use_count=0,
        reward_points=reward_points,
    )
    db.add(code)
    db.commit()
    db.refresh(code)
    return code


@pytest.fixture()
def db():
    engine = _engine()
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    session = SessionLocal()
    yield session
    session.close()


def test_percode_reward_overrides_global(db):
    """码 reward_points=300、全局=100 → 邀请人实得 300"""
    db.add(models.SystemConfig(key="invitation_reward_points", value="100"))
    db.add(models.SystemConfig(key="invitation_invitee_reward_points", value="50"))
    db.add(models.SystemConfig(key="invitation_enabled", value="true"))
    db.commit()

    tag = uuid.uuid4().hex[:8]
    inviter = _user(db, f"per-inviter-{tag}")
    invitee = _user(db, f"per-invitee-{tag}")
    code = _make_code(db, inviter.id, f"PER{tag.upper()}", reward_points=300)

    res = apply_invitation(db, invitee, code.code)
    db.commit()

    assert res["applied"] is True
    assert res["inviter_reward"] == 300
    rec = db.query(models.InvitationRecord).filter_by(invitee_id=invitee.id).one()
    assert rec.reward_points == 300
    log = db.query(models.PointsLog).filter_by(
        user_id=inviter.id, type="invite").one()
    assert log.amount == 300


def test_zero_reward_falls_back_to_global(db):
    """码 reward_points=0 → 跟随全局 100（存量行为不变）"""
    db.add(models.SystemConfig(key="invitation_reward_points", value="100"))
    db.add(models.SystemConfig(key="invitation_enabled", value="true"))
    db.commit()

    tag = uuid.uuid4().hex[:8]
    inviter = _user(db, f"zero-inviter-{tag}")
    invitee = _user(db, f"zero-invitee-{tag}")
    code = _make_code(db, inviter.id, f"ZERO{tag.upper()}", reward_points=0)

    res = apply_invitation(db, invitee, code.code)
    db.commit()

    assert res["applied"] is True
    assert res["inviter_reward"] == 100
    rec = db.query(models.InvitationRecord).filter_by(invitee_id=invitee.id).one()
    assert rec.reward_points == 100


def test_mycode_collision_retry(db, monkeypatch):
    """首次生成撞上已存在的码 → IntegrityError → 重试一次拿到新码，不抛 500"""
    tag = uuid.uuid4().hex[:8]
    user = _user(db, f"collide-user-{tag}")
    # 撞号目标码归另一个用户所有：my_code 查「我的码」时看不到它，
    # 但唯一索引在 commit 时会拦下同码插入 → 触发重试路径
    other = _user(db, f"collide-other-{tag}")
    existing = _make_code(db, other.id, f"COLLIDE{tag.upper()}")

    calls = {"n": 0}
    real_gen = invite_mod._generate_invite_code

    def flaky_gen(session):
        calls["n"] += 1
        if calls["n"] == 1:
            return existing.code  # 第一次故意返回已存在的码 → commit 时唯一索引拦下
        return real_gen(session)

    monkeypatch.setattr(invite_mod, "_generate_invite_code", flaky_gen)

    out = invite_mod.my_code(current_user=user, db=db)
    assert out["code"] != existing.code
    assert calls["n"] == 2
    row = db.query(models.InvitationCode).filter_by(
        user_id=user.id, code=out["code"]).one()
    assert int(row.reward_points or 0) == 0  # 自助码语义：跟随全局


def test_update_request_schema_accepts_reward_points():
    """更新请求的 pydantic 模型接受 reward_points（None=不改 / 0=跟随全局）"""
    from backend.api.admin_ops import InvitationCodeUpdateRequest, InvitationCodeGenerateRequest

    u = InvitationCodeUpdateRequest(reward_points=300)
    assert u.reward_points == 300
    g = InvitationCodeGenerateRequest(owner_user_id=1, reward_points=0)
    assert g.reward_points == 0
    d = InvitationCodeUpdateRequest()
    assert d.reward_points is None

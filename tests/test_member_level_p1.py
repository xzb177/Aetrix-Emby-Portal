"""P1 统一货币体系：会员等级 / 经验值测试

全部用隔离的内存 SQLite，不碰生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend import member_level as ml


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    ml.ensure_member_levels_seeded(session)
    yield session
    session.close()


def _make_user(db, username):
    u = models.WebUser(username=username, password_hash="x")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------- 种子 ----------

def test_seed_is_idempotent(db):
    """种子幂等：调两次还是 6 条"""
    assert ml.ensure_member_levels_seeded(db) == 0
    assert db.query(models.MemberLevel).count() == 6


def test_seed_defaults(db):
    lv2 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 2).first()
    assert lv2.name == "影迷"
    assert lv2.xp_threshold == 100
    assert lv2.is_active is True


# ---------- 经验累加 ----------

def test_add_xp_basic(db):
    u = _make_user(db, "xp1")
    r = ml.add_xp(db, u, 50, "recharge", ref_id="recharge:order1")
    db.commit()
    assert r["added"] is True
    assert r["xp_after"] == 50
    assert r["level"] == 1  # 50 < 100，仍是初幕
    assert r["leveled_up"] is False
    # 流水
    log = db.query(models.MemberXpLog).filter(
        models.MemberXpLog.user_id == u.id).first()
    assert log.xp_delta == 50 and log.xp_after == 50
    assert log.source == "recharge" and log.ref_id == "recharge:order1"


def test_add_xp_level_up(db):
    u = _make_user(db, "xp2")
    r = ml.add_xp(db, u, 100, "recharge")
    db.commit()
    assert r["level"] == 2
    assert r["leveled_up"] is True
    assert r["old_level"] == 1
    # 缓存等级写回
    db.refresh(u)
    assert u.member_level == 2


def test_add_xp_multi_level_jump(db):
    """一次加 600 经验：100 阈值过 2 级，500 阈值过 3 级"""
    u = _make_user(db, "xp3")
    r = ml.add_xp(db, u, 600, "invite", ref_id="invite:9")
    db.commit()
    assert r["level"] == 3
    assert r["leveled_up"] is True


def test_add_xp_nonpositive_ignored(db):
    # r33 语义变更：delta=0 仍忽略；delta<0 改为退款回滚（钳制 0），不再忽略
    u = _make_user(db, "xp4")
    assert ml.add_xp(db, u, 0, "recharge")["added"] is False
    assert db.query(models.MemberXpLog).count() == 0
    ml.add_xp(db, u, 50, "recharge", "recharge:RBX")
    db.commit()
    r = ml.add_xp(db, u, -5, "refund", "refund:RBX")
    db.commit()
    assert r["added"] is True
    db.refresh(u)
    assert u.member_xp == 45
    assert db.query(models.MemberXpLog).filter(
        models.MemberXpLog.xp_delta < 0).count() == 1


def test_add_xp_accumulates(db):
    """多次累加：50 + 60 = 110，触发升级"""
    u = _make_user(db, "xp5")
    ml.add_xp(db, u, 50, "recharge")
    db.commit()
    r = ml.add_xp(db, u, 60, "recharge")
    db.commit()
    assert r["xp_after"] == 110
    assert r["level"] == 2 and r["leveled_up"] is True


# ---------- 等级信息 ----------

def test_get_member_info_progress(db):
    u = _make_user(db, "xp6")
    ml.add_xp(db, u, 50, "recharge")
    db.commit()
    info = ml.get_member_info(db, u)
    assert info["level"] == 1
    assert info["level_name"] == "初幕"
    assert info["xp"] == 50
    assert info["next_level"] == 2
    assert info["next_threshold"] == 100
    assert info["xp_to_next"] == 50
    assert info["progress_pct"] == 50.0
    assert len(info["levels"]) == 6
    # 每个等级权益公开
    assert isinstance(info["levels"][0]["benefits"], list)


def test_get_member_info_max_level(db):
    u = _make_user(db, "xp7")
    ml.add_xp(db, u, 20000, "recharge")
    db.commit()
    info = ml.get_member_info(db, u)
    assert info["level"] == 6
    assert info["next_level"] is None
    assert info["progress_pct"] == 100.0


def test_disabled_level_skipped(db):
    """禁用的等级不参与推导"""
    lv2 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 2).first()
    lv2.is_active = False
    db.commit()
    u = _make_user(db, "xp8")
    r = ml.add_xp(db, u, 100, "recharge")
    db.commit()
    # 2 级禁用，100 经验仍停留在 1 级（500 才到 3 级）
    assert r["level"] == 1


# ---------- 邀请经验 ----------

def test_invitation_grants_xp(db):
    """有效邀请：邀请人 +10 经验"""
    from backend.api import invitation as inv

    inviter = _make_user(db, "inviter1")
    invitee = _make_user(db, "invitee1")
    code = models.InvitationCode(code="TESTCODE1", user_id=inviter.id,
                                 is_active=True, use_count=0)
    db.add(code)
    db.commit()

    # 开关默认可能关闭，先确保开启
    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "invitation_enabled").first()
    if cfg:
        cfg.value = "true"
    else:
        db.add(models.SystemConfig(key="invitation_enabled", value="true"))
    db.commit()

    result = inv.apply_invitation(db, invitee, "TESTCODE1")
    db.commit()
    assert result["applied"] is True

    xlog = db.query(models.MemberXpLog).filter(
        models.MemberXpLog.user_id == inviter.id,
        models.MemberXpLog.source == "invite",
    ).first()
    assert xlog is not None
    assert xlog.xp_delta == 10


# ---------- v1 → v2 命名迁移 ----------

def test_migrate_legacy_names(db):
    """旧命名行被迁移为暗房影院主题名，徽章同步更新"""
    # 模拟 v1 旧数据
    for lv in db.query(models.MemberLevel).all():
        lv.name = {"初幕": "普通会员", "影迷": "铜牌会员", "鉴赏家": "白银会员",
                   "放映师": "黄金会员", "造梦者": "铂金会员", "传奇": "钻石会员"}[lv.name]
    db.commit()

    updated = ml.migrate_legacy_level_names(db)
    assert updated == 6

    lv2 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 2).first()
    assert lv2.name == "影迷"
    assert lv2.badge_icon == "Clapperboard"
    assert lv2.badge_color == "#f59e0b"

    lv6 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 6).first()
    assert lv6.name == "传奇"
    assert lv6.badge_icon == "Crown"


def test_migrate_is_idempotent(db):
    """迁移幂等：已是新名的行不受影响，跑多次结果一致"""
    assert ml.migrate_legacy_level_names(db) == 0
    assert ml.migrate_legacy_level_names(db) == 0
    lv1 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 1).first()
    assert lv1.name == "初幕"


def test_migrate_preserves_custom_names(db):
    """管理员手动改过的名称不被迁移覆盖"""
    lv3 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 3).first()
    lv3.name = "我的专属等级"
    db.commit()

    updated = ml.migrate_legacy_level_names(db)
    assert updated == 0  # 没有旧默认名可迁移

    lv3 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 3).first()
    assert lv3.name == "我的专属等级"


def test_migrate_updates_benefits_text(db):
    """权益文案中的旧名同步替换"""
    for lv in db.query(models.MemberLevel).all():
        lv.name = {"初幕": "普通会员", "影迷": "铜牌会员", "鉴赏家": "白银会员",
                   "放映师": "黄金会员", "造梦者": "铂金会员", "传奇": "钻石会员"}[lv.name]
    import json
    lv2 = db.query(models.MemberLevel).filter(models.MemberLevel.level == 2).first()
    lv2.benefits_json = json.dumps(["铜牌专属徽章", "邀请奖励加成"], ensure_ascii=False)
    db.commit()

    ml.migrate_legacy_level_names(db)

    import json as _json
    benefits = _json.loads(lv2.benefits_json)
    assert benefits[0] == "影迷专属徽章"

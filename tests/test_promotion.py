"""分销/邀请第一阶段：推广奖励 + 邀请码白名单 + 注册渠道归因

全部离线：临时内存 SQLite（**不碰任何生产库/真实配置**），不发网络请求、不起服务。

钉住的是五件容易写错、且写错就直接变成「多发钱/算错账」的事：

1. **默认不发**：``promotion_reward_enabled`` 出厂 false、金额/天数出厂 0。
   升级上来的部署邀请成功必须**逐字不变**（只发原有的双向积分）——
   这条不钉住，以后谁改默认值就是给所有站点静默开了自动发奖。
2. **开关开着但值是 0 也不发**：不写「+0」的假明细。
3. **一个被邀请人只发一次**：预查 + ``invitee_id`` 唯一索引双保险。
4. **邀请关系、双向积分、推广奖励同一个事务**：任一环节冲突整体回滚，
   不留「关系建了奖没发」或反过来的半截状态。
5. **归因只认真实成立的邀请**：码无效/白名单未命中时不得记成 invitation；
   卡密进门优先级高于邀请码。

另附管理端口径：邀请码批量生成/改配/作废（可逆）与 ``list_users`` 渠道筛选。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, promotion, realms, register_channel
from backend.api import admin as admin_api
from backend.api import admin_ops
from backend.api.invitation import apply_invitation
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # store 的热读缓存是进程级全局的：不清掉会把上一个用例的库态带进来
    store.invalidate()
    try:
        yield session
    finally:
        session.close()
        store.invalidate()


@pytest.fixture()
def realm(db):
    """先建好当前服：``realms.claim`` 兜底建服时会 commit，别把发奖事务截断"""
    row = models.ServerRealm(name="主服", slug="main", access_mode="paid", is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _user(db, username, *, points=0, is_staff=False, channel=None):
    user = models.WebUser(
        username=username, password_hash="x", points=points, is_staff=is_staff,
        register_channel=channel,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _code(db, owner, *, code="INVITE001", max_uses=0, whitelist="", expires_at=None,
          is_active=True, use_count=0):
    row = models.InvitationCode(
        code=code, user_id=owner.id, max_uses=max_uses, use_count=use_count,
        reward_points=0, whitelist=whitelist, expires_at=expires_at, is_active=is_active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _policy(db, **values):
    applied = promotion.write_policy(db, values)
    db.commit()
    return applied


def _balance(db, user_id):
    return db.query(models.WebUser.points).filter(
        models.WebUser.id == user_id).scalar() or 0


def _rewards(db, inviter_id=None):
    query = db.query(models.PromotionReward)
    if inviter_id is not None:
        query = query.filter(models.PromotionReward.inviter_id == inviter_id)
    return query.all()


def _points_logs(db, user_id, type_):
    return db.query(models.PointsLog).filter(
        models.PointsLog.user_id == user_id, models.PointsLog.type == type_).all()


# ===========================================================================
# 1. 默认关闭：升级上来绝不多发
# ===========================================================================

def test_promotion_defaults_are_off(db):
    # 2026-10-21 A 类摘面板现值对齐：enabled 缺省翻为开（生产现值），
    # 但金额/天数仍为 0 → 实际不发放（active 口径不变）
    cfg = promotion.config(db)
    assert cfg["enabled"] is True
    assert cfg["amount"] == 0
    assert cfg["days"] == 0
    # 开关关着 → 后台必须显示「没在发」，不能看起来像已开启
    assert promotion.policy_payload(db)["active"] is False
    assert promotion.reward_type(db) == promotion.TYPE_BALANCE


def test_grant_is_noop_when_disabled(db):
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    code = _code(db, inviter)

    assert promotion.grant(db, inviter, invitee, code) is None
    db.commit()

    assert _rewards(db) == []
    assert _balance(db, inviter.id) == 0
    assert _points_logs(db, inviter.id, "promotion") == []


def test_apply_invitation_without_promotion_keeps_legacy_behavior(db):
    """出厂状态下邀请流程必须与升级前**逐字一致**：只发 100 + 50 两笔"""
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    _code(db, inviter)

    result = apply_invitation(db, invitee, "INVITE001")
    db.commit()

    assert result["applied"] is True
    assert _balance(db, inviter.id) == 100
    assert _balance(db, invitee.id) == 50
    assert _rewards(db) == []          # 推广奖励没开 → 一行都不写
    assert len(_points_logs(db, inviter.id, "invite")) == 1


# ===========================================================================
# 2. 开关与阈值：全走 SystemConfig，非法值不许写坏配置
# ===========================================================================

def test_write_policy_persists_and_clamps(db):
    applied = _policy(
        db,
        **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "50",
           promotion.CONFIG_TYPE: promotion.TYPE_DAYS, promotion.CONFIG_DAYS: "7"},
    )
    assert applied[promotion.CONFIG_AMOUNT] == "50"
    cfg = promotion.config(db)
    assert cfg["enabled"] is True
    assert cfg["reward_type"] == promotion.TYPE_DAYS
    assert cfg["days"] == 7
    assert promotion.policy_payload(db)["active"] is True

    # 上限：手写坏配置不能变成「发一百万」
    _policy(db, **{promotion.CONFIG_AMOUNT: "999999999"})
    assert promotion.config(db)["amount"] == promotion.AMOUNT_MAX
    _policy(db, **{promotion.CONFIG_DAYS: "-5"})
    assert promotion.config(db)["days"] == 0


def test_write_policy_ignores_unknown_and_invalid(db):
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "30"})
    applied = promotion.write_policy(db, {
        "promotion_reward_tpye": "typo-key",      # 白名单外
        promotion.CONFIG_TYPE: "gold",            # 非法枚举
        promotion.CONFIG_AMOUNT: "not-a-number",  # 非法数值
    })
    assert applied == {}                          # 一条都没写
    db.commit()
    cfg = promotion.config(db)
    assert cfg["amount"] == 30                    # 原值完好，没存进半个坏配置
    assert cfg["reward_type"] == promotion.TYPE_BALANCE
    # 其它 SystemConfig 键不会被这个入口碰到
    assert not db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "promotion_reward_tpye").first()


# ===========================================================================
# 3. 发奖：余额 / 有效期两种类型
# ===========================================================================

def test_grant_balance_credits_points_and_writes_row(db):
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "50"})

    row = promotion.grant(db, inviter, invitee, _code(db, inviter))
    db.commit()

    assert row is not None
    saved = db.query(models.PromotionReward).filter_by(id=row.id).one()
    assert saved.reward_type == promotion.TYPE_BALANCE
    assert saved.reward_value == 50
    assert saved.inviter_id == inviter.id and saved.invitee_id == invitee.id
    assert '"amount": 50' in saved.config_snapshot  # 发了多少要留痕
    assert _balance(db, inviter.id) == 50
    logs = _points_logs(db, inviter.id, "promotion")
    assert len(logs) == 1 and logs[0].amount == 50
    assert logs[0].ref_id == f"promotion:{invitee.id}"


def test_grant_days_extends_membership_and_records_realm(db, realm):
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_TYPE: "days",
                   promotion.CONFIG_DAYS: "7"})

    row = promotion.grant(db, inviter, invitee, _code(db, inviter))
    db.commit()

    assert row is not None
    saved = db.query(models.PromotionReward).filter_by(id=row.id).one()
    assert saved.reward_type == promotion.TYPE_DAYS
    assert saved.reward_value == 7
    assert saved.realm_id == realm.id          # 天发到哪个服必须记下来
    sub = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == inviter.id).one()
    assert sub.realm_id == realm.id
    assert sub.status == "active"
    assert sub.end_date >= datetime.now() + timedelta(days=6)
    assert _balance(db, inviter.id) == 0        # 走有效期就不动积分


def test_grant_skips_when_value_is_zero(db):
    """开关开着但值 0：只记关系、不发奖，也不写「+0」的假明细"""
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "0"})

    assert promotion.grant(db, inviter, invitee, _code(db, inviter)) is None
    db.commit()
    assert _rewards(db) == []
    assert _balance(db, inviter.id) == 0
    assert promotion.policy_payload(db)["active"] is False


def test_grant_is_idempotent_per_invitee(db):
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "50"})
    code = _code(db, inviter)

    assert promotion.grant(db, inviter, invitee, code) is not None
    db.commit()
    # 同一个被邀请人再来一次（重试/并发/重复调用）必须被预查拦下
    assert promotion.grant(db, inviter, invitee, code) is None
    db.commit()

    assert len(_rewards(db)) == 1
    assert _balance(db, inviter.id) == 50       # 没发第二次
    assert len(_points_logs(db, inviter.id, "promotion")) == 1


def test_grant_failure_never_breaks_the_invitation(db):
    """发奖是附加项：它自己炸了也不能把邀请关系带崩"""
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    _code(db, inviter)
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "50"})

    def boom(*_a, **_k):
        raise RuntimeError("积分服务炸了")

    import backend.api.economy as economy
    original = economy._add_points
    economy._add_points = boom
    try:
        assert promotion.grant(db, inviter, invitee, None) is None
    finally:
        economy._add_points = original
    db.rollback()
    # 发奖失败不留下半截：没写明细、没写台账
    assert _rewards(db) == []
    assert _points_logs(db, inviter.id, "promotion") == []

    # 邀请关系照常建立（100 邀请 + 50 推广，推广开关是开着的）
    result = apply_invitation(db, invitee, "INVITE001")
    db.commit()
    assert result["applied"] is True
    assert _balance(db, inviter.id) == 150
    assert len(_rewards(db)) == 1


# ===========================================================================
# 4. 邀请码：白名单 / 次数 / 有效期 / 幂等
# ===========================================================================

def test_apply_invitation_whitelist_blocks_others(db):
    inviter = _user(db, "boss")
    _code(db, inviter, whitelist="bob, carol")
    carol = _user(db, "mallory")

    result = apply_invitation(db, carol, "INVITE001")
    db.commit()

    assert result["applied"] is False
    assert db.query(models.InvitationRecord).count() == 0
    assert _balance(db, inviter.id) == 0
    assert _balance(db, carol.id) == 0


def test_apply_invitation_whitelist_hit_is_case_insensitive(db):
    inviter = _user(db, "boss")
    _code(db, inviter, whitelist="  BOB , carol ")
    bob = _user(db, "BoB")

    assert apply_invitation(db, bob, "INVITE001")["applied"] is True
    db.commit()
    assert _balance(db, inviter.id) == 100


def test_apply_invitation_empty_whitelist_means_unlimited(db):
    inviter = _user(db, "boss")
    _code(db, inviter, whitelist="   ")
    for name in ("u1", "u2"):
        assert apply_invitation(db, _user(db, name), "INVITE001")["applied"] is True
        db.commit()


def test_apply_invitation_respects_max_uses_and_expiry(db):
    inviter = _user(db, "boss")
    _code(db, inviter, max_uses=1)
    assert apply_invitation(db, _user(db, "u1"), "INVITE001")["applied"] is True
    db.commit()
    assert apply_invitation(db, _user(db, "u2"), "INVITE001")["applied"] is False

    _code(db, inviter, code="INVITE002", expires_at=datetime.now() - timedelta(days=1))
    assert apply_invitation(db, _user(db, "u3"), "INVITE002")["applied"] is False


def test_apply_invitation_is_idempotent(db):
    """重复调用不得二次发奖（既有关系的口径）"""
    inviter = _user(db, "boss")
    _code(db, inviter)
    invitee = _user(db, "newbie")

    assert apply_invitation(db, invitee, "INVITE001")["applied"] is True
    db.commit()
    first_balance = _balance(db, inviter.id)
    first_uses = db.query(models.InvitationCode).filter_by(code="INVITE001").one().use_count

    assert apply_invitation(db, invitee, "INVITE001")["applied"] is False
    db.commit()

    assert _balance(db, inviter.id) == first_balance
    assert db.query(models.InvitationRecord).count() == 1
    assert db.query(models.InvitationCode).filter_by(code="INVITE001").one().use_count \
        == first_uses


def test_apply_invitation_still_works_when_promotion_enabled(db):
    inviter = _user(db, "boss")
    invitee = _user(db, "newbie")
    _code(db, inviter)
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "30"})

    result = apply_invitation(db, invitee, "INVITE001")
    db.commit()

    assert result["applied"] is True
    assert _balance(db, inviter.id) == 130        # 100 邀请 + 30 推广
    assert _balance(db, invitee.id) == 50
    rewards = _rewards(db, inviter.id)
    assert len(rewards) == 1 and rewards[0].reward_value == 30
    assert rewards[0].code_id is not None


# ===========================================================================
# 5. 注册渠道归因
# ===========================================================================

def test_register_channel_normalize_and_labels():
    assert register_channel.normalize(" CODE ") == register_channel.CODE
    assert register_channel.normalize("weird") == register_channel.UNKNOWN
    assert register_channel.normalize(None) == register_channel.UNKNOWN
    assert register_channel.label_of(None) == "未记录"
    assert register_channel.label_of("") == "未记录"
    assert register_channel.label_of("open") == "开放注册"
    assert register_channel.UNKNOWN not in register_channel.ALL


def test_register_channel_priority():
    """admin > code > invitation > open；邀请码没成立不得记 invitation"""
    # 卡密进门 + 邀请码成立 → 卡密赢（更硬的凭据）
    assert register_channel.resolve(register_channel.CODE, True) == register_channel.CODE
    # 管理员建号 → 永远是 admin
    assert register_channel.resolve(register_channel.ADMIN, True) == register_channel.ADMIN
    # 开放注册 + 邀请码成立 → 升为 invitation
    assert register_channel.resolve(register_channel.OPEN, True) == register_channel.INVITATION
    # 开放注册 + 邀请码无效/被拒 → 仍是 open
    assert register_channel.resolve(register_channel.OPEN, False) == register_channel.OPEN
    # 认不出的入口值 + 无邀请 → 未记录，不硬猜
    assert register_channel.resolve("junk", False) == register_channel.UNKNOWN
    assert register_channel.resolve("", False) == register_channel.UNKNOWN


def test_apply_invitation_failure_does_not_mark_invitation(db):
    """码无效时 resolve(OPEN, False) 必须留在 open"""
    inviter = _user(db, "boss")
    _user(db, "newbie", channel=register_channel.OPEN)
    assert apply_invitation(db, db.query(models.WebUser).filter_by(
        username="newbie").one(), "NOSUCHCODE")["applied"] is False


def test_list_users_filters_by_channel(db):
    _user(db, "a_admin", channel=register_channel.ADMIN)
    _user(db, "b_code", channel=register_channel.CODE)
    _user(db, "c_invite", channel=register_channel.INVITATION)
    _user(db, "d_open", channel=register_channel.OPEN)
    _user(db, "e_legacy")                      # NULL = 存量未记录
    _user(db, "f_blank", channel="")            # 空串同样算未记录
    admin = _user(db, "root", is_staff=True)

    def call(**kwargs):
        return admin_api.list_users(
            current_admin=admin, db=db, search="", active=None,
            limit=200, offset=0, **kwargs)

    assert call()["total"] == 7                      # 6 个测试号 + 管理员自己
    assert [u["username"] for u in call(channel="code")["users"]] == ["b_code"]
    assert sorted(u["username"] for u in call(channel=register_channel.ADMIN)["users"]) \
        == ["a_admin"]
    legacy = call(channel=admin_api.UNRECORDED_CHANNEL)["users"]
    # root 是直接造的（没走注册），所以它也属于「未记录」
    assert sorted(u["username"] for u in legacy) == ["e_legacy", "f_blank", "root"]
    # 非法值兜底成「未记录」，不得因为前端传错就把整页查空
    assert call(channel="not-a-channel")["total"] == 3

    labels = {u["username"]: u["register_channel_label"] for u in call()["users"]}
    assert labels["root"] == "未记录"                 # 管理员自己没归因（未走注册）
    assert labels["b_code"] == "卡密注册"
    assert labels["e_legacy"] == "未记录"
    assert labels["f_blank"] == "未记录"
    values = {c["value"] for c in call()["channels"]}
    assert values == set(register_channel.ALL) | {admin_api.UNRECORDED_CHANNEL}


# ===========================================================================
# 6. 管理端：邀请码批量生成 / 改配 / 作废（可逆）
# ===========================================================================

def test_generate_invitation_codes_batch(db):
    admin = _user(db, "root", is_staff=True)
    owner = _user(db, "boss")

    out = admin_ops.generate_invitation_codes(
        request=admin_ops.InvitationCodeGenerateRequest(
            owner_user_id=owner.id, count=5, max_uses=3, expires_days=30,
            whitelist=" Alice，Bob , alice ",
        ),
        current_admin=admin, db=db,
    )
    assert out["success"] is True
    assert len(out["codes"]) == 5
    assert len({c["code"] for c in out["codes"]}) == 5        # 批内不重号
    for item in out["codes"]:
        assert item["state"] == "active"
        assert item["max_uses"] == 3 and item["remaining"] == 3
        assert item["whitelist"] == ["alice", "bob"]          # 去重 + 小写
        assert item["owner_username"] == "boss"
        # 29~30 天都算对：days_left 是按整天向下取整的
        assert 29 <= item["expires_days_left"] <= 30

    assert db.query(models.InvitationCode).count() == 5
    audit = db.query(models.AdminLog).filter(
        models.AdminLog.action == "generate_invitation_codes").first()
    assert audit is not None


def test_generate_invitation_codes_unlimited_and_permanent(db):
    admin = _user(db, "root", is_staff=True)
    owner = _user(db, "boss")
    out = admin_ops.generate_invitation_codes(
        request=admin_ops.InvitationCodeGenerateRequest(
            owner_user_id=owner.id, count=1, max_uses=0, expires_days=0),
        current_admin=admin, db=db,
    )
    item = out["codes"][0]
    assert item["remaining"] is None          # None = 不限，不要显示 0
    assert item["expires_at"] is None
    assert item["whitelist"] == []


def test_generate_invitation_codes_unknown_owner_is_404(db):
    from fastapi import HTTPException

    admin = _user(db, "root", is_staff=True)
    with pytest.raises(HTTPException) as err:
        admin_ops.generate_invitation_codes(
            request=admin_ops.InvitationCodeGenerateRequest(owner_user_id=999, count=1),
            current_admin=admin, db=db,
        )
    assert err.value.status_code == 404


def test_update_and_revoke_invitation_code_are_reversible(db):
    admin = _user(db, "root", is_staff=True)
    owner = _user(db, "boss")
    code = _code(db, owner, code="CHAN0001", max_uses=10, whitelist="a")

    out = admin_ops.update_invitation_code(
        code.id,
        request=admin_ops.InvitationCodeUpdateRequest(max_uses=2, expires_days=0,
                                                      whitelist="B, c", is_active=False),
        current_admin=admin, db=db,
    )["code"]
    assert out["max_uses"] == 2
    assert out["expires_at"] is None                       # 0 = 永不过期
    assert out["whitelist"] == ["b", "c"]
    assert out["state"] == "revoked"

    # 作废只翻 is_active，不删行：已有的邀请关系还指着这张码
    revoked = admin_ops.revoke_invitation_codes(
        request=admin_ops.InvitationCodeRevokeRequest(ids=[code.id]),
        current_admin=admin, db=db,
    )
    assert revoked["count"] == 1
    assert db.query(models.InvitationCode).count() == 1
    assert apply_invitation(db, _user(db, "late"), "CHAN0001")["applied"] is False

    # 可逆：重新启用 + 清掉白名单后就能用了
    admin_ops.update_invitation_code(
        code.id, request=admin_ops.InvitationCodeUpdateRequest(
            is_active=True, whitelist=""),
        current_admin=admin, db=db,
    )
    assert apply_invitation(db, _user(db, "late2"), "CHAN0001")["applied"] is True


def test_list_invitation_codes_summary_and_states(db):
    admin = _user(db, "root", is_staff=True)
    owner = _user(db, "boss")
    _code(db, owner, code="AAA00001", max_uses=1, use_count=1)          # 已用完
    _code(db, owner, code="AAA00002", expires_at=datetime.now() - timedelta(days=1))  # 已过期
    _code(db, owner, code="BBB00003", is_active=False)                  # 已作废
    _code(db, owner, code="BBB00004", max_uses=5, use_count=2)          # 可用

    def call(**kwargs):
        params = {"keyword": "", "owner_user_id": None, "state": "",
                  "limit": 100, "offset": 0}
        params.update(kwargs)
        return admin_ops.list_invitation_codes(
            current_admin=admin, db=db, **params)

    full = call()
    assert full["total"] == 4
    assert full["summary"]["total"] == 4
    assert full["summary"]["used_up"] == 1
    assert full["summary"]["expired"] == 1
    assert full["summary"]["revoked"] == 1
    assert full["summary"]["active"] == 1
    assert full["summary"]["uses"] == 3

    # 默认按 id 倒序（新的在前）
    assert [c["code"] for c in call()["codes"]][:2] == ["BBB00004", "BBB00003"]
    assert sorted(c["code"] for c in call(keyword="aaa")["codes"]) \
        == ["AAA00001", "AAA00002"]
    assert [c["code"] for c in call(state="active")["codes"]] == ["BBB00004"]
    assert [c["code"] for c in call(owner_user_id=owner.id, state="revoked")["codes"]] \
        == ["BBB00003"]
    assert call(owner_user_id=999999)["total"] == 0
    used = next(c for c in call()["codes"] if c["code"] == "AAA00001")
    assert used["remaining"] == 0
    assert used["state_label"] == "已用完"


def test_admin_promotion_endpoints_round_trip(db):
    admin = _user(db, "root", is_staff=True)
    inviter = _user(db, "boss")
    _code(db, inviter)
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "40"})
    promotion.grant(db, inviter, _user(db, "newbie"), None)
    db.commit()

    got = admin_ops.get_promotion(current_admin=admin, db=db, limit=100, offset=0)
    assert got["policy"]["active"] is True
    assert got["policy"]["amount"] == 40
    assert got["summary"]["total"] == 1
    assert got["summary"]["reward_24h"] == 1
    assert got["rewards"][0]["inviter_username"] == "boss"
    assert got["rewards"][0]["invitee_username"] == "newbie"

    saved = admin_ops.update_promotion_policy(
        payload=admin_ops.PromotionPolicyRequest(
            policy={promotion.CONFIG_ENABLED: "false", promotion.CONFIG_AMOUNT: "99999999"}),
        current_admin=admin, db=db,
    )
    assert saved["policy"]["enabled"] is False
    # 非法值被夹住，不是原样写库
    assert saved["policy"]["amount"] == promotion.AMOUNT_MAX
    assert saved["policy"]["active"] is False
    assert db.query(models.AdminLog).filter(
        models.AdminLog.action == "promotion_policy_update").first() is not None


def test_promotion_rewards_for_own_user_only(db):
    inviter = _user(db, "boss")
    other = _user(db, "rival")
    _policy(db, **{promotion.CONFIG_ENABLED: "true", promotion.CONFIG_AMOUNT: "10"})
    promotion.grant(db, inviter, _user(db, "newbie"), None)
    promotion.grant(db, other, _user(db, "other_newbie"), None)
    db.commit()

    mine = promotion.rewards_for(db, inviter.id, 50)
    assert len(mine) == 1
    assert mine[0]["invitee_username"] == "newbie"
    assert mine[0]["reward_type_label"] == promotion.TYPE_LABELS[promotion.TYPE_BALANCE]
    assert promotion.rewards_for(db, inviter.id, 50)[0]["created_at"]


# ===========================================================================
# 7. 迁移/自愈：四个键必须被登记为系统配置默认值
# ===========================================================================

def test_system_config_defaults_include_promotion_keys():
    from backend.config_self_heal import collect_system_config_defaults

    defaults = dict((key, value) for key, value, *_ in collect_system_config_defaults())
    for key in (promotion.CONFIG_ENABLED, promotion.CONFIG_TYPE,
                promotion.CONFIG_AMOUNT, promotion.CONFIG_DAYS):
        assert key in defaults
    assert defaults[promotion.CONFIG_ENABLED] == "true"   # 2026-10-21 对齐生产现值（金额 0 兜底不发放）
    assert defaults[promotion.CONFIG_AMOUNT] == "0"
    assert defaults[promotion.CONFIG_DAYS] == "0"


def test_default_realm_still_claimable(db):
    """兜底建服路径：promotion 走 days 时会经过 realms.claim"""
    assert realms.claim(db, None) == realms.active_realm(db).id

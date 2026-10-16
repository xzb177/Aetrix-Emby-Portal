"""用户授权资源卡片（Phase 4）：按服组装授权状态与资源

全部离线：只测纯函数（backend/user_grants.py）与判定口径，不发网络请求、不起服务。

钉住的是三件容易写错的事：

1. **状态现算**：``UserSubscription.status`` 不会随时间自动翻成 expired，
   所以「已到期」必须按 ``end_date`` 与当下比较得出（这里造一条 status='active'
   但 end_date 在过去的行，不钉住这点以后就会有人改回读 status 字段）。
2. **判定带服**：同一用户在甲服有订阅、在乙服没有，两张卡必须给不同结论。
3. **公益服两件事分开**：能看（不需要订阅）与能看账号地址（要积分解锁）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, realms, subscriptions, user_grants
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _realm(db, name="主服", slug="main", access_mode="paid", active=True):
    realm = models.ServerRealm(
        name=name, slug=slug, access_mode=access_mode, is_active=active,
    )
    db.add(realm)
    db.commit()
    return realm


def _user(db, username="alice", is_staff=False):
    user = models.WebUser(username=username, password_hash="x", is_staff=is_staff)
    db.add(user)
    db.commit()
    return user


def _plan(db, realm, name="月卡"):
    plan = models.SubscriptionPlan(
        name=name, price=10, duration_days=30, realm_id=realm.id,
    )
    db.add(plan)
    db.commit()
    return plan


def _sub(db, user, realm, plan, *, end_in_days=30, status="active", auto_renew=False):
    sub = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id,
        start_date=datetime.now() - timedelta(days=1),
        end_date=datetime.now() + timedelta(days=end_in_days),
        status=status, auto_renew=auto_renew,
    )
    db.add(sub)
    db.commit()
    return sub


def _card(db, user, realm_id):
    payload = user_grants.cards(db, user)
    return next(c for c in payload["cards"] if c["realm_id"] == realm_id)


# ---------------------------------------------------------------------------
# 1. 付费服：订阅生效中 / 即将到期 / 已到期 / 未授权
# ---------------------------------------------------------------------------

def test_paid_realm_with_active_subscription(db):
    realm = _realm(db)
    user = _user(db)
    plan = _plan(db, realm)
    _sub(db, user, realm, plan, end_in_days=30)

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_SUBSCRIPTION
    assert card["state"] == "ok"
    assert card["can_play"] is True
    assert card["view_granted"] is True
    assert card["expiring_soon"] is False
    assert card["subscription"]["days_left"] == 30
    assert card["subscription"]["plan_name"] == "月卡"


def test_paid_realm_expiring_within_seven_days_is_warn(db):
    realm = _realm(db)
    user = _user(db)
    _sub(db, user, realm, _plan(db, realm), end_in_days=3)

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_SUBSCRIPTION
    assert card["expiring_soon"] is True
    assert card["state"] == "warn"
    assert card["subscription"]["days_left"] == 3


def test_expired_subscription_is_computed_from_end_date_not_status(db):
    """status 仍是 'active' 但 end_date 已过 —— 必须按已到期展示（老口径）"""
    realm = _realm(db)
    user = _user(db)
    _sub(db, user, realm, _plan(db, realm), end_in_days=-1, status="active")

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_EXPIRED
    assert card["state"] == "off"
    assert card["can_play"] is False
    assert card["subscription"]["days_left"] == 0
    # 过期卡仍要说得清他买的什么套餐、什么时候到期的
    assert card["subscription"]["plan_name"] == "月卡"
    assert card["subscription"]["end_date"] is not None


def test_paid_realm_without_subscription_is_none(db):
    realm = _realm(db)
    user = _user(db)
    _plan(db, realm)

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_NONE
    assert card["state"] == "off"
    assert card["can_play"] is False
    assert card["view_granted"] is False
    # 从没订阅过 → 没有订阅块（而不是造一个空壳）
    assert card["subscription"]["subscription_id"] is None


def test_cancelled_subscription_is_reported_separately_from_expiry(db):
    """退款关单会把订阅翻成 cancelled（orders_admin）——不能说成「自然到期」"""
    realm = _realm(db)
    user = _user(db)
    _sub(db, user, realm, _plan(db, realm, name="季卡"), end_in_days=-2, status="cancelled")

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_EXPIRED
    assert card["grant_label"] == user_grants.GRANT_LABEL_CANCELLED
    assert card["subscription"]["cancelled"] is True
    assert card["can_play"] is False


def test_expiring_boundary_matches_subscription_overview(db):
    """卡片「即将到期」= 订阅总览筛选的 end_date <= now + 7 天（含边界那一侧）

    7 天整算到期、8 天不算——与「订阅总览 / 服订阅清单」的 expiring 筛选一致，
    否则同一个用户会在两个页面看到不同的即将到期数。
    """
    soon = _realm(db, name="七天后", slug="soon")
    later = _realm(db, name="八天后", slug="later")
    user = _user(db)
    _sub(db, user, soon, _plan(db, soon), end_in_days=7)
    _sub(db, user, later, _plan(db, later), end_in_days=8)

    payload = user_grants.cards(db, user)
    by_realm = {c["realm_id"]: c for c in payload["cards"]}
    assert by_realm[soon.id]["expiring_soon"] is True
    assert by_realm[soon.id]["subscription"]["days_left"] == 7
    assert by_realm[later.id]["expiring_soon"] is False
    assert by_realm[later.id]["subscription"]["days_left"] == 8
    assert payload["summary"]["realms_expiring"] == 1


def test_auto_renew_is_reported(db):
    realm = _realm(db)
    user = _user(db)
    _sub(db, user, realm, _plan(db, realm), auto_renew=True)

    assert _card(db, user, realm.id)["subscription"]["auto_renew"] is True


# ---------------------------------------------------------------------------
# 2. 判定必须带服（多服部署）
# ---------------------------------------------------------------------------

def test_subscription_is_scoped_to_its_realm(db):
    a = _realm(db, name="甲服", slug="a")
    b = _realm(db, name="乙服", slug="b")
    user = _user(db)
    plan = _plan(db, a, name="甲服月卡")
    _sub(db, user, a, plan)

    card_a = _card(db, user, a.id)
    card_b = _card(db, user, b.id)
    assert card_a["grant"] == user_grants.GRANT_SUBSCRIPTION
    # 乙服没有订阅 —— 不能拿甲服的订阅当通用结论
    assert card_b["grant"] == user_grants.GRANT_NONE
    assert card_b["can_play"] is False
    assert card_b["subscription"]["plan_name"] == ""


def test_subscription_gate_off_lets_everyone_play_without_subscription(db):
    realm = _realm(db)
    user = _user(db)
    db.add(models.SystemConfig(key="subscription_required", value="false"))
    db.commit()

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_NONE      # 仍然没有订阅
    assert card["can_play"] is True                      # 但付费墙关了就放行
    # 账号地址仍不下发：付费服的查看权限只认订阅（与用户端账号卡同一口径）。
    # 卡片把这两件事分开显示，正是为了不让管理员以为「能播 = 能看地址」。
    assert card["view_granted"] is False


# ---------------------------------------------------------------------------
# 3. 公益服：能看 ≠ 能看账号地址
# ---------------------------------------------------------------------------

def test_free_realm_needs_no_subscription_but_needs_unlock_for_account(db):
    realm = _realm(db, name="公益服", slug="free", access_mode="free")
    user = _user(db)

    card = _card(db, user, realm.id)
    assert card["is_free"] is True
    assert card["grant"] == user_grants.GRANT_FREE_OPEN
    assert card["can_play"] is True          # 免费开放：不需要订阅
    assert card["view_granted"] is False     # 但账号地址要花积分解锁
    assert card["access_note"]              # 公益服规则文案下发给管理员看


def test_free_realm_unlock_grants_view_permission(db):
    realm = _realm(db, name="公益服", slug="free", access_mode="free")
    user = _user(db)
    db.add(models.EmbyViewUnlock(
        user_id=user.id, realm_id=realm.id, points_spent=50,
        expires_at=datetime.now() + timedelta(days=30),
    ))
    db.commit()

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_UNLOCK
    assert card["state"] == "ok"
    assert card["view_granted"] is True
    assert card["unlock"]["points_spent"] == 50
    assert card["unlock"]["expires_at"] is not None


def test_expired_unlock_does_not_grant_view_permission(db):
    realm = _realm(db, name="公益服", slug="free", access_mode="free")
    user = _user(db)
    db.add(models.EmbyViewUnlock(
        user_id=user.id, realm_id=realm.id, points_spent=50,
        expires_at=datetime.now() - timedelta(days=1),
    ))
    db.commit()

    card = _card(db, user, realm.id)
    assert card["grant"] == user_grants.GRANT_FREE_OPEN
    assert card["view_granted"] is False
    assert card["can_play"] is True      # 公益服照看不误


def test_free_realm_download_follows_realm_policy(db):
    realm = _realm(db, name="公益服", slug="free", access_mode="free")
    realm.allow_download = False
    db.commit()
    user = _user(db)

    assert _card(db, user, realm.id)["download_allowed"] is False


# ---------------------------------------------------------------------------
# 4. 卡片集 / 汇总
# ---------------------------------------------------------------------------

def test_cards_cover_every_realm_and_put_playable_first(db):
    a = _realm(db, name="甲服", slug="a")
    b = _realm(db, name="乙服", slug="b")
    user = _user(db)
    _sub(db, user, a, _plan(db, a))

    payload = user_grants.cards(db, user)
    assert [c["realm_id"] for c in payload["cards"]] == [a.id, b.id]
    assert payload["summary"]["realms_total"] == 2
    assert payload["summary"]["realms_playable"] == 1
    assert payload["summary"]["realms_expired"] == 0
    assert payload["user"]["username"] == "alice"


def test_summary_counts_expiring_and_expired_realms(db):
    ok = _realm(db, name="正常服", slug="ok")
    soon = _realm(db, name="到期服", slug="soon")
    gone = _realm(db, name="过期服", slug="gone")
    user = _user(db)
    _sub(db, user, ok, _plan(db, ok), end_in_days=20)
    _sub(db, user, soon, _plan(db, soon), end_in_days=2)
    _sub(db, user, gone, _plan(db, gone), end_in_days=-5)

    summary = user_grants.cards(db, user)["summary"]
    assert summary["realms_expiring"] == 1
    assert summary["realms_expired"] == 1
    assert summary["realms_playable"] == 2


def test_summary_reports_devices_and_play_line(db):
    realm = _realm(db)
    user = _user(db)
    _sub(db, user, realm, _plan(db, realm))
    db.add(models.SystemConfig(key="device_limit_per_user", value="2"))
    db.add(models.UserDevice(
        user_id=user.id, device_id="dev-1", name="客厅电视",
        last_seen_at=datetime.now(),
    ))
    db.commit()

    summary = user_grants.cards(db, user)["summary"]
    assert summary["device_limit"] == 2
    assert summary["devices_used"] == 1
    # 没有偏好记录就是默认 302 直链（前端据此显示线路名而不是空白）
    assert summary["play_line"] == "relay"
    assert summary["play_line_label"] == "302 直链"


def test_disabled_realm_is_listed_and_marked(db):
    realm = _realm(db, name="停用服", slug="off", active=False)
    user = _user(db)

    card = _card(db, user, realm.id)
    assert card["is_active"] is False
    assert card["realm_name"] == "停用服"


def test_staff_user_can_play_every_realm(db):
    realm = _realm(db)
    admin = _user(db, username="root", is_staff=True)

    card = _card(db, admin, realm.id)
    assert card["grant"] == user_grants.GRANT_NONE   # 确实没有订阅
    assert card["can_play"] is True                  # 但管理员始终放行


# ---------------------------------------------------------------------------
# 5. 资源统计与 view_grant 单一事实来源
# ---------------------------------------------------------------------------

def test_resource_stats_failure_shows_unknown_not_zero(db, monkeypatch):
    """统计读不到时显示「—」（null），不能谎报 0——否则像内容被删了"""
    realm = _realm(db)
    user = _user(db)

    def boom(_db, _realm_id):
        raise RuntimeError("统计炸了")

    monkeypatch.setattr(realms, "stats", boom)
    card = _card(db, user, realm.id)
    assert card["resources"]["libraries"] is None
    assert card["resources"]["items"] is None
    # 授权结论仍然可用（资源读不到不影响「能不能看」）
    assert card["grant"] == user_grants.GRANT_NONE


def test_card_reports_realm_resources(db):
    realm = _realm(db)
    user = _user(db)
    lib = em.Library(guid="lib-1", name="电影库", realm_id=realm.id, is_enabled=True)
    off = em.Library(guid="lib-2", name="停用库", realm_id=realm.id, is_enabled=False)
    db.add_all([lib, off])
    db.commit()
    db.add(em.MediaItem(guid="item-1", name="测试影片", library_id=lib.id, item_type="movie"))
    db.commit()

    card = _card(db, user, realm.id)
    assert card["resources"]["libraries"] == 2
    assert card["resources"]["enabled_libraries"] == 1
    assert card["resources"]["items"] == 1


def test_view_grant_matches_card_verdict(db):
    """卡片与用户端账号卡读同一份判定（subscriptions.view_grant）"""
    paid = _realm(db, name="甲服", slug="a")
    free = _realm(db, name="公益服", slug="free", access_mode="free")
    user = _user(db)
    _sub(db, user, paid, _plan(db, paid))

    assert subscriptions.view_grant(db, user, paid.id)[0] is True
    assert subscriptions.view_grant(db, user, free.id)[0] is False
    assert _card(db, user, paid.id)["view_granted"] is True
    assert _card(db, user, free.id)["view_granted"] is False


def test_no_realms_returns_empty_cards(db):
    user = _user(db)
    payload = user_grants.cards(db, user)
    assert payload["cards"] == []
    assert payload["summary"]["realms_total"] == 0
    assert payload["summary"]["realms_playable"] == 0


def test_default_realm_flag(db):
    first = _realm(db, name="第一个服", slug="first")
    second = _realm(db, name="第二个服", slug="second")
    user = _user(db)

    assert _card(db, user, first.id)["is_default"] is True
    assert _card(db, user, second.id)["is_default"] is False
    assert realms.legacy_realm_id(db) == first.id

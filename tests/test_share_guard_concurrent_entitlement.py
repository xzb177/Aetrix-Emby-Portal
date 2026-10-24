"""订阅分档 PR-3：同播路数接入权益单口（share_guard._effective_concurrent_limit）。

钉住的口径（设计 reports/subscription-tier-design-20261024.md §4）：

- 同一闸门、两层口径：闸门只换「上限数字的来源」——用户当前套餐声明了
  ``concurrent_streams`` 就用声明值，没声明回落现行全局
  ``share_concurrent_limit``（默认 2）。四档处置（off/record/alert/
  enforce）、事件结构、拦截点全不动。
- 上线兼容红线：action=off 时闸门开头即返回，路数来源根本不被问到——
  填任何权益都零事件零拦截，与升级前逐字一致；record/enforce 下未声明
  权益时行为（事件 detail、拦截点）与改前逐字一致（对照组）。
- staff 豁免留在消费方，不变；entitlements 两级缓存命中时 note_session
  不额外查订阅两表（查询计数断言）。

全部用隔离的内存 SQLite，geoip 用 monkeypatch 假掉，不碰生产库。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import entitlements, models, share_guard
from backend.emby_server import models as em
from backend.integrations import store

KEY = "concurrent_streams"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 配置热缓存、权益缓存、告警节流都是进程级的：进出都清一次，
    # 否则内存库里各自从 1 开始的 user_id 会互相串味
    store.invalidate()
    share_guard._notified_at.clear()
    entitlements._PLAN_SNAPSHOT_CACHE.clear()
    entitlements._USER_PLAN_CACHE.clear()
    try:
        yield session
    finally:
        store.invalidate()
        share_guard._notified_at.clear()
        entitlements._PLAN_SNAPSHOT_CACHE.clear()
        entitlements._USER_PLAN_CACHE.clear()
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _fake_geo(monkeypatch):
    """城市固定查不到：同播 detail 走「归属地未知」分支，不碰真实 geoip"""
    monkeypatch.setattr(share_guard, "city_of", lambda db, ip: "")


def _user(db, username="alice", **kw):
    row = models.WebUser(username=username, password_hash="x", **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _policy(db, **overrides):
    values = {
        share_guard.CONFIG_CONCURRENT_ACTION: "off",
        share_guard.CONFIG_CONCURRENT_LIMIT: "2",
    }
    values.update(overrides)
    share_guard.write_policy(db, values)
    db.commit()
    return values


def _plan(db, name="环游执照", entitlements_value=None):
    row = models.SubscriptionPlan(
        name=name, price=100, duration_days=30, is_active=True,
        entitlements=entitlements_value)
    db.add(row)
    db.commit()
    return row


def _subscribe(db, user, plan, days=30):
    now = datetime.now()
    row = models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=None,
        start_date=now, end_date=now + timedelta(days=days), status="active")
    db.add(row)
    db.commit()
    return row


def _live(db, user_id, key):
    row = em.PlaybackSession(
        session_key=key, user_id=user_id, item_id=1,
        client_name="Infuse", remote_addr="1.1.1.1")
    db.add(row)
    db.commit()
    return row


def _events(db, user_id):
    return (
        db.query(models.ShareGuardEvent)
        .filter(models.ShareGuardEvent.user_id == user_id)
        .order_by(models.ShareGuardEvent.id.asc())
        .all()
    )


# ==================== 1. off 红线：填任何权益都零事件零拦截 ====================


def test_off_with_entitlement_still_zero_events(db):
    """action=off + 套餐声明了路数：闸门开头即返回，与今天逐字一致。"""
    user = _user(db)
    _subscribe(db, user, _plan(db, entitlements_value={KEY: 1}))
    _policy(db, share_concurrent_action="off", share_concurrent_limit="1")
    # 已有会话远超全局 1 与权益 1，新会话照样不判定、不写事件
    _live(db, user.id, "s1")
    _live(db, user.id, "s2")
    assert share_guard.note_session(db, user, "s3", "1.2.3.4") is None
    assert _events(db, user.id) == []


# ==================== 2. record 按权益值记事件 ====================


def test_record_uses_entitlement_limit(db):
    """权益 3（全局 2）：第 3 路不记（超全局也不能记），第 4 路才记。"""
    user = _user(db)
    _subscribe(db, user, _plan(db, entitlements_value={KEY: 3}))
    _policy(db, share_concurrent_action="record", share_concurrent_limit="2")

    _live(db, user.id, "s1")
    _live(db, user.id, "s2")
    # 此时 others=2：第 3 路。按全局 2 本该记事件，按权益 3 不记
    assert share_guard.note_session(db, user, "s3", "1.2.3.4") is None
    assert _events(db, user.id) == []

    _live(db, user.id, "s3")
    # others=3：第 4 路才记；record 不拦
    result = share_guard.note_session(db, user, "s4", "1.2.3.4")
    assert result is not None and result["blocked"] is False
    events = _events(db, user.id)
    assert len(events) == 1
    assert events[0].kind == share_guard.KIND_CONCURRENT
    assert events[0].action == "record"
    assert events[0].sessions == 4
    assert "同时播放 4 路（上限 3）" in (events[0].detail or "")


# ==================== 3. enforce 未声明权益：与改前逐字一致（对照组） ====================


def test_enforce_without_entitlement_matches_legacy(db):
    """有订阅但套餐没声明 concurrent_streams → 全局 2：第 3 路拦，detail 同改前。"""
    user = _user(db)
    _subscribe(db, user, _plan(db, entitlements_value={"media_seek_monthly_limit": 20}))
    _policy(db, share_concurrent_action="enforce", share_concurrent_limit="2")

    _live(db, user.id, "s1")
    assert share_guard.note_session(db, user, "s2", "1.2.3.4") is None

    _live(db, user.id, "s2")
    result = share_guard.note_session(db, user, "s3", "1.2.3.4")
    assert result is not None and result["blocked"] is True
    assert result["sessions"] == 3
    events = _events(db, user.id)
    assert len(events) == 1
    assert "同时播放 3 路（上限 2）" in (events[0].detail or "")


def test_enforce_no_subscription_matches_legacy(db):
    """无订阅用户同样走全局 2（回退链最末端与改前一致）。"""
    user = _user(db)
    _policy(db, share_concurrent_action="enforce", share_concurrent_limit="2")
    _live(db, user.id, "s1")
    _live(db, user.id, "s2")
    result = share_guard.note_session(db, user, "s3", "1.2.3.4")
    assert result is not None and result["blocked"] is True


# ==================== 4. enforce + 权益 4：第 5 路才拦 ====================


def test_enforce_with_entitlement_four(db):
    """权益 4：前 4 路会话正常建立（不拦），第 5 路才拦。"""
    user = _user(db)
    _subscribe(db, user, _plan(db, entitlements_value={KEY: 4}))
    _policy(db, share_concurrent_action="enforce", share_concurrent_limit="2")

    for i in range(1, 5):
        # 前 4 路：每次判定都放行（others 从 0 到 3，均 < 4）
        assert share_guard.note_session(db, user, f"s{i}", "1.2.3.4") is None
        _live(db, user.id, f"s{i}")
    assert _events(db, user.id) == []

    result = share_guard.note_session(db, user, "s5", "1.2.3.4")
    assert result is not None and result["blocked"] is True
    assert result["sessions"] == 5
    events = _events(db, user.id)
    assert len(events) == 1
    assert "同时播放 5 路（上限 4）" in (events[0].detail or "")


# ==================== 5. staff 豁免不变 ====================


def test_staff_exempt_unchanged(db):
    """staff：enforce + 无权益 + 已超全局上限，仍直接放行、零事件。"""
    user = _user(db, username="boss", is_staff=True)
    _policy(db, share_concurrent_action="enforce", share_concurrent_limit="2")
    _live(db, user.id, "s1")
    _live(db, user.id, "s2")
    _live(db, user.id, "s3")
    assert share_guard.note_session(db, user, "s4", "1.2.3.4") is None
    assert _events(db, user.id) == []


# ==================== 6. 缓存命中时不额外查订阅表 ====================


def test_cache_hit_adds_no_subscription_queries(db):
    """热路径预算：权益两级缓存命中后，note_session 对订阅两表零查询。"""
    user = _user(db)
    _subscribe(db, user, _plan(db, entitlements_value={KEY: 3}))
    _policy(db, share_concurrent_action="record", share_concurrent_limit="2")
    _live(db, user.id, "s1")

    # 先走一次完整判定把两级缓存焐热（未命中时允许查订阅表）
    assert share_guard.note_session(db, user, "sx", "1.2.3.4") is None

    engine = db.get_bind()
    hits = {"user_subscriptions": 0, "subscription_plans": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        text = statement.lower()
        for table in hits:
            if f"from {table}" in text or f"from main.{table}" in text:
                hits[table] += 1

    try:
        # 缓存命中下再判定一次（others=1 < 权益 3，放行）：订阅两表必须零查询
        assert share_guard.note_session(db, user, "sy", "1.2.3.4") is None
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert hits == {"user_subscriptions": 0, "subscription_plans": 0}


# ==================== 附：生效上限单口与策略快照 ====================


def test_effective_limit_fallback_chain(db):
    """_effective_concurrent_limit：声明用声明，未声明/过期回全局现值。"""
    plain = _user(db, username="plain")
    entitled = _user(db, username="rich")
    _subscribe(db, entitled, _plan(db, entitlements_value={KEY: 5}))
    expired = _user(db, username="old")
    _subscribe(db, expired, _plan(db, name="启程执照",
                                   entitlements_value={KEY: 5}), days=-1)
    _policy(db, share_concurrent_limit="2")
    assert share_guard._effective_concurrent_limit(db, plain) == 2
    assert share_guard._effective_concurrent_limit(db, entitled) == 5
    assert share_guard._effective_concurrent_limit(db, expired) == 2


def test_policy_payload_has_entitlement_note(db):
    """后台策略快照带只读说明字段；写路径白名单不含它（写了也不生效）。"""
    _policy(db, share_concurrent_limit="2")
    payload = share_guard.policy_payload(db)
    assert payload["concurrent_limit"] == 2
    assert "套餐权益" in payload["entitlement_note"]
    applied = share_guard.write_policy(db, {"entitlement_note": "x"})
    assert "entitlement_note" not in applied

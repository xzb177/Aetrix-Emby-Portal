# -*- coding: utf-8 -*-
"""公共贡献池 PR-5（goal-33）：熔断/半开、池级熔断、副本上限、
pool_janitor 巡检（对账/淘汰/残留收尾/补位）、GA-035 上游线路
取流口池分支。

钉死（design §6.3/§6.5 + 补充节 S4 + GA-036/GA-035）：
- 成员当日 quota_hits 达 pool_breaker_quota_hits_per_day →
  suspended(breaker) + breaker_until，H2 流水与 pool_breaker
  留痕齐；阈值 0 不熔断；未达线不动；
- 半开试探：冷却到期探一份副本，非 quota 恢复 active（回合
  清零），quota 立即回熔断且冷却翻倍；
- 池级熔断：参与成员人均命中达阈值 → create_pool_job 抛
  pool_breaker；条目可下发副本 + 在途 ≥ pool_item_max_copies
  → 抛 copies_max（熔断/暂停盘的副本不占名额）；
- janitor：绑定失效兜底落 suspended 且其 ready 副本标
  stale；死副本对账 strike 两次标 stale、源 fid 对不上立即
  stale、条目没了标 orphan；patrol_once 对池行整行跳过；
- 淘汰：超上限按 orphan → stale → 闲置冷 ready 顺序删到
  95% 滞后带，保留期与正在播跳过，删失败标 orphan；
- 残留收尾：终态成员 released 行逐个删文件并落标记，绑定
  已死的落 closed 标记收口、不重复处理；
- 补位：近 7 天有起播且因下线跌破份数的热条目才补，队列
  忙时整步跳过，补位单 trigger=refill 且不打已持副本的盘；
- GA-035：未绑池观众在详情拿到带签上游线路后，取流口同
  口径放行（不再 403）；池关时仍 403。

服务级测试用独立内存库（照 test_pool_lifecycle 夹具）；GA-035
用真 TestClient（照 test_pool_serve 种子形态，独立命名）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import config_service, models as base_models
from backend.emby_server import models as em
from backend.emby_server import pool_gate, pool_janitor
from backend.emby_server import transfer_decisions as td
from backend.emby_server import transfer_variant as tv
from backend.integrations import store

FULL_SCOPES = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/drive.readonly")
ORIG_FID = "guardorigfid"
TB = 1000 ** 4


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _note_here(db_factory, monkeypatch):
    """决策留痕写进本测试库（照 test_pool_intake 口径）。"""
    monkeypatch.setattr(td, "SessionLocal", db_factory)


@pytest.fixture(autouse=True)
def _clean_caches():
    yield
    store.invalidate()
    pool_gate.clear_pool_quota_cache()


@pytest.fixture(autouse=True)
def _transfer_master_on(monkeypatch):
    monkeypatch.setenv("TRANSFER_VARIANT_ENABLED", "1")


@pytest.fixture(autouse=True)
def _fake_token_ok(monkeypatch):
    """janitor 令牌探活在本文件默认打桩为可用（令牌刷新/落态
    本体是 drive_oauth 的测试面；需要验兜底的用例单独改桩）。"""
    monkeypatch.setattr(
        pool_gate, "pool_member_access_token",
        lambda db, member_id: "token-ok")


def _set_pool(db, **values):
    config_service.save_config(db, values, schema="pool")
    db.commit()


def _user(db, name):
    user = base_models.WebUser(username=name, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _binding(db, user_id, status="active", provider="drive"):
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status=status)
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label="贡献盘",
        credential_id=cred.id, status=status,
        granted_scopes=FULL_SCOPES)
    db.add(binding)
    db.commit()
    return binding


def _member(db, user_id, status="active", binding=True, **kw):
    binding_id = _binding(db, user_id).id if binding else None
    row = em.PoolMember(
        binding_id=binding_id, user_id=user_id,
        provider="drive" if binding_id else None, status=status, **kw)
    db.add(row)
    db.commit()
    return row


def _usage(db, member_id, *, quota_hits=0, play_starts=0):
    row = em.PoolUsageDaily(
        member_id=member_id, day=datetime.now().date(),
        transfer_jobs=0, transfer_bytes=0,
        play_starts=play_starts, quota_hits=quota_hits)
    db.add(row)
    db.commit()
    return row


def _mount(db, mount_id=3):
    existing = (db.query(em.StorageMount)
                .filter(em.StorageMount.id == mount_id).first())
    if existing is not None:
        return existing
    row = em.StorageMount(id=mount_id, name=f"m{mount_id}",
                          mount_type="drive", path="drive:drive1")
    db.add(row)
    db.commit()
    return row


def _item(db, size=1_000_000, fid=ORIG_FID):
    _mount(db)
    lib = em.Library(guid=uuid.uuid4().hex, name="池守卫测试库")
    db.add(lib)
    db.commit()
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name="池守卫片", drive_file_id=fid,
        file_path="mount://3/电影/a.mkv", size=size)
    db.add(item)
    db.commit()
    return item


def _variant(db, item_id, member_id, status="ready", stale_count=0,
             fid=None, size=1000, last_played_at=None, created_days=0,
             retention_until=None):
    row = em.TransferVariant(
        item_id=item_id, source_file_id=ORIG_FID,
        variant_file_id=fid or f"pool-fid-{member_id}-{item_id}-"
                               f"{uuid.uuid4().hex[:6]}",
        variant_drive_id=f"pooldrive:{member_id}",
        owner_scope="pool", owner_ref=str(member_id), status=status,
        stale_count=stale_count, size_bytes=size,
        last_played_at=last_played_at,
        retention_until=retention_until)
    db.add(row)
    db.flush()
    if created_days:
        row.created_at = datetime.now() - timedelta(days=created_days)
    db.commit()
    return row


def _events(db, member_id):
    return [
        (e.from_status, e.to_status, e.reason)
        for e in db.query(em.PoolMemberStatusEvent)
        .filter(em.PoolMemberStatusEvent.member_id == member_id)
        .order_by(em.PoolMemberStatusEvent.id.asc())
        .all()
    ]


def _decisions(db, scope):
    return (db.query(em.TransferDecisionRound)
            .filter(em.TransferDecisionRound.scope == scope)
            .order_by(em.TransferDecisionRound.id.asc())
            .all())


def _quota_ok(db, binding):
    return {"limit": 5 * TB, "usage": 1 * TB, "free": 4 * TB}


class _FakeClient:
    """成员盘客户端替身：get/delete 行为可配，调用留痕。"""

    def __init__(self, *, get_error=None, delete_error=None):
        self.get_error = get_error
        self.delete_error = delete_error
        self.get_calls = []
        self.delete_calls = []

    def get_file(self, drive_id, fid):
        self.get_calls.append((drive_id, fid))
        if self.get_error is not None:
            raise self.get_error
        return {"id": fid}

    def delete_file_ignore_missing(self, drive_id, fid):
        self.delete_calls.append((drive_id, fid))
        if self.delete_error is not None:
            raise self.delete_error


def _factory(client):
    return lambda db, binding: client


# ------------------------------------------------------------ 熔断（成员级）


def test_breaker_trips_at_threshold(db):
    member = _member(db, _user(db, "cb1").id)
    _usage(db, member.id, quota_hits=3)
    assert pool_gate.maybe_trip_member_breaker(db, member) is True
    db.commit()
    db.refresh(member)
    assert member.status == "suspended"
    assert member.suspended_reason == "breaker"
    assert member.breaker_until is not None
    delta = (member.breaker_until - datetime.now()).total_seconds()
    assert 3500 < delta <= 3600
    assert ("active", "suspended", "breaker") in _events(db, member.id)
    rounds = _decisions(db, "pool_breaker")
    assert rounds and rounds[-1].decision == "trip"


def test_breaker_below_threshold_and_zero_threshold(db):
    member = _member(db, _user(db, "cb2").id)
    _usage(db, member.id, quota_hits=2)
    assert pool_gate.maybe_trip_member_breaker(db, member) is False
    db.refresh(member)
    assert member.status == "active"
    _set_pool(db, pool_breaker_quota_hits_per_day=0)
    row = (db.query(em.PoolUsageDaily)
           .filter(em.PoolUsageDaily.member_id == member.id).first())
    row.quota_hits = 99
    db.commit()
    assert pool_gate.maybe_trip_member_breaker(db, member) is False
    db.refresh(member)
    assert member.status == "active"


def test_note_pool_quota_hit_bumps_and_trips(db, db_factory, monkeypatch):
    import backend.database as _dbmod

    monkeypatch.setattr(_dbmod, "SessionLocal", db_factory)
    member = _member(db, _user(db, "cb3").id)
    _usage(db, member.id, quota_hits=2)
    pool_gate.note_pool_quota_hit(member.id)
    s2 = db_factory()
    try:
        row = (s2.query(em.PoolUsageDaily)
               .filter(em.PoolUsageDaily.member_id == member.id)
               .first())
        assert row.quota_hits == 3
        fresh = (s2.query(em.PoolMember)
                 .filter(em.PoolMember.id == member.id).first())
        assert fresh.status == "suspended"
        assert fresh.suspended_reason == "breaker"
    finally:
        s2.close()


def test_breaker_recover_probe_success(db):
    member = _member(db, _user(db, "cb4").id, status="suspended",
                     suspended_reason="breaker",
                     breaker_until=datetime.now() - timedelta(minutes=5))
    item = _item(db)
    _variant(db, item.id, member.id)
    outcome = pool_gate.breaker_recover_probe(
        db, member, probe_fn=lambda variant: "ok")
    db.commit()
    assert outcome == "recovered"
    db.refresh(member)
    assert member.status == "active"
    assert member.breaker_until is None
    assert member.suspended_reason is None
    assert ("suspended", "active", "breaker_recovered") in \
        _events(db, member.id)


def test_breaker_recover_probe_quota_retrips_doubled(db):
    member = _member(db, _user(db, "cb5").id, status="suspended",
                     suspended_reason="breaker",
                     breaker_until=datetime.now() - timedelta(minutes=5))
    # 本回合已有一次 trip 事件：再熔断冷却应翻倍（3600 × 2）。
    pool_gate.record_member_status_event(
        db, member, "active", "suspended", "breaker")
    db.commit()
    item = _item(db)
    _variant(db, item.id, member.id)
    outcome = pool_gate.breaker_recover_probe(
        db, member, probe_fn=lambda variant: "quota")
    db.commit()
    assert outcome == "retripped"
    db.refresh(member)
    assert member.status == "suspended"
    delta = (member.breaker_until - datetime.now()).total_seconds()
    assert 7100 < delta <= 7200
    reasons = [e[2] for e in _events(db, member.id)]
    assert "breaker_retrip" in reasons


def test_breaker_recover_not_due(db):
    member = _member(db, _user(db, "cb6").id, status="suspended",
                     suspended_reason="breaker",
                     breaker_until=datetime.now() + timedelta(hours=1))
    assert pool_gate.breaker_recover_probe(
        db, member, probe_fn=lambda variant: "ok") == "not_due"
    db.refresh(member)
    assert member.status == "suspended"


# ------------------------------------------------------------ 池级熔断与副本上限


def test_pool_wide_breaker_open(db):
    m1 = _member(db, _user(db, "pw1").id)
    m2 = _member(db, _user(db, "pw2").id)
    _usage(db, m1.id, quota_hits=3)
    _usage(db, m2.id, quota_hits=2)
    # 人均 2.5 < 阈值 3：不开。
    assert pool_gate.pool_wide_breaker_open(db) is False
    row = (db.query(em.PoolUsageDaily)
           .filter(em.PoolUsageDaily.member_id == m2.id).first())
    row.quota_hits = 3
    db.commit()
    # 人均 3 ≥ 3：全池暂停建单。
    assert pool_gate.pool_wide_breaker_open(db) is True


def test_create_job_blocked_by_pool_breaker(db):
    _set_pool(db, pool_enabled=True)
    m1 = _member(db, _user(db, "pw3").id)
    m2 = _member(db, _user(db, "pw4").id)
    _usage(db, m1.id, quota_hits=3)
    _usage(db, m2.id, quota_hits=3)
    item = _item(db)
    with pytest.raises(tv.TransferError) as exc:
        pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert exc.value.kind == "pool_breaker"


def test_create_job_blocked_by_copies_max(db):
    _set_pool(db, pool_enabled=True)
    ma = _member(db, _user(db, "pw5").id)
    _member(db, _user(db, "pw6").id)
    item = _item(db)
    _variant(db, item.id, ma.id)
    _variant(db, item.id, ma.id)
    # 已有 2 份可下发副本（默认上限 2）：再建单被 copies_max 拦。
    with pytest.raises(tv.TransferError) as exc:
        pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert exc.value.kind == "copies_max"


def test_create_job_copies_on_suspended_member_do_not_block(db):
    _set_pool(db, pool_enabled=True)
    ma = _member(db, _user(db, "pw7").id, status="suspended",
                 suspended_reason="manual")
    mb = _member(db, _user(db, "pw8").id)
    item = _item(db)
    _variant(db, item.id, ma.id)
    _variant(db, item.id, ma.id)
    # 暂停盘上的副本不占名额（§6.3）：仍可往健康盘建单补位。
    job = pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert job.owner_ref == str(mb.id)


def test_create_job_normal_path_unaffected(db):
    _set_pool(db, pool_enabled=True)
    member = _member(db, _user(db, "pw9").id)
    item = _item(db)
    job = pool_gate.create_pool_job(db, item, quota_fn=_quota_ok)
    assert job.status == "pending"
    assert job.owner_ref == str(member.id)

# ------------------------------------------------------------ janitor：熔断兜底扫


def test_janitor_round_trips_member(db):
    member = _member(db, _user(db, "jn1").id)
    _usage(db, member.id, quota_hits=3)
    stats = pool_janitor.run_pool_janitor_once(db)
    assert stats["breaker_trips"]["tripped"] == 1
    db.refresh(member)
    assert member.status == "suspended"
    assert member.suspended_reason == "breaker"


def test_janitor_round_recovers_due_breaker(db):
    member = _member(db, _user(db, "jn2").id, status="suspended",
                     suspended_reason="breaker",
                     breaker_until=datetime.now() - timedelta(minutes=5))
    item = _item(db)
    _variant(db, item.id, member.id)
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        probe_fn=lambda variant: "ok")
    assert stats["breaker_recovery"]["recovered"] == 1
    db.refresh(member)
    assert member.status == "active"


# ------------------------------------------------------------ janitor：绑定兜底


def test_binding_backstop_suspends_and_stales_copies(db):
    user = _user(db, "jn3")
    member = _member(db, user.id)
    binding = pool_gate._member_binding(db, member)
    binding.status = "invalid"
    db.commit()
    item = _item(db)
    variant = _variant(db, item.id, member.id)
    stats = pool_janitor.run_pool_janitor_once(db)
    assert stats["binding_backstop"]["suspended"] == 1
    assert stats["binding_backstop"]["copies_staled"] == 1
    db.refresh(member)
    assert member.status == "suspended"
    assert member.suspended_reason == "binding_invalid"
    db.refresh(variant)
    assert variant.status == "stale"


def test_token_probe_backstop_when_refresh_marked_binding(db, monkeypatch):
    """令牌刷新失败已落绑定状态、但成员没跟上暂停时，探活步
    用 on_binding_failed 补齐（fake 桩模拟 drive_oauth 落态）。"""
    user = _user(db, "jn4")
    member = _member(db, user.id)

    def _dead_token(db_, member_id):
        binding = (db_.query(em.UserStorageBinding)
                   .filter(em.UserStorageBinding.user_id == user.id)
                   .first())
        binding.status = "invalid"
        db_.commit()
        return ""

    monkeypatch.setattr(pool_gate, "pool_member_access_token",
                        _dead_token)
    stats = pool_janitor.run_pool_janitor_once(db)
    assert stats["token_probe"]["suspended"] == 1
    db.refresh(member)
    assert member.status == "suspended"
    assert member.suspended_reason == "binding_invalid"


# ------------------------------------------------------------ janitor：死副本对账


def test_reconcile_second_strike_marks_stale(db):
    member = _member(db, _user(db, "jn5").id)
    item = _item(db)
    variant = _variant(db, item.id, member.id, stale_count=1)
    fake = _FakeClient(get_error=tv._NotFound("gone"))
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake))
    assert stats["reconcile"]["stale"] == 1
    db.refresh(variant)
    assert variant.status == "stale"
    assert variant.stale_count == 2


def test_reconcile_source_fid_mismatch_stale_without_probe(db):
    member = _member(db, _user(db, "jn6").id)
    item = _item(db, fid="newfid")
    variant = _variant(db, item.id, member.id)
    variant.source_file_id = "oldfid"
    db.commit()
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake))
    assert stats["reconcile"]["stale"] == 1
    assert fake.get_calls == []
    db.refresh(variant)
    assert variant.status == "stale"


def test_reconcile_item_gone_marks_orphan(db):
    member = _member(db, _user(db, "jn7").id)
    variant = _variant(db, 999999, member.id)
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()))
    assert stats["reconcile"]["orphaned"] == 1
    db.refresh(variant)
    assert variant.status == "orphan"


def test_reconcile_healthy_resets_strikes(db):
    member = _member(db, _user(db, "jn8").id)
    item = _item(db)
    variant = _variant(db, item.id, member.id, stale_count=1)
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()))
    assert stats["reconcile"]["checked"] == 1
    db.refresh(variant)
    assert variant.status == "ready"
    assert variant.stale_count == 0
    assert variant.last_checked_at is not None


def test_patrol_once_skips_pool_rows(db):
    member = _member(db, _user(db, "jn9").id)
    item = _item(db)
    pool_row = _variant(db, item.id, member.id)
    user_row = em.TransferVariant(
        item_id=item.id, source_file_id=ORIG_FID,
        variant_file_id="usercopy", variant_drive_id="userdrive:1",
        owner_scope="user", owner_ref="1", status="ready",
        stale_count=0, size_bytes=10)
    db.add(user_row)
    db.commit()

    class _SAClient:
        def get_file(self, drive_id, fid):
            raise tv._NotFound("gone")

    stats = tv.patrol_once(db, lambda: _SAClient())
    db.refresh(pool_row)
    # 池行整行跳过：不记 strike、不改状态（验活归 janitor）。
    assert pool_row.stale_count == 0
    assert pool_row.status == "ready"
    db.refresh(user_row)
    # 用户行照旧巡检：记一次 strike。
    assert user_row.stale_count == 1
    assert stats["checked"] == 1


# ------------------------------------------------------------ janitor：容量淘汰


def _evict_setup(db, *, cap=1000):
    member = _member(db, _user(db, "ev").id, capacity_cap_bytes=cap)
    return member


def test_evict_over_cap_order_and_hysteresis(db):
    _set_pool(db, pool_enabled=True)
    # 本例只测容量分支的顺序与滞后带：关掉闲置主动清理隔离，
    # 否则 40 天闲置的 ready 会被主动分支另清一份（新行为，
    # 见 idle_proactive 一节）。
    _set_janitor(db, pool_idle_evict_proactive_enabled=False)
    member = _evict_setup(db)
    item = _item(db)
    orphan = _variant(db, item.id, member.id, status="orphan", size=400)
    stale = _variant(db, item.id, member.id, status="stale", size=400)
    ready_idle = _variant(db, item.id, member.id, size=400,
                          last_played_at=datetime.now()
                          - timedelta(days=40))
    ready_hot = _variant(db, item.id, member.id, size=400,
                         last_played_at=datetime.now())
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    # 1600 > 1000 触发；删 orphan + stale 后用量 800 ≤ 950
    # （95% 滞后带）即停，两份 ready 都不动。
    assert stats["evict"]["evicted"] == 2
    assert len(fake.delete_calls) == 2
    for row in (orphan, stale):
        db.refresh(row)
        assert row.status == "released"
    for row in (ready_idle, ready_hot):
        db.refresh(row)
        assert row.status == "ready"
    assert _decisions(db, "pool_evict")


def test_evict_skips_retention_and_playing(db):
    _set_pool(db, pool_enabled=True)
    member = _evict_setup(db, cap=1)
    item = _item(db)
    kept_retention = _variant(
        db, item.id, member.id, status="orphan", size=500,
        retention_until=datetime.now() + timedelta(days=1))
    playing = _variant(db, item.id, member.id, size=500,
                       last_played_at=datetime.now()
                       - timedelta(days=40))
    db.add(em.PoolPlayEvent(
        member_id=member.id, variant_id=playing.id, item_id=item.id,
        viewer_user_id=999, created_at=datetime.now()))
    db.commit()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        quota_fn=_quota_ok)
    assert stats["evict"]["evicted"] == 0
    db.refresh(kept_retention)
    assert kept_retention.status == "orphan"
    db.refresh(playing)
    assert playing.status == "ready"


def test_evict_delete_failure_marks_orphan(db):
    _set_pool(db, pool_enabled=True)
    member = _evict_setup(db, cap=1)
    item = _item(db)
    stale = _variant(db, item.id, member.id, status="stale", size=500)
    fake = _FakeClient(delete_error=RuntimeError("boom"))
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    assert stats["evict"]["evicted"] == 0
    assert stats["evict"]["delete_failed"] == 1
    db.refresh(stale)
    assert stale.status == "orphan"


def test_evict_idle_days_zero_keeps_ready(db):
    _set_pool(db, pool_enabled=True, pool_copy_idle_evict_days=0)
    member = _evict_setup(db, cap=1)
    item = _item(db)
    ready = _variant(db, item.id, member.id, size=500,
                     last_played_at=datetime.now()
                     - timedelta(days=90))
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        quota_fn=_quota_ok)
    # 闲置淘汰关（0 天）时 ready 行不入候选，超上限也不淘。
    assert stats["evict"]["evicted"] == 0
    db.refresh(ready)
    assert ready.status == "ready"


# ------------------------------------------------ janitor：闲置主动清理


def _set_janitor(db, **values):
    config_service.save_config(db, values, schema="pool_janitor")
    db.commit()


def _proactive_setup(db, *, idle_days=7):
    """主动清理场景：池开、成员无个人容量上限（盘不紧张，容量
    分支永不触发），副本闲置与否只看 last_played_at/created_at。"""
    _set_pool(db, pool_enabled=True,
              pool_copy_idle_evict_days=idle_days)
    return _member(db, _user(db, "pi").id)


def test_idle_proactive_default_on_and_payload(db):
    # 缺行回代码默认开（用户 2026-11-09 亲点加主动清理）。
    assert pool_gate.janitor_config_payload(db) == {
        "pool_idle_evict_proactive_enabled": True}
    # 存量行覆盖默认。
    _set_janitor(db, pool_idle_evict_proactive_enabled=False)
    assert pool_gate.janitor_config_payload(db) == {
        "pool_idle_evict_proactive_enabled": False}


def test_idle_proactive_evicts_without_capacity_pressure(db):
    member = _proactive_setup(db)
    item = _item(db)
    idle = _variant(db, item.id, member.id, size=500,
                    last_played_at=datetime.now()
                    - timedelta(days=10))
    never_played_old = _variant(db, item.id, member.id, size=500,
                                created_days=10)
    fresh = _variant(db, item.id, member.id, size=500,
                     last_played_at=datetime.now())
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    # 盘不紧张：容量分支一份不淘，主动分支清掉两份闲置。
    assert stats["evict"]["evicted"] == 0
    assert stats["idle_proactive"]["evicted"] == 2
    assert stats["idle_proactive"]["members"] == 1
    assert len(fake.delete_calls) == 2
    db.refresh(idle)
    assert idle.status == "released"
    db.refresh(never_played_old)
    assert never_played_old.status == "released"
    db.refresh(fresh)
    assert fresh.status == "ready"
    # 决策留痕与容量淘汰同 scope，reason 带 proactive 标记。
    rounds = _decisions(db, "pool_evict")
    assert rounds and "proactive_idle" in (rounds[-1].reason or "")


def test_idle_proactive_switch_off_keeps_copies(db):
    _set_janitor(db, pool_idle_evict_proactive_enabled=False)
    member = _proactive_setup(db)
    item = _item(db)
    idle = _variant(db, item.id, member.id, size=500,
                    last_played_at=datetime.now()
                    - timedelta(days=30))
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    assert "idle_proactive" not in stats
    assert fake.delete_calls == []
    db.refresh(idle)
    assert idle.status == "ready"


def test_idle_proactive_skips_playing_retention_and_nonready(db):
    member = _proactive_setup(db)
    item = _item(db)
    old = datetime.now() - timedelta(days=30)
    playing = _variant(db, item.id, member.id, size=500,
                       last_played_at=old)
    db.add(em.PoolPlayEvent(
        member_id=member.id, variant_id=playing.id, item_id=item.id,
        viewer_user_id=999, created_at=datetime.now()))
    kept = _variant(db, item.id, member.id, size=500,
                    last_played_at=old,
                    retention_until=datetime.now()
                    + timedelta(days=1))
    stale = _variant(db, item.id, member.id, status="stale",
                     size=500, last_played_at=old)
    db.commit()
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    # 正在播/保留期内恒跳过；非 ready 行不归主动分支。
    assert stats["idle_proactive"]["evicted"] == 0
    assert fake.delete_calls == []
    for row, want in ((playing, "ready"), (kept, "ready"),
                      (stale, "stale")):
        db.refresh(row)
        assert row.status == want


def test_idle_proactive_caps_per_member_per_round(db):
    member = _proactive_setup(db)
    item = _item(db)
    rows = [
        _variant(db, item.id, member.id, size=100,
                 last_played_at=datetime.now()
                 - timedelta(days=30))
        for _ in range(25)]
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    # 每成员每轮上限 20 份，不许一轮清爆。
    assert stats["idle_proactive"]["evicted"] == 20
    assert len(fake.delete_calls) == 20
    for row in rows:
        db.refresh(row)
    assert sum(1 for r in rows if r.status == "released") == 20
    assert sum(1 for r in rows if r.status == "ready") == 5


def test_idle_proactive_delete_failure_marks_orphan(db):
    member = _proactive_setup(db)
    item = _item(db)
    idle = _variant(db, item.id, member.id, size=500,
                    last_played_at=datetime.now()
                    - timedelta(days=30))
    fake = _FakeClient(delete_error=RuntimeError("boom"))
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    assert stats["idle_proactive"]["evicted"] == 0
    assert stats["idle_proactive"]["delete_failed"] == 1
    db.refresh(idle)
    assert idle.status == "orphan"


def test_idle_proactive_days_zero_disables(db):
    member = _proactive_setup(db, idle_days=0)
    item = _item(db)
    idle = _variant(db, item.id, member.id, size=500,
                    last_played_at=datetime.now()
                    - timedelta(days=90))
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake), quota_fn=_quota_ok)
    # 闲置天数 0 = 闲置淘汰关：主动分支同口径不淘。
    assert stats["idle_proactive"]["evicted"] == 0
    assert fake.delete_calls == []
    db.refresh(idle)
    assert idle.status == "ready"


# ------------------------------------------------------------ janitor：退出残留收尾


def test_leftover_cleanup_deletes_and_marks_once(db):
    user = _user(db, "lc1")
    member = _member(db, user.id, status="exited")
    item = _item(db)
    row = _variant(db, item.id, member.id, status="released")
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake))
    assert stats["leftover_cleanup"]["cleaned"] == 1
    assert len(fake.delete_calls) == 1
    db.refresh(row)
    assert row.retention_reason == pool_janitor.CLEANED_MARKER
    # 第二轮不再重复核对/删除。
    stats2 = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake))
    assert stats2["leftover_cleanup"]["cleaned"] == 0
    assert len(fake.get_calls) == 1
    assert len(fake.delete_calls) == 1


def test_leftover_cleanup_binding_dead_closed(db):
    user = _user(db, "lc2")
    member = _member(db, user.id, status="ejected")
    binding = pool_gate._member_binding(db, member)
    binding.status = "invalid"
    db.commit()
    item = _item(db)
    row = _variant(db, item.id, member.id, status="released")
    fake = _FakeClient()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(fake))
    assert stats["leftover_cleanup"]["closed"] == 1
    assert fake.delete_calls == []
    db.refresh(row)
    assert row.retention_reason == pool_janitor.CLOSED_MARKER


def test_leftover_cleanup_skips_active_member_rows(db):
    member = _member(db, _user(db, "lc3").id)
    item = _item(db)
    row = _variant(db, item.id, member.id, status="released")
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()))
    # 在池成员的 released 行不归残留收尾（淘汰已当场删文件）。
    assert stats["leftover_cleanup"]["cleaned"] == 0
    assert stats["leftover_cleanup"]["closed"] == 0
    db.refresh(row)
    assert row.retention_reason is None


# ------------------------------------------------------------ janitor：热条目补位


def _refill_setup(db):
    _set_pool(db, pool_enabled=True)
    ma = _member(db, _user(db, "rf-a").id)
    mb = _member(db, _user(db, "rf-b").id)
    item = _item(db)
    return ma, mb, item


def _play(db, member_id, variant_id, item_id, *, days_ago=0):
    db.add(em.PoolPlayEvent(
        member_id=member_id, variant_id=variant_id, item_id=item_id,
        viewer_user_id=4242,
        created_at=datetime.now() - timedelta(days=days_ago)))
    db.commit()


def test_refill_hot_item_after_copy_loss(db):
    ma, mb, item = _refill_setup(db)
    kept = _variant(db, item.id, ma.id)
    _variant(db, item.id, mb.id, status="stale")
    _play(db, ma.id, kept.id, item.id)
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        quota_fn=_quota_ok)
    assert stats["refill"]["created"] == 1
    job = (db.query(em.TransferJob)
           .filter(em.TransferJob.item_id == item.id,
                   em.TransferJob.owner_scope == "pool",
                   em.TransferJob.trigger == "refill")
           .first())
    assert job is not None
    # 补位不打已持可下发副本的 A 盘。
    assert job.owner_ref == str(mb.id)


def test_refill_cold_item_skipped(db):
    ma, mb, item = _refill_setup(db)
    kept = _variant(db, item.id, ma.id)
    _variant(db, item.id, mb.id, status="stale")
    _play(db, ma.id, kept.id, item.id, days_ago=10)
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        quota_fn=_quota_ok)
    assert stats["refill"]["created"] == 0


def test_refill_never_lost_skipped(db):
    ma, mb, item = _refill_setup(db)
    kept = _variant(db, item.id, ma.id)
    _play(db, ma.id, kept.id, item.id)
    # 只有 1 份副本且从没丢过：走观众点播进池，不归补位。
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        quota_fn=_quota_ok)
    assert stats["refill"]["created"] == 0


def test_refill_at_target_skipped(db):
    ma, mb, item = _refill_setup(db)
    kept = _variant(db, item.id, ma.id)
    _variant(db, item.id, mb.id)
    _variant(db, item.id, mb.id, status="stale")
    _play(db, ma.id, kept.id, item.id)
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        quota_fn=_quota_ok)
    assert stats["refill"]["created"] == 0


def test_refill_busy_queue_skipped(db):
    ma, mb, item = _refill_setup(db)
    kept = _variant(db, item.id, ma.id)
    _variant(db, item.id, mb.id, status="stale")
    _play(db, ma.id, kept.id, item.id)
    db.add(em.TransferJob(
        item_id=item.id, trigger="playback", provider="drive",
        owner_scope="user", owner_ref="7", status="pending"))
    db.commit()
    stats = pool_janitor.run_pool_janitor_once(
        db, client_factory=_factory(_FakeClient()),
        quota_fn=_quota_ok)
    assert stats["refill"]["skipped_busy"] == 1
    assert stats["refill"]["created"] == 0

# ------------------------------------------------------------ GA-035：上游线路取流口

from fastapi.testclient import TestClient  # noqa: E402

from backend.security import hash_password  # noqa: E402
from backend.database import SessionLocal  # noqa: E402
from backend.emby_server import upstream_sources as us  # noqa: E402

PW = "pool-guard-pw"
FREE = "poolguard_free"
CONTRIB = "poolguard_contrib"
LIB_NAME = "池守卫上游库"
SOURCE_NAME = "池守卫上游源"
MOUNT_ID = 9873


def _h(token):
    return {"X-Emby-Token": token}


def _token(client, uname):
    q = chr(34)
    header = (f"MediaBrowser Client={q}PG{q}, Device={q}iOS{q}, "
              f"DeviceId={q}pg-dev-1{q}, Version={q}1.0{q}")
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers={"X-Emby-Authorization": header})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


@pytest.fixture(scope="module")
def app_seed():
    from backend.database import init_db
    init_db()  # 与 lifespan 同口径建表（单独跑本文件时无人触发）
    db = SessionLocal()
    users = {}
    for uname in (FREE, CONTRIB):
        u = base_models.WebUser(
            username=uname, password_hash=hash_password(PW),
            emby_password=hash_password(PW), is_staff=True)
        db.add(u)
        db.flush()
        users[uname] = u.id
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=users[CONTRIB],
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=users[CONTRIB], provider="drive", account_label="贡献盘",
        credential_id=cred.id, status="active",
        granted_scopes=FULL_SCOPES)
    db.add(binding)
    db.flush()
    member = em.PoolMember(
        binding_id=binding.id, user_id=users[CONTRIB], provider="drive",
        status="active")
    db.add(member)
    db.flush()
    db.add(em.StorageMount(id=MOUNT_ID, name="pgm", mount_type="drive",
                           path="drive:drive1"))
    lib = em.Library(guid=uuid.uuid4().hex, name=LIB_NAME,
                     collection_type="movies", is_enabled=True)
    db.add(lib)
    db.flush()
    movie = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type="movie",
        name="池守卫上游片", drive_file_id=ORIG_FID,
        file_path=f"mount://{MOUNT_ID}/电影/x.mkv",
        container="mkv", size=2_000_000)
    db.add(movie)
    db.flush()
    src = us.create_source(db, name=SOURCE_NAME, source_type="emby",
                           base_url="https://up.example.com",
                           auth_method="api_key")
    us.enable_source(db, src)
    db.add(em.UpstreamItemMapping(
        local_item_id=movie.id, source_id=src.id,
        upstream_item_id="up-pg-1", confidence=0.95, evidence="tmdb"))
    db.add(em.TransferVariant(
        item_id=movie.id, source_file_id=ORIG_FID,
        variant_file_id="poolcopy-pg", variant_drive_id="pooldrive:x",
        owner_scope="pool", owner_ref=str(member.id), status="ready",
        size_bytes=2_000_000))
    config_service.save_config(db, {"pool_enabled": True}, schema="pool")
    db.commit()
    data = {"movie": movie.guid, "member_id": member.id}
    db.close()
    yield data
    db = SessionLocal()
    try:
        db.query(em.PoolPlayEvent).delete()
        db.query(em.TransferVariant).filter(
            em.TransferVariant.owner_scope == "pool").delete(
            synchronize_session=False)
        db.query(em.PoolUsageDaily).delete()
        db.query(em.PoolMemberStatusEvent).delete()
        db.query(em.PoolMember).delete()
        db.query(em.UpstreamItemMapping).delete()
        db.query(em.MediaItem).filter(
            em.MediaItem.library_id.in_(
                db.query(em.Library.id).filter(
                    em.Library.name == LIB_NAME))).delete(
            synchronize_session=False)
        db.query(em.Library).filter(
            em.Library.name == LIB_NAME).delete()
        db.query(em.StorageMount).filter(
            em.StorageMount.id == MOUNT_ID).delete()
        for uname in (FREE, CONTRIB):
            uid = (db.query(base_models.WebUser.id)
                   .filter(base_models.WebUser.username == uname)
                   .scalar())
            if uid is not None:
                db.query(em.UserStorageBinding).filter(
                    em.UserStorageBinding.user_id == uid).delete()
                db.query(em.MountCredential).filter(
                    em.MountCredential.owner_user_id == uid).delete()
                db.query(base_models.WebUser).filter(
                    base_models.WebUser.id == uid).delete()
        db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name == SOURCE_NAME).delete()
        config_service.save_config(db, {"pool_enabled": None},
                                   schema="pool")
        db.commit()
        store.invalidate()
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def upstream_resolve(monkeypatch):
    from backend.emby_server import upstream_playback as up
    monkeypatch.setattr(
        up, "resolve_mapped_upstream_stream",
        lambda db, item_id, mapping_id=None:
        "https://up.example.com/play.m3u8")


def test_upstream_stream_pool_viewer_passes_gate(
        client, app_seed, upstream_resolve):
    token = _token(client, FREE)
    r = client.get(
        f"/emby/Videos/{app_seed['movie']}/upstream/stream",
        headers=_h(token), follow_redirects=False)
    # GA-035：详情 DTO 已按含池口径签了线路，取流口必须同口径
    # 放行——池观众（未绑盘、条目有就绪池副本）拿到 302。
    assert r.status_code == 302, r.text
    assert r.headers.get("location") == "https://up.example.com/play.m3u8"


def test_upstream_stream_pool_off_still_403(
        client, app_seed, upstream_resolve):
    db = SessionLocal()
    try:
        config_service.save_config(db, {"pool_enabled": False},
                                   schema="pool")
        db.commit()
        store.invalidate()
        token = _token(client, FREE)
        r = client.get(
            f"/emby/Videos/{app_seed['movie']}/upstream/stream",
            headers=_h(token), follow_redirects=False)
        assert r.status_code == 403, r.text
    finally:
        config_service.save_config(db, {"pool_enabled": True},
                                   schema="pool")
        db.commit()
        store.invalidate()
        db.close()

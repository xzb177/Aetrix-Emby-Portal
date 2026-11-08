"""私聊通报优化批（2026-11-08 用户批准方案）：

1. 到货类（求片到货 / 转存终态 / 管理员手动标记入库）统一走 proactive
   管道，两个开关默认开；手动标记入库不再单独直发 TG。
2. 承诺落点补齐：举报终审结果、贡献池审核结果、抽奖补发到账。
3. 公益侧：公益资格到期提醒（同订阅档位/去重）＋活力归零/恢复通知。
4. 到货消息带「去看看」按钮（site_url 拼 /want；失败保留 whyplay 并存）。
5. 管理员收口：新片入库私聊默认关（退役并入群通报）；新求片提醒补开关
   且网页提交同口径。
6. 用户级 TG 通知偏好三类：求片到货 / 到期提醒 / 活动中奖，只管 TG 份。

不连生产库；发送一律 monkeypatch 假件捕获。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ["TG_PROACTIVE_FLUSHER"] = "0"

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models, notifications, notify_prefs, reminders
from backend.api import admin as admin_api
from backend.api import panel as panel_api
from backend.api import pool_admin
from backend.api import welfare_admin
from backend import governance_report as gov
from backend import lottery, vitality
from backend.emby_server import models as em
from backend.emby_server import new_media_notify as nmn
from backend.tg_bot import features, proactive


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
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def sent(monkeypatch):
    calls = []

    def _fake(db, chat_id, text, reply_markup=None, parse_mode="HTML"):
        calls.append({
            "chat_id": chat_id, "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
        })
        return True, None

    monkeypatch.setattr("backend.tg_bot.sender.send_message", _fake)
    return calls


@pytest.fixture()
def event_calls(monkeypatch):
    """捕获 notify_admin_event_quiet（post-commit 通知守卫）的调用。"""
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(notifications, "notify_admin_event_quiet", _fake)
    return calls


def _cfg(db, key, value):
    db.add(models.SystemConfig(key=key, value=value, description=""))
    db.commit()


def _user(db, name, telegram_id=None, **kw):
    user = models.WebUser(
        username=name, password_hash="x", is_active=True,
        telegram_id=telegram_id, **kw,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _library_item(db, tmdb_id, name):
    library = em.Library(guid=f"lib-{tmdb_id}", name="库")
    db.add(library)
    db.commit()
    item = em.MediaItem(
        guid=f"item-{tmdb_id}", library_id=library.id,
        item_type="movie", name=name, tmdb_id=tmdb_id,
        file_path=f"/media/{tmdb_id}.mkv",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _request(db, user, tmdb_id, title, status="pending"):
    row = models.MovieRequest(
        user_id=user.id, movie_name=title, tmdb_id=tmdb_id,
        status=status, vote_count=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.add(models.MovieRequestVote(request_id=row.id, user_id=user.id))
    db.commit()
    return row


def _notices(db):
    return db.query(models.TgProactiveNotice).order_by(
        models.TgProactiveNotice.id).all()


# ---------- 1. 到货类默认开 ----------

def test_proactive_switches_default_on(db):
    assert proactive.store_arrival_enabled(db) is True
    assert proactive.transfer_done_enabled(db) is True
    assert welfare_admin.WELFARE_CONFIG_KEYS[proactive.KEY_STORE] == "true"
    assert welfare_admin.WELFARE_CONFIG_KEYS[proactive.KEY_TRANSFER] == "true"


def test_arrival_default_on_end_to_end(db, sent):
    owner = _user(db, "owner", telegram_id=1001)
    _library_item(db, "d1", "默认开的片")
    row = _request(db, owner, "d1", "默认开的片")
    # 没有任何开关配置行：到货照样翻转 + 入队 + 发出
    assert proactive.sync_arrived_requests(db) == 1
    assert proactive.flush_due(
        db, now=datetime.now() + timedelta(seconds=120)) == 1
    assert len(sent) == 1
    assert "你要的《默认开的片》到了" in sent[0]["text"]


def test_explicit_off_still_silent(db, sent):
    _cfg(db, proactive.KEY_STORE, "false")
    _cfg(db, proactive.KEY_TRANSFER, "false")
    owner = _user(db, "owner", telegram_id=1001)
    _library_item(db, "d2", "关着的片")
    row = _request(db, owner, "d2", "关着的片")
    assert proactive.sync_arrived_requests(db) == 0
    db.refresh(row)
    assert row.status == "pending"
    assert _notices(db) == []
    assert sent == []


def test_note_store_request_completed_explicit_voters(db):
    owner = _user(db, "owner", telegram_id=1001)
    voter = _user(db, "voter", telegram_id=1002)
    other = _user(db, "other", telegram_id=1003)
    row = _request(db, owner, "d3", "手动标记片", status="completed")
    db.add(models.MovieRequestVote(request_id=row.id, user_id=voter.id))
    db.add(models.MovieRequestVote(request_id=row.id, user_id=other.id))
    db.commit()
    # 显式名单为空：只有求片人（附议人名单由调用方门控后传入）
    assert proactive.note_store_request_completed(db, row, voter_ids=[]) == 1
    db.commit()
    assert {n.user_id for n in _notices(db)} == {owner.id}
    # 再显式带上 voter：只多 voter，other 不进
    row2 = _request(db, owner, "d4", "手动标记片2", status="completed")
    db.add(models.MovieRequestVote(request_id=row2.id, user_id=voter.id))
    db.add(models.MovieRequestVote(request_id=row2.id, user_id=other.id))
    db.commit()
    assert proactive.note_store_request_completed(
        db, row2, voter_ids=[voter.id]) == 2
    db.commit()
    assert {n.user_id for n in _notices(db)
            if n.subject_id == row2.id} == {owner.id, voter.id}


def test_mark_in_library_enqueues_via_proactive(db, monkeypatch):
    """#5 手动标记：TG 走 proactive 管道（求片人＋门控附议人），
    站内信那份的 channels 不再带 telegram（防双发）。"""
    owner = _user(db, "owner", telegram_id=1001)
    voter = _user(db, "voter", telegram_id=1002)
    admin = _user(db, "admin", telegram_id=None)
    row = _request(db, owner, "d5", "标记入库片")
    db.add(models.MovieRequestVote(request_id=row.id, user_id=voter.id))
    db.commit()

    info = admin_api._mark_media_seek_sync(db, admin.id, row.id, force=True)
    assert info["user_id"] == owner.id
    notices = {(n.kind, n.user_id) for n in _notices(db)}
    assert notices == {
        (proactive.KIND_ARRIVAL, owner.id),
        (proactive.KIND_ARRIVAL, voter.id),
    }

    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(admin_api, "notify_admin_event_quiet", _fake)
    payload = admin_api.MediaSeekMarkRequest(force=True)
    row2 = _request(db, owner, "d6", "标记入库片2")
    db.commit()
    asyncio.run(admin_api.mark_media_seek_in_library(
        row2.id, payload, current_admin=admin, db=db))
    assert calls, "标记入库应发站内信"
    for call in calls:
        assert call["channels"] == ["in_app"]


# ---------- 4. 去看看按钮 ----------

def test_watch_button_on_arrival(db, sent):
    _cfg(db, "site_url", "https://example.test/")
    owner = _user(db, "owner", telegram_id=1001)
    _library_item(db, "d7", "带按钮的片")
    row = _request(db, owner, "d7", "带按钮的片")
    assert proactive.sync_arrived_requests(db) == 1
    assert proactive.flush_due(
        db, now=datetime.now() + timedelta(seconds=120)) == 1
    markup = sent[0]["reply_markup"]
    assert markup == {"inline_keyboard": [[
        {"text": "去看看", "url": "https://example.test/want"},
    ]]}


def test_watch_and_whyplay_buttons_coexist(db, sent):
    _cfg(db, "site_url", "https://example.test")
    owner = _user(db, "owner", telegram_id=1001)
    item = _library_item(db, "d8", "混合片")
    row = _request(db, owner, "d8", "混合片")
    assert proactive.sync_arrived_requests(db) == 1
    job = em.TransferJob(
        item_id=item.id, trigger="store_want", provider="drive",
        owner_scope="user", owner_ref=str(owner.id), status="failed",
        error_kind="no_binding", attempts=1,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    assert proactive.note_transfer_terminal(db, job) is True
    db.commit()
    assert proactive.flush_due(
        db, now=datetime.now() + timedelta(seconds=120)) == 1
    assert len(sent) == 1
    rows = sent[0]["reply_markup"]["inline_keyboard"]
    flat = [btn for r in rows for btn in r]
    assert any(b.get("callback_data") == "menu:whyplay" for b in flat)
    assert any(b.get("url") == "https://example.test/want" for b in flat)


# ---------- 6. 用户偏好 ----------

def test_notify_prefs_defaults_and_tg_allowed(db):
    user = _user(db, "u", telegram_id=3001)
    for cat in (notify_prefs.CAT_SEEK, notify_prefs.CAT_EXPIRY,
                notify_prefs.CAT_LOTTERY):
        assert notify_prefs.tg_allowed(db, user.id, cat) is True
    # 查不到的用户按默认收（不因偏好查询失败丢通知）
    assert notify_prefs.tg_allowed(db, 999999, notify_prefs.CAT_SEEK) is True


def test_panel_notify_prefs_roundtrip(db):
    user = _user(db, "u", telegram_id=3001)
    got = panel_api.get_notify_prefs(db=db, user=user)
    assert got["data"] == {
        "seek": True, "expiry": True, "lottery": True,
    }
    out = panel_api.set_notify_prefs(
        panel_api.NotifyPrefsIn(seek=False), db=db, user=user)
    assert out["data"]["seek"] is False
    assert out["data"]["expiry"] is True
    assert notify_prefs.tg_allowed(db, user.id, notify_prefs.CAT_SEEK) is False
    assert notify_prefs.tg_allowed(db, user.id, notify_prefs.CAT_EXPIRY) is True


def test_flush_mutes_when_seek_pref_off(db, sent):
    owner = _user(db, "owner", telegram_id=1001)
    owner.tg_notify_seek = False
    db.commit()
    _library_item(db, "d9", "被静音的片")
    row = _request(db, owner, "d9", "被静音的片")
    assert proactive.sync_arrived_requests(db) == 1
    assert proactive.flush_due(
        db, now=datetime.now() + timedelta(seconds=120)) == 0
    assert sent == []
    assert {n.status for n in _notices(db)} == {"muted"}
    # 再 flush 不会翻出来重发
    assert proactive.flush_due(
        db, now=datetime.now() + timedelta(seconds=240)) == 0


def test_review_result_respects_seek_pref(db, monkeypatch):
    """#4 审批结果：偏好关时 TG 渠道被摘掉，站内信保留。"""
    owner = _user(db, "owner", telegram_id=1001)
    owner.tg_notify_seek = False
    db.commit()
    admin = _user(db, "admin")
    row = _request(db, owner, "d10", "审批片")
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(admin_api, "notify_admin_event_quiet", _fake)
    payload = admin_api.MediaSeekUpdateRequest(status="approved")
    asyncio.run(admin_api.update_media_seek(
        row.id, payload, current_admin=admin, db=db))
    assert calls and calls[0]["channels"] == ["in_app"]

    calls.clear()
    owner.tg_notify_seek = True
    db.commit()
    row2 = _request(db, owner, "d11", "审批片2")
    asyncio.run(admin_api.update_media_seek(
        row2.id, payload, current_admin=admin, db=db))
    assert calls and calls[0]["channels"] == ["in_app", "telegram"]


# ---------- 3. 公益侧提醒 ----------

@pytest.fixture()
def reminder_calls(monkeypatch):
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {"in_app": True, "telegram": True}

    monkeypatch.setattr(notifications, "notify_admin_event", _fake)
    return calls


def _welfare_user(db, name, expires_at, tg=4001, **kw):
    return _user(db, name, telegram_id=tg, is_welfare=True,
                 welfare_expires_at=expires_at, **kw)


def test_welfare_reminder_due_and_dedup(db, reminder_calls):
    now = datetime.now()
    _welfare_user(db, "w1", now + timedelta(days=2, hours=1))
    result = asyncio.run(reminders.run_expiry_reminders(db, now=now))
    assert result["welfare_reminded"] == 1
    welfare = [c for c in reminder_calls
               if str(c["event_type"]).startswith("welfare.")]
    assert len(welfare) == 1
    assert welfare[0]["channels"] == ["in_app", "telegram"]
    assert db.query(models.WelfareReminder).count() == 1
    # 第二轮不重发（去重表挡住）
    result = asyncio.run(reminders.run_expiry_reminders(db, now=now))
    assert result["welfare_reminded"] == 0
    assert len([c for c in reminder_calls
                if str(c["event_type"]).startswith("welfare.")]) == 1


def test_welfare_expired_notice_once(db, reminder_calls):
    now = datetime.now()
    _welfare_user(db, "w2", now - timedelta(hours=2))
    result = asyncio.run(reminders.run_expiry_reminders(db, now=now))
    assert result["welfare_expired_notified"] == 1
    result = asyncio.run(reminders.run_expiry_reminders(db, now=now))
    assert result["welfare_expired_notified"] == 0


def test_welfare_permanent_and_pref_off_channels(db, reminder_calls):
    now = datetime.now()
    # 永久资格（expires_at 为空）不提醒
    _welfare_user(db, "w3", None, tg=4003)
    user = _welfare_user(db, "w4", now + timedelta(days=2, hours=1), tg=4004)
    user.tg_notify_expiry = False
    db.commit()
    result = asyncio.run(reminders.run_expiry_reminders(db, now=now))
    assert result["welfare_reminded"] == 1
    welfare = [c for c in reminder_calls
               if str(c["event_type"]).startswith("welfare.")]
    assert welfare[0]["channels"] == ["in_app"]


def test_subscription_reminder_pref_drops_telegram(db, reminder_calls):
    now = datetime.now()
    user = _user(db, "sub", telegram_id=4002)
    user.tg_notify_expiry = False
    db.add(models.SubscriptionPlan(id=1, name="月卡", price=30,
                                   duration_days=30))
    db.add(models.UserSubscription(
        user_id=user.id, plan_id=1, status="active",
        start_date=now - timedelta(days=28),
        end_date=now + timedelta(days=2, hours=1),
    ))
    db.commit()
    result = asyncio.run(reminders.run_expiry_reminders(db, now=now))
    assert result["reminded"] == 1
    subs = [c for c in reminder_calls
            if str(c["event_type"]).startswith("subscription.")]
    assert subs and subs[0]["channels"] == ["in_app"]


def test_vitality_zero_and_recovery_notify(db, sent):
    user = _user(db, "v1", telegram_id=5001, is_welfare=True, vitality=1)
    result = vitality.daily_deduct(db)
    assert result["suspended"] == 1
    assert len(sent) == 1
    assert "活力" in sent[0]["text"]
    # 签到/观影恢复路径把活力从 0 拉起来 → 解封通知
    gained = vitality.award_watch_reward(db, user.id)
    assert gained >= 1
    assert len(sent) == 2
    assert "恢复" in sent[1]["text"]


def test_vitality_notify_respects_expiry_pref(db, sent):
    user = _user(db, "v2", telegram_id=5002, is_welfare=True, vitality=1)
    user.tg_notify_expiry = False
    db.commit()
    vitality.daily_deduct(db)
    assert sent == []


# ---------- 2. 承诺落点：抽奖补发 / 池审核 / 举报 ----------

def _lottery_round_with_winner(db, user, distributed=False):
    rnd = models.LotteryRound(
        title="测试抽奖", chat_id=-100999, status="done",
        drawn_at=datetime.now(),
    )
    db.add(rnd)
    db.commit()
    db.refresh(rnd)
    prize = models.LotteryRoundPrize(
        round_id=rnd.id, name="月卡", type="days", value=30, quantity=1,
    )
    entry = models.LotteryRoundEntry(
        round_id=rnd.id, user_id=user.id, telegram_id=user.telegram_id,
    )
    db.add_all([prize, entry])
    db.commit()
    db.refresh(prize)
    db.refresh(entry)
    winner = models.LotteryRoundWinner(
        round_id=rnd.id, entry_id=entry.id, prize_id=prize.id,
        distributed=distributed,
    )
    db.add(winner)
    db.commit()
    db.refresh(winner)
    return rnd, winner


def test_lottery_redistribute_sends_arrival_dm(db, sent, monkeypatch):
    user = _user(db, "win", telegram_id=6001)
    rnd, winner = _lottery_round_with_winner(db, user, distributed=False)

    def _fake_distribute(db_, round_id):
        db_.query(models.LotteryRoundWinner).filter(
            models.LotteryRoundWinner.round_id == round_id,
        ).update({"distributed": True}, synchronize_session=False)
        db_.commit()
        return {"distributed": 1, "errors": []}

    monkeypatch.setattr(lottery, "distribute_round", _fake_distribute)
    retried = lottery.retry_pending_distributions(db)
    assert retried == [rnd.id]
    dms = [c for c in sent if c["chat_id"] == 6001]
    assert len(dms) == 1
    assert "已发放到账" in dms[0]["text"]


def test_lottery_redistribute_respects_pref(db, sent, monkeypatch):
    user = _user(db, "win2", telegram_id=6002)
    user.tg_notify_lottery = False
    db.commit()
    rnd, winner = _lottery_round_with_winner(db, user, distributed=False)

    def _fake_distribute(db_, round_id):
        db_.query(models.LotteryRoundWinner).filter(
            models.LotteryRoundWinner.round_id == round_id,
        ).update({"distributed": True}, synchronize_session=False)
        db_.commit()
        return {"distributed": 1, "errors": []}

    monkeypatch.setattr(lottery, "distribute_round", _fake_distribute)
    lottery.retry_pending_distributions(db)
    assert [c for c in sent if c["chat_id"] == 6002] == []


def test_lottery_draw_dm_respects_pref(db, sent):
    user = _user(db, "win3", telegram_id=6003)
    user.tg_notify_lottery = False
    db.commit()
    rnd, winner = _lottery_round_with_winner(db, user, distributed=True)
    result = lottery.notify_draw_results(db, rnd.id, [winner])
    assert result["group"] is True
    assert result["dm_sent"] == 0
    assert [c for c in sent if c["chat_id"] == 6003] == []


def test_pool_review_notifies_applicant(db, event_calls):
    from backend.emby_server import pool_gate
    user = _user(db, "pooler", telegram_id=7001)
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user.id,
        sealed_credential="", key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user.id, provider="drive", account_label="贡献盘",
        credential_id=cred.id, status="active",
        granted_scopes=(
            "https://www.googleapis.com/auth/drive.file "
            "https://www.googleapis.com/auth/drive.readonly"),
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    member = pool_gate.apply_pool_membership(db, user.id, binding.id)
    admin = _user(db, "admin")
    req = SimpleNamespace(client=None)
    pool_admin.pool_approve_member(member.id, req, current_admin=admin, db=db)
    pool_calls = [c for c in event_calls
                  if str(c.get("event_type", "")).startswith("pool.")]
    assert len(pool_calls) == 1
    assert pool_calls[0]["user_id"] == user.id
    assert pool_calls[0]["channels"] == ["in_app", "telegram"]
    assert "通过" in pool_calls[0]["title"]


def test_governance_decide_notifies_reporter(db, event_calls):
    reporter = _user(db, "rep", telegram_id=8001)
    admin = _user(db, "admin")
    seek = _request(db, reporter, "g1", "被举报的片")
    report = models.GovernanceReport(
        reporter_id=reporter.id, target_type="media_seek",
        target_id=seek.id, reason="spam", status="escalated",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    out = gov.admin_decide(db, admin, report.id, "accepted", note="属实")
    assert out["status"] == "accepted"
    gov_calls = [c for c in event_calls
                 if str(c.get("event_type", "")).startswith("governance.")]
    assert len(gov_calls) == 1
    assert gov_calls[0]["user_id"] == reporter.id
    assert gov_calls[0]["channels"] == ["in_app", "telegram"]


# ---------- 5. 管理员收口 ----------

def test_new_media_notify_default_off(db):
    enabled, channels = nmn._get_config(db)
    assert enabled is False
    assert channels == ["telegram"]


def test_new_request_staff_notify_gate(db, sent):
    staff = _user(db, "staff", telegram_id=9001, is_staff=True)
    asker = _user(db, "asker", telegram_id=9002)
    # 默认开：发
    features._notify_staff_about_request(db, 42, asker, "新片名")
    assert len(sent) == 1
    assert sent[0]["chat_id"] == 9001
    # 关掉后不发
    _cfg(db, features.KEY_NEW_REQUEST, "false")
    features._notify_staff_about_request(db, 43, asker, "新片名2")
    assert len(sent) == 1
    # 配置键已登记进注册表与默认表
    from backend import config_schema
    assert config_schema.WELFARE_CONFIG_SCHEMA[
        features.KEY_NEW_REQUEST] == ("bool",)
    assert welfare_admin.WELFARE_CONFIG_KEYS[
        features.KEY_NEW_REQUEST] == "true"

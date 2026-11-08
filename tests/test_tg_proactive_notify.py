"""Telegram 主动私聊：求片到货、转存终态。

不连生产库。开关默认开（2026-11-08 私聊通报优化批；此前默认关）：
显式关着时不发、不写已推记录。开着时求片人与附议人各收一次，
重复触发不重发，没绑 Telegram 的跳过。到货消息可带「去看看」按钮；
失败说人话并给已有的 /whyplay 入口。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ["TG_PROACTIVE_FLUSHER"] = "0"

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api import welfare_admin
from backend.api.store_requests import _sync_row
from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv
from backend.tg_bot import proactive


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
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
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "parse_mode": parse_mode,
        })
        return True, None

    monkeypatch.setattr("backend.tg_bot.sender.send_message", _fake)
    return calls


def _cfg(db, key, value):
    db.add(models.SystemConfig(key=key, value=value, description=""))
    db.commit()


def _enable_store(db):
    _cfg(db, proactive.KEY_STORE, "true")


def _enable_transfer(db):
    _cfg(db, proactive.KEY_TRANSFER, "true")


def _user(db, name, telegram_id=None):
    user = models.WebUser(
        username=name, password_hash="x", is_active=True,
        telegram_id=telegram_id,
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
        guid=f"item-{tmdb_id}",
        library_id=library.id,
        item_type="movie",
        name=name,
        tmdb_id=tmdb_id,
        file_path=f"/media/{tmdb_id}.mkv",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _request(db, user, tmdb_id, title, status="pending"):
    row = models.MovieRequest(
        user_id=user.id, movie_name=title, tmdb_id=tmdb_id, status=status,
        vote_count=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    db.add(models.MovieRequestVote(request_id=row.id, user_id=user.id))
    db.commit()
    return row


def _vote(db, request_row, user):
    db.add(models.MovieRequestVote(request_id=request_row.id, user_id=user.id))
    db.commit()


def _job(db, user, item, trigger="store_want", status="pending",
         owner_scope="user", kind=None, detail=None):
    row = em.TransferJob(
        item_id=item.id if item is not None else 1,
        trigger=trigger,
        provider="drive",
        owner_scope=owner_scope,
        owner_ref=str(user.id) if owner_scope == "user" else None,
        status=status,
        error_kind=kind,
        error_detail=detail,
        attempts=1,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _notices(db):
    return db.query(models.TgProactiveNotice).order_by(
        models.TgProactiveNotice.id).all()


def test_config_keys_default_on():
    # 2026-11-08 起两个到货开关默认开（缺行=开，默认值表同步 true）
    assert welfare_admin.WELFARE_CONFIG_KEYS[proactive.KEY_STORE] == "true"
    assert welfare_admin.WELFARE_CONFIG_KEYS[proactive.KEY_TRANSFER] == "true"
    assert welfare_admin.WELFARE_CONFIG_KEYS[proactive.KEY_WINDOW] == "60"
    from backend import config_schema
    assert config_schema.WELFARE_CONFIG_SCHEMA[proactive.KEY_STORE] == ("bool",)
    assert config_schema.WELFARE_CONFIG_SCHEMA[proactive.KEY_TRANSFER] == ("bool",)
    assert config_schema.WELFARE_CONFIG_SCHEMA[proactive.KEY_WINDOW][0] == "int"


def test_switch_off_does_not_send_or_record(db, sent):
    # 默认已改开：本用例验「显式关」语义，先把两个开关写成 false
    _cfg(db, proactive.KEY_STORE, "false")
    _cfg(db, proactive.KEY_TRANSFER, "false")
    owner = _user(db, "owner", telegram_id=1001)
    item = _library_item(db, "t1", "到货片")
    row = _request(db, owner, "t1", "到货片")
    assert proactive.sync_arrived_requests(db) == 0
    db.refresh(row)
    assert row.status == "pending"
    assert _sync_row(db, row) is True
    db.commit()
    db.refresh(row)
    assert row.status == "completed"
    assert _notices(db) == []
    assert proactive.flush_due(db, now=datetime.now() + timedelta(seconds=120)) == 0
    assert sent == []

    job = _job(db, owner, item, status="failed", kind="no_binding",
               detail="/var/secret token=abc")
    assert proactive.note_transfer_terminal(db, job) is False
    db.commit()
    assert _notices(db) == []
    assert sent == []


def test_arrival_notifies_requester_and_voter_once(db, sent):
    _enable_store(db)
    owner = _user(db, "owner", telegram_id=1001)
    voter = _user(db, "voter", telegram_id=1002)
    unbound = _user(db, "plain", telegram_id=None)
    _library_item(db, "t9", "想看的片")
    row = _request(db, owner, "t9", "想看的片")
    _vote(db, row, voter)
    _vote(db, row, unbound)

    assert proactive.sync_arrived_requests(db) == 1
    db.refresh(row)
    assert row.status == "completed"
    assert proactive.sync_arrived_requests(db) == 0
    assert _sync_row(db, row) is False
    assert proactive.note_store_request_completed(db, row) == 0
    db.commit()

    notices = _notices(db)
    assert {(n.kind, n.user_id, n.status) for n in notices} == {
        (proactive.KIND_ARRIVAL, owner.id, "pending"),
        (proactive.KIND_ARRIVAL, voter.id, "pending"),
    }
    assert proactive.flush_due(db, now=datetime.now() + timedelta(seconds=120)) == 2
    assert proactive.flush_due(db, now=datetime.now() + timedelta(seconds=120)) == 0
    assert len(sent) == 2
    by_chat = {call["chat_id"]: call for call in sent}
    assert set(by_chat) == {1001, 1002}
    for call in sent:
        assert "你要的《想看的片》到了" in call["text"]
        assert call["reply_markup"] is None
        assert "立即播放" not in call["text"]
        assert "http" not in call["text"].lower()
    assert {n.status for n in _notices(db)} == {"sent"}


def test_transfer_success_is_plain_text(db, sent):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=2001)
    item = _library_item(db, "t2", "已转好")
    job = _job(db, user, item, status="done")
    assert proactive.note_transfer_terminal(db, job) is True
    assert proactive.note_transfer_terminal(db, job) is False
    db.commit()
    assert proactive.flush_due(db, now=datetime.now() + timedelta(seconds=120)) == 1
    assert len(sent) == 1
    assert sent[0]["text"] == "《已转好》可以看了"
    assert sent[0]["reply_markup"] is None
    assert sent[0]["chat_id"] == 2001


def test_transfer_failure_human_reason_and_whyplay(db, sent):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=2002)
    item = _library_item(db, "t3", "没转成的片")
    leaked = "/var/lib/secret/cookie token=should-not-leak"
    job = _job(db, user, item, status="failed", kind="no_binding", detail=leaked)
    assert tv._fail_job(db, job, "no_binding", leaked)["outcome"] == "failed"
    db.refresh(job)
    assert job.status == "failed"
    assert proactive.flush_due(db, now=datetime.now() + timedelta(seconds=120)) == 1
    text = sent[0]["text"]
    assert "《没转成的片》没转成" in text
    assert "网盘绑定失效了" in text
    assert "/whyplay" in text
    assert "should-not-leak" not in text
    assert "/var/lib" not in text
    assert "no_binding" not in text
    markup = sent[0]["reply_markup"]
    assert markup == proactive.WHYPLAY_MARKUP
    assert "立即播放" not in str(markup)
    assert "url" not in str(markup)


def test_unknown_failure_is_generic(db, sent):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=2003)
    item = _library_item(db, "t4", "未知片")
    detail = "Traceback /opt/app/secret.key boom"
    job = _job(db, user, item, status="failed", kind="weird_internal", detail=detail)
    proactive.note_transfer_terminal(db, job)
    db.commit()
    proactive.flush_due(db, now=datetime.now() + timedelta(seconds=120))
    text = sent[0]["text"]
    assert "转存没有完成，可以稍后再试" in text
    assert "weird_internal" not in text
    assert "Traceback" not in text
    assert "secret.key" not in text
    assert sent[0]["reply_markup"] == proactive.WHYPLAY_MARKUP


def test_intermediate_retry_and_system_jobs_skip(db, sent):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=2004)
    item = _library_item(db, "t5", "系统片")
    pending = _job(db, user, item, status="pending")
    pending.attempts = 1
    assert tv._schedule_retry(db, pending, RuntimeError("暂时网络不好")) == "retried"
    db.refresh(pending)
    assert pending.status == "pending"
    assert _notices(db) == []

    follow = _job(db, user, item, trigger="series_follow", status="failed",
                  kind="no_binding")
    auto = _job(db, user, item, trigger="auto", status="done")
    source = _job(db, user, item, trigger="manual", status="done",
                  owner_scope="source")
    assert proactive.note_transfer_terminal(db, follow) is False
    assert proactive.note_transfer_terminal(db, auto) is False
    assert proactive.note_transfer_terminal(db, source) is False
    db.commit()
    assert _notices(db) == []
    assert sent == []


def test_terminal_retry_exhaustion_notifies_once(db, sent):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=2005)
    item = _library_item(db, "t6", "重试尽了")
    job = _job(db, user, item, status="running")
    job.attempts = tv.MAX_ATTEMPTS
    assert tv._schedule_retry(db, job, RuntimeError("网络还是不行")) == "failed"
    db.refresh(job)
    assert job.status == "failed"
    assert len(_notices(db)) == 1
    tv._schedule_retry(db, job, RuntimeError("网络还是不行"))
    assert len(_notices(db)) == 1
    proactive.flush_due(db, now=datetime.now() + timedelta(seconds=120))
    assert len(sent) == 1
    assert "转存没有完成，可以稍后再试" in sent[0]["text"]


def test_merge_window_and_failure_button_only(db, sent):
    _enable_store(db)
    _enable_transfer(db)
    _cfg(db, proactive.KEY_WINDOW, "60")
    user = _user(db, "u", telegram_id=3001)
    arrived = _library_item(db, "m1", "甲片")
    ready = _library_item(db, "m2", "乙片")
    failed_item = _library_item(db, "m3", "丙片")
    request_row = _request(db, user, "m1", "甲片")
    assert _sync_row(db, request_row) is True
    ok_job = _job(db, user, ready, status="done")
    bad_job = _job(
        db, user, failed_item, status="failed", kind="insufficient_space",
        detail="/mnt/hidden",
    )
    proactive.note_transfer_terminal(db, ok_job)
    proactive.note_transfer_terminal(db, bad_job)
    db.commit()
    now = datetime.now()
    assert proactive.flush_due(db, now=now) == 0
    assert sent == []
    assert proactive.flush_due(db, now=now + timedelta(seconds=61)) == 1
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "你要的《甲片》到了" in text
    assert "《乙片》可以看了" in text
    assert "《丙片》没转成" in text
    assert "网盘空间不够" in text
    assert "/mnt/hidden" not in text
    assert "还有" not in text
    assert sent[0]["reply_markup"] == proactive.WHYPLAY_MARKUP
    buttons = sent[0]["reply_markup"]["inline_keyboard"]
    assert buttons == [[{"text": "为什么播不了", "callback_data": "menu:whyplay"}]]


def test_merge_truncates_extra_titles(db, sent):
    _enable_store(db)
    user = _user(db, "u", telegram_id=3002)
    now = datetime.now() - timedelta(seconds=120)
    for index in range(proactive.MAX_LINES + 3):
        db.add(models.TgProactiveNotice(
            kind=proactive.KIND_ARRIVAL,
            subject_id=index + 1,
            user_id=user.id,
            title=f"片{index}",
            detail="",
            status="pending",
            attempts=0,
            created_at=now,
            updated_at=now,
        ))
    db.commit()
    proactive.flush_due(db, now=datetime.now())
    text = sent[0]["text"]
    assert "还有 3 部" in text
    assert sent[0]["reply_markup"] is None
    assert "片0" in text
    assert f"片{proactive.MAX_LINES}" not in text


def test_send_failure_does_not_break_job_and_retries_then_stops(db, monkeypatch):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=4001)
    item = _library_item(db, "t7", "发送失败片")
    calls = {"n": 0}

    def _boom(db, chat_id, text, reply_markup=None, parse_mode="HTML"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("网络炸了")
        return False, "请求超时"

    monkeypatch.setattr("backend.tg_bot.sender.send_message", _boom)
    job = _job(db, user, item, status="running")
    result = tv._fail_job(db, job, "dead", "copy 403 /secret/path")
    assert result["outcome"] == "failed"
    db.refresh(job)
    assert job.status == "failed"
    now = datetime.now() + timedelta(seconds=120)
    assert proactive.flush_due(db, now=now) == 1
    row = _notices(db)[0]
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at is not None
    assert job.status == "failed"
    # 退避还没到，不再打
    assert proactive.flush_due(db, now=row.next_attempt_at - timedelta(seconds=1)) == 0
    assert calls["n"] == 1
    # 连续可重试失败，到次数后停
    for _ in range(5):
        due = row.next_attempt_at or datetime.now()
        proactive.flush_due(db, now=due + timedelta(seconds=1))
        db.refresh(row)
        if row.status == "gave_up":
            break
    assert row.status == "gave_up"
    assert calls["n"] == proactive.MAX_SEND_ATTEMPTS
    again = calls["n"]
    proactive.flush_due(db, now=datetime.now() + timedelta(days=1))
    assert calls["n"] == again


def test_blocked_user_is_not_retried(db, monkeypatch):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=4002)
    item = _library_item(db, "t8", "屏蔽片")
    calls = []

    def _blocked(db, chat_id, text, reply_markup=None, parse_mode="HTML"):
        calls.append(text)
        return False, "HTTP 状态码错误: 403"

    monkeypatch.setattr("backend.tg_bot.sender.send_message", _blocked)
    job = _job(db, user, item, status="failed", kind="auth_error",
               detail="token=leak")
    proactive.note_transfer_terminal(db, job)
    db.commit()
    now = datetime.now() + timedelta(seconds=120)
    proactive.flush_due(db, now=now)
    row = _notices(db)[0]
    assert row.status == "blocked"
    assert "授权失效" in calls[0]
    assert "token=leak" not in calls[0]
    proactive.flush_due(db, now=now + timedelta(days=1))
    assert len(calls) == 1


def test_fail_job_survives_notifier_crash(db, monkeypatch):
    _enable_transfer(db)
    user = _user(db, "u", telegram_id=4003)
    item = _library_item(db, "t10", "崩溃片")
    job = _job(db, user, item, status="running")

    def _explode(db, job):
        raise RuntimeError("入队坏了")

    monkeypatch.setattr(proactive, "note_transfer_terminal", _explode)
    result = tv._fail_job(db, job, "orphan", "permission")
    assert result["outcome"] == "failed"
    db.refresh(job)
    assert job.status == "failed"
    assert _notices(db) == []

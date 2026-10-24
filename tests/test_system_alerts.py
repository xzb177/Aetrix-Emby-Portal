# -*- coding: utf-8 -*-
"""系统告警测试。

覆盖：
1. 总开关默认关：关闭时 notify_alert / check_all 都是静默 no-op
2. 规则播种：ensure_default_rules 幂等，6 个事件都有规则
3. 冷却去重：冷却期内重复越线不重复打扰
4. check_all：指标越线触发告警；恢复后自动 resolved
5. enrich 夜间模式：白天零产出不误报停滞
6. WebhookChannel：payload 格式 + HMAC 签名
"""
import asyncio
import hashlib
import hmac
import json
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")


import pytest

from backend import models
from backend import system_alerts as sa
from backend.database import SessionLocal, init_db

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        try:
            s.rollback()
        except Exception:  # noqa: BLE001
            pass
        s.close()


# R16 P3-12：_staff 建的测试用户 id 记这里，_clean_alerts 按 id 精确清理
# （不删全表——测试 DB 可能被其他模块共享）
_CREATED_USER_IDS = []


def _wipe_alert_state(db):
    db.query(models.Alert).delete()
    db.query(models.AlertRule).filter(
        models.AlertRule.event_type.isnot(None)).delete()
    if _CREATED_USER_IDS:
        db.query(models.WebUser).filter(
            models.WebUser.id.in_(_CREATED_USER_IDS)
        ).delete(synchronize_session=False)
        _CREATED_USER_IDS.clear()
    db.commit()


@pytest.fixture()
def _clean_alerts(db):
    """每个用例前后清理告警相关表，避免互相污染。"""
    _wipe_alert_state(db)
    yield
    _wipe_alert_state(db)


def _staff(db, username=None):
    import uuid
    username = username or f"boss_{uuid.uuid4().hex[:8]}"
    u = models.WebUser(username=username, password_hash="x",
                       is_staff=True, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    _CREATED_USER_IDS.append(u.id)
    return u


def _enable(monkeypatch):
    monkeypatch.setenv(sa.MASTER_SWITCH_ENV, "1")


def _disable(monkeypatch):
    monkeypatch.delenv(sa.MASTER_SWITCH_ENV, raising=False)
    # DB 开关也确保关闭（用新会话，避免 fixture 会话缓存）
    s = SessionLocal()
    try:
        s.query(models.SystemConfig).filter(
            models.SystemConfig.key == sa.MASTER_SWITCH_KEY).delete()
        s.commit()
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 1. 总开关
# ---------------------------------------------------------------------------

def test_disabled_by_default(monkeypatch, db, _clean_alerts):
    _disable(monkeypatch)
    assert sa.enabled(db) is False


def test_notify_noop_when_disabled(monkeypatch, db, _clean_alerts):
    _disable(monkeypatch)
    _staff(db)
    ok = asyncio.run(sa.notify_alert(
        sa.EVT_DISK_FULL, "测试", "内容", db=db))
    assert ok is False
    assert db.query(models.Alert).count() == 0
    # R16 P3-12：只断言事件型规则（测试 DB 若残留 legacy 指标型规则不应误失败）
    assert db.query(models.AlertRule).filter(
        models.AlertRule.event_type.isnot(None)).count() == 0  # 关闭时不播种


def test_check_all_skipped_when_disabled(monkeypatch, db, _clean_alerts):
    _disable(monkeypatch)
    out = sa.check_all(db)
    assert out["skipped"] == ["disabled"]
    assert out["fired"] == []


# ---------------------------------------------------------------------------
# 2. 规则播种
# ---------------------------------------------------------------------------

def test_seed_default_rules(monkeypatch, db, _clean_alerts):
    _enable(monkeypatch)
    n = sa.ensure_default_rules(db)
    assert n == len(sa.ALL_EVENTS)
    # 幂等：再跑一次不重复
    assert sa.ensure_default_rules(db) == 0
    rules = db.query(models.AlertRule).filter(
        models.AlertRule.event_type.isnot(None)).all()
    assert {r.event_type for r in rules} == set(sa.ALL_EVENTS)
    for r in rules:
        assert r.cooldown_sec and r.cooldown_sec >= 60
        # R16 P3-14：notification_channels 直接写 list，兼容老的 json 字符串
        nc = r.notification_channels
        nc = json.loads(nc) if isinstance(nc, str) else nc
        assert nc  # 渠道非空
        assert json.loads(r.recipients) == {"staff": True}


# ---------------------------------------------------------------------------
# 3. 冷却去重
# ---------------------------------------------------------------------------

async def _fake_notify_staff(db, title, content, message_type="system",
                             related_id=None):
    return 1


def test_cooldown_dedup(monkeypatch, db, _clean_alerts):
    _enable(monkeypatch)
    _staff(db)
    import backend.notifications as _ntf
    monkeypatch.setattr(_ntf, "notify_staff_users", _fake_notify_staff)

    ok1 = asyncio.run(sa.notify_alert(
        sa.EVT_DISK_FULL, "磁盘告警", "只剩 5%", db=db))
    assert ok1 is True
    assert db.query(models.Alert).filter(
        models.Alert.status == "open").count() == 1

    # 冷却期内第二次 → 去重跳过
    ok2 = asyncio.run(sa.notify_alert(
        sa.EVT_DISK_FULL, "磁盘告警", "只剩 4%", db=db))
    assert ok2 is False
    assert db.query(models.Alert).filter(
        models.Alert.status == "open").count() == 1


def test_cooldown_expiry_allows_refire(monkeypatch, db, _clean_alerts):
    _enable(monkeypatch)
    _staff(db)
    import backend.notifications as _ntf
    monkeypatch.setattr(_ntf, "notify_staff_users", _fake_notify_staff)
    # 冷却设为 60 秒（最小值），手工把上一条告警时间改老
    from datetime import datetime, timedelta
    asyncio.run(sa.notify_alert(
        sa.EVT_DISK_FULL, "磁盘告警", "只剩 5%",
        cooldown_sec=60, db=db))
    row = db.query(models.Alert).first()
    row.created_at = datetime.now() - timedelta(seconds=3600)
    db.commit()

    ok = asyncio.run(sa.notify_alert(
        sa.EVT_DISK_FULL, "磁盘告警", "只剩 4%",
        cooldown_sec=60, db=db))
    assert ok is True
    assert db.query(models.Alert).count() == 2


# ---------------------------------------------------------------------------
# 4. check_all：触发与自动恢复
# ---------------------------------------------------------------------------

def _subs(**overrides):
    base = {
        "probe": {"status": "ok", "metrics": {"pending_ready": 0,
                                              "open_breakers": []}},
        "enrich": {"status": "ok", "metrics": {"backlog": 0,
                                               "idle_sec": 0}},
        "disk": {"status": "ok", "metrics": {"disk_free_percent": 50.0}},
        "sa_quota": {"status": "ok", "metrics": {"sa_count": 100}},
        "scan": {"status": "ok", "metrics": {"failed": 0}},
        "api": {"status": "ok", "metrics": {"global_error_ratio": 0.0}},
    }
    for k, v in overrides.items():
        base[k] = v
    return base


def test_check_all_fires_and_resolves(monkeypatch, db, _clean_alerts):
    _enable(monkeypatch)
    _staff(db)
    import backend.notifications as _ntf
    monkeypatch.setattr(_ntf, "notify_staff_users", _fake_notify_staff)

    bad = _subs(disk={"status": "warn",
                      "metrics": {"disk_free_percent": 5.0}})
    import backend.emby_server.observability as _obs
    monkeypatch.setattr(_obs, "probe_subsystems", lambda db: bad)

    out = sa.check_all(db)
    assert sa.EVT_DISK_FULL in out["fired"]
    assert db.query(models.Alert).filter(
        models.Alert.status == "open").count() == 1

    # 指标恢复 → 自动 resolved
    monkeypatch.setattr(_obs, "probe_subsystems", lambda db: _subs())
    out2 = sa.check_all(db)
    assert any(sa.EVT_DISK_FULL in r for r in out2["resolved"])
    assert db.query(models.Alert).filter(
        models.Alert.status == "open").count() == 0


def test_check_all_scan_failed_names(monkeypatch, db, _clean_alerts):
    _enable(monkeypatch)
    _staff(db)
    import backend.notifications as _ntf
    monkeypatch.setattr(_ntf, "notify_staff_users", _fake_notify_staff)

    bad = _subs(scan={"status": "warn",
                      "metrics": {"failed": 2,
                                  "last_failed": [{"name": "国产剧"},
                                                  {"name": "综艺"}]}})
    import backend.emby_server.observability as _obs
    monkeypatch.setattr(_obs, "probe_subsystems", lambda db: bad)
    out = sa.check_all(db)
    assert sa.EVT_SCAN_FAILED in out["fired"]
    row = db.query(models.Alert).filter(
        models.Alert.status == "open").first()
    assert "国产剧" in row.title


# ---------------------------------------------------------------------------
# 5. enrich 夜间模式不误报
# ---------------------------------------------------------------------------

def test_enrich_night_mode_no_false_positive(monkeypatch, db, _clean_alerts):
    _enable(monkeypatch)
    _staff(db)
    import backend.notifications as _ntf
    monkeypatch.setattr(_ntf, "notify_staff_users", _fake_notify_staff)

    # 白天：夜间模式开、不在窗口内、积压很大、idle 很长 → 也不该告警
    monkeypatch.setattr(sa, "_enrich_night_paused", lambda db: True)
    bad = _subs(enrich={"status": "warn",
                        "metrics": {"backlog": 50000, "idle_sec": 99999,
                                    "done_per_min": 0}})
    import backend.emby_server.observability as _obs
    monkeypatch.setattr(_obs, "probe_subsystems", lambda db: bad)
    out = sa.check_all(db)
    assert sa.EVT_ENRICH_STALLED not in out["fired"]


def test_enrich_stalled_fires_when_truly_stalled(monkeypatch, db, _clean_alerts):
    _enable(monkeypatch)
    _staff(db)
    import backend.notifications as _ntf
    monkeypatch.setattr(_ntf, "notify_staff_users", _fake_notify_staff)

    monkeypatch.setattr(sa, "_enrich_night_paused", lambda db: False)
    bad = _subs(enrich={"status": "warn",
                        "metrics": {"backlog": 8000, "idle_sec": 99999,
                                    "done_per_min": 0}})
    import backend.emby_server.observability as _obs
    monkeypatch.setattr(_obs, "probe_subsystems", lambda db: bad)
    out = sa.check_all(db)
    assert sa.EVT_ENRICH_STALLED in out["fired"]


# ---------------------------------------------------------------------------
# 6. WebhookChannel
# ---------------------------------------------------------------------------

class _FakeResp:
    status_code = 200
    text = "ok"


class _FakeClient:
    def __init__(self, *a, **kw):
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        self.posts.append({"url": url, "content": content,
                           "headers": headers})
        return _FakeResp()


def test_webhook_payload_and_hmac(monkeypatch):
    from backend.notifications import WebhookChannel
    import httpx
    fake = _FakeClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: fake)

    ch = WebhookChannel(url="https://example.com/hook", secret="s3cret")
    assert ch.enabled is True
    ok = asyncio.run(ch.send(0, "标题", "内容",
                             data={"event_type": "disk_full"}))
    assert ok is True
    assert len(fake.posts) == 1
    post = fake.posts[0]
    assert post["url"] == "https://example.com/hook"
    body = post["content"]
    payload = json.loads(body.decode("utf-8"))
    assert payload["title"] == "标题"
    assert payload["content"] == "内容"
    assert payload["data"]["event_type"] == "disk_full"
    assert payload["source"] == "aetrix"
    # HMAC 验签
    expect = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert post["headers"]["X-Signature"] == expect


def test_webhook_disabled_without_url():
    from backend.notifications import WebhookChannel
    ch = WebhookChannel(url="")
    assert ch.enabled is False
    ok = asyncio.run(ch.send(0, "t", "c"))
    assert ok is False


def test_webhook_non_2xx_is_failure(monkeypatch):
    from backend.notifications import WebhookChannel
    import httpx

    class _BadResp:
        status_code = 500
        text = "err"

    class _BadClient(_FakeClient):
        async def post(self, url, content=None, headers=None):
            return _BadResp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _BadClient())
    ch = WebhookChannel(url="https://example.com/hook")
    ok = asyncio.run(ch.send(0, "t", "c"))
    assert ok is False

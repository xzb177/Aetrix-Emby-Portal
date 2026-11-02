"""TG bot 扩展功能：账号中心、求片、快捷登录、排行榜。"""
from datetime import datetime, timedelta
import sys
sys.path.insert(0, ".")
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend import models
from backend.tg_bot import features

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close(); engine.dispose()

def user(db, tg=900001, name="alice", points=42):
    row = models.WebUser(username=name, password_hash="x", telegram_id=tg, points=points)
    db.add(row); db.commit(); return row

def test_account_unbound_is_private_safe(db):
    text = features.handle_account(db, {"id": 1}, 1, "")
    assert "绑定" in text and "账号中心" in text

def test_account_shows_subscription_summary(db):
    # 精简卡口径（2026-11-02 合并）：订阅只出汇总行（套餐名＋到期日），
    # 设备逐条已下线（明细去网站账号页）。
    row = user(db)
    plan = models.SubscriptionPlan(name="月卡", price=30, duration_days=30, is_active=True)
    db.add(plan); db.flush()
    db.add(models.UserSubscription(user_id=row.id, plan_id=plan.id, status="active", end_date=datetime.now()+timedelta(days=12)))
    db.add(models.UserDevice(user_id=row.id, device_id="d1", client="SenPlayer", last_seen_at=datetime.now()))
    db.commit()
    text = features.handle_account(db, {"id": row.telegram_id}, row.telegram_id, "")
    assert "alice" in text and "月卡" in text and "生效中" in text
    assert "SenPlayer" not in text and "最近设备" not in text

def test_request_unbound_does_not_write(db):
    text = features.handle_request(db, {"id": 100}, 100, "电影")
    assert "绑定" in text and db.query(models.MovieRequest).count() == 0

def test_request_uses_media_seek_rules_and_dedupes(db, monkeypatch):
    row = user(db)
    monkeypatch.setattr("backend.media_seek.seek_enabled", lambda db: True)
    monkeypatch.setattr("backend.media_seek.daily_limit", lambda db: 5)
    monkeypatch.setattr("backend.media_seek.monthly_quota", lambda db, u: {"monthly_remaining": 10, "monthly_limit": 10, "monthly_used": 0, "kind": "paid"})
    first = features.handle_request(db, {"id": row.telegram_id}, row.telegram_id, "流浪地球 | 2019 | 国语")
    second = features.handle_request(db, {"id": row.telegram_id}, row.telegram_id, "流浪地球 | 2019 | 国语")
    assert "已提交" in first and "处理中" in second and db.query(models.MovieRequest).count() == 1
    saved = db.query(models.MovieRequest).one()
    assert saved.movie_name == "流浪地球" and saved.note == "国语"

def test_login_returns_one_time_url_only_for_bound_user(db, monkeypatch):
    row = user(db)
    monkeypatch.setattr("backend.tg_bot.login_token.build_login_url", lambda db, u: "https://example.test/tg-login?token=once")
    result = features.handle_login(db, {"id": row.telegram_id}, row.telegram_id, "")
    assert isinstance(result, tuple)
    text, markup = result
    assert "10 分钟" in text and markup["inline_keyboard"][0][0]["url"].endswith("token=once")

def test_rank_uses_bound_users_and_marks_self(db):
    mine = user(db, tg=900001, name="mine", points=10)
    user(db, tg=900002, name="top", points=100)
    db.add(models.WebUser(username="unbound", password_hash="x", points=999)); db.commit()
    text = features.handle_rank(db, {"id": mine.telegram_id}, mine.telegram_id, "")
    assert "top" in text and "mine" in text and "unbound" not in text and "◀️" in text

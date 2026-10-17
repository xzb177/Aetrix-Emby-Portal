"""r30 L1–L5：抽奖时区口径 / 发放结果诚实化 / 创建校验 / 调度器 Event 复用 / 打码去重。"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import sys

sys.path.insert(0, ".")

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from zoneinfo import ZoneInfo

from backend import models
from backend import lottery
from backend.api import welfare_admin
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.LotteryRound.__table__.create(engine, checkfirst=True)
    models.LotteryRoundPrize.__table__.create(engine, checkfirst=True)
    models.LotteryRoundEntry.__table__.create(engine, checkfirst=True)
    models.LotteryRoundWinner.__table__.create(engine, checkfirst=True)
    models.LotteryBlacklist.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
    models.WelfareGrantLog.__table__.create(engine, checkfirst=True)
    models.AdminLog.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    staff = models.WebUser(username="boss", password_hash="x", is_staff=True)
    db.add(staff)
    db.commit()

    app = FastAPI()
    app.include_router(welfare_admin.admin_router)
    app.dependency_overrides[welfare_admin.get_current_admin] = lambda: staff

    def _get_db():
        yield db

    app.dependency_overrides[welfare_admin.get_db] = _get_db
    store.invalidate()
    with TestClient(app) as c:
        yield c
    store.invalidate()


def _set_group_ids(db, value):
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == "lottery_group_ids").first()
    if row is None:
        db.add(models.SystemConfig(key="lottery_group_ids", value=value))
    else:
        row.value = value
    db.commit()


# ---------- L1 ----------

def test_to_shanghai_naive_converts_utc_aware():
    """前端 toISOString 发 UTC aware：04:00Z → 入库上海 naive 12:00。"""
    dt = datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)
    out = lottery.to_shanghai_naive(dt)
    assert out.tzinfo is None
    assert out == datetime(2026, 10, 17, 12, 0)


def test_to_shanghai_naive_treats_naive_as_utc():
    """naive 输入按 UTC 理解（与前端发 UTC 同口径）。"""
    out = lottery.to_shanghai_naive(datetime(2026, 10, 17, 4, 0))
    assert out == datetime(2026, 10, 17, 12, 0)


def test_shanghai_now_naive_matches_zoneinfo():
    """shanghai_now_naive() 与 ZoneInfo 口径一致（容差 5 秒）。"""
    got = lottery.shanghai_now_naive()
    want = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    assert abs((got - want).total_seconds()) < 5


# ---------- L2 ----------

def test_notify_draw_results_honest_on_errors(db, monkeypatch):
    """发奖有失败时群公告如实写「部分奖品发放失败（N 笔）」，不宣称已发放。"""
    import backend.tg_bot.sender as sender_mod

    user = models.WebUser(username="w1", password_hash="x", telegram_id=555001)
    db.add(user)
    db.commit()
    rnd = lottery.create_round(
        db, title="诚实测试", chat_id=-100321,
        prizes=[{"name": "积分奖", "type": "points", "value": 10, "quantity": 1}],
    )
    texts = []
    monkeypatch.setattr(
        sender_mod, "send_message",
        lambda db_, chat_id, text, reply_markup=None: texts.append(text) or (True, None))
    lottery.notify_draw_results(
        db, rnd.id, [],
        distribute_errors=[{"winner_id": 1, "error": "用户不存在"},
                           {"winner_id": 2, "error": "奖品缺失"}])
    assert len(texts) == 1  # 无 winners → 只有群公告，无私聊
    assert "部分奖品发放失败（2 笔），系统将自动补发" in texts[0]
    assert "奖励已自动发放" not in texts[0]


def test_notify_draw_results_all_ok_keeps_old_text(db, monkeypatch):
    """无失败时群公告保持原样「奖励已自动发放」。"""
    import backend.tg_bot.sender as sender_mod

    rnd = lottery.create_round(
        db, title="全发测试", chat_id=-100322,
        prizes=[{"name": "积分奖", "type": "points", "value": 10, "quantity": 1}],
    )
    texts = []
    monkeypatch.setattr(
        sender_mod, "send_message",
        lambda db_, chat_id, text, reply_markup=None: texts.append(text) or (True, None))
    lottery.notify_draw_results(db, rnd.id, [], distribute_errors=[])
    assert len(texts) == 1
    assert "奖励已自动发放" in texts[0]


# ---------- L3 ----------

def test_create_rejects_zero_value_points_days(db, client):
    """points/days 的 value=0 创建时直接 400（开奖时必失败，不能建出来）。"""
    _set_group_ids(db, "-100777")
    for ptype in ("points", "days"):
        r = client.post("/api/admin/welfare/lottery/rounds", json={
            "title": f"零值{ptype}", "chat_id": -100777,
            "prizes": [{"name": "零奖", "type": ptype, "value": 0, "quantity": 1}],
        })
        assert r.status_code == 400, (ptype, r.text)
        assert "必须为正数" in r.json()["detail"]
    # whitelist 的 0 是合法语义（永久），保持可建
    r = client.post("/api/admin/welfare/lottery/rounds", json={
        "title": "白名单零值", "chat_id": -100777,
        "prizes": [{"name": "永久资格", "type": "whitelist", "value": 0, "quantity": 1}],
    })
    assert r.status_code in (200, 201), r.text


# ---------- L4 ----------

def test_lottery_loop_reuses_single_event():
    """_loop 内只 new 一次 Event（AST 断言，db_backup 同款写法）。"""
    import ast

    with open("backend/lottery.py", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    sched = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)
                 and n.name == "start_lottery_auto_draw_scheduler")
    src = ast.get_source_segment(open("backend/lottery.py", encoding="utf-8").read(), sched)
    assert "threading.Event().wait(" not in src, "每轮 new Event().wait() 不应再出现"
    assert src.count("threading.Event()") == 1, "只应 new 一个 Event 复用"


# ---------- L5 ----------

def test_verify_round_uses_module_mask(db):
    """verify_round 的打码与模块级 _mask_telegram 同一实现。"""
    user = models.WebUser(username="m1", password_hash="x", telegram_id=13800138000)
    db.add(user)
    db.commit()
    rnd = lottery.create_round(
        db, title="打码测试", chat_id=-100323,
        prizes=[{"name": "积分奖", "type": "points", "value": 10, "quantity": 1}],
    )
    db.add(models.LotteryRoundEntry(round_id=rnd.id, user_id=user.id,
                                   telegram_id=user.telegram_id))
    db.commit()
    report = lottery.verify_round(db, rnd.id)
    masked = [e["telegram_id_masked"] for e in report["entries"]]
    assert masked == [lottery._mask_telegram(13800138000)]
    assert masked[0] == "****8000"

"""审计 A-114：lottery_enabled 是唯一 bypass 摘面板 env 接管的开关。

is_enabled() 直读 DB，不看 LOTTERY_ENABLED；同文件 _get_str_config
却先查 env；管理端创建门同样直读 DB。四路（TG 面板按钮×2、
TG 抽奖命令、提案审批门、管理端创建门）全 bypass env。
修法：is_enabled 走 _get_str_config（env 口径与文件内其余键统一），
管理端创建门改走 lottery.is_enabled。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import sys

sys.path.insert(0, ".")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import lottery, models
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
    models.AdminLog.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
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
    with TestClient(app) as c:
        yield c


def _set_cfg(db, key, value):
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row is None:
        row = models.SystemConfig(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def _del_cfg(db, key):
    db.query(models.SystemConfig).filter(models.SystemConfig.key == key).delete()
    db.commit()


# ---- is_enabled env 接管矩阵（TG 门与提案门共用此谓词） ----

def test_db_on_env_off_is_disabled(db, monkeypatch):
    _set_cfg(db, "lottery_enabled", "1")
    monkeypatch.setenv("LOTTERY_ENABLED", "0")
    assert lottery.is_enabled(db) is False


def test_db_missing_env_off_is_disabled(db, monkeypatch):
    _del_cfg(db, "lottery_enabled")
    monkeypatch.setenv("LOTTERY_ENABLED", "0")
    assert lottery.is_enabled(db) is False


def test_db_on_env_unset_is_enabled(db, monkeypatch):
    _set_cfg(db, "lottery_enabled", "1")
    monkeypatch.delenv("LOTTERY_ENABLED", raising=False)
    assert lottery.is_enabled(db) is True


def test_db_off_env_on_is_enabled(db, monkeypatch):
    _set_cfg(db, "lottery_enabled", "0")
    monkeypatch.setenv("LOTTERY_ENABLED", "1")
    assert lottery.is_enabled(db) is True


def test_db_off_env_unset_is_disabled(db, monkeypatch):
    _set_cfg(db, "lottery_enabled", "0")
    monkeypatch.delenv("LOTTERY_ENABLED", raising=False)
    assert lottery.is_enabled(db) is False


def test_db_missing_env_unset_defaults_on(db, monkeypatch):
    _del_cfg(db, "lottery_enabled")
    monkeypatch.delenv("LOTTERY_ENABLED", raising=False)
    assert lottery.is_enabled(db) is True


# ---- 管理端创建门与 TG 门同批覆盖 ----

def _create_payload():
    return {
        "title": "A-114 测试抽奖",
        "chat_id": 999,
        "prizes": [{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
        "lottery_type": "button",
    }


def test_create_gate_env_off_returns_403(db, client, monkeypatch):
    _set_cfg(db, "lottery_enabled", "1")
    monkeypatch.setenv("LOTTERY_ENABLED", "0")
    resp = client.post("/api/admin/welfare/lottery/rounds", json=_create_payload())
    assert resp.status_code == 403
    assert "未开启" in resp.text


def test_create_gate_env_unset_db_on_succeeds(db, client, monkeypatch):
    _set_cfg(db, "lottery_enabled", "1")
    monkeypatch.delenv("LOTTERY_ENABLED", raising=False)
    resp = client.post("/api/admin/welfare/lottery/rounds", json=_create_payload())
    assert resp.status_code == 200
    assert resp.json()["title"] == "A-114 测试抽奖"

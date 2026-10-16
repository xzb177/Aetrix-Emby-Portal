"""新建群抽奖不再重复填群ID：后端从 lottery_group_ids 自动解析（端到端）。

背景：管理后台【新建活动】表单里的群ID输入框已删除（与功能开关里的
「允许群ID」重复）。创建请求不再带 chat_id 时，后端按规则解析：
  - 允许名单恰好 1 个群 → 用它
  - 名单为空或有 ≥2 个群 → 400，提示去功能开关里设成一个群
  - 显式传入 chat_id → 走原有校验（白名单、活跃轮次唯一）

走真实 HTTP 接口 + 内存 SQLite，鉴权用假管理员。
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

from backend import models
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


def _set_group_ids(db, value):
    row = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key == "lottery_group_ids")
        .first()
    )
    if row is None:
        row = models.SystemConfig(key="lottery_group_ids", value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def _payload(**kw):
    body = {
        "title": "测试抽奖",
        "prizes": [{"name": "积分奖", "type": "points", "value": 100, "quantity": 1}],
    }
    body.update(kw)
    return body


def test_create_without_chat_id_single_allowed_group(db, client):
    """① 不传 chat_id + 名单恰好一个群 → 创建成功，轮次 chat_id 等于该群。"""
    _set_group_ids(db, "-1004394035708")
    r = client.post("/api/admin/welfare/lottery/rounds", json=_payload())
    assert r.status_code == 200, r.text
    assert r.json()["chat_id"] == -1004394035708
    rnd = db.query(models.LotteryRound).first()
    assert rnd is not None and rnd.chat_id == -1004394035708


def test_create_without_chat_id_empty_allowlist(db, client):
    """② 不传 chat_id + 名单为空 → 400。"""
    _set_group_ids(db, "")
    r = client.post("/api/admin/welfare/lottery/rounds", json=_payload())
    assert r.status_code == 400
    assert "允许群ID" in r.json()["detail"]


def test_create_without_chat_id_multiple_groups(db, client):
    """③ 不传 chat_id + 名单两个群 → 400。"""
    _set_group_ids(db, "-100111,-100222")
    r = client.post("/api/admin/welfare/lottery/rounds", json=_payload())
    assert r.status_code == 400
    assert "允许群ID" in r.json()["detail"]


def test_create_with_explicit_allowed_chat_id(db, client):
    """④ 显式传名单内的 chat_id → 成功（回归）。"""
    _set_group_ids(db, "-100111,-100222")
    r = client.post("/api/admin/welfare/lottery/rounds", json=_payload(chat_id=-100222))
    assert r.status_code == 200, r.text
    assert r.json()["chat_id"] == -100222


def test_create_with_explicit_disallowed_chat_id(db, client):
    """⑤ 显式传不在名单内的 chat_id → 仍被拒（回归）。"""
    _set_group_ids(db, "-100111")
    r = client.post("/api/admin/welfare/lottery/rounds", json=_payload(chat_id=-999))
    assert r.status_code == 403

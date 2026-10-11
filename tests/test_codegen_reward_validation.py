"""统一码生成接口 generate_codes 的「奖励类型 × 面额」交叉校验测试。

用内存 SQLite，不碰生产库。
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.api import admin_ops as ops


@pytest.fixture()
def db():
    """内存 SQLite 会话，预置默认服（realms.claim 需要）。"""
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    session.add(models.ServerRealm(name="默认服", slug="main", is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def admin(db):
    """建一个管理员，供 generate_codes 做权限校验。"""
    user = models.WebUser(
        username="admin_test", password_hash="x", is_staff=True, is_active=True
    )
    db.add(user)
    db.commit()
    return user


def _request(**overrides):
    """构造统一码生成请求，默认生成 1 张 code_type=1 的卡。"""
    payload = {"count": 1, "code_type": 1}
    payload.update(overrides)
    return ops.CodeGenerateRequest(**payload)


def _reward_tables():
    """所有带 reward_type 字段的表（统一码/兑换码可能分表存放）。"""
    return [t for t in models.Base.metadata.sorted_tables if "reward_type" in t.c]


def _count_codes(db, reward_type):
    """统计库里指定 reward_type 的卡码数量。"""
    tables = _reward_tables()
    assert tables, "模型里找不到含 reward_type 字段的卡码表"
    total = 0
    for table in tables:
        total += (
            db.query(func.count())
            .select_from(table)
            .filter(table.c.reward_type == reward_type)
            .scalar()
        )
    return total


@pytest.mark.parametrize(
    "overrides",
    [
        {"reward_type": "points", "points_value": 0},
        {"reward_type": "discount", "discount_pct": 0},
        {"reward_type": "discount", "discount_pct": 100},
    ],
)
def test_bad_reward_value_rejected(db, admin, overrides):
    """面额非法时必须在生成阶段就 400，不能放坏卡入库。"""
    with pytest.raises(HTTPException) as exc:
        ops.generate_codes(_request(**overrides), admin, db)
    assert exc.value.status_code == 400


def test_points_code_created(db, admin):
    before = _count_codes(db, "points")
    ops.generate_codes(_request(reward_type="points", points_value=100), admin, db)
    assert _count_codes(db, "points") == before + 1


def test_discount_code_created(db, admin):
    before = _count_codes(db, "discount")
    ops.generate_codes(_request(reward_type="discount", discount_pct=85), admin, db)
    assert _count_codes(db, "discount") == before + 1


def test_subscription_code_created(db, admin):
    """回归：默认订阅面额不应被新增校验误伤。"""
    before = _count_codes(db, "subscription")
    ops.generate_codes(_request(reward_type="subscription"), admin, db)
    assert _count_codes(db, "subscription") == before + 1

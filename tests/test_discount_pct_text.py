# -*- coding: utf-8 -*-
"""discount_pct_text 与折扣文案口径的回归测试。

discount_pct 在全站是「实付百分比」口径：85 = 八五折（实付 85%），
不是「折扣百分比」。以前各处 f"{pct}折"（→「85折」）/ f"{100-pct}折"（→「15折」）
都写错了，本文件锁定统一实现 codes.discount_pct_text 及全部 4 处后端文案。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import codes as code_lib
from backend import models
from backend.api import economy as E
from backend.codes import discount_pct_text


@pytest.fixture()
def db():
    """内存 SQLite，不碰生产库。"""
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, name="discount_text_user"):
    u = models.WebUser(username=name, password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    return u


def test_discount_pct_text_standard():
    assert discount_pct_text(85) == "8.5折"
    assert discount_pct_text(90) == "9折"
    assert discount_pct_text(88) == "8.8折"


def test_discount_pct_text_clamp():
    # 与 economy._exchange_discount_amount 的 max(1, min(99, ...)) 同口径
    assert discount_pct_text(100) == "9.9折"
    assert discount_pct_text(0) == "0.1折"
    assert discount_pct_text(None) == "0.1折"
    assert discount_pct_text("85") == "8.5折"


def test_registration_discount_code_preview_and_redeem(db):
    u = _user(db)
    code = models.RegistrationCode(
        code="REG-DISCOUNT-85",
        code_type=code_lib.CODE_TYPE_RENEW,
        days=30,
        reward_type="discount",
        discount_pct=85,
        max_uses=10,
        use_count=0,
        is_active=True,
        used_by="",
    )
    db.add(code)
    db.commit()

    preview = code_lib.preview_code(db, code.code)
    assert "8.5折" in preview["message"]
    assert "15 折" not in preview["message"]

    result = code_lib.redeem_code(db, u, code.code)
    assert result["success"] is True
    assert "8.5折" in result["message"]


def test_exchange_discount_code_preview_and_redeem(db):
    u = _user(db, "discount_text_user2")
    ex = models.ExchangeCode(
        code="EX-DISCOUNT-85",
        type="discount",
        discount_pct=85,
        max_uses=10,
        use_count=0,
        is_active=True,
        expires_at=datetime.now() + timedelta(days=30),
    )
    db.add(ex)
    db.commit()

    preview = code_lib.preview_code(db, ex.code)
    assert preview["discount_text"] == "8.5折"

    result = E._redeem_exchange_core(db, u, ex.code)
    assert "8.5折" in result["message"]

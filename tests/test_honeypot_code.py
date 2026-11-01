# -*- coding: utf-8 -*-
"""诱饵码（蜜罐）测试：HONEY- 前缀方案，零数据库表结构改动。

覆盖：
1. is_honeypot 前缀识别（大小写不敏感、对象/字符串两种输入）；
2. render_code(decoy=True) 生成 HONEY- 前缀；
3. 兑换诱饵码 → 用户被封禁（is_active=False）、对外只返回「卡码无效」、
   logger.warning + 安全日志（LoginLog reason=decoy_code）落盘；
4. 诱饵码判定先于可用性检查：停用/用尽的诱饵码依然触发陷阱；
5. 普通卡码不受影响（兑换成功、用户不被封禁）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

# 注意（#1668 教训）：本文件不许在导入时重绑全局 backend.database.engine。
# 旧写法曾在导入时建临时库并重绑全局引擎且不调 init_db；一旦它成为套件
# 最后一个重绑者，后续依赖全局 SessionLocal 的测试会在 setup 集体报缺表。
# 本文件自带 db 夹具（内存库），根本用不到全局引擎，故直接删掉导入时副作用。
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import logging  # noqa: E402

import pytest  # noqa: E402

from backend import codes, models  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, username="victim"):
    u = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(u)
    db.flush()
    return u


def _code(db, code_str, **kw):
    # 默认值可被调用方覆盖（例如「已停用/已用尽」的诱饵码要传 is_active=False），
    # 直接把默认值写进构造调用会和 **kw 里的同名参数撞成 TypeError。
    fields = {"max_uses": 1, "use_count": 0, "is_active": True,
              "code_type": codes.CODE_TYPE_REGISTER, "days": 30}
    fields.update(kw)
    c = models.RegistrationCode(code=code_str, **fields)
    db.add(c)
    db.flush()
    return c


# ---------- 1. 前缀识别 ----------

def test_is_honeypot_string():
    assert codes.is_honeypot("HONEY-ABCDEF123456") is True
    assert codes.is_honeypot("honey-abcdef123456") is True  # 大小写不敏感
    assert codes.is_honeypot("  HONEY-XYZ  ") is True
    assert codes.is_honeypot("REG-ABCDEF123456") is False
    assert codes.is_honeypot("HONEYPOT-ABC") is False  # 必须 HONEY- 带横线
    assert codes.is_honeypot("") is False
    assert codes.is_honeypot(None) is False


def test_is_honeypot_model_object(db):
    c = _code(db, "HONEY-TRAP001")
    assert codes.is_honeypot(c) is True
    c2 = _code(db, "REG-NORMAL001")
    assert codes.is_honeypot(c2) is False


def test_render_code_decoy_prefix():
    for _ in range(5):
        s = codes.render_code(code_type=codes.CODE_TYPE_REGISTER, days=30, decoy=True)
        assert s.startswith("HONEY-")
        assert codes.is_honeypot(s)
    s = codes.render_code(code_type=codes.CODE_TYPE_REGISTER, days=30, decoy=False)
    assert not codes.is_honeypot(s)


# ---------- 2. 兑换陷阱 ----------

def test_redeem_honeypot_bans_user(db, caplog):
    u = _user(db)
    code_str = codes.render_code(code_type=codes.CODE_TYPE_REGISTER, days=30, decoy=True)
    _code(db, code_str)
    db.commit()

    with caplog.at_level(logging.WARNING, logger="backend.codes"):
        result = codes.redeem_code(db, u, code_str)
    db.commit()

    assert result["success"] is False
    assert result["message"] == "卡码无效"  # 对外绝不暴露是陷阱
    db.refresh(u)
    assert u.is_active is False  # 封号
    # 陷阱不烧码：不走 claim_code，use_count 不动
    row = codes.find_reg_code(db, code_str)
    assert (row.use_count or 0) == 0
    # logger.warning 记录了谁、何时、哪个码
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("诱饵码触发" in r.getMessage() and u.username in r.getMessage()
               and code_str in r.getMessage() for r in warnings)
    # 安全日志落盘（reason=decoy_code）
    log = db.query(models.LoginLog).filter(
        models.LoginLog.reason == "decoy_code").first()
    assert log is not None
    assert code_str in (log.detail or "")


def test_disabled_honeypot_still_traps(db):
    """停用/用尽的诱饵码依然触发陷阱：判定先于可用性检查。"""
    u = _user(db, "victim2")
    code_str = codes.render_code(code_type=codes.CODE_TYPE_REGISTER, days=30, decoy=True)
    _code(db, code_str, is_active=False, max_uses=1, use_count=1)
    db.commit()

    result = codes.redeem_code(db, u, code_str)
    db.commit()

    assert result == {"success": False, "message": "卡码无效"}
    db.refresh(u)
    assert u.is_active is False


def test_normal_code_not_affected(db):
    """普通卡码：兑换成功、用户不被封禁。"""
    u = _user(db, "normal")
    _code(db, "REG-VALID00000001")
    db.commit()

    result = codes.redeem_code(db, u, "REG-VALID00000001")
    db.commit()

    assert result["success"] is True
    db.refresh(u)
    assert u.is_active is True

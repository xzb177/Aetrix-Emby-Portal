# -*- coding: utf-8 -*-
"""TG 机器人白名单专属面板回归测试（2026-11-02 白名单体验包·机器人单）。

口径：
- 白名单判定只复用 backend.codes.is_whitelist_account（#1668 名单／
  #1670 活力豁免同一口：核销痕迹＋三处永久现算），面板不另立名单；
  夹具按该口径的输入面造数（白名单码核销痕迹＋资格/查看/订阅三处永久），
  并先断言判定函数认账，夹具漂移会先红在这里。
- 白名单用户：/me、/account 账号中心与 /start 精简 dashboard（user_panel）
  换专属版式——专属问候标题＋👑专属称号行（＋创世编号行，编号落地后才有）。
- 创世编号由身份组单落库，本单只读展示：编号未落地时只显示称号行；
  编号经 user 属性或 codes 名单行任一口出现时，面板自动带出编号行。
- 普通用户：user_panel 与账号中心已于 2026-11-02 合并为同一张精简卡
  （features.account_card 唯一构建器），金快照为合并后新口径输出。
- 本文件不重绑全局引擎、不引入真实 Timer／睡眠依赖。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import codes, models  # noqa: E402
from backend.realms import ensure_default_realm  # noqa: E402
from backend.tg_bot import features  # noqa: E402

TG_PLAIN = 900001
TG_VIP = 900002

# 合并后（2026-11-02）账号卡新口径：user_panel 与账号中心同一张卡。
PLAIN_PANEL_GOLDEN = (
    "▎<b>欢迎进入用户面板！</b>alice\n"
    "· 🆔 <b>用户编号</b> | 1\n"
    "· 👤 <b>账号名称</b> | <b>alice</b>\n"
    "· ⭐ <b>当前等级</b> | Lv.1 初幕 · 经验 0／100 ░░░░░░░░░░（距 Lv.2 影迷还差 100 经验）\n"
    "· 🍒 <b>能量余额</b> | <b>42</b>\n"
    "· 📊 <b>公益资格</b> | 未开通\n"
    "· 🎟️ <b>查看凭证</b> | 未开通\n"
    "· ⚡ <b>活力数值</b> | 14/14\n"
    "· 🔗 <b>网盘绑定</b> | 未绑定，绑定后才能点播\n"
    "· 🐉 <b>当前馆灵</b> | 🥚 蛋（累计观影 0.0 小时），距夜枭还差 50.0 小时\n"
    "· 🏅 <b>成就进度</b> | 已解锁 0／共 0 个\n"
    "· 📦 <b>订阅状态</b> | 暂无"
)

# 账号中心与用户面板已合并：同一张卡、同一份金快照。
PLAIN_ACCOUNT_GOLDEN = PLAIN_PANEL_GOLDEN


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db, tg, name, **kw):
    fields = {"password_hash": "x", "telegram_id": tg, "points": 42,
              "vitality": 14, "is_active": True}
    fields.update(kw)
    u = models.WebUser(username=name, **fields)
    db.add(u)
    db.commit()
    return u


def _make_whitelist(db, user):
    """按 codes 口径造生效中的白名单：核销痕迹＋三处永久，并断言判定认账。"""
    realm = ensure_default_realm(db)
    code = models.RegistrationCode(
        code=f"VIP-{user.username.upper()}", max_uses=1, use_count=1,
        is_active=True, code_type=codes.CODE_TYPE_WHITELIST, days=-1,
        reward_type="subscription")
    db.add(code)
    db.flush()
    db.add(models.CodeRedemption(
        code_kind=codes.REDEMPTION_KIND_REG, code_id=code.id,
        user_id=user.id))
    user.is_welfare = True
    user.welfare_expires_at = None
    db.add(models.EmbyViewUnlock(
        user_id=user.id, realm_id=realm.id, expires_at=None))
    plan = models.SubscriptionPlan(
        name="白名单永久", price=0, duration_days=36500)
    db.add(plan)
    db.flush()
    db.add(models.UserSubscription(
        user_id=user.id, plan_id=plan.id, realm_id=realm.id, status="active",
        end_date=datetime.now() + timedelta(days=36500)))
    db.commit()
    db.refresh(user)
    assert codes.is_whitelist_account(db, user) is True
    return user


def _tg(tg, name):
    return {"id": tg, "first_name": name}


# ---------- 0. 夹具与生产判定同口径 ----------

def test_fixture_matches_production_definition(db):
    plain = _user(db, TG_PLAIN, "alice")
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    assert codes.is_whitelist_account(db, vip) is True
    assert codes.is_whitelist_account(db, plain) is False


# ---------- 1. 白名单：精简 dashboard（user_panel / compact_dashboard） ----------

def test_user_panel_whitelist_layout(db):
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    text = features.user_panel(db, vip)
    # 专属问候标题（称号唯一来源 codes.WHITELIST_TITLE，断言按常量取值）
    assert text.startswith(f"▎<b>欢迎回来，{codes.WHITELIST_TITLE}！</b>vip")
    # 称号行：紧跟账号名称行、在当前等级行之前（合并卡行序）
    title_row = f"· 👑 <b>专属称号</b> | <b>{codes.WHITELIST_TITLE}</b>"
    assert title_row in text
    assert text.index("账号名称") < text.index("专属称号") < text.index("当前等级")
    # 编号尚未落地：只显示称号，不出编号行
    assert "创世编号" not in text
    # 其余信息行照现行口径（三处永久 → 长期有效、能量照旧）
    assert "· 📊 <b>公益资格</b> | 长期有效" in text
    assert "· 🎟️ <b>查看凭证</b> | 长期有效" in text
    assert "· 🍒 <b>能量余额</b> | <b>42</b>" in text


def test_compact_dashboard_whitelist_layout(db):
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    text = features.compact_dashboard(db, vip, display_name="Vip")
    assert f"· 👑 <b>专属称号</b> | <b>{codes.WHITELIST_TITLE}</b>" in text
    assert f"欢迎回来，{codes.WHITELIST_TITLE}！" in text


# ---------- 1b. 称号唯一来源：codes.WHITELIST_TITLE，面板不许再硬写一份 ----------

def test_title_follows_codes_constant(db, monkeypatch):
    """临时改 codes.WHITELIST_TITLE 的值，两个面板的问候与称号行必须随动。

    这是称号来源的自证：面板侧若再硬写字面，本例立刻红。
    """
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    monkeypatch.setattr(codes, "WHITELIST_TITLE", "探针称号")
    text = features.user_panel(db, vip)
    assert "· 👑 <b>专属称号</b> | <b>探针称号</b>" in text
    assert "欢迎回来，探针称号！" in text
    account = features.handle_account(db, _tg(TG_VIP, "Vip"), TG_VIP, "")
    assert "· 👑 <b>专属称号</b> | <b>探针称号</b>" in account


# ---------- 2. 白名单：账号中心（/account、/me） ----------

def test_account_whitelist_layout(db):
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    text = features.handle_account(db, _tg(TG_VIP, "Vip"), TG_VIP, "")
    # 合并卡：账号中心与用户面板同一版式（含专属问候标题）
    assert text.startswith(f"▎<b>欢迎回来，{codes.WHITELIST_TITLE}！</b>vip")
    title_row = f"· 👑 <b>专属称号</b> | <b>{codes.WHITELIST_TITLE}</b>"
    assert title_row in text
    # 称号行紧跟账号名称行、在当前等级行之前
    assert text.index("账号名称") < text.index("专属称号") < text.index("当前等级")
    assert "创世编号" not in text
    # 其余信息行照现行口径
    assert "· 👤 <b>账号名称</b> | <b>vip</b>" in text
    assert "· 🍒 <b>能量余额</b> | <b>42</b>" in text
    assert "· 📊 <b>公益资格</b> | 长期有效" in text


# ---------- 3. 创世编号落地后自动带出（只读兼容） ----------

def test_serial_shown_from_user_attribute(db):
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    vip.whitelist_no = 7  # 模拟身份组单落库后的字段
    text = features.user_panel(db, vip)
    assert "· 🔢 <b>创世编号</b> | No.7" in text
    account = features.handle_account(db, _tg(TG_VIP, "Vip"), TG_VIP, "")
    assert "· 🔢 <b>创世编号</b> | No.7" in account


def test_serial_shown_from_codes_roster_row(db, monkeypatch):
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    monkeypatch.setattr(
        codes, "whitelist_accounts",
        lambda db_: [{"user_id": vip.id, "serial": 3}])
    text = features.user_panel(db, vip)
    assert "· 🔢 <b>创世编号</b> | No.3" in text


# ---------- 4. 普通用户逐字不变 ----------

def test_user_panel_plain_user_unchanged(db):
    plain = _user(db, TG_PLAIN, "alice")
    assert features.user_panel(db, plain) == PLAIN_PANEL_GOLDEN


def test_account_plain_user_unchanged(db):
    plain = _user(db, TG_PLAIN, "alice")
    text = features.handle_account(db, _tg(TG_PLAIN, "Alice"), TG_PLAIN, "")
    assert text == PLAIN_ACCOUNT_GOLDEN


# ---------- 5. 白名单失效（回收/永久缺一）→ 回落普通版式 ----------

def test_revoked_whitelist_falls_back_to_plain_layout(db):
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    unlock = (db.query(models.EmbyViewUnlock)
              .filter(models.EmbyViewUnlock.user_id == vip.id).first())
    unlock.expires_at = datetime.now() + timedelta(days=30)
    db.commit()
    assert codes.is_whitelist_account(db, vip) is False
    text = features.user_panel(db, vip)
    assert "专属称号" not in text
    assert text.startswith("▎<b>欢迎进入用户面板！</b>vip")
    account = features.handle_account(db, _tg(TG_VIP, "Vip"), TG_VIP, "")
    assert "专属称号" not in account
    assert account.startswith("▎<b>欢迎进入用户面板！</b>vip")


# ---------- 6. 创世编号真口径：whitelist_serials 表（身份组 #1680） ----------

def test_serial_shown_from_serials_table(db):
    """编号在 WhitelistSerial 表里时，面板必须经 codes.whitelist_serial_for 读出。

    回归：面板原探查名单（user 属性／codes 同名 helper／名单行 serial 键）
    三处都对不上 #1680 的落地口径，编号已落库也恒显示不出来。
    """
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    db.add(models.WhitelistSerial(user_id=vip.id, serial=5))
    db.commit()
    identity = features._whitelist_identity(db, vip)
    assert identity is not None and identity["serial"] == 5
    rows = features._whitelist_rows(identity)
    assert any("创世编号" in row and "No.5" in row for row in rows)
    text = features.user_panel(db, vip)
    assert "· 🔢 <b>创世编号</b> | No.5" in text
    account = features.handle_account(db, _tg(TG_VIP, "Vip"), TG_VIP, "")
    assert "· 🔢 <b>创世编号</b> | No.5" in account


def test_serial_none_when_no_serial_row(db):
    """白名单但未分配编号：身份仍在，编号为 None、面板只出称号行。"""
    vip = _make_whitelist(db, _user(db, TG_VIP, "vip"))
    identity = features._whitelist_identity(db, vip)
    assert identity is not None and identity["serial"] is None
    assert "创世编号" not in features.user_panel(db, vip)


def test_serial_row_ignored_for_plain_user(db):
    """非白名单账号即使有编号行也不出专属版式（判定仍走 is_whitelist_account）。"""
    plain = _user(db, TG_PLAIN, "alice")
    db.add(models.WhitelistSerial(user_id=plain.id, serial=9))
    db.commit()
    assert features._whitelist_identity(db, plain) is None
    assert "创世编号" not in features.user_panel(db, plain)

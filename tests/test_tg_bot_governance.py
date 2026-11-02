"""TG Bot 等级治理 bot 补完：/level、/report、/propose、/launch 与新版 dashboard。

只测本模块：DB 用内存 SQLite；Telegram 发送全部 patch 掉，不发真实请求。
版式依据：用户 2026-10-29 08:44 亲批 dashboard 样例（逐字结构）与
等级公益服活动治理设计稿权利表。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em
from backend.tg_bot import features, handlers, poller, sender

TICKS = 10_000_000
GROUP = -100123
TG = 900001


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


def _user(db, tg=TG, name="alice", points=42, vitality=14, xp=0,
          is_welfare=False, welfare_expires_at=None):
    row = models.WebUser(
        username=name, password_hash="x", telegram_id=tg,
        points=points, vitality=vitality, member_xp=xp,
        is_welfare=is_welfare, welfare_expires_at=welfare_expires_at,
    )
    db.add(row)
    db.commit()
    return row


def _tg(tg=TG, first_name="Alice"):
    return {"id": tg, "first_name": first_name}


def _free_realm(db):
    realm = models.ServerRealm(name="公益服", slug="main", access_mode="free")
    db.add(realm)
    db.commit()
    db.add(models.SystemConfig(key="active_realm_id", value=str(realm.id)))
    db.commit()
    return realm


def _watch(db, user_id, hours_list):
    for i, h in enumerate(hours_list):
        item = em.MediaItem(
            guid=f"test-item-{user_id}-{i}", library_id=1,
            item_type="movie", name=f"片{i}",
            duration_ticks=int(h * 3600 * TICKS),
        )
        db.add(item)
        db.flush()
        db.add(em.PlaybackSession(
            user_id=user_id, item_id=item.id,
            position_ticks=int(h * 3600 * TICKS),
            session_key=f"test-sess-{user_id}-{i}",
        ))
    db.commit()


def _achievements(db, user_id, total=13, unlocked=0):
    for i in range(total):
        db.add(models.PanelAchievementDef(
            key=f"k{i}", name=f"成就{i}", desc="d", icon="🏅",
            sort=i + 1, enabled=True))
    for i in range(unlocked):
        db.add(models.PanelAchievementUnlock(user_id=user_id, ach_key=f"k{i}"))
    db.commit()


def _binding(db, user_id, provider, status="active"):
    db.add(em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label="盘",
        credential_id=1, status=status))
    db.commit()


def _checkin(db, user_id, streak, date=datetime(2026, 10, 28)):
    db.add(models.CheckinRecord(
        user_id=user_id, checkin_date=date, streak=streak))
    db.commit()


def _seek(db, user_id, name="求片A", realm_id=None, status="pending"):
    row = models.MovieRequest(
        user_id=user_id, movie_name=name, status=status, realm_id=realm_id)
    db.add(row)
    db.commit()
    return row


def _freeze(db, user_id):
    db.add(models.GovernanceFreeze(
        user_id=user_id, reason="test", created_by=user_id,
        expires_at=datetime.now() + timedelta(days=30)))
    db.commit()


def _dispatch(db, monkeypatch, text, chat_id=TG, chat_type="private"):
    sent = []

    def fake_send(db_, chat_id_, text_, reply_markup=None, parse_mode="HTML"):
        sent.append(text_)
        return (True, None)

    monkeypatch.setattr(sender, "send_message", fake_send)

    def fake_send_ex(db_, chat_id_, text_, reply_markup=None, parse_mode="HTML"):
        sent.append(text_)
        return (True, None, 1)

    monkeypatch.setattr(sender, "send_message_ex", fake_send_ex)
    monkeypatch.setattr(
        "backend.tg_bot.ephemeral.schedule_delete", lambda *a, **k: True)
    update = {
        "update_id": 1,
        "message": {"message_id": 1, "from": _tg(), "chat": {"id": chat_id, "type": chat_type},
                    "text": text},
    }
    import backend.tg_bot.router as router_mod
    router_mod.dispatch(db, update)
    return sent


# ---------- 新版 dashboard：样例逐字结构 ----------

def test_dashboard_sample_layout_exact(db):
    """用户批准的卡片式用户面板逐字结构：▎标题＋「· emoji 标签 | 值」信息行。

    查看凭证日期用 2030 年（样例的 2026-11-27 在该日后会过期，测试不用时间炸弹）；
    公益资格行不校验过期，可直接用样例日期。
    """
    row = _user(db, tg=TG, name="shunxian2580", points=57, vitality=14,
                is_welfare=True, welfare_expires_at=datetime(2026, 11, 27))
    _achievements(db, row.id, total=13, unlocked=0)
    db.add(models.EmbyViewUnlock(
        user_id=row.id, realm_id=1, expires_at=datetime(2030, 11, 27)))
    db.commit()
    _binding(db, row.id, "drive")
    _checkin(db, row.id, 2)
    text = features.compact_dashboard(db, row, display_name="Shun")
    expected = "\n".join([
        "▎<b>欢迎进入用户面板！</b>Shun",
        f"· 🆔 <b>用户编号</b> | {row.id}",
        "· 👤 <b>账号名称</b> | <b>shunxian2580</b>",
        "· ⭐ <b>当前等级</b> | Lv.1 初幕 · 经验 0／100 ░░░░░░░░░░（距 Lv.2 影迷还差 100 经验）",
        "· 🍒 <b>能量余额</b> | <b>57</b>",
        "· 📊 <b>公益资格</b> | 至 2026-11-27",
        "· 🎟️ <b>查看凭证</b> | 至 2030-11-27",
        "· ⚡ <b>活力数值</b> | 14/14",
        "· 🔗 <b>网盘绑定</b> | Drive 已绑 ✓",
        "· 🐉 <b>当前馆灵</b> | 🥚 蛋（累计观影 0.0 小时），距夜枭还差 50.0 小时",
        "· 🏅 <b>成就进度</b> | 已解锁 0／共 13 个",
        "· 📦 <b>订阅状态</b> | 暂无",
    ])
    assert text == expected


def test_account_level_line(db):
    row = _user(db, xp=0)
    text = features.handle_account(db, _tg(), TG, "")
    assert "⭐ <b>当前等级</b> | Lv.1 初幕 · 经验 0／100 ░░░░░░░░░░（距 Lv.2 影迷还差 100 经验）" in text
    row2 = _user(db, tg=TG + 1, name="max", xp=20000)
    text2 = features.handle_account(db, _tg(TG + 1), TG + 1, "")
    assert "⭐ <b>当前等级</b> | Lv.6 传奇 · 经验 20000（已满级）" in text2


# ---------- /level ----------

def test_level_free_realm(db):
    _free_realm(db)
    row = _user(db, xp=100)
    text = features.handle_level(db, _tg(), TG, "")
    assert "⭐ Lv.2 影迷" in text
    assert "经验 100／500" in text
    assert "距 Lv.3 鉴赏家还差 400 经验" in text
    assert "本月提案：0／1 次" in text
    assert "本月直接发起：Lv.5 起开放" in text
    assert "附议权重" in text and "我的权重：2 票" in text
    assert "举报复核" in text and "我的权重：无（Lv.3 起获得）" in text


def test_level_paid_realm_no_rights_table(db):
    row = _user(db, xp=100)
    text = features.handle_level(db, _tg(), TG, "")
    assert "⭐ Lv.2 影迷" in text
    assert "治理权表仅公益服开放" in text
    assert "附议权重" not in text


def test_level_frozen(db):
    _free_realm(db)
    row = _user(db, xp=1500)
    _freeze(db, row.id)
    text = features.handle_level(db, _tg(), TG, "")
    assert "治理特权已被冻结" in text
    assert "我的权重：1 票" in text


def test_level_unbound(db):
    assert "绑定" in features.handle_level(db, _tg(), TG, "")


# ---------- /report ----------

def test_report_success_and_duplicate(db):
    realm = _free_realm(db)
    reporter = _user(db)
    seek = _seek(db, reporter.id, realm_id=realm.id)
    text = features.handle_report(db, _tg(), TG, f"{seek.id} 刷屏 重复发了好几次")
    assert "举报已提交" in text and "刷屏／广告" in text
    dup = features.handle_report(db, _tg(), TG, f"{seek.id} 违规")
    assert "已举报过该求片" in dup


def test_report_bad_reason_and_missing_args(db):
    _free_realm(db)
    _user(db)
    assert "刷屏、重复、违规" in features.handle_report(db, _tg(), TG, "1 不知道")
    assert "/report 求片编号" in features.handle_report(db, _tg(), TG, "")


def test_report_frozen(db):
    realm = _free_realm(db)
    reporter = _user(db)
    _freeze(db, reporter.id)
    seek = _seek(db, reporter.id, realm_id=realm.id)
    assert "治理特权已被冻结" in features.handle_report(db, _tg(), TG, f"{seek.id} 违规")


# ---------- /propose /launch ----------

def test_propose_success_and_cap(db):
    _free_realm(db)
    row = _user(db, xp=100)
    text = features.handle_propose(db, _tg(), TG, "周末观影趴 | 能量:100x2 | -100123")
    assert "活动提案已提交" in text
    capped = features.handle_propose(db, _tg(), TG, "第二场 | 能量:50x1 | -100123")
    assert "本月活动提案已达上限" in capped


def test_propose_level_gate_and_bad_prize(db):
    _free_realm(db)
    _user(db, xp=0)
    gated = features.handle_propose(db, _tg(), TG, "周末趴 | 能量:100x1 | -100123")
    assert "Lv.2" in gated
    bad = features.handle_propose(db, _tg(), TG, "周末趴 | 金币:100x1 | -100123")
    assert "奖品类型只能是" in bad


def test_launch_success_and_gate(db):
    _free_realm(db)
    row = _user(db, xp=5000)
    text = features.handle_direct_launch(db, _tg(), TG, "周末观影趴 | 能量:100x2 | -100123")
    assert "活动已直接发起" in text
    low = _user(db, tg=TG + 1, name="low", xp=100)
    gated = features.handle_direct_launch(db, _tg(TG + 1), TG + 1, "周末趴 | 能量:100x1 | -100123")
    assert "Lv.5" in gated


# ---------- 路由与菜单 ----------

def test_level_private_only_in_group_report_propose_private_only(db, monkeypatch):
    # /level 已改为账号卡别名、账号卡私聊化：群里与 report/propose
    # 同口径，只回私聊提示（覆盖旧「/level 群内放行」口径）。
    _user(db)
    sent = _dispatch(db, monkeypatch, "/level", chat_id=GROUP, chat_type="group")
    assert sent and "请私聊" in sent[0] and "会员等级" not in sent[0]
    for cmd in ("/report 1 违规", "/propose 标题 | 能量:10x1 | -100123"):
        sent = _dispatch(db, monkeypatch, cmd, chat_id=GROUP, chat_type="group")
        assert sent and "请私聊" in sent[0], cmd


def test_bot_commands_cover_new_commands():
    registered = {cmd for cmd, _desc in poller.BOT_COMMANDS}
    assert {"report", "propose", "launch"} <= registered
    assert "level" not in registered  # /level 已摘出菜单（手输别名保留）

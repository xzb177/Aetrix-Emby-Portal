"""开奖公告中奖名单格式（2026-10-31 用户口径）：昵称＋实际中奖金额。

旧格式每行是「🥇 奖品名：用户****<TG号后四位>」——群里没人对得上是谁，
且不写金额。用户 2026-10-31 点选新口径：
- 每行「🥇 <昵称> — <奖品名＋金额>」，金额按该中奖人实际中的 prize_id 取；
- 昵称优先 TG 显示名（开奖时经 getChatMember 现取），取不到兜底站内用户名；
- 取名有总预算、失败/异常/退群一律兜底，绝不拖住或拖垮开奖公告；
- 名字按 HTML parse_mode 转义，特殊字符不能打坏公告格式；
- 完整 telegram_id 仍不许出现在公告里（隐私底线不变）。
"""
import sys

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend import lottery


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.WebUser.__table__.create(engine, checkfirst=True)
    models.SystemConfig.__table__.create(engine, checkfirst=True)
    models.LotteryRound.__table__.create(engine, checkfirst=True)
    models.LotteryRoundPrize.__table__.create(engine, checkfirst=True)
    models.LotteryRoundEntry.__table__.create(engine, checkfirst=True)
    models.LotteryRoundWinner.__table__.create(engine, checkfirst=True)
    models.LotteryBlacklist.__table__.create(engine, checkfirst=True)
    models.PointsLog.__table__.create(engine, checkfirst=True)
    models.WelfareGrantLog.__table__.create(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_user(db, username, tg_id):
    user = models.WebUser(username=username, password_hash="", telegram_id=tg_id)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_round_with_winners(db, monkeypatch, tg_names, *, raise_on_fetch=False,
                              fetch_override=None):
    """建一期两奖品（能量 500 / 公益 30 天）两中奖人，返回 (round, group_text)。

    tg_names: {telegram_id: member_result_dict | None}；
    None 表示 getChatMember 失败（退群/查无此人）。
    fetch_override 非空时直接用它当 get_chat_member_raw 的替身。
    中奖映射手工指定：user_a 中能量奖、user_b 中公益天数奖，
    以便逐行断言各自金额，互不串额。
    """
    import backend.tg_bot.sender as sender_mod

    sent = []

    def fake_send(db_, chat_id, text, reply_markup=None):
        sent.append((chat_id, text))
        return True, None

    def fake_get_member(db_, chat_id, user_id):
        if raise_on_fetch:
            raise RuntimeError("telegram api down")
        member = tg_names.get(user_id)
        if member is None:
            return False, "user not found", None
        return True, None, member

    monkeypatch.setattr(sender_mod, "send_message", fake_send)
    monkeypatch.setattr(sender_mod, "get_chat_member_raw",
                        fetch_override or fake_get_member)

    rnd = lottery.create_round(
        db,
        title="名单格式测试",
        chat_id=-100222,
        prizes=[
            {"name": "能量奖励", "type": "points", "value": 500, "quantity": 1},
            {"name": "公益月卡", "type": "days", "value": 30, "quantity": 1},
        ],
        draw_at=None,
    )
    user_a = _make_user(db, "site_user_a", 111111111)
    user_b = _make_user(db, "site_user_b", 222222222)
    entries = {}
    for u in (user_a, user_b):
        res = lottery.join_round(db, rnd, u, u.telegram_id)
        assert res["ok"], res
        entries[u.id] = (
            db.query(models.LotteryRoundEntry)
            .filter(models.LotteryRoundEntry.round_id == rnd.id,
                    models.LotteryRoundEntry.user_id == u.id)
            .first()
        )
    prizes = {
        p.name: p
        for p in db.query(models.LotteryRoundPrize)
        .filter(models.LotteryRoundPrize.round_id == rnd.id)
        .all()
    }
    winners = [
        models.LotteryRoundWinner(
            round_id=rnd.id, entry_id=entries[user_a.id].id,
            prize_id=prizes["能量奖励"].id, distributed=True),
        models.LotteryRoundWinner(
            round_id=rnd.id, entry_id=entries[user_b.id].id,
            prize_id=prizes["公益月卡"].id, distributed=True),
    ]
    db.add_all(winners)
    db.commit()
    result = lottery.notify_draw_results(db, rnd.id, winners)
    assert result["group"] is True
    group_texts = [t for c, t in sent if c == rnd.chat_id]
    assert len(group_texts) == 1
    return rnd, group_texts[0]


def test_winner_lines_show_tg_name_and_own_amount(db, monkeypatch):
    """昵称命中 TG 显示名；每行金额是本人中的那份奖（500 能量 / 公益 30 天，不串额）。"""
    _, text = _make_round_with_winners(db, monkeypatch, {
        111111111: {"user": {"first_name": "小明", "last_name": ""}, "status": "member"},
        222222222: {"user": {"first_name": "阿花", "last_name": "陈"}, "status": "member"},
    })
    assert "🥇 小明 — 能量奖励：500能量" in text
    assert "🥇 阿花 陈 — 公益月卡：公益30天" in text
    # 旧的打码名单不许再出现，完整 TG 号不许泄漏
    assert "****" not in text
    assert "111111111" not in text
    assert "222222222" not in text


def test_winner_lines_fallback_to_username_when_tg_name_missing(db, monkeypatch):
    """getChatMember 失败（退群/查无此人）→ 兜底站内用户名，公告照常发出。"""
    _, text = _make_round_with_winners(db, monkeypatch, {
        111111111: None,
        222222222: None,
    })
    assert "🥇 site_user_a — 能量奖励：500能量" in text
    assert "🥇 site_user_b — 公益月卡：公益30天" in text
    assert "111111111" not in text


def test_winner_lines_fallback_when_fetch_raises(db, monkeypatch):
    """取名单接口抛异常也不能拖垮开奖公告：兜底站内名、函数不抛。"""
    _, text = _make_round_with_winners(db, monkeypatch, {}, raise_on_fetch=True)
    assert "🥇 site_user_a — 能量奖励：500能量" in text
    assert "🥇 site_user_b — 公益月卡：公益30天" in text


def test_winner_name_html_escaped(db, monkeypatch):
    """TG 显示名带 HTML 特殊字符必须转义，不能打坏公告的 HTML 格式。"""
    evil = '<b>坏人</b>&"x"'
    _, text = _make_round_with_winners(db, monkeypatch, {
        111111111: {"user": {"first_name": evil, "last_name": ""}, "status": "member"},
        222222222: {"user": {"first_name": "正常名", "last_name": ""}, "status": "member"},
    })
    assert evil not in text
    assert "&lt;b&gt;坏人&lt;/b&gt;&amp;&quot;x&quot;" in text


def test_name_fetch_budget_exhausted_means_no_fetch(db, monkeypatch):
    """取名总预算耗尽（=0）时一次 API 都不许打，全员兜底站内名。"""
    calls = []

    def counting_get_member(db_, chat_id, user_id):
        calls.append(user_id)
        return True, None, {"user": {"first_name": "不该被取到"}, "status": "member"}

    monkeypatch.setattr(lottery, "_WINNER_NAME_FETCH_BUDGET_SEC", 0)
    _, text = _make_round_with_winners(db, monkeypatch, {},
                                        fetch_override=counting_get_member)
    assert calls == []
    assert "🥇 site_user_a — 能量奖励：500能量" in text
    assert "不该被取到" not in text


def test_prize_label_unchanged_and_winner_text_same_source(db):
    """同源一致：公告卡 _prize_label 输出与改版前逐字一致（编辑刷新路径不变），
    中奖行的奖品段与公告卡的一份口径相同（都出自 _prize_unit_text）。"""
    class P:
        def __init__(self, name, type_, value, quantity):
            self.name, self.type, self.value, self.quantity = name, type_, value, quantity

    assert lottery._prize_label(P("能量奖励", "points", 500, 88)) == "🎁 能量奖励：500能量 ×88"
    assert lottery._prize_label(P("公益月卡", "days", 30, 2)) == "🎁 公益月卡：公益30天 ×2"
    assert lottery._prize_label(P("白名单", "whitelist", 0, 1)) == "🎁 白名单：白名单 ×1"
    assert lottery._prize_label(P("神秘奖", "other", 0, 3)) == "🎁 神秘奖 ×3"
    assert lottery._prize_unit_text(P("能量奖励", "points", 500, 88)) == "能量奖励：500能量"
    assert lottery._prize_unit_text(P("公益月卡", "days", 30, 2)) == "公益月卡：公益30天"

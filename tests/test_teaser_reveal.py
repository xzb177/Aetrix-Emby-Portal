"""探险预告 · 保密新大陆（揭晓位配置）单元测试

这张卡的全部价值就一句话——「揭晓前不许漏，到了日子自动换」。
钉住三条底线：

1. **未到日期只下发保密文案**：公开视图里出现揭晓后标题 / 描述 / 链接
   中的任何一个 = 保密事故，直接判失败。
2. **到日子（服务端时间）下发揭晓内容**：状态翻转只看服务端时间，
   不信任客户端时区（所以时间判定做在后端，前端只做展示）。
3. **无配置回退现状**：什么都不配时，行为与原来写死的悬念卡逐字一致
   （标题 ``? ? ?``、副文案「神秘新大陆 · 到日子自动揭晓」、无倒计时）。

全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em
from backend.emby_server import portal, teaser
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 配置热缓存是进程级的：进出都清一次，避免污染别的内存库测试
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _set(db, values: dict):
    """直接写 SystemConfig（绕过 write_admin 的校验，测读路径用）"""
    for k, v in values.items():
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == k).first()
        if row is None:
            row = models.SystemConfig(key=k, value=v)
            db.add(row)
        else:
            row.value = v
    db.commit()
    store.invalidate()


_NOW = datetime(2026, 10, 13, 20, 0, 0)


def test_no_config_falls_back_to_static_card(db):
    """无配置 = 原来的写死悬念卡：默认文案、无倒计时"""
    view = teaser.public_view(db, now=_NOW)
    assert view is not None
    assert view["status"] == "secret"
    assert view["title"] == "? ? ?"
    assert view["subtitle"] == "神秘新大陆 · 到日子自动揭晓"
    assert view["countdown_days"] is None
    # 保密本分：公开视图里不许出现揭晓后字段
    assert "description" not in view and "link" not in view


def test_disabled_hides_card(db):
    """开关关 = 整张卡不显示（公开视图 None）"""
    _set(db, {"teaser_enabled": "0"})
    assert teaser.public_view(db, now=_NOW) is None


def test_before_reveal_only_secret_copy(db):
    """未到日期：只下发保密文案，揭晓后内容一个字都不许漏"""
    _set(db, {
        "teaser_enabled": "1",
        "teaser_reveal_at": "2026-11-01T10:00",
        "teaser_pre_title": "敬请期待",
        "teaser_pre_subtitle": "大事将近",
        "teaser_post_title": "绝密新功能上线",
        "teaser_post_desc": "这是揭晓后的描述",
        "teaser_post_link": "/game/operator",
    })
    view = teaser.public_view(db, now=_NOW)
    assert view["status"] == "secret"
    assert view["title"] == "敬请期待"
    assert view["subtitle"] == "大事将近"
    assert view["countdown_days"] == 19  # 10-13 → 11-01
    dumped = str(view)
    assert "绝密新功能上线" not in dumped
    assert "揭晓后的描述" not in dumped
    assert "/game/operator" not in dumped


def test_after_reveal_serves_content(db):
    """到日子（服务端时间）：自动换成揭晓后内容卡"""
    _set(db, {
        "teaser_reveal_at": "2026-10-13T19:00",
        "teaser_post_title": "新大陆开张",
        "teaser_post_desc": "全新玩法上线",
        "teaser_post_link": "/game/map",
    })
    view = teaser.public_view(db, now=_NOW)
    assert view["status"] == "revealed"
    assert view["title"] == "新大陆开张"
    assert view["description"] == "全新玩法上线"
    assert view["link"] == "/game/map"


def test_revealed_but_no_post_title_stays_secret(db):
    """到了日子但揭晓后标题为空：继续保密，不给用户看空卡"""
    _set(db, {"teaser_reveal_at": "2026-10-01T00:00"})
    view = teaser.public_view(db, now=_NOW)
    assert view["status"] == "secret"
    assert view["title"] == "? ? ?"


def test_invalid_reveal_at_treated_as_unset(db):
    """非法日期 = 没配：保密中、无倒计时，不炸"""
    _set(db, {"teaser_reveal_at": "不是日期"})
    view = teaser.public_view(db, now=_NOW)
    assert view["status"] == "secret"
    assert view["countdown_days"] is None


def test_countdown_today(db):
    """揭晓日当天（还没到时间点）：显示今天揭晓"""
    _set(db, {"teaser_reveal_at": "2026-10-13T23:59"})
    view = teaser.public_view(db, now=_NOW)
    assert view["status"] == "secret"
    assert view["countdown_days"] == 0


def test_write_rejects_unknown_key(db):
    with pytest.raises(ValueError, match="未知配置项"):
        teaser.write_admin(db, {"teaser_hacker": "1"})


def test_write_rejects_bad_datetime(db):
    with pytest.raises(ValueError, match="格式不对"):
        teaser.write_admin(db, {"teaser_reveal_at": "明天"})


def test_write_rejects_bad_link(db):
    with pytest.raises(ValueError, match="跳转链接"):
        teaser.write_admin(db, {"teaser_post_link": "javascript:alert(1)"})
    # 协议相对 URL（//evil.com）不算站内路由，照样拒绝
    with pytest.raises(ValueError, match="跳转链接"):
        teaser.write_admin(db, {"teaser_post_link": "//evil.com/x"})


def test_write_accepts_good_links(db):
    cfg = teaser.write_admin(db, {"teaser_post_link": "/game/map"})
    assert cfg["post_link"] == "/game/map"
    cfg = teaser.write_admin(db, {"teaser_post_link": "https://example.com/x"})
    assert cfg["post_link"] == "https://example.com/x"
    cfg = teaser.write_admin(db, {"teaser_post_link": ""})
    assert cfg["post_link"] == ""


def test_write_round_trip(db):
    """管理端写 → 读：值一致，派生状态正确"""
    cfg = teaser.write_admin(db, {
        "teaser_enabled": "1",
        "teaser_reveal_at": (_NOW + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
        "teaser_post_title": "三天后见",
    })
    assert cfg["enabled"] is True
    assert cfg["post_title"] == "三天后见"
    assert cfg["status"] == "secret"
    # 未写过的字段保持默认（空）
    assert cfg["pre_title"] == ""


def _user(db, username="teaser_alice"):
    row = models.WebUser(username=username, password_hash="x")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _call_calendar(db, user):
    request = SimpleNamespace(query_params={}, base_url="http://portal.test/")
    return portal.get_chase_calendar(request=request, request_user=user, db=db)


def test_calendar_response_carries_teaser(db):
    """日历接口：teaser 字段随响应下发；关掉后为 None"""
    user = _user(db)
    payload = _call_calendar(db, user)
    assert "teaser" in payload
    assert payload["teaser"]["status"] == "secret"

    _set(db, {"teaser_enabled": "0"})
    payload = _call_calendar(db, user)
    assert payload["teaser"] is None


def test_calendar_response_revealed_state(db):
    """日历接口：到日子后 teaser 翻转为揭晓视图"""
    user = _user(db)
    _set(db, {
        "teaser_reveal_at": "2020-01-01T00:00",
        "teaser_post_title": "已揭晓标题",
        "teaser_post_desc": "已揭晓描述",
    })
    payload = _call_calendar(db, user)
    assert payload["teaser"]["status"] == "revealed"
    assert payload["teaser"]["title"] == "已揭晓标题"

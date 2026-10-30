"""TG Bot 内联搜片（借鉴清单第 6 项）：@bot 片名 → 站内片库候选卡片。

只测本模块：DB 用内存 SQLite；Telegram 发送全部 patch 掉，不发真实请求。
口径哨兵：内联结果绝不带任何播放地址（守 #1341 播放门禁），未绑定只给
绑定引导，可见性与站内搜索一致（软删 / merged 副本 / 隐藏 / 库范围）。
"""
from __future__ import annotations

import inspect
import json
import re
import sys
from datetime import datetime

import pytest

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em
from backend.integrations import store
from backend.tg_bot import inline_search, policy, poller, router, sender


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    for table in (
        models.WebUser.__table__,
        models.SystemConfig.__table__,
        em.MediaItem.__table__,
    ):
        table.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    router._user_cmd_limits.clear()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture()
def answered(monkeypatch):
    """捕获 answerInlineQuery 的负载。"""
    out = []
    monkeypatch.setattr(
        sender, "answer_inline_query",
        lambda db_, iq_id, results, **kw: out.append(
            {"id": iq_id, "results": results, **kw}) or (True, None),
    )
    return out


def _user(db, username="cinephile", tg_id=910001):
    u = models.WebUser(username=username, telegram_id=tg_id)
    if hasattr(u, "password_hash") and getattr(u, "password_hash") is None:
        u.password_hash = ""
    db.add(u)
    db.commit()
    return u


def _item(db, item_id, name, *, item_type="movie", year=2023, hidden=False,
          merged_into=None, deleted=False, image=None):
    row = em.MediaItem(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type=item_type, name=name, sort_name=name,
        production_year=year, is_hidden=hidden,
        merged_into_id=merged_into,
        deleted_at=datetime(2026, 1, 1) if deleted else None,
        primary_image_url=image,
        file_path=f"drive:/{name}.mkv",
    )
    db.add(row)
    db.commit()
    return row


def _iq(tg_id=910001, query="阿凡达", iq_id="iq-1"):
    return {"id": iq_id, "from": {"id": tg_id, "first_name": "影迷"},
            "query": query, "offset": ""}


def _cfg(db):
    return policy.read_bot_config(db)


# ---------------- 命中与门禁哨兵 ----------------

def test_bound_user_search_hit_no_playback_fields(db, answered):
    _user(db)
    _item(db, 1, "阿凡达", year=2009,
          image="https://image.tmdb.org/t/p/w500/avatar.jpg")
    _item(db, 2, "阿凡达2：水之道", year=2022)
    inline_search.handle_inline_query(db, _iq(), _cfg(db))
    assert len(answered) == 1
    results = answered[0]["results"]
    assert len(results) == 2
    assert results[0]["title"] == "阿凡达"
    assert "2009" in results[0]["description"]
    assert results[0]["thumb_url"].startswith("https://image.tmdb.org/")
    # is_personal：结果按人（绑定态/可见库）不同，必须声明
    assert answered[0]["is_personal"] is True
    # 哨兵：整个结果 JSON 里不许出现任何播放/直链/令牌字样
    blob = json.dumps(results, ensure_ascii=False).lower()
    for bad in ("stream", "playback", "download", "api_key", "x-emby",
                "play_sign", "token=", "/videos/"):
        assert bad not in blob, bad
    # 点开卡片发出的文本不许带链接（引导去站内，不给播放地址）
    text = results[0]["input_message_content"]["message_text"]
    assert "http" not in text
    assert "阿凡达" in text


def test_unbound_user_gets_bind_guide_only(db, answered):
    _item(db, 1, "阿凡达")
    inline_search.handle_inline_query(db, _iq(tg_id=999999), _cfg(db))
    assert len(answered) == 1
    assert answered[0]["results"] == []
    assert answered[0]["switch_pm_parameter"] == "bind"
    assert "绑定" in answered[0]["switch_pm_text"]


def test_empty_query_does_not_search(db, answered, monkeypatch):
    _user(db)
    called = []
    monkeypatch.setattr(inline_search, "search_items",
                        lambda *a, **kw: called.append(1) or [])
    inline_search.handle_inline_query(db, _iq(query="  "), _cfg(db))
    assert called == []
    assert answered[0]["results"] == []


def test_short_query_does_not_search(db, answered, monkeypatch):
    _user(db)
    called = []
    monkeypatch.setattr(inline_search, "search_items",
                        lambda *a, **kw: called.append(1) or [])
    inline_search.handle_inline_query(db, _iq(query="阿"), _cfg(db))
    assert called == []
    assert answered[0]["results"] == []


# ---------------- 可见性口径 ----------------

def test_visibility_soft_deleted_merged_hidden_excluded(db, answered):
    _user(db)
    _item(db, 1, "测试片")
    _item(db, 2, "测试片", deleted=True)
    _item(db, 3, "测试片", merged_into=1)
    _item(db, 4, "测试片", hidden=True)
    _item(db, 5, "测试片 第一集", item_type="episode")
    inline_search.handle_inline_query(db, _iq(query="测试片"), _cfg(db))
    results = answered[0]["results"]
    assert [r["title"] for r in results] == ["测试片"]


def test_result_limit(db, answered):
    _user(db)
    for i in range(25):
        _item(db, 100 + i, f"星球大战{i:02d}")
    inline_search.handle_inline_query(db, _iq(query="星球大战"), _cfg(db))
    results = answered[0]["results"]
    assert len(results) == inline_search.RESULT_LIMIT
    assert len(results) <= 50  # Telegram 规格上限


def test_no_match_returns_empty(db, answered):
    _user(db)
    _item(db, 1, "阿凡达")
    inline_search.handle_inline_query(db, _iq(query="不存在的片子xyz"), _cfg(db))
    assert answered[0]["results"] == []


# ---------------- 开关 / 接线 ----------------

def test_switch_off_answers_empty(db, answered, monkeypatch):
    _user(db)
    _item(db, 1, "阿凡达")
    db.add(models.SystemConfig(key="bot_inline_search_enabled", value="false"))
    db.commit()
    called = []
    monkeypatch.setattr(inline_search, "search_items",
                        lambda *a, **kw: called.append(1) or [])
    inline_search.handle_inline_query(db, _iq(), _cfg(db))
    assert called == []
    assert answered[0]["results"] == []


def test_router_dispatch_routes_inline_query(db, answered):
    _user(db)
    _item(db, 1, "阿凡达")
    router.dispatch(db, {"update_id": 42, "inline_query": _iq()})
    assert len(answered) == 1
    assert answered[0]["results"][0]["title"] == "阿凡达"


def test_poller_requests_inline_updates():
    assert '"inline_query"' in inspect.getsource(poller._poll_once)

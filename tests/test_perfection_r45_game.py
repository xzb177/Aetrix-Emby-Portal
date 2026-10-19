# -*- coding: utf-8 -*-
"""r45 游戏化域修复回归测试：成就引擎 + panel 端点。

隔离：内存 SQLite，不碰开发库/生产库，不 import backend.main。
覆盖 r45 的 7 项修复（F2–F9，F1 死表证据见轮报告、revisit 语义待用户拍板未动）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend import panel_achievements as pa
from backend.api import panel as panel_api


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    try:
        from backend.emby_server import models as em
        em.Base.metadata.create_all(engine)
    except Exception:
        pass
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def user(db):
    u = models.WebUser(username="r45_test", password_hash="x", is_active=True)
    db.add(u)
    db.commit()
    return u


# 真实播放会话数据源（emby_playback_sessions）：每 (user, item) 一行，
# 观看秒数 = position_ticks/1e7（与 backend.panel_achievements.playback_seconds 同口径）。
_session_seq = [0]


def _mk_session(db, user, name, minutes_ago=10, duration=300, genres=""):
    from backend.emby_server import models as em
    _session_seq[0] += 1
    n = _session_seq[0]
    item = em.MediaItem(
        guid=f"sess-r45-{n:06d}",
        library_id=1, item_type="movie", name=name,
        date_added=datetime.now(), genres=genres,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    s = em.PlaybackSession(
        session_key=f"sess-r45-key-{n:06d}",
        user_id=user.id,
        item_id=item.id,
        start_time=datetime.now() - timedelta(minutes=minutes_ago),
        last_update_at=datetime.now() - timedelta(minutes=minutes_ago),
        position_ticks=int(duration * 10_000_000),
    )
    db.add(s)
    db.commit()
    return s


# ---------- F5: /collector 评估线程 60 秒防抖 ----------

def test_spawn_evaluate_debounced(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            started.append(self._args)

    monkeypatch.setattr(panel_api.threading, "Thread", FakeThread)
    now = [1000.0]
    monkeypatch.setattr(panel_api.time, "monotonic", lambda: now[0])
    panel_api._eval_last.clear()

    assert panel_api._spawn_evaluate_debounced(7) is True
    assert len(started) == 1
    # 60 秒内重复请求：跳过，不起新线程
    now[0] += 10
    assert panel_api._spawn_evaluate_debounced(7) is False
    assert len(started) == 1
    # 另一个用户不受影响
    assert panel_api._spawn_evaluate_debounced(8) is True
    assert len(started) == 2
    # 60 秒后：重新起
    now[0] += 61
    assert panel_api._spawn_evaluate_debounced(7) is True
    assert len(started) == 3
    panel_api._eval_last.clear()


# ---------- F6: 馆灵阶段配置形状校验 ----------

def test_read_pet_stages_invalid_shape_falls_back(monkeypatch, db):
    import backend.servers as servers

    cases = ['"oops"', '{"a": 1}', '[]', '[1, 2]', '[{"name": "x"}]',
             '[{"name": "a", "icon": "i", "hours": "not-a-number"}]']
    for raw in cases:
        monkeypatch.setattr(servers, "config_value", lambda db_, k, d="", _r=raw: _r)
        got = panel_api._read_pet_stages(db)
        assert got == [dict(s) for s in panel_api.PET_STAGES_DEFAULT], raw


def test_read_pet_stages_valid_passthrough(monkeypatch, db):
    import backend.servers as servers
    import json

    raw = json.dumps([{"name": "幼灵", "icon": "🌱", "hours": 0},
                      {"name": "灵狐", "icon": "🦊", "hours": 10}])
    monkeypatch.setattr(servers, "config_value", lambda db_, k, d="": raw)
    got = panel_api._read_pet_stages(db)
    assert [s["hours"] for s in got] == [0, 10]


# ---------- F7: _section_or_none 异常后 rollback ----------

def test_section_or_none_rolls_back_session(db, user):
    def bad_block(db_, user_):
        db_.execute(text("select * from no_such_table_xyz"))

    assert panel_api._section_or_none(bad_block, db, user) is None
    # 同一 session 后续查询必须可用（原来会连带 PendingRollbackError）
    assert db.execute(text("select 1")).scalar() == 1


# ---------- F3: seed 并发 IntegrityError 不刷误导 warning ----------

def test_seed_race_integrity_error_silent(db, monkeypatch):
    pa.seed_achievement_defs(db)
    assert db.query(models.PanelAchievementDef).count() == 13

    # 模拟并发：删掉一条制造"本进程查无、commit 时他进程已写入"的竞态
    db.query(models.PanelAchievementDef).filter_by(key="first_night").delete()
    db.commit()
    assert db.query(models.PanelAchievementDef).count() == 12

    real_commit = db.commit

    def boom():
        raise IntegrityError("insert", None, Exception("UNIQUE constraint failed"))

    # commit 撞主键：不抛、不刷误导性 warning，返回 0
    monkeypatch.setattr(db, "commit", boom)
    assert pa.seed_achievement_defs(db) == 0
    monkeypatch.setattr(db, "commit", real_commit)
    # 终态自愈：下次正常 seed 把缺的补回来
    assert pa.seed_achievement_defs(db) == 1
    assert db.query(models.PanelAchievementDef).count() == 13


# ---------- F2: evaluate 写库异常分类 ----------

def _grant_sharer_setup(db, user):
    code = models.InvitationCode(code="R45TEST", user_id=user.id)
    db.add(code)
    db.commit()
    db.add(models.InvitationRecord(inviter_id=user.id, invitee_id=99999, code_id=code.id))
    db.commit()


def test_evaluate_write_integrity_error_ignored(db, user, monkeypatch):
    pa.seed_achievement_defs(db)
    _grant_sharer_setup(db, user)
    real_commit = db.commit
    calls = []

    def boom_once():
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError("insert", None, Exception("UNIQUE constraint failed"))
        return real_commit()

    monkeypatch.setattr(db, "commit", boom_once)
    # 并发重复解锁：吞掉，不抛
    assert pa.evaluate(user.id, db) == ["sharer"]
    monkeypatch.setattr(db, "commit", real_commit)


def test_evaluate_write_unexpected_error_reraises(db, user, monkeypatch):
    pa.seed_achievement_defs(db)
    _grant_sharer_setup(db, user)

    def boom():
        raise RuntimeError("connection lost")

    monkeypatch.setattr(db, "commit", boom)
    # 非冲突异常：不能吞成假成功，必须抛出去（后台线程会打日志）
    with pytest.raises(RuntimeError):
        pa.evaluate(user.id, db)
    # 且没写进去
    assert db.query(models.PanelAchievementUnlock).filter_by(
        user_id=user.id).count() == 0


# ---------- F4: user_tastes 有界（5000） ----------

def test_user_tastes_bounded_at_volume(db, user):
    for i in range(5200):
        _mk_session(db, user, f"movie-{i % 50}", minutes_ago=i, duration=300)
    db.commit()
    tastes = pa.user_tastes(db, user.id)
    assert set(tastes.keys()) == {t[0] for t in pa.TASTE_MAP}
    # 5200 条全部进内存也不会炸；口味计数正常返回
    assert all(isinstance(v["count"], int) for v in tastes.values())


def test_user_tastes_query_emits_limit(db, user):
    """哨兵：user_tastes 的口味口径依赖 .limit(5000) 掐住无界内存增长；
    将来有人删掉/挪动 limit 时，这里必须先红。"""
    from sqlalchemy import event

    statements = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        pa.user_tastes(db, user.id)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert any("LIMIT" in s.upper() for s in statements), \
        "user_tastes 发出的 SQL 里没见到 LIMIT——limit(5000) 可能被删了"


# ---------- F8: panel_unseal 必须校验今日封存 ----------

def test_unseal_rejects_non_parcel_item(db, user, monkeypatch):
    from backend.emby_server import models as em
    from fastapi import HTTPException

    item = em.MediaItem(guid="r45-guid-1", library_id=1, item_type="movie",
                        name="路人甲", date_added=datetime.now())
    db.add(item)
    db.commit()
    # 今日封存为空（无新入库）：任何 id 都不在封存里
    monkeypatch.setattr(panel_api, "_today_parcel_items", lambda db_: [])
    with pytest.raises(HTTPException) as exc:
        panel_api.panel_unseal(item.id, db, user)
    assert exc.value.status_code == 400


def test_unseal_accepts_parcel_item(db, user):
    """今日封存内的条目：首次拆返回 200 + 建行 + already=False；
    再次拆幂等 already=True，不多建行。"""
    from backend.emby_server import models as em

    # 今日新入库的整部 movie → _today_parcel_items 的"今日封存"口径内
    item = em.MediaItem(guid="r45-guid-parcel", library_id=1, item_type="movie",
                        name="今日封存的片", date_added=datetime.now())
    db.add(item)
    db.commit()
    assert item.id in {it.id for it in panel_api._today_parcel_items(db)}, \
        "前置：测试数据没进今日封存口径"

    got = panel_api.panel_unseal(item.id, db, user)
    assert got["code"] == 0
    data = got["data"]
    assert data["unsealed"] is True and data["already"] is False
    assert data["item"]["id"] == item.id
    assert db.query(models.PanelUnseal).filter_by(
        user_id=user.id, item_id=item.id).count() == 1

    # 幂等：第二次直接返回，不再建行
    got2 = panel_api.panel_unseal(item.id, db, user)
    assert got2["data"]["already"] is True
    assert db.query(models.PanelUnseal).filter_by(
        user_id=user.id, item_id=item.id).count() == 1

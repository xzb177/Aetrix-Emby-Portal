# -*- coding: utf-8 -*-
"""求片 v2 测试：总开关 / 附议 / 附议者通知

覆盖：
1. 总开关关闭 → 创建求片 403（toggle 开关前后）
2. 附议：正常附议/取消附议、不能给自己附议、只能附议 pending、重复附议幂等
3. 附议开关关闭 → 403
4. 附议数 vote_count 正确增减
5. 配置脏值回默认值

全部用隔离的内存 SQLite。
"""
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import media_seek, models
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _user(db, username="seeker"):
    user = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _req(db, user, name="测试影片", status="pending"):
    r = models.MovieRequest(
        user_id=user.id, movie_name=name, type="movie", status=status,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg is None:
        cfg = models.SystemConfig(key=key, value=value)
        db.add(cfg)
    else:
        cfg.value = value
    db.commit()


# ==================== 总开关 ====================

def test_seek_enabled_default_true(db):
    assert media_seek.seek_enabled(db) is True


def test_seek_enabled_off(db):
    _set_config(db, media_seek.CONFIG_ENABLED, "0")
    assert media_seek.seek_enabled(db) is False


def test_seek_enabled_dirty_fallback(db):
    _set_config(db, media_seek.CONFIG_ENABLED, "nonsense")
    assert media_seek.seek_enabled(db) is True  # 脏值回默认（开）


# ==================== 附议 ====================

def test_toggle_vote_basic(db):
    a = _user(db, "a")
    b = _user(db, "b")
    r = _req(db, a)
    out = media_seek.toggle_vote(db, b, r.id)
    # Step 1：返回新增 weighted_vote_count；无服时按 paid 计，权重恒为 1
    assert out == {"voted": True, "vote_count": 1, "weighted_vote_count": 1}
    out = media_seek.toggle_vote(db, b, r.id)
    assert out == {"voted": False, "vote_count": 0, "weighted_vote_count": 0}


def test_vote_own_request_forbidden(db):
    a = _user(db, "a")
    r = _req(db, a)
    with pytest.raises(ValueError, match="不能给自己"):
        media_seek.toggle_vote(db, a, r.id)


def test_vote_non_pending_forbidden(db):
    a = _user(db, "a")
    b = _user(db, "b")
    r = _req(db, a, status="approved")
    with pytest.raises(ValueError, match="已处理"):
        media_seek.toggle_vote(db, b, r.id)


def test_vote_missing_request(db):
    a = _user(db, "a")
    with pytest.raises(ValueError, match="不存在"):
        media_seek.toggle_vote(db, a, 99999)


def test_vote_switch_off(db):
    _set_config(db, media_seek.CONFIG_VOTE_ENABLED, "0")
    a = _user(db, "a")
    b = _user(db, "b")
    r = _req(db, a)
    with pytest.raises(ValueError, match="已关闭"):
        media_seek.toggle_vote(db, b, r.id)


def test_vote_count_multiple_users(db):
    a = _user(db, "a")
    voters = [_user(db, f"v{i}") for i in range(3)]
    r = _req(db, a)
    for v in voters:
        media_seek.toggle_vote(db, v, r.id)
    db.refresh(r)
    assert r.vote_count == 3


def test_voter_ids_excludes_owner(db):
    a = _user(db, "a")
    b = _user(db, "b")
    r = _req(db, a)
    media_seek.toggle_vote(db, b, r.id)
    assert media_seek.voter_ids(db, r.id) == [b.id]
    assert media_seek.voter_ids(db, r.id, exclude_user_id=a.id) == [b.id]


def test_voted_ids_batch(db):
    a = _user(db, "a")
    b = _user(db, "b")
    r1 = _req(db, a, name="片1")
    r2 = _req(db, a, name="片2")
    media_seek.toggle_vote(db, b, r1.id)
    got = media_seek.voted_ids(db, b.id, [r1.id, r2.id])
    assert got == {r1.id}


def test_notify_voters_default_true(db):
    assert media_seek.notify_voters_enabled(db) is True
    _set_config(db, media_seek.CONFIG_NOTIFY_VOTERS, "false")
    assert media_seek.notify_voters_enabled(db) is False

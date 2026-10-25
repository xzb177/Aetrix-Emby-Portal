"""一等播放会话（FMBY 播放全换阶段 3-1）回归测试。

覆盖：
- 服务端签发：server_session_id 与 session_key 同值、变体与租约身份挂行；
- 解析兼容：先按 server id 查，再按旧 session_key 查；
- 预建行首次上报仍过 share_guard：enforce 档超限时被拦下且不留活行；
- 预建行首次上报通过时标志清零、后续上报不再重复判定；
- 旧行（无 server id）行为不变：上报与停播解析都走旧键；
- 结束会话幂等：已结束再结束返回 False。
全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

from backend import models, share_guard
from backend.emby_server import models as em
from backend.emby_server import compat_routes, play_session
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    share_guard._notified_at.clear()
    yield session
    store.invalidate()
    share_guard._notified_at.clear()
    session.close()


def _user(db, username="alice"):
    row = models.WebUser(username=username, password_hash="x")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, guid="abcdef1234567890abcdef1234567890"):
    row = em.MediaItem(guid=guid, library_id=1, item_type="Movie", name="Test Movie")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="1.1.1.1"))


def _policy(db, **overrides):
    values = {
        share_guard.CONFIG_TRAVEL_ACTION: "off",
        share_guard.CONFIG_TRAVEL_WINDOW: "30",
        share_guard.CONFIG_CONCURRENT_ACTION: "off",
        share_guard.CONFIG_CONCURRENT_LIMIT: "2",
    }
    values.update(overrides)
    share_guard.write_policy(db, values)
    db.commit()
    return values


def test_create_session_binds_variant_and_lease_identity(db):
    user, item = _user(db), _item(db)
    row = play_session.create_session(db, user, item, variant="drive_relay",
                                      device_id="dev-001")
    assert row.server_session_id
    assert row.session_key == row.server_session_id
    assert row.variant == "drive_relay"
    assert row.device_id == "dev-001"
    assert row.lease_identity == f"ses:{row.server_session_id}"
    assert row.awaiting_first_report is True
    assert row.ended_at is None


def test_resolve_prefers_server_id_then_legacy_key(db):
    user, item = _user(db), _item(db)
    row = play_session.create_session(db, user, item)
    assert play_session.resolve_session(db, row.server_session_id).id == row.id
    # 旧行：无 server id，按 session_key 查到
    legacy = em.PlaybackSession(session_key="legacy-key", user_id=user.id,
                                item_id=item.id)
    db.add(legacy)
    db.commit()
    assert play_session.resolve_session(db, "legacy-key").id == legacy.id
    assert play_session.resolve_session(db, "no-such-key") is None


def test_precreated_first_report_still_goes_through_guard(db, monkeypatch):
    """预建行首次上报被 enforce 档拦下：返回 None，且行被置 ended 不留活行。"""
    _policy(db, share_concurrent_action="enforce", share_concurrent_limit="1")
    user, item = _user(db), _item(db)
    # 既有 1 路活会话 → 预建行是第 2 路，超限
    live = em.PlaybackSession(session_key="s1", user_id=user.id, item_id=item.id)
    db.add(live)
    db.commit()
    monkeypatch.setattr(compat_routes, "real_client_ip", lambda req: "1.1.1.1")
    row = play_session.create_session(db, user, item)
    body = {"PlaySessionId": row.server_session_id, "PositionTicks": 0}
    session, written = compat_routes._upsert_session(db, _request(), user, item, body)
    assert session is None and written is False
    db.refresh(row)
    assert row.ended_at is not None
    assert row.awaiting_first_report is False


def test_precreated_first_report_passes_then_flag_cleared(db, monkeypatch):
    """预建行首次上报通过：标志清零；第二次上报不再把同一行当新会话。"""
    _policy(db)  # 两档 off，不判定
    user, item = _user(db), _item(db)
    monkeypatch.setattr(compat_routes, "real_client_ip", lambda req: "1.1.1.1")
    row = play_session.create_session(db, user, item)
    body = {"PlaySessionId": row.server_session_id, "PositionTicks": 0}
    session, written = compat_routes._upsert_session(db, _request(), user, item, body)
    assert session is not None and session.id == row.id
    db.refresh(row)
    assert row.awaiting_first_report is False
    # 第二次上报：同一行复用，不再触发“新会话”分支（行数不变）
    compat_routes._upsert_session(db, _request(), user, item, body)
    assert db.query(em.PlaybackSession).count() == 1


def test_legacy_row_report_unchanged(db, monkeypatch):
    """旧行上报：无标志位，行为与旧版一致（位置更新、行数不变）。"""
    _policy(db)
    user, item = _user(db), _item(db)
    monkeypatch.setattr(compat_routes, "real_client_ip", lambda req: "1.1.1.1")
    legacy = em.PlaybackSession(session_key="legacy-key", user_id=user.id,
                                item_id=item.id)
    db.add(legacy)
    db.commit()
    # 位置跳变须超过 PROGRESS_SEEK_TICKS（30 秒 = 3 亿 ticks）才算“有新信息”
    # 落库；小步进度按旧节流口径被跳过，不是本阶段改动。
    body = {"PlaySessionId": "legacy-key", "PositionTicks": 400_000_000}
    session, _ = compat_routes._upsert_session(db, _request(), user, item, body)
    assert session is not None and session.id == legacy.id
    assert session.position_ticks == 400_000_000


def test_end_session_idempotent(db):
    user, item = _user(db), _item(db)
    row = play_session.create_session(db, user, item)
    assert play_session.end_session(db, row) is True
    assert play_session.end_session(db, row) is False
    assert play_session.end_session(db, None) is False

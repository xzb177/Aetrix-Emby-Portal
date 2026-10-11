"""会话键稳定性（backend/emby_server/session_routes.py + compat_routes.py）回归测试

针对已确认 bug：客户端不带 PlaySessionId 时，resolve_session_key 每次返回新的
随机键 → Playing + 每次 Progress 上报各新建一行 PlaybackSession：
1. 会话表被幻影行灌满；
2. share_guard 同播检测按 ended_at IS NULL 计数 → 正常单路播放几次上报后就
   超过默认上限 2 → enforce 档下用户自己的播放上报被 403 拦掉（自己 DoS 自己）；
3. Stopped 上报找不到活着的行，幻影行要等 janitor（10 分钟）才回收。

修复后：无 PlaySessionId 时按「用户 | 设备 | 条目」HMAC 派生稳定键，
同一设备同一条目的多次上报复用同一行；同一键的新一轮播放（已结束行被复用）
会复活该行并重新走一次判定。

全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models, share_guard
from backend.emby_server import models as em
from backend.emby_server import compat_routes, session_routes
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


def _user(db, username="alice", **kw):
    row = models.WebUser(username=username, password_hash="x", **kw)
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


def _request(device_id="dev-001"):
    headers = {
        "X-Emby-Authorization":
            f'MediaBrowser Client="TinyPlayer", Device="Box", DeviceId="{device_id}", Version="1.0"',
    }
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host="1.2.3.4"))


def _report(db, req, user, item, ended=False):
    """走线上 wrapper 的完整链路：resolve_session_key 改写 body → _upsert_session"""
    body = {"ItemId": item.guid}  # 第三方客户端：不带 PlaySessionId
    body["PlaySessionId"] = session_routes.resolve_session_key(
        db, user, body, session_routes._device_id_of(req))
    session, _ = compat_routes._upsert_session(db, req, user, item, body, ended=ended)
    return body["PlaySessionId"], session


def _live_rows(db, user_id):
    return db.query(em.PlaybackSession).filter(
        em.PlaybackSession.user_id == user_id,
        em.PlaybackSession.ended_at.is_(None),
    ).all()


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


# ==================== 核心回归 ====================

def test_no_playsessionid_reuses_single_row(db):
    """无 PlaySessionId：Playing + 3 次 Progress 只产生一行，键稳定且合法"""
    user, item, req = _user(db), _item(db), _request()
    keys = {_report(db, req, user, item)[0] for _ in range(4)}
    assert len(keys) == 1, f"同一设备同一条目应派生同一会话键，实际 {len(keys)} 个"
    key = keys.pop()
    assert re.match(r"^[A-Za-z0-9_.:-]{1,64}$", key), f"会话键不满足字符集/长度约束: {key!r}"
    rows = _live_rows(db, user.id)
    assert len(rows) == 1, f"4 次上报应只产生 1 行会话，实际 {len(rows)} 行"
    assert rows[0].session_key == key


def test_different_devices_get_different_keys(db):
    """不同设备同一条目 → 不同的键、不同的行（同播检测能看到两路）"""
    user, item = _user(db), _item(db)
    key_a, _ = _report(db, _request("dev-A"), user, item)
    key_b, _ = _report(db, _request("dev-B"), user, item)
    assert key_a != key_b, "不同设备的派生键必须不同，否则同播检测失效"
    rows = _live_rows(db, user.id)
    assert len(rows) == 2, f"两台设备应各有一行，实际 {len(rows)} 行"


def test_enforce_does_not_self_dos(db):
    """enforce 档 + 上限 2：单设备正常播放 6 次上报都不应被拦"""
    _policy(db, **{share_guard.CONFIG_CONCURRENT_ACTION: "enforce",
                   share_guard.CONFIG_CONCURRENT_LIMIT: "2"})
    user, item, req = _user(db), _item(db), _request()
    for i in range(6):
        _key, session = _report(db, req, user, item)
        assert session is not None, f"第 {i} 次上报被同播拦截：正常单路播放被自己的幻影行 DoS"
    assert len(_live_rows(db, user.id)) == 1


def test_replay_after_stop_resurrects_row(db):
    """播完 Stopped 后同一设备重播：复用同一行、ended_at 被清除、仍计入并发"""
    user, item, req = _user(db), _item(db), _request()
    key1, _ = _report(db, req, user, item)
    _report(db, req, user, item, ended=True)
    assert len(_live_rows(db, user.id)) == 0, "Stopped 后应无活着的会话"
    key2, session = _report(db, req, user, item)
    assert key2 == key1, "同一设备同一条目重播应派生同一会话键"
    assert session is not None and session.ended_at is None, "复活的行 ended_at 必须被清除"
    total = db.query(em.PlaybackSession).filter(
        em.PlaybackSession.user_id == user.id).count()
    assert total == 1, f"重播不应新增行，实际 {total} 行"
    assert len(_live_rows(db, user.id)) == 1, "重播的会话应被计入并发"


def test_client_provided_key_still_reused(db):
    """客户端自带的合法 PlaySessionId 仍原样复用（既有行为不变）"""
    user, item, req = _user(db), _item(db), _request()
    body = {"ItemId": item.guid, "PlaySessionId": "client-guid-123"}
    key = session_routes.resolve_session_key(
        db, user, body, session_routes._device_id_of(req))
    assert key == "client-guid-123"
    key2 = session_routes.resolve_session_key(
        db, user, body, session_routes._device_id_of(req))
    assert key2 == "client-guid-123"


def test_foreign_key_falls_back_to_derived(db):
    """被别的用户占用的键不复用，降级为派生键（既有安全行为不变）"""
    alice, bob = _user(db, "alice"), _user(db, "bob")
    item, req = _item(db), _request()
    db.add(em.PlaybackSession(session_key="shared-key", user_id=alice.id, item_id=item.id))
    db.commit()
    body = {"ItemId": item.guid, "PlaySessionId": "shared-key"}
    key = session_routes.resolve_session_key(
        db, bob, body, session_routes._device_id_of(req))
    assert key != "shared-key", "被占用的键绝不能复用"
    assert key.startswith("s") and len(key) == 33


# ==================== F1 并发回归 ====================

import os
import threading


@pytest.fixture()
def file_engine(tmp_path):
    """多线程共享的文件 SQLite（内存库跨连接不可见）。"""
    os.environ.setdefault("DATABASE_TYPE", "sqlite")
    engine = create_engine(
        f"sqlite:///{tmp_path}/sess_race.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _race_request():
    return SimpleNamespace(
        headers={
            "X-Emby-Authorization":
                'MediaBrowser Client="TinyPlayer", Device="Box", '
                'DeviceId="dev-race", Version="1.0"',
        },
        client=SimpleNamespace(host="1.2.3.4"),
    )


def test_concurrent_playing_upsert_converges_to_single_row(file_engine):
    """F1 回归：确定性会话键下，Playing 与紧随的 Progress 并发双报时，

    两个请求可能同时查不到已提交行而各插一行 → 撞 session_key 唯一键。
    旧代码直接 IntegrityError 500；修复后第二个请求复用 winner 的行，
    最终每键恰好一行、两个请求都成功返回。
    """
    Session = sessionmaker(bind=file_engine, expire_on_commit=False)
    seed = Session()
    user = models.WebUser(username="race_u", password_hash="x")
    seed.add(user)
    seed.commit()
    uid = user.id
    store.invalidate()
    share_guard._notified_at.clear()
    _policy(seed)
    seed.close()

    errors = []
    N = 60
    for i in range(N):
        s = Session()
        item = em.MediaItem(
            guid=f"race-item-{i:04d}".ljust(32, "0")[:32],
            library_id=1, item_type="movie", name=f"Race {i}")
        s.add(item)
        s.commit()
        iid, iguid = item.id, item.guid
        s.close()

        gate = threading.Barrier(2)

        def run():
            s2 = Session()
            try:
                u2 = s2.get(models.WebUser, uid)
                it2 = s2.get(em.MediaItem, iid)
                body = {"ItemId": iguid, "PositionTicks": 0}
                body["PlaySessionId"] = session_routes.resolve_session_key(
                    s2, u2, body, "dev-race")
                gate.wait(timeout=15)
                sess, _ = compat_routes._upsert_session(
                    s2, _race_request(), u2, it2, body)
                assert sess is not None
            except Exception as exc:  # noqa: BLE001 — 收敛到主线程断言
                errors.append(exc)
            finally:
                s2.close()

        ts = [threading.Thread(target=run) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        assert all(not t.is_alive() for t in ts), "线程未在 30s 内结束"

    assert not errors, f"并发上报出现异常（旧代码这里是 IntegrityError 500）: {errors[:3]}"

    check = Session()
    try:
        rows = check.query(em.PlaybackSession).filter(
            em.PlaybackSession.user_id == uid).all()
        assert len(rows) == N, f"每键应恰好一行，实际 {len(rows)} 行 / {N} 键"
        keys = {r.session_key for r in rows}
        assert len(keys) == N, "会话键必须互不相同（一键一行）"
    finally:
        check.close()
        store.invalidate()
        share_guard._notified_at.clear()

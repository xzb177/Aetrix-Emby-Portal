"""进度双记 position + max_position（FMBY 学习批一 #4）回归钉。

口径：
- position = 当前位置（拖回去重看会变小），续播/PlayedPercentage 仍只读它，
  行为与双记之前完全一致；
- max_position = 本轮观看的历史最远点，只增不减；老数据（列为 NULL）回退
  position 作基线，不靠批量回填；
- 完播（90% 口径）与手动标记已看/未看：position 清零处 max 同步清零，
  与现行清零口径对齐。

测试分两层：_apply_play_progress / effective_max_position_ticks 单元钉 +
真实 token 的路由级钉（Progress 上报链、Stopped 完播、标记已看/未看）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server.api import _user_data_dto, mark_played, mark_unplayed
from backend.emby_server.compat_routes import (
    _apply_play_progress,
    effective_max_position_ticks,
)

init_db()

PW = "pd-pw"
UNAME = "pd_alice"
RUNTIME = 600_000_000  # 60 秒（ticks），与种子条目时长一致


def _guid():
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    old = db.query(models.WebUser).filter(models.WebUser.username == UNAME).first()
    if old:
        db.query(em.EmbyApiToken).filter(em.EmbyApiToken.user_id == old.id).delete(
            synchronize_session=False)
        db.query(em.PlaybackSession).filter(em.PlaybackSession.user_id == old.id).delete(
            synchronize_session=False)
        db.query(em.UserMediaData).filter(em.UserMediaData.user_id == old.id).delete(
            synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
        db.commit()
    for stale in db.query(em.Library).filter(em.Library.name.like("pd双记%")).all():
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == stale.id).all()]
        if ids:
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == stale.id).delete()
    db.commit()
    user = models.WebUser(username=UNAME, password_hash="x", is_active=True,
                          is_staff=True, emby_username="pd_emby_alice",
                          emby_password=PW)
    db.add(user)
    lib = em.Library(guid=_guid(), name="pd双记库", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()

    def mk(name):
        it = em.MediaItem(guid=_guid(), library_id=lib.id, item_type="movie",
                          name=name, file_path=f"/tmp/none/{name}.mp4",
                          container="mp4", sort_name=name,
                          duration_ticks=RUNTIME)
        db.add(it)
        db.flush()
        return it

    a = mk("pd进度链")
    b = mk("pd完播")
    c = mk("pd手动标记")
    db.commit()
    data = {"user_id": user.id,
            "a": a.guid, "a_id": a.id,
            "b": b.guid, "b_id": b.id,
            "c": c.guid, "c_id": c.id}
    db.close()
    return data


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="PDTest", Device="PC", '
                 'DeviceId="pd-dev-1", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token, device="pd-dev-1"):
    return {"X-Emby-Token": token,
            "X-Emby-Authorization":
                f'MediaBrowser Client="PDTest", Device="PC", '
                f'DeviceId="{device}", Version="1.0"'}


def _umd(user_id, item_id):
    db = SessionLocal()
    row = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user_id,
        em.UserMediaData.item_id == item_id).first()
    if row is not None:
        db.expunge(row)
    db.close()
    return row


# ---------- 单元钉：_apply_play_progress 的双记规则 ----------

def _fresh_umd(pos=0, max_pos=None):
    umd = em.UserMediaData(user_id=1, item_id=2)
    umd.playback_position_ticks = pos
    umd.max_position_ticks = max_pos
    umd.play_count = 0
    umd.played = False
    return umd


def test_smaller_position_does_not_clobber_max():
    umd = _fresh_umd()
    _apply_play_progress(umd, 500, False)
    _apply_play_progress(umd, 300, False)  # 拖回去重看
    assert umd.playback_position_ticks == 300
    assert umd.max_position_ticks == 500


def test_larger_position_advances_max():
    umd = _fresh_umd()
    _apply_play_progress(umd, 500, False)
    _apply_play_progress(umd, 800, False)
    assert umd.playback_position_ticks == 800
    assert umd.max_position_ticks == 800


def test_legacy_null_max_falls_back_to_old_position():
    # 升级前老行：max 为 NULL，断点停在 500 —— 有效最远点就是 500
    umd = _fresh_umd(pos=500, max_pos=None)
    assert effective_max_position_ticks(umd) == 500
    # 第一次新上报恰好更小（先拖回去）：max 必须以旧断点为基线，不能丢
    _apply_play_progress(umd, 300, False)
    assert umd.playback_position_ticks == 300
    assert umd.max_position_ticks == 500
    # 再往前看过旧断点：max 跟进
    _apply_play_progress(umd, 700, False)
    assert umd.max_position_ticks == 700


def test_finished_clears_position_and_max_like_before():
    umd = _fresh_umd()
    _apply_play_progress(umd, 590, False)
    _apply_play_progress(umd, 595, True)  # 完播：与现行口径一致地清零
    assert umd.played is True
    assert umd.play_count == 1
    assert umd.playback_position_ticks == 0
    assert umd.max_position_ticks == 0
    _apply_play_progress(umd, 595, True)  # 重复完播：play_count 不重复加
    assert umd.play_count == 1


def test_resume_dto_still_reads_position_not_max():
    # 续播口径不变：DTO 的断点与百分比只认 position，不认 max
    umd = _fresh_umd(pos=300, max_pos=540)
    dto = _user_data_dto(umd, "guid-x", RUNTIME)
    assert dto["PlaybackPositionTicks"] == 300
    assert dto["PlayedPercentage"] == pytest.approx(300 / RUNTIME * 100.0)


# ---------- 路由级钉：真实上报链 ----------

def test_progress_chain_records_dual_positions(client, seed, token):
    h = _h(token, "pd-dev-chain")
    r = client.post("/emby/Sessions/Playing",
                    json={"ItemId": seed["a"], "PositionTicks": 0}, headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/emby/Sessions/Playing/Progress",
                    json={"ItemId": seed["a"], "PositionTicks": 5_000_000},
                    headers=h)
    assert r.status_code == 200, r.text
    r = client.post("/emby/Sessions/Playing/Progress",
                    json={"ItemId": seed["a"], "PositionTicks": 2_000_000},
                    headers=h)
    assert r.status_code == 200, r.text
    umd = _umd(seed["user_id"], seed["a_id"])
    assert umd is not None
    assert umd.playback_position_ticks == 2_000_000
    assert umd.max_position_ticks == 5_000_000
    # 再往前：max 跟进
    r = client.post("/emby/Sessions/Playing/Progress",
                    json={"ItemId": seed["a"], "PositionTicks": 8_000_000},
                    headers=h)
    assert r.status_code == 200, r.text
    umd = _umd(seed["user_id"], seed["a_id"])
    assert umd.playback_position_ticks == 8_000_000
    assert umd.max_position_ticks == 8_000_000


def test_stopped_finished_clears_both(client, seed, token):
    h = _h(token, "pd-dev-fin")
    r = client.post("/emby/Sessions/Playing/Progress",
                    json={"ItemId": seed["b"], "PositionTicks": 300_000_000},
                    headers=h)
    assert r.status_code == 200, r.text
    # 550_000_000 >= 90% * 600_000_000 → 完播：position 与 max 一起清零
    r = client.post("/emby/Sessions/Playing/Stopped",
                    json={"ItemId": seed["b"], "PositionTicks": 550_000_000},
                    headers=h)
    assert r.status_code == 200, r.text
    umd = _umd(seed["user_id"], seed["b_id"])
    assert umd.played is True
    assert umd.playback_position_ticks == 0
    assert umd.max_position_ticks == 0


def test_mark_played_and_unplayed_clear_max(seed):
    # 先把 c 的进度喂到 position=400 / max=900（模拟看了一大半又拖回去）
    db = SessionLocal()
    user = db.query(models.WebUser).filter(models.WebUser.id == seed["user_id"]).first()
    umd = em.UserMediaData(user_id=user.id, item_id=seed["c_id"],
                           playback_position_ticks=400, max_position_ticks=900,
                           play_count=0, played=False)
    db.add(umd)
    db.commit()

    mark_played(seed["c"], str(user.id), user=user, db=db)
    row = _umd(seed["user_id"], seed["c_id"])
    assert row.played is True
    assert row.playback_position_ticks == 0
    assert row.max_position_ticks == 0

    mark_unplayed(seed["c"], str(user.id), user=user, db=db)
    row = _umd(seed["user_id"], seed["c_id"])
    assert row.played is False
    assert row.play_count == 0
    assert row.playback_position_ticks == 0
    assert row.max_position_ticks == 0
    db.close()

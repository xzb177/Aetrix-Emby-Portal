"""无可用线路条目自动隐藏（清理一期，2026-11-05 用户亲定）回归。

锁住的口径：
- 判定：无界（kind=applecms）库的顶层活行（parent_id 空、未软删、
  未合并），整棵子树（自身＋子行＋孙行）映射 ≥1 且没有任何一条
  指向启用源 → 不能播，对账时藏掉（is_hidden）并记台账
  （reason=no_enabled_line），与其它原因的 is_hidden 靠台账区分。
- 剧看子树：任意一集有启用线，整剧不藏；只有集挂停用源时藏剧行
  本身，季/集行不直接动。
- 片内单条死线（同片另有活线）不藏；无映射条目不藏；非无界库
  条目不在范围。
- 台账外 is_hidden（手工藏等）对账永不 claimed、永不解藏。
- 有线自动回来：对账解藏只动台账在册且子树已有启用线的条目；
  源被重新启用（enable_source）时立即定向解藏挂该源的在册条目。
- 对账幂等：重复跑 hidden/restored 均为 0、台账行数不变。
- 过滤面补齐：收藏（FavoriteItems）与已看（PlayedItems）列表不许
  再漏 is_hidden 条目（与 Resume 既有口径一致，标记本身不删）。

测试形态照 test_pilot_entry.py：真 TestClient 打整 app、种子直落
app 库。源开关翻转除 enable_source 用例走函数本身外，其余用直改
DB 模拟「外部变化后对账兜底」。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import backend.models as models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import no_line_hidden as nlh
from backend.emby_server import upstream_sources as us
from backend.security import hash_password

init_db()

PW = "NoLine#2026"
USER = "nlh_user"
SRC_ON = "nlh_启用源"
SRC_OFF = "nlh_停用源"
LIB_MOVIES = "nlh_电影库"
LIB_TV = "nlh_剧集库"
LIB_LOCAL = "nlh_本地库"

MOVIE_DEAD = "nlh_死线电影"
MOVIE_ALIVE = "nlh_活线电影"
MOVIE_MIXED = "nlh_混线电影"
MOVIE_NOMAP = "nlh_无映射电影"
MOVIE_MANUAL = "nlh_手工藏电影"
MOVIE_LOCAL = "nlh_本地库死线电影"
SERIES_DEAD = "nlh_死线剧"
SERIES_ALIVE = "nlh_活线剧"
ALL_ITEM_NAMES = [MOVIE_DEAD, MOVIE_ALIVE, MOVIE_MIXED, MOVIE_NOMAP,
                  MOVIE_MANUAL, MOVIE_LOCAL, SERIES_DEAD, SERIES_ALIVE]


def _guid() -> str:
    return uuid.uuid4().hex


def _cleanup(db: Session) -> None:
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.name.in_(ALL_ITEM_NAMES + [
            "nlh_死线剧季", "nlh_死线剧集", "nlh_活线剧季", "nlh_活线剧集",
        ])).all()
    ids = [r.id for r in rows]
    if ids:
        db.query(em.ItemHiddenLedger).filter(
            em.ItemHiddenLedger.item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.UpstreamItemMapping).filter(
            em.UpstreamItemMapping.local_item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.UserMediaData).filter(
            em.UserMediaData.item_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    for s in db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name.in_([SRC_ON, SRC_OFF])).all():
        db.delete(s)
    db.query(em.Library).filter(
        em.Library.name.in_([LIB_MOVIES, LIB_TV, LIB_LOCAL])).delete()
    u = db.query(models.WebUser).filter(
        models.WebUser.username == USER).first()
    if u is not None:
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == u.id).delete(synchronize_session=False)
        db.query(em.UserMediaData).filter(
            em.UserMediaData.user_id == u.id).delete(synchronize_session=False)
        db.query(models.StationMessage).filter(
            (models.StationMessage.to_user_id == u.id)
            | (models.StationMessage.from_user_id == u.id)
        ).delete(synchronize_session=False)
        db.delete(u)
    db.commit()


def _item(db, lib, name, item_type="movie", parent=None, series=None,
          hidden=False):
    it = em.MediaItem(guid=_guid(), library_id=lib.id, item_type=item_type,
                      name=name, parent_id=parent.id if parent else None,
                      series_id=series.id if series else None,
                      is_hidden=hidden)
    db.add(it)
    db.flush()
    return it


def _map(db, item, src, up_id):
    m = em.UpstreamItemMapping(
        local_item_id=item.id, source_id=src.id, upstream_item_id=up_id,
        confidence=0.9, evidence="tmdb")
    db.add(m)
    db.flush()
    return m


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)
    user = models.WebUser(username=USER, password_hash=hash_password(PW),
                          is_active=True, is_staff=True, emby_username=USER)
    setattr(user, "emby_password", PW)
    db.add(user)
    db.flush()

    def _src(name, enabled):
        s = em.UpstreamSource(
            name=name, source_type="applecms",
            base_url=f"https://{name}.example.com", auth_method="api_key",
            enabled=enabled)
        db.add(s)
        db.flush()
        return s

    src_on = _src(SRC_ON, True)
    src_off = _src(SRC_OFF, False)

    def _lib(name, kind, ctype):
        lib = em.Library(guid=_guid(), name=name, kind=kind,
                         collection_type=ctype, is_enabled=True,
                         upstream_source_ids=f"{src_on.id},{src_off.id}")
        db.add(lib)
        db.flush()
        return lib

    lib_m = _lib(LIB_MOVIES, "applecms", "movies")
    lib_tv = _lib(LIB_TV, "applecms", "tvshows")
    lib_local = _lib(LIB_LOCAL, "local", "movies")

    items = {}
    items["dead"] = _item(db, lib_m, MOVIE_DEAD)
    _map(db, items["dead"], src_off, "vod-dead")
    items["alive"] = _item(db, lib_m, MOVIE_ALIVE)
    _map(db, items["alive"], src_on, "vod-alive")
    items["mixed"] = _item(db, lib_m, MOVIE_MIXED)
    _map(db, items["mixed"], src_off, "vod-mix-off")
    _map(db, items["mixed"], src_on, "vod-mix-on")
    items["nomap"] = _item(db, lib_m, MOVIE_NOMAP)
    items["manual"] = _item(db, lib_m, MOVIE_MANUAL, hidden=True)
    _map(db, items["manual"], src_off, "vod-manual")
    items["local"] = _item(db, lib_local, MOVIE_LOCAL)
    _map(db, items["local"], src_off, "vod-local")

    s_dead = _item(db, lib_tv, SERIES_DEAD, item_type="series")
    season_d = _item(db, lib_tv, "nlh_死线剧季", item_type="season",
                     parent=s_dead, series=s_dead)
    ep_d = _item(db, lib_tv, "nlh_死线剧集", item_type="episode",
                 parent=season_d, series=s_dead)
    _map(db, ep_d, src_off, "ep-dead")
    items["series_dead"] = s_dead
    items["season_dead"] = season_d
    items["episode_dead"] = ep_d

    s_alive = _item(db, lib_tv, SERIES_ALIVE, item_type="series")
    season_a = _item(db, lib_tv, "nlh_活线剧季", item_type="season",
                     parent=s_alive, series=s_alive)
    ep_a = _item(db, lib_tv, "nlh_活线剧集", item_type="episode",
                 parent=season_a, series=s_alive)
    _map(db, season_a, src_off, "season-off-only")
    _map(db, ep_a, src_on, "ep-alive")
    items["series_alive"] = s_alive

    # 收藏/已看：活线片与死线片各一条用户数据
    for key, fav, played in (("alive", True, True), ("dead", True, True)):
        db.add(em.UserMediaData(
            user_id=user.id, item_id=items[key].id,
            is_favorite=fav, played=played, play_count=1 if played else 0))
    db.commit()
    data = {"user_id": user.id, "src_on": src_on.id, "src_off": src_off.id,
            "ids": {k: v.id for k, v in items.items()},
            "guids": {k: v.guid for k, v in items.items()}}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def token(client, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": USER, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="NLHTest", Device="PC", '
                 'DeviceId="nlh-dev-1", Version="1.0"'})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token):
    return {"X-Emby-Token": token}


def _fresh(item_id):
    db = SessionLocal()
    try:
        row = db.get(em.MediaItem, item_id)
        return row.is_hidden if row else None
    finally:
        db.close()


def _ledger_rows(item_id=None):
    db = SessionLocal()
    try:
        q = db.query(em.ItemHiddenLedger)
        if item_id is not None:
            q = q.filter(em.ItemHiddenLedger.item_id == item_id)
        return [(r.item_id, r.reason, r.restored_at is not None)
                for r in q.all()]
    finally:
        db.close()


def _reconcile():
    db = SessionLocal()
    try:
        return nlh.reconcile_no_line_hidden(db)
    finally:
        db.close()


def test_reconcile_hides_only_no_line_top_items(seed):
    stats = _reconcile()
    ids = seed["ids"]
    assert _fresh(ids["dead"]) is True
    assert _fresh(ids["series_dead"]) is True
    # 季/集行不直接动
    assert _fresh(ids["season_dead"]) is False
    assert _fresh(ids["episode_dead"]) is False
    # 有活线/混线/无映射/本地库/手工藏一律不动
    for key in ("alive", "mixed", "nomap", "local", "series_alive"):
        assert _fresh(ids[key]) is False, key
    assert _fresh(ids["manual"]) is True  # 本来就是藏的，对账不 claimed
    rows = _ledger_rows()
    assert {r[0] for r in rows} == {ids["dead"], ids["series_dead"]}
    assert all(r[1] == nlh.REASON_NO_ENABLED_LINE for r in rows)
    assert all(r[2] is False for r in rows)
    assert stats["hidden"] == 2


def test_reconcile_idempotent(seed):
    stats = _reconcile()
    assert stats["hidden"] == 0 and stats["restored"] == 0
    assert len(_ledger_rows()) == 2


def test_restore_via_reconcile_when_line_appears(seed):
    # 外部变化：停用源被重新启用（直改 DB，对账兜底解藏）
    db = SessionLocal()
    src = db.get(em.UpstreamSource, seed["src_off"])
    src.enabled = True
    db.commit()
    db.close()
    try:
        stats = _reconcile()
        ids = seed["ids"]
        assert stats["restored"] == 2
        assert _fresh(ids["dead"]) is False
        assert _fresh(ids["series_dead"]) is False
        # 手工藏的不是台账在册条目：线回来了也不许被解藏
        assert _fresh(ids["manual"]) is True
        rows = _ledger_rows()
        assert len(rows) == 2 and all(r[2] is True for r in rows)
    finally:
        db = SessionLocal()
        src = db.get(em.UpstreamSource, seed["src_off"])
        src.enabled = False
        db.commit()
        db.close()


def test_enable_source_hook_restores_immediately(seed):
    stats = _reconcile()
    assert stats["hidden"] == 2  # 源又停用了，重新藏回去（新台账行）
    db = SessionLocal()
    try:
        src = db.get(em.UpstreamSource, seed["src_off"])
        us.enable_source(db, src)
        db.commit()
    finally:
        db.close()
    ids = seed["ids"]
    assert _fresh(ids["dead"]) is False
    assert _fresh(ids["series_dead"]) is False
    open_rows = [r for r in _ledger_rows() if not r[2]]
    assert open_rows == []
    # 收尾：把源停回去并再对账一次，恢复种子终态供后续用例
    db = SessionLocal()
    try:
        src = db.get(em.UpstreamSource, seed["src_off"])
        us.disable_source(db, src)
        db.commit()
    finally:
        db.close()
    _reconcile()
    assert _fresh(ids["dead"]) is True


def test_favorites_exclude_hidden(client, seed, token):
    _reconcile()  # 保证死线片处于已藏态（幂等）
    r = client.get("/emby/Users/me/FavoriteItems", headers=_h(token))
    assert r.status_code == 200, r.text
    got = {i["Id"] for i in r.json()["Items"]}
    assert seed["guids"]["alive"] in got
    assert seed["guids"]["dead"] not in got
    assert r.json()["TotalRecordCount"] == 1


def test_played_exclude_hidden(client, seed, token):
    _reconcile()  # 保证死线片处于已藏态（幂等）
    r = client.get("/emby/Users/me/PlayedItems", headers=_h(token))
    assert r.status_code == 200, r.text
    got = {i["Id"] for i in r.json()["Items"]}
    assert seed["guids"]["alive"] in got
    assert seed["guids"]["dead"] not in got
    assert r.json()["TotalRecordCount"] == 1


def test_sync_round_end_runs_reconcile(seed, monkeypatch):
    # 先把死线片解藏成可见态，验证同步轮收尾对账会重新藏掉
    db = SessionLocal()
    try:
        row = db.get(em.MediaItem, seed["ids"]["dead"])
        row.is_hidden = False
        db.query(em.ItemHiddenLedger).filter(
            em.ItemHiddenLedger.item_id == row.id,
            em.ItemHiddenLedger.restored_at.is_(None),
        ).update({em.ItemHiddenLedger.restored_at: datetime.now()},
                 synchronize_session=False)
        db.commit()
    finally:
        db.close()
    from backend.emby_server import applecms_library_sync as als
    monkeypatch.setenv("APPLECMS_LIBRARY_SYNC_ENABLED", "1")
    monkeypatch.setattr(als, "sync_library",
                        lambda db, library, **kw: {"blocked": "stub"})
    results = als.run_library_sync_round(session_factory=SessionLocal)
    assert results  # 至少跑了种子里的无界库
    assert _fresh(seed["ids"]["dead"]) is True

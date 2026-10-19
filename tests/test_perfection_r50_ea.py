"""r50 EA 兼容层深挖修复回归（16 项）。

口径原则「是我们兼容人家」：字段/分页/计数/会话语义对标真实 Emby 客户端行为。
测试口径：路由级（真实 token 调接口）为主，纯函数用单元测试；静态断言只用于
字面量级改动（沿用 r46 先例）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server.auth import _token_from_request, issue_token
from backend.emby_server.api import _default_audio_stream_index, _chapters_for_item
from backend.emby_server.compat_routes import _apply_play_progress, _get_or_create_umd

init_db()

PW = "r50-ea-pw"
UNAME = "r50_alice"


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
    for stale in db.query(em.Library).filter(em.Library.name.like("r50兼容%")).all():
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == stale.id).all()]
        if ids:
            db.query(em.ItemFacet).filter(em.ItemFacet.item_id.in_(ids)).delete(
                synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == stale.id).delete()
    db.commit()
    user = models.WebUser(username=UNAME, password_hash="x", is_active=True,
                          is_staff=True,  # 管理员绕过付费墙，覆盖 PlaybackInfo
                          emby_username="r50_emby_alice",
                          emby_password=PW)  # 明文：verify_emby_password 兼容明文
    db.add(user)
    lib = em.Library(guid=_guid(), name="r50兼容库", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()

    def mk(item_type, name, **kw):
        it = em.MediaItem(guid=_guid(), library_id=lib.id,
                          item_type=item_type, name=name, **kw)
        db.add(it)
        db.flush()
        return it

    m1 = mk("movie", "r50zz搜片甲 (2024)", file_path="/tmp/none/r50-m1.mp4",
            container="mp4", sort_name="r50zz搜片甲", production_year=2024,
            duration_ticks=600_000_000)
    m2 = mk("movie", "r50zz搜片乙 (2024)", file_path="/tmp/none/r50-m2.mp4",
            container="mp4", sort_name="r50zz搜片乙", production_year=2024,
            duration_ticks=600_000_000)
    series = mk("series", "r50剧 (2023)", file_path="/tmp/none/r50-show",
                sort_name="r50剧", production_year=2023)
    s1 = mk("season", "Season 01", series_id=series.id, season_number=1)
    s2 = mk("season", "Season 02", series_id=series.id, season_number=2)
    e1 = mk("episode", "r50剧 S01E01", series_id=series.id, parent_id=s1.id,
            season_number=1, episode_number=1, file_path="/tmp/none/r50-e1.mp4",
            container="mp4", duration_ticks=600_000_000)
    e2 = mk("episode", "r50剧 S01E02", series_id=series.id, parent_id=s1.id,
            season_number=1, episode_number=2, file_path="/tmp/none/r50-e2.mp4",
            container="mp4", duration_ticks=600_000_000)
    # NextUp 种子：e1 已看 → 下一部是 e2
    db.add(em.UserMediaData(user_id=user.id, item_id=e1.id, played=True,
                            play_count=1, last_played_at=datetime.now()))
    db.commit()
    data = {"user_id": user.id, "lib": lib.guid, "m1": m1.guid, "m2": m2.guid,
            "series": series.guid, "e1": e1.guid, "e2": e2.guid,
            "e1_id": e1.id, "e2_id": e2.id}
    db.close()
    yield data
    try:
        db = SessionLocal()
        row = db.query(em.Library).filter(em.Library.guid == data["lib"]).first()
        if row is not None:
            ids = [r[0] for r in db.query(em.MediaItem.id)
                   .filter(em.MediaItem.library_id == row.id).all()]
            if ids:
                db.query(em.ItemFacet).filter(em.ItemFacet.item_id.in_(ids)).delete(
                    synchronize_session=False)
                db.query(em.UserMediaData).filter(
                    em.UserMediaData.item_id.in_(ids)).delete(synchronize_session=False)
                db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(em.Library).filter(em.Library.id == row.id).delete()
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == data["user_id"]).delete(synchronize_session=False)
        db.query(em.PlaybackSession).filter(
            em.PlaybackSession.user_id == data["user_id"]).delete(synchronize_session=False)
        db.query(em.UserMediaData).filter(
            em.UserMediaData.user_id == data["user_id"]).delete(synchronize_session=False)
        db.query(models.WebUser).filter(
            models.WebUser.id == data["user_id"]).delete()
        db.commit()
        db.close()
    except Exception:
        db.rollback()
        db.close()


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
                 'MediaBrowser Client="R50Test", Device="PC", '
                 'DeviceId="r50-dev-1", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token):
    return {"X-Emby-Token": token}


def _mk_request(headers=None, query=""):
    return Request({"type": "http",
                    "headers": [(k.lower().encode(), v.encode())
                                for k, v in (headers or {}).items()],
                    "query_string": query.encode()})


# ---------- F-a：官方 Token= 键值提取 ----------

def test_token_from_official_header_kv():
    req = _mk_request({"X-Emby-Authorization":
        'MediaBrowser Client="Infuse", Device="iPhone", DeviceId="abc", '
        'Version="7.0", Token="tok_abc_xyz"'})
    assert _token_from_request(req) == "tok_abc_xyz"


def test_token_explicit_header_wins_over_kv():
    req = _mk_request({"X-Emby-Token": "explicit_tok",
                       "X-Emby-Authorization":
        'MediaBrowser Client="Infuse", Token="kv_tok"'})
    assert _token_from_request(req) == "explicit_tok"


# ---------- F-j：issue_token docstring 诚实 ----------

def test_issue_token_docstring_no_reuse_claim():
    doc = issue_token.__doc__ or ""
    # 不得再宣称"幂等/同设备复用"（实现是每次登录签发新行，与官方一致）
    assert "幂等" not in doc
    assert "每次登录签发新行" in doc


# ---------- F-k / F-l：登录响应的能力宣称与用户名口径 ----------

def test_login_session_info_honest(client, seed, token):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="R50Test2", Device="PC", '
                 'DeviceId="r50-dev-2", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    si = body["SessionInfo"]
    assert si["SupportsRemoteControl"] is False
    assert si["SupportsMediaControl"] is False
    assert si["UserName"] == body["User"]["Name"]


# ---------- F-b：收藏响应带 ItemId/Key ----------

def test_favorite_returns_itemid_key(client, seed, token):
    r = client.post(f"/emby/Users/me/FavoriteItems/{seed['m1']}", headers=_h(token))
    assert r.status_code == 200, r.text
    ud = r.json()
    assert ud["ItemId"] == seed["m1"]
    assert ud["Key"] == seed["m1"]
    assert ud["IsFavorite"] is True
    r = client.delete(f"/emby/Users/me/FavoriteItems/{seed['m1']}", headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.json()["IsFavorite"] is False


# ---------- F-c：Search/Hints 总数 ----------

def test_search_hints_total_is_full_count(client, seed, token):
    r = client.get("/emby/Search/Hints",
                   params={"SearchTerm": "r50zz搜片", "Limit": 1},
                   headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["TotalRecordCount"] == 2
    assert len(body["SearchHints"]) == 1


# ---------- F-d：收藏/已看列表分页 ----------

def test_favorites_pagination(client, seed, token):
    for g in (seed["m1"], seed["m2"]):
        r = client.post(f"/emby/Users/me/FavoriteItems/{g}", headers=_h(token))
        assert r.status_code == 200, r.text
    r = client.get("/emby/Users/me/FavoriteItems",
                   params={"Limit": 1}, headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["TotalRecordCount"] == 2
    assert len(body["Items"]) == 1
    assert body["StartIndex"] == 0
    r2 = client.get("/emby/Users/me/FavoriteItems",
                    params={"Limit": 1, "StartIndex": 1}, headers=_h(token))
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["Items"]) == 1
    assert r2.json()["Items"][0]["Id"] != body["Items"][0]["Id"]
    for g in (seed["m1"], seed["m2"]):
        client.delete(f"/emby/Users/me/FavoriteItems/{g}", headers=_h(token))


# ---------- F-e：NextUp 分页与总数 ----------

def test_nextup_startindex_and_total(client, seed, token):
    r = client.get("/emby/Shows/NextUp", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["TotalRecordCount"] == 1
    assert len(body["Items"]) == 1
    assert body["Items"][0]["Id"] == seed["e2"]
    r2 = client.get("/emby/Shows/NextUp",
                    params={"StartIndex": 1}, headers=_h(token))
    assert r2.status_code == 200, r2.text
    assert r2.json()["TotalRecordCount"] == 1
    assert r2.json()["Items"] == []


# ---------- F-f：Seasons / Episodes 分页 ----------

def test_seasons_pagination(client, seed, token):
    r = client.get(f"/emby/Shows/{seed['series']}/Seasons",
                   params={"Limit": 1}, headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["TotalRecordCount"] == 2
    assert len(body["Items"]) == 1


def test_episodes_pagination(client, seed, token):
    r = client.get(f"/emby/Shows/{seed['series']}/Episodes",
                   params={"Limit": 1}, headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["TotalRecordCount"] == 2
    assert len(body["Items"]) == 1


# ---------- F-g：未知 SortBy 记日志 ----------

def test_unknown_sortby_warns_but_falls_back(client, seed, token, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="backend.emby_server.api"):
        r = client.get("/emby/Items", params={"SortBy": "BogusKeyR50"},
                       headers=_h(token))
    assert r.status_code == 200, r.text
    assert any("BogusKeyR50" in m for m in caplog.messages)


# ---------- F-h：Persons 畸形 Limit 回 default ----------

def test_persons_malformed_limit_no_422(client, seed, token):
    r = client.get("/emby/Persons", params={"Limit": "abc"}, headers=_h(token))
    assert r.status_code == 200, r.text


# ---------- F-i：显式 IncludeItemTypes=Episode ----------

def test_include_episode_types_not_swallowed(client, seed, token):
    r = client.get("/emby/Items", params={"IncludeItemTypes": "Episode"},
                   headers=_h(token))
    assert r.status_code == 200, r.text
    items = r.json()["Items"]
    assert len(items) >= 2
    assert all(i["Type"] == "Episode" for i in items)


# ---------- F-m：Stopped 落库最终进度 ----------

def test_stopped_writes_final_progress(client, seed, token):
    dev_h = dict(_h(token))
    dev_h["X-Emby-Authorization"] = ('MediaBrowser Client="R50Test", Device="PC", '
                                     'DeviceId="r50-dev-stop", Version="1.0"')
    r = client.post("/emby/Sessions/Playing",
                    json={"ItemId": seed["e2"], "PositionTicks": 1000},
                    headers=dev_h)
    assert r.status_code == 200, r.text
    # 只发 Stopped（模拟最后一次 Progress 丢失）：断点必须落库
    r = client.post("/emby/Sessions/Playing/Stopped",
                    json={"ItemId": seed["e2"], "PositionTicks": 5000},
                    headers=dev_h)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == seed["user_id"],
        em.UserMediaData.item_id == seed["e2_id"]).first()
    db.close()
    assert umd is not None
    assert umd.playback_position_ticks == 5000


def test_stopped_finished_marks_played_once(client, seed, token):
    dev_h = dict(_h(token))
    dev_h["X-Emby-Authorization"] = ('MediaBrowser Client="R50Test", Device="PC", '
                                     'DeviceId="r50-dev-stop2", Version="1.0"')
    # e2 时长 600_000_000 ticks，550_000_000 >= 90% → 自动已看
    for _ in range(2):  # 重复 Stopped：play_count 只加一次（幂等）
        r = client.post("/emby/Sessions/Playing/Stopped",
                        json={"ItemId": seed["e2"], "PositionTicks": 550_000_000},
                        headers=dev_h)
        assert r.status_code == 200, r.text
    db = SessionLocal()
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == seed["user_id"],
        em.UserMediaData.item_id == seed["e2_id"]).first()
    db.close()
    assert umd.played is True
    assert umd.play_count == 1
    assert umd.playback_position_ticks == 0


def test_apply_play_progress_unit():
    umd = em.UserMediaData(user_id=1, item_id=2)
    _apply_play_progress(umd, 1234, False)
    assert umd.playback_position_ticks == 1234
    assert not umd.played  # 瞬态对象列默认 None；落库后为 False
    _apply_play_progress(umd, 950, True)
    assert umd.played is True and umd.play_count == 1
    assert umd.playback_position_ticks == 0
    _apply_play_progress(umd, 950, True)  # 已标已看：不再重复加 play_count
    assert umd.play_count == 1


# ---------- F-n / F-o / F-p：PlaybackInfo 字段 ----------

def test_playback_info_direct_play_url_and_chapters(client, seed, token):
    r = client.post(f"/emby/Items/{seed['m1']}/PlaybackInfo", json={},
                    headers=_h(token))
    assert r.status_code == 200, r.text
    ms = r.json()["MediaSources"][0]
    assert ms["DirectPlayUrl"] == ms["DirectStreamUrl"]
    assert ms["SupportsDirectPlay"] is True
    assert "Chapters" in ms  # list（无标记时为 []）


def test_default_audio_stream_index_unit():
    streams = [
        {"Type": "Video", "Index": 0, "IsDefault": True},
        {"Type": "Audio", "Index": 1, "IsDefault": False},
        {"Type": "Audio", "Index": 2, "IsDefault": True},
        {"Type": "Subtitle", "Index": 3, "IsDefault": True},
    ]
    assert _default_audio_stream_index(streams) == 2
    no_default = [dict(s, IsDefault=False) for s in streams]
    assert _default_audio_stream_index(no_default) == 1
    assert _default_audio_stream_index([]) is None
    assert _default_audio_stream_index([{"Type": "Video", "Index": 0}]) is None


def test_chapters_helper_fail_safe():
    class Boom:
        id = 1
        item_type = "movie"

    class BadDB:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    assert _chapters_for_item(BadDB(), Boom()) == []
